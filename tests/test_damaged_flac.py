import logging
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_classicdrop import DB, Device, mock_device
from test_formats_playlists import ffmpeg, write_wave
import diagnostics
import system
from imageio_ffmpeg import get_ffmpeg_exe
from mutagen.easymp4 import EasyMP4
from audio_convert import source_audio
from transfer import transfer


def strict_decode(path):
    return subprocess.run([get_ffmpeg_exe(), "-nostdin", "-v", "error", "-xerror",
                           "-err_detect", "crccheck+explode", "-i", str(path), "-f", "s16le", "-"],
                          capture_output=True, **system.NO_WINDOW)


class DamagedFlac(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.site = Path(temp.name).resolve()
        self.root = self.site / "IPC3"
        self.device = mock_device(self.root)
        self.backups = self.site / "backups"
        wav = self.site / "完好.wav"
        self.pcm = write_wave(wav)
        self.flac = self.site / "歌曲.flac"
        ffmpeg("-i", wav, "-metadata", "artist=歌手", "-metadata", "title=标题", self.flac)
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.WARNING)

    def send(self, files=(), **kwargs):
        result = transfer(self.device, files, self.backups, **kwargs)
        self.device = Device.open(self.root)
        return result

    def junked(self, junk):
        damaged = self.site / f"加尾巴{len(junk)}.flac"
        damaged.write_bytes(self.flac.read_bytes() + junk)
        return damaged

    def test_junk_appended_to_a_valid_flac_is_recovered_and_converted(self):
        for junk in (b"TAG" + b"BABY ANGEL".ljust(125, b" "), os.urandom(4096)):
            with self.subTest(junk=len(junk)):
                self.root = self.site / f"IPC3-{len(junk)}"
                self.device = mock_device(self.root)
                damaged = self.junked(junk)
                self.assertNotEqual(strict_decode(damaged).returncode, 0)  # reproduces the report
                with self.assertLogs(diagnostics.log, "WARNING") as logged:
                    self.assertEqual(self.send([damaged], formats={damaged: "alac"})[:2], (1, 0))
                self.assertTrue(any("无效数据" in line and "MD5" in line for line in logged.output))
                track = self.device.library.snapshot.tracks[-1]
                output = self.root / track.metadata.location
                self.assertEqual(ffmpeg("-i", output, "-f", "s16le", "-"), self.pcm)
                tags = EasyMP4(output)
                self.assertEqual((tags["artist"], tags["title"]), (["歌手"], ["标题"]))

    def test_corruption_inside_a_frame_is_blocked_with_a_friendly_error(self):
        damaged = self.site / "坏帧.flac"
        data = bytearray(self.flac.read_bytes())
        frame = data.rfind(b"\xff\xf8")
        self.assertGreater(frame, 0)
        data[frame + 5] ^= 0xFF
        damaged.write_bytes(bytes(data))
        self.assertNotEqual(strict_decode(damaged).returncode, 0)
        before = (self.root / DB).read_bytes()
        with self.assertRaises(ValueError) as caught, \
                self.assertLogs(diagnostics.log, "WARNING"), \
                patch("transfer.device_lock") as lock:
            self.send([damaged], formats={damaged: "alac"})
        message = str(caught.exception)
        self.assertIn("未传入 iPod", message)
        self.assertIn("无法解码的数据", message)
        self.assertNotIn("[flac @", message)
        lock.assert_not_called()
        self.assertEqual((self.root / DB).read_bytes(), before)
        self.assertEqual(self.device.library.snapshot.tracks, ())

    def test_junk_appended_flac_without_a_checksum_is_blocked(self):
        damaged = self.junked(b"TAG" + bytes(125))
        with damaged.open("r+b") as stream:
            stream.seek(26)  # STREAMINFO PCM MD5
            stream.write(bytes(16))
        self.assertEqual(source_audio(damaged).info.md5_signature, 0)
        self.assertNotEqual(strict_decode(damaged).returncode, 0)
        before = (self.root / DB).read_bytes()
        with self.assertRaisesRegex(ValueError, "无法确认音频完整"), \
                patch("transfer.device_lock") as lock:
            self.send([damaged], formats={damaged: "alac"})
        lock.assert_not_called()
        self.assertEqual((self.root / DB).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
