"""File and git change watcher for registered repositories.

Watches only paths obtained from RepoRegistry (explicit allowlist, never arbitrary paths).
Debounces events and invokes a callback with the set of changed file paths.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable

from watchdog.events import (
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from devgraph.config import get_settings
from devgraph.indexer.dispatch import is_ignored_path
from devgraph.registry.store import RepoRegistry, RepoRecord

logger = logging.getLogger(__name__)


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

    def on_modified(self, event: FileModifiedEvent) -> None:
        """Record git state change and set debounce timer."""
        if not event.is_directory and _is_relevant_git_state_path(Path(event.src_path)):
            with self._lock:
                self._reset_debounce()

    def on_created(self, event: FileCreatedEvent) -> None:
        """Record git state change and set debounce timer."""
        if not event.is_directory and _is_relevant_git_state_path(Path(event.src_path)):
            with self._lock:
                self._reset_debounce()

    def on_deleted(self, event: FileDeletedEvent) -> None:
        """Record git state change and set debounce timer."""
        if not event.is_directory and _is_relevant_git_state_path(Path(event.src_path)):
            with self._lock:
                self._reset_debounce()

    def on_moved(self, event: FileMovedEvent) -> None:
        """Record git state change and set debounce timer."""
        if event.is_directory:
            return
        if _is_relevant_git_state_path(Path(event.src_path)) or _is_relevant_git_state_path(Path(event.dest_path)):
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
    ) -> None:
        """Initialize the watcher manager.

        Args:
            registry: RepoRegistry instance to read allowed repos from.
            on_changes: Callback(repo_id, changed_paths, deleted_paths)
                invoked when the debounce interval elapses.
            on_git_state_changed: Optional callback(repo_id) invoked when git
                state changes (.git/HEAD, .git/refs, etc.).
        """
        self._registry = registry
        self._on_changes = on_changes
        self._on_git_state_changed = on_git_state_changed
        self._observers: dict[str, Observer] = {}
        self._handlers: dict[str, _RepoEventHandler] = {}
        self._git_handlers: dict[str, _GitStateEventHandler] = {}
        self._debounce_ms = get_settings().watch_debounce_ms
        self._lock = threading.Lock()
        # Track repos with path issues (e.g. missing directory) so they don't
        # crash the whole watcher. Maps repo_id -> error message.
        self._repo_issues: dict[str, str] = {}

    def start(self) -> None:
        """Start watchers for all active, watch-enabled repos.
        
        Repos with invalid paths are logged as warnings and skipped;
        other repos continue normally so one bad path doesn't crash the whole watcher.
        """
        with self._lock:
            repos = self._registry.list_repos(active_only=True)
            repos_to_watch = [r for r in repos if r.watch_enabled]
            for repo in repos_to_watch:
                self._start_single(repo)

    def stop(self) -> None:
        """Stop all watchers and clean up."""
        with self._lock:
            for repo_id, observer in self._observers.items():
                if observer.is_alive():
                    observer.stop()
                    observer.join(timeout=5)
            self._observers.clear()
            self._handlers.clear()

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
            for child in repo.path.iterdir():
                if child.is_dir() and not is_ignored_path(child.relative_to(repo.path)):
                    observer.schedule(handler, str(child), recursive=True)
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
        self._handlers.pop(repo_id, None)
        self._git_handlers.pop(repo_id, None)

        if observer.is_alive():
            observer.stop()
            observer.join(timeout=5)
        logger.debug(f"Stopped watcher for {repo_id}")

    def get_repo_issues(self) -> dict[str, str]:
        """Return a copy of repos with path/watcher issues.
        
        Returns:
            Dict mapping repo_id -> error message for repos that couldn't be watched.
        """
        with self._lock:
            return dict(self._repo_issues)


class _RepoEventHandler(FileSystemEventHandler):
    """Handles file system events for a single repo with debouncing."""

    def __init__(
        self,
        repo_id: str,
        repo_root: Path,
        debounce_ms: int,
        on_changes: Callable[[str, set[Path], set[Path]], None],
    ) -> None:
        self._repo_id = repo_id
        self._repo_root = repo_root
        self._debounce_ms = debounce_ms
        self._on_changes = on_changes
        self._changed_paths: set[Path] = set()
        self._deleted_paths: set[Path] = set()
        self._debounce_timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def on_modified(self, event: FileModifiedEvent) -> None:
        """Record file modification and set debounce timer."""
        if event.is_directory:
            return

        # Track git state changes (.git/HEAD, .git/refs)
        event_path = Path(event.src_path)
        if self._is_tracked_path(event_path):
            with self._lock:
                self._changed_paths.add(event_path)
                self._deleted_paths.discard(event_path)
                self._reset_debounce()

    def on_created(self, event: FileCreatedEvent) -> None:
        """Record file creation and set debounce timer.

        Also covers the "write to a temp file, then create the real path"
        half of an atomic-save sequence some editors/tools use — without
        this, a plain on_modified-only handler misses the file entirely if
        the save never touches an already-existing inode via a modify event.
        """
        if event.is_directory:
            return

        event_path = Path(event.src_path)
        if self._is_tracked_path(event_path):
            with self._lock:
                self._changed_paths.add(event_path)
                self._deleted_paths.discard(event_path)
                self._reset_debounce()

    def on_deleted(self, event: FileDeletedEvent) -> None:
        """Record file deletion and set debounce timer.

        Can't use `_is_tracked_path` here -- its `is_file()` check is always
        False for a path that was just deleted, which would reject every
        real deletion, not just ignored ones. `_is_ignored_repo_path` does
        the same ignored-directory check without requiring the path to
        still exist.
        """
        if event.is_directory:
            return

        event_path = Path(event.src_path)
        if self._is_ignored_repo_path(event_path):
            return
        with self._lock:
            self._deleted_paths.add(event_path)
            self._changed_paths.discard(event_path)
            self._reset_debounce()

    def on_moved(self, event: FileMovedEvent) -> None:
        """Record an atomic-save rename (temp-file -> real path) as a change.

        Some editors/tools save by writing a temp file then renaming it onto
        the real path — watchdog reports that as delete(old) + moved(temp,
        real) rather than a modify on the real path, which an on_modified/
        on_deleted-only handler silently misses: the delete would wrongly
        queue the real path for removal, and nothing would ever queue it as
        changed. Treat the destination as changed and clear any stray delete
        recorded for it in the same debounce window; also treat the source
        path being vacated as no longer deleted (it may have raced in as a
        delete just before this move, e.g. rename onto an existing path).
        """
        if event.is_directory:
            return

        dest_path = Path(event.dest_path)
        src_path = Path(event.src_path)
        with self._lock:
            if self._is_tracked_path(dest_path):
                self._changed_paths.add(dest_path)
                self._deleted_paths.discard(dest_path)
                self._deleted_paths.discard(src_path)
                self._reset_debounce()
            elif not self._is_ignored_repo_path(src_path):
                # Destination isn't a file DevGraph tracks (e.g. moved out of
                # the repo or into a non-file) but the source WAS a real,
                # non-ignored path -- treat it like a deletion of the
                # original path. (If the source is also ignored -- e.g. a
                # lockfile-then-rename inside .git/ -- nothing real happened
                # on either end, so skip it entirely rather than record a
                # phantom deletion of an ignored path.)
                self._deleted_paths.add(src_path)
                self._changed_paths.discard(src_path)
                self._reset_debounce()

    def _is_tracked_path(self, path: Path) -> bool:
        """Check if this path should be tracked.

        Tracks regular files, except those under an ignored directory
        (.venv, __pycache__, build, ...) that nests inside an otherwise-
        watched top-level directory -- the per-child exclusion in
        WatcherManager._start_single only keeps top-level ignored dirs out
        of the OS watch, so this is the backstop for ignored dirs deeper in
        the tree (e.g. some_package/build/).
        """
        try:
            if not path.is_file():
                return False
        except OSError:
            return False
        return not self._is_ignored_repo_path(path)

    def _is_ignored_repo_path(self, path: Path) -> bool:
        """Shape-only ignored-directory check -- no existence check, so this
        is safe to call on a path that no longer exists (a deletion) or
        whose existence hasn't settled yet, unlike `_is_tracked_path`."""
        try:
            rel = path.resolve().relative_to(self._repo_root.resolve())
        except (OSError, ValueError):
            return False
        return is_ignored_path(rel)

    def _reset_debounce(self) -> None:
        """Reset the debounce timer. Must hold _lock."""
        if self._debounce_timer:
            self._debounce_timer.cancel()

        self._debounce_timer = threading.Timer(
            self._debounce_ms / 1000.0,
            self._fire_changes,
        )
        self._debounce_timer.daemon = True
        self._debounce_timer.start()

    def _fire_changes(self) -> None:
        """Invoke the callback with collected changes and deletions."""
        with self._lock:
            if self._changed_paths or self._deleted_paths:
                changed_copy = self._changed_paths.copy()
                deleted_copy = self._deleted_paths.copy()
                self._changed_paths.clear()
                self._deleted_paths.clear()
                self._debounce_timer = None
                # Invoke callback outside lock to avoid deadlock
                self._on_changes(self._repo_id, changed_copy, deleted_copy)
