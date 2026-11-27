"""Find a newer release on GitHub (with CNB mirror fallback) and download its installer. No interface code lives here."""

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from version import __version__
import cpuarch
import diagnostics
import system

REPOSITORY = "pretenderlu/ClassicDrop"
PROJECT_URL = "https://github.com/" + REPOSITORY
MIRROR_HOST = "https://cnb.cool"
MIRROR_RELEASES_URL = f"{MIRROR_HOST}/{REPOSITORY}/-/releases"
CNB_WEB_ACCEPT = "application/vnd.cnb.web+json"
DOWNLOAD_TIMEOUT = 25


def environment_lines(device=""):
    """Version, OS build and iPod model; nothing that identifies the user. Shared with the feedback report."""
    return [f"- ClassicDrop：v{__version__}", f"- 系统：{system.os_label()}",
            f"- 架构：{cpuarch.describe()}", f"- iPod：{device or '未连接'}"]


def issue_url(device=""):
    """New-issue page with the environment filled in and a placeholder for the copied diagnostics."""
    body = "\n".join(["## 问题描述", "", "", "## 复现步骤", "", "", "## 环境", *environment_lines(device), "",
                      "## 诊断信息", "", "（已复制到剪贴板，请粘贴到这里）", ""])
    return PROJECT_URL + "/issues/new?" + urlencode({"body": body})


INSTALLER = re.compile(r"ClassicDropSetup-[\w.\-]+\.exe")
MAC_DMG = re.compile(r"ClassicDrop-macOS-(arm64|x64)-[\w.\-]+\.dmg")


def mac_arch():
    """Hardware arch for the DMG choice: Apple Silicon even when the app runs under Rosetta."""
    return "arm64" if cpuarch.host_arch() == "ARM64" else "x64"


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
    mirror_url: str = ""


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


def _mirror_asset_url(repository, tag, name):
    if not repository or not tag or not name:
        return ""
    return f"{MIRROR_HOST}/{repository}/-/releases/download/{quote(tag, safe='')}/{quote(name, safe='')}"


def _cnb_release_url(repository, tag):
    return f"{MIRROR_HOST}/{repository}/-/releases/tag/{quote(tag, safe='')}"


def _cnb_tag(payload):
    tag = str(payload.get("tag_ref", "") or payload.get("tag_name", ""))
    return tag.removeprefix("refs/tags/")


def _match_installer(asset):
    name = str(asset.get("name", ""))
    if system.MAC:
        dmg = MAC_DMG.fullmatch(name)
        return bool(dmg and dmg.group(1) == mac_arch())
    return bool(INSTALLER.fullmatch(name))


def check_for_updates(current_version=__version__, repository=REPOSITORY, timeout=10):
    """The newest published release if it is newer than current_version, otherwise None."""
    try:
        with _open(f"https://api.github.com/repos/{repository}/releases/latest", "application/vnd.github+json", timeout) as reply:
            return _update_info(json.load(reply), current_version, repository)
    except (HTTPError, URLError, TimeoutError, OSError) as primary:
        diagnostics.log.info("GitHub 检查更新不可用，尝试 CNB 镜像：%s", primary)
        try:
            with _open(f"{MIRROR_HOST}/{repository}/-/releases/latest", CNB_WEB_ACCEPT, timeout) as reply:
                return _cnb_update_info(json.load(reply), current_version, repository)
        except Exception as secondary:
            diagnostics.log.info("CNB 镜像检查更新不可用：%s", secondary)
            raise primary


def _cnb_release_history(current_version, repository, timeout):
    with _open(f"{MIRROR_HOST}/{repository}/-/releases", CNB_WEB_ACCEPT, timeout) as reply:
        payload = json.load(reply)
    rows = payload.get("releases") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise UpdateError("CNB 镜像返回的更新记录无法识别。")
    published = sorted((r for r in rows if isinstance(r, dict) and not r.get("is_draft") and not r.get("draft")
                        and not r.get("is_prerelease") and not r.get("prerelease") and _cnb_tag(r)),
                       key=lambda r: version_key(_cnb_tag(r)), reverse=True)
    releases = tuple(Release(_cnb_tag(r), _cnb_release_url(repository, _cnb_tag(r)),
                             str(r.get("published_at") or "")[:10], plain_notes(str(r.get("body") or "")))
                     for r in published)
    return releases, (_cnb_update_info(published[0], current_version, repository) if published else None)


def release_history(current_version=__version__, repository=REPOSITORY, timeout=10):
    """Published releases, newest first, and the update the newest offers over current_version, if any."""
    try:
        with _open(f"https://api.github.com/repos/{repository}/releases?per_page=30", "application/vnd.github+json", timeout) as reply:
            payload = json.load(reply)
    except (HTTPError, URLError, TimeoutError, OSError) as primary:
        diagnostics.log.info("GitHub 读取更新记录不可用，尝试 CNB 镜像：%s", primary)
        try:
            return _cnb_release_history(current_version, repository, timeout)
        except Exception as secondary:
            diagnostics.log.info("CNB 镜像读取更新记录不可用：%s", secondary)
            raise primary
    if not isinstance(payload, list):
        raise UpdateError("GitHub 返回的更新记录无法识别。")
    published = sorted((r for r in payload if isinstance(r, dict) and not r.get("draft") and not r.get("prerelease")
                        and r.get("tag_name") and str(r.get("html_url", "")).startswith("https://github.com/")),
                       key=lambda r: version_key(str(r["tag_name"])), reverse=True)
    releases = tuple(Release(str(r["tag_name"]), str(r["html_url"]), str(r.get("published_at") or "")[:10],
                             plain_notes(str(r.get("body") or ""))) for r in published)
    return releases, (_update_info(published[0], current_version, repository) if published else None)


def _update_info(payload, current_version, repository=REPOSITORY):
    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        return None
    tag, url = str(payload.get("tag_name", "")), str(payload.get("html_url", ""))
    if not tag or not url.startswith("https://github.com/") or version_key(tag) <= version_key(current_version):
        return None
    installer = next((asset for asset in payload.get("assets") or [] if _match_installer(asset)), {})
    digest = str(installer.get("digest", "") or "")
    # Without GitHub's own checksum there is nothing to verify the download against, so only the page is offered.
    if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest) or not str(
            installer.get("browser_download_url", "")).startswith("https://"):
        installer = {}
    name = installer.get("name", "")
    mirror_url = _mirror_asset_url(repository, tag, name) if installer else ""
    return UpdateInfo(tag, url, installer.get("browser_download_url", ""), name,
                      int(installer.get("size", 0) or 0), digest.removeprefix("sha256:").lower() if installer else "",
                      installer.get("url", ""), str(payload.get("body", "") or ""), mirror_url)


def _cnb_update_info(payload, current_version, repository=REPOSITORY):
    if (not isinstance(payload, dict) or payload.get("is_draft") or payload.get("draft")
            or payload.get("is_prerelease") or payload.get("prerelease")):
        return None
    tag = _cnb_tag(payload)
    if not tag or version_key(tag) <= version_key(current_version):
        return None
    installer = next((asset for asset in payload.get("assets") or [] if _match_installer(asset)), {})
    algo = str(installer.get("hash_algo", "") or "").lower()
    digest = str(installer.get("hash_value", "") or "")
    path = str(installer.get("path", "") or "")
    prefix = f"/{repository}/-/releases/download/"
    if algo != "sha256" or not re.fullmatch(r"[0-9a-fA-F]{64}", digest) or not path.startswith(prefix) or ".." in path:
        installer = {}
    name = str(installer.get("name", "")) if installer else ""
    download_url = MIRROR_HOST + path if installer else ""
    size = int(installer.get("size_in_byte", 0) or installer.get("size", 0) or 0) if installer else 0
    github_fallback = (f"https://github.com/{repository}/releases/download/{quote(tag, safe='')}/{quote(name, safe='')}"
                       if installer else "")
    return UpdateInfo(tag, _cnb_release_url(repository, tag), download_url, name, size,
                      digest.lower() if installer else "", "", str(payload.get("body", "") or ""), github_fallback)


def _retryable(error):
    if isinstance(error, HTTPError):
        return error.code == 429 or error.code >= 500
    return isinstance(error, (URLError, TimeoutError, ConnectionError, _IncompleteDownload))


def _fetch(info, url, temporary, progress):
    digest = hashlib.sha256()
    downloaded = 0
    with _open(url, "application/octet-stream", DOWNLOAD_TIMEOUT) as reply:
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
    """Download the installer to destination, checking its size and SHA-256. Falls back to mirror/alternate URLs."""
    if not info.download_url:
        raise UpdateError("该版本没有可自动安装的" + (" macOS 更新包。" if system.MAC else " Windows 安装包。"))
    if not info.sha256:
        raise UpdateError("该版本缺少安装包校验信息，请前往发布页手动下载。")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".download")
    if info.mirror_url:
        # Try primary once, then switch to mirror immediately so blocked networks do not wait through 3 retries.
        urls = [info.download_url, info.mirror_url, *([info.alternate_url] if info.alternate_url else []), info.mirror_url]
    else:
        urls = [info.download_url, info.download_url, *([info.alternate_url] if info.alternate_url else []), info.download_url]
    last = None
    missing_mirrors = set()
    attempted = 0
    for url in urls:
        if url in missing_mirrors:
            continue
        if attempted:
            time.sleep(2)
        attempted += 1
        try:
            _fetch(info, url, temporary, progress)
            os.replace(temporary, destination)
            if url == info.mirror_url:
                diagnostics.log.info("已通过备用镜像完成更新包下载：%s", info.installer_name or info.version)
            return destination
        except UpdateError:
            raise
        except Exception as exc:
            # A brand-new release may still be syncing to the mirror; skip a 404 mirror instead of aborting.
            if url == info.mirror_url and isinstance(exc, HTTPError) and exc.code == 404:
                diagnostics.log.info("备用镜像尚未同步该版本（404），继续尝试其他地址。")
                missing_mirrors.add(url)
                if last is None:
                    last = exc
                continue
            if not _retryable(exc):
                raise UpdateError(f"下载失败：{exc}") from exc
            diagnostics.log.info("下载更新失败（%s），将尝试下一个地址。", exc)
            last = exc
        finally:
            temporary.unlink(missing_ok=True)
    if isinstance(last, _IncompleteDownload):
        raise UpdateError("更新包下载不完整，请重试。") from last
    raise UpdateError(f"下载失败：{last}")
