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
from devgraph.registry.store import RepoRegistry, RepoRecord

logger = logging.getLogger(__name__)


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
    ) -> None:
        """Initialize the watcher manager.

        Args:
            registry: RepoRegistry instance to read allowed repos from.
            on_changes: Callback(repo_id, changed_paths, deleted_paths)
                invoked when the debounce interval elapses.
        """
        self._registry = registry
        self._on_changes = on_changes
        self._observers: dict[str, Observer] = {}
        self._handlers: dict[str, _RepoEventHandler] = {}
        self._debounce_ms = get_settings().watch_debounce_ms
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start watchers for all active, watch-enabled repos."""
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
        """
        with self._lock:
            # Get current state
            current_ids = set(self._observers.keys())
            repos = self._registry.list_repos(active_only=True)
            desired_ids = {r.repo_id for r in repos if r.watch_enabled}

            # Stop watchers for repos that are no longer active/watch-enabled
            for repo_id in current_ids - desired_ids:
                self._stop_single(repo_id)

            # Start watchers for new repos
            for repo in repos:
                if repo.repo_id in (desired_ids - current_ids):
                    self._start_single(repo)

    def _start_single(self, repo: RepoRecord) -> None:
        """Start a watcher for a single repo. Must hold _lock."""
        if repo.repo_id in self._observers:
            return  # Already watching

        handler = _RepoEventHandler(
            repo.repo_id,
            repo.path,
            self._debounce_ms,
            self._on_changes,
        )
        observer = Observer()
        observer.schedule(handler, str(repo.path), recursive=True)
        observer.start()

        self._observers[repo.repo_id] = observer
        self._handlers[repo.repo_id] = handler
        logger.debug(f"Started watcher for {repo.repo_id} at {repo.path}")

    def _stop_single(self, repo_id: str) -> None:
        """Stop a watcher for a single repo. Must hold _lock."""
        if repo_id not in self._observers:
            return

        observer = self._observers.pop(repo_id)
        self._handlers.pop(repo_id, None)

        if observer.is_alive():
            observer.stop()
            observer.join(timeout=5)
        logger.debug(f"Stopped watcher for {repo_id}")


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
        """Record file deletion and set debounce timer."""
        if event.is_directory:
            return

        event_path = Path(event.src_path)
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
            else:
                # Destination isn't a file DevGraph tracks (e.g. moved out of
                # the repo or into a non-file); treat it like a deletion of
                # the original path.
                self._deleted_paths.add(src_path)
                self._changed_paths.discard(src_path)
            self._reset_debounce()

    def _is_tracked_path(self, path: Path) -> bool:
        """Check if this path should be tracked.

        Tracks regular files and git state indicators (.git/HEAD, .git/refs/...).
        """
        try:
            # Always track regular files
            if path.is_file():
                return True
        except OSError:
            pass
        return False

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
