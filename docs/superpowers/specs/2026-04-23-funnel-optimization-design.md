# Funnel Optimization — Cheaper, Sharper Apply Pipeline

**Date:** 2026-04-23
**Status:** Draft (awaiting user review)
**Scope:** Sub-project 1 of the larger ApplyPilot overhaul. Four targeted changes that shrink AI spend, stop stale processing, and enforce a per-company application cap. No new subsystems.

---

## Problem

The pipeline burns credits on jobs we will never apply to and burns applications on companies we've already saturated.

**Evidence from the live DB (6,430 jobs):**

| Symptom | Count | Cost |
|---|---|---|
| Jobs scored ≤6 (unusable under 8+ rule) | 3,984 | Already-paid scoring credits |
| Jobs tailored at score <8 (score 2: 88, score 7: 303) | 391 | Tailoring + cover-letter credits (Gemini Pro) |
| Failed applies (`no_result_line`, location, expired) | 1,245 | Claude Code Max-plan quota |
| Applied in excess of "2-per-company" rule (Netflix=9, 4 co's at 3+) | 10+ | Wasted applications that should have gone to new companies |
| Jobs in DB older than 30 days (stale) | 6,430 (all) | Pipeline will reprocess them if re-run as-is |

The current code has a *soft* company deprioritization (sort last if ≥2 apps in 7 days) but no hard cap. The `min_score` default is `7` in four places (`config.py`, `cli.py:86`, `cli.py:172`, `pipeline.py:223/234` — plus a hardcoded `if score >= 7` at `cli.py:380`). There is no stale-age filter anywhere. AI stages don't gate on score before spending tokens.

## Goals

1. Cut AI spend on jobs that won't convert by filtering early (score threshold, stale age).
2. Replace the soft sort-by-recency with a **hard** per-company cap that blocks saturated companies entirely.
3. Make the cap configurable globally and overridable per-company without code changes.
4. Keep the existing concurrency and ATS-lane rules — they're orthogonal to the cap.

## Non-goals (separate sub-projects)

- Apply reliability fixes (`no_result_line`, location gating, extension dead code) → sub-project 3.
- DOCX switchover → sub-project 2.
- Jobscan-guideline integration into tailor/cover prompts → sub-project 4 (research runs in parallel).
- Re-scoring existing 3,984 low-score jobs — they're sunk. New jobs get the new threshold.
- "Un-tailoring" the 391 low-score jobs that were already tailored — cost is already paid; they'll just drop out of active queues via the new threshold.

---

## Design

### Change 1 — Configurable minimum score, default 8

**Single source of truth** in `config.py`:

```python
DEFAULTS = {
    "min_score": 8,                          # was 7
    "max_job_age_days": 14,                  # NEW
    "max_in_flight_per_company": 3,          # NEW
    "in_flight_window_days": 30,             # NEW
    "max_apply_attempts": 3,
    "max_tailor_attempts": 5,
    "poll_interval": 60,
    "apply_timeout": 300,
    "viewport": "1280x900",
}
```

**Touchpoints that must stop hardcoding `7`:**

- `cli.py:86` — `min_score: int = typer.Option(7, ...)` → `typer.Option(config.DEFAULTS["min_score"], ...)`
- `cli.py:172` — same pattern, different command
- `cli.py:380` — `if score >= 7:` — read from config
- `cli.py:408` — color-coding logic `score >= 9 else 'yellow' if score >= 7` — keep 7 here (it's *display*, not a gate; score-7 jobs shown yellow "near miss" is useful signal)
- `pipeline.py:223, 234, 326, 346, 353, 361, 441, 503, 568` — function signatures with `min_score=7` default read from config
- `apply/launcher.py:1079` — `acquire_job(min_score=7, ...)` default reads from config

**Grep verification step in the implementation plan:** `grep -rn "min_score.*=.*7\|score >= 7\|fit_score >= 7" src/` must return zero gate-related hits after the change (display-only hits are allowed, annotated).

### Change 2 — Stale-job skip (14-day default)

Every SELECT in the pipeline that picks jobs for a paid stage adds:

```sql
AND discovered_at > datetime('now', '-{max_job_age_days} days')
```

This is cheaper than pruning (no DB writes) and reversible via config.

**Rationale for 14 days:** job listings on LinkedIn/Indeed/HN are stale within 1-2 weeks — reposts, filled positions, closed reqs. The user states "we can always find more jobs faster than we can apply," so re-discovery is cheap relative to processing stale entries. The current DB is entirely 30-90d old, so setting 14d will correctly nuke the backlog without touching DB state.

**Touchpoints:**

| File | Change |
|---|---|
| `scoring/scorer.py` | Candidate query adds age filter |
| `scoring/tailor.py` | Candidate query adds age filter |
| `scoring/cover_letter.py` | Candidate query adds age filter |
| `apply/launcher.py` `acquire_job()` | Candidate query adds age filter |
| `pipeline.py` `_count_pending()` | Counting queries add age filter so `status` reflects post-filter reality |
| `cli.py` `run` command | New `--max-age-days N` flag, default from config |
| `cli.py` `apply` command | Same flag |

**Override paths:**

- CLI: `applypilot run score --max-age-days 30` or `--max-age-days 0` (disables filter)
- Config: set `DEFAULTS["max_job_age_days"]` — but we keep this in code, not YAML, since it's a global tuning knob, not per-entity.

**Effect on existing DB:** every job is 30-90d old, so under 14d all queues become empty until discovery runs. That's intended — user will `applypilot run discover` to refill with fresh jobs.

**Also:** the `discover` stage itself gets a `fresh_only` default that filters out listings the source reports as older than `max_job_age_days * 1.5` (some sources expose `posted_at` in the listing — we use it to skip older results before even storing them, saving disk and downstream work).

### Change 3 — Per-company open-pipeline cap

**Replace** the existing soft-sort (`launcher.py:1160-1200`) with a hard cap enforced in Python after candidate SQL.

**Definitions:**

- **in-flight application:** a job with `apply_status IN ('applied', 'in_progress', 'needs_human')` AND `COALESCE(applied_at, last_attempted_at) > now - window_days`. `manual` and `failed` don't count — the company didn't see those.
- **cap:** max concurrent in-flight applications allowed for a company.
- **window:** days of history considered when counting in-flight.
- **open-pipeline semantics:** when an in-flight job ages out of the window OR is set to `failed`/`applied+rejected` later, a slot reopens. No lifetime limit.
- **special cases exempt from cap:**
  - `company IS NULL` or empty string (can't extract — ~83% of DB today; primarily HN posts and direct contact-only URLs, where the `site` column holds the company name but we can't reliably match it to URL-derived companies from other sources).
  - cap is explicitly `-1` (meaning: ignore cap, apply always — useful for pausing the feature per-company).

**Not special-cased** (unlike earlier draft): HN jobs don't need a `LIKE 'HN: %'` check because `extract_company()` stores NULL for them, not `HN: ...` (that string lives in the `site` column). NULL-exemption covers them.

**Config layering — new YAML file `~/.applypilot/company_limits.yaml`:**

```yaml
# Defaults applied to every company unless overridden below.
defaults:
  max_in_flight: 3
  window_days: 30

# Per-company overrides. Company names are matched case-insensitively against
# the `company` column (set by `extract_company()` from application_url).
overrides:
  netflix:
    max_in_flight: 1          # big brand, be selective
  stripe:
    max_in_flight: 5
    window_days: 14           # they hire fast — shorter cooldown
  google:
    max_in_flight: 0          # pause entirely (0 = block, -1 = unlimited)
  openai:
    max_in_flight: -1         # no cap
```

**Resolution:** `config.get_company_limit(company) -> (cap, window_days)`. Falls through defaults → override → defaults. Missing file is OK (uses code defaults only).

**New behavior in `acquire_job()` (`launcher.py`):**

```python
# AFTER the existing candidate SELECT (drop the deprioritization sort).
# Compute in-flight buckets for all companies in the candidate set.
recent = conn.execute("""
    SELECT LOWER(company) AS company,
           COALESCE(applied_at, last_attempted_at) AS ts
    FROM jobs
    WHERE apply_status IN ('applied', 'in_progress', 'needs_human')
      AND COALESCE(applied_at, last_attempted_at) IS NOT NULL
      AND COALESCE(applied_at, last_attempted_at) >
          datetime('now', '-90 days')      -- widest possible window
      AND company IS NOT NULL
""").fetchall()

from collections import defaultdict
from datetime import datetime, timedelta, timezone
in_flight_by_company = defaultdict(list)
for r in recent:
    in_flight_by_company[r["company"]].append(r["ts"])

def over_cap(company: str | None) -> bool:
    if not company:                          # NULL or empty → exempt
        return False
    cap, window = config.get_company_limit(company)
    if cap < 0:
        return False                         # unlimited
    if cap == 0:
        return True                          # explicit block
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window)).isoformat()
    count = sum(1 for ts in in_flight_by_company.get(company.lower(), [])
                if ts and ts > cutoff)
    return count >= cap

# Filter candidates by cap BEFORE the ATS-lane check.
candidates = [c for c in candidates if not over_cap(c["company"])]
```

**SQL changes to the candidates query:**

- Drop the `recent_applied_count` LEFT JOIN and the sort clause `CASE WHEN recent_applied_count >= 2 THEN 1 ELSE 0 END`.
- Drop the `company_rank` PARTITION BY — it was only there to support the soft sort.
- Keep `active_companies` concurrency exclusion (1 worker per company at once).
- Add the age filter from Change 2.
- Keep the ORDER BY as `j.fit_score DESC, j.url` (simple, predictable).

**Resulting ORDER BY:** `fit_score DESC, url ASC`. Deterministic and tie-broken by URL.

### Change 4 — Gate AI stages on the same filters

The 88-jobs-tailored-at-score-2 leak happened because earlier tailoring didn't enforce a score gate. Fix: **every paid stage respects the current threshold and age window**.

- `scoring/tailor.py` candidate SELECT: `WHERE fit_score >= ? AND discovered_at > datetime('now', ?)`
- `scoring/cover_letter.py`: same gate
- `scoring/scorer.py` itself: only the age filter (scorer is the stage that *produces* fit_score; it can't gate on its own output)

**Early-skip in scorer.py:** on LLM error (current behavior writes `score_error` + retry), *also* on score <`min_score`, immediately break out — don't continue to any downstream scoring chain. (Spot-check: I believe this is already the case because the scorer only writes `fit_score`, but the plan will verify.)

---

## Config layering summary

| Setting | Default | Where to override |
|---|---|---|
| `min_score` | 8 | CLI `--min-score`, or edit `config.DEFAULTS` |
| `max_job_age_days` | 14 | CLI `--max-age-days`, or edit `config.DEFAULTS` |
| `max_in_flight_per_company` | 3 | `~/.applypilot/company_limits.yaml` `defaults.max_in_flight` |
| `in_flight_window_days` | 30 | `~/.applypilot/company_limits.yaml` `defaults.window_days` |
| per-company cap/window | — | `~/.applypilot/company_limits.yaml` `overrides.<company>` |

**Package-shipped example:** `src/applypilot/config/company_limits.example.yaml` (for reference; not copied to user dir automatically — `applypilot init` prompts the user if they want overrides).

---

## File changes summary

| File | Lines (approx) | Change |
|---|---|---|
| `src/applypilot/config.py` | ~30 added | New `DEFAULTS` keys, `load_company_limits()`, `get_company_limit()` |
| `src/applypilot/config/company_limits.example.yaml` | New file | Documented example YAML |
| `src/applypilot/cli.py` | ~15 touched | All `min_score=7` literal defaults read from config; new `--max-age-days` flag on `run` and `apply` |
| `src/applypilot/pipeline.py` | ~10 touched | All stage signatures, count queries, progress reporting read config defaults |
| `src/applypilot/scoring/scorer.py` | ~5 added | Age filter in candidate query |
| `src/applypilot/scoring/tailor.py` | ~5 added | Age + score filters |
| `src/applypilot/scoring/cover_letter.py` | ~5 added | Age + score filters |
| `src/applypilot/apply/launcher.py` | ~40 touched | `acquire_job()` query simplified; Python-side cap filter added; age filter added |
| `src/applypilot/database.py` | ~20 added | `get_in_flight_by_company()` helper |
| `tests/test_company_cap.py` | New | Cap enforcement unit tests |
| `tests/test_stale_filter.py` | New | Age filter unit tests |
| `tests/test_min_score_config.py` | New | Config-driven threshold tests |
| `docs/superpowers/specs/2026-04-23-funnel-optimization-design.md` | This file | — |

**No DB schema changes.** All filtering is query-time; existing columns (`company`, `applied_at`, `last_attempted_at`, `apply_status`, `discovered_at`) are sufficient.

---

## Testing plan

Three new test modules. A new `tests/conftest.py` fixture will seed a temp SQLite DB via `applypilot.database.init_db()` against a tmp-path (the existing `tests/` has no conftest.py yet — the plan creates it).

### `tests/test_company_cap.py`

- `test_cap_zero_blocks_company`: cap=0 → never acquire, even at score 10
- `test_cap_three_allows_three_then_blocks`: seed 0 in-flight, acquire 3, 4th call returns None for that company
- `test_rejected_does_not_count`: seed `apply_status='failed'` → doesn't count toward cap
- `test_manual_does_not_count`: seed `apply_status='manual'` → doesn't count
- `test_in_progress_counts`: seed `in_progress` → counts
- `test_needs_human_counts`: seed `needs_human` → counts
- `test_window_boundary`: seed app exactly `window_days` + 1 second ago → doesn't count; `window_days` - 1 → counts
- `test_per_company_override`: Netflix cap=1 via YAML override → 2nd Netflix attempt blocked; Stripe cap=5 → allows 5
- `test_null_company_exempt`: jobs with NULL company never blocked
- `test_empty_company_exempt`: jobs with `company=''` never blocked
- `test_unlimited_cap`: cap=-1 → never blocked even at 1000 in-flight

### `tests/test_stale_filter.py`

- `test_stale_job_excluded_from_tailor`: job with `discovered_at` older than cutoff → not in tailor candidates
- `test_fresh_job_included`: job with `discovered_at = now` → included
- `test_age_filter_disabled`: `max_age_days=0` → all jobs included
- `test_age_filter_applies_to_score`: scorer query respects age
- `test_age_filter_applies_to_cover`: cover query respects age
- `test_age_filter_applies_to_apply_acquire`: `acquire_job()` respects age

### `tests/test_min_score_config.py`

- `test_default_threshold_is_eight`: `config.DEFAULTS["min_score"]` == 8
- `test_cli_min_score_reads_config`: invoking `run` without `--min-score` uses config value
- `test_tailor_gate_respects_config`: change config to 9, verify score-8 job excluded from tailor
- `test_acquire_respects_config`: same for apply

### Integration check (manual, listed in plan)

```bash
# Snapshot state before changes
applypilot status > /tmp/before.txt

# Apply changes (plan executes in this order)
# After implementation:
applypilot status > /tmp/after.txt
diff /tmp/before.txt /tmp/after.txt

# Verify Netflix (9 in-flight) is now blocked by default cap=3
applypilot company show netflix  # NEW command — see Future work
# OR: sqlite inspect
```

---

## Backfill / migration

1. **No DB rewrites.** Existing jobs keep their scores and statuses.
2. **Jobs at score <8 already tailored:** stay in DB but drop out of `ready_to_apply` queue (filtered by new min_score).
3. **Netflix / over-cap companies:** existing 9 apps aren't retroactively invalidated. They count toward the cap *only if within window* (most of Netflix's 9 are likely >30d old, so won't block new Netflix apps unless user sets a longer window).
4. **All existing jobs fail age filter (all are 30-90d old under 14d cutoff).** User runs `applypilot run discover` to refresh the queue. This is intended per user's stated workflow.
5. **No downtime.** Changes are backwards-compatible: missing `company_limits.yaml` → defaults apply; existing `--min-score` CLI usage still works.

---

## Risks & open questions

| Risk | Mitigation |
|---|---|
| Age filter nukes the entire queue until discovery runs | Intended. Status output will show `skipped_stale` count so the user knows why. |
| `discovered_at` is NULL for some old rows | The query `datetime('now') > NULL` evaluates NULL; we treat NULL-`discovered_at` as stale (excluded). Verified in tests. |
| `company` column NULL for ~83% of jobs today (HN posts + direct URLs where extract_company can't match) | Explicitly exempt from cap — slight under-enforcement for these, but preferable to blocking unrelated jobs that share a NULL key. Future work (not this spec): upgrade `extract_company()` to also read from the `site` column for HN entries. |
| `company_limits.yaml` with typo'd company name silently does nothing | `applypilot company list` (new CLI — see Future work) will print unmatched overrides as warnings. Not in MVP — deferred. |
| Python-side cap filtering runs O(candidates) + O(in_flight_total) each acquire | Both bounded (<100 rows typical). No concern. |
| Concurrency: two workers acquire simultaneously and both pass the cap check | Existing `active_companies` exclusion still prevents two workers on the same company at once. Cap check uses committed state (transactions serialized via `BEGIN IMMEDIATE`). |

**Open questions left for the implementation plan (not design):**

- Exact wording of `status` output's new "skipped-stale" / "blocked-by-cap" rows.
- Whether to ship `company_limits.example.yaml` empty or pre-populated with common FAANG caps.

---

## Future work (out of this spec)

- `applypilot company list|show|set|unset` CLI for managing overrides from the shell (instead of editing YAML). Defer — YAML is enough for first iteration.
- `applypilot prune --older-than N` command to soft-archive stale jobs. Defer — the age filter solves the functional problem; prune is a UI polish.
- Pre-apply location gate (in launcher, before invoking Claude Code). Belongs to sub-project 3 (apply reliability).
- DOCX migration. Sub-project 2.
- Jobscan-derived prompt tuning. Sub-project 4.

---

## Approval checkpoint

This design assumes user is OK with:
- Aggressive 14-day stale filter (will empty queues until rediscovery)
- YAML override file in `~/.applypilot/`
- Dropping the soft-sort entirely in favor of hard cap
- Not un-tailoring existing score-<8 jobs (sunk cost)
