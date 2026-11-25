"""Prepare local audio for the stock firmware before opening the device for writing."""

import os
import re
import shutil
import subprocess
import tempfile
import wave
from contextlib import contextmanager
from pathlib import Path

from imageio_ffmpeg import get_ffmpeg_exe
import diagnostics
import encoders
import system
from mutagen.dsdiff import DSDIFF, DSDIFFFile
from mutagen.dsf import DSF, DSFFile
from mutagen.easymp4 import EasyMP4
from mutagen._riff import RiffFile
from mutagen.flac import FLAC, VCFLACDict
from mutagen._vorbis import VorbisEncodingError
from mutagen.mp3 import EasyMP3, MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.wave import WAVE

ASK_FORMAT = {".flac", ".wav", ".dsf", ".dff"}
DSD_FILES = (".dsf", ".dff")
MAX_ALAC_BITS = 24
MAX_ALAC_RATE = 48000
LABELS = {"copy": "原格式", "alac": "ALAC 无损", "alac-fit": "ALAC 兼容转换",
          "alac-48": f"ALAC {MAX_ALAC_BITS} 位 / {MAX_ALAC_RATE // 1000} kHz", "aac": "AAC-LC"}
TAG_KEYS = {"title": "TIT2", "artist": "TPE1", "album": "TALB", "albumartist": "TPE2",
            "date": "TDRC", "genre": "TCON", "tracknumber": "TRCK", "discnumber": "TPOS"}
AAC_RATES = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000)


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
        for codec in ("utf-8-sig", "gb18030", "latin1"):
            try:
                value = text.decode(codec).strip()
                break
            except UnicodeDecodeError:
                continue
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


READERS = {".mp3": EasyMP3, ".m4a": EasyMP4, ".flac": _SourceFLAC, ".wav": _read_wave, ".dsf": _read_dsf, ".dff": _read_dff}


def source_audio(path):
    return READERS[path.suffix.lower()](path)


def needs_48k_alac(audio):
    info = audio.info
    # AAC/MP3 have no source PCM bit depth. The sample size in an AAC container is not such a depth.
    pcm_depth = 0 if (getattr(info, "codec", "") or "").startswith("mp4a") else getattr(info, "bits_per_sample", 0)
    return isinstance(audio, (DSF, DSDIFF)) or info.sample_rate > MAX_ALAC_RATE or pcm_depth > MAX_ALAC_BITS


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
                for codec in ("utf-8-sig", "gb18030", "latin1"):
                    try:
                        value = raw.decode(codec).strip()
                        break
                    except UnicodeDecodeError:
                        continue
                if value:
                    result[key] = [value]
    return result


def source_tags(audio):
    tags = audio.tags or {}
    if not isinstance(audio, (WAVE, DSF, DSDIFF)):
        return tags
    result = (riff_tags(audio.filename) if isinstance(audio, WAVE)
              else dff_comments(audio.filename) if isinstance(audio, DSDIFF) else {})
    result.update({key: [str(v) for v in tags[frame].text] for key, frame in TAG_KEYS.items()
                   if frame in tags and any(str(v).strip() for v in tags[frame].text)})
    return result


def lossless_profile(path, audio=None):
    """True when ALAC can retain the source samples without resampling or mixing."""
    audio = audio if audio is not None else source_audio(path)
    info = audio.info
    if not (0 < info.sample_rate <= MAX_ALAC_RATE and info.channels in (1, 2) and 0 < info.bits_per_sample <= MAX_ALAC_BITS):
        return False
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as pcm:
                return pcm.getcomptype() == "NONE"
        except (wave.Error, EOFError):
            return False
    return path.suffix.lower() == ".flac"


def _artist_folder(source):
    album = source.parent
    if album.name.casefold() == album.parent.name.casefold():
        album = album.parent
    if not re.fullmatch(r"(?:19|20)\d{2}\s*[-–—]\s*\S.*", album.name):
        return ""
    artist = album.parent.name.strip()
    if artist.casefold() in {"music", "audio", "downloads", "desktop", "documents", "albums", "various artists",
                             "音乐", "音樂", "下载", "下載", "歌曲", "专辑", "專輯", "合集", "合辑", "群星"}:
        return ""
    return artist


def _metadata(source, audio, destination):
    suffix = source.suffix.lower()
    tags = source_tags(audio)
    result = EasyMP4(destination)
    for key in TAG_KEYS:
        values = tags.get(key)
        if not values:
            continue
        if key in ("tracknumber", "discnumber"):
            part = "track" if key == "tracknumber" else "disc"
            number = str(values[0])
            total = tags.get(part + "total") or tags.get("total" + part + "s")
            if "/" not in number and total:
                number += "/" + str(total[0])
            if not all(v.isdecimal() and len(v) <= 4 for v in number.split("/")) or number.count("/") > 1:
                continue
            values = [number]
        result[key] = [str(v) for v in values]
    if not result.get("title"):
        result["title"] = [source.stem]
    if not any(v.strip() for v in result.get("artist", [])):
        artist = _artist_folder(source)
        if artist:
            result["artist"] = [artist]
    result.save()

    if suffix == ".m4a":
        covers = MP4(source).get("covr", [])
    else:
        if suffix == ".flac":
            pictures = audio.pictures
        else:
            id3 = MP3(source).tags if suffix == ".mp3" else audio.tags
            pictures = id3.getall("APIC") if id3 else ()
        covers = [MP4Cover(pic.data, imageformat=MP4Cover.FORMAT_PNG if pic.mime == "image/png" else MP4Cover.FORMAT_JPEG)
                  for pic in sorted(pictures, key=lambda p: p.type != 3) if pic.mime in ("image/png", "image/jpeg")]
    if covers:
        tagged = MP4(destination)
        tagged["covr"] = covers
        tagged.save()


def _unstamp(destination):
    """External encoders stamp wall-clock creation/modification times into mvhd/tkhd/mdhd,
    which would make every conversion hash differently; zero them like FFmpeg's bitexact mode."""
    stamped = {b"mvhd", b"tkhd", b"mdhd"}
    containers = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"edts"}
    data = bytearray(destination.read_bytes())
    def walk(start, end):
        while start + 8 <= end:
            size = int.from_bytes(data[start:start + 4], "big")
            kind = bytes(data[start + 4:start + 8])
            head = 8
            if size == 1:
                size = int.from_bytes(data[start + 8:start + 16], "big")
                head = 16
            elif size == 0:
                size = end - start
            if size < head or start + size > end:
                break
            if kind in stamped:
                wide = data[start + head] == 1
                for at in (start + head + 4, start + head + (12 if wide else 8)):
                    data[at:at + (8 if wide else 4)] = b"\0" * (8 if wide else 4)
            elif kind in containers:
                walk(start + head, start + size)
            start += size
    walk(0, len(data))
    destination.write_bytes(data)


def _ffmpeg(arguments):
    process = subprocess.run([get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror", *arguments],
                             stdin=subprocess.DEVNULL, capture_output=True, **system.NO_WINDOW)
    if process.returncode:
        raise ValueError("处理音频失败。\n" + process.stderr.decode("utf8", "replace").strip()[-900:])
    return process.stdout.strip()


def _pcm_md5(path, rate, channels, codec):
    output = _ffmpeg(["-protocol_whitelist", "file", "-err_detect", "crccheck+explode", "-i", str(path),
                      "-map", "0:a:0", "-c:a", codec, "-ar", str(rate), "-ac", str(channels),
                      "-threads:a", "1", "-fflags", "+bitexact", "-flags:a", "+bitexact", "-f", "md5", "pipe:1"])
    match = re.fullmatch(rb"MD5=([0-9a-fA-F]{32})", output)
    if not match:
        raise ValueError("无法校验解码后的音频采样。")
    return int(match[1], 16)


def _verify_duration(source, info, destination, encoded):
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
    pcm = _pcm_md5(source, rate, channels, codec)
    if pcm != _pcm_md5(destination, rate, channels, codec):
        raise ValueError("转换前后的音频采样不一致。" + lengths)
    if suffix == ".m4a":
        return
    native_codec = {8: "pcm_s8", 16: "pcm_s16le", 24: "pcm_s24le", 32: "pcm_s32le"}.get(info.bits_per_sample)
    if info.md5_signature and native_codec:
        if (info.sample_rate, info.channels, info.bits_per_sample) != (rate, channels, bits):
            pcm = _pcm_md5(source, info.sample_rate, info.channels, native_codec)
        if pcm != info.md5_signature:
            raise ValueError("原始 FLAC 的音频采样与文件自带的 MD5 不一致。" + lengths)


def _convert(source, mode, destination):
    if mode not in ("alac", "alac-fit", "alac-48", "aac"):
        raise ValueError("未知的传输格式。")
    audio = source_audio(source)
    info = audio.info
    if info.sample_rate <= 0 or info.channels <= 0:
        raise ValueError("无法读取源文件的采样率或声道数。")
    suffix = source.suffix.lower()
    if needs_48k_alac(audio):
        mode = "alac-48"
    if mode in ("alac", "alac-fit") and suffix not in ASK_FORMAT:
        raise ValueError("请选择 FLAC、WAV、DSF 或 DFF 作为 ALAC 转换源。")
    if mode == "alac" and not lossless_profile(source, audio):
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
    downmix = ["-af", f"aresample={rate}:ochl=stereo:rematrix_maxval=1"] if suffix in DSD_FILES and info.channels > 2 else []
    decode = [*decoder, "-i", str(source), "-map", "0:a:0", "-vn", "-sn", "-dn",
              *downmix, "-ar", str(rate), "-ac", str(channels), "-threads:a", "1",
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
    done = False
    if encoders.afconvert():
        if mode == "aac":
            af_args, pcm = (["-f", "m4af", "-d", "aac", "-b", "256000" if channels == 2 else "128000",
                             "-s", "2", "-q", "127"], "pcm_f32le")
        else:
            af_args, pcm = ["-f", "m4af", "-d", "alac"], "pcm_s24le" if bits == 24 else "pcm_s16le"
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
        _ffmpeg(["-n", "-protocol_whitelist", "file", *decoder, "-i", str(source), "-map", "0:a:0", "-vn", "-sn", "-dn",
                 "-map_metadata", "0", "-map_chapters", "-1", "-metadata", "creation_time=",
                 *downmix, *codec, "-ar", str(rate), "-ac", str(channels), "-threads:a", "1", "-fflags", "+bitexact",
                 "-flags:a", "+bitexact", "-f", "ipod", str(destination)])
    _metadata(source, audio, destination)
    encoded = EasyMP4(destination).info
    want = "mp4a.40.2" if mode == "aac" else "alac"
    if (encoded.codec != want or encoded.sample_rate != rate or encoded.channels != channels or encoded.length <= 0
            or (mode != "aac" and encoded.bits_per_sample != bits)):
        depth = "" if mode == "aac" else f"、{bits} 位"
        raise ValueError(f"音频参数未通过校验。预期 {want}、{rate} Hz、{channels} 声道{depth}。"
                         f"实际为 {encoded.codec}、{encoded.sample_rate} Hz、{encoded.channels} 声道、"
                         f"{encoded.bits_per_sample} 位、{encoded.length:.3f} 秒。")
    if mode != "aac":
        _verify_duration(source, info, destination, encoded)


@contextmanager
def prepared_files(files, formats, device_root, report, *, reviewed=None):
    files = tuple(Path(p) for p in files)
    formats = dict(formats or {})
    reviewed = reviewed or {}
    if reviewed and (set(reviewed) != set(files) or any(p != song.path or formats.get(p, song.mode) not in song.choices
                                                       for p, song in reviewed.items())):
        raise ValueError("预览的歌曲或传输格式与本批文件不一致。")
    if formats.keys() - set(files):
        raise ValueError("所选传输格式与待传文件不一致。")
    for source in files:
        if reviewed:
            song = reviewed[source]
            song.check_source()
            formats.setdefault(source, song.mode)
        elif source.suffix.lower() in READERS:
            try:
                if needs_48k_alac(source_audio(source)):
                    formats[source] = "alac-48"
            except Exception as exc:
                raise ValueError(f"{source.name}\n{exc}") from exc
    if not any(formats.get(p, "copy") != "copy" for p in files) and not any(s.needs_copy for s in reviewed.values()):
        yield files
        return
    temp_root = Path(tempfile.gettempdir()).resolve()
    if temp_root.is_relative_to(device_root):
        raise ValueError("转码需要电脑上的临时目录。")
    with tempfile.TemporaryDirectory(prefix="ClassicDrop-audio-", dir=temp_root) as folder:
        output = []
        for index, source in enumerate(files, 1):
            mode = formats.get(source, "copy")
            song = reviewed.get(source)
            if mode == "copy" and not (song and song.needs_copy):
                output.append(source)
                continue
            try:
                source = source.resolve(strict=True)
                stamp = source.stat()
                if mode == "copy":
                    dest = Path(folder) / f"{index}{source.suffix}"
                    report(f"正在写入歌曲信息 {index}/{len(files)} · {source.name}")
                    shutil.copyfile(source, dest)
                else:
                    dest = Path(folder) / f"{index}.m4a"
                    report(f"正在转换 {index}/{len(files)} · {source.name} → {LABELS.get(mode, mode)}")
                    _convert(source, mode, dest)
                if song:
                    song.write_tags(dest, mode != "copy")
                    song.check_source()
                end = source.stat()
                if (stamp.st_size, stamp.st_mtime_ns) != (end.st_size, end.st_mtime_ns):
                    raise ValueError("转换期间源文件发生变化，请重试。")
                os.utime(dest, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                output.append(dest)
            except Exception as exc:
                raise ValueError(f"{source.name}\n{exc}") from exc
        yield output
