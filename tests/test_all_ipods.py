"""End-to-end tests for every non-iOS iPod tier: unsigned classics/video/mini/nano 1-2G,
compressed+SQLite nano 5G (HASH72) and nano 6-7G (HASHAB), and iPod shuffle 1-4G (iTunesSD v1/v2)."""

import plistlib
import struct
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(ROOT / "vendor")]

from test_classicdrop import FIXTURES, GUID
from test_formats_playlists import write_wave
from test_native_covers import ART_DB, covered_m4a
from iPodDB.iTunesDB.cdb import is_iTunesCDB
from iPodDB.iTunesDB.writer.signature import verify_hash58, verify_hash72, verify_hashab
from iPodDB.library import Hash72Material, WriteChecksum, WriteTarget, create_empty_library
from library import CDB, DB, MUSIC, SD, SQLITE_DIR, SQLITE_NAMES, STAGE, Device
from native_covers import NANO_7, cover_formats, device_covers, load_cover
import shuffle_db
import system
from theme import SHAPES, player_image
from transfer import export_tracks, recover_interrupted, transfer

MAT72 = Hash72Material(None, bytes(range(12)), bytes(range(16)))


def make_unsigned_device(root, model=None):
    (root / DB).parent.mkdir(parents=True)
    (root / MUSIC / "F00").mkdir(parents=True)
    if model:
        info = root / "iPod_Control/Device/SysInfo"
        info.parent.mkdir(parents=True)
        info.write_text(f"ModelNumStr: {model}\n", encoding="utf8")
    target = WriteTarget(checksum=WriteChecksum.NONE, database_version=0x30)
    prepared = create_empty_library("经典 iPod", target, supports_podcasts=True).prepared
    (root / DB).write_bytes(prepared.itunes)
    return Device.open(root)


def make_cdb_device(root, model, checksum):
    (root / CDB).parent.mkdir(parents=True)
    (root / MUSIC / "F00").mkdir(parents=True)
    info = root / "iPod_Control/Device/SysInfoExtended"
    info.parent.mkdir(parents=True)
    info.write_bytes(plistlib.dumps({"FireWireGUID": int.from_bytes(GUID, "big"), "ModelNumStr": model}))
    target = WriteTarget(
        checksum=checksum,
        firewire_guid=GUID,
        database_version=0x30,
        compressed_database=True,
        sqlite_database=True,
        sqlite_checksum=checksum,
        hash72_material=MAT72 if checksum is WriteChecksum.HASH72 else None,
    )
    prepared = create_empty_library(f"测试 {model}", target, supports_podcasts=True).prepared
    (root / CDB).write_bytes(prepared.itunes)
    itlp = root / SQLITE_DIR
    itlp.mkdir(parents=True)
    for name, data in prepared.sqlite.artifacts():
        (itlp / name).write_bytes(data)
    return Device.open(root)


def make_shuffle_device(root, version, *, with_db=True):
    (root / DB).parent.mkdir(parents=True)
    (root / MUSIC / "F00").mkdir(parents=True)
    target = WriteTarget(checksum=WriteChecksum.NONE, database_version=0x30)
    prepared = create_empty_library("我的 Shuffle", target, supports_podcasts=False).prepared
    if with_db:
        (root / DB).write_bytes(prepared.itunes)
    (root / SD).write_bytes(shuffle_db.build_itunessd(prepared.snapshot, version))
    return Device.open(root)


class Tier1UnsignedModelsTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.site = Path(tmp.name).resolve()
        self.backups = self.site / "backups"

    def test_ipod_video_5_5g_writes_unsigned_db_and_two_covers_without_guid(self):
        root = self.site / "video55"
        dev = make_unsigned_device(root, "MA444")
        self.assertTrue(dev.writable)
        self.assertIsNone(dev.guid)
        self.assertEqual((dev.profile.family, dev.profile.generation, dev.supports_alac, dev.supports_covers),
                         ("iPod", "5.5th Gen", True, True))
        song = covered_m4a(self.site / "红.m4a")
        added, skipped, backup = transfer(dev, [song, FIXTURES / "lossless.m4a"], self.backups, new_playlist_name="复古")
        self.assertEqual((added, skipped), (2, 0))
        self.assertTrue(backup.is_file())
        reloaded = Device.open(root)
        self.assertTrue(reloaded.writable)
        self.assertEqual(len(reloaded.library.snapshot.tracks), 2)
        self.assertEqual([p.name for p in reloaded.library.snapshot.playlists], ["复古"])
        # Video 5/5.5G keeps 1028 (100x100) and 1029 (200x200).
        shards = sorted(p.name.split("_")[0] for p in (root / ART_DB).parent.glob("*.ithmb"))
        self.assertEqual(shards, ["F1028", "F1029"])
        small, large = device_covers(reloaded)
        self.assertEqual({i.size for i in small.values()}, {(200, 200)})
        self.assertEqual(large, {})
        # Deletion and export also work cleanly without a FireWire GUID.
        out = self.site / "exp"
        out.mkdir()
        self.assertEqual(export_tracks(reloaded, reloaded.library.snapshot.tracks, out), 2)
        transfer(reloaded, [], self.backups, remove_ids=[reloaded.library.snapshot.tracks[0].track_id])
        self.assertEqual(len(Device.open(root).library.snapshot.tracks), 1)

    def test_ipod_4g_photo_uses_140px_1016_without_conflicting_with_nano_7g(self):
        root = self.site / "photo4g"
        dev = make_unsigned_device(root, "M9585")
        transfer(dev, [covered_m4a(self.site / "蓝.m4a", "blue")], self.backups)
        reloaded = Device.open(root)
        small, large = device_covers(reloaded)
        self.assertEqual({i.size for i in small.values()}, {(140, 140)})
        self.assertEqual(large, {})

    def test_ipod_nano_1g_writes_two_covers_and_mini_1g_blocks_alac_and_skips_covers(self):
        nano_root = self.site / "nano1"
        nano = make_unsigned_device(nano_root, "MA004")
        transfer(nano, [covered_m4a(self.site / "红.m4a")], self.backups)
        shards = sorted(p.name.split("_")[0] for p in (nano_root / ART_DB).parent.glob("*.ithmb"))
        self.assertEqual(shards, ["F1027", "F1031"])

        mini1_root = self.site / "mini1"
        mini1 = make_unsigned_device(mini1_root, "M9160")
        self.assertEqual((mini1.writable, mini1.supports_alac, mini1.supports_covers), (True, False, False))
        # High-res / ALAC source on mini 1G is automatically converted to AAC-LC and writes no ArtworkDB.
        wav = self.site / "hires.wav"
        write_wave(wav, rate=96000, width=3)
        transfer(mini1, [covered_m4a(self.site / "带图.m4a"), FIXTURES / "lossless.m4a", wav], self.backups)
        reloaded = Device.open(mini1_root)
        self.assertFalse((mini1_root / ART_DB).exists())
        self.assertEqual([t.metadata.file_format for t in reloaded.library.snapshot.tracks],
                         ["AAC audio file", "AAC audio file", "AAC audio file"])

        mini2_root = self.site / "mini2"
        mini2 = make_unsigned_device(mini2_root, "M9800")
        self.assertEqual((mini2.writable, mini2.supports_alac, mini2.supports_covers), (True, True, False))
        transfer(mini2, [FIXTURES / "lossless.m4a"], self.backups)
        self.assertEqual(Device.open(mini2_root).library.snapshot.tracks[0].metadata.file_format,
                         "Apple Lossless audio file")


class Tier2CompressedSQLiteTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.site = Path(tmp.name).resolve()
        self.backups = self.site / "backups"

    def test_nano_5g_hash72_cdb_sqlite_covers_and_interrupted_rollback(self):
        root = self.site / "nano5"
        dev = make_cdb_device(root, "MC027", WriteChecksum.HASH72)
        self.assertEqual((dev.writable, dev.is_cdb, dev.uses_sqlite, dev.checksum),
                         (True, True, True, WriteChecksum.HASH72))
        self.assertIsNotNone(dev.hash72_material)

        song = covered_m4a(self.site / "红.m4a")
        before_cdb = (root / CDB).read_bytes()
        before_sqlite = {name: (root / SQLITE_DIR / name).read_bytes() for name in SQLITE_NAMES}

        # Simulate a crash right before committing iTunesCDB; recover must restore SQLite & covers.
        def abort(msg):
            if msg == "提交曲库…":
                raise SystemExit()

        with self.assertRaises(SystemExit):
            transfer(dev, [song], self.backups, report=abort)
        self.assertEqual((root / CDB).read_bytes(), before_cdb)
        recover_interrupted(dev, self.backups)
        self.assertFalse((root / STAGE).exists())
        self.assertEqual({name: (root / SQLITE_DIR / name).read_bytes() for name in SQLITE_NAMES}, before_sqlite)
        self.assertFalse((root / ART_DB).exists())

        # Now commit cleanly.
        added, skipped, backup = transfer(dev, [song, FIXTURES / "tone.mp3"], self.backups, new_playlist_name="五代歌单")
        self.assertEqual((added, skipped, backup.name), (2, 0, "iTunesCDB"))
        reloaded = Device.open(root)
        self.assertTrue(is_iTunesCDB(reloaded.original))
        self.assertTrue(verify_hash58(reloaded.original, GUID))
        self.assertTrue(verify_hash72(reloaded.original, MAT72.iv, MAT72.random_part, hashing_scheme=1))
        self.assertEqual(len(reloaded.library.snapshot.tracks), 2)
        self.assertEqual([p.name for p in reloaded.library.snapshot.playlists], ["五代歌单"])
        for name in SQLITE_NAMES:
            self.assertTrue((root / SQLITE_DIR / name).is_file(), name)
        self.assertNotEqual((root / SQLITE_DIR / "Library.itdb").read_bytes(), before_sqlite["Library.itdb"])
        shards = sorted(p.name.split("_")[0] for p in (root / ART_DB).parent.glob("*.ithmb"))
        self.assertEqual(shards, ["F1056", "F1073", "F1074", "F1078"])

        # Reload via lightweight dev.reload() and delete one song.
        transfer(reloaded, [], self.backups, remove_ids=[reloaded.library.snapshot.tracks[0].track_id])
        after_del = reloaded.reload()
        self.assertEqual(len(after_del.library.snapshot.tracks), 1)
        self.assertTrue(verify_hash72(after_del.original, MAT72.iv, MAT72.random_part, hashing_scheme=1))

    def test_nano_6g_and_7g_hashab_cdb_sqlite_and_57px_1016_cover(self):
        for model, expected_shards in (("MC525", ["F1073", "F1074", "F1085", "F1089"]),
                                       ("MD475", ["F1010", "F1013", "F1015", "F1016"])):
            with self.subTest(model=model):
                root = self.site / model
                dev = make_cdb_device(root, model, WriteChecksum.HASHAB)
                self.assertEqual((dev.writable, dev.is_cdb, dev.uses_sqlite, dev.checksum),
                                 (True, True, True, WriteChecksum.HASHAB))
                added, _, _ = transfer(dev, [covered_m4a(self.site / f"{model}.m4a", "blue")], self.backups)
                self.assertEqual(added, 1)
                reloaded = Device.open(root)
                self.assertTrue(verify_hashab(reloaded.original, GUID))
                shards = sorted(p.name.split("_")[0] for p in (root / ART_DB).parent.glob("*.ithmb"))
                self.assertEqual(shards, expected_shards)
                small, large = device_covers(reloaded)
                self.assertEqual({i.size for i in small.values()}, {(240, 240)})
                self.assertEqual(large, {})
                if model == "MD475":
                    # Confirm Nano 7G's format 1016 is 57x57 (not 4G Photo's 140x140).
                    lib = reloaded.library.with_artwork((root / ART_DB).read_bytes())
                    fmt1016 = next(f for f in cover_formats(NANO_7) if f.format_id == 1016)
                    read = lib.artwork_read(reloaded.library.snapshot.tracks[0].artwork_id, (fmt1016,), 57)
                    self.assertEqual(load_cover(root, read).size, (57, 57))


class Tier3ShuffleTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.site = Path(tmp.name).resolve()
        self.backups = self.site / "backups"

    def test_shuffle_2g_writes_itunessd_v1_converts_alac_and_rolls_back(self):
        root = self.site / "shuffle2"
        with patch.object(system, "volume_ids", return_value=(r"USB\VID_05AC&PID_1301\SHUF2",)):
            dev = make_shuffle_device(root, version=1)
        self.assertEqual((dev.writable, dev.shuffle_version, dev.supports_alac, dev.supports_covers),
                         (True, 1, False, False))
        before_sd = (root / SD).read_bytes()

        # Interrupted write rolls back iTunesSD cleanly.
        def abort(msg):
            if msg == "提交曲库…":
                raise SystemExit()
        with self.assertRaises(SystemExit):
            transfer(dev, [FIXTURES / "tone.mp3"], self.backups, report=abort)
        recover_interrupted(dev, self.backups)
        self.assertEqual((root / SD).read_bytes(), before_sd)

        # Clean write: ALAC is converted to AAC-LC, MP3 stays MP3, iTunesSD v1 matches 18 + 558*2 bytes.
        transfer(dev, [FIXTURES / "tone.mp3", FIXTURES / "lossless.m4a"], self.backups)
        sd_bytes = (root / SD).read_bytes()
        self.assertEqual(len(sd_bytes), 18 + 558 * 2)
        self.assertEqual(shuffle_db.sd_version_from_bytes(sd_bytes), 1)
        reloaded = Device.open(root)
        self.assertEqual([t.metadata.file_format for t in reloaded.library.snapshot.tracks],
                         ["MPEG audio file", "AAC audio file"])
        self.assertEqual(list(shuffle_db.parse_itunessd(sd_bytes)),
                         [t.metadata.location for t in reloaded.library.snapshot.tracks])
        self.assertFalse((root / ART_DB).exists())

    def test_shuffle_4g_writes_bdhs_v2_with_alac_playlists_and_sd_only_recovery(self):
        root = self.site / "shuffle4"
        with patch.object(system, "volume_ids", return_value=(r"USB\VID_05AC&PID_1303\SHUF4",)):
            dev = make_shuffle_device(root, version=2)
        self.assertEqual((dev.writable, dev.shuffle_version, dev.supports_alac, dev.profile.generation),
                         (True, 2, True, "4th Gen"))
        transfer(dev, [FIXTURES / "tone.mp3", FIXTURES / "lossless.m4a"], self.backups, new_playlist_name="跑步")
        sd_bytes = (root / SD).read_bytes()
        self.assertEqual(sd_bytes[:4], b"bdhs")
        # Header reports 2 tracks and 2 playlists (Master + 跑步).
        track_cnt, pl_cnt = struct.unpack_from("<II", sd_bytes, 12)
        self.assertEqual((track_cnt, pl_cnt), (2, 2))
        reloaded = Device.open(root)
        self.assertEqual([t.metadata.file_format for t in reloaded.library.snapshot.tracks],
                         ["MPEG audio file", "Apple Lossless audio file"])
        self.assertEqual(list(shuffle_db.parse_itunessd(sd_bytes)),
                         [t.metadata.location for t in reloaded.library.snapshot.tracks])

        # Delete iTunesDB so only iTunesSD remains: opening synthesizes the library and still allows adding songs.
        (root / DB).unlink()
        with patch.object(system, "volume_ids", return_value=(r"USB\VID_05AC&PID_1303\SHUF4",)):
            sd_only = Device.open(root)
        self.assertTrue(sd_only.writable and sd_only.synthesized_db)
        self.assertEqual(len(sd_only.library.snapshot.tracks), 2)
        transfer(sd_only, [FIXTURES / "aac.m4a"], self.backups)
        self.assertTrue((root / DB).is_file())
        self.assertEqual(len(shuffle_db.parse_itunessd((root / SD).read_bytes())), 3)

    def test_all_device_shapes_and_finishes_render(self):
        for shape in SHAPES:
            for finish in (None, "Silver", "White", "Graphite", "Space Gray", "Slate", "Gold", "U2", "Blue"):
                img = player_image(56, finish, shape)
                self.assertEqual(img.height, 56)
                self.assertGreater(img.width, 10)


if __name__ == "__main__":
    unittest.main()
