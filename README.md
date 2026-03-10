<!-- logo here -->

> **⚠️ ApplyPilot** is the original open-source project, created by [Pickle-Pixel](https://github.com/Pickle-Pixel) and first published on GitHub on **February 17, 2026**. We are **not affiliated** with applypilot.app, useapplypilot.com, or any other product using the "ApplyPilot" name. These sites are **not associated with this project** and may misrepresent what they offer. If you're looking for the autonomous, open-source job application agent — you're in the right place.

# ApplyPilot

**Applied to 1,000 jobs in 2 days. Fully autonomous. Open source.**

[![PyPI version](https://img.shields.io/pypi/v/applypilot?color=blue)](https://pypi.org/project/applypilot/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/ibarrajo/ApplyPilot?style=social)](https://github.com/ibarrajo/ApplyPilot)

> **Forked from [Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot)** — this fork adds multi-provider LLM fallback, human-in-the-loop apply, Gmail tracking, a Q&A knowledge base, ATS session persistence, and significant pipeline hardening. Thank you to the original authors for the foundation.




https://github.com/user-attachments/assets/7ee3417f-43d4-4245-9952-35df1e77f2df


---

## What It Does

ApplyPilot is a 7-stage autonomous job application pipeline. It discovers jobs across 6+ boards, scores them against your resume with AI, tailors your resume per job, writes cover letters, submits applications, and then monitors your inbox for responses — all without you lifting a finger.

Three commands. That's it.

```bash
pip install applypilot
pip install --no-deps python-jobspy    # separate install (broken numpy pin in metadata)
pip install pydantic tls-client requests markdownify regex  # jobspy runtime deps
applypilot init          # one-time setup: resume, profile, preferences, API keys
applypilot run           # discover > enrich > score > tailor > cover letters
applypilot apply         # autonomous browser-driven submission
applypilot track         # scan Gmail for responses and surface action items
```

---

## Two Paths

### Full Pipeline (recommended)
**Requires:** Python 3.11+, Node.js (for npx), Gemini API key (free), Claude Code CLI, Chrome

Runs all stages end-to-end: job discovery through autonomous application submission and response tracking.

### Discovery + Tailoring Only
**Requires:** Python 3.11+, Gemini API key (free)

Runs stages 1–5: discovers jobs, scores them, tailors your resume, generates cover letters. You submit applications manually with the AI-prepared materials.

---

## The Pipeline

| Stage | What Happens |
|-------|-------------|
| **1. Discover** | Scrapes 5 job boards (Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs) + Hacker News Who's Hiring + 48 Workday employer portals + 30 direct career sites |
| **2. Enrich** | Fetches full job descriptions via JSON-LD, CSS selectors, or AI-powered extraction. Retries failed fetches with exponential backoff by error category |
| **3. Score** | AI rates every job 1–10 against your resume and preferences. Only high-fit jobs proceed. Failed scores retry with backoff instead of writing a 0 |
| **4. Tailor** | AI rewrites your resume per job: reorganizes, emphasizes relevant experience, adds keywords. `resume_facts` (companies, metrics, projects) are preserved exactly — never fabricated |
| **5. Cover Letter** | AI generates a targeted cover letter per job referencing the specific company, role, and your relevant experience |
| **6. Auto-Apply** | Claude Code navigates application forms, fills fields, uploads documents, answers screening questions, and submits. Per-worker Chrome instances with ATS session persistence |
| **7. Track** | Scans Gmail for responses, classifies emails (rejection / interview / ghosting), surfaces action items, and flags jobs with no response after N days |

Each stage is independent. Run them all or pick what you need.

---

## ApplyPilot vs The Alternatives

| Feature | ApplyPilot | AIHawk | Manual |
|---------|-----------|--------|--------|
| Job discovery | 5 boards + HN + Workday + direct sites | LinkedIn only | One board at a time |
| AI scoring | 1–10 fit score per job | Basic filtering | Your gut feeling |
| Resume tailoring | Per-job AI rewrite | Template-based | Hours per application |
| Auto-apply | Full form navigation + submission | LinkedIn Easy Apply only | Click, type, repeat |
| Human-in-the-loop | HITL pauses for login walls, CAPTCHAs, screening Q&A | None | Always |
| Response tracking | Gmail scanning + classification | None | Manual inbox triage |
| LLM fallback | Gemini → OpenAI → Claude cascade | Single provider | N/A |
| License | AGPL-3.0 | MIT | N/A |

---

## Requirements

| Component | Required For | Details |
|-----------|-------------|---------|
| Python 3.11+ | Everything | Core runtime |
| Node.js 18+ | Auto-apply | Needed for `npx` to run Playwright MCP server |
| Gemini API key | Scoring, tailoring, cover letters, tracking | Free tier (15 RPM / 1M tokens/day) is enough |
| Chrome/Chromium | Auto-apply | Auto-detected on most systems |
| Claude Code CLI | Auto-apply | Install from [claude.ai/code](https://claude.ai/code) |

**Gemini API key is free.** Get one at [aistudio.google.com](https://aistudio.google.com). OpenAI is also supported as a fallback — see [LLM Configuration](#llm-configuration).

### Optional

| Component | What It Does |
|-----------|-------------|
| Gmail OAuth (via Google Cloud) | Enables response tracking with `applypilot track` |
| OpenAI API key | Fallback if Gemini rate limits are hit |
| CapSolver API key | Solves CAPTCHAs during auto-apply (hCaptcha, reCAPTCHA, Turnstile) |

> **Note:** python-jobspy is installed separately with `--no-deps` because it pins an exact numpy version in its metadata that conflicts with pip's resolver. It works fine at runtime.

---

## Configuration

All generated by `applypilot init`:

### `profile.json`
Your personal data: contact info, work authorization, compensation, skills, experience, `resume_facts` (locked facts preserved during tailoring), EEO defaults, and optional job board credentials. Powers scoring, tailoring, form auto-fill, and tracking.

### `searches.yaml`
Job search queries, target titles, locations, boards, filters. Run multiple searches with different parameters.

### `.env`
API keys and runtime config: `GEMINI_API_KEY`, `OPENAI_API_KEY` (optional), `CAPSOLVER_API_KEY` (optional).

### Package configs (shipped with ApplyPilot)
- `config/employers.yaml` — Workday employer registry (48 preconfigured)
- `config/sites.yaml` — Direct career sites (30+), blocked sites, base URLs
- `config/searches.example.yaml` — Example search configuration

---

## LLM Configuration

ApplyPilot uses a two-tier model strategy with automatic multi-provider fallback:

| Tier | Used For | Default Chain |
|------|----------|---------------|
| **Fast** | Scoring, HN extraction | Gemini 2.5 Flash → Gemini 2.0 Flash → GPT-4.1 Nano → GPT-4.1 Mini → Claude Haiku |
| **Quality** | Tailoring, cover letters | Gemini 2.5 Pro → Gemini 2.5 Flash → GPT-4.1 Mini → Claude Sonnet → Claude Haiku |

On rate limit (429): the exhausted model is skipped for 5 minutes and the next in chain is tried automatically. No intervention needed.

Set `GEMINI_API_KEY` and optionally `OPENAI_API_KEY` in `~/.applypilot/.env`. Both have free tiers that cover normal usage.

---

## How Stages Work

### Discover
Queries Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs via JobSpy. Scrapes the monthly Hacker News "Who's Hiring" thread. Hits 48 Workday employer portals (configurable in `employers.yaml`) and 30 direct career sites with custom extractors. Deduplicates by URL.

### Enrich
Visits each job URL and extracts the full description. 3-tier cascade: JSON-LD structured data → CSS selector patterns → AI-powered extraction for unknown layouts. Failed fetches are categorized (expired, retriable, permanent) and retried with exponential backoff.

### Score
AI scores every job 1–10 against your profile. 9–10 = strong match, 7–8 = good, 5–6 = moderate, 1–4 = skip. Only jobs above your threshold proceed to tailoring. Failed LLM calls write `score_error` and retry with backoff rather than writing `fit_score=0`.

### Tailor
Generates a custom resume per job: reorders experience, emphasizes relevant skills, incorporates keywords from the job description. Your `resume_facts` (companies, projects, metrics) are preserved exactly. A validation step checks for banned words and fabrication before accepting the output; retries up to 5 times.

### Cover Letter
Writes a targeted cover letter per job referencing the specific company, role, and how your experience maps to their requirements.

### Auto-Apply

Claude Code launches Chrome instances, navigates to each application page, detects the ATS type (Workday, Greenhouse, Lever, iCIMS, and more), fills personal information and work history, uploads the tailored resume and cover letter, answers screening questions, and submits.

Key behaviors:
- **Parallel workers**: multiple Chrome instances run simultaneously (default: 5)
- **ATS session persistence**: login cookies are saved per ATS so you don't re-login every run
- **Per-worker status badge**: a floating badge is injected into Chrome via CDP showing the current job, status, and worker controls
- **Company-aware scheduling**: jobs are spread across employers to avoid submitting to the same company twice in a row

```bash
applypilot apply                                  # launch with defaults (5 workers)
applypilot apply --workers 3 --min-score 9        # tune parallelism and score floor
applypilot apply --url URL --dry-run              # test a single job without submitting
applypilot apply --fresh-sessions                 # refresh Chrome cookies from your real profile first
applypilot apply --sessions                       # list saved ATS sessions
applypilot apply --clear-session workday          # clear a specific ATS session
applypilot apply --no-hitl                        # skip HITL waits (for overnight runs)
applypilot apply --no-focus                       # prevent Chrome windows from stealing focus
applypilot apply --reset-failed                   # retry all failed jobs
applypilot apply --reset-category CATEGORY        # retry jobs in a specific failure category
applypilot apply --mark-applied URL               # manually mark a job as applied
applypilot apply --mark-failed URL --fail-reason "reason"
applypilot apply --gen --url URL                  # generate prompt file for manual debugging
```

### Human-in-the-Loop (HITL)

When the apply agent hits something it can't solve autonomously (a login wall, CAPTCHA, or an unusual form), it pauses and asks you to take over.

**In-pipeline HITL** (default): Chrome stays open with the current page. A floating badge appears in the corner showing the problem and a "Take Over / Done" button. You solve it, click Done, and the agent resumes on the same Chrome session.

**Standalone HITL**: If you run apply in `--no-hitl` mode overnight, parked jobs are queued. Run `applypilot human-review` to open a web UI at `localhost:7373` listing all parked jobs with their reasons. Click a job to open Chrome and handle it.

**Screening Q&A**: When an agent encounters a novel screening question, it surfaces it interactively before parking the job. You answer once; the answer is stored in the Q&A knowledge base and reused automatically on future applications.

### Track

Scans your Gmail inbox for responses to submitted applications, classifies each email (rejection, interview request, ghosting, or other), and stores the result in the database.

```bash
applypilot track                        # scan inbox and classify new responses
applypilot track --days 30              # look back 30 days (default: 14)
applypilot track --actions              # show pending action items (interviews to schedule, etc.)
applypilot track --setup                # verify Gmail connectivity
applypilot track --dry-run              # fetch and classify without writing to DB
```

Requires Gmail OAuth setup. Run `python scripts/gmail_oauth.py` once to authorize access and save credentials locally.

---

## Q&A Knowledge Base

Every screening question answered during the apply stage is stored in a local knowledge base. On future applications, matching questions are answered automatically without interrupting you.

```bash
applypilot qa list                      # show stored Q&A pairs
applypilot qa stats                     # coverage stats by ATS and outcome
applypilot qa export --output qa.yaml   # export for review/editing
applypilot qa import qa.yaml            # import edited answers
```

---

## Credentials Management

Store job board usernames and passwords for ATS auto-login. Credentials are stored locally in SQLite and included in LLM prompts during the apply stage — see the [security section](#security--privacy--read-this-before-you-run) for implications.

```bash
applypilot creds list                           # list all saved credentials (passwords masked)
applypilot creds show DOMAIN                    # show unmasked credentials for a domain
applypilot creds add DOMAIN -e user@example.com # add or update (password prompted)
applypilot creds set DOMAIN -p newpass          # update a specific field
applypilot creds delete DOMAIN                  # delete with confirmation
applypilot creds import-logs                    # scan apply logs for credentials and import
applypilot creds import-logs --dry-run          # preview without writing
```

---

## Using ApplyPilot with Claude Code

Claude Code is Anthropic's CLI tool. Instead of memorizing every command, you can open the project in Claude Code and tell it what you want in plain language — it reads logs, runs commands, interprets errors, and fixes things that break.

### Quick Start

```bash
# Install Claude Code (requires a Claude account)
# → https://claude.ai/code

# Clone the repo and open in Claude Code
git clone https://github.com/ibarrajo/ApplyPilot
cd ApplyPilot
claude
```

Then just tell it what you want:

```
Help me set up ApplyPilot for the first time.
```
```
Run the pipeline and tell me what's happening at each stage.
```
```
I got an error. Here's what it said: [paste error]. Can you fix it?
```
```
Check status and apply to the top-scoring jobs.
```

### Setting Up Your CLAUDE.md

ApplyPilot ships a `CLAUDE.md.example` file that serves as an operating manual for Claude Code. When Claude Code opens your project, it reads this file to understand how the pipeline works, where your files are, and what your current state is.

**To use it:**

```bash
cp CLAUDE.md.example CLAUDE.md
# Edit CLAUDE.md — fill in your name, location, target roles, and current pipeline state
```

`CLAUDE.md` is gitignored. It's personal to you and your machine — never commit it.

**What to fill in:**
- Your name and job search email
- Target role level and type (Senior Backend, Staff Eng, etc.)
- Location rules (included cities, excluded cities)
- Current pipeline state (updated as you run stages)
- Any TODOs or known issues

With a well-maintained `CLAUDE.md`, you can hand off the entire pipeline to Claude Code with a single prompt and it will operate it correctly.

> **A Max plan is required for the auto-apply stage.** Discovery, scoring, tailoring, and tracking run on Gemini/OpenAI and don't use Claude credits. Only `applypilot apply` spawns a Claude Code subprocess.

---

## CLI Reference

### Pipeline

```bash
applypilot init                          # one-time setup wizard
applypilot run [stages...]               # run pipeline stages (discover enrich score tailor cover pdf)
applypilot run all                       # run all stages in sequence
applypilot run --workers 4               # parallel workers for discovery/enrichment
applypilot run --stream                  # run multiple stages concurrently (streaming mode)
applypilot run --min-score 8             # override score threshold
applypilot run --limit 50                # limit jobs per stage
applypilot run --dry-run                 # preview without executing
applypilot status                        # pipeline funnel stats
applypilot dashboard                     # generate and open HTML dashboard
```

### Apply

```bash
applypilot apply                         # launch auto-apply (5 workers by default)
applypilot apply --workers 3             # parallel browser instances
applypilot apply --min-score 9           # score floor for job selection
applypilot apply --dry-run               # fill forms without submitting
applypilot apply --continuous            # run forever, polling for new jobs
applypilot apply --headless              # headless browser mode
applypilot apply --url URL               # apply to one specific job
applypilot apply --no-hitl               # skip HITL waits (for overnight runs)
applypilot apply --no-focus              # prevent Chrome windows from stealing focus
applypilot apply --fresh-sessions        # refresh Chrome cookies from your real profile
applypilot apply --sessions              # list saved ATS sessions
applypilot apply --clear-session NAME    # clear a specific ATS session (e.g. workday)
applypilot apply --reset-failed          # reset all failed jobs for retry
applypilot apply --reset-category CAT   # reset jobs in a failure category
applypilot apply --mark-applied URL      # manually mark a job as applied
applypilot apply --mark-failed URL --fail-reason "reason"
applypilot apply --gen --url URL         # generate prompt file for manual debugging
applypilot human-review                  # open HITL web UI for parked jobs (port 7373)
```

### Tracking

```bash
applypilot track                         # scan Gmail and classify responses
applypilot track --days 30               # look-back period in days (default: 14)
applypilot track --actions               # show action items (interviews, follow-ups)
applypilot track --setup                 # verify Gmail MCP connectivity
applypilot track --dry-run               # classify without writing to DB
```

### Q&A Knowledge Base

```bash
applypilot qa list                       # list stored Q&A pairs
applypilot qa stats                      # coverage stats by ATS and outcome
applypilot qa export --output qa.yaml    # export to YAML for editing
applypilot qa import qa.yaml             # import from YAML
```

### Credentials

```bash
applypilot creds list                           # list all saved credentials
applypilot creds show DOMAIN                    # show unmasked credentials
applypilot creds add DOMAIN -e EMAIL            # add or update
applypilot creds set DOMAIN -p NEWPASS          # update a field
applypilot creds delete DOMAIN                  # delete with confirmation
applypilot creds import-logs                    # import from apply logs
applypilot creds import-logs --dry-run          # preview without writing
```

---

## Security & Privacy — Read This Before You Run

ApplyPilot handles sensitive data: your resume, contact info, work history, and optionally credentials for job boards. Before you run anything, understand exactly what stays on your machine and what leaves it.

### What stays local

- **`~/.applypilot/applypilot.db`** — SQLite database with every job discovered, scored, and applied to, plus your application status, Q&A knowledge base, and stored credentials
- **`~/.applypilot/profile.json`** — your full profile: name, contact info, work history, skills, salary expectations, EEO fields
- **`~/.applypilot/tailored_resumes/`** and **`~/.applypilot/cover_letters/`** — AI-generated documents per job
- **`~/.applypilot/.env`** — your API keys. Never committed to git.
- **`~/.gmail-mcp/`** — Gmail OAuth credentials. Never committed to git.

None of this is uploaded to any ApplyPilot server. There is no ApplyPilot cloud. The project is fully local.

### What gets sent to external APIs

| What's sent | Where | Why |
|-------------|-------|-----|
| Your resume + job description | Gemini (Google) or OpenAI | Scoring and tailoring |
| Your resume + company info | Gemini or OpenAI | Cover letter generation |
| Form field contents + job URL | Claude (Anthropic) | Auto-apply stage |

**Your resume content is sent to Google and/or OpenAI** every time a job is scored or tailored. Review the privacy policies of [Google AI Studio](https://aistudio.google.com) and [OpenAI](https://openai.com/policies/privacy-policy).

### Passwords and credentials — be careful

`profile.json` can include account passwords for job boards. These are stored in plaintext in the local SQLite database and are included verbatim in LLM prompts sent to external providers during the auto-apply stage.

**Recommendations:**
- Do not store your primary password for anything
- If you use password storage here, create a unique password used only for job applications
- Better yet: rely on browser session auth (log in manually once, let the browser remember it) and leave the password field empty
- Use a dedicated job-search email address, not your primary one

### The auto-apply stage (Tier 3)

The auto-apply stage spawns a Claude Code subprocess with `--bypassPermissions`. This means Claude can take autonomous actions in the browser without asking for confirmation on each step. It is sandboxed with `--strict-mcp-config` to limit which browser tools it can access, but you are granting meaningful autonomy to an AI agent acting on your behalf.

Dry run first:

```bash
applypilot apply --dry-run --url <job_url>
```

---

## A Note from Alex

If you're using this tool — thank you. Genuinely. It means a lot to know something I built during one of the harder chapters of my career is useful to someone else going through the same grind.

This pipeline helped me apply to hundreds of jobs I would never have reached manually, get my materials in front of companies I cared about, and stay sane while doing it. I hope it does the same for you.

If it's been useful, the best way to say thanks is simple: [follow me on GitHub](https://github.com/ibarrajo) and star this repo — or any of my other projects while you're there. It signals to me that this work matters and gives me motivation to keep improving it.

---

**But here's the thing I want to be honest about:** running an automation that blasts every ATS and every job board shouldn't be the goal of your job search — and I say that as someone who built this tool and used it himself.

The signal-to-noise problem in hiring is already catastrophic. ATS inboxes are flooded. Recruiters are overwhelmed. Candidates are invisible. There's an entire industry that exists just to help people game a system that was never designed for humans in the first place. Adding more volume doesn't fix that — it makes it worse for everyone, including you.

I built ApplyPilot as a force multiplier for a *focused* search: high-fit jobs, properly tailored materials, applied efficiently. Not a firehose.

The deeper problem is the matching problem itself — the **n×m problem** of connecting the right job seekers to the right employers at scale. I spent three years at [Jobscan](https://www.jobscan.co) as a lead engineer thinking about exactly this. We built resume optimization tuned per company and ATS, job trackers, job search tools, and coaching tools for career professionals — all trying to close the gap between what candidates bring and what employers can actually see. It's hard. The current system is structurally broken.

That's why, at a recent [Venture Mechanics](https://venturemechanics.com) AI Scalathon in Seattle, my team and I explored what I think is the natural next step: **agent-to-agent matching**. Instead of humans gaming systems built for computers, imagine employer agents and candidate agents negotiating directly — structured, transparent, and actually aligned with what both sides want. A distributed future where AI closes the gap rather than adding to the noise.

ApplyPilot is a tool for today's broken system. The agent-to-agent future is what I'm working toward next — and it's apparently been a running joke in VC and AI startup circles for years. Ask anyone building in the recruiting tech space and they'll tell you: "yeah, the real solution is obviously a2a, someone just needs to actually build it." A punchline that kept coming up in founder conversations, investor pitches, and hackathon hallways. My team and I are building that something. It's called **Pursit**.

Good luck out there. Reach me at [elninja.com](https://elninja.com) if you want to talk.

Oh, and — if you're a recruiter or hiring manager who ended up here: hi. I'm the one looking for a job. You just found me without a job board, a keyword filter, or an ATS — so maybe we can skip all that. [LinkedIn](https://www.linkedin.com/in/elninja) or [my resume](https://elninja.com/resume) work just fine.

— Alex Ibarra

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding standards, and PR guidelines.

---

## License

ApplyPilot is licensed under the [GNU Affero General Public License v3.0](LICENSE).

You are free to use, modify, and distribute this software. If you deploy a modified version as a service, you must release your source code under the same license.
