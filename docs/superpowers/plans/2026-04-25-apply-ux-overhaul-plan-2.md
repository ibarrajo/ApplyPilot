# Apply UX Overhaul — Plan 2: Switch apply pipeline to Chrome for Testing

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `chrome.py` actually launch Chrome for Testing 148 (not branded Chrome Stable). After this plan, `applypilot apply` runs against CfT, and the existing `--load-extension` block in chrome.py:1144 will work for the first time since Chrome 137 (the flag is silently rejected on branded builds, accepted on CfT — verified in plan 1's smoke).

**Architecture:** This is **plan 2 of 5** for the apply UX overhaul. (Plan 1 shipped: smoke validation + per-worker resume dir.) Scope is intentionally small — just update `config.get_chrome_path()` to prefer the CfT binary if installed, and verify the existing chrome.py flow works end-to-end on CfT. The full patchright launch-API refactor stays in plan 3 alongside the extension rewrite, because both need to coordinate launch flags + extension loading + CDP attach.

**Tech Stack:** Python 3.11, `pytest`, Chrome for Testing 148 (already installed in `~/.applypilot/chrome-for-testing/` from plan 1).

**Spec:** `docs/superpowers/specs/2026-04-25-apply-ux-overhaul-design.md`.

---

## Task 1: Pre-flight — verify Plan 1 baseline

**Files:**
- Read: (none) — verification only.

- [ ] **Step 1: Working tree clean, on main, top commit is plan-1's per-worker fix**

Run: `git status --short && git log --oneline -3`
Expected: empty `git status`. Top commit `183c5aa fix(apply): per-worker resume dir (audit #9)`.

If working tree is dirty or top commit is different, stop and report.

- [ ] **Step 2: 232 tests passing, CfT installed**

Run:
```bash
.venv/bin/pytest -q 2>&1 | tail -3
~/.applypilot/chrome-for-testing/chrome-linux64/chrome --version
```
Expected: `232 passed`. Chrome version `Google Chrome for Testing 148.0.7778.56` (or newer 148.x).

If CfT is missing, run `.venv/bin/python scripts/install_cft.py`.

---

## Task 2: Add CfT preference to `config.get_chrome_path()`

**Files:**
- Modify: `src/applypilot/config.py:38-80`

- [ ] **Step 1: Read the current Linux branch of `get_chrome_path`**

Run: `sed -n '38,82p' src/applypilot/config.py`

Note that the Linux candidates list is built by `shutil.which()` for `google-chrome`, `google-chrome-stable`, `chromium-browser`, `chromium`. We want CfT prepended to this list when installed.

- [ ] **Step 2: Edit the Linux branch to prepend CfT if installed**

Use Edit tool on `src/applypilot/config.py`:

**Old:**
```python
    else:  # Linux
        candidates = []
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
```

**New:**
```python
    else:  # Linux
        candidates = []
        # Prefer Chrome for Testing if installed: it is the only Chromium build
        # that still honors --load-extension (Chrome 137+ silently rejects it on
        # branded Stable/Beta/Dev/Canary). The apply layer relies on the
        # ApplyPilot extension loading.
        cft = Path.home() / ".applypilot" / "chrome-for-testing" / "chrome-linux64" / "chrome"
        if cft.exists():
            candidates.append(cft)
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
```

- [ ] **Step 3: Update the docstring to mention CfT preference**

**Old:**
```python
def get_chrome_path() -> str:
    """Auto-detect Chrome/Chromium executable path, cross-platform.

    Override with CHROME_PATH environment variable.
    """
```

**New:**
```python
def get_chrome_path() -> str:
    """Auto-detect Chrome/Chromium executable path, cross-platform.

    On Linux, prefers Chrome for Testing at
    ~/.applypilot/chrome-for-testing/chrome-linux64/chrome if installed —
    CfT is the only branded Chromium build that still accepts
    --load-extension (required by the apply layer).

    Override with CHROME_PATH environment variable.
    """
```

- [ ] **Step 4: Smoke check — running config.get_chrome_path() returns CfT**

Run:
```bash
.venv/bin/python -c "
from applypilot import config
import os
# Make sure CHROME_PATH override doesn't shadow the CfT preference
os.environ.pop('CHROME_PATH', None)
print(config.get_chrome_path())
"
```
Expected: `/home/elninja/.applypilot/chrome-for-testing/chrome-linux64/chrome`.

If a different binary returns: CfT might not be installed, or `cft.exists()` returns False — verify with `ls ~/.applypilot/chrome-for-testing/chrome-linux64/chrome`.

---

## Task 3: Add a regression test for the CfT preference

**Files:**
- Create: `tests/test_chrome_path_cft_preference.py`

- [ ] **Step 1: Write the test**

Create `tests/test_chrome_path_cft_preference.py`:

```python
"""Plan 2: get_chrome_path() prefers Chrome for Testing if installed.

The apply layer requires --load-extension to be honored. Branded Chrome
Stable/Beta/Dev/Canary silently reject the flag since Chrome 137. CfT is
the supported automation Chrome that still accepts it. The fix in
config.get_chrome_path puts CfT at the head of the candidate list on
Linux so that any installed CfT wins over a system google-chrome.
"""
from __future__ import annotations

import platform
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.mark.skipif(platform.system() != "Linux", reason="CfT preference is Linux-only")
class TestChromePathCftPreference:
    def test_cft_preferred_when_installed(self, tmp_path, monkeypatch):
        """If CfT is at ~/.applypilot/chrome-for-testing/..., it wins over
        any system google-chrome on PATH."""
        # Pretend $HOME points at tmp_path
        fake_home = tmp_path
        cft_bin = fake_home / ".applypilot" / "chrome-for-testing" / "chrome-linux64" / "chrome"
        cft_bin.parent.mkdir(parents=True)
        cft_bin.write_text("#!/bin/sh\necho fake-cft\n")
        cft_bin.chmod(0o755)

        # Pretend google-chrome exists on PATH
        sys_chrome = tmp_path / "bin" / "google-chrome"
        sys_chrome.parent.mkdir()
        sys_chrome.write_text("#!/bin/sh\necho fake-system-chrome\n")
        sys_chrome.chmod(0o755)

        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("PATH", str(sys_chrome.parent))
        monkeypatch.delenv("CHROME_PATH", raising=False)

        # Reload config so Path.home() picks up the new HOME
        import importlib

        import applypilot.config as config_module
        importlib.reload(config_module)

        result = config_module.get_chrome_path()
        assert result == str(cft_bin), \
            f"Expected CfT at {cft_bin}, got {result}"

    def test_falls_back_to_system_chrome_when_cft_missing(self, tmp_path, monkeypatch):
        """If CfT is NOT installed, the system google-chrome (or chromium) wins."""
        fake_home = tmp_path  # No CfT subtree under here

        sys_chrome = tmp_path / "bin" / "google-chrome"
        sys_chrome.parent.mkdir()
        sys_chrome.write_text("#!/bin/sh\necho fake-system-chrome\n")
        sys_chrome.chmod(0o755)

        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("PATH", str(sys_chrome.parent))
        monkeypatch.delenv("CHROME_PATH", raising=False)

        import importlib

        import applypilot.config as config_module
        importlib.reload(config_module)

        result = config_module.get_chrome_path()
        assert result == str(sys_chrome), \
            f"Expected system chrome at {sys_chrome}, got {result}"

    def test_chrome_path_env_overrides_cft(self, tmp_path, monkeypatch):
        """CHROME_PATH env var overrides everything, including CfT."""
        fake_home = tmp_path
        cft_bin = fake_home / ".applypilot" / "chrome-for-testing" / "chrome-linux64" / "chrome"
        cft_bin.parent.mkdir(parents=True)
        cft_bin.write_text("#!/bin/sh\n")
        cft_bin.chmod(0o755)

        override = tmp_path / "my-custom-chrome"
        override.write_text("#!/bin/sh\n")
        override.chmod(0o755)

        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("CHROME_PATH", str(override))

        import importlib

        import applypilot.config as config_module
        importlib.reload(config_module)

        result = config_module.get_chrome_path()
        assert result == str(override), f"CHROME_PATH override failed: got {result}"
```

- [ ] **Step 2: Run the new tests — should PASS (we already implemented the fix in Task 2)**

Run: `.venv/bin/pytest tests/test_chrome_path_cft_preference.py -v 2>&1 | tail -10`
Expected: 3 tests pass.

If any fail: re-read config.py:38-80 and verify the CfT prepend logic landed correctly.

- [ ] **Step 3: Run the full suite — should be 235 passing (232 + 3 new)**

Run: `.venv/bin/pytest -q 2>&1 | tail -3`
Expected: `235 passed`.

If less, check for regressions caused by the importlib.reload pattern (in rare cases it can confuse other tests that already imported config). If a previously-green test now fails, check whether it imports config eagerly at module level — if so, that test may need reordering or its own monkeypatch.

---

## Task 4: End-to-end verify — chrome.py launches CfT with extension

**Files:**
- (None — verification step using existing scripts.)

- [ ] **Step 1: Run a launch-and-attach smoke through chrome.py's actual code**

Run:
```bash
.venv/bin/python <<'PY'
"""Verify chrome.py uses CfT when called via the public API."""
import os
os.environ.pop("CHROME_PATH", None)

from applypilot import config
from applypilot.apply import chrome

# 1. Confirm the resolved binary is CfT.
exe = config.get_chrome_path()
print(f"Resolved chrome path: {exe}")
assert "chrome-for-testing" in exe, f"Expected CfT, got {exe}"

# 2. Launch a worker via launch_chrome().
print("Launching worker 9 on port 9231...")
proc = chrome.launch_chrome(worker_id=9, port=9231, headless=True, total_workers=1)
print(f"  Process started, pid={proc.pid}")

# 3. Confirm the process is alive and the CDP port is up.
import time
import urllib.request
import json
time.sleep(1)
try:
    with urllib.request.urlopen("http://localhost:9231/json/version", timeout=5) as r:
        data = json.load(r)
    print(f"  CDP responded: Browser={data.get('Browser')!r}")
    assert "Chrome" in data.get("Browser", ""), f"Unexpected browser: {data}"
finally:
    print("Cleaning up...")
    chrome.cleanup_worker(9, proc)
    print("Done.")
PY
```

Expected:
```
Resolved chrome path: /home/elninja/.applypilot/chrome-for-testing/chrome-linux64/chrome
Launching worker 9 on port 9231...
  Process started, pid=...
  CDP responded: Browser='Chrome/148.0.7778.56' (or similar)
Cleaning up...
Done.
```

If CDP doesn't respond:
- Check `~/.applypilot/chrome-workers/worker-9/` exists
- Check `chrome.py:1101` is reading the correct binary
- Run `chrome.py`'s `launch_chrome` with headless=False to see Chrome window appear

If CDP responds but browser version is wrong (e.g. branded Chrome):
- `CHROME_PATH` env var is leaking from the shell — re-run with `env -i HOME=$HOME PATH=$PATH .venv/bin/python ...`

---

## Task 5: Commit + decision log + memory update

**Files:**
- Modify: `CLAUDE.md` — append decision #33
- Modify: `~/.claude/projects/-home-elninja-Code-ApplyPilot/memory/project_apply_ux_overhaul.md`

- [ ] **Step 1: Append decision #33 to CLAUDE.md**

Find the row containing `| 32 | Per-worker resume dir |` and append a new row directly after it:

```markdown
| 33 | Apply uses Chrome for Testing | 2026-04-25. `config.get_chrome_path()` now prefers `~/.applypilot/chrome-for-testing/chrome-linux64/chrome` over system `google-chrome` on Linux. CfT is the only branded Chromium build that still accepts `--load-extension` after Chrome 137 (Stable/Beta/Dev/Canary all silently reject it). Existing chrome.py extension-load block at line 1144 finally works in production. CHROME_PATH env var still overrides. Plan 2 of 5 for the apply UX overhaul. |
```

- [ ] **Step 2: Update memory file**

Use Edit on `/home/elninja/.claude/projects/-home-elninja-Code-ApplyPilot/memory/project_apply_ux_overhaul.md`. Replace the `**Status:**` line with:

```markdown
**Status:** Plans 1-2 shipped 2026-04-25. P1: smoke validation + per-worker resume dir. P2: `config.get_chrome_path()` prefers CfT 148 — apply pipeline now uses CfT in production. 235 tests green. Plans 3-5 still pending: chrome.py + patchright launch refactor + extension rewrite, HITL collapse + pause wiring, --no-hitl + cleanup + integration tests.
```

- [ ] **Step 3: Stage all changes**

Run:
```bash
git add src/applypilot/config.py \
        tests/test_chrome_path_cft_preference.py \
        CLAUDE.md
git diff --cached --stat
```
Expected: 3 files changed.

- [ ] **Step 4: Commit**

Run:
```bash
git commit -m "$(cat <<'EOF'
feat(apply): apply pipeline launches Chrome for Testing 148

config.get_chrome_path() now prefers
~/.applypilot/chrome-for-testing/chrome-linux64/chrome over the
system google-chrome on Linux. CfT is the only branded Chromium
build that still accepts --load-extension since Chrome 137 (which
silently rejects it on Stable/Beta/Dev/Canary). The existing
chrome.py extension-load block at launch_chrome():1144 finally
works in production.

Three new tests in tests/test_chrome_path_cft_preference.py:
- CfT preferred when installed (over system chrome)
- Falls back to system chrome when CfT missing
- CHROME_PATH env var overrides everything

Plan 2 of 5 for the apply UX overhaul. Spec at
docs/superpowers/specs/2026-04-25-apply-ux-overhaul-design.md.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 5: Final verification**

Run: `git log --oneline -4 && .venv/bin/pytest -q 2>&1 | tail -3`
Expected: top commit is the CfT-switch commit. `235 passed`.

---

## Plan 2 done. Ready for Plan 3.

After Plan 2 lands:
- `applypilot apply` actually uses Chrome for Testing 148 in production.
- The 232+3 = 235 baseline is green.
- The existing extension's `--load-extension` block in chrome.py finally takes effect.

**Plan 3 (next) will:** rewrite the extension (manifest + SW + content + popup) per spec §3.1; add the patchright launch wrapper to `chrome.py` (so launch fingerprints are stripped); wire the pause-cycle data flow (filtered events + form snapshot, single Done POST carrying the log).
