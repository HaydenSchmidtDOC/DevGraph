"""Shared process-lifecycle helpers for the DevGraph tray app (watcher + incremental indexer).

Used by both `devgraph tray start/stop/status` (devgraph/cli/main.py) and the
MCP server's auto-start-on-connect (devgraph/mcp/server.py), so there is one
PID-file/liveness-check implementation instead of two independently-drifting
copies. No UI/console output here — callers format their own messages.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional

from devgraph.cli._env import resolve_repo_root, resolve_venv_python
from devgraph.config import get_settings


def tray_pid_path() -> Path:
    """PID file for the detached tray process, next to registry.sqlite3/heartbeat file."""
    return get_settings().registry_db_path.parent / "tray.pid"


def read_tray_pid() -> Optional[int]:
    pid_path = tray_pid_path()
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def pid_is_running(pid: int) -> bool:
    """Portable liveness check.

    os.kill(pid, 0) is not usable on Windows: it raises WinError 87
    ("parameter is incorrect") regardless of whether the PID is alive, since
    Windows has no signal-0 probe semantics. Use OpenProcess there instead;
    POSIX keeps the standard signal-0 probe.
    """
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_tray_running() -> bool:
    pid = read_tray_pid()
    return pid is not None and pid_is_running(pid)


def _holders_dir() -> Path:
    """Directory of one marker file per MCP server process currently relying
    on the shared tray process, named by that process's own PID.

    Multiple MCP clients can be connected at once, each spawning its own
    `devgraph.mcp.server` process (see that module's docstring) — the tray
    should only actually stop once none of them are still using it, not the
    moment any single one exits. Presence is re-verified against the live
    process list (`_live_holder_pids`) rather than trusted at face value, so
    a holder that crashed without cleaning up its marker doesn't wedge the
    tray on forever.
    """
    return tray_pid_path().parent / "tray_holders"


def _live_holder_pids() -> set[int]:
    holders_dir = _holders_dir()
    if not holders_dir.exists():
        return set()
    live: set[int] = set()
    for marker in holders_dir.iterdir():
        try:
            pid = int(marker.name)
        except ValueError:
            continue
        if pid_is_running(pid):
            live.add(pid)
        else:
            marker.unlink(missing_ok=True)  # stale marker from a crashed holder
    return live


def register_tray_holder(holder_pid: Optional[int] = None) -> None:
    """Record that the calling process (or `holder_pid`) is relying on the
    shared tray process, so `stop_tray_if_last_holder` won't stop it while
    this holder is still around.
    """
    holder_pid = holder_pid if holder_pid is not None else os.getpid()
    holders_dir = _holders_dir()
    holders_dir.mkdir(parents=True, exist_ok=True)
    (holders_dir / str(holder_pid)).touch()


def unregister_tray_holder(holder_pid: Optional[int] = None) -> None:
    """Remove this process's (or `holder_pid`'s) hold on the tray process.

    Does not itself stop the tray — call `stop_tray_if_last_holder` (or
    check `has_tray_holders`) afterward to actually shut it down once no
    holders remain.
    """
    holder_pid = holder_pid if holder_pid is not None else os.getpid()
    (_holders_dir() / str(holder_pid)).unlink(missing_ok=True)


def has_tray_holders() -> bool:
    """Whether any MCP server process is still relying on the shared tray."""
    return len(_live_holder_pids()) > 0


def clear_tray_holders() -> None:
    """Wipe all recorded holders, e.g. before a forced 'devgraph tray stop'."""
    holders_dir = _holders_dir()
    if not holders_dir.exists():
        return
    for marker in holders_dir.iterdir():
        marker.unlink(missing_ok=True)


def _start_lock_path() -> Path:
    return tray_pid_path().parent / "tray_start.lock"


def _acquire_start_lock():
    """OS-held advisory lock serializing the check-then-spawn below.

    Without this, two MCP server processes launched around the same time
    (e.g. two Claude Code windows connecting within the same second) can
    both pass the `read_tray_pid()`/`pid_is_running()` check before either
    has written a PID file, each spawning its own tray -- observed in
    practice as several duplicate tray icons. The lock is kernel-held
    (`msvcrt.locking` / `fcntl.flock`), so it releases automatically if the
    holding process dies mid-section -- no stale-lock cleanup needed, same
    self-healing property `tray.pid`/`tray_holders` already rely on.
    """
    path = _start_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        except OSError:
            # Another process has held it for >~10s (msvcrt's built-in retry
            # budget) -- treat as "someone else is starting the tray" and
            # bail out rather than blocking indefinitely.
            handle.close()
            return None
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _release_start_lock(handle) -> None:
    if handle is None:
        return
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def start_tray_if_not_running() -> Optional[int]:
    """Launch the tray app as a detached background process if not already running.

    Returns the tray process's PID if this call started it, or None if a
    live tray process was already running (no-op — safe to call from every
    MCP server process without spawning duplicates).
    """
    lock = _acquire_start_lock()
    if lock is None:
        return None
    try:
        return _start_tray_if_not_running_locked()
    finally:
        _release_start_lock(lock)


def _start_tray_if_not_running_locked() -> Optional[int]:
    existing_pid = read_tray_pid()
    if existing_pid is not None and pid_is_running(existing_pid):
        return None

    python_path = resolve_venv_python()
    repo_root = resolve_repo_root()
    creationflags = 0
    if sys.platform == "win32":
        # CREATE_NO_WINDOW/DETACHED_PROCESS alone aren't reliable here: this is
        # spawned from an MCP server process that itself may have no console of
        # its own (launched by the MCP client), and python.exe's console
        # subsystem can still cause a new console to be allocated for the
        # child in that situation. pythonw.exe has no console subsystem at
        # all, so it never allocates one regardless of the parent's state.
        pythonw_path = python_path.with_name("pythonw.exe")
        if pythonw_path.exists():
            python_path = pythonw_path
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS

    process = subprocess.Popen(
        [str(python_path), "-m", "devgraph.agent.tray"],
        cwd=repo_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )

    pid_path = tray_pid_path()
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(process.pid), encoding="utf-8")
    return process.pid


def stop_tray_if_last_holder(holder_pid: Optional[int] = None) -> bool:
    """Unregister this holder, and stop the shared tray process if it was the last one.

    Returns True if the tray process was actually stopped by this call.
    Safe to call even if the tray isn't running or this process was never a
    registered holder (e.g. it started the tray itself with no other
    clients ever connecting) — in that case it just stops the tray outright,
    same as `devgraph tray stop`.
    """
    unregister_tray_holder(holder_pid)
    if has_tray_holders():
        return False

    pid = read_tray_pid()
    if pid is None or not pid_is_running(pid):
        tray_pid_path().unlink(missing_ok=True)
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    finally:
        tray_pid_path().unlink(missing_ok=True)
    return True
