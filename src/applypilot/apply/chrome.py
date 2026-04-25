"""Chrome lifecycle management for apply workers.

Handles launching an isolated Chrome instance with remote debugging,
worker profile setup/cloning, and cross-platform process cleanup.
"""

import glob as _glob
import json
import logging
import os
import platform
import random
import shutil
import subprocess
import threading
import time
import urllib.request as _ureq
from pathlib import Path

from applypilot import config

logger = logging.getLogger(__name__)

# --- Port table (audit #11 — centralize all port literals here) -------------
# CDP port base — each worker uses BASE_CDP_PORT + worker_id
BASE_CDP_PORT = 9222

# HITL (Human-in-the-Loop) Chrome uses a fixed port and worker ID
# that don't conflict with apply workers (9222–9230+)
HITL_CDP_PORT = 9300
HITL_WORKER_ID = 99

# In-pipeline HITL HTTP listener: each worker exposes /api/done/{hash},
# /api/status, /api/takeover, etc. on HITL_LISTEN_BASE_PORT + worker_id.
# Extension (popup.js, background.js, content.js) also reads from this base.
HITL_LISTEN_BASE_PORT = 7380

# Standalone `applypilot human-review` server (separate from in-pipeline HITL).
# Will be deleted in plan 5 once _start_done_watcher is migrated out of
# human_review.py.
HUMAN_REVIEW_PORT = 7373
# ---------------------------------------------------------------------------

# Track Chrome processes per worker for cleanup
_chrome_procs: dict[int, subprocess.Popen] = {}


class _AdoptedChromeProcess:
    """Stub that wraps an already-running Chrome process adopted for reconnect.

    Provides the same .pid and .poll() interface as subprocess.Popen so that
    the rest of worker_loop() (HITL relaunch, cleanup_worker, etc.) works
    identically whether Chrome was freshly launched or reconnected.
    """

    def __init__(self, pid: int | None) -> None:
        self.pid: int = pid or 0
        self._pid = pid
        self.stdin = None
        self.stdout = None
        self.stderr = None

    def poll(self) -> int | None:
        """Return None if the process is alive, non-None if it has exited."""
        if not self._pid:
            return None  # Unknown PID — assume alive
        try:
            os.kill(self._pid, 0)
            return None  # Process exists and is reachable
        except (ProcessLookupError, OSError):
            return 1  # Dead

    def wait(self, timeout: float | None = None) -> int:
        return 0  # No-op; we don't manage lifecycle here
_chrome_lock = threading.Lock()

# Persistent ATS session storage
SESSIONS_DIR = config.APP_DIR / "chrome-sessions"

# ATS domain → slug mapping for persistent session management
ATS_DOMAINS: dict[str, str] = {
    # --- high-volume (seen in DB) ---
    "myworkdayjobs.com": "workday",
    "greenhouse.io": "greenhouse",
    "grnh.se": "greenhouse",       # Greenhouse short-link redirector
    "lever.co": "lever",
    "ashbyhq.com": "ashby",        # 143 jobs in DB
    "rippling.com": "rippling",    # 38 jobs in DB (ats.rippling.com)
    "workable.com": "workable",    # 14 jobs in DB (apply.workable.com)
    "recruitee.com": "recruitee",  # 12 jobs in DB
    "adp.com": "adp",              # 6 jobs in DB (workforcenow.adp.com)
    "icims.com": "icims",
    "jobvite.com": "jobvite",
    "oraclecloud.com": "oracle",
    "smartrecruiters.com": "smartrecruiters",
    "ultipro.com": "ultipro",
    "ukg.com": "ultipro",          # UKG acquired Ultipro
    "taleo.net": "taleo",
    # --- common enterprise / mid-market ---
    "successfactors.com": "successfactors",
    "successfactors.eu": "successfactors",
    "csod.com": "cornerstone",
    "bamboohr.com": "bamboohr",
    "dayforcehcm.com": "dayforce",
    "jazzhr.com": "jazzhr",
    "breezy.hr": "breezy",
    "teamtailor.com": "teamtailor",
    "pinpointhq.com": "pinpoint",
    "comeet.com": "comeet",
    "personio.com": "personio",
    "personio.de": "personio",
    "newtonsoftware.com": "newton",
}


def detect_ats(url: str | None) -> str | None:
    """Detect the ATS platform from a job or application URL.

    Returns:
        ATS slug (e.g., 'workday') or None if no known ATS detected.
    """
    if not url:
        return None
    from urllib.parse import urlparse
    try:
        host = urlparse(url).hostname or ""
        host = host.lower()
        for domain, slug in ATS_DOMAINS.items():
            if domain in host:
                return slug
    except Exception:
        pass
    return None


def get_ats_session_path(ats_slug: str) -> Path:
    """Get the persistent session directory for an ATS platform."""
    return SESSIONS_DIR / ats_slug


def save_ats_session(worker_profile_dir: Path, ats_slug: str) -> int:
    """Save auth-essential files from a worker profile to the ATS session dir.

    Called after a successful HITL login or apply on an ATS. Persists
    cookies, login data, and local storage so future workers can reuse
    the authenticated session.

    Args:
        worker_profile_dir: Path to the worker's Chrome user-data dir.
        ats_slug: ATS platform slug (e.g., 'workday').

    Returns:
        Number of files copied.
    """
    session_dir = get_ats_session_path(ats_slug)
    count = _copy_auth_files(worker_profile_dir, session_dir)
    if count:
        logger.info("Saved %d auth files to ATS session: %s", count, ats_slug)
    return count


def clear_ats_session(ats_slug: str) -> bool:
    """Remove a stale ATS session (e.g., expired cookies).

    Returns:
        True if a session was removed.
    """
    session_dir = get_ats_session_path(ats_slug)
    if session_dir.exists():
        shutil.rmtree(str(session_dir), ignore_errors=True)
        logger.info("Cleared stale ATS session: %s", ats_slug)
        return True
    return False


def list_ats_sessions() -> list[dict]:
    """List all saved ATS sessions with their age.

    Returns:
        List of dicts with keys: slug, path, age_hours, has_cookies.
    """
    sessions = []
    if not SESSIONS_DIR.exists():
        return sessions
    for entry in sorted(SESSIONS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        cookies = entry / "Default" / "Cookies"
        age_hours = None
        if cookies.exists():
            import os
            mtime = os.path.getmtime(cookies)
            age_hours = (time.time() - mtime) / 3600
        sessions.append({
            "slug": entry.name,
            "path": str(entry),
            "age_hours": age_hours,
            "has_cookies": cookies.exists(),
        })
    return sessions


# ---------------------------------------------------------------------------
# Cross-platform process helpers
# ---------------------------------------------------------------------------

def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its children.

    On Windows, Chrome spawns 10+ child processes (GPU, renderer, etc.),
    so taskkill /T is needed to kill the entire tree. On Unix, os.killpg
    handles the process group.
    """
    import signal as _signal

    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            # Unix: kill entire process group
            import os
            try:
                os.killpg(os.getpgid(pid), _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # Process already gone or owned by another user
                try:
                    os.kill(pid, _signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    except Exception:
        logger.debug("Failed to kill process tree for PID %d", pid, exc_info=True)


def _kill_on_port(port: int) -> None:
    """Kill any process listening on a specific port (zombie cleanup).

    Uses netstat on Windows, lsof on macOS/Linux.
    """
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    if pid.isdigit():
                        _kill_process_tree(int(pid))
        else:
            # macOS / Linux
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=10,
            )
            for pid_str in result.stdout.strip().splitlines():
                pid_str = pid_str.strip()
                if pid_str.isdigit():
                    _kill_process_tree(int(pid_str))
    except FileNotFoundError:
        logger.debug("Port-kill tool not found (netstat/lsof) for port %d", port)
    except Exception:
        logger.debug("Failed to kill process on port %d", port, exc_info=True)


# ---------------------------------------------------------------------------
# Worker profile management
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Whitelist-based profile cloning — only copy what's needed for auth
# ---------------------------------------------------------------------------

# Top-level files needed (outside Default/)
_TOP_LEVEL_FILES = ("Local State",)

# Files inside Default/ needed for sessions and auth
_DEFAULT_FILES = (
    "Cookies", "Cookies-journal",
    "Login Data", "Login Data-journal",
    "Web Data", "Web Data-journal",
    "Preferences", "Secure Preferences",
    "Affiliation Database", "Affiliation Database-journal",
    "Network Action Predictor", "Network Action Predictor-journal",
)

# Directories inside Default/ needed for auth (some sites store tokens here)
_DEFAULT_DIRS = (
    "Local Storage",
    "Session Storage",
    "IndexedDB",
    "Extension State",
    "Local Extension Settings",
)

# Session/tab files to NEVER copy (these are huge with many tabs open)
_NEVER_COPY = {
    "Current Session", "Current Tabs", "Last Session", "Last Tabs",
    "Sessions", "SingletonLock", "SingletonSocket", "SingletonCookie",
}


def _copy_auth_files(source: Path, dest: Path) -> int:
    """Copy only auth-essential files from a Chrome profile.

    Uses a whitelist approach: only copies cookies, login data, local storage,
    and preferences. Skips session/tab state, history, caches, and everything
    else that makes profiles huge.

    Returns:
        Number of files/dirs successfully copied.
    """
    copied = 0
    dest.mkdir(parents=True, exist_ok=True)

    # Top-level files
    for fname in _TOP_LEVEL_FILES:
        src = source / fname
        if src.exists():
            try:
                shutil.copy2(str(src), str(dest / fname))
                copied += 1
            except (PermissionError, OSError):
                pass

    # Default/ directory
    src_default = source / "Default"
    dst_default = dest / "Default"
    if not src_default.exists():
        return copied
    dst_default.mkdir(parents=True, exist_ok=True)

    # Individual files in Default/
    for fname in _DEFAULT_FILES:
        src = src_default / fname
        if src.exists():
            try:
                shutil.copy2(str(src), str(dst_default / fname))
                copied += 1
            except (PermissionError, OSError):
                pass

    # Directories in Default/ (local storage, IndexedDB, etc.)
    for dname in _DEFAULT_DIRS:
        src = src_default / dname
        if src.is_dir():
            try:
                shutil.copytree(
                    str(src), str(dst_default / dname),
                    dirs_exist_ok=True,
                )
                copied += 1
            except (PermissionError, OSError):
                pass

    return copied


def _refresh_session_files(profile_dir: Path) -> None:
    """Re-copy auth files from the user's real Chrome profile.

    Updates Cookies, Login Data, Web Data, and Local Storage in the
    worker's profile so that expired sessions get refreshed without
    wiping the entire worker profile.
    """
    source = config.get_chrome_user_data()
    count = _copy_auth_files(source, profile_dir)
    if count:
        logger.info("Refreshed %d auth files in worker profile", count)


def _init_clean_profile(profile_dir: Path) -> None:
    """Create a minimal, clean Chrome profile with no real-browser data.

    Writes only the bare Preferences needed for Chrome to start without a
    restore-nag. No cookies, no login data, no sync tokens — nothing copied
    from the user's real browser.
    """
    default_dir = profile_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)

    minimal_prefs: dict = {}
    prefs_file = default_dir / "Preferences"
    prefs_file.write_text(json.dumps(minimal_prefs), encoding="utf-8")


def setup_worker_profile(worker_id: int, refresh_cookies: bool = False,
                         ats_slug: str | None = None) -> Path:
    """Create an isolated Chrome profile for a worker.

    Profiles are completely clean — no data is copied from the user's real
    Chrome browser. This prevents Google sync, extension contamination, and
    unwanted password autofill triggered by real-browser sessions.

    ATS sessions saved by a previous successful apply (via save_ats_session)
    are overlaid on top so the worker can reuse prior authentication.

    Args:
        worker_id: Numeric worker identifier.
        refresh_cookies: Ignored (kept for API compatibility). Workers never
            copy from the real browser, so there is nothing to refresh.
        ats_slug: Optional ATS platform slug for session overlay.

    Returns:
        Path to the worker's Chrome user-data directory.
    """
    profile_dir = config.CHROME_WORKER_DIR / f"worker-{worker_id}"
    prefs_file = profile_dir / "Default" / "Preferences"
    if not prefs_file.exists():
        logger.info("[worker-%d] Creating clean isolated profile", worker_id)
        _init_clean_profile(profile_dir)

    # Wipe the entire in-profile Extensions directory.
    # Our extension is loaded via --load-extension (outside the user-data-dir),
    # so nothing in Extensions/ is needed.  Wiping it prevents Chrome from
    # re-loading or re-downloading legacy extensions (Momentum, Tampermonkey,
    # etc.) that may have accumulated before the clean-profile code was added.
    ext_in_profile = profile_dir / "Default" / "Extensions"
    if ext_in_profile.is_dir():
        shutil.rmtree(str(ext_in_profile), ignore_errors=True)
        ext_in_profile.mkdir(parents=True, exist_ok=True)
        logger.debug("[worker-%d] Wiped in-profile Extensions directory", worker_id)

    # Overlay ATS session if available
    if ats_slug:
        session_dir = get_ats_session_path(ats_slug)
        if (session_dir / "Default").exists():
            overlay_count = _copy_auth_files(session_dir, profile_dir)
            logger.info("[worker-%d] Overlaid %d auth files from %s session",
                        worker_id, overlay_count, ats_slug)

    # Deploy per-worker extension (copy source + inject per-worker config)
    # NOTE: Extension must live OUTSIDE the user-data-dir — Chrome 75+ ignores
    # --load-extension paths that are inside the user-data-dir.
    ext_src = Path(__file__).parent / "extension"
    if ext_src.exists():
        ext_dst = config.CHROME_WORKER_DIR / "extensions" / f"worker-{worker_id}"
        try:
            shutil.copytree(str(ext_src), str(ext_dst), dirs_exist_ok=True)
            server_port = HITL_LISTEN_BASE_PORT + worker_id
            config_js = (
                f"// Per-worker config — generated by setup_worker_profile()\n"
                f"// DO NOT EDIT — regenerated each time the worker profile is set up\n"
                f"globalThis.WORKER_CONFIG = {{workerId: {worker_id}, serverPort: {server_port}}};\n"
            )
            # config.js: used by popup.html (loaded as a regular <script> tag)
            (ext_dst / "config.js").write_text(config_js, encoding="utf-8")
            # Prepend config inline to background.js (classic SW — no module import needed)
            bg_src = (ext_dst / "background.js").read_text(encoding="utf-8")
            (ext_dst / "background.js").write_text(config_js + bg_src, encoding="utf-8")
            logger.debug("[worker-%d] Extension deployed to %s", worker_id, ext_dst)
        except (OSError, shutil.Error) as e:
            logger.warning("[worker-%d] Extension deploy failed (non-fatal): %s", worker_id, e)

    return profile_dir


def _remove_singleton_locks(profile_dir: Path) -> None:
    """Delete Chrome singleton lock files so Chrome can start cleanly.

    When a Chrome profile is copied from another machine (or left over from
    a crashed session), it contains SingletonLock/SingletonSocket/SingletonCookie
    files. Chrome sees these and refuses to open the debug port, showing a
    "profile in use by another computer" dialog instead.
    """
    _LOCKS = ("SingletonLock", "SingletonSocket", "SingletonCookie")
    for fname in _LOCKS:
        lock = profile_dir / fname
        if lock.exists():
            try:
                lock.unlink()
                logger.debug("Removed stale lock: %s", lock)
            except OSError:
                pass


def _suppress_restore_nag(profile_dir: Path, worker_id: int | None = None) -> None:
    """Patch Chrome Preferences before launch.

    Handles three things:
    1. Suppress the 'restore pages?' nag (Chrome marks exit_type=Crashed on kill).
    2. Disable Google account sync and sign-in so the worker profile stays isolated —
       without this, Chrome re-authenticates from copied session cookies and starts
       pulling in synced extensions, passwords, and settings from the user's real profile.
    3. Disable all password saving and autofill so Chrome doesn't interfere with
       the agent's form-filling (wrong saved credentials, unwanted popups).
    """
    prefs_file = profile_dir / "Default" / "Preferences"
    if not prefs_file.exists():
        return

    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))

        # --- 1. Suppress restore nag ---
        prefs.setdefault("profile", {})["exit_type"] = "Normal"
        if worker_id is not None:
            # Open the worker's status homepage on startup (served by the always-on
            # HTTP server at port 7380+worker_id, which starts before Chrome launches).
            server_port = HITL_LISTEN_BASE_PORT + worker_id
            prefs.setdefault("session", {})["restore_on_startup"] = 4  # 4 = open specific URLs
            prefs.setdefault("session", {})["startup_urls"] = [f"http://localhost:{server_port}/"]
        else:
            prefs.setdefault("session", {})["restore_on_startup"] = 5  # 5 = New Tab page
            prefs.setdefault("session", {}).pop("startup_urls", None)

        # --- 2. Disable Google sync and sign-in ---
        # Prevents Chrome from re-syncing extensions/passwords from the user's
        # real Google profile when the copied session cookies re-authenticate.
        prefs.setdefault("sync", {}).update({
            "requested": False,
            "has_setup_completed": False,
            "suppress_start": True,
            "keep_everything_synced": False,
        })
        prefs.setdefault("signin", {}).update({
            "allowed": False,
            "allowed_on_next_startup": False,
        })

        # --- 3. Disable password manager and all autofill ---
        prefs["credentials_enable_service"] = False
        prefs["credentials_enable_autosign"] = False
        prefs.setdefault("profile", {})["password_manager_enabled"] = False
        prefs.setdefault("password_manager", {}).update({
            "saving_enabled": False,
            "autosignin_enabled": False,
        })
        prefs.setdefault("autofill", {}).update({
            "enabled": False,
            "credit_card_enabled": False,
            "profile_enabled": False,
        })

        # --- 4. Extension registration and pinning ---
        # APPLYPILOT_EXT_ID is derived from the RSA key in manifest.json ("key" field).
        # sha256(base64decode(key))[:16bytes] mapped nibble→a-p.
        # Verified: base64.b64decode(key) | sha256 | first 32 hex nibbles → a-p
        APPLYPILOT_EXT_ID = "almfihgbaclbghnagbfecfpppmjfmlnp"

        ext_dir = prefs.setdefault("extensions", {})

        # Enable developer mode so --load-extension works
        ext_dir.setdefault("ui", {})["developer_mode"] = True

        # Whitelist-clean extensions.settings: keep only Chrome built-ins,
        # our known ApplyPilot extension ID, and worker-specific ext dirs.
        # Removes: web-store extensions, stale source-dir entries, relative paths.
        ext_settings = ext_dir.setdefault("settings", {})
        worker_ext_prefix = str(config.CHROME_WORKER_DIR / "extensions")
        keep_prefixes = ("/opt/google/chrome/", worker_ext_prefix)
        stale = [
            k for k, v in ext_settings.items()
            if k != APPLYPILOT_EXT_ID and
            not any(str(v.get("path", "")).startswith(p) for p in keep_prefixes)
        ]
        for k in stale:
            del ext_settings[k]

        # Inject/update the ApplyPilot extension entry so Chrome loads it from
        # the correct worker-specific dir with developer-mode trust.
        if worker_id is not None:
            worker_ext_path = str(config.CHROME_WORKER_DIR / "extensions" / f"worker-{worker_id}")
            entry = ext_settings.get(APPLYPILOT_EXT_ID, {})
            entry.update({
                "path": worker_ext_path,
                "location": 4,          # COMMAND_LINE — trusted, no update check
                "from_webstore": False,
                "disable_reasons": 0,   # no disable reasons → extension stays enabled
                # active_permissions must be present or Chrome treats the extension as
                # "not yet installed" and blocks access to its resources (ERR_BLOCKED_BY_CLIENT).
                # These values must exactly match manifest.json permissions/host_permissions.
                "active_permissions": {
                    "api": ["activeTab", "alarms", "storage"],
                    "explicit_host": ["http://localhost/*"],
                    "manifest_permissions": [],
                    "scriptable_host": [],
                },
            })
            ext_settings[APPLYPILOT_EXT_ID] = entry

        # Ensure ApplyPilot is pinned (visible in toolbar, not hidden behind puzzle icon).
        # Also forcibly delete known bad IDs from ext_settings — these IDs can point to the
        # same directory as the correct ID, causing Chrome to refuse to load the correct one
        # (Chrome won't load the same extension directory twice under different IDs).
        _EXTRA_STALE = {
            "eloakdpcfbnnadhnohionnmicpmedapk",  # old path-derived source-dir ID (no key)
            "lafmhibgcablhganbgeffcppmpfjlmpn",  # previously computed wrong key-derived ID
        }
        # Delete from settings unconditionally (the keep_prefix check would have spared these
        # since they pointed to the worker ext dir, causing the duplicate-dir loading bug).
        for bad_id in _EXTRA_STALE:
            ext_settings.pop(bad_id, None)
        remove_from_pinned = set(stale) | _EXTRA_STALE
        pinned = [p for p in ext_dir.get("pinned_extensions", [])
                  if p not in remove_from_pinned]
        if APPLYPILOT_EXT_ID not in pinned:
            pinned.append(APPLYPILOT_EXT_ID)
        ext_dir["pinned_extensions"] = pinned

        prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
    except Exception:
        logger.debug("Could not patch Chrome preferences", exc_info=True)


# ---------------------------------------------------------------------------
# Anti-detection helpers
# ---------------------------------------------------------------------------

def _get_real_user_agent() -> str:
    """Build a realistic Chrome user agent string for macOS.

    Reads the actual Chrome version to stay current. Falls back to a
    reasonable default if detection fails.
    """
    try:
        chrome_exe = config.get_chrome_path()
        result = subprocess.run(
            [chrome_exe, "--version"],
            capture_output=True, text=True, timeout=5,
        )
        # "Google Chrome 145.0.7632.76" -> "145.0.7632.76"
        version = result.stdout.strip().split()[-1]
    except Exception:
        version = "133.0.6943.141"

    system = platform.system()
    if system == "Darwin":
        os_part = "Macintosh; Intel Mac OS X 10_15_7"
    elif system == "Windows":
        os_part = "Windows NT 10.0; Win64; x64"
    else:
        os_part = "X11; Linux x86_64"

    return (
        f"Mozilla/5.0 ({os_part}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{version} Safari/537.36"
    )


# ---------------------------------------------------------------------------
# Viewport randomization — each worker picks a random common resolution
# ---------------------------------------------------------------------------

_VIEWPORT_POOL = [
    (1920, 1080), (1440, 900), (1536, 864), (1366, 768),
    (1600, 900), (1280, 800), (1280, 720),
]

_worker_viewports: dict[int, tuple[int, int]] = {}

# Approximate GNOME top-panel height (px).  Used when computing tile Y offsets.
# macOS: menu bar is 25px.  Windows: taskbar is usually at the bottom.
_PANEL_H = {
    "Linux": 36,
    "Darwin": 25,
    "Windows": 0,
}.get(platform.system(), 36)


def _get_screen_size() -> tuple[int, int]:
    """Return (width, height) of the total desktop area across all monitors.

    Linux: reads via GTK/GDK monitor geometry — works correctly under XWayland
    where the deprecated Screen.get_width/height() returns logical/scaled sizes.
    macOS / Windows: falls back to a reasonable default.
    """
    if platform.system() == "Linux":
        try:
            import gi  # type: ignore
            gi.require_version("Gdk", "3.0")
            from gi.repository import Gdk  # type: ignore
            d = Gdk.Display.get_default()
            max_w, max_h = 0, 0
            for i in range(d.get_n_monitors()):
                m = d.get_monitor(i)
                g = m.get_geometry()
                max_w = max(max_w, g.x + g.width)
                max_h = max(max_h, g.y + g.height)
            if max_w > 0 and max_h > 0:
                return max_w, max_h
        except Exception:
            pass
    return 1920, 1080  # safe fallback


def compute_tile(worker_id: int, total_workers: int) -> tuple[int, int, int, int]:
    """Return (x, y, width, height) for a tiled Chrome window.

    Layout on a 2560×1440 screen (panel_h = 36):
      1 worker : left ~62 % of screen, full height (leaves room for terminal)
      2 workers: side-by-side half-width columns, full height
      3 workers: 2×2 grid, bottom-right quadrant left empty for terminal
      4 workers: 2×2 grid, all quadrants filled

    On other OSes the same math applies using the detected screen size.
    """
    sw, sh = _get_screen_size()
    top  = _PANEL_H
    usable_h = sh - top

    if total_workers == 1:
        # Leave the right third free for the terminal
        w = int(sw * 0.62)
        return 0, top, w, usable_h

    if total_workers == 2:
        w = sw // 2
        x = worker_id * w
        return x, top, w, usable_h

    # 3–4 workers: 2×2 grid
    col = worker_id % 2
    row = worker_id // 2
    w = sw // 2
    h = usable_h // 2
    return col * w, top + row * h, w, h


def prevent_focus_stealing() -> str | None:
    """Stop new windows from stealing keyboard focus (Linux/GNOME only).

    Sets org.gnome.desktop.wm.preferences focus-new-windows to 'strict'
    so Chrome windows launched by workers don't interrupt the user.
    Returns the previous value so it can be restored later.

    On non-Linux or when gsettings is unavailable, returns None (no-op).

    macOS note: System Preferences > Mission Control > "When switching to an
    application, switch to a Space with open windows" controls similar
    behavior; no programmatic API exists.
    Windows note: no equivalent; Chrome launch flags can request no-activate
    but this is not reliable.
    """
    if platform.system() != "Linux":
        return None
    try:
        prev = subprocess.run(
            ["gsettings", "get",
             "org.gnome.desktop.wm.preferences", "focus-new-windows"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip().strip("'")
        subprocess.run(
            ["gsettings", "set",
             "org.gnome.desktop.wm.preferences", "focus-new-windows", "strict"],
            check=True, timeout=3,
        )
        logger.info("Focus-steal prevention: set focus-new-windows=strict (was '%s')", prev)
        return prev
    except Exception as e:
        logger.debug("prevent_focus_stealing: %s", e)
        return None


def restore_focus_mode(prev: str | None) -> None:
    """Restore the focus-new-windows setting saved by prevent_focus_stealing()."""
    if not prev or platform.system() != "Linux":
        return
    try:
        subprocess.run(
            ["gsettings", "set",
             "org.gnome.desktop.wm.preferences", "focus-new-windows", prev],
            check=True, timeout=3,
        )
        logger.info("Focus mode restored to '%s'", prev)
    except Exception as e:
        logger.debug("restore_focus_mode: %s", e)


def _pick_viewport() -> tuple[int, int]:
    """Pick a random viewport from the pool."""
    return random.choice(_VIEWPORT_POOL)


def get_worker_viewport(worker_id: int) -> tuple[int, int]:
    """Return the stored viewport for a worker, or (1280, 800) fallback."""
    return _worker_viewports.get(worker_id, (1280, 800))


# ---------------------------------------------------------------------------
# Chrome launch / kill
# ---------------------------------------------------------------------------

def bring_to_foreground() -> None:
    """Attempt to bring Chrome to the foreground (best-effort).

    Tries wmctrl then xdotool on Linux; AppleScript on macOS.
    Fails silently — this is UI polish, not critical.
    """
    try:
        if platform.system() == "Darwin":
            subprocess.run(
                ["osascript", "-e",
                 'tell application "Google Chrome" to activate'],
                timeout=3, capture_output=True,
            )
        else:
            # Try wmctrl first (more reliable for window managers)
            result = subprocess.run(
                ["wmctrl", "-a", "Chrome"],
                timeout=3, capture_output=True,
            )
            if result.returncode != 0:
                subprocess.run(
                    ["xdotool", "search", "--name", "Chrome",
                     "windowactivate", "--sync"],
                    timeout=3, capture_output=True,
                )
    except Exception:
        pass  # Best-effort only


def bring_to_foreground_cdp(cdp_port: int) -> bool:
    """Bring a Chrome window to the foreground via CDP Page.bringToFront.

    Uses websocket-client to send a CDP command directly to Chrome.
    Works on Wayland, X11, and macOS — no external tools required.

    Returns True on success, False if Chrome isn't reachable.
    """
    import json
    from urllib.request import urlopen
    from urllib.error import URLError

    try:
        with urlopen(f"http://localhost:{cdp_port}/json", timeout=2) as r:
            targets = json.loads(r.read())
    except (URLError, OSError, Exception):
        return False

    ws_url = next(
        (t.get("webSocketDebuggerUrl") for t in targets if t.get("type") == "page"),
        None,
    )
    if not ws_url:
        return False

    try:
        import websocket  # websocket-client
        ws = websocket.WebSocket()
        ws.connect(ws_url, timeout=3, origin="http://localhost")
        ws.send(json.dumps({"id": 1, "method": "Page.bringToFront"}))
        ws.recv()
        ws.close()
        return True
    except Exception:
        return False


def _raise_x11_window(pid: int) -> bool:
    """Raise an X11/XWayland window belonging to a PID via libX11 ctypes.

    Sends a _NET_ACTIVE_WINDOW ClientMessage to the root window, which asks
    the compositor (Mutter, KWin, etc.) to raise and focus the window.
    Works without xdotool or wmctrl installed.

    Returns True on success, False if libX11 is unavailable or the window
    can't be found.
    """
    if not pid:
        return False
    try:
        import ctypes
        X11 = ctypes.CDLL("libX11.so.6")
        X11.XOpenDisplay.restype = ctypes.c_void_p
        X11.XDefaultRootWindow.restype = ctypes.c_ulong
        X11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        X11.XInternAtom.restype = ctypes.c_ulong
        X11.XGetWindowProperty.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.c_long, ctypes.c_long, ctypes.c_int,
            ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_void_p),
        ]
        X11.XFree.argtypes = [ctypes.c_void_p]
        X11.XSendEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
            ctypes.c_long, ctypes.c_void_p,
        ]
        X11.XFlush.argtypes = [ctypes.c_void_p]
        X11.XCloseDisplay.argtypes = [ctypes.c_void_p]

        dpy = X11.XOpenDisplay(None)
        if not dpy:
            return False
        try:
            root = X11.XDefaultRootWindow(dpy)
            XA_WINDOW   = ctypes.c_ulong(33)
            XA_CARDINAL = ctypes.c_ulong(6)
            NET_CLIENT_LIST = X11.XInternAtom(dpy, b"_NET_CLIENT_LIST", 0)
            NET_WM_PID      = X11.XInternAtom(dpy, b"_NET_WM_PID", 0)
            NET_ACTIVE_WIN  = X11.XInternAtom(dpy, b"_NET_ACTIVE_WINDOW", 0)

            atype = ctypes.c_ulong()
            afmt = ctypes.c_int()
            nitems = ctypes.c_ulong()
            bafter = ctypes.c_ulong()
            data = ctypes.c_void_p()

            # Fetch all managed windows
            X11.XGetWindowProperty(
                dpy, root, NET_CLIENT_LIST,
                0, 0x7FFFFFFF, 0, XA_WINDOW,
                ctypes.byref(atype), ctypes.byref(afmt),
                ctypes.byref(nitems), ctypes.byref(bafter), ctypes.byref(data),
            )
            wins = list(ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[:nitems.value])
            X11.XFree(data)

            # Find the first window belonging to this PID
            target = None
            for win in wins:
                X11.XGetWindowProperty(
                    dpy, win, NET_WM_PID,
                    0, 1, 0, XA_CARDINAL,
                    ctypes.byref(atype), ctypes.byref(afmt),
                    ctypes.byref(nitems), ctypes.byref(bafter), ctypes.byref(data),
                )
                if atype.value == XA_CARDINAL.value and nitems.value:
                    win_pid = ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[0]
                    X11.XFree(data)
                    if win_pid == pid:
                        target = win
                        break
                else:
                    X11.XFree(data)

            if not target:
                return False

            # Send _NET_ACTIVE_WINDOW ClientMessage to root
            class _EvData(ctypes.Union):
                _fields_ = [("l", ctypes.c_long * 5), ("b", ctypes.c_char * 20)]
            class _XClientMsg(ctypes.Structure):
                _fields_ = [
                    ("type",         ctypes.c_int),
                    ("serial",       ctypes.c_ulong),
                    ("send_event",   ctypes.c_int),
                    ("display",      ctypes.c_void_p),
                    ("window",       ctypes.c_ulong),
                    ("message_type", ctypes.c_ulong),
                    ("format",       ctypes.c_int),
                    ("data",         _EvData),
                ]
            ev = _XClientMsg()
            ev.type = 33           # ClientMessage
            ev.window = target
            ev.message_type = NET_ACTIVE_WIN
            ev.format = 32
            ev.data.l[0] = 2       # source = application
            ev.data.l[1] = 0       # timestamp (0 = current)
            ev.data.l[2] = 0       # currently active window

            # SubstructureRedirectMask | SubstructureNotifyMask
            X11.XSendEvent(dpy, root, 0, (1 << 20) | (1 << 19), ctypes.byref(ev))
            X11.XFlush(dpy)
            return True
        finally:
            X11.XCloseDisplay(dpy)
    except Exception:
        logger.debug("X11 window raise failed", exc_info=True)
        return False


def bring_to_foreground_pid(pid: int) -> None:
    """Bring a Chrome window to the foreground by PID.

    Tries (in order): xdotool, wmctrl, X11 ctypes (_NET_ACTIVE_WINDOW),
    generic bring_to_foreground(). Best-effort — fails silently.
    """
    if not pid:
        bring_to_foreground()
        return
    try:
        if platform.system() == "Darwin":
            subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to set frontmost of '
                 f'(first process whose unix id is {pid}) to true'],
                timeout=3, capture_output=True,
            )
            return
        # xdotool
        result = subprocess.run(
            ["xdotool", "search", "--pid", str(pid), "windowactivate", "--sync"],
            timeout=3, capture_output=True,
        )
        if result.returncode == 0:
            return
        # wmctrl
        lp = subprocess.run(
            ["wmctrl", "-l", "-p"], timeout=3, capture_output=True, text=True,
        )
        if lp.returncode == 0:
            for line in lp.stdout.splitlines():
                parts = line.split(None, 4)
                if len(parts) >= 3 and parts[2] == str(pid):
                    subprocess.run(
                        ["wmctrl", "-i", "-a", parts[0]],
                        timeout=3, capture_output=True,
                    )
                    return
        # X11 ctypes — works on X11/XWayland without external tools
        if _raise_x11_window(pid):
            return
        bring_to_foreground()
    except Exception:
        pass  # Best-effort only


def _find_chrome_pid_for_port(port: int) -> int | None:
    """Find the PID of a Chrome/Chromium process listening on the given CDP port.

    Scans /proc/*/cmdline (Linux only). Returns None on non-Linux or if not found.
    """
    target = f"--remote-debugging-port={port}"
    for path in _glob.glob("/proc/*/cmdline"):
        try:
            with open(path, "rb") as fh:
                cmdline = fh.read().decode("utf-8", errors="replace").replace("\x00", " ")
            if target in cmdline and ("chrome" in cmdline or "chromium" in cmdline):
                return int(path.split("/")[2])
        except (OSError, ValueError, IndexError):
            pass
    return None


def probe_existing_chrome(port: int, expected_profile_dir: Path) -> int | None:
    """Check if a usable Chrome instance is already running on the given CDP port.

    Verifies the instance belongs to the expected worker profile by inspecting
    the process cmdline. Only works on Linux (requires /proc filesystem).

    Returns:
        Chrome PID if a verified Chrome is running on this port, None otherwise.
    """
    # Step 1: Does CDP respond?
    try:
        _ureq.urlopen(f"http://localhost:{port}/json", timeout=2).read()
    except Exception:
        return None  # Nothing (or wrong thing) on this port

    # Step 2: Find the Chrome PID via /proc cmdline scan
    pid = _find_chrome_pid_for_port(port)
    if pid is None:
        logger.debug("Chrome detected on port %d but PID not found in /proc", port)
        return None

    # Step 3: Verify it's using our expected profile directory
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmdline = fh.read().decode("utf-8", errors="replace")
        if str(expected_profile_dir) not in cmdline:
            logger.debug(
                "Chrome on port %d (pid %d) uses a different profile — not ours",
                port, pid,
            )
            return None
    except OSError:
        # /proc entry disappeared — process exited between steps
        return None

    logger.info("Verified existing Chrome on port %d (pid %d)", port, pid)
    return pid


def launch_chrome(worker_id: int, port: int | None = None,
                  headless: bool = False,
                  refresh_cookies: bool = False,
                  minimized: bool = False,
                  ats_slug: str | None = None,
                  total_workers: int = 1) -> subprocess.Popen:
    """Launch a Chrome instance with remote debugging for a worker.

    Args:
        worker_id: Numeric worker identifier.
        port: CDP port. Defaults to BASE_CDP_PORT + worker_id.
        headless: Run Chrome in headless mode (no visible window).
        refresh_cookies: Re-copy session files from user's Chrome profile.
        minimized: Start Chrome minimized. Defaults to False — windows are
            tiled on the desktop using compute_tile(). Pass True to override
            and start hidden (useful for background/CI runs).
        ats_slug: Optional ATS platform slug. If a persistent session
            exists for this ATS, its auth files are overlaid on the
            worker profile.
        total_workers: Total number of concurrent workers, used to compute
            the tile layout position. Defaults to 1.

    Returns:
        subprocess.Popen handle for the Chrome process.
    """
    if port is None:
        port = BASE_CDP_PORT + worker_id

    profile_dir = setup_worker_profile(worker_id, refresh_cookies=refresh_cookies,
                                       ats_slug=ats_slug)

    # Kill any zombie Chrome from a previous run on this port
    _kill_on_port(port)

    # Remove stale singleton locks (left from copied/crashed profiles)
    _remove_singleton_locks(profile_dir)

    # Patch preferences to suppress restore nag and set startup homepage
    _suppress_restore_nag(profile_dir, worker_id=worker_id)

    chrome_exe = config.get_chrome_path()

    vp = _pick_viewport()
    _worker_viewports[worker_id] = vp

    # Tile position for this worker (ignored when headless or minimized)
    tile_x, tile_y, tile_w, tile_h = compute_tile(worker_id, total_workers)

    cmd = [
        chrome_exe,
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=http://localhost",
        f"--user-data-dir={profile_dir}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        f"--window-size={tile_w},{tile_h}",
        "--disable-session-crashed-bubble",
        "--disable-features=InfiniteSessionRestore,PasswordManagerOnboarding,"
        "SyncDisabledWithNoNetwork,ChromeSignin,Sync,"
        # Bypass XDG portal for file dialogs — portal routes through Nautilus which
        # hangs when the saved last-directory path doesn't exist on this machine.
        # GtkFileDialogPortal disables portal; FileSystemAccessAPI keeps upload working.
        "GtkFileDialogPortal",
        "--hide-crash-restore-bubble",
        "--noerrdialogs",
        "--password-store=basic",
        "--disable-save-password-bubble",
        "--disable-sync",
        "--disable-popup-blocking",
        # Use XWayland instead of native Wayland to avoid NVIDIA EGL black rendering
        "--ozone-platform=x11",
        # Block dangerous permissions at browser level
        "--deny-permission-prompts",
        "--disable-notifications",
        f"--user-agent={_get_real_user_agent()}",
        "--disable-infobars",
    ]

    # Load the per-worker ApplyPilot extension if it exists
    # Extension lives outside the user-data-dir (Chrome 75+ requirement)
    ext_path = config.CHROME_WORKER_DIR / "extensions" / f"worker-{worker_id}"
    if ext_path.exists() and not headless:
        cmd.append(f"--load-extension={ext_path}")

    if headless:
        cmd.append("--headless=new")
    elif minimized:
        cmd.append("--start-minimized")
    else:
        # Position the window in its designated tile slot
        cmd.append(f"--window-position={tile_x},{tile_y}")

    # On Unix, start in a new process group so we can kill the whole tree
    kwargs: dict = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if platform.system() != "Windows":
        import os
        kwargs["preexec_fn"] = os.setsid

    proc = subprocess.Popen(cmd, **kwargs)
    with _chrome_lock:
        _chrome_procs[worker_id] = proc

    # Give Chrome time to start and open the debug port
    time.sleep(3)
    logger.info("[worker-%d] Chrome started on port %d (pid %d)",
                worker_id, port, proc.pid)
    return proc


def cleanup_worker(worker_id: int, process: subprocess.Popen | None) -> None:
    """Kill a worker's Chrome instance and remove it from tracking.

    Args:
        worker_id: Numeric worker identifier.
        process: The Popen handle (or _AdoptedChromeProcess) from launch_chrome.
    """
    if process and process.poll() is None:
        if process.pid:
            _kill_process_tree(process.pid)
        else:
            # Adopted process with unknown PID — fall back to port-based kill
            _kill_on_port(BASE_CDP_PORT + worker_id)
    with _chrome_lock:
        _chrome_procs.pop(worker_id, None)
    logger.info("[worker-%d] Chrome cleaned up", worker_id)


def kill_all_chrome() -> None:
    """Kill all Chrome instances and any port zombies.

    Called during graceful shutdown to ensure no orphan Chrome processes.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()

    for wid, proc in procs.items():
        if proc.poll() is None:
            if proc.pid:
                _kill_process_tree(proc.pid)
            else:
                _kill_on_port(BASE_CDP_PORT + wid)
        _kill_on_port(BASE_CDP_PORT + wid)

    # Sweep base port in case of zombies
    _kill_on_port(BASE_CDP_PORT)


def reset_worker_dir(worker_id: int) -> Path:
    """Wipe and recreate a worker's isolated working directory.

    Each job gets a fresh working directory so that file conflicts
    (resume PDFs, MCP configs) don't bleed between jobs.

    Args:
        worker_id: Numeric worker identifier.

    Returns:
        Path to the clean worker directory.
    """
    worker_dir = config.APPLY_WORKER_DIR / f"worker-{worker_id}"
    if worker_dir.exists():
        shutil.rmtree(str(worker_dir), ignore_errors=True)
    worker_dir.mkdir(parents=True, exist_ok=True)
    return worker_dir


def cleanup_on_exit() -> None:
    """Atexit handler: kill all Chrome processes and sweep CDP ports.

    Register this with atexit.register() at application startup.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _kill_on_port(BASE_CDP_PORT + wid)

    # Sweep base port for any orphan
    _kill_on_port(BASE_CDP_PORT)
