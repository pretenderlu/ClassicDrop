"""Colour themes, DPI scaling and ttk styles shared by the main window and the import preview.

The colour names below are module attributes that use() replaces. Read them as theme.NAVY at the moment of
drawing, and avoid importing them by name, so that a theme change reaches every widget.
"""

import ctypes
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageDraw

FONT = "Microsoft YaHei UI"
THEMES = {"light": "浅色", "dark": "深色"}
PALETTES = {
    "light": dict(
        NAVY="#18283e", BLUE="#225eab", MUTED="#526378", LINE="#d6dfe9", SIDE="#edf2f7", SURFACE="#ffffff",
        BAR="#f5f7fa", STRIPE="#f8fafc", HOVER="#e1ebf8", FIELD_LINE="#c3cedb", FAINT="#8b99ab", HOVER_LINE="#8fb0dc",
        NAV_HOVER="#e1e8f1", SELECTED="#dbe9fb", SELECTED_TEXT="#122e53", PRESSED="#cfdcee", ACCENT_TEXT="#ffffff",
        ACCENT_HOVER="#2f72c8", ACCENT_PRESSED="#184a88", OFF_FILL="#dfe6ef", OFF_TEXT="#576985", THUMB="#c9d2de",
        THUMB_HOVER="#a3afc0", THUMB_PRESSED="#8696aa", OK_FILL="#e2f3e8", OK_TEXT="#1c6638", WARN_FILL="#fbefd3",
        WARN_TEXT="#7d5200", FLOW="#f1f4f8", FLOW_TITLE="#18283e", FLOW_DETAIL="#526378", FLOW_MIRROR=0.42,
        FLOW_SHADE=0.16),
    "dark": dict(
        NAVY="#e4e9f0", BLUE="#3f86e6", MUTED="#9aa7b8", LINE="#2c3543", SIDE="#151a22", SURFACE="#1c222c",
        BAR="#171c25", STRIPE="#202733", HOVER="#2a3445", FIELD_LINE="#3b4758", FAINT="#6c798b", HOVER_LINE="#5b7fb5",
        NAV_HOVER="#1e2530", SELECTED="#2b4266", SELECTED_TEXT="#ffffff", PRESSED="#33415a", ACCENT_TEXT="#ffffff",
        ACCENT_HOVER="#5797ee", ACCENT_PRESSED="#3572c8", OFF_FILL="#272e3a", OFF_TEXT="#75828f", THUMB="#3c4758",
        THUMB_HOVER="#566479", THUMB_PRESSED="#6f7e96", OK_FILL="#1c3a29", OK_TEXT="#86d8a5", WARN_FILL="#3f3219",
        WARN_TEXT="#e8c27c", FLOW="#0f131a", FLOW_TITLE="#ffffff", FLOW_DETAIL="#9aa7b8", FLOW_MIRROR=0.5,
        FLOW_SHADE=0.22),
}
CURRENT = "light"
ICON = None  # path of the .ico file, set by the application


def use(name):
    """Switch the colours that later drawing reads. Widgets already on screen keep theirs until rebuilt."""
    global CURRENT
    CURRENT = name if name in PALETTES else "light"
    globals().update(PALETTES[CURRENT])


use(CURRENT)

PLAYERS = {  # body top, body bottom, edge, screen, wheel, wheel edge, centre button
    "Silver": ("#f3f5f7", "#c6ccd4", "#a3acb7", "#1d2733", "#fbfcfd", "#c3cad3", "#d5dae1"),
    "Black": ("#45484e", "#191b1f", "#0c0d0f", "#07080a", "#2c2e33", "#4a4d53", "#121316"),
}


def player_image(height, finish=None):
    """An iPod classic drawn in the given finish. None gives a neutral outline for an unknown or absent device."""
    top, bottom, edge, screen, wheel, wheel_edge, centre = PLAYERS.get(finish) or (
        BAR, SIDE, FIELD_LINE, FIELD_LINE, SURFACE, FIELD_LINE, SIDE)
    w, h = 480, 800
    ramp = Image.linear_gradient("L").resize((w, h))
    body = Image.composite(Image.new("RGBA", (w, h), bottom), Image.new("RGBA", (w, h), top), ramp)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((6, 6, w - 7, h - 7), radius=58, fill=255)
    image = Image.new("RGBA", (w, h))
    image.paste(body, mask=mask)
    pen = ImageDraw.Draw(image)
    pen.rounded_rectangle((6, 6, w - 7, h - 7), radius=58, outline=edge, width=12)
    pen.rounded_rectangle((52, 54, w - 53, 356), radius=16, fill=screen)
    if finish in PLAYERS:
        # A soft reflection across the glass.
        gloss = Image.new("RGBA", (w, h))
        ImageDraw.Draw(gloss).polygon(((52, 54), (300, 54), (120, 356), (52, 356)), fill=(255, 255, 255, 22))
        image.alpha_composite(gloss)
    pen.ellipse((84, 426, w - 85, 738), fill=wheel, outline=wheel_edge, width=8)
    pen.ellipse((186, 528, w - 187, 636), fill=centre, outline=wheel_edge, width=6)
    return image.resize((round(height * w / h), height), Image.Resampling.LANCZOS)


def blank_cover(side):
    """Stand-in for a song or album without artwork."""
    image = Image.new("RGB", (side * 4, side * 4), SIDE)
    pen = ImageDraw.Draw(image)
    unit = side / 10
    pen.ellipse([round(v * unit * 4) for v in (2.6, 5.6, 5, 8)], fill=FIELD_LINE)
    pen.rectangle([round(v * unit * 4) for v in (4.4, 2.4, 5, 6.8)], fill=FIELD_LINE)
    pen.polygon([round(v * unit * 4) for v in (4.4, 2.4, 7.4, 1.6, 7.4, 3.2, 4.4, 4)], fill=FIELD_LINE)
    return image.resize((side, side), Image.Resampling.LANCZOS)


def scaler(widget):
    zoom = widget.winfo_fpixels("1i") / 96
    def px(*units):
        scaled = tuple(round(u * zoom) for u in units)
        return scaled[0] if len(scaled) == 1 else scaled
    return px


def title_bar(window):
    """Give a window sharp icons and a title bar that matches the theme. Older systems ignore the colour request."""
    try:
        handle = int(window.wm_frame(), 16)
        user = ctypes.windll.user32
        if ICON:
            # Tk only ever picks the 16 and 32 pixel frames and lets Windows stretch them. Load the exact sizes.
            user.LoadImageW.restype = ctypes.c_void_p
            user.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            user.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]
            for kind, metric in ((0, 49), (1, 11)):  # small for the title bar, large for the taskbar and Alt+Tab
                size = user.GetSystemMetrics(metric)
                icon = user.LoadImageW(None, str(ICON), 1, size, size, 0x10)
                if icon:
                    user.SendMessageW(handle, 0x80, kind, icon)
        dark = ctypes.c_int(CURRENT == "dark")
        ctypes.windll.dwmapi.DwmSetWindowAttribute(handle, 20, ctypes.byref(dark), ctypes.sizeof(dark))
    except (AttributeError, OSError, ValueError, tk.TclError):
        pass


def flat_button(style, name, *, padding, fill, edge, hover, text=None, off_fill=None, off_text=None, **extra):
    off_fill = fill if off_fill is None else off_fill
    off_edge = off_fill if edge in (fill, BLUE) else LINE
    pressed = ACCENT_PRESSED if fill == BLUE else PRESSED
    style.configure(name, padding=padding, width=0, background=fill, bordercolor=edge, lightcolor=fill,
                    darkcolor=fill, foreground=text or NAVY, focuscolor=fill, **extra)
    fills = [("disabled", off_fill), ("pressed", pressed), ("active", hover)]
    style.map(name, background=fills, lightcolor=fills, darkcolor=fills, focuscolor=fills,
              bordercolor=[("disabled", off_edge), ("focus", BLUE), ("pressed", pressed if edge == fill else BLUE),
                           ("active", hover if edge == fill else HOVER_LINE if edge != BLUE else hover)],
              foreground=[("disabled", off_text or FAINT)])


def apply(root, px):
    """Configure every ttk style for the current theme. Safe to call again after use()."""
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", font=(FONT, 10), background=SURFACE, foreground=NAVY)
    flat_button(style, "TButton", padding=px(11, 5), fill=SURFACE, edge=FIELD_LINE, hover=HOVER)
    flat_button(style, "Tool.TButton", padding=px(9, 5), fill=SURFACE, edge=SURFACE, hover=HOVER)
    flat_button(style, "Picked.Tool.TButton", padding=px(9, 5), fill=SELECTED, edge=SELECTED, hover=SELECTED,
                text=SELECTED_TEXT)
    flat_button(style, "Accent.TButton", padding=px(17, 6), fill=BLUE, edge=BLUE, hover=ACCENT_HOVER,
                text=ACCENT_TEXT, off_fill=OFF_FILL, off_text=OFF_TEXT, font=(FONT, 10, "bold"))
    for field in ("TEntry", "TCombobox"):
        style.configure(field, padding=px(5, 3), fieldbackground=SURFACE, foreground=NAVY, bordercolor=FIELD_LINE,
                        lightcolor=SURFACE, darkcolor=SURFACE, insertcolor=NAVY, selectbackground=BLUE,
                        selectforeground=ACCENT_TEXT)
        style.map(field, bordercolor=[("focus", BLUE)], lightcolor=[("focus", SURFACE)],
                  fieldbackground=[("disabled", BAR)], foreground=[("disabled", FAINT)])
    style.configure("TCombobox", background=SURFACE, arrowcolor=MUTED, arrowsize=px(13))
    style.map("TCombobox", background=[("disabled", BAR), ("active", HOVER)], arrowcolor=[("disabled", FAINT)],
              fieldbackground=[("disabled", BAR), ("readonly", SURFACE)],
              selectbackground=[("readonly", SURFACE)], selectforeground=[("readonly", NAVY)])
    root.option_add("*TCombobox*Listbox.background", SURFACE)
    root.option_add("*TCombobox*Listbox.foreground", NAVY)
    root.option_add("*TCombobox*Listbox.selectBackground", SELECTED)
    root.option_add("*TCombobox*Listbox.selectForeground", SELECTED_TEXT)
    style.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
        ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
    style.configure("Vertical.TScrollbar", troughcolor=SURFACE, background=THUMB, bordercolor=SURFACE,
                    lightcolor=THUMB, darkcolor=THUMB, gripcount=0, arrowsize=px(9), relief="flat")
    thumb = [("disabled", SURFACE), ("pressed", THUMB_PRESSED), ("active", THUMB_HOVER)]
    style.map("Vertical.TScrollbar", background=thumb, lightcolor=thumb, darkcolor=thumb)
    style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=NAVY,
                    rowheight=px(32), borderwidth=0, bordercolor=LINE, lightcolor=SURFACE, darkcolor=SURFACE)
    style.map("Treeview", background=[("selected", SELECTED)], foreground=[("selected", SELECTED_TEXT)],
              bordercolor=[("focus", FIELD_LINE)])
    # Tk fills an image element by tiling it, so a tiny image makes every repaint slow. Keep it large.
    if not hasattr(root, "heading_underline"):
        root.heading_underline = tk.PhotoImage(master=root, width=512, height=64)
        style.element_create("Underline.heading", "image", root.heading_underline, border=(0, 0, 0, 1), sticky="nsew")
        def point(event):
            if isinstance(event.widget, ttk.Button):
                event.widget.configure(cursor="" if event.widget.instate(["disabled"]) else "hand2")
        root.bind_class("TButton", "<Enter>", point, add="+")
    root.heading_underline.put(SURFACE, to=(0, 0, 512, 63))
    root.heading_underline.put(LINE, to=(0, 63, 512, 64))
    style.layout("Treeview.Heading", [("Underline.heading", {"sticky": "nswe"}), ("Treeheading.padding", {
        "sticky": "nswe", "children": [("Treeheading.text", {"sticky": "we"})]})])
    style.configure("Treeview.Heading", foreground=MUTED, font=(FONT, 9, "bold"), padding=px(7, 6))
    style.layout("Nav.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    style.layout("Nav.Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
        ("Treeitem.text", {"sticky": "we"})]})])
    style.configure("Nav.Treeview", background=SIDE, fieldbackground=SIDE, rowheight=px(30))
    style.configure("Nav.Treeview.Item", padding=px(9, 0))
    style.layout("Group.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    style.layout("Group.Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
        ("Treeitem.image", {"side": "left", "sticky": ""}), ("Treeitem.text", {"sticky": "we"})]})])
    style.configure("Group.Treeview", background=SURFACE, fieldbackground=SURFACE, rowheight=px(52))
    style.configure("Group.Treeview.Item", padding=px(7, 0))
    style.configure("TProgressbar", troughcolor=LINE, background=BLUE, bordercolor=LINE, lightcolor=BLUE,
                    darkcolor=BLUE, thickness=px(6))
    style.configure("TSeparator", background=LINE, lightcolor=LINE, darkcolor=LINE, bordercolor=LINE)
    return style


def hover_rows(tree, colour):
    """Tint the row under the pointer. Selected rows keep the selection colour."""
    tree.tag_configure("hover", background=colour)
    def move(event=None):
        row = tree.identify_row(event.y) if event else ""
        for item in tree.tag_has("hover"):
            if item != row:
                tree.item(item, tags=[t for t in tree.item(item, "tags") if t != "hover"])
        if row and not tree.tag_has("hover", row) and "group" not in tree.item(row, "tags"):
            tree.item(row, tags=(*tree.item(row, "tags"), "hover"))
        tree.configure(cursor="hand2" if row and "group" not in tree.item(row, "tags") else "")
    tree.bind("<Motion>", move, add="+")
    tree.bind("<Leave>", lambda _: move(), add="+")


def stripe(tree):
    tree.tag_configure("odd", background=STRIPE)
    for index, item in enumerate(tree.get_children()):
        tree.item(item, tags=("odd",) if index % 2 else ())


class Placeholder:
    """Hint text shown inside an empty, unfocused entry. The entry's own value stays empty."""

    def __init__(self, entry, variable, text, px):
        self.variable, self.focused = variable, False
        self.label = ttk.Label(entry, text=text, foreground=FAINT, background=SURFACE, cursor="xterm")
        self.inset = px(7)
        self.label.bind("<Button-1>", lambda _: entry.focus_set())
        entry.bind("<FocusIn>", lambda _: self.update(True), add="+")
        entry.bind("<FocusOut>", lambda _: self.update(False), add="+")
        watch = variable.trace_add("write", lambda *_: self.update())
        # The variable outlives the entry when the window is rebuilt for another theme.
        self.label.bind("<Destroy>", lambda _: variable.trace_remove("write", watch))
        self.update()

    def update(self, focused=None):
        if focused is not None:
            self.focused = focused
        if self.variable.get() or self.focused:
            self.label.place_forget()
        else:
            self.label.place(x=self.inset, rely=0.5, anchor="w")
