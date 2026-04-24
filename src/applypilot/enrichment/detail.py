"""Detail page enrichment: scrapes full descriptions and apply URLs.

For each job URL in the database, navigates to the detail page and extracts:
  - full_description: the complete job posting text
  - application_url: the "Apply" button/link URL

Three-tier extraction cascade (cheapest first):
  Tier 1: JSON-LD JobPosting structured data (0 tokens)
  Tier 2: Deterministic CSS pattern matching (0 tokens)
  Tier 3: LLM-assisted extraction (1 LLM call)
"""

import json
import logging
import random
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from applypilot.database import init_db, transition_state
from applypilot.llm import get_client

log = logging.getLogger(__name__)

def _get_ua() -> str:
    """Build a realistic UA from the actual installed Chrome version."""
    from applypilot.apply.chrome import _get_real_user_agent
    return _get_real_user_agent()


UA = _get_ua()

# Stealth JS patches — hides automation signals from bot detectors
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

Object.defineProperty(navigator, 'plugins', {
  get: () => [
    {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer'},
    {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
    {name: 'Native Client', filename: 'internal-nacl-plugin'},
  ],
});

Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});

window.chrome = window.chrome || {};
window.chrome.runtime = window.chrome.runtime || {};

const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) =>
  parameters.name === 'notifications'
    ? Promise.resolve({state: Notification.permission})
    : originalQuery(parameters);
"""

# Sites that block scraping -- skip detail extraction entirely
SKIP_DETAIL_SITES = {"glassdoor", "google", "Workopolis"}

# Module-level proxy config (set from CLI or caller)
_PROXY_CONFIG: dict | None = None


def set_proxy(proxy_str: str | None):
    """Set proxy config from an external caller."""
    global _PROXY_CONFIG
    if proxy_str:
        from applypilot.discovery.jobspy import parse_proxy
        _PROXY_CONFIG = parse_proxy(proxy_str)


# -- URL resolution ----------------------------------------------------------

def _load_base_urls() -> dict[str, str | None]:
    """Load site base URLs from config/sites.yaml."""
    from applypilot.config import load_base_urls
    return load_base_urls()


def resolve_url(raw_url: str, site: str) -> str | None:
    """Resolve a stored URL to an absolute URL."""
    if not raw_url:
        return None

    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        return raw_url

    if site == "WelcomeToTheJungle":
        return None

    if site == "Randstad Canada" and "/" not in raw_url:
        return f"https://www.randstad.ca/jobs/search/{raw_url}"

    if site == "4DayWeek" and raw_url in ("/", "/jobs"):
        return None

    base = _load_base_urls().get(site)
    if not base:
        return None

    if ";jsessionid=" in raw_url:
        raw_url = raw_url.split(";jsessionid=")[0]

    return urljoin(base, raw_url)


def resolve_all_urls(conn: sqlite3.Connection) -> dict:
    """Resolve all relative URLs in the database. Returns stats."""
    rows = conn.execute("SELECT url, site FROM jobs").fetchall()
    resolved = 0
    failed = 0
    already_absolute = 0

    for row in rows:
        url, site = row[0], row[1]
        if url.startswith("http://") or url.startswith("https://"):
            already_absolute += 1
            continue

        new_url = resolve_url(url, site)
        if new_url and new_url != url:
            try:
                conn.execute("UPDATE jobs SET url = ? WHERE url = ?", (new_url, url))
                resolved += 1
            except sqlite3.IntegrityError:
                conn.execute("DELETE FROM jobs WHERE url = ?", (url,))
                resolved += 1
        else:
            failed += 1

    # Also resolve relative application_urls
    app_resolved = 0
    rows = conn.execute(
        "SELECT url, site, application_url FROM jobs "
        "WHERE application_url IS NOT NULL AND application_url != '' "
        "AND application_url NOT LIKE 'http%'"
    ).fetchall()
    for row in rows:
        url, site, app_url = row[0], row[1], row[2]
        new_app = resolve_url(app_url, site)
        if new_app and new_app != app_url:
            conn.execute("UPDATE jobs SET application_url = ? WHERE url = ?", (new_app, url))
            app_resolved += 1

    conn.commit()
    return {"resolved": resolved, "failed": failed, "already_absolute": already_absolute,
            "app_resolved": app_resolved}


def resolve_wttj_urls(conn: sqlite3.Connection) -> int:
    """Re-fetch WTTJ Algolia API to get proper detail URLs and fix slug-as-title.
    Returns count of URLs updated."""
    wttj_jobs = conn.execute(
        "SELECT url, title FROM jobs WHERE site = 'WelcomeToTheJungle'"
    ).fetchall()

    if not wttj_jobs:
        return 0

    algolia_data: dict = {}

    def capture_algolia(response):
        if "algolia.net" in response.url and "/queries" in response.url:
            try:
                algolia_data["response"] = json.loads(response.text())
            except Exception:
                pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA)
        context.add_init_script(_STEALTH_INIT_SCRIPT)
        page = context.new_page()
        page.on("response", capture_algolia)
        page.goto(
            "https://www.welcometothejungle.com/en/jobs?query=developer&refinementList%5Bremote%5D%5B%5D=fulltime",
            timeout=60000,
        )
        page.wait_for_load_state("networkidle")
        browser.close()

    if not algolia_data.get("response"):
        log.warning("WTTJ: No Algolia response captured")
        return 0

    results = algolia_data["response"].get("results", [])
    slug_map: dict = {}
    for rs in results:
        for hit in rs.get("hits", []):
            slug = hit.get("slug", "")
            org = hit.get("organization", {})
            org_slug = org.get("slug", "") if isinstance(org, dict) else ""
            name = hit.get("name", "")
            if slug and org_slug:
                detail_url = f"https://www.welcometothejungle.com/en/companies/{org_slug}/jobs/{slug}"
                slug_map[slug] = {"url": detail_url, "name": name}

    updated = 0
    for row in wttj_jobs:
        old_url, old_title = row[0], row[1]
        slug = old_url.split("_DFNS_")[0] if "_DFNS_" in old_url else old_url
        match = slug_map.get(slug) or slug_map.get(old_url)
        if match:
            try:
                conn.execute(
                    "UPDATE jobs SET url = ?, title = ? WHERE url = ?",
                    (match["url"], match["name"] or old_title, old_url),
                )
                updated += 1
            except sqlite3.IntegrityError:
                conn.execute("DELETE FROM jobs WHERE url = ?", (old_url,))
                updated += 1
        else:
            for s, data in slug_map.items():
                if s in old_url or old_url in s:
                    try:
                        conn.execute(
                            "UPDATE jobs SET url = ?, title = ? WHERE url = ?",
                            (data["url"], data["name"] or old_title, old_url),
                        )
                        updated += 1
                    except sqlite3.IntegrityError:
                        conn.execute("DELETE FROM jobs WHERE url = ?", (old_url,))
                        updated += 1
                    break

    conn.commit()
    return updated


# -- Detail page intelligence ------------------------------------------------

def collect_detail_intelligence(page) -> dict:
    """Collect signals from a detail page. Lighter than discovery -- no API interception."""
    intel: dict = {"json_ld": [], "page_title": "", "final_url": ""}

    intel["page_title"] = page.title()
    intel["final_url"] = page.url

    for el in page.query_selector_all('script[type="application/ld+json"]'):
        try:
            data = json.loads(el.inner_text())
            intel["json_ld"].append(data)
        except Exception:
            pass

    return intel


# -- Tier 1: JSON-LD extraction -----------------------------------------------

def extract_from_json_ld(intel: dict) -> dict | None:
    """Extract description and apply URL from JSON-LD JobPosting.
    Returns {"full_description": str, "application_url": str|None} or None."""

    def find_job_posting(data):
        if isinstance(data, dict):
            if data.get("@type") == "JobPosting":
                return data
            if "@graph" in data and isinstance(data["@graph"], list):
                for item in data["@graph"]:
                    result = find_job_posting(item)
                    if result:
                        return result
        elif isinstance(data, list):
            for item in data:
                result = find_job_posting(item)
                if result:
                    return result
        return None

    for ld in intel.get("json_ld", []):
        posting = find_job_posting(ld)
        if not posting:
            continue

        desc = posting.get("description", "")
        if not desc:
            continue

        desc_clean = clean_description(desc)
        if len(desc_clean) < 50:
            continue

        apply_url = None
        if posting.get("directApply"):
            apply_url = posting.get("url")
        if not apply_url:
            contact = posting.get("applicationContact")
            if isinstance(contact, dict):
                apply_url = contact.get("url")
        if not apply_url:
            apply_url = posting.get("url")

        return {
            "full_description": desc_clean,
            "application_url": apply_url,
        }

    return None


# -- Tier 2: Deterministic pattern matching ----------------------------------

APPLY_SELECTORS = [
    'a[href*="apply"]',
    'a[data-testid*="apply"]',
    'a[class*="apply"]',
    'a[aria-label*="pply"]',
    'button[data-testid*="apply"]',
    'a#apply_button',
    '.postings-btn-wrapper a',
    'a.ashby-job-posting-apply-button',
    '#grnhse_app a[href*="apply"]',
    'a[data-qa="btn-apply"]',
    'a[class*="btn-apply"]',
    'a[class*="apply-btn"]',
    'a[class*="apply-button"]',
]

DESCRIPTION_SELECTORS = [
    '#job-description',
    '#job_description',
    '#jobDescriptionText',
    '.job-description',
    '.job_description',
    '[class*="job-description"]',
    '[class*="jobDescription"]',
    '[data-testid*="description"]',
    '[data-testid="job-description"]',
    '.posting-page .posting-categories + div',
    '#content .posting-page',
    '#app_body .content',
    '#grnhse_app .content',
    '.ashby-job-posting-description',
    '[class*="posting-description"]',
    '[class*="job-detail"]',
    '[class*="jobDetail"]',
    '[class*="job-content"]',
    '[class*="job-body"]',
    '[role="main"] article',
    'main article',
    'article[class*="job"]',
    '.job-posting-content',
]


def extract_apply_url_deterministic(page) -> str | None:
    """Try known CSS patterns for apply buttons/links."""
    for sel in APPLY_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                href = el.get_attribute("href")
                if href and href != "#":
                    return href
                tag = el.evaluate("el => el.tagName.toLowerCase()")
                if tag == "button":
                    parent_href = el.evaluate("el => el.parentElement?.querySelector('a')?.href || null")
                    if parent_href:
                        return parent_href
                    return page.url
        except Exception:
            continue

    try:
        links = page.query_selector_all("a")
        for link in links:
            text = link.inner_text().strip().lower()
            if "apply" in text and len(text) < 50:
                href = link.get_attribute("href")
                if href and href != "#" and "javascript:" not in href:
                    return href
    except Exception:
        pass

    return None


def extract_description_deterministic(page) -> str | None:
    """Try known CSS patterns for the job description block."""
    for sel in DESCRIPTION_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                text = el.inner_text().strip()
                if len(text) >= 100:
                    return clean_description(text)
        except Exception:
            continue

    return None


# -- Tier 3: LLM extraction -------------------------------------------------

DETAIL_EXTRACT_PROMPT = """You are extracting job details from a single job posting page.

PAGE URL: {url}
PAGE TITLE: {title}

Find TWO things in the HTML below:
1. The full job description text (responsibilities, requirements, etc.)
2. The URL of the "Apply" button/link

Rules:
- For description: extract the FULL text. Include all sections (About, Responsibilities, Requirements, etc.)
- For apply URL: find the href of the link/button that starts the application process
- If you cannot find one, set it to null

Return ONLY valid JSON:
{{"full_description": "the complete job description text here", "application_url": "https://..." or null}}

No explanation, no markdown. Keep reasoning under 20 words.

HTML:
{content}"""


def extract_main_content(page) -> str:
    """Extract the main content area, stripped of navigation noise."""
    for sel in ["main", "article", '[role="main"]', "#content", ".content"]:
        try:
            el = page.query_selector(sel)
            if el:
                text_len = len(el.inner_text().strip())
                if text_len > 200:
                    html = el.inner_html()
                    if len(html) < 50000:
                        return clean_content_html(html)
        except Exception:
            continue

    try:
        html = page.evaluate("""
            () => {
                const clone = document.body.cloneNode(true);
                clone.querySelectorAll('nav, header, footer, script, style, noscript, svg, iframe').forEach(el => el.remove());
                return clone.innerHTML;
            }
        """)
        return clean_content_html(html[:50000])
    except Exception:
        return ""


def clean_content_html(html: str) -> str:
    """Clean detail page HTML for LLM consumption."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.select("script, style, noscript, svg, iframe, nav, header, footer"):
        tag.decompose()

    for tag in soup.find_all(True):
        new_attrs: dict = {}
        for attr, val in list(tag.attrs.items()):
            if attr in ("id", "href", "class", "role", "aria-label", "data-testid", "name", "for", "type"):
                if attr == "class":
                    classes = val if isinstance(val, list) else val.split()
                    kept = [c for c in classes if len(c) < 30 and not re.match(r"^[a-z]{1,2}-\d+$", c)]
                    if kept:
                        new_attrs["class"] = " ".join(kept[:3])
                else:
                    new_attrs[attr] = val
            elif attr.startswith("data-") or attr.startswith("aria-"):
                new_attrs[attr] = val
        tag.attrs = new_attrs

    return str(soup)


def extract_with_llm(page, url: str) -> dict:
    """Send focused HTML to LLM for extraction. Fallback tier."""
    content = extract_main_content(page)
    if not content:
        return {"full_description": None, "application_url": None}

    title = ""
    try:
        title = page.title()
    except Exception:
        pass

    prompt = DETAIL_EXTRACT_PROMPT.format(
        url=url,
        title=title,
        content=content[:30000],
    )

    try:
        client = get_client()
        t0 = time.time()
        raw = client.ask(prompt, temperature=0.0, max_tokens=4096)
        elapsed = time.time() - t0
        log.info("LLM: %d chars in, %.1fs", len(prompt), elapsed)

        from applypilot.discovery.smartextract import extract_json
        result = extract_json(raw)
        desc = result.get("full_description")
        apply_url = result.get("application_url")

        if desc:
            desc = clean_description(desc)

        return {"full_description": desc, "application_url": apply_url}
    except Exception as e:
        log.error("LLM ERROR: %s", e)
        return {"full_description": None, "application_url": None}


# -- Description cleaning ---------------------------------------------------

def clean_description(text: str) -> str:
    """Convert HTML description to clean readable text."""
    if not text:
        return ""

    if "<" in text and ">" in text:
        soup = BeautifulSoup(text, "html.parser")
        for br in soup.find_all("br"):
            br.replace_with("\n")
        for tag in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "li", "tr"]):
            tag.insert_before("\n")
            tag.insert_after("\n")
        for li in soup.find_all("li"):
            li.insert_before("- ")
        text = soup.get_text()

    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if line:
            lines.append(line)

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# -- Orchestration -----------------------------------------------------------

SITE_DELAYS = {
    "RemoteOK": 3.0,
    "WelcomeToTheJungle": 2.0,
    "Job Bank Canada": 1.5,
    "CareerJet Canada": 3.0,
    "Hacker News Jobs": 1.0,
    "BuiltIn Remote": 2.0,
}

RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}
PERMANENT_FAILURES = {404, 410, 451}

# Max enrichment retries before giving up and marking a job as permanently failed.
MAX_DETAIL_RETRIES = 5

# Error strings that indicate the job posting is still live but we hit a network/LLM issue.
_RETRIABLE_PATTERNS = (
    "timeout", "LLM error", "Client error",
    "HTTP 408", "HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504",
    "ERR_CONNECTION", "ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED",
    "net::ERR",
)

# Error strings that mean the posting is definitively gone.
_EXPIRED_PATTERNS = ("HTTP 404", "HTTP 410", "HTTP 451")

# Errors that can never succeed regardless of retries.
_PERMANENT_PATTERNS = (
    "no data extracted",
    "manual://",  # Synthetic HN contact-only URLs — not real web pages
)


def _classify_detail_error(error: str, current_retry_count: int) -> tuple[str, str | None]:
    """Classify an enrichment error and compute the next retry timestamp.

    Returns:
        (category, next_retry_at_iso)
        category: 'expired' | 'retriable' | 'permanent'
        next_retry_at_iso: ISO timestamp string, or None if no retry
    """
    if current_retry_count >= MAX_DETAIL_RETRIES:
        return "permanent", None

    if any(p in error for p in _EXPIRED_PATTERNS):
        return "expired", None

    if any(p in error for p in _PERMANENT_PATTERNS):
        return "permanent", None

    if any(p in error for p in _RETRIABLE_PATTERNS):
        # Exponential backoff: 5min, 20min, 80min, ~5h, ~21h
        delay_minutes = min(5 * (4 ** current_retry_count), 24 * 60)
        next_retry = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
        return "retriable", next_retry.isoformat()

    # Unknown error — treat as permanent to avoid infinite retries
    return "permanent", None


def _mark_enrich_result(
    conn,
    url: str,
    *,
    status: str,
    full_description: str | None,
    application_url: str | None,
    error: str | None,
    tier: int | None,
    retry_count: int,
    category: str | None = None,
    next_retry_at: str | None = None,
    now: str | None = None,
) -> None:
    """Persist one enrichment result and emit a state transition.

    Extracted from the inline UPDATE blocks inside ``run_detail_scraper`` so
    that tests can call it without launching a browser.

    ``status`` should be one of: ``"ok"``, ``"partial"``, ``"error"``.
    Retriable errors do NOT emit a transition (job stays in ``discovered``).

    ``category`` and ``next_retry_at`` may be pre-computed by the caller
    (e.g. to log them before writing).  When absent they are derived
    internally via ``_classify_detail_error``.
    """
    assert status in ("ok", "partial", "error"), f"Unexpected status: {status!r}"

    if now is None:
        now = datetime.now(timezone.utc).isoformat()

    if status in ("ok", "partial"):
        conn.execute(
            "UPDATE jobs SET full_description = ?, application_url = ?, "
            "detail_scraped_at = ?, detail_error = NULL, "
            "detail_error_category = NULL, enrich_attempts = 0, "
            "enrich_next_retry_at = NULL WHERE url = ?",
            (full_description, application_url, now, url),
        )
        transition_state(
            conn, url, "enriched",
            reason="description fetched",
            metadata={"chars": len(full_description or ""), "tier": tier},
            force=True,
        )
    else:
        error_msg = error or "unknown"
        if category is None or next_retry_at is None:
            category, next_retry_at = _classify_detail_error(error_msg, retry_count)
        conn.execute(
            "UPDATE jobs SET detail_error = ?, detail_error_category = ?, "
            "enrich_attempts = ?, enrich_next_retry_at = ?, "
            "detail_scraped_at = ? WHERE url = ?",
            (error_msg, category, retry_count + 1, next_retry_at, now, url),
        )
        if category != "retriable":
            transition_state(
                conn, url, "enrich_failed",
                reason=category,
                metadata={"error": error_msg},
                force=True,
            )
        # retriable: no transition — job stays in 'discovered' for retry loop


def scrape_detail_page(page, url: str) -> dict:
    """Full cascade for one detail page."""
    result: dict = {
        "full_description": None,
        "application_url": None,
        "status": "error",
        "tier_used": None,
        "error": None,
    }
    t0 = time.time()

    try:
        resp = page.goto(url, timeout=45000)
        if resp and resp.status in PERMANENT_FAILURES:
            result["error"] = f"HTTP {resp.status}"
            result["elapsed"] = time.time() - t0
            return result
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
    except Exception as e:
        err_str = str(e)
        if "timeout" in err_str.lower():
            result["error"] = "timeout"
        else:
            result["error"] = err_str[:200]
        result["elapsed"] = time.time() - t0
        return result

    intel = collect_detail_intelligence(page)

    # Tier 1: JSON-LD
    json_ld_result = extract_from_json_ld(intel)
    if json_ld_result and json_ld_result.get("full_description"):
        result.update(json_ld_result)
        result["tier_used"] = 1
        if not result.get("application_url"):
            apply = extract_apply_url_deterministic(page)
            if apply:
                result["application_url"] = apply
        result["status"] = "ok" if result.get("application_url") else "partial"
        result["elapsed"] = time.time() - t0
        return result

    # Tier 2: Deterministic CSS
    desc = extract_description_deterministic(page)
    apply = extract_apply_url_deterministic(page)

    if desc:
        result["full_description"] = desc
        result["application_url"] = apply
        result["tier_used"] = 2
        result["status"] = "ok" if apply else "partial"
        result["elapsed"] = time.time() - t0
        return result

    tier2_apply = apply

    # Tier 3: LLM
    llm_result = extract_with_llm(page, url)
    result["full_description"] = llm_result.get("full_description")
    result["application_url"] = llm_result.get("application_url") or tier2_apply
    result["tier_used"] = 3

    if result.get("full_description"):
        result["status"] = "ok" if result.get("application_url") else "partial"
    elif result.get("application_url"):
        result["status"] = "partial"
    else:
        result["status"] = "error"
        result["error"] = "no data extracted"

    result["elapsed"] = time.time() - t0
    return result


def scrape_site_batch(
    conn: sqlite3.Connection | None,
    site: str,
    jobs: list[tuple],
    delay: float = 2.0,
    max_jobs: int | None = None,
) -> dict:
    """Process all jobs for one site using shared browser context.

    If conn is None, creates its own DB connection.
    """
    stats: dict = {"processed": 0, "ok": 0, "partial": 0, "error": 0, "tiers": {1: 0, 2: 0, 3: 0}}

    if max_jobs:
        jobs = jobs[:max_jobs]

    if not jobs:
        return stats

    own_conn = conn is None
    if own_conn:
        conn = init_db()

    now = datetime.now(timezone.utc).isoformat()

    try:
        with sync_playwright() as p:
            launch_opts: dict = {"headless": True}
            if _PROXY_CONFIG:
                launch_opts["proxy"] = _PROXY_CONFIG["playwright"]
            browser = p.chromium.launch(**launch_opts)
            context = browser.new_context(user_agent=UA)
            context.add_init_script(_STEALTH_INIT_SCRIPT)
            page = context.new_page()

            for i, (url, title) in enumerate(jobs):
                log.info("[%d/%d] %s", i + 1, len(jobs), title[:50] if title else url[:50])

                result = scrape_detail_page(page, url)
                stats["processed"] += 1

                tier = result.get("tier_used")
                status = result["status"]
                elapsed = result.get("elapsed", 0)

                if tier:
                    stats["tiers"][tier] = stats["tiers"].get(tier, 0) + 1

                tier_str = f"T{tier}" if tier else "--"
                desc_len = len(result.get("full_description") or "")
                apply_str = "yes" if result.get("application_url") else "no"
                err_str = f" | err={result.get('error')}" if result.get("error") else ""

                log.info("  %s | %s | desc=%s chars | apply=%s | %.1fs%s",
                         status, tier_str, f"{desc_len:,}", apply_str, elapsed, err_str)

                if status in ("ok", "partial"):
                    stats[status] += 1
                    _mark_enrich_result(
                        conn, url,
                        status=status,
                        full_description=result.get("full_description"),
                        application_url=result.get("application_url"),
                        error=None,
                        tier=tier,
                        retry_count=0,
                        now=now,
                    )
                else:
                    stats["error"] += 1
                    # Fetch current retry count before classifying so the
                    # helper can compute the right backoff window.
                    row = conn.execute(
                        "SELECT COALESCE(enrich_attempts, 0) FROM jobs WHERE url = ?", (url,)
                    ).fetchone()
                    retry_count = row[0] if row else 0
                    error_msg = result.get("error", "unknown")
                    _cat, _next = _classify_detail_error(error_msg, retry_count)
                    log.info("  error_category=%s retry=%d/%d next=%s",
                             _cat, retry_count + 1, MAX_DETAIL_RETRIES,
                             _next or "never")
                    _mark_enrich_result(
                        conn, url,
                        status=status,
                        full_description=None,
                        application_url=None,
                        error=error_msg,
                        tier=tier,
                        retry_count=retry_count,
                        category=_cat,
                        next_retry_at=_next,
                        now=now,
                    )

                conn.commit()

                if i < len(jobs) - 1:
                    time.sleep(delay)

            browser.close()
    finally:
        if own_conn:
            conn.close()

    return stats


def _run_detail_scraper(
    conn: sqlite3.Connection,
    sites: list[str] | None = None,
    max_per_site: int | None = None,
    workers: int = 1,
) -> dict:
    """Groups pending jobs by site and processes each batch.

    Sequential by default. When workers > 1, processes multiple site batches
    in parallel using ThreadPoolExecutor (each thread gets its own browser
    and DB connection).

    Returns aggregate stats dict.
    """
    skip_filter = " AND ".join(f"site != '{s}'" for s in SKIP_DETAIL_SITES)
    where = (
        f"WHERE ({skip_filter}) AND ("
        "  detail_scraped_at IS NULL "
        "  OR (detail_error_category = 'retriable' "
        "      AND (enrich_next_retry_at IS NULL OR enrich_next_retry_at <= datetime('now')))"
        ")"
    )
    rows = conn.execute(
        f"SELECT url, title, site FROM jobs {where} ORDER BY site, discovered_at DESC"
    ).fetchall()

    if not rows:
        log.info("No pending jobs to scrape.")
        return {"processed": 0, "ok": 0, "partial": 0, "error": 0}

    site_jobs: dict[str, list[tuple]] = {}
    for row in rows:
        url, title, site = row[0], row[1], row[2]
        if sites and site not in sites:
            continue
        site_jobs.setdefault(site, []).append((url, title))

    # Shuffle site order so enrichment doesn't always process the same sources first
    site_items = list(site_jobs.items())
    random.shuffle(site_items)
    site_jobs = dict(site_items)

    log.info("Pending: %d jobs across %d sites (workers=%d)", len(rows), len(site_jobs), workers)
    for site, jobs in site_jobs.items():
        log.info("  %s: %d jobs", site, len(jobs))

    known_order = [
        "RemoteOK", "Job Bank Canada", "BuiltIn Remote",
        "WelcomeToTheJungle", "CareerJet Canada", "Hacker News Jobs",
    ]
    order = [s for s in known_order if s in site_jobs]
    order += [s for s in sorted(site_jobs.keys()) if s not in order]

    total_stats: dict = {"processed": 0, "ok": 0, "partial": 0, "error": 0, "tiers": {1: 0, 2: 0, 3: 0}}

    def _merge_stats(stats: dict) -> None:
        for k in ("processed", "ok", "partial", "error"):
            total_stats[k] += stats[k]
        for t, count in stats["tiers"].items():
            total_stats["tiers"][t] = total_stats["tiers"].get(t, 0) + count

    if workers > 1 and len(order) > 1:
        # Parallel mode: each site batch runs in its own thread with its own
        # DB connection (conn=None tells scrape_site_batch to create one)
        def _scrape_site(site: str) -> dict:
            jobs = site_jobs[site]
            delay = SITE_DELAYS.get(site, 2.0)
            log.info("%s -- %d jobs (delay=%.1fs)", site, len(jobs), delay)
            stats = scrape_site_batch(None, site, jobs, delay=delay, max_jobs=max_per_site)
            log.info("%s summary: %d ok, %d partial, %d error | T1=%d T2=%d T3=%d",
                     site, stats["ok"], stats["partial"], stats["error"],
                     stats["tiers"].get(1, 0), stats["tiers"].get(2, 0), stats["tiers"].get(3, 0))
            return stats

        with ThreadPoolExecutor(max_workers=min(workers, len(order))) as pool:
            futures = {pool.submit(_scrape_site, site): site for site in order}
            for future in as_completed(futures):
                _merge_stats(future.result())
    else:
        # Sequential mode (default)
        for site in order:
            jobs = site_jobs[site]
            delay = SITE_DELAYS.get(site, 2.0)
            log.info("%s -- %d jobs (delay=%.1fs)", site, len(jobs), delay)

            stats = scrape_site_batch(conn, site, jobs, delay=delay, max_jobs=max_per_site)
            _merge_stats(stats)

            log.info("Site summary: %d ok, %d partial, %d error | T1=%d T2=%d T3=%d",
                     stats["ok"], stats["partial"], stats["error"],
                     stats["tiers"].get(1, 0), stats["tiers"].get(2, 0), stats["tiers"].get(3, 0))

    log.info("TOTAL: %d processed | %d ok | %d partial | %d error",
             total_stats["processed"], total_stats["ok"], total_stats["partial"], total_stats["error"])
    log.info("Tier distribution: T1=%d T2=%d T3=%d",
             total_stats["tiers"].get(1, 0), total_stats["tiers"].get(2, 0), total_stats["tiers"].get(3, 0))

    llm_calls = total_stats["tiers"].get(3, 0)
    total = total_stats["processed"]
    if total > 0:
        savings = ((total - llm_calls) / total) * 100
        log.info("LLM calls: %d/%d (%.0f%% saved)", llm_calls, total, savings)

    return total_stats


# -- Streaming detail scraper (for sequential pipeline) ----------------------

def stream_detail(
    upstream_done,
    my_done,
    proxy_str: str | None = None,
    poll_interval: float = 5.0,
) -> None:
    """Streaming detail scraper: polls DB for un-scraped jobs, scrapes sites sequentially.

    Args:
        upstream_done: Event set when discover+extract done. None = run once.
        my_done: Event to set when this stage completes.
        proxy_str: Proxy in host:port:user:pass format.
        poll_interval: Seconds to sleep when no pending jobs found.
    """
    if proxy_str:
        set_proxy(proxy_str)

    conn = init_db()

    url_stats = resolve_all_urls(conn)
    log.info("URL resolution: %d resolved, %d absolute",
             url_stats['resolved'], url_stats['already_absolute'])

    total_ok = 0
    total_err = 0
    t0 = time.time()

    try:
        while True:
            skip_filter = " AND ".join(f"site != '{s}'" for s in SKIP_DETAIL_SITES)
            rows = conn.execute(
                "SELECT url, title, site FROM jobs "
                f"WHERE ({skip_filter}) AND ("
                "  detail_scraped_at IS NULL "
                "  OR (detail_error_category = 'retriable' "
                "      AND (enrich_next_retry_at IS NULL OR enrich_next_retry_at <= datetime('now')))"
                ") "
                "ORDER BY site, discovered_at DESC LIMIT 200"
            ).fetchall()

            if rows:
                site_jobs: dict[str, list[tuple]] = {}
                for row in rows:
                    url, title, site = row[0], row[1], row[2]
                    site_jobs.setdefault(site, []).append((url, title))

                site_items = list(site_jobs.items())
                random.shuffle(site_items)
                site_jobs = dict(site_items)

                for site, jobs in site_jobs.items():
                    delay = SITE_DELAYS.get(site, 2.0)
                    log.info("%s: %d jobs (delay=%.1fs)", site, len(jobs), delay)

                    try:
                        stats = scrape_site_batch(conn, site, jobs, delay=delay)
                        total_ok += stats["ok"] + stats["partial"]
                        total_err += stats["error"]
                        log.info("%s: %d ok, %d partial, %d error",
                                 site, stats['ok'], stats['partial'], stats['error'])
                    except Exception as e:
                        log.error("%s: CRASHED: %s", site, e)

            upstream_finished = upstream_done is None or upstream_done.is_set()
            if upstream_finished and not rows:
                break
            if not rows:
                time.sleep(poll_interval)
    finally:
        elapsed = time.time() - t0
        if total_ok or total_err:
            log.info("DONE: %d ok, %d errors in %.1fs", total_ok, total_err, elapsed)
        conn.close()
        my_done.set()


# -- Public entry point ------------------------------------------------------

def run_enrichment(limit: int = 100, workers: int = 1) -> dict:
    """Main entry point for detail page enrichment.

    Fetches pending jobs from the database (those without full_description),
    resolves relative URLs, then runs the three-tier extraction cascade on
    each detail page.

    Args:
        limit: Maximum number of jobs per site to process.
        workers: Number of parallel threads for site batch processing. Default 1 (sequential).

    Returns:
        Dict with stats: processed, ok, partial, error, tiers.
    """
    conn = init_db()

    # URL resolution first
    url_stats = resolve_all_urls(conn)
    log.info("URL resolution: %d resolved, %d absolute, %d failed",
             url_stats["resolved"], url_stats["already_absolute"], url_stats["failed"])

    # WTTJ special handling
    wttj_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE site = 'WelcomeToTheJungle'"
    ).fetchone()[0]
    if wttj_count > 0:
        sample = conn.execute(
            "SELECT url FROM jobs WHERE site = 'WelcomeToTheJungle' LIMIT 1"
        ).fetchone()
        if sample and not sample[0].startswith("http"):
            updated = resolve_wttj_urls(conn)
            log.info("WTTJ: %d URLs updated", updated)

    # Run the detail scraper
    stats = _run_detail_scraper(conn, max_per_site=limit, workers=workers)

    return stats
