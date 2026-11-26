"""Read and edit a signed stock music database of an iPod classic, or a third or fourth generation nano."""

import hashlib
import os
import plistlib
import re
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4
from xml.parsers.expat import ExpatError

from device_identity import device_secrets, identify, read_device_files
from audio_convert import MAX_ALAC_RATE, lossless_profile, needs_48k_alac, source_audio, source_tags, tag_text

import cpuarch
import shuffle_db
import system
cpuarch.import_wasmtime()  # under ARM64 emulation platform.machine() reports the host, not the x64 process

from iPodDB.iTunesDB.cdb import is_iTunesCDB
from iPodDB.iTunesDB.writer.signature import has_hash72_signature, verify_hash58, verify_hash72, verify_hashab
from iPodDB.library.creation import create_empty_library
from iPodDB.library.genius import reconcile_genius_playlists
from iPodDB.library.signing import (
    parse_hash72_info, recover_hash72_checksum_book_material, recover_hash72_material,
)
from iPodDB.sidecars import MAX_SIDECAR_BYTES, PlaybackSidecar
from iPodDB.library import (
    AudioEncoding, FileDependency, IPodLibrary, MediaType, Playlist, PlaylistKind, SQLiteSource, Track,
    TrackMetadata, PlaylistSortOrder, WriteChecksum, WriteResources, WriteTarget, playlist_entries, prepared_audio,
)

DB = "iPod_Control/iTunes/iTunesDB"
CDB = "iPod_Control/iTunes/iTunesCDB"
SD = shuffle_db.SD
SQLITE_DIR = "iPod_Control/iTunes/iTunes Library.itlp"
SQLITE_DB_NAMES = ("Library.itdb", "Locations.itdb", "Dynamic.itdb", "Extras.itdb", "Genius.itdb")
SQLITE_NAMES = (*SQLITE_DB_NAMES, "Locations.itdb.cbk")
HASH_INFO = "iPod_Control/Device/HashInfo"
MUSIC = "iPod_Control/Music"
STAGE = "iPod_Control/iTunes/ClassicDrop.transfer"
PLAY_COUNTS = "iPod_Control/iTunes/Play Counts"
FORMATS = (".mp3", ".m4a", ".wav")
MUSIC_SUFFIXES = {".mp3", ".m4a", ".m4p", ".aac", ".mp4", ".wav", ".wave", ".aif", ".aiff", ".aifc"}
MAX_DB = 256 * 1024 * 1024
# Known Apple USB product IDs for non-iOS mass-storage iPods (1G-7G, mini, nano 1-7G, shuffle 1-4G).
IPOD_USB_PIDS = {
    "1201", "1202", "1203", "1204", "1205", "1206", "1207", "1208", "1209", "120A",
    "1220", "1223", "1224", "1225", "1231", "1232", "1234",
    "1240", "1241", "1242", "1243", "1245", "1246", "1247", "1248", "1249", "124A",
    "1255", "1260", "1261", "1262", "1263", "1265", "1266", "1267",
    "1300", "1301", "1302", "1303",
}


def inside(root, relative):
    """Only accept normal descendants; never follow a junction off the volume."""
    item = Path(relative)
    if item.anchor or ":" in str(item) or ".." in item.parts or not item.parts:
        raise ValueError("设备中的文件路径无效。")
    path = root / item
    resolved = path.resolve()
    if resolved != path or not resolved.is_relative_to(root):
        raise ValueError(f"设备中的路径经过链接，已停止访问：{relative}")
    return path


def fingerprint(file):
    with file.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_file(path, data):
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _guid(value):
    if isinstance(value, int) and -(1 << 63) <= value < (1 << 64):
        value = f"{value & ((1 << 64) - 1):016x}"
    if isinstance(value, str):
        match = re.fullmatch(r"(?:0x)?([0-9a-f]{16})", value.strip(), re.I)
        if match and int(match[1], 16):
            return bytes.fromhex(match[1])
    return None


def _device_guids(root, disk_ids, files=None):
    raw_files = read_device_files(root, inside) if files is None else files
    for name in ("SysInfoExtended", "SysInfo"):
        data = raw_files.get(name)
        if not data:
            continue
        if not data.startswith(b"bplist"):
            data = data.strip(b"\x00\r\n ")
        if name == "SysInfoExtended":
            try:
                pairs = plistlib.loads(data).items()
            except (ValueError, TypeError, AttributeError, ExpatError, plistlib.InvalidFileException):
                continue
        else:
            pairs = (line.split(":", 1) for line in data.decode("utf-8", "replace").splitlines() if ":" in line)
        for key, value in pairs:
            if key.strip().lower() in ("firewireguid", "firewireid"):
                yield _guid(value)

    for value in disk_ids:
        yield _guid(value.split("\\")[-1].split("&")[0])


def _ipod_usb_instance(disk_ids):
    ipod_disk = any(re.match(r"(USBSTOR|SCSI)\\DISK&VEN_APPLE&PROD_IPOD", v, re.I) for v in disk_ids)
    for node in disk_ids:
        apple = re.fullmatch(r"USB\\VID_05AC&PID_([0-9A-F]{4})\\[^\\]+", node, re.I)
        if apple and (ipod_disk or apple[1].upper() in IPOD_USB_PIDS):
            return node
    return None


def _read_database_bytes(path, *, allow_cdb=False):
    if not 108 <= path.stat().st_size <= MAX_DB:
        raise ValueError("曲库为空、文件过大或不完整，无法读取。")
    original = path.read_bytes()
    if original[:4] != b"mhbd":
        raise ValueError("曲库文件头不正确。")
    declared = int.from_bytes(original[8:12], "little")
    if allow_cdb and is_iTunesCDB(original):
        if not 108 <= declared <= len(original):
            raise ValueError("iTunesCDB 文件头或长度不正确。")
        return original, True
    if declared != len(original):
        raise ValueError("iTunesDB 文件头或长度不正确。")
    return original, False


def _synthesize_shuffle_library(root, sd_bytes):
    """Build an in-memory library from an iPod shuffle that only has iTunesSD and no iTunesDB."""
    target = WriteTarget(checksum=WriteChecksum.NONE, database_version=0x30)
    empty = create_empty_library("iPod shuffle", target, supports_podcasts=False).prepared.itunes
    lib = IPodLibrary(empty)
    paths = shuffle_db.parse_itunessd(sd_bytes)
    additions = []
    for idx, rel in enumerate(paths, 1):
        try:
            candidate = inside(root, rel)
            if not candidate.is_file():
                continue
            size = candidate.stat().st_size
            track, enc = audio_track(candidate, -idx, rel, size, allow_alac=True)
            additions.append(Addition(candidate, track, FileDependency(rel, size, fingerprint(candidate)), enc))
        except Exception:
            continue
    if not additions:
        return empty, lib
    snap = lib.snapshot
    desired = replace(snap, tracks=tuple(a.track for a in additions))
    draft = lib.begin_draft(desired)
    plan = lib.analyze(draft, target)
    media = tuple(prepared_audio(a.track.track_id, a.dependency, a.encoding) for a in additions)
    result = lib.prepare(plan, WriteResources(media=media, pending_playback_sidecars=False))
    if result.prepared is None:
        return empty, lib
    return result.prepared.itunes, IPodLibrary(result.prepared.itunes)


def _open_database_file(root, shuf):
    """Return (db_relative, original_bytes, library, is_cdb, synthesized_db)."""
    cdb_path = inside(root, CDB)
    if cdb_path.is_file() and cdb_path.stat().st_size:
        original, is_cdb = _read_database_bytes(cdb_path, allow_cdb=True)
        return CDB, original, IPodLibrary(original), is_cdb, False
    db_path = inside(root, DB)
    if db_path.is_file():
        original, is_cdb = _read_database_bytes(db_path, allow_cdb=False)
        return DB, original, IPodLibrary(original), is_cdb, False
    if shuf is not None:
        sd_path = inside(root, SD)
        sd_bytes = sd_path.read_bytes() if sd_path.is_file() and sd_path.stat().st_size <= shuffle_db.MAX_SD else b""
        original, lib = _synthesize_shuffle_library(root, sd_bytes) if sd_bytes else (
            create_empty_library("iPod shuffle", WriteTarget(checksum=WriteChecksum.NONE, database_version=0x30),
                                 supports_podcasts=False).prepared.itunes, None)
        return DB, original, (lib or IPodLibrary(original)), False, True
    raise ValueError("未找到原厂曲库。请选择包含 iPod_Control 的 iPod 根目录。")


def _recover_hash72(root, original, guid, scheme):
    """Find valid HASH72 signing material from HashInfo, the current CDB, or Locations.itdb.cbk."""
    def matches(material):
        if material is None:
            return False
        if verify_hash72(original, material.iv, material.random_part, hashing_scheme=scheme or 1):
            return True
        return guid is not None and material.belongs_to_guid(guid)

    try:
        info = inside(root, HASH_INFO)
        if info.is_file() and info.stat().st_size == 54:
            mat = parse_hash72_info(info.read_bytes())
            if matches(mat):
                return mat
    except (OSError, ValueError):
        pass
    try:
        if has_hash72_signature(original):
            mat = recover_hash72_material(original)
            if matches(mat):
                return mat
    except ValueError:
        pass
    try:
        loc = inside(root, f"{SQLITE_DIR}/Locations.itdb")
        cbk = inside(root, f"{SQLITE_DIR}/Locations.itdb.cbk")
        if loc.is_file() and cbk.is_file() and loc.stat().st_size <= MAX_DB and cbk.stat().st_size <= MAX_DB:
            mat = recover_hash72_checksum_book_material(loc.read_bytes(), cbk.read_bytes())
            if matches(mat):
                return mat
    except (OSError, ValueError):
        pass
    return None


def read_sqlite_sources(device):
    """Read existing SQLite companion files under iTunes Library.itlp for lossless round-trip writes."""
    if not device.uses_sqlite:
        return ()
    folder = inside(device.root, SQLITE_DIR)
    if not folder.is_dir():
        return ()
    sources = []
    for name in SQLITE_DB_NAMES:
        file = inside(device.root, f"{SQLITE_DIR}/{name}")
        if file.is_file() and 0 < file.stat().st_size <= MAX_DB:
            sources.append(SQLiteSource(name, file.read_bytes()))
    return tuple(sources)


def _verify_signature(checksum, data, guid, hash72_material=None):
    if checksum is WriteChecksum.NONE:
        return data[:4] == b"mhbd"
    if not guid:
        return False
    if checksum is WriteChecksum.HASH58:
        return verify_hash58(data, guid)
    if checksum is WriteChecksum.HASHAB:
        return verify_hashab(data, guid)
    if checksum is WriteChecksum.HASH72:
        if hash72_material is None:
            return False
        scheme = int.from_bytes(data[48:50], "little") or 1
        return verify_hash58(data, guid) and verify_hash72(
            data, hash72_material.iv, hash72_material.random_part, hashing_scheme=scheme
        )
    return False


UNSIGNABLE_REASONS = ("unsigned", "unsupported_signature", "missing_hash72")


@dataclass
class Device:
    root: Path
    original: bytes
    version: str
    library: IPodLibrary
    guid: bytes | None
    usb_instance: str | None = None
    profile: object = None
    disk_ids: tuple = ()
    secrets: tuple = ()
    readonly_reason: str = ""
    signature_scheme: int = 1
    guid_candidates: int = 0
    sysinfo_files: int = 0
    db_relative: str = DB
    checksum: WriteChecksum = WriteChecksum.HASH58
    is_cdb: bool = False
    uses_sqlite: bool = False
    hash72_material: object = None
    shuffle_version: int = 0
    synthesized_db: bool = False

    @property
    def writable(self):
        if self.readonly_reason:
            return False
        if self.shuffle_version in (1, 2):
            return True
        if self.checksum is WriteChecksum.NONE:
            return True
        if self.checksum is WriteChecksum.HASH72:
            return bool(self.guid and self.hash72_material)
        return bool(self.guid)

    @property
    def supports_alac(self):
        if self.profile is not None:
            return self.profile.supports_alac
        return self.shuffle_version == 0

    @property
    def supports_covers(self):
        if self.shuffle_version:
            return False
        if self.profile is not None:
            return self.profile.supports_covers
        return True

    @property
    def backup_key(self):
        if self.guid:
            return self.guid.hex()
        if self.usb_instance:
            return "usb-" + hashlib.sha256(self.usb_instance.casefold().encode("utf-8")).hexdigest()[:16]
        if self.profile and self.profile.model_number:
            return self.profile.model_number.lower()
        if self.shuffle_version:
            return f"shuffle-v{self.shuffle_version}"
        return "unsigned"

    @property
    def lock_target_relative(self):
        return SD if self.synthesized_db else self.db_relative

    def write_target(self, *, cover_formats=(), artwork=None):
        return WriteTarget(
            checksum=self.checksum,
            firewire_guid=self.guid or b"",
            compressed_database=self.is_cdb,
            sqlite_database=self.uses_sqlite,
            sqlite_checksum=self.checksum if self.uses_sqlite else WriteChecksum.NONE,
            hash72_material=self.hash72_material if self.checksum is WriteChecksum.HASH72 else None,
            cover_formats=cover_formats,
            artwork_root_value=artwork.artwork_root_value if artwork is not None else None,
            supports_sparse_artwork=artwork.supports_sparse_artwork if artwork is not None else True,
            include_auxiliary_image_records=getattr(artwork, "include_auxiliary_image_records", True) if artwork is not None else True,
        )

    @classmethod
    def open(cls, directory, manual_guid=None):
        root = Path(directory).resolve(strict=True)
        disk_ids = system.volume_ids(root)
        usb_instance = _ipod_usb_instance(disk_ids)
        files = read_device_files(root, inside)
        shuf = shuffle_db.detect_shuffle(root, usb_instance)
        profile = identify(root, usb_instance, files=files)
        db_relative, original, library, is_cdb, synthesized_db = _open_database_file(root, shuf)
        scheme = int.from_bytes(original[48:50], "little")
        raw_candidates = (_guid(manual_guid),) if manual_guid else _device_guids(root, disk_ids, files)
        candidates = tuple(dict.fromkeys(g for g in raw_candidates if g))

        shuffle_version = shuf[1] if shuf is not None else 0
        uses_sqlite = is_cdb
        hash72_material = None
        guid = None
        reason = ""

        # Choose expected signing mode based on device tier and database header.
        if shuffle_version:
            checksum = WriteChecksum.NONE
            uses_sqlite = False
        elif is_cdb:
            prof_chk = profile.capabilities.database.checksum.value if profile else ""
            if prof_chk == "hashab" or scheme in (3, 4):
                checksum = WriteChecksum.HASHAB
            elif prof_chk == "hash72" or has_hash72_signature(original) or inside(root, HASH_INFO).is_file():
                checksum = WriteChecksum.HASH72
            else:
                checksum = WriteChecksum.HASHAB
        elif scheme == 1:
            checksum = WriteChecksum.HASH58
        elif scheme == 0:
            prof_chk = profile.capabilities.database.checksum.value if profile else "none"
            if prof_chk != "none":
                checksum = WriteChecksum(prof_chk)
                reason = "unsigned"
            else:
                checksum = WriteChecksum.NONE
        elif scheme in (3, 4):
            checksum = WriteChecksum.HASHAB
        else:
            checksum = WriteChecksum.HASH58
            reason = "unsupported_signature"

        # Verify GUID & signing material for signed models.
        if checksum is WriteChecksum.NONE:
            guid = candidates[0] if candidates else None
        elif not reason:
            if checksum is WriteChecksum.HASH58:
                guid = next((g for g in candidates if verify_hash58(original, g)), None)
            elif checksum is WriteChecksum.HASHAB:
                guid = next((g for g in candidates if verify_hashab(original, g)), None)
            elif checksum is WriteChecksum.HASH72:
                guid = next((g for g in candidates if verify_hash58(original, g)), None)
                if guid is not None:
                    hash72_material = _recover_hash72(root, original, guid, scheme)
                    if hash72_material is None:
                        reason = "missing_hash72"
            if manual_guid and not guid:
                if not candidates:
                    raise ValueError("请输入 16 位十六进制 FireWire GUID。")
                raise ValueError("这个 GUID 与当前曲库校验不匹配。")
            if not guid and not reason:
                if candidates:
                    reason = "guid_mismatch"
                elif not system.is_volume_root(root):
                    reason = "not_volume"
                elif not disk_ids:
                    reason = "no_hardware_id"
                else:
                    reason = "no_apple_usb"
        elif manual_guid:
            if not candidates:
                raise ValueError("请输入 16 位十六进制 FireWire GUID。")
            raise ValueError("当前曲库的签名状态不支持通过填写 GUID 解锁写入。")

        version = hashlib.sha256(original).hexdigest()
        return cls(
            root, original, version, library, guid, usb_instance, profile, disk_ids, device_secrets(files),
            reason, scheme, len(candidates), len(files), db_relative, checksum, is_cdb, uses_sqlite,
            hash72_material, shuffle_version, synthesized_db,
        )

    def reload(self):
        """Reload the database from disk while reusing already-probed hardware identity and secrets."""
        path = inside(self.root, self.db_relative)
        if not path.is_file():
            return Device.open(self.root)
        original, is_cdb = _read_database_bytes(path, allow_cdb=self.is_cdb)
        if self.checksum is not WriteChecksum.NONE and not _verify_signature(
                self.checksum, original, self.guid, self.hash72_material):
            return Device.open(self.root)
        return replace(self, original=original, version=hashlib.sha256(original).hexdigest(),
                       library=IPodLibrary(original), is_cdb=is_cdb, synthesized_db=False)

    def eject(self):
        if not self.usb_instance or not system.is_volume_root(self.root):
            raise ValueError("弹出需要通过 USB 连接的 iPod 卷。")
        current_ids = system.volume_ids(self.root)
        current = _ipod_usb_instance(current_ids)
        if not current or current.casefold() != self.usb_instance.casefold():
            raise OSError("设备对应的 iPod 已断开或改变，请刷新设备。")
        system.eject_device(current, current_ids)


def readonly_message(device):
    """One-line status bar message explaining why a device is read-only and what to do next."""
    reason = getattr(device, "readonly_reason", "")
    root_word = "盘符根目录" if system.WINDOWS else "宗卷根目录"
    messages = {
        "unsigned": "当前为只读：该机型需要签名校验，但当前曲库不含签名，请先用 iTunes 同步一次。",
        "unsupported_signature": "当前为只读：该曲库使用不支持的签名格式，仅支持浏览和导出。",
        "missing_hash72": "当前为只读：未找到 nano 第 5 代所需的 HASH72 校验信息（HashInfo / 原曲库签名），请先用 iTunes 同步一次。",
        "guid_mismatch": "当前为只读：读到的设备标识与曲库校验不匹配。可用 iTunes 同步重签，或在「工具与备份」填写正确 GUID。",
        "not_volume": f"当前为只读：当前打开的是本地文件夹，且缺少设备标识。真机请选择{root_word}，或在「工具与备份」填写 GUID。",
        "no_hardware_id": "当前为只读：未能向系统读取 iPod 的 USB 硬件标识。请直连电脑 USB 后刷新，或在「工具与备份」填写 GUID。",
        "no_apple_usb": "当前为只读：未检测到直连的 Apple iPod USB 标识（读卡器或部分扩展坞无法读取）。请用数据线直连电脑，或在「工具与备份」填写 GUID。",
    }
    return messages.get(reason, "当前为只读。可以浏览、导出，或在「工具与备份」填写设备 GUID。")


def readonly_explanation(device):
    """Detailed troubleshooting text shown when the user clicks the 只读 badge."""
    reason = getattr(device, "readonly_reason", "")
    root_word = "盘符根目录（例如 H:\\）" if system.WINDOWS else "宗卷根目录（例如 /Volumes/IPOD）"
    guide = {
        "unsigned": (
            "该机型（如 iPod classic 或 nano 第 3、4 代）需要 Hash58 签名，但当前曲库缺少签名标记。\n\n"
            "请先用 iTunes 连接并同步一次重新生成签名曲库；当前仍可浏览曲库和导出音频。"
        ),
        "unsupported_signature": (
            f"该曲库使用不支持的签名方案（方案 {getattr(device, 'signature_scheme', '未知')}），"
            "ClassicDrop 暂不支持写入此类曲库。\n\n当前仍可浏览曲库和导出音频。"
        ),
        "missing_hash72": (
            "iPod nano 第 5 代使用 HASH72 签名，需要从设备现有的有效 iTunesCDB、HashInfo 或 Locations.itdb.cbk 提取签名参数。\n\n"
            "请先用 iTunes 连接这台 nano 同步一次以生成完整签名文件，再重新打开 ClassicDrop。"
        ),
        "guid_mismatch": (
            "程序读到了设备的 FireWire GUID，但用它校验当前曲库签名未通过。\n\n"
            "常见原因：\n"
            "· 从另一台 iPod 直接拷贝了整个 iPod_Control 文件夹，或更换过主板；\n"
            "· 曲库曾被其他工具改写且未使用本机 GUID 重新签名。\n\n"
            "解决方法：用 iTunes 连接这台 iPod 同步一次以重新签名，或在「工具与备份 → 填写设备 GUID」"
            "手动输入与当前曲库匹配的 16 位十六进制 GUID。"
        ),
        "not_volume": (
            f"当前打开的是电脑本地文件夹或子目录，且目录中的 SysInfo 未包含可用 GUID。\n\n"
            f"如果你已用数据线连接 iPod，请点「选择设备」选择完整的{root_word}；"
            "如果只打算编辑本地副本，可在「工具与备份 → 填写设备 GUID」手动输入 16 位十六进制 GUID。"
        ),
        "no_hardware_id": (
            "程序未能从操作系统获取到这个磁盘的 USB 硬件标识（Windows 上可能是安全软件拦截了 PowerShell/WMI 查询或查询超时）。\n\n"
            "解决方法：\n"
            "· 将数据线直连电脑主板 USB 口后点「刷新」；\n"
            "· 或点「工具与备份 → 填写设备 GUID」，手动填入这台 iPod 的 16 位十六进制 FireWire GUID。"
        ),
        "no_apple_usb": (
            "程序读取到了磁盘信息，但没有检测到直连的 Apple iPod USB 设备标识。\n\n"
            "常见原因：拆出硬盘/SD 卡通过读卡器连接电脑，或经过了会遮蔽 USB 设备树的扩展坞/虚拟机。\n\n"
            "解决方法：请将 iPod 用数据线直连电脑后点「刷新」，或在「工具与备份 → 填写设备 GUID」"
            "手动填入这台 iPod 的 16 位十六进制 FireWire GUID。"
        ),
    }
    return guide.get(reason, "当前处于只读模式，可以浏览曲库和导出音频，或在「工具与备份」手动填写设备 GUID。")


def readonly_log_detail(device):
    """Privacy-safe diagnostic summary for the run log (counts and flags only, never serials or GUIDs)."""
    reason = getattr(device, "readonly_reason", "") or "unknown"
    return (f"原因={reason}，scheme={getattr(device, 'signature_scheme', '?')}，"
            f"cdb={getattr(device, 'is_cdb', False)}，volume={system.is_volume_root(device.root)}，"
            f"sysinfo={getattr(device, 'sysinfo_files', 0)}，disk_ids={len(getattr(device, 'disk_ids', ()))}，"
            f"apple_usb={bool(getattr(device, 'usb_instance', None))}，candidates={getattr(device, 'guid_candidates', 0)}")


def _merge_play_counts(device, *, strict):
    file = inside(device.root, PLAY_COUNTS)
    if not file.is_file():
        return device.library, None
    size = file.stat().st_size
    if size > MAX_SIDECAR_BYTES or (not strict and size == 0):
        if strict:
            raise ValueError("文件过大。")
        return device.library, None
    data = file.read_bytes()
    merged = device.library.with_sidecars((PlaybackSidecar("Play Counts", data),))
    if merged.sidecar_issues:
        if strict:
            raise ValueError(merged.sidecar_issues[0].message)
        return device.library, None
    if not strict and len(merged.snapshot.tracks) != len(device.library.snapshot.tracks):
        return device.library, None
    return merged, data


def listening(device):
    """Rating, from 0 to 100, and play count of every track as the iPod itself shows them, by track id.

    Stars given and songs played on the iPod stay in its Play Counts file until a sync merges them into the
    library. This lays that file over the library without changing either. Without a usable file, the
    library's own values are returned.
    """
    library = device.library
    try:
        library, _ = _merge_play_counts(device, strict=False)
    except Exception:
        pass
    return {t.track_id: (t.rating, t.play_count) for t in library.snapshot.tracks}


def with_listening(device):
    """The device with its Play Counts laid over the library, and that file's bytes; (device, None) without one.

    A library written from this state contains the stars and plays recorded on the iPod, so the file must be
    retired in the same operation or they would be counted again.
    """
    try:
        merged, data = _merge_play_counts(device, strict=True)
    except ValueError as error:
        raise ValueError(f"无法合并 iPod 上的播放记录（Play Counts），已停止保存。\n{error}") from error
    if data is None:
        return device, None
    return replace(device, library=merged), data


@dataclass
class Addition:
    source: Path
    track: Track
    dependency: FileDependency
    encoding: AudioEncoding


def _numbers(value):
    result = re.fullmatch(r"(\d{1,4})(?:/(\d{1,4}))?", value)
    return (int(result[1]), int(result[2] or 0)) if result else (0, 0)


def audio_track(source, track_id, destination, size, *, allow_alac=True):
    suffix = source.suffix.lower()
    audio = source_audio(source)
    if needs_48k_alac(audio):
        raise ValueError("请重新预览此音源，传入前须转为 24 位 / 48 kHz ALAC。")
    if suffix == ".mp3":
        if audio.info.layer != 3:
            raise ValueError("需要 MPEG Layer III 音频。")
        encoding, label = AudioEncoding.MP3, "MPEG audio file"
    elif suffix == ".m4a":
        codec = audio.info.codec.lower()
        if codec == "alac":
            if not allow_alac:
                raise ValueError("此机型不支持 Apple Lossless（ALAC），请在待传列表中选择 AAC-LC。")
            encoding, label = AudioEncoding.ALAC, "Apple Lossless audio file"
        elif codec == "mp4a.40.2":
            encoding, label = AudioEncoding.AAC, "AAC audio file"
        else:
            raise ValueError(f"不支持的 M4A 编码：{codec or '未知'}，请使用 AAC-LC 或 ALAC。")
    elif suffix == ".wav" and lossless_profile(source, audio):
        encoding, label = AudioEncoding.WAV, "WAV audio file"
    else:
        raise ValueError("此音源需要转换。请在待传列表中选择传输格式。")
    info = audio.info
    if not (0 < info.length and 0 < info.sample_rate <= MAX_ALAC_RATE and info.channels in (1, 2)):
        raise ValueError("请使用单声道或立体声、采样率不超过 48 kHz 的音频。")
    if size >= 2**32 or info.length * 1000 >= 2**32:
        raise ValueError("文件大小或时长超过 iPod 单曲上限。")
    tags = source_tags(audio)
    number, total = _numbers(tag_text(tags, "tracknumber"))
    disc, discs = _numbers(tag_text(tags, "discnumber"))
    year = re.match(r"\d{4}", tag_text(tags, "date"))
    meta = TrackMetadata(location=destination, file_format=label, sample_rate_hz=info.sample_rate,
                         total_tracks=total, disc_number=disc, total_discs=discs,
                         date_added=int(time.time()), last_modified=int(source.stat().st_mtime))
    track = Track(track_id, tag_text(tags, "title") or source.stem, tag_text(tags, "artist"),
                  tag_text(tags, "album"), max(1, round(info.length * 1000)),
                  genre=tag_text(tags, "genre"), year=int(year[0]) if year else 0,
                  album_artist=tag_text(tags, "albumartist"), track_number=number, size_bytes=size,
                  bitrate_kbps=round(getattr(info, "bitrate", 0) / 1000), metadata=meta)
    return track, encoding


def select_additions(device, files, report):
    music = inside(device.root, MUSIC)
    folders = sorted(p.name for p in music.iterdir() if re.fullmatch(r"F\d\d", p.name, re.I) and p.is_dir()) if music.is_dir() else []
    folders = folders or ["F00"]
    by_size = defaultdict(list)
    for track in device.library.snapshot.tracks:
        location = track.metadata.location
        if location.lower().startswith(MUSIC.lower() + "/"):
            by_size[track.size_bytes].append((track.track_id, location))
    seen = {}
    additions, track_ids = [], []
    skipped = 0
    for idx, file in enumerate(files, 1):
        source = Path(file).resolve(strict=True)
        report(f"检查歌曲 {idx}/{len(files)} · {source.name}")
        try:
            if source.suffix.lower() not in FORMATS or not source.is_file():
                raise ValueError("请选择 MP3、M4A 或 PCM WAV 文件。FLAC、DSF 和 DFF 需要先选择传输格式。")
            size = source.stat().st_size
            digest = fingerprint(source)
            # Read each matching-size device file at most once, even in large batches.
            for existing_id, rel in by_size.pop(size, ()):
                candidate = inside(device.root, rel)
                if candidate.is_file():
                    seen.setdefault(fingerprint(candidate), existing_id)
            if digest in seen:
                skipped += 1
                track_ids.append(seen[digest])
                continue
            folder = folders[(len(device.library.snapshot.tracks) + len(additions)) % len(folders)]
            dest = f"{MUSIC}/{folder}/CD{uuid4().hex[:12].upper()}{source.suffix.lower()}"
            if inside(device.root, dest).exists():
                raise ValueError("文件名冲突，请重试。")
            track, enc = audio_track(source, -1 - len(additions), dest, size, allow_alac=device.supports_alac)
            additions.append(Addition(source, track, FileDependency(dest, size, digest), enc))
            seen[digest] = track.track_id
            track_ids.append(track.track_id)
        except Exception as exc:
            raise ValueError(f"{source.name}\n{exc}") from exc
    return additions, skipped, track_ids


def editable_playlist(playlist):
    return playlist.kind == PlaylistKind.PLAYLIST and not playlist.system_managed and not playlist.is_phase


def validate_playlist_name(snapshot, value, playlist_id=None):
    name = (value or "").strip()
    if not name or len(name) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("请输入 1 至 128 个字符的歌单名称。")
    if snapshot and any(p.playlist_id != playlist_id and p.name.casefold() == name.casefold() for p in snapshot.playlists):
        raise ValueError("同名歌单已存在，请选择其他名称。")
    return name


def _ordinary_playlist(snapshot, playlist_id):
    original = next((p for p in snapshot.playlists if p.playlist_id == playlist_id), None)
    if original is None or not editable_playlist(original):
        raise ValueError("请选择当前曲库中的普通播放列表。")
    return original


def _with_playlist(snapshot, edited):
    return replace(snapshot, playlists=tuple(edited if p.playlist_id == edited.playlist_id else p for p in snapshot.playlists))


def playlist_edit(snapshot, track_ids, *, playlist_id=None, name=None, remove=False, entry_ids=None):
    selected = tuple(dict.fromkeys(track_ids))
    if not set(selected) <= {t.track_id for t in snapshot.tracks}:
        raise ValueError("有曲目已不在当前曲库中。请刷新后再操作。")
    if playlist_id is None:
        entry = Playlist(-1, validate_playlist_name(snapshot, name), entries=playlist_entries(selected))
        return replace(snapshot, playlists=(*snapshot.playlists, entry))
    original = _ordinary_playlist(snapshot, playlist_id)
    if remove:
        chosen = set(entry_ids if entry_ids is not None else selected)
        retained = (e for e in original.entries if (e.entry_id if entry_ids is not None else e.track_id) not in chosen)
        entries = tuple(replace(e, position=n) for n, e in enumerate(retained))
    else:
        existing = set(original.track_ids)
        members = original.track_ids + tuple(i for i in selected if i not in existing)
        entries = playlist_entries(members, original.entries)
    if tuple(e.track_id for e in entries) == original.track_ids:
        return snapshot
    return _with_playlist(snapshot, replace(original, entries=entries))


def rename_playlist(snapshot, playlist_id, name):
    original = _ordinary_playlist(snapshot, playlist_id)
    return _with_playlist(snapshot, replace(original, name=validate_playlist_name(snapshot, name, playlist_id)))


def delete_playlist(snapshot, playlist_id):
    _ordinary_playlist(snapshot, playlist_id)
    return replace(snapshot, playlists=tuple(p for p in snapshot.playlists if p.playlist_id != playlist_id))


def move_playlist_entries(snapshot, playlist_id, entry_ids, step):
    original = _ordinary_playlist(snapshot, playlist_id)
    entries = list(original.entries)
    chosen = set(entry_ids)
    if step not in (-1, 1) or not chosen <= {e.entry_id for e in entries}:
        raise ValueError("请刷新后重新选择要移动的条目。")
    indices = range(1, len(entries)) if step == -1 else range(len(entries) - 2, -1, -1)
    for i in indices:
        j = i + step
        if entries[i].entry_id in chosen and entries[j].entry_id not in chosen:
            entries[i], entries[j] = entries[j], entries[i]
    if tuple(entries) == original.entries:
        return snapshot
    ordered = tuple(replace(e, position=i) for i, e in enumerate(entries))
    return _with_playlist(snapshot, replace(original, entries=ordered, sort_order=PlaylistSortOrder.MANUAL))


def is_music(track):
    location = track.metadata.location
    return (track.media_types in ((), (MediaType.AUDIO,)) and not track.metadata.podcast
            and not track.metadata.podcast_guid and not track.metadata.podcast_rss_url
            and not (track.ipod and (track.ipod.unknown_media_type_bits or track.ipod.movie_flag))
            and (not location or (location.replace("\\", "/").casefold().startswith(MUSIC.casefold() + "/")
                                  and Path(location).suffix.lower() in MUSIC_SUFFIXES)))


def without_music(snapshot, removed):
    songs = {t.track_id for t in snapshot.tracks if is_music(t)}
    if not removed or not removed <= songs:
        raise ValueError("只能删除当前曲库中的音乐曲目。请刷新后重新选择。")
    tracks = tuple(t for t in snapshot.tracks if t.track_id not in removed)
    playlists = []
    for playlist in snapshot.playlists:
        entries = tuple(e for e in playlist.entries if e.track_id not in removed)
        if len(entries) != len(playlist.entries):
            entries = tuple(replace(e, position=n) for n, e in enumerate(entries))
            playlist = replace(playlist, entries=entries)
        playlists.append(playlist)
    desired = replace(snapshot, tracks=tracks, playlists=tuple(playlists))
    return reconcile_genius_playlists(snapshot, desired)


def rename_device(snapshot, name):
    name = name.strip()
    if not name or len(name) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("请输入 1 至 128 个字符的设备名称，名称须在一行内。")
    return replace(snapshot, device_name=name)


def prepared_database(device, additions, desired=None, *, removed=frozenset(), delete_playlist_id=None,
                      target=None, resources=None):
    before = device.library.snapshot
    if desired is None:
        desired = replace(before, tracks=before.tracks + tuple(item.track for item in additions))
    target = target or device.write_target()
    draft = device.library.begin_draft(desired, delete_omissions=bool(removed) or delete_playlist_id is not None)
    plan = device.library.analyze(draft, target)
    media = tuple(prepared_audio(a.track.track_id, a.dependency, a.encoding) for a in additions)
    # Appending preserves positional sidecars. Deletion remaps them in the transaction.
    result = device.library.prepare(plan, replace(resources or WriteResources(), media=media, pending_playback_sidecars=False))
    if result.prepared is None:
        raise ValueError("曲库未通过写入检查。\n" + "\n".join(i.message for i in result.issues[:4]))
    prepared = result.prepared
    after = prepared.snapshot
    retained = [t for t in before.tracks if t.track_id not in removed]
    prefix = after.tracks[:len(retained)]
    ids = {m.draft_id: m.output_id for m in prepared.identities if m.subject == "playlist"}
    track_ids = {m.draft_id: m.output_id for m in prepared.identities if m.subject == "track"}
    expected_lists = {(ids.get(p.playlist_id, p.playlist_id), p.name, p.kind, p.parent_id, p.sort_order,
                       tuple(track_ids.get(t, t) for t in p.track_ids))
                      for p in desired.playlists}
    actual_lists = {(p.playlist_id, p.name, p.kind, p.parent_id, p.sort_order, p.track_ids) for p in after.playlists}
    if (len(after.tracks) != len(desired.tracks) or len(prefix) != len(retained)
            or after.device_name != desired.device_name or actual_lists != expected_lists
            or any((a.track_id, a.metadata.location, a.ipod.db_track_id)
                   != (b.track_id, b.metadata.location, b.ipod.db_track_id)
                   for a, b in zip(retained, prefix, strict=True))
            or [t.metadata.location for t in after.tracks[len(prefix):]]
            != [a.dependency.relative_path for a in additions]
            or any((a.title, a.artist, a.album) != (b.title, b.artist, b.album)
                   for a, b in zip(desired.tracks, after.tracks, strict=True))
            or not _verify_signature(target.checksum, prepared.itunes, target.firewire_guid, target.hash72_material)):
        raise ValueError("新曲库的曲目、歌单或设备校验不一致。")
    return prepared
