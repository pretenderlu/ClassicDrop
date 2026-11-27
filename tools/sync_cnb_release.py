"""Sync a published GitHub Release and its assets to a CNB (cnb.cool) mirror repository.

Stdlib-only (plus system curl for streaming large binary transfers without holding them in memory).
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "ClassicDrop-CNB-Sync/1.0"
CNB_ACCEPT = "application/vnd.cnb.api+json"


def _json_request(method, url, *, headers, payload=None, allow_404=False, timeout=60):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req_headers = {"User-Agent": USER_AGENT, **headers}
    if body is not None:
        req_headers.setdefault("Content-Type", "application/json")
    for attempt in range(1, 4):
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if allow_404 and exc.code == 404:
                return 404, None
            if exc.code >= 500 and attempt < 3:
                time.sleep(2 * attempt)
                continue
            raise RuntimeError(f"{method} {url} failed ({exc.code}): {detail[:400]}") from exc
        except urllib.error.URLError as exc:
            if attempt < 3:
                time.sleep(2 * attempt)
                continue
            raise RuntimeError(f"{method} {url} network error: {exc}") from exc
    raise RuntimeError(f"{method} {url} exhausted retries")


def _curl_escape(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run_curl(args, config_lines):
    config = "\n".join(config_lines) + "\n"
    proc = subprocess.run(
        ["curl", "-fsS", "--retry", "3", "--retry-delay", "3", "-K", "-", *args],
        input=config,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"curl failed (exit {proc.returncode}): {err[:400]}")


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_github_release(gh_repo, gh_token, tag):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"
    if tag and tag.lower() != "latest":
        path = f"tags/{urllib.parse.quote(tag, safe='')}"
    else:
        path = "latest"
    url = f"https://api.github.com/repos/{gh_repo}/releases/{path}"
    _, release = _json_request("GET", url, headers=headers)
    if release.get("draft"):
        raise RuntimeError(f"GitHub release {release.get('tag_name')} is still a draft; refusing to sync.")
    return release


def download_github_asset(asset, gh_token, dest):
    config = [
        f"url = {_curl_escape(asset['url'])}",
        f"header = {_curl_escape('Accept: application/octet-stream')}",
        f"header = {_curl_escape('User-Agent: ' + USER_AGENT)}",
    ]
    if gh_token:
        config.append(f"header = {_curl_escape('Authorization: Bearer ' + gh_token)}")
    _run_curl(["-L", "-o", str(dest)], config)
    actual_size = dest.stat().st_size
    if actual_size != asset["size"]:
        raise RuntimeError(f"{asset['name']}: size mismatch ({actual_size} != {asset['size']})")
    expected_digest = asset.get("digest") or ""
    if expected_digest.startswith("sha256:"):
        actual_hash = _sha256(dest)
        if actual_hash != expected_digest.removeprefix("sha256:").lower():
            raise RuntimeError(f"{asset['name']}: SHA-256 verification failed after download")


def ensure_cnb_release(cnb_api, cnb_repo, cnb_headers, gh_release):
    tag = gh_release["tag_name"]
    title = gh_release.get("name") or tag
    body = gh_release.get("body") or ""
    prerelease = bool(gh_release.get("prerelease"))
    make_latest = "false" if prerelease else "true"
    tag_quoted = urllib.parse.quote(tag, safe="")

    status, existing = _json_request(
        "GET",
        f"{cnb_api}/{cnb_repo}/-/releases/tags/{tag_quoted}",
        headers=cnb_headers,
        allow_404=True,
    )
    if status == 404:
        print(f"Creating CNB release {tag} ({title})...")
        _, created = _json_request(
            "POST",
            f"{cnb_api}/{cnb_repo}/-/releases",
            headers=cnb_headers,
            payload={
                "tag_name": tag,
                "target_commitish": gh_release.get("target_commitish") or "main",
                "name": title,
                "body": body,
                "draft": False,
                "prerelease": prerelease,
                "make_latest": make_latest,
            },
        )
        return created
    release_id = existing["id"]
    print(f"Updating existing CNB release {tag} (id={release_id})...")
    _, updated = _json_request(
        "PATCH",
        f"{cnb_api}/{cnb_repo}/-/releases/{release_id}",
        headers=cnb_headers,
        payload={
            "name": title,
            "body": body,
            "draft": False,
            "prerelease": prerelease,
            "make_latest": make_latest,
        },
    )
    return updated or existing


def upload_cnb_asset(cnb_api, cnb_repo, cnb_headers, release_id, file_path, name):
    size = file_path.stat().st_size
    _, ticket = _json_request(
        "POST",
        f"{cnb_api}/{cnb_repo}/-/releases/{release_id}/asset-upload-url",
        headers=cnb_headers,
        payload={
            "asset_name": name,
            "size": size,
            "overwrite": True,
            "ttl": 0,
        },
    )
    upload_url = ticket["upload_url"]
    verify_url = ticket["verify_url"]
    if verify_url.startswith("/"):
        verify_url = cnb_api.rstrip("/") + verify_url

    # Upload directly to the pre-signed storage URL without sending CNB Bearer credentials.
    _run_curl(
        ["-X", "PUT", "-T", str(file_path)],
        [
            f"url = {_curl_escape(upload_url)}",
            f"header = {_curl_escape('Content-Type: application/octet-stream')}",
            'header = "Expect:"',
        ],
    )
    # Confirm upload so CNB attaches the file permanently to the release.
    _json_request("POST", verify_url, headers=cnb_headers, payload={})


def sync_release(gh_repo, gh_token, cnb_repo, cnb_token, *, cnb_api="https://api.cnb.cool", tag="", overwrite=False):
    cnb_headers = {"Authorization": f"Bearer {cnb_token}", "Accept": CNB_ACCEPT}
    gh_release = fetch_github_release(gh_repo, gh_token, tag)
    cnb_release = ensure_cnb_release(cnb_api, cnb_repo, cnb_headers, gh_release)
    release_id = cnb_release["id"]
    existing_assets = {a["name"]: a for a in (cnb_release.get("assets") or [])}

    gh_assets = gh_release.get("assets") or []
    print(f"Syncing {len(gh_assets)} asset(s) for {gh_release['tag_name']} -> CNB release {release_id}")
    pending = []
    for asset in gh_assets:
        name = asset["name"]
        size = asset["size"]
        prev = existing_assets.get(name)
        if not overwrite and prev is not None and prev.get("size") == size:
            print(f"  [skip] {name} ({size} bytes already on CNB)")
        else:
            pending.append(asset)

    def transfer_one(asset, tmp_dir):
        name = asset["name"]
        size = asset["size"]
        local_file = tmp_dir / name
        print(f"  [download] {name} ({size} bytes) from GitHub...", flush=True)
        download_github_asset(asset, gh_token, local_file)
        try:
            print(f"  [upload] {name} -> CNB...", flush=True)
            upload_cnb_asset(cnb_api, cnb_repo, cnb_headers, release_id, local_file, name)
            print(f"  [done] {name}", flush=True)
        finally:
            local_file.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="cnb-sync-") as tmp:
        tmp_dir = Path(tmp)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(transfer_one, asset, tmp_dir) for asset in pending]
            for future in as_completed(futures):
                future.result()

    tag_quoted = urllib.parse.quote(gh_release["tag_name"], safe="")
    _, final = _json_request(
        "GET",
        f"{cnb_api}/{cnb_repo}/-/releases/tags/{tag_quoted}",
        headers=cnb_headers,
    )
    print(f"CNB release {final['tag_name']} ready with {len(final.get('assets') or [])} asset(s):")
    for item in final.get("assets") or []:
        url = item.get("browser_download_url") or item.get("url") or ""
        print(f"  - {item['name']} ({item.get('size')} bytes) {url}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="", help="GitHub release tag to sync (empty or 'latest' for latest)")
    parser.add_argument("--overwrite", action="store_true", help="Re-upload assets even if size matches on CNB")
    args = parser.parse_args(argv)

    cnb_token = os.environ.get("CNB_TOKEN", "").strip()
    if not cnb_token:
        raise SystemExit("CNB_TOKEN is not set.")
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "pretenderlu/ClassicDrop").strip()
    cnb_repo = os.environ.get("CNB_REPO", "pretenderlu/ClassicDrop").strip()
    cnb_api = os.environ.get("CNB_API", "https://api.cnb.cool").strip().rstrip("/")
    gh_token = os.environ.get("GH_TOKEN", "").strip()

    sync_release(gh_repo, gh_token, cnb_repo, cnb_token, cnb_api=cnb_api, tag=args.tag.strip(), overwrite=args.overwrite)


if __name__ == "__main__":
    main()
