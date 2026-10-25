"""Shared process-lifecycle helpers for the DevGraph tray app (watcher + incremental indexer).

Used by both `devgraph tray start/stop/status` (devgraph/cli/main.py) and the
MCP server's auto-start-on-connect (devgraph/mcp/server.py), so there is one
PID-file/liveness-check implementation instead of two independently-drifting
copies. No UI/console output here — callers format their own messages.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from devgraph.cli._env import resolve_repo_root, resolve_venv_python
from devgraph.config import get_settings

logger = logging.getLogger(__name__)


def tray_pid_path() -> Path:
    """PID file for the detached tray process, next to registry.sqlite3/heartbeat file."""
    return get_settings().registry_db_path.parent / "tray.pid"


#: The module the agent runs as; its command line names it.
AGENT_MODULE = "devgraph.agent.tray"


def _read_tray_record() -> Optional[tuple[str, int, str]]:
    """`tray.pid` as read (text, pid, start identity), or None when it is
    missing or unreadable. A file from before the identity line, or written
    when the identity couldn't be read, has an empty one."""
    try:
        text = tray_pid_path().read_text(encoding="utf-8")
        lines = text.splitlines()
        return text, int(lines[0].strip()), (lines[1].strip() if len(lines) > 1 else "")
    except (OSError, ValueError, IndexError):
        return None


def _write_tray_record(pid: int, identity: str) -> None:
    pid_path = tray_pid_path()
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = pid_path.with_name(f"{pid_path.name}.{os.getpid()}.tmp")
    tmp.write_text(f"{pid}\n{identity}\n", encoding="utf-8")
    os.replace(tmp, pid_path)


def record_tray_pid(pid: int) -> None:
    """Write `tray.pid` for the agent just started: its pid and its start
    identity (`process_identity`), replaced atomically so no reader sees half.
    Without an identity the record still names the pid, and readers recognise
    the agent by its command line instead (`read_tray_pid`)."""
    identity = process_identity(pid)
    if identity is None:
        logger.warning("could not read the start time of the agent (pid %d); recognising it by its command line", pid)
    _write_tray_record(pid, identity or "")


def forget_tray_pid(pid: int) -> None:
    """Remove `tray.pid` when it records `pid` (the agent exiting), leaving a
    newer agent's record alone."""
    record = _read_tray_record()
    if record is not None and record[1] == pid:
        _drop_tray_record(record[0])


def _drop_tray_record(text: str) -> None:
    """Remove `tray.pid` only if it still holds exactly `text`, the record that
    was checked: a starter may have replaced it with a fresh one meanwhile."""
    try:
        if tray_pid_path().read_text(encoding="utf-8") == text:
            tray_pid_path().unlink(missing_ok=True)
    except OSError:
        pass


def _heartbeat_is_fresh() -> bool:
    beat = _heartbeat_at()
    if beat is None:
        return False
    interval = getattr(get_settings(), "health_check_interval_s", 30)
    return (datetime.now(timezone.utc) - beat).total_seconds() <= 2 * interval


def read_tray_pid() -> Optional[int]:
    """The pid of the running agent, or None.

    The pid in `tray.pid` counts only while that process still has the start
    identity recorded beside it and, where its command line can be read,
    runs `devgraph.agent.tray`. After a reboot, a crash or a kill the file
    stays behind and its pid can name an unrelated process, which must never
    be adopted or signalled: a record that fails the check is stale and is
    removed.

    A record with no identity (written before identities were recorded, by
    the agent `devgraph update` is about to stop, or when it couldn't be read)
    counts when the process's command line runs the agent or, on Windows
    where the command line isn't read, while the agent's heartbeat is fresh;
    it is then rewritten with the identity.
    """
    record = _read_tray_record()
    if record is None:
        return None
    text, pid, identity = record
    current = process_identity(pid)
    command = process_command(pid)
    if identity:
        if current == identity and (command is None or AGENT_MODULE in command):
            return pid
    elif current is not None or pid_is_running(pid):
        if (AGENT_MODULE in command) if command is not None else _heartbeat_is_fresh():
            if current is not None and _read_tray_record() == record:
                _write_tray_record(pid, current)
            return pid
    _drop_tray_record(text)
    return None


def process_identity(pid: int) -> Optional[str]:
    """A string naming this instance of process `pid` (its start time), or None
    when it is gone or unreadable. A reused pid gets a different one."""
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            created, exited, kernel, user = (ctypes.c_ulonglong() for _ in range(4))
            if not kernel32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
            ):
                return None
            return f"win:{created.value}"
        finally:
            kernel32.CloseHandle(handle)
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
            boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
            # Field 22, start time in clock ticks since boot; the name (field 2)
            # can hold spaces and parentheses, so count from its closing one.
            return f"linux:{boot}:{stat.rsplit(')', 1)[1].split()[19]}"
        except (OSError, IndexError):
            return None
    return _ps_field(pid, "lstart")


def process_command(pid: int) -> Optional[str]:
    """Process `pid`'s command line, or None where it can't be read (Windows,
    whose check rests on the start identity alone)."""
    if sys.platform == "win32":
        return None
    if sys.platform.startswith("linux"):
        try:
            return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            return None
    return _ps_field(pid, "command")


def _ps_field(pid: int, field: str) -> Optional[str]:
    # lstart is printed in the locale's words and the local time zone: fix
    # both, so the identity read later matches the one recorded.
    env = {**os.environ, "LC_ALL": "C", "TZ": "UTC0"}
    try:
        result = subprocess.run(
            ["ps", "-o", f"{field}=", "-p", str(pid)], capture_output=True, text=True, timeout=5, env=env
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return f"ps:{value}" if result.returncode == 0 and value else None


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
    return read_tray_pid() is not None


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
    if read_tray_pid() is not None:
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
        [str(python_path), "-P", "-m", "devgraph.agent.tray"],
        cwd=repo_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )

    record_tray_pid(process.pid)
    return process.pid


def tray_ui_problem() -> Optional[str]:
    """Why no tray icon can be shown in this session, or None when it can.

    pystray picks its backend at import, and on Linux with no display (or no
    tray backend for the desktop) the import itself raises; the tray process
    then runs the headless agent instead (devgraph/agent/tray.py `main`).
    """
    try:
        import pystray  # noqa: F401
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


#: How long `devgraph tray start` waits for the agent's first heartbeat.
TRAY_START_TIMEOUT_S = 30.0


def _exited(pid: int) -> bool:
    """Whether `pid` is gone. Reaps it first when it is this process's own
    child: a child that died stays a zombie, which signal 0 still reports alive."""
    if sys.platform != "win32":
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                return True
        except ChildProcessError:
            pass  # not our child; the liveness probe below decides
        except OSError:
            return True
    return not pid_is_running(pid)


def _heartbeat_at() -> Optional[datetime]:
    path = tray_pid_path().parent / "tray_heartbeat.txt"
    try:
        return datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def wait_for_tray(pid: int, since: datetime, timeout_s: float = TRAY_START_TIMEOUT_S) -> str:
    """Wait for the agent `pid` to write its first heartbeat after `since` (UTC).

    "running" once a fresh heartbeat lands and the process is still alive a
    second later (a tray that dies on its icon does so just after the health
    thread's first beat); "exited" when the process ends first; "timeout"
    when no heartbeat comes within `timeout_s`.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if _exited(pid):
            return "exited"
        beat = _heartbeat_at()
        if beat is not None and beat >= since:
            time.sleep(1.0)
            return "exited" if _exited(pid) else "running"
        if time.monotonic() >= deadline:
            return "timeout"
        time.sleep(0.25)


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
    if pid is None:
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    finally:
        tray_pid_path().unlink(missing_ok=True)
    return True
