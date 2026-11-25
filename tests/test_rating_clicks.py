from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
import unittest
from tempfile import TemporaryDirectory

from test_classicdrop import FIXTURES, ROOT, mock_device
sys.path.insert(0, str(ROOT))
import app
from library import Device, listening
from transfer import transfer

FILES = [FIXTURES / "tone.mp3", FIXTURES / "aac.m4a", FIXTURES / "lossless.m4a"]


def fake_event(x=0, y=0):
    return SimpleNamespace(x=x, y=y)


class RatingClickTests(unittest.TestCase):
    """iPod-style stars in the 评级 column: hover shows the slots, clicks queue one batched write."""

    def setUp(self):
        import logging
        logging.disable(logging.NOTSET)
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.site = Path(temp.name).resolve()
        window = self.window = app.TkinterDnD.Tk()
        def cleanup():
            for timer in window.tk.call("after", "info"):
                window.after_cancel(timer)
            if window.winfo_exists():
                window.destroy()
        self.addCleanup(cleanup)
        window.geometry("1100x700+28000+28000")
        self.ui = app.App(window)
        patcher = patch.object(app, "BACKUPS", self.site / "backups")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(app, "check_for_updates", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = self.site / "ipod"
        device = mock_device(self.root)
        transfer(device, FILES, self.site / "backups")
        self.device = Device.open(self.root)
        self.ui.device = self.device
        self.ui.listening = listening(self.device)
        self.ui.render()
        window.update()
        self.ids = [t.track_id for t in self.device.library.snapshot.tracks]
        self.column = self.ui._rating_column()

    def settle(self, until=lambda: True, seconds=10):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.window.update()
            if not self.ui.busy and until():
                self.window.update()
                return
            time.sleep(.01)
        self.fail("window never settled")

    def cell(self, iid):
        return self.ui.tree.set(iid, self.column)

    def row_y(self, iid):
        return self.ui.tree.bbox(iid)[1] + 2

    def slot_x(self, iid, slot):
        """Centre x of the given slot inside the rating cell; slots are fixed one-star-wide targets."""
        x, _, _, _ = self.ui.tree.bbox(iid, self.column)
        star = self.ui._rating_slots()[0]
        return x + self.ui.px(4) + (slot - 1) * star + star // 2

    @staticmethod
    def glyphs(text):
        return "".join(c for c in text if c in "★½·")

    def click(self, iid, slot):
        self.ui._rating_click(fake_event(self.slot_x(iid, slot), self.row_y(iid)))

    def test_hover_shows_slots_only_on_the_hovered_row(self):
        self.assertEqual(self.cell("0"), "")
        self.ui._rating_hover(fake_event(y=self.row_y("0")))
        self.assertEqual(self.glyphs(self.cell("0")), "·····")
        self.assertEqual(self.cell("1"), "")
        self.ui._rating_hover(fake_event(y=self.row_y("1")))
        self.assertEqual(self.cell("0"), "")
        self.assertEqual(self.glyphs(self.cell("1")), "·····")
        self.ui._rating_hover(None)
        self.assertEqual(self.cell("1"), "")
        # A rated row keeps its stars and only fills the leftover slots with dots.
        self.ui.pending_ratings[self.ids[0]] = 60
        self.ui._rating_hover(fake_event(y=self.row_y("0")))
        self.assertEqual(self.glyphs(self.cell("0")), "★★★··")
        self.ui._rating_hover(None)
        self.assertEqual(self.cell("0"), "★★★")

    def test_all_five_slots_share_the_star_width(self):
        self.ui._rating_hover(fake_event(y=self.row_y("0")))
        text, font = self.cell("0"), self.ui._rating_font()
        star = font.measure("★")
        per = len(text) // 5
        for slot in range(5):
            slot_text = text[slot * per:(slot + 1) * per]
            self.assertIn("·", slot_text)
            self.assertAlmostEqual(font.measure(slot_text), star, delta=2)
        # The same slot sits at the same x whether the row is rated or not.
        self.ui._rating_hover(fake_event(y=self.row_y("1")))
        self.ui.pending_ratings[self.ids[1]] = 60
        self.ui._set_rating("1", dots=True)
        for slot in (1, 3, 5):
            self.assertEqual(self.slot_x("0", slot), self.slot_x("1", slot))
        self.assertEqual(self.glyphs(self.cell("1")), "★★★··")

    def test_clicking_slots_rates_and_the_top_star_clears(self):
        self.ui.tree.selection_set("0")
        for slot in (1, 2, 3, 4, 5):
            self.ui._rating_hover(fake_event(y=self.row_y("0")))
            self.click("0", slot)
            self.assertEqual(self.ui.pending_ratings[self.ids[0]], slot * 20)
        self.assertEqual(self.ui.tree.selection(), ("0",))
        # Clicking the slot matching the current whole-star rating clears it.
        self.click("0", 5)
        self.assertEqual(self.ui.pending_ratings[self.ids[0]], 0)
        self.assertEqual(self.glyphs(self.cell("0")), "·····")
        self.assertEqual(self.ui.tree.selection(), ("0",))

    def test_three_quick_ratings_are_saved_in_one_real_write(self):
        calls = []
        with patch.object(app, "transfer", wraps=lambda *a, **k: (calls.append(k["ratings"]), transfer(*a, **k))[1]):
            for iid, slot in (("0", 3), ("1", 5), ("2", 1)):
                self.ui._rating_hover(fake_event(y=self.row_y(iid)))
                self.click(iid, slot)
            self.assertEqual(self.ui.rating_count["text"], "有 3 首歌曲的评级待保存")
            self.assertTrue(self.ui.rating_note.winfo_manager())
            self.settle(lambda: len(calls) == 1, seconds=15)
        self.assertEqual(sorted(calls[0].values()), [20, 60, 100])
        self.assertFalse(self.ui.pending_ratings)
        self.assertFalse(self.ui.rating_note.winfo_manager())
        snapshot = Device.open(self.root).library.snapshot
        self.assertEqual({t.track_id: t.rating for t in snapshot.tracks},
                         {self.ids[0]: 60, self.ids[1]: 100, self.ids[2]: 20})
        self.assertEqual(self.cell("0"), "★★★")
        # A second batch right after also saves cleanly, proving the version check still passes.
        self.ui._rating_hover(fake_event(y=self.row_y("0")))
        self.click("0", 4)
        self.settle(lambda: not self.ui.pending_ratings, seconds=15)
        snapshot = Device.open(self.root).library.snapshot
        self.assertEqual(snapshot.tracks[0].rating, 80)

    def test_undo_restores_the_cells_and_writes_nothing(self):
        calls = []
        with patch.object(app, "transfer", side_effect=lambda *a, **k: calls.append(1)):
            self.ui._rating_hover(fake_event(y=self.row_y("0")))
            self.click("0", 3)
            self.assertEqual(self.glyphs(self.cell("0")), "★★★··")
            self.window.update()  # the freshly packed undo link only takes events once mapped
            undo = next(w for w in self.ui.rating_note.winfo_children() if w["text"] == "撤销")
            undo.event_generate("<Button-1>")
            self.window.update()
            self.assertFalse(self.ui.pending_ratings)
            self.assertIsNone(self.ui.rating_timer)
            self.assertEqual(self.glyphs(self.cell("0")), "·····")
            self.ui._rating_hover(None)
            self.assertEqual(self.cell("0"), "")
            self.assertFalse(calls)
            self.assertFalse(self.ui.rating_note.winfo_manager())

    def test_a_busy_window_defers_the_save_and_later_clicks_form_a_second_batch(self):
        clicks = []
        def wrapped(*args, **kwargs):
            clicks.append(kwargs["ratings"])
            time.sleep(.5)  # keep the write busy so the next click lands while it runs
            return transfer(*args, **kwargs)
        self.ui.busy = True
        self.ui._rating_hover(fake_event(y=self.row_y("0")))
        self.click("0", 3)
        self.ui._save_ratings()
        self.assertIsNotNone(self.ui.rating_timer)
        self.assertTrue(self.ui.pending_ratings)
        self.ui.busy = False
        with patch.object(app, "transfer", wraps=wrapped):
            # The timer retries once the window is free; a click during the save forms a second batch.
            clicked = False
            end = time.monotonic() + 15
            while time.monotonic() < end:
                self.window.update()
                if self.ui.busy and clicks and not clicked:
                    self.ui._rating_hover(fake_event(y=self.row_y("1")))
                    self.click("1", 4)
                    clicked = True
                if not self.ui.busy and clicked and len(clicks) == 2 and not self.ui.pending_ratings:
                    break
                time.sleep(.01)
            else:
                self.fail("second batch never saved")
        self.assertEqual(clicks[0], {self.ids[0]: 60})
        self.assertEqual(clicks[1], {self.ids[1]: 80})

    def test_close_and_eject_flush_the_pending_ratings_first(self):
        order = []
        def wrapped(*args, **kwargs):
            order.append("transfer")
            return transfer(*args, **kwargs)
        with patch.object(app, "transfer", wraps=wrapped):
            with patch.object(self.window, "destroy", side_effect=lambda: order.append("destroy")):
                self.ui._rating_hover(fake_event(y=self.row_y("0")))
                self.click("0", 2)
                self.ui.close()
                self.settle(lambda: order == ["transfer", "destroy"], seconds=15)
            self.assertEqual(Device.open(self.root).library.snapshot.tracks[0].rating, 40)
            with patch.object(self.ui, "_eject", side_effect=lambda: order.append("eject")):
                self.ui._rating_hover(fake_event(y=self.row_y("1")))
                self.click("1", 5)
                self.ui.eject()
                self.settle(lambda: order[-1] == "eject" and not self.ui.pending_ratings, seconds=15)
            self.assertEqual(Device.open(self.root).library.snapshot.tracks[1].rating, 100)

    def test_the_light_reload_never_re_reads_covers(self):
        with patch.object(app, "device_covers") as covers:
            self.ui._rating_hover(fake_event(y=self.row_y("0")))
            self.click("0", 3)
            self.settle(lambda: not self.ui.pending_ratings, seconds=15)
        covers.assert_not_called()
        self.assertTrue(self.ui.covers is not None)

    def test_a_read_only_device_ignores_rating_clicks(self):
        from dataclasses import replace
        self.ui.device = replace(self.device, guid=None)
        self.ui._rating_hover(fake_event(y=self.row_y("0")))
        self.click("0", 3)
        self.assertFalse(self.ui.pending_ratings)


if __name__ == "__main__":
    unittest.main()
