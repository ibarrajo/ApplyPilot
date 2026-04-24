"""Amazon.jobs direct search API scraper.

Amazon exposes a fully public GET JSON API at amazon.jobs/en/search.json with
no auth required. Returns job listings filtered by keyword + location +
category. Biggest single local employer (~1,500 Seattle software roles as of
2026-04-24) and the queue is almost entirely absent from LinkedIn scraping
since Amazon aggressively filters its listings off third-party aggregators.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

import yaml

from applypilot import config
from applypilot.database import commit_with_retry, get_connection, init_db

log = logging.getLogger(__name__)


SEARCH_URL = "https://www.amazon.jobs/en/search.json"
_HEADERS = {
    "User-Agent": "ApplyPilot/1.0 (job-discovery)",
    "Accept": "application/json",
}


# ── HTML strip ────────────────────────────────────────────────────────

class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip = True
        elif tag in ("p", "br", "li", "div", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
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
        return html
    return s.text()


# ── HTTP fetch with pagination ────────────────────────────────────────

def _fetch_page(params: dict, timeout: float = 20.0) -> dict:
    query = urllib.parse.urlencode(params, doseq=True)
    url = f"{SEARCH_URL}?{query}"
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def search_amazon_jobs(
    base_query: str,
    location: str = "Seattle, Washington",
    radius_km: int = 24,
    page_size: int = 100,
    max_pages: int = 10,
    category: str = "software-development",
) -> list[dict]:
    """Return normalized job dicts from amazon.jobs/en/search.json.

    Paginates until either `max_pages` or the total hits are exhausted.
    """
    jobs: list[dict] = []
    offset = 0
    pages_fetched = 0

    while pages_fetched < max_pages:
        params = {
            "base_query": base_query,
            "loc_query": location,
            "radius": f"{radius_km}km",
            "result_limit": page_size,
            "offset": offset,
        }
        if category:
            params["category[]"] = category

        try:
            data = _fetch_page(params)
        except urllib.error.HTTPError as e:
            log.warning("amazon.jobs HTTP %d for %r: %s", e.code, base_query, e.reason)
            break
        except Exception as e:
            log.warning("amazon.jobs fetch error for %r: %s", base_query, e)
            break

        hits = data.get("hits", 0)
        page_jobs = data.get("jobs", []) or []
        if not page_jobs:
            break

        for job in page_jobs:
            job_path = job.get("job_path") or ""
            if not job_path:
                continue
            url = f"https://amazon.jobs{job_path}" if job_path.startswith("/") else job_path

            # Description: combine the short `description` + `basic_qualifications`
            # + `preferred_qualifications` if available.
            desc_parts = []
            for key in ("description", "basic_qualifications", "preferred_qualifications"):
                v = job.get(key)
                if v:
                    desc_parts.append(_strip_html(v))
            description = "\n\n".join(desc_parts)

            city = (job.get("city") or "").strip()
            state = (job.get("state") or "").strip()
            location_str = ", ".join(p for p in (city, state) if p) or location

            jobs.append({
                "url": url,
                "title": job.get("title") or "",
                "location": location_str,
                "description": description[:500] if description else None,
                "full_description": description if len(description) > 200 else None,
                "application_url": url,
                "posted_date": job.get("posted_date") or "",
            })

        pages_fetched += 1
        offset += page_size
        if offset >= hits:
            break

        # Be polite — small pause between pages
        time.sleep(0.5)

    return jobs


# ── DB insert ─────────────────────────────────────────────────────────

def _insert_jobs(conn: sqlite3.Connection, jobs: list[dict]) -> tuple[int, int]:
    new = 0
    existing = 0
    now = datetime.now(timezone.utc).isoformat()

    for job in jobs:
        url = job.get("url")
        if not url:
            continue
        full_description = job.get("full_description")
        detail_scraped_at = now if full_description else None
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, "
                "discovered_at, full_description, application_url, detail_scraped_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (url, job.get("title"), None, job.get("description"), job.get("location"),
                 "Amazon", "amazon_jobs", now, full_description, job.get("application_url"),
                 detail_scraped_at),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    commit_with_retry(conn)
    return new, existing


# ── Public entry point ────────────────────────────────────────────────

def run_amazon_discovery(workers: int = 1, queries: list[str] | None = None) -> dict:
    """Discover jobs on amazon.jobs for the configured queries + Seattle area.

    Important: Amazon's search is exact-match (no stemming/fuzzy). User queries
    like "Senior Software Engineer backend" return 0 hits because Amazon's
    standard title is "SDE" / "Software Development Engineer". We default to
    a small set of broad Amazon-style queries and rely on the downstream
    scorer to filter by seniority + stack.

    Args:
        workers: Unused — pages are fetched sequentially.
        queries: Override the Amazon query list.

    Returns:
        {'found': int, 'new': int, 'existing': int, 'queries': int}
    """
    # Amazon-specific broad queries. The scorer + pre-filter will reject
    # roles that aren't backend/distributed-systems at Senior+ level.
    AMAZON_DEFAULT_QUERIES = [
        "software engineer",
        "software development engineer",
        "principal engineer",
        "principal software engineer",
        "senior software engineer",
        "staff engineer",
        "backend engineer",
        "platform engineer",
        "distributed systems engineer",
    ]

    if queries is None:
        queries = AMAZON_DEFAULT_QUERIES

    if not queries:
        log.warning("No queries configured for Amazon discovery.")
        return {"found": 0, "new": 0, "existing": 0, "queries": 0}

    conn = get_connection()
    init_db()

    grand_new = 0
    grand_existing = 0
    grand_found = 0

    log.info("Amazon discovery: %d queries", len(queries))

    for i, q in enumerate(queries, 1):
        try:
            jobs = search_amazon_jobs(q, location="Seattle, Washington")
        except Exception as e:
            log.warning("Amazon query %r failed: %s", q, e)
            continue

        new, existing = _insert_jobs(conn, jobs)
        grand_new += new
        grand_existing += existing
        grand_found += len(jobs)
        log.info("  [%d/%d] %r: %d found (%d new, %d existing)",
                 i, len(queries), q, len(jobs), new, existing)

    log.info("Amazon discovery done: %d found (%d new, %d existing) across %d queries",
             grand_found, grand_new, grand_existing, len(queries))

    return {
        "found": grand_found,
        "new": grand_new,
        "existing": grand_existing,
        "queries": len(queries),
    }
