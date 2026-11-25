"""Install a downloaded DMG update on macOS by swapping the running .app bundle.

The app is unsigned, so the only requirements are that the process really runs from a
<name>.app in a writable folder and is not Gatekeeper-app-translocated. Everything here is
importable and unit-testable off the Mac; only stage_update/launch_swap shell out.
"""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile


def running_bundle(exe=None):
    """The .app bundle the process runs from, or None outside a frozen bundle."""
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(exe or sys.executable).resolve()
    if exe.parent.name == "MacOS" and exe.parent.parent.name == "Contents" and \
            exe.parent.parent.parent.suffix == ".app":
        return exe.parent.parent.parent
    return None


def cannot_replace(bundle):
    """Why the bundle cannot be swapped in place, else None."""
    if bundle is None:
        return "程序不在 .app 中运行"
    if "AppTranslocation" in bundle.parts:
        return "程序被 Gatekeeper 移到了隔离位置"
    if not os.access(bundle.parent, os.W_OK):
        return "程序所在文件夹不可写"
    return None


def _run(command, report):
    p = subprocess.run(command, capture_output=True, text=True, timeout=300)
    if p.returncode:
        raise OSError(f"{' '.join(map(str, command))} 失败（退出码 {p.returncode}）："
                      f"{(p.stderr or p.stdout).strip()[-300:]}")
    return p


def bundle_version(app):
    with open(app / "Contents/Info.plist", "rb") as stream:
        return plistlib.load(stream).get("CFBundleShortVersionString", "")


def stage_update(dmg, bundle, version, report=lambda _: None):
    """Mount the DMG, copy the new .app next to the target bundle and verify it.

    Returns the staged bundle path; raises a Chinese-message error and detaches on failure.
    """
    work = Path(tempfile.mkdtemp(prefix="classicdrop-update-"))
    mount = work / "mnt"
    mount.mkdir()
    staged = bundle.parent / ".ClassicDrop-update.app"
    try:
        report("正在装载更新包…")
        _run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen",
              "-mountpoint", str(mount), str(dmg)], report)
        try:
            shutil.rmtree(staged, ignore_errors=True)
            report("正在准备新版本…")
            _run(["ditto", str(mount / "ClassicDrop.app"), str(staged)], report)
        finally:
            detach = subprocess.run(["hdiutil", "detach", str(mount)], capture_output=True)
            if detach.returncode:
                subprocess.run(["hdiutil", "detach", "-force", str(mount)], capture_output=True)
        _run(["codesign", "--verify", "--deep", "--strict", str(staged)], report)
        got = bundle_version(staged)
        if got != version.lstrip("v"):
            raise ValueError(f"更新包版本不匹配：包内为 {got or '未知'}，应为 {version.lstrip('v')}。")
    except Exception:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return staged


SWAP_SCRIPT = """#!/bin/sh
# Waits for the app to exit, then swaps in the staged update. Args: pid target staged log [tries].
exec >>"$4" 2>&1
i=0
tries=${5:-600}
while [ $i -lt "$tries" ] && kill -0 "$1" 2>/dev/null; do
    i=$((i + 1))
    sleep 0.1 2>/dev/null || sleep 1
done
if kill -0 "$1" 2>/dev/null; then
    rm -rf "$3"
    echo "app still running; update skipped for $2"
    exit 1
fi
old="$2.old.$$"
if mv "$2" "$old" && mv "$3" "$2"; then
    rm -rf "$old"
    xattr -dr com.apple.quarantine "$2" 2>/dev/null
    open "$2"
else
    if [ -d "$old" ]; then
        mv "$old" "$2"
    fi
    echo "update swap failed for $2"
fi
"""


def launch_swap(bundle, staged, log, pid=None):
    """Write the swap script to a temp dir and launch it detached, waiting on this pid."""
    script = Path(tempfile.mkdtemp(prefix="classicdrop-swap-")) / "swap.sh"
    script.write_text(SWAP_SCRIPT, encoding="utf8")
    script.chmod(0o755)
    return subprocess.Popen(["/bin/sh", str(script), str(pid or os.getpid()), str(bundle),
                             str(staged), str(log)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def prepare(dmg, version, report=lambda _: None):
    """Decide between automatic swap and manual drag; returns a plan tuple.

    ("swap", bundle, staged) when the running bundle can be replaced in place,
    ("manual", dmg) otherwise — the caller opens the DMG for a manual drag install.
    """
    bundle = running_bundle()
    if cannot_replace(bundle) is not None:
        return "manual", dmg
    return "swap", bundle, stage_update(dmg, bundle, version, report)
