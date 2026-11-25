"""Diagnose afconvert channel handling on the CI Mac (read-only; temp files only).

For mono and stereo test WAVs, runs the app's real conversion path (audio_convert._convert,
which calls encoders.afconvert_encode), records the exact ffmpeg/afconvert command lines,
keeps the intermediate CAF, and prints afinfo + channel info for both files.
Usage: python tools/mac_afconvert_probe.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(ROOT / "tests"))

import audio_convert
import encoders
from test_formats_playlists import write_wave


def show(command):
    p = subprocess.run(command, capture_output=True, text=True)
    print("$", " ".join(str(c) for c in command))
    print((p.stdout or p.stderr).strip()[:600] or f"(exit {p.returncode})")


class Keep:
    def __init__(self, folder):
        self.folder = folder

    def __enter__(self):
        return self.folder

    def __exit__(self, *exc):
        return False


def main():
    print(subprocess.run(["afconvert", "-h"], capture_output=True, text=True).stdout[:3000])
    print("=" * 70)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for channels in (1, 2):
            wav = tmp / f"probe-{channels}ch.wav"
            write_wave(wav, channels=channels)
            for mode in ("alac", "aac"):
                calls = []
                original = subprocess.run

                def record(args, **kw):
                    calls.append(list(map(str, args)))
                    return original(args, **kw)

                keep = tmp / f"keep-{channels}-{mode}"
                keep.mkdir()
                out = tmp / f"probe-{channels}-{mode}.m4a"
                print(f"### channels={channels} mode={mode}")
                with patch.object(subprocess, "run", record), \
                        patch.object(encoders.tempfile, "TemporaryDirectory", lambda prefix=None: Keep(keep)):
                    try:
                        audio_convert._convert(wav, mode, out)
                    except Exception as exc:
                        print("convert failed:", exc)
                for call in calls:
                    print("$", " ".join(call))
                for f in sorted(keep.iterdir()) + [out]:
                    if f.exists():
                        show(["afinfo", str(f)])
                print("-" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
