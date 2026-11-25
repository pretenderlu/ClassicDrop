import math
from array import array
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from test_classicdrop import DB, MUSIC, Device, fingerprint, mock_device
from test_formats_playlists import ffmpeg
from test_import import scan
from test_native_covers import picture
from audio_convert import prepared_files
from transfer import transfer
from mutagen.dsdiff import DSDIFF
from mutagen.id3 import APIC, TALB, TIT2, TPE1, TRCK
from mutagen.mp4 import MP4

CHANNEL_IDS = {1: (b"C   ",), 2: (b"SLFT", b"SRGT"), 6: (b"MLFT", b"MRGT", b"C   ", b"LFE ", b"LS  ", b"RS  ")}


def chunk(kind, body):
    return kind + struct.pack(">Q", len(body)) + body + bytes(len(body) % 2)


def write_dff(path, *, multiplier=64, channels=2, frames=11037, comments=None):
    """Encode a periodic test tone into uncompressed one-bit DSDIFF."""
    streams = []
    for channel in range(channels):
        period = bytearray(100 * multiplier // 8)
        error = 0.0
        for i in range(100 * multiplier):
            error += 0.5 + 0.16 * math.sin(2 * math.pi * (channel + 1) * i / (100 * multiplier))
            if error >= 1:
                period[i // 8] |= 1 << (7 - i % 8)
                error -= 1
        size = frames * multiplier // 8
        streams.append((period * (size // len(period) + 1))[:size])
    payload = bytes(b for sample in zip(*streams) for b in sample)
    sound = (b"SND " + chunk(b"FS  ", struct.pack(">L", 44100 * multiplier))
             + chunk(b"CHNL", struct.pack(">H", channels) + b"".join(CHANNEL_IDS[channels]))
             + chunk(b"CMPR", b"DSD " + bytes([14]) + b"not compressed"))
    body = b"DSD " + chunk(b"FVER", bytes((1, 5, 0, 0))) + chunk(b"PROP", sound) + chunk(b"DSD ", payload)
    if comments:
        body += chunk(b"DIIN", b"".join(chunk(kind, struct.pack(">L", len(text)) + text) for kind, text in comments))
    path.write_bytes(b"FRM8" + struct.pack(">Q", len(body)) + body)


def tag_dff(path):
    audio = DSDIFF(path)
    audio.add_tags()
    for kind, value in ((TIT2, "夜航"), (TPE1, "测试歌手"), (TALB, "测试专辑"), (TRCK, "2/8")):
        audio.tags.add(kind(encoding=3, text=[value]))
    for role, color in ((4, "blue"), (3, "red")):
        audio.tags.add(APIC(encoding=3, mime="image/png", type=role, desc=color, data=picture(color)))
    audio.save()


class DffImportTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.site = Path(temp.name).resolve()
        self.root = self.site / "iPod"
        self.device = mock_device(self.root)

    def test_dsd_rates_and_channel_layouts_use_48k_alac(self):
        for multiplier, channels in ((64, 2), (128, 1), (256, 6)):
            src = self.site / f"DSD{multiplier}.DFF"
            write_dff(src, multiplier=multiplier, channels=channels)
            songs, errors = scan([src])
            self.assertEqual(errors, [])
            song, = songs
            self.assertEqual((song.mode, song.choices), ("alac-48", ("alac-48",)))
            stereo_or_mono = min(channels, 2)
            reference = ffmpeg("-i", src, "-map", "0:a:0", "-ar", 48000, "-ac", stereo_or_mono, "-f", "s24le", "-")
            self.assertLessEqual(abs(len(reference) / (3 * stereo_or_mono) - 11037 * 48000 / 44100), 1)
            self.assertGreater(max(array("b", reference[2::3])), 27)
            with self.subTest(multiplier=multiplier), prepared_files([src], {}, self.root, lambda _: None, reviewed={src: song}) as files:
                dest, = files
                info = MP4(dest).info
                self.assertEqual((dest.suffix, info.codec, info.bits_per_sample, info.sample_rate, info.channels),
                                 (".m4a", "alac", 24, 48000, stereo_or_mono))
                pcm = ffmpeg("-i", dest, "-map", "0:a:0", "-f", "s24le", "-")
                self.assertTrue(pcm == reference, "ALAC must preserve every sample of the converted PCM")

    def test_id3_tags_artwork_and_master_comments_through_transfer(self):
        album = self.site / "音乐/目录歌手/2021-目录专辑"
        album.mkdir(parents=True)
        a, b, c = album / "01 带标签.DFF", album / "02 母带信息.dff", album / "03 无标签.dff"
        write_dff(a)
        tag_dff(a)
        write_dff(b, comments=((b"DIAR", "母带歌手".encode("gb18030")), (b"DITI", "母带歌名".encode("utf8") + b"\0")))
        write_dff(c)
        folder_art = album / "cover.png"
        folder_art.write_bytes(picture("blue"))
        originals = {p: fingerprint(p) for p in (a, b, c, folder_art)}
        songs, errors = scan([album])
        self.assertEqual(errors, [])
        tagged, master, bare = songs
        self.assertEqual((tagged.title, tagged.artist, tagged.album, tagged.cover.read()),
                         ("夜航", "测试歌手", "测试专辑", picture("red")))
        self.assertEqual((master.title, master.artist, master.album, master.cover.path),
                         ("母带歌名", "母带歌手", "目录专辑", folder_art))
        self.assertEqual((bare.title, bare.artist, bare.album), (c.stem, "目录歌手", "目录专辑"))
        reviewed = {a: tagged, b: master, c: bare}
        formats = {p: s.mode for p, s in reviewed.items()}
        self.assertEqual(transfer(self.device, (a, b, c), self.site / "backup", formats=formats, reviewed=reviewed)[:2], (3, 0))
        tracks = Device.open(self.root).library.snapshot.tracks
        self.assertEqual([(t.title, t.artist, t.album) for t in tracks],
                         [(s.title, s.artist, s.album) for s in (tagged, master, bare)])
        self.assertEqual((tracks[0].track_number, tracks[0].metadata.total_tracks), (2, 8))
        for track, color in zip(tracks, ("red", "blue", "blue")):
            audio = MP4(self.root / track.metadata.location)
            self.assertEqual((audio.info.codec, bytes(audio["covr"][0])), ("alac", picture(color)))
        self.assertEqual({p: fingerprint(p) for p in originals}, originals)

    def test_damaged_dff_fails_before_writing(self):
        good = self.site / "完好.dff"
        write_dff(good)
        bad = self.site / "截断.dff"
        bad.write_bytes(good.read_bytes()[:-4096])
        silent = self.site / "零采样率.dff"
        data = bytearray(good.read_bytes())
        at = data.index(b"FS  ") + 12
        data[at:at + 4] = bytes(4)
        silent.write_bytes(data)
        songs, errors = scan([bad, silent, good])
        self.assertEqual([s.path for s in songs], [good])
        self.assertEqual(len(errors), 2)
        with patch("transfer.device_lock") as lock, self.assertRaisesRegex(ValueError, "不完整"):
            transfer(self.device, [bad], self.site / "backup", formats={bad: "alac-48"})
        lock.assert_not_called()
        self.assertEqual((self.root / DB).read_bytes(), self.device.original)
        self.assertFalse(any(p.is_file() for p in (self.root / MUSIC).rglob("*")))
