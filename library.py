"""Read and edit a signed stock music database of an iPod classic, or a third or fourth generation nano."""

import ctypes
import hashlib
import os
import plistlib
import re
import subprocess
import time
from collections import defaultdict
from ctypes import wintypes
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4
from xml.parsers.expat import ExpatError

from device_identity import identify
from audio_convert import lossless_profile, needs_48k_alac, source_audio, source_tags, tag_text

from iPodDB.iTunesDB.writer.signature import verify_hash58
from iPodDB.library.genius import reconcile_genius_playlists
from iPodDB.sidecars import MAX_SIDECAR_BYTES, PlaybackSidecar
from iPodDB.library import (
    AudioEncoding, FileDependency, IPodLibrary, MediaType, Playlist, PlaylistKind, Track, TrackMetadata,
    PlaylistSortOrder, WriteChecksum, WriteResources, WriteTarget, playlist_entries, prepared_audio,
)

DB = "iPod_Control/iTunes/iTunesDB"
MUSIC = "iPod_Control/Music"
STAGE = "iPod_Control/iTunes/ClassicDrop.transfer"
PLAY_COUNTS = "iPod_Control/iTunes/Play Counts"
FORMATS = (".mp3", ".m4a", ".wav")
MUSIC_SUFFIXES = {".mp3", ".m4a", ".m4p", ".aac", ".mp4", ".wav", ".wave", ".aif", ".aiff", ".aifc"}
MAX_DB = 256 * 1024 * 1024


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


def _device_guids(root, disk_ids):
    for name in ("SysInfoExtended", "SysInfo"):
        file = inside(root, "iPod_Control/Device/" + name)
        if not file.is_file() or file.stat().st_size > 2 * 1024 * 1024:
            continue
        data = file.read_bytes()
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


def _windows_disk_ids(drive):
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
        ps = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        output = subprocess.run([str(ps), "-NoProfile", "-NonInteractive", "-Command", query],
                                capture_output=True, timeout=12, creationflags=subprocess.CREATE_NO_WINDOW).stdout
    except subprocess.TimeoutExpired as exc:
        # The disk identifiers may already have arrived while Windows was resolving a parent.
        output = exc.stdout or b""
    except OSError:
        return ()
    return tuple(line.strip() for line in output.decode("ascii", "ignore").splitlines() if line.strip())


def _ipod_usb_instance(disk_ids):
    ipod_disk = any(re.match(r"(USBSTOR|SCSI)\\DISK&VEN_APPLE&PROD_IPOD", v, re.I) for v in disk_ids)
    for node in disk_ids:
        apple = re.fullmatch(r"USB\\VID_05AC&PID_([0-9A-F]{4})\\[^\\]+", node, re.I)
        if apple and (ipod_disk or apple[1] in ("1260", "1261", "1262", "1263")):
            return node
    return None


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


@dataclass
class Device:
    root: Path
    original: bytes
    version: str
    library: IPodLibrary
    guid: bytes | None
    usb_instance: str | None = None
    profile: object = None

    @classmethod
    def open(cls, directory, manual_guid=None):
        root = Path(directory).resolve(strict=True)
        path = inside(root, DB)
        compressed = inside(root, "iPod_Control/iTunes/iTunesCDB")
        if compressed.is_file() and compressed.stat().st_size:
            raise ValueError("这是压缩曲库。此版本适用于使用 iTunesDB 的 iPod classic 和 nano 第 3、4 代。")
        if not path.is_file():
            raise ValueError("未找到原厂曲库。请选择包含 iPod_Control 的 iPod 根目录。")
        if not 108 <= path.stat().st_size <= MAX_DB:
            raise ValueError("曲库为空、文件过大或不完整，无法读取。")
        original = path.read_bytes()
        if original[:4] != b"mhbd" or int.from_bytes(original[8:12], "little") != len(original):
            raise ValueError("iTunesDB 文件头或长度不正确。")
        signed = int.from_bytes(original[48:50], "little") == 1
        disk_ids = _windows_disk_ids(root.drive) if os.name == "nt" and root == Path(root.anchor) else ()
        candidates = (_guid(manual_guid),) if manual_guid else _device_guids(root, disk_ids)
        guid = next((g for g in candidates if g and signed and verify_hash58(original, g)), None)
        if manual_guid and not guid:
            raise ValueError("这个 GUID 与当前曲库校验不匹配。")
        usb_instance = _ipod_usb_instance(disk_ids)
        return cls(root, original, hashlib.sha256(original).hexdigest(), IPodLibrary(original), guid,
                   usb_instance, identify(root, usb_instance))

    def eject(self):
        if not self.usb_instance or self.root != Path(self.root.anchor):
            raise ValueError("弹出需要通过 USB 连接的 iPod 盘符。")
        current = _ipod_usb_instance(_windows_disk_ids(self.root.drive))
        if not current or current.casefold() != self.usb_instance.casefold():
            raise OSError("盘符对应的 iPod 已断开或改变，请刷新设备。")
        _request_usb_eject(current)


def listening(device):
    """Rating, from 0 to 100, and play count of every track as the iPod itself shows them, by track id.

    Stars given and songs played on the iPod stay in its Play Counts file until a sync merges them into the
    library. This lays that file over the library without changing either. Without a usable file, the
    library's own values are returned.
    """
    library = device.library
    try:
        file = inside(device.root, PLAY_COUNTS)
        if file.is_file() and 0 < file.stat().st_size <= MAX_SIDECAR_BYTES:
            merged = library.with_sidecars((PlaybackSidecar("Play Counts", file.read_bytes()),))
            if not merged.sidecar_issues and len(merged.snapshot.tracks) == len(library.snapshot.tracks):
                library = merged
    except Exception:
        pass
    return {t.track_id: (t.rating, t.play_count) for t in library.snapshot.tracks}


def with_listening(device):
    """The device with its Play Counts laid over the library, and that file's bytes; (device, None) without one.

    A library written from this state contains the stars and plays recorded on the iPod, so the file must be
    retired in the same operation or they would be counted again.
    """
    file = inside(device.root, PLAY_COUNTS)
    if not file.is_file():
        return device, None
    try:
        if file.stat().st_size > MAX_SIDECAR_BYTES:
            raise ValueError("文件过大。")
        data = file.read_bytes()
        merged = device.library.with_sidecars((PlaybackSidecar("Play Counts", data),))
        if merged.sidecar_issues:
            raise ValueError(merged.sidecar_issues[0].message)
    except ValueError as error:
        raise ValueError(f"无法合并 iPod 上的播放记录（Play Counts），已停止保存。\n{error}") from error
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


def audio_track(source, track_id, destination, size):
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
    if not (0 < info.length and 0 < info.sample_rate <= 48000 and info.channels in (1, 2)):
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
            track, enc = audio_track(source, -1 - len(additions), dest, size)
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
    target = target or WriteTarget(checksum=WriteChecksum.HASH58, firewire_guid=device.guid)
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
            or not verify_hash58(prepared.itunes, device.guid)):
        raise ValueError("新曲库的曲目、歌单或设备校验不一致。")
    return prepared
