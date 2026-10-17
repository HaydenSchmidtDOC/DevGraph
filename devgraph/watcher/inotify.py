"""A watchdog inotify emitter that keeps ignored folders out of the kernel watch (Linux only).

watchdog opens one inotify instance per scheduled watch, and Linux allows 128
per user by default, shared with every other program that watches files.
`WatcherManager` used to schedule one per top-level folder, so a repository
with about 100 of them used up the limit. On Linux it now watches each
repository with one recursive watch on its root through this emitter, which:

- leaves out folders the indexer skips (`folder_filter`: by name or place, or by a
  .gitignore), at any depth, when watching starts and when a folder is created
  or moved in, so `.venv`, `node_modules` and build output cost no kernel
  watches;
- watches a folder moved in from outside the watched tree, or from an ignored
  folder (watchdog leaves those unwatched);
- adds watches for folders that are no longer ignored (`add_missing_watches`,
  after a .gitignore change); a folder that becomes ignored keeps its watch
  until the agent restarts (its events are dropped by the handler);
- never watches a symlinked folder below the root (inotify follows links);
- closes the inotify descriptor and its pipe when setting up a watch fails
  (watchdog leaks them, e.g. when the watch limit is hit part-way).

With `watch_git`, the repository's git state (`.git`, `.git/refs`,
`.git/refs/heads` and below) is watched on the same instance, so a
repository costs one inotify instance. Watches on any other path are not
filtered.

It overrides watchdog internals (`Inotify._add_watch`, `_add_dir_watch`,
`_wd_for_path`, `_closed`, `InotifyBuffer.__init__`); watchdog is pinned to
6.x in pyproject, and `SUPPORTED` checks they are there at import (without
them the watcher uses per-folder watches). An exception in watchdog's reader
is reported to the watcher, which records it as a repository issue.
"""

from __future__ import annotations

import contextlib
import errno
import functools
import logging
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable

from watchdog.observers.api import DEFAULT_EMITTER_TIMEOUT, DEFAULT_OBSERVER_TIMEOUT, BaseObserver
from watchdog.observers.inotify import InotifyEmitter
from watchdog.observers.inotify_buffer import InotifyBuffer
from watchdog.observers.inotify_c import DEFAULT_EVENT_BUFFER_SIZE, Inotify, InotifyConstants, InotifyEvent
from watchdog.utils import BaseThread
from watchdog.utils.delayed_queue import DelayedQueue

from devgraph.watcher.folder_filter import ignored_folder_filter

logger = logging.getLogger(__name__)

#: How the emitter tells the watcher about a problem: (kind, error). Kinds:
#: "broken" (watchdog's reader raised; its thread ends, so nothing is watched
#: from then on), "overflow" (the kernel's queue overflowed: events were
#: dropped) and "limit" (a folder found after the watch started can't be
#: watched: ENOSPC or EMFILE, the inotify limits).
Report = Callable[[str, BaseException], None]


def supports(inotify_cls: Any, buffer_cls: Any) -> bool:
    """Whether watchdog has the internals this module overrides. 5.0.2, for
    one, has no `Inotify._closed`; the root watch's emitter would kill its
    reader thread there, so the watcher falls back to per-folder watches."""
    try:
        names = set(inotify_cls.__init__.__code__.co_names)
    except AttributeError:
        return False
    methods = ("_add_dir_watch", "_add_watch", "read_events", "_parse_event_buffer")
    return (
        {"_closed", "_wd_for_path", "_path_for_wd", "_path"} <= names
        and all(callable(getattr(inotify_cls, name, None)) for name in methods)
        and isinstance(getattr(buffer_cls, "delay", None), (int, float))
    )


#: Whether the installed watchdog can carry the root watch (see `supports`).
SUPPORTED = supports(Inotify, InotifyBuffer)

#: The `_FilteredInotify` whose `read_events` runs on this thread.
_reading = threading.local()


def _parse_event_buffer(event_buffer: bytes) -> Iterator[tuple[int, int, int, bytes]]:
    """watchdog's parser, noting an overflow for the `_FilteredInotify`
    reading on this thread: watchdog's `read_events` skips the overflow
    event (wd -1) without a trace. Installed on `Inotify` itself, since
    `read_events` calls it by class; to anything else it is the original."""
    for wd, mask, cookie, name in _watchdog_parse_event_buffer(event_buffer):
        if wd == -1 and mask & InotifyConstants.IN_Q_OVERFLOW:
            inotify = getattr(_reading, "inotify", None)
            if inotify is not None:
                inotify._overflowed = True
        yield wd, mask, cookie, name


if SUPPORTED:
    _watchdog_parse_event_buffer = Inotify._parse_event_buffer
    Inotify._parse_event_buffer = staticmethod(_parse_event_buffer)  # type: ignore[method-assign]


class _FilteredInotify(Inotify):
    """`Inotify` that never puts a folder `skip` accepts under a kernel watch."""

    def __init__(
        self,
        path: bytes,
        *,
        recursive: bool,
        event_mask: int | None,
        skip: Callable[[bytes], bool] | None,
        report: Report | None = None,
    ) -> None:
        self._skip = skip
        self._report = report
        self._overflowed = False
        self._started = False
        # Stand-in watch descriptors for skipped folders, below -1 (the
        # kernel's are positive; -1 marks an overflow): see `_add_watch`.
        self._placeholders: list[int] = []
        try:
            super().__init__(path, recursive=recursive, event_mask=event_mask)
            self._started = True
        except BaseException:
            for name in ("_inotify_fd", "_kill_r", "_kill_w"):
                fd = getattr(self, name, None)
                if fd is not None:
                    with contextlib.suppress(OSError):
                        os.close(fd)
            raise

    def _skipped(self, path: bytes) -> bool:
        """A folder below the root to keep unwatched: one `skip` accepts, or
        a symlink (inotify would follow it, perhaps out of the repository)."""
        if self._skip is None or path == self._path:
            return False
        return os.path.islink(path) or self._skip(path)

    def _add_dir_watch(self, path: bytes, mask: int, *, recursive: bool) -> None:
        if not os.path.isdir(path):
            raise OSError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), path)
        self._add_watch(path, mask)
        if recursive:
            self._add_watches_below(path, mask)

    def _add_watches_below(
        self, path: bytes, mask: int, *, only_missing: bool = False, lock: Any = None
    ) -> list[bytes]:
        """Watch every folder below `path` that isn't skipped (or a symlink),
        without entering skipped ones; with `only_missing`, only those not
        watched yet. With `lock`, the walk runs outside it and each watch is
        added under it. Returns the folders newly watched."""
        added = []
        for parent, dirnames, _ in os.walk(path):
            kept = []
            for name in dirnames:
                full = os.path.join(parent, name)
                if os.path.islink(full) or self._skipped(full):
                    continue
                kept.append(name)
                with lock if lock is not None else contextlib.nullcontext():
                    if lock is not None and self._closed:
                        return added
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
        try:
            return super()._add_watch(path, mask)
        except OSError as e:
            # Past the start (whose failure the watcher reports itself), a
            # limit reached leaves this folder unwatched until a restart.
            if self._started and e.errno in (errno.ENOSPC, errno.EMFILE) and self._report is not None:
                self._report("limit", e)
            raise

    def read_events(self, *, event_buffer_size: int = DEFAULT_EVENT_BUFFER_SIZE) -> list[InotifyEvent]:
        """watchdog's, plus the placeholder cleanup and moved-in folders
        below. An exception ends the reader thread, so it is reported first:
        otherwise watching would stop without a word."""
        try:
            return self._read_events(event_buffer_size)
        except Exception as e:
            if self._report is not None:
                self._report("broken", e)
            raise

    def _read_events(self, event_buffer_size: int) -> list[InotifyEvent]:
        _reading.inotify = self
        try:
            events = super().read_events(event_buffer_size=event_buffer_size)
        finally:
            _reading.inotify = None
        if self._overflowed:
            self._overflowed = False
            if self._report is not None:
                self._report("overflow", OSError(errno.EOVERFLOW, "inotify event queue overflowed"))
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
        if self._closed or self._skip is None:
            return []
        # The walk (and the .gitignore checks) run outside the lock, which
        # the reader thread needs for every event.
        return self._add_watches_below(self._path, self._event_mask, only_missing=True, lock=self._lock)


class _FilteredInotifyBuffer(InotifyBuffer):
    """`InotifyBuffer` over a `_FilteredInotify`; its __init__, with that one change."""

    def __init__(
        self,
        path: bytes,
        *,
        recursive: bool,
        event_mask: int | None,
        skip: Callable[[bytes], bool] | None,
        report: Report | None,
    ) -> None:
        BaseThread.__init__(self)
        self._queue = DelayedQueue["InotifyEvent | tuple[InotifyEvent, InotifyEvent]"](self.delay)
        self._inotify = _FilteredInotify(path, recursive=recursive, event_mask=event_mask, skip=skip, report=report)
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
        watch_git: bool = False,
        report: Report | None = None,
    ) -> None:
        super().__init__(event_queue, watch, timeout=timeout, event_filter=event_filter)
        self._report = report
        self._skip = (
            ignored_folder_filter(filter_root, watch_git=watch_git)
            if filter_root is not None and Path(watch.path) == filter_root
            else None
        )

    def on_thread_start(self) -> None:
        self._inotify = _FilteredInotifyBuffer(  # type: ignore[assignment]
            os.fsencode(self.watch.path),
            recursive=self.watch.is_recursive,
            event_mask=self.get_event_mask_from_filter(),
            skip=self._skip,
            report=self._report,
        )

    def add_missing_watches(self) -> list[Path]:
        """See `_FilteredInotify.add_missing_watches`; nothing once stopped."""
        buffer = self._inotify
        if buffer is None:
            return []
        return [Path(os.fsdecode(p)) for p in buffer._inotify.add_missing_watches()]


def new_observer(filter_root: Path | None, *, watch_git: bool = False, report: Report | None = None) -> BaseObserver:
    """An observer whose watch on `filter_root` skips ignored folders (but,
    with `watch_git`, keeps the git state), whose every watch closes what it
    opened when it can't start, and which tells `report` about problems."""
    emitter_class = functools.partial(
        FilteredInotifyEmitter, filter_root=filter_root, watch_git=watch_git, report=report
    )
    return BaseObserver(emitter_class, timeout=DEFAULT_OBSERVER_TIMEOUT)  # type: ignore[arg-type]
