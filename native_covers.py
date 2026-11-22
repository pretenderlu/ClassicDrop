"""Prepare and publish the cover thumbnails used by the stock iPod classic player."""

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image
from mutagen import MutagenError
from device_registry import DEFAULT_DEVICE_REGISTRY
from iPodDB.ArtworkDB.parser.parse_ArtworkDB import parse_ArtworkDB
from iPodDB.ArtworkDB.shared.chunk_defs.mhif import MhifHeader
from iPodDB.library import (ArtworkAsset, ArtworkPixels, CoverFormat, CoverPixelFormat, FileDependency, SourceFile,
                            WriteChecksum, WriteResources, WriteTarget, content_chunks, content_sha256)
from library import MAX_DB, STAGE, fingerprint, inside, prepared_database, write_file
from music_import import picture_data, thumbnail

ART_DIR = "iPod_Control/Artwork"
ART_DB = ART_DIR + "/ArtworkDB"
SHARD = re.compile(r"iPod_Control/Artwork/F[0-9]+_[0-9]+\.ithmb", re.I)
CLASSIC = DEFAULT_DEVICE_REGISTRY.profile_for_model_number("MC293").capabilities.artwork
FORMATS = tuple(CoverFormat(f.format_id, f.width, f.height, f.row_bytes, CoverPixelFormat(f.pixel_format.value))
                for f in CLASSIC.cover_formats)


def load_cover(root, read):
    """Decode one stored thumbnail from its planned location on the device."""
    with inside(root, read.relative_path).open("rb") as stream:
        stream.seek(read.offset)
        pixels = read.decode(stream.read(read.length))
    return Image.frombytes("RGB", (pixels.width, pixels.height), pixels.rgb888)


def device_covers(device):
    """Covers already stored on the device, keyed by artwork id. Read-only and best effort.

    Returns the small pictures, read now, and the locations of the large ones for reading on demand.
    """
    small, large, shards = {}, {}, {}
    try:
        index = inside(device.root, ART_DB)
        if not index.is_file() or not 0 < index.stat().st_size <= MAX_DB:
            return small, large
        library = device.library.with_artwork(index.read_bytes())
        for artwork_id in {t.artwork_id for t in library.snapshot.tracks}:
            read = library.artwork_read(artwork_id, FORMATS, 128)
            if not read:
                continue
            try:
                if read.relative_path not in shards:
                    shards[read.relative_path] = inside(device.root, read.relative_path).open("rb")
                stream = shards[read.relative_path]
                stream.seek(read.offset)
                pixels = read.decode(stream.read(read.length))
                small[artwork_id] = Image.frombytes("RGB", (pixels.width, pixels.height), pixels.rgb888)
            except (OSError, ValueError):
                continue
            sharp = library.artwork_read(artwork_id, FORMATS, 320)
            if sharp and sharp != read:
                large[artwork_id] = sharp
    except Exception:
        pass
    finally:
        for stream in shards.values():
            stream.close()
    return small, large


def embedded_pixels(path):
    """Use the front picture when tagged, and reduce it to the largest native display size."""
    try:
        for data in picture_data(path):
            try:
                rgb = thumbnail(data, 320)
                return ArtworkPixels(rgb.width, rgb.height, rgb.tobytes())
            except (OSError, ValueError, Image.DecompressionBombError):
                continue
    except (MutagenError, FileNotFoundError):
        pass
    return None


class _Buffer:
    """The engine can read verified thumbnail prefixes without holding whole shards in RAM."""

    def __init__(self, stack):
        self.stream = stack.enter_context(tempfile.TemporaryFile())
        self.size = 0
        self.digest = hashlib.sha256()

    def __len__(self):
        return self.size

    @property
    def sha256(self):
        return self.digest.hexdigest()

    def read_at(self, offset, length):
        self.stream.seek(offset)
        return self.stream.read(length)

    def append(self, data):
        self.stream.seek(self.size)
        self.stream.write(data)
        self.digest.update(data)
        self.size += len(data)

    def finish(self):
        return self


@dataclass
class CoverWrite:
    database: bytes
    old_index: bytes | None
    files: dict
    records: list
    count: int


def prepare_covers(device, additions, desired, paths, stack, report):
    if not paths:
        return None, 0
    prefetched = {}
    if additions:
        # A coverless import can proceed even when an unrelated artwork index is unreadable.
        for key, path in paths.items():
            prefetched[key] = embedded_pixels(path)
            if prefetched[key] is not None:
                break
        else:
            return None, len(paths)
    index = inside(device.root, ART_DB)
    original = None
    library = device.library
    if index.exists():
        if not 0 < index.stat().st_size <= MAX_DB:
            raise ValueError("现有 ArtworkDB 的大小无效。")
        original = index.read_bytes()
        library = library.with_artwork(original)
    existing = {t.track_id: t.artwork_id for t in library.snapshot.tracks}
    tracks, assets, ids = [], {}, {}
    updated = missing = 0
    for track in desired.tracks:
        artwork_id = existing.get(track.track_id, 0)
        if track.track_id in paths and (original is None or not artwork_id):
            report(f"读取内嵌封面 · {track.title}")
            pixels = (prefetched[track.track_id] if track.track_id in prefetched
                      else embedded_pixels(paths[track.track_id]))
            if pixels is None:
                missing += 1
            else:
                if pixels not in ids:
                    key = -(len(ids) + 1)
                    ids[pixels] = key
                    assets[key] = ArtworkAsset(key, pixels)
                artwork_id = ids[pixels]
                updated += 1
        tracks.append(replace(track, artwork_id=artwork_id) if track.artwork_id != artwork_id else track)
    if not updated:
        return None, missing

    report(f"生成 {updated} 首歌曲的原厂封面缩略图…")
    formats = FORMATS
    if original:
        declared = {row.chunk.header.format_id for row in parse_ArtworkDB(original).find_chunks(MhifHeader)}
        if declared:
            formats = tuple(f for f in FORMATS if f.format_id in declared)
            if not formats:
                raise ValueError("设备封面库使用的格式与 iPod classic 不匹配。")
    target = WriteTarget(checksum=WriteChecksum.HASH58, firewire_guid=device.guid, cover_formats=formats,
                         artwork_root_value=CLASSIC.artwork_root_value, supports_sparse_artwork=CLASSIC.supports_sparse_artwork)
    if Path(tempfile.gettempdir()).resolve().is_relative_to(device.root):
        raise ValueError("处理封面需要电脑上的临时目录。")
    names = {name.casefold(): name for name in library.artwork_file_paths()}
    if index.parent.exists():
        for path in index.parent.glob("*.ithmb"):
            name = path.relative_to(device.root).as_posix()
            names.setdefault(name.casefold(), name)
    inventory = {}
    sources = []
    for name in sorted(names.values()):
        file = inside(device.root, name)
        if not file.is_file():
            if file.exists():
                raise ValueError(f"封面路径与已有目录冲突：{name}")
            continue
        buf = _Buffer(stack)
        with file.open("rb") as reader:
            while chunk := reader.read(1024 * 1024):
                buf.append(chunk)
        dep = FileDependency(name, len(buf), buf.sha256)
        inventory[name.casefold()] = dep
        sources.append(SourceFile(dep, buf))
    resources = WriteResources(artwork=tuple(assets.values()), files=tuple(sources),
                               file_inventory=tuple(inventory.values()), create_file_buffer=lambda: _Buffer(stack))
    result = prepared_database(replace(device, library=library), additions, replace(desired, tracks=tuple(tracks)),
                               target=target, resources=resources)
    if result.artwork is None or any(t.artwork_id and out.artwork_id <= 0
                                     for t, out in zip(tracks, result.snapshot.tracks, strict=True)):
        raise ValueError("设备曲库没有关联生成的封面。")
    files = {f.relative_path: f.data for f in result.artwork_files}
    if any(not SHARD.fullmatch(path) for path in files):
        raise ValueError("曲库引擎返回了无效的封面文件名。")
    files[ART_DB] = result.artwork  # Publish the index after all of its thumbnails.
    if original is not None:
        inventory[ART_DB.casefold()] = FileDependency(ART_DB, len(original), content_sha256(original))
    records = []
    for path, data in files.items():
        old = inventory.get(path.casefold())
        records.append([path, old.size if old else 0, old.sha256 if old else None, content_sha256(data)])
    return CoverWrite(result.itunes, original, files, records, updated), missing


def publish_covers(device, covers):
    part = inside(device.root, STAGE + "/cover.part")
    inside(device.root, ART_DIR).mkdir(exist_ok=True)
    for name, _, old, new in covers.records:
        path = inside(device.root, name)
        with part.open("xb") as out:
            for chunk in content_chunks(covers.files[name]):
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if fingerprint(part) != new or (fingerprint(path) if path.exists() else None) != old:
            raise OSError(f"封面文件在写入前未通过校验：{path.name}")
        if old is None:
            part.rename(path)
        else:
            os.replace(part, path)


def settle_covers(device, records, backup_dir, committed):
    part = inside(device.root, STAGE + "/cover.part")
    # On rollback restore the old index before retiring any of its successor's thumbnails.
    for name, size, old, new in reversed(records):
        if name != ART_DB and not SHARD.fullmatch(name):
            raise ValueError("封面恢复记录包含无效路径。")
        path = inside(device.root, name)
        actual = fingerprint(path) if path.is_file() else None
        if actual == (new if committed else old):
            continue
        if committed or actual != new:
            raise OSError(f"封面文件已变化，保留恢复记录：{path.name}")
        if old is None:
            path.unlink()
        elif name == ART_DB:
            data = (backup_dir / ("ArtworkDB-" + old)).read_bytes()
            if content_sha256(data) != old:
                raise OSError("电脑上的原封面索引备份未通过校验。")
            part.unlink(missing_ok=True)
            write_file(part, data)
            if fingerprint(part) != old:
                raise OSError("恢复封面索引时校验失败。")
            os.replace(part, path)
        else:
            # The library engine verifies that every changed existing shard retains its exact prefix.
            digest = hashlib.sha256()
            with path.open("r+b") as stream:
                remaining = size
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise OSError("原有封面数据长度不足。")
                    digest.update(chunk)
                    remaining -= len(chunk)
                if digest.hexdigest() != old:
                    raise OSError("原有封面数据已变化，已保留。")
                stream.truncate(size)
                stream.flush()
                os.fsync(stream.fileno())
        if (fingerprint(path) if path.exists() else None) != old:
            raise OSError(f"封面恢复后校验失败：{path.name}")
