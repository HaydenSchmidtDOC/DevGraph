"""An in-process cache of parsed docs front matter, so a save re-parses only changed files.

The invariant: every read through this cache returns exactly what
`docs.read_front_matter(path)` returns for the file's current bytes, outside
the spec's known limits (`docs/superpowers/specs/2026-10-07-docs-read-cache-design.md`).

- **Identity (C2).** An entry is valid while `(st_dev, st_ino, st_size,
  st_mtime_ns, st_ctime_ns)` from `os.stat` (following symlinks) is unchanged.
  A miss takes the clock, stats, reads, and stats again; it stores only if
  both stats agree and the file is outside the 3 s racy window, measured from
  `max(mtime, ctime)`. The ctime guarantee is POSIX-only: there any write,
  `chmod` or `utime` sets ctime to now. On Windows, FAT and exFAT ctime is the
  creation time, so the identity rests on size, mtime and file index.
- **Bounds (C3).** One process-wide LRU keyed by `(realpath(repo root), rel)`,
  capped at `MAX_ENTRIES` entries and `MAX_BYTES` bytes. An entry is charged
  `ENTRY_OVERHEAD`, the size of its `rel` string and the approximate deep size
  of its value; one over `MAX_ENTRY_BYTES` is not stored. Nothing is persisted.
- **Copies (C4).** The store keeps a private `deepcopy` and every hit returns
  a `deepcopy`, so no caller can mutate a stored value. No parse, copy or
  size walk runs under `_lock`.
- **What is stored (C5, C6).** Only `(values, None)`. Every lookup or
  `read_fresh` that does not store evicts the entry: a failed stat or read, a
  refusal, a racy timestamp, a stat mismatch or an oversize entry.

A miss is `docs.read_front_matter`, called through the module attribute, so
every bound and refusal of the uncached read applies unchanged.
"""

from __future__ import annotations

import functools
import os
import sys
import threading
import time
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from typing import Any, NamedTuple

from devgraph.indexer.providers import docs

RACY_WINDOW_NS = 3_000_000_000
MAX_ENTRIES = 20_000
MAX_BYTES = 64 * 1024 * 1024
MAX_ENTRY_BYTES = 1024 * 1024
#: Charged per entry for its key tuple, identity tuple and dict slot (the `rel` string is charged on top).
ENTRY_OVERHEAD = 512

_clock = time.time_ns
_stat = os.stat
_lock = threading.Lock()


class _Entry(NamedTuple):
    identity: tuple[int, int, int, int, int]
    value: dict[Any, Any]
    charge: int


_store: OrderedDict[tuple[str, str], _Entry] = OrderedDict()
_bytes = 0
_hits = 0
_misses = 0
_fresh = 0


def _identity(st: Any) -> tuple[int, int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _trusted(st: Any, now_ns: int) -> bool:
    """Whether a file stat'd at `now_ns` is old enough that a later write must change its identity."""
    age = now_ns - max(st.st_mtime_ns, st.st_ctime_ns)
    return age >= RACY_WINDOW_NS


def _deep_size(value: Any) -> int:
    """`sys.getsizeof` summed over containers, keys and leaves, each object counted once."""
    seen: set[int] = set()
    total = 0
    stack = [value]
    while stack:
        item = stack.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        total += sys.getsizeof(item)
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            stack.extend(item)
    return total


def _root_key(root: Path) -> str:
    """`os.path.realpath(root)`, memoised on the absolute path so a cwd change can't alias roots.

    A memo gone stale because a root symlink was repointed is safe: validity is
    each file's own identity, so entries are never served for the wrong file,
    and the memo is bounded at 256 roots.
    """
    return _realpath(os.path.abspath(root))


@functools.lru_cache(maxsize=256)
def _realpath(absolute: str) -> str:
    return os.path.realpath(absolute)


def _evict(key: tuple[str, str]) -> None:
    global _bytes
    with _lock:
        entry = _store.pop(key, None)
        if entry is not None:
            _bytes -= entry.charge


def _load(key: tuple[str, str], path: Path, now: int, before: Any) -> tuple[dict[Any, Any] | None, str | None]:
    """Parse `path` and store the result if C2 allows it; otherwise evict `key`."""
    global _bytes
    result = docs.read_front_matter(path)
    values = result[0]
    if values is None or before is None or not _trusted(before, now):
        _evict(key)
        return result
    try:
        after = _stat(path)
    except OSError:
        _evict(key)
        return result
    identity = _identity(before)
    if _identity(after) != identity:
        _evict(key)
        return result
    stored = deepcopy(values)
    charge = ENTRY_OVERHEAD + sys.getsizeof(key[1]) + _deep_size(stored)
    if charge > MAX_ENTRY_BYTES:
        _evict(key)
        return result
    with _lock:
        old = _store.pop(key, None)
        if old is not None:
            _bytes -= old.charge
        _store[key] = _Entry(identity, stored, charge)
        _bytes += charge
        while len(_store) > MAX_ENTRIES or _bytes > MAX_BYTES:
            _, dropped = _store.popitem(last=False)
            _bytes -= dropped.charge
    return result


def _stat_or_none(path: Path) -> Any:
    try:
        return _stat(path)
    except OSError:
        return None


def read(repo_root: Path, rel: str, path: Path) -> tuple[dict[Any, Any] | None, str | None]:
    """`docs.read_front_matter(path)`, served from the cache while the file's identity is unchanged."""
    global _hits, _misses
    key = (_root_key(repo_root), rel)
    now = _clock()
    st = _stat_or_none(path)
    value = None
    with _lock:
        entry = _store.get(key) if st is not None else None
        if entry is not None and entry.identity == _identity(st):
            _store.move_to_end(key)
            _hits += 1
            value = entry.value
        else:
            _misses += 1
    if value is not None:
        return deepcopy(value), None
    return _load(key, path, now, st)


def read_fresh(repo_root: Path, rel: str, path: Path) -> tuple[dict[Any, Any] | None, str | None]:
    """`docs.read_front_matter(path)` without a lookup, for a file just reported changed; stores or evicts."""
    global _fresh
    key = (_root_key(repo_root), rel)
    now = _clock()
    with _lock:
        _fresh += 1
    return _load(key, path, now, _stat_or_none(path))


def forget(repo_root: Path) -> None:
    """Drop every entry for one repository."""
    global _bytes
    root = _root_key(repo_root)
    with _lock:
        for key in [k for k in _store if k[0] == root]:
            _bytes -= _store.pop(key).charge


def stats() -> dict[str, int]:
    """Process totals: `read` hits and misses, `read_fresh` calls, live entries and their charged bytes."""
    with _lock:
        return {"hits": _hits, "misses": _misses, "fresh": _fresh, "entries": len(_store), "bytes": _bytes}
