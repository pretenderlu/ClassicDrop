import math
import struct
import subprocess
import system
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_classicdrop import DB, FIXTURES, GUID, MUSIC, Device, mock_device
from imageio_ffmpeg import get_ffmpeg_exe
from mutagen.easymp4 import EasyMP4
from mutagen.flac import FLAC
from iPodDB.iTunesDB.writer.signature import verify_hash58
from iPodDB.library import PlaylistKind, PlaylistSortOrder, playlist_entries
from audio_convert import _verify_duration, prepared_files, source_audio
from library import audio_track, delete_playlist, fingerprint, move_playlist_entries, playlist_edit, rename_playlist
from music_import import scan_inputs
from transfer import transfer


def write_wave(path, rate=44100, channels=2, width=2):
    amplitude = 8500 * 256 ** (width - 2)
    frames = b"".join(int(amplitude * math.sin(i / 17 + c)).to_bytes(width, "little", signed=True)
                      for i in range(4097) for c in range(channels))
    with wave.open(str(path), "wb") as stream:
        stream.setparams((channels, width, rate, 0, "NONE", "unused"))
        stream.writeframes(frames)
    return frames


def ffmpeg(*args):
    return subprocess.run([get_ffmpeg_exe(), "-nostdin", "-v", "error", *map(str, args)], check=True,
                          capture_output=True, **system.NO_WINDOW).stdout


class FormatsAndPlaylists(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.site = Path(temp.name).resolve()
        self.root = self.site / "IPC3"
        self.device = mock_device(self.root)
        self.backups = self.site / "backups"

    def send(self, files=(), **kwargs):
        result = transfer(self.device, files, self.backups, **kwargs)
        self.device = Device.open(self.root)
        return result

    def flac_with_wrong_duration(self, width=2):
        wav = self.site / "时长测试.wav"
        write_wave(wav, width=width)
        flac = wav.with_suffix(".flac")
        ffmpeg("-i", wav, flac)
        # STREAMINFO packs the 36-bit sample count directly before the PCM MD5.
        with flac.open("r+b") as stream:
            stream.seek(18)
            packed = int.from_bytes(stream.read(8), "big")
            stream.seek(18)
            stream.write((packed + 120834).to_bytes(8, "big"))
        return flac

    def test_flac_header_duration_disagrees_but_actual_pcm_matches(self):
        flac = self.flac_with_wrong_duration()
        self.assertNotEqual(source_audio(flac).info.md5_signature, 0)
        self.assertEqual(self.send([flac], formats={flac: "alac"})[:2], (1, 0))
        track = self.device.library.snapshot.tracks[0]
        self.assertEqual(track.length_ms, round(4097 * 1000 / 44100))
        output = self.root / track.metadata.location
        self.assertEqual(ffmpeg("-i", output, "-f", "s16le", "-"), ffmpeg("-i", flac, "-f", "s16le", "-"))
        with flac.open("r+b") as stream:
            stream.seek(26)
            stream.write(bytes(16))
        self.assertEqual(self.send([flac], formats={flac: "alac"}), (0, 1, None))

    def test_length_fallback_blocks_bad_audio_and_bad_embedded_checksum(self):
        flac = self.flac_with_wrong_duration()
        other = self.site / "other.m4a"
        ffmpeg("-i", flac, "-af", "volume=0.5", "-c:a", "alac", other)
        info = source_audio(flac).info
        with self.assertRaisesRegex(ValueError, "采样不一致"):
            _verify_duration(flac, info, other, EasyMP4(other).info)
        with flac.open("r+b") as stream:
            stream.seek(26)
            stream.write(b"x" * 16)
        before = self.device.original
        with self.assertRaisesRegex(ValueError, "MD5 不一致"), patch("transfer.device_lock") as lock:
            self.send([flac], formats={flac: "alac"})
        lock.assert_not_called()
        self.assertEqual((self.root / DB).read_bytes(), before)

    def test_24_bit_flac_with_wrong_duration_checks_the_lowest_bit(self):
        flac = self.flac_with_wrong_duration(width=3)
        pcm = ffmpeg("-i", flac, "-f", "s24le", "-")
        with prepared_files([flac], {flac: "alac"}, self.root, lambda _: None) as files:
            self.assertEqual(ffmpeg("-i", files[0], "-f", "s24le", "-"), pcm)
        altered = bytearray(pcm)
        altered[0] ^= 1
        wav = self.site / "altered.wav"
        with wave.open(str(wav), "wb") as stream:
            stream.setparams((2, 3, 44100, 0, "NONE", "unused"))
            stream.writeframes(altered)
        other = wav.with_suffix(".m4a")
        ffmpeg("-i", wav, "-c:a", "alac", other)
        self.assertEqual(ffmpeg("-i", other, "-f", "s16le", "-"), ffmpeg("-i", flac, "-f", "s16le", "-"))
        with self.assertRaisesRegex(ValueError, "采样不一致"):
            _verify_duration(flac, source_audio(flac).info, other, EasyMP4(other).info)

    def test_one_flac_can_mix_gbk_artist_and_utf8_title(self):
        wav = self.site / "legacy.wav"
        write_wave(wav)
        flac = wav.with_suffix(".flac")
        ffmpeg("-i", wav, "-metadata", "artist=ZZZZZZ", "-metadata", "title=老街", "-metadata", "album=原专辑", flac)
        data = flac.read_bytes()
        self.assertEqual(data.count(b"ZZZZZZ"), 1)
        flac.write_bytes(data.replace(b"ZZZZZZ", "李荣浩".encode("gbk")))
        original = fingerprint(flac)
        for mode in ("aac", "alac"):
            self.send([flac], formats={flac: mode})
            track = self.device.library.snapshot.tracks[-1]
            tags = EasyMP4(self.root / track.metadata.location)
            self.assertEqual((track.artist, track.title, track.album), ("李荣浩", "老街", "原专辑"))
            self.assertEqual(tags["artist"], ["李荣浩"])
        self.assertEqual(fingerprint(flac), original)

    def test_missing_artist_dated_album_and_explicit_artist_priority(self):
        album = self.site / "李荣浩" / "2013-模特" / "2013-模特"
        album.mkdir(parents=True)
        wav = album / "没有标签.wav"
        write_wave(wav)
        flac = album / "另一首.flac"
        ffmpeg("-i", wav, "-metadata", "artist=嘉宾歌手", flac)
        unknown = self.site / "Downloads" / "2010-专辑名" / "未知.wav"
        unknown.parent.mkdir(parents=True)
        write_wave(unknown)
        for path, artist in ((wav, "李荣浩"), (flac, "嘉宾歌手"), (unknown, "")):
            self.send([path], formats={path: "alac"})
            track = self.device.library.snapshot.tracks[-1]
            self.assertEqual(track.artist, artist)
            output = EasyMP4(self.root / track.metadata.location)
            self.assertEqual(output.get("artist", []), [artist] if artist else [])

    def test_lossless_pcm_and_tags_roundtrip_repeat_and_cleanup(self):
        wav = self.site / "无标签的曲名.wav"
        pcm = write_wave(wav)
        flac = self.site / "另一张专辑.FLAC"
        ffmpeg("-i", wav, flac)
        tags = FLAC(flac)
        for key, value in dict(title="海上的月亮", artist="测试艺人", album="海风", tracknumber="2",
                               tracktotal="9", discnumber="1/2", date="2024").items():
            tags[key] = value
        tags.save()
        sources = [flac, wav]
        originals = {p: fingerprint(p) for p in sources}
        formats = dict.fromkeys(sources, "alac")
        with prepared_files(sources, formats, self.root, lambda _: None) as ready:
            folder = ready[0].parent
            for dest in ready:
                audio = EasyMP4(dest)
                self.assertEqual((audio.info.codec, audio.info.sample_rate, audio.info.bits_per_sample), ("alac", 44100, 16))
                self.assertEqual(ffmpeg("-i", dest, "-f", "s16le", "-"), pcm)
        self.assertFalse(folder.exists())
        self.assertEqual(self.send(sources, formats=formats)[:2], (2, 0))
        first, second = self.device.library.snapshot.tracks
        self.assertEqual((first.title, first.artist, first.album, first.track_number, first.metadata.total_tracks,
                          first.metadata.total_discs, first.year), ("海上的月亮", "测试艺人", "海风", 2, 9, 2, 2024))
        self.assertEqual(second.title, wav.stem)
        self.assertEqual(self.send(sources, formats=formats), (0, 2, None))
        self.assertEqual({p: fingerprint(p) for p in sources}, originals)

    def test_pcm_wav_copy_aac_and_explicit_high_resolution_adaptation(self):
        mono = self.site / "单声道.wav"
        write_wave(mono, channels=1)
        self.send([mono])
        direct = self.root / self.device.library.snapshot.tracks[0].metadata.location
        self.assertEqual(direct.suffix, ".wav")
        self.assertEqual(fingerprint(direct), fingerprint(mono))
        self.send([mono], formats={mono: "aac"})
        aac = EasyMP4(self.root / self.device.library.snapshot.tracks[-1].metadata.location)
        self.assertEqual((aac.info.codec, aac.info.channels, aac.info.sample_rate), ("mp4a.40.2", 1, 44100))
        self.assertEqual(self.send([mono], formats={mono: "aac"}), (0, 1, None))
        hires = self.site / "高解析.wav"
        write_wave(hires, rate=96000, channels=2, width=3)
        self.send([hires])
        alac = EasyMP4(self.root / self.device.library.snapshot.tracks[-1].metadata.location)
        self.assertEqual((alac.info.codec, alac.info.channels, alac.info.sample_rate, alac.info.bits_per_sample),
                         ("alac", 2, 48000, 24))
        self.assertEqual(self.send([hires], formats={hires: "aac"}), (0, 1, None))

    def test_high_depth_or_rate_unifies_wav_flac_alac_and_aac(self):
        for rate, width, codec in ((44100, 4, "wav"), (48000, 4, "wav"), (44100, 4, "pcm_f32le"),
                                   (44100, 4, "flac"), (88200, 2, "wav"),
                                   (96000, 3, "alac"), (192000, 3, "flac"), (96000, 2, "aac")):
            with self.subTest(rate=rate, width=width, codec=codec):
                wav = self.site / f"{rate}-{width}-{codec}.wav"
                write_wave(wav, rate=rate, width=width)
                src = wav
                if codec != "wav":
                    suffix = {"flac": ".flac", "pcm_f32le": ".wav"}.get(codec, ".m4a")
                    src = wav.with_name(wav.stem + "-encoded" + suffix)
                    options = ("-strict", "experimental", "-bits_per_raw_sample", 32) if codec == "flac" and width == 4 else ()
                    ffmpeg("-i", wav, "-c:a", codec, *options, src)
                songs, errors = scan_inputs([src], {}, lambda _: None, lambda: False)
                self.assertEqual(errors, [])
                self.assertEqual(songs[0].choices, ("alac-48",))
                if codec == "alac":
                    with self.assertRaisesRegex(ValueError, "48 kHz ALAC"):
                        audio_track(src, -1, "raw.m4a", src.stat().st_size)
                with prepared_files([src], {src: "copy"}, self.root, lambda _: None) as files:
                    result, = files
                    info = EasyMP4(result).info
                    self.assertEqual((info.codec, info.sample_rate, info.bits_per_sample), ("alac", 48000, 24))
                    self.assertEqual(ffmpeg("-i", result, "-map", "0:a:0", "-f", "s24le", "-"),
                                     ffmpeg("-i", src, "-map", "0:a:0", "-ar", 48000, "-f", "s24le", "-"))

    def test_in_range_wav_flac_and_alac_preserve_original_samples(self):
        for rate, width, codec in ((44100, 2, "wav"), (48000, 2, "flac"), (44100, 3, "wav"), (44100, 3, "flac"),
                                   (48000, 3, "wav"), (48000, 3, "flac"), (44100, 3, "alac"), (48000, 3, "alac")):
            with self.subTest(rate=rate, width=width, codec=codec):
                wav = self.site / f"{rate}-{width}-{codec}.wav"
                pcm = write_wave(wav, rate=rate, width=width)
                src = wav
                if codec != "wav":
                    src = wav.with_suffix(".flac" if codec == "flac" else ".m4a")
                    ffmpeg("-i", wav, "-c:a", codec, "-metadata", "title=边界", "-metadata", "artist=歌手",
                           "-metadata", "album=专辑", src)
                song, = scan_inputs([src], {}, lambda _: None, lambda: False)[0]
                self.assertEqual(song.mode, "copy" if codec == "alac" else "alac")
                self.assertIn("aac", song.choices)
                with prepared_files([src], {}, self.root, lambda _: None, reviewed={src: song}) as files:
                    output, = files
                    info = EasyMP4(output).info
                    self.assertEqual((info.codec, info.sample_rate, info.bits_per_sample), ("alac", rate, width * 8))
                    self.assertEqual(ffmpeg("-i", output, "-f", f"s{width * 8}le", "-"), pcm)
                    track, _ = audio_track(output, -1, "test.m4a", output.stat().st_size)
                    self.assertEqual(track.metadata.sample_rate_hz, rate)
                    if codec == "alac":
                        self.assertEqual(output.read_bytes(), src.read_bytes())

    def test_pcm_wav_with_24_valid_bits_in_32_bit_storage(self):
        wav = self.site / "padded.wav"
        pcm = write_wave(wav, width=3)
        padded = b"".join(b"\0" + pcm[i:i + 3] for i in range(0, len(pcm), 3))
        fmt = struct.pack("<HHIIHHHHI16s", wave.WAVE_FORMAT_EXTENSIBLE, 2, 44100, 44100 * 8, 8, 32, 22, 24, 3,
                          wave.KSDATAFORMAT_SUBTYPE_PCM)
        wav.write_bytes(b"RIFF" + struct.pack("<I", 20 + len(fmt) + len(padded)) + b"WAVEfmt "
                        + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(padded)) + padded)
        song, = scan_inputs([wav], {}, lambda _: None, lambda: False)[0]
        self.assertEqual(song.mode, "alac")
        with prepared_files([wav], {}, self.root, lambda _: None, reviewed={wav: song}) as files:
            info = EasyMP4(files[0]).info
            self.assertEqual((info.codec, info.sample_rate, info.bits_per_sample), ("alac", 44100, 24))
            self.assertEqual(ffmpeg("-i", files[0], "-f", "s24le", "-"), pcm)

    def test_bad_source_leaves_batch_and_device_untouched(self):
        wav = self.site / "先转换.wav"
        write_wave(wav)
        broken = self.site / "不完整.flac"
        broken.write_bytes(b"fLaC")
        original = self.device.original
        with self.assertRaisesRegex(ValueError, broken.name), patch("transfer.device_lock") as lock:
            self.send([wav, broken], formats={wav: "alac", broken: "aac"})
        lock.assert_not_called()
        self.assertEqual((self.root / DB).read_bytes(), original)
        self.assertFalse(self.backups.exists())
        self.assertFalse(list((self.root / MUSIC).rglob("*.*")))

    def test_transfer_to_playlist_reuses_tracks_and_commits_once(self):
        old = FIXTURES / "tone.mp3"
        wav = self.site / "后来.wav"
        write_wave(wav)
        self.send([old])
        snap = self.device.library.snapshot
        self.send(desired=playlist_edit(snap, [], name="路上"))
        playlist_id = self.device.library.snapshot.playlists[0].playlist_id
        previous = self.device.original
        before_count = len(list(self.backups.rglob("iTunesDB")))
        added, skipped, backup = self.send([wav, old, wav], formats={wav: "alac"}, playlist_id=playlist_id)
        self.assertEqual((added, skipped), (1, 2))
        self.assertEqual(backup.read_bytes(), previous)
        self.assertEqual(len(list(self.backups.rglob("iTunesDB"))), before_count + 1)
        snap = self.device.library.snapshot
        self.assertEqual(snap.playlists[0].track_ids, (snap.tracks[1].track_id, snap.tracks[0].track_id))
        self.assertEqual(self.send([old, wav], formats={wav: "alac"}, playlist_id=playlist_id), (0, 2, None))
        self.send(desired=playlist_edit(snap, [], name="第二张"))
        target = self.device.library.snapshot.playlists[-1].playlist_id
        self.assertIsNotNone(self.send([old], playlist_id=target)[2])
        self.assertEqual(self.device.library.snapshot.playlists[-1].track_ids, (snap.tracks[0].track_id,))
        self.assertTrue(verify_hash58(self.device.original, GUID))

    def test_new_playlist_commits_once_and_reuses_existing_music(self):
        old, new = FIXTURES / "tone.mp3", FIXTURES / "lossless.m4a"
        self.send([old])
        before = self.device.original
        previous_backups = len(list(self.backups.rglob("iTunesDB")))
        added, skipped, backup = self.send([new, old, new], new_playlist_name="  夜行 Mix  ")
        self.assertEqual((added, skipped, backup.read_bytes()), (1, 2, before))
        self.assertEqual(len(list(self.backups.rglob("iTunesDB"))), previous_backups + 1)
        snap = self.device.library.snapshot
        first, second = snap.tracks
        playlist, = snap.playlists
        self.assertEqual((playlist.name, playlist.track_ids), ("夜行 Mix", (second.track_id, first.track_id)))
        self.assertEqual(self.send([old, old], new_playlist_name="重听")[:2], (0, 2))
        self.assertEqual(self.device.library.snapshot.playlists[-1].track_ids, (first.track_id,))
        self.assertTrue(verify_hash58(self.device.original, GUID))

    def test_failed_import_creates_no_playlist_and_can_be_retried(self):
        song = FIXTURES / "tone.mp3"
        self.send(desired=playlist_edit(self.device.library.snapshot, [], name="Mix"))
        before = self.device.original
        with patch("transfer.prepared_files") as prepare:
            for invalid in ("  ", "mix", "a" * 129):
                with self.subTest(name=invalid), self.assertRaises(ValueError):
                    self.send([song], new_playlist_name=invalid)
            with self.assertRaisesRegex(ValueError, "一个目标"):
                self.send([song], playlist_id=self.device.library.snapshot.playlists[0].playlist_id, new_playlist_name="新歌")
            with self.assertRaisesRegex(ValueError, "先选择"):
                self.send([], new_playlist_name="新歌")
            prepare.assert_not_called()
        with patch("transfer.shutil.copyfileobj", side_effect=OSError("模拟写入中断")), self.assertRaisesRegex(OSError, "中断"):
            self.send([song], new_playlist_name="新歌")
        self.assertEqual((self.root / DB).read_bytes(), before)
        self.assertFalse(any(p.is_file() for p in (self.root / MUSIC).rglob("*")))
        self.assertEqual(self.send([song], new_playlist_name="新歌")[:2], (1, 0))
        self.assertEqual([p.name for p in self.device.library.snapshot.playlists], ["Mix", "新歌"])

    def test_manage_playlist_preserves_files_and_other_lists(self):
        self.send([FIXTURES / n for n in ("tone.mp3", "aac.m4a", "lossless.m4a")])
        snap = self.device.library.snapshot
        ids = tuple(t.track_id for t in snap.tracks)
        self.send(desired=playlist_edit(snap, ids, name="路上"))
        self.send(desired=playlist_edit(self.device.library.snapshot, ids[:1], name="收藏"))
        snap = self.device.library.snapshot
        first, other = snap.playlists
        media = {t.metadata.location: fingerprint(self.root / t.metadata.location) for t in snap.tracks}
        with self.assertRaisesRegex(ValueError, "同名"):
            rename_playlist(snap, first.playlist_id, other.name)
        self.send(desired=rename_playlist(snap, first.playlist_id, "晚归"))
        snap = self.device.library.snapshot
        first = snap.playlists[0]
        self.assertEqual(first.name, "晚归")
        self.send(desired=move_playlist_entries(snap, first.playlist_id, [e.entry_id for e in first.entries[1:]], -1))
        snap = self.device.library.snapshot
        moved = snap.playlists[0]
        self.assertEqual(moved.track_ids, (ids[1], ids[2], ids[0]))
        self.assertEqual(moved.sort_order, PlaylistSortOrder.MANUAL)
        self.send(desired=move_playlist_entries(snap, moved.playlist_id, [e.entry_id for e in moved.entries[:2]], 1))
        self.assertEqual(self.device.library.snapshot.playlists[0].track_ids, ids)
        self.send(delete_playlist_id=first.playlist_id)
        snap = self.device.library.snapshot
        self.assertEqual([(p.name, p.track_ids) for p in snap.playlists], [(other.name, other.track_ids)])
        self.assertEqual(media, {t.metadata.location: fingerprint(self.root / t.metadata.location) for t in snap.tracks})
        for playlist in (replace(other, system_managed=True), replace(other, kind=PlaylistKind.SMART),
                         replace(other, is_phase=True)):
            with self.assertRaises(ValueError):
                delete_playlist(replace(snap, playlists=(playlist,)), playlist.playlist_id)

    def test_move_specific_duplicate_occurrence(self):
        snap = self.device.library.snapshot
        draft = playlist_edit(snap, [], name="重复曲目")
        p = replace(draft.playlists[0], entries=playlist_entries([10, 11, 10, 12]))
        draft = replace(draft, playlists=(p,))
        changed = move_playlist_entries(draft, p.playlist_id, [p.entries[2].entry_id], -1)
        self.assertEqual([e.entry_id for e in changed.playlists[0].entries],
                         [p.entries[i].entry_id for i in (0, 2, 1, 3)])
