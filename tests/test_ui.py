import ctypes
import os
from dataclasses import replace
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
import unittest
from tempfile import TemporaryDirectory

from test_classicdrop import FIXTURES, ROOT, mock_device
from test_formats_playlists import write_wave
from test_native_covers import ART_DB, FORMATS, covered_m4a, picture
sys.path.insert(0, str(ROOT))
from tkinter import ttk
import app
import system
from import_dialog import MODE_NAMES
from library import Device, playlist_edit
from transfer import transfer


class WindowsUITests(unittest.TestCase):
    def review(self, window, open_dialog, interact):
        errors = []
        finished = False
        deadline = time.monotonic() + 12
        def poll():
            nonlocal finished
            dialog = next((w for w in window.winfo_children() if isinstance(w, app.ImportDialog)), None)
            if dialog and dialog.rows and not dialog.scanning:
                try:
                    if system.WINDOWS:
                        self.assertIsNone(dialog.grab_current())
                        self.assertTrue(app._disabled(window))
                    else:
                        # Modality comes from the dialog's grab where -disabled does not exist.
                        self.assertIs(dialog.grab_current(), dialog)
                    interact(dialog)
                except BaseException as exc:
                    errors.append(exc)
                    dialog.cancel()
                finished = True
            elif time.monotonic() >= deadline:
                errors.append(AssertionError("Import preview did not finish loading"))
                if dialog:
                    dialog.cancel()
                finished = True
            else:
                window.after(40, poll)
        timer = window.after(60, poll)
        try:
            open_dialog()
            while not finished:
                window.update()
                time.sleep(.01)
        finally:
            window.after_cancel(timer)
        if errors:
            raise errors[0]
        self.assertFalse(app._disabled(window))

    def make_window(self):
        window = app.TkinterDnD.Tk()
        def cleanup():
            for timer in window.tk.call("after", "info"):
                window.after_cancel(timer)
            window.destroy()
        self.addCleanup(cleanup)
        return window

    @unittest.skipUnless(sys.platform == "win32", "OLE 拖放事件只在 Windows 上存在")
    def test_windows_drop_finishes_before_preview_and_releases_mouse(self):
        window = self.make_window()
        ui = app.App(window)
        window.geometry("900x560+28000+28000")
        window.update()
        path = FIXTURES / "tone.mp3"
        def drop():
            target = str(ui.tree)
            window.tk.call("::tkdnd::olednd::HandleDragEnter", target, ("CF_HDROP",), ("copy",), (), 10, 10, (15,))
            effect = window.tk.call("::tkdnd::olednd::HandleDrop", target, (), 10, 10, "CF_HDROP", (str(path),))
            self.assertEqual(effect, "copy")
            window.update_idletasks()
            self.assertFalse(any(isinstance(w, app.ImportDialog) for w in window.winfo_children()))
        def check(dialog):
            self.assertEqual(tuple(dialog.rows), (path,))
            self.assertTrue(True if os.name != "nt" else not ctypes.windll.user32.GetCapture())
            dialog.cancel()
        self.review(window, drop, check)
        self.assertFalse(ui.pending)

    def test_eject_button_keeps_queue_and_handles_busy_or_veto(self):
        with TemporaryDirectory() as tmp:
            copy = mock_device(Path(tmp) / "iPod")
            window = self.make_window()
            ui = app.App(window)
            window.geometry("900x560+28000+28000")
            window.update()
            ui.device = copy
            ui.render()
            self.assertEqual(str(ui.eject_button["state"]), "disabled")
            self.review(window, lambda: ui.add_files([FIXTURES / "tone.mp3"]), lambda d: d.queue_button.invoke())
            dev = replace(copy, guid=None, usb_instance=r"USB\VID_05AC&PID_1261\TEST")
            ui.device = dev
            ui.render()
            self.assertEqual(str(ui.eject_button["state"]), "normal")
            def settle():
                end = time.monotonic() + 5
                while ui.busy and time.monotonic() < end:
                    window.update()
                    time.sleep(.01)
                self.assertFalse(ui.busy)
            with patch.object(Device, "eject", side_effect=[OSError("设备被占用"), None]) as eject, patch.object(app.messagebox, "showerror") as error:
                ui.busy = True
                ui._buttons()
                ui.eject_button.invoke()
                ui.eject()
                eject.assert_not_called()
                ui.busy = False
                ui._buttons()
                ui.eject_button.invoke()
                settle()
                self.assertIs(ui.device, dev)
                self.assertIn("设备被占用", error.call_args.args[1])
                ui.eject_button.invoke()
                settle()
            self.assertIsNone(ui.device)
            self.assertIn("已安全弹出", ui.status.get())
            self.assertEqual(str(ui.eject_button["state"]), "disabled")
            self.assertEqual(list(ui.pending), [FIXTURES / "tone.mp3"])

    def test_folder_preview_edit_single_and_album_confirm_isolated_batch(self):
        with TemporaryDirectory() as tmp:
            site = Path(tmp).resolve()
            root = site / "stock"
            device = mock_device(root)
            transfer(device, [], site / "setup", desired=playlist_edit(device.library.snapshot, [], name="新专辑"))
            device = Device.open(root)
            album = site / "音乐/目录歌手/2020-目录专辑"
            album.mkdir(parents=True)
            first = album / "01.wav"
            second = album / "02.wav"
            write_wave(first)
            write_wave(second, channels=1)
            (album / "cover.png").write_bytes(picture("red"))
            custom = site / "blue.png"
            custom.write_bytes(picture("blue"))
            original_audio = [p.read_bytes() for p in (first, second)]
            window = self.make_window()
            ui = app.App(window)
            window.geometry("1000x640+28000+28000")
            window.update()
            def offline(d):
                self.assertFalse(d.device_name)
                self.assertEqual(str(d.send_button["state"]), "disabled")
                d.queue_button.invoke()
            self.review(window, lambda: ui.add_files([FIXTURES / "tone.mp3"]), offline)
            ui.device = device
            ui.render()
            def edit(d):
                self.assertEqual(tuple(d.rows), (first, second))
                self.assertEqual((d.values["artist"].get(), d.values["album"].get(), d.cover_name["text"]),
                                 ("目录歌手", "目录专辑", "cover.png"))
                self.assertIsNotNone(d.photo)
                d.select_all()
                window.update()
                self.assertEqual(str(d.entries["title"]["state"]), "disabled")
                d.values["artist"].set("最终歌手")
                d.values["album"].set("最终专辑")
                with patch.object(app.filedialog, "askopenfilename", return_value=str(custom)):
                    d.replace_button.invoke()
                d.tree.selection_set(str(first))
                window.update()
                d.values["title"].set("改好的歌名")
                d.mode.set(MODE_NAMES["aac"])
                d.mode.event_generate("<<ComboboxSelected>>")
                window.update()
                self.assertEqual((d.rows[first].artist, d.rows[second].artist), ("最终歌手", "最终歌手"))
                self.assertEqual(d.rows[second].mode, "alac")
                self.assertEqual((root / app.DB).read_bytes(), device.original)
            def cancel(d):
                edit(d)
                d.new_list_button.invoke()
                self.assertEqual(d.playlist_entry.get(), "最终专辑")
                d.cancel()
            with patch.object(app.filedialog, "askdirectory", return_value=str(album)), patch.object(app, "BACKUPS", site / "backups"):
                self.review(window, ui.pick_folder, cancel)
                self.assertEqual(list(ui.pending), [FIXTURES / "tone.mp3"])
                self.assertFalse((site / "backups").exists())
                def send(d):
                    edit(d)
                    d.target.current(1)
                    d.new_list_button.invoke()
                    d.new_list_button.invoke()
                    self.assertEqual(d.target.get(), "新专辑")
                    d.new_list_button.invoke()
                    d.playlist_entry.delete(0, "end")
                    d.send_button.invoke()
                    self.assertIn("请输入", d.status.get())
                    d.playlist_entry.insert(0, "新专辑")
                    d.send_button.invoke()
                    self.assertIn("同名", d.status.get())
                    self.assertEqual((root / app.DB).read_bytes(), device.original)
                    d.playlist_entry.delete(0, "end")
                    d.playlist_entry.insert(0, "  自选歌单  ")
                    d.send_button.invoke()
                with patch.object(app.messagebox, "showerror") as error:
                    self.review(window, ui.pick_folder, send)
                    stop = time.monotonic() + 15
                    while ui.busy and time.monotonic() < stop:
                        window.update()
                        time.sleep(.01)
                    self.assertFalse(ui.busy)
                    error.assert_not_called()
            self.assertEqual(list(ui.pending), [FIXTURES / "tone.mp3"])
            self.assertIsNone(ui.queue_target)
            library = ui.device.library.with_artwork((root / ART_DB).read_bytes())
            existing, created = library.snapshot.playlists
            self.assertEqual(existing.track_ids, ())
            self.assertEqual((created.name, created.track_ids), ("自选歌单", tuple(t.track_id for t in library.snapshot.tracks)))
            self.assertEqual(ui.view, created.playlist_id)
            self.assertEqual([t.title for t in library.snapshot.tracks], ["改好的歌名", "02"])
            self.assertEqual([t.metadata.file_format for t in library.snapshot.tracks], ["AAC audio file", "Apple Lossless audio file"])
            for track in library.snapshot.tracks:
                self.assertEqual((track.artist, track.album), ("最终歌手", "最终专辑"))
                self.assertIsNotNone(library.artwork_read(track.artwork_id, FORMATS, 320))
            self.assertEqual([p.read_bytes() for p in (first, second)], original_audio)

    def test_offline_queue_remembers_new_playlist_until_transfer(self):
        with TemporaryDirectory() as tmp:
            site = Path(tmp).resolve()
            device = mock_device(site / "IPC3")
            window = self.make_window()
            ui = app.App(window)
            window.geometry("1000x640+28000+28000")
            window.update()
            files = [FIXTURES / "tone.mp3", FIXTURES / "aac.m4a"]
            def queue(d):
                d.new_list_button.invoke()
                self.assertEqual(d.playlist_entry.get(), next(iter(d.rows.values())).album)
                d.playlist_entry.delete(0, "end")
                d.playlist_entry.insert(0, "离线歌单")
                d.queue_button.invoke()
            def edit(d):
                self.assertEqual(tuple(d.rows), tuple(files))
                self.assertEqual(d.playlist_entry.get(), "离线歌单")
                d.playlist_entry.delete(0, "end")
                d.playlist_entry.insert(0, "出门听")
                d.queue_button.invoke()
            def settle():
                deadline = time.monotonic() + 15
                while ui.busy and time.monotonic() < deadline:
                    window.update()
                    time.sleep(.01)
                self.assertFalse(ui.busy)
            with patch.object(app, "BACKUPS", site / "backup"), patch.object(app.messagebox, "showerror") as error:
                self.review(window, lambda: ui.add_files(files), queue)
                self.assertEqual(ui.target.get(), "离线歌单（新建）")
                self.assertEqual(str(ui.send_button["state"]), "disabled")
                ui.tree.selection_set("0")
                self.review(window, ui.queue_new_list.invoke, edit)
                self.assertEqual(ui.target.get(), "出门听（新建）")
                extra = FIXTURES / "lossless.m4a"
                self.review(window, lambda: ui.add_files([extra]), lambda d: d.queue_button.invoke())
                ui.tree.selection_set("2")
                def remove_extra(d):
                    self.assertEqual(tuple(d.rows), (extra,))
                    d.remove_selected()
                    d.queue_button.invoke()
                self.review(window, ui.preview_button.invoke, remove_extra)
                self.assertEqual(tuple(ui.pending), tuple(files))
                self.assertEqual(ui.target.get(), "出门听（新建）")
                ui.load(device.root)
                settle()
                self.assertEqual(ui.target.get(), "出门听（新建）")
                self.assertEqual((device.root / app.DB).read_bytes(), device.original)
                self.assertFalse((site / "backup").exists())
                ui.send_button.invoke()
                settle()
                error.assert_not_called()
            snap = ui.device.library.snapshot
            created, = snap.playlists
            self.assertEqual((created.name, created.track_ids), ("出门听", tuple(t.track_id for t in snap.tracks)))
            self.assertEqual((ui.view, ui.queue_target, ui.target.get()), (created.playlist_id, created.playlist_id, "出门听"))
            self.assertFalse(ui.pending)

    def test_format_prompt_queue_playlist_controls_and_library_files(self):
        with TemporaryDirectory() as tmp:
            site = Path(tmp).resolve()
            root = site / "临时 iPod"
            device = mock_device(root)
            wav = site / "第一首.wav"
            write_wave(wav)
            second = site / "第二首.wav"
            write_wave(second, channels=1)
            hires = site / "高解析.wav"
            write_wave(hires, rate=96000, width=3)
            window = self.make_window()
            ui = app.App(window)
            window.geometry("1000x640+28000+28000")
            window.update()
            ui.device = device
            ui.render()

            def settle():
                deadline = time.monotonic() + 20
                while ui.busy and time.monotonic() < deadline:
                    window.update()
                    time.sleep(.01)
                self.assertFalse(ui.busy)

            observed = []
            def answer(value):
                def click(dialog):
                    song = next(iter(dialog.rows.values()))
                    observed.append((song.mode, song.choices))
                    if value == "cancel":
                        dialog.cancel()
                        return
                    if value is not None:
                        dialog.mode.set(MODE_NAMES[value])
                        dialog.change_mode()
                    dialog.queue_button.invoke()
                return click

            with patch.object(app, "BACKUPS", site / "backup"), patch.object(app.messagebox, "showerror") as error:
                self.review(window, lambda: ui.add_files([FIXTURES / "tone.mp3"]), lambda d: d.queue_button.invoke())
                self.review(window, lambda: ui.add_files([wav, second]), answer("cancel"))
                self.assertEqual(list(ui.pending), [FIXTURES / "tone.mp3"])
                self.review(window, lambda: ui.drop_files(SimpleNamespace(data=" ".join("{" + str(p) + "}" for p in (wav, second)))), answer(None))
                self.assertEqual(ui.pending[wav].mode, "alac")
                ui.tree.selection_set("2")
                self.review(window, ui.preview_button.invoke, answer("aac"))
                self.assertEqual(ui.pending[second].mode, "aac")
                self.review(window, lambda: ui.add_files([hires]), answer("cancel"))
                self.assertEqual([v for v, _ in observed], ["alac", "alac", "alac", "alac-48"])
                self.assertEqual(observed[-1][1], ("alac-48",))
                self.assertEqual((root / app.DB).read_bytes(), device.original)

                with patch.object(app.simpledialog, "askstring", return_value="测试播放列表"):
                    ui.new_list.invoke()
                settle()
                target = ui.device.library.snapshot.playlists[0].playlist_id
                self.assertEqual((ui.view, ui.target.get(), ui.queue_target), ("queue", "测试播放列表", target))
                self.assertEqual(ui.target.current(), 1)
                ui.target.current(0)
                ui.target.event_generate("<<ComboboxSelected>>")
                window.update()
                self.assertIsNone(ui.queue_target)
                ui.target.current(1)
                ui.target.event_generate("<<ComboboxSelected>>")
                window.update()
                ui.send_button.invoke()
                settle()
                snap = ui.device.library.snapshot
                self.assertEqual(ui.view, target)
                self.assertEqual(snap.playlists[0].track_ids, tuple(t.track_id for t in snap.tracks))
                self.assertEqual([t.metadata.file_format for t in snap.tracks],
                                 ["MPEG audio file", "Apple Lossless audio file", "AAC audio file"])
                before_audio = [(root / t.metadata.location).read_bytes() for t in snap.tracks]
                ui.tree.selection_set("1", "2")
                with patch.object(app.tk.Menu, "tk_popup", autospec=True) as popup:
                    ui.action_menu()
                    menu = popup.call_args.args[0]
                    with patch.object(app.simpledialog, "askstring", return_value=None):
                        menu.invoke("批量修改歌手")
                    self.assertFalse(ui.busy)
                    with patch.object(app.simpledialog, "askstring", return_value="李荣浩"):
                        menu.invoke("批量修改歌手")
                settle()
                fixed = ui.device.library.snapshot
                self.assertEqual([t.artist for t in fixed.tracks], [snap.tracks[0].artist, "李荣浩", "李荣浩"])
                self.assertEqual(fixed.playlists[0].track_ids, snap.playlists[0].track_ids)
                self.assertEqual([(root / t.metadata.location).read_bytes() for t in fixed.tracks], before_audio)
                ui.tree.selection_set("1")
                window.update()
                buttons = {b["text"]: b for b in ui.list_bar.winfo_children()}
                buttons["上移"].invoke()
                settle()
                self.assertEqual(ui.tree.item("0", "values")[0], wav.stem)
                self.assertEqual(ui.tree.selection(), ("0",))
                ui.delete_key(None)
                settle()
                self.assertEqual(len(ui.device.library.snapshot.tracks), 3)
                self.assertEqual(len(ui.device.library.snapshot.playlists[0].entries), 2)
                with patch.object(app.simpledialog, "askstring", return_value="通勤"):
                    buttons["改名"].invoke()
                settle()
                self.assertEqual(ui.heading["text"], "通勤")
                with patch.object(app.messagebox, "askyesno", return_value=False):
                    buttons["删除歌单"].invoke()
                self.assertEqual(len(ui.device.library.snapshot.playlists), 1)
                with patch.object(app.messagebox, "askyesno", return_value=True):
                    buttons["删除歌单"].invoke()
                settle()
                self.assertFalse(ui.device.library.snapshot.playlists)
                self.assertEqual(len(ui.device.library.snapshot.tracks), 3)
                error.assert_not_called()

    def test_file_drop_transfer_search_playlist_edit_and_export(self):
        with TemporaryDirectory() as tmp:
            site = Path(tmp).resolve()
            root = site / "测试 iPod"
            mock_device(root)
            window = self.make_window()
            ui = app.App(window)
            window.geometry("1000x640+28000+28000")
            def settle():
                end = time.monotonic() + 20
                while time.monotonic() < end:
                    window.update()
                    if not ui.busy:
                        return
                    time.sleep(0.01)
                self.fail("GUI operation timed out")
            with patch.object(app, "BACKUPS", site / "backup"), patch.object(app.messagebox, "showerror") as error:
                ui.load(root)
                settle()
                self.assertEqual(ui.device_name["text"], "测试 iPod")
                files = [FIXTURES / "tone.mp3", covered_m4a(site / "有封面.m4a")]
                self.review(window, lambda: ui.drop_files(SimpleNamespace(data=" ".join("{" + str(p) + "}" for p in files))),
                            lambda d: d.queue_button.invoke())
                self.assertEqual(ui.view, "queue")
                self.assertEqual(len(ui.tree.get_children()), 2)
                with patch("transfer.prepare_covers", return_value=(None, 0)):
                    ui.send_button.invoke()
                    settle()
                self.assertEqual(len(ui.device.library.snapshot.tracks), 2)
                self.assertFalse(ui.pending)
                self.assertFalse((root / ART_DB).exists())
                with patch.object(app.tk.Menu, "tk_popup", autospec=True) as popup:
                    ui.tools_button.invoke()
                    popup.call_args.args[0].invoke("补齐设备封面")
                settle()
                self.assertIn("已补齐 1 首", ui.status.get())
                self.assertIn("1 首歌曲没有可读取的内嵌封面", ui.status.get())
                self.assertTrue((root / ART_DB).is_file())
                ui.search.set("mp3")
                ui.render()
                self.assertEqual(len(ui.tree.get_children()), 1)
                ui.tree.selection_set("0")
                with patch.object(app.simpledialog, "askstring", return_value="随身听"):
                    ui.change_playlist()
                settle()
                playlist = ui.device.library.snapshot.playlists[0]
                self.assertEqual(playlist.name, "随身听")
                ui.view = playlist.playlist_id
                ui.search.set("")
                ui.render()
                ui.tree.selection_set("0")
                ui.edit_tags()
                dialog = next(w for w in window.winfo_children() if w.winfo_class() == "Toplevel")
                field = next(w for w in dialog.winfo_children() if isinstance(w, ttk.Entry))
                field.delete(0, "end")
                field.insert(0, "随身试听曲")
                next(w for w in dialog.winfo_children() if isinstance(w, ttk.Button)).invoke()
                settle()
                self.assertEqual(ui.tree.item("0", "values")[0], "随身试听曲")
                ui.tree.selection_set("0")
                directory = site / "export"
                directory.mkdir()
                with patch.object(app.filedialog, "askdirectory", return_value=str(directory)):
                    ui.export()
                settle()
                self.assertEqual(next(directory.iterdir()).read_bytes(), files[0].read_bytes())
                with patch.object(app.tk.Menu, "tk_popup", autospec=True) as popup:
                    ui.tools_button.invoke()
                    tools = popup.call_args.args[0]
                    with patch.object(app.simpledialog, "askstring", return_value="路上的歌"):
                        tools.invoke("修改设备名称")
                    settle()
                    self.assertEqual(ui.device_name["text"], "路上的歌")

                    ui.tree.selection_set("0")
                    ui.action_menu()
                    actions = popup.call_args.args[0]
                    with patch.object(app.messagebox, "askyesno", return_value=False):
                        actions.invoke("从 iPod 删除歌曲")
                    self.assertEqual(len(ui.device.library.snapshot.tracks), 2)
                    with patch.object(app.messagebox, "askyesno", return_value=True):
                        actions.invoke("从 iPod 删除歌曲")
                    settle()
                    self.assertEqual(len(ui.device.library.snapshot.tracks), 1)
                    self.assertEqual(ui.device.library.snapshot.playlists[0].track_ids, ())

                    ui.search.set("无匹配")
                    ui.render()
                    ui.tools_button.invoke()
                    tools = popup.call_args.args[0]
                    with patch.object(app.simpledialog, "askstring", return_value="取消"):
                        tools.invoke("清空音乐库")
                    self.assertEqual(len(ui.device.library.snapshot.tracks), 1)
                    with patch.object(app.simpledialog, "askstring", return_value="清空"):
                        tools.invoke("清空音乐库")
                    settle()
                    self.assertEqual(ui.device.library.snapshot.tracks, ())
                    self.assertEqual(ui.device_name["text"], "路上的歌")
                    ui.tools_button.invoke()
                    self.assertEqual(popup.call_args.args[0].entrycget("清空音乐库", "state"), "disabled")
                    self.assertEqual(popup.call_args.args[0].entrycget("补齐设备封面", "state"), "disabled")
                error.assert_not_called()

    def test_readonly_badge_explains_reason_and_unsigned_disables_guid_entry(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "iPod"
            copy = mock_device(root)
            window = self.make_window()
            ui = app.App(window)
            window.geometry("900x560+28000+28000")
            ui.device = replace(copy, guid=None, readonly_reason="no_apple_usb")
            ui.render()
            window.update()
            self.assertEqual(str(ui.device_badge["cursor"]), "hand2")
            with patch.object(app.messagebox, "showinfo") as info:
                ui.device_badge.event_generate("<Button-1>")
                window.update()
            self.assertIn("读卡器", info.call_args.args[1])
            with patch.object(app.tk.Menu, "tk_popup", autospec=True) as popup:
                ui.tools_button.invoke()
                self.assertEqual(popup.call_args.args[0].entrycget("填写设备 GUID", "state"), "normal")
                ui.device = replace(copy, guid=None, readonly_reason="unsigned", signature_scheme=0)
                ui.render()
                ui.tools_button.invoke()
                self.assertEqual(popup.call_args.args[0].entrycget("填写设备 GUID", "state"), "disabled")
