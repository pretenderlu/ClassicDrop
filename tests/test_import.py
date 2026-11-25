import shutil
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_classicdrop import DB, FIXTURES, Device, fingerprint, mock_device
from test_formats_playlists import ffmpeg, write_wave
from test_native_covers import ART_DB, FORMATS, covered_m4a, picture
from audio_convert import riff_tags, source_tags
from music_import import Cover, scan_inputs
from transfer import transfer
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from mutagen.wave import WAVE


def scan(paths):
    return scan_inputs(paths, {}, lambda _: None, lambda: False)


class ReviewedImportTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.site = Path(temp.name).resolve()
        self.root = self.site / "iPod"
        self.dev = mock_device(self.root)
        self.album = self.site / "Music/李荣浩/2013-模特"
        self.album.mkdir(parents=True)

    def send(self, songs):
        reviewed = {s.path: s for s in songs}
        result = transfer(self.dev, tuple(reviewed), self.site / "backup", reviewed=reviewed,
                          formats={p: s.mode for p, s in reviewed.items()})
        self.dev = Device.open(self.root)
        return result

    def test_recursive_scan_fill_each_field_separately_prefer_embedded_and_album_art(self):
        disc = self.album / "Disc 1"
        disc.mkdir()
        wav = disc / "10 等待.WAV"
        write_wave(wav)
        flac = disc / "2 已有标签.flac"
        ffmpeg("-i", wav, "-metadata", "artist=客席歌手", "-metadata", "title=来自文件", flac)
        tags = FLAC(flac)
        for role, color in ((4, "red"), (3, "yellow")):
            cover = Picture()
            cover.type, cover.mime, cover.data = role, "image/png", picture(color)
            tags.add_picture(cover)
        tags.save()
        external = self.album / "Folder.PNG"
        external.write_bytes(picture("blue"))
        (self.album / "back.png").write_bytes(picture("red"))
        (disc / "坏文件.mp3").write_bytes(b"broken")
        (self.album / "说明.txt").write_text("readme", encoding="utf8")
        songs, errors = scan([self.album, flac])
        self.assertEqual([s.path for s in songs], [flac, wav])
        self.assertEqual(len(errors), 1)
        self.assertIn("坏文件", errors[0])
        a, b = songs
        self.assertEqual((a.title, a.artist, a.album, a.mode), ("来自文件", "客席歌手", "模特", "alac"))
        self.assertEqual((a.cover.label, bytes(a.cover.read())), ("内嵌封面", picture("yellow")))
        self.assertEqual((b.title, b.artist, b.album, b.cover.path), (wav.stem, "李荣浩", "模特", external))
        src = {p: fingerprint(p) for p in (wav, flac, external)}
        revised = [replace(a, album="最终专辑", cover=b.cover), replace(b, title="手改的歌名", mode="aac")]
        self.assertEqual(self.send(revised)[:2], (2, 0))
        lib = self.dev.library.with_artwork((self.root / ART_DB).read_bytes())
        for song, track, codec in zip(revised, lib.snapshot.tracks, ("alac", "mp4a.40.2"), strict=True):
            self.assertEqual((track.title, track.artist, track.album), (song.title, song.artist, song.album))
            dest = MP4(self.root / track.metadata.location)
            self.assertEqual((dest["©nam"][0], dest["©ART"][0], dest["©alb"][0], dest.info.codec),
                             (song.title, song.artist, song.album, codec))
            self.assertEqual(bytes(dest["covr"][0]), picture("blue"))
            read = lib.artwork_read(track.artwork_id, FORMATS, 320)
            with (self.root / read.relative_path).open("rb") as stream:
                stream.seek(read.offset)
                color = read.decode(stream.read(read.length)).rgb888[:3]
            self.assertEqual(color[:2], b"\0\0")
            self.assertGreaterEqual(color[2], 248)
        self.assertEqual(self.send(revised), (0, 2, None))
        self.assertEqual({p: fingerprint(p) for p in src}, src)

    def test_flat_album_and_unambiguous_sibling_art_only(self):
        album = self.site / "Downloads/张三 - 第一次"
        album.mkdir(parents=True)
        wav = album / "音轨.wav"
        write_wave(wav)
        (self.site / "Downloads/cover.png").write_bytes(picture("blue"))
        art = album / "picture.png"
        art.write_bytes(picture("red"))
        (album / "back.jpg").write_bytes(picture("blue"))
        song = scan([wav])[0][0]
        self.assertEqual((song.artist, song.album, song.cover.path), ("张三", "第一次", art))
        (album / "second.png").write_bytes(picture("yellow"))
        self.assertIsNone(scan([wav])[0][0].cover)
        top = self.site / "Downloads/单曲.wav"
        shutil.copyfile(wav, top)
        bare = scan([top])[0][0]
        self.assertEqual((bare.artist, bare.album), ("", ""))
        deluxe = self.album.parent / "2014-模特 - 豪华版"
        deluxe.mkdir()
        wav = deluxe / "01.wav"
        write_wave(wav)
        song = scan([wav])[0][0]
        self.assertEqual((song.artist, song.album), ("李荣浩", "模特 - 豪华版"))

    def test_copy_formats_cover_override_and_riff_tags_match_without_reencoding(self):
        wav = self.album / "一.wav"
        write_wave(wav)
        tagged = self.album / "二.wav"
        ffmpeg("-i", wav, "-metadata", "artist=文件歌手", "-metadata", "album=文件专辑", "-metadata", "title=文件曲名", tagged)
        m4a = covered_m4a(self.album / "三.m4a")
        mp3 = self.album / "四.mp3"
        shutil.copyfile(FIXTURES / "tone.mp3", mp3)
        custom = self.album / "自选封面.png"
        custom.write_bytes(picture("blue"))
        songs, _ = scan([tagged, m4a, mp3])
        a, b, c = songs
        self.assertEqual((a.artist, a.album, a.title), ("文件歌手", "文件专辑", "文件曲名"))
        self.assertEqual(b.cover.label, "内嵌封面")
        cover = Cover.from_file(custom)
        edited = [replace(a, mode="copy", artist="新歌手", album="", cover=cover),
                  replace(b, artist="AAC 歌手", cover=None), replace(c, album="MP3 专辑", cover=cover)]
        original = {p: fingerprint(p) for p in (mp3, m4a, tagged)}
        self.send(edited)
        paths = [self.root / t.metadata.location for t in self.dev.library.snapshot.tracks]
        self.assertEqual(source_tags(WAVE(paths[0])).get("artist"), ["新歌手"])
        self.assertEqual(riff_tags(paths[0])["artist"], ["新歌手"])
        self.assertNotIn("album", source_tags(WAVE(paths[0])))
        self.assertEqual(WAVE(paths[0]).tags.getall("APIC")[0].data, picture("blue"))
        self.assertNotIn("covr", MP4(paths[1]))
        self.assertEqual(ID3(paths[2]).getall("APIC")[0].data, picture("blue"))
        for src, dest, s, track in zip((tagged, m4a, mp3), paths, edited, self.dev.library.snapshot.tracks, strict=True):
            self.assertEqual((track.title, track.artist, track.album), (s.title, s.artist, s.album))
            self.assertEqual(ffmpeg("-i", dest, "-map", "0:a:0", "-f", "s16le", "-"),
                             ffmpeg("-i", src, "-map", "0:a:0", "-f", "s16le", "-"))
        self.assertEqual(original, {p: fingerprint(p) for p in original})
        self.assertEqual(self.send(edited), (0, 3, None))

    def test_byte_exact_copy_and_preview_change_block_before_device_write(self):
        source = covered_m4a(self.album / "完整标签.m4a")
        song = scan([source])[0][0]
        self.assertFalse(song.needs_copy)
        self.send([song])
        self.assertEqual(fingerprint(source), fingerprint(self.root / self.dev.library.snapshot.tracks[0].metadata.location))
        art = self.album / "cover.png"
        art.write_bytes(picture("blue"))
        chosen = replace(song, cover=Cover.from_file(art))
        art.write_bytes(b"changed")
        before = self.dev.original
        with patch("transfer.device_lock") as lock, self.assertRaisesRegex(ValueError, "封面来源已变化"):
            self.send([chosen])
        lock.assert_not_called()
        with source.open("ab") as stream:
            stream.write(b"changed")
        with patch("transfer.device_lock") as lock, self.assertRaisesRegex(ValueError, "预览后歌曲已变化"):
            self.send([song])
        lock.assert_not_called()
        self.assertEqual((self.root / DB).read_bytes(), before)
