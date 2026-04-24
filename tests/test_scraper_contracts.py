"""Contract tests for initial state transitions in the 4 remaining scrapers.

TDD: these tests were written before the implementation. They verify that
every scraper INSERT:
  1. Sets the `state` column explicitly (not relying on the DEFAULT).
  2. Inserts exactly one row in `job_state_transitions` with from_state=NULL.
  3. Sets `to_state` matching the actual state assigned.

Tests written to fail first, pass after implementation.
"""

import sqlite3

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _transitions_for(conn: sqlite3.Connection, url: str) -> list[dict]:
    rows = conn.execute(
        "SELECT from_state, to_state, reason, at "
        "FROM job_state_transitions WHERE job_url = ?",
        (url,),
    ).fetchall()
    return [dict(r) for r in rows]


def _state_of(conn: sqlite3.Connection, url: str) -> str | None:
    row = conn.execute("SELECT state FROM jobs WHERE url = ?", (url,)).fetchone()
    return row["state"] if row else None


# ---------------------------------------------------------------------------
# C1 — jobspy.py
# ---------------------------------------------------------------------------

def test_jobspy_insert_creates_initial_transition(tmp_db):
    """Short description => state='discovered', one transition row with from_state=NULL."""
    conn = tmp_db()
    from applypilot.discovery.jobspy import store_jobspy_results
    import pandas as pd

    short_desc = "x" * 50  # < 200 chars → discovered
    df = pd.DataFrame([{
        "job_url": "https://indeed.com/job/1",
        "title": "Engineer",
        "company": "Acme",
        "location": "Remote",
        "min_amount": None,
        "max_amount": None,
        "interval": None,
        "currency": None,
        "description": short_desc,
        "site": "indeed",
        "is_remote": True,
        "job_url_direct": None,
        "date_posted": None,
    }])

    store_jobspy_results(conn, df, "test-query")

    url = "https://indeed.com/job/1"
    assert _state_of(conn, url) == "discovered"

    transitions = _transitions_for(conn, url)
    assert len(transitions) == 1, f"Expected 1 transition, got {len(transitions)}"
    assert transitions[0]["from_state"] is None
    assert transitions[0]["to_state"] == "discovered"


def test_jobspy_long_description_lands_in_enriched(tmp_db):
    """Description > 200 chars => state='enriched', one transition row."""
    conn = tmp_db()
    from applypilot.discovery.jobspy import store_jobspy_results
    import pandas as pd

    long_desc = "x" * 300  # > 200 chars → enriched
    df = pd.DataFrame([{
        "job_url": "https://linkedin.com/jobs/2",
        "title": "Staff Engineer",
        "company": "BigCo",
        "location": "Seattle, WA",
        "min_amount": None,
        "max_amount": None,
        "interval": None,
        "currency": None,
        "description": long_desc,
        "site": "linkedin",
        "is_remote": False,
        "job_url_direct": None,
        "date_posted": None,
    }])

    store_jobspy_results(conn, df, "test-query")

    url = "https://linkedin.com/jobs/2"
    assert _state_of(conn, url) == "enriched"

    transitions = _transitions_for(conn, url)
    assert len(transitions) == 1
    assert transitions[0]["from_state"] is None
    assert transitions[0]["to_state"] == "enriched"


# ---------------------------------------------------------------------------
# C2 — hackernews.py
# ---------------------------------------------------------------------------

def test_hackernews_insert_creates_initial_transition(tmp_db):
    """HN jobs store a full description => state='enriched', one transition row."""
    conn = tmp_db()
    from applypilot.discovery.hackernews import _store_hn_job

    job = {
        "url": "https://example.com/hn-job",
        "title": "Backend Engineer",
        "company": "StartupCo",
        "location": "Remote",
        "remote": True,
        "salary": "$150K-$200K",
        "description": "We are building the future. " * 20,  # long description
        "contact": None,
    }

    result = _store_hn_job(conn, job, "Ask HN: Who is Hiring? (April 2026)")
    assert result is True  # new insertion

    url = "https://example.com/hn-job"
    assert _state_of(conn, url) == "enriched"

    transitions = _transitions_for(conn, url)
    assert len(transitions) == 1, f"Expected 1 transition, got {len(transitions)}"
    assert transitions[0]["from_state"] is None
    assert transitions[0]["to_state"] == "enriched"


# ---------------------------------------------------------------------------
# C3 — costco.py
# ---------------------------------------------------------------------------

def test_costco_insert_creates_initial_transition(tmp_db):
    """Costco jobs with full description => state='enriched', one transition row."""
    conn = tmp_db()
    from applypilot.discovery.costco import _insert_jobs

    jobs = [{
        "url": "https://careers.costco.com/job/12345",
        "title": "Software Engineer",
        "location": "Issaquah, WA",
        "description": "A great role.",
        "full_description": "We are looking for a software engineer " * 10,  # > 200 chars
        "application_url": "https://careers.costco.com/job/12345",
    }]

    new, existing = _insert_jobs(conn, jobs)
    assert new == 1
    assert existing == 0

    url = "https://careers.costco.com/job/12345"
    assert _state_of(conn, url) == "enriched"

    transitions = _transitions_for(conn, url)
    assert len(transitions) == 1, f"Expected 1 transition, got {len(transitions)}"
    assert transitions[0]["from_state"] is None
    assert transitions[0]["to_state"] == "enriched"


def test_costco_captures_posted_date_when_available(tmp_db):
    """When posted_at is provided, it is stored in the `posted_at` column."""
    conn = tmp_db()
    from applypilot.discovery.costco import _insert_jobs

    jobs = [{
        "url": "https://careers.costco.com/job/99999",
        "title": "Data Engineer",
        "location": "Issaquah, WA",
        "description": "Role summary.",
        "full_description": "Looking for an experienced data engineer " * 10,
        "application_url": "https://careers.costco.com/job/99999",
        "posted_at": "2026-04-15T00:00:00+00:00",
    }]

    new, existing = _insert_jobs(conn, jobs)
    assert new == 1

    row = conn.execute(
        "SELECT posted_at FROM jobs WHERE url = ?",
        ("https://careers.costco.com/job/99999",),
    ).fetchone()
    assert row is not None
    assert row["posted_at"] == "2026-04-15T00:00:00+00:00"


# ---------------------------------------------------------------------------
# C4 — smartextract.py
# ---------------------------------------------------------------------------

def test_smartextract_insert_creates_initial_transition(tmp_db):
    """SmartExtract jobs with description => state='enriched', one transition row."""
    conn = tmp_db()
    from applypilot.discovery.smartextract import _store_jobs_filtered

    jobs = [{
        "url": "https://company.com/jobs/senior-engineer",
        "title": "Senior Engineer",
        "location": "Remote",
        "description": "An amazing opportunity at our company. " * 10,  # > 200 chars
        "salary": None,
    }]

    new, existing = _store_jobs_filtered(
        conn, jobs, "CompanySite", "json_ld",
        accept_locs=[], reject_locs=[],
    )
    assert new == 1

    url = "https://company.com/jobs/senior-engineer"
    assert _state_of(conn, url) == "enriched"

    transitions = _transitions_for(conn, url)
    assert len(transitions) == 1, f"Expected 1 transition, got {len(transitions)}"
    assert transitions[0]["from_state"] is None
    assert transitions[0]["to_state"] == "enriched"
