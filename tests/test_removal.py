import json
import plistlib
import stat
import system
import struct
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from test_classicdrop import FIXTURES, GUID, mock_device
from iPodDB.iTunesDB.writer.signature import sign_hash58, verify_hash58
from iPodDB.library import MediaType
from library import DB, MUSIC, STAGE, Device, is_music, playlist_edit, rename_device
from transfer import export_tracks, recover_interrupted, transfer


def positions(magic, header, width, values):
    return (struct.pack("<4sIII", magic, header, width, len(values)).ljust(header, b"\0")
            + b"".join(struct.pack("<I", n).ljust(width, b"\0") for n in values))


class MusicRemovalTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.site = Path(temp.name).resolve()
        self.root = self.site / "IPC3"
        self.backups = self.site / "backup"
        self.db = self.root / DB
        self.device = mock_device(self.root)
        self.run_write(files=[FIXTURES / name for name in ("tone.mp3", "aac.m4a", "lossless.m4a")])

    def refresh(self):
        self.device = Device.open(self.root)
        return self.device.library.snapshot

    def run_write(self, files=(), **changes):
        transfer(self.device, files, self.backups, **changes)
        return self.refresh()

    def test_rename_single_delete_playlist_and_positional_history(self):
        old = self.device.library.snapshot
        ids = [t.track_id for t in old.tracks]
        self.run_write(desired=playlist_edit(old, ids, name="我喜欢的歌"))
        before = self.device.original
        snap = self.run_write(desired=rename_device(self.device.library.snapshot, "  路上的 iPod  "))
        self.assertEqual(snap.device_name, "路上的 iPod")
        self.assertEqual(snap.tracks, old.tracks)
        self.assertIn(before, [p.read_bytes() for p in self.backups.rglob("iTunesDB")])
        for invalid in (" ", "a\nb", "a" * 129):
            with self.assertRaises(ValueError):
                rename_device(snap, invalid)
        counts, otg = self.db.parent / "Play Counts", self.db.parent / "OTGPlaylistInfo_1"
        counts.write_bytes(positions(b"mhdp", 96, 16, (11, 22, 33)))
        counts.chmod(stat.S_IREAD)
        self.addCleanup(counts.chmod, stat.S_IWRITE)
        otg.write_bytes(positions(b"mhpo", 20, 4, (2, 1, 0, 1)))
        empty_otg = self.db.parent / "OTGPlaylistInfo_2"
        empty_otg.touch()
        plist = self.db.parent / "PlayCounts.plist"
        rows = [{"persistentID": t.ipod.db_track_id, "playCount": n} for n,t in enumerate(snap.tracks)]
        plist.write_bytes(plistlib.dumps({"tracks": rows, "other": "keep"}))
        after = self.run_write(remove_ids=(ids[1], ids[1]))
        self.assertEqual(after.device_name, "路上的 iPod")
        self.assertEqual(tuple(t.track_id for t in after.tracks), (ids[0],ids[2]))
        self.assertEqual(after.playlists[0].track_ids, (ids[0],ids[2]))
        self.assertFalse((self.root / snap.tracks[1].metadata.location).exists())
        self.assertEqual(counts.read_bytes(), positions(b"mhdp", 96, 16, (11, 33)))
        self.assertTrue(system.is_readonly(counts.stat()))
        self.assertEqual(otg.read_bytes(), positions(b"mhpo", 20, 4, (1,0)))
        self.assertEqual(empty_otg.read_bytes(), b"")
        self.assertEqual(plistlib.loads(plist.read_bytes()), {"tracks": [rows[0],rows[2]], "other": "keep"})
        self.assertTrue(verify_hash58(self.device.original, GUID))

    def test_clear_music_preserves_other_media_files_settings_and_empty_normal_playlist(self):
        snap = self.device.library.snapshot
        self.run_write(desired=playlist_edit(snap, [t.track_id for t in snap.tracks[:2]], name="保留歌单"))
        raw = bytearray(self.device.original)
        row = raw.find(b"mhit")
        for _ in range(2):
            row = raw.index(b"mhit", row + struct.unpack_from("<I", raw, row+8)[0])
        struct.pack_into("<I", raw, row + 0xD0, 8)  # native Audiobook bit on an M4A
        self.db.write_bytes(sign_hash58(raw, GUID))
        snap = self.refresh()
        book = snap.tracks[-1]
        self.assertFalse(is_music(book))
        self.assertTrue(all(not is_music(replace(snap.tracks[0], media_types=(kind,)))
                            for kind in MediaType if kind != MediaType.AUDIO))
        memo = replace(snap.tracks[0], metadata=replace(snap.tracks[0].metadata, location="Recordings/note.wav"))
        self.assertFalse(is_music(memo))
        loose = self.root / MUSIC / "F00/private.mp3"
        settings = self.root / "iPod_Control/Device/Preferences"
        note = self.root / "Notes/trip.txt"
        note.parent.mkdir()
        fixed = {loose: b"unindexed", settings: b"my settings", note: b"journal",
                 self.root / book.metadata.location: (FIXTURES / "lossless.m4a").read_bytes()}
        for path, data in fixed.items():
            if not path.exists():
                path.write_bytes(data)
        with self.assertRaises(ValueError):
            self.run_write(remove_ids=[book.track_id])
        after = self.run_write(remove_ids=[t.track_id for t in snap.tracks if is_music(t)])
        self.assertEqual([t.track_id for t in after.tracks], [book.track_id])
        self.assertEqual([(p.name,p.track_ids) for p in after.playlists], [("保留歌单", ())])
        self.assertEqual({path: path.read_bytes() for path in fixed}, fixed)
        self.assertFalse((self.root / STAGE).exists())
        self.assertEqual(len(self.run_write(files=[FIXTURES / "tone.mp3"]).tracks), 2)

    def test_shared_file_and_accidental_omission(self):
        snap = self.device.library.snapshot
        with self.assertRaises(ValueError):
            self.run_write(desired=replace(snap, tracks=snap.tracks[1:]))
        a,b = (Path(t.metadata.location).name for t in snap.tracks[1:])
        raw = self.device.original
        self.assertEqual(raw.count(b.encode("utf-16-le")), 1)
        self.db.write_bytes(sign_hash58(raw.replace(b.encode("utf-16-le"), a.encode("utf-16-le")), GUID))
        snap = self.refresh()
        shared = self.root / snap.tracks[1].metadata.location
        self.run_write(remove_ids=[snap.tracks[1].track_id])
        self.assertEqual(shared.read_bytes(), (FIXTURES / "aac.m4a").read_bytes())

    def test_clear_on_mixed_device_preserves_podcast_and_video(self):
        raw = bytearray(self.device.original)
        row = raw.index(b"mhit")
        for media_bit in (4, 2):
            row = raw.index(b"mhit", row + struct.unpack_from("<I", raw, row+8)[0])
            struct.pack_into("<I", raw, row + 0xD0, media_bit)
        self.db.write_bytes(sign_hash58(raw, GUID))
        before = self.refresh()
        keep = {self.root / t.metadata.location: (self.root / t.metadata.location).read_bytes()
                for t in before.tracks[1:]}
        self.assertEqual([is_music(t) for t in before.tracks], [True,False,False])
        after = self.run_write(remove_ids=[t.track_id for t in before.tracks if is_music(t)])
        self.assertEqual([t.track_id for t in after.tracks], [t.track_id for t in before.tracks[1:]])
        self.assertEqual({path: path.read_bytes() for path in keep}, keep)

    def test_read_only_audio_deleted_only_after_commit(self):
        track = self.device.library.snapshot.tracks[0]
        media = self.root / track.metadata.location
        media.chmod(stat.S_IREAD)
        try:
            def stop(message):
                if message == "提交曲库…":
                    raise SystemExit()
            with self.assertRaises(SystemExit):
                self.run_write(remove_ids=[track.track_id], report=stop)
            recover_interrupted(self.device, self.backups)
            self.assertTrue(system.is_readonly(media.stat()))
            self.run_write(remove_ids=[track.track_id])
            self.assertFalse(media.exists())
        finally:
            if media.exists():
                media.chmod(stat.S_IWRITE)

    def test_partial_quarantine_rollback_and_tampered_receipt(self):
        before = self.device.original
        tracks = self.device.library.snapshot.tracks
        original = {self.root / t.metadata.location: (self.root / t.metadata.location).read_bytes() for t in tracks}
        def stop(msg):
            if msg.startswith("准备删除 3/3"):
                raise SystemExit()
        with self.assertRaises(SystemExit):
            self.run_write(remove_ids=[t.track_id for t in tracks], report=stop)
        self.assertEqual(self.db.read_bytes(), before)
        stage = self.root / STAGE
        journal = json.loads((stage / "plan.json").read_bytes())
        self.assertEqual(len(list((stage / "removed").iterdir())), 2)
        receipt = next(self.backups.rglob("delete-" + journal["receipt"] + ".json"))
        saved = receipt.read_bytes()
        receipt.write_bytes(saved + b" ")
        with self.assertRaises(ValueError):
            recover_interrupted(self.device, self.backups)
        receipt.write_bytes(saved)
        recover_interrupted(self.device, self.backups)
        recover_interrupted(self.device, self.backups)
        self.assertEqual({p: p.read_bytes() for p in original}, original)
        self.assertFalse(stage.exists())

    def test_after_commit_finish_cleanup_resume_empty_and_reimport(self):
        tracks = self.device.library.snapshot.tracks
        slot = self.root / STAGE / "removed/1"
        real_unlink = Path.unlink
        def interrupt(path, *args, **kw):
            if path == slot:
                raise SystemExit()
            return real_unlink(path, *args, **kw)
        with patch.object(Path, "unlink", interrupt), self.assertRaises(SystemExit):
            self.run_write(remove_ids=[t.track_id for t in tracks])
        self.assertEqual(self.refresh().tracks, ())
        self.assertTrue(slot.is_file())
        recover_interrupted(self.device, self.backups)
        self.assertFalse((self.root / STAGE).exists())
        self.assertEqual(list((self.root / MUSIC).rglob("*.m*")), [])
        self.assertEqual(len(self.run_write(files=[FIXTURES / "aac.m4a"]).tracks), 1)

    def test_delete_clear_and_export_allow_size_mismatch_and_guard_non_file(self):
        tracks = self.device.library.snapshot.tracks
        edited = self.root / tracks[0].metadata.location
        modified_bytes = edited.read_bytes() + b"\x00" * 512  # e.g. tags/cover edited directly on iPod
        edited.write_bytes(modified_bytes)

        # Exporting a track whose on-disk size differs from iTunesDB still succeeds and preserves exact bytes.
        out = self.site / "exported"
        out.mkdir()
        with self.assertLogs("ClassicDrop", level="WARNING") as export_logs:
            self.assertEqual(export_tracks(self.device, [tracks[0]], out), 1)
        self.assertTrue(any("导出音频大小与曲库记录不符" in msg for msg in export_logs.output))
        exported_files = list(out.iterdir())
        self.assertEqual(len(exported_files), 1)
        self.assertEqual(exported_files[0].read_bytes(), modified_bytes)

        # Clearing the music library removes the size-mismatched file cleanly without aborting.
        with self.assertLogs("ClassicDrop", level="WARNING") as del_logs:
            after = self.run_write(remove_ids=[t.track_id for t in tracks])
        self.assertTrue(any("音频文件大小与曲库记录不符" in msg for msg in del_logs.output))
        self.assertEqual(after.tracks, ())
        self.assertFalse(edited.exists())
        self.assertEqual(list((self.root / MUSIC).rglob("*.m*")), [])
        self.assertTrue(verify_hash58(self.device.original, GUID))

        # A directory masquerading at a track's path must still be rejected.
        reloaded = self.run_write(files=[FIXTURES / "tone.mp3"])
        bad = self.root / reloaded.tracks[0].metadata.location
        bad.unlink()
        bad.mkdir()
        with self.assertRaises(ValueError):
            self.run_write(remove_ids=[reloaded.tracks[0].track_id])
