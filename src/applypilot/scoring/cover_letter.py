"""Cover letter generation: LLM-powered, profile-driven, with validation.

Generates concise, engineering-voice cover letters tailored to specific job
postings. All personal data (name, skills, achievements) comes from the user's
profile at runtime. No hardcoded personal information.
"""

import hashlib
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from applypilot.config import COVER_LETTER_DIR, RESUME_PATH, load_profile
from applypilot.database import get_connection, transition_state, write_with_retry
from applypilot.llm import get_client
from applypilot.scoring.validator import (
    sanitize_text,
    validate_cover_letter,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # max cross-run retries before giving up


# ── Prompt Builder (profile-driven) ──────────────────────────────────────

def _build_cover_letter_prompt(profile: dict) -> str:
    """Build the cover letter system prompt from the user's profile.

    All personal data, skills, and sign-off name come from the profile.
    """
    personal = profile.get("personal", {})
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Preferred name for the sign-off (falls back to full name)
    sign_off_name = personal.get("preferred_name") or personal.get("full_name", "")

    # Flatten all allowed skills
    all_skills: list[str] = []
    for items in boundary.values():
        if isinstance(items, list):
            all_skills.extend(items)
    skills_str = ", ".join(all_skills) if all_skills else "the tools listed in the resume"

    # Real metrics from resume_facts
    real_metrics = resume_facts.get("real_metrics", [])
    preserved_projects = resume_facts.get("preserved_projects", [])

    # Build achievement examples for the prompt
    projects_hint = ""
    if preserved_projects:
        projects_hint = f"\nKnown projects to reference: {', '.join(preserved_projects)}"

    metrics_hint = ""
    if real_metrics:
        metrics_hint = f"\nReal metrics to use: {', '.join(real_metrics)}"

    return f"""Write a cover letter for {sign_off_name}. The goal is to get an interview.

STRUCTURE: 4 paragraphs. TARGET 300-400 words; MINIMUM 260 words (Jobscan 3.4x interview-rate sweet spot is 250-400). Letters under 260 words get rejected automatically.

PARAGRAPH 1 — HOOK (4-6 sentences, ~80 words): Open with a specific thing YOU built that solves THEIR problem. Identify the problem they're hiring to solve (infer from the job description) and name the work you've done that directly addresses it. Include enough context that the reader understands the scope and impact. Not "I'm excited about this role." Not "This role aligns with my experience." Start with the work.

PARAGRAPH 2 — EVIDENCE (4-6 sentences, ~120 words): Pick 2 achievements from the resume that are MOST relevant to THIS job. For each, name the problem, the concrete action you took (specific tools, architecture decisions), and the quantified outcome. Use numbers. Frame each as solving their problem, not listing your accomplishments.{projects_hint}{metrics_hint}

PARAGRAPH 3 — COMPANY FIT (3-4 sentences, ~70 words): Reference one specific thing about the company from the job description (a product, a technical challenge, a team structure). Connect it to your experience with a concrete parallel, not a generic nod. Show you've read the posting and that you've solved a similar shape of problem.

PARAGRAPH 4 — CLOSE (2 sentences, ~30 words): Short CTA pointing to a next step plus the sign-off. "Happy to walk through the migration details or the on-call setup." Specific, not generic. Then sign off.

BANNED WORDS/PHRASES (using ANY of these = instant rejection):
"resonated", "aligns with", "passionate", "eager", "eager to", "excited to apply", "I am confident",
"I believe", "proven track record", "strong track record", "cutting-edge", "innovative", "innovative solutions",
"leverage", "leveraging", "robust", "driven", "dedicated", "committed to",
"I look forward to hearing from you", "great fit", "unique opportunity",
"commitment to excellence", "dynamic team", "fast-paced environment",
"I am writing to express", "caught my eye", "caught my attention"

BANNED PUNCTUATION: No em dashes. Use commas or periods.

VOICE:
- Write like a real engineer emailing someone they respect. Not formal, not casual. Just direct.
- NEVER narrate or explain what you're doing. BAD: "This demonstrates my commitment to X." GOOD: Just state the fact and move on.
- NEVER hedge. BAD: "might address some of your challenges." GOOD: "solves the same problem your team is facing."
- NEVER use "Also," to start a sentence. NEVER use "Furthermore," or "Additionally,".
- Every sentence should contain either a number, a tool name, or a specific outcome. If it doesn't, cut it.
- Read it out loud. If it sounds like a robot wrote it, rewrite it.

ADDITIONAL BANNED PHRASES:
"This demonstrates", "This reflects", "This showcases", "This shows",
"This experience translates", "which aligns with", "which is relevant to",
"as demonstrated by", "showing experience with", "reflecting the need for",
"which directly addresses", "I have experience with",
"Also,", "Furthermore,", "Additionally,", "Moreover,"

FABRICATION = INSTANT REJECTION:
The candidate's real tools are ONLY: {skills_str}.
Do NOT mention ANY tool not in this list. If the job asks for tools not listed, talk about the work you did, not the tools.

Sign off: just "{sign_off_name}"

Output ONLY the letter. Start with "Dear Hiring Manager," end with the name."""


# ── Core Generation ──────────────────────────────────────────────────────

def generate_cover_letter(
    resume_text: str, job: dict, profile: dict, max_retries: int = 3
) -> str:
    """Generate a cover letter with fresh context on each retry + auto-sanitize.

    Same design as tailor_resume: fresh conversation per attempt, issues noted
    in the prompt, no conversation history stacking.

    Args:
        resume_text: The candidate's resume text (base or tailored).
        job: Job dict with title, site, location, full_description.
        profile: User profile dict.
        max_retries: Maximum retry attempts.

    Returns:
        The cover letter text (best attempt even if validation failed).
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    avoid_notes: list[str] = []
    letter = ""
    client = get_client(quality=True)
    cl_prompt_base = _build_cover_letter_prompt(profile)

    for attempt in range(max_retries + 1):
        # Fresh conversation every attempt
        prompt = cl_prompt_base
        if avoid_notes:
            prompt += "\n\n## AVOID THESE ISSUES:\n" + "\n".join(
                f"- {n}" for n in avoid_notes[-5:]
            )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": (
                f"RESUME:\n{resume_text}\n\n---\n\n"
                f"TARGET JOB:\n{job_text}\n\n"
                "Write the cover letter:"
            )},
        ]

        letter = client.chat(messages, max_tokens=8192, temperature=0.7)
        letter = sanitize_text(letter)  # auto-fix em dashes, smart quotes

        validation = validate_cover_letter(letter)
        if validation["passed"]:
            return letter

        avoid_notes.extend(validation["errors"])
        log.debug(
            "Cover letter attempt %d/%d failed: %s",
            attempt + 1, max_retries + 1, validation["errors"],
        )

    return letter  # last attempt even if failed


# ── Batch Entry Point ────────────────────────────────────────────────────

def _cover_one_job(job: dict, resume_text: str, profile: dict, doc_format: str = "docx") -> dict:
    """Generate cover letter for a single job. Safe to call from multiple threads."""
    from applypilot.scoring.tailor import _name_parts, _extract_keywords
    letter = generate_cover_letter(resume_text, job, profile)

    # Filename: FirstName_LastName_JobTitle_hash_CL.{ext} (Jobscan §3).
    first, last = _name_parts(profile)
    safe_title = re.sub(r"[^\w\s-]", "", job.get("title") or "untitled")[:50].strip().replace(" ", "_")
    url_hash = hashlib.md5(job["url"].encode()).hexdigest()[:8]
    if first and last:
        prefix = f"{first}_{last}_{safe_title}_{url_hash}"
    elif first:
        prefix = f"{first}_{safe_title}_{url_hash}"
    else:
        safe_site = re.sub(r"[^\w\s-]", "", job["site"])[:20].strip().replace(" ", "_")
        prefix = f"{safe_site}_{safe_title}_{url_hash}"

    cl_path = COVER_LETTER_DIR / f"{prefix}_CL.txt"
    cl_path.write_text(letter, encoding="utf-8")

    doc_path = None
    try:
        from applypilot.scoring.pdf import convert_to_pdf
        personal = profile.get("personal", {})
        full_name = personal.get("full_name") or personal.get("preferred_name") or ""
        job_title = (job.get("title") or "").strip()[:150]
        site = (job.get("site") or "").strip()[:80]
        cl_metadata = {
            "title": f"Cover Letter — {full_name} for {job_title}" if full_name else f"Cover Letter — {job_title}",
            "subject": job_title,
            "author": full_name,
            "category": "Cover Letter",
            "keywords": _extract_keywords(job, profile),
            "comments": (
                f"Cover letter for: {job_title}\n"
                f"Source: {site}\n"
                f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
            ),
        }
        doc_path = str(convert_to_pdf(cl_path, doc_format=doc_format, metadata=cl_metadata))
    except Exception:
        log.debug("Document generation failed for %s", cl_path, exc_info=True)

    return {
        "url": job["url"],
        "path": str(cl_path),
        "pdf_path": doc_path,
        "title": job["title"],
        "site": job["site"],
    }


def _mark_cover_result(
    conn,
    url: str,
    path: str | None,
    *,
    error: str | None = None,
    now: str | None = None,
) -> None:
    """Persist one cover letter result and emit a state transition.

    Extracted from ``_flush_cover_results`` so tests can call it directly.
    Transitions to ``ready_to_apply`` on success, ``cover_failed`` on failure.
    """
    from datetime import datetime, timezone as _tz

    if now is None:
        now = datetime.now(_tz.utc).isoformat()

    if path:
        conn.execute(
            "UPDATE jobs SET cover_letter_path=?, cover_letter_at=?, "
            "cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
            (path, now, url),
        )
        transition_state(
            conn, url, "ready_to_apply",
            reason="cover letter done",
            metadata={"path": path},
            force=True,
        )
    else:
        conn.execute(
            "UPDATE jobs SET cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
            (url,),
        )
        transition_state(
            conn, url, "cover_failed",
            reason="cover generation failed",
            metadata={"error": error},
            force=True,
        )


def run_cover_letters(min_score: int | None = None, limit: int = 20, workers: int = 1,
                      doc_format: str = "docx", max_age_days: int | None = None) -> dict:
    """Generate cover letters for high-scoring jobs that have tailored resumes.

    Args:
        min_score: Minimum fit_score threshold (default from config).
        limit: Maximum jobs to process.
        workers: Parallel LLM threads (default 1 = sequential).
        doc_format: Output document format — "docx" (default) or "pdf".
        max_age_days: Skip jobs older than this (default from config).

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

    # Note: get_jobs_by_stage applies a 14-day discovered_at filter by default
    # (config.DEFAULTS["max_job_age_days"]). Pass max_age_days=0 to disable.
    jobs = get_jobs_by_stage(conn=conn, stage="pending_cover",
                             min_score=min_score, max_age_days=max_age_days,
                             limit=limit)

    # Per-company cover-letter cap (mirrors tailor cap in tailor.py).
    # Keys resolved from `company`, with `site` fallback for direct-employer
    # scrapers. Aggregator sites (LinkedIn, Indeed, etc.) are exempt.
    from applypilot.scoring.tailor import resolve_company_key
    cap = DEFAULTS["max_tailored_per_company"]

    existing_rows = conn.execute("""
        SELECT LOWER(company) AS key, COUNT(*) AS n
        FROM jobs
        WHERE cover_letter_path IS NOT NULL
          AND company IS NOT NULL AND TRIM(company) != ''
          AND discovered_at > datetime('now', ?)
        GROUP BY key
        UNION ALL
        SELECT LOWER(site) AS key, COUNT(*) AS n
        FROM jobs
        WHERE cover_letter_path IS NOT NULL
          AND (company IS NULL OR TRIM(company) = '')
          AND strategy IN ('greenhouse_api', 'workday_api', 'lever_api',
                           'ashby_api', 'amazon_jobs', 'microsoft_careers',
                           'apple_jobs', 'google_careers')
          AND site IS NOT NULL AND TRIM(site) != ''
          AND discovered_at > datetime('now', ?)
        GROUP BY key
    """, (f"-{max_age_days or DEFAULTS['max_job_age_days']} days",
          f"-{max_age_days or DEFAULTS['max_job_age_days']} days")).fetchall()
    existing: dict[str, int] = {}
    for r in existing_rows:
        existing[r["key"]] = existing.get(r["key"], 0) + r["n"]

    added_per_company: dict[str, int] = {}
    capped_jobs: list[dict] = []
    skipped_by_cap = 0
    for job in jobs:
        key = resolve_company_key(job)
        if key is None:
            capped_jobs.append(job)
            continue
        already = existing.get(key, 0) + added_per_company.get(key, 0)
        if already >= cap:
            skipped_by_cap += 1
            continue
        capped_jobs.append(job)
        added_per_company[key] = added_per_company.get(key, 0) + 1
    if skipped_by_cap:
        log.info("Cover cap: skipped %d job(s) where company is at/over %d covers.",
                 skipped_by_cap, cap)
    jobs = capped_jobs

    conn.commit()  # Close read transaction before long LLM phase

    if not jobs:
        log.info("No jobs needing cover letters (score >= %d; after per-company cap).", min_score)
        return {"generated": 0, "errors": 0, "elapsed": 0.0}

    COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)
    log.info(
        "Generating cover letters for %d jobs (score >= %d, workers=%d)...",
        len(jobs), min_score, workers,
    )
    t0 = time.time()
    completed = 0
    results: list[dict] = []
    error_count = 0

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_cover_one_job, job, resume_text, profile, doc_format): job for job in jobs}
            for future in as_completed(futures):
                job = futures[future]
                completed += 1
                try:
                    result = future.result()
                except Exception as e:
                    result = {
                        "url": job["url"], "title": job.get("title") or "", "site": job["site"],
                        "path": None, "pdf_path": None, "error": str(e),
                    }
                    error_count += 1
                    log.error("[ERROR] %s -- %s", (job.get("title") or "")[:40], e)

                results.append(result)
                elapsed = time.time() - t0
                rate = completed / elapsed if elapsed > 0 else 0
                status = "OK" if result.get("path") else "ERR"
                log.info("%d/%d [%s] | %.1f jobs/min | %s", completed, len(jobs), status, rate * 60,
                         (result.get("title") or "")[:40])
    else:
        for job in jobs:
            completed += 1
            try:
                result = _cover_one_job(job, resume_text, profile, doc_format)
                elapsed = time.time() - t0
                rate = completed / elapsed if elapsed > 0 else 0
                log.info("%d/%d [OK] | %.1f jobs/min | %s", completed, len(jobs), rate * 60,
                         (result.get("title") or "")[:40])
            except Exception as e:
                result = {
                    "url": job["url"], "title": job.get("title") or "", "site": job["site"],
                    "path": None, "pdf_path": None, "error": str(e),
                }
                error_count += 1
                log.error("%d/%d [ERROR] %s -- %s", completed, len(jobs),
                          (job.get("title") or "")[:40], e)
            results.append(result)

    # Persist to DB: increment attempt counter for ALL, save path only for successes
    now = datetime.now(timezone.utc).isoformat()
    saved = sum(1 for r in results if r.get("path"))

    def _flush_cover_results(conn, results, now):
        for r in results:
            # Prefer the generated DOCX/PDF path; fall back to text path
            # if conversion failed (apply layer will flag as invalid).
            stored_path = r.get("pdf_path") or r.get("path")
            _mark_cover_result(
                conn, r["url"], stored_path,
                error=r.get("error"), now=now,
            )

    try:
        write_with_retry(conn, _flush_cover_results, conn, results, now)
    except Exception as flush_err:
        log.exception("DB flush failed for cover letter batch: %s", flush_err)

    elapsed = time.time() - t0
    log.info("Cover letters done in %.1fs: %d generated, %d errors", elapsed, saved, error_count)

    return {
        "generated": saved,
        "errors": error_count,
        "elapsed": elapsed,
    }
