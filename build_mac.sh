#!/usr/bin/env bash
# Build ClassicDrop.app (PyInstaller --onedir --windowed), ad-hoc sign it and pack a DMG.
# Usage: bash build_mac.sh [python]   — run from the repo root on macOS.
set -euo pipefail

PYTHON="${1:-python3}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

VERSION="$("$PYTHON" -c "from version import __version__; print(__version__)")"
ARCH="$(uname -m)"; if [ "$ARCH" = "x86_64" ]; then ARCH="x64"; fi   # DMG naming: arm64 or x64

# Bundled FFmpeg comes from the imageio-ffmpeg mac wheel for this arch.
ENCODER="$("$PYTHON" - <<'PY'
from pathlib import Path
import imageio_ffmpeg, platform, sys
arch = "arm64" if platform.machine() == "arm64" else "x86_64"
hits = list((Path(imageio_ffmpeg.__file__).parent / "binaries").glob(f"ffmpeg-macos-{arch}-*"))
if not hits:
    sys.exit("Install the imageio-ffmpeg macOS wheel for this architecture.")
print(hits[0])
PY
)"

# Application icon: derive an .icns from the Windows .ico with Pillow (no committed binary).
ICON="$ROOT/build/ClassicDrop.icns"
mkdir -p "$ROOT/build" "$ROOT/dist"
"$PYTHON" - "$ICON" <<'PY'
import sys
from PIL import Image
icon = Image.open("ClassicDrop.ico")
size = max(icon.info.get("sizes", {(256, 256)}))
icon.size = size
icon.save(sys.argv[1], sizes=[(s, s) for s in (16, 32, 64, 128, 256, 512)])
PY

PYI_ARGS=(
    -m PyInstaller --noconfirm --onedir --windowed --noupx
    --name ClassicDrop --paths "$ROOT/vendor"
    --workpath "$ROOT/build" --specpath "$ROOT/build" --distpath "$ROOT/dist"
    --icon "$ICON"
    --add-data "$ROOT/ClassicDrop.ico:."
    --add-data "$ROOT/LICENSE:licenses"
    --add-data "$ROOT/vendor/COPYING.md:licenses"
    --add-data "$ROOT/vendor/iPodDB/iTunesDB/writer/calcHashAB.wasm.b64:iPodDB/iTunesDB/writer"
    --add-binary "$ENCODER:imageio_ffmpeg/binaries"
    --hidden-import imageio_ffmpeg.binaries
    --collect-binaries wasmtime
)
for dist in mutagen imageio-ffmpeg pycryptodome pillow wasmtime whenever tkinterdnd2 tzdata; do
    PYI_ARGS+=(--copy-metadata "$dist")
done
"$PYTHON" "${PYI_ARGS[@]}" "$ROOT/app.py"

APP="$ROOT/dist/ClassicDrop.app"
PLIST="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier io.github.pretenderlu.classicdrop" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName ClassicDrop" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :NSHighResolutionCapable bool true" "$PLIST" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Set :NSHighResolutionCapable true" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string 12.0" "$PLIST" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Set :LSMinimumSystemVersion 12.0" "$PLIST"

# Ad-hoc signature only; the DMG still needs the Gatekeeper workaround in README.
codesign --force --deep --sign - "$APP"

DMG="$ROOT/dist/ClassicDrop-macOS-$ARCH-v$VERSION.dmg"
STAGING="$ROOT/build/dmg-staging"
rm -rf "$STAGING"
mkdir -p "$STAGING"
cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"
hdiutil create -volname ClassicDrop -srcfolder "$STAGING" -format UDZO -ov "$DMG"
echo "Built $DMG"
