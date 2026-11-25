"""Find a newer release on GitHub and download its installer. No interface code lives here."""

import hashlib
import json
import os
import platform
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from version import __version__

REPOSITORY = "pretenderlu/ClassicDrop"
PROJECT_URL = "https://github.com/" + REPOSITORY


def environment_lines(device=""):
    """Version, Windows build and iPod model; nothing that identifies the user. Shared with the feedback report."""
    return [f"- ClassicDrop：v{__version__}", f"- 系统：Windows {platform.release()}（{platform.version()}）",
            f"- iPod：{device or '未连接'}"]


def issue_url(device=""):
    """New-issue page with the environment filled in and a placeholder for the copied diagnostics."""
    body = "\n".join(["## 问题描述", "", "", "## 复现步骤", "", "", "## 环境", *environment_lines(device), "",
                      "## 诊断信息", "", "（已复制到剪贴板，请粘贴到这里）", ""])
    return PROJECT_URL + "/issues/new?" + urlencode({"body": body})


INSTALLER = re.compile(r"ClassicDropSetup-[\w.\-]+\.exe")


class UpdateError(RuntimeError):
    pass


class _IncompleteDownload(Exception):
    """The stream ended before the expected size; usually a dropped connection, so worth retrying."""


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    url: str
    download_url: str = ""
    installer_name: str = ""
    size: int = 0
    sha256: str = ""
    alternate_url: str = ""
    notes: str = ""


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    published: str  # the day it was published, as YYYY-MM-DD, when known
    notes: str


def plain_notes(markdown):
    """Release notes for a plain text box: Markdown marks are dropped, links keep their text."""
    lines = []
    for line in markdown.replace("\r\n", "\n").split("\n"):
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
        line = re.sub(r"\*\*|__|`", "", line)
        heading = re.match(r"#{1,6}\s+(.*)", line)
        lines.append(heading[1] if heading else re.sub(r"^(\s*)[-*]\s+", r"\1· ", line))
    return "\n".join(lines).strip()


def version_key(value):
    numbers = []
    for part in value.strip().lstrip("vV").split("-", 1)[0].split("."):
        match = re.match(r"\d+", part)
        if not match:
            break
        numbers.append(int(match.group(0)))
    return tuple(numbers + [0] * (4 - len(numbers)))


def _open(url, accept, timeout):
    request = Request(url, headers={"Accept": accept, "User-Agent": f"ClassicDrop/{__version__}"})
    return urlopen(request, timeout=timeout)


def check_for_updates(current_version=__version__, repository=REPOSITORY, timeout=10):
    """The newest published release if it is newer than current_version, otherwise None."""
    with _open(f"https://api.github.com/repos/{repository}/releases/latest", "application/vnd.github+json", timeout) as reply:
        return _update_info(json.load(reply), current_version)


def release_history(current_version=__version__, repository=REPOSITORY, timeout=10):
    """Published releases, newest first, and the update the newest offers over current_version, if any."""
    with _open(f"https://api.github.com/repos/{repository}/releases?per_page=30", "application/vnd.github+json", timeout) as reply:
        payload = json.load(reply)
    if not isinstance(payload, list):
        raise UpdateError("GitHub 返回的更新记录无法识别。")
    published = sorted((r for r in payload if isinstance(r, dict) and not r.get("draft") and not r.get("prerelease")
                        and r.get("tag_name") and str(r.get("html_url", "")).startswith("https://github.com/")),
                       key=lambda r: version_key(str(r["tag_name"])), reverse=True)
    releases = tuple(Release(str(r["tag_name"]), str(r["html_url"]), str(r.get("published_at") or "")[:10],
                             plain_notes(str(r.get("body") or ""))) for r in published)
    return releases, (_update_info(published[0], current_version) if published else None)


def _update_info(payload, current_version):
    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        return None
    tag, url = str(payload.get("tag_name", "")), str(payload.get("html_url", ""))
    if not tag or not url.startswith("https://github.com/") or version_key(tag) <= version_key(current_version):
        return None
    installer = next((asset for asset in payload.get("assets") or []
                      if INSTALLER.fullmatch(str(asset.get("name", "")))), {})
    digest = str(installer.get("digest", "") or "")
    # Without GitHub's own checksum there is nothing to verify the download against, so only the page is offered.
    if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest) or not str(
            installer.get("browser_download_url", "")).startswith("https://"):
        installer = {}
    return UpdateInfo(tag, url, installer.get("browser_download_url", ""), installer.get("name", ""),
                      int(installer.get("size", 0) or 0), digest.removeprefix("sha256:").lower() if installer else "",
                      installer.get("url", ""), str(payload.get("body", "") or ""))


def _retryable(error):
    if isinstance(error, HTTPError):
        return error.code == 429 or error.code >= 500
    return isinstance(error, (URLError, TimeoutError, ConnectionError, _IncompleteDownload))


def _fetch(info, url, temporary, progress):
    digest = hashlib.sha256()
    downloaded = 0
    with _open(url, "application/octet-stream", 60) as reply:
        total = info.size or int(reply.headers.get("Content-Length", 0) or 0)
        with temporary.open("wb") as output:
            while chunk := reply.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                downloaded += len(chunk)
                if progress:
                    progress(downloaded, total)
    if info.size and downloaded != info.size:
        raise _IncompleteDownload(f"{downloaded}/{info.size}")
    if digest.hexdigest() != info.sha256.lower():
        raise UpdateError("更新包校验失败，已阻止安装。")


def download_update(info, destination, progress=None):
    """Download the installer to destination, checking its size and SHA-256. Dropped connections are retried."""
    if not info.download_url:
        raise UpdateError("该版本没有可自动安装的 Windows 安装包。")
    if not info.sha256:
        raise UpdateError("该版本缺少安装包校验信息，请前往发布页手动下载。")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".download")
    urls = [info.download_url, info.download_url, *([info.alternate_url] if info.alternate_url else []), info.download_url]
    last = None
    for attempt, url in enumerate(urls):
        if attempt:
            time.sleep(2)
        try:
            _fetch(info, url, temporary, progress)
            os.replace(temporary, destination)
            return destination
        except UpdateError:
            raise
        except Exception as exc:
            if not _retryable(exc):
                raise UpdateError(f"下载失败：{exc}") from exc
            last = exc
        finally:
            temporary.unlink(missing_ok=True)
    if isinstance(last, _IncompleteDownload):
        raise UpdateError("更新包下载不完整，请重试。") from last
    raise UpdateError(f"下载失败：{last}")
