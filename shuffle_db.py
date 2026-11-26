"""Read and write iPod shuffle iTunesSD databases (v1 for 1st/2nd gen, v2 bdhs for 3rd/4th gen).

Neither iTunesSD version uses cryptographic signing. ClassicDrop keeps a plain unsigned
iTunesDB alongside iTunesSD so full track metadata and playlists remain browsable on reload.
"""

from pathlib import Path
import re
import struct

from iPodDB.library import MediaType

SD = "iPod_Control/iTunes/iTunesSD"
MAX_SD = 32 * 1024 * 1024

# Apple USB product IDs for the four iPod shuffle generations.
SHUFFLE_PIDS = {
    "1300": ("1st Gen", 1),
    "1301": ("2nd Gen", 1),
    "1302": ("3rd Gen", 2),
    "1303": ("4th Gen", 2),
}

_V1_HEADER_SIZE = 18
_V1_ENTRY_SIZE = 558
_V2_HEADER_SIZE = 64
_V2_TRACK_SIZE = 0x174


def shuffle_pid_info(usb_instance):
    """Return (generation, sd_version) when usb_instance names an Apple iPod shuffle, else None."""
    if not isinstance(usb_instance, str):
        return None
    match = re.fullmatch(r"USB\\VID_05AC&PID_([0-9A-F]{4})\\[^\\]+", usb_instance, re.I)
    if not match:
        return None
    return SHUFFLE_PIDS.get(match[1].upper())


def sd_version_from_bytes(data):
    """Return 2 for bdhs (3G/4G), 1 for 18-byte big-endian v1 (1G/2G), or None."""
    if len(data) >= _V2_HEADER_SIZE and data[:4] == b"bdhs":
        return 2
    if len(data) >= _V1_HEADER_SIZE and data[6:9] == b"\x00\x00\x12" and (len(data) - _V1_HEADER_SIZE) % _V1_ENTRY_SIZE == 0:
        return 1
    return None


def detect_shuffle(root, usb_instance=None):
    """Identify an iPod shuffle from USB PID or an existing iTunesSD file.

    Returns (generation, sd_version) where generation may be '' when only iTunesSD is present.
    """
    hw = shuffle_pid_info(usb_instance)
    if hw is not None:
        return hw
    sd_path = Path(root) / SD
    try:
        if sd_path.is_file() and 0 < sd_path.stat().st_size <= MAX_SD:
            version = sd_version_from_bytes(sd_path.read_bytes())
            if version is not None:
                return "", version
    except OSError:
        pass
    return None


def _filetype_code(location, file_format=""):
    suffix = Path(location).suffix.lower()
    if suffix in (".m4a", ".m4b", ".m4p", ".mp4", ".aac"):
        return 2
    if suffix in (".wav", ".wave", ".aif", ".aiff", ".aifc"):
        return 4
    if suffix == ".mp3":
        return 1
    fmt = (file_format or "").lower()
    if any(tok in fmt for tok in ("aac", "alac", "lossless", "mp4", "m4a")):
        return 2
    if "wav" in fmt or "aiff" in fmt:
        return 4
    return 1


def _sd_path(location):
    rel = location.replace(":", "/").replace("\\", "/").lstrip("/")
    return "/" + rel


def _u24be(value):
    clamped = max(0, min(0xFFFFFF, int(value)))
    return clamped.to_bytes(3, "big")


def _is_nonstandard(track):
    return bool(track.metadata.podcast or MediaType.PODCAST in track.media_types
                or MediaType.AUDIOBOOK in track.media_types)


def build_itunessd_v1(tracks):
    """Build a big-endian iTunesSD v1 database for iPod shuffle 1st/2nd generation."""
    out = bytearray()
    out += _u24be(len(tracks))
    out += _u24be(0x010600)
    out += _u24be(_V1_HEADER_SIZE)
    out += b"\x00" * 9

    for track in tracks:
        location = track.metadata.location
        if not location:
            continue
        path_bytes = _sd_path(location).encode("utf-16-le")[:522]
        path_padded = path_bytes.ljust(522, b"\x00")
        nonstd = _is_nonstandard(track)
        entry = bytearray()
        entry += _u24be(_V1_ENTRY_SIZE)
        entry += _u24be(0x5AA501)
        entry += _u24be(0)          # start time / 256
        entry += _u24be(0)
        entry += _u24be(0)
        entry += _u24be(0)          # stop time / 256
        entry += _u24be(0)
        entry += _u24be(0)
        entry += _u24be(100)        # neutral volume gain (0..200)
        entry += _u24be(_filetype_code(location, track.metadata.file_format))
        entry += _u24be(0x000200)
        entry += path_padded        # 522 bytes UTF-16LE
        entry.append(0 if nonstd else 1)  # shuffle flag (1 = play in shuffle)
        entry.append(1 if nonstd else 0)  # bookmark flag
        entry.append(0)
        assert len(entry) == _V1_ENTRY_SIZE
        out += entry
    # Patch exact written count in case any track had no location.
    written = (len(out) - _V1_HEADER_SIZE) // _V1_ENTRY_SIZE
    out[0:3] = _u24be(written)
    return bytes(out)


def build_itunessd_v2(snapshot, *, voiceover=False):
    """Build a little-endian bdhs iTunesSD v2 database for iPod shuffle 3rd/4th generation."""
    tracks = [t for t in snapshot.tracks if t.metadata.location]
    track_count = len(tracks)
    nonstd_total = sum(1 for t in tracks if _is_nonstandard(t))
    track_index = {t.track_id: idx for idx, t in enumerate(tracks)}

    # Assign stable positive album/artist group IDs used by shuffle VoiceOver / album grouping.
    album_ids = {}
    artist_ids = {}
    for t in tracks:
        album_ids.setdefault(t.album.casefold(), len(album_ids) + 1)
        artist_ids.setdefault(t.artist.casefold(), len(artist_ids) + 1)

    hths_header_len = 20 + 4 * track_count
    track_base = _V2_HEADER_SIZE + hths_header_len
    offsets = []
    track_chunks = bytearray()
    for t in tracks:
        offsets.append(track_base + len(track_chunks))
        raw_path = _sd_path(t.metadata.location).encode("utf-8")[:256].ljust(256, b"\x00")
        nonstd = _is_nonstandard(t)
        db_id = (t.ipod.db_track_id if t.ipod and t.ipod.db_track_id else t.track_id) & ((1 << 64) - 1)
        rths = struct.pack(
            "<4sIIIII256sIBBBBIIIIIIIHHQQI32s",
            b"rths",
            _V2_TRACK_SIZE,
            0,                                                     # start_ms
            0,                                                     # stop_ms
            0,                                                     # volume_gain
            _filetype_code(t.metadata.location, t.metadata.file_format),
            raw_path,
            0,                                                     # bookmark_ms
            0 if nonstd else 1,                                    # dontskip (1 = include in shuffle)
            1 if nonstd else 0,                                    # remember playback position
            0,                                                     # uninterruptible album
            0,
            0x200,                                                 # pregap
            0x200,                                                 # postgap
            0,                                                     # numsamples
            0,
            0,                                                     # gapless_data
            0,
            album_ids[t.album.casefold()],
            max(0, min(0xFFFF, t.track_number)),
            max(0, min(0xFFFF, t.metadata.disc_number)),
            0,                                                     # 8 bytes unknown
            db_id,                                                 # 8 bytes VoiceOver / dbid
            artist_ids[t.artist.casefold()],
            b"\x00" * 32,
        )
        assert len(rths) == _V2_TRACK_SIZE
        track_chunks += rths

    hths = bytearray(struct.pack("<4sIIQ", b"hths", hths_header_len, track_count, 0))
    for off in offsets:
        hths += struct.pack("<I", off)
    hths += track_chunks

    # Assemble playlists: Master playlist (stype=1) followed by non-empty user/podcast/audiobook playlists.
    pl_specs = []
    if tracks:
        pl_specs.append((1, 0, list(range(track_count))))
    for pl in snapshot.playlists:
        members = [track_index[e.track_id] for e in pl.entries if e.track_id in track_index]
        if not members:
            continue
        if pl.kind.name == "PODCAST" or getattr(pl, "podcast", False):
            stype = 3
        else:
            stype = 2
        pl_id = (getattr(pl, "persistent_id", None) or pl.playlist_id) & ((1 << 64) - 1)
        pl_specs.append((stype, pl_id, members))

    pl_count = len(pl_specs)
    master_cnt = sum(1 for stype, _, _ in pl_specs if stype == 1)
    podcast_cnt = sum(1 for stype, _, _ in pl_specs if stype == 3)
    audiobook_cnt = sum(1 for stype, _, _ in pl_specs if stype == 4)

    playlist_header_offset = _V2_HEADER_SIZE + len(hths)
    hphs_header_len = 20 + 4 * pl_count
    pl_base = playlist_header_offset + hphs_header_len
    pl_offsets = []
    pl_chunks = bytearray()
    for stype, pl_id, members in pl_specs:
        pl_offsets.append(pl_base + len(pl_chunks))
        nonstd_pl = sum(1 for idx in members if _is_nonstandard(tracks[idx]))
        lphs_len = 44 + 4 * len(members)
        lphs = bytearray(struct.pack(
            "<4sIIIQI16s",
            b"lphs",
            lphs_len,
            len(members),
            len(members) - nonstd_pl,
            0 if stype == 1 else pl_id,
            stype,
            b"\x00" * 16,
        ))
        for idx in members:
            lphs += struct.pack("<I", idx)
        pl_chunks += lphs

    hphs = bytearray(struct.pack(
        "<4sIHHHHHH",
        b"hphs",
        hphs_header_len,
        pl_count,
        0,
        pl_count - podcast_cnt,
        master_cnt,
        pl_count - audiobook_cnt,
        0,
    ))
    for off in pl_offsets:
        hphs += struct.pack("<I", off)
    hphs += pl_chunks

    bdhs = struct.pack(
        "<4sIIIIQBBHIII20s",
        b"bdhs",
        0x02000003,
        _V2_HEADER_SIZE,
        track_count,
        pl_count,
        0,
        0,                                      # max_volume
        1 if voiceover else 0,                  # voiceover_enabled
        0,
        track_count - nonstd_total,
        _V2_HEADER_SIZE,
        playlist_header_offset,
        b"\x00" * 20,
    )
    assert len(bdhs) == _V2_HEADER_SIZE
    return bytes(bdhs + hths + hphs)


def build_itunessd(snapshot, version):
    if version == 1:
        return build_itunessd_v1(snapshot.tracks)
    if version == 2:
        return build_itunessd_v2(snapshot)
    raise ValueError(f"不支持的 iTunesSD 版本：{version}")


def parse_itunessd(data):
    """Parse an iTunesSD v1 or v2 binary and return relative track paths (without leading slash)."""
    version = sd_version_from_bytes(data)
    if version == 1:
        count = int.from_bytes(data[0:3], "big")
        paths = []
        pos = _V1_HEADER_SIZE
        for _ in range(count):
            if pos + _V1_ENTRY_SIZE > len(data):
                break
            raw = data[pos + 33:pos + 33 + 522]
            text = raw.decode("utf-16-le", "ignore").split("\x00", 1)[0].lstrip("/")
            if text:
                paths.append(text)
            pos += _V1_ENTRY_SIZE
        return tuple(paths)
    if version == 2:
        track_header_off = struct.unpack_from("<I", data, 36)[0]
        if track_header_off + 20 > len(data) or data[track_header_off:track_header_off + 4] != b"hths":
            raise ValueError("iTunesSD 曲目表头无效。")
        count = struct.unpack_from("<I", data, track_header_off + 8)[0]
        paths = []
        for i in range(count):
            off_pos = track_header_off + 20 + 4 * i
            if off_pos + 4 > len(data):
                break
            rec = struct.unpack_from("<I", data, off_pos)[0]
            if rec + 0x118 > len(data) or data[rec:rec + 4] != b"rths":
                continue
            raw = data[rec + 24:rec + 24 + 256]
            text = raw.split(b"\x00", 1)[0].decode("utf-8", "replace").lstrip("/")
            if text:
                paths.append(text)
        return tuple(paths)
    raise ValueError("无法识别的 iTunesSD 格式。")
