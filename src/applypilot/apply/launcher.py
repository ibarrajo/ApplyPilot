"""Apply orchestration: acquire jobs, spawn Claude Code sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + Claude Code for each one, parses the
result, and updates the database. Supports parallel workers via --workers.
"""

import atexit
import hashlib
import json
import logging
import os
import platform
import queue
import re
import signal
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.database import get_connection, categorize_apply_result, get_in_flight_by_company
from applypilot.apply import prompt as prompt_mod
from applypilot.apply.chrome import (
    launch_chrome, cleanup_worker, kill_all_chrome,
    detect_ats, save_ats_session, clear_ats_session,
    reset_worker_dir, cleanup_on_exit, _kill_process_tree,
    BASE_CDP_PORT, bring_to_foreground,
    probe_existing_chrome, _AdoptedChromeProcess,
    _chrome_procs, _chrome_lock,
)
from applypilot.apply.dashboard import (
    init_worker, update_state, add_event, get_state,
    render_full, get_totals, start_health_checks, stop_health_checks,
)

logger = logging.getLogger(__name__)

# Document format for resume/cover letter uploads ("pdf" or "docx").
# Set once by the CLI before workers start; read by run_job/gen_prompt.
_doc_format: str = "docx"


def set_doc_format(fmt: str) -> None:
    """Set the document format for apply uploads."""
    global _doc_format
    _doc_format = fmt


# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from applypilot.config import load_blocked_sites
    return load_blocked_sites()

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()

# Track active Claude Code processes for skip (Ctrl+C) handling
_claude_procs: dict[int, subprocess.Popen] = {}
_claude_lock = threading.Lock()

# Register cleanup on exit
atexit.register(cleanup_on_exit)


def _kill_all_children() -> None:
    """Kill all Claude subprocesses and mini-task procs."""
    with _claude_lock:
        procs = list(_claude_procs.values())
        _claude_procs.clear()
    for p in procs:
        if p.poll() is None:
            try:
                _kill_process_tree(p.pid)
            except Exception:
                pass
    for p in list(_mini_procs.values()):
        if p.poll() is None:
            try:
                _kill_process_tree(p.pid)
            except Exception:
                pass
    _mini_procs.clear()


atexit.register(_kill_all_children)

if platform.system() != "Windows":
    def _sigterm_handler(*_):
        _stop_event.set()
        _kill_all_children()
        kill_all_chrome()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _sigterm_handler)

# Q&A interactive queue: worker threads post questions, main thread answers
# Each item: (worker_id, questions_list, answer_event)
_qa_queue: queue.Queue = queue.Queue()

# Track workers waiting for human input (thread-safe)
_waiting_workers: dict[int, str] = {}  # worker_id -> wait type
_waiting_lock = threading.Lock()

# Per-worker HITL HTTP servers (legacy — kept for backwards compat, unused when always-on server is running)
_hitl_servers: dict[int, HTTPServer] = {}
_hitl_server_lock = threading.Lock()

# Base port for in-pipeline HITL HTTP listeners (7380-7384 for workers 0-4)
HITL_LISTEN_BASE_PORT = 7380

# Always-on per-worker HTTP servers (one per worker, started once in worker_loop)
_worker_servers: dict[int, HTTPServer] = {}
_worker_server_lock = threading.Lock()

# Per-worker mutable state, closed over by each worker's HTTP handler
# Keys: job, status, reason, instructions, hitl_event, hitl_job_hash,
#       handback_instructions, mini_proc
_worker_state: dict[int, dict] = {}
_worker_state_lock = threading.Lock()

# Per-worker takeover/handback events
_takeover_events: dict[int, threading.Event] = {}
_handback_events: dict[int, threading.Event] = {}

# Per-worker active mini-task Claude processes
_mini_procs: dict[int, subprocess.Popen] = {}


def _register_waiting(worker_id: int, wait_type: str) -> None:
    """Register a worker as waiting for human input."""
    with _waiting_lock:
        _waiting_workers[worker_id] = wait_type


def _unregister_waiting(worker_id: int) -> None:
    """Remove a worker from the waiting list."""
    with _waiting_lock:
        _waiting_workers.pop(worker_id, None)


def _get_waiting_count() -> int:
    """Get the number of workers currently waiting for human input."""
    with _waiting_lock:
        return len(_waiting_workers)


# ---------------------------------------------------------------------------
# Per-worker HITL HTTP listener
# ---------------------------------------------------------------------------

def _start_hitl_listener(worker_id: int, done_event: threading.Event,
                         job_hash: str) -> int:
    """Register a HITL done event with the always-on worker listener.

    If the always-on worker listener is running (normal case), stores the
    done_event in the worker state so /api/done/{hash} can fire it.
    Falls back to creating a temporary server if the always-on server isn't up.

    Returns:
        The port the HITL listener is on.
    """
    port = HITL_LISTEN_BASE_PORT + worker_id
    with _worker_state_lock:
        state = _worker_state.get(worker_id)
    if state is not None:
        # Always-on server is running — store event reference
        state["hitl_event"] = done_event
        state["hitl_job_hash"] = job_hash
        return port

    # Fallback: start a temporary per-HITL server
    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path.rstrip("/") == f"/api/done/{job_hash}":
                done_event.set()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_response(404)
                self.end_headers()

        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.end_headers()

        def log_message(self, format, *args):
            pass

    try:
        server = HTTPServer(("127.0.0.1", port), _Handler)
    except OSError:
        server = HTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]

    with _hitl_server_lock:
        _hitl_servers[worker_id] = server

    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name=f"hitl-http-w{worker_id}")
    thread.start()
    logger.debug("HITL listener (fallback) for worker %d on port %d", worker_id, port)
    return port


def _stop_hitl_listener(worker_id: int) -> None:
    """Clear HITL event from worker state and kill the done watcher process."""
    with _worker_state_lock:
        state = _worker_state.get(worker_id)
    if state is not None:
        state["hitl_event"] = None
        state["hitl_job_hash"] = None
        watcher = state.pop("hitl_watcher_proc", None)
        if watcher is not None and watcher.poll() is None:
            try:
                watcher.kill()
            except Exception:
                pass
    # Also shut down any legacy fallback server
    with _hitl_server_lock:
        server = _hitl_servers.pop(worker_id, None)
    if server:
        server.shutdown()


def _run_mini_task(worker_id: int, cdp_port: int, instructions: str) -> subprocess.Popen:
    """Spawn a mini Claude Code session to execute user instructions in Chrome.

    The mini Claude has Playwright MCP access to the worker's Chrome window.
    It should complete the task and output TASK:COMPLETE when done.

    Args:
        worker_id: Worker whose Chrome window to use.
        cdp_port: CDP debug port for the worker's Chrome.
        instructions: What the user wants Claude to do.

    Returns:
        Running subprocess.Popen handle (stdout is readable).
    """
    prompt = (
        f"You have browser access via Playwright MCP (CDP port {cdp_port}).\n"
        f"The user needs you to do the following task:\n\n"
        f"{instructions}\n\n"
        f"Use the browser tools to complete this task. When finished, output TASK:COMPLETE.\n"
        f"Do NOT submit any job applications — only do what the user explicitly asked."
    )

    mcp_config_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"

    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    proc = subprocess.Popen(
        [
            "claude",
            "--model", "sonnet",
            "-p",
            "--mcp-config", str(mcp_config_path),
            "--strict-mcp-config",
            "--permission-mode", "bypassPermissions",
            "--no-session-persistence",
            "--output-format", "stream-json",
            "--verbose", "-",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(config.APP_DIR),
        start_new_session=True,
    )
    proc.stdin.write(prompt)
    proc.stdin.close()
    return proc


# ---------------------------------------------------------------------------
# Always-on per-worker HTTP listener
# ---------------------------------------------------------------------------

class _ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """HTTPServer that handles each connection in a new daemon thread."""
    daemon_threads = True


def _start_worker_listener(worker_id: int) -> int:
    """Start the always-on HTTP server for a worker.

    This server starts once at the beginning of worker_loop() and lives for
    the entire worker lifetime. It exposes the Take Over, Run Task, Handback,
    and Done endpoints used by the Chrome extension popup.

    Endpoints:
        GET  /api/status         — current worker state (for extension polling)
        POST /api/takeover       — user takes over; kills current Claude proc
        POST /api/run-task       — spawn mini Claude for a user instruction
        GET  /api/task-stream    — SSE stream of mini Claude output
        POST /api/handback       — resume main agent (optionally with context)
        POST /api/done/{hash}    — HITL "done" signal (banner button)

    Returns:
        Port the server is listening on.
    """
    port = HITL_LISTEN_BASE_PORT + worker_id
    cdp_port = BASE_CDP_PORT + worker_id

    # Per-worker mutable state (closed over by handler)
    state: dict = {
        "job": None,
        "status": "idle",
        "reason": None,
        "instructions": None,
        "hitl_event": None,
        "hitl_job_hash": None,
        "hitl_watcher_proc": None,
        "handback_instructions": None,
        "mini_proc": None,
        "saved_instruction": None,
        "chrome_pid": None,
        "last_focused": 0,
        "history": [],  # list of completed job summaries for the homepage log
    }

    takeover_event = threading.Event()
    handback_event = threading.Event()

    with _worker_state_lock:
        _worker_state[worker_id] = state
    _takeover_events[worker_id] = takeover_event
    _handback_events[worker_id] = handback_event

    class _Handler(BaseHTTPRequestHandler):
        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self):
            if self.path in ("/", ""):
                self._handle_homepage()
            elif self.path == "/api/status":
                self._handle_status()
            elif self.path == "/api/log":
                self._handle_log()
            elif self.path == "/api/task-stream":
                self._handle_task_stream()
            elif self.path == "/api/focus":
                self._handle_focus()
            elif self.path.startswith("/api/jobs"):
                self._handle_jobs_list()
            else:
                self.send_response(404)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

        def do_POST(self):
            # Always close after POST response.  If we leave the connection
            # open (HTTP/1.1 keep-alive) and the handler doesn't consume the
            # request body, Python's HTTP server will try to parse the leftover
            # bytes as the next request, causing a deadlock where the server
            # waits for a valid request line and the client waits for a response.
            self.close_connection = True
            if self.path == "/api/takeover":
                self._handle_takeover()
            elif self.path == "/api/run-task":
                self._handle_run_task()
            elif self.path == "/api/handback":
                self._handle_handback()
            elif self.path.startswith("/api/done"):
                self._handle_done()
            elif self.path == "/api/add-job":
                self._handle_add_job()
            elif self.path == "/api/jobs/mark":
                self._handle_jobs_mark()
            else:
                self.send_response(404)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length))
            except Exception:
                return {}

        def _json_ok(self, data: dict) -> None:
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _text_ok(self, text: bytes = b"ok") -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(text)))
            self.end_headers()
            self.wfile.write(text)

        def _handle_homepage(self):
            import html as _html
            job = state.get("job") or {}
            status = state.get("status", "idle")
            title = job.get("title", "") or "No active job"
            company = job.get("company", "")
            site = job.get("site", "")
            score = job.get("fit_score", 0)
            status_color = {
                "applying": "#22c55e",
                "waiting_human": "#a855f7",
                "idle": "#6b7280",
            }.get(status, "#eab308")
            meta_parts = [p for p in [company, site] if p]
            if score:
                meta_parts.append(f"Score {score}/10")
            meta_line = " · ".join(meta_parts)
            instructions = state.get("instructions", "")
            instructions_block = ""
            if instructions:
                instructions_block = (
                    f'<div class="instructions">'
                    f'<strong>Instructions:</strong><br>'
                    f'{_html.escape(instructions).replace(chr(10), "<br>")}'
                    f'</div>'
                )

            # Build activity log rows
            history = list(reversed(state.get("history", [])))
            outcome_colors = {
                "applied":       ("#22c55e", "✓ Applied"),
                "already_applied": ("#6366f1", "↩ Already applied"),
                "expired":       ("#6b7280", "⌛ Expired"),
                "needs_human":   ("#a855f7", "⚑ Needs human"),
                "failed":        ("#ef4444", "✗ Failed"),
            }
            log_rows = ""
            for h in history:
                oc = h.get("outcome", "failed")
                color, label = outcome_colors.get(oc, ("#6b7280", oc))
                ts_str = datetime.fromtimestamp(h["ts"]).strftime("%H:%M:%S") if h.get("ts") else "–"
                job_title = _html.escape(h.get("title", "–")[:60])
                job_co = _html.escape(h.get("company", "")[:30])
                sc = h.get("score", 0)
                dur = h.get("duration_s", 0)
                url = _html.escape(h.get("url", "#"))
                log_rows += (
                    f'<tr>'
                    f'<td style="color:#64748b;font-size:11px">{ts_str}</td>'
                    f'<td><span style="color:{color};font-weight:600;font-size:11px">{label}</span></td>'
                    f'<td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
                    f'<a href="{url}" target="_blank" style="color:#e2e8f0;text-decoration:none">{job_title}</a>'
                    f'<br><span style="font-size:10px;color:#64748b">{job_co}</span></td>'
                    f'<td style="color:#60a5fa;font-size:11px;text-align:center">{sc}/10</td>'
                    f'<td style="color:#64748b;font-size:11px;text-align:right">{dur}s</td>'
                    f'</tr>'
                )
            log_section = ""
            if log_rows:
                log_section = f"""
<div class="log-panel">
  <div class="log-title">Session Activity</div>
  <table class="log-table">
    <thead><tr>
      <th>Time</th><th>Result</th><th>Job</th><th>Score</th><th>Time</th>
    </tr></thead>
    <tbody>{log_rows}</tbody>
  </table>
</div>"""

            body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>ApplyPilot W{worker_id}</title>
<style>
  * {{box-sizing:border-box;margin:0;padding:0}}
  body {{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;
        min-height:100vh;padding:24px;display:flex;flex-direction:column;
        align-items:center;gap:16px}}
  .badge {{background:#1e293b;border:1px solid #334155;border-radius:12px;
           padding:20px 28px;width:100%;max-width:600px;text-align:center}}
  .wid {{font-size:40px;font-weight:700;color:#eab308}}
  .status {{display:inline-block;padding:4px 12px;border-radius:9999px;
            font-size:12px;font-weight:600;margin:8px 0;
            background:{status_color}22;color:{status_color};
            border:1px solid {status_color}44}}
  .title {{font-size:18px;font-weight:600;margin:6px 0}}
  .meta {{font-size:12px;color:#94a3b8}}
  .instructions {{margin-top:12px;padding:10px 12px;background:#0f172a;
                  border-left:3px solid #a855f7;border-radius:4px;
                  font-size:12px;text-align:left;line-height:1.5}}
  .log-panel {{width:100%;max-width:600px;background:#1e293b;
               border:1px solid #334155;border-radius:12px;overflow:hidden}}
  .log-title {{padding:12px 16px;font-size:12px;font-weight:700;
               text-transform:uppercase;letter-spacing:.5px;color:#64748b;
               border-bottom:1px solid #334155}}
  .log-table {{width:100%;border-collapse:collapse;font-size:12px}}
  .log-table th {{padding:6px 10px;text-align:left;font-size:10px;
                  text-transform:uppercase;color:#475569;
                  border-bottom:1px solid #1e293b}}
  .log-table td {{padding:7px 10px;border-bottom:1px solid #0f172a;vertical-align:middle}}
  .log-table tr:last-child td {{border-bottom:none}}
  .hint {{font-size:10px;color:#334155;margin-top:4px}}
</style>
</head>
<body>
<div class="badge">
  <div class="wid">W{worker_id}</div>
  <div class="status">{status.upper().replace("_", " ")}</div>
  <div class="title">{_html.escape(title)}</div>
  {'<div class="meta">' + meta_line + '</div>' if meta_line else ''}
  {instructions_block}
</div>
{log_section}
<div class="hint">ApplyPilot Worker {worker_id} &nbsp;·&nbsp; <span id="ts"></span></div>
<script>
  document.getElementById('ts').textContent = new Date().toLocaleTimeString();
  setTimeout(() => location.reload(), 5000);
</script>
</body>
</html>""".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_status(self):
            job = state.get("job") or {}
            site = job.get("site", "")
            reason = state.get("reason", "")
            # Use cached saved_instruction — never hit DB on status polls.
            # (DB access from per-request daemon threads leaks SQLite fds over time.)
            self._json_ok({
                "workerId": worker_id,
                "status": state.get("status", "idle"),
                "jobTitle": job.get("title", ""),
                "jobSite": site,
                "jobCompany": job.get("company", ""),
                "score": job.get("fit_score", 0),
                "reason": reason,
                "instructions": state.get("instructions"),
                "savedInstruction": state.get("saved_instruction"),
                "chromePid": state.get("chrome_pid"),
                "lastFocused": state.get("last_focused", 0),
            })

        def _handle_log(self):
            self._json_ok({"history": list(reversed(state.get("history", [])))})

        def _handle_focus(self):
            state["last_focused"] = time.time()
            try:
                from applypilot.apply.chrome import bring_to_foreground_cdp, bring_to_foreground_pid
                # CDP bringToFront focuses the tab within Chrome.
                # bring_to_foreground_pid raises the OS window (X11/Wayland).
                # Both are needed: CDP alone doesn't always raise the window.
                bring_to_foreground_cdp(cdp_port)
                bring_to_foreground_pid(state.get("chrome_pid"))
            except Exception:
                pass
            self._text_ok()

        def _handle_takeover(self):
            takeover_event.set()
            # Kill the active Claude proc
            with _claude_lock:
                cproc = _claude_procs.get(worker_id)
            if cproc and cproc.poll() is None:
                _kill_process_tree(cproc.pid)
            state["status"] = "paused_by_user"
            self._text_ok()

        def _handle_run_task(self):
            body = self._read_body()
            instructions = body.get("instructions", "").strip()
            if not instructions:
                self.send_response(400)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"instructions required")
                return
            # Kill any previous mini proc
            old = state.get("mini_proc")
            if old and old.poll() is None:
                _kill_process_tree(old.pid)
            proc = _run_mini_task(worker_id, cdp_port, instructions)
            state["mini_proc"] = proc
            _mini_procs[worker_id] = proc
            task_id = str(int(time.time()))
            self._json_ok({"taskId": task_id})

        def _handle_task_stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            proc = state.get("mini_proc")
            if not proc:
                try:
                    self.wfile.write(b"data: No task running\n\n")
                    self.wfile.flush()
                except Exception:
                    pass
                return
            try:
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    # Extract text from stream-json format
                    try:
                        msg = json.loads(line)
                        if msg.get("type") == "assistant":
                            for block in msg.get("message", {}).get("content", []):
                                if block.get("type") == "text":
                                    text = block["text"].replace("\n", "\\n")
                                    self.wfile.write(f"data: {text}\n\n".encode())
                                    self.wfile.flush()
                    except json.JSONDecodeError:
                        safe = line.replace("\n", "\\n")
                        self.wfile.write(f"data: {safe}\n\n".encode())
                        self.wfile.flush()
                proc.wait()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _handle_handback(self):
            body = self._read_body()
            instructions = body.get("instructions", "").strip()
            save = body.get("save", False)
            if save and instructions:
                job = state.get("job") or {}
                site = job.get("site", "unknown")
                reason = state.get("reason") or "takeover"
                try:
                    from applypilot.database import store_qa
                    store_qa(
                        question=f"HITL:{site}:{reason}",
                        answer=instructions,
                        source="human",
                        field_type="hitl_instruction",
                    )
                    state["saved_instruction"] = instructions
                except Exception as e:
                    logger.debug("Failed to save HITL instruction to Q&A KB: %s", e)
            state["handback_instructions"] = instructions or None
            state["status"] = "applying"
            # Clear takeover so next job doesn't see it
            takeover_event.clear()
            # Unblock worker_loop
            handback_event.set()
            self._text_ok()

        def _handle_done(self):
            body = self._read_body()
            custom_instructions = (body.get("instructions") or "").strip()
            if custom_instructions:
                state["handback_instructions"] = custom_instructions
            hitl_evt = state.get("hitl_event")
            if hitl_evt:
                # Mark as resuming immediately so the extension shows loading state
                # before the worker loop picks it up and changes status to "applying".
                state["status"] = "resuming"
                hitl_evt.set()
            self._text_ok()

        def _handle_jobs_list(self):
            """Return actionable jobs for the extension Jobs tab."""
            from urllib.parse import parse_qs, urlparse as _up
            qs = parse_qs(_up(self.path).query)
            limit = min(int(qs.get("limit", ["50"])[0]), 200)
            try:
                from applypilot.database import get_connection
                conn = get_connection()
                rows = conn.execute("""
                    SELECT url, title, company, site, fit_score,
                           apply_status, apply_category, apply_error,
                           tailored_resume_path, cover_letter_path
                    FROM jobs
                    WHERE fit_score IS NOT NULL AND fit_score >= 6
                      AND (apply_status IS NULL
                           OR apply_status NOT IN ('applied', 'manual', 'in_progress'))
                    ORDER BY fit_score DESC, discovered_at DESC
                    LIMIT ?
                """, (limit,)).fetchall()
                self._json_ok({"jobs": [dict(r) for r in rows]})
            except Exception as e:
                logger.debug("jobs_list error: %s", e)
                self.send_response(500)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(str(e).encode())

        def _handle_add_job(self):
            """Add a job URL to the discovery queue from the extension."""
            from applypilot.database import get_connection
            body = self._read_body()
            url = (body.get("url") or "").strip()
            title = (body.get("title") or "").strip()
            if not url or not url.startswith("http"):
                self.send_response(400)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"url required")
                return
            try:
                from urllib.parse import urlparse
                site = urlparse(url).netloc.replace("www.", "")
                conn = get_connection()
                existing = conn.execute(
                    "SELECT url, apply_status FROM jobs WHERE url=?", (url,)
                ).fetchone()
                if existing:
                    self._json_ok({"status": "exists",
                                   "applyStatus": existing["apply_status"]})
                    return
                conn.execute(
                    "INSERT INTO jobs (url, title, site, discovered_at) "
                    "VALUES (?, ?, ?, datetime('now'))",
                    (url, title or "Unknown Position", site),
                )
                conn.commit()
                logger.info("[W%d] Added job via extension: %s", worker_id, url[:80])
                self._json_ok({"status": "queued"})
            except Exception as e:
                logger.debug("add_job error: %s", e)
                self.send_response(500)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(str(e).encode())

        def _handle_jobs_mark(self):
            """Manually mark a job's apply status from the extension Jobs tab."""
            from applypilot.database import get_connection
            from datetime import datetime, timezone as tz
            body = self._read_body()
            url    = (body.get("url") or "").strip()
            action = (body.get("action") or "").strip()
            valid  = ("applied", "skip", "error", "reset")
            if not url or action not in valid:
                self.send_response(400)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"url and action required")
                return
            try:
                conn = get_connection()
                now = datetime.now(tz.utc).isoformat()
                if action == "applied":
                    conn.execute("""UPDATE jobs SET apply_status='applied', applied_at=?,
                        apply_category='applied', apply_attempts=COALESCE(apply_attempts,0)+1
                        WHERE url=?""", (now, url))
                elif action == "skip":
                    conn.execute("""UPDATE jobs SET apply_status='failed',
                        apply_category='archived_ineligible',
                        apply_error='manually skipped', apply_attempts=99 WHERE url=?""", (url,))
                elif action == "error":
                    conn.execute("""UPDATE jobs SET apply_status='failed',
                        apply_category='archived_platform',
                        apply_error='manually marked error', apply_attempts=99 WHERE url=?""", (url,))
                elif action == "reset":
                    conn.execute("""UPDATE jobs SET apply_status=NULL, apply_category='pending',
                        apply_error=NULL, apply_attempts=0, agent_id=NULL WHERE url=?""", (url,))
                conn.commit()
                logger.info("[W%d] Manual mark '%s': %s", worker_id, action, url[:70])
                self._json_ok({"status": "ok", "action": action})
            except Exception as e:
                logger.debug("jobs_mark error: %s", e)
                self.send_response(500)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(str(e).encode())

        def log_message(self, format, *args):
            pass  # Suppress HTTP logging

    # Retry binding to the preferred port for up to 5s (old process may be dying).
    # Fall back to a random port only as a last resort, with a warning.
    preferred_port = port
    server = None
    for _attempt in range(6):
        try:
            server = _ThreadedHTTPServer(("127.0.0.1", port), _Handler)
            break
        except OSError:
            if _attempt < 5:
                time.sleep(1)
            else:
                server = _ThreadedHTTPServer(("127.0.0.1", 0), _Handler)
                port = server.server_address[1]
                logger.warning(
                    "Worker %d: preferred port %d was busy; using random port %d "
                    "(extension will not connect — restart the pipeline to fix)",
                    worker_id, preferred_port, port,
                )

    with _worker_server_lock:
        _worker_servers[worker_id] = server

    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name=f"worker-http-w{worker_id}")
    thread.start()
    logger.debug("Worker listener for worker %d on port %d", worker_id, port)
    return port


def _stop_worker_listener(worker_id: int) -> None:
    """Shut down a worker's always-on HTTP server."""
    with _worker_server_lock:
        server = _worker_servers.pop(worker_id, None)
    if server:
        server.shutdown()
    with _worker_state_lock:
        _worker_state.pop(worker_id, None)
    _takeover_events.pop(worker_id, None)
    _handback_events.pop(worker_id, None)
    _mini_procs.pop(worker_id, None)


def _inject_banner_for_worker(worker_id: int, cdp_port: int, job: dict,
                              reason: str, server_port: int,
                              navigate_url: str | None = None,
                              instructions: str | None = None) -> bool:
    """Inject a HITL banner into the worker's Chrome via CDP.

    Navigates to navigate_url first (so the user sees the stuck page, not
    about:blank), injects the banner, then brings Chrome to the foreground.
    """
    from applypilot.apply.human_review import _inject_banner, _navigate_chrome

    # Navigate to the stuck URL so the user sees the page (not about:blank)
    if navigate_url:
        _navigate_chrome(cdp_port, navigate_url)
        time.sleep(1)  # Give the page a moment to start loading

    # Build a job-like dict with HITL fields for the banner
    banner_job = dict(job)
    if instructions is None:
        instructions = _HITL_INSTRUCTIONS.get(reason, f"Human action required: {reason}")
    banner_job["needs_human_instructions"] = instructions

    result = _inject_banner(cdp_port, banner_job, server_port=server_port)

    # Un-minimize Chrome so the user sees the HITL request
    bring_to_foreground()

    return result


# ---------------------------------------------------------------------------
# Account capture from agent output
# ---------------------------------------------------------------------------

def _parse_account_created(output: str, job_url: str | None = None) -> None:
    """Parse ACCOUNT_CREATED lines from agent output and save to DB."""
    from applypilot.database import store_account
    for line in output.split("\n"):
        if "ACCOUNT_CREATED:" not in line:
            continue
        try:
            json_str = line.split("ACCOUNT_CREATED:", 1)[1].strip()
            account = json.loads(json_str)
            conn = get_connection()
            store_account(conn, account, job_url=job_url)
            logger.info("Saved new account: %s @ %s",
                        account.get("email"), account.get("domain"))
        except (json.JSONDecodeError, IndexError, Exception) as e:
            logger.warning("Failed to parse ACCOUNT_CREATED line: %s", e)


def _parse_qa_lines(output: str, job_url: str | None = None,
                    ats_slug: str | None = None) -> int:
    """Parse QA: lines from agent output and store in qa_knowledge DB.

    Format: QA:{question}|{answer}|{field_type}

    Returns count of Q&A pairs stored.
    """
    from applypilot.database import store_qa
    count = 0
    for line in output.split("\n"):
        if not line.strip().startswith("QA:"):
            continue
        try:
            payload = line.split("QA:", 1)[1].strip()
            parts = payload.split("|")
            if len(parts) < 2:
                continue
            question = parts[0].strip()
            answer = parts[1].strip()
            field_type = parts[2].strip() if len(parts) > 2 else None
            if question and answer:
                store_qa(question, answer, source="agent",
                         field_type=field_type, ats_slug=ats_slug,
                         job_url=job_url)
                count += 1
        except Exception as e:
            logger.debug("Failed to parse QA line: %s", e)
    if count:
        logger.info("Stored %d Q&A pair(s) from agent output", count)
    return count


# ---------------------------------------------------------------------------
# Gmail token refresh
# ---------------------------------------------------------------------------

_gmail_token_lock = threading.Lock()


def _refresh_gmail_token() -> bool:
    """Ensure the Gmail OAuth access token is fresh.

    The @gongrzhe/server-gmail-autoauth-mcp MCP server does NOT refresh
    tokens on its own — it reads the access_token from credentials.json
    and uses it directly.  Access tokens expire after 1 hour, so we
    must refresh proactively before each apply run.

    Returns True if token is valid/refreshed, False if Gmail is unavailable.
    """
    creds_path = Path.home() / ".gmail-mcp" / "credentials.json"
    keys_path = Path.home() / ".gmail-mcp" / "gcp-oauth.keys.json"

    if not creds_path.exists() or not keys_path.exists():
        logger.debug("Gmail MCP credentials not found, skipping refresh")
        return False

    with _gmail_token_lock:
        try:
            creds = json.loads(creds_path.read_text())
            keys_data = json.loads(keys_path.read_text())
            key_info = keys_data.get("installed") or keys_data.get("web", {})

            # Check if token expires within next 5 minutes
            expiry_ms = creds.get("expiry_date", 0)
            now_ms = time.time() * 1000
            if expiry_ms - now_ms > 300_000:  # > 5 min remaining
                return True

            logger.info("Gmail token expiring soon, refreshing...")
            import urllib.request
            import urllib.parse

            data = urllib.parse.urlencode({
                "client_id": key_info["client_id"],
                "client_secret": key_info["client_secret"],
                "refresh_token": creds["refresh_token"],
                "grant_type": "refresh_token",
            }).encode()
            req = urllib.request.Request(
                "https://oauth2.googleapis.com/token", data=data,
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=15).read())

            creds["access_token"] = resp["access_token"]
            creds["expiry_date"] = int((time.time() + resp["expires_in"]) * 1000)
            creds_path.write_text(json.dumps(creds, indent=2))
            logger.info("Gmail token refreshed, expires in %ds", resp["expires_in"])
            return True
        except Exception as e:
            logger.warning("Gmail token refresh failed: %s", e)
            return False


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------

def _make_mcp_config(cdp_port: int, worker_id: int = 0) -> dict:
    """Build MCP config dict for a specific CDP port.

    Passes the real Chrome user-agent to Playwright MCP so it doesn't
    override our Chrome --user-agent flag with its default
    "HeadlessChrome" UA when connecting via CDP.

    The viewport is synced with the Chrome window size chosen for this
    worker (see chrome._pick_viewport / get_worker_viewport).
    """
    from applypilot.apply.chrome import _get_real_user_agent, get_worker_viewport

    vp = get_worker_viewport(worker_id)
    return {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": [
                    "@playwright/mcp@latest",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                    f"--viewport-size={vp[0]}x{vp[1]}",
                    f"--user-agent={_get_real_user_agent()}",
                ],
            },
            "gmail": {
                "command": "npx",
                "args": ["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
            },
        }
    }


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def _db_retry_execute(conn: "sqlite3.Connection", sql: str,
                      params: tuple = (), timeout: float = 300.0) -> "sqlite3.Cursor":
    """Execute a SQL statement with retry on 'database is locked' errors.

    Concurrent tailor/cover/pdf pipeline stages can hold WAL write locks for
    minutes at a time.  Plain conn.execute() raises OperationalError immediately
    when that happens.  This helper retries with exponential backoff so callers
    don't crash just because another stage is mid-write.
    """
    deadline = time.monotonic() + timeout
    delay = 2.0
    while True:
        try:
            return conn.execute(sql, params)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            logger.debug("DB locked, retrying in %.0fs…", delay)
            time.sleep(delay)
            delay = min(delay * 1.5, 30.0)


def _db_retry_commit(conn: "sqlite3.Connection", timeout: float = 300.0) -> None:
    """Commit with retry on 'database is locked' errors."""
    deadline = time.monotonic() + timeout
    delay = 2.0
    while True:
        try:
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            logger.debug("DB locked on commit, retrying in %.0fs…", delay)
            time.sleep(delay)
            delay = min(delay * 1.5, 30.0)


def acquire_job(target_url: str | None = None,
                min_score: int | None = None,
                max_score: int | None = None,
                max_age_days: int | None = None,
                worker_id: int = 0) -> dict | None:
    """Atomically acquire the next job to apply to.

    Enforces:
      - Minimum fit score (config.DEFAULTS["min_score"] default)
      - Job age cutoff (config.DEFAULTS["max_job_age_days"] default)
      - Per-company open-pipeline cap (YAML-configurable)
      - Per-company concurrency: at most 1 active worker per company
      - Per-ATS concurrency: at most 1 active worker per ATS family
      - Manual-ATS skip list
    """
    from applypilot import config as _cfg
    from datetime import datetime, timedelta, timezone

    if min_score is None:
        min_score = _cfg.DEFAULTS["min_score"]
    if max_age_days is None:
        max_age_days = _cfg.DEFAULTS["max_job_age_days"]

    conn = get_connection()
    try:
        _begin_deadline = time.monotonic() + 300
        _begin_delay = 2.0
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as _be:
                if "locked" not in str(_be).lower():
                    raise
                if time.monotonic() >= _begin_deadline:
                    raise
                logger.debug("acquire_job: DB locked, retrying in %.0fs…", _begin_delay)
                time.sleep(_begin_delay)
                _begin_delay = min(_begin_delay * 1.5, 30.0)

        # Release stale in_progress locks from crashed runs (>30 min old)
        conn.execute("""
            UPDATE jobs SET apply_status = NULL, agent_id = NULL
            WHERE apply_status = 'in_progress'
              AND last_attempted_at IS NOT NULL
              AND last_attempted_at < datetime('now', '-30 minutes')
        """)

        if target_url:
            like = f"%{target_url.split('?')[0].rstrip('/')}%"
            row = conn.execute("""
                SELECT url, title, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path, company
                FROM jobs
                WHERE (url = ? OR application_url = ? OR application_url LIKE ? OR url LIKE ?)
                  AND tailored_resume_path IS NOT NULL
                  AND (apply_status IS NULL OR apply_status != 'in_progress')
                ORDER BY
                    CASE WHEN url = ? OR application_url = ? THEN 0 ELSE 1 END
                LIMIT 1
            """, (target_url, target_url, like, like,
                  target_url, target_url)).fetchone()
        else:
            blocked_sites, blocked_patterns = _load_blocked()
            site_filter = " AND ".join(f"site != '{s}'" for s in blocked_sites) if blocked_sites else "1=1"
            url_filter = " AND ".join(f"url NOT LIKE '{p}'" for p in blocked_patterns) if blocked_patterns else "1=1"
            max_score_filter = f"AND j.fit_score <= {max_score}" if max_score is not None else ""

            # Per-worker concurrency: don't let two workers run the same company or ATS
            in_progress_rows = conn.execute(
                "SELECT company, application_url FROM jobs WHERE apply_status = 'in_progress'"
            ).fetchall()
            active_companies: set[str] = set()
            active_ats: set[str] = set()
            for ip in in_progress_rows:
                if ip["company"]:
                    active_companies.add(ip["company"].lower())
                ats = detect_ats(ip["application_url"] or "")
                if ats:
                    active_ats.add(ats)

            if active_companies:
                ph = ",".join("?" * len(active_companies))
                company_excl = f"AND LOWER(COALESCE(j.company, '')) NOT IN ({ph})"
                company_excl_params: list = list(active_companies)
            else:
                company_excl = ""
                company_excl_params = []

            # Age filter
            age_filter = ""
            age_params: list = []
            if max_age_days and max_age_days > 0:
                age_filter = "AND j.discovered_at > datetime('now', ?)"
                age_params = [f"-{max_age_days} days"]

            # Fetch candidates. No more soft-sort deprioritization — hard cap
            # is enforced in Python below via get_company_limit().
            candidates = conn.execute(f"""
                SELECT j.url, j.title, j.site, j.application_url,
                       j.tailored_resume_path, j.fit_score, j.location,
                       j.full_description, j.cover_letter_path, j.company,
                       j.strategy
                FROM jobs j
                WHERE j.tailored_resume_path IS NOT NULL
                  AND (j.apply_status IS NULL OR j.apply_status = 'failed')
                  AND (j.apply_attempts IS NULL OR j.apply_attempts < {config.DEFAULTS["max_apply_attempts"]})
                  AND j.fit_score >= ?
                  {max_score_filter}
                  AND {site_filter}
                  AND {url_filter}
                  {company_excl}
                  {age_filter}
                ORDER BY j.fit_score DESC, j.url
                LIMIT 100
            """, (min_score, *company_excl_params, *age_params)).fetchall()

            # Build in-flight buckets once, reuse for every candidate.
            # Use resolve_company_key so Greenhouse/Workday jobs (NULL company,
            # employer name in `site`) bucket correctly.
            from applypilot.scoring.tailor import resolve_company_key
            in_flight = get_in_flight_by_company(conn)
            now_utc = datetime.now(timezone.utc)

            def over_cap(job: dict) -> bool:
                key = resolve_company_key(job)
                if not key:
                    return False
                cap, window = _cfg.get_company_limit(key)
                if cap < 0:
                    return False
                if cap == 0:
                    return True
                cutoff = (now_utc - timedelta(days=window)).isoformat()
                count = sum(1 for ts in in_flight.get(key, [])
                            if ts and ts > cutoff)
                return count >= cap

            # Pick first candidate whose company is under cap AND ATS lane is free.
            row = None
            for cand in candidates:
                if over_cap(dict(cand)):
                    continue
                ats = detect_ats(cand["application_url"] or cand["url"] or "")
                if ats is not None and ats in active_ats:
                    continue
                row = cand
                break

            if row is None and candidates:
                logger.debug(
                    "acquire_job: all %d candidates blocked (ATS lanes=%s, cap-blocked companies present)",
                    len(candidates), active_ats,
                )

        if not row:
            conn.rollback()
            return None

        from applypilot.config import is_manual_ats
        apply_url = row["application_url"] or row["url"]
        if is_manual_ats(apply_url):
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS', "
                "apply_category = 'manual_only' WHERE url = ?",
                (row["url"],),
            )
            conn.commit()
            logger.info("Skipping manual ATS: %s", row["url"][:80])
            return None

        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            UPDATE jobs SET apply_status = 'in_progress',
                           agent_id = ?,
                           last_attempted_at = ?
            WHERE url = ?
        """, (f"worker-{worker_id}", now, row["url"]))
        conn.commit()

        return dict(row)
    except Exception:
        conn.rollback()
        raise


def mark_result(url: str, status: str, error: str | None = None,
                permanent: bool = False, duration_ms: int | None = None,
                task_id: str | None = None) -> None:
    """Update a job's apply status in the database."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        _db_retry_execute(conn, """
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?,
                           apply_category = 'applied'
            WHERE url = ?
        """, (now, duration_ms, task_id, url))
    else:
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        category = categorize_apply_result(status, error)
        _db_retry_execute(conn, f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?,
                           apply_category = ?
            WHERE url = ?
        """, (status, error or "unknown", duration_ms, task_id, category, url))
    _db_retry_commit(conn)


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    _db_retry_execute(conn,
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    _db_retry_commit(conn)


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int | None = None, max_score: int | None = None,
               model: str = "sonnet", worker_id: int = 0) -> Path | None:
    """Generate a prompt file and print the Claude CLI command for manual debugging.

    Returns:
        Path to the generated prompt file, or None if no job found.
    """
    from applypilot import config as _cfg
    if min_score is None:
        min_score = _cfg.DEFAULTS["min_score"]
    job = acquire_job(target_url=target_url, min_score=min_score, max_score=max_score,
                      worker_id=worker_id)
    if not job:
        return None

    # Read resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    prompt = prompt_mod.build_prompt(job=job, tailored_resume=resume_text, doc_format=_doc_format)

    # Release the lock so the job stays available
    release_lock(job["url"])

    # Write prompt file
    config.ensure_dirs()
    site_slug = (job.get("site") or "unknown")[:20].replace(" ", "_")
    prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{(job.get('title') or 'unknown')[:30].replace(' ', '_')}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    # Write MCP config for reference
    port = BASE_CDP_PORT + worker_id
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_path.write_text(json.dumps(_make_mcp_config(port, worker_id=worker_id)), encoding="utf-8")

    return prompt_file


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        _db_retry_execute(conn, """
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_category = 'applied'
            WHERE url = ?
        """, (now, url))
    else:
        error = reason or "manual"
        category = categorize_apply_result("failed", error)
        _db_retry_execute(conn, """
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = 99, agent_id = NULL,
                           apply_category = ?
            WHERE url = ?
        """, (error, category, url))
    _db_retry_commit(conn)


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = _db_retry_execute(conn, """
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL,
                       apply_category = NULL
        WHERE apply_status = 'failed'
          OR (apply_status IS NOT NULL AND apply_status != 'applied'
              AND apply_status != 'in_progress'
              AND apply_status != 'needs_human')
    """)
    _db_retry_commit(conn)
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Human-in-the-Loop (HITL) support
# ---------------------------------------------------------------------------

_HITL_INSTRUCTIONS: dict[str, str] = {
    "workday_signup": (
        "CREATE a Workday account using alex@elninja.com. "
        "Click 'Create Account', fill in your email and a password, "
        "verify your email if prompted. Click Done when you're logged in."
    ),
    "login_required": (
        "LOG IN to this site using alex@elninja.com. "
        "If you don't have an account, create one. "
        "Click Done once you're logged in and on the application page."
    ),
    "login_issue": (
        "LOGIN ISSUE. The agent couldn't complete login. Try logging in manually "
        "with alex@elninja.com (or create an account if needed). "
        "Click Done when you're logged in and on the application page."
    ),
    "captcha": (
        "CAPTCHA DETECTED. Solve the CAPTCHA shown on the page, then click Done "
        "to let the agent continue the application."
    ),
    "account_required": (
        "ACCOUNT REQUIRED. Create an account using alex@elninja.com, then navigate "
        "to the job application. Click Done when you're on the application form."
    ),
    "sso_required": (
        "SSO LOGIN REQUIRED. Log in using Google or Microsoft SSO. "
        "Click Done when you're logged in and on the application page."
    ),
    "resume_upload_blocked": (
        "RESUME UPLOAD BLOCKED. Manually upload ~/.applypilot/resume.pdf to the "
        "upload field on the page. Click Done when the file is uploaded."
    ),
    "stuck": (
        "FORM STUCK. The agent got stuck on a form element. Review the form, "
        "fix any issues or stuck fields, and submit if possible. "
        "Click Done when done (even if you only unstuck it for the agent to retry)."
    ),
    "email_verification": (
        "COMPLETE EMAIL VERIFICATION. Check alex@elninja.com for a verification "
        "email/code, then enter it on the page. Click Done when verified."
    ),
    "sms_verification": (
        "COMPLETE SMS/PHONE VERIFICATION. The site requires a phone code that "
        "the agent cannot receive. Check your phone for the code, enter it on "
        "the page, then click Done."
    ),
    "form_stuck": (
        "COMPLETE THE APPLICATION FORM. The agent filled what it could but got stuck "
        "on a form element (usually a custom dropdown or validation error). "
        "Review the form, fix missing fields, and submit. "
        "Click Done when the application is submitted."
    ),
    "screening_questions": (
        "ANSWER SCREENING QUESTIONS. The agent reached screening questions it wasn't "
        "confident answering from your profile. Review and answer them, then submit. "
        "Click Done when finished."
    ),
    "security_concern": (
        "⚠️ SECURITY ALERT — The agent flagged suspicious content on this form. "
        "Check the apply log for details on what was detected (prompt injection, "
        "bot trap, credential request, or data exfiltration attempt). "
        "Review the page carefully before proceeding. "
        "If the form looks legitimate, complete it manually and click Done. "
        "If it looks malicious, close the tab and click Done to abandon."
    ),
}


def mark_needs_human(url: str, reason: str, stuck_url: str,
                     instructions: str, duration_ms: int | None = None) -> None:
    """Park a job for human review instead of marking it as failed."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    _db_retry_execute(conn, """
        UPDATE jobs SET apply_status = 'needs_human',
                       needs_human_reason = ?,
                       needs_human_url = ?,
                       needs_human_instructions = ?,
                       agent_id = NULL,
                       apply_duration_ms = ?,
                       last_attempted_at = ?,
                       apply_category = 'needs_human'
        WHERE url = ?
    """, (reason, stuck_url, instructions, duration_ms, now, url))
    _db_retry_commit(conn)


def reset_needs_human(url: str | None = None) -> int:
    """Reset parked jobs (needs_human) back to NULL so they can be retried.

    Args:
        url: Reset a specific job URL. If None, resets all parked jobs.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    if url:
        cursor = _db_retry_execute(conn, """
            UPDATE jobs SET apply_status = NULL,
                           needs_human_reason = NULL,
                           needs_human_url = NULL,
                           needs_human_instructions = NULL,
                           agent_id = NULL,
                           apply_category = NULL
            WHERE url = ? AND apply_status = 'needs_human'
        """, (url,))
    else:
        cursor = _db_retry_execute(conn, """
            UPDATE jobs SET apply_status = NULL,
                           needs_human_reason = NULL,
                           needs_human_url = NULL,
                           needs_human_instructions = NULL,
                           agent_id = NULL,
                           apply_category = NULL
            WHERE apply_status = 'needs_human'
        """)
    _db_retry_commit(conn)
    return cursor.rowcount


def _send_desktop_notification(title: str, body: str) -> None:
    """Send a desktop notification. Silent on failure."""
    try:
        if platform.system() == "Darwin":
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{body}" with title "{title}"'],
                timeout=5, capture_output=True,
            )
        else:
            subprocess.run(
                ["notify-send", "--urgency=critical", title, body],
                timeout=5, capture_output=True,
            )
    except Exception:
        pass


def notify_human_needed(job: dict, reason: str, stuck_url: str) -> None:
    """Print a prominent terminal alert and send desktop notification."""
    title = job.get("title", "Unknown")
    company = job.get("site", "")
    score = job.get("fit_score", "?")
    instructions = _HITL_INSTRUCTIONS.get(reason, f"Human action required: {reason}")

    print(
        f"\n\033[1;35m⚑ HUMAN REVIEW NEEDED ⚑\033[0m\n"
        f"  Job:    {title} @ {company}  (score: {score}/10)\n"
        f"  Reason: {reason}\n"
        f"  URL:    {stuck_url}\n"
        f"  Action: {instructions}\n"
        f"  Review: applypilot human-review\n"
        f"\a",
        file=sys.stderr, flush=True,
    )

    _send_desktop_notification(
        "ApplyPilot: Human Review Needed",
        f"{title} @ {company} — {reason}",
    )


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def _infer_result_from_output(output: str) -> str | None:
    """Infer a result from agent output when no RESULT line was emitted.

    Scans for common phrases that indicate success or a specific failure mode.
    Returns 'applied' for detected successful submissions, a failure reason
    string for failures, or None if nothing can be inferred.
    """
    lower = output.lower()

    # Check for successful application first — agent submitted but forgot RESULT:APPLIED
    success_phrases = [
        "application submitted successfully",
        "application was sent to",
        "application has been received",
        "successfully submitted",
        "thank you for applying",
        "your application was sent",
        "application sent",
        "application received",
        "application submitted",
    ]
    # Strong indicators alone are enough
    strong_success = [
        "application submitted successfully",
        "your application was sent to",
        "thank you for applying",
        "successfully submitted",
    ]
    for phrase in strong_success:
        if phrase in lower:
            return "applied"
    # Weaker indicators need 2+ matches
    success_count = sum(1 for p in success_phrases if p in lower)
    if success_count >= 2:
        return "applied"

    # Order matters — check most specific patterns first
    patterns: list[tuple[str, list[str]]] = [
        ("login_issue", [
            "password reset requires email",
            "cannot log in",
            "login failed permanently",
            "cannot access external email",
            "session has now expired",
            "credentials between session",
        ]),
        ("account_required", [
            "account was successfully created",
            "password from the original account",
            "password was not stored",
        ]),
        ("captcha", [
            "blocked by captcha",
            "captcha cannot be solved",
            "unsolvable captcha",
        ]),
        ("already_applied", [
            "you've already applied",
            "you have already applied",
            "already applied to this",
            "application already submitted",
            "duplicate application",
            "already applied for this role",
            "application already exists",
        ]),
        ("expired", [
            "no longer accepting",
            "job has been closed",
            "position has been filled",
            "listing is closed",
            "listing has expired",
        ]),
        ("not_eligible_location", [
            "not eligible for this location",
            "onsite only",
            "outside your area",
            "cannot relocate",
        ]),
        ("stuck", [
            "cannot be completed through browser automation",
            "must be completed manually",
            "sandbox environment",
            "sandboxed environment",
            "non-sandboxed environment",
            "technical blocker",
            "cannot satisfy",
            "blocked_by_environment",
            "infrastructure-level",
        ]),
        ("browser_unavailable", [
            "browser automation service is not responding",
            "connection refused on localhost",
            "browser connection issue",
        ]),
    ]
    for reason, phrases in patterns:
        for phrase in phrases:
            if phrase in lower:
                return reason
    return None


def _reset_browser_tabs(port: int) -> None:
    """Close all existing tabs and open a fresh about:blank tab via CDP.

    Prevents leftover tabs from a previous job confusing the next agent.
    Skips chrome:// internal pages (omnibox overlays etc.) to avoid side effects.
    """
    import urllib.request
    try:
        data = urllib.request.urlopen(f"http://localhost:{port}/json", timeout=3).read()
        tabs = json.loads(data)
        # Only close http/https/about: pages — never touch chrome:// internal targets
        closeable = [
            t for t in tabs
            if t.get("type") == "page"
            and not t.get("url", "").startswith("chrome://")
        ]
        if not closeable:
            return
        # Open a fresh blank tab first (CDP requires PUT for /json/new)
        req = urllib.request.Request(
            f"http://localhost:{port}/json/new?about:blank", method="PUT"
        )
        urllib.request.urlopen(req, timeout=3)
        # Close all old closeable tabs
        for tab in closeable:
            try:
                urllib.request.urlopen(
                    f"http://localhost:{port}/json/close/{tab['id']}", timeout=2
                )
            except Exception:
                pass
    except Exception:
        pass  # Chrome not ready yet or CDP unavailable — agent will navigate anyway


def _activate_agent_tab(port: int, timeout: float = 20.0) -> None:
    """Background thread: activate the first real (non-blank) page the agent navigates to.

    Playwright MCP creates a new tab for the agent rather than reusing the existing
    about:blank tab. Without this, the user's visible Chrome tab stays on about:blank
    while the agent works in a background tab.
    """
    import urllib.request
    deadline = time.time() + timeout
    activated_url = None
    while time.time() < deadline:
        try:
            data = urllib.request.urlopen(f"http://localhost:{port}/json", timeout=2).read()
            tabs = json.loads(data)
            for tab in tabs:
                url = tab.get("url", "")
                if (tab.get("type") == "page"
                        and url
                        and not url.startswith("about:")
                        and not url.startswith("chrome://")):
                    tab_id = tab.get("id")
                    if tab_id and url != activated_url:
                        urllib.request.urlopen(
                            f"http://localhost:{port}/json/activate/{tab_id}", timeout=2
                        )
                        activated_url = url
                        # Keep watching in case the agent opens a new tab mid-job
                    break
        except Exception:
            pass
        time.sleep(0.75)


def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False,
            skip_tab_reset: bool = False,
            extra_context: str | None = None) -> tuple[str, int, list[dict]]:
    """Spawn a Claude Code session for one job application.

    Args:
        job: Job dict from the database.
        port: CDP port for the worker's Chrome.
        worker_id: Numeric worker identifier.
        model: Claude model name.
        dry_run: If True, don't click Submit.
        skip_tab_reset: If True, don't close leftover tabs (used after HITL/takeover).
        extra_context: Optional instructions from a previous human takeover, prepended
            to the agent prompt so it knows what was done.

    Returns:
        Tuple of (status_string, duration_ms, screening_questions).
        screening_questions is a list of dicts with keys: question, field_type, options.
    """
    # Close leftover tabs from previous job so agent starts on a blank page
    if not skip_tab_reset:
        _reset_browser_tabs(port)

    # Read tailored resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    # Build the prompt
    agent_prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=dry_run,
        worker_id=worker_id,
        doc_format=_doc_format,
    )

    # When resuming after user takeover: inject a RESUME banner so Claude does NOT
    # follow step 1 (browser_navigate) and wipe whatever the user already filled in.
    if skip_tab_reset:
        resume_header = (
            "== ⚠ RESUMING AFTER USER TAKEOVER ⚠ ==\n"
            "The browser already has the application form open. The user may have partially filled it.\n"
            "MANDATORY FIRST ACTION: browser_snapshot — see the current page state before doing anything else.\n"
            "FORBIDDEN: browser_navigate — do NOT navigate to any URL. Do NOT load the job URL.\n"
            "SKIP steps 1, 1a, 3, and 4 in STEP-BY-STEP entirely.\n"
            "After the snapshot: check for a login wall (step 5), then continue filling remaining form\n"
            "fields from the current page state and submit.\n"
            "== END RESUME ==\n\n"
        )
        if extra_context:
            resume_header += (
                f"== USER INSTRUCTIONS ==\n"
                f"{extra_context}\n"
                f"== END USER INSTRUCTIONS ==\n\n"
            )
        agent_prompt = resume_header + agent_prompt
    elif extra_context:
        agent_prompt = (
            f"== USER INSTRUCTIONS (from previous human takeover) ==\n"
            f"{extra_context}\n"
            f"== END USER INSTRUCTIONS ==\n\n"
            f"{agent_prompt}"
        )

    # Refresh Gmail token before writing MCP config (the MCP server doesn't auto-refresh)
    _refresh_gmail_token()

    # Write per-worker MCP config
    mcp_config_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_config_path.write_text(json.dumps(_make_mcp_config(port, worker_id=worker_id)), encoding="utf-8")

    # Build claude command
    cmd = [
        "claude",
        "--model", model,
        "-p",
        "--mcp-config", str(mcp_config_path),
        "--strict-mcp-config",
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
        "--disallowedTools", ",".join([
            # browser_install restarts the browser in CDP mode, breaking the session
            "mcp__playwright__browser_install",
            # Block Gmail write tools (read-only access for email verification)
            "mcp__gmail__draft_email", "mcp__gmail__modify_email",
            "mcp__gmail__delete_email", "mcp__gmail__download_attachment",
            "mcp__gmail__batch_modify_emails", "mcp__gmail__batch_delete_emails",
            "mcp__gmail__create_label", "mcp__gmail__update_label",
            "mcp__gmail__delete_label", "mcp__gmail__get_or_create_label",
            "mcp__gmail__list_email_labels", "mcp__gmail__create_filter",
            "mcp__gmail__list_filters", "mcp__gmail__get_filter",
            "mcp__gmail__delete_filter",
        ]),
        "--output-format", "stream-json",
        "--verbose", "-",
    ]

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    # Remove ANTHROPIC_API_KEY so the subprocess uses the user's Max plan
    # login instead of API billing. The key is loaded by config.load_env()
    # for the Gemini/OpenAI LLM fallback chain but must NOT leak into the
    # Claude Code subprocess — it would override interactive auth and hit
    # "credit balance is too low" on an unfunded API account.
    env.pop("ANTHROPIC_API_KEY", None)

    worker_dir = reset_worker_dir(worker_id)

    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="starting")
    add_event(f"[W{worker_id}] Starting: {(job.get('title') or '')[:40]} @ {job.get('site', '')}")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {job.get('site', '')}\n"
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"{'=' * 60}\n"
    )

    start = time.time()
    stats: dict = {}
    proc = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(worker_dir),
            start_new_session=True,
        )
        with _claude_lock:
            _claude_procs[worker_id] = proc

        proc.stdin.write(agent_prompt)
        proc.stdin.close()

        # Background thread: activate the agent's tab as soon as it navigates.
        # Playwright MCP creates a new tab rather than reusing the existing blank tab,
        # so without this the user's visible Chrome tab stays on about:blank.
        threading.Thread(
            target=_activate_agent_tab,
            args=(port,),
            daemon=True,
            name=f"tab-activator-w{worker_id}",
        ).start()

        text_parts: list[str] = []
        screening_qs: list[dict] = []
        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(log_header)

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                # Check for user takeover between output lines
                tev = _takeover_events.get(worker_id)
                if tev and tev.is_set():
                    break
                try:
                    msg = json.loads(line)
                    msg_type = msg.get("type")
                    if msg_type == "assistant":
                        for block in msg.get("message", {}).get("content", []):
                            bt = block.get("type")
                            if bt == "text":
                                text_parts.append(block["text"])
                                lf.write(block["text"] + "\n")
                                # Parse SCREENING_Q lines from text
                                for tl in block["text"].split("\n"):
                                    tl = tl.strip()
                                    if tl.startswith("SCREENING_Q:"):
                                        payload = tl[len("SCREENING_Q:"):].strip()
                                        parts = payload.split("|")
                                        if len(parts) >= 2:
                                            screening_qs.append({
                                                "question": parts[0].strip(),
                                                "field_type": parts[1].strip(),
                                                "options": parts[2].strip() if len(parts) > 2 else "",
                                            })
                            elif bt == "tool_use":
                                name = (
                                    block.get("name", "")
                                    .replace("mcp__playwright__", "")
                                    .replace("mcp__gmail__", "gmail:")
                                )
                                inp = block.get("input", {})
                                if "url" in inp:
                                    desc = f"{name} {inp['url'][:60]}"
                                elif "ref" in inp:
                                    desc = f"{name} {inp.get('element', inp.get('text', ''))}"[:50]
                                elif "fields" in inp:
                                    desc = f"{name} ({len(inp['fields'])} fields)"
                                elif "paths" in inp:
                                    desc = f"{name} upload"
                                else:
                                    desc = name

                                lf.write(f"  >> {desc}\n")
                                ws = get_state(worker_id)
                                cur_actions = ws.actions if ws else 0
                                update_state(worker_id,
                                             actions=cur_actions + 1,
                                             last_action=desc[:35])
                    elif msg_type == "result":
                        stats = {
                            "input_tokens": msg.get("usage", {}).get("input_tokens", 0),
                            "output_tokens": msg.get("usage", {}).get("output_tokens", 0),
                            "cache_read": msg.get("usage", {}).get("cache_read_input_tokens", 0),
                            "cache_create": msg.get("usage", {}).get("cache_creation_input_tokens", 0),
                            "cost_usd": msg.get("total_cost_usd", 0),
                            "turns": msg.get("num_turns", 0),
                        }
                        text_parts.append(msg.get("result", ""))
                except json.JSONDecodeError:
                    text_parts.append(line)
                    lf.write(line + "\n")

        proc.wait(timeout=300)
        returncode = proc.returncode
        proc = None

        # Check if a user takeover killed the proc
        tev = _takeover_events.get(worker_id)
        if tev and tev.is_set():
            return "takeover", int((time.time() - start) * 1000), []

        if returncode and returncode < 0:
            return "skipped", int((time.time() - start) * 1000), []

        output = "\n".join(text_parts)
        elapsed = int(time.time() - start)
        duration_ms = int((time.time() - start) * 1000)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_log = config.LOG_DIR / f"claude_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
        job_log.write_text(output, encoding="utf-8")

        if stats:
            cost = stats.get("cost_usd", 0)
            ws = get_state(worker_id)
            prev_cost = ws.total_cost if ws else 0.0
            update_state(worker_id, total_cost=prev_cost + cost)

        # Detect Claude Code credit exhaustion — stop the entire worker
        if "credit balance is too low" in output.lower() or "insufficient credits" in output.lower():
            add_event(f"[W{worker_id}] CREDIT EXHAUSTED — Claude Code credits depleted")
            update_state(worker_id, status="credits_exhausted",
                         last_action="NO CREDITS")
            logger.error("Claude Code credits exhausted. Cannot auto-apply. "
                         "Top up at https://console.anthropic.com/settings/billing")
            return "failed:credits_exhausted", duration_ms, []

        # Parse ACCOUNT_CREATED lines and save to DB
        _parse_account_created(output, job.get("url"))

        # Parse QA: lines and store in knowledge base
        job_url = job.get("url")
        job_ats = detect_ats(job.get("application_url") or job_url or "")
        _parse_qa_lines(output, job_url=job_url, ats_slug=job_ats)

        def _clean_reason(s: str) -> str:
            return re.sub(r'[*`"]+$', '', s).strip()

        for result_status in ["APPLIED", "ALREADY_APPLIED", "SUCCESS", "EXPIRED", "CAPTCHA", "LOGIN_ISSUE"]:
            if f"RESULT:{result_status}" in output:
                # Normalize SUCCESS/ALREADY_APPLIED -> applied (already applied counts as applied)
                canonical = "applied" if result_status in ("SUCCESS", "ALREADY_APPLIED") else result_status.lower()
                # Mark Q&A outcomes based on application result
                if canonical == "applied" and job_url:
                    from applypilot.database import mark_qa_outcome
                    mark_qa_outcome(job_url, "accepted")
                display = "ALREADY APPLIED" if result_status == "ALREADY_APPLIED" else canonical.upper()
                add_event(f"[W{worker_id}] {display} ({elapsed}s): {(job.get('title') or '')[:30]}")
                update_state(worker_id, status=canonical,
                             last_action=f"{display} ({elapsed}s)")
                return canonical, duration_ms, screening_qs

        # Check for RESULT:NEEDS_HUMAN:{reason}:{stuck_url}
        # Must be parsed before RESULT:FAILED since the format includes colons
        if "RESULT:NEEDS_HUMAN:" in output:
            for out_line in output.split("\n"):
                if "RESULT:NEEDS_HUMAN:" in out_line:
                    # Format: RESULT:NEEDS_HUMAN:{reason}:{url} [reason: detail]
                    # Split on "NEEDS_HUMAN:" then split first colon to get reason vs rest
                    after = out_line.split("RESULT:NEEDS_HUMAN:", 1)[-1].strip()
                    after = _clean_reason(after)
                    # Extract optional [reason: ...] detail suffix from the end
                    reason_detail = ""
                    if " [reason: " in after:
                        after, detail_part = after.rsplit(" [reason: ", 1)
                        reason_detail = detail_part.rstrip("]").strip()
                        after = after.strip()
                    if ":" in after:
                        nh_reason, nh_url = after.split(":", 1)
                        nh_reason = nh_reason.strip()
                        nh_url = nh_url.strip()
                    else:
                        nh_reason = after
                        nh_url = job.get("application_url") or job["url"]
                    if reason_detail:
                        nh_url = f"{nh_url}|detail:{reason_detail}"
                    add_event(f"[W{worker_id}] NEEDS_HUMAN:{nh_reason} ({elapsed}s): {(job.get('title') or '')[:30]}")
                    update_state(worker_id, status="needs_human",
                                 last_action=f"NEEDS_HUMAN: {nh_reason[:25]}")
                    return f"needs_human:{nh_reason}:{nh_url}", duration_ms, screening_qs

        if "RESULT:FAILED" in output:
            for out_line in output.split("\n"):
                if "RESULT:FAILED" in out_line:
                    reason = (
                        out_line.split("RESULT:FAILED:")[-1].strip()
                        if ":" in out_line[out_line.index("FAILED") + 6:]
                        else "unknown"
                    )
                    reason = _clean_reason(reason)
                    PROMOTE_TO_STATUS = {"captcha", "expired", "login_issue"}
                    if reason in PROMOTE_TO_STATUS:
                        add_event(f"[W{worker_id}] {reason.upper()} ({elapsed}s): {(job.get('title') or '')[:30]}")
                        update_state(worker_id, status=reason,
                                     last_action=f"{reason.upper()} ({elapsed}s)")
                        return reason, duration_ms, screening_qs
                    add_event(f"[W{worker_id}] FAILED ({elapsed}s): {reason[:30]}")
                    update_state(worker_id, status="failed",
                                 last_action=f"FAILED: {reason[:25]}")
                    return f"failed:{reason}", duration_ms, screening_qs
            return "failed:unknown", duration_ms, screening_qs

        # No explicit RESULT line. Try to infer the outcome from agent output.
        inferred = _infer_result_from_output(output)
        if inferred in ("applied", "already_applied"):
            label = "ALREADY APPLIED" if inferred == "already_applied" else "APPLIED"
            add_event(f"[W{worker_id}] INFERRED {label} ({elapsed}s): {(job.get('title') or '')[:30]}")
            update_state(worker_id, status="applied",
                         last_action=f"{label} (inferred, {elapsed}s)")
            return "applied", duration_ms, screening_qs
        if inferred:
            add_event(f"[W{worker_id}] INFERRED {inferred.upper()} ({elapsed}s): {(job.get('title') or '')[:30]}")
            update_state(worker_id, status="failed",
                         last_action=f"inferred:{inferred[:25]}")
            return f"failed:{inferred}", duration_ms, screening_qs

        add_event(f"[W{worker_id}] NO RESULT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"no result ({elapsed}s)")
        return "failed:no_result_line", duration_ms, screening_qs

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        elapsed = int(time.time() - start)
        add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"TIMEOUT ({elapsed}s)")
        return "failed:timeout", duration_ms, []
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        add_event(f"[W{worker_id}] ERROR: {str(e)[:40]}")
        update_state(worker_id, status="failed", last_action=f"ERROR: {str(e)[:25]}")
        return f"failed:{str(e)[:100]}", duration_ms, []
    finally:
        with _claude_lock:
            _claude_procs.pop(worker_id, None)
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc.pid)


# ---------------------------------------------------------------------------
# Failed attempts log — human-readable log with next steps
# ---------------------------------------------------------------------------

_NEXT_STEPS: dict[str, str] = {
    "login_required": "Log in manually in the Chrome worker window, then re-run. The session will persist.",
    "login_issue": "Login failed permanently. Check if the site requires SSO or a different account. May need to apply manually.",
    "sso_required": "Site requires Google/Microsoft/SSO login. Apply manually via browser.",
    "captcha": "Blocked by unsolvable CAPTCHA. Try again later or apply manually.",
    "expired": "Job listing is closed/expired. No action needed — remove from queue.",
    "not_eligible_location": "Job is onsite-only outside your area. No action needed.",
    "not_eligible_salary": "Salary below floor. No action needed.",
    "not_a_job_application": "Site is a profile builder / talent marketplace, not a job application. No action needed.",
    "unsafe_permissions": "Site requested camera/mic/screen permissions. Apply manually if interested.",
    "unsafe_verification": "Site requires video/biometric verification. Apply manually if interested.",
    "already_applied": "Already applied to this job. No action needed.",
    "stuck": "Agent got stuck on the page after 3 attempts. Check the worker log for details, then try manually.",
    "page_error": "Page was broken (500 error, blank page). Try again later.",
    "timeout": "Agent timed out. Job may be complex (multi-page form). Try with longer timeout or apply manually.",
    "no_result_line": "Agent finished but didn't output a result code. Check worker log for what happened.",
}

_FAILED_LOG = config.LOG_DIR / "failed_actions.log"
_MANUAL_LOG = config.APP_DIR / "manual_actions.md"


def _record_job_history(worker_id: int, job: dict, result: str,
                        duration_ms: int) -> None:
    """Append a completed job entry to the worker's in-memory history list.

    Shown on the per-worker homepage (http://localhost:{7380+worker_id}/).
    Keeps the last 50 entries; oldest are dropped automatically.
    """
    with _worker_state_lock:
        ws = _worker_state.get(worker_id)
    if ws is None:
        return
    history: list = ws.setdefault("history", [])
    # Classify result into a display category
    if "applied" in result.lower():
        outcome = "applied"
    elif "expired" in result.lower():
        outcome = "expired"
    elif "already_applied" in result.lower():
        outcome = "already_applied"
    elif "needs_human" in result.lower():
        outcome = "needs_human"
    else:
        outcome = "failed"
    history.append({
        "ts": time.time(),
        "title": job.get("title", ""),
        "company": job.get("company") or job.get("site", ""),
        "url": job.get("application_url") or job.get("url", ""),
        "score": job.get("fit_score", 0),
        "result": result,
        "outcome": outcome,
        "duration_s": round(duration_ms / 1000) if duration_ms else 0,
    })
    if len(history) > 50:
        del history[:-50]


def _log_failed_attempt(job: dict, reason: str, worker_id: int,
                        duration_ms: int, permanent: bool) -> None:
    """Append a structured entry to the failed actions log.

    Each entry includes the job, failure reason, whether it's retryable,
    and a human-readable next step so the user knows what to do.
    """
    config.ensure_dirs()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    url = job.get("application_url") or job["url"]
    title = job.get("title", "Unknown")
    company = job.get("site", "Unknown")
    score = job.get("fit_score", "?")
    duration_s = duration_ms / 1000 if duration_ms else 0

    # Look up the next step for this failure reason
    next_step = _NEXT_STEPS.get(reason, "Check the worker log for details. May need to apply manually.")

    retryable = "NO (permanent)" if permanent else "YES (will retry automatically)"

    entry = (
        f"\n{'─' * 70}\n"
        f"[{ts}]  {title} @ {company}  (score: {score}/10)\n"
        f"URL:      {url}\n"
        f"Reason:   {reason}\n"
        f"Duration: {duration_s:.0f}s  |  Worker: {worker_id}  |  Retryable: {retryable}\n"
        f"Action:   {next_step}\n"
    )

    try:
        with open(_FAILED_LOG, "a", encoding="utf-8") as f:
            f.write(entry)
    except OSError:
        logger.debug("Could not write to failed actions log", exc_info=True)


def _log_manual_action(job: dict, reason: str, instructions: str) -> None:
    """Append a human-action-required entry to ~/.applypilot/manual_actions.md."""
    config.ensure_dirs()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    url = job.get("application_url") or job["url"]
    title = job.get("title", "Unknown")
    company = job.get("site", "Unknown")
    score = job.get("fit_score", "?")

    entry = (
        f"\n## {title} @ {company}\n"
        f"- **When**: {ts}\n"
        f"- **Score**: {score}/10\n"
        f"- **URL**: {url}\n"
        f"- **Reason**: {reason}\n"
        f"- **Action needed**: {instructions}\n"
        f"- **Retry**: `applypilot apply --url '{url}'`\n"
    )

    try:
        with open(_MANUAL_LOG, "a", encoding="utf-8") as f:
            if f.tell() == 0:
                f.write("# Manual Actions Required\n\nJobs that need human intervention before retrying.\n")
            f.write(entry)
    except OSError:
        logger.debug("Could not write to manual actions log", exc_info=True)


# ---------------------------------------------------------------------------
# Permanent failure classification
# ---------------------------------------------------------------------------

PERMANENT_FAILURES: set[str] = {
    "expired", "form_interaction_error", "browser_unavailable",
    "not_eligible_location", "not_eligible_salary",
    "already_applied", "not_a_job_application", "unsafe_permissions",
    "unsafe_verification", "contract_only",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
    "credits_exhausted", "file_upload_blocked",
    "account_creation_broken", "page_error",
    "application_limit_exceeded",
}

# Errors that should pause and wait for human intervention via HITL banner
# instead of being marked as permanent failures.
HITL_AUTO_ROUTE: frozenset[str] = frozenset({
    "captcha",
    "login_issue",
    "account_required",
    "sso_required",
    "email_verification",
    "resume_upload_blocked",
    "stuck",
})

# login_required is retryable — user logs in manually, then retry succeeds
RETRYABLE_AUTH_FAILURES: set[str] = {"login_required"}

# Errors that are "permanent" normally but transient after a HITL pause
# (e.g. backend was down while user was completing the form; retry after 30s)
_HITL_TRANSIENT_ERRORS: frozenset[str] = frozenset({"page_error", "stuck", "browser_unavailable"})

PERMANENT_PREFIXES: tuple[str, ...] = ("site_blocked", "cloudflare", "blocked_by")


def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    # login_required is explicitly retryable
    if result in RETRYABLE_AUTH_FAILURES or reason in RETRYABLE_AUTH_FAILURES:
        return False
    return (
        result in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
    )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def _probe_for_reconnect(worker_id: int, port: int) -> tuple[int | None, str | None]:
    """Check if a previous Chrome session can be reconnected to.

    On startup, if a previous `applypilot apply` run was killed while Chrome
    was still running, this detects the live browser and finds the interrupted
    in-progress job so the new run can resume instead of starting fresh.

    Returns:
        (chrome_pid, interrupted_job_url) if a reconnectable Chrome is found,
        or (None, None) if Chrome is not running / profile doesn't match.
    """
    profile_dir = config.CHROME_WORKER_DIR / f"worker-{worker_id}"
    pid = probe_existing_chrome(port, profile_dir)
    if pid is None:
        return None, None

    logger.info(
        "[W%d] Existing Chrome on port %d (pid %d) — checking for interrupted job",
        worker_id, port, pid,
    )
    add_event(f"[W{worker_id}] Reconnecting to existing Chrome (pid {pid})")

    # Find the job this worker was applying to when the pipeline was killed.
    # acquire_job() sets agent_id = "worker-{N}" when locking a job.
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT url, application_url, title FROM jobs "
            "WHERE apply_status = 'in_progress' AND agent_id = ? "
            "ORDER BY last_attempted_at DESC LIMIT 1",
            (f"worker-{worker_id}",),
        ).fetchone()
        if row:
            job_url = row["url"]
            apply_url = row["application_url"] or row["url"]
            title = row["title"] or apply_url
            # Reset the lock so acquire_job() can re-acquire it normally
            conn.execute(
                "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ?",
                (job_url,),
            )
            conn.commit()
            add_event(
                f"[W{worker_id}] Found interrupted job: {title[:40]} — will resume"
            )
            logger.info("[W%d] Interrupted job reset for reconnect: %s", worker_id, apply_url[:80])
            return pid, apply_url
        else:
            add_event(f"[W{worker_id}] No interrupted job found — Chrome has next job")
            return pid, None
    except Exception as exc:
        logger.warning("[W%d] Reconnect probe: could not look up interrupted job: %s", worker_id, exc)
        return pid, None


def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int | None = None,
                max_score: int | None = None,
                max_age_days: int | None = None,
                headless: bool = False,
                model: str = "sonnet", dry_run: bool = False,
                fresh_sessions: bool = False,
                total_workers: int = 1,
                no_hitl: bool = False) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        max_score: Maximum fit_score threshold (optional).
        max_age_days: Maximum job age in days (optional).
        headless: Run Chrome headless.
        model: Claude model name.
        dry_run: Don't click Submit.
        fresh_sessions: Refresh Chrome session cookies before launching.
        total_workers: Total concurrent workers (used for window tiling).

    Returns:
        Tuple of (applied_count, failed_count).
    """
    from applypilot import config as _cfg
    if min_score is None:
        min_score = _cfg.DEFAULTS["min_score"]
    if max_age_days is None:
        max_age_days = _cfg.DEFAULTS["max_job_age_days"]

    applied = 0
    failed = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id

    # Start always-on worker HTTP listener (used by Chrome extension + HITL banner)
    _start_worker_listener(worker_id)
    try:
        return _worker_loop_body(
            worker_id, limit, target_url, min_score, max_score, max_age_days,
            headless, model, dry_run, fresh_sessions, applied, failed, continuous,
            jobs_done, empty_polls, port, total_workers, no_hitl=no_hitl,
        )
    finally:
        _stop_worker_listener(worker_id)


def _worker_loop_body(
    worker_id: int, limit: int, target_url: str | None,
    min_score: int, max_score: int | None, max_age_days: int | None,
    headless: bool,
    model: str, dry_run: bool, fresh_sessions: bool,
    applied: int, failed: int, continuous: bool,
    jobs_done: int, empty_polls: int, port: int,
    total_workers: int = 1, no_hitl: bool = False,
) -> tuple[int, int]:
    """Main per-worker processing loop."""
    # ── Reconnect probe ───────────────────────────────────────────────────────
    # If a previous run was killed while Chrome was running, adopt the existing
    # browser and resume the interrupted job rather than starting fresh.
    _reconnect_pid, _reconnect_url = _probe_for_reconnect(worker_id, port)
    # ─────────────────────────────────────────────────────────────────────────

    while not _stop_event.is_set():
        if not continuous and jobs_done >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        # On reconnect, prioritize the interrupted job URL for this iteration only
        _effective_target = _reconnect_url or target_url
        _reconnect_url = None  # clear after first use

        job = acquire_job(target_url=_effective_target, min_score=min_score,
                          max_score=max_score, max_age_days=max_age_days,
                          worker_id=worker_id)
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle",
                         last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            # Use Event.wait for interruptible sleep
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break  # Stop was requested during wait
            continue

        empty_polls = 0

        # Consume reconnect state for this job iteration (cleared after first use)
        _this_reconnect_pid = _reconnect_pid
        _this_had_interrupted_job = _effective_target is not None and _effective_target != target_url
        _reconnect_pid = None

        chrome_proc = None
        was_skipped = False
        try:
            # Detect ATS for persistent session overlay
            apply_url = job.get("application_url") or job.get("url", "")
            ats_slug = detect_ats(apply_url)
            if ats_slug:
                add_event(f"[W{worker_id}] ATS: {ats_slug}")

            if _this_reconnect_pid is not None:
                # Reuse the existing Chrome — skip launch entirely
                add_event(f"[W{worker_id}] Reconnecting to Chrome (pid {_this_reconnect_pid})...")
                chrome_proc = _AdoptedChromeProcess(_this_reconnect_pid)
                with _chrome_lock:
                    _chrome_procs[worker_id] = chrome_proc
            else:
                add_event(f"[W{worker_id}] Launching Chrome...")
                chrome_proc = launch_chrome(worker_id, port=port, headless=headless,
                                            refresh_cookies=fresh_sessions,
                                            ats_slug=ats_slug,
                                            total_workers=total_workers)

            with _worker_state_lock:
                ws = _worker_state.get(worker_id)
            if ws is not None:
                ws["chrome_pid"] = chrome_proc.pid

            # Inject status badge into every page (replaces Chrome extension —
            # Extension popup (loaded via --load-extension) handles status display.
            # The inject_status_badge overlay was removed — it showed "offline"
            # on startup before the first poll and provided no value over the popup.

            # Update always-on worker state so the extension popup knows the current job
            with _worker_state_lock:
                ws = _worker_state.get(worker_id)
            if ws is not None:
                ws.update({"job": job, "status": "applying", "reason": None,
                           "instructions": None, "saved_instruction": None})

            # On reconnect with interrupted job: don't reset tabs (form is mid-fill)
            _reconnect_ctx = None
            if _this_had_interrupted_job:
                _reconnect_ctx = (
                    "PIPELINE RESTART: The apply pipeline was killed while you were "
                    "working on this application. The Chrome browser was left running "
                    "with the form potentially partially filled. "
                    "Take a browser_snapshot immediately to see the current page state, "
                    "then continue filling and submitting the application. "
                    "Do NOT navigate away from the current page unless it is completely blank."
                )

            result, duration_ms, screening_qs = run_job(
                job, port=port, worker_id=worker_id,
                model=model, dry_run=dry_run,
                skip_tab_reset=_this_had_interrupted_job,
                extra_context=_reconnect_ctx,
            )

            # --- Relaunch sub-loop: handles Q&A, HITL, and takeover without closing Chrome ---
            relaunch = True
            while relaunch:
                relaunch = False

                if result == "skipped":
                    release_lock(job["url"])
                    add_event(f"[W{worker_id}] Skipped: {(job.get('title') or '')[:30]}")
                    was_skipped = True
                    break
                elif "credits_exhausted" in result:
                    reason = result.split(":", 1)[-1] if ":" in result else result
                    mark_result(job["url"], "failed", reason, permanent=True,
                                duration_ms=duration_ms)
                    _log_failed_attempt(job, reason, worker_id, duration_ms, True)
                    failed += 1
                    _stop_event.set()
                    break
                elif result == "applied":
                    mark_result(job["url"], "applied", duration_ms=duration_ms)
                    _record_job_history(worker_id, job, result, duration_ms)
                    applied += 1
                    update_state(worker_id, jobs_applied=applied,
                                 jobs_done=applied + failed)
                    if ats_slug:
                        profile_dir = config.CHROME_WORKER_DIR / f"worker-{worker_id}"
                        save_ats_session(profile_dir, ats_slug)
                    break

                elif result == "takeover":
                    # User clicked "Take Over" in the extension popup.
                    # The Claude proc was already killed by the takeover handler.
                    # Wait for the user to click "Give Back Control" (handback event).
                    add_event(f"[W{worker_id}] PAUSED by user: {(job.get('title') or '')[:30]}")
                    update_state(worker_id, status="paused_by_user",
                                 last_action="paused by user")
                    _register_waiting(worker_id, "waiting_human")

                    hb_event = _handback_events.get(worker_id)
                    if hb_event:
                        hb_event.clear()  # Clear any stale signal from previous job

                    while not _stop_event.is_set():
                        if hb_event and hb_event.wait(timeout=5.0):
                            break
                    _unregister_waiting(worker_id)
                    if _stop_event.is_set():
                        break

                    # Collect handback instructions from always-on server state
                    extra_ctx = None
                    with _worker_state_lock:
                        ws = _worker_state.get(worker_id)
                    if ws is not None:
                        extra_ctx = ws.get("handback_instructions")
                        ws["handback_instructions"] = None
                        ws["status"] = "applying"

                    # Clear takeover event so next run_job() doesn't exit immediately
                    tev = _takeover_events.get(worker_id)
                    if tev:
                        tev.clear()

                    add_event(f"[W{worker_id}] Resuming after user takeover...")
                    update_state(worker_id, status="applying",
                                 last_action="resuming after takeover",
                                 start_time=time.time(), actions=0)
                    result, duration_ms, screening_qs = run_job(
                        job, port=port, worker_id=worker_id,
                        model=model, dry_run=dry_run,
                        skip_tab_reset=True, extra_context=extra_ctx,
                    )
                    relaunch = True
                    continue

                elif result.startswith("needs_human:"):
                    # Parse reason and URL (optional |detail:... suffix from agent)
                    after = result[len("needs_human:"):]
                    if ":" in after:
                        nh_reason, nh_url = after.split(":", 1)
                    else:
                        nh_reason, nh_url = after, job.get("application_url") or job["url"]
                    # Extract detail suffix: "https://url|detail:reason text"
                    nh_detail = ""
                    if "|detail:" in nh_url:
                        nh_url, nh_detail = nh_url.split("|detail:", 1)
                        nh_url = nh_url.strip()
                        nh_detail = nh_detail.strip()

                    # --- Screening Q&A: interactive TUI answers + relaunch ---
                    if nh_reason == "screening_questions" and screening_qs:
                        add_event(f"[W{worker_id}] Q&A: {len(screening_qs)} question(s) — waiting for answers")
                        update_state(worker_id, status="waiting_answer",
                                     last_action=f"Q&A: {len(screening_qs)} question(s)")
                        _register_waiting(worker_id, "waiting_answer")

                        # Post to the Q&A queue — main thread will prompt user
                        answer_event = threading.Event()
                        _qa_queue.put((worker_id, screening_qs, answer_event))

                        # Block until main thread provides answers (interruptible)
                        while not _stop_event.is_set():
                            if answer_event.wait(timeout=5.0):
                                break
                        _unregister_waiting(worker_id)
                        if _stop_event.is_set():
                            break

                        # Relaunch agent on same Chrome (form still open)
                        add_event(f"[W{worker_id}] Relaunching with Q&A answers...")
                        update_state(worker_id, status="applying",
                                     last_action="relaunching with answers",
                                     start_time=time.time(), actions=0)
                        result, duration_ms, screening_qs = run_job(
                            job, port=port, worker_id=worker_id,
                            model=model, dry_run=dry_run, skip_tab_reset=True)
                        relaunch = True
                        continue

                    # --- General HITL: keep Chrome open, inject banner, wait ---
                    nh_instructions = _HITL_INSTRUCTIONS.get(
                        nh_reason, f"Human action required: {nh_reason}"
                    )
                    if nh_detail:
                        nh_instructions = f"{nh_instructions}\n\nAgent detail: {nh_detail}"
                    # Mark in DB so stale-lock cleanup won't steal this job
                    mark_needs_human(
                        job["url"], nh_reason, nh_url, nh_instructions, duration_ms
                    )

                    if no_hitl:
                        add_event(f"[W{worker_id}] --no-hitl: parking '{nh_reason}' and moving on")
                        update_state(worker_id, last_action=f"parked: {nh_reason[:25]}")
                        break

                    job_hash = hashlib.sha256(job["url"].encode()).hexdigest()[:12]
                    hitl_event = threading.Event()
                    hitl_port = _start_hitl_listener(worker_id, hitl_event, job_hash)

                    _inject_banner_for_worker(worker_id, port, job, nh_reason, hitl_port,
                                             navigate_url=nh_url,
                                             instructions=nh_instructions)
                    # Start background watcher: polls window.__ap_hitl_done via CDP
                    # and calls /api/done/{hash} from Node (bypasses page CSP).
                    from applypilot.apply.human_review import _start_done_watcher
                    _watcher = _start_done_watcher(port, hitl_port, job_hash)
                    notify_human_needed(job, nh_reason, nh_url)
                    add_event(f"[W{worker_id}] WAITING for human: {nh_reason[:20]}")
                    update_state(worker_id, status="waiting_human",
                                 last_action=f"WAITING: {nh_reason[:25]}")
                    # Update extension state so popup shows correct info
                    with _worker_state_lock:
                        ws = _worker_state.get(worker_id)
                    if ws is not None:
                        _saved = None
                        try:
                            from applypilot.database import get_qa
                            from applypilot.database import close_connection
                            _saved = get_qa(f"HITL:{job.get('site', '')}:{nh_reason}")
                            close_connection()
                        except Exception:
                            pass
                        ws.update({"status": "waiting_human", "reason": nh_reason,
                                   "instructions": nh_instructions,
                                   "saved_instruction": _saved,
                                   "hitl_watcher_proc": _watcher})
                    _register_waiting(worker_id, "waiting_human")

                    # Block until user clicks Done in Chrome banner (interruptible).
                    # Re-launch Chrome if it crashes while we're waiting.
                    while not _stop_event.is_set():
                        if hitl_event.wait(timeout=5.0):
                            break
                        if chrome_proc and chrome_proc.poll() is not None:
                            add_event(f"[W{worker_id}] Chrome crashed during HITL; relaunching...")
                            try:
                                chrome_proc = launch_chrome(worker_id, port=port,
                                                            headless=headless, ats_slug=ats_slug,
                                                            total_workers=total_workers)
                                _inject_banner_for_worker(worker_id, port, job, nh_reason,
                                                          hitl_port, navigate_url=nh_url,
                                                          instructions=nh_instructions)
                            except Exception:
                                logger.debug("Chrome relaunch during HITL failed", exc_info=True)
                    _stop_hitl_listener(worker_id)
                    _unregister_waiting(worker_id)
                    if _stop_event.is_set():
                        break

                    # Reset status so agent can re-acquire and apply
                    reset_needs_human(job["url"])

                    # Relaunch agent on same Chrome; retry up to 3× on transient errors
                    # (e.g. backend was down while user was completing the form)
                    for _hitl_attempt in range(3):
                        add_event(f"[W{worker_id}] Human done, relaunching agent"
                                  f" (attempt {_hitl_attempt + 1}/3)...")
                        update_state(worker_id, status="applying",
                                     last_action=f"relaunching after HITL (attempt {_hitl_attempt + 1})",
                                     start_time=time.time(), actions=0)
                        result, duration_ms, screening_qs = run_job(
                            job, port=port, worker_id=worker_id,
                            model=model, dry_run=dry_run, skip_tab_reset=True)
                        _hitl_reason = result.split(":", 1)[-1] if ":" in result else result
                        if _hitl_reason not in _HITL_TRANSIENT_ERRORS or _stop_event.is_set():
                            break
                        if _hitl_attempt < 2:
                            add_event(f"[W{worker_id}] Transient ({_hitl_reason}), retrying in 30s...")
                            time.sleep(30)
                    relaunch = True
                    continue

                else:
                    reason = result.split(":", 1)[-1] if ":" in result else result
                    # login_required: route to HITL with banner + wait
                    if reason == "login_required":
                        if ats_slug:
                            clear_ats_session(ats_slug)
                        nh_url = job.get("application_url") or job["url"]
                        nh_instructions = _HITL_INSTRUCTIONS["login_required"]
                        # Mark in DB so stale-lock cleanup won't steal this job
                        mark_needs_human(
                            job["url"], "login_required", nh_url,
                            nh_instructions, duration_ms
                        )

                        if no_hitl:
                            add_event(f"[W{worker_id}] --no-hitl: parking 'login_required' and moving on")
                            update_state(worker_id, last_action="parked: login_required")
                            break

                        job_hash = hashlib.sha256(job["url"].encode()).hexdigest()[:12]
                        hitl_event = threading.Event()
                        hitl_port = _start_hitl_listener(worker_id, hitl_event, job_hash)

                        _inject_banner_for_worker(worker_id, port, job, "login_required", hitl_port,
                                                 navigate_url=nh_url)
                        from applypilot.apply.human_review import _start_done_watcher
                        _watcher = _start_done_watcher(port, hitl_port, job_hash)
                        notify_human_needed(job, "login_required", nh_url)
                        add_event(f"[W{worker_id}] WAITING for human: login_required")
                        update_state(worker_id, status="waiting_human",
                                     last_action="WAITING: login_required")
                        with _worker_state_lock:
                            ws = _worker_state.get(worker_id)
                        if ws is not None:
                            _saved = None
                            try:
                                from applypilot.database import get_qa, close_connection
                                _saved = get_qa(f"HITL:{job.get('site', '')}:login_required")
                                close_connection()
                            except Exception:
                                pass
                            ws.update({"status": "waiting_human", "reason": "login_required",
                                       "instructions": nh_instructions,
                                       "saved_instruction": _saved,
                                       "hitl_watcher_proc": _watcher})
                        _register_waiting(worker_id, "waiting_human")

                        while not _stop_event.is_set():
                            if hitl_event.wait(timeout=5.0):
                                break
                            if chrome_proc and chrome_proc.poll() is not None:
                                add_event(f"[W{worker_id}] Chrome crashed during login HITL; relaunching...")
                                try:
                                    chrome_proc = launch_chrome(worker_id, port=port,
                                                                headless=headless, ats_slug=ats_slug)
                                    _inject_banner_for_worker(worker_id, port, job, "login_required",
                                                              hitl_port, navigate_url=nh_url)
                                except Exception:
                                    logger.debug("Chrome relaunch during HITL failed", exc_info=True)
                        _stop_hitl_listener(worker_id)
                        _unregister_waiting(worker_id)
                        if _stop_event.is_set():
                            break

                        reset_needs_human(job["url"])
                        # Relaunch agent; retry up to 3× on transient errors after login
                        for _login_attempt in range(3):
                            add_event(f"[W{worker_id}] Human done, relaunching agent"
                                      f" (attempt {_login_attempt + 1}/3)...")
                            update_state(worker_id, status="applying",
                                         last_action=f"relaunching after login (attempt {_login_attempt + 1})",
                                         start_time=time.time(), actions=0)
                            result, duration_ms, screening_qs = run_job(
                                job, port=port, worker_id=worker_id,
                                model=model, dry_run=dry_run, skip_tab_reset=True)
                            _login_reason = result.split(":", 1)[-1] if ":" in result else result
                            if _login_reason not in _HITL_TRANSIENT_ERRORS or _stop_event.is_set():
                                break
                            if _login_attempt < 2:
                                add_event(f"[W{worker_id}] Transient ({_login_reason}), retrying in 30s...")
                                time.sleep(30)
                        relaunch = True
                        continue

                    elif reason in HITL_AUTO_ROUTE:
                        # Route to HITL instead of marking as permanent failure.
                        # User intervenes via the Chrome banner, then agent relaunches.
                        nh_url = job.get("application_url") or job["url"]
                        nh_instructions = _HITL_INSTRUCTIONS.get(
                            reason, f"Manual action required: {reason}"
                        )
                        mark_needs_human(
                            job["url"], reason, nh_url, nh_instructions, duration_ms
                        )

                        if no_hitl:
                            add_event(f"[W{worker_id}] --no-hitl: parking '{reason}' and moving on")
                            update_state(worker_id, last_action=f"parked: {reason[:25]}")
                            break

                        job_hash = hashlib.sha256(job["url"].encode()).hexdigest()[:12]
                        hitl_event = threading.Event()
                        hitl_port = _start_hitl_listener(worker_id, hitl_event, job_hash)

                        _inject_banner_for_worker(worker_id, port, job, reason, hitl_port,
                                                 navigate_url=nh_url)
                        from applypilot.apply.human_review import _start_done_watcher
                        _watcher = _start_done_watcher(port, hitl_port, job_hash)
                        notify_human_needed(job, reason, nh_url)
                        add_event(f"[W{worker_id}] WAITING for human: {reason}")
                        update_state(worker_id, status="waiting_human",
                                     last_action=f"WAITING: {reason[:25]}")
                        with _worker_state_lock:
                            ws = _worker_state.get(worker_id)
                        if ws is not None:
                            _saved = None
                            try:
                                from applypilot.database import get_qa, close_connection
                                _saved = get_qa(f"HITL:{job.get('site', '')}:{reason}")
                                close_connection()
                            except Exception:
                                pass
                            ws.update({"status": "waiting_human", "reason": reason,
                                       "instructions": nh_instructions,
                                       "saved_instruction": _saved,
                                       "hitl_watcher_proc": _watcher})
                        _register_waiting(worker_id, "waiting_human")

                        while not _stop_event.is_set():
                            if hitl_event.wait(timeout=5.0):
                                break
                            if chrome_proc and chrome_proc.poll() is not None:
                                add_event(f"[W{worker_id}] Chrome crashed during HITL; relaunching...")
                                try:
                                    chrome_proc = launch_chrome(worker_id, port=port,
                                                                headless=headless, ats_slug=ats_slug)
                                    _inject_banner_for_worker(worker_id, port, job, reason,
                                                              hitl_port, navigate_url=nh_url)
                                except Exception:
                                    logger.debug("Chrome relaunch during HITL failed", exc_info=True)
                        _stop_hitl_listener(worker_id)
                        _unregister_waiting(worker_id)
                        if _stop_event.is_set():
                            break

                        reset_needs_human(job["url"])
                        for _auto_hitl_attempt in range(3):
                            add_event(f"[W{worker_id}] Human done, relaunching agent"
                                      f" (attempt {_auto_hitl_attempt + 1}/3)...")
                            update_state(worker_id, status="applying",
                                         last_action=f"relaunching after {reason} HITL"
                                                     f" (attempt {_auto_hitl_attempt + 1})",
                                         start_time=time.time(), actions=0)
                            result, duration_ms, screening_qs = run_job(
                                job, port=port, worker_id=worker_id,
                                model=model, dry_run=dry_run, skip_tab_reset=True)
                            _auto_reason = result.split(":", 1)[-1] if ":" in result else result
                            if _auto_reason not in _HITL_TRANSIENT_ERRORS or _stop_event.is_set():
                                break
                            if _auto_hitl_attempt < 2:
                                add_event(f"[W{worker_id}] Transient ({_auto_reason}), retrying in 30s...")
                                time.sleep(30)
                        relaunch = True
                        continue

                    else:
                        perm = _is_permanent_failure(result)
                        mark_result(job["url"], "failed", reason,
                                    permanent=perm, duration_ms=duration_ms)
                        _log_failed_attempt(job, reason, worker_id, duration_ms, perm)
                        _record_job_history(worker_id, job, result, duration_ms)
                        failed += 1
                        update_state(worker_id, jobs_failed=failed,
                                     jobs_done=applied + failed)

        except KeyboardInterrupt:
            release_lock(job["url"])
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except Exception as e:
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {type(e).__name__}: {str(e)[:35]}")
            _log_failed_attempt(job, f"launcher_error:{str(e)[:80]}", worker_id, 0, False)
            release_lock(job["url"])
            failed += 1
            update_state(worker_id, jobs_failed=failed)
        finally:
            if chrome_proc:
                cleanup_worker(worker_id, chrome_proc)

        if was_skipped:
            continue
        jobs_done += 1
        if target_url:
            break

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


# ---------------------------------------------------------------------------
# Q&A interactive prompt (called from main thread)
# ---------------------------------------------------------------------------

def _prompt_user_for_qa(console: Console, worker_id: int,
                        questions: list[dict]) -> list[str]:
    """Prompt the user in the terminal for screening question answers.

    Args:
        console: Rich Console for pretty printing.
        worker_id: Which worker needs answers.
        questions: List of question dicts with keys: question, field_type, options.

    Returns:
        List of answer strings (one per question).
    """
    console.print(f"\n[bold cyan]Worker {worker_id} needs your help with screening questions:[/bold cyan]")
    answers: list[str] = []
    for i, q in enumerate(questions, 1):
        console.print(f"\n  [bold]Q{i}:[/bold] {q['question']}")
        if q.get("field_type"):
            console.print(f"  [dim]Type: {q['field_type']}[/dim]")
        if q.get("options"):
            opts = q["options"].split(",") if isinstance(q["options"], str) else q["options"]
            opts = [o.strip() for o in opts if o.strip()]
            if opts:
                for j, opt in enumerate(opts, 1):
                    console.print(f"    {j}. {opt}")
        try:
            answer = console.input("  [bold]Your answer: [/bold]")
            # If user typed a number and there are options, map to the option text
            if q.get("options") and answer.strip().isdigit():
                opts = [o.strip() for o in q["options"].split(",") if o.strip()]
                idx = int(answer.strip()) - 1
                if 0 <= idx < len(opts):
                    answer = opts[idx]
            answers.append(answer.strip())
        except (EOFError, KeyboardInterrupt):
            answers.append("")
    console.print(f"[green]Answers recorded. Relaunching agent for Worker {worker_id}...[/green]\n")
    return answers


# ---------------------------------------------------------------------------
# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------

def main(limit: int = 1, target_url: str | None = None,
         min_score: int | None = None, max_score: int | None = None,
         max_age_days: int | None = None,
         headless: bool = False, model: str = "sonnet",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1,
         fresh_sessions: bool = False, no_hitl: bool = False,
         no_focus: bool = False) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to (0 or with continuous=True means run forever).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        max_score: Maximum fit_score threshold (optional, for testing on lower-score jobs).
        max_age_days: Maximum job age in days (optional).
        headless: Run Chrome in headless mode.
        model: Claude model name.
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
        fresh_sessions: Refresh Chrome session cookies from user's real profile.
        no_hitl: Skip human-in-the-loop waits; park jobs as needs_human and move on.
        no_focus: Prevent Chrome windows from stealing keyboard focus (Linux/GNOME only).
    """
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()

    config.ensure_dirs()
    console = Console()
    _prev_focus_mode: str | None = None  # set before workers start; restored in finally

    # Re-queue any jobs stuck in needs_human from a previous session.
    # Their Chrome windows are gone (killed by _kill_on_port() when workers start),
    # so reset them to NULL so they get picked up as normal jobs.
    _boot_conn = get_connection()
    _nh_count = _boot_conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_status='needs_human'"
    ).fetchone()[0]
    if _nh_count > 0:
        _boot_conn.execute(
            "UPDATE jobs SET apply_status=NULL, apply_category=NULL, "
            "needs_human_reason=NULL, needs_human_url=NULL, "
            "needs_human_instructions=NULL WHERE apply_status='needs_human'"
        )
        _boot_conn.commit()
        console.print(f"[yellow]Re-queued {_nh_count} needs_human job(s) from previous session[/yellow]")
        logger.info("Startup: re-queued %d needs_human jobs from previous session", _nh_count)

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    # Initialize dashboard for all workers
    for i in range(workers):
        init_worker(i)

    start_health_checks()

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(f"Launching apply pipeline ({mode_label}, {worker_label}, poll every {POLL_INTERVAL}s)...")
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Skipping current job(s)... (Ctrl+C again to STOP)[/yellow]")
            # Kill all active Claude processes to skip current jobs
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
            kill_all_chrome()
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            # Daemon thread for display refresh only (no business logic)
            _dashboard_running = True

            def _refresh():
                while _dashboard_running:
                    try:
                        live.update(render_full())
                    except Exception:
                        pass
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            # Always use executor — main thread handles Q&A input
            if effective_limit:
                base = effective_limit // workers
                extra = effective_limit % workers
                limits = [base + (1 if i < extra else 0)
                          for i in range(workers)]
            else:
                limits = [0] * workers  # continuous mode

            # Prevent Chrome windows from stealing keyboard focus while workers run.
            # Restores the previous GNOME focus-new-windows setting when done.
            from applypilot.apply.chrome import prevent_focus_stealing, restore_focus_mode
            _prev_focus_mode = prevent_focus_stealing() if (no_focus and not headless) else None

            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="apply-worker") as executor:
                futures = {
                    executor.submit(
                        worker_loop,
                        worker_id=i,
                        limit=limits[i],
                        target_url=target_url,
                        min_score=min_score,
                        max_score=max_score,
                        max_age_days=max_age_days,
                        headless=headless,
                        model=model,
                        dry_run=dry_run,
                        fresh_sessions=fresh_sessions,
                        total_workers=workers,
                        no_hitl=no_hitl,
                    ): i
                    for i in range(workers)
                }

                # --- Main thread event loop: Q&A input + all-blocked detection ---
                _all_blocked_prompted = False
                while not all(f.done() for f in futures):
                    # Check Q&A queue for screening questions from workers
                    try:
                        wid, questions, answer_event = _qa_queue.get(timeout=0.5)
                        _dashboard_running = False  # pause refresh thread
                        time.sleep(0.6)  # let refresh thread finish current cycle
                        live.stop()

                        answers = _prompt_user_for_qa(console, wid, questions)
                        # Store answers in Q&A knowledge base
                        from applypilot.database import store_qa
                        for q_dict, ans in zip(questions, answers):
                            if ans:
                                store_qa(q_dict["question"], ans, source="human",
                                         field_type=q_dict.get("field_type"))
                        answer_event.set()  # unblock the worker

                        live.start()
                        _dashboard_running = True
                    except queue.Empty:
                        pass

                    # Check if all workers are blocked
                    waiting = _get_waiting_count()
                    active_workers = sum(
                        1 for f in futures if not f.done()
                    )
                    if waiting > 0 and waiting >= active_workers and not _all_blocked_prompted:
                        _all_blocked_prompted = True
                        add_event(f"[bold magenta]All {waiting} active worker(s) waiting for human input[/bold magenta]")

                    if _all_blocked_prompted and _get_waiting_count() == 0:
                        _all_blocked_prompted = False

                results: list[tuple[int, int]] = []
                for future in futures:
                    wid = futures[future]
                    try:
                        results.append(future.result())
                    except Exception:
                        logger.exception("Worker %d crashed", wid)
                        results.append((0, 0))

            total_applied = sum(r[0] for r in results)
            total_failed = sum(r[1] for r in results)

            _dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

        totals = get_totals()
        console.print(
            f"\n[bold]Done: {total_applied} applied, {total_failed} failed "
            f"(${totals['cost']:.3f})[/bold]"
        )
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        stop_health_checks()
        kill_all_chrome()
        restore_focus_mode(_prev_focus_mode)
