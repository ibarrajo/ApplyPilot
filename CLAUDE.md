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
2. **Greenhouse scraper (medium, sub-project):** ~20 Tier-1 companies use Greenhouse (Temporal, Pulumi, Anduril, Carta, Smartsheet, Tanium, OfferUp, ExtraHop, Databricks, Axon, Sprout Social, Stackline, Yoodli, Ai2, etc.). One scraper unlocks all of them. Greenhouse has a public JSON API at `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs`.
3. **Lever scraper (small, sub-project):** Stripe, Highspot, Outreach, Rover. Lever API: `https://api.lever.co/v0/postings/{slug}?mode=json`.
4. **Ashby scraper (small, sub-project):** MotherDuck, Statsig, Deepgram, Common Room, DevZero, Impart Security, Clarify. Ashby has a private API — may need Playwright scrape.

Roadmap: do Workday first (aligns with existing code), then build a single generic "JSON-API ATS" scraper that covers Greenhouse + Lever (both have clean JSON) as one medium-sized sub-project.
