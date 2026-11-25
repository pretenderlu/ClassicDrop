"""Process vs host CPU architecture.

The x64 build runs under emulation on Windows 11 ARM64, where platform.machine()
on Python 3.12 reports the host (ARM64), not the process arch. wasmtime 38 picks
its DLL directory from platform.machine(), so it must see the process arch instead.
"""
import ctypes
from ctypes import wintypes
import platform
import sys
import sysconfig

_PROCESS_NAMES = {"win-amd64": "x64", "win-arm64": "ARM64", "win32": "x86"}
_MACHINES = {0x8664: "x64", 0xAA64: "ARM64", 0x014C: "x86"}
_WASMTIME_NAMES = {"x64": "AMD64", "ARM64": "ARM64", "x86": "x86"}


def _from_machine(name):
    name = name.upper()
    if name in ("AMD64", "X64"):
        return "x64"
    if name.startswith("ARM") or name == "AARCH64":
        return "ARM64"
    return "x86"


def process_arch():
    """Architecture the running interpreter was built for."""
    return _PROCESS_NAMES.get(sysconfig.get_platform()) or _from_machine(platform.machine())


def host_arch():
    """Architecture of the machine itself. x64-on-ARM64 is not wow64, so the process
    machine is UNKNOWN there but the native machine is still ARM64."""
    try:
        process_machine, native_machine = wintypes.USHORT(0), wintypes.USHORT(0)
        kernel = ctypes.windll.kernel32
        if kernel.IsWow64Process2(kernel.GetCurrentProcess(),
                                  ctypes.byref(process_machine), ctypes.byref(native_machine)):
            arch = _MACHINES.get(native_machine.value)
            if arch:
                return arch
    except (AttributeError, OSError):
        pass
    return _from_machine(platform.machine())


def describe():
    process, host = process_arch(), host_arch()
    return process if process == host else f"{process}（电脑为 {host}，模拟运行）"


def import_wasmtime():
    """Import wasmtime with platform.machine() reporting the process architecture."""
    if "wasmtime" in sys.modules:
        return
    original = platform.machine
    platform.machine = lambda: _WASMTIME_NAMES.get(process_arch(), "AMD64")
    try:
        import wasmtime  # noqa: F401
    finally:
        platform.machine = original
