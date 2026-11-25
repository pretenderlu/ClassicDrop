"""Download the pinned qaac release and place its x64 binaries in vendor/qaac.

These binaries are fetched at build/test time and are not committed to git.
"""
import hashlib
import io
from pathlib import Path
import sys
from urllib.request import Request, urlopen
from zipfile import ZipFile

URL = "https://github.com/nu774/qaac/releases/download/v3.07/qaac_3.07.zip"
SHA256 = "1fb3ab4aa81725e2607ac1b31afa0e13d2617ee6eb5900f5070c092f5271cbb3"
FILES = {
    "qaac64.exe": "6c982e8536d83f09ee08212daa43a41973559bb20133cec52f10137f54681b99",
    "refalac64.exe": "db7b6cdb6daf1ab64070cb1e9f0b6e3124497f8c115cff8366437360e9254438",
    "libsoxr64.dll": "84e7bb0ffea76879baaf72b04e6d1201e94ac22581571a40cb3933444e0eae53",
}
DEST = Path(__file__).resolve().parents[1] / "vendor" / "qaac"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    if all((DEST / name).is_file() and digest(DEST / name) == want for name, want in FILES.items()):
        print("vendor/qaac is up to date")
        return
    print(f"Downloading {URL}")
    with urlopen(Request(URL, headers={"User-Agent": "ClassicDrop-build"}), timeout=60) as reply:
        data = reply.read()
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise SystemExit("qaac download failed its SHA-256 check")
    with ZipFile(io.BytesIO(data)) as zip_file:
        for name in FILES:
            member = f"qaac_3.07/x64/{name}"
            (DEST / name).write_bytes(zip_file.read(member))
    for name, want in FILES.items():
        if digest(DEST / name) != want:
            raise SystemExit(f"{name} failed its SHA-256 check")
    print("vendor/qaac:", ", ".join(FILES))


if __name__ == "__main__":
    sys.exit(main())
