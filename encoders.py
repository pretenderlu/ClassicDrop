"""External encoders: the bundled qaac/refalac tools and Apple's CoreAudioToolbox components.

refalac needs nothing else. qaac uses Apple's DLLs, which are never distributed: the user
can have them fetched from Apple's own iTunes installer into QTFILES, or an installed
iTunes provides them. FFmpeg stays the fallback encoder and the decoder for everything.
"""

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from threading import Thread
import wave
from urllib.request import Request, urlopen

from imageio_ffmpeg import get_ffmpeg_exe

import system
from version import __version__

TOOLS = (Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).parent / "vendor") / "qaac"
QAAC = TOOLS / "qaac64.exe"
REFALAC = TOOLS / "refalac64.exe"
QTFILES = system.DATA / "QTfiles64"

ITUNES_URL = "https://www.apple.com/itunes/download/win64"
# Everything qaac needs, found by its PE imports; icudt*.dll is matched by glob as its version changes.
APPLE_DLLS = ("ASL.dll", "CoreAudioToolbox.dll", "CoreFoundation.dll", "objc.dll", "libdispatch.dll",
              "libicuin.dll", "libicuuc.dll", "msvcp140.dll", "vcruntime140.dll")

_probe_cache = None


def _run(args, error, timeout=900):
    proc = subprocess.run([str(a) for a in args], capture_output=True, timeout=timeout, **system.NO_WINDOW)
    if proc.returncode:
        raise ValueError(error + f"（退出码 {proc.returncode}）")


def _run_check(folder):
    """'CoreAudioToolbox 7.10.9.0' when qaac finds Apple's DLLs under folder (or iTunes), else None."""
    if not QAAC.is_file():
        return None
    env = dict(os.environ)
    if folder:
        env["PATH"] = str(folder) + os.pathsep + env.get("PATH", "")
    try:
        proc = subprocess.run([str(QAAC), "--check"], capture_output=True, timeout=15,
                              **system.NO_WINDOW, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"CoreAudioToolbox\s+[\d.]+", (proc.stdout + proc.stderr).decode("utf8", "replace"))
    return match[0] if proc.returncode == 0 and match else None


def _detect():
    """(version, source): source is 'qtfiles' for our folder, 'itunes' for an installed iTunes."""
    if QTFILES.is_dir():
        version = _run_check(QTFILES)
        if version:
            return version, "qtfiles"
    version = _run_check(None)
    return (version, "itunes") if version else (None, None)


def _probe():
    global _probe_cache
    if _probe_cache is None:
        _probe_cache = _detect()
    return _probe_cache


def apple_version():
    """e.g. 'CoreAudioToolbox 7.10.9.0' when Apple's encoder is usable, else None."""
    return _probe()[0]


def apple_source():
    """'qtfiles', 'itunes' or None; only 'qtfiles' is ours to remove."""
    return _probe()[1]


def refresh():
    global _probe_cache, _afconvert_cache
    _probe_cache = None
    _afconvert_cache = None


AFCONVERT = "/usr/bin/afconvert"
_afconvert_cache = None  # not yet probed; "" means unusable


def afconvert():
    """Path to macOS's own Apple encoder when it can write AAC m4a, else None."""
    global _afconvert_cache
    if _afconvert_cache is not None:
        return _afconvert_cache or None
    _afconvert_cache = ""
    if not system.WINDOWS and Path(AFCONVERT).is_file():
        # Help-text wording differs between macOS versions, so probe by encoding a
        # moment of silence to AAC; success plus real output means usable.
        try:
            with tempfile.TemporaryDirectory(prefix="classicdrop-") as folder:
                src = Path(folder) / "probe.wav"
                out = Path(folder) / "probe.m4a"
                with wave.open(str(src), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(44100)
                    wav.writeframes(b"\0\0" * 4410)
                probe = subprocess.run([AFCONVERT, "-f", "m4af", "-d", "aac", "-b", "128000",
                                        "-c", "1", "-l", "Mono", "-s", "2", "-q", "127", str(src), str(out)],
                                       capture_output=True, timeout=15, **system.NO_WINDOW)
                if probe.returncode == 0 and out.is_file() and out.stat().st_size:
                    _afconvert_cache = AFCONVERT
        except (OSError, subprocess.TimeoutExpired):
            pass
    return _afconvert_cache or None


def afconvert_encode(tool_args, decode_args, pcm_codec, destination):
    """FFmpeg decodes to a temp CAF file; afconvert encodes it to m4a."""
    with tempfile.TemporaryDirectory(prefix="classicdrop-") as folder:
        caf = Path(folder) / "decode.caf"
        decoder = subprocess.run([get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error",
                                  "-xerror", *decode_args, "-c:a", pcm_codec, "-f", "caf", str(caf)],
                                 capture_output=True, timeout=900, **system.NO_WINDOW)
        if decoder.returncode:
            detail = decoder.stderr.decode("utf8", "replace").strip()[-400:]
            raise ValueError(f"FFmpeg 解码失败（退出码 {decoder.returncode}）。{detail}")
        encoder = subprocess.run([AFCONVERT, *tool_args, str(caf), str(destination)],
                                 capture_output=True, timeout=900, **system.NO_WINDOW)
        if encoder.returncode:
            detail = (encoder.stderr or encoder.stdout).decode("utf8", "replace").strip()[-400:]
            raise ValueError(f"afconvert 编码失败（退出码 {encoder.returncode}）。{detail}")


def qaac():
    """(exe, child env) when Apple's encoder is usable, else None."""
    if not apple_version():
        return None
    env = dict(os.environ)
    if apple_source() == "qtfiles":
        env["PATH"] = str(QTFILES) + os.pathsep + env.get("PATH", "")
    return str(QAAC), env


def refalac():
    """(exe, child env) when the bundled refalac is present, else None."""
    return (str(REFALAC), dict(os.environ)) if REFALAC.is_file() else None


def pipe_encode(exe, env, tool_args, decode_args, raw_format, destination, timeout=900):
    """FFmpeg decodes to raw PCM on stdout; the external encoder reads it on stdin.

    Raises ValueError when either side fails; the caller deletes the partial file.
    """
    decoder = subprocess.Popen([get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
                                *decode_args, "-f", raw_format, "pipe:1"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               **system.NO_WINDOW)
    encoder = None
    decoder_tail = bytearray()
    def drain_decoder():
        try:
            while chunk := decoder.stderr.read(4096):
                decoder_tail.extend(chunk)
                if len(decoder_tail) > 65536:
                    del decoder_tail[:-32768]
        finally:
            decoder.stderr.close()
    drainer = Thread(target=drain_decoder, daemon=True)
    drainer.start()
    try:
        encoder = subprocess.Popen([exe, *tool_args, "-", "-o", str(destination)],
                                   stdin=decoder.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   **system.NO_WINDOW, env=env)
        decoder.stdout.close()
        try:
            encoder_error = encoder.communicate(timeout=timeout)[1]
            decoder.wait(timeout=30)
        except subprocess.TimeoutExpired as exc:
            for proc in (encoder, decoder):
                proc.kill()
            encoder.wait()
            decoder.wait()
            raise ValueError(f"{Path(exe).stem} 编码超时。") from exc
    except BaseException:
        for proc in (encoder, decoder):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()
        if decoder.stdout and not decoder.stdout.closed:
            decoder.stdout.close()
        raise
    finally:
        drainer.join(timeout=5)
    if decoder.returncode or encoder.returncode:
        detail = " ".join(t for t in (bytes(decoder_tail).decode("utf8", "replace").strip()[-400:],
                                      encoder_error.decode("utf8", "replace").strip()[-400:]) if t)
        raise ValueError(f"{Path(exe).stem} 编码失败（退出码 {decoder.returncode}/{encoder.returncode}）。{detail}")


def _download(url, destination, report):
    request = Request(url, headers={"User-Agent": f"ClassicDrop/{__version__}"})
    with urlopen(request, timeout=30) as reply:
        total = int(reply.headers.get("Content-Length", 0) or 0)
        done = shown = 0
        with destination.open("wb") as out:
            while chunk := reply.read(1 << 20):
                out.write(chunk)
                done += len(chunk)
                mark = (done * 100 // total) if total else (done >> 20)
                if mark != shown:
                    shown = mark
                    report(f"正在下载 iTunes 安装包 {done / 2**20:.0f} MB" + (f" · {mark}%" if total else ""))
    if total and done != total:
        raise ValueError("iTunes 安装包下载不完整，请重试。")


def _verify_signature(installer):
    """The iTunes installer must carry Apple's valid Authenticode signature."""
    quoted = str(installer).replace("'", "''")
    proc = system.powershell(f"$s = Get-AuthenticodeSignature -LiteralPath '{quoted}'; $s.Status; $s.SignerCertificate.Subject")
    lines = proc.stdout.decode("utf8", "replace").splitlines()
    if not lines or lines[0].strip() != "Valid" or "O=Apple Inc." not in "".join(lines[1:]):
        raise ValueError("下载的 iTunes 安装包没有通过 Apple 签名验证，已放弃。")


def _slice_cab(installer, cab):
    """Write the MSCF payload embedded in the installer to a plain .cab file."""
    with installer.open("rb") as source, cab.open("wb") as out:
        tail = b""
        while True:
            block = source.read(8 << 20)
            if not block:
                raise ValueError("iTunes 安装包中没有找到内嵌的 CAB 数据。")
            chunk = tail + block
            found = chunk.find(b"MSCF")
            if found >= 0:
                out.write(chunk[found:])
                break
            tail = chunk[-3:]
        shutil.copyfileobj(source, out, 1 << 20)


def install_apple(report):
    """Fetch Apple's encoder DLLs from Apple's iTunes installer into QTFILES. Returns the version."""
    root = QTFILES.parent
    root.mkdir(parents=True, exist_ok=True)
    staging = root / "QTfiles64.new"
    temp = Path(tempfile.mkdtemp(prefix="itunes-", dir=root))
    try:
        installer = temp / "iTunes64Setup.exe"
        _download(ITUNES_URL, installer, report)
        _verify_signature(installer)
        report("正在提取 Apple 编码器组件…")
        # The installer is a self-extracting cabinet; the embedded CAB is handled without elevation.
        cab = temp / "setup.cab"
        _slice_cab(installer, cab)
        extracted = temp / "ext"
        extracted.mkdir()
        extract = Path(os.environ["SystemRoot"]) / "System32/extrac32.exe"
        _run([extract, "/Y", "/L", extracted, cab, "iTunes64.msi"], "无法解压 iTunes 安装包。")
        msi = next(extracted.rglob("iTunes64.msi"), None)
        if not msi:
            raise ValueError("iTunes 安装包中没有找到 iTunes64.msi。")
        admin = temp / "admin"
        _run(["msiexec", "/a", msi, "/qn", f"TARGETDIR={admin}"], "无法提取 iTunes 组件。")
        folder = next((p.parent for p in admin.rglob("CoreAudioToolbox.dll")), None)
        if not folder:
            raise ValueError("提取结果中没有 CoreAudioToolbox.dll。")
        missing = [name for name in APPLE_DLLS if not (folder / name).is_file()]
        if missing:
            raise ValueError("提取结果缺少组件：" + "、".join(missing))
        icudt = list(folder.glob("icudt*.dll"))
        if len(icudt) != 1:
            raise ValueError("未能确定 icudt 组件的版本。")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        for file in [folder / name for name in APPLE_DLLS] + icudt:
            shutil.copy2(file, staging / file.name)
        version = _run_check(staging)
        if not version:
            raise ValueError("提取的组件未能通过编码器自检。")
        shutil.rmtree(QTFILES, ignore_errors=True)
        staging.rename(QTFILES)
        refresh()
        return version
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(temp, ignore_errors=True)


def remove_apple():
    """Delete our copy of Apple's components (an installed iTunes is left alone)."""
    shutil.rmtree(QTFILES, ignore_errors=True)
    refresh()
