"""Review local tracks, artwork and encoding together before a transfer."""

from dataclasses import dataclass, replace
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from mutagen import MutagenError
from PIL import Image, ImageTk
from tkinterdnd2 import DND_FILES
from audio_convert import DSD_FILES, LABELS, READERS
from library import editable_playlist, validate_playlist_name
from music_import import Cover, FIELDS, scan_inputs, thumbnail
import system
import theme
app_mod = "Command" if system.MAC else "Control"

MODE_NAMES = {"copy": "保留原格式", "alac": "Apple Lossless（ALAC）", "alac-fit": "ALAC 兼容转换", "alac-48": LABELS["alac-48"],
              "aac": "AAC-LC（有损）"}
AUDIO_TYPES = [("音频文件", " ".join("*" + suffix for suffix in READERS))]


@dataclass(frozen=True)
class NewPlaylist:
    name: str


def playlist_targets(snapshot, selected=None):
    options = [(None, "仅传入曲库")]
    if snapshot:
        options.extend((p.playlist_id, p.name) for p in snapshot.playlists if editable_playlist(p))
    if isinstance(selected, NewPlaylist):
        options.append((selected, f"{selected.name}（新建）"))
    return options


class ImportDialog(tk.Toplevel):
    def __init__(self, parent, *, on_close, paths=(), initial=(), known=None, device_name="", snapshot=None,
                 target=None, new_playlist=False):
        super().__init__(parent)
        self.withdraw()
        self.title("导入预览")
        if parent.winfo_viewable():
            self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.px = theme.scaler(parent)
        self.known = known or {}
        self.rows = {s.path: s for s in initial}
        self.editing = bool(self.rows)
        self.selected_paths = ()
        self.snapshot = snapshot
        self.targets = playlist_targets(snapshot if device_name else None)
        self.device_name = device_name
        self.on_close = on_close
        self.result = None
        self.scanning = self.loading = False
        self.cancelled = Event()
        self.messages = Queue()
        self.timer = None
        self.errors = []
        self.status = tk.StringVar(self)
        self.values = {key: tk.StringVar(self) for key in FIELDS}
        self.entries = {}
        self._layout(target, new_playlist)
        for key, value in self.values.items():
            value.trace_add("write", lambda *_, field=key: self.change_text(field))
        for song in initial:
            self._row(song)
        self._first()
        self._ready()
        self.bind("<Escape>", lambda _: self.cancel())
        self.bind(f"<{app_mod}-Return>", lambda _: self.finish(bool(self.device_name)))
        for widget in (self, self.tree):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._drop)
        self.update_idletasks()
        width = min(self.px(940), self.winfo_screenwidth() - self.px(24))
        height = min(self.px(640), self.winfo_screenheight() - self.px(68))
        self.minsize(min(width, self.px(760)), min(height, self.px(480)))
        x = max(0, min(parent.winfo_rootx() + (parent.winfo_width() - width) // 2, self.winfo_screenwidth() - width))
        y = max(0, min(parent.winfo_rooty() + (parent.winfo_height() - height) // 2, self.winfo_screenheight() - height - 40))
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.deiconify()
        if system.MAC:
            # The parent's "-disabled" modality does not exist on macOS; take the grab instead.
            self.grab_set()
        theme.title_bar(self)
        (self.playlist_entry if self.creating_playlist else self.tree).focus_set()
        if paths:
            self.scan(paths)

    def _layout(self, selected_target, new_playlist):
        px = self.px
        style = ttk.Style(self)
        style.configure("Import.TFrame", background=theme.SURFACE)
        style.configure("Import.TLabel", background=theme.SURFACE, foreground=theme.NAVY)
        style.configure("ImportBar.TFrame", background=theme.BAR)
        style.configure("ImportBar.TLabel", background=theme.BAR, foreground=theme.MUTED)
        self.configure(background=theme.SURFACE)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        top = ttk.Frame(self, padding=px(12, 10, 12, 8), style="ImportBar.TFrame")
        top.grid(row=0, sticky="ew")
        self.add_files = ttk.Button(top, text="添加文件", command=self.pick_files)
        self.add_folder = ttk.Button(top, text="添加文件夹", command=self.pick_folder)
        self.add_files.pack(side="left")
        self.add_folder.pack(side="left", padx=px(5, 12))
        ttk.Button(top, text="全选", command=self.select_all).pack(side="left")
        ttk.Button(top, text="移出本次", command=self.remove_selected).pack(side="left", padx=px(5))
        self.skipped = ttk.Button(top, command=self.show_errors)
        ttk.Label(top, textvariable=self.status, style="ImportBar.TLabel").pack(side="left", padx=px(10, 0))
        content = ttk.Frame(self, padding=px(12, 10), style="Import.TFrame")
        content.grid(row=1, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)
        table = ttk.Frame(content, style="Import.TFrame")
        table.grid(row=0, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=(*FIELDS, "mode"), show="headings", selectmode="extended", height=8)
        for key, label, width in zip((*FIELDS, "mode"), ("歌名", "歌手", "专辑", "格式"), (156, 100, 115, 140)):
            self.tree.heading(key, text=label, anchor="w")
            self.tree.column(key, width=self.px(width), minwidth=self.px(136 if key == "mode" else 44), anchor="w", stretch=True)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._selection)
        self.tree.bind(f"<{app_mod}-a>", self.select_all)
        self.tree.bind("<Delete>", self.remove_selected)

        edit = ttk.Frame(content, padding=px(16, 0, 4, 0), style="Import.TFrame")
        edit.grid(row=0, column=1, sticky="ns")
        edit.columnconfigure(0, minsize=px(280))
        self.cover_side = px(144)
        self.canvas = tk.Canvas(edit, width=self.cover_side, height=self.cover_side, bd=0, highlightthickness=1,
                                highlightbackground=theme.LINE, bg=theme.SIDE)
        self.canvas.grid(row=0, pady=px(1, 4))
        self.cover_name = ttk.Label(edit, anchor="center", style="Import.TLabel", foreground=theme.MUTED, font=(theme.FONT, 9))
        self.cover_name.grid(row=1, sticky="ew")
        art = ttk.Frame(edit, style="Import.TFrame")
        art.grid(row=2, pady=px(6, 8))
        self.replace_button = ttk.Button(art, text="更换封面", command=self.pick_cover)
        self.replace_button.pack(side="left", padx=px(0, 6))
        self.clear_button = ttk.Button(art, text="移除封面", command=lambda: self.set_cover(None))
        self.clear_button.pack(side="left")
        def field(row, label, widget):
            ttk.Label(edit, text=label, style="Import.TLabel", foreground=theme.MUTED, font=(theme.FONT, 9)).grid(
                row=row, sticky="w", pady=px(5, 1))
            widget.grid(row=row + 1, sticky="ew")
            return widget
        for row, (key, label) in zip((3, 5, 7), zip(FIELDS, ("歌名", "歌手", "专辑"))):
            self.entries[key] = field(row, label, ttk.Entry(edit, textvariable=self.values[key]))
        self.mode = field(9, "传输格式", ttk.Combobox(edit, state="readonly"))
        self.mode.bind("<<ComboboxSelected>>", self.change_mode)
        self.hint_box = tk.Frame(edit, bg=theme.BAR, padx=px(10), pady=px(7))
        self.hint_box.grid(row=11, sticky="ew", pady=px(10, 0))
        self.hint = tk.Label(self.hint_box, wraplength=px(258), justify="left", anchor="w", bg=theme.BAR, fg=theme.MUTED,
                             font=(theme.FONT, 9))
        self.hint.pack(fill="x")

        ttk.Separator(self).grid(row=2, sticky="ew")
        footer = ttk.Frame(self, padding=px(12, 9, 12, 10), style="ImportBar.TFrame")
        footer.grid(row=3, sticky="ew")
        footer.columnconfigure(0, weight=1)
        left = ttk.Frame(footer, style="ImportBar.TFrame")
        left.grid(row=0, column=0, sticky="w")
        self.target_label = ttk.Label(left, style="ImportBar.TLabel")
        self.target_label.grid(row=0, column=0, padx=px(0, 5))
        self.target = ttk.Combobox(left, state="readonly", width=15, values=[label for _, label in self.targets])
        self.target.current(next((i for i, (key, _) in enumerate(self.targets) if key == selected_target), 0))
        self.playlist_entry = ttk.Entry(left, width=17)
        if isinstance(selected_target, NewPlaylist):
            self.playlist_entry.insert(0, selected_target.name)
        self.new_list_button = ttk.Button(left, command=lambda: self.set_new_playlist(not self.creating_playlist))
        self.new_list_button.grid(row=0, column=2, padx=px(5, 0))
        self.set_new_playlist(new_playlist or isinstance(selected_target, NewPlaylist))
        buttons = ttk.Frame(footer, style="ImportBar.TFrame")
        buttons.grid(row=0, column=1, sticky="e")
        ttk.Button(buttons, text="取消", command=self.cancel).pack(side="left", padx=px(4))
        self.queue_button = ttk.Button(buttons, text="保存到待传" if self.editing else "加入待传",
                                      command=lambda: self.finish(False),
                                      style="TButton" if self.device_name else "Accent.TButton")
        self.queue_button.pack(side="left", padx=px(4))
        self.send_button = ttk.Button(buttons, text="确认传入 iPod", style="Accent.TButton", command=lambda: self.finish(True))
        if self.device_name:
            self.send_button.pack(side="left", padx=px(4, 0))

    def set_new_playlist(self, active):
        self.creating_playlist = active
        self.target_label.configure(text="列表名称" if active else "传入位置")
        self.new_list_button.configure(text="取消新建" if active else "新建播放列表")
        if active:
            self.target.grid_remove()
            self.playlist_entry.grid(row=0, column=1, sticky="ew")
            albums = {song.album.strip() for song in self.rows.values()}
            if not self.playlist_entry.get() and len(albums) == 1:
                self.playlist_entry.insert(0, albums.pop())
            self.playlist_entry.focus_set()
            self.playlist_entry.selection_range(0, "end")
        else:
            self.playlist_entry.grid_remove()
            self.target.grid(row=0, column=1, sticky="ew")

    def pick_files(self):
        self.scan(filedialog.askopenfilenames(parent=self, title="选择音乐文件", filetypes=AUDIO_TYPES))

    def pick_folder(self):
        folder = filedialog.askdirectory(parent=self, title="选择音乐文件夹（包含子文件夹）", mustexist=True)
        if folder:
            self.scan([folder])

    def _drop(self, event):
        if self.scanning:
            return "refuse_drop"
        self.scan(self.tk.splitlist(event.data))
        return "copy"

    def scan(self, paths):
        if self.scanning or not paths:
            return
        self.scanning = True
        self._ready()
        self.status.set("正在扫描文件夹和歌曲信息…")
        known = {**self.known, **self.rows}
        messages, cancelled = self.messages, self.cancelled
        def worker():
            try:
                result = scan_inputs(paths, known, lambda s: messages.put(("progress", s)), cancelled.is_set)
            except Exception as exc:
                result = [], [str(exc) or type(exc).__name__]
            messages.put(("done", result))
        Thread(target=worker, daemon=True).start()
        self.timer = self.after(45, self._poll)

    def _poll(self):
        self.timer = None
        try:
            while True:
                kind, data = self.messages.get_nowait()
                if kind == "progress":
                    self.status.set(data)
                    continue
                songs, errors = data
                self.errors.extend(errors)
                for song in songs:
                    if song.path not in self.rows or song.stamp != self.rows[song.path].stamp:
                        self.rows[song.path] = song
                        self._row(song)
                self.scanning = False
                self._first()
                self._ready()
                if not songs:
                    self.status.set("本次未找到可读取的 MP3、M4A、FLAC、WAV、DSF 或 DFF。")
                return
        except Empty:
            self.timer = self.after(55, self._poll)

    def show_errors(self):
        messagebox.showinfo("已跳过的文件", "\n\n".join(self.errors[:12]) +
                            (f"\n\n另有 {len(self.errors) - 12} 项。" if len(self.errors) > 12 else ""), parent=self)

    def _row(self, song):
        values = (song.title, song.artist, song.album, LABELS[song.mode])
        iid = str(song.path)
        if self.tree.exists(iid):
            self.tree.item(iid, values=values)
        else:
            self.tree.insert("", "end", iid=iid, values=values)

    def _first(self):
        if self.rows and not self.tree.selection():
            first = str(next(iter(self.rows)))
            self.tree.selection_set(first)
            self.tree.focus(first)
        self._selection()

    def _ready(self):
        theme.stripe(self.tree)
        self.status.set(f"本次共 {len(self.rows)} 首　·　" +
                        (f"设备「{self.device_name}」" if self.device_name else "可先加入待传，连接可写 iPod 后传入。"))
        for widget in (self.add_files, self.add_folder):
            widget.configure(state="disabled" if self.scanning else "normal")
        self.queue_button.configure(state="normal" if not self.scanning and (self.rows or self.editing) else "disabled")
        self.send_button.configure(state="normal" if self.device_name and self.rows and not self.scanning else "disabled")
        if self.errors:
            self.skipped.configure(text=f"已跳过 {len(self.errors)} 项")
            self.skipped.pack(side="right")

    def _selection(self, _=None):
        self.selected_paths = tuple(Path(i) for i in self.tree.selection())
        songs = [self.rows[p] for p in self.selected_paths]
        self.loading = True
        try:
            for key in FIELDS:
                values = {getattr(s, key) for s in songs}
                self.values[key].set(values.pop() if len(values) == 1 else "")
                self.entries[key].configure(state="normal" if songs and (key != "title" or len(songs) == 1) else "disabled")
            choices = [k for k in MODE_NAMES if songs and all(k in s.choices for s in songs)]
            if songs and any("alac-fit" in s.choices for s in songs) and all("alac" in s.choices or "alac-fit" in s.choices for s in songs):
                if "alac-fit" not in choices:
                    choices.insert(0, "alac-fit")
            modes = {s.mode for s in songs}
            editable = choices and (len(choices) > 1 or modes != {choices[0]})
            self.mode.configure(values=[MODE_NAMES[k] for k in choices], state="readonly" if editable else "disabled")
            self.mode.set(MODE_NAMES[modes.pop()] if len(modes) == 1 else "保留各曲设置" if songs else "")
            self._show_cover(songs)
            self._hint(songs)
        finally:
            self.loading = False

    def _hint(self, songs):
        lines = []
        if len(songs) == 1:
            s = songs[0]
            lines.append(s.path.name)
            missing = [label for i, label in ((1, "歌手"), (2, "专辑")) if not s.original_text[i] and getattr(s, FIELDS[i])]
            if missing:
                lines.append("已补全" + "、".join(missing))
        elif songs:
            lines.append(f"已选 {len(songs)} 首，修改将应用到所选歌曲。")
        if any(s.path.suffix.lower() in DSD_FILES for s in songs):
            lines.append("DSF、DFF 的 DSD 音频会先转换为 PCM。")
        if any(s.mode == "alac-48" for s in songs):
            lines.append(f"高规格音源统一转为 {LABELS['alac-48']}，最多双声道。")
        if any(s.mode == "alac-fit" for s in songs):
            lines.append("兼容转换保留采样率，最多输出双声道。")
        elif any(s.mode == "aac" for s in songs):
            lines.append("AAC-LC 使用立体声 256 / 单声道 128 kbps。")
        self.hint.configure(text="\n".join(lines))
        if lines:
            self.hint_box.grid()
        else:
            self.hint_box.grid_remove()

    def _show_cover(self, songs):
        covers = {s.cover for s in songs}
        common = next(iter(covers)) if len(covers) == 1 else None
        note = "多张封面" if len(covers) > 1 else "无封面"
        self.photo = None
        if common:
            try:
                self.photo = ImageTk.PhotoImage(thumbnail(common.read(), self.cover_side - self.px(2)), master=self)
                note = common.label
            except (OSError, ValueError, MutagenError, Image.DecompressionBombError):
                note = "封面无法读取"
        self.canvas.delete("all")
        center = self.cover_side // 2 + 1
        if self.photo:
            self.canvas.create_image(center, center, image=self.photo)
        else:
            self.canvas.create_text(center, center, text="多张封面" if len(covers) > 1 else "无预览", fill=theme.MUTED)
        self.cover_name.configure(text=note)
        self.replace_button.configure(state="normal" if songs else "disabled")
        self.clear_button.configure(state="normal" if any(s.cover for s in songs) else "disabled")

    def change_text(self, field):
        if self.loading or (field == "title" and len(self.selected_paths) != 1):
            return
        value = self.values[field].get()
        for p in self.selected_paths:
            self.rows[p] = replace(self.rows[p], **{field: value})
            self._row(self.rows[p])

    def change_mode(self, _=None):
        selected = next((k for k, v in MODE_NAMES.items() if v == self.mode.get()), None)
        if not selected:
            return
        for p in self.selected_paths:
            song = self.rows[p]
            mode = "alac" if selected == "alac-fit" and "alac-fit" not in song.choices else selected
            if mode in song.choices:
                self.rows[p] = replace(song, mode=mode)
                self._row(self.rows[p])
        self._hint([self.rows[p] for p in self.selected_paths])

    def pick_cover(self):
        path = filedialog.askopenfilename(parent=self, title="选择专辑封面", filetypes=[("图片", "*.jpg *.jpeg *.png *.webp *.bmp")])
        if path:
            try:
                self.set_cover(Cover.from_file(path))
            except (OSError, ValueError, Image.DecompressionBombError) as exc:
                messagebox.showerror("读取封面", str(exc), parent=self)

    def set_cover(self, cover):
        for p in self.selected_paths:
            self.rows[p] = replace(self.rows[p], cover=cover)
        self._show_cover([self.rows[p] for p in self.selected_paths])

    def select_all(self, _=None):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def remove_selected(self, _=None):
        for iid in self.tree.selection():
            self.rows.pop(Path(iid))
            self.tree.delete(iid)
        self._first()
        self._ready()
        return "break"

    def finish(self, direct):
        if self.scanning or (direct and (not self.device_name or not self.rows)) or (not self.rows and not self.editing):
            return
        changes = {}
        for p, song in self.rows.items():
            fields = {key: getattr(song, key).strip() for key in FIELDS}
            if not fields["title"] or any(any(ord(c) < 32 for c in value) for value in fields.values()):
                self.tree.selection_set(str(p))
                self.tree.see(str(p))
                messagebox.showerror("歌曲信息", f"请填写歌名，并使用单行文字。\n{p.name}", parent=self)
                return
            changes[p] = replace(song, **fields)
        target = self.targets[self.target.current()][0]
        if self.creating_playlist and changes:
            try:
                target = NewPlaylist(validate_playlist_name(self.snapshot, self.playlist_entry.get()))
            except ValueError as exc:
                self.status.set(str(exc))
                self.playlist_entry.focus_set()
                self.playlist_entry.selection_range(0, "end")
                return
        self.result = (changes, target, direct)
        self.cancel()

    def cancel(self):
        self.cancelled.set()
        if self.timer is not None:
            self.after_cancel(self.timer)
        self.destroy()
        self.on_close(self.result)
