import hashlib
import logging
import os
from dataclasses import replace
from pathlib import Path
import plistlib
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]
from mutagen.easyid3 import EasyID3
from iPodDB.library import IPodLibrary, WriteTarget, WriteChecksum, create_empty_library
from iPodDB.iTunesDB.writer.signature import sign_hash58, sign_hash72, verify_hash58, verify_hash72
from library import DB, MUSIC, Device, fingerprint, playlist_edit
from transfer import STAGE, device_lock, export_tracks, recover_interrupted, transfer

logging.disable(logging.WARNING)
GUID = bytes.fromhex("0011223344556677")
FIXTURES = ROOT / "tests" / "fixtures"


def mock_device(root):
    (root / DB).parent.mkdir(parents=True)
    (root / MUSIC / "F00").mkdir(parents=True)
    info = root / "iPod_Control/Device/SysInfoExtended"
    info.parent.mkdir()
    info.write_bytes(plistlib.dumps({"FireWireGUID": int.from_bytes(GUID, "big")}))
    target = WriteTarget(checksum=WriteChecksum.HASH58, firewire_guid=GUID, database_version=0x30)
    prepared = create_empty_library("测试 iPod", target, supports_podcasts=True).prepared
    (root / DB).write_bytes(prepared.itunes)
    return Device.open(root)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.site = Path(self.temp.name)
        self.root = self.site / "ipod"
        self.backups = self.site / "backups"
        self.device = mock_device(self.root)
        self.audio = FIXTURES / "tone.mp3"

    def send(self, files, **kw):
        return transfer(self.device, files, self.backups, **kw)

    def refresh(self):
        self.device = Device.open(self.root)
        return self.device.library.snapshot

    def test_three_formats_backup_export_and_repeat(self):
        original = self.device.original
        files = [self.audio, FIXTURES / "aac.m4a", FIXTURES / "lossless.m4a"]
        added, skipped, backup = self.send(files + [self.audio])
        self.assertEqual((added, skipped), (3, 1))
        self.assertEqual(backup.read_bytes(), original)
        snapshot = self.refresh()
        self.assertEqual([t.artist for t in snapshot.tracks], ["测试歌手"] * 3)
        for f, t in zip(files, snapshot.tracks, strict=True):
            self.assertEqual(fingerprint(f), fingerprint(self.root / t.metadata.location))
        self.assertTrue(verify_hash58(self.device.original, GUID))
        self.assertEqual(self.send(files), (0, 3, None))
        self.assertEqual((self.root / DB).read_bytes(), self.device.original)
        out = self.site / "export"
        out.mkdir()
        self.assertEqual(export_tracks(self.device, snapshot.tracks, out), 3)
        self.assertEqual(export_tracks(self.device, snapshot.tracks, out), 0)
        self.assertEqual({fingerprint(p) for p in out.iterdir()}, {fingerprint(p) for p in files})

    def test_manage_playlist_and_tags(self):
        self.send([self.audio, FIXTURES / "aac.m4a"])
        snapshot = self.refresh()
        first, second = (t.track_id for t in snapshot.tracks)
        self.send([], desired=playlist_edit(snapshot, [first], name="通勤"))
        snapshot = self.refresh()
        playlist_id = snapshot.playlists[0].playlist_id
        self.assertEqual(snapshot.playlists[0].track_ids, (first,))
        self.send([], desired=playlist_edit(snapshot, [first, second], playlist_id=playlist_id))
        snapshot = self.refresh()
        self.assertEqual(snapshot.playlists[0].track_ids, (first, second))
        self.send([], desired=playlist_edit(snapshot, [first], playlist_id=playlist_id, remove=True))
        snapshot = self.refresh()
        self.assertEqual(snapshot.playlists[0].track_ids, (second,))
        old = snapshot.tracks[0]
        changed = replace(old, title="新标题", artist="新歌手", album="新专辑")
        self.send([], desired=replace(snapshot, tracks=(changed, snapshot.tracks[1])))
        after = self.refresh()
        self.assertEqual((after.tracks[0].title, after.tracks[0].artist, after.tracks[0].album),
                         ("新标题", "新歌手", "新专辑"))
        self.assertEqual(after.playlists[0].track_ids, (second,))
        self.assertEqual(fingerprint(self.root / old.metadata.location), fingerprint(self.audio))

    def test_55_track_batch_with_existing_library_and_play_counts(self):
        self.send([self.audio])
        before = self.refresh()
        self.send([], desired=playlist_edit(before, [before.tracks[0].track_id], name="原歌单"))
        before = self.refresh()
        counts = self.root / "iPod_Control/iTunes/Play Counts"
        counts.write_bytes(b"mhdp" + (16).to_bytes(4, "little") + (16).to_bytes(4, "little")
                           + (1).to_bytes(4, "little") + bytes(16))
        files = []
        for n in range(55):
            p = self.site / f"{n:02}.mp3"
            shutil.copyfile(self.audio, p)
            tags = EasyID3(p)
            tags["title"] = f"新歌 {n}"
            tags.save(p)
            files.append(p)
        self.assertEqual(self.send(files)[:2], (55, 0))
        after = self.refresh()
        self.assertEqual(len(after.tracks), 56)
        self.assertEqual(after.tracks[0].ipod.db_track_id, before.tracks[0].ipod.db_track_id)
        self.assertEqual(after.playlists, before.playlists)
        self.assertEqual(counts.stat().st_size, 32)
        self.assertEqual(self.send(files)[:2], (0, 55))

    def test_contention_invalid_audio_and_read_only(self):
        original = self.device.original
        with device_lock(self.device), self.assertRaises(OSError):
            self.send([self.audio])
        fake = self.site / "not_music.mp3"
        fake.write_bytes(b"fake")
        with self.assertRaises(ValueError):
            self.send([self.audio, fake])
        (self.root / "iPod_Control/Device/SysInfoExtended").unlink()
        self.assertIsNone(Device.open(self.root).guid)
        with self.assertRaises(ValueError):
            Device.open(self.root, "deadbeefdeadbeef")
        self.assertEqual(Device.open(self.root, GUID.hex()).guid, GUID)
        self.assertEqual((self.root / DB).read_bytes(), original)
        self.assertEqual(list((self.root / MUSIC).rglob("*.mp3")), [])

    def test_interruption_before_and_after_commit(self):
        files = [self.audio, FIXTURES / "aac.m4a"]
        def crash(message):
            if "2/2" in message and message.startswith("复制"):
                raise SystemExit()
        with self.assertRaises(SystemExit):
            self.send(files, report=crash)
        self.assertEqual((self.root / DB).read_bytes(), self.device.original)
        self.assertTrue((self.root / STAGE).exists())
        self.assertEqual(len(list((self.root / MUSIC).rglob("*.mp3"))), 1)
        recover_interrupted(self.device)
        self.assertFalse((self.root / STAGE).exists())
        self.assertEqual(list((self.root / MUSIC).rglob("*.mp3")), [])
        from transfer import recover
        def crash_after_commit(dev, backups=None):
            if (self.root / DB).read_bytes() != dev.original:
                raise SystemExit()
            recover(dev, backups)
        with patch("transfer.recover", side_effect=crash_after_commit), self.assertRaises(SystemExit):
            self.send(files)
        self.assertEqual(len(self.refresh().tracks), 2)
        recover_interrupted(self.device)
        self.assertEqual(self.send(files)[:2], (0, 2))

    def test_dual_signature_and_stale_device(self):
        iv, random = bytes(range(16)), bytes(range(20, 32))
        dual = sign_hash58(sign_hash72(self.device.original, iv, random, hashing_scheme=1), GUID)
        (self.root / DB).write_bytes(dual)
        with self.assertRaises(ValueError):
            self.send([self.audio])
        self.refresh()
        self.send([self.audio])
        final = (self.root / DB).read_bytes()
        self.assertTrue(verify_hash72(final, iv, random, hashing_scheme=1))
        self.assertTrue(verify_hash58(final, GUID))
        self.assertEqual(len(IPodLibrary(final).snapshot.tracks), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
