import logging
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from mutagen.easymp4 import EasyMP4
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4FreeForm

import audio_convert
import encoders
from test_formats_playlists import ffmpeg, write_wave

QAAC_PATH = "C:/tools/qaac64.exe"
REFALAC_PATH = "C:/tools/refalac64.exe"


def stub_encode(calls, codec_args, fail=()):
    """Record the candidate chosen, then produce real output with FFmpeg so post-checks still run."""
    def stub(exe, env, tool_args, decode_args, raw, dest):
        calls.append((exe, list(tool_args), raw))
        if exe in fail:
            raise ValueError("stub 编码失败")
        audio_convert._ffmpeg([*decode_args, *codec_args, "-f", "ipod", str(dest)])
    return stub


def alac_codec(bits):
    return ["-c:a", "alac", "-sample_fmt", "s16p" if bits == 16 else "s32p", "-bits_per_raw_sample", str(bits)]


class EncoderChainTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.site = Path(temp.name)
        self.wav = self.site / "a.wav"
        write_wave(self.wav)
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.WARNING)

    def convert(self, mode, dest=None, **kw):
        return audio_convert._convert(self.wav, mode, dest or self.site / f"{mode}.m4a")

    def test_apple_components_make_qaac_the_alac_and_aac_encoder(self):
        calls = []
        with patch.object(encoders, "qaac", return_value=(QAAC_PATH, {})), \
                patch.object(encoders, "refalac", return_value=(REFALAC_PATH, {})), \
                patch.object(encoders, "pipe_encode", stub_encode(calls, alac_codec(16))):
            self.convert("alac")
        exe, args, raw = calls[0]
        self.assertEqual(len(calls), 1)
        self.assertEqual(exe, QAAC_PATH)
        self.assertEqual((raw, args[:2], args[2:]), ("s16le", ["-A", "--raw"],
                         ["--raw-format", "S16L", "--raw-channels", "2", "--raw-rate", "44100", "--silent"]))
        calls.clear()
        with patch.object(encoders, "qaac", return_value=(QAAC_PATH, {})), \
                patch.object(encoders, "refalac", return_value=(REFALAC_PATH, {})), \
                patch.object(encoders, "pipe_encode", stub_encode(calls, ["-c:a", "aac", "-b:a", "256k"])):
            self.convert("aac")
        exe, args, raw = calls[0]
        self.assertEqual(exe, QAAC_PATH)
        self.assertIn("--cvbr", args)
        self.assertEqual((raw, args[args.index("--cvbr") + 1], args[args.index("--raw-format") + 1]),
                         ("f32le", "256", "F32L"))

    def test_mono_aac_uses_cvbr_128(self):
        write_wave(self.wav, channels=1)
        calls = []
        with patch.object(encoders, "qaac", return_value=(QAAC_PATH, {})), \
                patch.object(encoders, "refalac", return_value=None), \
                patch.object(encoders, "pipe_encode", stub_encode(calls, ["-c:a", "aac", "-b:a", "128k"])):
            self.convert("aac")
        exe, args, raw = calls[0]
        self.assertEqual((raw, args[args.index("--cvbr") + 1], args[args.index("--raw-channels") + 1]),
                         ("f32le", "128", "1"))

    def test_24bit_alac_uses_s24l_and_refalac_is_used_without_apple(self):
        write_wave(self.wav, width=3)
        calls = []
        with patch.object(encoders, "qaac", return_value=None), \
                patch.object(encoders, "refalac", return_value=(REFALAC_PATH, {})), \
                patch.object(encoders, "pipe_encode", stub_encode(calls, alac_codec(24))):
            self.convert("alac")
        exe, args, raw = calls[0]
        self.assertEqual((exe, raw, args[args.index("--raw-format") + 1]), (REFALAC_PATH, "s24le", "S24L"))

    def test_refalac_failure_falls_back_to_ffmpeg_with_a_warning(self):
        with patch.object(encoders, "qaac", return_value=None), \
                patch.object(encoders, "refalac", return_value=(REFALAC_PATH, {})), \
                patch.object(encoders, "pipe_encode", stub_encode([], alac_codec(16), fail=(REFALAC_PATH,))), \
                self.assertLogs("ClassicDrop", "WARNING") as caught:
            self.convert("alac")
        self.assertIn("refalac", caught.records[0].getMessage())
        info = EasyMP4(self.site / "alac.m4a").info
        self.assertEqual((info.codec, info.sample_rate, info.bits_per_sample), ("alac", 44100, 16))

    def test_no_tools_uses_plain_ffmpeg(self):
        calls = []
        with patch.object(encoders, "qaac", return_value=None), \
                patch.object(encoders, "refalac", return_value=None), \
                patch.object(encoders, "pipe_encode", stub_encode(calls, [])):
            self.convert("alac")
        self.assertEqual(calls, [])


@unittest.skipUnless(encoders.REFALAC.is_file(), "vendor/qaac/refalac64.exe not fetched")
class RefalacIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.site = Path(temp.name)
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.WARNING)

    def convert(self, source, mode="alac"):
        dest = self.site / (source.stem + ".m4a")
        with patch.object(encoders, "qaac", return_value=None):  # deterministic: refalac, not an installed iTunes
            audio_convert._convert(source, mode, dest)
        return dest

    def test_16_and_24_bit_wav_round_trip_through_refalac(self):
        for width, codec in ((2, "pcm_s16le"), (3, "pcm_s24le")):
            wav = self.site / f"{width * 8}bit.wav"
            write_wave(wav, width=width)
            dest = self.convert(wav)
            info = EasyMP4(dest).info
            self.assertEqual((info.codec, info.bits_per_sample, info.sample_rate, info.channels),
                             ("alac", width * 8, 44100, 2))
            self.assertEqual(audio_convert._pcm_md5(wav, 44100, 2, codec),
                             audio_convert._pcm_md5(dest, 44100, 2, codec))

    def test_tags_and_cover_survive_the_refalac_path(self):
        flac = self.site / "tagged.flac"
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=0.5:sample_rate=44100",
               "-ac", "2", "-c:a", "flac", flac)
        audio = FLAC(flac)
        audio["title"] = "标题"
        audio["artist"] = "歌手"
        pic = Picture()
        pic.type = 3
        pic.mime = "image/png"
        pic.data = (Path(__file__).parent / "fixtures" / "cover.png").read_bytes() \
            if (Path(__file__).parent / "fixtures" / "cover.png").is_file() else _png()
        audio.add_picture(pic)
        audio.save()
        dest = self.convert(flac)
        tagged = MP4(dest)
        self.assertEqual((tagged["©nam"][0], tagged["©ART"][0]), ("标题", "歌手"))
        self.assertTrue(tagged["covr"])

    def test_metadata_save_keeps_the_gapless_atom(self):
        wav = self.site / "a.wav"
        write_wave(wav)
        dest = self.convert(wav)
        audio = MP4(dest)
        audio["----:com.apple.iTunes:iTunSMPB"] = [MP4FreeForm(b"\x00" * 8)]
        audio.save()
        audio_convert._metadata(wav, audio_convert.source_audio(wav), dest)
        self.assertIn("----:com.apple.iTunes:iTunSMPB", MP4(dest))


def _png():
    from io import BytesIO
    data = BytesIO()
    try:
        from PIL import Image
        Image.new("RGB", (8, 8), "red").save(data, format="PNG")
    except ImportError:
        data = BytesIO(bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001080600000"
            "01f15c4890000000d49444154789c626001000000ffff030000060005"
            "57bfabd40000000049454e44ae426082"))
    return data.getvalue()


class InstallAppleTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "ClassicDrop"
        patcher = patch.object(encoders, "QTFILES", self.root / "QTfiles64")
        patcher.start()
        self.addCleanup(patcher.stop)

    def left_behind(self):
        return [p.name for p in self.root.iterdir()] if self.root.is_dir() else []

    def install(self, dlls, check="CoreAudioToolbox 7.10.9.0"):
        def fake_download(url, dest, report):
            dest.write_bytes(b"installer")
            report("下载完成")
        def fake_run(args, error, timeout=900):
            args = [str(a) for a in args]
            if "/a" not in args:  # extrac32: write the msi into the /L target
                ext = Path(args[args.index("/L") + 1])
                ext.mkdir(parents=True, exist_ok=True)
                (ext / "iTunes64.msi").write_bytes(b"msi")
            else:
                folder = Path(next(a.split("=", 1)[1] for a in args if a.startswith("TARGETDIR"))) / "iTunes"
                folder.mkdir(parents=True, exist_ok=True)
                for name in dlls:
                    (folder / name).write_bytes(b"dll")
        patches = (patch.object(encoders, "_download", fake_download),
                   patch.object(encoders, "_slice_cab", lambda i, c: c.write_bytes(b"cab")),
                   patch.object(encoders, "_verify_signature", lambda i: None),
                   patch.object(encoders, "_run", fake_run),
                   patch.object(encoders, "_run_check", lambda folder: check))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def all_dlls(self):
        return [*encoders.APPLE_DLLS, "icudt62.dll"]

    def test_success_copies_exactly_the_required_dlls(self):
        self.install(self.all_dlls())
        version = encoders.install_apple(lambda text: None)
        self.assertEqual(version, "CoreAudioToolbox 7.10.9.0")
        qtfiles = self.root / "QTfiles64"
        self.assertEqual(sorted(p.name for p in qtfiles.iterdir()), sorted(self.all_dlls()))
        self.assertEqual(self.left_behind(), ["QTfiles64"])

    def test_bad_signature_leaves_nothing_behind(self):
        self.install(self.all_dlls())
        with patch.object(encoders, "_verify_signature", side_effect=ValueError("签名无效")):
            with self.assertRaisesRegex(ValueError, "签名无效"):
                encoders.install_apple(lambda text: None)
        self.assertEqual(self.left_behind(), [])

    def test_missing_dll_leaves_nothing_behind(self):
        dlls = self.all_dlls()
        dlls.remove("libdispatch.dll")
        self.install(dlls)
        with self.assertRaisesRegex(ValueError, "libdispatch"):
            encoders.install_apple(lambda text: None)
        self.assertEqual(self.left_behind(), [])

    def test_check_failure_leaves_nothing_behind(self):
        self.install(self.all_dlls(), check=None)
        with self.assertRaisesRegex(ValueError, "自检"):
            encoders.install_apple(lambda text: None)
        self.assertEqual(self.left_behind(), [])


if __name__ == "__main__":
    unittest.main()
