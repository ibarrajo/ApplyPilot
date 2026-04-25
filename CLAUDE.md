# ApplyPilot — Claude Code Operating Manual

## Mission

ApplyPilot is an autonomous job application pipeline.
Claude's job is to **operate, monitor, and fix the pipeline** — not to manually do what the pipeline automates.

**The goal**: Discover jobs → Score them → Tailor resumes → Generate cover letters → Auto-apply. All automated.

---

## Claude's Role (READ THIS FIRST)

**Claude is the pipeline engineer and operator. Claude does NOT manually apply to jobs.**

### What Claude Does
1. Run pipeline commands (`applypilot run ...`, `applypilot apply`, `applypilot status`)
2. Monitor logs and output for errors
3. Diagnose root causes when things break
4. Fix the source code (`src/applypilot/`)
5. Re-run and verify fixes work
6. Keep this CLAUDE.md updated with decisions and learnings

### What Claude Does NOT Do
- Open a browser via Playwright and manually fill out application forms
- Act as the "apply agent" — that's what `applypilot apply` spawns Claude Code subprocesses for
- Skip the automation and do things by hand "just this once"

### Daily Operating Loop
```
1. applypilot status              # Where are we? What's the funnel?
2. applypilot run discover        # Find new jobs
3. applypilot run enrich          # Fetch full descriptions
4. applypilot run score           # AI scoring
5. applypilot run tailor          # Tailor resumes for 7+ scores
6. applypilot run cover           # Generate cover letters
7. applypilot apply               # Auto-apply (Tier 3 — uses Claude Code credits)
8. applypilot dashboard           # Generate HTML dashboard for review
```

When a stage fails: stop, read logs, find root cause, fix code, re-run.

---

## Architecture

### Three Tiers
- **Tier 1** (Discovery): No API key. Scrapes job boards.
- **Tier 2** (AI Processing): Gemini/OpenAI API. Score, tailor, cover letters.
- **Tier 3** (Auto-Apply): Claude Code CLI as subprocess. Fills forms via Playwright.

### Two Credit Systems (IMPORTANT)
- **Tier 2**: Gemini API (free tier) + OpenAI fallback. Keys in `~/.applypilot/.env`
- **Tier 3**: Claude Code CLI with Max plan. IMPORTANT: `ANTHROPIC_API_KEY` must be stripped from subprocess env (launcher.py does this) or it overrides Max plan auth with API billing. No Gemini browser agent exists — the Gemini/OpenAI cascade is Tier 2 only. `--strict-mcp-config` is required to prevent Docker MCP's Playwright (which can't access host files) from interfering with resume uploads.

### LLM Client (`src/applypilot/llm.py`)

Multi-provider fallback with two-tier model strategy:
- **Fast** (scoring, HN extraction): Gemini Flash → OpenAI → Anthropic Haiku
- **Quality** (tailoring, cover letters): Gemini Pro → OpenAI → Anthropic Sonnet

Key behaviors:
- `get_client(quality=False)` for fast, `get_client(quality=True)` for quality
- On 429: marks model exhausted for 5 min, falls to next in chain
- `config.load_env()` MUST be called before importing `llm` (env vars read at module import)
- Gemini 2.5+ thinking tokens consume max_tokens budget — set much higher than visible output needs

### Database (`src/applypilot/database.py`)

SQLite with WAL mode. Thread-local connections.
- `ensure_columns()` auto-adds missing columns via ALTER TABLE
- URL normalization at insert time (resolves relative URLs via `sites.yaml` base_urls)
- `company` column extracted from `application_url` domain (Workday, Greenhouse, Lever, iCIMS patterns)
- `acquire_job()` uses company-aware prioritization to spread applications across employers

### Pipeline Stages

| Stage | Condition | Tab |
|-------|-----------|-----|
| `discovered` | no description, no error | active |
| `enrich_error` | has `detail_error` | archive |
| `enriched` | has description, no score | active |
| `scored` | score < 7 | archive |
| `scored_high` | score >= 7, not tailored | active |
| `tailor_failed` | attempts >= 5, no result | archive |
| `tailored` | has resume, no cover letter | active |
| `cover_ready` | has cover letter, not applied | active |
| `applied` | `apply_status = 'applied'` | applied |
| `apply_failed` | permanent apply error | archive |
| `apply_retry` | retryable apply error | active |

---

## File Locations

| What | Path |
|------|------|
| Source code | `src/applypilot/` (editable install) |
| Venv | `.venv/` |
| Resume (txt) | `~/.applypilot/resume.txt` |
| Resume (PDF) | `~/.applypilot/resume.pdf` |
| API keys | `~/.applypilot/.env` (NEVER commit.) |
| Profile | `~/.applypilot/profile.json` |
| Search config | `~/.applypilot/searches.yaml` |
| Database | `~/.applypilot/applypilot.db` |
| Tailored resumes | `~/.applypilot/tailored_resumes/{site}_{title}_{hash}.txt` (+`.pdf`) |
| Cover letters | `~/.applypilot/cover_letters/{site}_{title}_{hash}_CL.txt` (+`.pdf`) |
| Apply logs | `~/.applypilot/logs/claude_{YYYYMMDD_HHMMSS}_w{N}_{site}.txt` |
| Dashboard | `~/.applypilot/dashboard.html` |

---

## Candidate Profile

Loaded from `~/.applypilot/profile.json` at runtime. See `applypilot init` to create one.

---

## Key Commands

```bash
# Tier 2 pipeline (safe, uses Gemini/OpenAI)
applypilot run discover                        # Find new jobs
applypilot run enrich                          # Fetch full descriptions
applypilot run score --limit 100               # AI scoring
applypilot run tailor --limit 50               # Tailor resumes (score >= 7)
applypilot run cover                           # Generate cover letters
applypilot run score tailor cover --stream     # All stages concurrently
applypilot status                              # Pipeline funnel stats
applypilot dashboard                           # Generate HTML dashboard

# Tier 3 apply (uses Claude Code credits)
applypilot apply --dry-run --url URL           # Test one job (no submit)
applypilot apply                               # Auto-apply to cover_ready jobs
```

---

## Orchestration Strategy

When running the pipeline:
1. **Throughput** — use `--stream` for concurrent stages
2. **Quality** — highest scores get tailored first, company diversity in applications
3. **Error handling** — if > 30% failure rate, stop and fix before continuing
4. **Bottleneck focus** — priority is building the apply-ready queue

Error patterns:
- Gemini 429: automatic fallback, no intervention needed
- Tailor validation failures > 30%: investigate validator settings
- Apply credit exhaustion: alert user, cannot auto-fix
- `hn://` URLs or malformed data: check hackernews.py sanitization

---

## Security Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 0 | Never paste API keys in chat | Keys go directly into `~/.applypilot/.env` |
| 1 | Display name from profile.json | `preferred_name` in profile.json. Legal name for background checks only. |
| 3 | No real password in profile.json | Embedded in plaintext in LLM prompts |
| 4 | Tier 2 only until pipeline is stable | Auto-apply (bypassPermissions) = prompt injection risk |
| 5 | Review tailored resumes before using | `resume_facts` pins facts but still check |
| 6 | Gemini free tier + OpenAI fallback | Free primary, cheap fallback |
| 7 | Location from searches.yaml | Search radius and accept patterns are config-driven |
| 12 | Two-tier model strategy | Flash for speed, Pro for quality writing |
| 13 | High max_tokens for thinking models | Scoring: 8192, Tailoring: 16384, Cover: 8192 |
| 17 | Skip Gmail MCP / CapSolver | Too much attack surface with bypassPermissions |
| 18 | URL normalization at discovery | Resolves relative URLs via sites.yaml base_urls |
| 19 | Banned words = warnings not errors | LLM judge handles tone |
| 20 | Jobs without application_url = manual | LinkedIn Easy Apply marked `apply_status='manual'` |
| 23 | Company-aware apply prioritization | ROW_NUMBER() PARTITION BY company spreads applications across employers |
| 25 | Apply uses Claude Code CLI, not Gemini | Separate billing system. Spawns `claude` subprocess. |
| 26 | HN URL sanitization | Only stores http(s) URLs, deobfuscates emails, synthetic URLs for contact-only posts |
| 27 | Basic prompt injection defense | LLM prompts instruct to treat input as untrusted. Minimal — not a sandbox. |
| 28 | `--strict-mcp-config` for apply subprocess | Docker MCP Toolkit exposes duplicate Playwright tools that run in containers (can't access host files). Strict mode ensures only our local npx Playwright is available. |
| 29 | Funnel config: min_score=8, age=14d, cap=3/30d | 2026-04-23 funnel-optimization spec (docs/superpowers/specs/). Replaces score=7 threshold and soft-sort deprioritization. All paid stages gate on `fit_score >= min_score` AND `discovered_at` within age cutoff. Hard per-company cap enforced in `acquire_job` via YAML overrides at `~/.applypilot/company_limits.yaml`. Disable age filter with `--max-age-days 0`. Per-company overrides: set `max_in_flight: 0` to block, `-1` to unlimit. |
| 30 | Explicit state machine replaces implicit status derivation | 2026-04-24. `jobs.state` column (23 canonical values: discovered → enriched → scored → tailored → ready_to_apply → applying → applied → responded → interview → offer, with _failed/archived branches). Transitions via `database.transition_state(conn, url, to_state, reason, metadata, force)` — atomic UPDATE + audit row insert to `job_state_transitions`. Validated against `VALID_TRANSITIONS`. `apply_category` + `apply_status` + `tracking_status` kept for backward compat (DEPRECATED — read from `state` instead). `posted_at` column now captures employer-reported posting date (Amazon + Greenhouse scrapers fill it; other scrapers TODO). Retry-counter renames: `detail_retry_count → enrich_attempts`, `detail_next_retry_at → enrich_next_retry_at`, `score_retry_count → score_attempts`. |
| 31 | P0 state-machine rollout shipped | 2026-04-25. Tier-2 pipeline (discover/enrich/score/tailor/cover) and apply-stage launcher mark/reset functions all emit transitions. All 6 scrapers (jobspy/hackernews/costco/smartextract/amazon/greenhouse) emit NULL→initial transitions. 218 tests pass; 53 added in test_state_machine/test_stage_transitions/test_launcher_state_transitions/test_scraper_contracts. Known follow-up leaks (P0.5, NOT shipped — adjacent systems, single follow-up commit): (a) `update_tracking_status` (database.py:1843) bypasses transitions on email-driven status changes; (b) manual-ATS skip in launcher.py:1254; (c) stale-lock release in launcher.py:1133/2423/3107; (d) HITL re-queue startup path. |
| 32 | Per-worker resume dir | 2026-04-25. `build_prompt` now writes the resume copy to `APPLY_WORKER_DIR/worker-{wid}/` instead of the shared `APPLY_WORKER_DIR/current/`. Fixes audit #9 from the apply UX overhaul spec — concurrent workers no longer cross-pollute clean-filename uploads. `gen_prompt` debug helper updated to thread `worker_id` through. Plan 1 of 5 for the apply UX overhaul. |
| 33 | Apply uses Chrome for Testing | 2026-04-25. `config.get_chrome_path()` now prefers `~/.applypilot/chrome-for-testing/chrome-linux64/chrome` over system `google-chrome` on Linux. CfT is the only branded Chromium build that still accepts `--load-extension` after Chrome 137 (Stable/Beta/Dev/Canary all silently reject it). Existing chrome.py extension-load block at line 1144 finally works in production. CHROME_PATH env var still overrides. Plan 2 of 5 for the apply UX overhaul. |
| 34 | HITL paths collapsed | 2026-04-25. Three near-duplicate HITL paths in `_worker_loop_body` (generic `needs_human`, `login_required`, `HITL_AUTO_ROUTE`) collapsed into one `_run_hitl(worker_id, port, job, reason, instructions, navigate_url, duration_ms, ...)` helper at the top of launcher.py. ~150-line reduction. Each call site now parameterizes its differences (e.g. `login_required` clears the ATS session up front, generic threads `nh_detail` into instructions) and delegates to the same code. Screening-questions HITL stays separate — its mechanism is the TUI Q&A queue, not a banner. Module extraction to `apply/hitl.py` and the terminal-stdin Done-button fallback (audit #6) are deferred. Plan 3 of 5 for the apply UX overhaul. |
| 35 | Standalone human-review deleted | 2026-04-25. `applypilot human-review` CLI command and the 567-line standalone HTTP server (`serve`, `_start_hitl_chrome`, `_run_agent_for_job`, `_build_ui_html`) were removed. Audit #5 — duplicated the in-pipeline HITL banner+wait flow. Jobs parked as `needs_human` are now picked up automatically by the next `applypilot apply` run. `human_review.py` shrunk from 1,044 → 474 lines; remaining helpers (`_inject_banner`, `_navigate_chrome`, `_start_done_watcher`, `_build_banner_js`) still imported by `_run_hitl` in launcher.py. Plan 5 of 5 for the apply UX overhaul. |
| 36 | patchright launch flags + stdin Done fallback | 2026-04-25. (a) Added 15 patchright/playwright launch-time flags from `chromiumSwitches.js` to `launch_chrome` in chrome.py: `--disable-field-trial-config`, `--disable-background-networking`, `--disable-background-timer-throttling`, `--disable-backgrounding-occluded-windows`, `--disable-breakpad`, `--disable-dev-shm-usage`, `--enable-features=CDPScreenshotNewSurface`, `--disable-hang-monitor`, `--disable-prompt-on-repost`, `--disable-renderer-backgrounding`, `--force-color-profile=srgb`, `--use-mock-keychain`, `--no-service-autorun`, `--export-tagged-pdf`, `--disable-search-engine-choice-screen`, `--edge-skip-compat-layer-relaunch`. Plus a longer `--disable-features` list including `AvoidUnnecessaryBeforeUnloadCheckSync`, `BoundaryEventDispatchTracksNodeRemoval`, `DestroyProfileOnBrowserClose`, etc. Stealth-flags-only — no patchright Python API integration (CDP-side init scripts deferred to extension rewrite). Verified CfT 148.0.7778.56 still launches via `/json/version` probe. (b) Audit #6: added a module-level `_stdin_fallback_lock` in launcher.py that gates a one-shot stdin reader inside `_run_hitl`. When a worker pauses, if no other worker holds the lock, a daemon thread reads one line from stdin; typing `done` (or empty) sets `hitl_event` so the user can override broken banner Done buttons. Multi-worker contention handled by the lock — second paused worker falls back to banner-only. |
| 37 | P0.5 status path leaks closed (audit #10) | 2026-04-25. All four leaks documented in decision #31 now emit `transition_state` calls so the canonical `state` column stays in sync: (a) `database.update_tracking_status` — when email signals raise `tracking_status` (ghosted/rejection/confirmation/follow_up/interview/offer), now also transitions `state` via `_TRACKING_TO_STATE` mapping. (b) `acquire_job` manual-ATS skip path (launcher.py:1257) — when a job is skipped because `is_manual_ats(url)` is True, transitions `state` to `manual_only`. (c) Stale-lock release in `acquire_job` (launcher.py:1135) — when bulk-clearing in_progress locks older than 30 min, transitions each affected URL back to `ready_to_apply`. (d) HITL re-queue startup path (launcher.py:3115) — when launcher.main re-queues parked needs_human jobs at startup, transitions each back to `ready_to_apply`. All four use `force=True` since the from-state isn't always one that legally permits the transition. Closes audit #10 in the apply UX overhaul spec. |

---

## Known Technical Gotchas

1. **Gemini thinking tokens**: 2.5+ models use thinking tokens that consume max_tokens budget. A simple response needs 30 tokens, a bullet rewrite needs 1200+.
2. **Agent log timezone**: Log filenames use local time, DB `last_attempted_at` is UTC. Dashboard matcher converts UTC→local.
3. **Singleton LLM client**: `llm.py` reads env vars at module import. Call `config.load_env()` BEFORE importing.
4. **Editable install**: `pip install -e .` means source edits take effect immediately.
5. **gemini-2.0-flash deprecated**: Use `gemini-2.5-flash` or newer for new API users.
6. **Docker MCP Toolkit interference**: If Docker Desktop is installed with MCP Toolkit, it exposes `mcp__MCP_DOCKER__browser_*` tools that shadow the local Playwright MCP. These Docker tools can't access the host filesystem, breaking resume/cover letter uploads. Fix: `--strict-mcp-config` in the claude subprocess command.

---

## Future Work — Local-employer discovery expansion (2026-04-24)

Two YAML files in `docs/` catalog ~120 Seattle-area employers (Downtown / SLU / Fremont / Ballard / Queen Anne / Belltown / Redmond / Bellevue / Kirkland / Capitol Hill / Eastlake):

- `docs/seattle_employers_v1.yaml` — 41 companies, senior/staff-focused
- `docs/seattle_employers_v2.yaml` — 80 net-new from AI2 Incubator, Madrona, PSL, Voyager, Built In Seattle, and gaming/biotech clusters

**TODOs to integrate these into discovery:**

1. **Workday scraping (quick win, ~1h):** verify tenant subdomains for the ~14 Workday-hosted companies and add to `src/applypilot/config/employers.yaml` following the existing pattern (`tenant`, `site_id`, `base_url`). Candidates: Remitly, Okta, Expedia Group, Zillow Group, F5, Salesforce, Adobe Seattle, Apptio, Lululemon, Prologis Tech, Qualtrics, SoFi.
2. **Greenhouse scraper — ✅ shipped 2026-04-24 (commit f7cba79).** `src/applypilot/discovery/greenhouse.py` hits the public board API at `boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true`. Registry at `src/applypilot/config/greenhouse_employers.yaml` (18 verified slugs across Temporal, Pulumi, Anduril, Carta, Smartsheet, OfferUp, ExtraHop, Tanium, Databricks, Axon, Sprout Social, Stackline, Yoodli, plus Twilio/Pinterest/Airbnb/Stripe/UberFreight). Combined: ~1,500 jobs per full crawl. Ai2 and Echodyne aren't on Greenhouse — still need investigation.

3. **Amazon scraper — ✅ shipped 2026-04-24 (commit 3b2c745/cc2917d).** `src/applypilot/discovery/amazon.py` uses the public GET JSON API at `amazon.jobs/en/search.json`. Key learning: Amazon's search is exact-match, not fuzzy — use broad queries ("software engineer", "software development engineer", etc.) not LinkedIn-style "Senior Software Engineer backend". Also: Amazon's `radius` param is unreliable, so client-side filter via `_in_seattle_area()` enforces WA state + explicit Seattle-area cities. Pulls ~500 Seattle-specific jobs per full crawl.

4. **Costco scraper — ✅ shipped 2026-04-24 (commit 3b2c745).** `src/applypilot/discovery/costco.py` hits `careers.costco.com/api/jobs`. Each job is nested under a `data` key. Most results are retail (Cake Decorator, Cashier) — scorer pre-filter now includes retail-role patterns to shortcut them to score=2 without LLM calls.

5. **Workday registry expansion — TODO.** The 2026-04-24 mega-corp audit (`docs/seattle_employers_v3_megacorps.yaml`) identified ~18 companies tagged as Workday (T-Mobile, Starbucks, Nordstrom, REI, Boeing, Alaska Airlines, PACCAR, Weyerhaeuser, HP, Dell, Accenture, Wayfair, Tyler Tech, ServiceNow, Raytheon, Limeade, Expedia, Tableau). Automated tenant-guess probes all failed — those companies either use different ATSes than Workday OR use non-standard tenant subdomains. Verify each manually by visiting their careers page and checking for `.wdN.myworkdayjobs.com/{tenant}/{site_id}` in the URL. Once verified: add to `src/applypilot/config/employers.yaml`. Expected yield: ~500-2000 additional local jobs.

6. **Playwright-based scraper for custom ATSes — TODO sub-project.** Microsoft (Eightfold, blocked JSON API), Google (custom boq-hiring, no JSON), Apple (session-cookie-gated), Meta (CF + GraphQL blocked), Starbucks (Eightfold blocked). The Missing-Employer ATS audit 2026-04-24 detailed each: see agent `a2d1ba79afbc5593f` output. Combined these represent ~1500-5000 high-value local jobs. Build a Playwright harness that opens the site, waits for JS-rendered listings, extracts them. Non-trivial (~2-5 days per scraper).
3. **Lever scraper (small, sub-project):** Stripe, Highspot, Outreach, Rover. Lever API: `https://api.lever.co/v0/postings/{slug}?mode=json`.
4. **Ashby scraper (small, sub-project):** MotherDuck, Statsig, Deepgram, Common Room, DevZero, Impart Security, Clarify. Ashby has a private API — may need Playwright scrape.

Roadmap: do Workday first (aligns with existing code), then build a single generic "JSON-API ATS" scraper that covers Greenhouse + Lever (both have clean JSON) as one medium-sized sub-project.
