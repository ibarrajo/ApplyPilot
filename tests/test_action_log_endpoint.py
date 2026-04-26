"""Integration test: POST /api/action-log/{hash} via the always-on server.

Exercises the full network path that content.js uses — start the worker
listener (real HTTPServer in a daemon thread), POST a payload, verify
the launcher's _action_log_cache holds it.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# Pick a worker id far above the max realistic deployment (5) to avoid
# colliding with any leftover servers. The HTTP port becomes 7380 + 91 = 7471.
TEST_WORKER_ID = 91


@pytest.fixture
def worker_server():
    """Start the always-on per-worker server for TEST_WORKER_ID and tear it
    down after the test. Returns the (port, hash) the test should use."""
    from applypilot.apply import launcher

    # Initialize per-worker state so the server has a `state` dict to read.
    with launcher._worker_state_lock:
        launcher._worker_state[TEST_WORKER_ID] = {
            "status": "idle",
            "job": {},
            "history": [],
        }

    port = launcher._start_worker_listener(TEST_WORKER_ID)
    # Give the server thread a moment to bind.
    time.sleep(0.1)

    yield port

    # Cleanup
    launcher._stop_worker_listener(TEST_WORKER_ID)
    with launcher._worker_state_lock:
        launcher._worker_state.pop(TEST_WORKER_ID, None)


def test_action_log_endpoint_caches_body(worker_server):
    """POST /api/action-log/{hash} → body lands in launcher._action_log_cache."""
    from applypilot.apply import launcher

    # Clear any previous cache state.
    with launcher._action_log_cache_lock:
        launcher._action_log_cache.clear()

    port = worker_server
    job_hash = "deadbeef0000"
    payload = {
        "events": [
            {"type": "click", "t": 1700, "text": "Sign in"},
            {"type": "submit", "t": 8500, "fields": [{"name": "email", "value": "a@b"}]},
        ],
        "snapshots": {
            "https://example.com/step-2": {"email": "alex@example.com"},
        },
    }

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/action-log/{job_hash}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2) as resp:
        assert resp.status == 200

    # Cache should now hold the payload, keyed by hash.
    with launcher._action_log_cache_lock:
        cached = launcher._action_log_cache.get(job_hash)
    assert cached == payload


def test_action_log_endpoint_overwrites_same_hash(worker_server):
    """Two POSTs to the same hash: latest wins (last-write-wins cache)."""
    from applypilot.apply import launcher

    with launcher._action_log_cache_lock:
        launcher._action_log_cache.clear()

    port = worker_server
    job_hash = "feedface0000"

    def _post(payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/action-log/{job_hash}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2):
            pass

    _post({"events": [{"type": "click", "t": 1, "text": "first"}], "snapshots": {}})
    _post({"events": [{"type": "click", "t": 2, "text": "second"}], "snapshots": {}})

    with launcher._action_log_cache_lock:
        cached = launcher._action_log_cache.get(job_hash)
    assert cached["events"][0]["text"] == "second"


def test_action_log_endpoint_rejects_empty_hash(worker_server):
    """POST /api/action-log/ (trailing slash, empty hash) → 400."""
    port = worker_server
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/action-log/",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=2)
    assert exc.value.code == 400


def test_action_log_endpoint_then_format_renders_section(worker_server):
    """Cached payload survives round-trip through _format_action_log."""
    from applypilot.apply import launcher

    with launcher._action_log_cache_lock:
        launcher._action_log_cache.clear()

    port = worker_server
    job_hash = "cafe12345678"
    payload = {
        "events": [
            {"type": "click", "t": 0, "text": "Allow", "href": None},
            {"type": "nav", "t": 3000, "mode": "popstate", "url": "https://x.com/y"},
        ],
        "snapshots": {"https://x.com/y": {"name": "Test User"}},
    }

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/action-log/{job_hash}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2):
        pass

    # Pop and format (the same thing _run_hitl does after hitl_event fires).
    with launcher._action_log_cache_lock:
        popped = launcher._action_log_cache.pop(job_hash, None)
    assert popped is not None

    rendered = launcher._format_action_log(popped)
    assert rendered is not None
    assert rendered.startswith("USER ACTIONS DURING PAUSE:")
    assert "Allow" in rendered
    assert "popstate" in rendered
    assert "name: Test User" in rendered
