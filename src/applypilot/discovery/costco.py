"""Costco careers direct API scraper.

careers.costco.com exposes a public GET JSON API at /api/jobs (Phenom+iCIMS
hybrid frontend). Most roles are warehouse/retail but corporate HQ tech
roles sit under "Home/Regional Offices" — worth capturing for completeness
since Costco's Issaquah HQ is local. Zero bot protection.
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

from applypilot import config
from applypilot.database import commit_with_retry, get_connection, init_db

log = logging.getLogger(__name__)


SEARCH_URL = "https://careers.costco.com/api/jobs"
_HEADERS = {
    "User-Agent": "ApplyPilot/1.0 (job-discovery)",
    "Accept": "application/json",
}


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "br", "li", "div"):
            self.parts.append("\n")

    def handle_data(self, data):
        if data.strip():
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


def _fetch_page(params: dict, timeout: float = 20.0) -> dict:
    query = urllib.parse.urlencode(params, doseq=True)
    url = f"{SEARCH_URL}?{query}"
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def search_costco_jobs(
    query: str = "",
    location: str = "Seattle, WA",
    page_size: int = 25,
    max_pages: int = 10,
) -> list[dict]:
    jobs: list[dict] = []
    offset = 0
    pages_fetched = 0

    while pages_fetched < max_pages:
        params = {
            "location": location,
            "limit": page_size,
            "offset": offset,
        }
        if query:
            params["keyword"] = query

        try:
            data = _fetch_page(params)
        except urllib.error.HTTPError as e:
            log.warning("costco.com HTTP %d for %r: %s", e.code, query, e.reason)
            break
        except Exception as e:
            log.warning("costco.com fetch error for %r: %s", query, e)
            break

        total = data.get("totalCount") or data.get("total") or 0
        page_jobs = data.get("jobs") or data.get("results") or []
        if not page_jobs:
            break

        for wrapper in page_jobs:
            # Costco wraps each result under a `data` key.
            job = wrapper.get("data") if isinstance(wrapper, dict) and "data" in wrapper else wrapper
            if not isinstance(job, dict):
                continue

            apply_url = (job.get("apply_url") or job.get("applyUrl")
                         or job.get("url") or "")
            if not apply_url:
                req_id = job.get("req_id") or job.get("requisition_id") or ""
                if req_id:
                    apply_url = f"https://careers.costco.com/job/{req_id}"
            if not apply_url:
                continue

            city = (job.get("city") or "").strip()
            state = (job.get("state") or "").strip()
            full_location = (job.get("full_location") or "").strip()
            location_str = full_location or ", ".join(p for p in (city, state) if p) or location

            description = job.get("description") or job.get("job_description") or ""
            description = _strip_html(description) if description else ""

            jobs.append({
                "url": apply_url,
                "title": job.get("title") or job.get("job_title") or "",
                "location": location_str,
                "description": description[:500] if description else None,
                "full_description": description if len(description) > 200 else None,
                "application_url": apply_url,
                "req_id": job.get("req_id") or job.get("requisition_id") or "",
            })

        pages_fetched += 1
        offset += page_size
        if offset >= total:
            break

        time.sleep(0.5)

    return jobs


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
                 "Costco", "costco_careers", now, full_description, job.get("application_url"),
                 detail_scraped_at),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    commit_with_retry(conn)
    return new, existing


def run_costco_discovery(workers: int = 1, queries: list[str] | None = None) -> dict:
    """Discover jobs on careers.costco.com for Seattle-area roles."""
    search_cfg = config.load_search_config()

    if queries is None:
        all_queries = search_cfg.get("queries", []) or []
        queries = [q["query"] for q in all_queries if q.get("tier", 99) <= 2]

    if not queries:
        return {"found": 0, "new": 0, "existing": 0, "queries": 0}

    conn = get_connection()
    init_db()

    grand_new = 0
    grand_existing = 0
    grand_found = 0

    log.info("Costco discovery: %d queries", len(queries))

    for i, q in enumerate(queries, 1):
        try:
            jobs = search_costco_jobs(q, location="Seattle, WA")
        except Exception as e:
            log.warning("Costco query %r failed: %s", q, e)
            continue

        new, existing = _insert_jobs(conn, jobs)
        grand_new += new
        grand_existing += existing
        grand_found += len(jobs)
        log.info("  [%d/%d] %r: %d found (%d new, %d existing)",
                 i, len(queries), q, len(jobs), new, existing)

    log.info("Costco discovery done: %d found (%d new, %d existing)",
             grand_found, grand_new, grand_existing)

    return {
        "found": grand_found,
        "new": grand_new,
        "existing": grand_existing,
        "queries": len(queries),
    }
