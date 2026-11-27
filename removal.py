"""Move only library-owned music to private slots until the database commits."""

import base64
import hashlib
import json
import os
import plistlib
import re
import stat
from pathlib import Path

import cpuarch
import diagnostics
import system
cpuarch.import_wasmtime()
from iPodDB.sidecars import (MAX_SIDECAR_BYTES, genius_playlist_number, is_playback_sidecar,
                             parse_itunes_stats, parse_play_counts_plist, remap_genius_playlist,
                             remap_playback_sidecar)
from iPodDB.SQLiteDB.genius import GeniusData, GeniusDatabase, parse_genius_database, serialize_genius_database
from library import DB, MUSIC, MUSIC_SUFFIXES, STAGE, fingerprint, inside, write_file

GENIUS = "Extras.itdb"
MAX_GENIUS = 128 * 1024 * 1024


def genius_source(device, removed):
    """What the engine needs to delete songs that iTunes gave Genius identities.

    Returns the Genius database to hand to the engine and the bytes now on the iPod, or (None, None) when no
    removed song has such an identity. The engine drops the removed songs from the database, which is then
    published with the library. An iPod without the file has nothing to drop, so the engine is given an
    empty database and its output is not written.
    """
    if not any(t.ipod and t.ipod.genius_id for t in device.library.snapshot.tracks if t.track_id in removed):
        return None, None
    path = inside(device.root, "iPod_Control/iTunes/" + GENIUS)
    if not path.exists():
        return serialize_genius_database(GeniusDatabase(GeniusData())), None
    if not path.is_file() or path.stat().st_size > MAX_GENIUS:
        raise ValueError("iPod 上的 Genius 数据（Extras.itdb）无法读取，已停止删除。")
    data = path.read_bytes()
    try:
        parse_genius_database(data)
    except Exception as error:
        raise ValueError(f"iPod 上的 Genius 数据（Extras.itdb）已损坏，已停止删除。\n{error}") from error
    return data, data


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _sidecars(device):
    parent = inside(device.root, DB).parent
    return {p.name: inside(device.root, p.relative_to(device.root))
            for p in parent.iterdir() if is_playback_sidecar(p.name)}


def _remap(name, data, before, after, old_ids, new_ids):
    if not data:
        return data
    if genius_playlist_number(name) is not None:
        return remap_genius_playlist(data, tuple(t.ipod.db_track_id for t in before),
                                     tuple(t.ipod.db_track_id for t in after))
    if data[:4] in (b"mhdp", b"pdhm", b"mhpo", b"ophm"):
        return remap_playback_sidecar(data, old_ids, new_ids)
    if name.casefold() == "playcounts.plist":
        counts = parse_play_counts_plist(data)
        deleted = {t.ipod.db_track_id for t in before} - {t.ipod.db_track_id for t in after}
        if not any(row.persistent_id in deleted for row in counts.entries):
            return data
        root = plistlib.loads(data)
        root["tracks"] = [v for v, row in zip(root["tracks"], counts.entries, strict=True)
                          if row.persistent_id not in deleted]
        fmt = plistlib.FMT_BINARY if data.startswith(b"bplist") else plistlib.FMT_XML
        return plistlib.dumps(root, fmt=fmt, sort_keys=False)
    if name.casefold() == "itunesstats":
        counts = parse_itunes_stats(data)
        if len(counts.entries) > len(old_ids):
            raise ValueError("播放记录数超过原曲目数。")
        word = 3 if counts.format == "iTunesStats24" else 4
        offset = word * 2
        keep = set(new_ids)
        rows = []
        for identity in old_ids[:len(counts.entries)]:
            size = int.from_bytes(data[offset:offset + word], "little")
            if identity in keep:
                rows.append(data[offset:offset + size])
            offset += size
        return len(rows).to_bytes(word, "little") + data[word:2 * word] + b"".join(rows) + data[offset:]
    raise ValueError("无法识别播放记录格式。")


def _music_file(device, location):
    parts = Path(location).parts
    if (len(parts) == 4 and "/".join(parts[:2]).casefold() == MUSIC.casefold()
            and re.fullmatch(r"F[0-9]{2}", parts[2], re.I) and Path(parts[3]).suffix.lower() in MUSIC_SUFFIXES):
        return inside(device.root, location)
    return None


def _file_stamp(path):
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns]


def removal_receipt(device, desired, after_hash, report, genius=None):
    """genius is the Genius database before and after the removal, when the iPod keeps one that changes."""
    before, after = device.library.snapshot.tracks, desired.tracks
    old_ids, new_ids = tuple(t.track_id for t in before), tuple(t.track_id for t in after)
    keep = set(new_ids)
    protected = {_music_file(device, t.metadata.location) for t in after if t.metadata.location}
    media = {}
    for track in before:
        if track.track_id in keep:
            continue
        path = _music_file(device, track.metadata.location)
        if path is None or path in protected or path in media or not path.exists():
            continue
        if not path.is_file():
            raise ValueError(f"音频文件不可用，已停止删除：{path.name}")
        stamp = _file_stamp(path)
        if stamp[0] != track.size_bytes:
            diagnostics.log.warning(
                "音频文件大小与曲库记录不符，仍按实际文件继续删除：%s（曲库 %d 字节，磁盘 %d 字节）",
                path.name, track.size_bytes, stamp[0],
            )
        media[path] = [path.relative_to(device.root).as_posix(), stamp]
    updates = {}
    for name, path in _sidecars(device).items():
        if not path.is_file() or path.stat().st_size > MAX_SIDECAR_BYTES:
            raise ValueError(f"无法核对设备上的播放记录：{name}")
        data = path.read_bytes()
        try:
            result = _remap(name, data, before, after, old_ids, new_ids)
        except ValueError as error:
            raise ValueError(f"无法保留《{name}》的播放记录，已停止删除。\n{error}") from error
        if result == data:
            new = "same"
        elif result is None:
            new = None
        else:
            new = base64.b64encode(result).decode("ascii")
        readonly = system.is_readonly(path.stat())
        updates[name] = [_digest(data), new, readonly]
    if genius is not None:
        old, new = genius
        path = inside(device.root, "iPod_Control/iTunes/" + GENIUS)
        readonly = system.is_readonly(path.stat())
        updates[GENIUS] = [_digest(old), "same" if new == old else base64.b64encode(new).decode("ascii"), readonly]
    report(f"已核对 {len(media)} 个独立音频文件。")
    return dict(before=device.version, after=after_hash, guid=device.backup_key,
                media=list(media.values()), sidecars=updates)


def merge_receipt(device, after_hash, counts):
    """A receipt that retires Play Counts once its stars and plays are part of the committed library."""
    updates = {}
    for name, path in _sidecars(device).items():
        if not path.is_file() or path.stat().st_size > MAX_SIDECAR_BYTES:
            raise ValueError(f"无法核对设备上的播放记录：{name}")
        data = path.read_bytes()
        readonly = system.is_readonly(path.stat())
        retire = name.casefold() == "play counts"
        if retire and data != counts:
            raise ValueError("设备上的播放记录已变化，请刷新后重试。")
        updates[name] = [_digest(data), None if retire else "same", readonly]
    return dict(before=device.version, after=after_hash, guid=device.backup_key, media=[], sidecars=updates)


def quarantine(device, receipt, report):
    slots = inside(device.root, STAGE + "/removed")
    slots.mkdir()
    count = len(receipt["media"])
    for i, (relative, stamp) in enumerate(receipt["media"]):
        path = inside(device.root, relative)
        if _file_stamp(path) != stamp:
            raise ValueError(f"歌曲在操作期间已变化，请刷新后重试：{path.name}")
        if i % 100 == 0 or i + 1 == count:
            report(f"准备删除 {i + 1}/{count} · {path.name}")
        path.rename(slots / str(i))
    files = _sidecars(device)
    if GENIUS in receipt["sidecars"]:
        files[GENIUS] = inside(device.root, "iPod_Control/iTunes/" + GENIUS)
    if (files.keys() != receipt["sidecars"].keys() or any(not path.is_file() for path in files.values()) or
            any(fingerprint(path) != receipt["sidecars"][name][0] for name, path in files.items())):
        raise ValueError("设备上的播放记录已变化，请刷新后重试。")


def checked_receipt(device, plan, backups, kind):
    for key in ("before", "after", "receipt"):
        if not re.fullmatch(r"[0-9a-f]{64}", plan[key]):
            raise ValueError("操作记录的校验值无效。")
    if backups is None:
        raise ValueError("请在发起操作的电脑上打开「恢复中断操作」。")
    path = Path(backups) / device.backup_key / plan["before"][:20] / (kind + "-" + plan["receipt"] + ".json")
    if not path.is_file():
        raise ValueError(f"需要本次操作的电脑备份记录：{path}")
    data = path.read_bytes()
    if _digest(data) != plan["receipt"]:
        raise ValueError("电脑上的操作记录未通过校验。")
    receipt = json.loads(data)
    if (receipt["before"], receipt["after"], receipt["guid"]) != (plan["before"], plan["after"], device.backup_key):
        raise ValueError("操作记录与设备曲库不匹配。")
    return receipt, path.parent


def settle_removal(device, plan, backups, committed):
    receipt, _ = checked_receipt(device, plan, backups, "delete")
    slots = inside(device.root, STAGE + "/removed")
    if slots.exists():
        expected = {str(i) for i in range(len(receipt["media"]))}
        if any(p.name not in expected and not system.junk_file(p.name) for p in slots.iterdir()):
            raise ValueError("暂存目录中出现其他文件，请检查后再恢复。")
    for i, (relative, stamp) in enumerate(receipt["media"]):
        slot = inside(device.root, STAGE + "/removed/" + str(i))
        if slot.exists():
            if not slot.is_file() or _file_stamp(slot) != stamp:
                raise ValueError(f"暂存音频已改变，已保留：{relative}")
            if committed:
                try:
                    slot.unlink()
                except PermissionError:
                    if not system.is_readonly(slot.stat()):
                        raise
                    slot.chmod(slot.stat().st_mode | stat.S_IWUSR)
                    slot.unlink()
            else:
                target = inside(device.root, relative)
                if not system.WINDOWS and (target.exists() or target.is_symlink()):
                    raise FileExistsError(f"原路径已存在文件，已保留暂存音频：{relative}")
                # Windows rename never overwrites a file recreated at the original path.
                slot.rename(target)
        elif not committed:
            original = inside(device.root, relative)
            if not original.is_file():
                raise ValueError(f"暂存音频和原文件均不可用：{relative}")
    if slots.is_dir():
        slots.rmdir()
    if committed:
        _apply_sidecars(device, receipt["sidecars"])


def _apply_sidecars(device, updates):
    # Release the deleted music's space before allocating a replacement sidecar.
    part = inside(device.root, STAGE + "/sidecar.part")
    for name, (old, new, readonly) in updates.items():
        if new == "same":
            continue
        live = inside(device.root, "iPod_Control/iTunes/" + name)
        result = base64.b64decode(new, validate=True) if new is not None else None
        actual = fingerprint(live) if live.exists() else None
        if actual == (_digest(result) if result is not None else None):
            if readonly and result is not None:
                live.chmod(stat.S_IREAD)
            continue
        if actual != old:
            raise ValueError(f"播放记录已经改变，已保留现场：{name}")
        if result is not None:
            part.unlink(missing_ok=True)
            write_file(part, result)
            if fingerprint(part) != _digest(result):
                raise OSError(f"播放记录写入校验失败：{name}")
        if readonly:
            live.chmod(stat.S_IWRITE)
        try:
            if result is None:
                live.unlink()
            else:
                os.replace(part, live)
        finally:
            if readonly and live.exists():
                live.chmod(stat.S_IREAD)
