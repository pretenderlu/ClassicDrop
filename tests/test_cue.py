import math
import sys
import time
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from mutagen.apev2 import APEBinaryValue, APEv2
from mutagen.mp4 import MP4
from mutagen.wavpack import WavPack

from test_classicdrop import DB, Device, mock_device
from test_formats_playlists import ffmpeg
from test_native_covers import ART_DB, FORMATS, picture
import app
from cue import CueKey, parse_cue
from import_dialog import MODE_NAMES
from music_import import scan_inputs
from transfer import transfer


def write_album_wav(path, seconds=6, rate=44100, channels=2, width=2):
    """Synthesize a deterministic PCM WAV whose frames have unique frequencies per second."""
    total_samples = rate * seconds
    amplitude = 8500 * 256 ** (width - 2)
    frames = bytearray(total_samples * channels * width)
    pos = 0
    for i in range(total_samples):
        freq = 300 + (i // rate) * 90
        for c in range(channels):
            val = int(amplitude * math.sin(2 * math.pi * (freq + c * 35) * i / rate))
            frames[pos:pos + width] = val.to_bytes(width, "little", signed=True)
            pos += width
    with wave.open(str(path), "wb") as stream:
        stream.setparams((channels, width, rate, 0, "NONE", "unused"))
        stream.writeframes(frames)
    return bytes(frames)


def scan(paths, known=None):
    return scan_inputs(paths, known or {}, lambda _: None, lambda: False)


class CueSplitTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.site = Path(temp.name).resolve()
        self.root = self.site / "iPod"
        self.dev = mock_device(self.root)
        self.backups = self.site / "backups"

    def send(self, songs):
        reviewed = {s.key: s for s in songs}
        result = transfer(self.dev, tuple(reviewed), self.backups, reviewed=reviewed,
                          formats={k: s.mode for k, s in reviewed.items()})
        self.dev = Device.open(self.dev.root)
        return result

    def test_gbk_cue_flac_seamless_split_is_bit_exact_with_tags_cover_and_dedup(self):
        album = self.site / "音乐/周杰伦/2003-叶惠美"
        album.mkdir(parents=True)
        wav = self.site / "master.wav"
        pcm = write_album_wav(wav, seconds=6)
        flac = album / "叶惠美.flac"
        ffmpeg("-i", wav, "-c:a", "flac", flac)
        (album / "cover.png").write_bytes(picture("blue"))

        # References .wav while only .flac sits beside it; track 2 has an INDEX 00 pregap.
        cue_text = (
            'REM GENRE Pop\r\n'
            'REM DATE 2003\r\n'
            'REM DISCNUMBER 1\r\n'
            'REM TOTALDISCS 2\r\n'
            'PERFORMER "周杰伦"\r\n'
            'TITLE "叶惠美"\r\n'
            'FILE "叶惠美.wav" WAVE\r\n'
            '  TRACK 01 AUDIO\r\n'
            '    TITLE "以父之名"\r\n'
            '    PERFORMER "周杰伦"\r\n'
            '    INDEX 01 00:00:00\r\n'
            '  TRACK 02 AUDIO\r\n'
            '    TITLE "懦夫"\r\n'
            '    PERFORMER "周杰伦 / 余妮"\r\n'
            '    INDEX 00 00:01:60\r\n'
            '    INDEX 01 00:02:00\r\n'
            '  TRACK 03 AUDIO\r\n'
            '    TITLE "晴天"\r\n'
            '    INDEX 01 00:04:15\r\n'
        )
        cue = album / "叶惠美.cue"
        cue.write_bytes(cue_text.encode("gb18030"))

        songs, errors = scan([album])
        self.assertEqual(errors, [])
        self.assertEqual(len(songs), 3)
        self.assertTrue(all(isinstance(s.key, CueKey) for s in songs))
        self.assertEqual([s.title for s in songs], ["以父之名", "懦夫", "晴天"])
        self.assertEqual([s.artist for s in songs], ["周杰伦", "周杰伦 / 余妮", "周杰伦"])
        self.assertEqual([s.album for s in songs], ["叶惠美"] * 3)
        self.assertEqual([s.choices for s in songs], [("alac", "aac")] * 3)
        self.assertTrue(all(s.cover and s.cover.path.name == "cover.png" for s in songs))

        # Dropping both the audio image and the .cue directly must also deduplicate the image.
        both, both_err = scan([flac, cue])
        self.assertEqual(both_err, [])
        self.assertEqual([s.key for s in both], [s.key for s in songs])

        self.assertEqual(self.send(songs)[:2], (3, 0))
        lib = self.dev.library.with_artwork((self.root / ART_DB).read_bytes())
        tracks = lib.snapshot.tracks
        self.assertEqual(len(tracks), 3)
        # Expected sample spans (44100 / 75 = 588 samples per CD frame, 4 bytes per stereo 16-bit sample):
        # Track 1: 0..150 frames (keeps track 2's pregap seamlessly up to track 2's INDEX 01)
        # Track 2: 150..315 frames
        # Track 3: 315..450 frames (end of file)
        spans = [(0, 150 * 588), (150 * 588, 315 * 588), (315 * 588, 450 * 588)]
        for i, (track, (s0, s1)) in enumerate(zip(tracks, spans, strict=True), 1):
            self.assertEqual((track.track_number, track.metadata.total_tracks, track.year, track.genre,
                              track.metadata.disc_number, track.metadata.total_discs),
                             (i, 3, 2003, "Pop", 1, 2))
            self.assertIsNotNone(lib.artwork_read(track.artwork_id, FORMATS, 320))
            dest = self.root / track.metadata.location
            decoded = ffmpeg("-i", dest, "-f", "s16le", "-")
            self.assertEqual(decoded, pcm[s0 * 4:s1 * 4])

        # Re-importing the exact same CUE batch deduplicates cleanly via deterministic SHA-256.
        self.assertEqual(self.send(songs), (0, 3, None))

    def test_wavpack_tta_aiff_and_alac_images_and_single_tracks(self):
        wav = self.site / "src.wav"
        pcm = write_album_wav(wav, seconds=4)
        for ext, codec in ((".wv", "wavpack"), (".tta", "tta"), (".aiff", "pcm_s16be"), (".m4a", "alac")):
            with self.subTest(ext=ext):
                folder = self.site / f"fmt-{ext.lstrip('.')}"
                folder.mkdir()
                image = folder / f"renamed_album{ext}"
                ffmpeg("-i", wav, "-c:a", codec, image)
                if ext == ".wv":
                    wv = WavPack(image)
                    wv.tags = APEv2()
                    wv.tags["Artist"] = "整轨歌手"
                    wv.tags["Cover Art (Front)"] = APEBinaryValue(b"front.png\0" + picture("red"))
                    wv.save()
                # CUE mentions a different filename; sole-audio fallback matches renamed_album.<ext>.
                cue = folder / "disc.cue"
                cue.write_text(
                    'TITLE "测试碟"\nFILE "Original.wav" WAVE\n'
                    '  TRACK 01 AUDIO\n    TITLE "第一段"\n    INDEX 01 00:00:00\n'
                    '  TRACK 02 AUDIO\n    TITLE "第二段"\n    INDEX 01 00:02:00\n',
                    encoding="utf-8",
                )
                songs, errors = scan([cue])
                self.assertEqual(errors, [])
                self.assertEqual([s.mode for s in songs], ["alac", "alac"])
                if ext == ".wv":
                    self.assertEqual(songs[0].artist, "整轨歌手")
                    self.assertEqual(songs[0].cover.label, "内嵌封面")
                self.dev = mock_device(self.site / f"ipod-{ext.lstrip('.')}")
                self.assertEqual(self.send(songs)[:2], (2, 0))
                tracks = self.dev.library.snapshot.tracks
                self.assertEqual(ffmpeg("-i", self.dev.root / tracks[0].metadata.location, "-f", "s16le", "-"),
                                 pcm[:150 * 588 * 4])
                self.assertEqual(ffmpeg("-i", self.dev.root / tracks[1].metadata.location, "-f", "s16le", "-"),
                                 pcm[150 * 588 * 4:])
                # Standalone .wv / .tta / .aiff files also scan and convert cleanly without a CUE.
                if ext != ".m4a":
                    solo, solo_err = scan([image])
                    self.assertEqual(solo_err, [])
                    self.assertEqual(len(solo), 1)
                    self.assertIsNone(solo[0].cue)
                    self.assertEqual(solo[0].mode, "alac")

    def test_cue_split_recovers_flac_with_trailing_junk(self):
        folder = self.site / "junk-cue"
        folder.mkdir()
        wav = self.site / "clean.wav"
        pcm = write_album_wav(wav, seconds=4)
        flac = folder / "album.flac"
        ffmpeg("-i", wav, "-c:a", "flac", flac)
        flac.write_bytes(flac.read_bytes() + b"TAG" + b"TRAILING JUNK".ljust(125, b" "))
        cue = folder / "album.cue"
        cue.write_text(
            'TITLE "修复专辑"\nPERFORMER "歌手"\nFILE "album.flac" WAVE\n'
            '  TRACK 01 AUDIO\n    TITLE "上半"\n    INDEX 01 00:00:00\n'
            '  TRACK 02 AUDIO\n    TITLE "下半"\n    INDEX 01 00:02:00\n',
            encoding="utf-8",
        )
        songs, errors = scan([folder])
        self.assertEqual(errors, [])
        self.assertEqual(self.send(songs)[:2], (2, 0))
        tracks = self.dev.library.snapshot.tracks
        self.assertEqual(ffmpeg("-i", self.root / tracks[0].metadata.location, "-f", "s16le", "-"),
                         pcm[:150 * 588 * 4])
        self.assertEqual(ffmpeg("-i", self.root / tracks[1].metadata.location, "-f", "s16le", "-"),
                         pcm[150 * 588 * 4:])

    def test_cue_validation_blocks_traversal_overflow_and_post_preview_edits(self):
        folder = self.site / "bad-cue"
        folder.mkdir()
        wav = folder / "ok.wav"
        write_album_wav(wav, seconds=3)

        traversal = folder / "evil.cue"
        traversal.write_text('FILE "../secret.wav" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "路径无效"):
            parse_cue(traversal, {".wav"})

        overflow = folder / "over.cue"
        overflow.write_text(
            'FILE "ok.wav" WAVE\n'
            '  TRACK 01 AUDIO\n    TITLE "A"\n    INDEX 01 00:00:00\n'
            '  TRACK 02 AUDIO\n    TITLE "B"\n    INDEX 01 00:05:00\n',
            encoding="utf-8",
        )
        songs, errors = scan([overflow])
        self.assertEqual(songs, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("超过整轨音频长度", errors[0])

        good = folder / "good.cue"
        good.write_text(
            'FILE "ok.wav" WAVE\n'
            '  TRACK 01 AUDIO\n    TITLE "A"\n    INDEX 01 00:00:00\n'
            '  TRACK 02 AUDIO\n    TITLE "B"\n    INDEX 01 00:01:30\n',
            encoding="utf-8",
        )
        songs, errors = scan([good])
        self.assertEqual((len(songs), errors), (2, []))
        good.write_text(good.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        before = (self.root / DB).read_bytes()
        with patch("transfer.device_lock") as lock, self.assertRaisesRegex(ValueError, "CUE 文件已变化"):
            self.send(songs)
        lock.assert_not_called()
        self.assertEqual((self.root / DB).read_bytes(), before)

    def test_ui_preview_edits_removes_and_transfers_cue_tracks(self):
        folder = self.site / "ui-cue"
        folder.mkdir()
        wav = folder / "live.wav"
        write_album_wav(wav, seconds=6)
        cue = folder / "live.cue"
        cue.write_text(
            'TITLE "现场版"\nPERFORMER "原歌手"\nFILE "live.wav" WAVE\n'
            '  TRACK 01 AUDIO\n    TITLE "序曲"\n    INDEX 01 00:00:00\n'
            '  TRACK 02 AUDIO\n    TITLE "主打"\n    INDEX 01 00:02:00\n'
            '  TRACK 03 AUDIO\n    TITLE "安可"\n    INDEX 01 00:04:00\n',
            encoding="utf-8",
        )
        window = app.TkinterDnD.Tk()
        def cleanup():
            for timer in window.tk.call("after", "info"):
                window.after_cancel(timer)
            if window.winfo_exists():
                window.destroy()
        self.addCleanup(cleanup)
        window.geometry("1000x640+28000+28000")
        ui = app.App(window)
        ui.device = self.dev
        ui.render()
        window.update()

        errors = []
        done = False
        deadline = time.monotonic() + 15
        def poll():
            nonlocal done
            dialog = next((w for w in window.winfo_children() if isinstance(w, app.ImportDialog)), None)
            if dialog and dialog.rows and not dialog.scanning:
                try:
                    self.assertEqual(len(dialog.rows), 3)
                    self.assertIn("CUE 自动分轨", dialog.hint["text"])
                    iids = dialog.tree.get_children()
                    # Remove track 3, change track 2 to AAC, rename artist for remaining tracks.
                    dialog.tree.selection_set(iids[2])
                    window.update()
                    dialog.remove_selected()
                    dialog.select_all()
                    window.update()
                    dialog.values["artist"].set("统一歌手")
                    remaining = dialog.tree.get_children()
                    dialog.tree.selection_set(remaining[1])
                    window.update()
                    dialog.mode.set(MODE_NAMES["aac"])
                    dialog.change_mode()
                    dialog.send_button.invoke()
                except BaseException as exc:
                    errors.append(exc)
                    dialog.cancel()
                done = True
            elif time.monotonic() >= deadline:
                errors.append(AssertionError("CUE preview dialog timed out"))
                done = True
            else:
                window.after(40, poll)

        timer = window.after(50, poll)
        with patch.object(app, "BACKUPS", self.backups), patch.object(app.messagebox, "showerror") as err_box:
            try:
                ui.add_files([folder])
                while not done:
                    window.update()
                    time.sleep(0.01)
            finally:
                window.after_cancel(timer)
            if errors:
                raise errors[0]
            end = time.monotonic() + 20
            while ui.busy and time.monotonic() < end:
                window.update()
                time.sleep(0.01)
            self.assertFalse(ui.busy)
            err_box.assert_not_called()

        snap = ui.device.library.snapshot
        self.assertEqual([(t.title, t.artist, t.album, t.track_number, t.metadata.file_format) for t in snap.tracks], [
            ("序曲", "统一歌手", "现场版", 1, "Apple Lossless audio file"),
            ("主打", "统一歌手", "现场版", 2, "AAC audio file"),
        ])
        self.assertEqual(MP4(self.root / snap.tracks[0].metadata.location).info.codec, "alac")
        self.assertEqual(MP4(self.root / snap.tracks[1].metadata.location).info.codec, "mp4a.40.2")


if __name__ == "__main__":
    unittest.main()
