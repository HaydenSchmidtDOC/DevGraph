"""File and git change watcher for registered repositories.

Watches only paths obtained from RepoRegistry (explicit allowlist, never arbitrary paths).
Debounces events and invokes a callback with the set of changed file paths.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from watchdog.events import (
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer
from watchdog.observers.api import ObservedWatch

from devgraph.config import get_settings
from devgraph.indexer.dispatch import is_ignored_path
from devgraph.indexer.walk import _junction_inside, indexable_paths_under, is_ignored_dir_name
from devgraph.registry.store import RepoRegistry, RepoRecord

logger = logging.getLogger(__name__)

#: `threading.Timer`'s signature: (interval_s, function) -> a startable,
#: cancellable timer with a `daemon` attribute. Injected by tests.
TimerFactory = Callable[[float, Callable[[], None]], Any]

#: How long a reconcile waits before retrying a top-level folder it couldn't
#: watch; after the last, it gives up until the next top-level change.
RECONCILE_RETRY_DELAYS_S = (0.5, 1.0, 2.0, 4.0)

#: Failed `observer.schedule` calls one reconcile run makes before leaving the
#: rest to its retry. Each failure leaks an inotify instance in watchdog 6.0.0,
#: so a persistent failure must not be repeated per folder.
MAX_SCHEDULE_FAILURES_PER_RECONCILE = 3

#: How long after `.git/index.lock` is released a HEAD or ref change still
#: counts as part of the same git operation (W6).
GIT_LOCK_WINDOW_S = 5.0


def _is_relevant_git_state_path(path: Path) -> bool:
    """Whether a path under `.git/` actually represents git *history* state.

    The non-recursive watch on `.git/` itself (see `_start_single`) is
    scheduled on the whole directory because watchdog can't filter by
    filename at schedule time, so it delivers events for every direct child
    -- not just `HEAD`/`packed-refs`. Files like `index`, `COMMIT_EDITMSG`,
    or `FETCH_HEAD` churn on routine operations (`git status` rewrites
    `index`'s stat-cache on essentially every call) without any history
    actually changing; reacting to those was firing a full
    `sync_git_history()` for no reason on every such call. `refs/heads/*`
    (branch tips) come through the separate recursive watch on that
    subdirectory and are always relevant.
    """
    return path.name in {"HEAD", "packed-refs"} or "refs" in path.parts


class _GitStateEventHandler(FileSystemEventHandler):
    """Handles git state changes (.git/HEAD, .git/refs) with debouncing.

    Also tells the manager when the burst that moved HEAD or a ref began (W6):
    checkout, pull, merge, reset and stash take `.git/index.lock` before they
    write files, so the burst starts at that lock's creation. A lock counts
    only when it was created before the HEAD/ref event and was still held, or
    released no more than `GIT_LOCK_WINDOW_S` before it: an IDE's `git status`
    takes the same lock without moving HEAD.
    """

    def __init__(
        self,
        repo_id: str,
        debounce_ms: int,
        on_burst: Callable[[str, float | None], None],
        *,
        timer_factory: TimerFactory = threading.Timer,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._repo_id = repo_id
        self._debounce_ms = debounce_ms
        self._on_burst = on_burst
        self._timer_factory = timer_factory
        self._now = now
        self._debounce_timer: Any = None
        self._lock = threading.Lock()
        # The newest index.lock: when it was created, and released (None
        # while held). Both None when there is no record.
        self._lock_created: float | None = None
        self._lock_released: float | None = None
        # The pending burst's start, from a lock that preceded its HEAD/ref event.
        self._burst_start: float | None = None

    def on_modified(self, event: FileModifiedEvent) -> None:  # type: ignore[override]
        """Record git state change and set debounce timer."""
        if not event.is_directory and _is_relevant_git_state_path(Path(str(event.src_path))):
            self._state_changed()

    def on_created(self, event: FileCreatedEvent) -> None:  # type: ignore[override]
        """Record git state change and set debounce timer."""
        if event.is_directory:
            return
        path = Path(str(event.src_path))
        if path.name == "index.lock":
            with self._lock:
                self._lock_created, self._lock_released = self._now(), None
        if _is_relevant_git_state_path(path):
            self._state_changed()

    def on_deleted(self, event: FileDeletedEvent) -> None:  # type: ignore[override]
        """Record git state change and set debounce timer."""
        if event.is_directory:
            return
        path = Path(str(event.src_path))
        if path.name == "index.lock":
            self._lock_was_released()
        if _is_relevant_git_state_path(path):
            self._state_changed()

    def on_moved(self, event: FileMovedEvent) -> None:  # type: ignore[override]
        """Record git state change and set debounce timer."""
        if event.is_directory:
            return
        if Path(str(event.src_path)).name == "index.lock":
            self._lock_was_released()  # renamed onto `index`
        if _is_relevant_git_state_path(Path(str(event.src_path))) or _is_relevant_git_state_path(Path(str(event.dest_path))):
            self._state_changed()

    def _lock_was_released(self) -> None:
        with self._lock:
            if self._lock_created is not None and self._lock_released is None:
                self._lock_released = self._now()

    def _state_changed(self) -> None:
        """A HEAD/ref event: tie it to the lock that preceded it, if any, and
        restart the debounce."""
        t_h = self._now()
        with self._lock:
            created, released = self._lock_created, self._lock_released
            if released is not None and t_h - released > GIT_LOCK_WINDOW_S:
                self._lock_created = self._lock_released = None  # too old to matter
            elif created is not None and created < t_h:
                self._burst_start = created if self._burst_start is None else min(self._burst_start, created)
            self._reset_debounce()

    def _reset_debounce(self) -> None:
        """Reset the debounce timer. Must hold _lock."""
        if self._debounce_timer:
            self._debounce_timer.cancel()

        self._debounce_timer = self._timer_factory(
            self._debounce_ms / 1000.0,
            self._fire_change,
        )
        self._debounce_timer.daemon = True
        self._debounce_timer.start()

    def _fire_change(self) -> None:
        """Invoke the callback with the repo_id and the burst's start."""
        with self._lock:
            self._debounce_timer = None
            burst_start, self._burst_start = self._burst_start, None
        # Invoke callback outside lock to avoid deadlock
        self._on_burst(self._repo_id, burst_start)


class WatcherManager:
    """Manages file watchers for active, watch-enabled repositories.

    Only watches paths explicitly registered in RepoRegistry.
    Collects file change/deletion events over a debounce window and invokes
    a callback with the repo_id, changed paths, and deleted paths.
    """

    def __init__(
        self,
        registry: RepoRegistry,
        on_changes: Callable[[str, set[Path], set[Path]], None],
        on_git_state_changed: Callable[[str], None] | None = None,
        *,
        timer_factory: TimerFactory = threading.Timer,
        reconcile_delay_s: float = 0.2,
        on_catch_up: Callable[..., None] | None = None,
        git_catch_up_delay_s: float = 2.0,
        now: Callable[[], float] = time.time,
    ) -> None:
        """Initialize the watcher manager.

        Args:
            registry: RepoRegistry instance to read allowed repos from.
            on_changes: Callback(repo_id, changed_paths, deleted_paths)
                invoked when the debounce interval elapses.
            on_git_state_changed: Optional callback(repo_id) invoked when git
                state changes (.git/HEAD, .git/refs, etc.).
            timer_factory: Builds the debounce and reconcile timers.
            reconcile_delay_s: How long a top-level watch reconcile waits to
                coalesce the events that asked for it.
            on_catch_up: Optional callback(repo_id, since, reason) that
                catches up on changes made since `since` (W5). It runs under
                the repo's batch lock: when watching starts (reason "start"),
                after a git operation ("git"), and when asked through
                `request_catch_up` ("retry").
            git_catch_up_delay_s: How long after a git burst its catch-up runs.
            now: The wall clock, in seconds; tests inject it.
        """
        self._registry = registry
        self._on_changes = on_changes
        self._on_git_state_changed = on_git_state_changed
        self._observers: dict[str, Observer] = {}  # type: ignore[valid-type]
        self._handlers: dict[str, _RepoEventHandler] = {}
        self._git_handlers: dict[str, _GitStateEventHandler] = {}
        self._debounce_ms = get_settings().watch_debounce_ms
        self._lock = threading.Lock()
        # Track repos with path issues (e.g. missing directory) so they don't
        # crash the whole watcher. Maps repo_id -> error message.
        self._repo_issues: dict[str, str] = {}
        self._timer_factory = timer_factory
        self._reconcile_delay_s = reconcile_delay_s
        # Top-level directory watches per repo: path -> (watch, the directory's
        # identity when scheduled). The root's own non-recursive watch is not
        # listed; it is never reconciled.
        self._watches: dict[str, dict[Path, tuple[ObservedWatch, tuple[int, int] | None]]] = {}
        # One per repo, created on demand and never dropped, so a handler
        # recreated by stop/start or refresh shares it with run_exclusive (W4).
        self._batch_locks: dict[str, threading.Lock] = {}
        # Guards the reconcile bookkeeping below and _stopping. Handlers take
        # it (never _lock); it is never held while calling into an observer.
        self._reconcile_lock = threading.Lock()
        self._reconcile_pending: dict[str, Any] = {}
        self._reconcile_running: set[str] = set()
        # Top-level names deleted or moved away since the last reconcile began.
        self._reconcile_gone: dict[str, set[str]] = {}
        # Consecutive reconcile runs that couldn't watch every folder.
        self._reconcile_retries: dict[str, int] = {}
        self._stopping = False
        self._on_catch_up = on_catch_up
        self._git_catch_up_delay_s = git_catch_up_delay_s
        self._now = now
        # Guards the catch-up bookkeeping below. Never held while taking
        # _lock or a batch lock, so request_catch_up can be called from
        # inside a batch.
        self._catch_up_lock = threading.Lock()
        # repo_id -> [earliest since, reason, timer]
        self._catch_up_pending: dict[str, list[Any]] = {}
        # repo_id -> the start-of-watching catch-up's timer, until it fires.
        self._start_catch_ups: dict[str, Any] = {}
        self._catch_up_stopped = False

    def start(self) -> None:
        """Start watchers for all active, watch-enabled repos.
        
        Repos with invalid paths are logged as warnings and skipped;
        other repos continue normally so one bad path doesn't crash the whole watcher.
        """
        with self._reconcile_lock:
            self._stopping = False
        with self._catch_up_lock:
            self._catch_up_stopped = False
        with self._lock:
            repos = self._registry.list_repos(active_only=True)
            repos_to_watch = [r for r in repos if r.watch_enabled]
            for repo in repos_to_watch:
                self._start_single(repo)

    def stop(self) -> None:
        """Stop all watchers and clean up.

        Pending reconciles are cancelled, and one that is running finishes
        without queueing anything. Pending debounces and catch-ups are
        cancelled; the changes they held are left for the next start's
        catch-up. A catch-up that is running is not waited for.
        """
        with self._reconcile_lock:
            self._stopping = True
            timers = list(self._reconcile_pending.values())
            self._reconcile_pending.clear()
            self._reconcile_gone.clear()
        with self._catch_up_lock:
            self._catch_up_stopped = True
            timers += [entry[2] for entry in self._catch_up_pending.values()]
            timers += list(self._start_catch_ups.values())
            self._catch_up_pending.clear()
            self._start_catch_ups.clear()
        for timer in timers:
            timer.cancel()
        with self._lock:
            for repo_id, observer in self._observers.items():
                if observer.is_alive():
                    observer.stop()
                    observer.join(timeout=5)
            for handler in self._handlers.values():
                handler.close()
            self._observers.clear()
            self._handlers.clear()
            self._watches.clear()

    def run_exclusive(self, repo_id: str, fn: Callable[[], Any]) -> Any:
        """Run `fn` under the repo's batch lock, so it never interleaves with
        a live batch. `_lock` is held only to look the lock up."""
        with self._lock:
            lock = self._batch_locks.setdefault(repo_id, threading.Lock())
        with lock:
            return fn()

    def request_catch_up(self, repo_id: str, since: datetime, delay_s: float, reason: str = "retry") -> None:
        """Run `on_catch_up(repo_id, since)` in `delay_s`, under the repo's
        batch lock. Requests made before it runs coalesce: the earliest
        `since` is kept, and so is the first request's timer.

        Never takes `_lock` or a batch lock: `RepoSync.on_changes` calls it
        from inside a batch.
        """
        if self._on_catch_up is None:
            return
        with self._catch_up_lock:
            if self._catch_up_stopped:
                return
            entry = self._catch_up_pending.get(repo_id)
            if entry is not None:
                entry[0] = min(entry[0], since)
                if reason == "git":
                    entry[1] = reason
                return
            timer = self._timer_factory(delay_s, lambda: self._run_requested_catch_up(repo_id))
            timer.daemon = True
            self._catch_up_pending[repo_id] = [since, reason, timer]
            timer.start()

    def _run_requested_catch_up(self, repo_id: str) -> None:
        with self._catch_up_lock:
            entry = self._catch_up_pending.pop(repo_id, None)
            if entry is None or self._catch_up_stopped:
                return
        since, reason, _timer = entry

        def run() -> None:
            if not self._catch_up_stopped:
                self._on_catch_up(repo_id, since, reason)

        self.run_exclusive(repo_id, run)

    def _start_catch_up(self, repo_id: str, snapshot: datetime | None) -> None:
        """The catch-up when watching starts, on its own thread (W5).

        `snapshot` is `last_indexed` as read before the observer started, so
        a live batch that wins the batch lock first cannot raise it past
        edits made while nothing was watching. A None snapshot is re-read
        under the lock: a registration in this process may just have
        finished its first scan under the same lock.
        """
        with self._catch_up_lock:
            self._start_catch_ups.pop(repo_id, None)
            if self._catch_up_stopped:
                return

        def run() -> None:
            since = snapshot if snapshot is not None else self._last_indexed(repo_id)
            if since is None:
                logger.info('DevGraph hasn\'t indexed %s yet; run "devgraph rescan %s"', repo_id, repo_id)
                return
            self._on_catch_up(repo_id, since, "start")

        self.run_exclusive(repo_id, run)

    def _last_indexed(self, repo_id: str) -> datetime | None:
        repo = self._registry.get(repo_id)
        if repo is None or not repo.last_indexed:
            return None
        return datetime.fromisoformat(repo.last_indexed)

    def _on_git_burst(self, repo_id: str, burst_start: float | None) -> None:
        """A git burst's debounce fired: catch up from the start of the burst
        (or `last_indexed`) shortly, to repair events the OS dropped (W6),
        then sync git history."""
        if self._on_catch_up is not None:
            last = self._last_indexed(repo_id)
            if last is not None:
                since = last
                if burst_start is not None:
                    since = min(last, datetime.fromtimestamp(burst_start, timezone.utc))
                self.request_catch_up(repo_id, since, self._git_catch_up_delay_s, "git")
        if self._on_git_state_changed is not None:
            self._on_git_state_changed(repo_id)

    def refresh(self) -> None:
        """Rebuild watcher set by re-reading the registry.

        Called when the registry changes (add/remove/enable/disable).
        Skips repos with invalid paths (already logged as warnings).
        """
        with self._lock:
            # Get current state
            current_ids = set(self._observers.keys())
            repos = self._registry.list_repos(active_only=True)
            desired_ids = {r.repo_id for r in repos if r.watch_enabled}

            # Stop watchers for repos that are no longer active/watch-enabled
            for repo_id in current_ids - desired_ids:
                self._stop_single(repo_id)
                self._repo_issues.pop(repo_id, None)  # Clear any cached issues

            # Start watchers for new repos
            for repo in repos:
                if repo.repo_id in (desired_ids - current_ids):
                    self._start_single(repo)

    def _start_single(self, repo: RepoRecord) -> None:
        """Start a watcher for a single repo. Must hold _lock.
        
        Logs and caches errors for repos with invalid paths; does not raise.
        This allows other repos to continue watching normally.
        """
        if repo.repo_id in self._observers:
            return  # Already watching

        # Validate path exists before attempting to watch
        if not repo.path.exists() or not repo.path.is_dir():
            error_msg = f"path does not exist or is not a directory: {repo.path}"
            self._repo_issues[repo.repo_id] = error_msg
            logger.warning(
                f"Skipping watcher for {repo.repo_id}: {error_msg}"
            )
            return

        try:
            handler = _RepoEventHandler(
                repo.repo_id,
                repo.path,
                self._debounce_ms,
                self._on_changes,
                timer_factory=self._timer_factory,
                batch_lock=self._batch_locks.setdefault(repo.repo_id, threading.Lock()),
                request_reconcile=self._request_reconcile,
            )
            observer = Observer()
            # Don't hand watchdog a single recursive watch on repo.path: that
            # puts every file under .venv/.git/build/etc under OS-level
            # notification too, alongside the repo's actual (much smaller)
            # source tree. On Windows in particular, a directory that busy can
            # overflow ReadDirectoryChangesW's notification buffer, which
            # silently drops ALL pending events for the watch -- including ones
            # for real source edits -- so the graph looks "live" but quietly
            # stops picking up changes. Watch the root non-recursively (for
            # root-level files) plus each non-ignored top-level subdirectory
            # recursively, mirroring full_scan's IGNORED_DIR_NAMES exclusions.
            observer.schedule(handler, str(repo.path), recursive=False)
            watches = {
                child: self._schedule_dir(observer, handler, child)
                for child in sorted(self._desired_dirs(repo.path))
            }
        except (FileNotFoundError, OSError) as e:
            error_msg = f"failed to schedule watches: {e}"
            self._repo_issues[repo.repo_id] = error_msg
            logger.warning(
                f"Skipping watcher for {repo.repo_id}: {error_msg}"
            )
            return

        # Watch git state changes (.git/HEAD, .git/refs) if .git exists as a directory.
        # Skip if .git is a file (linked git worktree contains gitdir: ... pointer).
        git_dir = repo.path / ".git"
        if git_dir.is_dir() and (self._on_git_state_changed or self._on_catch_up):
            git_handler = _GitStateEventHandler(
                repo.repo_id,
                self._debounce_ms,
                self._on_git_burst,
                timer_factory=self._timer_factory,
                now=self._now,
            )
            # Non-recursive watch on .git itself (catches HEAD, packed-refs)
            observer.schedule(git_handler, str(git_dir), recursive=False)
            # Recursive watch on .git/refs/heads if it exists
            refs_heads = git_dir / "refs" / "heads"
            if refs_heads.is_dir():
                observer.schedule(git_handler, str(refs_heads), recursive=True)
            self._git_handlers[repo.repo_id] = git_handler

        # Read before the observer starts (W5): see _start_catch_up.
        snapshot = self._last_indexed(repo.repo_id) if self._on_catch_up is not None else None
        try:
            observer.start()
            self._observers[repo.repo_id] = observer
            self._handlers[repo.repo_id] = handler
            self._watches[repo.repo_id] = watches
            # Clear any cached issues now that watch started successfully
            self._repo_issues.pop(repo.repo_id, None)
            logger.debug(f"Started watcher for {repo.repo_id} at {repo.path}")
        except Exception as e:
            error_msg = f"failed to start observer: {e}"
            self._repo_issues[repo.repo_id] = error_msg
            logger.warning(
                f"Skipping watcher for {repo.repo_id}: {error_msg}"
            )
            return
        if self._on_catch_up is not None:
            repo_id = repo.repo_id
            timer = self._timer_factory(0.0, lambda: self._start_catch_up(repo_id, snapshot))
            timer.daemon = True
            with self._catch_up_lock:
                self._start_catch_ups[repo_id] = timer
            timer.start()

    def _stop_single(self, repo_id: str) -> None:
        """Stop a watcher for a single repo. Must hold _lock."""
        if repo_id not in self._observers:
            return

        observer = self._observers.pop(repo_id)
        handler = self._handlers.pop(repo_id, None)
        self._git_handlers.pop(repo_id, None)
        self._watches.pop(repo_id, None)
        with self._reconcile_lock:
            timer = self._reconcile_pending.pop(repo_id, None)
            self._reconcile_gone.pop(repo_id, None)
        with self._catch_up_lock:
            timers = [timer, self._start_catch_ups.pop(repo_id, None)]
            entry = self._catch_up_pending.pop(repo_id, None)
            if entry is not None:
                timers.append(entry[2])
        for timer in timers:
            if timer is not None:
                timer.cancel()
        if handler is not None:
            handler.close()

        if observer.is_alive():
            observer.stop()
            observer.join(timeout=5)
        logger.debug(f"Stopped watcher for {repo_id}")

    def _desired_dirs(self, root: Path) -> set[Path]:
        """The root's children that get their own recursive watch: real
        directories with no ignored name, plus junctions whose target is
        inside the repository and not ignored. Symlinked directories are
        skipped, as `walk._walk` skips them. Raises OSError if the root can't
        be listed."""
        desired = set()
        resolved_root: Path | None = None
        with os.scandir(root) as entries:
            for entry in entries:
                if is_ignored_dir_name(entry.name):
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                path = root / entry.name
                if path.is_junction():
                    resolved_root = resolved_root or root.resolve()
                    if not _junction_inside(path, resolved_root):
                        continue
                desired.add(path)
        return desired

    @staticmethod
    def _dir_identity(path: Path) -> tuple[int, int] | None:
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_dev, st.st_ino)

    def _schedule_dir(
        self, observer: Any, handler: _RepoEventHandler, directory: Path
    ) -> tuple[ObservedWatch, tuple[int, int] | None]:
        """Schedule a recursive watch on a top-level directory. Must hold _lock."""
        identity = self._dir_identity(directory)
        return observer.schedule(handler, str(directory), recursive=True), identity

    def _request_reconcile(self, repo_id: str, gone: str | None = None) -> None:
        """Ask for a top-level watch reconcile (W3). Called from handler code
        on watchdog's dispatch thread, so it takes only `_reconcile_lock`;
        requests coalesce until the timer fires.

        `gone` names a top-level entry that was deleted or moved away. Its
        watch is replaced whatever its emitter and identity say: a folder
        deleted and made again can get the same inode back (ext4 reuses them
        at once) before the old emitter has read its delete."""
        with self._reconcile_lock:
            if gone is not None and not self._stopping:
                self._reconcile_gone.setdefault(repo_id, set()).add(gone)
            self._schedule_reconcile(repo_id, self._reconcile_delay_s)

    def _schedule_reconcile(self, repo_id: str, delay_s: float) -> None:
        """Start a reconcile timer unless one is pending. Must hold _reconcile_lock."""
        if self._stopping or repo_id in self._reconcile_pending:
            return
        timer = self._timer_factory(delay_s, lambda: self._reconcile(repo_id))
        timer.daemon = True
        self._reconcile_pending[repo_id] = timer
        timer.start()

    def _reconcile(self, repo_id: str) -> None:
        """Make the repo's top-level watches match its top-level directories.

        Runs on its own timer thread. A watch is kept only while its emitter
        is alive and its path is still the same desired directory; any other
        is unscheduled, which covers a deleted directory (inotify's emitter
        stops itself but stays registered, so re-scheduling the same path
        would be a no-op), a renamed one whose stale emitter still reports the
        old name, and one that became ignored. Missing directories are then
        scheduled and walked, so files written before the watch existed are
        not lost.
        """
        with self._reconcile_lock:
            self._reconcile_pending.pop(repo_id, None)
            gone = self._reconcile_gone.pop(repo_id, set())
            if self._stopping:
                return
            self._reconcile_running.add(repo_id)
        try:
            with self._lock:
                if self._stopping or repo_id not in self._observers:
                    return
                observer = self._observers[repo_id]
                handler = self._handlers[repo_id]
                root = handler._repo_root
                try:
                    desired = self._desired_dirs(root)
                except OSError as e:
                    logger.debug("Skipping watch reconcile for %s: %s", repo_id, e)
                    return
                watches = self._watches[repo_id]
                for path, (watch, identity) in list(watches.items()):
                    emitter = observer._emitter_for_watch.get(watch)
                    if (
                        emitter is not None
                        and emitter.is_alive()
                        and path in desired
                        and path.name not in gone
                        and self._dir_identity(path) == identity
                    ):
                        continue
                    if emitter is not None:
                        observer.unschedule(watch)
                    del watches[path]
                added = []
                failed: list[str] = []
                error: OSError | None = None
                for path in sorted(desired - watches.keys()):
                    if len(failed) >= MAX_SCHEDULE_FAILURES_PER_RECONCILE:
                        failed.append(path.name)  # left for the retry
                        continue
                    if not path.is_dir():
                        continue  # gone since the listing; its event will follow
                    try:
                        watches[path] = self._schedule_dir(observer, handler, path)
                    except OSError as e:
                        failed.append(path.name)
                        error = e
                        continue
                    added.append(path)
            files: set[Path] = set()
            for path in added:
                files |= indexable_paths_under(root, path)
            with self._reconcile_lock:
                # A handler closed by stop() or refresh() drops what it's given.
                if files and not self._stopping:
                    handler.queue_changed(files)
                self._retry_reconcile(repo_id, failed, error)
        finally:
            with self._reconcile_lock:
                self._reconcile_running.discard(repo_id)

    def _retry_reconcile(self, repo_id: str, failed: list[str], error: OSError | None) -> None:
        """Retry folders a reconcile couldn't watch, with backoff, then give
        up until the next top-level change. Must hold _reconcile_lock."""
        if not failed:
            self._reconcile_retries.pop(repo_id, None)
            return
        attempt = self._reconcile_retries.get(repo_id, 0)
        folders = ", ".join(failed)
        if attempt >= len(RECONCILE_RETRY_DELAYS_S):
            self._reconcile_retries.pop(repo_id, None)
            logger.warning(
                "Couldn't watch %s in %s (%s); changes there won't be picked up "
                "until another top-level folder changes or DevGraph restarts",
                folders, repo_id, error,
            )
            return
        delay = RECONCILE_RETRY_DELAYS_S[attempt]
        self._reconcile_retries[repo_id] = attempt + 1
        logger.info("Couldn't watch %s in %s yet (%s); retrying in %g s", folders, repo_id, error, delay)
        self._schedule_reconcile(repo_id, delay)

    def get_repo_issues(self) -> dict[str, str]:
        """Return a copy of repos with path/watcher issues.
        
        Returns:
            Dict mapping repo_id -> error message for repos that couldn't be watched.
        """
        with self._lock:
            return dict(self._repo_issues.items())


class _RepoEventHandler(FileSystemEventHandler):
    """Collects one repo's file events and hands them to `on_changes` in
    debounced batches.

    A rename is a delete of its source plus a change of its destination (W1).
    A deleted directory is queued as that directory; `remove_paths` expands it
    from the graph. A created or moved-in directory is walked for its files
    (W2). An event on a direct child of the root asks the manager to reconcile
    its top-level watches (W3).

    Runs on watchdog's dispatch thread, which holds the observer's lock, so it
    never takes the manager's `_lock`. Its own `_lock` guards only the pending
    sets: walks and `on_changes` run outside it, and a batch holds the repo's
    batch lock instead (W4), so events keep collecting while one runs.

    Pending paths are keyed by their exact string, so a case-only rename's
    source and destination never cancel each other, even where `Path`
    equality ignores case.
    """

    def __init__(
        self,
        repo_id: str,
        repo_root: Path,
        debounce_ms: int,
        on_changes: Callable[[str, set[Path], set[Path]], None],
        *,
        timer_factory: TimerFactory = threading.Timer,
        batch_lock: threading.Lock | None = None,
        request_reconcile: Callable[..., None] | None = None,
    ) -> None:
        self._repo_id = repo_id
        self._repo_root = repo_root
        self._debounce_ms = debounce_ms
        self._on_changes = on_changes
        self._timer_factory = timer_factory
        self._batch_lock = batch_lock or threading.Lock()
        self._request_reconcile = request_reconcile
        self._changed: dict[str, Path] = {}
        self._deleted: dict[str, Path] = {}
        # Folder log lines, keyed by the folder's repo-relative path.
        self._folder_events: dict[str, str] = {}
        self._debounce_timer: Any = None
        self._closed = False
        self._lock = threading.Lock()

    # --- watchdog callbacks -------------------------------------------------

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(str(event.src_path))
        if self._is_tracked_path(path):
            self._queue(changed=[path])

    def on_created(self, event: FileSystemEvent) -> None:
        """A created file is a change; a created directory (new, or moved in
        from outside the repository) is walked for its files.

        Also covers the "write to a temp file, then create the real path"
        half of an atomic save."""
        path = Path(str(event.src_path))
        if event.is_directory:
            if self._should_walk(event, path):
                self._queue(changed=self._walk_dir(path))
        elif self._is_tracked_path(path):
            self._queue(changed=[path])
        self._signal_top_level(str(event.src_path))

    def on_deleted(self, event: FileSystemEvent) -> None:
        """Queue the deleted path, file or directory, unless it is ignored.

        Either kind is queued the same way: Windows reports a deleted
        directory as `FileDeletedEvent`, and `remove_paths` expands whatever
        it is given from the graph's own file list."""
        rel = self._queue_rel(str(event.src_path))
        if rel is not None:
            folders = {rel: f"Folder removed from {self._repo_id}: {rel}"} if event.is_directory else {}
            self._queue(deleted=[Path(str(event.src_path))], folders=folders)
        self._signal_top_level(gone=str(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        """A move is a delete of its source plus a change of its destination.

        The source is queued for deletion unless it is empty (a Windows rename
        pair split across two reads), outside the repository or ignored. The
        destination is a change if it is a tracked file, or, for a directory,
        its walked files; otherwise the move is a delete. An atomic save
        (temp -> real path) therefore leaves the real path changed only.
        """
        src_raw, dest_raw = str(event.src_path), str(event.dest_path)
        src_rel = self._queue_rel(src_raw)
        dest = Path(dest_raw) if dest_raw else None
        deleted = [Path(src_raw)] if src_rel is not None else []
        folders: dict[str, str] = {}
        if event.is_directory:
            changed = self._walk_dir(dest) if dest is not None and self._should_walk(event, dest) else set()
            if src_rel is not None:
                dest_rel = self._queue_rel(dest_raw)
                folders[src_rel] = (
                    f"Folder renamed in {self._repo_id}: {src_rel} → {dest_rel}"
                    if dest_rel is not None
                    else f"Folder removed from {self._repo_id}: {src_rel}"
                )
        else:
            changed = [dest] if dest is not None and self._is_tracked_path(dest) else []
        self._queue(changed=changed, deleted=deleted, folders=folders)
        self._signal_top_level(dest_raw, gone=src_raw)

    # --- batching -----------------------------------------------------------

    def queue_changed(self, paths: set[Path]) -> None:
        """Queue files found outside an event (the manager's reconcile walk)."""
        self._queue(changed=paths)

    def flush(self) -> None:
        """Fire the pending batch now, if there is one."""
        self.cancel()
        self._fire_changes()

    def cancel(self) -> None:
        """Drop the pending debounce timer. Pending paths stay queued."""
        with self._lock:
            self._cancel_timer()

    def close(self) -> None:
        """Retire the handler (the manager stopped watching its repo): drop
        the pending timer, and queue and fire nothing from now on."""
        with self._lock:
            self._closed = True
            self._cancel_timer()

    def _cancel_timer(self) -> None:
        """Must hold _lock."""
        if self._debounce_timer is not None:
            self._debounce_timer.cancel()
            self._debounce_timer = None

    def _queue(
        self,
        changed: Iterable[Path] = (),
        deleted: Iterable[Path] = (),
        folders: dict[str, str] | None = None,
    ) -> None:
        """Record deletes, then changes, keeping the two sets disjoint, and
        restart the debounce. A delete under a directory already queued for
        deletion is covered by it (a moved directory's per-child events)."""
        changed, deleted = list(changed), list(deleted)
        if not changed and not deleted:
            return
        with self._lock:
            if self._closed:
                return
            for path in deleted:
                key = str(path)
                self._changed.pop(key, None)
                if not self._has_deleted_ancestor(path):
                    self._deleted[key] = path
            for path in changed:
                key = str(path)
                self._deleted.pop(key, None)
                self._changed[key] = path
            self._folder_events.update(folders or {})
            self._reset_debounce()

    def _has_deleted_ancestor(self, path: Path) -> bool:
        """Must hold _lock."""
        for parent in path.parents:
            if parent == self._repo_root or len(parent.parts) < len(self._repo_root.parts):
                return False
            if str(parent) in self._deleted:
                return True
        return False

    def _reset_debounce(self) -> None:
        """Restart the debounce timer. Must hold _lock."""
        if self._debounce_timer is not None:
            self._debounce_timer.cancel()
        self._debounce_timer = self._timer_factory(self._debounce_ms / 1000.0, self._fire_changes)
        self._debounce_timer.daemon = True
        self._debounce_timer.start()

    def _fire_changes(self) -> None:
        """Hand the pending batch to `on_changes` under the repo's batch lock.

        The sets are swapped under `_lock`, which is released before the
        callback, so the dispatch thread can keep queueing while it runs."""
        with self._batch_lock:
            with self._lock:
                # Checked under the batch lock, so no batch starts after the
                # manager's stop() has returned.
                if self._closed or (not self._changed and not self._deleted):
                    return
                changed = set(self._changed.values())
                deleted = set(self._deleted.values())
                folders = self._folder_events
                self._changed, self._deleted, self._folder_events = {}, {}, {}
            for rel in sorted(folders):
                # One line per folder event, never per child of a moved or
                # removed folder.
                if not any(parent.as_posix() in folders for parent in Path(rel).parents[:-1]):
                    logger.info(folders[rel])
            self._on_changes(self._repo_id, changed, deleted)

    # --- paths --------------------------------------------------------------

    def _should_walk(self, event: FileSystemEvent, directory: Path) -> bool:
        """Whether a created or moved-in directory needs a walk here.

        Not for watchdog's synthetic per-child events (the parent's walk
        covered them; walking each would cost files x depth), and not for a
        direct child of the root, which the manager's reconcile walks once it
        has a watch on it."""
        if event.is_synthetic:
            return False
        if self._request_reconcile is not None:
            rel = self._rel(str(directory))
            if rel is not None and len(rel.parts) == 1:
                return False
        return True

    def _walk_dir(self, directory: Path) -> set[Path]:
        """The indexable files under a directory, walked outside `_lock`."""
        return indexable_paths_under(self._repo_root, directory)

    def _rel(self, raw: str) -> Path | None:
        """Repo-relative path of an event path, or None if empty or outside
        the repository. Lexical first; otherwise the parent is resolved and
        the leaf appended, so a missing leaf is never resolved."""
        if not raw:
            return None
        path = Path(raw)
        try:
            return path.relative_to(self._repo_root)
        except ValueError:
            pass
        try:
            return (path.parent.resolve() / path.name).relative_to(self._repo_root.resolve())
        except (OSError, ValueError):
            return None

    def _queue_rel(self, raw: str) -> str | None:
        """The repo-relative POSIX path of an event path that may be queued:
        inside the repository, not the root itself, and not ignored."""
        rel = self._rel(raw)
        if rel is None or not rel.parts or is_ignored_path(rel):
            return None
        return rel.as_posix()

    def _is_tracked_path(self, path: Path) -> bool:
        """A regular file inside the repository and not under an ignored
        directory. The per-child watches keep only top-level ignored
        directories out of the OS watch; this is the backstop for deeper ones
        (e.g. some_package/build/)."""
        try:
            if not path.is_file():
                return False
        except OSError:
            return False
        return self._queue_rel(str(path)) is not None

    def _signal_top_level(self, *raw_paths: str, gone: str | None = None) -> None:
        """Ask the manager to reconcile watches when an event touches a direct
        child of the root, naming it when it was deleted or moved away
        (`gone`). Called with `_lock` released."""
        if self._request_reconcile is None:
            return
        gone_rel = self._rel(gone) if gone else None
        if gone_rel is not None and len(gone_rel.parts) == 1:
            self._request_reconcile(self._repo_id, gone_rel.name)
            return
        for raw in raw_paths:
            rel = self._rel(raw)
            if rel is not None and len(rel.parts) == 1:
                self._request_reconcile(self._repo_id)
                return
