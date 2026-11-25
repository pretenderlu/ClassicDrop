import math
from array import array
from dataclasses import replace
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from test_classicdrop import DB, MUSIC, Device, fingerprint, mock_device
from test_formats_playlists import ffmpeg
from test_import import scan
from test_native_covers import ART_DB, FORMATS, picture
from audio_convert import prepared_files
from transfer import transfer
from mutagen.dsf import DSF
from mutagen.id3 import APIC, TALB, TIT2, TPE1, TRCK
from mutagen.mp4 import MP4


def write_dsf(path, *, multiplier=64, channels=2, msb=False, frames=11037):
    """Encode a periodic test tone into one-bit DSD, with a padded final channel block."""
    block = 4096
    channel_data = []
    for channel in range(channels):
        period = bytearray(100 * multiplier // 8)
        error = 0.0
        for i in range(100 * multiplier):
            error += 0.5 + 0.16 * math.sin(2 * math.pi * (channel + 1) * i / (100 * multiplier))
            if error >= 1:
                period[i // 8] |= 1 << (7 - i % 8 if msb else i % 8)
                error -= 1
        size = frames * multiplier // 8
        data = (period * (size // len(period) + 1))[:size]
        channel_data.append(data + bytes(-len(data) % block))
    payload = b"".join(d[start:start + block] for start in range(0, len(channel_data[0]), block) for d in channel_data)
    layout = {1: 1, 2: 2, 6: 7}[channels]
    path.write_bytes(struct.pack("<4sQQQ", b"DSD ", 28, 92 + len(payload), 0)
                     + struct.pack("<4sQ6IQ2I", b"fmt ", 52, 1, 0, layout, channels, 44100 * multiplier,
                                   8 if msb else 1, frames * multiplier, block, 0)
                     + struct.pack("<4sQ", b"data", 12 + len(payload)) + payload)


def tag_dsf(path):
    audio = DSF(path)
    audio.add_tags()
    for kind, value in ((TIT2, "夜航"), (TPE1, "测试歌手"), (TALB, "测试专辑"), (TRCK, "2/8")):
        audio.tags.add(kind(encoding=3, text=[value]))
    for role, color in ((4, "blue"), (3, "red")):
        audio.tags.add(APIC(encoding=3, mime="image/png", type=role, desc=color, data=picture(color)))
    audio.save()


class DsfImportTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.site = Path(temp.name).resolve()
        self.root = self.site / "iPod"
        self.device = mock_device(self.root)

    def test_dsd_rates_bit_orders_and_channel_layouts_use_48k_alac(self):
        for multiplier, channels, msb in ((64, 2, False), (128, 1, True), (256, 6, False), (512, 2, True)):
            src = self.site / f"DSD{multiplier}.DSF"
            write_dsf(src, multiplier=multiplier, channels=channels, msb=msb)
            songs, errors = scan([src])
            self.assertEqual(errors, [])
            song, = songs
            self.assertEqual((song.mode, song.choices), ("alac-48", ("alac-48",)))
            stereo_or_mono = min(channels, 2)
            reference = ffmpeg("-i", src, "-map", "0:a:0", "-ar", 48000, "-ac", stereo_or_mono, "-f", "s24le", "-")
            self.assertLessEqual(abs(len(reference) / (3 * stereo_or_mono) - 11037 * 48000 / 44100), 1)
            self.assertGreater(max(array("b", reference[2::3])), 27)
            self.assertTrue(any(reference[::3]))
            with self.subTest(multiplier=multiplier), prepared_files([src], {}, self.root, lambda _: None, reviewed={src: song}) as files:
                dest, = files
                info = MP4(dest).info
                self.assertEqual((dest.suffix, info.codec, info.bits_per_sample, info.sample_rate, info.channels),
                                 (".m4a", "alac", 24, 48000, stereo_or_mono))
                pcm = ffmpeg("-i", dest, "-map", "0:a:0", "-f", "s24le", "-")
                self.assertTrue(pcm == reference, "ALAC must preserve every sample of the converted PCM")

    def test_tags_and_artwork_through_confirmed_transfer(self):
        album = self.site / "音乐/目录歌手/2021-目录专辑"
        album.mkdir(parents=True)
        a, b = album / "01 带标签.DSF", album / "02 无标签.dsf"
        for source in (a, b):
            write_dsf(source)
        tag_dsf(a)
        folder_art = album / "cover.png"
        folder_art.write_bytes(picture("blue"))
        originals = {p: fingerprint(p) for p in (a, b, folder_art)}
        songs, errors = scan([album, a])
        self.assertEqual(errors, [])
        tagged, bare = songs
        self.assertEqual((tagged.title, tagged.artist, tagged.album, tagged.cover.read()),
                         ("夜航", "测试歌手", "测试专辑", picture("red")))
        self.assertEqual((bare.title, bare.artist, bare.album, bare.cover.path), (b.stem, "目录歌手", "目录专辑", folder_art))
        reviewed = {a: tagged, b: replace(bare, artist="手改歌手")}
        formats = {p: s.mode for p, s in reviewed.items()}
        self.assertEqual(transfer(self.device, (a, b), self.site / "backup", formats=formats, reviewed=reviewed)[:2], (2, 0))
        dev = Device.open(self.root)
        lib = dev.library.with_artwork((self.root / ART_DB).read_bytes())
        first, second = lib.snapshot.tracks
        self.assertEqual((first.track_number, first.metadata.total_tracks), (2, 8))
        for song, track, color in ((tagged, first, "red"), (reviewed[b], second, "blue")):
            self.assertEqual((track.title, track.artist, track.album), (song.title, song.artist, song.album))
            dest = self.root / track.metadata.location
            audio = MP4(dest)
            self.assertEqual((dest.suffix, audio.info.codec, audio["©ART"][0], bytes(audio["covr"][0])),
                             (".m4a", "alac", song.artist, picture(color)))
            thumb = lib.artwork_read(track.artwork_id, FORMATS, 320)
            with (self.root / thumb.relative_path).open("rb") as stream:
                stream.seek(thumb.offset)
                rgb = thumb.decode(stream.read(thumb.length)).rgb888[:3]
            self.assertGreaterEqual(rgb[0 if color == "red" else 2], 248)
            self.assertEqual(rgb[2 if color == "red" else 0], 0)
        self.assertEqual(transfer(dev, (a, b), self.site / "backup", formats=formats, reviewed=reviewed), (0, 2, None))
        self.assertEqual({p: fingerprint(p) for p in originals}, originals)

    def test_damaged_dsf_and_invalid_preview_fail_before_writing(self):
        good = self.site / "完好.dsf"
        write_dsf(good)
        bad = self.site / "截断.dsf"
        bad.write_bytes(good.read_bytes()[:-4096])
        invalid = self.site / "零采样率.dsf"
        data = bytearray(good.read_bytes())
        struct.pack_into("<I", data, 56, 0)
        invalid.write_bytes(data)
        songs, errors = scan([bad, invalid, good])
        self.assertEqual([s.path for s in songs], [good])
        self.assertEqual(len(errors), 2)
        for mode in ("alac-48", "aac"):
            with self.subTest(mode=mode), patch("transfer.device_lock") as lock, self.assertRaisesRegex(ValueError, "不完整"):
                transfer(self.device, [bad], self.site / "backup", formats={bad: mode})
            lock.assert_not_called()
        with patch("transfer.device_lock") as lock, self.assertRaisesRegex(ValueError, "预览"):
            transfer(self.device, [good], self.site / "backup", formats={good: "aac"}, reviewed={good: songs[0]})
        lock.assert_not_called()
        self.assertEqual((self.root / DB).read_bytes(), self.device.original)
        self.assertFalse(any(p.is_file() for p in (self.root / MUSIC).rglob("*")))
