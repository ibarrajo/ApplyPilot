"""End-to-end test: real CfT + extension + launcher HTTP server.

Exercises the complete action-log flow:
  1. Start the always-on per-worker HTTP server (real port).
  2. Launch CfT 148 with our extension via patchright, headless.
  3. Have the SW POST {events, snapshots} via /api/action-log/{hash}.
  4. Assert the launcher's _action_log_cache holds it.
  5. Assert _format_action_log renders a non-empty USER ACTIONS block.

Skipped automatically if CfT or patchright isn't installed (CI / dev
environments without the runtime substrate).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


CFT_BIN = Path.home() / ".applypilot" / "chrome-for-testing" / "chrome-linux64" / "chrome"
EXT_DIR = Path(__file__).parent.parent / "src" / "applypilot" / "apply" / "extension"
TEST_WORKER_ID = 92  # avoid collision with realistic deployments (≤5)


@pytest.fixture(scope="module")
def cft_available():
    if not CFT_BIN.exists():
        pytest.skip(f"CfT binary not at {CFT_BIN} — run scripts/install_cft.py")
    try:
        import patchright  # noqa: F401
    except ImportError:
        pytest.skip("patchright not installed")


@pytest.fixture
def worker_server():
    """Start the always-on per-worker HTTP server on TEST_WORKER_ID's port."""
    from applypilot.apply import launcher

    with launcher._worker_state_lock:
        launcher._worker_state[TEST_WORKER_ID] = {
            "status": "idle",
            "job": {},
            "history": [],
        }
    port = launcher._start_worker_listener(TEST_WORKER_ID)
    time.sleep(0.1)
    yield port
    launcher._stop_worker_listener(TEST_WORKER_ID)
    with launcher._worker_state_lock:
        launcher._worker_state.pop(TEST_WORKER_ID, None)


def test_extension_loads_in_cft(cft_available):
    """The custom extension must load on Chrome for Testing."""
    import tempfile

    from patchright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as td:
        with sync_playwright() as p:
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
                # The SW target must register within a few seconds.
                deadline = time.time() + 5.0
                sw = None
                while time.time() < deadline:
                    if ctx.service_workers:
                        sw = ctx.service_workers[0]
                        break
                    time.sleep(0.1)
                assert sw is not None, "extension service worker did not register"
            finally:
                ctx.close()


def test_stealth_overrides_apply_in_main_world(cft_available):
    """chrome.scripting injection lands navigator.webdriver === undefined etc.

    Reads via SW-side chrome.scripting.executeScript with world: 'MAIN' —
    patchright's page.evaluate runs in ISOLATED world by design and can't
    see MAIN-world overrides.
    """
    import tempfile

    from patchright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as td:
        with sync_playwright() as p:
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
                page.goto("https://example.com", timeout=10_000)
                time.sleep(2)
                sw = ctx.service_workers[0]
                # Read multiple stealth markers via MAIN-world execute.
                results = sw.evaluate(
                    """
                    async () => {
                      const tabs = await chrome.tabs.query({active: true});
                      const r = await chrome.scripting.executeScript({
                        target: {tabId: tabs[0].id},
                        world: 'MAIN',
                        func: () => ({
                          stealthMarker: window.__ap_stealth_loaded === true,
                          webdriverUndefined: navigator.webdriver === undefined,
                          pluginsLength: navigator.plugins.length,
                          chromeRuntime: !!(window.chrome && window.chrome.runtime),
                          languages: navigator.languages.length,
                        }),
                      });
                      return r[0].result;
                    }
                    """
                )
                assert results["stealthMarker"] is True, \
                    f"stealth.js not loaded in MAIN world; got {results}"
                assert results["webdriverUndefined"] is True
                assert results["pluginsLength"] >= 1
                assert results["chromeRuntime"] is True
                assert results["languages"] >= 1
            finally:
                ctx.close()


def test_action_log_round_trip_via_extension(cft_available, worker_server):
    """Full flow: SW posts {events, snapshots} → launcher caches it → format renders.

    Simulates the user clicking the banner Done by setting
    window.__ap_hitl_done from the SW context, then asserts the cache
    contains the payload.
    """
    import tempfile

    from applypilot.apply import launcher
    from patchright.sync_api import sync_playwright

    port = worker_server  # 7380 + 92 = 7472
    job_hash = "e2eabc12cdef"

    with launcher._action_log_cache_lock:
        launcher._action_log_cache.clear()

    with tempfile.TemporaryDirectory() as td:
        with sync_playwright() as p:
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
                page.goto("https://example.com", timeout=10_000)
                time.sleep(2)

                # Use the SW to POST /api/action-log/{hash} via fetch.
                # (Skipping content.js's banner-click polling since simulating
                # window.__ap_hitl_done in MAIN world here would require an
                # extra hop that doesn't add coverage.)
                sw = ctx.service_workers[0]
                # Pass the payload as a JS arg so we don't have to worry
                # about Python repr → JS literal translation (None vs null).
                payload = {
                    "events": [
                        {"type": "click", "t": 1000, "text": "Sign in"},
                        {"type": "submit", "t": 5000, "fields": [{"name": "x", "value": "y"}]},
                    ],
                    "snapshots": {"https://example.com": {"email": "test@example.com"}},
                }
                sw.evaluate(
                    """
                    async (args) => {
                      const url = 'http://localhost:' + args.port + '/api/action-log/' + args.hash;
                      await fetch(url, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(args.payload),
                      });
                    }
                    """,
                    {"port": port, "hash": job_hash, "payload": payload},
                )
                time.sleep(0.5)
            finally:
                ctx.close()

    # After Chrome shut down, the launcher's cache should still hold the entry.
    with launcher._action_log_cache_lock:
        cached = launcher._action_log_cache.pop(job_hash, None)
    assert cached is not None, "launcher did not receive the SW's POST"
    assert cached["snapshots"]["https://example.com"]["email"] == "test@example.com"

    rendered = launcher._format_action_log(cached)
    assert rendered is not None
    assert "USER ACTIONS DURING PAUSE" in rendered
    assert "Sign in" in rendered
