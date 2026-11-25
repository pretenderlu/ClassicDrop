from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit
import time
import tkinter as tk
import unittest
from unittest.mock import patch

import app
from updates import PROJECT_URL, Release, UpdateError, UpdateInfo
from version import __version__

PAGE = "https://github.com/pretenderlu/ClassicDrop/releases/"
NEWER = UpdateInfo("v9.9.9", PAGE + "v9.9.9", "https://example.invalid/setup.exe", "ClassicDropSetup-v9.9.9.exe", 100,
                   "a" * 64, "", "- 新增 **A**\n- 修复 B")
HISTORY = (Release("v9.9.9", PAGE + "v9.9.9", "2026-12-01", "· 新增 A\n· 修复 B"),
           Release("v" + __version__, PAGE + "current", "2026-11-25", "· 这一版的内容"),
           Release("v1.0.0", PAGE + "v1.0.0", "", ""))


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
        # Nothing in these tests may reach GitHub: both questions the window can ask are answered here.
        for target, name, value in ((app.tempfile, "gettempdir", str(self.site)), (app, "release_history", ((), None)),
                                    (app, "check_for_updates", None)):
            patcher = patch.object(target, name, return_value=value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        # Answers come from a background thread. Waiting for the window to have handled one is the only
        # reliable sign that it is done: the question being asked, or an empty queue, can both be seen too early.
        self.handled = []
        for name in ("_update_checked", "_history_arrived"):
            def record(*args, inner=getattr(self.ui, name), name=name):
                inner(*args)
                self.handled.append(name)
            setattr(self.ui, name, record)
        window.update()

    def answered(self, name, count):
        return lambda: self.handled.count(name) > count

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
        self.check_for_updates.side_effect = result if isinstance(result, Exception) else None
        self.check_for_updates.return_value = result
        before = self.handled.count("_update_checked")
        self.ui.check_updates(asked)
        self.settle(self.answered("_update_checked", before))
        self.window.update()
        self.check_for_updates.assert_called_with(__version__)

    def history(self, result):
        """Open the dialog from the version number and wait for the answer about the release history."""
        self.release_history.side_effect = result if isinstance(result, Exception) else None
        self.release_history.return_value = result
        before = self.handled.count("_history_arrived")
        self.ui.version_link.event_generate("<Button-1>")
        self.settle(self.answered("_history_arrived", before))
        self.window.update()
        self.release_history.assert_called_with(__version__)

    def dialogs(self):
        return [w for w in self.window.winfo_children() if isinstance(w, tk.Toplevel)]

    def shown(self):
        ui = self.ui
        return (ui.version_notes.get("1.0", "end").strip(), ui.version_state["text"],
                [b["text"] for b in ui.version_buttons.winfo_children()])

    def button(self, text):
        return next(b for b in self.ui.version_buttons.winfo_children() if b["text"] == text)

    def test_corner_shows_the_project_link_and_version_without_asking_the_network(self):
        ui = self.ui
        self.settle()
        self.check_for_updates.assert_not_called()
        self.release_history.assert_not_called()
        self.assertEqual((ui.github_link["text"], ui.version_link["text"]), ("GitHub", "v" + __version__))
        self.assertTrue(ui.github_link.winfo_ismapped() and ui.version_link.winfo_ismapped())
        self.assertFalse(ui.update_dot.winfo_ismapped())
        self.assertLessEqual(len(self.window.tk.call("after", "info")), 3)
        with patch.object(app.webbrowser, "open") as opened:
            ui.github_link.event_generate("<Button-1>")
            self.window.update()
        opened.assert_called_once_with(PROJECT_URL, new=2)
        self.assertEqual(PROJECT_URL, "https://github.com/pretenderlu/ClassicDrop")
        self.assertEqual(ui.feedback_link["text"], "反馈")
        with patch.object(app.webbrowser, "open") as opened:
            ui.feedback_link.event_generate("<Button-1>")
            self.window.update()
            dialog, = self.dialogs()
            self.assertEqual(dialog.title(), "反馈问题")
            self.assertIn("ClassicDrop：v" + __version__, ui.feedback_text.get("1.0", "end"))
            opened.assert_not_called()
            copy = next(b for b in ui.feedback_buttons.winfo_children() if b["text"] == "复制并打开 GitHub")
            copy.invoke()
            self.window.update()
        url = opened.call_args.args[0]
        self.assertTrue(url.startswith(PROJECT_URL + "/issues/new?body="))
        body = parse_qs(urlsplit(url).query)["body"][0]
        self.assertIn("ClassicDrop：v" + __version__, body)
        self.assertIn("Windows", body)
        copied = self.window.clipboard_get()
        self.assertIn("ClassicDrop：v" + __version__, copied)
        self.assertIn("最近的日志", copied)
        self.assertEqual(self.dialogs(), [])

    def test_a_failed_operation_is_remembered_and_points_at_feedback(self):
        import diagnostics
        diagnostics.reset()
        self.addCleanup(diagnostics.reset)
        with patch.object(app.messagebox, "showerror") as shown:
            self.ui._work("测试操作", lambda report: 1 / 0, lambda result: None)
            self.settle(lambda: shown.called)
        text = shown.call_args.args[1]
        self.assertIn("division by zero", text)
        self.assertIn("「反馈」", text)
        report = diagnostics.report()
        self.assertIn("division by zero", report)
        self.assertIn("ZeroDivisionError", report)

    def test_quiet_check_shows_a_red_dot_and_a_manual_check_reports(self):
        ui = self.ui
        self.check(OSError("offline"))
        self.assertNotIn("检查更新", ui.status.get())
        self.check(OSError("offline"), asked=True)
        self.assertIn("检查更新失败：offline", ui.status.get())
        self.check(None, asked=True)
        self.assertEqual(ui.status.get(), f"当前已是最新版本 {__version__}。")
        self.assertEqual((ui.update_dot.winfo_ismapped(), self.dialogs()), (0, []))
        self.check(NEWER)
        self.assertEqual((ui.update_dot.winfo_ismapped(), str(ui.update_dot["fg"]), self.dialogs()), (1, app.theme.ALERT, []))
        self.assertIn("发现新版本 v9.9.9", ui.status.get())
        ui.set_theme("dark")
        self.addCleanup(app.theme.use, "light")
        self.window.update()
        self.assertEqual((ui.update_dot.winfo_ismapped(), ui.version_link["text"]), (1, "v" + __version__))
        # Asking by hand goes straight to the dialog that offers it.
        self.release_history.return_value = (HISTORY, NEWER)
        before = self.handled.count("_history_arrived")
        self.check(NEWER, asked=True)
        self.settle(self.answered("_history_arrived", before))
        self.assertEqual(len(self.dialogs()), 1)
        self.assertIn("下载并安装 v9.9.9", self.shown()[2])

    def test_version_number_opens_the_history_and_installs_the_newer_release(self):
        ui = self.ui
        self.history((HISTORY, NEWER))
        dialog, = self.dialogs()
        self.assertEqual(dialog.title(), "版本与更新")
        text, state, buttons = self.shown()
        self.assertEqual((state, buttons), ("发现新版本 v9.9.9", ["关闭", "下载并安装 v9.9.9"]))
        self.assertEqual(text.splitlines(), [
            "v9.9.9   2026-12-01   新版本", "· 新增 A", "· 修复 B",
            f"v{__version__}   2026-11-25   当前版本", "· 这一版的内容",
            "v1.0.0", "（没有更新说明）"])
        self.assertTrue(ui.update_dot.winfo_ismapped())  # found here, though the check at start never ran
        ui.open_versions()
        self.assertEqual(len(self.dialogs()), 1)
        self.button("关闭").invoke()
        self.assertEqual(self.dialogs(), [])
        self.history((HISTORY, NEWER))
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
            self.button("下载并安装 v9.9.9").invoke()
            self.assertEqual(self.dialogs(), [])
            self.settle(lambda: closed.called)
        target = self.site / "ClassicDrop" / "updates" / "ClassicDropSetup-v9.9.9.exe"
        self.assertEqual(fetched.call_args.args[:2], (NEWER, target))
        self.assertEqual((reports, target.read_bytes()), ([True] * 4, b"setup"))
        started.assert_called_once_with([str(target), "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"], cwd=target.parent)
        closed.assert_called_once_with()

    def test_long_notes_break_between_chinese_characters_and_keep_words_whole(self):
        def measure(text):
            return sum(2 if ord(c) > 0x2E80 else 1 for c in text)
        wrap = app.theme.wrap_lines
        self.assertEqual(wrap("· 支持 iPod nano 第 3 代和第 4 代（尚未真机验证）：见 README。", measure, 20).split("\n"),
                         ["· 支持 iPod nano 第", "  3 代和第 4 代（尚", "  未真机验证）：见", "  README。"])
        self.assertEqual(wrap("开发时没有真机，依据来自 iOpenPod 的机型资料。", measure, 20).split("\n"),
                         ["开发时没有真机，依据", "来自 iOpenPod 的机型", "资料。"])
        self.assertEqual(wrap("  · 二级 ClassicDropSetup-v1.5.2.exe 安装包", measure, 20).split("\n"),
                         ["  · 二级", "    ClassicDropSetup-v1.5.2.exe", "    安装包"])
        self.assertEqual(wrap("一行\n\n两行", measure, 20), "一行\n\n两行")
        long = Release("v9.9.9", PAGE + "v9.9.9", "", "· " + "很长的一条更新说明，" * 12)
        self.history(((long,), NEWER))
        lines = self.shown()[0].splitlines()
        self.assertGreater(len(lines), 3)
        self.assertTrue(all(line.startswith("  ") for line in lines[2:]))
        self.assertEqual("".join(lines[1:]).replace(" ", ""), long.notes.replace(" ", ""))

    def test_history_of_the_newest_version_offers_nothing(self):
        self.history((HISTORY[1:], None))
        text, state, buttons = self.shown()
        self.assertEqual((state, buttons, text.splitlines()[0]), ("已是最新版本", ["关闭"], f"v{__version__}   2026-11-25   当前版本"))
        self.assertFalse(self.ui.update_dot.winfo_ismapped())
        self.ui.close_versions()
        self.history(((), None))
        self.assertEqual(self.shown(), ("还没有发布记录。", "已是最新版本", ["关闭"]))

    def test_unreachable_history_still_offers_an_update_found_at_start(self):
        ui = self.ui
        self.history(OSError("offline"))
        self.assertEqual(self.shown(), ("无法读取更新记录：offline", "", ["关闭"]))
        ui.close_versions()
        self.check(NEWER)
        self.history(OSError("offline"))
        text, state, buttons = self.shown()
        self.assertEqual((text.splitlines(), state, buttons),
                         (["v9.9.9   新版本", "· 新增 A", "· 修复 B", "", "无法读取更新记录：offline"],
                          "发现新版本 v9.9.9", ["关闭", "下载并安装 v9.9.9"]))
        # An answer that arrives after the dialog was closed is dropped.
        ui.close_versions()
        before = self.handled.count("_history_arrived")
        ui.open_versions()
        ui.close_versions()
        self.settle(self.answered("_history_arrived", before))
        self.assertEqual(self.dialogs(), [])

    def test_update_waits_for_work_and_queued_songs_and_failures_offer_a_retry(self):
        ui = self.ui
        self.check(NEWER)
        with patch.object(app.messagebox, "showwarning") as warned:
            ui.pending = {Path("a.mp3"): None}
            self.history((HISTORY, NEWER))  # reading the history is always allowed
            self.button("下载并安装 v9.9.9").invoke()
            self.assertIn("待传列表里还有 1 首", warned.call_args.args[1])
            ui.pending = {}
            ui.busy = True
            ui.download_update()
            ui.busy = False
            self.assertEqual((warned.call_count, self.dialogs()), (2, []))
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
        bare = UpdateInfo("v9.9.9", NEWER.url, notes="")
        self.history((HISTORY, bare))
        self.assertEqual(self.shown()[2], ["关闭", "打开发布页"])
        with patch.object(app.webbrowser, "open") as opened, patch.object(app, "download_update") as fetched:
            self.button("打开发布页").invoke()
        opened.assert_called_once_with(NEWER.url, new=2)
        fetched.assert_not_called()
        self.assertEqual(self.dialogs(), [])
