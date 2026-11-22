"""A Cover Flow strip: the chosen album faces forward and its neighbours lean away on either side."""

from concurrent.futures import ThreadPoolExecutor
import math
from queue import Empty, SimpleQueue
import tkinter as tk

from PIL import Image, ImageFilter, ImageTk

import theme

STEP, FIRST = 0.26, 0.66  # spacing of the leaning covers and distance of the first one, in cover widths
LEAN = (0.44, 0.82, 0.07, 0.68)  # width, outer height, inner top and inner height of a leaning cover
MIRROR = 0.4  # how much of the cover shows in the reflection


def _perspective(corners, width, height):
    """PIL coefficients that draw a width x height picture into the quadrilateral corners (TL, TR, BR, BL)."""
    rows = []
    for (x, y), (u, v) in zip(corners, ((0, 0), (width, 0), (width, height), (0, height))):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y, u])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y, v])
    for column in range(8):
        pivot = max(range(column, 8), key=lambda r: abs(rows[r][column]))
        rows[column], rows[pivot] = rows[pivot], rows[column]
        lead = rows[column][column]
        rows[column] = [value / lead for value in rows[column]]
        for other in range(8):
            if other != column and rows[other][column]:
                factor = rows[other][column]
                rows[other] = [a - factor * b for a, b in zip(rows[other], rows[column])]
    return [row[8] for row in rows]


def cover_with_reflection(cover, size):
    """The cover above its mirror image: clear where the two meet, then blurring and fading into the background."""
    cover = cover.convert("RGB").resize((size, size), Image.Resampling.LANCZOS)
    depth = round(size * MIRROR)
    mirror = cover.transpose(Image.Transpose.FLIP_TOP_BOTTOM).crop((0, 0, size, depth))
    distance = Image.linear_gradient("L").resize((size, depth))  # 0 at the cover's edge, 255 at the far end
    mirror = Image.composite(mirror.filter(ImageFilter.GaussianBlur(max(1, size * 0.035))), mirror, distance)
    strength = theme.FLOW_MIRROR
    opacity = distance.point([round(255 * strength * (1 - value / 255) ** 1.7) for value in range(256)])
    floor = Image.composite(mirror, Image.new("RGB", (size, depth), theme.FLOW), opacity)
    both = Image.new("RGB", (size, size + depth), theme.FLOW)
    both.paste(cover, (0, 0))
    both.paste(floor, (0, size))
    return both


def leaning_cover(cover, size, left):
    """A cover turned away from the viewer. left is True for covers shown left of the chosen one."""
    scale = 2  # draw large, then shrink, so the slanted edges stay smooth
    flat = cover_with_reflection(cover if left else cover.transpose(Image.Transpose.FLIP_LEFT_RIGHT), size * scale)
    width, outer, inner_top, inner = (round(part * size * scale) for part in LEAN)
    tall = 1 + MIRROR
    corners = ((0, 0), (width, inner_top), (width, inner_top + round(inner * tall)), (0, round(outer * tall)))
    canvas = (width, round(outer * tall))
    shade = Image.blend(flat, Image.new("RGB", flat.size, theme.FLOW), theme.FLOW_SHADE).convert("RGBA")
    turned = shade.transform(canvas, Image.Transform.PERSPECTIVE, _perspective(corners, *flat.size),
                             Image.Resampling.BICUBIC)
    turned = turned.resize((canvas[0] // scale, canvas[1] // scale), Image.Resampling.LANCZOS)
    return turned if left else turned.transpose(Image.Transpose.FLIP_LEFT_RIGHT)


class CoverFlow(tk.Canvas):
    """Shows items as (key, title, detail, art). picture(art) returns (image, quality) or None."""

    def __init__(self, parent, px, *, picture, on_pick, wanted=lambda arts: None):
        super().__init__(parent, bg=theme.FLOW, highlightthickness=0, height=px(236), takefocus=True)
        self.px, self.picture, self.on_pick, self.wanted = px, picture, on_pick, wanted
        self.items = []
        self.position, self.target = 0.0, 0
        self.cache = {}  # (art, kind, size) -> (quality, picture)
        self.spots = []
        self.timer = self.collector = None
        self.blank = theme.blank_cover(128)
        # Leaning covers take several milliseconds each, so they are drawn off the interface thread.
        self.workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="coverflow")
        self.pending = set()
        self.finished = SimpleQueue()
        self.epoch = 0
        self.stand_ins = {}
        # Timers belong to the window, so that closing or rebuilding it never leaves one pointing at this strip.
        self.clock = self.winfo_toplevel()
        self.bind("<Destroy>", lambda _: self._stop(closing=True))
        self.bind("<Configure>", lambda _: self.redraw())
        self.bind("<Button-1>", self._click)
        self.bind("<MouseWheel>", lambda event: self.step(-1 if event.delta > 0 else 1))
        for key, amount in (("<Left>", -1), ("<Right>", 1), ("<Prior>", -5), ("<Next>", 5)):
            self.bind(key, lambda _, amount=amount: self.step(amount))

    def show(self, items, key):
        """Display items with key in front. A changed list jumps, the same list glides."""
        items = list(items)
        same = [i[0] for i in items] == [i[0] for i in self.items]
        self.items = items
        self.target = next((n for n, item in enumerate(items) if item[0] == key), 0)
        if same:
            # A long trip would draw every cover on the way. Start it a few covers short of the destination.
            self.position = min(max(self.position, self.target - 5), self.target + 5)
            self._glide()
        else:
            self.position = float(self.target)
            self.redraw()

    def refresh(self, art):
        """A sharper picture for art has arrived. The current one stays until its replacement is drawn."""
        self.redraw()

    def forget(self):
        """Drop every drawn cover, including ones still being drawn, because the pictures behind them changed."""
        self.epoch += 1
        self.cache.clear()
        self.pending.clear()

    def step(self, amount):
        if self.items:
            chosen = min(len(self.items) - 1, max(0, self.target + amount))
            if chosen != self.target:
                self.on_pick(self.items[chosen][0])
        return "break"

    def _click(self, event):
        self.focus_set()
        for index, left, right in reversed(self.spots):
            if left <= event.x < right:
                if index != self.target:
                    self.on_pick(self.items[index][0])
                break

    def _stop(self, closing=False):
        if self.timer:
            self.clock.after_cancel(self.timer)
            self.timer = None
        if closing:
            if self.collector:
                self.clock.after_cancel(self.collector)
                self.collector = None
            self.workers.shutdown(wait=False, cancel_futures=True)

    def _glide(self):
        self._stop()
        if not self.winfo_exists():
            return
        gap = self.target - self.position
        if abs(gap) < 0.02:
            self.position = float(self.target)
        else:
            self.position += gap * 0.32
            self.timer = self.clock.after(16, self._glide)
        self.redraw()

    def _image(self, art, kind, size):
        """The picture to draw now. A leaning cover that is not ready yet is ordered and a stand-in returned."""
        found = self.picture(art)
        source, quality = found if found else (self.blank, 0)
        name = (art, kind, size)
        have = self.cache.get(name)
        if have and have[0] >= quality:
            return have[1]
        if kind == "front":
            if len(self.cache) > 700:
                self.cache.clear()
            self.cache[name] = quality, ImageTk.PhotoImage(cover_with_reflection(source, size), master=self)
            return self.cache[name][1]
        job = (*name, quality, self.epoch)
        if job not in self.pending:
            self.pending.add(job)
            self.workers.submit(self._draw, job, source)
            if self.collector is None:
                self.collector = self.clock.after(12, self._collect)
        if have:
            return have[1]
        if (kind, size) not in self.stand_ins:
            # A plain panel, quieter than a row of placeholder pictures during the moment it is visible.
            plain = Image.new("RGB", (64, 64), theme.LINE)
            self.stand_ins[kind, size] = ImageTk.PhotoImage(leaning_cover(plain, size, kind == "left"), master=self)
        return self.stand_ins[kind, size]

    def _draw(self, job, source):
        try:
            picture = leaning_cover(source, job[2], job[1] == "left")
        except Exception:
            picture = None
        self.finished.put((job, picture))

    def _collect(self):
        """Take finished covers from the background threads and show them."""
        self.collector = None
        if not self.winfo_exists():
            return
        arrived = 0
        try:
            while arrived < 8:  # a few at a time keeps each pause short
                job, picture = self.finished.get_nowait()
                self.pending.discard(job)
                art, kind, size, quality, epoch = job
                have = self.cache.get((art, kind, size))
                if picture is not None and epoch == self.epoch and not (have and have[0] >= quality):
                    self.cache[art, kind, size] = quality, ImageTk.PhotoImage(picture, master=self)
                    arrived += 1
        except Empty:
            pass
        if arrived:
            self.redraw()
        if (self.pending or not self.finished.empty()) and self.collector is None:
            self.collector = self.clock.after(12, self._collect)

    def redraw(self):
        self.delete("all")
        self.spots = []
        width, height = self.winfo_width(), self.winfo_height()
        if not self.items or width < 20 or height < 20:
            return
        size = max(24, round(height * 0.52))
        middle, top = width // 2, round(height * 0.07)
        reach = math.ceil((width / 2 / size - FIRST) / STEP) + 2
        nearest = round(self.position)
        visible = range(max(0, nearest - reach), min(len(self.items), nearest + reach + 1))
        for index in sorted(visible, key=lambda i: -abs(i - self.position)):
            offset = index - self.position
            art = self.items[index][3]
            if abs(offset) < 0.5:
                x = middle + round(offset * FIRST * size)
                self.create_image(x, top, anchor="n", image=self._image(art, "front", size))
                self.spots.append((index, x - size // 2, x + size // 2))
            else:
                side = -1 if offset < 0 else 1
                x = middle + round(side * (FIRST + (abs(offset) - 1) * STEP) * size)
                lean = round(LEAN[0] * size)
                self.create_image(x, top + round(size * (1 - LEAN[1]) * 0.9), anchor="n",
                                  image=self._image(art, "left" if side < 0 else "right", size))
                self.spots.append((index, x - lean // 2, x + lean // 2))
        key, title, detail, _ = self.items[self.target]
        base = height - self.px(34)
        self.create_text(middle, base, text=title, fill=theme.FLOW_TITLE, font=(theme.FONT, 11, "bold"), width=width - self.px(40))
        self.create_text(middle, base + self.px(19), text=detail, fill=theme.FLOW_DETAIL, font=(theme.FONT, 9))
        if self.timer is None:
            self.wanted([self.items[i][3] for i in sorted(visible, key=lambda i: abs(i - self.target))[:7]])
            # Order ahead what the next few steps will need: both leanings near the front, one further out.
            for index in range(max(0, self.target - reach - 4), min(len(self.items), self.target + reach + 5)):
                for kind in (("left", "right") if abs(index - self.target) <= 3 else
                             ("left",) if index < self.target else ("right",)):
                    self._image(self.items[index][3], kind, size)
