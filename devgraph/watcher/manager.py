"""File and git change watcher for registered repositories.

Watches only paths obtained from RepoRegistry (explicit allowlist, never arbitrary paths).
Debounces events and invokes a callback with the set of changed file paths.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterable
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
    """Handles git state changes (.git/HEAD, .git/refs) with debouncing."""

    def __init__(
        self,
        repo_id: str,
        debounce_ms: int,
        on_git_state_changed: Callable[[str], None],
    ) -> None:
        self._repo_id = repo_id
        self._debounce_ms = debounce_ms
        self._on_git_state_changed = on_git_state_changed
        self._debounce_timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def on_modified(self, event: FileModifiedEvent) -> None:  # type: ignore[override]
        """Record git state change and set debounce timer."""
        if not event.is_directory and _is_relevant_git_state_path(Path(str(event.src_path))):
            with self._lock:
                self._reset_debounce()

    def on_created(self, event: FileCreatedEvent) -> None:  # type: ignore[override]
        """Record git state change and set debounce timer."""
        if not event.is_directory and _is_relevant_git_state_path(Path(str(event.src_path))):
            with self._lock:
                self._reset_debounce()

    def on_deleted(self, event: FileDeletedEvent) -> None:  # type: ignore[override]
        """Record git state change and set debounce timer."""
        if not event.is_directory and _is_relevant_git_state_path(Path(str(event.src_path))):
            with self._lock:
                self._reset_debounce()

    def on_moved(self, event: FileMovedEvent) -> None:  # type: ignore[override]
        """Record git state change and set debounce timer."""
        if event.is_directory:
            return
        if _is_relevant_git_state_path(Path(str(event.src_path))) or _is_relevant_git_state_path(Path(str(event.dest_path))):
            with self._lock:
                self._reset_debounce()

    def _reset_debounce(self) -> None:
        """Reset the debounce timer. Must hold _lock."""
        if self._debounce_timer:
            self._debounce_timer.cancel()

        self._debounce_timer = threading.Timer(
            self._debounce_ms / 1000.0,
            self._fire_change,
        )
        self._debounce_timer.daemon = True
        self._debounce_timer.start()

    def _fire_change(self) -> None:
        """Invoke the callback with the repo_id."""
        with self._lock:
            self._debounce_timer = None
        # Invoke callback outside lock to avoid deadlock
        self._on_git_state_changed(self._repo_id)


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
        self._stopping = False

    def start(self) -> None:
        """Start watchers for all active, watch-enabled repos.
        
        Repos with invalid paths are logged as warnings and skipped;
        other repos continue normally so one bad path doesn't crash the whole watcher.
        """
        with self._reconcile_lock:
            self._stopping = False
        with self._lock:
            repos = self._registry.list_repos(active_only=True)
            repos_to_watch = [r for r in repos if r.watch_enabled]
            for repo in repos_to_watch:
                self._start_single(repo)

    def stop(self) -> None:
        """Stop all watchers and clean up.

        Pending reconciles are cancelled, and one that is running finishes
        without queueing anything. Pending debounces are cancelled; the
        changes they held are left for the next start's catch-up.
        """
        with self._reconcile_lock:
            self._stopping = True
            timers = list(self._reconcile_pending.values())
            self._reconcile_pending.clear()
        for timer in timers:
            timer.cancel()
        with self._lock:
            for repo_id, observer in self._observers.items():
                if observer.is_alive():
                    observer.stop()
                    observer.join(timeout=5)
            for handler in self._handlers.values():
                handler.cancel()
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
        if git_dir.is_dir() and self._on_git_state_changed:
            git_handler = _GitStateEventHandler(
                repo.repo_id,
                self._debounce_ms,
                self._on_git_state_changed,
            )
            # Non-recursive watch on .git itself (catches HEAD, packed-refs)
            observer.schedule(git_handler, str(git_dir), recursive=False)
            # Recursive watch on .git/refs/heads if it exists
            refs_heads = git_dir / "refs" / "heads"
            if refs_heads.is_dir():
                observer.schedule(git_handler, str(refs_heads), recursive=True)
            self._git_handlers[repo.repo_id] = git_handler

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
        if timer is not None:
            timer.cancel()
        if handler is not None:
            handler.cancel()

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

    def _request_reconcile(self, repo_id: str) -> None:
        """Ask for a top-level watch reconcile (W3). Called from handler code
        on watchdog's dispatch thread, so it takes only `_reconcile_lock`;
        requests coalesce until the timer fires."""
        with self._reconcile_lock:
            if self._stopping or repo_id in self._reconcile_pending:
                return
            timer = self._timer_factory(self._reconcile_delay_s, lambda: self._reconcile(repo_id))
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
                        and self._dir_identity(path) == identity
                    ):
                        continue
                    if emitter is not None:
                        observer.unschedule(watch)
                    del watches[path]
                added = []
                for path in sorted(desired - watches.keys()):
                    try:
                        watches[path] = self._schedule_dir(observer, handler, path)
                    except OSError as e:
                        logger.debug("Couldn't watch %s: %s", path, e)
                        continue
                    added.append(path)
            files: set[Path] = set()
            for path in added:
                files |= indexable_paths_under(root, path)
            with self._reconcile_lock:
                if files and not self._stopping:
                    handler.queue_changed(files)
        finally:
            with self._reconcile_lock:
                self._reconcile_running.discard(repo_id)

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
        request_reconcile: Callable[[str], None] | None = None,
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
        self._signal_top_level(str(event.src_path))

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
            changed = self._walk_dir(dest) if dest is not None else set()
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
        self._signal_top_level(src_raw, dest_raw)

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
                if not self._changed and not self._deleted:
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

    def _signal_top_level(self, *raw_paths: str) -> None:
        """Ask the manager to reconcile watches when an event touches a direct
        child of the root. Called with `_lock` released."""
        if self._request_reconcile is None:
            return
        for raw in raw_paths:
            rel = self._rel(raw)
            if rel is not None and len(rel.parts) == 1:
                self._request_reconcile(self._repo_id)
                return
