"""Chrome lifecycle management for apply workers.

Handles launching an isolated Chrome instance with remote debugging,
worker profile setup/cloning, and cross-platform process cleanup.
"""

import json
import logging
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path

from applypilot import config

logger = logging.getLogger(__name__)

# CDP port base — each worker uses BASE_CDP_PORT + worker_id
BASE_CDP_PORT = 9222

# Track Chrome processes per worker for cleanup
_chrome_procs: dict[int, subprocess.Popen] = {}
_chrome_lock = threading.Lock()


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


def setup_worker_profile(worker_id: int, refresh_cookies: bool = False) -> Path:
    """Create an isolated Chrome profile for a worker.

    Uses a whitelist approach: only copies auth-essential files (cookies,
    login data, preferences, local storage). Skips tab state, history,
    caches, and extensions — making this fast even with hundreds of tabs.

    Args:
        worker_id: Numeric worker identifier.
        refresh_cookies: If True, re-copy auth files from the source Chrome
            profile into the existing worker profile.

    Returns:
        Path to the worker's Chrome user-data directory.
    """
    profile_dir = config.CHROME_WORKER_DIR / f"worker-{worker_id}"
    if (profile_dir / "Default").exists():
        if refresh_cookies:
            _refresh_session_files(profile_dir)
        return profile_dir  # Already initialized

    # Find a source: prefer existing worker (has session cookies), else user profile
    source: Path | None = None
    for wid in range(10):
        if wid == worker_id:
            continue
        candidate = config.CHROME_WORKER_DIR / f"worker-{wid}"
        if (candidate / "Default").exists():
            source = candidate
            break
    if source is None:
        source = config.get_chrome_user_data()

    logger.info("[worker-%d] Copying auth files from %s ...",
                worker_id, source.name)

    count = _copy_auth_files(source, profile_dir)
    logger.info("[worker-%d] Copied %d auth files (skipped tab state, caches, history)",
                worker_id, count)

    return profile_dir


def _suppress_restore_nag(profile_dir: Path) -> None:
    """Clear Chrome's 'restore pages' nag by fixing Preferences.

    Chrome writes exit_type=Crashed when killed, which triggers a
    'Restore pages?' prompt on next launch. This patches it out.
    """
    prefs_file = profile_dir / "Default" / "Preferences"
    if not prefs_file.exists():
        return

    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        prefs.setdefault("profile", {})["exit_type"] = "Normal"
        prefs.setdefault("session", {})["restore_on_startup"] = 4  # 4 = open blank
        prefs.setdefault("session", {}).pop("startup_urls", None)
        prefs["credentials_enable_service"] = False
        prefs.setdefault("password_manager", {})["saving_enabled"] = False
        prefs.setdefault("autofill", {})["profile_enabled"] = False
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
# Chrome launch / kill
# ---------------------------------------------------------------------------

def launch_chrome(worker_id: int, port: int | None = None,
                  headless: bool = False,
                  refresh_cookies: bool = False) -> subprocess.Popen:
    """Launch a Chrome instance with remote debugging for a worker.

    Args:
        worker_id: Numeric worker identifier.
        port: CDP port. Defaults to BASE_CDP_PORT + worker_id.
        headless: Run Chrome in headless mode (no visible window).
        refresh_cookies: Re-copy session files from user's Chrome profile.

    Returns:
        subprocess.Popen handle for the Chrome process.
    """
    if port is None:
        port = BASE_CDP_PORT + worker_id

    profile_dir = setup_worker_profile(worker_id, refresh_cookies=refresh_cookies)

    # Kill any zombie Chrome from a previous run on this port
    _kill_on_port(port)

    # Patch preferences to suppress restore nag
    _suppress_restore_nag(profile_dir)

    chrome_exe = config.get_chrome_path()

    cmd = [
        chrome_exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1024,768",
        "--disable-session-crashed-bubble",
        "--disable-features=InfiniteSessionRestore,PasswordManagerOnboarding",
        "--hide-crash-restore-bubble",
        "--noerrdialogs",
        "--password-store=basic",
        "--disable-save-password-bubble",
        "--disable-popup-blocking",
        # Block dangerous permissions at browser level
        "--deny-permission-prompts",
        "--disable-notifications",
        # Anti-detection: remove automation signals
        "--disable-blink-features=AutomationControlled",
        f"--user-agent={_get_real_user_agent()}",
        # Suppress "unsupported flag" info bars
        "--enable-automation=false",
        "--disable-infobars",
    ]
    if headless:
        cmd.append("--headless=new")

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
        process: The Popen handle returned by launch_chrome.
    """
    if process and process.poll() is None:
        _kill_process_tree(process.pid)
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
            _kill_process_tree(proc.pid)
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
