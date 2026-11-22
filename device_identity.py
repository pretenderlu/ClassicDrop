"""Work out which iPod model and finish is connected. Read-only, and never required for opening a library."""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path

from device_registry import DEFAULT_DEVICE_REGISTRY, DeviceEvidence, EvidenceAuthority, parse_sysinfo, parse_sysinfo_extended

GENERATIONS = {"6th Gen": "第 6 代", "6.5th Gen": "第 6 代（120 GB）", "7th Gen": "第 7 代"}
FINISHES = {"Silver": "银色", "Black": "黑色"}


class _PassThrough(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_ushort), ("ScsiStatus", ctypes.c_ubyte), ("PathId", ctypes.c_ubyte),
                ("TargetId", ctypes.c_ubyte), ("Lun", ctypes.c_ubyte), ("CdbLength", ctypes.c_ubyte),
                ("SenseInfoLength", ctypes.c_ubyte), ("DataIn", ctypes.c_ubyte), ("DataTransferLength", ctypes.c_ulong),
                ("TimeOutValue", ctypes.c_ulong), ("DataBufferOffset", ctypes.c_size_t), ("SenseInfoOffset", ctypes.c_ulong),
                ("Cdb", ctypes.c_ubyte * 16)]


class _Inquiry(ctypes.Structure):
    _fields_ = [("header", _PassThrough), ("sense", ctypes.c_ubyte * 32), ("data", ctypes.c_ubyte * 255)]


def scsi_sysinfo(drive):
    """The device description an iPod returns over SCSI INQUIRY, as iTunes reads it. No data is written."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                   wintypes.DWORD, wintypes.HANDLE]
    kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    # Pass-through needs a handle opened for reading and writing, even for a command that only reads.
    handle = kernel.CreateFileW("//./" + drive, 0xC0000000, 3, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())

    def page(number):
        packet = _Inquiry()
        head = packet.header
        head.Length, head.CdbLength, head.SenseInfoLength, head.DataIn = ctypes.sizeof(_PassThrough), 6, 32, 1
        head.DataTransferLength, head.TimeOutValue = 255, 5
        head.DataBufferOffset, head.SenseInfoOffset = _Inquiry.data.offset, _Inquiry.sense.offset
        head.Cdb[:6] = (0x12, 1, number, 0, 255, 0)
        returned = wintypes.DWORD()
        if not kernel.DeviceIoControl(handle, 0x4D004, ctypes.byref(packet), ctypes.sizeof(packet), ctypes.byref(packet),
                                      ctypes.sizeof(packet), ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if head.ScsiStatus:
            raise OSError(f"SCSI status {head.ScsiStatus}")
        raw = bytes(packet.data)
        return raw[4:4 + raw[3]]

    try:
        return b"".join(page(number) for number in page(0xC0) if number >= 0xC2)
    finally:
        kernel.CloseHandle(handle)


def identify(root, connected_by_usb):
    """The catalog profile of this iPod, or None when its model cannot be told for certain."""
    try:
        evidence = DeviceEvidence()
        for name, parse in (("SysInfoExtended", parse_sysinfo_extended), ("SysInfo", parse_sysinfo)):
            file = root / "iPod_Control/Device" / name
            if file.is_file() and 0 < file.stat().st_size <= 2 * 1024 * 1024:
                evidence = evidence.merged_with(parse(file.read_bytes()))
        if connected_by_usb and os.name == "nt" and root == Path(root.anchor):
            evidence = evidence.merged_with(parse_sysinfo_extended(
                scsi_sysinfo(root.drive), source="SCSI", authority=EvidenceAuthority.CURRENT_HARDWARE))
        return DEFAULT_DEVICE_REGISTRY.identify(evidence).profile
    except Exception:
        return None


def describe(profile):
    """Short Chinese label such as 第 7 代 · 银色."""
    if not profile:
        return ""
    parts = [GENERATIONS.get(profile.generation, profile.generation), FINISHES.get(profile.finish, profile.finish)]
    return " · ".join(p for p in parts if p)
