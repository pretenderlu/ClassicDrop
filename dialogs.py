"""Modal dialogs shared by the main window: text viewers, track tag editor and help text."""

import tkinter as tk
from tkinter import ttk

import theme


def help_text(backups_dir):
    return (
        "适用于 Windows 与 macOS 可读取的 iPod classic 原厂曲库。iPod nano 第 3、4 代使用同一种曲库，也可以读写，"
        "但尚未经真机验证。\n\n"
        "拖入歌曲、CUE 整轨或整个文件夹，在「导入预览」核对歌名、歌手、专辑和封面，也可多选一起修改。"
        "支持 MP3、M4A、FLAC、WAV、APE、WV、TTA、AIFF、DSF、DFF 以及 CUE 自动分轨。"
        "超过 24 位或 48 kHz 的音源和 DSF、DFF 统一转为 24 位 / 48 kHz ALAC。点「确认传入 iPod」，或先加入待传。"
        "在「传入位置」旁点「新建播放列表」，可以把本次传入的歌曲放进新歌单。传输完成后可点左侧「弹出设备」。\n\n"
        "传歌时会用内嵌图片生成原厂播放界面的封面。以前传入的歌可点「补齐设备封面」。\n\n"
        "点「新建播放列表」创建歌单。打开歌单可调序、改名、移出歌曲或删除列表。\n\n"
        "修改设备名称会更新 iPod 的原厂曲库名称。清空音乐库会清理歌曲和对应音频，保留其他文件。\n\n"
        "打开索引备份可查看每次改动前的曲库。恢复中断操作用于传歌或删除时意外断开。"
        "仅在设备显示只读且已知本机标识时，需要手动填写 GUID。\n\n"
        f"电脑备份位置\n{backups_dir}\n\n曲库引擎采用 iOpenPod。许可证为 GPL-3.0-or-later。"
    )


def center_dialog(parent, dialog):
    dialog.update_idletasks()
    x = parent.winfo_rootx() + (parent.winfo_width() - dialog.winfo_width()) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - dialog.winfo_height()) // 2
    dialog.geometry(f"+{x}+{y}")


def create_text_dialog(parent, px, *, title, height, width=58, intro=None, heading=None,
                       link_text, on_link, on_close):
    """Build a modal read-only scrolled text dialog with a bottom link and button bar."""
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.configure(bg=theme.SURFACE)
    dialog.resizable(False, False)
    dialog.transient(parent)
    dialog.grab_set()
    parent.after(60, theme.title_bar, dialog)
    frame = tk.Frame(dialog, bg=theme.SURFACE, padx=px(22), pady=px(20))
    frame.pack(fill="both", expand=True)
    state_label = None
    if heading is not None:
        tk.Label(frame, text=heading, bg=theme.SURFACE, fg=theme.NAVY,
                 font=(theme.FONT, 14, "bold")).pack(anchor="w")
        state_label = tk.Label(frame, bg=theme.SURFACE, font=(theme.FONT, 9))
        state_label.pack(anchor="w", pady=px(2, 0))
    if intro is not None:
        tk.Label(frame, text=intro, bg=theme.SURFACE, fg=theme.NAVY,
                 font=(theme.FONT, 10)).pack(anchor="w")
    box = tk.Frame(frame, bg=theme.LINE, padx=1, pady=1)
    box.pack(fill="both", expand=True, pady=px(12, 0))
    text_box = tk.Text(box, wrap="char", height=height, width=width, font=(theme.FONT, 10), bg=theme.BAR,
                       fg=theme.NAVY, relief="flat", highlightthickness=0, padx=px(12), pady=px(6),
                       cursor="arrow", spacing1=px(1), spacing3=px(1))
    scroll = ttk.Scrollbar(box, orient="vertical", command=text_box.yview)
    text_box.configure(yscrollcommand=scroll.set)
    text_box.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    foot = tk.Frame(frame, bg=theme.SURFACE)
    foot.pack(fill="x", pady=px(14, 0))
    link = tk.Label(foot, text=link_text, bg=theme.SURFACE, fg=theme.BLUE, cursor="hand2",
                    font=(theme.FONT, 10, "underline"))
    link.pack(side="left")
    link.bind("<Button-1>", lambda _: on_link())
    buttons = tk.Frame(foot, bg=theme.SURFACE)
    buttons.pack(side="right")
    dialog.protocol("WM_DELETE_WINDOW", on_close)
    dialog.bind("<Escape>", lambda _: on_close())
    return dialog, text_box, state_label, buttons


def open_edit_track_dialog(parent, px, track, on_save):
    """Modal editor for one iPod library track's title, artist and album."""
    dialog = tk.Toplevel(parent)
    dialog.title("编辑 iPod 曲库信息")
    dialog.configure(bg=theme.SURFACE)
    parent.after(60, theme.title_bar, dialog)
    dialog.transient(parent)
    dialog.grab_set()
    dialog.resizable(False, False)
    values = []
    for i, (label, value) in enumerate((("歌名", track.title), ("歌手", track.artist), ("专辑", track.album))):
        ttk.Label(dialog, text=label).grid(row=i, column=0, padx=px(15), pady=px(8))
        field = ttk.Entry(dialog, width=38)
        field.insert(0, value)
        field.grid(row=i, column=1, padx=px(0, 15), pady=px(8))
        values.append(field)

    def save(*_):
        title, artist, album = (v.get().strip() for v in values)
        if not title:
            values[0].focus_set()
            return
        dialog.destroy()
        on_save(title, artist, album)

    ttk.Button(dialog, text="保存到 iPod", command=save).grid(row=3, column=1, sticky="e", padx=px(15), pady=px(11))
    dialog.bind("<Return>", save)
    dialog.bind("<Escape>", lambda _: dialog.destroy())
    values[0].focus_set()
    return dialog
