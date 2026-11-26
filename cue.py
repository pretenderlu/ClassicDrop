"""Parse CUE sheets for single-file and multi-file lossless rips."""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
import re

import system

FRAMES_PER_SECOND = 75
MAX_CUE_BYTES = 2 * 1024 * 1024

_FILE_RE = re.compile(r'^FILE\s+(?:"([^"]+)"|(\S+))\s+\S+', re.I)
_TRACK_RE = re.compile(r'^TRACK\s+(\d{1,2})\s+(\S+)', re.I)
_INDEX_RE = re.compile(r'^INDEX\s+(\d{1,2})\s+(\d+):(\d{1,2}):(\d{1,2})\b', re.I)
_REM_RE = re.compile(r'^REM\s+(\S+)\s+(.*)$', re.I)
_FIELD_RE = re.compile(r'^(TITLE|PERFORMER)\s+(.*)$', re.I)


@dataclass(frozen=True)
class CueKey:
    """Stable key for one track inside a CUE sheet (distinct from plain Path keys)."""

    cue_path: Path
    track_number: int

    def __str__(self):
        return f"{self.cue_path}#T{self.track_number:02d}"


@dataclass(frozen=True)
class CueSlice:
    """One audio track extracted from a CUE sheet and bound to its resolved image audio file."""

    cue_path: Path
    cue_stamp: tuple
    audio_path: Path
    track_number: int
    total_tracks: int
    start_frames: int
    end_frames: int | None
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    date: str = ""
    genre: str = ""
    disc_number: str = ""

    @property
    def key(self):
        return CueKey(self.cue_path, self.track_number)

    @property
    def start_seconds(self):
        return self.start_frames / FRAMES_PER_SECOND

    def duration_seconds(self, total_length):
        end = self.end_frames / FRAMES_PER_SECOND if self.end_frames is not None else total_length
        return end - self.start_seconds

    def sample_range(self, sample_rate):
        start = round(self.start_frames * sample_rate / FRAMES_PER_SECOND)
        end = round(self.end_frames * sample_rate / FRAMES_PER_SECOND) if self.end_frames is not None else None
        return start, end


def _decode_cue(raw):
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    for codec in ("utf-8", "gb18030", "big5", "cp932", "latin1"):
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _unquote(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].replace('""', '"')
    return text.strip()


def _parse_frames(minutes, seconds, frames):
    m, s, f = int(minutes), int(seconds), int(frames)
    if s >= 60 or f >= FRAMES_PER_SECOND:
        raise ValueError(f"CUE 时间戳格式无效：{minutes}:{seconds}:{frames}")
    return (m * 60 + s) * FRAMES_PER_SECOND + f


def _safe_filename(file_ref):
    ref = file_ref.strip()
    if not ref:
        return ""
    # Use only the basename so a CUE sheet can never point outside its own directory.
    win_parts = PureWindowsPath(ref).parts
    posix_parts = PurePosixPath(ref).parts
    if ".." in win_parts or ".." in posix_parts:
        raise ValueError(f"CUE 引用的音频路径无效：{file_ref}")
    return PureWindowsPath(ref.replace("/", "\\")).name


def _resolve_audio(cue_dir, file_ref, audio_suffixes, single_file_cue):
    target_name = _safe_filename(file_ref)
    if not target_name:
        raise ValueError("CUE 中的 FILE 文件名为空。")
    candidates = [
        p for p in cue_dir.iterdir()
        if p.is_file() and not system.junk_file(p.name) and p.suffix.lower() in audio_suffixes
    ]
    exact = next((p for p in candidates if p.name.casefold() == target_name.casefold()), None)
    if exact is not None:
        return exact.resolve(strict=True)
    # Rips commonly reference "Album.wav" in the CUE while shipping "Album.flac" / ".ape" / ".wv".
    stem = Path(target_name).stem.casefold()
    same_stem = [p for p in candidates if p.stem.casefold() == stem]
    if len(same_stem) == 1:
        return same_stem[0].resolve(strict=True)
    # Fall back to the sole audio file in the folder for single-image CUE sheets renamed by users.
    if single_file_cue and len(candidates) == 1:
        return candidates[0].resolve(strict=True)
    raise ValueError(f"找不到 CUE 指向的整轨音频：{target_name}")


def parse_cue(path, audio_suffixes):
    """Parse a CUE sheet and return resolved CueSlice entries using seamless INDEX 01 boundaries."""
    cue_path = Path(path).resolve(strict=True)
    stat = cue_path.stat()
    if not 0 < stat.st_size <= MAX_CUE_BYTES:
        raise ValueError("CUE 文件为空或过大。")
    cue_stamp = (stat.st_size, stat.st_mtime_ns)
    text = _decode_cue(cue_path.read_bytes())

    album = album_artist = album_date = album_genre = disc = total_discs = ""
    current_file = None
    seen_files = []
    raw_tracks = []
    current = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _FILE_RE.match(line)
        if match:
            current_file = (match.group(1) or match.group(2) or "").strip()
            if current_file and current_file not in seen_files:
                seen_files.append(current_file)
            continue
        match = _TRACK_RE.match(line)
        if match:
            current = {
                "number": int(match.group(1)),
                "mode": match.group(2).upper(),
                "file": current_file,
                "title": "",
                "artist": "",
                "date": "",
                "genre": "",
                "index00": None,
                "index01": None,
            }
            raw_tracks.append(current)
            continue
        match = _INDEX_RE.match(line)
        if match and current is not None:
            idx = int(match.group(1))
            frames = _parse_frames(match.group(2), match.group(3), match.group(4))
            if idx == 1 and current["index01"] is None:
                current["index01"] = frames
            elif idx == 0 and current["index00"] is None:
                current["index00"] = frames
            continue
        match = _REM_RE.match(line)
        if match:
            key, val = match.group(1).upper(), _unquote(match.group(2))
            if not val:
                continue
            if key == "DATE":
                if current is None:
                    album_date = album_date or val
                else:
                    current["date"] = current["date"] or val
            elif key == "GENRE":
                if current is None:
                    album_genre = album_genre or val
                else:
                    current["genre"] = current["genre"] or val
            elif key == "DISCNUMBER" and current is None:
                disc = disc or val
            elif key == "TOTALDISCS" and current is None:
                total_discs = total_discs or val
            continue
        match = _FIELD_RE.match(line)
        if match:
            key, val = match.group(1).upper(), _unquote(match.group(2))
            if current is None:
                if key == "TITLE":
                    album = album or val
                else:
                    album_artist = album_artist or val
            else:
                if key == "TITLE":
                    current["title"] = current["title"] or val
                else:
                    current["artist"] = current["artist"] or val

    audio_tracks = [t for t in raw_tracks if t["mode"] == "AUDIO"]
    if not audio_tracks:
        raise ValueError("CUE 文件中没有音频曲目。")

    single_file_cue = len(seen_files) == 1
    resolved_cache = {}
    disc_number = f"{disc}/{total_discs}" if disc and total_discs and "/" not in disc else disc

    # Group consecutive tracks sharing the same FILE so each slice runs from INDEX 01 to next INDEX 01.
    slices = []
    total = len(audio_tracks)
    used_numbers = set()
    for i, track in enumerate(audio_tracks):
        file_ref = track["file"]
        if not file_ref:
            raise ValueError(f"第 {track['number']} 轨缺少对应的 FILE 音频声明。")
        if file_ref not in resolved_cache:
            resolved_cache[file_ref] = _resolve_audio(cue_path.parent, file_ref, audio_suffixes, single_file_cue)
        audio_path = resolved_cache[file_ref]

        start = track["index01"] if track["index01"] is not None else track["index00"]
        if start is None:
            raise ValueError(f"第 {track['number']} 轨缺少 INDEX 时间点。")

        end = None
        if i + 1 < total and audio_tracks[i + 1]["file"] == file_ref:
            nxt = audio_tracks[i + 1]
            end = nxt["index01"] if nxt["index01"] is not None else nxt["index00"]
            if end is None:
                raise ValueError(f"第 {nxt['number']} 轨缺少 INDEX 时间点。")
            if end <= start:
                raise ValueError(f"第 {track['number']} 轨的时间范围无效。")

        number = track["number"]
        if number <= 0 or number in used_numbers:
            raise ValueError(f"CUE 中的音轨编号无效或重复：{number}")
        used_numbers.add(number)

        slices.append(CueSlice(
            cue_path=cue_path,
            cue_stamp=cue_stamp,
            audio_path=audio_path,
            track_number=number,
            total_tracks=total,
            start_frames=start,
            end_frames=end,
            title=track["title"],
            artist=track["artist"] or album_artist,
            album=album,
            album_artist=album_artist,
            date=track["date"] or album_date,
            genre=track["genre"] or album_genre,
            disc_number=disc_number,
        ))
    return tuple(slices)
