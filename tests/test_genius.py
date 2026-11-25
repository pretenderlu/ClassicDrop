import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from test_classicdrop import DB, FIXTURES, GUID, MUSIC, Device, mock_device
from iPodDB.iTunesDB.writer.signature import sign_hash58, verify_hash58
from iPodDB.SQLiteDB.genius import (GeniusData, GeniusDatabase, GeniusMetadataRow, GeniusSimilaritiesRow,
                                    parse_genius_database, serialize_genius_database)
from library import STAGE
from transfer import recover_interrupted, transfer

EXTRAS = "iPod_Control/iTunes/Extras.itdb"
IDS = (1000, 1001, 1002)


class GeniusRemovalTests(unittest.TestCase):
    """Songs synced by iTunes with Genius on carry an identity that joins the iPod's Extras.itdb."""

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.site = Path(temp.name).resolve()
        self.root = self.site / "iPod"
        self.backups = self.site / "backups"
        transfer(mock_device(self.root), [FIXTURES / "tone.mp3", FIXTURES / "aac.m4a", FIXTURES / "lossless.m4a"], self.backups)
        data = bytearray((self.root / DB).read_bytes())
        at = -1
        for identity in IDS:
            at = data.index(b"mhit", at + 1)
            struct.pack_into("<Q", data, at + 0x1E4, identity)
        (self.root / DB).write_bytes(sign_hash58(bytes(data), GUID))
        self.device = Device.open(self.root)
        self.tracks = self.device.library.snapshot.tracks
        self.assertEqual(([t.ipod.genius_id for t in self.tracks], bool(self.device.guid)), (list(IDS), True))

    def write_extras(self):
        rows = GeniusData(metadata=tuple(GeniusMetadataRow(i, 1, None) for i in IDS),
                          similarities=tuple(GeniusSimilaritiesRow(i, 1, None) for i in IDS))
        path = self.root / EXTRAS
        path.write_bytes(serialize_genius_database(GeniusDatabase(rows)))
        return path

    def known(self):
        data = parse_genius_database((self.root / EXTRAS).read_bytes()).data
        return sorted(r.genius_id for r in data.metadata), sorted(r.genius_id for r in data.similarities)

    def remove(self, *positions, **kwargs):
        transfer(self.device, [], self.backups, remove_ids=[self.tracks[p].track_id for p in positions], **kwargs)
        self.device = Device.open(self.root)
        return [t.ipod.genius_id for t in self.device.library.snapshot.tracks]

    def test_removed_songs_leave_the_genius_database_too(self):
        path = self.write_extras()
        before = path.read_bytes()
        self.assertEqual(self.remove(1), [1000, 1002])
        self.assertEqual(self.known(), ([1000, 1002], [1000, 1002]))
        self.assertTrue(verify_hash58(self.device.original, GUID))
        self.assertFalse((self.root / STAGE).exists())
        self.assertEqual(len(list((self.root / MUSIC).rglob("*.m4a"))) + len(list((self.root / MUSIC).rglob("*.mp3"))), 2)
        saved, = self.backups.rglob("Extras.itdb-*")
        self.assertEqual(saved.read_bytes(), before)
        # Adding and playlists never needed the file, and still leave it alone.
        current = path.read_bytes()
        transfer(self.device, [FIXTURES / "aac.m4a"], self.backups, new_playlist_name="路上")
        self.assertEqual(path.read_bytes(), current)

    def test_interruption_before_the_library_commits_leaves_both_files(self):
        path = self.write_extras()
        before = path.read_bytes(), (self.root / DB).read_bytes()
        def interrupt(message):
            if message == "提交曲库…":
                raise SystemExit
        with self.assertRaises(SystemExit):
            self.remove(0, report=interrupt)
        self.assertTrue((self.root / STAGE).exists())
        recover_interrupted(self.device, self.backups)
        self.assertEqual((path.read_bytes(), (self.root / DB).read_bytes()), before)
        self.assertFalse((self.root / STAGE).exists())
        self.assertEqual(len([p for p in (self.root / MUSIC).rglob("*") if p.is_file()]), 3)

    def test_ipod_without_the_file_and_songs_without_identities(self):
        self.assertEqual(self.remove(0), [1001, 1002])  # nothing to prune, and no file is created
        self.assertFalse((self.root / EXTRAS).exists())
        # A song this program added has no identity: its removal does not read the file at all.
        transfer(self.device, [FIXTURES / "tone.mp3"], self.backups)
        self.device = Device.open(self.root)
        self.tracks = self.device.library.snapshot.tracks
        (self.root / EXTRAS).write_bytes(b"not a database")
        self.assertEqual(self.remove(2), [1001, 1002])
        self.assertEqual((self.root / EXTRAS).read_bytes(), b"not a database")

    def test_damaged_genius_database_stops_before_anything_changes(self):
        (self.root / EXTRAS).write_bytes(b"not a database")
        before = (self.root / DB).read_bytes()
        with self.assertRaisesRegex(ValueError, "Genius 数据（Extras.itdb）已损坏"):
            self.remove(0)
        self.assertEqual(((self.root / DB).read_bytes(), (self.root / EXTRAS).read_bytes()), (before, b"not a database"))
        self.assertFalse((self.root / STAGE).exists())
        self.assertEqual(len([p for p in (self.root / MUSIC).rglob("*") if p.is_file()]), 3)
