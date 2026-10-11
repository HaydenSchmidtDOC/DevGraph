"""A watchdog inotify emitter that keeps ignored folders out of the kernel watch (Linux only).

watchdog opens one inotify instance per scheduled watch, and Linux allows 128
per user by default, shared with every other program that watches files.
`WatcherManager` used to schedule one per top-level folder, so a repository
with about 100 of them used up the limit. On Linux it now watches each
repository with one recursive watch on its root through this emitter, which:

- leaves out folders the indexer skips (by name, `IGNORED_DIR_NAMES`, or by a
  .gitignore), at any depth, when watching starts and when a folder is created
  or moved in, so `.venv`, `node_modules` and build output cost no kernel
  watches;
- watches a folder moved in from outside the watched tree, or from an ignored
  folder (watchdog leaves those unwatched);
- adds watches for folders that are no longer ignored (`add_missing_watches`,
  after a .gitignore change);
- closes the inotify descriptor and its pipe when setting up a watch fails
  (watchdog leaks them, e.g. when the watch limit is hit part-way).

Watches on any other path (the `.git` ones) are not filtered.

It overrides watchdog internals (`Inotify._add_watch`, `_add_dir_watch`,
`_wd_for_path`, `InotifyBuffer.__init__`); watchdog is pinned below 7 in
pyproject.
"""

from __future__ import annotations

import contextlib
import errno
import functools
import logging
import os
from pathlib import Path
from typing import Any, Callable

from watchdog.observers.api import DEFAULT_EMITTER_TIMEOUT, DEFAULT_OBSERVER_TIMEOUT, BaseObserver
from watchdog.observers.inotify import InotifyEmitter
from watchdog.observers.inotify_buffer import InotifyBuffer
from watchdog.observers.inotify_c import DEFAULT_EVENT_BUFFER_SIZE, Inotify, InotifyEvent
from watchdog.utils import BaseThread
from watchdog.utils.delayed_queue import DelayedQueue

from devgraph.indexer.gitignore import is_gitignored
from devgraph.indexer.walk import is_ignored_path

logger = logging.getLogger(__name__)


def ignored_folder_filter(root: Path) -> Callable[[bytes], bool]:
    """Whether a folder under `root` is one the indexer never enters."""

    def ignored(path: bytes) -> bool:
        try:
            rel = Path(os.fsdecode(path)).relative_to(root)
        except ValueError:
            return False
        if not rel.parts:
            return False
        return is_ignored_path(rel) or is_gitignored(root, rel.as_posix(), is_dir=True)

    return ignored


class _FilteredInotify(Inotify):
    """`Inotify` that never puts a folder `skip` accepts under a kernel watch."""

    def __init__(
        self, path: bytes, *, recursive: bool, event_mask: int | None, skip: Callable[[bytes], bool] | None
    ) -> None:
        self._skip = skip
        # Stand-in watch descriptors for skipped folders, below -1 (the
        # kernel's are positive; -1 marks an overflow): see `_add_watch`.
        self._placeholders: list[int] = []
        try:
            super().__init__(path, recursive=recursive, event_mask=event_mask)
        except BaseException:
            for name in ("_inotify_fd", "_kill_r", "_kill_w"):
                fd = getattr(self, name, None)
                if fd is not None:
                    with contextlib.suppress(OSError):
                        os.close(fd)
            raise

    def _skipped(self, path: bytes) -> bool:
        return self._skip is not None and path != self._path and self._skip(path)

    def _add_dir_watch(self, path: bytes, mask: int, *, recursive: bool) -> None:
        if not os.path.isdir(path):
            raise OSError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), path)
        self._add_watch(path, mask)
        if recursive:
            self._add_watches_below(path, mask)

    def _add_watches_below(self, path: bytes, mask: int, *, only_missing: bool = False) -> list[bytes]:
        """Watch every folder below `path` that isn't skipped (or a symlink),
        without entering skipped ones; with `only_missing`, only those not
        watched yet. Returns the folders newly watched."""
        added = []
        for parent, dirnames, _ in os.walk(path):
            kept = []
            for name in dirnames:
                full = os.path.join(parent, name)
                if os.path.islink(full) or self._skipped(full):
                    continue
                kept.append(name)
                if only_missing and full in self._wd_for_path:
                    continue
                self._add_watch(full, mask)
                added.append(full)
            dirnames[:] = kept
        return added

    def _add_watch(self, path: bytes, mask: int) -> int:
        """A skipped folder gets no kernel watch: it gets a placeholder and
        raises OSError. watchdog's `read_events` then skips the walk it makes
        of a created folder, and when the skipped folder is found inside
        such a walk, the placeholder is there for the files it simulates
        below it. Placeholders are dropped as `read_events` returns."""
        if self._skipped(path):
            wd = -2 - len(self._placeholders)
            self._placeholders.append(wd)
            self._wd_for_path[path] = wd
            self._path_for_wd[wd] = path
            raise OSError(errno.EPERM, "folder not watched: ignored", path)
        return super()._add_watch(path, mask)

    def read_events(self, *, event_buffer_size: int = DEFAULT_EVENT_BUFFER_SIZE) -> list[InotifyEvent]:
        events = super().read_events(event_buffer_size=event_buffer_size)
        with self._lock:
            for wd in self._placeholders:
                path = self._path_for_wd.pop(wd, None)
                if path is not None and self._wd_for_path.get(path) == wd:
                    del self._wd_for_path[path]
            self._placeholders.clear()
            if self._skip is None or not self.is_recursive or self._closed:
                return events
            for event in events:
                # A folder moved in from outside the watched tree (or from a
                # skipped folder) has no watch: watchdog only carries over the
                # watches of a folder moved within it.
                if event.is_moved_to and event.is_directory and event.src_path not in self._wd_for_path:
                    if self._skipped(event.src_path):
                        continue
                    try:
                        self._add_dir_watch(event.src_path, self._event_mask, recursive=True)
                    except OSError as e:
                        logger.warning("Couldn't watch %s: %s", os.fsdecode(event.src_path), e)
        return events

    def add_missing_watches(self) -> list[bytes]:
        """Watch the folders that are no longer skipped (a .gitignore
        changed). Returns the folders newly watched."""
        with self._lock:
            if self._closed or self._skip is None:
                return []
            return self._add_watches_below(self._path, self._event_mask, only_missing=True)


class _FilteredInotifyBuffer(InotifyBuffer):
    """`InotifyBuffer` over a `_FilteredInotify`; its __init__, with that one change."""

    def __init__(
        self, path: bytes, *, recursive: bool, event_mask: int | None, skip: Callable[[bytes], bool] | None
    ) -> None:
        BaseThread.__init__(self)
        self._queue = DelayedQueue["InotifyEvent | tuple[InotifyEvent, InotifyEvent]"](self.delay)
        self._inotify = _FilteredInotify(path, recursive=recursive, event_mask=event_mask, skip=skip)
        self.start()


class FilteredInotifyEmitter(InotifyEmitter):
    """`InotifyEmitter` whose watch on `filter_root`, if that is its path,
    leaves out the folders the indexer skips."""

    def __init__(
        self,
        event_queue: Any,
        watch: Any,
        *,
        timeout: float = DEFAULT_EMITTER_TIMEOUT,
        event_filter: Any = None,
        filter_root: Path | None = None,
    ) -> None:
        super().__init__(event_queue, watch, timeout=timeout, event_filter=event_filter)
        self._skip = (
            ignored_folder_filter(filter_root)
            if filter_root is not None and Path(watch.path) == filter_root
            else None
        )

    def on_thread_start(self) -> None:
        self._inotify = _FilteredInotifyBuffer(  # type: ignore[assignment]
            os.fsencode(self.watch.path),
            recursive=self.watch.is_recursive,
            event_mask=self.get_event_mask_from_filter(),
            skip=self._skip,
        )

    def add_missing_watches(self) -> list[Path]:
        """See `_FilteredInotify.add_missing_watches`; nothing once stopped."""
        buffer = self._inotify
        if buffer is None:
            return []
        return [Path(os.fsdecode(p)) for p in buffer._inotify.add_missing_watches()]


def new_observer(filter_root: Path | None) -> BaseObserver:
    """An observer whose watch on `filter_root` skips ignored folders, and
    whose every watch closes what it opened when it can't start."""
    emitter_class = functools.partial(FilteredInotifyEmitter, filter_root=filter_root)
    return BaseObserver(emitter_class, timeout=DEFAULT_OBSERVER_TIMEOUT)  # type: ignore[arg-type]
