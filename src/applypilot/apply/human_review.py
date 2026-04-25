"""HITL banner injection helpers (CDP-side).

Used by the in-pipeline HITL flow in launcher._run_hitl. The standalone
`applypilot human-review` server (formerly the rest of this file) was
deleted in plan 5 — its only remaining caller was the launcher-restart
path, which is now covered by `_run_hitl`'s own pause-survival logic.

Public surface (still imported by launcher.py):
- `_inject_banner(port, job, server_port)` — paint banner via CDP.
- `_navigate_chrome(port, url)` — CDP-side navigate.
- `_start_done_watcher(cdp_port, server_port, hash)` — Node poller of
  window.__ap_hitl_done that POSTs /api/done/{hash} on click.
- Internal helpers: `_job_hash`, `_cdp_list_targets`, `_build_banner_js`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import time

logger = logging.getLogger(__name__)


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
