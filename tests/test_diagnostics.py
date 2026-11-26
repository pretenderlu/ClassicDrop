import logging
import system
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import diagnostics
from version import __version__


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.site = Path(temp.name).resolve()
        diagnostics.reset()
        self.addCleanup(diagnostics.reset)
        # test_classicdrop silences INFO for the whole suite; these tests need the file handler to see it.
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.WARNING)

    def file(self):
        return self.site / "logs" / "classicdrop.log"

    def test_setup_writes_a_rotating_log_once(self):
        diagnostics.setup(self.site / "logs")
        diagnostics.setup(self.site / "logs")
        handlers = [h for h in diagnostics.log.handlers if isinstance(h, RotatingFileHandler)]
        ipod_handlers = [h for h in logging.getLogger("iPodDB").handlers if isinstance(h, RotatingFileHandler)]
        self.assertEqual(len(handlers), 1)
        self.assertEqual(ipod_handlers, handlers)
        self.assertIs(ipod_handlers[0], handlers[0])
        self.assertEqual((handlers[0].maxBytes, handlers[0].backupCount), (1_000_000, 2))
        self.assertEqual(diagnostics.log.level, logging.INFO)
        self.assertEqual(logging.getLogger("iPodDB").level, logging.WARNING)
        diagnostics.log.info("第一行")
        diagnostics.log.info("第二行")
        text = self.file().read_text(encoding="utf8")
        self.assertIn("INFO ClassicDrop: 第一行", text)
        self.assertIn("INFO ClassicDrop: 第二行", text)

    def test_home_and_secrets_are_scrubbed_in_messages_and_tracebacks(self):
        home = Path.home()
        variants = {str(home), str(home).replace("\\", "/"), str(home).lower(), str(home).upper()}
        diagnostics.hide("SECRET-SERIAL", "0011223344556677")
        self.assertNotIn(str(home), diagnostics.scrub(str(home)))
        for variant in variants:
            self.assertEqual(diagnostics.scrub(variant), "~")
        self.assertEqual(diagnostics.scrub("id 0011223344556677"), "id [已隐藏]")
        self.assertEqual(diagnostics.scrub("id " + "0011223344556677".upper()), "id [已隐藏]")
        self.assertEqual(diagnostics.scrub("sn SECRET-SERIAL"), "sn [已隐藏]")
        diagnostics.setup(self.site / "logs")
        try:
            raise ValueError(f"在 {home} 坏了 SECRET-SERIAL")
        except ValueError as exc:
            diagnostics.remember("操作失败", exc)
        text = self.file().read_text(encoding="utf8")
        self.assertIn("操作失败", text)
        self.assertEqual(text.count("Traceback"), 1)
        self.assertIn("[已隐藏]", text)
        for secret in ("SECRET-SERIAL", "0011223344556677", *variants - {str(home)}):
            self.assertNotIn(secret, text)
        self.assertNotIn(str(home), text)

    def test_report_has_environment_last_error_and_log_tail_without_secrets(self):
        diagnostics.setup(self.site / "logs")
        diagnostics.hide("SECRET-SERIAL")
        for n in range(70):
            diagnostics.log.info("行 %d SECRET-SERIAL", n)
        try:
            raise RuntimeError("SECRET-SERIAL 报错")
        except RuntimeError as exc:
            diagnostics.remember("出错了", exc)
        text = diagnostics.report("iPod classic（第 7 代）")
        self.assertIn("ClassicDrop：v" + __version__, text)
        self.assertIn("macOS" if system.MAC else "Windows", text)
        self.assertIn("iPod classic（第 7 代）", text)
        self.assertIn("出错了", text)
        self.assertIn("RuntimeError", text)
        self.assertIn("行 69", text)
        self.assertNotIn("行 9 ", text)
        self.assertNotIn("SECRET-SERIAL", text)
        self.assertNotIn(str(Path.home()), text)

    def test_report_works_without_a_log_or_error(self):
        text = diagnostics.report()
        self.assertIn("ClassicDrop：v" + __version__, text)
        self.assertIn("未连接", text)
        self.assertIn("本次运行没有报错", text)
        self.assertIn("最近的日志", text)


    def test_a_loaded_device_hides_the_disk_serial_guid_and_its_name(self):
        from types import SimpleNamespace
        import app
        device = SimpleNamespace(root=self.site, guid=bytes.fromhex("0011223344556677"), profile=None,
                                 usb_instance=r"USB\VID_05AC&PID_1261\0011223344556677", disk_ids=("ZZ1234567AB",),
                                 library=SimpleNamespace(snapshot=SimpleNamespace(device_name="小明的 iPod")))
        app.App._hide_device(None, device)
        self.assertEqual(diagnostics.scrub("ZZ1234567AB 0011223344556677 小明的 iPod"), "[已隐藏] [已隐藏] [已隐藏]")
        device.library.snapshot.device_name = "iPod"
        diagnostics.reset()
        app.App._hide_device(None, device)
        self.assertEqual(diagnostics.scrub("iPod"), "iPod")

    def test_readonly_device_logs_reason_without_leaking_secrets(self):
        from types import SimpleNamespace
        import app
        diagnostics.setup(self.site / "logs")
        device = SimpleNamespace(root=self.site, guid=None, profile=None,
                                 usb_instance=r"USB\VID_05AC&PID_1261\0011223344556677",
                                 disk_ids=("ZZ1234567AB",), secrets=("SN987654321",),
                                 readonly_reason="guid_mismatch", signature_scheme=1,
                                 guid_candidates=1, sysinfo_files=1,
                                 library=SimpleNamespace(snapshot=SimpleNamespace(device_name="小明的 iPod")))
        app.App._hide_device(None, device)
        text = self.file().read_text(encoding="utf8")
        self.assertIn("只读", text)
        self.assertIn("原因=guid_mismatch", text)
        self.assertIn("candidates=1", text)
        for secret in ("0011223344556677", "ZZ1234567AB", "SN987654321", "小明的 iPod"):
            self.assertNotIn(secret, text)

if __name__ == "__main__":
    unittest.main()
