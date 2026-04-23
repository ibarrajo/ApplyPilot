"""Shared pytest fixtures for applypilot tests.

`tmp_db` yields a factory returning a fresh, schema-initialized SQLite
connection rooted at a tmp path. Each call is isolated — tests that need
multiple DBs get multiple calls.

Adaptation notes vs. original plan:
- database uses `_local` (threading.local), not `_thread_local`
- connections are cached in `_local.connections` dict keyed by path string
- init_db(db_path) accepts a path arg and returns the connection directly
"""

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

    # Reset the module-level thread-local connection cache so get_connection
    # opens fresh against the new tmp path.  _local.connections is a dict
    # keyed by path string; clear it entirely to avoid any stale handle.
    if hasattr(database._local, "connections"):
        for conn in database._local.connections.values():
            try:
                conn.close()
            except Exception:
                pass
        database._local.connections.clear()

    def _factory() -> sqlite3.Connection:
        # init_db(db_path) creates the schema and returns the connection.
        return database.init_db(db_file)

    yield _factory

    # Cleanup: close the connection opened for the tmp db_file
    if hasattr(database._local, "connections"):
        path_key = str(db_file)
        conn = database._local.connections.pop(path_key, None)
        if conn is not None:
            try:
                conn.close()
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
