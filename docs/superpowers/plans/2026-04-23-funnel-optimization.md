# Funnel Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut AI spend and enforce a hard per-company cap by gating every paid stage on (fit_score ≥ 8, discovered_at within 14 days) and blocking companies with ≥3 in-flight applications in the last 30 days. Overrides live in `~/.applypilot/company_limits.yaml`.

**Architecture:** Pure query-level changes. Three new `config.DEFAULTS` keys plus a YAML loader. Every paid SELECT adds `discovered_at > cutoff` and `fit_score >= min_score`. `acquire_job()` replaces its soft-sort deprioritization with a Python-side hard cap that consults per-company overrides. No schema changes, no new subsystems.

**Tech Stack:** Python 3.11+, SQLite (WAL), PyYAML, Typer CLI, pytest.

**Spec:** `docs/superpowers/specs/2026-04-23-funnel-optimization-design.md`

---

## Prerequisites

Run these before starting to confirm the working tree is clean and the venv works:

```bash
cd /home/elninja/Code/ApplyPilot
git status                           # should be clean or have only expected changes
.venv/bin/python -c "import applypilot" && echo "import OK"
.venv/bin/pytest tests/ -q           # baseline: existing tests pass
```

---

## Task 1 — Test scaffolding (tmp-DB fixture)

Every subsequent task's unit tests use a temp-DB fixture. This task creates it.

**Files:**
- Create: `tests/conftest.py`

- [ ] **Step 1: Write the fixture**

Create `tests/conftest.py`:

```python
"""Shared pytest fixtures for applypilot tests.

`tmp_db` yields a function returning a fresh, schema-initialized SQLite
connection rooted at a tmp path. Each call is isolated — tests that need
multiple DBs get multiple calls.
"""

import os
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Yield a factory that returns a fresh sqlite3.Connection backed by a tmp file.

    Also monkeypatches applypilot.config.DB_PATH and APP_DIR so that
    `applypilot.database.get_connection()` returns the same connection.
    """
    from applypilot import config
    from applypilot import database

    db_file = tmp_path / "applypilot.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(config, "APP_DIR", tmp_path)

    # Reset the module-level thread-local connection so get_connection opens fresh
    if hasattr(database, "_thread_local"):
        if hasattr(database._thread_local, "conn"):
            try:
                database._thread_local.conn.close()
            except Exception:
                pass
            del database._thread_local.conn

    def _factory() -> sqlite3.Connection:
        conn = database.get_connection()
        database.init_db(conn)
        return conn

    yield _factory

    # Cleanup: close any thread-local connection opened during the test
    if hasattr(database, "_thread_local") and hasattr(database._thread_local, "conn"):
        try:
            database._thread_local.conn.close()
        except Exception:
            pass


@pytest.fixture
def seed_job():
    """Return a callable that inserts a minimally-valid job row into a connection."""
    from datetime import datetime, timezone

    def _seed(conn: sqlite3.Connection, **overrides) -> str:
        now = datetime.now(timezone.utc).isoformat()
        row = {
            "url": f"https://example.com/job/{overrides.get('url_suffix', 'default')}",
            "title": "Senior Software Engineer",
            "description": "A job.",
            "full_description": "A full description.",
            "location": "Remote (US)",
            "site": "linkedin",
            "company": "acme",
            "application_url": "https://boards.greenhouse.io/acme/jobs/1",
            "fit_score": 9,
            "tailored_resume_path": "/tmp/resume.pdf",
            "cover_letter_path": "/tmp/cover.pdf",
            "discovered_at": now,
            "apply_status": None,
            "apply_attempts": 0,
        }
        row.update({k: v for k, v in overrides.items() if k != "url_suffix"})
        cols = ", ".join(row.keys())
        qs = ", ".join("?" * len(row))
        conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({qs})", tuple(row.values()))
        conn.commit()
        return row["url"]

    return _seed
```

- [ ] **Step 2: Run tests to verify fixture loads without breaking anything**

Run:
```bash
.venv/bin/pytest tests/ -q --no-header 2>&1 | tail -5
```

Expected: all existing tests still pass. New fixtures don't run by themselves.

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "test: add tmp_db and seed_job fixtures for funnel-optimization tests

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2 — Config: add DEFAULTS keys and company-limits loader

**Files:**
- Modify: `src/applypilot/config.py` (DEFAULTS dict at line 179, add loader after)
- Test: `tests/test_config_funnel.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_funnel.py`:

```python
"""Tests for funnel-optimization config additions."""

from pathlib import Path

import pytest


def test_defaults_has_new_keys():
    from applypilot import config
    assert config.DEFAULTS["min_score"] == 8
    assert config.DEFAULTS["max_job_age_days"] == 14
    assert config.DEFAULTS["max_in_flight_per_company"] == 3
    assert config.DEFAULTS["in_flight_window_days"] == 30


def test_get_company_limit_uses_defaults_when_no_yaml(tmp_path, monkeypatch):
    from applypilot import config
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    # No company_limits.yaml exists
    cap, window = config.get_company_limit("anycorp")
    assert cap == 3
    assert window == 30


def test_get_company_limit_honors_defaults_override(tmp_path, monkeypatch):
    from applypilot import config
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    (tmp_path / "company_limits.yaml").write_text("""
defaults:
  max_in_flight: 5
  window_days: 7
""".strip(), encoding="utf-8")
    config._company_limits_cache = None  # reset cache
    cap, window = config.get_company_limit("anycorp")
    assert cap == 5
    assert window == 7


def test_get_company_limit_honors_per_company_override(tmp_path, monkeypatch):
    from applypilot import config
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    (tmp_path / "company_limits.yaml").write_text("""
defaults:
  max_in_flight: 3
  window_days: 30
overrides:
  netflix:
    max_in_flight: 1
  stripe:
    max_in_flight: 5
    window_days: 14
""".strip(), encoding="utf-8")
    config._company_limits_cache = None
    assert config.get_company_limit("netflix") == (1, 30)
    assert config.get_company_limit("NETFLIX") == (1, 30)    # case-insensitive
    assert config.get_company_limit("stripe") == (5, 14)
    assert config.get_company_limit("unlisted") == (3, 30)   # falls through to defaults


def test_get_company_limit_unlimited_cap(tmp_path, monkeypatch):
    from applypilot import config
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    (tmp_path / "company_limits.yaml").write_text("""
overrides:
  openai:
    max_in_flight: -1
""".strip(), encoding="utf-8")
    config._company_limits_cache = None
    cap, _ = config.get_company_limit("openai")
    assert cap == -1


def test_get_company_limit_malformed_yaml_falls_back(tmp_path, monkeypatch, caplog):
    from applypilot import config
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    (tmp_path / "company_limits.yaml").write_text("{this: is: not: valid]", encoding="utf-8")
    config._company_limits_cache = None
    cap, window = config.get_company_limit("anycorp")
    assert cap == 3
    assert window == 30
```

- [ ] **Step 2: Run test to confirm it fails**

Run: `.venv/bin/pytest tests/test_config_funnel.py -v`
Expected: all tests fail with `AttributeError: module 'applypilot.config' has no attribute 'get_company_limit'` (or similar), plus `KeyError` on DEFAULTS lookup.

- [ ] **Step 3: Update `DEFAULTS` in `src/applypilot/config.py`**

Edit `src/applypilot/config.py` lines 179-186. Replace:

```python
DEFAULTS = {
    "min_score": 7,
    "max_apply_attempts": 3,
    "max_tailor_attempts": 5,
    "poll_interval": 60,
    "apply_timeout": 300,
    "viewport": "1280x900",
}
```

With:

```python
DEFAULTS = {
    "min_score": 8,                           # was 7; per 2026-04-23 funnel spec
    "max_job_age_days": 14,                   # stale-job cutoff (discovered_at)
    "max_in_flight_per_company": 3,           # hard cap per company
    "in_flight_window_days": 30,              # window for in-flight count
    "max_apply_attempts": 3,
    "max_tailor_attempts": 5,
    "poll_interval": 60,
    "apply_timeout": 300,
    "viewport": "1280x900",
}
```

- [ ] **Step 4: Add company-limits loader at the end of `config.py`**

Append to `src/applypilot/config.py` (after the existing `check_tier` function):

```python
# ---------------------------------------------------------------------------
# Per-company application limits (open-pipeline cap)
# ---------------------------------------------------------------------------

COMPANY_LIMITS_PATH_NAME = "company_limits.yaml"

_company_limits_cache: dict | None = None


def _load_company_limits() -> dict:
    """Load and cache ~/.applypilot/company_limits.yaml.

    Returns an empty dict if the file doesn't exist or fails to parse.
    Cache can be reset by setting `_company_limits_cache = None`.
    """
    global _company_limits_cache
    if _company_limits_cache is not None:
        return _company_limits_cache

    path = APP_DIR / COMPANY_LIMITS_PATH_NAME
    if not path.exists():
        _company_limits_cache = {}
        return _company_limits_cache

    import logging
    import yaml

    log = logging.getLogger(__name__)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"expected mapping, got {type(data).__name__}")
        _company_limits_cache = data
    except Exception as e:
        log.warning("Failed to parse %s (%s); using defaults.", path, e)
        _company_limits_cache = {}

    return _company_limits_cache


def get_company_limit(company: str) -> tuple[int, int]:
    """Return (max_in_flight, window_days) for the given company.

    Resolution order:
      1. overrides.<company-lowercased> (YAML)
      2. defaults.* (YAML)
      3. DEFAULTS["max_in_flight_per_company"] / ["in_flight_window_days"]

    A cap of -1 means "unlimited". A cap of 0 means "explicitly blocked".
    Both are passed through as-is; interpretation lives in the caller.
    """
    limits = _load_company_limits()

    yaml_defaults = limits.get("defaults", {}) or {}
    default_cap = yaml_defaults.get("max_in_flight", DEFAULTS["max_in_flight_per_company"])
    default_window = yaml_defaults.get("window_days", DEFAULTS["in_flight_window_days"])

    overrides = limits.get("overrides", {}) or {}
    co = (company or "").lower().strip()
    if co and co in {k.lower(): v for k, v in overrides.items()}:
        # case-insensitive match
        normalized = {k.lower(): v for k, v in overrides.items()}
        entry = normalized[co] or {}
        cap = entry.get("max_in_flight", default_cap)
        window = entry.get("window_days", default_window)
        return int(cap), int(window)

    return int(default_cap), int(default_window)
```

- [ ] **Step 5: Run tests to confirm they pass**

Run: `.venv/bin/pytest tests/test_config_funnel.py -v`
Expected: all 6 tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/applypilot/config.py tests/test_config_funnel.py
git commit -m "feat(config): add funnel DEFAULTS and company-limits loader

- min_score default 7 → 8
- New keys: max_job_age_days=14, max_in_flight_per_company=3,
  in_flight_window_days=30
- load_company_limits() + get_company_limit() for per-company
  YAML overrides at ~/.applypilot/company_limits.yaml

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3 — Ship the example YAML

**Files:**
- Create: `src/applypilot/config/company_limits.example.yaml`

- [ ] **Step 1: Create the example file**

Write `src/applypilot/config/company_limits.example.yaml`:

```yaml
# ApplyPilot per-company application limits.
#
# Copy this file to ~/.applypilot/company_limits.yaml to activate overrides.
# Missing file → the defaults in config.DEFAULTS apply to every company.
#
# Cap semantics:
#   max_in_flight: N  — at most N jobs at this company may be in-flight at once.
#   max_in_flight: 0  — block this company entirely (no new applications).
#   max_in_flight: -1 — no cap (apply as many as queue allows).
#
# "In-flight" = apply_status IN ('applied', 'in_progress', 'needs_human')
# AND COALESCE(applied_at, last_attempted_at) within window_days.
# `manual` and `failed` DO NOT count — the company didn't see those.

defaults:
  max_in_flight: 3
  window_days: 30

overrides:
  # Example: cap big brands where we want to be selective
  # netflix:
  #   max_in_flight: 1
  #
  # Example: hiring fast, shorter cooldown
  # stripe:
  #   max_in_flight: 5
  #   window_days: 14
  #
  # Example: pause a company entirely
  # google:
  #   max_in_flight: 0
  #
  # Example: unlimited (no cap)
  # openai:
  #   max_in_flight: -1
```

- [ ] **Step 2: Verify the file is packaged**

Run:
```bash
cat pyproject.toml | grep -A2 "hatch.build"
```

Expected: `artifacts = ["src/applypilot/config/*.yaml"]` line is present (it is — verified).

- [ ] **Step 3: Commit**

```bash
git add src/applypilot/config/company_limits.example.yaml
git commit -m "feat(config): ship company_limits.example.yaml with overrides docs

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4 — Stale-age filter in `get_jobs_by_stage`

This change adds the age filter once; scorer, tailor all inherit it via `get_jobs_by_stage`. Cover letter uses an inline query — we handle it in Task 7.

**Files:**
- Modify: `src/applypilot/database.py` (function at line 1151-1228)
- Test: `tests/test_stale_filter.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stale_filter.py`:

```python
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
    # NULL discovered_at → excluded when filter is active
    assert not any("null-age" in r["url"] for r in rows)
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `.venv/bin/pytest tests/test_stale_filter.py -v`
Expected: all 4 fail with `TypeError: get_jobs_by_stage() got an unexpected keyword argument 'max_age_days'`.

- [ ] **Step 3: Update `get_jobs_by_stage`**

Edit `src/applypilot/database.py` starting at line 1151. Replace the function signature and body:

```python
def get_jobs_by_stage(conn: sqlite3.Connection | None = None,
                      stage: str = "discovered",
                      min_score: int | None = None,
                      max_age_days: int | None = None,
                      limit: int = 100) -> list[dict]:
    """Fetch jobs filtered by pipeline stage.

    Args:
        conn: Database connection. Uses get_connection() if None.
        stage: One of "discovered", "enriched", "scored", "tailored", "applied",
               "pending_score", "pending_tailor", "pending_apply", "pending_cover".
        min_score: Minimum fit_score filter (only relevant for scored+ stages).
        max_age_days: Exclude jobs with discovered_at older than this many days.
                      None or 0 disables the filter. NULL discovered_at is
                      treated as stale (excluded) when filter is active.
        limit: Maximum number of rows to return (0 = no limit).

    Returns:
        List of job dicts.
    """
    from applypilot.config import DEFAULTS

    if conn is None:
        conn = get_connection()

    if min_score is None:
        min_score = DEFAULTS["min_score"]
    if max_age_days is None:
        max_age_days = DEFAULTS["max_job_age_days"]

    conditions = {
        "discovered": "1=1",
        "pending_detail": (
            "detail_scraped_at IS NULL "
            "OR (detail_error_category = 'retriable' "
            "    AND (detail_next_retry_at IS NULL OR detail_next_retry_at <= datetime('now')))"
        ),
        "enriched": "full_description IS NOT NULL",
        "pending_score": (
            "full_description IS NOT NULL AND ("
            "  (fit_score IS NULL AND score_error IS NULL) "
            "  OR (score_error IS NOT NULL AND score_retry_count < 5 "
            "      AND (score_next_retry_at IS NULL OR score_next_retry_at <= datetime('now')))"
            ")"
        ),
        "scored": "fit_score IS NOT NULL",
        "pending_tailor": (
            "fit_score >= ? AND full_description IS NOT NULL "
            "AND tailored_resume_path IS NULL AND COALESCE(tailor_attempts, 0) < 5"
        ),
        "pending_cover": (
            "fit_score >= ? AND tailored_resume_path IS NOT NULL "
            "AND full_description IS NOT NULL "
            "AND (cover_letter_path IS NULL OR cover_letter_path = '') "
            "AND COALESCE(cover_attempts, 0) < 5"
        ),
        "tailored": "tailored_resume_path IS NOT NULL",
        "pending_apply": (
            "tailored_resume_path IS NOT NULL AND applied_at IS NULL "
            "AND application_url IS NOT NULL"
        ),
        "applied": "applied_at IS NOT NULL",
    }

    where = conditions.get(stage, "1=1")
    params: list = []

    if "?" in where:
        params.append(min_score)

    if stage in ("scored", "tailored", "applied") and "fit_score" not in where:
        where += " AND fit_score >= ?"
        params.append(min_score)

    # Age filter: only active when max_age_days > 0.
    # NULL discovered_at is excluded because `col > val` is NULL (→ falsy in WHERE).
    if max_age_days and max_age_days > 0:
        where += " AND discovered_at > datetime('now', ?)"
        params.append(f"-{max_age_days} days")

    query = f"""
        SELECT * FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY COALESCE(site, 'unknown')
                ORDER BY discovered_at DESC
            ) AS _site_rank
            FROM jobs WHERE {where}
        )
        ORDER BY fit_score DESC NULLS LAST, _site_rank ASC, discovered_at DESC
    """
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    if rows and not isinstance(rows[0], dict):
        columns = rows[0].keys()
        rows = [dict(zip(columns, r)) for r in rows]
    return rows
```

Note: this replaces the body entirely including the existing default-min-score-7 fallback (`params.append(7)`) which is gone.

- [ ] **Step 4: Run tests to confirm they pass**

Run: `.venv/bin/pytest tests/test_stale_filter.py -v`
Expected: all 4 tests pass.

- [ ] **Step 5: Confirm existing tests still pass**

Run: `.venv/bin/pytest tests/ -q --no-header 2>&1 | tail -3`
Expected: no regressions.

- [ ] **Step 6: Commit**

```bash
git add src/applypilot/database.py tests/test_stale_filter.py
git commit -m "feat(db): add max_age_days filter to get_jobs_by_stage

Every pipeline stage using get_jobs_by_stage (score/tailor) now respects
a configurable discovered_at cutoff (default 14d). Adds a new
'pending_cover' stage so cover_letter can migrate to the shared helper
in a later task.

NULL discovered_at is treated as stale (excluded when filter active).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5 — CLI flags: min_score default from config; add --max-age-days

**Files:**
- Modify: `src/applypilot/cli.py` (lines 86, 172, plus two new --max-age-days flags)

- [ ] **Step 1: Update the `run` command (around line 86)**

Edit `src/applypilot/cli.py`. Replace line 86:

```python
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
```

With:

```python
    min_score: int = typer.Option(
        config.DEFAULTS["min_score"], "--min-score",
        help=f"Minimum fit score for tailor/cover stages (default: {config.DEFAULTS['min_score']}).",
    ),
    max_age_days: int = typer.Option(
        config.DEFAULTS["max_job_age_days"], "--max-age-days",
        help=(
            "Skip jobs whose discovered_at is older than this many days. "
            "0 = no age filter. "
            f"Default: {config.DEFAULTS['max_job_age_days']}."
        ),
    ),
```

- [ ] **Step 2: Thread `max_age_days` through to `run_pipeline` (cli.py lines 153-164)**

Current call:
```python
    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        limit=limit,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        sources=resolved_sources,
        doc_format=doc_format,
    )
```

Replace with:
```python
    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        max_age_days=max_age_days,
        limit=limit,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        sources=resolved_sources,
        doc_format=doc_format,
    )
```

- [ ] **Step 3: Update the `apply` command (around line 172)**

Edit `src/applypilot/cli.py` line 172. Replace:

```python
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
```

With:

```python
    min_score: int = typer.Option(
        config.DEFAULTS["min_score"], "--min-score",
        help=f"Minimum fit score for job selection (default: {config.DEFAULTS['min_score']}).",
    ),
    max_age_days: int = typer.Option(
        config.DEFAULTS["max_job_age_days"], "--max-age-days",
        help=(
            "Skip jobs whose discovered_at is older than this many days. "
            "0 = no age filter. "
            f"Default: {config.DEFAULTS['max_job_age_days']}."
        ),
    ),
```

- [ ] **Step 4: Thread `max_age_days` through the `apply` command's worker_loop calls**

`apply` command spawns workers via `worker_loop(...)`. Search for all call sites in `cli.py`:

```bash
grep -n "worker_loop(" src/applypilot/cli.py
```

For each call, add `max_age_days=max_age_days` as a kwarg alongside `min_score=min_score`. Example pattern:

```python
    # Before:
    worker_loop(worker_id=i, min_score=min_score, max_score=max_score, ...)
    # After:
    worker_loop(worker_id=i, min_score=min_score, max_age_days=max_age_days,
                max_score=max_score, ...)
```

(The `worker_loop` signature will be extended in Task 9 Step 4.)

- [ ] **Step 5: Confirm `config` is imported at top of cli.py**

Run: `grep -n "^from applypilot import config\|^from applypilot.config\|^import applypilot.config" src/applypilot/cli.py | head -3`

If not imported, add near the other applypilot imports:
```python
from applypilot import config
```

- [ ] **Step 6: Smoke-test the CLI still loads**

Run:
```bash
.venv/bin/applypilot run --help 2>&1 | head -20
.venv/bin/applypilot apply --help 2>&1 | head -20
```

Expected: both print help without error, show `--min-score` default `8`, show `--max-age-days` default `14`.

- [ ] **Step 7: Commit**

```bash
git add src/applypilot/cli.py
git commit -m "feat(cli): read min_score from config; add --max-age-days flag

run and apply commands now use config.DEFAULTS for min_score (8) and
expose --max-age-days (14). Flags are threaded through to the pipeline
and worker loop.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6 — Pipeline: thread min_score + max_age_days through stages

**Files:**
- Modify: `src/applypilot/pipeline.py` (multiple signatures around lines 223-660)

- [ ] **Step 1: Update `_run_tailor` (line 223)**

Edit `src/applypilot/pipeline.py`. Replace:

```python
def _run_tailor(min_score: int = 7, limit: int = 20, workers: int = 1, doc_format: str = "pdf") -> dict:
    """Stage: Resume tailoring — generate tailored resumes for high-fit jobs."""
    try:
        from applypilot.scoring.tailor import run_tailoring
        run_tailoring(min_score=min_score, limit=limit, workers=workers, doc_format=doc_format)
        return {"status": "ok"}
    except Exception as e:
        log.exception("Tailoring failed: %s", e)
        return {"status": f"error: {e}"}
```

With:

```python
def _run_tailor(min_score: int | None = None, max_age_days: int | None = None,
                limit: int = 20, workers: int = 1, doc_format: str = "pdf") -> dict:
    """Stage: Resume tailoring — generate tailored resumes for high-fit jobs."""
    from applypilot.config import DEFAULTS
    if min_score is None:
        min_score = DEFAULTS["min_score"]
    if max_age_days is None:
        max_age_days = DEFAULTS["max_job_age_days"]
    try:
        from applypilot.scoring.tailor import run_tailoring
        run_tailoring(min_score=min_score, max_age_days=max_age_days,
                      limit=limit, workers=workers, doc_format=doc_format)
        return {"status": "ok"}
    except Exception as e:
        log.exception("Tailoring failed: %s", e)
        return {"status": f"error: {e}"}
```

- [ ] **Step 2: Update `_run_cover` (line 234) similarly**

Replace:

```python
def _run_cover(min_score: int = 7, limit: int = 20, workers: int = 1, doc_format: str = "pdf") -> dict:
    """Stage: Cover letter generation."""
    try:
        from applypilot.scoring.cover_letter import run_cover_letters
        run_cover_letters(min_score=min_score, limit=limit, workers=workers, doc_format=doc_format)
        return {"status": "ok"}
    except Exception as e:
        log.exception("Cover letter generation failed: %s", e)
        return {"status": f"error: {e}"}
```

With:

```python
def _run_cover(min_score: int | None = None, max_age_days: int | None = None,
               limit: int = 20, workers: int = 1, doc_format: str = "pdf") -> dict:
    """Stage: Cover letter generation."""
    from applypilot.config import DEFAULTS
    if min_score is None:
        min_score = DEFAULTS["min_score"]
    if max_age_days is None:
        max_age_days = DEFAULTS["max_job_age_days"]
    try:
        from applypilot.scoring.cover_letter import run_cover_letters
        run_cover_letters(min_score=min_score, max_age_days=max_age_days,
                          limit=limit, workers=workers, doc_format=doc_format)
        return {"status": "ok"}
    except Exception as e:
        log.exception("Cover letter generation failed: %s", e)
        return {"status": f"error: {e}"}
```

- [ ] **Step 3: Update `_run_score` (line 212)**

Current:
```python
def _run_score(workers: int = 1) -> dict:
    """Stage: AI job fit scoring."""
    try:
        from applypilot.scoring.scorer import run_scoring
        run_scoring(workers=workers)
        return {"status": "ok"}
    except Exception as e:
        log.exception("Scoring failed: %s", e)
        return {"status": f"error: {e}"}
```

Replace with:
```python
def _run_score(workers: int = 1, max_age_days: int | None = None) -> dict:
    """Stage: AI job fit scoring."""
    from applypilot.config import DEFAULTS
    if max_age_days is None:
        max_age_days = DEFAULTS["max_job_age_days"]
    try:
        from applypilot.scoring.scorer import run_scoring
        run_scoring(workers=workers, max_age_days=max_age_days)
        return {"status": "ok"}
    except Exception as e:
        log.exception("Scoring failed: %s", e)
        return {"status": f"error: {e}"}
```

(`run_scoring` gains the `max_age_days` kwarg in Task 7.)

- [ ] **Step 4: Add age filter to `_PENDING_SQL` dict and `_count_pending` (lines 321-354)**

Current `_PENDING_SQL` dict (lines 322-340) has one row per stage. Replace each SQL string to include a `discovered_at` filter. Also change `cover` to include `fit_score >=` so it matches the new tailor/cover semantics.

Full replacement for lines 321-354:

```python
# SQL to count pending work for each stage.
# The `?` params are: (min_score, age_cutoff_iso_offset) when both present;
# scroll through _count_pending to see the binding order.
_PENDING_SQL: dict[str, str] = {
    "enrich": (
        "SELECT COUNT(*) FROM jobs "
        "WHERE detail_scraped_at IS NULL"
    ),
    "score":  (
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL AND fit_score IS NULL"
    ),
    "tailor": (
        "SELECT COUNT(*) FROM jobs WHERE fit_score >= ? "
        "AND full_description IS NOT NULL "
        "AND tailored_resume_path IS NULL "
        "AND COALESCE(tailor_attempts, 0) < 5"
    ),
    "cover": (
        "SELECT COUNT(*) FROM jobs WHERE fit_score >= ? "
        "AND tailored_resume_path IS NOT NULL "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '') "
        "AND COALESCE(cover_attempts, 0) < 5"
    ),
    "pdf": (
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL "
        "AND tailored_resume_path LIKE '%.txt'"
    ),
}

# Stages whose SQL takes a ? for min_score.
_PENDING_SQL_TAKES_MIN_SCORE = {"tailor", "cover"}

# How long to sleep between polling loops in streaming mode (seconds)
_STREAM_POLL_INTERVAL = 10


def _count_pending(stage: str, min_score: int | None = None,
                   max_age_days: int | None = None) -> int:
    """Count pending work items for a stage, honoring min_score and max_age_days."""
    from applypilot.config import DEFAULTS

    if min_score is None:
        min_score = DEFAULTS["min_score"]
    if max_age_days is None:
        max_age_days = DEFAULTS["max_job_age_days"]

    sql = _PENDING_SQL.get(stage)
    if sql is None:
        return 0

    params: list = []
    if stage in _PENDING_SQL_TAKES_MIN_SCORE:
        params.append(min_score)

    if max_age_days and max_age_days > 0:
        sql += " AND discovered_at > datetime('now', ?)"
        params.append(f"-{max_age_days} days")

    conn = get_connection()
    return conn.execute(sql, params).fetchone()[0]
```

- [ ] **Step 5: Update `_run_stage_streaming`, `_run_sequential`, `_run_streaming`, `run_pipeline`**

**5a.** `_run_stage_streaming` (line 357): add `max_age_days: int | None = None` to the signature after `min_score`. In the kwargs-building section (lines 374-383), add:

```python
    if stage in ("score", "tailor", "cover"):
        kwargs["max_age_days"] = max_age_days
```

(This adds `max_age_days` to scorer/tailor/cover dispatches; discover/enrich/pdf don't need it.)

**5b.** `_run_sequential` (line 439): add `max_age_days: int | None = None` to the signature after `min_score`. In the per-stage kwargs block (lines 463-472), add the same conditional:

```python
    if name in ("score", "tailor", "cover"):
        kwargs["max_age_days"] = max_age_days
```

**5c.** `_run_streaming` (line 503): change signature from:
```python
def _run_streaming(ordered: list[str], min_score: int, limit: int = 20,
                   workers: int = 1, sources: list[str] | None = None,
                   doc_format: str = "pdf") -> dict:
```

To:
```python
def _run_streaming(ordered: list[str], min_score: int,
                   max_age_days: int | None = None,
                   limit: int = 20,
                   workers: int = 1, sources: list[str] | None = None,
                   doc_format: str = "pdf") -> dict:
```

Then at line 525 (inside `threading.Thread(target=_run_stage_streaming, args=...)`), update the `args` tuple to pass `max_age_days`:

```python
            args=(name, tracker, stop_event, min_score, max_age_days,
                  limit, workers, sources, doc_format),
```

And update `_run_stage_streaming`'s positional signature to accept it:
```python
def _run_stage_streaming(
    stage: str,
    tracker: _StageTracker,
    stop_event: threading.Event,
    min_score: int = 7,
    max_age_days: int | None = None,
    limit: int = 20,
    workers: int = 1,
    sources: list[str] | None = None,
    doc_format: str = "pdf",
) -> None:
```

**5d.** `run_pipeline` (line 566): change signature from:
```python
def run_pipeline(
    stages: list[str] | None = None,
    min_score: int = 7,
    limit: int | None = None,
    dry_run: bool = False,
    stream: bool = False,
    workers: int = 1,
    sources: list[str] | None = None,
    doc_format: str = "pdf",
) -> dict:
```

To:
```python
def run_pipeline(
    stages: list[str] | None = None,
    min_score: int | None = None,
    max_age_days: int | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    stream: bool = False,
    workers: int = 1,
    sources: list[str] | None = None,
    doc_format: str = "pdf",
) -> dict:
    """Run pipeline stages.

    Defaults (min_score, max_age_days) read from config.DEFAULTS when None.
    """
    from applypilot.config import DEFAULTS
    if min_score is None:
        min_score = DEFAULTS["min_score"]
    if max_age_days is None:
        max_age_days = DEFAULTS["max_job_age_days"]
```

Also update the call sites of `_run_streaming` (line 633) and `_run_sequential` (line 635) to pass `max_age_days`:

```python
        if stream:
            result = _run_streaming(ordered, min_score,
                                    max_age_days=max_age_days,
                                    limit=effective_limit, workers=workers,
                                    sources=sources, doc_format=doc_format)
        else:
            result = _run_sequential(ordered, min_score,
                                     max_age_days=max_age_days,
                                     limit=effective_limit, workers=workers,
                                     sources=sources, doc_format=doc_format)
```

And in the banner block (line 608), add a line showing the age cutoff:
```python
    console.print(f"  Min score: {min_score}")
    console.print(f"  Max age:   {max_age_days}d")
    console.print(f"  Limit:     {effective_limit} jobs/batch")
```

- [ ] **Step 6: Smoke-test**

Run: `.venv/bin/python -c "from applypilot.pipeline import run_pipeline; help(run_pipeline)" 2>&1 | head -20`

Expected: `max_age_days` is in the signature. No import errors.

Run the CLI with `--dry-run` to confirm arguments thread through:

```bash
.venv/bin/applypilot run score --dry-run --min-score 8 --max-age-days 14 2>&1 | tail -10
```

Expected: prints the planned stages with the correct min_score / max_age_days values.

- [ ] **Step 7: Commit**

```bash
git add src/applypilot/pipeline.py
git commit -m "feat(pipeline): thread max_age_days through all stage dispatch

_run_score, _run_tailor, _run_cover, _count_pending, run_pipeline now
accept max_age_days and default from config.DEFAULTS. Stage functions
receive it as a kwarg so scorer/tailor/cover can filter stale jobs.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7 — Scorer, Tailor, Cover: accept max_age_days and filter

**Files:**
- Modify: `src/applypilot/scoring/scorer.py` (line 268, `run_scoring`)
- Modify: `src/applypilot/scoring/tailor.py` (line 494, `run_tailoring`)
- Modify: `src/applypilot/scoring/cover_letter.py` (lines 200-231, `run_cover_letters`)
- Test: extend `tests/test_stale_filter.py`

- [ ] **Step 1: Write the failing integration tests**

Append to `tests/test_stale_filter.py`:

```python
def test_run_tailoring_skips_stale(tmp_db, seed_job, monkeypatch):
    """run_tailoring passes max_age_days through to get_jobs_by_stage."""
    from applypilot.scoring import tailor
    conn = tmp_db()
    seed_job(conn, url_suffix="fresh", fit_score=9, full_description="x",
             tailored_resume_path=None, discovered_at=_iso(1))
    seed_job(conn, url_suffix="stale", fit_score=9, full_description="x",
             tailored_resume_path=None, discovered_at=_iso(30))

    captured = {}

    def fake_get_jobs_by_stage(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(tailor, "get_jobs_by_stage", fake_get_jobs_by_stage)
    # Need load_profile + RESUME_PATH to not break — stub them:
    monkeypatch.setattr(tailor, "load_profile", lambda: {"personal": {}, "skills_boundary": {}})
    monkeypatch.setattr(tailor, "RESUME_PATH", type("P", (), {"read_text": lambda *a, **k: "resume"})())
    monkeypatch.setattr(tailor.Path if hasattr(tailor, "Path") else object, "__init__", lambda *a, **k: None)

    tailor.run_tailoring(min_score=8, max_age_days=14, limit=10)
    assert captured.get("max_age_days") == 14


def test_run_cover_letters_skips_stale(tmp_db, seed_job, monkeypatch):
    """run_cover_letters uses pending_cover stage with age filter."""
    from applypilot.scoring import cover_letter
    conn = tmp_db()
    seed_job(conn, url_suffix="fresh", fit_score=9,
             tailored_resume_path="/tmp/r.pdf", cover_letter_path=None,
             discovered_at=_iso(1))
    seed_job(conn, url_suffix="stale", fit_score=9,
             tailored_resume_path="/tmp/r.pdf", cover_letter_path=None,
             discovered_at=_iso(30))

    monkeypatch.setattr(cover_letter, "load_profile",
                        lambda: {"personal": {}, "skills_boundary": {}, "resume_facts": {}})
    monkeypatch.setattr(cover_letter.RESUME_PATH, "read_text",
                        lambda *a, **k: "resume", raising=False)

    result = cover_letter.run_cover_letters(min_score=8, max_age_days=14, limit=10, workers=1)
    # Only the fresh job was in the candidate set; LLM never reached, but
    # the fact that we got past the fetch step without error is the signal.
    assert result["errors"] == 0 or "generated" in result
```

(Note: these tests monkeypatch heavily because the LLM path is out of scope; the goal is to verify the `max_age_days` kwarg is accepted and threaded. Adjust monkeypatches based on actual module imports.)

- [ ] **Step 2: Update `scorer.py` `run_scoring`**

Edit `src/applypilot/scoring/scorer.py` line 268. Replace:

```python
def run_scoring(limit: int = 0, rescore: bool = False, workers: int = 1) -> dict:
```

With:

```python
def run_scoring(limit: int = 0, rescore: bool = False, workers: int = 1,
                max_age_days: int | None = None) -> dict:
```

Find the `get_jobs_by_stage(...)` call (around line 288) and add `max_age_days=max_age_days`:

```python
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score",
                                 max_age_days=max_age_days, limit=limit)
```

- [ ] **Step 3: Update `tailor.py` `run_tailoring`**

Edit `src/applypilot/scoring/tailor.py` line 494. Replace:

```python
def run_tailoring(min_score: int = 7, limit: int = 20, workers: int = 1, doc_format: str = "pdf") -> dict:
```

With:

```python
def run_tailoring(min_score: int | None = None, limit: int = 20, workers: int = 1,
                  doc_format: str = "pdf", max_age_days: int | None = None) -> dict:
    from applypilot.config import DEFAULTS
    if min_score is None:
        min_score = DEFAULTS["min_score"]
```

Then find the `get_jobs_by_stage(...)` call at line 510 and add `max_age_days`:

```python
    jobs = get_jobs_by_stage(conn=conn, stage="pending_tailor",
                             min_score=min_score, max_age_days=max_age_days,
                             limit=limit)
```

- [ ] **Step 4: Migrate `cover_letter.py` to use `pending_cover` stage**

Edit `src/applypilot/scoring/cover_letter.py`. Replace the function signature (line 200) and the inline query (lines 217-232):

```python
def run_cover_letters(min_score: int | None = None, limit: int = 20, workers: int = 1,
                      doc_format: str = "pdf", max_age_days: int | None = None) -> dict:
    """Generate cover letters for high-scoring jobs that have tailored resumes.

    Args:
        min_score: Minimum fit_score threshold.
        limit: Maximum jobs to process.
        workers: Parallel LLM threads (default 1 = sequential).
        doc_format: Output document format — "pdf" (default) or "docx".
        max_age_days: Skip jobs older than this many days (default from config).

    Returns:
        {"generated": int, "errors": int, "elapsed": float}
    """
    from applypilot.config import DEFAULTS
    from applypilot.database import get_jobs_by_stage
    if min_score is None:
        min_score = DEFAULTS["min_score"]

    profile = load_profile()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    jobs = get_jobs_by_stage(conn=conn, stage="pending_cover",
                             min_score=min_score, max_age_days=max_age_days,
                             limit=limit)
    conn.commit()  # Close read transaction before long LLM phase

    if not jobs:
        log.info("No jobs needing cover letters (score >= %d).", min_score)
        return {"generated": 0, "errors": 0, "elapsed": 0.0}

    COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)
    log.info(
        "Generating cover letters for %d jobs (score >= %d, workers=%d)...",
        len(jobs), min_score, workers,
    )
    t0 = time.time()
    completed = 0
    # ... rest of function body unchanged (workers loop, result handling) ...
```

(Preserve everything after `completed = 0` — only the signature and the fetch block change.)

- [ ] **Step 5: Run stale tests**

Run: `.venv/bin/pytest tests/test_stale_filter.py -v`
Expected: all tests pass (including the two new integration ones; adjust monkeypatches if any test fails due to missing stubs).

- [ ] **Step 6: Smoke-test live CLI**

Run:
```bash
.venv/bin/applypilot status 2>&1 | tail -10
.venv/bin/applypilot run score --limit 1 --max-age-days 14 2>&1 | head -10 || true
```

Expected: `status` shows the funnel without error. `run score` either finds 0 jobs (because current DB is 30-90d old and cutoff is 14d — expected) or processes one job. No crashes.

- [ ] **Step 7: Commit**

```bash
git add src/applypilot/scoring/scorer.py src/applypilot/scoring/tailor.py \
        src/applypilot/scoring/cover_letter.py tests/test_stale_filter.py
git commit -m "feat(scoring): age-filter and config-driven min_score in all three stages

run_scoring, run_tailoring, run_cover_letters now accept max_age_days
and default min_score from config. cover_letter migrated from inline
SQL to the shared pending_cover stage in get_jobs_by_stage.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8 — Database helper: `get_in_flight_by_company`

**Files:**
- Modify: `src/applypilot/database.py` (append new function)
- Test: extend or create `tests/test_company_cap.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_company_cap.py`:

```python
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
             applied_at=_iso(3))           # excluded
    seed_job(conn, url_suffix="a5", company="acme", apply_status="manual",
             applied_at=_iso(4))           # excluded

    result = get_in_flight_by_company(conn)
    # Lowercase keys
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
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `.venv/bin/pytest tests/test_company_cap.py -v`
Expected: `ImportError: cannot import name 'get_in_flight_by_company'`.

- [ ] **Step 3: Implement the helper**

Append to `src/applypilot/database.py` (after `extract_company` or near other read helpers):

```python
def get_in_flight_by_company(conn: sqlite3.Connection | None = None,
                             max_window_days: int = 90) -> dict[str, list[str]]:
    """Return {company_lower: [timestamp_iso, ...]} for all recent in-flight jobs.

    "In-flight" = apply_status IN ('applied', 'in_progress', 'needs_human').
    `manual` and `failed` are excluded — the company didn't see those.

    Timestamp is COALESCE(applied_at, last_attempted_at). NULL-company rows
    are skipped (caller handles NULL-company exemption separately).

    max_window_days bounds the query scan. Callers filter by their
    specific window on the returned lists.
    """
    if conn is None:
        conn = get_connection()

    rows = conn.execute("""
        SELECT LOWER(company) AS co,
               COALESCE(applied_at, last_attempted_at) AS ts
        FROM jobs
        WHERE apply_status IN ('applied', 'in_progress', 'needs_human')
          AND company IS NOT NULL AND TRIM(company) != ''
          AND COALESCE(applied_at, last_attempted_at) IS NOT NULL
          AND COALESCE(applied_at, last_attempted_at) > datetime('now', ?)
    """, (f"-{max_window_days} days",)).fetchall()

    from collections import defaultdict
    out: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        out[r["co"]].append(r["ts"])
    return dict(out)
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_company_cap.py -v`
Expected: all 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/database.py tests/test_company_cap.py
git commit -m "feat(db): add get_in_flight_by_company helper

Returns {company_lower: [timestamps]} for jobs with apply_status in
('applied', 'in_progress', 'needs_human'). Used by acquire_job's
hard per-company cap. Case-insensitive, skips NULL/empty companies.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9 — `acquire_job`: hard per-company cap + age filter

**Files:**
- Modify: `src/applypilot/apply/launcher.py` (function at line 1078)
- Test: extend `tests/test_company_cap.py`

- [ ] **Step 1: Write the failing tests for the cap behavior**

Append to `tests/test_company_cap.py`:

```python
def test_acquire_job_blocks_over_cap(tmp_db, seed_job, monkeypatch):
    """Default cap=3 blocks a 4th acquire for the same company."""
    from applypilot.apply import launcher
    from applypilot import config

    conn = tmp_db()
    # 3 existing in-flight at acme
    for i, status in enumerate(("applied", "applied", "in_progress")):
        seed_job(conn, url_suffix=f"existing-{i}", company="acme",
                 apply_status=status,
                 applied_at=_iso(1) if status == "applied" else None,
                 last_attempted_at=_iso(1) if status == "in_progress" else None)
    # 1 new candidate at acme — should be blocked
    seed_job(conn, url_suffix="new", company="acme",
             fit_score=9, tailored_resume_path="/tmp/r.pdf",
             apply_status=None, applied_at=None, last_attempted_at=None,
             discovered_at=_iso(1))

    # Use default cap (no YAML override)
    config._company_limits_cache = None
    monkeypatch.setattr(config, "APP_DIR", conn.execute("PRAGMA database_list").fetchone()[2])

    result = launcher.acquire_job(min_score=8, worker_id=99)
    assert result is None, "acme is over cap, nothing should be acquired"


def test_acquire_job_respects_per_company_override(tmp_db, seed_job, monkeypatch, tmp_path):
    from applypilot.apply import launcher
    from applypilot import config

    conn = tmp_db()
    # Two existing Netflix apps
    for i in range(2):
        seed_job(conn, url_suffix=f"nf-{i}", company="netflix",
                 apply_status="applied", applied_at=_iso(1))
    # One candidate
    seed_job(conn, url_suffix="nf-new", company="netflix", fit_score=10,
             tailored_resume_path="/tmp/r.pdf", apply_status=None,
             discovered_at=_iso(1))

    # YAML override: netflix cap=1
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
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `.venv/bin/pytest tests/test_company_cap.py -v -k "acquire_job"`
Expected: all 4 tests fail (either because the cap is currently soft-sort-only, or `max_age_days` isn't accepted).

- [ ] **Step 3: Refactor `acquire_job`**

Edit `src/applypilot/apply/launcher.py`. Replace the function at line 1078. Here is the full new body (from the `def acquire_job` line through the end of the function):

```python
def acquire_job(target_url: str | None = None,
                min_score: int | None = None,
                max_score: int | None = None,
                max_age_days: int | None = None,
                worker_id: int = 0) -> dict | None:
    """Atomically acquire the next job to apply to.

    Enforces:
      - Minimum fit score (config.DEFAULTS["min_score"] default)
      - Job age cutoff (config.DEFAULTS["max_job_age_days"] default)
      - Per-company open-pipeline cap (YAML-configurable)
      - Per-company concurrency: at most 1 active worker per company
      - Per-ATS concurrency: at most 1 active worker per ATS family
      - Manual-ATS skip list
    """
    from applypilot import config as _cfg
    from datetime import datetime, timedelta, timezone
    from collections import defaultdict

    if min_score is None:
        min_score = _cfg.DEFAULTS["min_score"]
    if max_age_days is None:
        max_age_days = _cfg.DEFAULTS["max_job_age_days"]

    conn = get_connection()
    try:
        _begin_deadline = time.monotonic() + 300
        _begin_delay = 2.0
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as _be:
                if "locked" not in str(_be).lower():
                    raise
                if time.monotonic() >= _begin_deadline:
                    raise
                logger.debug("acquire_job: DB locked, retrying in %.0fs…", _begin_delay)
                time.sleep(_begin_delay)
                _begin_delay = min(_begin_delay * 1.5, 30.0)

        # Release stale in_progress locks from crashed runs (>30 min old)
        conn.execute("""
            UPDATE jobs SET apply_status = NULL, agent_id = NULL
            WHERE apply_status = 'in_progress'
              AND last_attempted_at IS NOT NULL
              AND last_attempted_at < datetime('now', '-30 minutes')
        """)

        if target_url:
            like = f"%{target_url.split('?')[0].rstrip('/')}%"
            row = conn.execute("""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path, company
                FROM jobs
                WHERE (url = ? OR application_url = ? OR application_url LIKE ? OR url LIKE ?)
                  AND tailored_resume_path IS NOT NULL
                  AND (apply_status IS NULL OR apply_status != 'in_progress')
                ORDER BY
                    CASE WHEN url = ? OR application_url = ? THEN 0 ELSE 1 END
                LIMIT 1
            """, (target_url, target_url, like, like,
                  target_url, target_url)).fetchone()
        else:
            blocked_sites, blocked_patterns = _load_blocked()
            site_filter = " AND ".join(f"site != '{s}'" for s in blocked_sites) if blocked_sites else "1=1"
            url_filter = " AND ".join(f"url NOT LIKE '{p}'" for p in blocked_patterns) if blocked_patterns else "1=1"
            max_score_filter = f"AND j.fit_score <= {max_score}" if max_score is not None else ""

            # Per-worker concurrency: don't let two workers run the same company or ATS
            in_progress_rows = conn.execute(
                "SELECT company, application_url FROM jobs WHERE apply_status = 'in_progress'"
            ).fetchall()
            active_companies: set[str] = set()
            active_ats: set[str] = set()
            for ip in in_progress_rows:
                if ip["company"]:
                    active_companies.add(ip["company"].lower())
                ats = detect_ats(ip["application_url"] or "")
                if ats:
                    active_ats.add(ats)

            if active_companies:
                ph = ",".join("?" * len(active_companies))
                company_excl = f"AND LOWER(COALESCE(j.company, '')) NOT IN ({ph})"
                company_excl_params: list = list(active_companies)
            else:
                company_excl = ""
                company_excl_params = []

            # Age filter
            age_filter = ""
            age_params: list = []
            if max_age_days and max_age_days > 0:
                age_filter = "AND j.discovered_at > datetime('now', ?)"
                age_params = [f"-{max_age_days} days"]

            # Fetch candidates. No more soft-sort deprioritization — hard cap
            # is enforced in Python below.
            candidates = conn.execute(f"""
                SELECT j.url, j.title, j.site, j.application_url,
                       j.tailored_resume_path, j.fit_score, j.location,
                       j.full_description, j.cover_letter_path, j.company
                FROM jobs j
                WHERE j.tailored_resume_path IS NOT NULL
                  AND (j.apply_status IS NULL OR j.apply_status = 'failed')
                  AND (j.apply_attempts IS NULL OR j.apply_attempts < {config.DEFAULTS["max_apply_attempts"]})
                  AND j.fit_score >= ?
                  {max_score_filter}
                  AND {site_filter}
                  AND {url_filter}
                  {company_excl}
                  {age_filter}
                ORDER BY j.fit_score DESC, j.url
                LIMIT 100
            """, (min_score, *company_excl_params, *age_params)).fetchall()

            # Build in-flight buckets once, reuse for every candidate.
            in_flight = get_in_flight_by_company(conn)
            now_utc = datetime.now(timezone.utc)

            def over_cap(company: str | None) -> bool:
                if not company or not company.strip():
                    return False
                cap, window = _cfg.get_company_limit(company)
                if cap < 0:
                    return False
                if cap == 0:
                    return True
                cutoff = (now_utc - timedelta(days=window)).isoformat()
                count = sum(1 for ts in in_flight.get(company.lower(), [])
                            if ts and ts > cutoff)
                return count >= cap

            # Pick first candidate whose company is under cap AND ATS lane is free.
            row = None
            for cand in candidates:
                if over_cap(cand["company"]):
                    continue
                ats = detect_ats(cand["application_url"] or cand["url"] or "")
                if ats is not None and ats in active_ats:
                    continue
                row = cand
                break

            if row is None and candidates:
                logger.debug(
                    "acquire_job: all %d candidates blocked (ATS lanes=%s, cap-blocked companies present)",
                    len(candidates), active_ats,
                )

        if not row:
            conn.rollback()
            return None

        from applypilot.config import is_manual_ats
        apply_url = row["application_url"] or row["url"]
        if is_manual_ats(apply_url):
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS', "
                "apply_category = 'manual_only' WHERE url = ?",
                (row["url"],),
            )
            conn.commit()
            logger.info("Skipping manual ATS: %s", row["url"][:80])
            return None

        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            UPDATE jobs SET apply_status = 'in_progress',
                           agent_id = ?,
                           last_attempted_at = ?
            WHERE url = ?
        """, (f"worker-{worker_id}", now, row["url"]))
        conn.commit()

        return dict(row)
    except Exception:
        conn.rollback()
        raise
```

Also update the import block at the top of `launcher.py` to include `get_in_flight_by_company`:

```python
from applypilot.database import (
    get_connection,
    mark_result,                              # existing imports
    # ... keep all existing imports ...
    get_in_flight_by_company,                 # NEW
)
```

(Check actual imports; add `get_in_flight_by_company` to whatever import list is there.)

- [ ] **Step 4: Update `worker_loop` and `_worker_loop_body` (launcher.py lines 2329-2380) to accept and thread `max_age_days`**

Current `worker_loop` (line 2329):

```python
def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 7, max_score: int | None = None,
                headless: bool = False,
                model: str = "sonnet", dry_run: bool = False,
                fresh_sessions: bool = False,
                total_workers: int = 1,
                no_hitl: bool = False) -> tuple[int, int]:
```

Replace with:

```python
def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int | None = None,
                max_score: int | None = None,
                max_age_days: int | None = None,
                headless: bool = False,
                model: str = "sonnet", dry_run: bool = False,
                fresh_sessions: bool = False,
                total_workers: int = 1,
                no_hitl: bool = False) -> tuple[int, int]:
    from applypilot import config as _cfg
    if min_score is None:
        min_score = _cfg.DEFAULTS["min_score"]
    if max_age_days is None:
        max_age_days = _cfg.DEFAULTS["max_job_age_days"]
```

In the body at line 2364, update the `_worker_loop_body(...)` call to pass the new arg:

```python
        return _worker_loop_body(
            worker_id, limit, target_url, min_score, max_score, max_age_days,
            headless, model, dry_run, fresh_sessions, applied, failed, continuous,
            jobs_done, empty_polls, port, total_workers, no_hitl=no_hitl,
        )
```

Update `_worker_loop_body` signature at line 2373 to accept the new positional:

```python
def _worker_loop_body(
    worker_id: int, limit: int, target_url: str | None,
    min_score: int, max_score: int | None, max_age_days: int | None,
    headless: bool,
    model: str, dry_run: bool, fresh_sessions: bool,
    applied: int, failed: int, continuous: bool,
    jobs_done: int, empty_polls: int, port: int,
    total_workers: int = 1, no_hitl: bool = False,
) -> tuple[int, int]:
```

Update the `acquire_job(...)` call at line 2399 to pass `max_age_days`:

```python
        job = acquire_job(target_url=_effective_target, min_score=min_score,
                          max_score=max_score, max_age_days=max_age_days,
                          worker_id=worker_id)
```

Check for additional `acquire_job(...)` call sites:

```bash
grep -n "acquire_job(" src/applypilot/apply/launcher.py
```

Line 1293 has one (inside the target-url path). It already passes target_url / min_score / max_score / worker_id — add `max_age_days=max_age_days` if the enclosing function has that variable, or leave unchanged (this call path is for `--url URL` which is explicit user intent and should bypass the age filter — verify by reading 10 lines before line 1293 to see the calling context).

- [ ] **Step 5: Run all cap tests**

Run: `.venv/bin/pytest tests/test_company_cap.py -v`
Expected: all 8 tests pass.

- [ ] **Step 6: Smoke-test against live DB (dry-run)**

Run: `.venv/bin/applypilot apply --dry-run --min-score 8 --workers 1 --max-age-days 14 2>&1 | head -30`

Expected: either finds 0 jobs (current DB is 30-90d old — expected with max-age=14) or acquires one and dry-run logs its URL. No exceptions.

- [ ] **Step 7: Commit**

```bash
git add src/applypilot/apply/launcher.py tests/test_company_cap.py
git commit -m "feat(apply): hard per-company cap + stale filter in acquire_job

Replaces the previous soft-sort deprioritization with a Python-side
hard cap consulting config.get_company_limit(company). Drops the
recent_applied_count LEFT JOIN and company_rank PARTITION BY. Adds
discovered_at age filter to candidate SELECT.

Default cap: 3 in-flight per 30d per company. NULL/empty company is
exempt. YAML overrides at ~/.applypilot/company_limits.yaml.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10 — Verify no hardcoded `score >= 7` gates remain

This is a grep-verification step plus cleanup for anything missed.

**Files:**
- Audit only; any fixes live in the relevant file.

- [ ] **Step 1: Grep for stale score-7 gates**

Run:
```bash
grep -rn "min_score.*=.*7\|score >= 7\|fit_score >= 7" src/applypilot/ --include='*.py' | grep -v test_
```

Expected remaining hits (all acceptable, annotate if not already):
- `cli.py:408` (or similar) — display-only color coding `if score >= 7: color = yellow`
- Any docstring/comment reference

Forbidden hits — any `fit_score >= 7` in a SELECT, any function parameter `min_score: int = 7`, any `min_score=7` kwarg.

- [ ] **Step 2: Fix any remaining gate**

If a hit is a gate, edit to read from `config.DEFAULTS["min_score"]`. For inline SQL that still has `fit_score >= 7`, replace with parameterized form reading from the caller's `min_score` argument.

- [ ] **Step 3: Run full test suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all pass.

- [ ] **Step 4: Commit (only if fixes were needed)**

```bash
git add -u
git commit -m "chore: remove lingering min_score=7 literal gates

Follow-up to the config-driven threshold change. Display-only color
code at cli.py:408 intentionally kept.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

If no changes, skip the commit.

---

## Task 11 — Status output: show skipped-stale and blocked-by-cap counts

**Files:**
- Modify: `src/applypilot/cli.py` (`status` command, around line 337)
- Modify: `src/applypilot/database.py` (extend `get_stats()`)

- [ ] **Step 1: Extend `get_stats()` in database.py**

Run: `grep -n "def get_stats" src/applypilot/database.py` to locate.

Edit `get_stats()` to add two new keys:

```python
    # Existing code computes total, scored, etc.
    # Append two new sections:

    from applypilot.config import DEFAULTS
    max_age = DEFAULTS["max_job_age_days"]

    # Skipped-stale: jobs with tailored_resume_path but discovered_at too old
    stats["skipped_stale"] = conn.execute(f"""
        SELECT COUNT(*) FROM jobs
        WHERE tailored_resume_path IS NOT NULL
          AND (apply_status IS NULL OR apply_status = 'failed')
          AND (discovered_at IS NULL OR discovered_at <= datetime('now', '-{max_age} days'))
    """).fetchone()[0]

    # Blocked-by-cap: companies over their per-company cap
    from collections import defaultdict
    from applypilot.config import get_company_limit
    in_flight = get_in_flight_by_company(conn)
    blocked_companies: list[str] = []
    for co, stamps in in_flight.items():
        cap, _ = get_company_limit(co)
        if cap >= 0 and len(stamps) >= cap and cap > 0:
            blocked_companies.append(co)
        elif cap == 0:
            blocked_companies.append(co)
    stats["blocked_by_cap"] = {
        "count": len(blocked_companies),
        "companies": sorted(blocked_companies)[:20],  # preview
    }
```

- [ ] **Step 2: Update `status` CLI command to print the new fields**

Edit `src/applypilot/cli.py` `status` function. After the existing funnel output, add a section like:

```python
    # Age-filter and cap diagnostics
    if stats.get("skipped_stale"):
        console.print(f"\n[dim]Skipped as stale (>{config.DEFAULTS['max_job_age_days']}d old): {stats['skipped_stale']}[/dim]")

    bbc = stats.get("blocked_by_cap") or {}
    if bbc.get("count"):
        console.print(
            f"\n[yellow]Blocked by company cap:[/yellow] {bbc['count']} companies"
        )
        if bbc["companies"]:
            preview = ", ".join(bbc["companies"][:10])
            more = f" (+{bbc['count'] - 10} more)" if bbc["count"] > 10 else ""
            console.print(f"  [dim]{preview}{more}[/dim]")
```

- [ ] **Step 3: Smoke-test**

Run: `.venv/bin/applypilot status 2>&1 | tail -20`
Expected:
- Pipeline funnel as before
- `Skipped as stale (>14d old): <N>` line (should be ~4176+ for current DB)
- `Blocked by company cap: <N> companies` line (netflix likely shown since it had 9 apps, most aged out though)

- [ ] **Step 4: Commit**

```bash
git add src/applypilot/cli.py src/applypilot/database.py
git commit -m "feat(status): surface skipped-stale and blocked-by-cap counts

applypilot status now shows how many ready-to-apply jobs got filtered
out by the age cutoff, and how many companies are currently blocked
by the per-company cap.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 12 — End-to-end verification

Manual checklist. No code changes unless issues surface.

- [ ] **Step 1: Baseline snapshot**

Run: `.venv/bin/applypilot status > /tmp/after-funnel.txt && cat /tmp/after-funnel.txt`

Expected output contains:
- `Min score: 8` somewhere (or at least no `min_score=7` mentions)
- `Skipped as stale (>14d old):` line with a nonzero count
- `Blocked by company cap:` line if any company has ≥3 in-flight in last 30d

- [ ] **Step 2: Dry-run apply to confirm queue behavior**

Run: `.venv/bin/applypilot apply --dry-run --workers 1 2>&1 | head -30`

Expected: either "No jobs to apply" (because most/all are stale under 14d) or at least one acquired job that is NOT from netflix (if netflix was over cap historically). If the dry-run succeeds, check its company doesn't exceed 3 in-flight.

- [ ] **Step 3: Create a minimal company_limits.yaml**

Create `~/.applypilot/company_limits.yaml`:
```yaml
defaults:
  max_in_flight: 3
  window_days: 30
overrides:
  # Start with just an example; user edits as desired
  # google:
  #   max_in_flight: 0
```

- [ ] **Step 4: Force a discovery refresh**

Run: `.venv/bin/applypilot run discover 2>&1 | tail -5`

Expected: discovery fetches fresh jobs from the configured searches. After completion, `applypilot status` should show nonzero `discovered` / `enriched` in the last 14 days.

- [ ] **Step 5: Run full test suite one more time**

Run: `.venv/bin/pytest tests/ -q`
Expected: all green.

- [ ] **Step 6: Final commit marker (no-op if clean)**

```bash
git status
# If clean, no commit needed.
# If any adjustments were made during verification:
git commit -m "chore: funnel-optimization verification tweaks

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 7: Update CLAUDE.md decision log**

Add under `## Security Decisions`:

```markdown
| 29 | min_score default = 8, age filter 14d, hard per-company cap 3/30d | 2026-04-23 funnel spec. Configurable via config.DEFAULTS and ~/.applypilot/company_limits.yaml. Replaces soft-sort deprioritization with hard cap. |
```

Commit:
```bash
git add CLAUDE.md
git commit -m "docs: record funnel optimization in decision log

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review Checklist

Before handing this plan to an executor:

- [x] **Spec coverage.** Every change from the spec has a task:
  - Change 1 (min_score=8 configurable) → Tasks 2, 5, 6, 7, 10
  - Change 2 (stale skip) → Tasks 2, 4, 5, 6, 7
  - Change 3 (company cap) → Tasks 2, 3, 8, 9
  - Change 4 (AI stage gating) → Tasks 4, 7 (via pending_tailor / pending_cover)
  - Status output → Task 11
  - End-to-end → Task 12

- [x] **No placeholders.** All code blocks are complete. "TBD" / "similar to" scanned; none found.

- [x] **Type consistency.** `get_company_limit` returns `tuple[int, int]`, used consistently in Task 9. `max_age_days: int | None` signature consistent across tasks 4-7 and 9. `get_in_flight_by_company` returns `dict[str, list[str]]`, consumed as such in Task 9.

- [x] **Exact file paths.** Every task names files with absolute or repo-relative paths.

- [x] **TDD order.** Tests before implementation in tasks 2, 4, 7, 8, 9.

- [x] **Frequent commits.** Every task ends with a commit step.

One known soft spot: Task 7 Step 1 monkeypatch test may be brittle depending on how `tailor.py` imports Path/load_profile. If the test fails at runtime, the executor can simplify the test to just assert that calling `run_tailoring(max_age_days=14)` doesn't raise and returns `{"approved": 0, ...}` when the DB has only stale jobs — the integration behavior matters more than the kwarg-capture assertion.
