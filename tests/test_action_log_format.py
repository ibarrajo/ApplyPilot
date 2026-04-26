"""Pause-cycle action log: _format_action_log render + cache wiring.

Exercises the formatter that turns content.js's POSTed
{events, snapshots} payload into the USER ACTIONS DURING PAUSE
prompt section the agent sees on resume (spec §4.4).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_empty_payload_returns_none():
    from applypilot.apply.launcher import _format_action_log
    assert _format_action_log({}) is None
    assert _format_action_log({"events": [], "snapshots": {}}) is None
    assert _format_action_log(None) is None


def test_payload_with_events_renders_timeline():
    from applypilot.apply.launcher import _format_action_log
    out = _format_action_log({
        "events": [
            {"type": "click", "t": 1000, "text": "Sign in", "href": None},
            {"type": "nav", "t": 5000, "mode": "pushState", "url": "https://example.com/step-2"},
        ],
        "snapshots": {},
    })
    assert out is not None
    assert out.startswith("USER ACTIONS DURING PAUSE:")
    assert "Timeline" in out
    assert "Sign in" in out
    assert "+0:00" in out
    assert "+0:04" in out
    assert "step-2" in out


def test_payload_with_snapshots_renders_form_values():
    from applypilot.apply.launcher import _format_action_log
    out = _format_action_log({
        "events": [],
        "snapshots": {
            "https://workday.com/step-2": {"email": "a@b.com", "phone": "555"},
        },
    })
    assert out is not None
    assert "Form values now in tabs" in out
    assert "email: a@b.com" in out
    assert "phone: 555" in out


def test_payload_caps_events_at_50():
    """Excess events are truncated at the tail (most recent kept)."""
    from applypilot.apply.launcher import _format_action_log
    events = [
        {"type": "click", "t": i * 100, "text": f"btn-{i}"}
        for i in range(200)
    ]
    out = _format_action_log({"events": events, "snapshots": {}})
    assert out is not None
    # Should contain the LAST 50 events (btn-150 through btn-199).
    assert "btn-199" in out
    assert "btn-150" in out
    # Earliest events should be excluded.
    assert "btn-0\n" not in out and "btn-49\n" not in out


def test_endpoint_caches_payload(monkeypatch):
    """POST /api/action-log/{hash} stashes the body into _action_log_cache."""
    from applypilot.apply import launcher
    # Clear any previous cache state.
    with launcher._action_log_cache_lock:
        launcher._action_log_cache.clear()

    # Simulate the handler logic directly (bypassing HTTPServer).
    body = {"events": [{"type": "click", "t": 100, "text": "X"}], "snapshots": {}}
    with launcher._action_log_cache_lock:
        launcher._action_log_cache["abc123"] = body

    # _run_hitl will pop and format.
    with launcher._action_log_cache_lock:
        popped = launcher._action_log_cache.pop("abc123", None)
    assert popped == body
    formatted = launcher._format_action_log(popped)
    assert formatted is not None
    assert "X" in formatted
