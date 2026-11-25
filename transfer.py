"""Publish an already-verified Classic database after its audio files are durable."""

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path

from audio_convert import prepared_files
import cpuarch
import system
cpuarch.import_wasmtime()
from iPodDB.library import WriteResources
from library import (DB, MUSIC, PLAY_COUNTS, STAGE, delete_playlist, fingerprint, inside, is_music, playlist_edit,
                     prepared_database, select_additions, validate_playlist_name, with_listening, without_music,
                     write_file)
from native_covers import prepare_covers, publish_covers, settle_covers
from removal import (GENIUS, checked_receipt, genius_source, merge_receipt, quarantine, removal_receipt,
                     settle_removal)

NEW_FILE = re.compile(r"iPod_Control/Music/F\d{2}/CD[0-9A-F]{12}\.(?:mp3|m4a|wav)", re.I)


@contextmanager
def device_lock(device):
    if not device.guid:
        raise ValueError("需要与现有曲库匹配的 FireWire GUID 才能传歌。")
    path = inside(device.root, "iPod_Control/iTunes/iTunesLock")
    with system.lock_file(path, inside(device.root, DB)) as release_database:
        if fingerprint(inside(device.root, DB)) != device.version:
            raise ValueError("读取后曲库已改变。请刷新设备，再传入歌曲。")
        yield release_database


def backup_bytes(destination, data, digest):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temp = Path(stream.name)
        try:
            with temp.open("wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            if fingerprint(temp) != digest:
                raise OSError("电脑上的备份校验失败。")
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
    if fingerprint(destination) != digest:
        raise OSError(f"电脑上的备份文件未通过校验：{destination}")
    return destination


def _stage_file(device, name):
    return inside(device.root, f"{STAGE}/{name}")


def _retire_play_counts(device, digest):
    """Remove the Play Counts file whose stars and plays the committed library now contains.

    A file with other content was written by the iPod since then and is left alone.
    """
    file = inside(device.root, PLAY_COUNTS)
    if file.is_file() and fingerprint(file) == digest:
        file.chmod(stat.S_IWRITE)
        file.unlink()


def recover(device, backups=None):
    """Under the device lock, settle an interrupted batch against the actual DB."""
    folder = inside(device.root, STAGE)
    if not folder.exists():
        return
    journal = _stage_file(device, "plan.json")
    plan = None
    if journal.exists():
        if journal.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("中断记录超出可读取范围。")
        plan = json.loads(journal.read_text(encoding="utf8"))
    deletion = plan is not None and plan.get("version") == 2
    covers = plan is not None and plan.get("version") == 3
    allowed = {"plan.json", "plan.tmp", "new.db"}
    allowed.update(("sidecar.part", "removed") if deletion else ("media.part",))
    if covers:
        allowed.add("cover.part")
    if {p.name for p in folder.iterdir() if not system.junk_file(p.name)} - allowed:
        raise ValueError(f"暂存目录内含有无法识别的文件，请检查：{folder}")
    if plan is not None:
        current = fingerprint(inside(device.root, DB))
        if plan["version"] not in (1, 2, 3) or current not in (plan["before"], plan["after"]):
            raise ValueError(f"曲库与中断记录不一致。已保留暂存文件：{folder}")
        if covers:
            receipt, backup_dir = checked_receipt(device, plan, backups, "covers")
            if plan["files"] != receipt["files"]:
                raise ValueError("暂存歌曲列表与电脑上的备份记录不符。")
            settle_covers(device, receipt["covers"], backup_dir, committed=current == plan["after"])
        if deletion:
            settle_removal(device, plan, backups, committed=current == plan["after"])
        elif current == plan["before"]:
            if current != device.version:
                raise ValueError("请刷新曲库后重试恢复。")
            protected = {t.metadata.location.replace("\\", "/").casefold() for t in device.library.snapshot.tracks}
            removals = []
            for rel, size, sha in plan["files"]:
                if not NEW_FILE.fullmatch(rel) or rel.casefold() in protected:
                    raise ValueError("中断记录中有非本次可清理的音乐文件。")
                candidate = inside(device.root, rel)
                if candidate.exists():
                    if candidate.stat().st_size != size or fingerprint(candidate) != sha:
                        raise ValueError(f"暂存的歌曲已被改动，已保留：{rel}")
                    removals.append(candidate)
            for file in removals:
                file.unlink()
        if plan.get("counts") and current == plan["after"]:
            _retire_play_counts(device, plan["counts"])
    elif _stage_file(device, "media.part").exists() or _stage_file(device, "new.db").exists():
        raise ValueError(f"缺少传输记录，已保留暂存文件：{folder}")
    if covers:
        _stage_file(device, "cover.part").unlink(missing_ok=True)
    for name in (("sidecar.part" if deletion else "media.part"), "new.db", "plan.tmp", "plan.json"):
        _stage_file(device, name).unlink(missing_ok=True)
    folder.rmdir()


def recover_interrupted(device, backups=None):
    with device_lock(device):
        recover(device, backups)


def _prepare_commit(device, ready, stack, report, *, desired, removed, delete_playlist_id, playlist_id,
                    new_playlist_name, repair_covers, reviewed, ratings):
    """Build the target library snapshot, optional artwork/Genius updates and final signed database."""
    counts = None
    if ratings:
        if any(type(value) is not int or not 0 <= value <= 100 for value in ratings.values()):
            raise ValueError("评级必须在 0 到 100 之间。")
        # Stars given on the iPod live in its Play Counts file and would override the library's,
        # so they are merged into the library here and the file is retired with the commit.
        device, counts = with_listening(device)
        if not set(ratings) <= {t.track_id for t in device.library.snapshot.tracks}:
            raise ValueError("有曲目已不在当前曲库中。请刷新后再操作。")
        desired = replace(device.library.snapshot, tracks=tuple(
            replace(t, rating=ratings[t.track_id]) if t.track_id in ratings else t for t in device.library.snapshot.tracks))
    elif ready or repair_covers:
        # New songs are appended to the library as the iPod itself shows it, so that stars and plays
        # recorded on the iPod survive the transfer. Records that cannot be read are left alone.
        try:
            device, counts = with_listening(device)
        except ValueError:
            pass
    before = device.library.snapshot
    if removed:
        desired = without_music(before, removed)
    if delete_playlist_id is not None:
        desired = delete_playlist(before, delete_playlist_id)
    additions, skipped, track_ids = select_additions(device, ready, report) if ready else ([], 0, ())
    if reviewed:
        for song in reviewed.values():
            song.check_source()
    if ready or repair_covers:
        desired = replace(before, tracks=before.tracks + tuple(a.track for a in additions))
    if playlist_id is not None or new_playlist_name is not None:
        desired = playlist_edit(desired, track_ids, playlist_id=playlist_id, name=new_playlist_name)
    paths = ({t.track_id: inside(device.root, t.metadata.location) for t in before.tracks
              if is_music(t) and t.metadata.location} if repair_covers
             else dict(zip(track_ids, ready, strict=True)))
    covers, missing = prepare_covers(device, additions, desired, paths, stack, report)
    if not covers and not additions and (desired is None or desired == before):
        return None, (0, missing if repair_covers else skipped, None)
    report("生成曲库并核对原有歌曲、播放列表及设备校验值…")
    genius = None
    if covers:
        database = covers.database
    else:
        source, on_device = genius_source(device, removed)
        prepared = prepared_database(device, additions, desired, removed=removed, delete_playlist_id=delete_playlist_id,
                                     resources=WriteResources(genius_source=source) if source else None)
        database = prepared.itunes
        if ratings and any((made.rating, made.play_count) != (wanted.rating, wanted.play_count)
                           for made, wanted in zip(prepared.snapshot.tracks, desired.tracks, strict=True)):
            raise ValueError("新曲库的评级或播放次数与预期不一致。")
        if on_device is not None and prepared.genius is not None:
            genius = on_device, prepared.genius
    return (device, counts, additions, skipped, covers, missing, database, genius, desired), None


def _write_backups_and_plan(device, backups, database, new_hash, *, additions, covers, counts, removed, ratings,
                            desired, genius, report):
    """Verify free space, write PC backups of everything about to change, and assemble the journal plan."""
    needed = len(database) + sum(item.dependency.size for item in additions) + (len(genius[1]) if genius else 0)
    if covers:
        growth = peak = 0
        for path, old_size, _, _ in covers.records:
            new_size = len(covers.files[path])
            peak = max(peak, growth + new_size)
            growth += new_size - old_size
        needed += peak
    if shutil.disk_usage(device.root).free < needed:
        raise OSError("iPod 可用空间不足。")
    if Path(backups).resolve().is_relative_to(device.root):
        raise ValueError("曲库备份必须存放在电脑上。")
    backup_dir = Path(backups) / device.guid.hex() / device.version[:20]
    backup = backup_bytes(backup_dir / "iTunesDB", device.original, device.version)
    receipt = None
    old_counts = None
    if counts is not None:
        old_counts = hashlib.sha256(counts).hexdigest()
        backup_bytes(backup_dir / ("Play Counts-" + old_counts), counts, old_counts)
    if removed or (counts is not None and ratings):
        if genius and genius[0] != genius[1]:
            old_genius = hashlib.sha256(genius[0]).hexdigest()
            backup_bytes(backup_dir / (GENIUS + "-" + old_genius), genius[0], old_genius)
        receipt = (removal_receipt(device, desired, new_hash, report, genius) if removed
                   else merge_receipt(device, new_hash, counts))
        payload = json.dumps(receipt, ensure_ascii=False, separators=(",", ":")).encode("utf8")
        digest = hashlib.sha256(payload).hexdigest()
        backup_bytes(backup_dir / f"delete-{digest}.json", payload, digest)
        plan = dict(version=2, before=device.version, after=new_hash, receipt=digest)
    else:
        plan = dict(version=1, before=device.version, after=new_hash,
                    files=[(a.dependency.relative_path, a.dependency.size, a.dependency.sha256) for a in additions])
        if covers:
            if covers.old_index is not None:
                old_hash = hashlib.sha256(covers.old_index).hexdigest()
                backup_bytes(backup_dir / ("ArtworkDB-" + old_hash), covers.old_index, old_hash)
            cover_receipt = dict(before=device.version, after=new_hash, guid=device.guid.hex(),
                                 covers=covers.records, files=plan["files"])
            payload = json.dumps(cover_receipt, separators=(",", ":")).encode("utf8")
            digest = hashlib.sha256(payload).hexdigest()
            backup_bytes(backup_dir / f"covers-{digest}.json", payload, digest)
            plan.update(version=3, receipt=digest)
        if old_counts is not None:
            plan["counts"] = old_counts
    return backup, plan, receipt


def _commit_stage(device, backups, database, new_hash, backup, plan, receipt, additions, covers,
                  release_database, removed, report):
    """Write journal, stage and verify media/covers, atomically swap iTunesDB, then settle."""
    folder = inside(device.root, STAGE)
    folder.mkdir()
    try:
        write_file(_stage_file(device, "plan.tmp"), json.dumps(plan).encode())
        _stage_file(device, "plan.tmp").rename(_stage_file(device, "plan.json"))
        staged_db = _stage_file(device, "new.db")
        write_file(staged_db, database)
        if fingerprint(staged_db) != new_hash:
            raise OSError("iPod 上的暂存曲库校验失败。")
        if receipt is not None:
            quarantine(device, receipt, report)
        for idx, item in enumerate(additions, 1):
            report(f"复制并校验 {idx}/{len(additions)} · {item.track.title}")
            temp = _stage_file(device, "media.part")
            with item.source.open("rb") as source, temp.open("xb") as out:
                shutil.copyfileobj(source, out, 1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
            if temp.stat().st_size != item.dependency.size or fingerprint(temp) != item.dependency.sha256:
                raise OSError(f"源文件变动或写入不完整：{item.source.name}")
            destination = inside(device.root, item.dependency.relative_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            # On Windows rename refuses to replace an existing destination.
            temp.rename(destination)
        if covers:
            report("写入并核对原厂封面索引及缩略图…")
            publish_covers(device, covers)
        report("提交曲库…")
        live = inside(device.root, DB)
        if fingerprint(live) != device.version:
            raise ValueError("传输期间曲库发生变化。")
        # Windows replace needs the destination closed; iTunesLock stays exclusive.
        release_database()
        os.replace(staged_db, live)
        if fingerprint(live) != new_hash:
            raise OSError(f"写入后的曲库校验失败。电脑上的备份在：{backup}")
    except Exception as exc:
        try:
            recover(device, backups)
        except Exception as recovery_error:
            raise OSError(f"{exc}\n恢复记录已保留。重新连接后点「恢复中断操作」。\n{recovery_error}") from exc
        raise
    if removed:
        report("曲库已提交，正在更新播放记录并释放音频空间…")
    try:
        recover(device, backups)
    except (OSError, ValueError) as exc:
        raise OSError(f"曲库已写入，暂存记录尚未处理完。请刷新后点「恢复中断操作」。\n{exc}") from exc


def transfer(device, files, backups, report=lambda _: None, *, desired=None, remove_ids=(),
             formats=None, playlist_id=None, new_playlist_name=None, delete_playlist_id=None, repair_covers=False, reviewed=None,
             ratings=None):
    """ratings maps track ids to a new rating from 0 to 100, in steps of 20 for whole stars."""
    removed = frozenset(remove_ids)
    if sum((bool(files), desired is not None, bool(removed), delete_playlist_id is not None, repair_covers, bool(ratings))) > 1:
        raise ValueError("传歌、删除和其他编辑需要分别提交。")
    if playlist_id is not None and new_playlist_name is not None:
        raise ValueError("一次传输只能选择一个目标播放列表。")
    if (playlist_id is not None or new_playlist_name is not None) and not files:
        raise ValueError("请先选择要传入播放列表的歌曲。")
    if not device.guid:
        raise ValueError("设备处于只读状态。")
    if new_playlist_name is not None:
        validate_playlist_name(device.library.snapshot, new_playlist_name)
    with prepared_files(files, formats, device.root, report, reviewed=reviewed) as ready, \
            device_lock(device) as release_database, ExitStack() as stack:
        recover(device, backups)
        prepared, noop = _prepare_commit(
            device, ready, stack, report, desired=desired, removed=removed,
            delete_playlist_id=delete_playlist_id, playlist_id=playlist_id,
            new_playlist_name=new_playlist_name, repair_covers=repair_covers,
            reviewed=reviewed, ratings=ratings,
        )
        if noop is not None:
            return noop
        device, counts, additions, skipped, covers, missing, database, genius, desired = prepared
        new_hash = hashlib.sha256(database).hexdigest()
        backup, plan, receipt = _write_backups_and_plan(
            device, backups, database, new_hash, additions=additions, covers=covers,
            counts=counts, removed=removed, ratings=ratings, desired=desired,
            genius=genius, report=report,
        )
        _commit_stage(device, backups, database, new_hash, backup, plan, receipt,
                      additions, covers, release_database, removed, report)
        if repair_covers:
            return covers.count, missing, backup
        return len(additions), skipped, backup


def import_tracks(device, files, backups, report=lambda _: None, *, formats=None,
                  playlist_id=None, new_playlist_name=None, reviewed=None):
    return transfer(device, files, backups, report, formats=formats, playlist_id=playlist_id,
                    new_playlist_name=new_playlist_name, reviewed=reviewed)


def update_library(device, desired, backups, report=lambda _: None):
    return transfer(device, (), backups, report, desired=desired)


def delete_tracks(device, remove_ids, backups, report=lambda _: None):
    return transfer(device, (), backups, report, remove_ids=remove_ids)


def delete_playlist_on_device(device, playlist_id, backups, report=lambda _: None):
    return transfer(device, (), backups, report, delete_playlist_id=playlist_id)


def repair_device_covers(device, backups, report=lambda _: None):
    return transfer(device, (), backups, report, repair_covers=True)


def save_ratings(device, ratings, backups, report=lambda _: None):
    return transfer(device, (), backups, report, ratings=ratings)


def export_tracks(device, tracks, directory, report=lambda _: None):
    directory = Path(directory).resolve(strict=True)
    if directory.is_relative_to(device.root) or not directory.is_dir():
        raise ValueError("请选择电脑上的导出文件夹。")
    if fingerprint(inside(device.root, DB)) != device.version:
        raise ValueError("曲库已改变。请刷新设备。")
    written = 0
    for track in tracks:
        try:
            location = track.metadata.location
            if not location.casefold().startswith(MUSIC.casefold() + "/"):
                raise ValueError("这首歌没有可导出的本地音乐路径。")
            source = inside(device.root, location)
            suffix = source.suffix.lower()
            if not re.fullmatch(r"\.[a-z0-9]{1,5}", suffix):
                raise ValueError("设备文件扩展名无效。")
            title = " - ".join(s for s in (track.artist, track.title) if s)
            stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title)[:100].strip(" .") or str(track.track_id)
            if Path(stem + suffix).is_reserved():
                stem = "_" + stem
            if source.stat().st_size != track.size_bytes:
                raise ValueError("设备文件大小与曲库记录不一致。")
            report(f"导出 {written + 1}/{len(tracks)} · {track.title}")
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".classicdrop-", delete=False) as staged:
                temp = Path(staged.name)
            try:
                with source.open("rb") as src, temp.open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                    dst.flush()
                    os.fsync(dst.fileno())
                digest = fingerprint(temp)
                if digest != fingerprint(source):
                    raise OSError("导出文件校验失败。")
                index = 1
                while True:
                    name = stem if index == 1 else f"{stem} ({index})"
                    destination = directory / (name + suffix)
                    if destination.exists() or destination.is_symlink():
                        # POSIX rename silently overwrites; the no-replace check must run first.
                        if destination.is_file() and not destination.is_symlink() and fingerprint(destination) == digest:
                            break
                        index += 1
                        continue
                    try:
                        temp.rename(destination)
                        written += 1
                        break
                    except FileExistsError:
                        index += 1
            finally:
                temp.unlink(missing_ok=True)
        except Exception as exc:
            raise OSError(f"已新导出 {written} 首。处理「{track.title}」时出错：{exc}") from exc
    return written
