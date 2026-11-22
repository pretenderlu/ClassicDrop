import io
import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_classicdrop import DB, FIXTURES, GUID, MUSIC, Device, fingerprint, mock_device
from test_formats_playlists import ffmpeg, write_wave
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.wave import WAVE
from PIL import Image
from iPodDB.iTunesDB.writer.signature import verify_hash58
from library import STAGE, playlist_edit
from native_covers import ART_DB, FORMATS
from transfer import recover, recover_interrupted, transfer


def picture(color):
    png = io.BytesIO()
    Image.new("RGB", (80, 80), color).save(png, format="PNG")
    return png.getvalue()


def covered_m4a(path, color="red"):
    shutil.copyfile(FIXTURES / "aac.m4a", path)
    audio = MP4(path)
    audio["©nam"] = [path.stem]
    audio["covr"] = [MP4Cover(picture(color), imageformat=MP4Cover.FORMAT_PNG)]
    audio.save()
    return path


class NativeCoverTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.site = Path(tmp.name)
        self.root = self.site / "IPC3"
        self.backups = self.site / "backups"
        self.device = mock_device(self.root)
        self.index = self.root / ART_DB

    def send(self, files=(), **kwargs):
        result = transfer(self.device, files, self.backups, **kwargs)
        self.device = Device.open(self.root)
        return result

    def art_files(self):
        return {p: p.read_bytes() for p in self.index.parent.glob("*") if p.is_file()}

    def library(self):
        self.assertTrue(verify_hash58(self.device.original, GUID))
        return self.device.library.with_artwork(self.index.read_bytes())

    def assert_cover(self, library, track, color):
        self.assertGreater(track.artwork_id, 0)
        self.assertEqual((track.ipod.artwork_id_ref, track.ipod.has_artwork), (track.artwork_id, 1))
        self.assertGreater(track.metadata.artwork_count, 0)
        for fmt in FORMATS:
            read = library.artwork_read(track.artwork_id, (fmt,), fmt.width)
            with (self.root / read.relative_path).open("rb") as stream:
                stream.seek(read.offset)
                pixels = read.decode(stream.read(read.length))
            self.assertEqual((pixels.width, pixels.height), (fmt.width, fmt.height))
            self.assertLessEqual(max(abs(channel - expected) for channel, expected in zip(pixels.rgb888[:3], color)), 7)

    def test_converted_flac_and_wav_keep_front_art_in_m4a_and_native_player(self):
        wav = self.site / "红色.wav"
        write_wave(wav)
        flac = self.site / "蓝色.flac"
        ffmpeg("-i", wav, flac)
        audio = FLAC(flac)
        for role, color in ((4, "red"), (3, "blue")):
            art = Picture()
            art.type, art.mime, art.data = role, "image/png", picture(color)
            audio.add_picture(art)
        audio.save()
        audio = WAVE(wav)
        audio.add_tags()
        audio.tags.add(APIC(mime="image/png", type=3, desc="front", data=picture("red")))
        audio.save()
        originals = {p: fingerprint(p) for p in (wav, flac)}
        self.assertEqual(self.send([flac, wav], formats={flac: "alac", wav: "aac"})[:2], (2, 0))
        library = self.library()
        for t, color, rgb, codec in zip(library.snapshot.tracks, ("blue", "red"), ((0, 0, 255), (255, 0, 0)),
                                        ("alac", "mp4a.40.2"), strict=True):
            self.assert_cover(library, t, rgb)
            audio = MP4(self.root / t.metadata.location)
            self.assertEqual((audio.info.codec, bytes(audio["covr"][0])), (codec, picture(color)))
        self.assertEqual({p: fingerprint(p) for p in originals}, originals)

    def test_repair_legacy_tracks_share_front_cover_and_skip_repeat_and_untagged(self):
        tracks = [covered_m4a(self.site / "一.m4a"), covered_m4a(self.site / "二.m4a"), FIXTURES / "tone.mp3"]
        with patch("transfer.prepare_covers", return_value=(None, 0)):
            self.send(tracks)
        self.assertFalse(self.index.exists())
        old = self.device.library.snapshot
        self.send(desired=playlist_edit(old, [old.tracks[1].track_id, old.tracks[0].track_id], name="保留次序"))
        before = self.device.library.snapshot
        count, missing, backup = self.send(repair_covers=True)
        self.assertEqual((count, missing), (2, 1))
        self.assertTrue(backup.is_file())
        library = self.library()
        a, b, plain = library.snapshot.tracks
        for t in (a, b):
            self.assert_cover(library, t, (255, 0, 0))
        self.assertEqual(library.artwork_read(a.artwork_id, FORMATS, 320), library.artwork_read(b.artwork_id, FORMATS, 320))
        self.assertEqual(plain.artwork_id, 0)
        self.assertEqual(library.snapshot.playlists, before.playlists)
        for old, new, src in zip(before.tracks, library.snapshot.tracks, tracks, strict=True):
            self.assertEqual((old.track_id, old.metadata.location, old.ipod.db_track_id),
                             (new.track_id, new.metadata.location, new.ipod.db_track_id))
            self.assertEqual(fingerprint(self.root / old.metadata.location), fingerprint(src))
        existing, db = self.art_files(), self.device.original
        self.assertEqual(self.send(repair_covers=True), (0, 1, None))
        self.assertEqual(self.send(tracks), (0, 3, None))
        self.assertEqual((self.device.original, self.art_files()), (db, existing))
        self.send(desired=playlist_edit(self.device.library.snapshot, [a.track_id], name="新歌单"))
        self.assert_cover(self.library(), self.library().snapshot.tracks[0], (255, 0, 0))
        self.assertEqual(self.art_files(), existing)

    def test_copy_mp3_and_recover_missing_entire_index(self):
        source = self.site / "front.mp3"
        shutil.copyfile(FIXTURES / "tone.mp3", source)
        tags = ID3(source)
        for role, color in ((4, "blue"), (3, "red")):
            tags.add(APIC(mime="image/png", type=role, desc=color, data=picture(color)))
        tags.save()
        self.send([source])
        library = self.library()
        self.assert_cover(library, library.snapshot.tracks[0], (255, 0, 0))
        self.index.unlink()
        self.assertEqual(self.send(repair_covers=True)[:2], (1, 0))
        library = self.library()
        self.assert_cover(library, library.snapshot.tracks[0], (255, 0, 0))
        self.assertEqual(fingerprint(self.root / library.snapshot.tracks[0].metadata.location), fingerprint(source))

    def test_coverless_import_with_unreadable_index(self):
        self.index.parent.mkdir()
        self.index.write_bytes(b"unreadable")
        source = self.site / "broken.m4a"
        shutil.copyfile(FIXTURES / "aac.m4a", source)
        audio = MP4(source)
        audio["covr"] = [MP4Cover(b"not an image")]
        audio.save()
        self.assertEqual(self.send([FIXTURES / "tone.mp3", source])[:2], (2, 0))
        self.assertEqual(self.index.read_bytes(), b"unreadable")

    def test_interrupted_append_preserves_old_images_and_commits_complete_new_images(self):
        first = covered_m4a(self.site / "old.m4a")
        second = covered_m4a(self.site / "new.m4a", "blue")
        self.send([first])
        before = self.art_files()
        audio = self.root / self.device.library.snapshot.tracks[0].metadata.location
        def interrupt(message):
            if message == "提交曲库…":
                raise SystemExit
        with self.assertRaises(SystemExit):
            self.send([second], report=interrupt)
        self.assertNotEqual(self.art_files(), before)
        self.assertEqual(self.device.original, (self.root / DB).read_bytes())
        recover_interrupted(self.device, self.backups)
        self.assertEqual(self.art_files(), before)
        self.assertEqual(list((self.root / MUSIC).rglob("*.m4a")), [audio])
        def after_commit(dev, backups):
            if (self.root / DB).read_bytes() != dev.original:
                raise SystemExit
            recover(dev, backups)
        with patch("transfer.recover", side_effect=after_commit), self.assertRaises(SystemExit):
            self.send([second])
        self.device = Device.open(self.root)
        recover_interrupted(self.device, self.backups)
        self.assertFalse((self.root / STAGE).exists())
        library = self.library()
        for t, rgb in zip(library.snapshot.tracks, ((255, 0, 0), (0, 0, 255)), strict=True):
            self.assert_cover(library, t, rgb)
        self.assertTrue(all(p.read_bytes().startswith(data) for p, data in before.items() if p != self.index))
        self.assertEqual(next(self.backups.rglob("ArtworkDB-*")).read_bytes(), before[self.index])

    def test_interruption_before_first_index_and_tampered_journal(self):
        song = covered_m4a(self.site / "one.m4a")
        def interrupt(message):
            if message == "提交曲库…":
                raise SystemExit
        with self.assertRaises(SystemExit):
            self.send([song], report=interrupt)
        staged = self.root / STAGE / "plan.json"
        original = staged.read_bytes()
        altered = json.loads(original)
        altered["files"] = []
        staged.write_text(json.dumps(altered), encoding="utf8")
        with self.assertRaisesRegex(ValueError, "备份记录不符"):
            recover_interrupted(self.device, self.backups)
        self.assertTrue(self.index.exists())
        staged.write_bytes(original)
        shard = next(self.index.parent.glob("*.ithmb"))
        data = shard.read_bytes()
        shard.write_bytes(data[:-1])
        with self.assertRaisesRegex(OSError, "封面文件已变化"):
            recover_interrupted(self.device, self.backups)
        self.assertEqual(shard.read_bytes(), data[:-1])
        shard.write_bytes(data)
        recover_interrupted(self.device, self.backups)
        self.assertFalse(self.art_files())
        self.assertFalse(list((self.root / MUSIC).rglob("*.m4a")))
