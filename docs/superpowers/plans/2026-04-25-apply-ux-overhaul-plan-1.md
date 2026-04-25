# Apply UX Overhaul — Plan 1: Smoke validation + per-worker resume dir

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Validate the runtime substrate (Chrome for Testing 148 + patchright + `--load-extension`) and fix the per-worker resume-copy race (audit #9 in the spec). After this plan, we know the new browser+wrapper combination works on this machine, and the existing apply pipeline no longer cross-pollinates resume files between concurrent workers.

**Architecture:** This is **plan 1 of 4** for the apply UX overhaul. It ships no user-facing changes and no functional regressions. Plan 2 will decompose `launcher.py` into orchestrator/result_handlers/hitl modules; plan 3 will rewrite the extension and wire the pause cycle; plan 4 will add `--no-hitl`, delete the standalone `human-review` command, and add integration tests. Plan 1 is the smallest reversible substrate.

**Tech Stack:** Python 3.11, `pytest`, `patchright` (new dep), Chrome for Testing 148.

**Spec:** `docs/superpowers/specs/2026-04-25-apply-ux-overhaul-design.md` (commit `3be319f`).

---

## Task 1: Pre-flight — verify baseline

**Files:**
- Read: (none) — verification only.

- [ ] **Step 1: Confirm working tree is clean**

Run: `git status --short`
Expected: empty output (no staged/unstaged changes).

If output is non-empty, stop and ask user how to proceed.

- [ ] **Step 2: Confirm baseline tests are green**

Run: `.venv/bin/pytest -q 2>&1 | tail -3`
Expected: a line containing `230 passed` (or higher).

If tests fail, stop. Do not proceed with plan changes on a red baseline.

- [ ] **Step 3: Confirm branch position**

Run: `git log --oneline -3`
Expected: top commit is `3be319f docs(apply): approved design spec for apply UX overhaul`.

---

## Task 2: Add patchright dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Install patchright into the venv**

Run: `.venv/bin/pip install 'patchright>=1.50'`
Expected: terminal shows `Successfully installed patchright-X.Y.Z` (or "Requirement already satisfied" if cached).

- [ ] **Step 2: Verify the import works**

Run: `.venv/bin/python -c "from patchright.sync_api import sync_playwright; print('ok')"`
Expected: `ok`.

If import fails: check `.venv/bin/pip show patchright` and re-install. Do not proceed until import succeeds.

- [ ] **Step 3: Add patchright to pyproject.toml dependencies**

Locate the `[project]` block's `dependencies` list. Find the line containing `playwright>=...`. Insert a new line directly after it:

```toml
    "patchright>=1.50",
```

(Preserve the existing trailing-comma style; align indentation with the existing entries.)

- [ ] **Step 4: Re-install to verify pyproject.toml is parseable**

Run: `.venv/bin/pip install -e . 2>&1 | tail -5`
Expected: ends with `Successfully installed applypilot-...` and no parse errors mentioning `patchright`.

- [ ] **Step 5: Run patchright's chromium download (one-time)**

Run: `.venv/bin/patchright install chromium 2>&1 | tail -5`
Expected: `chromium ... downloaded` or `is already installed`. This downloads patchright's bundled chromium (separate from CfT — patchright needs it for some launch paths; CfT is the one we'll actually use).

(Do not commit yet — bundled with Task 5.)

---

## Task 3: Install Chrome for Testing 148

**Files:**
- Create: `scripts/install_cft.py`

- [ ] **Step 1: Write `scripts/install_cft.py`**

Create the file with this content:

```python
#!/usr/bin/env python3
"""Download Chrome for Testing 148 to ~/.applypilot/chrome-for-testing/.

Idempotent: skips download if binary already present and version matches latest 148.x.
"""
import json
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

CFT_DIR = Path.home() / ".applypilot" / "chrome-for-testing"
TARGET_BIN = CFT_DIR / "chrome-linux64" / "chrome"
MAJOR = "148"


def latest_148_url() -> tuple[str, str]:
    """Return (version, download_url) for the latest CfT 148.x linux64 build."""
    feed = "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json"
    with urllib.request.urlopen(feed) as r:
        data = json.load(r)
    candidates = [v for v in data["versions"] if v["version"].startswith(f"{MAJOR}.")]
    if not candidates:
        sys.exit(f"No CfT {MAJOR}.x found in feed {feed}")
    latest = candidates[-1]
    chrome_dl = next(d for d in latest["downloads"]["chrome"] if d["platform"] == "linux64")
    return latest["version"], chrome_dl["url"]


def main() -> None:
    version, dl_url = latest_148_url()
    if TARGET_BIN.exists():
        try:
            cur = subprocess.check_output([str(TARGET_BIN), "--version"], text=True).strip()
        except subprocess.CalledProcessError:
            cur = ""
        if version in cur:
            print(f"CfT {version} already installed at {TARGET_BIN}")
            return
        print(f"Replacing existing CfT install (was: {cur!r}, want: {version})")
        shutil.rmtree(CFT_DIR / "chrome-linux64", ignore_errors=True)

    CFT_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = CFT_DIR / "chrome-linux64.zip"
    print(f"Downloading CfT {version} from {dl_url}...")
    urllib.request.urlretrieve(dl_url, zip_path)

    print("Extracting...")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(CFT_DIR)
    zip_path.unlink()

    if not TARGET_BIN.exists():
        sys.exit(f"Extraction failed: {TARGET_BIN} not found after unzip")
    TARGET_BIN.chmod(0o755)

    out = subprocess.check_output([str(TARGET_BIN), "--version"], text=True).strip()
    print(f"Installed: {out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the script**

Run: `.venv/bin/python scripts/install_cft.py`
Expected output (first run): `Downloading CfT 148.X.Y.Z from ...` / `Extracting...` / `Installed: Google Chrome for Testing 148.X.Y.Z`.

If it fails with a network error, retry once. If it fails with a JSON parse or feed error, stop and report.

- [ ] **Step 3: Verify idempotency**

Run: `.venv/bin/python scripts/install_cft.py`
Expected: `CfT 148.X.Y.Z already installed at /home/elninja/.applypilot/chrome-for-testing/chrome-linux64/chrome` (no re-download).

- [ ] **Step 4: Direct-launch sanity check**

Run: `~/.applypilot/chrome-for-testing/chrome-linux64/chrome --version`
Expected: `Google Chrome for Testing 148.X.Y.Z`.

(Do not commit yet — bundled with Task 5.)

---

## Task 4: Smoke launch — CfT + patchright + extension

**Files:**
- Create: `scripts/smoke_cft.py`

- [ ] **Step 1: Write `scripts/smoke_cft.py`**

Create the file:

```python
#!/usr/bin/env python3
"""Smoke test for the apply runtime substrate: CfT + patchright + --load-extension.

Three checks:
  A. Direct CfT launch with --load-extension does NOT print the
     'not allowed in Google Chrome, ignoring' warning.
  B. patchright can launch CfT and run a basic page navigation.
  C. The custom extension at src/applypilot/apply/extension/ is loaded
     (visible via chrome://extensions/ DOM query).

Exits 0 on full success; non-zero on any failure with a diagnostic line.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXT_DIR = REPO_ROOT / "src" / "applypilot" / "apply" / "extension"
CFT_BIN = Path.home() / ".applypilot" / "chrome-for-testing" / "chrome-linux64" / "chrome"


def check_a_direct_launch() -> None:
    """Verify --load-extension is not silently rejected."""
    if not CFT_BIN.exists():
        sys.exit(f"FAIL[A]: CfT binary missing at {CFT_BIN} — run scripts/install_cft.py first")
    if not EXT_DIR.exists():
        sys.exit(f"FAIL[A]: extension dir missing at {EXT_DIR}")

    with tempfile.TemporaryDirectory() as td:
        proc = subprocess.Popen(
            [
                str(CFT_BIN),
                f"--user-data-dir={td}",
                f"--load-extension={EXT_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
                "--headless=new",
                "--disable-gpu",
                "about:blank",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(3)
        proc.terminate()
        try:
            _, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate()
    if "not allowed in Google Chrome, ignoring" in err:
        sys.exit(f"FAIL[A]: --load-extension was silently rejected. stderr:\n{err}")
    print("PASS[A]: --load-extension accepted by CfT (no rejection warning)")


def check_b_patchright_launch() -> None:
    """Verify patchright can drive CfT."""
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as e:
        sys.exit(f"FAIL[B]: patchright import failed: {e}")

    with tempfile.TemporaryDirectory() as td:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=td,
                executable_path=str(CFT_BIN),
                headless=True,
                args=[
                    f"--load-extension={EXT_DIR}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            page = ctx.new_page()
            page.goto("about:blank", timeout=10_000)
            title = page.title()
            ctx.close()
    print(f"PASS[B]: patchright launched CfT and navigated (title={title!r})")


def check_c_extension_loaded() -> None:
    """Verify the extension is recognized by chrome://extensions/."""
    from patchright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as td:
        with sync_playwright() as p:
            # chrome://extensions requires non-headless to enumerate via shadow DOM
            # in some Chrome versions. Try headless first; fall back if empty.
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=td,
                executable_path=str(CFT_BIN),
                headless=False,
                args=[
                    f"--load-extension={EXT_DIR}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            try:
                page = ctx.new_page()
                page.goto("chrome://extensions/", timeout=10_000)
                # Wait for the extensions-manager element to render.
                page.wait_for_selector("extensions-manager", timeout=10_000)
                names = page.evaluate(
                    """
                    () => {
                      const m = document.querySelector('extensions-manager');
                      if (!m || !m.shadowRoot) return [];
                      const items = m.shadowRoot.querySelectorAll('extensions-item');
                      return Array.from(items).map(it => {
                        const name = it.shadowRoot && it.shadowRoot.querySelector('#name');
                        return name ? name.textContent.trim() : '(unknown)';
                      });
                    }
                    """
                )
            finally:
                ctx.close()
    if not names:
        sys.exit("FAIL[C]: chrome://extensions/ reported zero extensions (extension did not load)")
    print(f"PASS[C]: extension(s) loaded: {names}")


def main() -> None:
    print("Apply runtime smoke test")
    print(f"  CfT binary: {CFT_BIN}")
    print(f"  Extension : {EXT_DIR}")
    print()
    check_a_direct_launch()
    check_b_patchright_launch()
    check_c_extension_loaded()
    print()
    print("ALL CHECKS PASSED — runtime substrate is viable")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the smoke script**

Run: `.venv/bin/python scripts/smoke_cft.py`
Expected (full success):
```
Apply runtime smoke test
  CfT binary: /home/elninja/.applypilot/chrome-for-testing/chrome-linux64/chrome
  Extension : /home/elninja/Code/ApplyPilot/src/applypilot/apply/extension
PASS[A]: --load-extension accepted by CfT (no rejection warning)
PASS[B]: patchright launched CfT and navigated (title='')
PASS[C]: extension(s) loaded: ['ApplyPilot ...']
ALL CHECKS PASSED — runtime substrate is viable
```

If check A fails: investigate stderr — possibly CfT version older than expected, or --load-extension truly removed. Fall back to Ungoogled Chromium (see spec §5 / §8 risks).

If check B fails: confirm patchright install (`.venv/bin/pip show patchright`); confirm `executable_path` points to a valid binary.

If check C fails: extension manifest may be invalid. Check `~/.config/chromium/Default/Preferences` for an "extensions" key with errors. Spec section §3.1 calls for a manifest rewrite later (plan 3); for plan 1, it's enough that the existing manifest loads cleanly.

- [ ] **Step 3: Mark scripts executable**

Run: `chmod +x scripts/install_cft.py scripts/smoke_cft.py`

(No commit yet — bundled with Task 5.)

---

## Task 5: Commit smoke validation work

**Files:**
- Modify: `pyproject.toml` (from Task 2)
- Create: `scripts/install_cft.py` (from Task 3)
- Create: `scripts/smoke_cft.py` (from Task 4)

- [ ] **Step 1: Stage the files**

Run: `git add pyproject.toml scripts/install_cft.py scripts/smoke_cft.py`

- [ ] **Step 2: Verify staged content**

Run: `git diff --cached --stat`
Expected: 3 files changed, with `pyproject.toml` showing 1 line added and the two scripts showing many lines added.

- [ ] **Step 3: Commit**

Run:
```bash
git commit -m "$(cat <<'EOF'
chore(apply): add CfT + patchright smoke validation substrate

- Adds patchright>=1.50 to pyproject.toml deps (anti-fingerprint
  drop-in for playwright launch).
- scripts/install_cft.py: idempotent installer for Chrome for Testing
  148.x linux64 to ~/.applypilot/chrome-for-testing/.
- scripts/smoke_cft.py: 3-check smoke validation (load-extension
  accepted, patchright launches CfT, extension loaded in DOM).

Plan 1 of the apply UX overhaul: validates the runtime substrate
before any code change. Spec at
docs/superpowers/specs/2026-04-25-apply-ux-overhaul-design.md.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 4: Verify commit landed**

Run: `git log --oneline -2`
Expected: top commit is the smoke validation commit.

---

## Task 6: Write failing test for per-worker resume dir

**Files:**
- Create: `tests/test_per_worker_resume_dir.py`

- [ ] **Step 1: Read the existing test pattern we will mirror**

Run: `head -130 tests/test_prompt_doc_format.py`

Note `_MINIMAL_PROFILE`, `_MINIMAL_SEARCH`, `_setup_paths`, `_make_resume`, `_build_job`, and `_mock_db_calls`. Our new test file uses the same helpers (copied, not imported — keeping each test file self-contained).

- [ ] **Step 2: Write the failing test**

Create `tests/test_per_worker_resume_dir.py`:

```python
"""Per-worker resume copy isolation (audit #9 fix).

Today, prompt.build_prompt() copies the resume to APPLY_WORKER_DIR/'current/',
which is shared across concurrent workers — worker A's resume can land on
worker B's clean filename between B's "build prompt" and "spawn claude" steps.

After the fix: each worker writes to APPLY_WORKER_DIR/f'worker-{wid}/'.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


_MINIMAL_PROFILE = {
    "personal": {
        "full_name": "Test User",
        "preferred_name": "Test",
        "email": "test@example.com",
        "password": "hunter2",
        "phone": "555-000-0000",
        "address": "1 Main St",
        "city": "Seattle",
        "province_state": "WA",
        "country": "USA",
        "postal_code": "98101",
    },
    "work_authorization": {
        "legally_authorized_to_work": "Yes",
        "require_sponsorship": "No",
    },
    "availability": {"earliest_start_date": "Immediately"},
    "compensation": {"salary_expectation": "150000", "salary_currency": "USD"},
    "experience": {
        "years_of_experience_total": "10",
        "current_job_title": "Senior Software Engineer",
    },
    "eeo_voluntary": {},
    "skills_boundary": {},
    "resume_facts": {},
    "site_credentials": {},
    "files": {},
}


_MINIMAL_SEARCH = {
    "location": {
        "primary": "Seattle",
        "accept_patterns": ["Seattle", "Remote"],
        "linkedin_type_chars": 3,
    },
    "queries": [{"query": "software engineer", "tier": 1}],
}


def _setup_paths(tmp_path, monkeypatch):
    """Point config paths at a tmp dir, write a profile + search config.

    Returns the apply-workers dir so tests can assert its contents.
    """
    from applypilot import config

    app_dir = tmp_path / "applypilot_home"
    app_dir.mkdir()
    apply_worker_dir = app_dir / "apply-workers"
    apply_worker_dir.mkdir()

    profile_path = app_dir / "profile.json"
    profile_path.write_text(json.dumps(_MINIMAL_PROFILE), encoding="utf-8")

    search_path = app_dir / "searches.yaml"
    search_path.write_text(yaml.safe_dump(_MINIMAL_SEARCH), encoding="utf-8")

    monkeypatch.setattr(config, "APP_DIR", app_dir)
    monkeypatch.setattr(config, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(config, "SEARCH_CONFIG_PATH", search_path)
    monkeypatch.setattr(config, "APPLY_WORKER_DIR", apply_worker_dir)

    return apply_worker_dir


def _make_resume(tmp_path, ext: str) -> Path:
    """Create a fake resume .txt + counterpart file with the given extension."""
    resume_dir = tmp_path / "tailored"
    resume_dir.mkdir()
    txt = resume_dir / "acme_senior_engineer_abc123.txt"
    txt.write_text("Test User\nSenior Software Engineer\n", encoding="utf-8")
    doc = txt.with_suffix(f".{ext}")
    doc.write_bytes(b"fake-doc-content")
    return txt


def _build_job(resume_txt: Path) -> dict:
    return {
        "url": "https://example.com/job/1",
        "title": "Senior Software Engineer",
        "site": "acme",
        "application_url": "https://boards.greenhouse.io/acme/jobs/1",
        "fit_score": 9,
        "tailored_resume_path": str(resume_txt),
        "cover_letter_path": None,
    }


def _mock_db_calls(monkeypatch):
    """Stub out DB-dependent helpers so tests don't need a live DB."""
    from applypilot.apply import prompt as prompt_module
    monkeypatch.setattr(prompt_module, "get_all_qa", lambda **_kw: [])
    from applypilot import database
    monkeypatch.setattr(database, "get_accounts_for_prompt", lambda: {})


class TestPerWorkerResumeDir:
    def test_resume_lands_in_per_worker_dir(self, tmp_path, monkeypatch):
        """build_prompt(worker_id=N) must write to apply-workers/worker-N/."""
        apply_worker_dir = _setup_paths(tmp_path, monkeypatch)
        resume_txt = _make_resume(tmp_path, "pdf")
        _mock_db_calls(monkeypatch)

        from applypilot.apply.prompt import build_prompt
        job = _build_job(resume_txt)
        build_prompt(job, tailored_resume="Resume text", worker_id=3, doc_format="pdf")

        worker_dir = apply_worker_dir / "worker-3"
        assert worker_dir.exists(), \
            f"worker-3 dir not created. apply-workers contents: " \
            f"{sorted(p.name for p in apply_worker_dir.iterdir())}"
        upload = worker_dir / "Test_User_Resume.pdf"
        assert upload.exists(), \
            f"resume not in worker-3/. dir contents: " \
            f"{sorted(p.name for p in worker_dir.iterdir())}"
        # The shared 'current/' dir from the buggy behavior must NOT exist.
        assert not (apply_worker_dir / "current").exists(), \
            "shared 'current/' dir was created — fix did not take effect"

    def test_two_workers_do_not_collide(self, tmp_path, monkeypatch):
        """Workers 0 and 1 must write to separate dirs and not overwrite each other."""
        apply_worker_dir = _setup_paths(tmp_path, monkeypatch)
        resume_txt = _make_resume(tmp_path, "pdf")
        _mock_db_calls(monkeypatch)

        from applypilot.apply.prompt import build_prompt
        job = _build_job(resume_txt)
        build_prompt(job, tailored_resume="x", worker_id=0, doc_format="pdf")
        build_prompt(job, tailored_resume="x", worker_id=1, doc_format="pdf")

        w0 = apply_worker_dir / "worker-0" / "Test_User_Resume.pdf"
        w1 = apply_worker_dir / "worker-1" / "Test_User_Resume.pdf"
        assert w0.exists() and w1.exists()
        # Verify the files are independent (rewriting one does not touch the other).
        w0.write_bytes(b"WORKER0_VERSION")
        assert w1.read_bytes() != b"WORKER0_VERSION"
```

- [ ] **Step 3: Run the new tests to confirm they fail**

Run: `.venv/bin/pytest tests/test_per_worker_resume_dir.py -v 2>&1 | tail -20`
Expected: both tests FAIL. The first should fail on the `worker-3 dir not created` or `shared 'current/' dir was created` assertion. The second should fail similarly.

If either test PASSES, the implementation is somehow already in place — stop, investigate, and notify the user before continuing.

- [ ] **Step 4: Confirm full suite still has 230 baseline tests passing (no regression from fixture imports)**

Run: `.venv/bin/pytest -q --ignore=tests/test_per_worker_resume_dir.py 2>&1 | tail -3`
Expected: `230 passed`.

(No commit — failing tests don't go to git.)

---

## Task 7: Implement per-worker resume dir

**Files:**
- Modify: `src/applypilot/apply/prompt.py:585`
- Modify: `src/applypilot/apply/launcher.py:1369`

- [ ] **Step 1: Replace the shared dir line in prompt.py**

Use Edit tool on `src/applypilot/apply/prompt.py`:

**Old:**
```python
    dest_dir = config.APPLY_WORKER_DIR / "current"
```

**New:**
```python
    dest_dir = config.APPLY_WORKER_DIR / f"worker-{worker_id}"
```

Note: `build_prompt` already takes `worker_id: int = 0` as a parameter (see signature at line 548). No signature change needed.

- [ ] **Step 2: Fix the gen_prompt call site that drops worker_id**

Use Edit tool on `src/applypilot/apply/launcher.py`:

**Old:**
```python
    prompt = prompt_mod.build_prompt(job=job, tailored_resume=resume_text, doc_format=_doc_format)
```

**New:**
```python
    prompt = prompt_mod.build_prompt(job=job, tailored_resume=resume_text, worker_id=worker_id, doc_format=_doc_format)
```

Verify there is exactly one such call in launcher.py: `grep -n "build_prompt(" src/applypilot/apply/launcher.py` should now show TWO call sites, both passing `worker_id=worker_id`.

- [ ] **Step 3: Run the per-worker tests — should pass**

Run: `.venv/bin/pytest tests/test_per_worker_resume_dir.py -v 2>&1 | tail -15`
Expected: both tests PASS.

If they fail, re-read prompt.py around line 585 to confirm the substitution actually happened.

- [ ] **Step 4: Run the full suite — should be 232 passing (230 baseline + 2 new)**

Run: `.venv/bin/pytest -q 2>&1 | tail -3`
Expected: a line containing `232 passed`.

If less than 232 passes: another test relies on the `current/` path. Search: `grep -rn '"current"' tests/ src/applypilot/` and update any other references.

---

## Task 8: Commit + update decision log

**Files:**
- Modify: `CLAUDE.md` — append decision #32

- [ ] **Step 1: Append a new decision row to CLAUDE.md**

Open `CLAUDE.md`, find the security-decisions table (the one whose row #31 starts with "P0 state-machine rollout shipped"). Append a new row at the end of the table:

```markdown
| 32 | Per-worker resume dir | 2026-04-25. `build_prompt` now writes the resume copy to `APPLY_WORKER_DIR/worker-{wid}/` instead of the shared `APPLY_WORKER_DIR/current/`. Fixes audit #9 from the apply UX overhaul spec — concurrent workers no longer cross-pollute clean-filename uploads. `gen_prompt` debug helper updated to thread `worker_id` through. Plan 1 of 4 for the apply UX overhaul. |
```

(Match the existing table's column count and pipe alignment.)

- [ ] **Step 2: Stage all changes**

Run:
```bash
git add src/applypilot/apply/prompt.py \
        src/applypilot/apply/launcher.py \
        tests/test_per_worker_resume_dir.py \
        CLAUDE.md
```

- [ ] **Step 3: Verify staged content**

Run: `git diff --cached --stat`
Expected: 4 files changed: `prompt.py` (1 line changed), `launcher.py` (1 line changed), the new test file (many lines added), `CLAUDE.md` (1 line added).

- [ ] **Step 4: Commit**

Run:
```bash
git commit -m "$(cat <<'EOF'
fix(apply): per-worker resume dir (audit #9)

build_prompt now writes the resume copy to
APPLY_WORKER_DIR/worker-{wid}/ instead of the shared
APPLY_WORKER_DIR/current/. Concurrent workers no longer cross-
pollute clean-filename uploads between "build prompt" and "spawn
claude" steps. Also threads worker_id through gen_prompt's
build_prompt call (was defaulting to 0).

Two new tests in tests/test_per_worker_resume_dir.py cover the
isolated-dir landing and the two-worker non-collision case.

Plan 1 of 4 for the apply UX overhaul. Spec at
docs/superpowers/specs/2026-04-25-apply-ux-overhaul-design.md.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 5: Final verification**

Run: `git log --oneline -3 && .venv/bin/pytest -q 2>&1 | tail -3`
Expected: top commit is the per-worker fix; pytest line shows `232 passed`.

---

## Plan 1 done. Ready for Plan 2.

After Plan 1 lands:
- Smoke validation has confirmed CfT + patchright + `--load-extension` is a viable substrate.
- Resume-copy race (audit #9) is fixed.
- 232 tests green, no user-facing change.

**Plan 2 (next) will:** decompose `launcher.py` (3,273 lines) into `apply/orchestrator.py` + `apply/result_handlers.py` + `apply/hitl.py`. No behavior change — purely structural refactor that creates the homes for plan 3's new code.
