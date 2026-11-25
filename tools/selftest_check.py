"""Run the bundled app's --self-test with a hard timeout and check the JSON report.

Usage: python tools/selftest_check.py <report.json> <arm64|x64>
"""
import json
import subprocess
import sys

BINARY = "dist/ClassicDrop.app/Contents/MacOS/ClassicDrop"


def main():
    target, arch = sys.argv[1], sys.argv[2]
    try:
        p = subprocess.run([BINARY, "--self-test", target], timeout=120)
    except subprocess.TimeoutExpired:
        print("self-test timed out")
        return 1
    report = json.load(open(target, encoding="utf-8"))
    print(json.dumps(report, ensure_ascii=False, indent=1))
    assert p.returncode == 0 and report["ok"], report
    assert report["process_arch"] == ("ARM64" if arch == "arm64" else "x64"), report
    return 0


if __name__ == "__main__":
    sys.exit(main())
