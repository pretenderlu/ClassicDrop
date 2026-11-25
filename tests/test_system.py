"""system.py: platform-neutral wrappers, the macOS disk-id synthesis and macOS junk filtering."""
import os
from pathlib import Path
import plistlib
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import system
from test_classicdrop import GUID, mock_device
from library import Device, _device_guids, _ipod_usb_instance

SERIAL = GUID.hex()
IOREG = [{
    "idVendor": 1452, "idProduct": 0x1261, "USB Serial Number": SERIAL,
    "IORegistryEntryChildren": [{"BSD Name": "disk4"}, {"BSD Name": "disk4s1"}],
}]


def completed(stdout=b"", returncode=0):
    return subprocess.CompletedProcess((), returncode, stdout=stdout, stderr=b"")


class JunkTests(unittest.TestCase):
    def test_mac_metadata_names_are_junk(self):
        for name in ("._song.mp3", ".DS_Store", "._.DS_Store"):
            self.assertTrue(system.junk_file(name), name)
        for name in ("song.mp3", "cover.jpg", ".hidden-but-real.mp3"):
            self.assertFalse(system.junk_file(name), name)

    def test_folder_scan_skips_appledouble_files_and_metadata_dirs(self):
        from music_import import scan_inputs
        fixture = Path(__file__).parent / "fixtures" / "tone.mp3"
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            import shutil
            shutil.copy(fixture, folder / "real.mp3")
            shutil.copy(fixture, folder / "._real.mp3")
            (folder / ".DS_Store").write_bytes(b"junk")
            for name in (".Spotlight-V100", ".fseventsd", ".Trashes", ".TemporaryItems"):
                (folder / name).mkdir()
                shutil.copy(fixture, folder / name / "ghost.mp3")
            songs, errors = scan_inputs([folder], {}, lambda _: None, lambda: False)
        self.assertEqual([song.path.name for song in songs], ["real.mp3"])
        self.assertEqual(errors, [])


class MacDiskIdTests(unittest.TestCase):
    """diskutil/ioreg output is synthesised into the id shape the Windows parsers already consume."""

    def ids(self, ioreg=IOREG, whole="disk4"):
        def run(command, **kwargs):
            if command[0] == "diskutil":
                return completed(plistlib.dumps({"ParentWholeDisk": whole}))
            return completed(plistlib.dumps(ioreg))
        with patch.object(system.subprocess, "run", side_effect=run):
            return system._mac_disk_ids(Path("/Volumes/IPOD"))

    def test_whole_disk_and_usb_instance_are_derived(self):
        ids = self.ids()
        expected = f"USB\\VID_05AC&PID_1261\\{SERIAL}"
        self.assertIn("DISK:disk4", ids)
        self.assertIn(expected, ids)
        self.assertEqual(_ipod_usb_instance(ids), expected)
        with TemporaryDirectory() as tmp:
            root = mock_device(Path(tmp) / "iPod").root
            self.assertIn(GUID, set(_device_guids(root, ids)))

    def test_ioreg_without_the_disk_yields_only_the_whole_disk_id(self):
        ids = self.ids(ioreg=[{"idVendor": 1452, "idProduct": 0x1261,
                               "USB Serial Number": SERIAL, "BSD Name": "disk9"}])
        self.assertEqual(ids, ("DISK:disk4",))
        self.assertIsNone(_ipod_usb_instance(ids))

    def test_failures_return_empty(self):
        def run(command, **kwargs):
            raise subprocess.TimeoutExpired(command[0], 12)
        with patch.object(system.subprocess, "run", side_effect=run):
            self.assertEqual(system._mac_disk_ids(Path("/Volumes/IPOD")), ())
        with patch.object(system.subprocess, "run", return_value=completed(b"not a plist")):
            self.assertEqual(system._mac_disk_ids(Path("/Volumes/IPOD")), ())


@unittest.skipIf(system.WINDOWS, "macOS volume enumeration")
class MacVolumeTests(unittest.TestCase):
    def test_eject_uses_diskutil_on_the_whole_disk(self):
        instance = f"USB\\VID_05AC&PID_1261\\{SERIAL}"
        calls = []
        with patch.object(system.subprocess, "run",
                          side_effect=lambda c, **k: calls.append(c) or completed()) as run:
            system.eject_device(instance, (f"DISK:disk4", instance))
        self.assertEqual(calls, [["diskutil", "eject", "disk4"]])

    def test_eject_failure_raises_the_chinese_error(self):
        with patch.object(system.subprocess, "run", return_value=completed(b"busy", 1)):
            with self.assertRaisesRegex(OSError, "推出"):
                system.eject_device("USB\\x", ("DISK:disk4",))

    def test_flock_conflict_reports_the_lock_error(self):
        import fcntl
        with TemporaryDirectory() as tmp:
            lock = Path(tmp) / "iTunesLock"
            db = Path(tmp) / "iTunesDB"
            db.write_bytes(b"db")
            handle = os.open(lock, os.O_RDWR | os.O_CREAT)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX)
                with self.assertRaisesRegex(OSError, "无法锁定 iPod"):
                    with system.lock_file(lock, db):
                        pass
            finally:
                os.close(handle)

    def test_is_readonly_follows_the_mode(self):
        import stat
        with TemporaryDirectory() as tmp:
            file = Path(tmp) / "f"
            file.write_bytes(b"x")
            self.assertFalse(system.is_readonly(file.stat()))
            file.chmod(file.stat().st_mode & ~stat.S_IWUSR)
            self.assertTrue(system.is_readonly(file.stat()))


class MacIdentifyTests(unittest.TestCase):
    """A classic over USB with empty SysInfo resolves to a writable classic player on any platform."""

    def test_pid_1261_without_sysinfo_identifies_the_classic(self):
        from device_identity import identify
        with TemporaryDirectory() as tmp:
            device = mock_device(Path(tmp) / "iPod")
            (device.root / "iPod_Control/Device/SysInfoExtended").write_bytes(b"")
            instance = f"USB\\VID_05AC&PID_1261\\{SERIAL}"
            player = identify(device.root, instance)
        self.assertIsNotNone(player)
        self.assertEqual(player.family, "iPod Classic")


if __name__ == "__main__":
    unittest.main()
