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


def test_acquire_job_blocks_over_cap(tmp_db, seed_job, monkeypatch, tmp_path):
    """Default cap=3 blocks a 4th acquire for the same company."""
    from applypilot.apply import launcher
    from applypilot import config

    conn = tmp_db()
    for i, status in enumerate(("applied", "applied", "in_progress")):
        seed_job(conn, url_suffix=f"existing-{i}", company="acme",
                 apply_status=status,
                 applied_at=_iso(1) if status == "applied" else None,
                 last_attempted_at=_iso(0) if status == "in_progress" else None)
    seed_job(conn, url_suffix="new", company="acme",
             fit_score=9, tailored_resume_path="/tmp/r.pdf",
             apply_status=None, applied_at=None, last_attempted_at=None,
             discovered_at=_iso(1))

    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    config._company_limits_cache = None

    result = launcher.acquire_job(min_score=8, worker_id=99)
    assert result is None, "acme is over cap, nothing should be acquired"


def test_acquire_job_respects_per_company_override(tmp_db, seed_job, monkeypatch, tmp_path):
    from applypilot.apply import launcher
    from applypilot import config

    conn = tmp_db()
    for i in range(2):
        seed_job(conn, url_suffix=f"nf-{i}", company="netflix",
                 apply_status="applied", applied_at=_iso(1))
    seed_job(conn, url_suffix="nf-new", company="netflix", fit_score=10,
             tailored_resume_path="/tmp/r.pdf", apply_status=None,
             discovered_at=_iso(1))

    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    (tmp_path / "company_limits.yaml").write_text("""
overrides:
  netflix:
    max_in_flight: 1
""".strip(), encoding="utf-8")
    config._company_limits_cache = None

    assert launcher.acquire_job(min_score=8, worker_id=99) is None


def test_acquire_job_null_company_exempt(tmp_db, seed_job, monkeypatch, tmp_path):
    from applypilot.apply import launcher
    from applypilot import config

    conn = tmp_db()
    seed_job(conn, url_suffix="hn-post", company=None, fit_score=9,
             tailored_resume_path="/tmp/r.pdf", apply_status=None,
             discovered_at=_iso(1))
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    config._company_limits_cache = None

    result = launcher.acquire_job(min_score=8, worker_id=99)
    assert result is not None
    assert "hn-post" in result["url"]


def test_acquire_job_stale_filter(tmp_db, seed_job, monkeypatch, tmp_path):
    from applypilot.apply import launcher
    from applypilot import config

    conn = tmp_db()
    seed_job(conn, url_suffix="stale", company="acme", fit_score=9,
             tailored_resume_path="/tmp/r.pdf", apply_status=None,
             discovered_at=_iso(30))
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    config._company_limits_cache = None

    assert launcher.acquire_job(min_score=8, max_age_days=14, worker_id=99) is None
