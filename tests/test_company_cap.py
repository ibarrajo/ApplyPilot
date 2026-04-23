"""Tests for per-company open-pipeline cap."""

from datetime import datetime, timedelta, timezone


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_in_flight_counts_applied_in_progress_needs_human(tmp_db, seed_job):
    from applypilot.database import get_in_flight_by_company
    conn = tmp_db()
    seed_job(conn, url_suffix="a1", company="acme", apply_status="applied",
             applied_at=_iso(5))
    seed_job(conn, url_suffix="a2", company="acme", apply_status="in_progress",
             applied_at=None, last_attempted_at=_iso(1))
    seed_job(conn, url_suffix="a3", company="acme", apply_status="needs_human",
             applied_at=None, last_attempted_at=_iso(2))
    seed_job(conn, url_suffix="a4", company="acme", apply_status="failed",
             applied_at=_iso(3))
    seed_job(conn, url_suffix="a5", company="acme", apply_status="manual",
             applied_at=_iso(4))

    result = get_in_flight_by_company(conn)
    assert "acme" in result
    assert len(result["acme"]) == 3


def test_in_flight_uses_applied_at_or_last_attempted(tmp_db, seed_job):
    from applypilot.database import get_in_flight_by_company
    conn = tmp_db()
    seed_job(conn, url_suffix="with-applied-at", company="acme",
             apply_status="applied", applied_at=_iso(5), last_attempted_at=None)
    seed_job(conn, url_suffix="no-applied-at", company="acme",
             apply_status="in_progress", applied_at=None, last_attempted_at=_iso(7))
    result = get_in_flight_by_company(conn)
    assert len(result["acme"]) == 2


def test_in_flight_case_insensitive_company(tmp_db, seed_job):
    from applypilot.database import get_in_flight_by_company
    conn = tmp_db()
    seed_job(conn, url_suffix="x1", company="Netflix", apply_status="applied",
             applied_at=_iso(1))
    seed_job(conn, url_suffix="x2", company="NETFLIX", apply_status="applied",
             applied_at=_iso(1))
    result = get_in_flight_by_company(conn)
    assert len(result["netflix"]) == 2


def test_in_flight_skips_null_company(tmp_db, seed_job):
    from applypilot.database import get_in_flight_by_company
    conn = tmp_db()
    seed_job(conn, url_suffix="n1", company=None, apply_status="applied",
             applied_at=_iso(1))
    result = get_in_flight_by_company(conn)
    assert None not in result
    assert "" not in result
