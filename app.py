import ctypes
import json
import platform
import plistlib
from dataclasses import replace
from pathlib import Path
from queue import Empty, Queue
import re
import shutil
import subprocess
import sys
import tempfile
from threading import Lock, Thread
import webbrowser
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk

BASE = Path(__file__).resolve().parent
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(BASE / "vendor"))

from PIL import Image, ImageTk
from tkinterdnd2 import DND_FILES, TkinterDnD
from audio_convert import LABELS
from coverflow import CoverFlow
from device_identity import describe
import diagnostics
import encoders
from import_dialog import AUDIO_TYPES, ImportDialog, NewPlaylist, playlist_targets
import theme
from library import (DB, Device, editable_playlist, is_music, listening, move_playlist_entries, playlist_edit, rename_device,
                     rename_playlist)
from native_covers import device_covers, load_cover
from transfer import STAGE, export_tracks, recover_interrupted, transfer
from updates import PROJECT_URL, Release, check_for_updates, download_update, issue_url, plain_notes, release_history, version_key
from version import __version__
import cpuarch
import system

BACKUPS = system.DATA / "Backups"
SETTINGS = BACKUPS.with_name("settings.json")
MOD = "Command" if system.MAC else "Control"


def discover():
    return [d for d in system.volumes() if (d / DB).is_file()]


def _disabled(window):
    # "-disabled" is a Windows-only wm attribute; macOS gets modality from grab_set() in the dialog.
    return system.WINDOWS and window.attributes("-disabled")


class App:
    def __init__(self, window, browse="all", settings=None, check_updates=False):
        """settings is the file that remembers the theme and browsing mode. Without it nothing is read or saved.

        check_updates asks GitHub for a newer release shortly after start.
        """
        self.window = window
        self.latest_update = self.update_dialog = self.feedback_dialog = None
        self.history_stamp = 0
        self.settings = settings
        saved = {}
        if settings:
            try:
                saved = json.loads(Path(settings).read_text(encoding="utf8"))
            except (OSError, ValueError):
                pass
        saved = saved if isinstance(saved, dict) else {}
        theme.use(saved.get("theme") if saved.get("theme") in theme.THEMES else "light")
        if saved.get("browse") in ("all", "album", "artist"):
            browse = saved["browse"]
        px = self.px = theme.scaler(window)
        window.title("ClassicDrop · iPod 音乐管理")
        width = min(px(1100), window.winfo_screenwidth() - px(28))
        height = min(px(780), window.winfo_screenheight() - px(68))
        window.geometry(f"{width}x{height}")
        window.minsize(min(width, px(810)), min(height, px(505)))
        if (BASE / "ClassicDrop.ico").exists():
            if system.WINDOWS:
                window.iconbitmap(default=str(BASE / "ClassicDrop.ico"))
            theme.ICON = BASE / "ClassicDrop.ico"
        window.option_add("*Font", (theme.FONT, 10))
        self.device = None
        self.pending = {}
        self.queue_target = None
        self.target_keys = [None]
        self.view = "library"
        self.browse = browse
        self.group = self.album = None
        self.album_rows = {}
        self.group_keys = []
        self.group_shown = None
        self.covers, self.cover_reads, self.sharp_covers, self.cover_thumbs = {}, {}, {}, {}
        self.nav_shown = self.columns_shown = self.usage = None
        self.listening = {}
        self.pending_ratings = {}
        self.rating_timer = self.hover_row = self._slot_glyphs = None
        self.cover_images = {}
        self.cover_pending = set()
        self.cover_stamp = 0
        self.cover_lock = Lock()
        self.busy = False
        self.events = Queue()
        self.search = tk.StringVar()
        self.status = tk.StringVar(value="连接 iPod 后点「刷新」，或选择 iPod 的根目录。")
        self.render_version = 0
        self.search_after = self.progress_after = None
        self.keys = []
        self.rows = {}
        self._layout()
        self.search.trace_add("write", self._search_changed)
        self.render()
        window.after(65, self._poll)
        window.after(120, theme.title_bar, window)
        if check_updates:
            window.after(1500, self.check_updates)
        window.protocol("WM_DELETE_WINDOW", self.close)
        window.report_callback_exception = self._callback_error

    def _callback_error(self, kind, exc, tb):
        diagnostics.remember(str(exc) or kind.__name__, exc)

    def _layout(self):
        w = self.window
        px = self.px
        theme.apply(w, px)
        w.configure(bg=theme.SURFACE)
        w.columnconfigure(1, weight=1)
        w.rowconfigure(0, weight=1)
        side = tk.Frame(w, width=px(210), bg=theme.SIDE)
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_propagate(False)
        side.columnconfigure(0, weight=1)
        side.rowconfigure(3, weight=1)
        card = tk.Frame(side, bg=theme.SURFACE, highlightthickness=1, highlightbackground=theme.LINE)
        card.grid(row=1, sticky="ew", padx=px(14), pady=px(20, 0))
        card.columnconfigure(1, weight=1)
        self.players = {}
        self.device_picture = tk.Label(card, bg=theme.SURFACE)
        self.device_picture.grid(row=0, column=0, rowspan=3, sticky="n", padx=px(11, 0), pady=px(11, 0))
        self.device_name = tk.Label(card, text="尚未连接", font=(theme.FONT, 12, "bold"), bg=theme.SURFACE, fg=theme.NAVY,
                                    wraplength=px(112), anchor="w", justify="left")
        self.device_name.grid(row=0, column=1, sticky="ew", padx=px(9, 10), pady=px(9, 1))
        self.device_name.bind("<Double-1>", lambda _: self.change_device_name())
        state = tk.Frame(card, bg=theme.SURFACE)
        state.grid(row=1, column=1, sticky="ew", padx=px(10, 10))
        self.device_model = tk.Label(card, bg=theme.SURFACE, fg=theme.MUTED, font=(theme.FONT, 8), anchor="w", justify="left")
        self.device_model.grid(row=2, column=1, sticky="new", padx=px(10, 10), pady=px(2, 0))
        self.device_badge = tk.Label(state, font=(theme.FONT, 8), padx=px(6), pady=px(1))
        self.device_info = tk.Label(state, bg=theme.SURFACE, fg=theme.MUTED, font=(theme.FONT, 9))
        self.device_info.pack(side="left")
        self.capacity = tk.Frame(card, bg=theme.SURFACE)
        self.capacity.grid(row=3, columnspan=2, sticky="ew", padx=px(12), pady=px(9, 0))
        track = tk.Frame(self.capacity, bg=theme.LINE, height=px(4))
        track.pack(fill="x")
        self.capacity_fill = tk.Frame(track, bg=theme.BLUE)
        self.capacity_text = tk.Label(self.capacity, bg=theme.SURFACE, fg=theme.MUTED, font=(theme.FONT, 8))
        self.capacity_text.pack(anchor="w", pady=px(3, 0))
        device_actions = tk.Frame(card, bg=theme.SURFACE)
        device_actions.grid(row=4, columnspan=2, sticky="ew", padx=px(11), pady=px(10, 11))
        device_actions.columnconfigure(0, weight=1)
        ttk.Button(device_actions, text="选择设备", command=self.pick_device).grid(row=0, column=0, sticky="ew", padx=px(0, 5))
        ttk.Button(device_actions, text="刷新", command=self.refresh).grid(row=0, column=1)
        self.eject_button = ttk.Button(device_actions, text="弹出设备", command=self.eject)
        self.eject_button.grid(row=1, columnspan=2, sticky="ew", pady=px(5, 0))
        tk.Label(side, text="资料库", font=(theme.FONT, 9), bg=theme.SIDE, fg=theme.MUTED).grid(row=2, sticky="w", padx=px(20), pady=px(16, 0))
        self.nav = ttk.Treeview(side, style="Nav.Treeview", show="tree", selectmode="browse", columns=("count",))
        self.nav.column("#0", stretch=True, minwidth=px(60))
        self.nav.column("count", width=px(38), stretch=False, anchor="e")
        self.nav.tag_configure("group", foreground=theme.MUTED, font=(theme.FONT, 9))
        self.nav.grid(row=3, sticky="nsew", padx=px(10), pady=px(5, 10))
        self.nav.bind("<<TreeviewSelect>>", self.navigate)
        theme.hover_rows(self.nav, theme.NAV_HOVER)
        self.new_list = ttk.Button(side, text="＋ 新建播放列表", command=self.change_playlist)
        self.new_list.grid(row=4, sticky="ew", padx=px(18), pady=px(0, 4))
        self.tools_button = ttk.Button(side, text="工具与备份 ▾", command=self.tools_menu)
        self.tools_button.grid(row=5, sticky="ew", padx=px(18), pady=px(3, 13))
        body = tk.Frame(w, bg=theme.SURFACE)
        body.grid(row=0, column=1, sticky="nsew", padx=px(22), pady=px(20, 16))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        head = tk.Frame(body, bg=theme.SURFACE)
        head.grid(row=0, sticky="ew")
        self.heading = tk.Label(head, font=(theme.FONT, 19, "bold"), bg=theme.SURFACE, fg=theme.NAVY)
        self.heading.pack(side="left")
        self.counter = tk.Label(head, font=(theme.FONT, 10), bg=theme.SURFACE, fg=theme.MUTED)
        self.counter.pack(side="left", padx=px(10), anchor="s", pady=px(4))
        self.find = ttk.Entry(head, textvariable=self.search, width=30)
        self.find.pack(side="right")
        self.find_hint = theme.Placeholder(self.find, self.search, "搜索歌名、歌手、专辑", px)
        tools = tk.Frame(body, bg=theme.SURFACE)
        tools.grid(row=1, sticky="ew", pady=px(14, 9))
        tools.columnconfigure(0, weight=1)
        self.browse_bar = tk.Frame(tools, bg=theme.SURFACE)
        self.browse_bar.grid(row=0, column=1)
        self.browse_buttons = {}
        for mode, label in (("all", "全部"), ("album", "专辑"), ("artist", "歌手")):
            button = ttk.Button(self.browse_bar, text=label, style="Tool.TButton", command=lambda m=mode: self.set_browse(m))
            button.pack(side="left", padx=px(0, 2))
            self.browse_buttons[mode] = button
        self.actions = ttk.Button(tools, text="选中项操作 ▾", command=self.action_menu)
        self.actions.grid(row=0, column=2, padx=px(12, 0))
        self.queue_bar = tk.Frame(tools, bg=theme.SURFACE)
        self.queue_bar.grid(row=0, column=0, sticky="w")
        self.preview_button = ttk.Button(self.queue_bar, text="预览与编辑", command=self.edit_pending)
        self.preview_button.pack(side="left")
        tk.Label(self.queue_bar, text="传入位置", bg=theme.SURFACE, fg=theme.MUTED).pack(side="left", padx=px(12, 5))
        self.target = ttk.Combobox(self.queue_bar, state="readonly", width=19)
        self.target.pack(side="left", fill="x")
        self.target.bind("<<ComboboxSelected>>", self.choose_target)
        self.queue_new_list = ttk.Button(self.queue_bar, text="新建播放列表", command=lambda: self.edit_pending(new_playlist=True))
        self.queue_new_list.pack(side="left", padx=px(5))
        self.list_bar = tk.Frame(tools, bg=theme.SURFACE)
        self.list_bar.grid(row=0, column=0, sticky="w")
        for label, func in (("上移", lambda: self.manage_list("up")), ("下移", lambda: self.manage_list("down")),
                            ("移出歌单", lambda: self.change_playlist(self.view, True)),
                            ("改名", lambda: self.manage_list("rename")), ("删除歌单", lambda: self.manage_list("delete"))):
            ttk.Button(self.list_bar, text=label, style="Tool.TButton", command=func).pack(side="left", padx=px(0, 5))
        self.add_to_list = ttk.Button(tools, text="加入播放列表 ▾", command=self.playlist_menu)
        self.add_to_list.grid(row=0, column=0, sticky="w")
        content = tk.Frame(body, bg=theme.SURFACE)
        content.grid(row=2, sticky="nsew")
        content.columnconfigure(1, weight=1)
        content.rowconfigure(0, weight=1)
        self.group_pane = tk.Frame(content, bg=theme.LINE, padx=1, pady=1, width=px(236))
        self.group_pane.grid(row=0, column=0, sticky="nsew", padx=px(0, 10))
        self.group_pane.grid_propagate(False)
        self.group_pane.columnconfigure(0, weight=1)
        self.group_pane.rowconfigure(0, weight=1)
        self.groups = ttk.Treeview(self.group_pane, style="Group.Treeview", show="tree", selectmode="browse")
        group_scroll = ttk.Scrollbar(self.group_pane, orient="vertical", command=self.groups.yview)
        self.groups.configure(yscrollcommand=group_scroll.set)
        self.groups.grid(row=0, column=0, sticky="nsew")
        group_scroll.grid(row=0, column=1, sticky="ns")
        self.groups.bind("<<TreeviewSelect>>", self.choose_group)
        theme.hover_rows(self.groups, theme.STRIPE)
        self.no_cover = ImageTk.PhotoImage(theme.blank_cover(px(40)), master=w)
        right = tk.Frame(content, bg=theme.SURFACE)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        self.flow = CoverFlow(right, px, picture=self._flow_picture, on_pick=self.choose_album, wanted=self._want_covers)
        self.flow.grid(row=0, sticky="ew", pady=px(0, 10))
        holder = tk.Frame(right, bg=theme.LINE, padx=1, pady=1)
        holder.grid(row=1, sticky="nsew")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(holder, show="headings", selectmode="extended")
        bar = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.tag_configure("odd", background=theme.STRIPE)
        self.tree.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        self.empty = tk.Label(self.tree, bg=theme.SURFACE, fg=theme.MUTED, justify="center", font=(theme.FONT, 11))
        self.tree.bind("<Button-1>", self._rating_click)
        self.tree.bind("<Motion>", self._rating_hover)
        self.tree.bind("<Leave>", lambda _: self._rating_hover(None))
        if system.MAC:
            self.tree.bind("<Button-2>", self.context_click)
            self.tree.bind("<Control-Button-1>", self.context_click)
        else:
            self.tree.bind("<Button-3>", self.context_click)
        self.tree.bind("<Double-1>", lambda _: self.edit_pending() if self.view == "queue" else self.edit_tags())
        self.tree.bind(f"<{MOD}-a>", self.select_all)
        self.tree.bind("<Delete>", self.delete_key)
        self.tree.bind("<<TreeviewSelect>>", self._picked)
        w.bind(f"<{MOD}-f>", lambda _: self.find.focus_set())
        w.bind(f"<{MOD}-o>", lambda _: self.pick_files())
        w.bind(f"<{MOD}-Return>", lambda _: self.send())
        w.bind("<F5>", lambda _: self.refresh())
        w.bind("<F2>", lambda _: self.change_device_name())
        for item in (w, self.tree, self.empty):
            item.drop_target_register(DND_FILES)
            item.dnd_bind("<<Drop>>", self.drop_files)
        foot = tk.Frame(body, bg=theme.SURFACE)
        foot.grid(row=3, sticky="ew", pady=px(12, 0))
        ttk.Button(foot, text="添加歌曲", command=self.pick_files).pack(side="left")
        ttk.Button(foot, text="导入文件夹", command=self.pick_folder).pack(side="left", padx=px(6, 0))
        self.send_button = ttk.Button(foot, style="Accent.TButton", command=self.send)
        self.send_button.pack(side="right")
        self.progress = ttk.Progressbar(foot, mode="indeterminate", length=px(180))
        statusbar = tk.Frame(w, bg=theme.BAR)
        statusbar.grid(row=1, columnspan=2, sticky="ew")
        note = self.rating_note = tk.Frame(statusbar, bg=theme.BAR)
        self.rating_count = tk.Label(note, bg=theme.BAR, fg=theme.MUTED, font=(theme.FONT, 9))
        self.rating_count.pack(side="left", padx=px(16, 0))
        undo = tk.Label(note, text="撤销", bg=theme.BAR, fg=theme.BLUE, cursor="hand2", font=(theme.FONT, 9))
        undo.pack(side="left", padx=px(6, 0))
        undo.bind("<Button-1>", lambda _: self._undo_ratings())
        tk.Label(statusbar, textvariable=self.status, bg=theme.BAR, fg=theme.MUTED, anchor="w",
                 padx=px(16), pady=px(6), font=(theme.FONT, 9)).pack(side="left", fill="x", expand=True)
        links = tk.Frame(statusbar, bg=theme.BAR)
        links.pack(side="right", padx=px(0, 14))
        def link(text, action):
            label = tk.Label(links, text=text, bg=theme.BAR, fg=theme.MUTED, cursor="hand2", font=(theme.FONT, 9))
            label.bind("<Button-1>", lambda _: action())
            label.bind("<Enter>", lambda _: label.configure(fg=theme.BLUE))
            label.bind("<Leave>", lambda _: label.configure(fg=theme.MUTED))
            return label
        self.github_link = link("GitHub", lambda: webbrowser.open(PROJECT_URL, new=2))
        self.github_link.pack(side="left", padx=px(0, 14))
        self.feedback_link = link("反馈", self.open_feedback)
        self.feedback_link.pack(side="left", padx=px(0, 14))
        self.version_link = link("v" + __version__, self.open_versions)
        self.version_link.pack(side="left")
        # Shown beside the version number while a newer release is waiting.
        self.update_dot = tk.Label(links, text="●", bg=theme.BAR, fg=theme.ALERT, cursor="hand2", font=(theme.FONT, 7))
        self.update_dot.bind("<Button-1>", lambda _: self.open_versions())
        self._offer_update()

    def _search_changed(self, *_):
        if self.search_after:
            self.window.after_cancel(self.search_after)
        def delayed():
            self.search_after = None
            self.render()
        self.search_after = self.window.after(100, delayed)

    def _rating_column(self):
        return next((f"#{i + 1}" for i, (name, _) in enumerate(self.columns_shown or ()) if name == "评级"), None)

    def _rating(self, track):
        return self.pending_ratings.get(
            track.track_id, self.listening.get(track.track_id, (track.rating, track.play_count))[0])

    _SPACE_CHARS = "            "

    @staticmethod
    def _spaces_for(target, font):
        """Space characters adding up to (or closest to) the target pixel width."""
        options = sorted(((w, c) for c in App._SPACE_CHARS if (w := font.measure(c)) > 0), reverse=True)
        out = []
        for width, char in options:
            count, target = divmod(target, width)
            out.append(char * count)
        if options and target > 0 and options[-1][0] - target <= target:
            out.append(options[-1][1])  # one narrow space overshoots less than the gap left
        return "".join(out)

    def _pad_center(self, text, width, font):
        diff = width - font.measure(text)
        if diff <= 0:
            return text
        left = self._spaces_for(diff // 2, font)
        return left + text + self._spaces_for(diff - font.measure(left), font)

    def _rating_slots(self):
        """(star width, padded dot, padded half-star): every slot is one star wide."""
        if self._slot_glyphs is None:
            font = self._rating_font()
            star = font.measure("★")
            self._slot_glyphs = star, self._pad_center("·", star, font), self._pad_center("½", star, font)
        return self._slot_glyphs

    def _rating_text(self, rating, dots=False):
        whole, half = rating // 20, rating % 20 >= 10
        if not dots:
            return "★" * whole + ("½" if half else "")
        _, dot, half_star = self._rating_slots()
        return "★" * whole + (half_star if half else "") + dot * (5 - whole - bool(half))

    def _set_rating(self, iid, dots=False):
        record = self.rows.get(iid, (None,))[0]
        column = self._rating_column()
        if column and getattr(record, "track_id", None) is not None:
            self.tree.set(iid, column, self._rating_text(self._rating(record), dots))

    def _rating_font(self):
        # Treeview rows take their font from *Font; the counter label uses the same one.
        return tkfont.Font(font=self.counter.cget("font"))

    def _rating_hover(self, event):
        iid = self.tree.identify_row(event.y) if event else ""
        if iid == self.hover_row:
            return
        old, self.hover_row = self.hover_row, iid
        if old:
            self._set_rating(old)
        if iid:
            self._set_rating(iid, dots=True)

    def _rating_slot(self, event):
        """(row iid, slot 1..5) for a click inside the rating glyphs, else (iid or None, None)."""
        iid = self.tree.identify_row(event.y)
        record = self.rows.get(iid, (None,))[0]
        if getattr(record, "track_id", None) is None or not (self.device and self.device.guid):
            return None, None
        column = self._rating_column()
        if not column or self.tree.identify_column(event.x) != column:
            return None, None
        bbox = self.tree.bbox(iid, column)
        if not bbox:
            return iid, None
        x = event.x - bbox[0] - self.px(4)
        star = self._rating_slots()[0]
        if 0 <= x < 5 * star:
            return iid, x // star + 1
        return iid, None

    def _rating_click(self, event):
        iid, slot = self._rating_slot(event)
        if slot is None:
            return
        track = self.rows[iid][0]
        rating = slot * 20
        self.pending_ratings[track.track_id] = 0 if rating == self._rating(track) else rating
        self._set_rating(iid, dots=iid == self.hover_row)
        self._show_pending()
        self._arm_rating_timer()
        return "break"

    def _show_pending(self):
        if self.pending_ratings:
            self.rating_count.configure(text=f"有 {len(self.pending_ratings)} 首歌曲的评级待保存")
            self.rating_note.pack(side="left")
        else:
            self.rating_note.pack_forget()

    def _undo_ratings(self):
        pending, self.pending_ratings = self.pending_ratings, {}
        if self.rating_timer:
            self.window.after_cancel(self.rating_timer)
            self.rating_timer = None
        for iid, (record, _) in self.rows.items():
            if getattr(record, "track_id", None) in pending:
                self._set_rating(iid, dots=iid == self.hover_row)
        self._show_pending()

    def _arm_rating_timer(self):
        if self.rating_timer:
            self.window.after_cancel(self.rating_timer)
        self.rating_timer = self.window.after(2000, self._save_ratings)

    def _save_ratings(self):
        self.rating_timer = None
        if self.busy:
            self.rating_timer = self.window.after(1000, self._save_ratings)
            return
        dev = self.device
        if not (dev and dev.guid):
            self._undo_ratings()
            return
        ids = {t.track_id for t in dev.library.snapshot.tracks}
        batch, stale = {}, set()
        for tid, rating in self.pending_ratings.items():
            if tid in ids:
                batch[tid] = rating
            else:
                stale.add(tid)
        self.pending_ratings = {}
        for iid, (record, _) in self.rows.items():
            if getattr(record, "track_id", None) in stale:
                self._set_rating(iid, dots=iid == self.hover_row)
        self._show_pending()
        if batch:
            self._write_ratings(batch, f"已保存 {len(batch)} 首歌曲的评级。")

    def _write_ratings(self, ratings, message, after=None):
        """One write for all pending stars, then a light reload: a ratings write never touches artwork."""
        dev = self.device
        selection, scroll = self.tree.selection(), self.tree.yview()[0]
        def work(report):
            transfer(dev, [], BACKUPS, report, ratings=ratings)
            new = Device.open(dev.root, dev.guid.hex())
            return new, listening(new)
        def done(result):
            new, heard = result
            self._hide_device(new)
            self.device, self.listening = new, heard
            self.render(keep_view=True)
            kept = [iid for iid in selection if self.tree.exists(iid)]
            if kept:
                self.tree.selection_set(kept)
            self.tree.yview_moveto(scroll)
            self._show_pending()
            if message:
                self.status.set(message)
            if after:
                after()
        self._work("正在保存评级…", work, done)

    def _flush_ratings(self, then):
        """Save pending ratings first, then run then(); failure keeps the batch and stops here."""
        dev = self.device
        if self.busy or not self.pending_ratings or not (dev and dev.guid):
            then()
            return
        ids = {t.track_id for t in dev.library.snapshot.tracks}
        batch = {tid: rating for tid, rating in self.pending_ratings.items() if tid in ids}
        self.pending_ratings = {}
        self._show_pending()
        if not batch:
            then()
            return
        self._write_ratings(batch, "", after=then)

    def _work(self, status, function, done):
        if self.busy:
            return
        self.busy = True
        self.status.set(status)
        self._buttons()
        self.progress_after = self.window.after(300, self._show_progress)
        diagnostics.log.info("%s", status)
        def report(text):
            diagnostics.log.info("%s", text)
            self.events.put(("status", text))
        def worker():
            with self.cover_lock:
                pass  # let a cover that is being read finish before the device is used
            try:
                result = function(report)
                self.events.put(("done", (done, result)))
            except Exception as exc:
                message = str(exc) or type(exc).__name__
                diagnostics.remember(message, exc)
                self.events.put(("error", message))
        Thread(target=worker, daemon=True).start()

    def _show_error(self, title, message):
        messagebox.showerror(title, f"{message}\n\n如需反馈，点窗口右下角的「反馈」，报错信息会自动附上。",
                             parent=self.window)

    def _fail(self, title, message, exc=None):
        diagnostics.remember(message, exc)
        self._show_error(title, message)

    def _show_progress(self):
        self.progress_after = None
        self.progress.pack(side="right", padx=self.px(0, 14))
        if str(self.progress["mode"]) == "indeterminate":
            self.progress.start(18)

    def _track_progress(self, text):
        step = re.match(r"\D*(\d+)/(\d+) · ", text)
        share = re.search(r" · (\d+)%$", text)
        self.progress.stop()
        if share:
            self.progress.configure(mode="determinate", maximum=100, value=int(share[1]))
        elif step and int(step[2]):
            self.progress.configure(mode="determinate", maximum=int(step[2]), value=int(step[1]) - 0.5)
        else:
            self.progress.configure(mode="indeterminate", value=0)
            if self.progress.winfo_manager():
                self.progress.start(18)

    def _end_progress(self):
        if self.progress_after:
            self.window.after_cancel(self.progress_after)
            self.progress_after = None
        self.progress.stop()
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.pack_forget()

    def _poll(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "status":
                    self.status.set(data)
                    self._track_progress(data)
                elif kind == "update":
                    self._update_checked(*data)
                elif kind == "history":
                    self._history_arrived(*data)
                elif kind == "cover":
                    stamp, art, image = data
                    self.cover_pending.discard(art)
                    if stamp == self.cover_stamp and image is not None:
                        self.sharp_covers[art] = image
                        if image:
                            self.flow.refresh(art)
                else:
                    self.busy = False
                    self._end_progress()
                    self._buttons()
                    if kind == "done":
                        diagnostics.log.info("操作完成")
                        callback, result = data
                        callback(result)
                    else:
                        self.status.set("操作未完成，请查看提示。")
                        self._show_error("ClassicDrop", data)
        except Empty:
            pass
        self.window.after(65, self._poll)

    def _buttons(self):
        writable = self.device and self.device.guid and not self.busy
        self.eject_button.configure(state="normal" if self.device and self.device.usb_instance and not self.busy else "disabled")
        allowed = writable and self.pending
        self.send_button.configure(text=f"传入 iPod  ({len(self.pending)})" if self.pending else "传入 iPod",
                                   state="normal" if allowed else "disabled")
        self.new_list.configure(state="normal" if writable else "disabled")
        self.preview_button.configure(state="normal" if self.pending and not self.busy else "disabled")
        self.queue_new_list.configure(state="normal" if self.pending and not self.busy else "disabled")
        self.target.configure(state="readonly" if not self.busy and len(self.target_keys) > 1 else "disabled")
        selected = bool(self.tree.selection())
        self.add_to_list.configure(state="normal" if writable and selected else "disabled")
        for i, button in enumerate(self.list_bar.winfo_children()):
            # Reordering works on the whole playlist, so it waits until the album or artist filter is cleared.
            button.configure(state="normal" if writable and (i >= 3 or selected) and (i >= 2 or self.browse == "all")
                             else "disabled")

    def choose_target(self, _):
        index = self.target.current()
        self.queue_target = self.target_keys[index] if index >= 0 else None

    def refresh(self, after=None):
        self._flush_ratings(lambda: self._refresh(after))

    def _refresh(self, after=None):
        if self.busy:
            return
        if self.device and self.device.root.is_dir() and (self.device.root / DB).is_file():
            self.load(self.device.root, after_load=after)
        else:
            self.device = None
            self.render()
            def found(drives):
                if len(drives) == 1:
                    self.load(drives[0], after_load=after)
                    return
                elif drives:
                    self.status.set("检测到多台 iPod，请点「选择设备」指定盘符。")
                else:
                    self.status.set("未发现 iPod。请用数据线连接并启用磁盘使用，然后刷新。")
                if after:
                    after()
            self._work("正在查找 iPod…", lambda _: discover(), found)

    def pick_device(self):
        if not self.busy:
            folder = filedialog.askdirectory(title="选择包含 iPod_Control 的设备根目录", parent=self.window)
            if folder:
                self.load(folder)

    def eject(self):
        self._flush_ratings(self._eject)

    def _eject(self):
        dev = self.device
        if self.busy or not dev or not dev.usb_instance:
            return
        name = dev.library.snapshot.device_name or "iPod"
        def removed(_):
            self.device = None
            if not isinstance(self.queue_target, NewPlaylist):
                self.queue_target = None
            self.render()
            self.status.set(f"「{name}」已安全弹出，可以拔下数据线。")
        self._work(f"正在安全弹出「{name}」…", lambda _: dev.eject(), removed)

    def load(self, folder, guid=None, status=None, after_load=None):
        self._flush_ratings(lambda: self._load(folder, guid, status, after_load))

    def _load(self, folder, guid=None, status=None, after_load=None):
        if self.busy:
            return
        old = self.device
        if (not old or old.root != Path(folder).resolve()) and not isinstance(self.queue_target, NewPlaylist):
            self.queue_target = None
        if old and old.root != Path(folder).resolve():
            self.view = "library"
        if guid is None and old and old.guid and old.root == Path(folder).resolve():
            saved = old.guid.hex()
        else:
            saved = None
        # Keep the same read-only view available if a manually entered GUID fails.
        if not (guid and old and not old.guid and old.root == Path(folder).resolve()):
            self.device = None
        self._buttons()
        covers = []
        def read(_):
            device = None
            if saved:
                try:
                    device = Device.open(folder, saved)
                except ValueError:
                    pass
            device = device or Device.open(folder, guid)
            # Asking a sleeping disk for its free space can take seconds, so it is done here, off the interface.
            try:
                covers.append(shutil.disk_usage(device.root) if system.is_volume_root(device.root) else None)
            except OSError:
                covers.append(None)
            small, large = device_covers(device)
            side = self.px(40)
            covers.extend((small, large, {art: image.resize((side, side), Image.Resampling.LANCZOS)
                                          for art, image in small.items()}))
            covers.append(listening(device))
            return device
        def loaded(device):
            self._hide_device(device)
            self.device = device
            self.usage, self.covers, self.cover_reads, self.cover_thumbs, self.listening = covers
            self.sharp_covers, self.cover_images, self.group_shown = {}, {}, None
            self.cover_pending.clear()
            self.cover_stamp += 1
            self.flow.forget()
            self.render()
            message = status or "曲库已读取。"
            if not status and device.profile and not device.profile.proven:
                message = (f"已读取 {device.profile.name} 的曲库。这个机型尚未经真机验证，"
                           "每次改动前的曲库都会备份在电脑上，传歌后请在 iPod 上核对。")
            if not device.guid:
                message = "当前为只读。可以浏览、导出，或在「工具与备份」填写设备 GUID。"
            if (device.root / STAGE).exists():
                message += " 检测到中断记录，请先点「工具与备份 → 恢复中断操作」。"
            self.status.set(message)
            if after_load:
                after_load()
        self.render(keep_view=True)
        self._work("正在读取 iPod 曲库…", read, loaded)

    def _hide_device(self, dev):
        """The serial, FireWire GUID and device name must never reach the log or the feedback report."""
        name = dev.library.snapshot.device_name or ""
        generic = {"ipod", "ipod classic", (dev.profile.name if dev.profile else "").lower()}
        values = [name if name.strip().lower() not in generic else "", dev.usb_instance or "", *dev.disk_ids]
        if dev.guid:
            values.append(dev.guid.hex())
        info = dev.root / "iPod_Control/Device"
        try:
            data = plistlib.loads((info / "SysInfoExtended").read_bytes())
            values += [str(v) for k, v in data.items() if "serial" in str(k).lower()]
        except Exception:
            pass
        try:
            for line in (info / "SysInfo").read_text(encoding="utf-8", errors="replace").splitlines():
                key, _, value = line.partition(":")
                if "serial" in key.strip().lower():
                    values.append(value.strip())
        except OSError:
            pass
        diagnostics.hide(*values)
        diagnostics.log.info("已识别设备：%s", describe(dev.profile) or "未知机型")

    def navigate(self, _):
        chosen = self.nav.selection()
        if not chosen:
            return
        if chosen[0] == "group":
            at = self.keys.index(self.view) if self.view in self.keys else 0
            self.nav.selection_set("2" if at < 2 else "1")
            return
        key = self.keys[int(chosen[0])]
        if key != self.view:
            self.view = key
            self.group = self.album = None
            self.search.set("")
            self.render()

    def set_browse(self, mode):
        if mode != self.browse:
            self.browse, self.group, self.album = mode, None, None
            self._remember()
            self.render()

    def _remember(self):
        if self.settings:
            try:
                Path(self.settings).parent.mkdir(parents=True, exist_ok=True)
                Path(self.settings).write_text(json.dumps({"theme": theme.CURRENT, "browse": self.browse}), encoding="utf8")
            except OSError:
                pass

    def set_theme(self, name):
        if name == theme.CURRENT or name not in theme.THEMES or self.busy or _disabled(self.window):
            return
        theme.use(name)
        self._remember()
        # Colours are fixed when a widget is made, so the window is laid out again from the same state.
        for child in self.window.winfo_children():
            child.destroy()
        self.group_shown = self.nav_shown = self.columns_shown = None
        self._layout()
        self.render()
        theme.title_bar(self.window)

    def _picked(self, _):
        self._buttons()
        chosen = self.tree.selection()
        if self.album_rows and chosen and chosen[0] in self.rows:
            album = self.rows[chosen[0]][0].album.strip()
            if album != self.album and album in self.album_rows:
                self.album = album
                self.flow.show(self.flow.items, album)

    def choose_group(self, _):
        chosen = self.groups.selection()
        at = self.groups.index(chosen[0]) if chosen else -1
        if not 0 <= at < len(self.group_keys):
            return
        group = self.group_keys[at]
        if self.browse == "album":
            self.choose_album(group)
        elif group != self.group:
            self.group, self.album = group, None
            self.render()

    def choose_album(self, album):
        if album == self.album:
            return
        self.album = album
        if self.browse != "all":
            self.render()
        elif album in self.album_rows:
            # The whole list stays in view: bring the album forward and jump to its first song.
            self.flow.show(self.flow.items, album)
            row = self.album_rows[album]
            if self.tree.exists(row):
                self.tree.selection_set(row)
                self.tree.see(row)

    def _albums(self, rows):
        found = {}
        for row in rows:
            found.setdefault(row[0].album.strip(), []).append(row)
        return {name: found[name] for name in sorted(found, key=lambda name: (not name, name.casefold()))}

    @staticmethod
    def _album_detail(tracks):
        artists = {t.artist.strip() for t in tracks}
        return ((artists.pop() or "未知歌手") if len(artists) == 1 else "多位歌手") + f" · {len(tracks)} 首"

    def _show_strip(self, albums):
        if self.album not in albums:
            self.album = next(iter(albums), None)
        self.flow.show([(name, name or "未知专辑", self._album_detail([row[0] for row in members]),
                         self._art([row[0] for row in members])) for name, members in albums.items()], self.album)

    def _art(self, tracks):
        return next((t.artwork_id for t in tracks if t.artwork_id in self.covers), 0)

    def _cover(self, tracks):
        art = self._art(tracks)
        if not art:
            return self.no_cover
        if art not in self.cover_images:
            self.cover_images[art] = ImageTk.PhotoImage(self.cover_thumbs[art], master=self.window)
        return self.cover_images[art]

    def _flow_picture(self, art):
        if self.sharp_covers.get(art):
            return self.sharp_covers[art], 2
        return (self.covers[art], 1) if art in self.covers else None

    def _want_covers(self, arts):
        """Read the large version of covers that are on screen, in the background and only while idle."""
        dev = self.device
        missing = [a for a in dict.fromkeys(arts) if a in self.cover_reads and a not in self.sharp_covers
                   and a not in self.cover_pending]
        if not missing or self.busy or not dev:
            return
        self.cover_pending.update(missing)
        stamp, reads = self.cover_stamp, self.cover_reads
        def worker():
            for art in missing:
                image = None
                with self.cover_lock:
                    if not self.busy and stamp == self.cover_stamp:
                        try:
                            image = load_cover(dev.root, reads[art])
                        except Exception:
                            image = False  # unreadable, so do not ask again
                self.events.put(("cover", (stamp, art, image)))
        Thread(target=worker, daemon=True).start()

    def _show_groups(self, rows, signature):
        """Fill the left list with artists or albums, then lay the albums out in the cover strip.

        Returns the rows of the album in front.
        """
        by_album = self.browse == "album"
        if by_album:
            albums = listed = self._albums(rows)
            self.group, self.group_keys = None, list(albums)
        else:
            found = {}
            for row in rows:
                found.setdefault(row[0].artist.strip(), []).append(row)
            listed = {name: found[name] for name in sorted(found, key=lambda name: (not name, name.casefold()))}
            if self.group not in listed:
                self.group = None
            self.group_keys = [None, *listed]
            albums = self._albums(listed[self.group] if self.group is not None else rows)
        if signature != self.group_shown:
            self.group_shown = signature
            self.groups.delete(*self.groups.get_children())
            if not by_album:
                self.groups.insert("", "end", image=self.no_cover, text=f"  全部歌手\n  {len(listed)} 位，{len(rows)} 首")
            for name, members in listed.items():
                tracks = [row[0] for row in members]
                detail = (self._album_detail(tracks) if by_album
                          else f"{len({t.album.strip() for t in tracks})} 张专辑 · {len(tracks)} 首")
                self.groups.insert("", "end", image=self._cover(tracks),
                                   text=f"  {name or ('未知专辑' if by_album else '未知歌手')}\n  {detail}")
        self._show_strip(albums)
        items = self.groups.get_children()
        current = self.album if by_album else self.group
        if current in self.group_keys and items:
            chosen = items[self.group_keys.index(current)]
            if self.groups.selection() != (chosen,):
                self.groups.selection_set(chosen)
                self.groups.see(chosen)
        return albums.get(self.album, [])

    def render(self, keep_view=False):
        if self.search_after:
            self.window.after_cancel(self.search_after)
            self.search_after = None
        self.render_version += 1
        self.hover_row = None
        generation = self.render_version
        dev = self.device
        snap = dev.library.snapshot if dev else None
        player = dev.profile if dev else None
        self.device_name.configure(text=(snap.device_name or (player.name if player else "iPod classic")) if snap else "尚未连接")
        usage = None
        look = (player.finish or None, player.shape) if player else (None, "classic")
        if look not in self.players:
            self.players[look] = ImageTk.PhotoImage(theme.player_image(self.px(56), *look), master=self.window)
        self.device_picture.configure(image=self.players[look])
        self.device_model.configure(text=describe(player) + ("\n尚未真机验证" if player and not player.proven else ""))
        if dev:
            on_drive = system.is_volume_root(dev.root)
            self.device_info.configure(text=dev.root.drive if on_drive and system.WINDOWS else dev.root.name)
            self.device_badge.configure(text="可写入" if dev.guid else "只读", bg=theme.OK_FILL if dev.guid else theme.WARN_FILL,
                                        fg=theme.OK_TEXT if dev.guid else theme.WARN_TEXT)
            self.device_badge.pack(side="left", padx=self.px(0, 6), before=self.device_info)
            usage = self.usage
        else:
            self.device_info.configure(text="连接后点「刷新」")
            self.device_badge.pack_forget()
        if usage and usage.total:
            self.capacity_fill.place(relwidth=usage.used / usage.total, relheight=1)
            self.capacity_text.configure(text=f"已用 {usage.used / 2**30:.1f} GB，共 {usage.total / 2**30:.1f} GB")
            self.capacity.grid()
        else:
            self.capacity.grid_remove()
        lists = list(snap.playlists) if snap else []
        targets = playlist_targets(snap if dev and dev.guid else None, self.queue_target)
        self.target_keys = [key for key, _ in targets]
        self.target.configure(values=[label for _, label in targets])
        if snap and self.queue_target not in self.target_keys:
            self.queue_target = None
        self.target.current(self.target_keys.index(self.queue_target) if self.queue_target in self.target_keys else 0)
        self.keys = ["library", "queue"] + [p.playlist_id for p in lists]
        if not keep_view and self.view not in self.keys and (snap or not self.busy):
            self.view = "library"
        # Rebuilding the sidebar and the table columns is slow in Tk, so both are left alone until they change.
        sidebar = (len(snap.tracks) if snap else "", len(self.pending) or "",
                   tuple((p.name or "未命名歌单", len(p.entries)) for p in lists))
        if sidebar != self.nav_shown or not self.nav.get_children():
            self.nav_shown = sidebar
            top = self.nav.yview()[0]
            self.nav.delete(*self.nav.get_children())
            self.nav.insert("", "end", iid="0", text="所有歌曲", values=(sidebar[0],))
            self.nav.insert("", "end", iid="1", text="待传歌曲", values=(sidebar[1],))
            if lists:
                self.nav.insert("", "end", iid="group", text="播放列表", tags=("group",))
            for i, (name, count) in enumerate(sidebar[2], 2):
                self.nav.insert("", "end", iid=str(i), text=name, values=(count,))
            self.nav.yview_moveto(top)
        chosen = (str(self.keys.index(self.view)),) if self.view in self.keys else ()
        if self.nav.selection() != chosen:
            self.nav.selection_set(chosen)
        queue_view = self.view == "queue"
        playlist = next((p for p in lists if p.playlist_id == self.view), None)
        for bar in (self.queue_bar, self.list_bar, self.add_to_list):
            bar.grid_remove()
        if queue_view:
            self.queue_bar.grid()
        elif playlist and editable_playlist(playlist):
            self.list_bar.grid()
        elif snap:
            self.add_to_list.grid()
        # One album at a time is listed in track order, with the track number in front.
        numbered = bool(snap) and not queue_view and self.browse != "all"
        columns = (("歌名", 210), ("歌手", 130), ("专辑", 140), ("传输格式", 140)) if queue_view else (
            ("歌名", 225), ("歌手", 132), ("专辑", 160), ("评级", 84), ("播放", 46), ("时长", 60))
        if numbered:
            # Every row belongs to the album named above the table, so its column makes room for the others.
            columns = (("#", 46), *(c for c in columns if c[0] != "专辑"))
        if columns != self.columns_shown:
            self.columns_shown = columns
            self.tree.configure(columns=tuple(str(i) for i in range(len(columns))))
            for i, (name, width) in enumerate(columns):
                side = "center" if name in ("#", "播放") else "w"
                self.tree.heading(str(i), text=name, anchor=side)
                self.tree.column(str(i), width=self.px(width), minwidth=self.px(136 if name == "传输格式" else 40),
                                 stretch=name not in ("时长", "#", "评级", "播放"), anchor=side)
        self.tree.delete(*self.tree.get_children())
        self.empty.place_forget()
        query = self.search.get().strip().casefold()
        if queue_view:
            heading = "待传歌曲"
            data = [(p, (song.title, song.artist, song.album, LABELS[song.mode]), None) for p, song in self.pending.items()]
        else:
            heading = playlist.name if playlist else "所有歌曲"
            tracks = snap.tracks if snap else ()
            if playlist:
                by_id = {t.track_id: t for t in tracks}
                ordered = ((by_id[e.track_id], e.entry_id) for e in playlist.entries if e.track_id in by_id)
            else:
                ordered = ((t, None) for t in tracks)
            def heard(track):
                plays = self.listening.get(track.track_id, (track.rating, track.play_count))[1]
                return self._rating_text(self._rating(track)), plays or ""
            data = [(t, (t.title, t.artist, t.album, *heard(t), f"{t.length_ms // 60000}:{t.length_ms // 1000 % 60:02}"), entry)
                    for t, entry in ordered]
        self.heading.configure(text=heading)
        count = len(data)
        if query:
            # Text and the last column only: a digit typed into the search should not match play counts.
            data = [row for row in data if any(query in str(x).casefold() for x in row[1][:3] + row[1][-1:])
                    or (queue_view and query in str(row[0]).casefold())]
        browsing = bool(snap) and not queue_view
        for mode, button in self.browse_buttons.items():
            button.configure(style="Picked.Tool.TButton" if mode == self.browse else "Tool.TButton")
        if browsing:
            self.browse_bar.grid()
        else:
            self.browse_bar.grid_remove()
        self.album_rows = {}
        if browsing and self.browse != "all":
            data = self._show_groups(data, (self.view, self.browse, query, id(snap)))
            discs = {row[0].metadata.disc_number for row in data} - {0}
            def place(row):
                disc, number = row[0].metadata.disc_number, row[0].track_number
                return (disc or 1) if len(discs) > 1 else 0, number or 1 << 30
            data = [(row[0], ((f"{place(row)[0]}-{row[0].track_number}" if len(discs) > 1 else row[0].track_number)
                              if row[0].track_number else "", *row[1][:2], *row[1][3:]), row[2])
                    for row in sorted(data, key=place)]
            self.group_pane.grid()
            self.flow.grid()
        elif browsing and data:
            # Every song stays listed. The strip follows the selected song and jumps to an album when clicked.
            self.group = None
            self.group_pane.grid_remove()
            for index, row in enumerate(data):
                self.album_rows.setdefault(row[0].album.strip(), str(index))
            self._show_strip(self._albums(data))
            self.flow.grid()
        else:
            # The chosen artist and album are kept, so that they are still in front once a reload has finished.
            self.group_pane.grid_remove()
            self.flow.grid_remove()
        self.counter.configure(text=f"{len(data)} / {count} 首" if len(data) != count else f"{count} 首")
        self.rows = {str(i): (record, entry) for i, (record, _, entry) in enumerate(data)}
        def fill(start):
            if generation != self.render_version:
                return
            end = min(start + 350, len(data))
            for i in range(start, end):
                self.tree.insert("", "end", iid=str(i), values=data[i][1], tags=("odd",) if i % 2 else ())
            if end < len(data):
                self.window.after_idle(lambda: fill(end))
        fill(0)
        if not data:
            empty = ("没有匹配的歌曲" if query else "把音乐文件或文件夹拖到这里" if queue_view else
                     "连接 iPod 以查看曲库\n也可以先拖入音乐文件或文件夹" if not dev else "这里还没有歌曲")
            self.empty.configure(text=empty)
            self.empty.place(relx=0.5, rely=0.48, anchor="center")
        self._buttons()

    def selected(self):
        return [self.rows[i][0] for i in self.tree.selection() if i in self.rows]

    def select_all(self, _):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def pick_files(self):
        if not self.busy:
            self.add_files(filedialog.askopenfilenames(parent=self.window, title="选择音乐文件", filetypes=AUDIO_TYPES))

    def pick_folder(self):
        if not self.busy:
            folder = filedialog.askdirectory(parent=self.window, title="选择音乐文件夹（包含子文件夹）", mustexist=True)
            if folder:
                self.add_files([folder])

    def drop_files(self, event):
        if self.busy or _disabled(self.window):
            return "refuse_drop"
        paths = self.window.tk.splitlist(event.data)
        # tkdnd runs idle callbacks inside the OLE drop. A timer lets Explorer finish the drag first.
        self.window.after(0, self.add_files, paths)
        return "copy"

    def edit_pending(self, *, new_playlist=False):
        if self.busy or self.view != "queue" or not self.pending:
            return
        paths = self.pending if new_playlist else self.selected() or self.pending
        self.add_files((), initial=[self.pending[p] for p in paths], new_playlist=new_playlist)

    def add_files(self, paths, *, initial=(), new_playlist=False):
        if self.busy or _disabled(self.window) or not (paths or initial):
            return
        dev = self.device
        writable = dev and dev.guid
        system.WINDOWS and self.window.attributes("-disabled", True)
        try:
            ImportDialog(self.window, on_close=lambda result: self._finish_preview(initial, result),
                         paths=paths, initial=initial, known=self.pending, target=self.queue_target, new_playlist=new_playlist,
                         snapshot=dev.library.snapshot if dev else None,
                         device_name=(dev.library.snapshot.device_name or (dev.profile.name if dev.profile else "iPod classic"))
                         if writable else "")
        except Exception:
            system.WINDOWS and self.window.attributes("-disabled", False)
            raise

    def _finish_preview(self, initial, result):
        system.WINDOWS and self.window.attributes("-disabled", False)
        if result is None:
            return
        batch, target, direct = result
        for song in initial:
            if song.path not in batch:
                self.pending.pop(song.path, None)
        self.pending.update(batch)
        previous_target = self.queue_target
        if batch:
            self.queue_target = target
        elif not self.pending:
            self.queue_target = None
        self.view = "queue"
        self.search.set("")
        self.render()
        if direct:
            self.send(batch, previous_target=previous_target)
        else:
            self.status.set(f"待传 {len(self.pending)} 首。点「传入 iPod」开始。")

    def remove_pending(self):
        if self.busy or self.view != "queue":
            return
        selected = set(self.selected())
        for path in selected:
            self.pending.pop(path)
        if not self.pending and isinstance(self.queue_target, NewPlaylist):
            self.queue_target = None
        self.render()

    def send(self, batch=None, *, previous_target=None):
        dev, reviewed, target = self.device, self.pending.copy() if batch is None else batch, self.queue_target
        if not dev or not dev.guid or not reviewed or self.busy:
            return
        new_name = target.name if isinstance(target, NewPlaylist) else None
        def done(result):
            added, skipped, _ = result
            for path in reviewed:
                self.pending.pop(path, None)
            self.search.set("")
            message = f"已传入 {added} 首，跳过 {skipped} 首重复歌曲。"
            if new_name is not None:
                message += f"已新建播放列表「{new_name}」。"
            elif target is not None:
                message += "已更新目标播放列表。"
            def show_target():
                saved = target
                if new_name is not None:
                    saved = next((p.playlist_id for p in self.device.library.snapshot.playlists if p.name == new_name), None)
                self.queue_target = previous_target if self.pending and previous_target != target else saved
                self.view = saved if saved is not None else "library"
                self.render()
            self.load(dev.root, dev.guid.hex(), message + "可以安全弹出设备。", show_target)
        formats = {p: s.mode for p, s in reviewed.items()}
        self._work("准备传歌…", lambda report: transfer(dev, tuple(reviewed), BACKUPS, report, reviewed=reviewed,
                                                    formats=formats, playlist_id=target if new_name is None else None,
                                                    new_playlist_name=new_name), done)

    def _save(self, desired, message, after_load=None):
        dev = self.device
        if desired == dev.library.snapshot:
            self.status.set("没有需要保存的改动。")
            return
        self._work("正在保存曲库…", lambda report: transfer(dev, [], BACKUPS, report, desired=desired),
                   lambda _: self.load(dev.root, dev.guid.hex(), message, after_load))

    def change_device_name(self):
        dev = self.device
        if self.busy or not dev or not dev.guid:
            return
        snap = dev.library.snapshot
        name = simpledialog.askstring("修改设备名称", "iPod 原厂曲库中的设备名称",
                                      initialvalue=snap.device_name, parent=self.window)
        if name is None:
            return
        try:
            desired = rename_device(snap, name)
            if desired.device_name.strip().lower() not in {"ipod", "ipod classic"}:
                diagnostics.hide(desired.device_name)
        except ValueError as exc:
            self._fail("修改设备名称", str(exc), exc)
            return
        self._save(desired, f"设备已改名为「{desired.device_name}」。")

    def repair_covers(self):
        dev = self.device
        if self.busy or not dev or not dev.guid:
            return
        def done(result):
            count, missing, _ = result
            message = f"已补齐 {count} 首歌曲的设备封面。" if count else "设备封面已核对。"
            if missing:
                message += f"{missing} 首歌曲没有可读取的内嵌封面。"
            self.load(dev.root, dev.guid.hex(), message + "可以安全弹出设备。")
        self._work("正在检查原厂封面…", lambda report: transfer(dev, (), BACKUPS, report, repair_covers=True), done)

    def delete_music(self, *, all_music=False):
        dev = self.device
        if self.busy or not dev or not dev.guid or (not all_music and self.view == "queue"):
            return
        snap = dev.library.snapshot
        tracks = ([t for t in snap.tracks if is_music(t)] if all_music else self.selected())
        if not tracks or not all(is_music(t) for t in tracks):
            return
        ids = tuple(dict.fromkeys(t.track_id for t in tracks))
        device = f"「{snap.device_name or 'iPod'}」 ({dev.root})"
        if all_music:
            text = (f"将从 {device} 删除全部 {len(ids)} 首音乐，并释放机内音频空间。\n"
                    "保留普通歌单、播客、有声书、视频、设置及其他文件。\n\n"
                    "自动备份仅含曲库索引。要保留歌曲，请先导出音频。\n\n输入「清空」以继续。")
            answer = simpledialog.askstring("清空音乐库", text, parent=self.window)
            if answer is None or answer.strip() != "清空":
                return
        else:
            preview = "、".join(dict.fromkeys(t.title or "未命名" for t in tracks[:3]))
            text = (f"从 {device} 删除选中的 {len(ids)} 首歌曲？\n\n{preview}"
                    f"{'…' if len(ids) > 3 else ''}\n\n会从所有歌单移除，并清理机内音频。"
                    "自动备份仅含曲库索引。")
            if not messagebox.askyesno("从 iPod 删除歌曲", text, icon="warning", default="no", parent=self.window):
                return
        message = f"已从 iPod 删除 {len(ids)} 首音乐。" if not all_music else f"音乐库已清空，共删除 {len(ids)} 首音乐。"
        self._work("正在整理曲库与音频…", lambda report: transfer(dev, [], BACKUPS, report, remove_ids=ids),
                   lambda _: self.load(dev.root, dev.guid.hex(), message))

    def change_playlist(self, playlist_id=None, remove=False):
        if self.busy or not self.device or not self.device.guid:
            return
        name = None
        if playlist_id is None:
            name = simpledialog.askstring("新建歌单", "歌单名称", parent=self.window)
            if not name:
                return
        tracks = [t.track_id for t in self.selected()] if self.view != "queue" else []
        try:
            entries = [self.rows[i][1] for i in self.tree.selection()] if remove else None
            desired = playlist_edit(self.device.library.snapshot, tracks, playlist_id=playlist_id, name=name,
                                    remove=remove, entry_ids=entries)
        except ValueError as exc:
            self._fail("歌单", str(exc), exc)
            return
        def show_new():
            created = next(p for p in self.device.library.snapshot.playlists if p.name == name.strip())
            if self.view == "queue":
                self.queue_target = created.playlist_id
            else:
                self.view = created.playlist_id
                self.search.set("")
            self.render()
        self._save(desired, "歌单已保存到 iPod。", show_new if name is not None else None)

    def playlist_menu(self):
        if self.busy or not self.device or not self.device.guid or not self.selected():
            return
        menu = tk.Menu(self.window, tearoff=False)
        for playlist in self.device.library.snapshot.playlists:
            if editable_playlist(playlist):
                menu.add_command(label=playlist.name, command=lambda i=playlist.playlist_id: self.change_playlist(i))
        if menu.index("end") is not None:
            menu.add_separator()
        menu.add_command(label="新建播放列表", command=self.change_playlist)
        menu.tk_popup(self.add_to_list.winfo_rootx(), self.add_to_list.winfo_rooty() + self.add_to_list.winfo_height())

    def delete_key(self, _):
        if self.view == "queue":
            self.remove_pending()
        elif self.view == "library":
            self.delete_music()
        elif self.view in self.target_keys:
            self.change_playlist(self.view, True)
        return "break"

    def manage_list(self, action):
        dev = self.device
        if self.busy or not dev or not dev.guid or self.view not in self.target_keys:
            return
        snap = dev.library.snapshot
        playlist = next(p for p in snap.playlists if p.playlist_id == self.view)
        if action == "delete":
            if messagebox.askyesno("删除播放列表", f"删除播放列表「{playlist.name}」？\n其中的歌曲会保留在曲库中。",
                                   default="no", parent=self.window):
                self._work("正在删除播放列表…",
                           lambda report: transfer(dev, (), BACKUPS, report, delete_playlist_id=playlist.playlist_id),
                           lambda _: self.load(dev.root, dev.guid.hex(), "播放列表已删除，歌曲已保留。"))
            return
        try:
            if action == "rename":
                name = simpledialog.askstring("播放列表改名", "新的播放列表名称", initialvalue=playlist.name, parent=self.window)
                if name is not None:
                    self._save(rename_playlist(snap, playlist.playlist_id, name), "播放列表已改名。")
            else:
                chosen = {self.rows[i][1] for i in self.tree.selection()}
                desired = move_playlist_entries(snap, playlist.playlist_id, chosen, -1 if action == "up" else 1)
                updated = next(p for p in desired.playlists if p.playlist_id == playlist.playlist_id)
                positions = [i for i, e in enumerate(updated.entries) if e.entry_id in chosen]
                def reselect():
                    current = next(p for p in self.device.library.snapshot.playlists if p.playlist_id == playlist.playlist_id)
                    entries = {current.entries[i].entry_id for i in positions}
                    self.tree.selection_set([i for i, (_, e) in self.rows.items() if e in entries and self.tree.exists(i)])
                self._save(desired, "播放顺序已保存。", reselect)
        except ValueError as exc:
            self._fail("播放列表", str(exc), exc)

    def edit_tags(self):
        tracks = self.selected()
        if self.busy or not self.device or not self.device.guid or self.view == "queue" or not tracks:
            return
        if len(tracks) > 1:
            ids = {t.track_id for t in tracks}
            name = simpledialog.askstring("批量修改歌手", f"将选中 {len(ids)} 首歌曲的曲库歌手统一设为",
                                          parent=self.window)
            if name and name.strip():
                snap = self.device.library.snapshot
                edited = tuple(replace(t, artist=name.strip()) if t.track_id in ids else t for t in snap.tracks)
                self._save(replace(snap, tracks=edited), f"已更新 {len(ids)} 首歌曲的曲库歌手。")
            return
        track = tracks[0]
        dialog = tk.Toplevel(self.window)
        dialog.title("编辑 iPod 曲库信息")
        dialog.configure(bg=theme.SURFACE)
        self.window.after(60, theme.title_bar, dialog)
        dialog.transient(self.window)
        dialog.grab_set()
        dialog.resizable(False, False)
        values = []
        for i, (label, value) in enumerate((("歌名", track.title), ("歌手", track.artist), ("专辑", track.album))):
            ttk.Label(dialog, text=label).grid(row=i, column=0, padx=self.px(15), pady=self.px(8))
            field = ttk.Entry(dialog, width=38)
            field.insert(0, value)
            field.grid(row=i, column=1, padx=self.px(0, 15), pady=self.px(8))
            values.append(field)
        def save(*_):
            title, artist, album = (v.get().strip() for v in values)
            if not title:
                values[0].focus_set()
                return
            snapshot = self.device.library.snapshot
            changed = replace(track, title=title, artist=artist, album=album)
            desired = replace(snapshot, tracks=tuple(changed if t.track_id == track.track_id else t for t in snapshot.tracks))
            dialog.destroy()
            self._save(desired, "歌曲信息已保存到 iPod 曲库。")
        ttk.Button(dialog, text="保存到 iPod", command=save).grid(row=3, column=1, sticky="e", padx=self.px(15), pady=self.px(11))
        dialog.bind("<Return>", save)
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        values[0].focus_set()

    def export(self):
        dev, tracks = self.device, tuple(self.selected())
        if self.busy or not dev or not tracks or self.view == "queue":
            return
        folder = filedialog.askdirectory(parent=self.window, title="将歌曲导出到电脑上的文件夹")
        if folder:
            self._work("正在导出歌曲…", lambda report: export_tracks(dev, tracks, folder, report),
                       lambda n: self.status.set(f"新导出 {n} 首到 {folder}。相同内容已跳过。"))

    def rate(self, rating):
        """Give the selected songs this rating, from 0 to 100. Saving also merges what the iPod recorded."""
        dev, tracks = self.device, self.selected()
        if self.busy or not dev or not dev.guid or not tracks or self.view == "queue":
            return
        ratings = {**self.pending_ratings, **{t.track_id: rating for t in tracks}}
        self.pending_ratings = {}
        if self.rating_timer:
            self.window.after_cancel(self.rating_timer)
            self.rating_timer = None
        done = f"已将 {len(ratings)} 首歌曲评为 {'★' * (rating // 20)}。" if rating else f"已清除 {len(ratings)} 首歌曲的评级。"
        self._write_ratings(ratings, done)

    def context_click(self, event):
        row = self.tree.identify_row(event.y)
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
        elif not row:
            self.tree.selection_remove(*self.tree.selection())
        self.action_menu(event.x_root, event.y_root)

    def action_menu(self, x=None, y=None):
        if self.busy:
            return
        menu = tk.Menu(self.window, tearoff=False)
        selected = self.selected()
        writable = self.device is not None and self.device.guid is not None
        if self.view == "queue":
            menu.add_command(label="从待传列表移除", command=self.remove_pending, state="normal" if selected else "disabled")
        else:
            menu.add_command(label="导出到电脑", state="normal" if selected else "disabled", command=self.export)
            menu.add_command(label="批量修改歌手" if len(selected) > 1 else "编辑歌曲信息",
                             state="normal" if writable and selected else "disabled",
                             command=self.edit_tags)
            stars = tk.Menu(menu, tearoff=False)
            for count in (5, 4, 3, 2, 1):
                stars.add_command(label="★" * count, command=lambda count=count: self.rate(count * 20))
            stars.add_separator()
            stars.add_command(label="清除评级", command=lambda: self.rate(0))
            menu.add_cascade(label="评级", menu=stars, state="normal" if writable and selected else "disabled")
            menu.add_separator()
            menu.add_command(label="新建歌单", state="normal" if writable else "disabled", command=self.change_playlist)
            editable = [p for p in self.device.library.snapshot.playlists if editable_playlist(p)] if self.device else []
            sub = tk.Menu(menu, tearoff=False)
            for p in editable:
                sub.add_command(label=p.name, command=lambda i=p.playlist_id: self.change_playlist(i))
            menu.add_cascade(label="加入已有歌单", menu=sub, state="normal" if writable and selected and editable else "disabled")
            if any(p.playlist_id == self.view for p in editable):
                menu.add_command(label="从当前歌单移除", command=lambda: self.change_playlist(self.view, True),
                                 state="normal" if writable and selected else "disabled")
            menu.add_separator()
            menu.add_command(label="从 iPod 删除歌曲", accelerator="Delete" if self.view == "library" else "", command=self.delete_music,
                             state="normal" if writable and selected and all(is_music(t) for t in selected) else "disabled")
        if x is None:
            x, y = self.actions.winfo_rootx(), self.actions.winfo_rooty() + self.actions.winfo_height()
        menu.tk_popup(x, y)

    def tools_menu(self):
        menu = tk.Menu(self.window, tearoff=False)
        dev = self.device
        writable = dev and dev.guid and not self.busy
        music_state = "normal" if writable and any(map(is_music, dev.library.snapshot.tracks)) else "disabled"
        menu.add_command(label="修改设备名称", accelerator="F2", command=self.change_device_name,
                         state="normal" if writable else "disabled")
        menu.add_command(label="清空音乐库", command=lambda: self.delete_music(all_music=True), state=music_state)
        menu.add_command(label="补齐设备封面", command=self.repair_covers, state=music_state)
        menu.add_separator()
        menu.add_command(label="打开电脑上的曲库索引备份", state="normal" if BACKUPS.is_dir() else "disabled",
                         command=lambda: system.open_folder(BACKUPS))
        def enter_guid():
            value = simpledialog.askstring("设备校验", "输入这台 iPod 的 16 位十六进制 FireWire GUID", parent=self.window)
            if value:
                self.load(dev.root, value.strip())
        menu.add_command(label="填写设备 GUID", command=enter_guid,
                         state="normal" if dev and not dev.guid and not self.busy else "disabled")
        menu.add_command(label="Apple 编码器", command=self.apple_encoders,
                         state="disabled" if self.busy else "normal")
        menu.add_command(label="恢复中断操作",
                         command=lambda: self._work("核对中断记录…", lambda _: recover_interrupted(dev, BACKUPS),
                                                    lambda _: self.load(dev.root, dev.guid.hex(), "中断记录已处理。")),
                         state="normal" if dev and dev.guid and (dev.root / STAGE).is_dir() and not self.busy else "disabled")
        menu.add_separator()
        looks = tk.Menu(menu, tearoff=False)
        self.theme_choice = tk.StringVar(self.window, theme.CURRENT)
        for name, label in theme.THEMES.items():
            # Rebuild the window after the menu has closed.
            looks.add_radiobutton(label=label, value=name, variable=self.theme_choice,
                                  command=lambda name=name: self.window.after(20, self.set_theme, name))
        menu.add_cascade(label="主题", menu=looks, state="disabled" if self.busy else "normal")
        menu.add_command(label="检查更新", command=lambda: self.check_updates(asked=True))
        menu.add_command(label="使用说明", command=lambda: messagebox.showinfo(
            "ClassicDrop " + __version__, "适用于 Windows 可读取的 iPod classic 原厂曲库。iPod nano 第 3、4 代使用同一种曲库，也可以读写，"
            "但尚未经真机验证。\n\n"
            "拖入歌曲或整个文件夹，在「导入预览」核对歌名、歌手、专辑和封面，也可多选一起修改。"
            "超过 24 位或 48 kHz 的音源和 DSF、DFF 统一转为 24 位 / 48 kHz ALAC。点「确认传入 iPod」，或先加入待传。"
            "在「传入位置」旁点「新建播放列表」，可以把本次传入的歌曲放进新歌单。传输完成后可点左侧「弹出设备」。\n\n"
            "传歌时会用内嵌图片生成原厂播放界面的封面。以前传入的歌可点「补齐设备封面」。\n\n"
            "点「新建播放列表」创建歌单。打开歌单可调序、改名、移出歌曲或删除列表。\n\n"
            "修改设备名称会更新 iPod 的原厂曲库名称。清空音乐库会清理歌曲和对应音频，保留其他文件。\n\n"
            "打开索引备份可查看每次改动前的曲库。恢复中断操作用于传歌或删除时意外断开。"
            "仅在设备显示只读且已知本机标识时，需要手动填写 GUID。\n\n"
            f"电脑备份位置\n{BACKUPS}\n\n曲库引擎采用 iOpenPod。许可证为 GPL-3.0-or-later。", parent=self.window))
        menu.tk_popup(self.tools_button.winfo_rootx(), self.tools_button.winfo_rooty() + self.tools_button.winfo_height())

    def apple_encoders(self):
        """Offer Apple's CoreAudioToolbox encoder, fetched from Apple's iTunes installer, or remove ours."""
        if self.busy:
            return
        if system.MAC:
            messagebox.showinfo("Apple 编码器",
                                "使用 macOS 内置的 Apple 编码器转换 ALAC 和 AAC，无需安装其他组件。", parent=self.window)
            return
        def checked(_):
            encoders.refresh()
            return encoders.apple_version(), encoders.apple_source()
        def done(result):
            version, source = result
            if version and source == "qtfiles":
                self.status.set(f"Apple 编码器已启用（{version}）。")
                if messagebox.askyesno("Apple 编码器",
                                       f"Apple 编码器已启用（{version}）。转换 ALAC 和 AAC 时使用 Apple 的编码器。"
                                       "要移除这些组件吗？", parent=self.window):
                    encoders.remove_apple()
                    self.status.set("已移除 Apple 编码器组件。")
            elif version:
                self.status.set(f"Apple 编码器已启用（{version}），来自已安装的 iTunes。")
                messagebox.showinfo("Apple 编码器",
                                    f"正在使用电脑上已安装的 iTunes 提供的 Apple 编码器（{version}），"
                                    "不需要另外下载。", parent=self.window)
            else:
                self.status.set("Apple 编码器未启用。")
                if messagebox.askyesno("Apple 编码器",
                                       "转换 AAC 时可以改用 Apple 的编码器，音质更好。程序会从 Apple 官网下载 iTunes "
                                       "安装包（约 200 MB），只提取编码器需要的组件（约 43 MB），不会安装 iTunes。要继续吗？",
                                       parent=self.window):
                    self._work("正在下载 iTunes 安装包…", encoders.install_apple, installed)
        def installed(version):
            self.status.set(f"Apple 编码器已启用（{version}）。")
            messagebox.showinfo("Apple 编码器", f"Apple 编码器已启用（{version}）。", parent=self.window)
        self._work("正在检查 Apple 编码器…", checked, done)

    def check_updates(self, asked=False):
        """Look for a newer release without holding up the window. asked is True when the user chose the menu item."""
        if asked:
            self.status.set("正在检查更新…")
        def worker():
            try:
                found = check_for_updates(__version__)
            except Exception as exc:
                found = exc
            self.events.put(("update", (found, asked)))
        Thread(target=worker, daemon=True).start()

    def _update_checked(self, found, asked):
        if isinstance(found, Exception):
            if asked:
                self.status.set(f"检查更新失败：{found}")
            return
        if not found:
            if asked:
                self.status.set(f"当前已是最新版本 {__version__}。")
            return
        self.latest_update = found
        self._offer_update()
        self.status.set(f"发现新版本 {found.version} · 点右下角的版本号查看")
        if asked:
            self.open_versions()

    def _offer_update(self):
        if self.latest_update:
            self.update_dot.pack(side="left", padx=self.px(3, 0))
        else:
            self.update_dot.pack_forget()

    def _can_update_now(self):
        """Installing closes the program, so it waits for work in progress and for songs still queued."""
        if self.busy or _disabled(self.window):
            messagebox.showwarning("无法更新", "请等待当前操作完成后再更新。", parent=self.window)
            return False
        if self.pending:
            messagebox.showwarning("无法更新", f"待传列表里还有 {len(self.pending)} 首歌曲，请先传入或移除后再更新。",
                                   parent=self.window)
            return False
        return True

    def open_feedback(self):
        """Prepare the diagnostics for an Issue: environment, the last error and the recent log."""
        if self.feedback_dialog and self.feedback_dialog.winfo_exists():
            return
        player = self.device.profile if self.device else None
        model = f"{player.name}（{describe(player)}）" if player else ""
        text = diagnostics.report(model)
        px = self.px
        dialog = self.feedback_dialog = tk.Toplevel(self.window)
        dialog.title("反馈问题")
        dialog.configure(bg=theme.SURFACE)
        dialog.resizable(False, False)
        dialog.transient(self.window)
        dialog.grab_set()
        self.window.after(60, theme.title_bar, dialog)
        frame = tk.Frame(dialog, bg=theme.SURFACE, padx=px(22), pady=px(20))
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="下面的内容会复制到剪贴板。Issue 是公开的，提交前可以在 GitHub 上删改。",
                 bg=theme.SURFACE, fg=theme.NAVY, font=(theme.FONT, 10)).pack(anchor="w")
        box = tk.Frame(frame, bg=theme.LINE, padx=1, pady=1)
        box.pack(fill="both", expand=True, pady=px(12, 0))
        notes = self.feedback_text = tk.Text(box, wrap="char", height=20, width=58, font=(theme.FONT, 10), bg=theme.BAR,
                                             fg=theme.NAVY, relief="flat", highlightthickness=0, padx=px(12), pady=px(6),
                                             cursor="arrow", spacing1=px(1), spacing3=px(1))
        scroll = ttk.Scrollbar(box, orient="vertical", command=notes.yview)
        notes.configure(yscrollcommand=scroll.set)
        notes.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        notes.insert("end", text)
        notes.configure(state="disabled")
        foot = tk.Frame(frame, bg=theme.SURFACE)
        foot.pack(fill="x", pady=px(14, 0))
        def show_logs(_):
            diagnostics.LOGS.mkdir(parents=True, exist_ok=True)
            system.open_folder(diagnostics.LOGS)
        logs = tk.Label(foot, text="打开日志文件夹", bg=theme.SURFACE, fg=theme.BLUE, cursor="hand2",
                        font=(theme.FONT, 10, "underline"))
        logs.pack(side="left")
        logs.bind("<Button-1>", show_logs)
        self.feedback_buttons = tk.Frame(foot, bg=theme.SURFACE)
        self.feedback_buttons.pack(side="right")
        def copy_and_open():
            self.window.clipboard_clear()
            self.window.clipboard_append(text)
            webbrowser.open(issue_url(model), new=2)
            self.close_feedback()
        ttk.Button(self.feedback_buttons, text="关闭", command=self.close_feedback).pack(side="left")
        ttk.Button(self.feedback_buttons, text="复制并打开 GitHub", style="Accent.TButton",
                   command=copy_and_open).pack(side="left", padx=px(8, 0))
        dialog.protocol("WM_DELETE_WINDOW", self.close_feedback)
        dialog.bind("<Escape>", lambda _: self.close_feedback())
        dialog.update_idletasks()
        dialog.geometry(f"+{self.window.winfo_rootx() + (self.window.winfo_width() - dialog.winfo_width()) // 2}"
                        f"+{self.window.winfo_rooty() + (self.window.winfo_height() - dialog.winfo_height()) // 2}")

    def close_feedback(self):
        if self.feedback_dialog and self.feedback_dialog.winfo_exists():
            self.feedback_dialog.destroy()
        self.feedback_dialog = None

    def open_versions(self):
        """Show what each release changed, and offer the newer one when there is one."""
        if self.update_dialog and self.update_dialog.winfo_exists():
            return
        px = self.px
        dialog = self.update_dialog = tk.Toplevel(self.window)
        dialog.title("版本与更新")
        dialog.configure(bg=theme.SURFACE)
        dialog.resizable(False, False)
        dialog.transient(self.window)
        dialog.grab_set()
        self.window.after(60, theme.title_bar, dialog)
        frame = tk.Frame(dialog, bg=theme.SURFACE, padx=px(22), pady=px(20))
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="ClassicDrop " + __version__, bg=theme.SURFACE, fg=theme.NAVY,
                 font=(theme.FONT, 14, "bold")).pack(anchor="w")
        self.version_state = tk.Label(frame, bg=theme.SURFACE, font=(theme.FONT, 9))
        self.version_state.pack(anchor="w", pady=px(2, 0))
        box = tk.Frame(frame, bg=theme.LINE, padx=1, pady=1)
        box.pack(fill="both", expand=True, pady=px(12, 0))
        notes = self.version_notes = tk.Text(box, wrap="char", height=17, width=58, font=(theme.FONT, 10), bg=theme.BAR,
                                             fg=theme.NAVY, relief="flat", highlightthickness=0, padx=px(12), pady=px(6),
                                             cursor="arrow", spacing1=px(1), spacing3=px(1))
        scroll = ttk.Scrollbar(box, orient="vertical", command=notes.yview)
        notes.configure(yscrollcommand=scroll.set)
        notes.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        notes.tag_configure("version", font=(theme.FONT, 11, "bold"), spacing1=px(9))
        notes.tag_configure("date", foreground=theme.MUTED, font=(theme.FONT, 9))
        notes.tag_configure("new", foreground=theme.ALERT, font=(theme.FONT, 9, "bold"))
        notes.tag_configure("quiet", foreground=theme.MUTED)
        foot = tk.Frame(frame, bg=theme.SURFACE)
        foot.pack(fill="x", pady=px(14, 0))
        everything = tk.Label(foot, text="在 GitHub 上查看全部版本", bg=theme.SURFACE, fg=theme.BLUE, cursor="hand2",
                              font=(theme.FONT, 10, "underline"))
        everything.pack(side="left")
        everything.bind("<Button-1>", lambda _: webbrowser.open(PROJECT_URL + "/releases", new=2))
        self.version_buttons = tk.Frame(foot, bg=theme.SURFACE)
        self.version_buttons.pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", self.close_versions)
        dialog.bind("<Escape>", lambda _: self.close_versions())
        self._show_versions((), "正在读取更新记录…", checked=False)
        dialog.update_idletasks()
        dialog.geometry(f"+{self.window.winfo_rootx() + (self.window.winfo_width() - dialog.winfo_width()) // 2}"
                        f"+{self.window.winfo_rooty() + (self.window.winfo_height() - dialog.winfo_height()) // 2}")
        self.history_stamp += 1
        stamp = self.history_stamp
        def worker():
            try:
                found = release_history(__version__)
            except Exception as exc:
                found = exc
            self.events.put(("history", (stamp, found)))
        Thread(target=worker, daemon=True).start()

    def close_versions(self):
        if self.update_dialog and self.update_dialog.winfo_exists():
            self.update_dialog.destroy()
        self.update_dialog = None

    def _history_arrived(self, stamp, found):
        if stamp != self.history_stamp or not (self.update_dialog and self.update_dialog.winfo_exists()):
            return
        if isinstance(found, Exception):
            self._show_versions((), f"无法读取更新记录：{found}", checked=False)
            return
        releases, update = found
        if update:
            self.latest_update = update
            self._offer_update()
        self._show_versions(releases, "" if releases else "还没有发布记录。", checked=True)

    def _show_versions(self, releases, remark, *, checked):
        """Fill the open dialog. checked is True once GitHub has answered."""
        info = self.latest_update
        listed = list(releases)
        if info and all(version_key(r.version) != version_key(info.version) for r in listed):
            # Known from the check at start, even when the full list cannot be read now.
            listed.insert(0, Release(info.version, info.url, "", plain_notes(info.notes)))
        notes = self.version_notes
        font = tkfont.Font(font=notes["font"])
        room = (int(notes["width"]) - 1) * font.measure("0")  # the box is that many digits wide
        notes.configure(state="normal")
        notes.delete("1.0", "end")
        for release in listed:
            notes.insert("end", release.version, "version")
            if release.published:
                notes.insert("end", "   " + release.published, "date")
            if info and version_key(release.version) == version_key(info.version):
                notes.insert("end", "   新版本", "new")
            elif version_key(release.version) == version_key(__version__):
                notes.insert("end", "   当前版本", "date")
            notes.insert("end", "\n" + theme.wrap_lines(release.notes or "（没有更新说明）", font.measure, room) + "\n")
        if remark:
            notes.insert("end", ("\n" if listed else "") + remark, "quiet")
        notes.configure(state="disabled")
        self.version_state.configure(text=f"发现新版本 {info.version}" if info else "已是最新版本" if checked else "",
                                     fg=theme.ALERT if info else theme.MUTED)
        for button in self.version_buttons.winfo_children():
            button.destroy()
        ttk.Button(self.version_buttons, text="关闭", command=self.close_versions).pack(side="left")
        if info:
            def accept():
                self.close_versions()
                if info.download_url:
                    self.download_update()
                else:
                    webbrowser.open(info.url, new=2)
            ttk.Button(self.version_buttons, text=f"下载并安装 {info.version}" if info.download_url else ("打开下载页" if system.MAC else "打开发布页"),
                       style="Accent.TButton", command=accept).pack(side="left", padx=self.px(8, 0))

    def download_update(self):
        info = self.latest_update
        if not info or not self._can_update_now():
            return
        destination = Path(tempfile.gettempdir()) / "ClassicDrop" / "updates" / info.installer_name
        def work(report):
            shown = [-1]
            def progress(done, total):
                share = done * 100 // total if total else 0
                if share != shown[0]:
                    shown[0] = share
                    report(f"正在下载更新 {done / 2**20:.1f} MB · {share}%" if total
                           else f"正在下载更新 {done / 2**20:.1f} MB")
            try:
                return download_update(info, destination, progress)
            except Exception as exc:
                return exc
        self._work(f"正在下载更新 {info.version}…", work, self._install_update)

    def _install_update(self, installer):
        info = self.latest_update
        if not isinstance(installer, Exception):
            self.status.set("更新包校验完成，正在安装…")
            try:
                # The installer closes this program, replaces its files and starts the new version.
                subprocess.Popen([str(installer), "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"], cwd=installer.parent)
            except OSError as exc:
                installer = exc
            else:
                self.window.destroy()
                return
        self.status.set(f"自动更新失败：{installer}")
        choice = messagebox.askyesnocancel(
            "自动更新失败", f"{installer}\n\n选择「是」重试下载，选择「否」打开发布页手动下载。", parent=self.window)
        if choice:
            self.download_update()
        elif choice is False and info:
            webbrowser.open(info.url, new=2)

    def close(self):
        if self.busy:
            self.status.set("当前操作正在进行，结束后即可关闭窗口。")
        else:
            self._flush_ratings(self.window.destroy)


def main():
    if sys.argv[1:2] == ["--self-test"]:
        from selftest import self_test
        sys.exit(self_test(Path(sys.argv[2])))
    if system.WINDOWS:
        ctypes.windll.user32.SetProcessDPIAware()
    diagnostics.setup()
    os_name = f"macOS {platform.mac_ver()[0]}" if system.MAC else f"Windows {platform.release()}（{platform.version()}）"
    diagnostics.log.info("ClassicDrop %s 启动 · %s · 架构 %s", __version__, os_name, cpuarch.describe())
    window = TkinterDnD.Tk()
    app = App(window, browse="album", settings=SETTINGS, check_updates=True)
    if system.MAC:
        window.createcommand("tk::mac::Quit", app.close)
    paths = [Path(value) for value in sys.argv[1:]]
    folder = next((p for p in paths if p.is_dir() and (p / "iPod_Control/iTunes").is_dir()), None)
    sources = [p for p in paths if p != folder]
    def start():
        preview = (lambda: app.add_files(sources)) if sources else None
        if folder:
            app.load(folder, after_load=preview)
        else:
            app.refresh(after=preview)
    window.after(100, start)
    window.mainloop()


if __name__ == "__main__":
    main()
