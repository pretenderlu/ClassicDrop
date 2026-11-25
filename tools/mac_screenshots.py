"""Capture the app on macOS CI so fonts, scaling and dialogs can be reviewed.

Usage: python tools/mac_screenshots.py <output-dir>
Runs the real app with a mock device holding three cover-art songs under a throwaway HOME,
then crops windows out of a full-screen capture (points → pixels via the screen ratio).
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(ROOT / "tests"))


def pump(window, seconds=0.6):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        window.update()
        time.sleep(0.02)


def capture(window, widget, name, out):
    """Full-screen grab cropped to widget's on-screen geometry (Retina-aware)."""
    window.update_idletasks()
    pump(window, 0.4)
    tmp = out / "_full.png"
    subprocess.run(["screencapture", "-x", str(tmp)], check=True)
    from PIL import Image
    shot = Image.open(tmp)
    scale = shot.width / window.winfo_screenwidth()
    x, y = widget.winfo_rootx(), widget.winfo_rooty()
    w, h = widget.winfo_width(), widget.winfo_height()
    shot.crop((round(x * scale), round(y * scale), round((x + w) * scale), round((y + h) * scale))).save(out / name)
    tmp.unlink()


def main(out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home  # before app/system import so DATA/LOGS point inside it
        import app
        from test_classicdrop import FIXTURES, Device, mock_device
        from test_native_covers import covered_m4a
        from transfer import transfer

        root = Path(home) / "iPod"
        root.mkdir()
        device = mock_device(root)
        site = Path(home) / "music"
        site.mkdir()
        songs = []
        for i, color in enumerate(("red", "green", "blue")):
            src = covered_m4a(site / f"{color}.m4a", color)
            songs.append(src)
        try:
            transfer(device, songs, Path(home) / "backups")
            device = Device.open(root)
            print("transfer ok:", len(device.library.snapshot.tracks), "tracks")
        except Exception as exc:
            print("transfer failed:", repr(exc))

        window = app.TkinterDnD.Tk()
        window.geometry("1100x700+60+60")
        ui = app.App(window)
        ui.load(str(root))  # real attach path: sets device, covers and the status line
        pump(window, 8)
        print("status:", ui.status.get())
        print("rows:", len(ui.tree.get_children()), "busy:", ui.busy)

        def dialog_shot(dialog_attr, name):
            dialog = getattr(ui, dialog_attr, None)
            if dialog is None or not dialog.winfo_exists():
                print("no dialog for", name)
                return
            dialog.lift()
            capture(window, dialog, name, out)
            dialog.destroy()
            pump(window, 0.2)

        for name in ("light", "dark"):
            ui.set_theme(name)
            pump(window)
            capture(window, window, f"main-{name}.png", out)

        ui.set_theme("light")
        ui.set_browse("album")
        pump(window)
        capture(window, window, "album-light.png", out)

        # Import preview dialog
        import import_dialog as id_mod
        ui.add_files([FIXTURES / "tone.mp3"])
        pump(window, 2)
        preview = next((w for w in window.winfo_children() if isinstance(w, id_mod.ImportDialog)), None)
        if preview:
            pump(window, 2)
            capture(window, preview, "import-preview.png", out)
            preview.cancel()
            pump(window, 0.3)
        else:
            print("import dialog not found")

        ui.open_versions()
        pump(window, 2)
        dialog_shot("update_dialog", "versions.png")

        ui.open_feedback()
        pump(window)
        dialog_shot("feedback_dialog", "feedback.png")

        ui.action_menu(window.winfo_rootx() + 400, window.winfo_rooty() + 200)
        pump(window, 0.4)
        capture(window, window, "context-menu.png", out)
        window.update()
        window.destroy()
    print("screenshots written to", out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "screenshots"))
