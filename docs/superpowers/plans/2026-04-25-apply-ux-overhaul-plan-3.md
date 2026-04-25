# Apply UX Overhaul — Plan 3: HITL collapse into single `_run_hitl` helper

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the two near-duplicate HITL paths in `_worker_loop_body` (generic at `launcher.py:2725-2816` and `login_required` at `launcher.py:2820-2895`) into a single `_run_hitl(...)` helper. Pure refactor: same observable behavior, ~150 fewer lines of duplication, single place to add the terminal-stdin fallback later (audit #6). Screening-questions HITL stays separate — it has a fundamentally different mechanism (TUI Q&A, no banner).

**Architecture:** This is **plan 3 of 5** for the apply UX overhaul. Plans 1-2 shipped: smoke validation, per-worker resume dir, CfT switch. Plan 3 keeps `_run_hitl` inside `launcher.py` for now — extracting it into `apply/hitl.py` happens in plan 5 alongside the broader decompose. Putting it in its own module today would make this plan riskier without delivering more value.

**Tech Stack:** Python 3.11, `pytest`. No new deps.

**Spec:** `docs/superpowers/specs/2026-04-25-apply-ux-overhaul-design.md` (audit #4, §3.3).

---

## Task 1: Pre-flight

**Files:**
- Read: (none)

- [ ] **Step 1: Verify baseline state**

Run: `git status --short && git log --oneline -3 && .venv/bin/pytest -q 2>&1 | tail -3`
Expected: empty `git status`. Top commit `707cc2f feat(apply): apply pipeline launches Chrome for Testing 148`. `235 passed`.

If baseline is wrong, stop.

---

## Task 2: Identify and document the duplicate ranges

**Files:**
- Read: `src/applypilot/apply/launcher.py:2640-2900`

- [ ] **Step 1: Confirm the two near-duplicate paths still live where the spec says**

Run: `sed -n '2722,2725p' src/applypilot/apply/launcher.py`
Expected: a comment line `# --- General HITL: keep Chrome open, inject banner, wait ---`.

Run: `sed -n '2820,2825p' src/applypilot/apply/launcher.py`
Expected: a comment block including `# login_required: route to HITL with banner + wait`.

If the line numbers shifted, locate each block with grep:
```bash
grep -n "General HITL: keep Chrome open" src/applypilot/apply/launcher.py
grep -n "login_required: route to HITL" src/applypilot/apply/launcher.py
```
Use the discovered ranges in subsequent tasks.

---

## Task 3: Add a smoke regression test for the helper API surface

**Files:**
- Create: `tests/test_run_hitl_smoke.py`

This test guards two things: (a) `_run_hitl` exists with the expected signature, (b) it can be imported without crashing other module-load. Full integration tests (with real Chrome + extension) come in Plan 5's integration suite.

- [ ] **Step 1: Write the test**

Create `tests/test_run_hitl_smoke.py`:

```python
"""Plan 3 smoke: _run_hitl exists with the expected signature.

The helper collapses two near-duplicate HITL paths in _worker_loop_body
(generic + login_required). This test pins the public-ish API so future
edits don't accidentally break callers.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_run_hitl_helper_exists():
    from applypilot.apply.launcher import _run_hitl
    assert callable(_run_hitl), "_run_hitl must be a callable"


def test_run_hitl_signature_has_required_params():
    from applypilot.apply.launcher import _run_hitl
    sig = inspect.signature(_run_hitl)
    params = sig.parameters
    # Required positional/kw args. If any of these are renamed, callers in
    # _worker_loop_body must be updated atomically.
    for name in (
        "worker_id", "port", "job", "reason", "instructions", "navigate_url",
        "duration_ms",
    ):
        assert name in params, f"_run_hitl missing required parameter: {name}"


def test_run_hitl_returns_tuple_of_result_and_duration_and_qs():
    """The helper must return the same shape that callers slot into:
        result, duration_ms, screening_qs = _run_hitl(...)
    We can't actually invoke it (would need real Chrome), but we can assert
    the documented return type via __annotations__.
    """
    from applypilot.apply.launcher import _run_hitl
    ann = _run_hitl.__annotations__.get("return")
    assert ann is not None, \
        "_run_hitl must declare a return-type annotation so callers know the shape"
    # Stringify defensively — annotation may be a typing.Tuple or a string forward-ref.
    s = str(ann)
    assert "tuple" in s.lower() or "Tuple" in s, \
        f"_run_hitl return annotation should be a tuple, got: {s}"
```

- [ ] **Step 2: Run the new tests — ALL THREE should fail (helper doesn't exist yet)**

Run: `.venv/bin/pytest tests/test_run_hitl_smoke.py -v 2>&1 | tail -10`
Expected: 3 FAILS with `ImportError: cannot import name '_run_hitl'` or similar.

If any pass: a `_run_hitl` symbol already exists somewhere — investigate before continuing.

(No commit — failing tests stay local until Task 5 lands the helper.)

---

## Task 4: Implement `_run_hitl` helper

**Files:**
- Modify: `src/applypilot/apply/launcher.py` — insert helper above `worker_loop` (around line 2440, just before `def worker_loop`)

- [ ] **Step 1: Find the insertion point**

Run: `grep -n "^def worker_loop" src/applypilot/apply/launcher.py`
Expected: one match around line 2443.

Insert `_run_hitl` directly above this function (so it's defined before `_worker_loop_body` uses it).

- [ ] **Step 2: Insert the helper**

Use Edit tool with this `old_string` (the line above `def worker_loop`):

**Old:**
```python
def worker_loop(worker_id: int = 0, limit: int = 1,
```

**New:**
```python
def _run_hitl(
    worker_id: int,
    port: int,
    job: dict,
    reason: str,
    instructions: str,
    navigate_url: str,
    duration_ms: int,
    *,
    headless: bool = False,
    ats_slug: str | None = None,
    total_workers: int = 1,
    model: str = "sonnet",
    dry_run: bool = False,
    no_hitl: bool = False,
    chrome_proc=None,
    add_event=None,
    update_state=None,
    stop_event=None,
) -> tuple[str, int, list[dict]] | None:
    """Block on a needs_human pause; return the post-resume run_job result.

    Replaces the two near-duplicate HITL paths in _worker_loop_body
    (generic + login_required). Steps:

      1. mark_needs_human(...) — DB row says needs_human, prevents stale-lock theft.
      2. If no_hitl: return None (caller should break and move on to next job).
      3. Start hitl_listener (HTTP server on port 7380+wid) for /api/done/{hash}.
      4. Inject banner via CDP → page.
      5. Start the Node-based done watcher (polls window.__ap_hitl_done).
      6. notify_human_needed (desktop notification).
      7. Update worker state to "waiting_human".
      8. Wait on hitl_event with chrome-crash recovery.
      9. reset_needs_human(...) — DB row back to its pre-pause state.
      10. Re-launch agent on same Chrome, retry up to 3× on transient errors.

    Returns (result, duration_ms, screening_qs) from the post-resume run_job,
    or None if no_hitl or stop was signaled.
    """
    import hashlib
    import threading
    import time

    # 1. Persist the needs_human row.
    mark_needs_human(job["url"], reason, navigate_url, instructions, duration_ms)

    # 2. --no-hitl: park the job and bail.
    if no_hitl:
        if add_event:
            add_event(f"[W{worker_id}] --no-hitl: parking '{reason}' and moving on")
        if update_state:
            update_state(worker_id, last_action=f"parked: {reason[:25]}")
        return None

    # 3. HTTP listener + 4. banner + 5. done watcher.
    job_hash = hashlib.sha256(job["url"].encode()).hexdigest()[:12]
    hitl_event = threading.Event()
    hitl_port = _start_hitl_listener(worker_id, hitl_event, job_hash)

    _inject_banner_for_worker(worker_id, port, job, reason, hitl_port,
                              navigate_url=navigate_url, instructions=instructions)
    from applypilot.apply.human_review import _start_done_watcher
    _watcher = _start_done_watcher(port, hitl_port, job_hash)

    # 6. Desktop notify + 7. worker state.
    notify_human_needed(job, reason, navigate_url)
    if add_event:
        add_event(f"[W{worker_id}] WAITING for human: {reason[:20]}")
    if update_state:
        update_state(worker_id, status="waiting_human",
                     last_action=f"WAITING: {reason[:25]}")
    with _worker_state_lock:
        ws = _worker_state.get(worker_id)
    if ws is not None:
        _saved = None
        try:
            from applypilot.database import close_connection, get_qa
            _saved = get_qa(f"HITL:{job.get('site', '')}:{reason}")
            close_connection()
        except Exception:
            pass
        ws.update({"status": "waiting_human", "reason": reason,
                   "instructions": instructions,
                   "saved_instruction": _saved,
                   "hitl_watcher_proc": _watcher})
    _register_waiting(worker_id, "waiting_human")

    # 8. Wait, with Chrome-crash recovery.
    while stop_event is None or not stop_event.is_set():
        if hitl_event.wait(timeout=5.0):
            break
        if chrome_proc and chrome_proc.poll() is not None:
            if add_event:
                add_event(f"[W{worker_id}] Chrome crashed during HITL; relaunching...")
            try:
                chrome_proc = launch_chrome(worker_id, port=port,
                                            headless=headless, ats_slug=ats_slug,
                                            total_workers=total_workers)
                _inject_banner_for_worker(worker_id, port, job, reason,
                                          hitl_port, navigate_url=navigate_url,
                                          instructions=instructions)
            except Exception:
                logger.debug("Chrome relaunch during HITL failed", exc_info=True)
    _stop_hitl_listener(worker_id)
    _unregister_waiting(worker_id)
    if stop_event is not None and stop_event.is_set():
        return None

    # 9. Reset DB row.
    reset_needs_human(job["url"])

    # 10. Re-launch agent with transient-error retry.
    last_result = None
    last_dur = 0
    last_qs: list[dict] = []
    for _attempt in range(3):
        if add_event:
            add_event(f"[W{worker_id}] Human done, relaunching agent"
                      f" (attempt {_attempt + 1}/3)...")
        if update_state:
            update_state(worker_id, status="applying",
                         last_action=f"relaunching after HITL (attempt {_attempt + 1})",
                         start_time=time.time(), actions=0)
        last_result, last_dur, last_qs = run_job(
            job, port=port, worker_id=worker_id,
            model=model, dry_run=dry_run, skip_tab_reset=True)
        _hitl_reason = last_result.split(":", 1)[-1] if ":" in last_result else last_result
        if _hitl_reason not in _HITL_TRANSIENT_ERRORS:
            break
        if stop_event is not None and stop_event.is_set():
            break
        if _attempt < 2:
            if add_event:
                add_event(f"[W{worker_id}] Transient ({_hitl_reason}), retrying in 30s...")
            time.sleep(30)
    return last_result, last_dur, last_qs


def worker_loop(worker_id: int = 0, limit: int = 1,
```

- [ ] **Step 3: Run the smoke tests for `_run_hitl` — should now PASS**

Run: `.venv/bin/pytest tests/test_run_hitl_smoke.py -v 2>&1 | tail -8`
Expected: 3 passed.

If failures: re-read the helper insertion. Common issues — typo in signature, missing import inside the function body.

- [ ] **Step 4: Run the full suite — should be 238 passing (235 + 3 new)**

Run: `.venv/bin/pytest -q 2>&1 | tail -3`
Expected: `238 passed`.

If a previously-passing test now fails: most likely the helper introduced an import-time side effect. Check that the helper body uses local imports (`import hashlib`, `import threading`, `import time`) so module load is unaffected.

(No commit — call sites still need updating.)

---

## Task 5: Replace the generic HITL path with `_run_hitl(...)`

**Files:**
- Modify: `src/applypilot/apply/launcher.py:2725-2816` (or wherever the "General HITL" block lives)

- [ ] **Step 1: Locate the exact range to replace**

Run: `grep -n "# --- General HITL" src/applypilot/apply/launcher.py`
Note the line number. The block starts there.

Run: `sed -n '<that-line>,$p' src/applypilot/apply/launcher.py | sed -n '/relaunch = True/{p;q}' | head -5` to find where the block ends (the `relaunch = True` line right before `continue`).

The block is roughly: from `# --- General HITL: keep Chrome open` through `relaunch = True\n    continue` (about 90 lines).

- [ ] **Step 2: Edit — replace the entire block with a single `_run_hitl` call**

Use the Edit tool. The `old_string` is the full block from "# --- General HITL: keep Chrome open" through the `continue` statement that ends it. The `new_string` is:

```python
                    # --- General HITL: keep Chrome open, inject banner, wait ---
                    nh_instructions = _HITL_INSTRUCTIONS.get(
                        nh_reason, f"Human action required: {nh_reason}"
                    )
                    if nh_detail:
                        nh_instructions = f"{nh_instructions}\n\nAgent detail: {nh_detail}"

                    hitl_outcome = _run_hitl(
                        worker_id=worker_id, port=port, job=job,
                        reason=nh_reason, instructions=nh_instructions,
                        navigate_url=nh_url, duration_ms=duration_ms,
                        headless=headless, ats_slug=ats_slug,
                        total_workers=total_workers, model=model, dry_run=dry_run,
                        no_hitl=no_hitl, chrome_proc=chrome_proc,
                        add_event=add_event, update_state=update_state,
                        stop_event=_stop_event,
                    )
                    if hitl_outcome is None:
                        # no_hitl mode parked the job, or stop was signaled.
                        break
                    result, duration_ms, screening_qs = hitl_outcome
                    relaunch = True
                    continue
```

(Tip: for the Edit, you'll need the exact `old_string`. Read the block first via `sed -n` so you have the precise text including indentation.)

- [ ] **Step 3: Verify launcher.py still imports clean**

Run: `.venv/bin/python -c "from applypilot.apply import launcher; print('ok')"`
Expected: `ok`.

If syntax error: re-read the inserted block, check for indentation drift.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/pytest -q 2>&1 | tail -3`
Expected: `238 passed`.

If anything regresses, revert this Task 5 edit and re-investigate; the call site shape may need adjustment (e.g. `add_event` / `update_state` are closures from `_worker_loop_body` — confirm they're in scope).

(No commit yet.)

---

## Task 6: Replace the `login_required` HITL path with `_run_hitl(...)`

**Files:**
- Modify: `src/applypilot/apply/launcher.py:2820-2895`

- [ ] **Step 1: Locate the exact range**

Run: `grep -n "# login_required: route to HITL" src/applypilot/apply/launcher.py`

The block starts there. It ends at the `continue` statement after the 3-attempt retry loop. Roughly 75 lines.

- [ ] **Step 2: Edit — replace with `_run_hitl(...)` call**

The `old_string` is the entire `if reason == "login_required":` block (including ATS clear, mark_needs_human, listener setup, banner inject, watcher start, notify, wait loop, reset, retry loop, `relaunch = True`, `continue`).

The `new_string` is:

```python
                    if reason == "login_required":
                        if ats_slug:
                            clear_ats_session(ats_slug)
                        nh_url = job.get("application_url") or job["url"]
                        nh_instructions = _HITL_INSTRUCTIONS["login_required"]

                        hitl_outcome = _run_hitl(
                            worker_id=worker_id, port=port, job=job,
                            reason="login_required", instructions=nh_instructions,
                            navigate_url=nh_url, duration_ms=duration_ms,
                            headless=headless, ats_slug=ats_slug,
                            total_workers=total_workers, model=model, dry_run=dry_run,
                            no_hitl=no_hitl, chrome_proc=chrome_proc,
                            add_event=add_event, update_state=update_state,
                            stop_event=_stop_event,
                        )
                        if hitl_outcome is None:
                            break
                        result, duration_ms, screening_qs = hitl_outcome
                        relaunch = True
                        continue
```

- [ ] **Step 3: Verify import still works**

Run: `.venv/bin/python -c "from applypilot.apply import launcher; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/pytest -q 2>&1 | tail -3`
Expected: `238 passed`.

(No commit yet.)

---

## Task 7: Verify duplicates are gone + commit

**Files:**
- Verify: launcher.py size shrunk; `_inject_banner_for_worker` / `_start_hitl_listener` no longer called from `_worker_loop_body` directly (only from `_run_hitl`).

- [ ] **Step 1: Confirm the duplicate scaffolding is gone from `_worker_loop_body`**

Run:
```bash
sed -n '2440,3025p' src/applypilot/apply/launcher.py | grep -cE "_start_hitl_listener|_inject_banner_for_worker|_start_done_watcher"
```
Expected: `3` — those three calls now live ONLY inside `_run_hitl` (each one once). Before this plan, each appeared 2-3 times in worker_loop_body alone.

If the count is higher: a duplicate path wasn't replaced. Find with:
```bash
grep -n "_start_hitl_listener\|_inject_banner_for_worker\|_start_done_watcher" src/applypilot/apply/launcher.py
```

- [ ] **Step 2: Confirm line count shrank**

Run: `wc -l src/applypilot/apply/launcher.py`
Expected: roughly 3,140 lines (was 3,276 — about 130 fewer).

If it didn't shrink as expected: the helper was added but old code wasn't fully removed. Re-check Tasks 5 and 6.

- [ ] **Step 3: Run the full suite once more**

Run: `.venv/bin/pytest -q 2>&1 | tail -3`
Expected: `238 passed`.

- [ ] **Step 4: Append decision #34 to CLAUDE.md**

Find the row containing `| 33 | Apply uses Chrome for Testing |` and append after it:

```markdown
| 34 | HITL paths collapsed | 2026-04-25. Two near-duplicate HITL paths in `_worker_loop_body` (generic + `login_required`) collapsed into a single `_run_hitl(worker_id, port, job, reason, instructions, navigate_url, duration_ms, ...)` helper in `launcher.py`. ~130 line reduction. Screening-questions HITL stays separate (different mechanism — TUI Q&A, no banner). The terminal-stdin fallback (audit #6) and module extraction to `apply/hitl.py` are deferred to plan 5. Plan 3 of 5 for the apply UX overhaul. |
```

- [ ] **Step 5: Stage all changes**

Run:
```bash
git add src/applypilot/apply/launcher.py \
        tests/test_run_hitl_smoke.py \
        CLAUDE.md
git diff --cached --stat
```

- [ ] **Step 6: Commit**

Run:
```bash
git commit -m "$(cat <<'EOF'
refactor(apply): collapse 2 HITL paths into _run_hitl helper

Two near-duplicate HITL paths in _worker_loop_body (generic
needs_human + login_required) collapsed into a single
_run_hitl(worker_id, port, job, reason, instructions,
navigate_url, duration_ms, ...) helper. Same observable behavior:
mark_needs_human, listener+banner+watcher trio, wait with
Chrome-crash recovery, reset_needs_human, 3-attempt agent
relaunch with transient-error retry.

Net ~130 lines deleted. Both call sites in _worker_loop_body now
parameterize the differences (generic threads nh_detail; login
clears ats_session up front) and call into the same code.

Screening-questions HITL stays separate — its mechanism is the
TUI Q&A queue, not a banner. The terminal-stdin fallback for
broken Done buttons (audit #6) and the module extraction to
apply/hitl.py are deferred to plan 5.

Three new smoke tests in tests/test_run_hitl_smoke.py guard the
helper's signature so future edits don't accidentally break the
two callers.

Plan 3 of 5 for the apply UX overhaul. Spec at
docs/superpowers/specs/2026-04-25-apply-ux-overhaul-design.md.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 7: Final verification**

Run: `git log --oneline -5 && .venv/bin/pytest -q 2>&1 | tail -3`
Expected: top commit is the HITL collapse. `238 passed`.

---

## Plan 3 done. Ready for Plan 4.

After plan 3:
- 4 (audit-#4-cited) → 1 HITL paths in launcher.py.
- 238 tests green.
- ~130 fewer lines of duplicated scaffolding.
- One place to add the terminal-stdin fallback (audit #6) when plan 5 lands.

**Plan 4 (next) will:** patchright launch wrapper for chrome.py (CDP-side stealth via init scripts injected by patchright's `connect_over_cdp` after subprocess.Popen launch); per-job tab tracking in extension's `background.js` via `chrome.tabs.onCreated` + `openerTabId`; rewire popup.js to expose Mark Applied / Skip / Take Over / Resume buttons against the existing always-on HTTP server.
