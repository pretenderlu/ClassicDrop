"""Work out which iPod model and finish is connected. Read-only, and never required for opening a library."""

from dataclasses import dataclass
import plistlib
import re
from types import SimpleNamespace
from xml.parsers.expat import ExpatError

import shuffle_db
import system
from device_registry import (DEFAULT_DEVICE_REGISTRY, DatabaseChecksum, DeviceEvidence, DeviceIdentifier, EvidenceAuthority,
                             UsbIdentifier, parse_sysinfo, parse_sysinfo_extended)

GENERATIONS = {
    "1st Gen": "第 1 代", "2nd Gen": "第 2 代", "3rd Gen": "第 3 代",
    "4th Gen (mono)": "第 4 代", "4th Gen (photo)": "第 4 代（Photo）", "4th Gen (color)": "第 4 代（彩屏）",
    "4th Gen": "第 4 代", "5th Gen": "video 第 5 代", "5.5th Gen": "video 第 5.5 代",
    "6th Gen": "第 6 代", "6.5th Gen": "第 6 代（120 GB）", "7th Gen": "第 7 代",
}
FINISHES = {
    "Silver": "银色", "Black": "黑色", "White": "白色", "Graphite": "深空灰",
    "Space Gray": "深空灰", "Slate": "炭黑", "Gold": "金色", "U2": "U2 特别版",
    "Blue": "蓝色", "Green": "绿色", "Red": "红色", "Pink": "粉色",
    "Purple": "紫色", "Orange": "橙色", "Yellow": "黄色",
}
FAMILIES = {
    "iPod": "iPod",
    "iPod Classic": "iPod classic",
    "iPod Mini": "iPod mini",
    "iPod Nano": "iPod nano",
    "iPod Shuffle": "iPod shuffle",
}

# Hardware models whose stock firmware does not decode Apple Lossless (ALAC).
_NO_ALAC = {
    ("iPod", "1st Gen"), ("iPod", "2nd Gen"), ("iPod", "3rd Gen"),
    ("iPod Mini", "1st Gen"),
    ("iPod Shuffle", "1st Gen"), ("iPod Shuffle", "2nd Gen"),
}

_SUPPORTED_CHECKSUMS = {
    DatabaseChecksum.NONE,
    DatabaseChecksum.HASH58,
    DatabaseChecksum.HASH72,
    DatabaseChecksum.HASHAB,
}


def _shuffle_capabilities(sd_version):
    db = SimpleNamespace(
        checksum=DatabaseChecksum.NONE,
        supports_compressed_database=False,
        uses_sqlite_database=False,
        sqlite_checksum=DatabaseChecksum.NONE,
        sd_version=sd_version,
    )
    art = SimpleNamespace(
        cover_formats=(),
        artwork_root_value=None,
        supports_sparse_artwork=False,
    )
    return SimpleNamespace(database=db, artwork=art)


@dataclass(frozen=True)
class Player:
    """What could be told about an iPod. A blank field means its candidates differ in that respect."""

    family: str
    generation: str
    finish: str
    capabilities: object
    model_number: str = ""

    @property
    def shape(self):
        """Which outline to draw for it."""
        if self.family == "iPod Mini":
            return "mini"
        if self.family == "iPod Shuffle":
            return {"1st Gen": "shuffle1", "3rd Gen": "shuffle3"}.get(self.generation, "shuffle2")
        if self.family == "iPod Nano":
            return {
                "1st Gen": "nano1", "2nd Gen": "nano1",
                "3rd Gen": "nano3",
                "4th Gen": "nano4", "5th Gen": "nano4",
                "6th Gen": "nano6",
                "7th Gen": "nano7",
            }.get(self.generation, "nano4")
        return "classic"

    @property
    def proven(self):
        """Only the classic has been used with this program on real hardware."""
        return self.family == "iPod Classic"

    @property
    def supports_alac(self):
        """Whether stock firmware on this model decodes Apple Lossless (ALAC)."""
        if self.family == "iPod Shuffle":
            # iTunesSD v2 (bdhs) is exclusive to shuffle 3rd/4th gen, both of which decode ALAC;
            # v1 belongs to shuffle 1st/2nd gen, neither of which decodes ALAC.
            return getattr(self.capabilities.database, "sd_version", 1) >= 2
        return (self.family, self.generation) not in _NO_ALAC

    @property
    def supports_covers(self):
        """Whether this model has a colour screen and native ArtworkDB cover thumbnails."""
        return bool(self.capabilities.artwork.cover_formats)

    @property
    def name(self):
        """What to call it when its library carries no name."""
        return FAMILIES.get(self.family, self.family)


def read_device_files(root, resolver=None):
    """Read SysInfoExtended and SysInfo once (up to 2 MB each). resolver validates paths when provided."""
    files = {}
    for name in ("SysInfoExtended", "SysInfo"):
        rel = "iPod_Control/Device/" + name
        file = resolver(root, rel) if resolver else root / rel
        if file.is_file() and 0 < file.stat().st_size <= 2 * 1024 * 1024:
            files[name] = file.read_bytes()
    return files


def device_secrets(files):
    """Serial numbers found in the already-read SysInfoExtended/SysInfo bytes, for log scrubbing."""
    values = []
    extended = files.get("SysInfoExtended")
    if extended:
        try:
            data = extended if extended.startswith(b"bplist") else extended.strip(b"\x00\r\n ")
            parsed = plistlib.loads(data)
            if isinstance(parsed, dict):
                values.extend(str(v) for k, v in parsed.items() if "serial" in str(k).lower())
        except (ValueError, TypeError, ExpatError, plistlib.InvalidFileException):
            pass
    plain = files.get("SysInfo")
    if plain:
        for line in plain.decode("utf-8", "replace").splitlines():
            key, sep, value = line.partition(":")
            if sep and "serial" in key.strip().lower():
                values.append(value.strip())
    return tuple(v for v in values if v)


def _device_files(root, files=None):
    raw = read_device_files(root) if files is None else files
    evidence = DeviceEvidence()
    for name, parse in (("SysInfoExtended", parse_sysinfo_extended), ("SysInfo", parse_sysinfo)):
        data = raw.get(name)
        if data:
            evidence = evidence.merged_with(parse(data))
    return evidence


def _supported(capabilities):
    """Every non-iOS mass-storage iPod family in the catalog is supported."""
    return capabilities.database.checksum in _SUPPORTED_CHECKSUMS


def _compat_key(profile):
    db = profile.capabilities.database
    return (
        profile.capabilities.artwork,
        db.checksum,
        db.supports_compressed_database,
        db.uses_sqlite_database,
        db.sqlite_checksum,
        (profile.family, profile.generation) not in _NO_ALAC,
    )


def _resolve(evidence):
    result = DEFAULT_DEVICE_REGISTRY.identify(evidence)
    exact = result.profile
    if exact:
        if not _supported(exact.capabilities):
            return None
        return Player(exact.family, exact.generation, exact.finish, exact.capabilities, exact.model_number)
    # The USB product number names the family without the finish. That is enough when every candidate
    # stores its library and its covers the same way.
    candidates = result.candidates
    if not candidates or len({c.family for c in candidates}) != 1:
        return None
    first = candidates[0]
    if not _supported(first.capabilities) or any(_compat_key(c) != _compat_key(first) for c in candidates):
        return None
    generations = {c.generation for c in candidates}
    return Player(first.family, generations.pop() if len(generations) == 1 else "", "", first.capabilities)


def identify(root, usb=None, *, files=None):
    """What this iPod is, or None when that cannot be told or it is an unsupported device.

    usb is the Windows/macOS instance name of the Apple USB device the volume belongs to, or any true value for
    a USB connection whose instance is unknown.
    """
    try:
        shuf = shuffle_db.detect_shuffle(root, usb)
        if shuf is not None:
            generation, sd_version = shuf
            return Player("iPod Shuffle", generation, "", _shuffle_capabilities(sd_version))
        hardware = DeviceEvidence()
        if usb and system.WINDOWS and system.is_volume_root(root):
            try:
                hardware = parse_sysinfo_extended(system.scsi_sysinfo(root.drive), source="SCSI",
                                                  authority=EvidenceAuthority.CURRENT_HARDWARE)
            except OSError:
                pass
        product = re.fullmatch(r"USB\\VID_05AC&PID_([0-9A-F]{4})\\[^\\]+", usb, re.I) if isinstance(usb, str) else None
        if product:
            identifier = DeviceIdentifier(UsbIdentifier(0x05AC, int(product[1], 16)), "USB", EvidenceAuthority.CURRENT_HARDWARE)
            hardware = hardware.merged_with(DeviceEvidence(usb_identifiers=(identifier,)))
        # Files on the volume can have been copied from another iPod. When they contradict what the
        # hardware itself reports, the hardware decides.
        return _resolve(_device_files(root, files).merged_with(hardware)) or _resolve(hardware)
    except Exception:
        return None


def describe(player):
    """Short Chinese label such as 第 7 代 · 银色, video 第 5.5 代 · 黑色 or nano 第 7 代 · 深空灰."""
    if not player:
        return ""
    generation = GENERATIONS.get(player.generation, player.generation)
    # The classic is the default subject of the program, so its family is only named when nothing else is known.
    if player.family == "iPod Classic":
        model = generation or player.name
    elif player.family == "iPod":
        model = generation if generation.startswith("video") else f"iPod {generation}".strip()
    else:
        model = f"{player.name.removeprefix('iPod ')} {generation}".strip()
    return " · ".join(p for p in (model, FINISHES.get(player.finish, player.finish)) if p)
