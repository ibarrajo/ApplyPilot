"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from applypilot.config import RESUME_PATH, load_profile
from applypilot.database import get_connection, get_jobs_by_stage, write_with_retry
from applypilot.llm import get_client

log = logging.getLogger(__name__)


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT_TEMPLATE = """You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.

THE CANDIDATE: {candidate_summary}

⚠️ GEOGRAPHY CHECK — DO THIS FIRST, BEFORE ANYTHING ELSE:
The candidate is US-based (Seattle, WA). Any role restricted to non-US geography is INELIGIBLE.
- EMEA, APAC, EU only, UK only, Europe only, Germany only, India only in the TITLE or LOCATION → SCORE 2, STOP.
- Description says "Remote (Europe)", "Remote - UK", "Europe Time Zone", "CET timezone", "based in Europe/UK/India" → SCORE 2, STOP.
- Title contains "(m/f/d)" or "(m/w/d)" (German job suffix) → SCORE 2, STOP.
- "US remote" or "global remote" or no geographic restriction → proceed to scoring below.
This is non-negotiable. A perfect tech stack match for an EMEA-only role is still SCORE 2.

SCORING CRITERIA:
- 10: Near-perfect IC engineering match. The role is a software/platform/infrastructure engineer position requiring the candidate's exact stack (Go/Kotlin/Python/Java, distributed systems, K8s). Seniority aligns (Senior/Staff/Principal). The candidate would be a top-tier applicant with minimal gaps.
- 9: Excellent engineering match. Strong alignment on tech stack and seniority, with 1-2 gaps in secondary skills or slightly different domain.
- 7-8: Good engineering match. Candidate has most required technical skills. Minor gaps in specific frameworks or domain experience, easily bridged.
- 5-6: Moderate match. The role is engineering but uses a different primary stack, or there's a seniority mismatch (e.g., junior role or executive-only role with no IC component).
- 3-4: Weak match. Engineering role but wrong specialization (frontend-only, mobile, ML research, data science), or a non-engineering role with some technical overlap.
- 1-2: Poor match. Non-engineering role (recruiting, design, marketing, product management, sales), completely different field, OR non-US geographic restriction.

ADDITIONAL RULES:
- Non-engineering roles (recruiters, designers, PMs, marketing, sales, executive search) score 1-2 MAX regardless of seniority or domain.
- Roles requiring a specific language the candidate doesn't know (Rust, C++, Ruby, Scala, Clojure) as the PRIMARY requirement score 4-6 max depending on transferability.
- "CTO" or "VP Engineering" roles that are purely management with no IC engineering component score 5-6 max.
- LOCATION is N/A: check the description for any office/city requirement. If the description implies onsite in a specific US city outside Seattle/Bellevue/Kirkland/Redmond, cap at 7.
- Distinguish REQUIRED skills from NICE-TO-HAVE. Only penalize for missing required skills.
- Value transferable experience: workflow orchestration, distributed systems, microservices, developer platforms transfer across domains.

You MUST include all three lines below. Do not skip REASONING.

SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences explaining the score, what matched well, and any gaps]"""


# ── Rule-based pre-filter (catches obvious ineligible before LLM call) ─────

# Patterns that make a job ineligible regardless of tech stack.
# Checked against title + location field only (not full description, to avoid false positives).
_INELIGIBLE_TITLE_PATTERNS = re.compile(
    r'\bEMEA\b'              # "Senior Dev Advocate, EMEA"
    r'|\bAPAC\b'             # "Engineering Manager APAC"
    r'|\bEU[- ]only\b'       # "Remote - EU only"
    r'|\bUK[- ]only\b'
    r'|\bEurope[- ]only\b'
    r'|\(m/[fw]/d\)'         # German job title suffix (m/f/d) or (m/w/d)
    r'|\bm/[fw]/d\b',
    re.IGNORECASE,
)

# Patterns checked against the location field specifically
_INELIGIBLE_LOCATION_PATTERNS = re.compile(
    r'\bEMEA\b'
    r'|\bAPAC\b'
    r'|\bEurope\b'
    r'|\bGermany\b'
    r'|\bNetherlands\b'
    r'|\bFrance\b'
    r'|\bSpain\b'
    r'|\bItaly\b'
    r'|\bIndia\b'
    r'|\bAustralia\b'
    r'|\bSingapore\b'
    r'|\bPoland\b'
    r'|\bUkraine\b'
    r'|\bCzech\b',
    re.IGNORECASE,
)

# Also check for EMEA/geographic restriction in first 800 chars of description
_INELIGIBLE_DESC_PATTERNS = re.compile(
    r'Remote\s*[\(\-]\s*(EMEA|Europe|EU|UK|Germany|India)'
    r'|EMEA\s*(only|region|remote|based)'
    r'|(Europe|European)\s*(only|Time\s*Zone|timezone|based|remote)'
    r'|based\s+in\s+(Europe|UK|Germany|India|Netherlands)'
    r'|CET\s+timezone'
    r'|GMT[+\-]\d+\s+timezone',
    re.IGNORECASE,
)


def _check_ineligible(job: dict) -> str | None:
    """Return an ineligibility reason if the job is obviously non-US, else None.

    Checked before the LLM call to save tokens and ensure consistency.
    Only uses title, location field, and first 800 chars of description
    to avoid false positives from US companies mentioning global offices.
    """
    title = job.get("title") or ""
    location = job.get("location") or ""
    desc_head = (job.get("full_description") or "")[:800]

    if _INELIGIBLE_TITLE_PATTERNS.search(title):
        return f"non-US geography in title: {title}"
    if location and _INELIGIBLE_LOCATION_PATTERNS.search(location):
        return f"non-US location field: {location}"
    if _INELIGIBLE_DESC_PATTERNS.search(desc_head):
        return "non-US geography in description"
    return None


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    score = 0
    keywords = ""
    reasoning = response

    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("SCORE:"):
            try:
                score = int(re.search(r"\d+", line).group())
                score = max(1, min(10, score))
            except (AttributeError, ValueError):
                score = 0
        elif line.startswith("KEYWORDS:"):
            keywords = line.replace("KEYWORDS:", "").strip()
        elif line.startswith("REASONING:"):
            reasoning = line.replace("REASONING:", "").strip()

    return {"score": score, "keywords": keywords, "reasoning": reasoning}


def _build_candidate_summary(profile: dict) -> str:
    """Build a candidate summary string from profile for the scoring prompt."""
    exp = profile.get("experience", {})
    boundary = profile.get("skills_boundary", {})
    years = exp.get("years_of_experience_total", "several")
    current_title = exp.get("current_job_title", "Software Engineer")
    target = exp.get("target_role", "Software Engineer")
    languages = boundary.get("languages", [])
    platforms = boundary.get("platforms", [])
    parts = [f"{current_title} with {years} years experience."]
    if languages:
        parts.append(f"Primary stack: {', '.join(languages[:8])}.")
    if platforms:
        parts.append(f"Platforms: {', '.join(platforms[:6])}.")
    parts.append(f"Targets: {target}.")
    return " ".join(parts)


def score_job(resume_text: str, job: dict, profile: dict | None = None) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    if profile is None:
        profile = load_profile()

    # Rule-based pre-filter: catch obvious non-US ineligible jobs before LLM call
    ineligible_reason = _check_ineligible(job)
    if ineligible_reason:
        log.info("Pre-filter INELIGIBLE (score=2): %s — %s", (job.get("title") or "?")[:60], ineligible_reason)
        return {
            "score": 2,
            "keywords": "",
            "reasoning": f"Ineligible: {ineligible_reason}. Candidate is US-based.",
        }

    try:
        candidate_summary = _build_candidate_summary(profile)
        score_prompt = SCORE_PROMPT_TEMPLATE.format(candidate_summary=candidate_summary)

        job_text = (
            f"TITLE: {job['title']}\n"
            f"COMPANY: {job['site']}\n"
            f"LOCATION: {job.get('location', 'N/A')}\n\n"
            f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
        )

        messages = [
            {"role": "system", "content": score_prompt},
            {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
        ]

        client = get_client()
        response = client.chat(messages, max_tokens=8192, temperature=0.2)
        return _parse_score_response(response)
    except Exception as e:
        log.error("LLM error scoring job '%s': %s", (job or {}).get("title") or "?", e)
        return {"score": None, "keywords": "", "reasoning": "", "error": f"LLM error: {e}"}


MAX_SCORE_RETRIES = 5


def _score_backoff_minutes(retry_count: int) -> int:
    """Exponential backoff for scoring retries: 5, 20, 80, ~5h, ~21h."""
    return min(5 * (4 ** retry_count), 24 * 60)


def _flush_score_batch(conn, batch: list[dict], now: str) -> None:
    """Write a batch of scoring results to the DB.

    On success (score is not None): writes fit_score, clears score_error.
    On failure (score is None): leaves fit_score NULL, writes score_error + backoff.
    Jobs that have already hit MAX_SCORE_RETRIES stay unscored indefinitely (manual rescue needed).
    """
    for r in batch:
        if r["score"] is not None:
            conn.execute(
                "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ?, "
                "score_error = NULL, score_retry_count = 0, score_next_retry_at = NULL "
                "WHERE url = ?",
                (r["score"], f"{r['keywords']}\n{r['reasoning']}", now, r["url"]),
            )
        else:
            # LLM failure — keep fit_score NULL so it stays in pending_score
            row = conn.execute(
                "SELECT COALESCE(score_retry_count, 0) FROM jobs WHERE url = ?", (r["url"],)
            ).fetchone()
            retry_count = row[0] if row else 0
            if retry_count >= MAX_SCORE_RETRIES:
                # Give up — write score_error but don't schedule another retry
                conn.execute(
                    "UPDATE jobs SET score_error = ?, score_retry_count = ?, "
                    "score_next_retry_at = NULL, scored_at = ? WHERE url = ?",
                    (r["error"], retry_count + 1, now, r["url"]),
                )
            else:
                delay = _score_backoff_minutes(retry_count)
                next_retry = (
                    datetime.now(timezone.utc) + timedelta(minutes=delay)
                ).isoformat()
                conn.execute(
                    "UPDATE jobs SET score_error = ?, score_retry_count = ?, "
                    "score_next_retry_at = ?, scored_at = ? WHERE url = ?",
                    (r["error"], retry_count + 1, next_retry, now, r["url"]),
                )
                log.info("  score retry %d/%d scheduled in %d min for %s",
                         retry_count + 1, MAX_SCORE_RETRIES, delay, r["url"][:60])


def run_scoring(limit: int = 0, rescore: bool = False, workers: int = 1,
                max_age_days: int | None = None) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).
        workers: Parallel LLM threads (default 1 = sequential).

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    if rescore:
        query = "SELECT * FROM jobs WHERE full_description IS NOT NULL"
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    else:
        # Note: get_jobs_by_stage now applies a 14-day discovered_at filter by
        # default (config.DEFAULTS["max_job_age_days"]). Pass max_age_days=0
        # to disable.
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score",
                                 max_age_days=max_age_days, limit=limit)

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    log.info("Scoring %d jobs (workers=%d)...", len(jobs), workers)
    t0 = time.time()
    completed = 0
    errors = 0
    batch_size = 25  # Commit every N jobs so downstream stages see results sooner
    batch: list[dict] = []

    def _score_one(job: dict) -> dict:
        try:
            result = score_job(resume_text, job)
            result["url"] = job["url"]
        except Exception as e:
            log.error("Unexpected error scoring '%s': %s", (job or {}).get("title") or "?", e)
            result = {
                "score": None, "keywords": "", "reasoning": "",
                "error": f"Unexpected: {e}", "url": (job or {}).get("url", ""),
            }
        return result

    def _flush_and_log(batch: list[dict], completed: int) -> list[dict]:
        now = datetime.now(timezone.utc).isoformat()
        try:
            write_with_retry(conn, _flush_score_batch, conn, batch, now)
        except Exception as flush_err:
            log.exception("Batch flush failed (batch of %d): %s", len(batch), flush_err)
        log.info("Committed batch of %d scores to DB (%d/%d total)", len(batch), completed, len(jobs))
        return []

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_score_one, job): job for job in jobs}
            for future in as_completed(futures):
                job = futures[future]
                result = future.result()
                completed += 1
                if result["score"] is None:
                    errors += 1
                batch.append(result)
                log.info(
                    "[%d/%d] score=%s  %s",
                    completed, len(jobs),
                    result["score"] if result["score"] is not None else "ERR",
                    (job.get("title") or "")[:60],
                )
                if len(batch) >= batch_size:
                    batch = _flush_and_log(batch, completed)
    else:
        for job in jobs:
            result = _score_one(job)
            completed += 1
            if result["score"] is None:
                errors += 1
            batch.append(result)
            log.info(
                "[%d/%d] score=%s  %s",
                completed, len(jobs),
                result["score"] if result["score"] is not None else "ERR",
                (job.get("title") or "")[:60],
            )
            if len(batch) >= batch_size:
                batch = _flush_and_log(batch, completed)

    # Flush remaining
    if batch:
        now = datetime.now(timezone.utc).isoformat()
        try:
            write_with_retry(conn, _flush_score_batch, conn, batch, now)
        except Exception as flush_err:
            log.exception("Final batch flush failed (batch of %d): %s", len(batch), flush_err)

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs (%.1f jobs/sec)", completed, elapsed, completed / elapsed if elapsed > 0 else 0)

    # Score distribution
    try:
        dist = conn.execute("""
            SELECT fit_score, COUNT(*) FROM jobs
            WHERE fit_score IS NOT NULL
            GROUP BY fit_score ORDER BY fit_score DESC
        """).fetchall()
        distribution = [(row[0], row[1]) for row in dist]
    except Exception as dist_err:
        log.exception("Distribution query failed: %s", dist_err)
        distribution = []

    return {
        "scored": completed,
        "errors": errors,
        "elapsed": elapsed,
        "distribution": distribution,
    }
