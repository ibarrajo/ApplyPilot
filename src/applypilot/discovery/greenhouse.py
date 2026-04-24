"""Greenhouse ATS direct API scraper.

Scrapes Greenhouse-powered career sites (Temporal, Pulumi, Anduril, Stripe,
Databricks, etc.) via the public board API. Zero LLM, zero browser — pure HTTP.

Board API: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true

Company slugs are configured in config/greenhouse_employers.yaml.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

import yaml

from applypilot import config
from applypilot.config import CONFIG_DIR
from applypilot.database import commit_with_retry, get_connection, init_db

log = logging.getLogger(__name__)


GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
_HEADERS = {
    "User-Agent": "ApplyPilot/1.0 (job-discovery)",
    "Accept": "application/json",
}


# ── Employer registry ─────────────────────────────────────────────────

def load_employers() -> dict:
    """Load Greenhouse employer registry from config/greenhouse_employers.yaml."""
    path = CONFIG_DIR / "greenhouse_employers.yaml"
    if not path.exists():
        log.warning("greenhouse_employers.yaml not found at %s", path)
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("employers", {})


# ── HTML strip helper ─────────────────────────────────────────────────

class _HTMLStripper(HTMLParser):
    """Strip HTML tags, preserve text content and line breaks."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skip = True
        elif tag in ("p", "br", "li", "div", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        # Collapse whitespace
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _strip_html(html: str) -> str:
    if not html:
        return ""
    s = _HTMLStripper()
    try:
        s.feed(html)
    except Exception:
        return html  # fallback: return raw
    return s.text()


# ── Location filter ───────────────────────────────────────────────────

def _load_location_filter(search_cfg: dict | None = None):
    if search_cfg is None:
        search_cfg = config.load_search_config()
    loc = search_cfg.get("location", {}) or {}
    accept = loc.get("accept_patterns", []) or []
    return accept


def _location_ok(location: str | None, accept: list[str]) -> bool:
    """Return True if location passes the user's filter.

    Remote is always accepted. Otherwise location must contain one of the
    accept patterns (case-insensitive).
    """
    if not location:
        # Empty location — let it through (some Greenhouse jobs omit location).
        return True
    loc = location.lower()
    if any(r in loc for r in ("remote", "anywhere", "work from home", "wfh")):
        return True
    if not accept:
        return True
    return any(a.lower() in loc for a in accept)


# ── HTTP fetch ────────────────────────────────────────────────────────

def _fetch_json(url: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── Per-employer scrape ───────────────────────────────────────────────

def scrape_one_employer(
    slug: str,
    emp: dict,
    accept_locs: list[str],
    max_retries: int = 2,
) -> tuple[list[dict], str | None]:
    """Fetch all jobs from one Greenhouse board.

    Returns (jobs, error) — jobs is a list of normalized dicts, error is
    non-None if the fetch failed.
    """
    url = GREENHOUSE_API.format(slug=slug) + "?content=true"
    last_err: str | None = None

    for attempt in range(max_retries):
        try:
            data = _fetch_json(url)
            break
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} {e.reason}"
            if e.code == 404:
                return [], last_err
            time.sleep(2 + attempt * 3)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(2 + attempt * 3)
    else:
        return [], last_err or "unknown error"

    jobs_raw = data.get("jobs", [])
    name = emp.get("name", slug)

    out = []
    for job in jobs_raw:
        location_name = (job.get("location") or {}).get("name") or ""
        if not _location_ok(location_name, accept_locs):
            continue

        abs_url = job.get("absolute_url")
        if not abs_url:
            continue

        content_html = job.get("content") or ""
        # Greenhouse sometimes returns HTML-entity-encoded content
        content_html = content_html.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        description = _strip_html(content_html)

        # Greenhouse returns `updated_at` + `first_published`. Prefer the
        # earlier posting date when available.
        posted_at = job.get("first_published") or job.get("updated_at") or None

        out.append({
            "url": abs_url,
            "title": job.get("title") or "",
            "location": location_name or None,
            "description": description[:500] if description else None,
            "full_description": description if len(description) > 200 else None,
            "application_url": abs_url,
            "employer_name": name,
            "employer_slug": slug,
            "posted_at": posted_at,
        })

    return out, None


# ── DB insert ─────────────────────────────────────────────────────────

def _insert_jobs(conn: sqlite3.Connection, jobs: list[dict]) -> tuple[int, int]:
    """Insert jobs. Returns (new, existing)."""
    new = 0
    existing = 0
    now = datetime.now(timezone.utc).isoformat()

    for job in jobs:
        url = job.get("url")
        if not url:
            continue

        full_description = job.get("full_description")
        detail_scraped_at = now if full_description else None
        site = job.get("employer_name", "Greenhouse")
        strategy = "greenhouse_api"

        initial_state = "enriched" if full_description else "discovered"
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, "
                "discovered_at, posted_at, full_description, application_url, "
                "detail_scraped_at, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (url, job.get("title"), None, job.get("description"), job.get("location"),
                 site, strategy, now, job.get("posted_at"), full_description,
                 job.get("application_url"), detail_scraped_at, initial_state),
            )
            conn.execute(
                "INSERT INTO job_state_transitions "
                "(job_url, from_state, to_state, at, reason, metadata) "
                "VALUES (?, NULL, ?, ?, ?, ?)",
                (url, initial_state, now, f"discovered via {strategy}", None),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    commit_with_retry(conn)
    return new, existing


# ── Public entry point ────────────────────────────────────────────────

def run_greenhouse_discovery(employers: dict | None = None, workers: int = 1) -> dict:
    """Discover jobs from Greenhouse-powered career sites.

    Args:
        employers: Override the employer registry (for tests). Loads from YAML if None.
        workers: Currently unused — HTTP fetches are sequential for simplicity.

    Returns:
        {'found': int, 'new': int, 'existing': int, 'employers': int, 'errors': list}
    """
    if employers is None:
        employers = load_employers()

    if not employers:
        log.warning("No Greenhouse employers configured. Create config/greenhouse_employers.yaml.")
        return {"found": 0, "new": 0, "existing": 0, "employers": 0, "errors": []}

    accept_locs = _load_location_filter()

    conn = get_connection()
    init_db()

    grand_new = 0
    grand_existing = 0
    grand_found = 0
    errors: list[str] = []

    log.info("Greenhouse crawl: %d employers", len(employers))

    for slug, emp in employers.items():
        name = emp.get("name", slug)
        try:
            jobs, err = scrape_one_employer(slug, emp, accept_locs)
            if err:
                log.warning("  [%s] %s", slug, err)
                errors.append(f"{slug}: {err}")
                continue

            new, existing = _insert_jobs(conn, jobs)
            grand_new += new
            grand_existing += existing
            grand_found += len(jobs)
            log.info("  [%s] %s: %d found (%d new, %d existing)",
                     slug, name, len(jobs), new, existing)
        except Exception as e:
            log.exception("Greenhouse scrape failed for %s: %s", slug, e)
            errors.append(f"{slug}: {e}")

    log.info("Greenhouse crawl done: %d found (%d new, %d existing) across %d employers",
             grand_found, grand_new, grand_existing, len(employers))

    return {
        "found": grand_found,
        "new": grand_new,
        "existing": grand_existing,
        "employers": len(employers),
        "errors": errors,
    }
