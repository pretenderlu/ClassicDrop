import struct
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_classicdrop import FIXTURES, Device, mock_device
from test_native_covers import covered_m4a
import app
from library import PLAY_COUNTS, listening
from transfer import transfer


def write_play_counts(root, rows, width=28):
    """One (plays, rating) row per track in library order. A rating of None leaves the library's own."""
    body = b"".join(struct.pack("<7I", plays, 0, 0, 0xFFFFFFFF if rating is None else rating, 0, 0, 0)[:width]
                    for plays, rating in rows)
    path = root / PLAY_COUNTS
    path.write_bytes(b"mhdp" + struct.pack("<3I", 96, width, len(rows)) + bytes(80) + body)
    return path


class ListeningTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.site = Path(temp.name)
        self.root = self.site / "iPod"
        transfer(mock_device(self.root), [FIXTURES / "tone.mp3", FIXTURES / "aac.m4a", FIXTURES / "lossless.m4a"],
                 self.site / "backups")
        self.device = Device.open(self.root)
        self.ids = [t.track_id for t in self.device.library.snapshot.tracks]

    def heard(self):
        found = listening(self.device)
        return [found[i] for i in self.ids]

    def test_stars_and_plays_from_the_ipod_are_laid_over_the_library(self):
        self.assertEqual(self.heard(), [(0, 0)] * 3)
        file = write_play_counts(self.root, [(3, 100), (0, None), (1, 50)])
        recorded = file.read_bytes()
        self.assertEqual(self.heard(), [(100, 3), (0, 0), (50, 1)])
        # Reading changes neither the file nor the library that is used for writing.
        self.assertEqual(file.read_bytes(), recorded)
        self.assertEqual([(t.rating, t.play_count) for t in self.device.library.snapshot.tracks], [(0, 0)] * 3)
        # Rows written before more songs arrived still belong to the first tracks.
        transfer(self.device, [covered_m4a(self.site / "new.m4a")], self.site / "backups")
        self.device = Device.open(self.root)
        self.assertEqual((file.read_bytes(), len(self.device.library.snapshot.tracks)), (recorded, 4))
        self.assertEqual(sorted(listening(self.device).values()), [(0, 0), (0, 0), (50, 1), (100, 3)])

    def test_unusable_records_fall_back_to_the_library(self):
        write_play_counts(self.root, [(1, 101), (0, 0), (0, 0)])  # an impossible rating
        self.assertEqual(self.heard(), [(0, 0)] * 3)
        # Rows can outlive songs that were removed. The engine applies those that still have a track.
        write_play_counts(self.root, [(1, 100), (2, 80), (3, 60), (4, 40), (5, 20)])
        self.assertEqual(self.heard(), [(100, 1), (80, 2), (60, 3)])
        (self.root / PLAY_COUNTS).write_bytes(b"not a play counts file")
        self.assertEqual(self.heard(), [(0, 0)] * 3)
        write_play_counts(self.root, [(2, 80)] * 3, width=12)  # the oldest layout has no rating
        self.assertEqual(self.heard(), [(0, 2)] * 3)

    def test_table_shows_stars_and_plays_and_search_ignores_them(self):
        write_play_counts(self.root, [(3, 100), (0, None), (12, 50)])
        window = app.TkinterDnD.Tk()
        def cleanup():
            for timer in window.tk.call("after", "info"):
                window.after_cancel(timer)
            window.destroy()
        self.addCleanup(cleanup)
        window.geometry("1100x700+28000+28000")
        ui = app.App(window)
        def settle():
            end = time.monotonic() + 20
            while time.monotonic() < end:
                window.update()
                if not ui.busy:
                    window.update()
                    return
                time.sleep(0.01)
            self.fail("GUI operation timed out")
        def rows():
            return [tuple(str(v) for v in ui.tree.item(i, "values")[-3:-1]) for i in ui.tree.get_children()]
        with patch.object(app, "BACKUPS", self.site / "backups"):
            ui.load(self.root)
            settle()
            self.assertEqual([ui.tree.heading(c, "text") for c in ui.tree["columns"]], ["歌名", "歌手", "专辑", "评级", "播放", "时长"])
            self.assertEqual(rows(), [("★★★★★", "3"), ("", ""), ("★★½", "12")])
            ui.search.set("12")
            ui.render()
            self.assertEqual(rows(), [])
            ui.search.set("")
            ui.set_browse("album")
            self.assertEqual([ui.tree.heading(c, "text") for c in ui.tree["columns"]], ["#", "歌名", "歌手", "评级", "播放", "时长"])
            self.assertIn(("★★★★★", "3"), rows())
            ui.view = "queue"
            ui.render()
            self.assertNotIn("评级", [ui.tree.heading(c, "text") for c in ui.tree["columns"]])
