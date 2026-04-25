#!/usr/bin/env python3
"""Smoke test for the apply runtime substrate: CfT + patchright + --load-extension.

Three checks:
  A. Direct CfT launch with --load-extension does NOT print the
     'not allowed in Google Chrome, ignoring' warning.
  B. patchright can launch CfT and run a basic page navigation.
  C. The custom extension at src/applypilot/apply/extension/ is loaded
     (visible via chrome://extensions/ DOM query).

Exits 0 on full success; non-zero on any failure with a diagnostic line.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXT_DIR = REPO_ROOT / "src" / "applypilot" / "apply" / "extension"
CFT_BIN = Path.home() / ".applypilot" / "chrome-for-testing" / "chrome-linux64" / "chrome"


def check_a_direct_launch() -> None:
    """Verify --load-extension is not silently rejected."""
    if not CFT_BIN.exists():
        sys.exit(f"FAIL[A]: CfT binary missing at {CFT_BIN} — run scripts/install_cft.py first")
    if not EXT_DIR.exists():
        sys.exit(f"FAIL[A]: extension dir missing at {EXT_DIR}")

    with tempfile.TemporaryDirectory() as td:
        proc = subprocess.Popen(
            [
                str(CFT_BIN),
                f"--user-data-dir={td}",
                f"--load-extension={EXT_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
                "--headless=new",
                "--disable-gpu",
                "about:blank",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(3)
        proc.terminate()
        try:
            _, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate()
    if "not allowed in Google Chrome, ignoring" in err:
        sys.exit(f"FAIL[A]: --load-extension was silently rejected. stderr:\n{err}")
    print("PASS[A]: --load-extension accepted by CfT (no rejection warning)")


def check_b_patchright_launch() -> None:
    """Verify patchright can drive CfT."""
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as e:
        sys.exit(f"FAIL[B]: patchright import failed: {e}")

    with tempfile.TemporaryDirectory() as td:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=td,
                executable_path=str(CFT_BIN),
                headless=True,
                args=[
                    f"--load-extension={EXT_DIR}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            page = ctx.new_page()
            page.goto("about:blank", timeout=10_000)
            title = page.title()
            ctx.close()
    print(f"PASS[B]: patchright launched CfT and navigated (title={title!r})")


def check_c_extension_loaded() -> None:
    """Verify the extension is recognized by chrome://extensions/."""
    from patchright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as td:
        with sync_playwright() as p:
            # chrome://extensions requires non-headless to enumerate via shadow DOM
            # in some Chrome versions. Try headless first; fall back if empty.
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=td,
                executable_path=str(CFT_BIN),
                headless=False,
                args=[
                    f"--load-extension={EXT_DIR}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            try:
                page = ctx.new_page()
                page.goto("chrome://extensions/", timeout=10_000)
                # Wait for the extensions-manager element to render.
                page.wait_for_selector("extensions-manager", timeout=10_000)
                names = page.evaluate(
                    """
                    () => {
                      const m = document.querySelector('extensions-manager');
                      if (!m || !m.shadowRoot) return [];
                      const itemList = m.shadowRoot.querySelector('extensions-item-list');
                      if (!itemList || !itemList.shadowRoot) return [];
                      const items = itemList.shadowRoot.querySelectorAll('extensions-item');
                      return Array.from(items).map(it => {
                        const name = it.shadowRoot && it.shadowRoot.querySelector('#name');
                        return name ? name.textContent.trim() : '(unknown)';
                      });
                    }
                    """
                )
            finally:
                ctx.close()
    if not names:
        sys.exit("FAIL[C]: chrome://extensions/ reported zero extensions (extension did not load)")
    print(f"PASS[C]: extension(s) loaded: {names}")


def main() -> None:
    print("Apply runtime smoke test")
    print(f"  CfT binary: {CFT_BIN}")
    print(f"  Extension : {EXT_DIR}")
    print()
    check_a_direct_launch()
    check_b_patchright_launch()
    check_c_extension_loaded()
    print()
    print("ALL CHECKS PASSED — runtime substrate is viable")


if __name__ == "__main__":
    main()
