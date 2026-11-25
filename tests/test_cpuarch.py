"""cpuarch: the x64 process must load the x86_64 wasmtime even when the host is ARM64."""
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]

import encoders
import updates
import cpuarch

SCRIPT_ENV = {**os.environ, "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "vendor"))),
              "PYTHONUTF8": "1"}


def run_python(code):
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=SCRIPT_ENV, timeout=120)


@unittest.skipUnless(sys.platform == "win32", "wasmtime emulation patch is Windows only")
class WasmtimeImportTests(unittest.TestCase):
    PATCH = "import platform; platform.machine = lambda: 'ARM64'; "

    def test_library_import_loads_the_x64_wasmtime_under_an_arm64_machine_report(self):
        out = run_python(
            self.PATCH +
            "import cpuarch; cpuarch.import_wasmtime();"
            "import library;"
            "from wasmtime import _ffi; print(_ffi.filename)")
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertIn("win32-x86_64", out.stdout, out.stdout)

    def test_a_hashab_roundtrip_works_after_the_patch(self):
        out = run_python(
            self.PATCH +
            "import cpuarch; cpuarch.import_wasmtime();"
            "from iPodDB.library import WriteChecksum, WriteTarget;"
            "from iPodDB.library.creation import create_empty_library;"
            "from iPodDB.iTunesDB.writer.signature import sign_hashab, verify_hashab;"
            "guid = bytes.fromhex('0011223344556677');"
            "data = create_empty_library('t', WriteTarget(checksum=WriteChecksum.HASH58,"
            " firewire_guid=guid, database_version=0x30), supports_podcasts=True).prepared.itunes;"
            "print(verify_hashab(sign_hashab(data, guid), guid))")
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertIn("True", out.stdout)

    def test_without_the_patch_wasmtime_targets_the_arm64_dll_and_fails(self):
        out = run_python(
            self.PATCH + "import wasmtime; from wasmtime import _ffi; print(_ffi.filename)")
        # The unpatched import either raises (DLL missing) or resolves an aarch64 path.
        self.assertTrue(out.returncode or "win32-aarch64" in out.stdout.replace("/", "\\"),
                        out.stdout + out.stderr[-500:])


class HostArchTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "IsWow64Process2 is a Windows API")
    def test_native_machine_reported_by_iswow64process2_wins(self):
        def fake_iswow64process2(_process, process_machine, native_machine):
            process_machine._obj.value = 0xAA64  # IMAGE_FILE_MACHINE_UNKNOWN is used for native ARM64 x64 apps
            native_machine._obj.value = 0xAA64
            return 1
        kernel = cpuarch.ctypes.windll.kernel32
        with patch.object(kernel, "IsWow64Process2", fake_iswow64process2):
            self.assertEqual(cpuarch.host_arch(), "ARM64")

    def test_describe_names_the_emulated_process(self):
        with patch.object(cpuarch, "host_arch", return_value="ARM64"), \
             patch.object(cpuarch, "process_arch", return_value="x64"):
            expected = "x64（电脑为 Apple 芯片，Rosetta 转译）" if sys.platform == "darwin" else "x64（电脑为 ARM64，模拟运行）"
            self.assertEqual(cpuarch.describe(), expected)
        with patch.object(cpuarch, "host_arch", return_value="x64"), \
             patch.object(cpuarch, "process_arch", return_value="x64"):
            self.assertEqual(cpuarch.describe(), "x64")

    @unittest.skipUnless(sys.platform == "win32", "IsWow64Process2 is a Windows API")
    def test_host_arch_falls_back_to_platform_machine(self):
        kernel = cpuarch.ctypes.windll.kernel32
        with patch.object(kernel, "IsWow64Process2", side_effect=OSError("missing")), \
             patch.object(cpuarch.platform, "machine", return_value="AMD64"):
            self.assertEqual(cpuarch.host_arch(), "x64")

    def test_machine_is_restored_after_import_wasmtime(self):
        before = cpuarch.platform.machine
        cpuarch.import_wasmtime()
        self.assertIs(cpuarch.platform.machine, before)


class EnvironmentLineTests(unittest.TestCase):
    def test_environment_lines_report_the_architecture(self):
        lines = updates.environment_lines()
        arch = next(line for line in lines if line.startswith("- 架构："))
        self.assertIn(cpuarch.describe(), arch)
        self.assertEqual(len(lines), 4)


class SelfTestTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin" or encoders.REFALAC.is_file(), "vendor/qaac/refalac64.exe not fetched")
    def test_self_test_writes_a_passing_report(self):
        from selftest import self_test
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "self-test.json"
            self.assertEqual(self_test(out), 0, out.read_text(encoding="utf-8"))
            report = json.loads(out.read_text(encoding="utf-8"))
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["version"], updates.__version__)
        if sys.platform == "win32":
            self.assertIn("win32-x86_64", report["wasmtime_dll"])
        encoder = "afconvert" if sys.platform == "darwin" else "refalac"
        for name in ("wasmtime", "ffmpeg", encoder, "tkdnd"):
            self.assertTrue(report["checks"][name]["ok"], report["checks"][name])


class VersionOrderTests(unittest.TestCase):
    def test_1_5_10_sorts_above_1_5_9(self):
        self.assertGreater(updates.version_key("1.5.10"), updates.version_key("1.5.9"))


if __name__ == "__main__":
    unittest.main()
