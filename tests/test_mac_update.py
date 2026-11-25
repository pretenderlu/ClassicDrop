import os
import plistlib
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import mac_update
import system


def fake_bundle(base, version="9.9.9", exe=b"#!/bin/sh\nexit 0\n"):
    app = Path(base) / "ClassicDrop.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    binary = macos / "ClassicDrop"
    binary.write_bytes(exe)
    binary.chmod(0o755)
    with open(app / "Contents" / "Info.plist", "wb") as stream:
        plistlib.dump({"CFBundleShortVersionString": version, "CFBundleIdentifier": "test",
                       "CFBundleExecutable": "ClassicDrop"}, stream)
    return app


class BundleTests(unittest.TestCase):
    def test_running_bundle_finds_the_app_and_rejects_other_paths(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp).resolve()
            bundle = fake_bundle(tmp)
            exe = bundle / "Contents" / "MacOS" / "ClassicDrop"
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(exe)):
                self.assertEqual(mac_update.running_bundle(), bundle)
            for exe in (bundle / "Contents" / "MacOS", tmp / "run", tmp / "x.app" / "ClassicDrop"):
                with patch.object(sys, "executable", str(exe)):
                    self.assertIsNone(mac_update.running_bundle())
            with patch.object(sys, "frozen", False, create=True):
                self.assertIsNone(mac_update.running_bundle())

    def test_cannot_replace_covers_translocation_unwritable_and_plain(self):
        with TemporaryDirectory() as tmp:
            bundle = fake_bundle(tmp)
            self.assertIsNone(mac_update.cannot_replace(bundle))
            ghost = Path("/private/var/folders/x/AppTranslocation/AAAA/d/ClassicDrop.app")
            self.assertIn("隔离", mac_update.cannot_replace(ghost))
            with patch("os.access", return_value=False):
                self.assertIn("不可写", mac_update.cannot_replace(bundle))
            self.assertIsNotNone(mac_update.cannot_replace(None))

    def test_bundle_version_reads_the_plist(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(mac_update.bundle_version(fake_bundle(tmp, "1.2.3")), "1.2.3")

    def test_launch_swap_writes_the_script_and_detaches(self):
        with TemporaryDirectory() as tmp, patch.object(mac_update.subprocess, "Popen") as popen:
            bundle, staged = fake_bundle(tmp), fake_bundle(Path(tmp) / "new")
            mac_update.launch_swap(bundle, staged, Path(tmp) / "update.log", pid=4321)
        args = popen.call_args.args[0]
        self.assertEqual(args[0], "/bin/sh")
        self.assertEqual(args[2:], ["4321", str(bundle), str(staged), str(Path(tmp) / "update.log")])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        script = Path(args[1]).read_text(encoding="utf8")
        self.assertIn("com.apple.quarantine", script)


@unittest.skipUnless(sys.platform == "darwin", "需要 macOS 的 hdiutil/codesign/ditto")
class MacInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.site = Path(self.tmp.name).resolve()

    def make_dmg(self, version="9.9.9"):
        staging = self.site / "dmg-src"
        staging.mkdir()
        bundle = fake_bundle(staging, version)
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(bundle)], check=True,
                       capture_output=True)
        dmg = self.site / "update.dmg"
        subprocess.run(["hdiutil", "create", "-volname", "ClassicDrop", "-srcfolder", str(staging),
                        "-format", "UDZO", "-ov", str(dmg)], check=True, capture_output=True)
        return dmg, bundle

    def test_stage_update_mounts_copies_and_verifies(self):
        dmg, _ = self.make_dmg("9.9.9")
        target = fake_bundle(self.site / "target", "1.0.0")
        staged = mac_update.stage_update(dmg, target, "9.9.9")
        self.assertEqual(staged, target.parent / ".ClassicDrop-update.app")
        self.assertEqual(mac_update.bundle_version(staged), "9.9.9")
        self.assertFalse(list(self.site.glob("mnt*")))

    def test_stage_update_rejects_a_version_mismatch(self):
        dmg, _ = self.make_dmg("9.9.9")
        target = fake_bundle(self.site / "target", "1.0.0")
        with self.assertRaisesRegex(ValueError, "版本不匹配"):
            mac_update.stage_update(dmg, target, "8.8.8")
        self.assertFalse((target.parent / ".ClassicDrop-update.app").exists())

    def run_swap(self, target, staged, log, tries="600"):
        script = self.site / "swap.sh"
        script.write_text(mac_update.SWAP_SCRIPT, encoding="utf8")
        # The pid must be reaped: a zombie child still answers kill -0.
        sleeper = subprocess.Popen(["sleep", "0.1"])
        sleeper.wait()
        return subprocess.run(["/bin/sh", str(script), str(sleeper.pid), str(target), str(staged), str(log),
                               tries], capture_output=True, timeout=60)

    def test_swap_replaces_and_relaunches(self):
        target = fake_bundle(self.site / "installed", "1.0.0")
        staged = fake_bundle(self.site / "new", "9.9.9")
        moved = target.parent / ".staged.app"
        shutil.move(str(staged), moved)
        # The script's final `open` just reports a launch failure into the log for a non-Mach-O binary.
        self.run_swap(target, moved, self.site / "swap.log")
        self.assertEqual(mac_update.bundle_version(target), "9.9.9")
        self.assertFalse(moved.exists())
        self.assertFalse(list(target.parent.glob("*.old.*")))
        self.assertTrue((target / "Contents" / "MacOS" / "ClassicDrop").exists())

    def test_swap_rolls_back_when_the_staged_bundle_is_missing(self):
        target = fake_bundle(self.site / "installed", "1.0.0")
        ghost = self.site / "ghost.app"
        self.run_swap(target, ghost, self.site / "swap.log")
        self.assertEqual(mac_update.bundle_version(target), "1.0.0")
        self.assertIn("swap failed", (self.site / "swap.log").read_text())

    def test_swap_aborts_while_the_app_is_still_running(self):
        target = fake_bundle(self.site / "installed", "1.0.0")
        staged = fake_bundle(self.site / "new", "9.9.9")
        keeper = subprocess.Popen(["sleep", "30"])
        self.addCleanup(keeper.kill)
        script = self.site / "swap.sh"
        script.write_text(mac_update.SWAP_SCRIPT, encoding="utf8")
        result = subprocess.run(["/bin/sh", str(script), str(keeper.pid), str(target), str(staged),
                                 str(self.site / "swap.log"), "30"], capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(mac_update.bundle_version(target), "1.0.0")
        self.assertFalse(staged.exists())
        self.assertIn("still running", (self.site / "swap.log").read_text())


if __name__ == "__main__":
    unittest.main()
