"""The third and fourth generation nano share the classic's library format. No such device was available,
so these tests hold the program to the catalog's description of them, not to real hardware."""

import plistlib
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_classicdrop import FIXTURES, GUID, Device, mock_device
from test_native_covers import covered_m4a
import app
from device_identity import describe, identify
from iPodDB.iTunesDB.writer.signature import verify_hash58
from library import _ipod_usb_instance
from native_covers import ART_DB, CLASSIC, FORMATS, NANO_4, cover_formats, cover_layout, device_covers, load_cover
from theme import player_image
from transfer import transfer

NANO_4_SIZES = {1055: 128, 1068: 128, 1071: 240, 1074: 50, 1078: 80, 1084: 240}
RED, BLUE = (255, 0, 0), (0, 0, 255)


def numbers(layout):
    """The thumbnail numbers of an artwork layout. The nano 3 lists the classic's four in another order."""
    return {f.format_id for f in layout.cover_formats}


def usb(product):
    return f"USB\\VID_05AC&PID_{product}\\000A27001234ABCD"


class NanoIdentityTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.info = self.root / "iPod_Control/Device"
        self.info.mkdir(parents=True)

    def test_model_number_or_serial_names_generation_and_colour(self):
        (self.info / "SysInfo").write_text("ModelNumStr: xB754\n", encoding="utf8")
        nano = identify(self.root)
        self.assertEqual((nano.family, nano.generation, nano.finish, nano.model_number, nano.shape, nano.proven, nano.name),
                         ("iPod Nano", "4th Gen", "Black", "MB754", "nano4", False, "iPod nano"))
        self.assertEqual(describe(nano), "nano 第 4 代 · 黑色")
        self.assertEqual(numbers(nano.capabilities.artwork), set(NANO_4_SIZES))
        (self.info / "SysInfo").unlink()
        (self.info / "SysInfoExtended").write_bytes(plistlib.dumps({"SerialNumber": "AA000000YXX"}))
        nano = identify(self.root)
        self.assertEqual((nano.model_number, nano.shape, describe(nano)), ("MB261", "nano3", "nano 第 3 代 · 黑色"))
        self.assertEqual(numbers(nano.capabilities.artwork), numbers(CLASSIC))
        (self.info / "SysInfoExtended").write_bytes(plistlib.dumps({"ModelNumStr": "MB739"}))
        self.assertEqual(describe(identify(self.root)), "nano 第 4 代 · 紫色")

    def test_usb_product_alone_names_the_generation(self):
        for product, expected, shape, layout in (("1262", "nano 第 3 代", "nano3", CLASSIC), ("1263", "nano 第 4 代", "nano4", NANO_4),
                                                 ("1261", "iPod classic", "classic", CLASSIC)):
            with self.subTest(product=product):
                player = identify(self.root, usb(product))
                self.assertEqual((describe(player), player.shape, player.finish, player.model_number), (expected, shape, "", ""))
                self.assertEqual(numbers(player.capabilities.artwork), numbers(layout))
        self.assertTrue(identify(self.root, usb("1261")).proven)
        (self.info / "SysInfo").write_text("ModelNumStr: xB754\n", encoding="utf8")
        self.assertEqual(describe(identify(self.root, usb("1263"))), "nano 第 4 代 · 黑色")
        # Files copied from another model do not decide how this one is written: the hardware does.
        classic = identify(self.root, usb("1261"))
        self.assertEqual((classic.family, classic.finish, classic.shape), ("iPod Classic", "", "classic"))
        (self.info / "SysInfo").write_text("ModelNumStr: xC297\n", encoding="utf8")
        nano = identify(self.root, usb("1263"))
        self.assertEqual((describe(nano), numbers(nano.capabilities.artwork)), ("nano 第 4 代", set(NANO_4_SIZES)))

    def test_all_non_ios_generations_are_identified(self):
        for model, family, gen in (("xA477", "iPod Nano", "2nd Gen"), ("xC027", "iPod Nano", "5th Gen"),
                                   ("xA444", "iPod", "5.5th Gen"), ("xD475", "iPod Nano", "7th Gen")):
            (self.info / "SysInfo").write_text(f"ModelNumStr: {model}\n", encoding="utf8")
            found = identify(self.root)
            self.assertIsNotNone(found, model)
            self.assertEqual((found.family, found.generation), (family, gen))
        (self.info / "SysInfo").unlink()
        for product, family in (("1260", "iPod Nano"), ("1265", "iPod Nano"), ("1266", "iPod Nano"),
                                ("1267", "iPod Nano"), ("1209", "iPod"), ("1303", "iPod Shuffle")):
            found = identify(self.root, usb(product))
            self.assertIsNotNone(found, product)
            self.assertEqual(found.family, family)

    def test_windows_usb_nodes_of_supported_ipods_can_be_ejected(self):
        for product in ("1262", "1263", "1265", "1266", "1267", "1209", "1303"):
            self.assertEqual(_ipod_usb_instance((usb(product),)), usb(product))
        self.assertIsNone(_ipod_usb_instance((usb("9999"),)))

    def test_each_shape_and_colour_has_its_own_picture(self):
        looks = [("Silver", "nano3"), ("Red", "nano3"), (None, "nano3"), ("Silver", "nano4"), ("Purple", "nano4"),
                 ("Yellow", "nano4"), (None, "nano4"), ("Silver", "classic")]
        pictures = {look: player_image(56, *look) for look in looks}
        self.assertEqual({look[1]: p.size for look, p in pictures.items()}, {"nano3": (42, 56), "nano4": (24, 56), "classic": (34, 56)})
        self.assertEqual(len({p.tobytes() for p in pictures.values()}), len(looks))
        self.assertEqual(player_image(56, "Unheard of", "nano4").tobytes(), pictures[None, "nano4"].tobytes())


class NanoLibraryTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.site = Path(temp.name).resolve()
        self.root = self.site / "nano"
        self.backups = self.site / "backups"

    def send(self, files, **kwargs):
        result = transfer(Device.open(self.root), files, self.backups, **kwargs)
        return result, Device.open(self.root)

    def stored(self, device, track):
        """Sizes and centre colours of every thumbnail kept for one track, by thumbnail number."""
        library = device.library.with_artwork((self.root / ART_DB).read_bytes())
        found = {}
        for fmt in cover_formats(NANO_4) + FORMATS:
            read = library.artwork_read(track.artwork_id, (fmt,), fmt.width)
            if read:
                with (self.root / read.relative_path).open("rb") as stream:
                    stream.seek(read.offset)
                    pixels = read.decode(stream.read(read.length))
                middle = (pixels.height // 2 * pixels.width + pixels.width // 2) * 3
                found[fmt.format_id] = (pixels.width, pixels.height, tuple(pixels.rgb888[middle:middle + 3]))
        return found

    def assert_covers(self, found, sizes, colour):
        self.assertEqual({number: side for number, (side, _, _) in found.items()}, sizes)
        for width, height, rgb in found.values():
            self.assertEqual(width, height)
            self.assertLessEqual(max(abs(a - b) for a, b in zip(rgb, colour)), 8)

    def shards(self):
        return sorted(p.name.split("_")[0] for p in (self.root / ART_DB).parent.glob("*.ithmb"))

    def test_fourth_generation_gets_its_six_thumbnails(self):
        device = mock_device(self.root, "MB754")
        self.assertEqual((device.profile.generation, device.guid, cover_layout(device)), ("4th Gen", GUID, NANO_4))
        (added, skipped, backup), device = self.send([covered_m4a(self.site / "红.m4a"), FIXTURES / "tone.mp3"])
        self.assertEqual((added, skipped, backup.read_bytes() != device.original), (2, 0, True))
        self.assertTrue(verify_hash58(device.original, GUID))
        covered, plain = device.library.snapshot.tracks
        self.assertEqual((covered.title, plain.artwork_id), ("红", 0))
        self.assert_covers(self.stored(device, covered), NANO_4_SIZES, RED)
        self.assertEqual(self.shards(), ["F1055", "F1068", "F1071", "F1074", "F1078", "F1084"])
        # The browser reads what the nano keeps: 128 pixels for lists, and its largest, 240, for the front cover.
        small, large = device_covers(device)
        self.assertEqual(({i.size for i in small.values()}, set(small), set(large)),
                         ({(128, 128)}, {covered.artwork_id}, {covered.artwork_id}))
        self.assertEqual(load_cover(self.root, large[covered.artwork_id]).size, (240, 240))
        # A second batch appends to the same six files and leaves the first cover alone.
        before = self.stored(device, covered)
        (added, _, _), device = self.send([covered_m4a(self.site / "蓝.m4a", "blue")], new_playlist_name="路上")
        self.assertEqual(added, 1)
        tracks = device.library.snapshot.tracks
        self.assertEqual(self.stored(device, tracks[0]), before)
        self.assert_covers(self.stored(device, tracks[2]), NANO_4_SIZES, BLUE)
        self.assertEqual([p.name for p in device.library.snapshot.playlists if p.name == "路上"], ["路上"])
        self.assertTrue(verify_hash58(device.original, GUID))

    def test_third_generation_uses_the_classic_layout(self):
        device = mock_device(self.root, "MA978")
        self.assertEqual((describe(device.profile), numbers(cover_layout(device))), ("nano 第 3 代 · 银色", numbers(CLASSIC)))
        _, device = self.send([covered_m4a(self.site / "红.m4a")])
        self.assert_covers(self.stored(device, device.library.snapshot.tracks[0]), {1055: 128, 1060: 320, 1061: 55, 1068: 128}, RED)
        self.assertEqual(self.shards(), ["F1055", "F1060", "F1061", "F1068"])

    def test_unnamed_device_follows_the_thumbnails_it_already_keeps(self):
        mock_device(self.root, "MB754")
        self.send([covered_m4a(self.site / "红.m4a")])
        # The same device, now reporting nothing about its model.
        (self.root / "iPod_Control/Device/SysInfoExtended").write_bytes(plistlib.dumps({"FireWireGUID": int.from_bytes(GUID, "big")}))
        device = Device.open(self.root)
        self.assertEqual((device.profile, device.guid), (None, GUID))
        self.assertEqual(cover_layout(device, set(NANO_4_SIZES)), NANO_4)
        self.assertEqual(cover_layout(device, {1055, 1068}), CLASSIC)
        self.assertEqual(cover_layout(device), CLASSIC)
        _, device = self.send([covered_m4a(self.site / "蓝.m4a", "blue")])
        self.assert_covers(self.stored(device, device.library.snapshot.tracks[1]), NANO_4_SIZES, BLUE)
        self.assertEqual(self.shards(), ["F1055", "F1068", "F1071", "F1074", "F1078", "F1084"])

    def test_window_names_the_nano_and_says_it_is_unverified(self):
        mock_device(self.root, "MB739")
        self.send([covered_m4a(self.site / "红.m4a")])
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
        with patch.object(app, "BACKUPS", self.backups):
            ui.load(self.root)
            settle()
            self.assertEqual((ui.device_model["text"], ui.device_badge["text"]), ("nano 第 4 代 · 紫色\n尚未真机验证", "可写入"))
            self.assertIn(("Purple", "nano4"), ui.players)
            self.assertIn("iPod nano", ui.status.get())
            self.assertIn("尚未经真机验证", ui.status.get())
            self.assertEqual({i.size for i in ui.covers.values()}, {(128, 128)})
            ui.load(self.root, status="已保存。")
            settle()
            self.assertEqual(ui.status.get(), "已保存。")
