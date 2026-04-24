"""Tests for the job state machine in database.py.

Covers: VALID_TRANSITIONS graph, transition_state() atomicity + validation,
current_state(), state_history(), and backfill_states() derivation rules.
"""

import pytest

from applypilot.database import (
    VALID_STATES,
    VALID_TRANSITIONS,
    backfill_states,
    current_state,
    state_history,
    transition_state,
)


# ── VALID_TRANSITIONS graph invariants ────────────────────────────

def test_all_states_have_transition_entry():
    """Every state in VALID_STATES must appear as a key in VALID_TRANSITIONS."""
    missing = VALID_STATES - set(VALID_TRANSITIONS.keys())
    assert not missing, f"States missing from VALID_TRANSITIONS: {missing}"


def test_all_transition_targets_are_valid_states():
    """Every 'to' state in VALID_TRANSITIONS must be a declared state."""
    for src, targets in VALID_TRANSITIONS.items():
        invalid = targets - VALID_STATES
        assert not invalid, f"{src} has invalid targets: {invalid}"


def test_archived_is_terminal():
    """archived is the only universal terminal state."""
    assert VALID_TRANSITIONS["archived"] == frozenset()


def test_offer_only_transitions_to_archived():
    """An offer should only transition to archived (after accept/decline)."""
    assert VALID_TRANSITIONS["offer"] == frozenset({"archived"})


# ── transition_state() behavior ───────────────────────────────────

def test_legal_transition_succeeds(tmp_db, seed_job):
    conn = tmp_db()
    url = seed_job(conn, url_suffix="t1")["url"]
    # Seeded jobs start at state='discovered' (default).
    assert transition_state(conn, url, "enriched", reason="test") is True
    assert current_state(conn, url) == "enriched"


def test_illegal_transition_rejected(tmp_db, seed_job):
    conn = tmp_db()
    url = seed_job(conn, url_suffix="t2")["url"]
    # discovered → interview is illegal (would need to go through many steps)
    assert transition_state(conn, url, "interview") is False
    assert current_state(conn, url) == "discovered"


def test_force_bypasses_validation(tmp_db, seed_job):
    conn = tmp_db()
    url = seed_job(conn, url_suffix="t3")["url"]
    assert transition_state(conn, url, "interview", force=True) is True
    assert current_state(conn, url) == "interview"


def test_unknown_state_raises(tmp_db, seed_job):
    conn = tmp_db()
    url = seed_job(conn, url_suffix="t4")["url"]
    with pytest.raises(ValueError, match="Unknown state"):
        transition_state(conn, url, "not_a_real_state")


def test_missing_job_raises(tmp_db):
    conn = tmp_db()
    with pytest.raises(ValueError, match="Job not found"):
        transition_state(conn, "https://example.com/nope", "enriched")


def test_same_state_transition_is_allowed(tmp_db, seed_job):
    """Transitioning to the current state (no-op) should succeed silently."""
    conn = tmp_db()
    url = seed_job(conn, url_suffix="t5")["url"]
    assert transition_state(conn, url, "discovered") is True


def test_transition_writes_audit_row(tmp_db, seed_job):
    conn = tmp_db()
    url = seed_job(conn, url_suffix="t6")["url"]
    assert transition_state(conn, url, "enriched", reason="loaded 3k chars",
                            metadata={"chars": 3012, "tier": "T1"}) is True

    history = state_history(conn, url)
    assert len(history) == 1
    assert history[0]["from_state"] == "discovered"
    assert history[0]["to_state"] == "enriched"
    assert history[0]["reason"] == "loaded 3k chars"
    # metadata is JSON-serialized
    import json
    md = json.loads(history[0]["metadata"])
    assert md["chars"] == 3012
    assert md["tier"] == "T1"


def test_state_history_ordering(tmp_db, seed_job):
    """state_history returns newest-first."""
    conn = tmp_db()
    url = seed_job(conn, url_suffix="t7")["url"]
    transition_state(conn, url, "enriched")
    transition_state(conn, url, "scored")
    transition_state(conn, url, "tailoring")

    history = state_history(conn, url)
    assert [h["to_state"] for h in history] == ["tailoring", "scored", "enriched"]


# ── backfill_states() derivation ──────────────────────────────────

def test_backfill_derives_applied(tmp_db, seed_job):
    conn = tmp_db()
    url = seed_job(conn, url_suffix="b1",
                   apply_status="applied",
                   applied_at="2026-04-01T00:00:00+00:00")["url"]
    counts = backfill_states(conn)
    assert counts.get("applied") == 1
    assert current_state(conn, url) == "applied"


def test_backfill_derives_interview_from_tracking(tmp_db, seed_job):
    """tracking_status beats apply_status in precedence."""
    conn = tmp_db()
    url = seed_job(conn, url_suffix="b2",
                   apply_status="applied",
                   tracking_status="interview")["url"]
    counts = backfill_states(conn)
    assert counts.get("interview") == 1
    assert current_state(conn, url) == "interview"


def test_backfill_derives_low_score(tmp_db, seed_job):
    conn = tmp_db()
    url = seed_job(conn, url_suffix="b3",
                   fit_score=3)["url"]
    backfill_states(conn)
    assert current_state(conn, url) == "low_score"


def test_backfill_derives_ready_to_apply(tmp_db, seed_job):
    conn = tmp_db()
    url = seed_job(conn, url_suffix="b4",
                   fit_score=9,
                   tailored_resume_path="/tmp/r.docx",
                   cover_letter_path="/tmp/c.docx",
                   application_url="https://ex.com/apply")["url"]
    backfill_states(conn)
    assert current_state(conn, url) == "ready_to_apply"


def test_backfill_is_idempotent(tmp_db, seed_job):
    """Running backfill twice shouldn't re-process any job."""
    conn = tmp_db()
    seed_job(conn, url_suffix="i1", fit_score=9,
             tailored_resume_path="/tmp/r.docx",
             cover_letter_path="/tmp/c.docx",
             application_url="https://ex.com/apply")
    first = backfill_states(conn)
    assert sum(first.values()) == 1
    second = backfill_states(conn)
    assert second == {}  # nothing more to do
