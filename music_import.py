"""Read an import batch without writing to either the source library or the iPod."""

import io
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps
from mutagen import MutagenError
from mutagen.aiff import AIFF
from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.wave import WAVE

from audio_convert import ASK_FORMAT, READERS, lossless_profile, needs_48k_alac, riff_tags, source_audio, source_tags, tag_text

FIELDS = ("title", "artist", "album")
PICTURES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
GENERIC_FOLDERS = {"music", "my music", "audio", "downloads", "desktop", "documents", "albums", "flac", "wav", "dsd", "dsf", "dff",
                   "mp3", "lossless", "音乐", "音樂", "下载", "下載", "歌曲", "专辑", "專輯", "无损音乐", "无损", "無損"}
DISC = re.compile(r"(?:cd|disc|disk|碟|碟片|光盘|光盤)[\s._-]*0*(?:\d+|[ivx]+)(?:\s*[-–—].*)?", re.I)
YEAR = re.compile(r"^(?:\[(?:19|20)\d{2}\]|【(?:19|20)\d{2}】|(?:19|20)\d{2}(?=\s|[._–—-]))[\s._–—-]*")


def _stamp(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def picture_data(path, audio=None):
    suffix = path.suffix.lower()
    if suffix in (".m4a", ".m4p", ".mp4"):
        yield from MP4(path).get("covr", ())
    elif suffix == ".flac":
        pictures = (audio if audio is not None else source_audio(path)).pictures
        yield from (p.data for p in sorted(pictures, key=lambda p: p.type != 3))
    elif suffix in (".mp3", ".dsf", ".dff", ".wav", ".wave", ".aif", ".aiff", ".aifc"):
        if suffix == ".mp3":
            tags = ID3(path)
        elif suffix in (".dsf", ".dff"):
            tags = (audio if audio is not None else source_audio(path)).tags
        else:
            audio = audio if audio is not None else (WAVE if suffix in (".wav", ".wave") else AIFF)(path)
            tags = audio.tags
        if tags:
            yield from (p.data for p in sorted(tags.getall("APIC"), key=lambda p: p.type != 3))


def _open_image(data):
    image = Image.open(io.BytesIO(data))
    if image.width * image.height > 32 * 1024 * 1024:
        image.close()
        raise ValueError("封面超过 3200 万像素。")
    return image


def thumbnail(data, side):
    with _open_image(data) as image:
        image.thumbnail((side, side), Image.Resampling.LANCZOS)
        return ImageOps.exif_transpose(image).convert("RGB")


@dataclass(frozen=True)
class Cover:
    path: Path
    index: int  # -1 for an external image, otherwise its position among the embedded covers
    stamp: tuple

    @property
    def label(self):
        return "内嵌封面" if self.index >= 0 else self.path.name

    def read(self):
        if _stamp(self.path) != self.stamp:
            raise ValueError(f"封面来源已变化，请重新选择：{self.path.name}")
        if self.index == -1:
            if self.stamp[0] > 64 * 1024 * 1024:
                raise ValueError("封面文件超过 64 MB。")
            return self.path.read_bytes()
        for index, data in enumerate(picture_data(self.path)):
            if index == self.index:
                return data
        raise ValueError(f"无法再读取这张内嵌封面：{self.path.name}")

    @classmethod
    def from_file(cls, path):
        path = Path(path).resolve(strict=True)
        cover = cls(path, -1, _stamp(path))
        thumbnail(cover.read(), 100)
        return cover

    def for_tag(self):
        data = self.read()
        with _open_image(data) as image:
            if image.format in ("JPEG", "PNG"):
                return bytes(data), "image/jpeg" if image.format == "JPEG" else "image/png"
            output = io.BytesIO()
            ImageOps.exif_transpose(image).convert("RGB").save(output, format="PNG")
            return output.getvalue(), "image/png"


def _embedded(path, audio, stamp):
    try:
        for index, data in enumerate(picture_data(path, audio)):
            try:
                thumbnail(data, 100)
                return Cover(path, index, stamp)
            except (OSError, ValueError, Image.DecompressionBombError):
                continue
    except MutagenError:
        pass
    return None


def _folder_tags(path):
    directory = path.parent
    while directory != directory.parent and (DISC.fullmatch(directory.name.strip())
                                             or directory.name.casefold() == directory.parent.name.casefold()):
        directory = directory.parent
    name = YEAR.sub("", directory.name).strip()
    artist, album = directory.parent.name, name
    parts = re.split(r"\s+[-–—]\s+|\s*—\s*|(?<=[\u3400-\u9fff])[-–](?=[\u3400-\u9fff])", name, maxsplit=1)
    artist_level = (artist.casefold() not in GENERIC_FOLDERS
                    and (directory.parent.parent.name.casefold() in GENERIC_FOLDERS or name != directory.name.strip()))
    if len(parts) == 2 and all(parts) and (not artist_level or parts[0].casefold() == artist.casefold()):
        artist, album = parts[0], YEAR.sub("", parts[1])
    elif not artist or artist.casefold() in GENERIC_FOLDERS or directory.parent == Path.home():
        artist = ""
    if not album or album.casefold() in GENERIC_FOLDERS or directory == Path.home():
        return directory, "", ""
    return directory, artist.strip(), album.strip()


def _folder_cover(folder):
    back = r"(?:^|[\W_])(?:back|rear|disc|cd)(?:$|[\W_\d])|封底"
    images = sorted((p for p in folder.iterdir() if p.suffix.lower() in PICTURES and p.is_file()
                     and not re.search(back, p.stem, re.I)),
                    key=lambda p: (p.name.casefold(), p.name))
    names = ("cover", "folder", "front", "封面", "专辑封面", "專輯封面", "albumart", "album", folder.name.casefold())
    def rank(path):
        name = path.stem.casefold()
        if name in names:
            return names.index(name)
        if re.search(r"(?:^|[\W_])(?:front|cover)(?:$|[\W_])", name) or "封面" in name or name.startswith("albumart"):
            return len(names)
        return len(names) + 1
    for path in sorted(images, key=rank):
        if rank(path) > len(names) and len(images) != 1:
            continue
        try:
            return Cover.from_file(path)
        except (OSError, ValueError, Image.DecompressionBombError):
            continue
    return None


@dataclass(frozen=True)
class ImportSong:
    path: Path
    stamp: tuple
    title: str
    artist: str
    album: str
    cover: Cover | None
    mode: str
    choices: tuple
    original_text: tuple
    original_cover: Cover | None

    @property
    def text_changes(self):
        return {key: getattr(self, key) for key, old in zip(FIELDS, self.original_text)
                if getattr(self, key) != old}

    @property
    def needs_copy(self):
        return bool(self.text_changes) or self.cover != self.original_cover

    def check_source(self):
        if _stamp(self.path) != self.stamp:
            raise ValueError(f"预览后歌曲已变化，请重新添加：{self.path.name}")

    def write_tags(self, dest, converted):
        """Only called for a prepared PC copy, never an original."""
        changes = dict(zip(FIELDS, (self.title, self.artist, self.album))) if converted else self.text_changes
        change_cover = self.cover != self.original_cover or (converted and self.cover is not None)
        cover_data, mime = self.cover.for_tag() if change_cover and self.cover else (None, "")
        if dest.suffix.lower() == ".m4a":
            audio = MP4(dest)
            if audio.tags is None:
                audio.add_tags()
            keys = {"title": "©nam", "artist": "©ART", "album": "©alb"}
            dirty = False
            for key, value in changes.items():
                atom = keys[key]
                if (audio.get(atom) or [""])[0] == value:
                    continue
                if value:
                    audio[atom] = [value]
                else:
                    audio.tags.pop(atom, None)
                dirty = True
            if change_cover and (audio.get("covr") or [None])[0] != cover_data:
                if cover_data is None:
                    audio.tags.pop("covr", None)
                else:
                    fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
                    audio["covr"] = [MP4Cover(cover_data, imageformat=fmt)]
                dirty = True
            if dirty:
                audio.save()
        else:
            audio = (WAVE if dest.suffix.lower() == ".wav" else MP3)(dest)
            if audio.tags is None:
                audio.add_tags()
            for key, value in changes.items():
                frame = {"title": TIT2, "artist": TPE1, "album": TALB}[key]
                audio.tags.delall(frame.__name__)
                if value:
                    audio.tags.add(frame(encoding=3, text=[value]))
            if change_cover:
                audio.tags.delall("APIC")
                if cover_data:
                    audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Front cover", data=cover_data))
            audio.save()
            if dest.suffix.lower() == ".wav" and changes:
                riff_tags(dest, changes)


def _read_song(path, folder_covers):
    stamp = _stamp(path)
    audio = source_audio(path)
    tags = source_tags(audio)
    original = tuple(tag_text(tags, k) for k in FIELDS)
    folder, artist, album = _folder_tags(path)
    embedded = _embedded(path, audio, stamp)
    cover = embedded
    directory = path.parent
    while cover is None:
        if directory not in folder_covers:
            try:
                folder_covers[directory] = _folder_cover(directory)
            except PermissionError:
                folder_covers[directory] = None
        cover = folder_covers[directory]
        if directory == folder:
            break
        directory = directory.parent
    suffix = path.suffix.lower()
    if needs_48k_alac(audio):
        choices = ("alac-48",)
    elif suffix in ASK_FORMAT:
        exact = lossless_profile(path, audio)
        choices = ("alac", "copy", "aac") if exact and suffix == ".wav" else (("alac" if exact else "alac-fit"), "aac")
    else:
        info = audio.info
        playable = (0 < info.sample_rate <= 48000 and info.channels in (1, 2)
                    and (info.layer == 3 if suffix == ".mp3" else info.codec.lower() in ("alac", "mp4a.40.2")))
        choices = ("copy", "aac") if playable else ("aac",)
    return ImportSong(path, stamp, original[0] or path.stem, original[1] or tag_text(tags, "albumartist") or artist,
                      original[2] or album, cover, choices[0], choices, original, embedded)


def scan_inputs(inputs, known, report, cancelled):
    songs, errors, seen, folder_covers = [], [], set(), {}
    def read(path):
        try:
            path = path.resolve(strict=True)
            if path in seen:
                return
            seen.add(path)
            if len(seen) == 1 or len(seen) % 10 == 0:
                report(f"正在读取第 {len(seen)} 首 · {path.name}")
            previous = known.get(path)
            songs.append(previous if previous and previous.stamp == _stamp(path) else _read_song(path, folder_covers))
        except (OSError, ValueError, MutagenError, Image.DecompressionBombError) as exc:
            errors.append(f"{path}　{exc}")
    def walk_error(exc):
        errors.append(str(exc))
    for path in map(Path, inputs):
        if cancelled():
            break
        if path.is_dir():
            for directory, subdirs, files in path.walk(on_error=walk_error):
                if cancelled():
                    return songs, errors
                subdirs[:] = sorted((d for d in subdirs if d.casefold() != "ipod_control"
                                     and not (directory / d).is_junction()), key=str.casefold)
                for name in sorted(files, key=lambda s: [int(v) if v.isdecimal() else v.casefold() for v in re.split(r"(\d+)", s)]):
                    if cancelled():
                        return songs, errors
                    file = directory / name
                    if file.suffix.lower() in READERS and file.is_file():
                        read(file)
        elif path.suffix.lower() in READERS:
            read(path)
        else:
            errors.append(f"{path}　不支持的音频格式")
    return songs, errors
