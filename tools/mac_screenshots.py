"""Capture the main window (light and dark) on macOS CI so fonts and scaling can be reviewed.

Usage: python tools/mac_screenshots.py <output-dir>
Runs the real app with the tests' mock device under a throwaway HOME; screencapture -R crops
the window region straight from the screen. Non-destructive: no settings or devices are touched.
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


def capture(window, name, out):
    window.update_idletasks()
    window.update()
    time.sleep(0.5)
    x, y = window.winfo_rootx(), window.winfo_rooty()
    w, h = window.winfo_width(), window.winfo_height()
    subprocess.run(["screencapture", "-x", "-R", f"{x},{y},{w},{h}", str(out / name)], check=True)


def main(out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as home:
        # Keep the app's DATA/LOGS inside the temp dir; HOME must be set before importing app.
        os.environ["HOME"] = home
        import app
        import theme
        from test_classicdrop import FIXTURES, mock_device
        from transfer import transfer

        root = Path(home) / "iPod"
        root.mkdir()
        device = mock_device(root)
        transfer(device, [FIXTURES / "tone.mp3", FIXTURES / "aac.m4a", FIXTURES / "lossless.m4a"],
                 Path(home) / "backups")

        window = app.TkinterDnD.Tk()
        window.geometry("1100x700+60+60")
        ui = app.App(window)
        ui.device = device
        ui.render()
        for name in ("light", "dark"):
            ui.set_theme(name)
            capture(window, f"main-{name}.png", out)
        window.destroy()
    print("screenshots written to", out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "screenshots"))
