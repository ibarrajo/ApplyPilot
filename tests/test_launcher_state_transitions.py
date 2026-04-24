"""TDD tests for state-machine wiring in launcher.py manual-intervention paths.

Each test seeds a job, calls the relevant launcher function, then asserts
the state column and audit history match the expected transition.

The functions under test (mark_job, reset_failed, mark_needs_human,
reset_needs_human) all call get_connection() internally.  We rely on
tmp_db to monkeypatch DB_PATH so that get_connection() opens the same
in-memory-backed tmp file.

HTTP handler (B5 / _handle_jobs_mark) uses the same transition calls
as mark_job / mark_needs_human, so its paths are covered transitively
by the CLI tests; no separate HTTP integration test is added here
(see commit message for rationale).
"""

import pytest

from applypilot.database import current_state, state_history


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_ready(conn, seed_job, suffix):
    """Seed a job in 'ready_to_apply' state (the normal pre-apply state)."""
    row = seed_job(conn, url_suffix=suffix)
    # Force the state column to ready_to_apply so transitions make sense.
    conn.execute("UPDATE jobs SET state = 'ready_to_apply' WHERE url = ?", (row["url"],))
    conn.commit()
    return row


def _seed_applying(conn, seed_job, suffix):
    """Seed a job in 'applying' state (worker holds it)."""
    row = seed_job(conn, url_suffix=suffix)
    conn.execute("UPDATE jobs SET state = 'applying' WHERE url = ?", (row["url"],))
    conn.commit()
    return row


def _seed_needs_human(conn, seed_job, suffix):
    """Seed a job in 'needs_human' state."""
    row = seed_job(conn, url_suffix=suffix, apply_status="needs_human")
    conn.execute("UPDATE jobs SET state = 'needs_human' WHERE url = ?", (row["url"],))
    conn.commit()
    return row


def _seed_apply_failed(conn, seed_job, suffix):
    """Seed a job in 'apply_failed' state."""
    row = seed_job(conn, url_suffix=suffix, apply_status="failed")
    conn.execute("UPDATE jobs SET state = 'apply_failed' WHERE url = ?", (row["url"],))
    conn.commit()
    return row


# ---------------------------------------------------------------------------
# B1 — mark_needs_human → 'needs_human'
# ---------------------------------------------------------------------------

def test_mark_needs_human_transitions_to_needs_human(tmp_db, seed_job):
    conn = tmp_db()
    row = _seed_applying(conn, seed_job, "nh-b1")
    url = row["url"]

    from applypilot.apply.launcher import mark_needs_human
    mark_needs_human(
        url=url,
        reason="captcha",
        stuck_url="https://example.com/captcha",
        instructions="Solve the CAPTCHA",
        duration_ms=5000,
    )

    assert current_state(conn, url) == "needs_human"
    history = state_history(conn, url)
    reasons = [h["reason"] for h in history]
    assert any("needs_human" in (r or "") or "captcha" in (r or "") for r in reasons), (
        f"Expected a transition reason containing 'needs_human' or 'captcha'; got {reasons}"
    )


# ---------------------------------------------------------------------------
# B2 — reset_needs_human → 'applying'
# ---------------------------------------------------------------------------

def test_reset_needs_human_transitions_to_applying(tmp_db, seed_job):
    conn = tmp_db()
    row = _seed_needs_human(conn, seed_job, "nh-b2")
    url = row["url"]

    from applypilot.apply.launcher import reset_needs_human
    count = reset_needs_human(url=url)

    assert count == 1, f"Expected 1 row reset, got {count}"
    assert current_state(conn, url) == "applying"
    history = state_history(conn, url)
    reasons = [h["reason"] for h in history]
    assert any("applying" in (r or "") or "needs_human resolved" in (r or "")
               for r in reasons), (
        f"Expected transition reason mentioning 'applying' or 'resolved'; got {reasons}"
    )


# ---------------------------------------------------------------------------
# B3 — mark_job 'applied' → 'applied'
# ---------------------------------------------------------------------------

def test_mark_job_applied_transitions_to_applied(tmp_db, seed_job):
    conn = tmp_db()
    row = _seed_applying(conn, seed_job, "mj-applied")
    url = row["url"]

    from applypilot.apply.launcher import mark_job
    mark_job(url=url, status="applied")

    assert current_state(conn, url) == "applied"
    history = state_history(conn, url)
    assert any(h["to_state"] == "applied" for h in history), (
        f"No 'applied' transition found in history: {history}"
    )
    assert any("manually marked applied" in (h["reason"] or "") for h in history), (
        f"No CLI reason in history: {[h['reason'] for h in history]}"
    )


# ---------------------------------------------------------------------------
# B3 — mark_job 'failed' → 'apply_failed'
# ---------------------------------------------------------------------------

def test_mark_job_failed_transitions_to_apply_failed(tmp_db, seed_job):
    conn = tmp_db()
    row = _seed_applying(conn, seed_job, "mj-failed")
    url = row["url"]

    from applypilot.apply.launcher import mark_job
    mark_job(url=url, status="failed", reason="test failure")

    assert current_state(conn, url) == "apply_failed"
    history = state_history(conn, url)
    assert any(h["to_state"] == "apply_failed" for h in history), (
        f"No 'apply_failed' transition found: {history}"
    )
    assert any("manually marked failed" in (h["reason"] or "") for h in history), (
        f"No CLI reason in history: {[h['reason'] for h in history]}"
    )


# ---------------------------------------------------------------------------
# B3 — mark_job 'manual' → 'manual_only'
# ---------------------------------------------------------------------------

def test_mark_job_manual_transitions_to_manual_only(tmp_db, seed_job):
    conn = tmp_db()
    row = _seed_ready(conn, seed_job, "mj-manual")
    url = row["url"]

    from applypilot.apply.launcher import mark_job
    mark_job(url=url, status="manual")

    assert current_state(conn, url) == "manual_only"
    history = state_history(conn, url)
    assert any(h["to_state"] == "manual_only" for h in history), (
        f"No 'manual_only' transition found: {history}"
    )
    assert any("manually marked manual" in (h["reason"] or "") for h in history), (
        f"No CLI reason in history: {[h['reason'] for h in history]}"
    )


# ---------------------------------------------------------------------------
# B4 — reset_failed → 'ready_to_apply'
# ---------------------------------------------------------------------------

def test_reset_failed_transitions_to_ready_to_apply(tmp_db, seed_job):
    conn = tmp_db()
    row = _seed_apply_failed(conn, seed_job, "rf-b4")
    url = row["url"]

    from applypilot.apply.launcher import reset_failed
    count = reset_failed()

    assert count >= 1, f"Expected at least 1 row reset, got {count}"
    assert current_state(conn, url) == "ready_to_apply"
    history = state_history(conn, url)
    reasons = [h["reason"] for h in history]
    assert any("reset_failed" in (r or "") or "re-queued" in (r or "") for r in reasons), (
        f"Expected transition reason mentioning 'reset_failed' or 're-queued'; got {reasons}"
    )
