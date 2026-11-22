from pathlib import Path
from tempfile import TemporaryDirectory
import time
import tkinter as tk
import unittest
from unittest.mock import patch

import app
from updates import UpdateError, UpdateInfo
from version import __version__

NEWER = UpdateInfo("v9.9.9", "https://github.com/pretenderlu/ClassicDrop/releases/v9.9.9",
                   "https://example.invalid/setup.exe", "ClassicDropSetup-v9.9.9.exe", 100, "a" * 64, "", "- 新增 A\n- 修复 B")


class UpdateInterfaceTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.site = Path(temp.name)
        window = self.window = app.TkinterDnD.Tk()
        def cleanup():
            for timer in window.tk.call("after", "info"):
                window.after_cancel(timer)
            if window.winfo_exists():
                window.destroy()
        self.addCleanup(cleanup)
        window.geometry("1100x700+28000+28000")
        self.ui = app.App(window)
        for target, value in ((app.tempfile, "gettempdir"),):
            patcher = patch.object(target, value, return_value=str(self.site))
            patcher.start()
            self.addCleanup(patcher.stop)
        window.update()

    def settle(self, until=lambda: True):
        end = time.monotonic() + 10
        while time.monotonic() < end:
            self.window.update()
            if not self.ui.busy and until():
                self.window.update()
                return
            time.sleep(0.01)
        self.fail("GUI operation timed out")

    def check(self, result, asked=False):
        with patch.object(app, "check_for_updates", side_effect=result if isinstance(result, Exception) else None,
                          return_value=result) as checked:
            before = checked.call_count
            self.ui.check_updates(asked)
            self.settle(lambda: checked.call_count > before and self.ui.events.empty())
            self.window.update()
        checked.assert_called_with(__version__)

    def dialog(self):
        return next((w for w in self.window.winfo_children() if isinstance(w, tk.Toplevel)), None)

    def test_start_does_not_ask_the_network_unless_told_to(self):
        with patch.object(app, "check_for_updates") as checked:
            self.settle()
            pending = self.window.tk.call("after", "info")
        checked.assert_not_called()
        self.assertFalse(self.ui.update_button.winfo_ismapped())
        self.assertLessEqual(len(pending), 3)

    def test_quiet_check_shows_a_button_and_a_manual_check_reports(self):
        ui = self.ui
        self.check(OSError("offline"))
        self.assertNotIn("检查更新", ui.status.get())
        self.check(OSError("offline"), asked=True)
        self.assertIn("检查更新失败：offline", ui.status.get())
        self.check(None, asked=True)
        self.assertEqual(ui.status.get(), f"当前已是最新版本 {__version__}。")
        self.assertFalse(ui.update_button.winfo_ismapped())
        self.check(NEWER)
        self.assertEqual((ui.update_button.winfo_ismapped(), ui.update_button["text"], self.dialog()),
                         (1, "新版本 v9.9.9", None))
        self.assertIn("发现新版本 v9.9.9", ui.status.get())
        ui.set_theme("dark")
        self.addCleanup(app.theme.use, "light")
        self.window.update()
        self.assertEqual((ui.update_button.winfo_ismapped(), ui.update_button["text"]), (1, "新版本 v9.9.9"))

    def test_dialog_shows_notes_and_installs_after_a_verified_download(self):
        ui = self.ui
        self.check(NEWER, asked=True)
        dialog = self.dialog()
        self.assertEqual(dialog.title(), "自动更新")
        notes = next(w for box in dialog.winfo_children()[0].winfo_children() for w in box.winfo_children()
                     if isinstance(w, tk.Text))
        self.assertEqual(notes.get("1.0", "end").strip(), "- 新增 A\n- 修复 B")
        ui.open_update()
        self.assertEqual(len([w for w in self.window.winfo_children() if isinstance(w, tk.Toplevel)]), 1)
        buttons = {b["text"]: b for b in dialog.winfo_children()[0].winfo_children()[-1].winfo_children()}
        self.assertEqual(set(buttons), {"否", "是"})
        buttons["否"].invoke()
        self.assertIsNone(self.dialog())
        ui.open_update()
        yes = next(b for b in self.dialog().winfo_children()[0].winfo_children()[-1].winfo_children() if b["text"] == "是")
        reports = []
        def download(info, destination, progress):
            destination.parent.mkdir(parents=True, exist_ok=True)
            for done in (10, 55, 55, 100):
                progress(done, 100)
                reports.append(ui.busy)
            destination.write_bytes(b"setup")
            return destination
        with patch.object(app, "download_update", side_effect=download) as fetched, \
                patch.object(app.subprocess, "Popen") as started, patch.object(self.window, "destroy") as closed:
            yes.invoke()
            self.settle(lambda: closed.called)
        target = self.site / "ClassicDrop" / "updates" / "ClassicDropSetup-v9.9.9.exe"
        self.assertEqual(fetched.call_args.args[:2], (NEWER, target))
        self.assertEqual((reports, target.read_bytes()), ([True] * 4, b"setup"))
        started.assert_called_once_with([str(target), "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"], cwd=target.parent)
        closed.assert_called_once_with()

    def test_update_waits_for_work_and_queued_songs_and_failures_offer_a_retry(self):
        ui = self.ui
        self.check(NEWER)
        with patch.object(app.messagebox, "showwarning") as warned:
            ui.pending = {Path("a.mp3"): None}
            ui.open_update()
            self.assertIn("待传列表里还有 1 首", warned.call_args.args[1])
            ui.pending = {}
            ui.busy = True
            ui.download_update()
            ui.busy = False
            self.assertEqual((warned.call_count, self.dialog()), (2, None))
        answers = iter((True, False, None))
        with patch.object(app, "download_update", side_effect=UpdateError("更新包校验失败，已阻止安装。")) as fetched, \
                patch.object(app.messagebox, "askyesnocancel", side_effect=lambda *a, **k: next(answers)) as asked, \
                patch.object(app.webbrowser, "open") as opened, patch.object(app.subprocess, "Popen") as started:
            ui.download_update()
            self.settle(lambda: asked.call_count == 2)
            self.assertEqual(fetched.call_count, 2)
            opened.assert_called_once_with(NEWER.url, new=2)
            self.assertIn("校验失败", asked.call_args.args[1])
            self.assertIn("自动更新失败", ui.status.get())
            ui.download_update()
            self.settle(lambda: asked.call_count == 3)
            opened.assert_called_once()
            started.assert_not_called()
        with patch.object(app, "download_update", return_value=self.site / "missing.exe"), \
                patch.object(app.subprocess, "Popen", side_effect=OSError("cannot start")), \
                patch.object(app.messagebox, "askyesnocancel", return_value=None) as asked:
            ui.download_update()
            self.settle(lambda: asked.called)
            self.assertIn("cannot start", asked.call_args.args[1])
        self.assertTrue(self.window.winfo_exists())

    def test_release_without_an_installer_opens_its_page(self):
        self.check(UpdateInfo("v9.9.9", NEWER.url, notes=""), asked=True)
        buttons = {b["text"]: b for b in self.dialog().winfo_children()[0].winfo_children()[-1].winfo_children()}
        self.assertEqual(set(buttons), {"关闭", "打开发布页"})
        with patch.object(app.webbrowser, "open") as opened, patch.object(app, "download_update") as fetched:
            buttons["打开发布页"].invoke()
        opened.assert_called_once_with(NEWER.url, new=2)
        fetched.assert_not_called()
        self.assertIsNone(self.dialog())
