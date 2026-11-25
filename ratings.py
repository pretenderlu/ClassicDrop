"""Inline star-rating column interaction, hover slots and debounced batch saving."""

from tkinter import font as tkfont

from library import listening


class RatingMixin:
    """Mixin for App managing the 评级 column hover, click targets and debounced writes."""

    _SPACE_CHARS = "            "

    def _rating_column(self):
        return next((f"#{i + 1}" for i, (name, _) in enumerate(self.columns_shown or ()) if name == "评级"), None)

    def _rating(self, track):
        return self.pending_ratings.get(
            track.track_id, self.listening.get(track.track_id, (track.rating, track.play_count))[0])

    @staticmethod
    def _spaces_for(target, font):
        """Space characters adding up to (or closest to) the target pixel width."""
        options = sorted(((w, c) for c in RatingMixin._SPACE_CHARS if (w := font.measure(c)) > 0), reverse=True)
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

    def _cancel_rating_timer(self):
        if self.rating_timer:
            self.window.after_cancel(self.rating_timer)
            self.rating_timer = None

    def _undo_ratings(self):
        pending, self.pending_ratings = self.pending_ratings, {}
        self._cancel_rating_timer()
        for iid, (record, _) in self.rows.items():
            if getattr(record, "track_id", None) in pending:
                self._set_rating(iid, dots=iid == self.hover_row)
        self._show_pending()

    def _arm_rating_timer(self):
        self._cancel_rating_timer()
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
            self._commit_ratings(dev, report, ratings)
            new = dev.reload()
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
        self._cancel_rating_timer()
        ids = {t.track_id for t in dev.library.snapshot.tracks}
        batch = {tid: rating for tid, rating in self.pending_ratings.items() if tid in ids}
        self.pending_ratings = {}
        self._show_pending()
        if not batch:
            then()
            return
        self._write_ratings(batch, "", after=then)
