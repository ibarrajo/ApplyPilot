"""Human-in-the-Loop (HITL) review server for parked apply jobs.

Starts a local HTTP server at localhost:7373. Presents a web UI listing
jobs that need human assistance (needs_human status). The user clicks
Start to open Chrome at the stuck page with an overlay banner, completes
the required action (e.g. create a Workday account), clicks Done in the
banner, and the apply agent immediately takes over to finish the application.

Usage:
    applypilot human-review
    applypilot human-review --port 7374 --no-browser
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from applypilot import config
from applypilot.apply.chrome import (
    HITL_CDP_PORT, HITL_WORKER_ID,
    launch_chrome, cleanup_worker, bring_to_foreground,
)

logger = logging.getLogger(__name__)

# In-memory session state keyed by job hash
# Each value: { "job": dict, "status": str, "result": str|None,
#               "chrome_proc": Popen|None, "log_offset": int }
_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()

# The single HITL Chrome process (one at a time in v1)
_hitl_chrome_proc = None
_hitl_chrome_lock = threading.Lock()


def _job_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:12]


def _cdp_list_targets(port: int) -> list[dict]:
    """List CDP targets (tabs). Returns [] if Chrome isn't running."""
    import urllib.request
    try:
        data = urllib.request.urlopen(
            f"http://localhost:{port}/json", timeout=3
        ).read()
        return json.loads(data)
    except Exception:
        return []


def _inject_banner(port: int, job: dict, server_port: int = 7373) -> bool:
    """Inject the HITL banner overlay via CDP using Node.js + @playwright/test.

    Returns True on success, False if injection failed.
    """
    h = _job_hash(job["url"])
    title = (job.get("title") or "Unknown Position").replace("\\", "\\\\").replace("'", "\\'")
    company = (job.get("site") or job.get("company") or "").replace("\\", "\\\\").replace("'", "\\'")
    score = job.get("fit_score", "?")
    instructions = (
        (job.get("needs_human_instructions") or "Complete the required action on this page.")
        .replace("\\", "\\\\").replace("'", "\\'")
    )

    js = _build_banner_js(h, title, company, score, instructions, server_port=server_port)
    # Escape backticks and backslashes for embedding in a JS template literal
    js_escaped = js.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")

    node_script = f"""
const {{ chromium }} = require('@playwright/test');
(async () => {{
  try {{
    const b = await chromium.connectOverCDP('http://localhost:{port}');
    const ctx = b.contexts()[0];
    const bannerJs = `{js_escaped}`;
    await ctx.addInitScript(bannerJs);
    const pages = ctx.pages();
    for (const p of pages) {{
      try {{ await p.evaluate(bannerJs); }} catch(e) {{}}
    }}
    await b.close();
    process.exit(0);
  }} catch(e) {{
    process.stderr.write(e.message + '\\n');
    process.exit(1);
  }}
}})();
"""

    try:
        result = subprocess.run(
            ["node", "-e", node_script],
            timeout=15,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning("Banner injection failed: %s", result.stderr[:200])
            return False
        return True
    except Exception as e:
        logger.warning("Banner injection error: %s", e)
        return False


def _build_banner_js(hash_: str, title: str, company: str,
                     score: int | str, instructions: str,
                     server_port: int = 7373) -> str:
    """Build the JavaScript banner overlay that persists across navigations.

    Features:
    - "Continue ▶" button: resume agent without human action
    - "Done ✓" button: human completed an action, resume agent
    - "Other ✏" button: opens a text box for custom instructions to the agent
    - "▼ Details" collapsible: full instructions text
    - "−" collapse button: shrinks banner to a floating pill (top-left)
    """
    # Instructions summary (first sentence for inline display)
    first_period = instructions.find(". ")
    if 0 < first_period < 80:
        instructions_summary = instructions[:first_period + 1]
    else:
        instructions_summary = instructions[:80] + ("..." if len(instructions) > 80 else "")
    instructions_summary_js = instructions_summary.replace("\\", "\\\\").replace("'", "\\'")
    instructions_full_js = instructions.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")

    return f"""
(function() {{
  if (window.__ap_banner) return;
  window.__ap_banner = true;
  var HASH = '{hash_}';
  var PORT = {server_port};
  var STORAGE_KEY = '__ap_banner_collapsed_' + HASH;

  function _signalDone(customInstructions) {{
    window.__ap_hitl_done = HASH;
    var body = customInstructions ? JSON.stringify({{instructions: customInstructions}}) : null;
    fetch('http://localhost:' + PORT + '/api/done/' + HASH, {{
      method: 'POST',
      headers: body ? {{'Content-Type': 'application/json'}} : {{}},
      body: body || undefined
    }}).catch(function() {{}});
  }}

  function _disableAllBtns() {{
    var btns = document.querySelectorAll('#__ap_banner_root button:not(#__ap_collapse_btn)');
    btns.forEach(function(b) {{ b.disabled = true; }});
  }}

  function _showCollapsed() {{
    var root = document.getElementById('__ap_banner_root');
    if (root) root.style.display = 'none';
    document.body.style.removeProperty('padding-top');

    var pill = document.getElementById('__ap_pill');
    if (!pill) {{
      pill = document.createElement('div');
      pill.id = '__ap_pill';
      pill.title = 'ApplyPilot HITL — click to expand';
      pill.innerHTML = '&#9872; AP';
      pill.style.cssText = [
        'position:fixed', 'top:8px', 'left:8px', 'z-index:2147483647',
        'background:#7c3aed', 'color:#fff',
        'font-family:system-ui,sans-serif', 'font-size:13px', 'font-weight:700',
        'padding:5px 10px', 'border-radius:20px',
        'box-shadow:0 2px 8px rgba(0,0,0,0.5)',
        'cursor:pointer', 'user-select:none',
        'display:flex', 'align-items:center', 'gap:5px'
      ].join(';');
      pill.onclick = function() {{ _showExpanded(); }};
      document.body.appendChild(pill);
    }} else {{
      pill.style.display = 'flex';
    }}
    try {{ localStorage.setItem(STORAGE_KEY, '1'); }} catch(e) {{}}
  }}

  function _showExpanded() {{
    var pill = document.getElementById('__ap_pill');
    if (pill) pill.style.display = 'none';
    var root = document.getElementById('__ap_banner_root');
    if (root) {{
      root.style.display = 'block';
      document.body.style.paddingTop = '90px';
    }}
    try {{ localStorage.removeItem(STORAGE_KEY); }} catch(e) {{}}
  }}

  function _injectBanner() {{
    if (document.getElementById('__ap_banner_root')) return;

    var root = document.createElement('div');
    root.id = '__ap_banner_root';
    root.style.cssText = [
      'position:fixed', 'top:0', 'left:0', 'right:0', 'z-index:2147483647',
      'background:linear-gradient(90deg,#7c3aed,#4f46e5)',
      'color:#fff', 'font-family:system-ui,sans-serif', 'font-size:14px',
      'padding:10px 16px 8px', 'box-shadow:0 2px 8px rgba(0,0,0,0.4)',
      'user-select:none'
    ].join(';');

    // ── Top row ──────────────────────────────────────────────────────────────
    var topRow = document.createElement('div');
    topRow.style.cssText = 'display:flex;align-items:center;gap:10px';

    var info = document.createElement('div');
    info.style.cssText = 'flex:1;overflow:hidden;min-width:0';
    info.innerHTML = '<strong>&#9872; ApplyPilot HITL</strong>'
      + ' &mdash; <em>{title}</em> @ {company} (score:{score}/10)'
      + '<br><span style="font-size:11px;opacity:0.85">{instructions_summary_js}</span>';

    function _makeBtn(label, bg, fg, title) {{
      var b = document.createElement('button');
      b.innerHTML = label;
      if (title) b.title = title;
      b.style.cssText = [
        'background:' + bg, 'color:' + fg,
        'border:none', 'border-radius:6px',
        'padding:6px 12px', 'font-size:12px', 'font-weight:700',
        'cursor:pointer', 'white-space:nowrap', 'flex-shrink:0',
        'line-height:1.2'
      ].join(';');
      return b;
    }}

    var btnContinue = _makeBtn('Continue &#9654;', '#22c55e', '#000',
      'Resume agent from current page — no human action needed');
    var btnDone = _makeBtn('Done &#10003;', '#fff', '#4f46e5',
      'I completed the required action — hand back to agent');
    var btnOther = _makeBtn('Other &#9998;', 'rgba(255,255,255,0.15)', '#fff',
      'Enter custom instructions for the agent');
    var btnCollapse = _makeBtn('&#8722;', 'rgba(0,0,0,0.2)', '#fff',
      'Minimize banner');
    btnCollapse.id = '__ap_collapse_btn';
    btnCollapse.style.borderRadius = '50%';
    btnCollapse.style.padding = '4px 8px';
    btnCollapse.style.fontSize = '14px';

    btnContinue.onclick = function() {{
      if (btnContinue.disabled) return;
      _disableAllBtns();
      btnContinue.innerHTML = 'Resuming...';
      _signalDone(null);
    }};

    btnDone.onclick = function() {{
      if (btnDone.disabled) return;
      _disableAllBtns();
      btnDone.innerHTML = 'Notifying...';
      btnDone.style.background = '#22c55e';
      btnDone.style.color = '#000';
      _signalDone(null);
    }};

    btnOther.onclick = function() {{
      var panel = document.getElementById('__ap_other_panel');
      if (panel) {{
        panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
      }}
    }};

    btnCollapse.onclick = function() {{ _showCollapsed(); }};

    topRow.appendChild(info);
    topRow.appendChild(btnContinue);
    topRow.appendChild(btnDone);
    topRow.appendChild(btnOther);
    topRow.appendChild(btnCollapse);
    root.appendChild(topRow);

    // ── "Other" panel (hidden by default) ────────────────────────────────────
    var otherPanel = document.createElement('div');
    otherPanel.id = '__ap_other_panel';
    otherPanel.style.cssText = 'display:none;margin-top:8px';

    var textarea = document.createElement('textarea');
    textarea.placeholder = 'Enter instructions for the agent (e.g. "Skip the cover letter field and submit")';
    textarea.rows = 3;
    textarea.style.cssText = [
      'width:100%', 'box-sizing:border-box',
      'background:rgba(0,0,0,0.3)', 'color:#fff',
      'border:1px solid rgba(255,255,255,0.3)', 'border-radius:5px',
      'padding:7px 10px', 'font-size:12px', 'font-family:system-ui,sans-serif',
      'resize:vertical', 'outline:none'
    ].join(';');

    var otherBtnRow = document.createElement('div');
    otherBtnRow.style.cssText = 'display:flex;gap:8px;margin-top:6px;justify-content:flex-end';

    var btnCancel = _makeBtn('Cancel', 'rgba(255,255,255,0.1)', '#fff', '');
    var btnSubmit = _makeBtn('Submit Instructions &#8594;', '#22c55e', '#000', 'Send custom instructions and resume agent');

    btnCancel.onclick = function() {{
      otherPanel.style.display = 'none';
      textarea.value = '';
    }};

    btnSubmit.onclick = function() {{
      var txt = textarea.value.trim();
      if (!txt) {{ textarea.focus(); return; }}
      _disableAllBtns();
      btnSubmit.innerHTML = 'Sending...';
      _signalDone(txt);
    }};

    otherBtnRow.appendChild(btnCancel);
    otherBtnRow.appendChild(btnSubmit);
    otherPanel.appendChild(textarea);
    otherPanel.appendChild(otherBtnRow);
    root.appendChild(otherPanel);

    // ── Details collapsible ───────────────────────────────────────────────────
    var details = document.createElement('details');
    details.style.cssText = 'font-size:11px;margin-top:5px;cursor:pointer;opacity:0.85';
    var summary = document.createElement('summary');
    summary.style.cssText = 'list-style:none;outline:none';
    summary.innerHTML = '&#9660; Details';
    var detailBody = document.createElement('div');
    detailBody.style.cssText = [
      'margin-top:5px', 'padding:7px 10px',
      'background:rgba(0,0,0,0.25)', 'border-radius:5px',
      'white-space:pre-wrap', 'line-height:1.5', 'font-size:11px', 'opacity:1'
    ].join(';');
    detailBody.textContent = '{instructions_full_js}'.replace(/\\\\n/g, '\\n');
    details.appendChild(summary);
    details.appendChild(detailBody);
    root.appendChild(details);

    // ── Insert into DOM ───────────────────────────────────────────────────────
    function _tryInsert() {{
      if (document.body) {{
        document.body.style.paddingTop = '90px';
        document.body.insertBefore(root, document.body.firstChild);
        // Restore collapsed state across navigations
        try {{
          if (localStorage.getItem(STORAGE_KEY)) {{ _showCollapsed(); }}
        }} catch(e) {{}}
      }} else {{
        setTimeout(_tryInsert, 100);
      }}
    }}
    _tryInsert();
  }}

  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', _injectBanner);
  }} else {{
    _injectBanner();
  }}
}})();
"""


def _start_done_watcher(cdp_port: int, server_port: int, hash_: str) -> subprocess.Popen | None:
    """Start a background Node.js process that polls for the HITL done signal.

    The banner button sets window.__ap_hitl_done = hash_ (a JS assignment that
    bypasses page CSP restrictions). This watcher detects it via CDP (outside
    the page context) and POSTs to the worker HTTP server to fire the hitl_event.

    Returns the subprocess handle (so the caller can kill it when HITL ends),
    or None if Node.js is unavailable.
    """
    watcher_script = f"""
const {{ chromium }} = require('@playwright/test');
(async () => {{
  try {{
    const b = await chromium.connectOverCDP('http://localhost:{cdp_port}');
    const ctx = b.contexts()[0];
    let done = false;

    const watchdog = setTimeout(() => {{
      if (!done) {{ done = true; process.exit(2); }}
    }}, 1800000); // 30-minute safety exit

    const check = setInterval(async () => {{
      if (done) {{ clearInterval(check); return; }}
      try {{
        const pages = ctx.pages();
        for (const page of pages) {{
          const val = await page.evaluate(() => window.__ap_hitl_done || null).catch(() => null);
          if (val === '{hash_}') {{
            done = true;
            clearInterval(check);
            clearTimeout(watchdog);
            // Update all banner action buttons to confirmed state
            await page.evaluate(`(function() {{
              var root = document.getElementById('__ap_banner_root');
              if (!root) return;
              var btns = root.querySelectorAll('button:not(#__ap_collapse_btn)');
              btns.forEach(function(btn) {{
                btn.innerHTML = 'Agent taking over...';
                btn.style.background = '#22c55e';
                btn.style.color = '#000';
                btn.disabled = true;
              }});
            }})()`).catch(() => {{}});
            // POST to server via Node HTTP — not subject to page CSP
            const http = require('http');
            await new Promise((res) => {{
              const req = http.request(
                {{ hostname: '127.0.0.1', port: {server_port},
                   path: '/api/done/{hash_}', method: 'POST' }},
                res
              );
              req.on('error', res);
              req.end();
            }});
            process.exit(0);
            break;
          }}
        }}
      }} catch(e) {{}}
    }}, 500);
  }} catch(e) {{
    process.stderr.write(e.message + '\\n');
    process.exit(1);
  }}
}})();
"""
    try:
        proc = subprocess.Popen(
            ["node", "-e", watcher_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.debug("Done watcher started (pid=%d) for hash=%s port=%d",
                     proc.pid, hash_, server_port)
        return proc
    except Exception as e:
        logger.warning("Failed to start done watcher: %s", e)
        return None


def _navigate_chrome(port: int, url: str) -> bool:
    """Navigate the first Chrome tab to a URL via CDP."""
    import urllib.request
    try:
        targets = _cdp_list_targets(port)
        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            return False
        tab_id = pages[0]["id"]
        req_url = f"http://localhost:{port}/json/activate/{tab_id}"
        urllib.request.urlopen(req_url, timeout=3)
        # Navigate by opening a new blank tab and using CDP
        # Use the existing tab's websocket to navigate
        # Simple approach: PUT /json/new with URL
        req = urllib.request.Request(
            f"http://localhost:{port}/json/new?{url}", method="PUT"
        )
        urllib.request.urlopen(req, timeout=3)
        # Close the old blank tab
        try:
            urllib.request.urlopen(
                f"http://localhost:{port}/json/close/{tab_id}", timeout=2
            )
        except Exception:
            pass
        return True
    except Exception as e:
        logger.debug("CDP navigate failed: %s", e)
        return False


def _start_hitl_chrome(job: dict) -> subprocess.Popen | None:
    """Launch (or reuse) the HITL Chrome instance and navigate to the stuck URL."""
    global _hitl_chrome_proc

    with _hitl_chrome_lock:
        # Kill old HITL Chrome if still running
        if _hitl_chrome_proc and _hitl_chrome_proc.poll() is None:
            from applypilot.apply.chrome import cleanup_worker
            cleanup_worker(HITL_WORKER_ID, _hitl_chrome_proc)
            _hitl_chrome_proc = None

        stuck_url = job.get("needs_human_url") or job.get("application_url") or job["url"]

        proc = launch_chrome(
            HITL_WORKER_ID,
            port=HITL_CDP_PORT,
            headless=False,
            minimized=False,
        )
        _hitl_chrome_proc = proc

    # Give Chrome time to be ready
    time.sleep(2)

    # Navigate to the stuck URL
    _navigate_chrome(HITL_CDP_PORT, stuck_url)
    time.sleep(1)

    # Inject the banner overlay
    _inject_banner(HITL_CDP_PORT, job)

    # Bring to foreground
    bring_to_foreground()

    return proc


def _run_agent_for_job(h: str) -> None:
    """Background thread: reset job and run apply agent after user clicks Done."""
    from applypilot.apply.launcher import (
        reset_needs_human, run_job, mark_result, mark_needs_human,
        _HITL_INSTRUCTIONS,
    )
    from applypilot.database import get_connection

    with _sessions_lock:
        session = _sessions.get(h)
        if not session:
            return
        job = session["job"]
        session["status"] = "agent_running"
        extra_ctx = session.pop("custom_instructions", None)

    # Reset job back to NULL so run_job can acquire it
    reset_needs_human(job["url"])

    # Re-read the job from DB (needs_human columns cleared, fresh state)
    conn = get_connection()
    row = conn.execute(
        "SELECT url, title, site, application_url, tailored_resume_path, "
        "       fit_score, location, full_description, cover_letter_path, company "
        "FROM jobs WHERE url = ?",
        (job["url"],),
    ).fetchone()
    if row:
        job = dict(zip(row.keys(), row))

    logger.info("[HITL] Spawning agent for job: %s", job.get("title"))

    result, duration_ms, _screening_qs = run_job(
        job,
        port=HITL_CDP_PORT,
        worker_id=HITL_WORKER_ID,
        model="haiku",
        dry_run=False,
        extra_context=extra_ctx,
    )

    logger.info("[HITL] Agent result: %s", result)

    # Process result
    if result == "applied":
        mark_result(job["url"], "applied", duration_ms=duration_ms)
        # Save ATS session — the user just authenticated via HITL,
        # so this session has fresh cookies to reuse on future jobs
        from applypilot.apply.chrome import detect_ats, save_ats_session
        from applypilot import config
        ats_slug = detect_ats(job.get("application_url") or job.get("url"))
        if ats_slug:
            profile_dir = config.CHROME_WORKER_DIR / f"worker-{HITL_WORKER_ID}"
            save_ats_session(profile_dir, ats_slug)
            logger.info("[HITL] Saved %s session for future jobs", ats_slug)
    elif result.startswith("needs_human:"):
        after = result[len("needs_human:"):]
        nh_reason, nh_url = (after.split(":", 1) if ":" in after
                             else (after, job.get("application_url") or job["url"]))
        nh_instructions = _HITL_INSTRUCTIONS.get(
            nh_reason, f"Human action required: {nh_reason}"
        )
        mark_needs_human(job["url"], nh_reason, nh_url, nh_instructions, duration_ms)
    else:
        reason = result.split(":", 1)[-1] if ":" in result else result
        from applypilot.apply.launcher import _is_permanent_failure
        perm = _is_permanent_failure(result)
        mark_result(job["url"], "failed", reason, permanent=perm,
                    duration_ms=duration_ms)

    with _sessions_lock:
        if h in _sessions:
            _sessions[h]["status"] = "done"
            _sessions[h]["result"] = result


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    """HTTP handler for the HITL review UI."""

    def log_message(self, format, *args):
        # Suppress default request logging (use our logger instead)
        pass

    def _send_json(self, data: dict | list, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse_headers(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/" or path == "":
            self._send_html(_build_ui_html())
            return

        if path == "/api/jobs":
            from applypilot.database import get_needs_human_jobs
            jobs = get_needs_human_jobs()
            # Add hash for each job
            for j in jobs:
                j["hash"] = _job_hash(j["url"])
            self._send_json(jobs)
            return

        if path.startswith("/api/status/"):
            h = path[len("/api/status/"):]
            with _sessions_lock:
                session = _sessions.get(h, {})
            self._send_json({
                "status": session.get("status", "idle"),
                "result": session.get("result"),
            })
            return

        if path.startswith("/api/result-stream/"):
            h = path[len("/api/result-stream/"):]
            self._handle_sse(h)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path

        if path.startswith("/api/start/"):
            h = path[len("/api/start/"):]
            self._handle_start(h)
            return

        if path.startswith("/api/done/"):
            h = path[len("/api/done/"):]
            self._handle_done(h)
            return

        if path.startswith("/api/skip/"):
            h = path[len("/api/skip/"):]
            self._handle_skip(h)
            return

        self.send_response(404)
        self.end_headers()

    def _handle_start(self, h: str) -> None:
        """Launch HITL Chrome and inject banner for the given job hash."""
        from applypilot.database import get_needs_human_jobs

        # Find the job
        jobs = get_needs_human_jobs()
        job = next((j for j in jobs if _job_hash(j["url"]) == h), None)
        if not job:
            self._send_json({"error": "Job not found"}, 404)
            return

        # Check if another session is already active
        with _sessions_lock:
            active = [
                s for s in _sessions.values()
                if s.get("status") in ("chrome_open", "agent_running")
                and _job_hash(s["job"]["url"]) != h
            ]
            if active:
                self._send_json({"error": "Another session is active"}, 409)
                return

            _sessions[h] = {
                "job": job,
                "status": "chrome_open",
                "result": None,
                "log_offset": 0,
            }

        # Launch Chrome in background thread (may take a few seconds)
        def _launch():
            try:
                proc = _start_hitl_chrome(job)
                with _sessions_lock:
                    if h in _sessions:
                        _sessions[h]["chrome_proc"] = proc
            except Exception as e:
                logger.error("Failed to start HITL Chrome: %s", e)
                with _sessions_lock:
                    if h in _sessions:
                        _sessions[h]["status"] = "error"
                        _sessions[h]["result"] = f"launch_error:{e}"

        threading.Thread(target=_launch, daemon=True).start()
        self._send_json({"ok": True, "hash": h})

    def _handle_done(self, h: str) -> None:
        """User clicked Done or Continue — spawn agent thread."""
        length = int(self.headers.get("Content-Length", 0))
        body = {}
        if length:
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                pass
        custom_instructions = (body.get("instructions") or "").strip()

        with _sessions_lock:
            session = _sessions.get(h)

        if not session:
            self._send_json({"error": "Session not found"}, 404)
            return

        if session.get("status") == "agent_running":
            self._send_json({"error": "Agent already running"}, 409)
            return

        with _sessions_lock:
            if h in _sessions:
                _sessions[h]["status"] = "agent_running"
                if custom_instructions:
                    _sessions[h]["custom_instructions"] = custom_instructions

        threading.Thread(
            target=_run_agent_for_job, args=(h,), daemon=True
        ).start()
        self._send_json({"ok": True})

    def _handle_skip(self, h: str) -> None:
        """Permanently skip a job (mark as failed)."""
        from applypilot.database import get_needs_human_jobs

        jobs = get_needs_human_jobs()
        job = next((j for j in jobs if _job_hash(j["url"]) == h), None)
        if not job:
            self._send_json({"error": "Job not found"}, 404)
            return

        from applypilot.apply.launcher import mark_result
        mark_result(job["url"], "failed", "human_skipped", permanent=True)

        with _sessions_lock:
            _sessions.pop(h, None)

        self._send_json({"ok": True})

    def _handle_sse(self, h: str) -> None:
        """Stream agent log output via SSE while the agent runs."""
        self._send_sse_headers()
        worker_log = config.LOG_DIR / f"worker-{HITL_WORKER_ID}.log"

        with _sessions_lock:
            session = _sessions.get(h, {})
            offset = session.get("log_offset", 0)

        try:
            while True:
                with _sessions_lock:
                    session = _sessions.get(h, {})
                    status = session.get("status", "idle")
                    result = session.get("result")

                # Tail the log file
                if worker_log.exists():
                    try:
                        content = worker_log.read_text(encoding="utf-8", errors="replace")
                        if len(content) > offset:
                            new_text = content[offset:].replace("\n", "\\n")
                            offset = len(content)
                            with _sessions_lock:
                                if h in _sessions:
                                    _sessions[h]["log_offset"] = offset
                            data = json.dumps({"text": new_text})
                            self.wfile.write(f"data: {data}\n\n".encode())
                            self.wfile.flush()
                    except OSError:
                        pass

                # Check if Chrome closed (ConnectionRefusedError on CDP)
                if status == "chrome_open":
                    targets = _cdp_list_targets(HITL_CDP_PORT)
                    if not targets:
                        self.wfile.write(
                            b'event: chrome_closed\ndata: {}\n\n'
                        )
                        self.wfile.flush()
                        break

                # Done
                if status == "done" and result is not None:
                    data = json.dumps({"result": result})
                    self.wfile.write(f"event: done\ndata: {data}\n\n".encode())
                    self.wfile.flush()
                    break

                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError):
            pass


# ---------------------------------------------------------------------------
# HTML UI
# ---------------------------------------------------------------------------

def _build_ui_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>ApplyPilot — Human Review</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0; padding: 2rem; }
  h1 { font-size: 1.5rem; margin-bottom: 0.4rem; }
  .subtitle { color: #94a3b8; margin-bottom: 2rem; font-size: 0.9rem; }
  .empty { color: #64748b; font-size: 1rem; margin-top: 2rem; }
  .job-list { display: flex; flex-direction: column; gap: 1rem; }
  .job-card {
    background: #1e293b; border-radius: 10px; padding: 1.25rem;
    border-left: 4px solid #7c3aed;
  }
  .job-title { font-size: 1rem; font-weight: 700; margin-bottom: 0.4rem; }
  .job-meta { font-size: 0.8rem; color: #94a3b8; margin-bottom: 0.6rem; }
  .reason-badge {
    display: inline-block; font-size: 0.7rem; font-weight: 700;
    padding: 0.15rem 0.5rem; border-radius: 4px;
    background: #7c3aed33; color: #c4b5fd;
    text-transform: uppercase; margin-right: 0.5rem;
  }
  .instructions { font-size: 0.82rem; color: #e2e8f0; margin: 0.5rem 0; line-height: 1.5; }
  .actions { display: flex; gap: 0.75rem; margin-top: 0.75rem; }
  .btn-start {
    background: #7c3aed; color: #fff; border: none; border-radius: 6px;
    padding: 0.5rem 1rem; font-size: 0.85rem; font-weight: 600; cursor: pointer;
  }
  .btn-start:hover { background: #6d28d9; }
  .btn-start:disabled { background: #334155; color: #64748b; cursor: not-allowed; }
  .btn-skip {
    background: transparent; color: #ef4444; border: 1px solid #ef444433;
    border-radius: 6px; padding: 0.5rem 1rem; font-size: 0.85rem; cursor: pointer;
  }
  .btn-skip:hover { background: #ef444411; }
  .status-msg { font-size: 0.8rem; color: #94a3b8; margin-top: 0.5rem; }
  .result-ok { color: #22c55e; font-weight: 600; }
  .result-fail { color: #ef4444; font-weight: 600; }
  .log-box {
    background: #0f172a; border-radius: 6px; padding: 0.75rem;
    font-size: 0.72rem; font-family: monospace; color: #94a3b8;
    max-height: 200px; overflow-y: auto; margin-top: 0.5rem;
    white-space: pre-wrap; word-break: break-all;
    display: none;
  }
  .log-box.active { display: block; }
</style>
</head>
<body>
<h1>&#9872; Human Review Queue</h1>
<p class="subtitle">Jobs parked for human assistance. Click Start to open Chrome at the stuck page.</p>
<div id="content"><p class="empty">Loading...</p></div>

<script>
async function load() {
  const r = await fetch('/api/jobs');
  const jobs = await r.json();
  const el = document.getElementById('content');
  if (!jobs.length) {
    el.innerHTML = '<p class="empty">No jobs in the human review queue. Run <code>applypilot apply</code> to generate more.</p>';
    return;
  }
  el.innerHTML = '<div class="job-list">' + jobs.map(j => renderJob(j)).join('') + '</div>';
}

function renderJob(j) {
  const h = j.hash;
  return `<div class="job-card" id="card-${h}">
    <div class="job-title">${esc(j.title || 'Unknown Position')}</div>
    <div class="job-meta">
      <span class="reason-badge">${esc(j.needs_human_reason || '?')}</span>
      ${esc(j.site || '')} &nbsp;·&nbsp; Score: ${j.fit_score || '?'}/10
    </div>
    <div class="instructions">${esc(j.needs_human_instructions || '')}</div>
    <div class="actions">
      <button class="btn-start" id="btn-${h}" onclick="startJob('${h}')">Start &#8594; Open Chrome</button>
      <button class="btn-skip" onclick="skipJob('${h}')">Skip</button>
    </div>
    <div class="status-msg" id="status-${h}"></div>
    <div class="log-box" id="log-${h}"></div>
  </div>`;
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

async function startJob(h) {
  const btn = document.getElementById('btn-' + h);
  const status = document.getElementById('status-' + h);
  btn.disabled = true;
  btn.textContent = 'Opening Chrome...';
  status.textContent = 'Launching browser and injecting banner...';

  const r = await fetch('/api/start/' + h, { method: 'POST' });
  if (!r.ok) {
    const e = await r.json();
    if (r.status === 409) {
      status.textContent = 'Another session is active. Finish it first.';
    } else {
      status.textContent = 'Error: ' + (e.error || 'unknown');
    }
    btn.disabled = false;
    btn.textContent = 'Start → Open Chrome';
    return;
  }

  status.textContent = 'Chrome open with overlay. Complete the action and click Done in the banner.';

  // Start SSE stream to watch agent output
  startSSE(h);
}

function startSSE(h) {
  const log = document.getElementById('log-' + h);
  const status = document.getElementById('status-' + h);
  const es = new EventSource('/api/result-stream/' + h);

  es.onmessage = function(e) {
    const d = JSON.parse(e.data);
    if (d.text) {
      log.classList.add('active');
      log.textContent += d.text.replace(/\\\\n/g, '\\n');
      log.scrollTop = log.scrollHeight;
    }
  };

  es.addEventListener('done', function(e) {
    const d = JSON.parse(e.data);
    const result = d.result || '';
    status.innerHTML = result === 'applied'
      ? '<span class="result-ok">&#10003; Applied successfully!</span>'
      : '<span class="result-fail">&#10007; Result: ' + esc(result) + '</span>';
    es.close();
    // Refresh the list after 3 seconds
    setTimeout(load, 3000);
  });

  es.addEventListener('chrome_closed', function() {
    status.textContent = 'Chrome was closed. Click Start to try again.';
    const btn = document.getElementById('btn-' + h);
    if (btn) { btn.disabled = false; btn.textContent = 'Start → Open Chrome'; }
    es.close();
  });

  es.onerror = function() { es.close(); };
}

async function skipJob(h) {
  if (!confirm('Skip this job permanently?')) return;
  await fetch('/api/skip/' + h, { method: 'POST' });
  load();
}

load();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

def serve(port: int = 7373, open_browser: bool = True) -> None:
    """Start the HITL review HTTP server.

    Args:
        port: TCP port to bind to (default 7373).
        open_browser: If True, open the review UI in the default browser.

    Raises:
        SystemExit on Ctrl+C.
    """
    global _hitl_chrome_proc

    try:
        server = HTTPServer(("127.0.0.1", port), _Handler)
    except OSError as e:
        if e.errno == 98:  # Address already in use
            print(
                f"\n[red]Port {port} is already in use.[/red]\n"
                f"Try: applypilot human-review --port {port + 1}",
                file=sys.stderr,
            )
            sys.exit(1)
        raise

    url = f"http://localhost:{port}"
    print(f"\n  Human Review UI → {url}")
    print("  Press Ctrl+C to stop.\n")

    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        # Cleanup HITL Chrome if still running
        with _hitl_chrome_lock:
            if _hitl_chrome_proc and _hitl_chrome_proc.poll() is None:
                cleanup_worker(HITL_WORKER_ID, _hitl_chrome_proc)
                _hitl_chrome_proc = None
        print("\n  Review server stopped.")
