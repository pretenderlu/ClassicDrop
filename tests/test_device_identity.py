import plistlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_classicdrop import mock_device
import system
from device_identity import describe, identify
from theme import player_image


class DeviceIdentityTests(unittest.TestCase):
    def test_model_and_finish_from_device_files(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            info = root / "iPod_Control/Device"
            info.mkdir(parents=True)
            self.assertIsNone(identify(root, False))
            self.assertEqual(describe(None), "")
            (info / "SysInfo").write_text("ModelNumStr: xC297\n", encoding="utf8")
            black = identify(root, False)
            self.assertEqual((black.generation, black.finish, describe(black)), ("7th Gen", "Black", "第 7 代 · 黑色"))
            (info / "SysInfo").unlink()
            (info / "SysInfoExtended").write_bytes(plistlib.dumps({"SerialNumber": "AA0000009ZS"}))
            silver = identify(root, False)
            self.assertEqual((silver.model_number, describe(silver)), ("MC293", "第 7 代 · 银色"))

    @unittest.skipUnless(system.WINDOWS, "SCSI sysinfo is read on Windows only")
    def test_unreadable_hardware_never_blocks_opening(self):
        with TemporaryDirectory() as tmp:
            device = mock_device(Path(tmp) / "iPod")
            self.assertIsNone(device.profile)
            drive = Path(Path(tmp).anchor)
            with patch.object(system, "scsi_sysinfo", side_effect=OSError("denied")) as scsi:
                self.assertIsNone(identify(drive, True))
                scsi.assert_called_once_with(drive.drive)
                self.assertIsNone(identify(drive, False))
                scsi.assert_called_once()
            reply = plistlib.dumps({"SerialNumber": "AA0000009ZS"}, fmt=plistlib.FMT_XML)
            with patch.object(system, "scsi_sysinfo", return_value=reply):
                self.assertEqual(identify(drive, True).finish, "Silver")

    def test_player_pictures_keep_the_requested_height(self):
        pictures = {finish: player_image(56, finish) for finish in ("Silver", "Black", None)}
        self.assertEqual({p.size for p in pictures.values()}, {(34, 56)})
        self.assertEqual(len({p.tobytes() for p in pictures.values()}), 3)
