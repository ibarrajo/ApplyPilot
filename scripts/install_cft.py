#!/usr/bin/env python3
"""Download Chrome for Testing 148 to ~/.applypilot/chrome-for-testing/.

Idempotent: skips download if binary already present and version matches latest 148.x.
"""
import json
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

CFT_DIR = Path.home() / ".applypilot" / "chrome-for-testing"
TARGET_BIN = CFT_DIR / "chrome-linux64" / "chrome"
MAJOR = "148"


def latest_148_url() -> tuple[str, str]:
    """Return (version, download_url) for the latest CfT 148.x linux64 build."""
    feed = "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json"
    with urllib.request.urlopen(feed) as r:
        data = json.load(r)
    candidates = [v for v in data["versions"] if v["version"].startswith(f"{MAJOR}.")]
    if not candidates:
        sys.exit(f"No CfT {MAJOR}.x found in feed {feed}")
    latest = candidates[-1]
    chrome_dl = next(d for d in latest["downloads"]["chrome"] if d["platform"] == "linux64")
    return latest["version"], chrome_dl["url"]


def main() -> None:
    version, dl_url = latest_148_url()
    if TARGET_BIN.exists():
        try:
            cur = subprocess.check_output([str(TARGET_BIN), "--version"], text=True).strip()
        except subprocess.CalledProcessError:
            cur = ""
        if version in cur:
            print(f"CfT {version} already installed at {TARGET_BIN}")
            return
        print(f"Replacing existing CfT install (was: {cur!r}, want: {version})")
        shutil.rmtree(CFT_DIR / "chrome-linux64", ignore_errors=True)

    CFT_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = CFT_DIR / "chrome-linux64.zip"
    print(f"Downloading CfT {version} from {dl_url}...")
    urllib.request.urlretrieve(dl_url, zip_path)

    print("Extracting...")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(CFT_DIR)
    zip_path.unlink()

    if not TARGET_BIN.exists():
        sys.exit(f"Extraction failed: {TARGET_BIN} not found after unzip")

    # zipfile.extractall() does not preserve POSIX permissions. Restore exec
    # bits on every file CfT ships as a binary. chrome_crashpad_handler is
    # spawned by chrome at startup and aborts the whole process if it can't
    # exec.
    extracted = CFT_DIR / "chrome-linux64"
    for name in ("chrome", "chrome_crashpad_handler", "chrome-wrapper", "chrome_sandbox"):
        p = extracted / name
        if p.exists():
            p.chmod(0o755)

    out = subprocess.check_output([str(TARGET_BIN), "--version"], text=True).strip()
    print(f"Installed: {out}")


if __name__ == "__main__":
    main()
