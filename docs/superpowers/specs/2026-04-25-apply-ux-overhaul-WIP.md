# Apply UX Overhaul — Design WIP (2026-04-25)

> **Status:** BLOCKED on user response to browser-choice question + 3 follow-up clarifying questions.
> **Resume by:** reading this file end-to-end, then re-asking question Q1 (browser choice) and continuing the brainstorming flow.

---

## 1. Why this exists

After P0 state-machine work shipped, user flagged that the apply (Tier 3) layer is "vibe coded" and unreliable:

> "extension often is broken, not tracking the right window, the HITL does not work, resume often does not work, so there is a lot to address and fix"

Inline audit followed, surfaced 11 issues. User then provided detailed product requirements (see §3). Browser-choice research completed (see §5). Three tactical fixes from the audit have already shipped (see §2). The bigger spec is paused mid-brainstorm.

---

## 2. Already shipped this session

These are done — do **not** re-do.

| What | Commit | Notes |
|---|---|---|
| `release_lock` state-machine fix | `a3f332f` | `applying → ready_to_apply` on lock release |
| Doc-format prompt cleanup | `4524130` | hard-coded "PDF" → `{doc_format.upper()}`, renamed `pdf_path` → `resume_doc_path` |
| Q&A KB format-aware filter + backfill | `1e00d93` | `get_qa`/`get_all_qa` accept `doc_format`; live DB had 2 stale `.pdf` entries fixed |
| Dead `inject_status_badge` removed | `5860c1e` | 177-line deletion, stale comment in launcher.py also removed |
| Decision log update | `8470cb8` | CLAUDE.md decision #31 (P0 ship + 4 known leaks) |

230 tests passing. Branch is `main`, 49 commits ahead of `origin/main`.

---

## 3. Audit findings (the 11 issues)

Captured during inline review of `launcher.py` (3,273 lines), `chrome.py`, `human_review.py`, `prompt.py`, `extension/`. Severity per code-review bar.

### Browser Control / Extension

- **#1 P0 Extension is dead code on Chrome Stable.** `chrome.py:1144` passes `--load-extension={path}` but Chrome 147 silently rejects it (Chrome 137 removed the flag from branded builds). Entire extension stack is unused: deploy logic in `setup_worker_profile` (`chrome.py:425-578`), per-worker config injection, `Preferences.extensions` manipulation. → Fixed by browser switch (§5) + extension repair.
- **#2 P0 `inject_status_badge` defined but never called.** ✅ DELETED (commit `5860c1e`).
- **#3 P1 Multi-window tracking via `document.hasFocus()` is unreliable** on Linux/X11 multi-monitor. → Solved by extension owning the worker_id-to-window mapping.

### HITL

- **#4 P1 Four near-duplicate HITL paths in `_worker_loop_body`** (lines 2725-2816 generic, 2640-2723 screening_questions, 2820-2895 login_required, 2779-2789 + 2870-2890 post-Chrome-crash recovery). Each runs: `mark_needs_human → start_listener → inject_banner → start_done_watcher → notify → wait → reset → relaunch`. → Collapse to one `_run_hitl(worker_id, port, job, reason, instructions, navigate_url)` helper. Estimated ~150 lines deleted.
- **#5 P2 Standalone `applypilot human-review` (port 7373)** duplicates in-pipeline HITL (port 7380+wid). → Extract shared `_build_banner_js` helper or kill standalone path.
- **#6 P1 "Done" button mechanism is fragile** — three layers between click and relaunch (JS fetch → HTTP server → hitl_event; OR Node-based CDP watcher polling `window.__ap_hitl_done`). Silent failures if Node/Playwright/npx breaks. → Add terminal fallback: "Is the form submitted? [y/n]".

### Resume Upload

- **#7 P0 Hard-coded "PDF" in agent prompt despite docx-first.** ✅ FIXED (commit `4524130`).
- **#8 P0 Q&A KB poisoned with stale `.pdf` entries.** ✅ FIXED (commit `1e00d93`).
- **#9 P1 Resume copy to `apply-workers/current/` is racy** — same dest_dir per-worker, can land between worker A's "build prompt" and "spawn claude" steps; worker A's claude reads worker B's resume. → Move to `apply-workers/worker-{wid}/`.

### Code Duplication / Multiple Ways

- **#10 P1 ~9 distinct paths to write `apply_status`.** → Funnel through `transition_state` or single `set_apply_outcome(url, outcome)` helper. Some already done (Tasks A/B/C of P0); rest is the four P0.5 leaks documented in CLAUDE.md decision #31.
- **#11 P2 CDP base ports scattered.** → One constant table at top of `chrome.py`.

---

## 4. User-stated requirements (verbatim → interpretation)

User's 2026-04-25 reply, condensed:

### Out

- CDP-injected always-on badge (sites fingerprint Playwright/CDP; badge also blocks viewport during normal operation).

### In (extension is the surface)

- **Liveness signal** — extension reports "actually running" (not just "Chrome is alive").
- **Multi-tab/window control** — application can span popups, OAuth flows, verification windows; extension tracks all of them.
- **User actions** — Mark Applied, Skip (terminal — never look at again), Take Over, Resume.
- **HITL toggle**:
  - Default mode (HITL on): agent stops on blocker, page banner shows what action is needed, user completes step, hits Resume.
  - `--no-hitl` mode: agent encounters blocker → save state → move on to next job in queue.
- **Banner during HITL only** — fingerprint risk acceptable here because user is already taking over.
- **Action log during pause** — extension logs user actions: clicks, form input (final values), navigation, new tabs/windows, actions taken in those windows. Right-sized: enough context for agent to continue, not overwhelming. On Resume, log feeds into agent prompt as "here's what the user did."
- **Per-job page tracking** — every tab/window opened during a job application is part of that job's process.

### Process / refactor

- Collapse the 4 HITL paths into one helper.
- Per-worker file scoping (audit #9).
- launcher.py decomposition (audit followup, ~3,273 lines → 3 files of ~1,100).

---

## 5. Browser research result — recommended pick

**Chrome for Testing (CfT) 148 linux64 + patchright**, full report in conversation transcript.

### Why CfT

When Chrome 137 removed `--load-extension` "from all branded Chrome builds," the Chromium maintainers explicitly carved out **Chromium and Chrome for Testing** as exceptions. CfT is Google's officially-maintained automation Chrome — same engine as branded stable, ships in lockstep, no policy blocks. Source: [chromium-extensions PSA](https://groups.google.com/a/chromium.org/g/chromium-extensions/c/1-g8EFx2BBY/m/S0ET5wPjCAAJ).

### Why patchright

Drop-in `playwright` Python replacement (3k+ stars, active April 2026). Strips standard automation fingerprints: `navigator.webdriver`, `--enable-automation`, `Runtime.enable` CDP leak. Pairs with `channel="chrome"` + persistent `user_data_dir` for real-Chrome cookie shape.

### Open validation questions

1. Does CfT linux64 ship working Widevine? (test via Netflix landing or known DRM CAPTCHA site)
2. patchright × playwright-mcp interop — patchright covers launch fingerprints; the agent's playwright-mcp connects via CDP and issues its own commands. The CDP-client-side stealth (Runtime.enable trick) only works if patchright is the CDP client too. Net: still much better than today, but not perfect.
3. LinkedIn "BrowserGate" — LinkedIn probes 6,000+ extension `web_accessible_resources` for fingerprinting. Our custom extension WILL be a signal once installed. Mitigation: empty `web_accessible_resources` in manifest, consider per-worker-randomized extension IDs.
4. MV3 service worker lifetime — Chrome kills idle SWs after ~30s. Action-log + status logic must survive SW restart (offscreen documents or chrome.alarms patterns).

### Rejected alternatives

- **Branded Chrome + `Extensions.loadUnpacked` via `--remote-debugging-pipe`**: requires rewriting multi-worker port model (pipe ↔ port mutually exclusive); undocumented flag.
- **Ungoogled Chromium**: viable fallback, but lags upstream + "no Google pings" can itself be a fingerprint signal.
- **Brave / Edge / Chrome Beta/Dev/Canary**: all branded → all blocked.
- **Camoufox (Firefox fork)**: would require porting our MV3 extension.
- **Playwright Chromium**: doesn't have real-Chrome cookie shape.

### Proposed first action when unblocked

Three reversible smoke-test steps:

- **A.** Download CfT 148 linux64 to `~/.applypilot/chrome-for-testing/`. Set `CHROME_PATH` env var. Spin up one worker, hit a tough site (LinkedIn Easy Apply or Workday tenant). See if it logs in clean.
- **B.** `pip install patchright`. Swap `from playwright.sync_api` → `from patchright.sync_api` in our launch path. Re-test.
- **C.** Drop existing `src/applypilot/apply/extension/` into CfT via `--load-extension={path}`. Confirm popup loads.

If A/B/C succeed, we can finalize the design. If any fail, fall back to Ungoogled Chromium.

---

## 6. Decisions made so far

- ✅ Chrome extension is the user-facing surface (NOT CDP-injected always-on badge).
- ✅ HITL banner injection STAYS but only during pause events.
- ✅ HITL paths collapse to one helper.
- ✅ Per-worker file scoping for resumes.
- ✅ launcher.py decomposes into ~3 files.
- ✅ Doc-format consistency sweep (already shipped).
- ⏳ Browser pick: leaning CfT + patchright, awaiting user OK.
- ⏳ All other extension/HITL design details: blocked.

---

## 7. Open questions queue (in priority order)

1. **Q1 (BLOCKED)** — Approve CfT + patchright as the runtime? Or proceed with smoke tests A/B/C above before committing?
2. **Q2** — Action log granularity. Clicks + final field values + navigation + new tabs/windows + actions in those — but precise shape? E.g.:
   - Per-event JSON: `{type: "input", selector: "#email", value: "alex@..."}` ?
   - Or summary: `{tabs_opened: 3, fields_filled: 12, last_action: "click submit"}` ?
   - Or both, with the agent prompt injecting only the summary by default and detail on demand?
3. **Q3** — Tab/window tracking model: does the extension passively log all activity, or actively coordinate which tab is "the application tab" vs ancillary popups? Coordinate model is more powerful but more complex (extension needs to know roles).
4. **Q4** — Pause-survival across launcher restarts. If user kills `applypilot apply` mid-pause, can they resume later? Stored where — DB column? File on disk? Extension's local storage?
5. **Q5** — Skip semantics. When user clicks Skip in the extension popup, mark as `manual_only` (could be revisited) or `archived` (terminal, never look at again)? User's wording suggests `archived`, but worth confirming.

---

## 8. Files to touch (preliminary mapping)

- `src/applypilot/apply/launcher.py` → split into:
  - `apply/orchestrator.py` (worker_loop, main, _worker_loop_body)
  - `apply/result_handlers.py` (mark_*, reset_*, release_lock, run_job result parsing, mark_result)
  - `apply/hitl.py` (the unified _run_hitl helper)
- `src/applypilot/apply/extension/` → repair + extend (manifest, popup.js, content.js, background.js)
  - liveness ping endpoint
  - tab/window tracker
  - action log (with throttling/dedup)
  - Mark Applied / Skip buttons
  - HITL toggle indicator
- `src/applypilot/apply/chrome.py` → switch to CfT binary, integrate patchright launch flags, kill `--load-extension` removal block, simplify `setup_worker_profile`
- `src/applypilot/apply/prompt.py` → inject action-log section into agent prompt on resume after pause
- `src/applypilot/apply/human_review.py` → either consolidate or kill standalone path (Q5 area)
- `src/applypilot/database.py` → maybe a `job_pages` table for per-job tab/window history? Or a JSON column?
- `src/applypilot/cli.py` → `--no-hitl` becomes default-aware mode flag

---

## 9. Resume instructions

When picking this up:

1. Read this file end-to-end.
2. Read CLAUDE.md decision #31 (P0 done, P0.5 leaks tracked).
3. Run `git log --oneline -10` — verify the 5 commits in §2 are present.
4. Run `.venv/bin/pytest -q` — should be 230 passing.
5. Re-pose Q1 to user. If they say "go", run smoke tests A/B/C (§5).
6. Once browser is settled, work through Q2–Q5 in order.
7. Once all 5 questions are answered, write the proper spec doc (replace this WIP file with `docs/superpowers/specs/YYYY-MM-DD-apply-ux-overhaul-design.md`), get user review, then writing-plans skill for the implementation plan.

Don't skip the brainstorming flow even though the audit is done — the user-stated requirements need to be reflected back as a design they approve.

---

## 10. Tasks

- Task #33 (in_progress): Spec extension + HITL overhaul → still in progress, paused on Q1.
- Task #34 (completed): Tactical fixes from apply audit → done (commits §2).
- Tasks #30–32 (completed): P0 state machine rollout → done.
