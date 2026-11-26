"""Prepare local audio for the stock firmware before opening the device for writing."""

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import wave
from contextlib import contextmanager
from pathlib import Path

from imageio_ffmpeg import get_ffmpeg_exe
from cue import CueKey
import diagnostics
import encoders
import system
from mutagen.aiff import AIFF
from mutagen.dsdiff import DSDIFF, DSDIFFFile
from mutagen.dsf import DSF, DSFFile
from mutagen.easymp4 import EasyMP4
from mutagen._riff import RiffFile
from mutagen.flac import FLAC, VCFLACDict
from mutagen._vorbis import VorbisEncodingError
from mutagen.monkeysaudio import MonkeysAudio
from mutagen.mp3 import EasyMP3, MP3
from mutagen.mp4 import Atoms, MP4, MP4Cover
from mutagen.trueaudio import TrueAudio
from mutagen.wave import WAVE
from mutagen.wavpack import WavPack

ASK_FORMAT = {".flac", ".wav", ".dsf", ".dff", ".ape", ".wv", ".tta", ".aif", ".aiff"}
LOSSLESS_CODECS = {".flac", ".ape", ".wv", ".tta", ".aif", ".aiff"}
DSD_FILES = (".dsf", ".dff")
MAX_ALAC_BITS = 24
MAX_ALAC_RATE = 48000
LABELS = {"copy": "原格式", "alac": "ALAC 无损", "alac-fit": "ALAC 兼容转换",
          "alac-48": f"ALAC {MAX_ALAC_BITS} 位 / {MAX_ALAC_RATE // 1000} kHz", "aac": "AAC-LC"}
TAG_KEYS = {"title": "TIT2", "artist": "TPE1", "album": "TALB", "albumartist": "TPE2",
            "date": "TDRC", "genre": "TCON", "tracknumber": "TRCK", "discnumber": "TPOS"}
APE_KEYS = {"title": ("title",), "artist": ("artist",), "album": ("album",),
            "albumartist": ("album artist", "albumartist"), "date": ("year", "date"),
            "genre": ("genre",), "tracknumber": ("track", "tracknumber"), "discnumber": ("disc", "discnumber")}
MP4_ATOMS = {"title": "©nam", "artist": "©ART", "album": "©alb", "albumartist": "aART",
             "date": "©day", "genre": "©gen", "tracknumber": "trkn", "discnumber": "disk"}
AAC_RATES = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000)
GENERIC_FOLDERS = {"music", "my music", "audio", "downloads", "desktop", "documents", "albums", "flac", "wav", "dsd", "dsf", "dff",
                   "mp3", "lossless", "various artists", "音乐", "音樂", "下载", "下載", "歌曲", "专辑", "專輯",
                   "无损音乐", "无损", "無損", "合集", "合辑", "群星"}
FLAC_PCM = {8: ("pcm_s8", "s8"), 16: ("pcm_s16le", "s16le"),
            24: ("pcm_s24le", "s24le"), 32: ("pcm_s32le", "s32le")}


def _decode_text(raw):
    for codec in ("utf-8-sig", "gb18030", "latin1"):
        try:
            return raw.decode(codec).strip()
        except UnicodeDecodeError:
            continue
    return ""


class _FlacComments(VCFLACDict):
    def load(self, data, errors="replace", framing=False):
        # Some older Chinese rips mix valid UTF-8 and GBK in the same comment block.
        start = data.tell()
        try:
            super().load(data, errors="surrogateescape", framing=framing)
        except VorbisEncodingError:
            self.clear()
            data.seek(start)
            super().load(data, errors=errors, framing=framing)
            return
        for i, (key, text) in enumerate(self):
            if re.search(r"[\udc80-\udcff]", text):
                raw = text.encode("utf-8", "surrogateescape")
                try:
                    text = raw.decode("gb18030")
                except UnicodeDecodeError:
                    text = raw.decode("utf-8", "replace")
                self[i:i + 1] = [(key, text)]


class _SourceFLAC(FLAC):
    METADATA_BLOCKS = FLAC.METADATA_BLOCKS.copy()
    METADATA_BLOCKS[VCFLACDict.code] = _FlacComments


def _read_dsf(path):
    with open(path, "rb") as stream:
        header = DSFFile(stream)
        fmt, data = header.fmt_chunk, header.data_chunk
        if not (fmt.sampling_frequency and fmt.channel_num and fmt.sample_count and fmt.bits_per_sample in (1, 8)):
            raise ValueError("DSF 的采样率、声道或采样参数无效。")
        size = os.fstat(stream.fileno()).st_size
        tag_start = header.dsd_chunk.offset_metdata_chunk
        audio_bytes = ((fmt.sample_count + 7) // 8) * fmt.channel_num
        if (audio_bytes > data.chunk_size - data.CHUNK_SIZE or tag_start > size
                or data.chunk_offset + data.chunk_size > (tag_start or size)):
            raise ValueError("DSF 音频数据不完整，或与标注的采样数不一致。")
        stream.seek(0)
        return DSF(stream)


def _read_dff(path):
    with open(path, "rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        audio = DSDIFF(stream)
        info = audio.info
        if not (info.sample_rate > 0 and info.channels > 0 and info.length > 0 and info.compression in ("DSD", "DST")):
            raise ValueError("DFF 的采样率、声道或压缩方式无效。")
        stream.seek(0)
        container = DSDIFFFile(stream)
        data = container["DSD" if info.compression == "DSD" else "DST"]
        if (container.root.offset + container.root.size > size or data.data_offset + data.data_size > size
                or (info.compression == "DSD" and data.data_size % info.channels)):
            raise ValueError("DFF 音频数据不完整，或与文件头标注的长度不一致。")
    audio.filename = str(path)
    return audio


def dff_comments(path):
    """Title and artist from the DSDIFF edited-master chunk, which SACD rips use in place of ID3."""
    result = {}
    with open(path, "rb") as stream:
        try:
            block = DSDIFFFile(stream)["DIIN"]
        except KeyError:
            return result
        raw = block.read() if block.data_size <= 1 << 20 else b""
    at = 0
    while at + 12 <= len(raw):
        kind, length = raw[at:at + 4], int.from_bytes(raw[at + 4:at + 12], "big")
        body = raw[at + 12:at + 12 + length]
        at += 12 + length + length % 2
        key = {b"DITI": "title", b"DIAR": "artist"}.get(kind)
        if not key or len(body) < 4:
            continue
        text = body[4:4 + int.from_bytes(body[:4], "big")].rstrip(b"\0")
        value = _decode_text(text)
        if value:
            result[key] = [value]
    return result


def _read_wave(path):
    audio = WAVE(path)
    info = audio.info
    if info.audio_format == wave.WAVE_FORMAT_EXTENSIBLE and info.bits_per_sample in (16, 24, 32):
        with open(path, "rb") as stream:
            fmt = RiffFile(stream)["fmt"]
            stream.seek(fmt.data_offset)
            header = stream.read(min(40, fmt.data_size))
        valid_bits = int.from_bytes(header[18:20], "little")
        if (header[24:40] == wave.KSDATAFORMAT_SUBTYPE_PCM and int.from_bytes(header[16:18], "little") >= 22
                and 0 < valid_bits < info.bits_per_sample):
            # FFmpeg can mistake left-aligned 24-bit integer PCM in a 32-bit WAV container for floating point.
            info.pcm_decoder = f"pcm_s{info.bits_per_sample}le"
            info.bits_per_sample = valid_bits
    return audio


def _descriptor_chain(data):
    i = 0
    while i < len(data):
        tag = data[i]
        i += 1
        length = 0
        while i < len(data):
            byte = data[i]
            i += 1
            length = (length << 7) | byte & 0x7F
            if not byte & 0x80:
                break
        yield tag, data[i:i + length]
        i += length


def _asc(esds):
    """AudioSpecificConfig bytes inside an esds element's descriptor chain, or None."""
    for tag, es in _descriptor_chain(esds):
        if tag != 0x03 or len(es) < 3:
            continue
        i = 3  # ES_ID (2 bytes) + flags
        if es[2] & 0x80:
            i += 2
        if es[2] & 0x40:
            if i >= len(es):
                continue
            i += 1 + es[i]
        if es[2] & 0x20:
            i += 2
        for tag, decoder in _descriptor_chain(es[i:]):
            if tag != 0x04 or len(decoder) < 13:
                continue
            for tag, info in _descriptor_chain(decoder[13:]):
                if tag == 0x05:
                    return info
    return None


def mp4_channels(path):
    """AAC channelConfiguration from the AudioSpecificConfig, or None when unreadable.

    Apple's encoders can leave the stsd channel count at 2 on a mono file, so the esds
    stream config is the only reliable channel count for AAC.
    """
    try:
        with open(path, "rb") as stream:
            for moov in Atoms(stream).atoms:
                if moov.name != b"moov":
                    continue
                todo = list(moov.children or ())
                while todo:
                    atom = todo.pop()
                    todo += atom.children or ()
                    if atom.name != b"stsd":
                        continue
                    data = atom.read(stream)[1]
                    i = data.find(b"esds")
                    if i >= 0:
                        info = _asc(data[i + 8:])  # box name + FullAtom version/flags
                        if info and len(info) >= 2:
                            count = info[1] >> 3 & 0x0F
                            return {7: 8}.get(count, count) or None
    except Exception:
        pass
    return None


def mp4_audio(path):
    """EasyMP4 whose .info.channels follows the AAC stream rather than the stsd entry."""
    audio = EasyMP4(path)
    if (audio.info.codec or "").startswith("mp4a"):
        audio.info.channels = mp4_channels(path) or audio.info.channels
    return audio


def _read_tta(path):
    audio = TrueAudio(path)
    with open(path, "rb") as stream:
        head = stream.read(10)
        offset = 0
        if head[:3] == b"ID3" and len(head) == 10:
            # Synchsafe ID3v2 size sits in bytes 6..10.
            size = ((head[6] & 0x7F) << 21) | ((head[7] & 0x7F) << 14) | ((head[8] & 0x7F) << 7) | (head[9] & 0x7F)
            offset = 10 + size
        stream.seek(offset)
        tta = stream.read(18)
    if len(tta) >= 10 and tta.startswith(b"TTA1"):
        audio.info.channels = int.from_bytes(tta[6:8], "little")
        audio.info.bits_per_sample = int.from_bytes(tta[8:10], "little")
    return audio


READERS = {
    ".mp3": EasyMP3, ".m4a": mp4_audio, ".flac": _SourceFLAC, ".wav": _read_wave,
    ".dsf": _read_dsf, ".dff": _read_dff, ".ape": MonkeysAudio, ".wv": WavPack,
    ".tta": _read_tta, ".aif": AIFF, ".aiff": AIFF,
}


def source_audio(path):
    return READERS[path.suffix.lower()](path)


def needs_48k_alac(audio):
    info = audio.info
    # AAC/MP3 have no source PCM bit depth. The sample size in an AAC container is not such a depth.
    pcm_depth = 0 if (getattr(info, "codec", "") or "").startswith("mp4a") else getattr(info, "bits_per_sample", 0)
    return isinstance(audio, (DSF, DSDIFF)) or info.sample_rate > MAX_ALAC_RATE or pcm_depth > MAX_ALAC_BITS


def is_native_playable(suffix, info):
    """True when an MP3 or M4A meets stock-firmware playback limits without re-encoding."""
    if not (0 < info.length and 0 < info.sample_rate <= MAX_ALAC_RATE and info.channels in (1, 2)):
        return False
    if suffix == ".mp3":
        return info.layer == 3
    return (info.codec or "").lower() in ("alac", "mp4a.40.2")


def tag_text(tags, key):
    return next((str(v).strip() for v in tags.get(key, ()) if str(v).strip()), "")


def riff_tags(path, changes=None):
    keys = {"INAM": "title", "IART": "artist", "IPRD": "album", "IGNR": "genre", "ICRD": "date", "ITRK": "tracknumber"}
    result = {}
    with open(path, "rb" if changes is None else "r+b") as stream:
        for block in RiffFile(stream).root.subchunks():
            if block.id != "LIST" or block.name != "INFO":
                continue
            for chunk in block.subchunks():
                key = keys.get(chunk.id)
                if not key or chunk.data_size > 65536:
                    continue
                if changes is not None:
                    if key in changes:
                        data = changes[key].encode("utf8") + b"\0"
                        chunk.resize(len(data))
                        chunk.write(data)
                    continue
                raw = chunk.read().rstrip(b"\0")
                value = _decode_text(raw)
                if value:
                    result[key] = [value]
    return result


def source_tags(audio):
    tags = audio.tags or {}
    if isinstance(audio, (MonkeysAudio, WavPack)):
        lowered = {k.lower(): v for k, v in tags.items()}
        result = {}
        for key, aliases in APE_KEYS.items():
            for alias in aliases:
                item = lowered.get(alias)
                if item is None or getattr(item, "kind", 0) != 0:
                    continue
                values = [str(v).strip() for v in item if str(v).strip()]
                if values:
                    result[key] = values
                    break
        return result
    if not isinstance(audio, (WAVE, DSF, DSDIFF, TrueAudio, AIFF)):
        return tags
    result = (riff_tags(audio.filename) if isinstance(audio, WAVE)
              else dff_comments(audio.filename) if isinstance(audio, DSDIFF) else {})
    result.update({key: [str(v) for v in tags[frame].text] for key, frame in TAG_KEYS.items()
                   if frame in tags and any(str(v).strip() for v in tags[frame].text)})
    return result


def lossless_profile(path, audio=None, *, allow_alac_m4a=False):
    """True when ALAC can retain the source samples without resampling or mixing."""
    audio = audio if audio is not None else source_audio(path)
    info = audio.info
    if not (0 < info.sample_rate <= MAX_ALAC_RATE and info.channels in (1, 2) and 0 < info.bits_per_sample <= MAX_ALAC_BITS):
        return False
    suffix = path.suffix.lower()
    if suffix == ".wav":
        try:
            with wave.open(str(path), "rb") as pcm:
                return pcm.getcomptype() == "NONE"
        except (wave.Error, EOFError):
            return False
    if allow_alac_m4a and suffix == ".m4a":
        return (getattr(info, "codec", "") or "").lower() == "alac"
    return suffix in LOSSLESS_CODECS


def _artist_folder(source):
    album = source.parent
    if album.name.casefold() == album.parent.name.casefold():
        album = album.parent
    if not re.fullmatch(r"(?:19|20)\d{2}\s*[-–—]\s*\S.*", album.name):
        return ""
    artist = album.parent.name.strip()
    if artist.casefold() in GENERIC_FOLDERS:
        return ""
    return artist


def _ape_pictures(tags):
    if not tags:
        return ()
    entries = []
    for key, item in tags.items():
        if "cover art" not in key.lower() or getattr(item, "kind", 0) != 1:
            continue
        raw = bytes(item.value)
        sep = raw.find(b"\0")
        if sep < 0 or sep + 1 >= len(raw):
            continue
        entries.append((0 if "front" in key.lower() else 1, raw[sep + 1:]))
    entries.sort(key=lambda pair: pair[0])
    return tuple(data for _, data in entries)


def _metadata(source, audio, destination, *, song=None, cue=None):
    suffix = source.suffix.lower()
    tags = dict(source_tags(audio))
    if cue is not None:
        cue_fields = {
            "title": cue.title,
            "artist": cue.artist,
            "album": cue.album,
            "albumartist": cue.album_artist,
            "date": cue.date,
            "genre": cue.genre,
            "tracknumber": f"{cue.track_number}/{cue.total_tracks}",
            "discnumber": cue.disc_number,
        }
        for key, value in cue_fields.items():
            if value:
                tags[key] = [value]
    result = MP4(destination)
    if result.tags is None:
        result.add_tags()
    for key, atom in MP4_ATOMS.items():
        values = tags.get(key)
        if not values:
            continue
        if key in ("tracknumber", "discnumber"):
            part = "track" if key == "tracknumber" else "disc"
            number = str(values[0])
            total = tags.get(part + "total") or tags.get("total" + part + "s")
            if "/" not in number and total:
                number += "/" + str(total[0])
            parts = number.split("/")
            if not all(v.isdecimal() and len(v) <= 4 for v in parts) or len(parts) > 2:
                continue
            result[atom] = [(int(parts[0]), int(parts[1]) if len(parts) == 2 else 0)]
        else:
            result[atom] = [str(v) for v in values]
    if song is not None:
        for key, value in (("title", song.title), ("artist", song.artist), ("album", song.album)):
            atom = MP4_ATOMS[key]
            if value:
                result[atom] = [value]
            else:
                result.tags.pop(atom, None)
        if song.cover is None:
            result.tags.pop("covr", None)
        else:
            cover_data, mime = song.cover.for_tag()
            fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            result["covr"] = [MP4Cover(cover_data, imageformat=fmt)]
    else:
        if not result.get("©nam"):
            result["©nam"] = [source.stem]
        if not any(v.strip() for v in result.get("©ART", [])):
            artist = _artist_folder(source)
            if artist:
                result["©ART"] = [artist]
        if suffix == ".m4a":
            covers = MP4(source).get("covr", [])
        elif suffix in (".ape", ".wv"):
            covers = []
            for data in _ape_pictures(audio.tags):
                if data.startswith(b"\x89PNG\r\n\x1a\n"):
                    covers.append(MP4Cover(data, imageformat=MP4Cover.FORMAT_PNG))
                elif data.startswith(b"\xff\xd8"):
                    covers.append(MP4Cover(data, imageformat=MP4Cover.FORMAT_JPEG))
        else:
            if suffix == ".flac":
                pictures = audio.pictures
            else:
                id3 = MP3(source).tags if suffix == ".mp3" else audio.tags
                pictures = id3.getall("APIC") if id3 else ()
            covers = [MP4Cover(pic.data, imageformat=MP4Cover.FORMAT_PNG if pic.mime == "image/png" else MP4Cover.FORMAT_JPEG)
                      for pic in sorted(pictures, key=lambda p: p.type != 3) if pic.mime in ("image/png", "image/jpeg")]
        if covers:
            result["covr"] = covers
    result.save()


def _unstamp(destination):
    """External encoders stamp wall-clock creation/modification times into mvhd/tkhd/mdhd,
    which would make every conversion hash differently; zero them in place like FFmpeg's bitexact mode."""
    stamped = {b"mvhd", b"tkhd", b"mdhd"}
    containers = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"edts"}
    with destination.open("r+b") as stream:
        total = os.fstat(stream.fileno()).st_size
        def walk(start, end):
            while start + 8 <= end:
                stream.seek(start)
                header = stream.read(8)
                if len(header) < 8:
                    break
                size = int.from_bytes(header[:4], "big")
                kind = header[4:8]
                head = 8
                if size == 1:
                    ext = stream.read(8)
                    if len(ext) < 8:
                        break
                    size = int.from_bytes(ext, "big")
                    head = 16
                elif size == 0:
                    size = end - start
                if size < head or start + size > end:
                    break
                if kind in stamped:
                    stream.seek(start + head)
                    version = stream.read(1)
                    if not version:
                        break
                    wide = version[0] == 1
                    span = 16 if wide else 8
                    if start + head + 4 + span <= start + size:
                        stream.seek(start + head + 4)
                        stream.write(b"\0" * span)
                elif kind in containers:
                    walk(start + head, start + size)
                start += size
        walk(0, total)


def _ffmpeg(arguments, timeout=900):
    try:
        process = subprocess.run([get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror", *arguments],
                                 stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, **system.NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("处理音频超时。") from exc
    if process.returncode:
        raise ValueError("处理音频失败。\n" + process.stderr.decode("utf8", "replace").strip()[-900:])
    return process.stdout.strip()


def _pcm_md5(path, rate, channels, codec, input_args=()):
    output = _ffmpeg(["-protocol_whitelist", "file", "-err_detect", "crccheck+explode",
                      *input_args, "-i", str(path),
                      "-map", "0:a:0", "-c:a", codec, "-ar", str(rate), "-ac", str(channels),
                      "-threads:a", "1", "-fflags", "+bitexact", "-flags:a", "+bitexact", "-f", "md5", "pipe:1"])
    match = re.fullmatch(rb"MD5=([0-9a-fA-F]{32})", output)
    if not match:
        raise ValueError("无法校验解码后的音频采样。")
    return int(match[1], 16)


def _verify_duration(source, info, destination, encoded, pcm=None):
    """pcm may be a (path, input_args) pair that decodes the source's samples cleanly."""
    pcm_path, pcm_input = pcm or (source, ())
    rate, channels = encoded.sample_rate, encoded.channels
    if info.length <= 0 or abs(encoded.length - info.length) <= 1.01 / rate:
        return
    lengths = f"源文件头标注 {info.length:.3f} 秒，转换后为 {encoded.length:.3f} 秒。"
    suffix = source.suffix.lower()
    if suffix not in (".flac", ".m4a"):
        raise ValueError("音频时长未通过校验。" + lengths)
    # FLAC headers can overstate duration and MP4 edit lists can trim encoder delay.
    bits = encoded.bits_per_sample
    codec = f"pcm_s{bits}le"
    pcm_md5 = _pcm_md5(pcm_path, rate, channels, codec, pcm_input)
    if pcm_md5 != _pcm_md5(destination, rate, channels, codec):
        raise ValueError("转换前后的音频采样不一致。" + lengths)
    if suffix == ".m4a":
        return
    native_codec = FLAC_PCM.get(info.bits_per_sample, (None,))[0]
    if info.md5_signature and native_codec:
        if (info.sample_rate, info.channels, info.bits_per_sample) != (rate, channels, bits):
            pcm_md5 = _pcm_md5(pcm_path, info.sample_rate, info.channels, native_codec, pcm_input)
        if pcm_md5 != info.md5_signature:
            raise ValueError("原始 FLAC 的音频采样与文件自带的 MD5 不一致。" + lengths)


def _flac_recovery(source, info, folder):
    """Prove a FLAC whose strict decode fails is intact outside the bad bytes.

    Tolerantly decodes the whole stream to the source's native PCM and compares it with
    the STREAMINFO checksum and total_samples. Returns (path, input_args) of a byte-exact
    clean copy to feed the encoders; raises a friendly error when it cannot be proven.
    """
    codec, fmt = FLAC_PCM.get(info.bits_per_sample, (None, None))
    if codec is None:
        raise ValueError("内部有无法解码的数据，音频不完整，未传入 iPod。可能是下载或拷贝不完整。"
                         "可以用 foobar2000 的「验证完整性」检查，或重新获取文件后再传。")
    raw = Path(folder) / "clean.pcm"
    try:
        proc = subprocess.run([get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error",
                               "-protocol_whitelist", "file", "-i", str(source), "-map", "0:a:0",
                               "-vn", "-sn", "-dn", "-c:a", codec, "-ar", str(info.sample_rate),
                               "-ac", str(info.channels), "-threads:a", "1", "-fflags", "+bitexact",
                               "-flags:a", "+bitexact", "-f", fmt, str(raw)],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=900, **system.NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("音频容错解码超时，未传入 iPod。") from exc
    detail = proc.stderr.decode("utf8", "replace").strip()
    if detail:
        diagnostics.log.warning("%s 容错解码：%s", source.name, detail[-900:])
    digest = hashlib.md5()
    size = 0
    if not proc.returncode and raw.exists():
        with open(raw, "rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
                size += len(chunk)
    unit = info.bits_per_sample // 8 * info.channels
    samples, odd = divmod(size, unit)
    if not info.md5_signature:
        raise ValueError("内部有无法解码的数据，音频不完整，未传入 iPod。文件没有自带校验值，"
                         "无法确认音频完整。可以用 foobar2000 的「验证完整性」检查，或重新获取文件后再传。")
    if samples < info.total_samples and not odd:
        raise ValueError("内部有无法解码的数据，音频不完整，未传入 iPod"
                         f"（约在第 {int(samples / info.sample_rate)} 秒附近）。可能是下载或拷贝不完整。"
                         "可以用 foobar2000 的「验证完整性」检查，或重新获取文件后再传。")
    if proc.returncode or odd or samples != info.total_samples or \
            int.from_bytes(digest.digest(), "big") != info.md5_signature:
        raise ValueError("内部有无法解码的数据，音频不完整，未传入 iPod。可能是下载或拷贝不完整。"
                         "可以用 foobar2000 的「验证完整性」检查，或重新获取文件后再传。")
    return raw, ["-f", fmt, "-ar", str(info.sample_rate), "-ac", str(info.channels)]


def _verify_cue_duration(expected, encoded, is_last):
    tolerance = 0.08 if is_last else 0.02
    if expected > 0 and abs(encoded.length - expected) > tolerance:
        raise ValueError(f"分轨时长未通过校验。预期 {expected:.3f} 秒，转换后为 {encoded.length:.3f} 秒。")


def _convert(source, mode, destination, *, song=None, cue=None, allow_alac=True):
    if mode not in ("alac", "alac-fit", "alac-48", "aac"):
        raise ValueError("未知的传输格式。")
    audio = source_audio(source)
    info = audio.info
    if info.sample_rate <= 0 or info.channels <= 0:
        raise ValueError("无法读取源文件的采样率或声道数。")
    expected_cue_length = None
    if cue is not None:
        if cue.start_seconds >= info.length:
            raise ValueError(f"第 {cue.track_number} 轨的起始时间超过整轨音频总时长。")
        if cue.end_frames is not None and cue.end_frames / 75.0 > info.length + 0.5:
            raise ValueError(f"第 {cue.track_number} 轨的结束时间超过整轨音频总时长。")
        expected_cue_length = cue.duration_seconds(info.length)
        if expected_cue_length <= 0:
            raise ValueError(f"第 {cue.track_number} 轨的时长无效。")
    suffix = source.suffix.lower()
    if needs_48k_alac(audio):
        mode = "alac-48" if allow_alac else "aac"
    elif not allow_alac and mode in ("alac", "alac-fit", "alac-48"):
        mode = "aac"
    alac_m4a_cue = cue is not None and suffix == ".m4a" and (getattr(info, "codec", "") or "").lower() == "alac"
    if mode in ("alac", "alac-fit") and suffix not in ASK_FORMAT and not alac_m4a_cue:
        raise ValueError("请选择无损音频作为 ALAC 转换源。")
    if mode == "alac" and not lossless_profile(source, audio, allow_alac_m4a=alac_m4a_cue):
        raise ValueError("此音源需要适配。请在待传列表的「传输格式」中选择 ALAC 兼容转换或 AAC。")
    rate = MAX_ALAC_RATE if mode == "alac-48" else info.sample_rate
    bits = MAX_ALAC_BITS if mode == "alac-48" or getattr(info, "bits_per_sample", 0) > 16 else 16
    if mode == "aac" and rate not in AAC_RATES:
        rate = 44100 if rate % 44100 == 0 else 48000
    channels = min(2, info.channels)
    codec = (["-c:a", "aac", "-profile:a", "aac_low", "-b:a", "256k" if channels == 2 else "128k"]
             if mode == "aac" else ["-c:a", "alac", "-sample_fmt", "s16p" if bits == 16 else "s32p",
                                    "-bits_per_raw_sample", str(bits)])
    decoder = ["-c:a", info.pcm_decoder] if hasattr(info, "pcm_decoder") else []
    # Bound the stereo mix before encoding multichannel DSD, including the floating-point AAC path.
    downmix_expr = f"aresample={rate}:ochl=stereo:rematrix_maxval=1" if suffix in DSD_FILES and info.channels > 2 else ""

    def slice_parts(raw_pcm):
        """Return (pre_input_args, post_input_args, filter_args) for optional CUE track slicing."""
        pre, post, filters = [], [], []
        if cue is not None:
            if raw_pcm:
                start_s, end_s = cue.sample_range(info.sample_rate)
                trim = f"atrim=start_sample={start_s}" + (f":end_sample={end_s}" if end_s is not None else "")
                filters.append(trim + ",asetpts=PTS-STARTPTS")
            else:
                if cue.start_frames > 0:
                    pre.extend(["-ss", f"{cue.start_seconds:.9f}"])
                if cue.end_frames is not None:
                    post.extend(["-t", f"{expected_cue_length:.9f}"])
        if downmix_expr:
            filters.append(downmix_expr)
        return pre, post, (["-af", ",".join(filters)] if filters else [])

    def decode_args(origin, origin_input=()):
        pre, post, af = slice_parts(raw_pcm=origin != source)
        return [*pre, *decoder, *origin_input, "-i", str(origin), "-map", "0:a:0", "-vn", "-sn", "-dn",
                *post, *af, "-ar", str(rate), "-ac", str(channels), "-threads:a", "1",
                "-fflags", "+bitexact", "-flags:a", "+bitexact"]
    candidates = []
    if mode == "aac":
        apple = encoders.qaac()
        if apple:
            exe, env = apple
            candidates.append((f"qaac --cvbr {256 if channels == 2 else 128}", exe, env,
                               ["--cvbr", "256" if channels == 2 else "128", "--raw", "--raw-format", "F32L",
                                "--raw-channels", str(channels), "--raw-rate", str(rate), "--silent"], "f32le"))
    else:
        raw_format, raw_name = ("s24le", "S24L") if bits == 24 else ("s16le", "S16L")
        raw_args = ["--raw", "--raw-format", raw_name, "--raw-channels", str(channels),
                    "--raw-rate", str(rate), "--silent"]
        apple = encoders.qaac()
        if apple:
            exe, env = apple
            candidates.append(("qaac -A", exe, env, ["-A", *raw_args], raw_format))
        bundle = encoders.refalac()
        if bundle:
            exe, env = bundle
            candidates.append(("refalac", exe, env, raw_args, raw_format))

    def encode(origin, origin_input=()):
        decode = decode_args(origin, origin_input)
        done = False
        if encoders.afconvert():
            layout = "-l", "Stereo" if channels == 2 else "Mono"
            if mode == "aac":
                af_args, pcm = (["-f", "m4af", "-d", "aac", "-b", "256000" if channels == 2 else "128000",
                                 "-c", str(channels), *layout, "-s", "2", "-q", "127"], "pcm_f32le")
            else:
                af_args = ["-f", "m4af", "-d", "alac", "-c", str(channels), *layout]
                pcm = "pcm_s24le" if bits == 24 else "pcm_s16le"
            try:
                encoders.afconvert_encode(af_args, decode, pcm, destination)
                _unstamp(destination)
            except Exception as exc:
                destination.unlink(missing_ok=True)
                diagnostics.log.warning("afconvert %s 编码失败，改用下一种编码器。%s", mode.upper(), exc)
            else:
                diagnostics.log.info("afconvert %s 编码：%s", mode.upper(), source.name)
                done = True
        for label, exe, env, tool_args, raw in candidates:
            if done:
                break
            try:
                encoders.pipe_encode(exe, env, tool_args, decode, raw, destination)
                _unstamp(destination)
            except Exception as exc:
                destination.unlink(missing_ok=True)
                diagnostics.log.warning("%s 编码失败，改用下一种编码器。%s", label, exc)
                continue
            diagnostics.log.info("%s 编码：%s", label, source.name)
            done = True
            break
        if not done:
            pre, post, af = slice_parts(raw_pcm=origin != source)
            _ffmpeg(["-n", "-protocol_whitelist", "file", *pre, *decoder, *origin_input, "-i", str(origin),
                     "-map", "0:a:0", "-vn", "-sn", "-dn",
                     "-map_metadata", "0", "-map_chapters", "-1", "-metadata", "creation_time=",
                     *post, *af, *codec, "-ar", str(rate), "-ac", str(channels), "-threads:a", "1", "-fflags", "+bitexact",
                     "-flags:a", "+bitexact", "-f", "ipod", str(destination)])

    pcm_source = (source, ())
    try:
        encode(source)
    except ValueError as exc:
        if suffix != ".flac":
            raise
        diagnostics.log.warning("%s 严格解码失败：%s", source.name, exc)
        clean, clean_input = _flac_recovery(source, info, destination.parent)
        diagnostics.log.warning("「%s」含有音频以外的无效数据，已核对采样数和 MD5 完全一致，按完好音频转换",
                                source.name)
        destination.unlink(missing_ok=True)
        encode(clean, clean_input)
        pcm_source = (clean, clean_input)
    try:
        _metadata(source, audio, destination, song=song, cue=cue)
        encoded = mp4_audio(destination).info
        want = "mp4a.40.2" if mode == "aac" else "alac"
        if (encoded.codec != want or encoded.sample_rate != rate or encoded.channels != channels or encoded.length <= 0
                or (mode != "aac" and encoded.bits_per_sample != bits)):
            depth = "" if mode == "aac" else f"、{bits} 位"
            raise ValueError(f"音频参数未通过校验。预期 {want}、{rate} Hz、{channels} 声道{depth}。"
                             f"实际为 {encoded.codec}、{encoded.sample_rate} Hz、{encoded.channels} 声道、"
                             f"{encoded.bits_per_sample} 位、{encoded.length:.3f} 秒。")
        if mode != "aac":
            if cue is not None:
                _verify_cue_duration(expected_cue_length, encoded, cue.end_frames is None)
            else:
                _verify_duration(source, info, destination, encoded, pcm_source)
    finally:
        if pcm_source[0] != source:
            # The proven PCM copy can be hundreds of MB; drop it as soon as the file is checked.
            Path(pcm_source[0]).unlink(missing_ok=True)


@contextmanager
def prepared_files(files, formats, device_root, report, *, reviewed=None, allow_alac=True):
    files = tuple(p if isinstance(p, CueKey) else Path(p) for p in files)
    formats = dict(formats or {})
    reviewed = reviewed or {}
    if reviewed and (set(reviewed) != set(files) or any(
            (item != song.key if isinstance(item, CueKey) else item.resolve() != song.path)
            or formats.get(item, song.mode) not in song.choices
            for item, song in reviewed.items())):
        raise ValueError("预览的歌曲或传输格式与本批文件不一致。")
    if formats.keys() - set(files):
        raise ValueError("所选传输格式与待传文件不一致。")
    for item in files:
        if reviewed:
            song = reviewed[item]
            song.check_source()
            mode = formats.setdefault(item, song.mode)
            if not allow_alac and mode in ("alac", "alac-fit", "alac-48"):
                formats[item] = "aac"
        elif isinstance(item, CueKey):
            raise ValueError("CUE 分轨需要通过导入预览确认后传入。")
        elif item.suffix.lower() in READERS:
            try:
                audio = source_audio(item)
                if not allow_alac and (
                        formats.get(item) in ("alac", "alac-fit", "alac-48")
                        or (item.suffix.lower() == ".m4a" and (getattr(audio.info, "codec", "") or "").lower() == "alac")
                        or needs_48k_alac(audio)):
                    formats[item] = "aac"
                elif needs_48k_alac(audio):
                    formats[item] = "alac-48"
            except Exception as exc:
                raise ValueError(f"{item.name}\n{exc}") from exc
    if not any(formats.get(p, "copy") != "copy" for p in files) and not any(s.needs_copy for s in reviewed.values()):
        yield files
        return
    temp_root = Path(tempfile.gettempdir()).resolve()
    if temp_root.is_relative_to(device_root):
        raise ValueError("转码需要电脑上的临时目录。")
    with tempfile.TemporaryDirectory(prefix="ClassicDrop-audio-", dir=temp_root) as folder:
        output = []
        for index, item in enumerate(files, 1):
            mode = formats.get(item, "copy")
            song = reviewed.get(item)
            cue = song.cue if song else None
            if mode == "copy" and not (song and song.needs_copy):
                output.append(item)
                continue
            source = song.path if song else item
            label_name = song.title if (song and cue is not None) else source.name
            try:
                source = source.resolve(strict=True)
                stamp = source.stat()
                if mode == "copy":
                    if cue is not None:
                        raise ValueError("CUE 分轨必须转换后传入。")
                    dest = Path(folder) / f"{index}{source.suffix}"
                    report(f"正在写入歌曲信息 {index}/{len(files)} · {label_name}")
                    shutil.copyfile(source, dest)
                    if song:
                        song.write_tags(dest, False)
                else:
                    dest = Path(folder) / f"{index}.m4a"
                    report(f"正在转换 {index}/{len(files)} · {label_name} → {LABELS.get(mode, mode)}")
                    _convert(source, mode, dest, song=song, cue=cue, allow_alac=allow_alac)
                if song:
                    song.check_source()
                end = source.stat()
                if (stamp.st_size, stamp.st_mtime_ns) != (end.st_size, end.st_mtime_ns):
                    raise ValueError("转换期间源文件发生变化，请重试。")
                os.utime(dest, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                output.append(dest)
            except Exception as exc:
                err_name = song.display_name if song else source.name
                raise ValueError(f"{err_name}\n{exc}") from exc
        yield output
