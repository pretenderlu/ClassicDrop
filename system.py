"""Operating-system specifics. Every platform call lives here so call sites stay neutral."""
import ctypes
from contextlib import contextmanager
from ctypes import wintypes
import os
from pathlib import Path
import platform
import plistlib
import re
import stat
import subprocess
import sys

MAC = sys.platform == "darwin"
WINDOWS = os.name == "nt"
MOD = "Command" if MAC else "Control"

DATA = Path(os.environ["LOCALAPPDATA"]) / "ClassicDrop" if WINDOWS else \
    Path.home() / "Library/Application Support/ClassicDrop"
LOGS = DATA / "Logs" if WINDOWS else Path.home() / "Library/Logs/ClassicDrop"
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if WINDOWS else {}

# Files and folders macOS writes onto volumes; the iPod does not understand them.
JUNK_DIRS = {".Spotlight-V100", ".fseventsd", ".Trashes", ".TemporaryItems"}


def os_label():
    return f"macOS {platform.mac_ver()[0]}" if MAC else f"Windows {platform.release()}（{platform.version()}）"


def powershell(command, timeout=120):
    ps = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    return subprocess.run([str(ps), "-NoProfile", "-NonInteractive", "-Command", command],
                          capture_output=True, timeout=timeout, **NO_WINDOW)


def junk_file(name):
    return name.startswith("._") or name == ".DS_Store"


def open_folder(path):
    if WINDOWS:
        os.startfile(path)
    else:
        subprocess.run(["open", str(path)], check=False)


def volumes():
    """Drive letters on Windows, real mount points under /Volumes on macOS."""
    if WINDOWS:
        return [Path(d) for d in os.listdrives() if ctypes.windll.kernel32.GetDriveTypeW(d) in (2, 3)]
    try:
        return [p for p in Path("/Volumes").iterdir()
                if p.is_dir() and os.path.ismount(p) and os.path.realpath(p) != "/"]
    except OSError:
        return []


def is_volume_root(root):
    return root == Path(root.anchor) if WINDOWS else os.path.ismount(root)


def is_readonly(stat_result):
    if WINDOWS:
        return bool(stat_result.st_file_attributes & stat.FILE_ATTRIBUTE_READONLY)
    return not stat_result.st_mode & stat.S_IWUSR


def _windows_disk_ids(drive):
    """Serial number and USB ancestors of the drive."""
    if not re.fullmatch(r"[A-Z]:", drive, re.I):
        return ()
    query = f"""$v = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='{drive}'"\n""" + r"""
$p = Get-CimAssociatedInstance -InputObject $v -Association Win32_LogicalDiskToPartition
if (@($p).Count -ne 1 -or !$p) { return }
$d = Get-CimAssociatedInstance -InputObject $p -Association Win32_DiskDriveToDiskPartition
if (@($d).Count -ne 1 -or !$d) { return }
$d.SerialNumber
$d.PNPDeviceID
$node = $d.PNPDeviceID
if ($node -isnot [string]) { return }
for ($hop = 0; $node -and $hop -lt 12; $hop++) {
    if ($node -match '^USB\\VID_([0-9A-F]{4})&PID_[0-9A-F]{4}\\([^\\]+)$') {
        if ($Matches[1] -eq '05AC') { $node }
        break
    }
    $node = (Get-PnpDeviceProperty -InstanceId $node -KeyName 'DEVPKEY_Device_Parent' -ErrorAction SilentlyContinue).Data
}
"""
    try:
        output = powershell(query, timeout=12).stdout
    except subprocess.TimeoutExpired as exc:
        # The disk identifiers may already have arrived while Windows was resolving a parent.
        output = exc.stdout or b""
    except OSError:
        return ()
    return tuple(line.strip() for line in output.decode("ascii", "ignore").splitlines() if line.strip())


def _bsd_names(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "BSD Name":
                yield value
            else:
                yield from _bsd_names(value)
    elif isinstance(node, list):
        for item in node:
            yield from _bsd_names(item)


def _mac_usb_ids():
    """IOUSBHostDevice (then IOUSBDevice) registry entries as parsed plists."""
    for device_class in ("IOUSBHostDevice", "IOUSBDevice"):
        try:
            output = subprocess.run(["ioreg", "-a", "-r", "-c", device_class, "-l"],
                                    capture_output=True, timeout=12).stdout
            devices = plistlib.loads(output)
        except Exception:
            continue
        if devices:
            return devices
    return ()


def _mac_disk_ids(root):
    """('DISK:diskN', 'USB\\VID_05AC&PID_xxxx\\\\<serial>') so the Windows parsers work unchanged."""
    try:
        info = subprocess.run(["diskutil", "info", "-plist", str(root)],
                              capture_output=True, timeout=12).stdout
        whole = plistlib.loads(info).get("ParentWholeDisk")
    except Exception:
        return ()
    if not whole:
        return ()
    for device in _mac_usb_ids():
        if device.get("idVendor") != 1452 or whole not in set(_bsd_names(device)):
            continue
        serial = device.get("USB Serial Number") or device.get("kUSBSerialNumberString")
        product = device.get("idProduct")
        if serial and product is not None:
            return (f"DISK:{whole}", f"USB\\VID_05AC&PID_{product:04X}\\{serial}")
    return (f"DISK:{whole}",)


def volume_ids(root):
    """Hardware identifiers of the disk holding root; empty when it cannot be told."""
    if not is_volume_root(root):
        return ()
    return _windows_disk_ids(root.drive) if WINDOWS else _mac_disk_ids(root)


def _request_usb_eject(instance):
    cfg = ctypes.WinDLL("cfgmgr32")
    locate = cfg.CM_Locate_DevNodeW
    locate.argtypes = (ctypes.POINTER(wintypes.DWORD), wintypes.LPCWSTR, wintypes.ULONG)
    locate.restype = wintypes.ULONG
    node = wintypes.DWORD()
    if locate(ctypes.byref(node), instance, 0):
        raise OSError("Windows 已找不到这台 iPod，请刷新设备。")
    request = cfg.CM_Request_Device_EjectW
    request.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.ULONG, wintypes.ULONG)
    request.restype = wintypes.ULONG
    veto = wintypes.DWORD()
    name = ctypes.create_unicode_buffer(260)
    status = request(node, ctypes.byref(veto), name, len(name), 0)
    if status:
        detail = f"（{name.value}）" if name.value else f"（Windows 代码 0x{status:02X}）"
        raise OSError("Windows 未能安全弹出 iPod" + detail + "。请保持连接，关闭占用设备的窗口或程序后重试。")


def eject_device(instance, disk_ids):
    if WINDOWS:
        _request_usb_eject(instance)
        return
    whole = next((i.split(":", 1)[1] for i in disk_ids if i.startswith("DISK:")), None)
    if not whole:
        raise OSError("无法确定这台 iPod 的磁盘，请刷新设备。")
    proc = subprocess.run(["diskutil", "eject", whole], capture_output=True, text=True, timeout=30)
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).strip()
        raise OSError("macOS 未能安全推出 iPod" + (f"（{detail}）" if detail else "")
                      + "。请保持连接，关闭占用设备的窗口或程序后重试。")


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


@contextmanager
def lock_file(lock_path, db_path):
    """Exclusive hold on iTunesLock plus a read reference on the DB; yields release_database()."""
    if WINDOWS:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.CreateFileW(str(lock_path), 0xC0000000, 0, None, 4, 0x80, None)
        if handle == wintypes.HANDLE(-1).value:
            detail = ctypes.WinError(ctypes.get_last_error())
            raise OSError(f"无法锁定 iPod。请关闭其他传歌软件，并检查磁盘写入权限。\n{detail}")
        db_handle = kernel.CreateFileW(str(db_path), 0x80000000, 5, None, 3, 0x80, None)
        invalid = wintypes.HANDLE(-1).value

        def release_database():
            nonlocal db_handle
            if db_handle != invalid:
                kernel.CloseHandle(db_handle)
                db_handle = invalid
        try:
            if db_handle == invalid:
                raise OSError("曲库正在被其他软件写入，或设备已断开。")
            yield release_database
        finally:
            release_database()
            kernel.CloseHandle(handle)
        return
    import fcntl
    try:
        handle = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        raise OSError(f"无法锁定 iPod。请关闭其他传歌软件，并检查磁盘写入权限。\n{exc}") from exc
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(handle)
        raise OSError(f"无法锁定 iPod。请关闭其他传歌软件，并检查磁盘写入权限。\n{exc}") from exc
    try:
        db_handle = open(db_path, "rb")
    except OSError as exc:
        os.close(handle)
        raise OSError("曲库正在被其他软件写入，或设备已断开。") from exc
    released = False

    def release_database():
        nonlocal released
        if not released:
            db_handle.close()
            released = True
    try:
        yield release_database
    finally:
        release_database()
        os.close(handle)
