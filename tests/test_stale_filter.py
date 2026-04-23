"""Tests for stale-job age filter in pipeline queries."""

from datetime import datetime, timedelta, timezone


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_pending_score_excludes_stale(tmp_db, seed_job):
    from applypilot.database import get_jobs_by_stage
    conn = tmp_db()
    seed_job(conn, url_suffix="fresh", fit_score=None, full_description="x",
             discovered_at=_iso(1))
    seed_job(conn, url_suffix="stale", fit_score=None, full_description="x",
             discovered_at=_iso(30))
    rows = get_jobs_by_stage(conn, stage="pending_score", max_age_days=14)
    urls = [r["url"] for r in rows]
    assert any("fresh" in u for u in urls)
    assert not any("stale" in u for u in urls)


def test_pending_tailor_excludes_stale(tmp_db, seed_job):
    from applypilot.database import get_jobs_by_stage
    conn = tmp_db()
    seed_job(conn, url_suffix="fresh", fit_score=9, full_description="x",
             tailored_resume_path=None, discovered_at=_iso(1))
    seed_job(conn, url_suffix="stale", fit_score=9, full_description="x",
             tailored_resume_path=None, discovered_at=_iso(30))
    rows = get_jobs_by_stage(conn, stage="pending_tailor", min_score=8, max_age_days=14)
    urls = [r["url"] for r in rows]
    assert any("fresh" in u for u in urls)
    assert not any("stale" in u for u in urls)


def test_max_age_days_zero_disables_filter(tmp_db, seed_job):
    from applypilot.database import get_jobs_by_stage
    conn = tmp_db()
    seed_job(conn, url_suffix="ancient", fit_score=9, full_description="x",
             tailored_resume_path=None, discovered_at=_iso(365))
    rows = get_jobs_by_stage(conn, stage="pending_tailor", min_score=8, max_age_days=0)
    assert any("ancient" in r["url"] for r in rows)


def test_null_discovered_at_treated_as_stale(tmp_db, seed_job):
    from applypilot.database import get_jobs_by_stage
    conn = tmp_db()
    seed_job(conn, url_suffix="null-age", fit_score=9, full_description="x",
             tailored_resume_path=None, discovered_at=None)
    rows = get_jobs_by_stage(conn, stage="pending_tailor", min_score=8, max_age_days=14)
    assert not any("null-age" in r["url"] for r in rows)
