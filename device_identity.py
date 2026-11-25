"""Work out which iPod model and finish is connected. Read-only, and never required for opening a library."""

from dataclasses import dataclass
import os
from pathlib import Path
import re

import system
from device_registry import (DEFAULT_DEVICE_REGISTRY, DatabaseChecksum, DeviceEvidence, DeviceIdentifier, EvidenceAuthority,
                             UsbIdentifier, parse_sysinfo, parse_sysinfo_extended)

GENERATIONS = {"6th Gen": "第 6 代", "6.5th Gen": "第 6 代（120 GB）", "7th Gen": "第 7 代",
               "3rd Gen": "第 3 代", "4th Gen": "第 4 代"}
FINISHES = {"Silver": "银色", "Black": "黑色", "Blue": "蓝色", "Green": "绿色", "Red": "红色", "Pink": "粉色",
            "Purple": "紫色", "Orange": "橙色", "Yellow": "黄色"}
FAMILIES = {"iPod Classic": "iPod classic", "iPod Nano": "iPod nano"}


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
        return {("iPod Nano", "3rd Gen"): "nano3", ("iPod Nano", "4th Gen"): "nano4"}.get(
            (self.family, self.generation), "classic")

    @property
    def proven(self):
        """Only the classic has been used with this program on real hardware."""
        return self.family == "iPod Classic"

    @property
    def name(self):
        """What to call it when its library carries no name."""
        return FAMILIES.get(self.family, self.family)


def _device_files(root):
    evidence = DeviceEvidence()
    for name, parse in (("SysInfoExtended", parse_sysinfo_extended), ("SysInfo", parse_sysinfo)):
        file = root / "iPod_Control/Device" / name
        if file.is_file() and 0 < file.stat().st_size <= 2 * 1024 * 1024:
            evidence = evidence.merged_with(parse(file.read_bytes()))
    return evidence


def _supported(capabilities):
    """This program writes a plain iTunesDB signed with HASH58: the classic and the nano of 2007 and 2008."""
    database = capabilities.database
    return (database.checksum is DatabaseChecksum.HASH58 and not database.supports_compressed_database
            and not database.uses_sqlite_database)


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
    first = candidates[0].capabilities
    if not _supported(first) or any((c.capabilities.artwork, c.capabilities.database) != (first.artwork, first.database)
                                    for c in candidates):
        return None
    generations = {c.generation for c in candidates}
    return Player(candidates[0].family, generations.pop() if len(generations) == 1 else "", "", first)


def identify(root, usb=None):
    """What this iPod is, or None when that cannot be told or it is a model this program does not write.

    usb is the Windows instance name of the Apple USB device the volume belongs to, or any true value for
    a USB connection whose instance is unknown.
    """
    try:
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
        return _resolve(_device_files(root).merged_with(hardware)) or _resolve(hardware)
    except Exception:
        return None


def describe(player):
    """Short Chinese label such as 第 7 代 · 银色 or nano 第 4 代 · 紫色."""
    if not player:
        return ""
    generation = GENERATIONS.get(player.generation, player.generation)
    # The classic is the default subject of the program, so its family is only named when nothing else is known.
    if player.family == "iPod Classic":
        model = generation or player.name
    else:
        model = f"{player.name.removeprefix('iPod ')} {generation}".strip()
    return " · ".join(p for p in (model, FINISHES.get(player.finish, player.finish)) if p)
