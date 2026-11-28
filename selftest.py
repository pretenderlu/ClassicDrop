"""`ClassicDrop.exe --self-test out.json`: architecture and bundled-runtime smoke test.

Writes a UTF-8 JSON report and returns 0 iff every check passed. Used by CI to prove the
x64 build works under Windows 11 ARM64 emulation; makes no window and touches no settings.
"""
import json
from pathlib import Path
import platform
import tempfile

from version import __version__
import cpuarch
import system


def self_test(output):
    checks = {}

    def check(name, run):
        try:
            detail = run() or ""
            checks[name] = {"ok": True, "detail": str(detail)}
        except Exception as exc:  # noqa: BLE001 — the report must capture whatever broke
            checks[name] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    def signatures():
        from iPodDB.iTunesDB.writer.signature import sign_hash58, sign_hashab, verify_hash58, verify_hashab
        from iPodDB.library import WriteChecksum, WriteTarget
        from iPodDB.library.creation import create_empty_library
        guid = bytes.fromhex("0011223344556677")
        target = WriteTarget(checksum=WriteChecksum.HASH58, firewire_guid=guid, database_version=0x30)
        data = create_empty_library("self-test", target, supports_podcasts=True).prepared.itunes
        if not verify_hashab(sign_hashab(data, guid), guid):
            raise ValueError("hashab 签名校验失败")
        if not verify_hash58(sign_hash58(data, guid), guid):
            raise ValueError("hash58 签名校验失败")
        from wasmtime import _ffi
        return _ffi.filename

    check("wasmtime", signatures)

    def ffmpeg_check():
        import subprocess
        from imageio_ffmpeg import get_ffmpeg_exe
        out = subprocess.run([get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-version"],
                             capture_output=True, text=True, timeout=30, **system.NO_WINDOW)
        if out.returncode or "ffmpeg version" not in out.stdout:
            raise ValueError(out.stderr[-200:] or "ffmpeg -version 失败")
        return out.stdout.splitlines()[0]

    check("ffmpeg", ffmpeg_check)

    def encoder_check():
        import math
        import struct
        import subprocess
        import wave
        import encoders
        mac = system.MAC
        tool = encoders.afconvert() if mac else encoders.refalac()
        if not tool:
            raise ValueError("缺少 afconvert" if mac else "缺少 refalac64.exe")
        with tempfile.TemporaryDirectory(prefix="classicdrop-selftest") as tmp:
            tmp = Path(tmp)
            source = tmp / "tone.wav"
            with wave.open(str(source), "wb") as wav:
                wav.setnchannels(2)
                wav.setsampwidth(2)
                wav.setframerate(44100)
                frames = b"".join(
                    struct.pack("<hh", sample, sample)
                    for n in range(44100)
                    for sample in (int(12000 * math.sin(2 * math.pi * 440 * n / 44100)),))
                wav.writeframes(frames)
            destination = tmp / "tone.m4a"
            if mac:
                encoders.afconvert_encode(
                    ["-f", "m4af", "-d", "aac", "-b", "256000", "-c", "2", "-l", "Stereo", "-s", "2", "-q", "127"],
                    ["-i", str(source)], "pcm_f32le", destination)
                if not destination.is_file():
                    raise ValueError("afconvert 没有产出 m4a")
            else:
                exe, env = tool
                encoded = subprocess.run([exe, str(source), "-o", str(destination), "--silent"],
                                         capture_output=True, env=env, timeout=60, **system.NO_WINDOW)
                if encoded.returncode or not destination.is_file():
                    raise ValueError(encoded.stderr.decode("utf8", "replace")[-200:] or "refalac 编码失败")
            from imageio_ffmpeg import get_ffmpeg_exe
            decoded = subprocess.run([get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error",
                              "-i", str(destination), "-f", "null", "-"],
                             capture_output=True, timeout=30, **system.NO_WINDOW)
            if decoded.returncode:
                raise ValueError("ffmpeg 无法解码编码器输出")
        return tool if mac else tool[0]

    check("afconvert" if system.MAC else "refalac", encoder_check)

    def tkdnd_check():
        from tkinterdnd2 import TkinterDnD
        window = TkinterDnD.Tk()
        window.update()
        window.destroy()
        return "ok"

    check("tkdnd", tkdnd_check)

    report = {
        "version": __version__,
        "process_arch": cpuarch.process_arch(),
        "host_arch": cpuarch.host_arch(),
        "machine": platform.machine(),
        "wasmtime_dll": str(checks["wasmtime"]["detail"]) if checks["wasmtime"]["ok"] else "",
        "checks": checks,
        "ok": all(item["ok"] for item in checks.values()),
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1
