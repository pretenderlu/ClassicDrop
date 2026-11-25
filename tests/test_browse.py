from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from test_classicdrop import mock_device
from test_formats_playlists import write_wave
from test_import import scan
from test_native_covers import picture
import app
import theme
from library import Device, playlist_edit
from native_covers import device_covers, load_cover
from transfer import transfer


class BrowseTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.site = Path(temp.name).resolve()
        self.root = self.site / "iPod"
        device = mock_device(self.root)
        sources = []
        for folder, names in (("甲", ("一", "二")), ("乙", ("三",))):
            (self.site / folder).mkdir()
            for name in names:
                sources.append(self.site / folder / f"{name}.wav")
                write_wave(sources[-1], rate=(44100, 48000, 32000)[len(sources) - 1])
        (self.site / "甲/cover.png").write_bytes(picture("red"))
        songs, errors = scan(sources)
        self.assertEqual(errors, [])
        reviewed = {s.path: replace(s, album=s.path.parent.name, artist="歌手" + s.path.parent.name) for s in songs}
        transfer(device, tuple(reviewed), self.site / "backup", formats={p: s.mode for p, s in reviewed.items()},
                 reviewed=reviewed)
        device = Device.open(self.root)
        tracks = device.library.snapshot.tracks
        transfer(device, [], self.site / "backup",
                 desired=playlist_edit(device.library.snapshot, [t.track_id for t in tracks], name="路上"))
        window = app.TkinterDnD.Tk()
        def cleanup():
            for timer in window.tk.call("after", "info"):
                window.after_cancel(timer)
            window.destroy()
        self.addCleanup(cleanup)
        self.window, self.ui = window, app.App(window)
        window.geometry("1100x640+28000+28000")
        patcher = patch.object(app, "BACKUPS", self.site / "backup")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ui.load(self.root)
        self.settle()

    def settle(self):
        end = time.monotonic() + 20
        while time.monotonic() < end:
            self.window.update()
            if not self.ui.busy:
                self.window.update()
                return
            time.sleep(0.01)
        self.fail("GUI operation timed out")

    def pick(self, group):
        self.ui.groups.selection_set(self.ui.groups.get_children()[self.ui.group_keys.index(group)])
        self.window.update()

    def titles(self):
        tree = self.ui.tree
        column = [tree.heading(c, "text") for c in tree["columns"]].index("歌名")
        return [tree.item(i, "values")[column] for i in tree.get_children()]

    def flow(self):
        return [item[0] for item in self.ui.flow.items], self.ui.flow.items[self.ui.flow.target][0]

    def test_device_covers_are_read_small_now_and_large_on_demand(self):
        device = Device.open(self.root)
        small, large = device_covers(device)
        with_art = {t.artwork_id for t in device.library.snapshot.tracks if t.album == "甲"}
        self.assertEqual((set(small), set(large)), (with_art, with_art))
        for art, image in small.items():
            self.assertEqual(image.size, (128, 128))
            sharp = load_cover(self.root, large[art])
            self.assertEqual(sharp.size, (320, 320))
            for picture_ in (image, sharp):
                red, green, blue = picture_.getpixel((20, 20))
                self.assertGreater(red, 240)
                self.assertLess(green + blue, 20)
        ui = self.ui
        # The large pictures are read in the background as soon as the strip shows them, so they may already be here.
        self.assertEqual(set(ui.covers), with_art)
        ui.set_browse("album")
        self.pick("甲")
        deadline = time.monotonic() + 10
        while not ui.sharp_covers and time.monotonic() < deadline:
            self.window.update()
            time.sleep(0.01)
        self.assertEqual({a: i.size for a, i in ui.sharp_covers.items()}, {a: (320, 320) for a in with_art})
        self.assertEqual(ui.cover_pending, set())
        ui.busy = True
        ui.sharp_covers.clear()
        ui._want_covers(with_art)
        self.assertEqual(ui.cover_pending, set())
        ui.busy = False
        (self.root / "iPod_Control/Artwork/ArtworkDB").write_bytes(b"damaged")
        self.assertEqual(device_covers(device), ({}, {}))

    def test_albums_artists_cover_strip_and_search(self):
        ui = self.ui
        self.assertEqual((ui.browse, self.titles(), ui.counter["text"]), ("all", ["一", "二", "三"], "3 首"))
        self.assertEqual((ui.group_pane.winfo_ismapped(), ui.flow.winfo_ismapped()), (0, 1))
        self.assertEqual((self.flow(), ui.tree.selection()), ((["乙", "甲"], "乙"), ()))
        ui.tree.selection_set("1")
        self.window.update()
        self.assertEqual((ui.album, self.flow()[1], self.titles()), ("甲", "甲", ["一", "二", "三"]))
        ui.flow.step(-1)
        self.window.update()
        self.assertEqual((ui.album, self.flow()[1], ui.tree.selection(), self.titles()), ("乙", "乙", ("2",), ["一", "二", "三"]))
        ui.search.set("一")
        ui.render()
        self.assertEqual((self.flow(), self.titles()), ((["甲"], "甲"), ["一"]))
        ui.search.set("没有")
        ui.render()
        self.window.update()
        self.assertFalse(ui.flow.winfo_ismapped())
        ui.search.set("")
        ui.set_browse("album")
        self.window.update()
        self.assertTrue(ui.group_pane.winfo_ismapped() and ui.flow.winfo_ismapped())
        self.assertEqual((ui.group_keys, ui.album, self.flow()), (["乙", "甲"], "乙", (["乙", "甲"], "乙")))
        self.assertIn("歌手甲 · 2 首", ui.groups.item(ui.groups.get_children()[1], "text"))
        self.assertEqual((self.titles(), ui.counter["text"]), (["三"], "1 / 3 首"))
        self.pick("甲")
        self.assertEqual((self.titles(), self.flow()[1]), (["一", "二"], "甲"))
        ui.flow.step(-1)
        self.window.update()
        self.assertEqual((ui.album, self.titles()), ("乙", ["三"]))
        self.assertEqual(ui.groups.index(ui.groups.selection()[0]), 0)
        ui.flow.step(-1)
        self.assertEqual(ui.album, "乙")
        ui.search.set("二")
        ui.render()
        self.assertEqual((ui.group_keys, ui.album, self.titles()), (["甲"], "甲", ["二"]))
        ui.search.set("没有")
        ui.render()
        self.assertEqual((ui.group_keys, ui.album, self.titles(), ui.flow.items), ([], None, [], []))
        ui.search.set("")
        ui.set_browse("artist")
        self.assertEqual((ui.group_keys, ui.group, self.flow(), self.titles()),
                         ([None, "歌手乙", "歌手甲"], None, (["乙", "甲"], "乙"), ["三"]))
        self.pick("歌手甲")
        self.assertEqual((ui.group, self.flow(), self.titles()), ("歌手甲", (["甲"], "甲"), ["一", "二"]))
        self.pick(None)
        self.assertEqual(self.flow()[0], ["乙", "甲"])
        ui.set_browse("all")
        self.window.update()
        self.assertEqual((ui.group_pane.winfo_ismapped(), ui.flow.winfo_ismapped()), (0, 1))
        self.assertEqual((ui.group, ui.album, self.titles(), ui.counter["text"]), (None, "乙", ["一", "二", "三"], "3 首"))
        ui.set_browse("album")
        ui.view = "queue"
        ui.render()
        self.window.update()
        self.assertFalse(ui.browse_bar.winfo_ismapped() or ui.flow.winfo_ismapped())

    def test_playlist_reordering_waits_for_the_whole_list(self):
        ui = self.ui
        ui.view = ui.keys[-1]
        ui.set_browse("album")
        buttons = {b["text"]: b for b in ui.list_bar.winfo_children()}
        self.pick("甲")
        ui.tree.selection_set("0")
        self.window.update()
        self.assertEqual([str(buttons[name]["state"]) for name in ("上移", "下移", "移出歌单", "改名")],
                         ["disabled", "disabled", "normal", "normal"])
        ui.set_browse("all")
        ui.tree.selection_set("0")
        self.window.update()
        self.assertEqual([str(buttons[name]["state"]) for name in ("上移", "下移")], ["normal", "normal"])
        ui.set_browse("artist")
        self.pick("歌手甲")
        ui.view = "library"
        ui.navigate(None)
        self.assertEqual((ui.group, ui.album), (None, "乙"))

    def test_theme_and_browsing_mode_are_remembered_and_rebuild_the_window(self):
        self.addCleanup(theme.use, "light")
        saved = self.site / "settings/settings.json"
        ui = self.ui
        ui.set_browse("artist")
        ui.set_theme("dark")
        self.assertFalse(saved.exists())
        self.assertEqual(theme.CURRENT, "dark")
        ui.set_theme("light")
        ui.settings = saved
        ui.tree.selection_set("0")
        old_tree, old_flow = ui.tree, ui.flow
        ui.search.set("一")
        ui.set_theme("dark")
        self.window.update()
        self.assertEqual((theme.CURRENT, theme.SURFACE, old_tree.winfo_exists(), old_flow.winfo_exists()),
                         ("dark", theme.PALETTES["dark"]["SURFACE"], 0, 0))
        self.assertEqual((ui.tree["style"], str(ui.flow["bg"]), str(ui.heading["bg"]), self.titles(), self.flow()),
                         ("", theme.FLOW, theme.SURFACE, ["一"], (["甲"], "甲")))
        ui.search.set("")
        ui.render()
        self.assertEqual((ui.album, self.titles()), ("甲", ["一", "二"]))
        ui.busy = True
        ui.set_theme("light")
        ui.busy = False
        self.assertEqual(theme.CURRENT, "dark")
        ui.set_theme("unknown")
        self.assertEqual(theme.CURRENT, "dark")
        window = app.TkinterDnD.Tk()
        self.addCleanup(window.destroy)
        again = app.App(window, browse="all", settings=saved)
        self.assertEqual((theme.CURRENT, again.browse), ("dark", "artist"))
        for timer in window.tk.call("after", "info"):
            window.after_cancel(timer)
        saved.write_text("{broken", encoding="utf8")
        window = app.TkinterDnD.Tk()
        self.addCleanup(window.destroy)
        fresh = app.App(window, browse="album", settings=saved)
        self.assertEqual((theme.CURRENT, fresh.browse), ("light", "album"))
        for timer in window.tk.call("after", "info"):
            window.after_cancel(timer)

    def test_leaning_covers_are_drawn_in_the_background(self):
        ui = self.ui
        ui.set_browse("album")
        self.window.update()
        flow = ui.flow
        self.assertIn(("front", ), {name[1:2] for name in flow.cache})
        stand_in = flow._image(0, "right", 64)
        self.assertIs(stand_in, flow.stand_ins["right", 64])
        deadline = time.monotonic() + 10
        while (flow.pending or not flow.finished.empty() or ui.cover_pending) and time.monotonic() < deadline:
            self.window.update()
            time.sleep(0.01)
        self.assertEqual(flow.pending, set())
        kinds = {name[1] for name in flow.cache}
        self.assertEqual(kinds, {"front", "left", "right"})
        self.assertIsNot(flow._image(0, "right", 64), stand_in)
        ready = dict(flow.cache)
        flow.step(1)
        self.window.update()
        self.assertTrue(all(flow.cache[name] is picture for name, picture in ready.items()))
        flow.forget()
        self.assertEqual((flow.cache, flow.pending), ({}, set()))
        flow._image(0, "left", 64)
        stale = next(iter(flow.pending))
        flow.forget()
        flow.finished.put((stale, ready[next(n for n in ready if n[1] == "left")][1]))
        flow._collect()
        self.assertEqual(flow.cache, {})

    def test_cache_eviction_keeps_active_frame_covers(self):
        flow = self.ui.flow
        flow.redraw()
        active = set(flow._frame_keys)
        self.assertTrue(active)
        for i in range(720):
            flow.cache[("dummy", i, 64)] = (1, f"img-{i}")
        flow._trim_cache()
        self.assertLessEqual(len(flow.cache), 550 + len(active))
        self.assertTrue(active <= set(flow.cache))

    def test_an_album_is_listed_in_track_order_with_numbers(self):
        ui = self.ui
        def numbers():
            return [str(ui.tree.item(i, "values")[0]) for i in ui.tree.get_children()]
        def renumber(places):
            device = Device.open(self.root)
            snapshot = device.library.snapshot
            tracks = tuple(replace(t, track_number=places[t.title][1],
                                   metadata=replace(t.metadata, disc_number=places[t.title][0])) for t in snapshot.tracks)
            transfer(device, [], self.site / "backup", desired=replace(snapshot, tracks=tracks))
            ui.load(self.root)
            self.settle()
        self.assertEqual((ui.tree["columns"], numbers()), (("0", "1", "2", "3", "4", "5"), ["一", "二", "三"]))
        renumber({"一": (1, 2), "二": (1, 1), "三": (0, 0)})
        self.assertEqual(self.titles(), ["一", "二", "三"])
        ui.set_browse("album")
        self.assertEqual((ui.tree.heading("0", "text"), self.titles(), numbers()), ("#", ["三"], [""]))
        self.pick("甲")
        self.assertEqual((self.titles(), numbers()), (["二", "一"], ["1", "2"]))
        ui.tree.selection_set("0")
        self.assertEqual([t.title for t in ui.selected()], ["二"])
        renumber({"一": (1, 2), "二": (2, 1), "三": (0, 0)})
        self.assertEqual((ui.album, self.titles(), numbers()), ("甲", ["一", "二"], ["1-2", "2-1"]))
        ui.set_browse("artist")
        self.pick("歌手甲")
        self.assertEqual((self.titles(), numbers()), (["一", "二"], ["1-2", "2-1"]))
        ui.view = "queue"
        ui.render()
        self.assertEqual(ui.tree.heading("0", "text"), "歌名")
        ui.view = "library"
        ui.set_browse("all")
        self.assertEqual((ui.tree.heading("0", "text"), self.titles()), ("歌名", ["一", "二", "三"]))
