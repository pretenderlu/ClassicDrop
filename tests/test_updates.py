import hashlib
import io
import json
import re
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError, URLError

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import system
import updates
from updates import UpdateError, UpdateInfo, check_for_updates, download_update, plain_notes, release_history, version_key
from version import __version__
DIGEST = "a" * 64


def release(tag, digest=DIGEST, size=10, name="ClassicDropSetup-v9.9.9.exe", **extra):
    asset = {"name": name, "browser_download_url": "https://example.invalid/setup.exe", "size": size,
             "url": "https://api.example.invalid/asset/1"}
    if digest is not None:
        asset["digest"] = "sha256:" + digest
    return {"tag_name": tag, "html_url": "https://github.com/pretenderlu/ClassicDrop/releases/" + tag,
            "assets": [{"name": "ClassicDrop-Windows-x64.zip"}, asset], "prerelease": False, "draft": False,
            "body": "- 修复若干问题", **extra}


class Reply(io.BytesIO):
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def cnb_release(tag, digest=DIGEST, size=10, name="ClassicDropSetup-v9.9.9.exe", **extra):
    asset = {
        "name": name,
        "path": f"/pretenderlu/ClassicDrop/-/releases/download/{tag}/{name}",
        "size_in_byte": size,
        "hash_algo": "sha256",
    }
    if digest is not None:
        asset["hash_value"] = digest
    return {
        "tag_ref": f"refs/tags/{tag}" if tag else "",
        "title": f"ClassicDrop {tag}",
        "body": "- 镜像修复若干问题",
        "published_at": "2026-12-01T08:00:00Z",
        "is_prerelease": False,
        "is_draft": False,
        "assets": [{"name": "ClassicDrop-Windows-x64.zip", "path": "/pretenderlu/ClassicDrop/-/releases/download/x/z.zip"}, asset],
        **extra,
    }


def latest(payload):
    return patch.object(updates, "urlopen", return_value=Reply(json.dumps(payload).encode("utf8")))


class CheckTests(unittest.TestCase):
    def test_version_key(self):
        self.assertGreater(version_key("v1.6.10"), version_key("1.6.9"))
        self.assertEqual(version_key("1.7"), (1, 7, 0, 0))
        self.assertEqual(version_key("v2.0.1-beta.3"), (2, 0, 1, 0))
        self.assertEqual(version_key("nonsense"), (0, 0, 0, 0))

    def test_newer_release_with_digest_offers_the_installer(self):
        with latest(release("v9.9.9")) as opened:
            info = check_for_updates("1.5.1")
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/pretenderlu/ClassicDrop/releases/latest")
        self.assertEqual(request.get_header("User-agent"), f"ClassicDrop/{__version__}")
        if system.MAC:
            # macOS is never offered the Windows installer; the release page opens instead.
            self.assertEqual((info.version, info.download_url, info.installer_name, info.size, info.sha256,
                              info.alternate_url, info.notes),
                             ("v9.9.9", "", "", 0, "", "", "- 修复若干问题"))
        else:
            self.assertEqual((info.version, info.download_url, info.installer_name, info.size, info.sha256, info.notes),
                             ("v9.9.9", "https://example.invalid/setup.exe", "ClassicDropSetup-v9.9.9.exe", 10, DIGEST,
                              "- 修复若干问题"))
            self.assertEqual(info.alternate_url, "https://api.example.invalid/asset/1")
            self.assertEqual(info.mirror_url,
                             "https://cnb.cool/pretenderlu/ClassicDrop/-/releases/download/v9.9.9/ClassicDropSetup-v9.9.9.exe")

    def test_release_that_cannot_be_verified_only_offers_its_page(self):
        for payload in (release("v9.9.9", digest=None), release("v9.9.9", digest="12"),
                        release("v9.9.9", name="ClassicDrop-1.5.1.exe")):
            with self.subTest(payload=payload["assets"][1]), latest(payload):
                info = check_for_updates("1.5.1")
            self.assertEqual((info.download_url, info.sha256, info.installer_name), ("", "", ""))
            self.assertTrue(info.url.startswith("https://github.com/"))

    def test_mac_picks_the_dmg_for_the_machine_arch(self):
        dmg = {"name": "ClassicDrop-macOS-arm64-v9.9.9.dmg", "browser_download_url": "https://example.invalid/m.dmg",
               "size": 20, "url": "https://api.example.invalid/asset/2", "digest": "sha256:" + DIGEST}
        payload = release("v9.9.9")
        payload["assets"].append(dmg)
        with patch.object(system, "MAC", True), patch.object(updates, "mac_arch", return_value="arm64"),                 latest(payload):
            info = check_for_updates("1.5.1")
        self.assertEqual((info.download_url, info.installer_name, info.size, info.sha256),
                         ("https://example.invalid/m.dmg", dmg["name"], 20, DIGEST))
        # Under Rosetta the hardware is still Apple Silicon.
        with patch.object(system, "MAC", True), patch.object(updates, "mac_arch", return_value="x64"),                 latest(payload):
            info = check_for_updates("1.5.1")
        self.assertEqual((info.download_url, info.installer_name), ("", ""))
        # Missing or bad digest: only the page is offered, same as the Windows installer.
        dmg["digest"] = "sha256:12"
        with patch.object(system, "MAC", True), patch.object(updates, "mac_arch", return_value="arm64"),                 latest(payload):
            info = check_for_updates("1.5.1")
        self.assertEqual((info.download_url, info.installer_name), ("", ""))

    def test_same_older_draft_and_prerelease_are_ignored(self):
        for payload in (release("v1.5.1"), release("v1.5.0"), release("v9.9.9", prerelease=True),
                        release("v9.9.9", draft=True), release(""), ["unexpected"],
                        release("v9.9.9", html_url="https://example.invalid/phish")):
            with self.subTest(payload=payload), latest(payload):
                self.assertIsNone(check_for_updates("1.5.1"))

    def test_missing_notes_are_empty(self):
        payload = release("v9.9.9")
        del payload["body"]
        with latest(payload):
            self.assertEqual(check_for_updates("1.5.1").notes, "")

    def test_check_falls_back_to_cnb_mirror_when_github_is_unreachable(self):
        payload = cnb_release("v9.9.9")
        payload["assets"].append({
            "name": "ClassicDrop-macOS-arm64-v9.9.9.dmg",
            "path": "/pretenderlu/ClassicDrop/-/releases/download/v9.9.9/ClassicDrop-macOS-arm64-v9.9.9.dmg",
            "size_in_byte": 20,
            "hash_algo": "sha256",
            "hash_value": DIGEST,
        })
        replies = [URLError("timed out"), Reply(json.dumps(payload).encode("utf8"))]
        with patch.object(updates, "urlopen", side_effect=replies) as opened, \
                patch.object(updates, "mac_arch", return_value="arm64"):
            info = check_for_updates("1.5.1")
        self.assertEqual([c.args[0].full_url for c in opened.call_args_list], [
            "https://api.github.com/repos/pretenderlu/ClassicDrop/releases/latest",
            "https://cnb.cool/pretenderlu/ClassicDrop/-/releases/latest",
        ])
        self.assertEqual(opened.call_args_list[1].args[0].get_header("Accept"), updates.CNB_WEB_ACCEPT)
        self.assertEqual(info.url, "https://cnb.cool/pretenderlu/ClassicDrop/-/releases/tag/v9.9.9")
        expected_name = "ClassicDrop-macOS-arm64-v9.9.9.dmg" if system.MAC else "ClassicDropSetup-v9.9.9.exe"
        self.assertEqual(info.installer_name, expected_name)
        self.assertEqual(info.download_url, f"https://cnb.cool/pretenderlu/ClassicDrop/-/releases/download/v9.9.9/{expected_name}")
        self.assertEqual(info.mirror_url, f"https://github.com/pretenderlu/ClassicDrop/releases/download/v9.9.9/{expected_name}")
        self.assertEqual(info.sha256, DIGEST)

        # Bad/missing digest or spoofed path on CNB still refuses automatic download and only offers the page.
        bad_path = cnb_release("v9.9.9")
        bad_path["assets"][1]["path"] = "/other/repo/-/releases/download/v9.9.9/ClassicDropSetup-v9.9.9.exe"
        for bad in (cnb_release("v9.9.9", digest=None), cnb_release("v9.9.9", digest="12"), bad_path):
            with patch.object(updates, "urlopen", side_effect=[URLError("reset"), Reply(json.dumps(bad).encode("utf8"))]):
                info = check_for_updates("1.5.1")
            self.assertEqual((info.download_url, info.sha256, info.mirror_url), ("", "", ""))
            self.assertTrue(info.url.startswith("https://cnb.cool/"))

        # When both GitHub and CNB fail, the original GitHub error is surfaced.
        with patch.object(updates, "urlopen", side_effect=[URLError("gh-offline"), URLError("cnb-offline")]):
            with self.assertRaisesRegex(URLError, "gh-offline"):
                check_for_updates("1.5.1")


class HistoryTests(unittest.TestCase):
    def test_published_releases_newest_first_with_readable_notes(self):
        older = release("v1.5.1", published_at="2026-11-22T12:27:38Z",
                        body="## 下载\r\n\r\n- **`a.exe`**：见 [Issues](https://x.invalid/i)。\r\n")
        newest = release("v9.9.9", published_at="2026-12-01T08:00:00Z")
        payload = [older, release("v9.9.10", draft=True), newest, release("v10.0.0", prerelease=True),
                   release("v0.9", html_url="https://example.invalid/phish"), "unexpected", release("", body="no tag")]
        with latest(payload) as opened:
            releases, update = release_history("1.5.1")
        self.assertEqual(opened.call_args.args[0].full_url,
                         "https://api.github.com/repos/pretenderlu/ClassicDrop/releases?per_page=30")
        self.assertEqual([(r.version, r.published, r.notes) for r in releases],
                         [("v9.9.9", "2026-12-01", "· 修复若干问题"), ("v1.5.1", "2026-11-22", "下载\n\n· a.exe：见 Issues。")])
        self.assertTrue(all(r.url.startswith("https://github.com/") for r in releases))
        self.assertEqual((update.version, update.installer_name, update.sha256),
                         ("v9.9.9", "", "") if system.MAC else ("v9.9.9", "ClassicDropSetup-v9.9.9.exe", DIGEST))
        with latest(payload):
            self.assertIsNone(release_history("9.9.9")[1])
        with latest([]):
            self.assertEqual(release_history("1.5.1"), ((), None))
        with latest({"message": "API rate limit exceeded"}), self.assertRaisesRegex(UpdateError, "无法识别"):
            release_history("1.5.1")

    def test_plain_notes(self):
        self.assertEqual(plain_notes("# 标题\n\n* 一\n  - 二\n__粗__ 与 `码`\n"), "标题\n\n· 一\n  · 二\n粗 与 码")
        self.assertEqual(plain_notes("  \n"), "")

    def test_history_falls_back_to_cnb_mirror_when_github_is_unreachable(self):
        older = cnb_release("v1.5.1", published_at="2026-11-22T12:27:38Z", body="- 旧版")
        newest = cnb_release("v9.9.9", published_at="2026-12-01T08:00:00Z")
        cnb_payload = {"releases": [older, cnb_release("v9.9.10", is_draft=True), newest, cnb_release("v10.0.0", is_prerelease=True)]}
        replies = [URLError("blocked"), Reply(json.dumps(cnb_payload).encode("utf8"))]
        with patch.object(updates, "urlopen", side_effect=replies) as opened:
            releases, update = release_history("1.5.1")
        self.assertEqual([c.args[0].full_url for c in opened.call_args_list], [
            "https://api.github.com/repos/pretenderlu/ClassicDrop/releases?per_page=30",
            "https://cnb.cool/pretenderlu/ClassicDrop/-/releases",
        ])
        self.assertEqual([(r.version, r.published) for r in releases], [("v9.9.9", "2026-12-01"), ("v1.5.1", "2026-11-22")])
        self.assertTrue(all(r.url.startswith("https://cnb.cool/") for r in releases))
        self.assertEqual(update.version, "v9.9.9")
        self.assertEqual(update.sha256, "" if system.MAC else DIGEST)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.target = Path(temp.name).resolve() / "updates" / "setup.exe"
        self.content = b"installer-bytes" * 1000
        self.info = UpdateInfo("v9.9.9", "https://github.com/x", "https://dl.invalid/setup.exe", "setup.exe",
                               len(self.content), hashlib.sha256(self.content).hexdigest(), "https://api.invalid/asset")

    def run_download(self, replies, info=None):
        steps = []
        with patch.object(updates, "urlopen", side_effect=replies) as opened, patch.object(updates.time, "sleep"):
            try:
                return download_update(info or self.info, self.target, lambda done, total: steps.append((done, total))), opened, steps
            except UpdateError as error:
                return error, opened, steps

    def leftovers(self):
        return sorted(p.name for p in self.target.parent.iterdir()) if self.target.parent.exists() else []

    def test_verified_download_reports_progress(self):
        result, opened, steps = self.run_download([Reply(self.content)])
        self.assertEqual((result, result.read_bytes(), steps[-1]), (self.target, self.content, (len(self.content),) * 2))
        self.assertEqual(self.leftovers(), ["setup.exe"])
        self.assertEqual(opened.call_args.args[0].get_header("Accept"), "application/octet-stream")

    def test_wrong_checksum_is_refused_without_retrying(self):
        result, opened, _ = self.run_download([Reply(self.content)] * 4, UpdateInfo(
            "v9.9.9", "https://github.com/x", "https://dl.invalid/setup.exe", "setup.exe", len(self.content), "0" * 64))
        self.assertIn("校验失败", str(result))
        self.assertEqual((opened.call_count, self.leftovers()), (1, []))

    def test_dropped_connections_are_retried_then_the_asset_api_is_used(self):
        result, opened, _ = self.run_download([Reply(self.content[:100]), URLError("reset"), Reply(self.content)])
        self.assertEqual(result.read_bytes(), self.content)
        self.assertEqual([call.args[0].full_url for call in opened.call_args_list],
                         ["https://dl.invalid/setup.exe", "https://dl.invalid/setup.exe", "https://api.invalid/asset"])

    def test_short_download_gives_up_after_every_address(self):
        result, opened, _ = self.run_download([Reply(self.content[:100]) for _ in range(4)])
        self.assertIn("不完整", str(result))
        self.assertEqual((opened.call_count, self.leftovers()), (4, []))

    def test_server_errors_retry_and_client_errors_stop(self):
        def failure(code):
            return HTTPError("https://dl.invalid", code, "error", {}, None)
        result, opened, _ = self.run_download([failure(503), failure(429), Reply(self.content)])
        self.assertEqual((result, opened.call_count), (self.target, 3))
        self.target.unlink()
        result, opened, _ = self.run_download([failure(404), Reply(self.content)])
        self.assertIn("下载失败", str(result))
        self.assertEqual((opened.call_count, self.leftovers()), (1, []))

    def test_download_needs_an_installer_and_a_checksum(self):
        for info, text in ((UpdateInfo("v9.9.9", "https://github.com/x"), "没有可自动安装"),
                           (UpdateInfo("v9.9.9", "https://github.com/x", "https://dl.invalid/setup.exe"), "缺少安装包校验")):
            result, opened, _ = self.run_download([], info)
            self.assertIn(text, str(result))
            opened.assert_not_called()

    def test_download_switches_to_mirror_and_skips_unsynced_404_mirror(self):
        mirrored = UpdateInfo(
            "v9.9.9", "https://github.com/x", "https://dl.invalid/setup.exe", "setup.exe",
            len(self.content), hashlib.sha256(self.content).hexdigest(), "https://api.invalid/asset",
            mirror_url="https://cnb.cool/pretenderlu/ClassicDrop/-/releases/download/v9.9.9/setup.exe",
        )
        # Primary fails once -> immediately falls back to CNB mirror and succeeds with SHA-256 check.
        result, opened, _ = self.run_download([URLError("reset"), Reply(self.content)], mirrored)
        self.assertEqual(result.read_bytes(), self.content)
        self.assertEqual([c.args[0].full_url for c in opened.call_args_list], [mirrored.download_url, mirrored.mirror_url])
        self.target.unlink()

        # If CNB mirror has not finished syncing (404), skip it and succeed via GitHub asset API.
        mirror_404 = HTTPError(mirrored.mirror_url, 404, "Not Found", {}, None)
        result, opened, _ = self.run_download([URLError("reset"), mirror_404, Reply(self.content)], mirrored)
        self.assertEqual(result.read_bytes(), self.content)
        self.assertEqual([c.args[0].full_url for c in opened.call_args_list],
                         [mirrored.download_url, mirrored.mirror_url, mirrored.alternate_url])
        self.target.unlink()

        # If mirror returns 404 and GitHub asset API also fails, skip retrying the 404 mirror again.
        result, opened, _ = self.run_download([URLError("reset"), mirror_404, URLError("still-down")], mirrored)
        self.assertIn("下载失败", str(result))
        self.assertEqual([c.args[0].full_url for c in opened.call_args_list],
                         [mirrored.download_url, mirrored.mirror_url, mirrored.alternate_url])


class ReleaseConsistencyTests(unittest.TestCase):
    def test_version_is_stated_once_and_matches_the_documents(self):
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")
        readme = (PROJECT / "README.md").read_text(encoding="utf8")
        self.assertTrue(readme.startswith(f"# ClassicDrop {__version__}\n"))
        self.assertEqual(set(re.findall(r"ClassicDropSetup-v([\d.]+)\.exe", readme)), {__version__})
        for name in ("app.py", "import_dialog.py", "updates.py"):
            self.assertNotIn(__version__, (PROJECT / name).read_text(encoding="utf8"), name)

    def test_installer_and_workflow_use_the_same_names(self):
        script = (PROJECT / "installer" / "ClassicDrop.iss").read_text(encoding="utf8")
        self.assertIn("OutputBaseFilename=ClassicDropSetup-v{#MyAppVersion}", script)
        self.assertTrue(updates.INSTALLER.fullmatch(f"ClassicDropSetup-v{__version__}.exe"))
        self.assertIn(updates.REPOSITORY, script)
        workflow = (PROJECT / ".github" / "workflows" / "release.yml").read_text(encoding="utf8")
        self.assertIn("ClassicDropSetup-*.exe", workflow)
        self.assertIn("--draft", workflow)
