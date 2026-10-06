# Docs read cache — design

Follow-up to `2026-10-06-docs-frontmatter-keys-design.md`, which left the
`(path, mtime_ns, size)` read cache out of scope (§4 Cost, §7). Upstream epic:
HaydenSchmidtDOC/DevGraph#1.

## Problem

A batch that touches an id-keyed docs type builds a `KeyedView` (K6): one walk
of the repository and one `read_front_matter` of every file any id-keyed type
selects, so ownership is always computed from every file on disk. Reviews
measured about 2.4 s per save at 5,000 such files, almost all of it
`bounded_safe_load`. Nothing is quadratic. The same unchanged files are parsed
again on every save.

## Goal

A save re-parses only the files that changed since the process last parsed
them. The graph, the owner map and every refusal stay exactly what they are
without the cache. A hostile repository cannot use the cache to grow memory
past a fixed bound. There is no new config knob.

## Decisions

| # | Decision |
| --- | --- |
| C1 | **Cache the parsed front matter per file**, not verdicts, claims or owners. |
| C2 | **Validity is the file's full identity**, `(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)` from `os.stat`, plus a 3 s **racy window**. |
| C3 | **One process-wide store**, keyed by `(normalized repo root, repo-relative path)`, with one LRU bound on entries and bytes. Never persisted. |
| C4 | **The store is never aliased.** Every store keeps a private `deepcopy`, and every hit returns a `deepcopy`. |
| C5 | **Only successful parses are cached.** Every refusal and read failure is recomputed each time. |
| C6 | **The batch's own files are read fresh** (`read_fresh`), and **a lookup that does not store evicts the entry**. |
| C7 | **A full scan forgets its repository first.** This also covers a schema change, which is applied by the rescan. |

### C1: what to cache

Front matter depends only on the file's bytes. Verdicts, claims and owners
also depend on the schema and on every other file. A cache of them would need
the schema hash in its key and would still have to recompute owners across
files. The parse is the cost, and conditions, keys and owners over cached
values are cheap pure functions. Caching the parse keeps K6 intact: owners are
still computed from every file, on every batch.

### C2: validity key

An entry records the identity from the `stat` taken just before its read. A
lookup stats the path again and is a hit only if all five fields are equal.
`os.stat` follows symlinks, as `read_bounded`'s `open` does, so the identity is
the target's. The stat function is the module attribute `_stat = os.stat`, so
tests can shim it.

| Case | Caught by |
| --- | --- |
| Ordinary edit | `mtime_ns`, `ctime_ns` (and usually `size`) |
| Atomic save (write a temp file, rename over) | `st_ino` |
| Symlink retargeted | `st_dev`/`st_ino` of the target |
| Same size, mtime restored with `utime` (tools, `git checkout`, `touch -r`) | `ctime_ns` (POSIX only, see below) |
| Same size, same tick, on a coarse-timestamp filesystem (FAT 2 s, HFS+/ext3 1 s) | the racy window |
| Permission change | `ctime_ns` (POSIX only) |
| Schema change | not needed (C1). A full scan forgets anyway (C7). |

**The ctime guarantee is POSIX-only.** On POSIX, any write, `chmod` or `utime`
sets `st_ctime` to now, and it cannot be set back. On Windows, and on FAT or
exFAT, `st_ctime` is the creation time, so it never changes for an
existing file. Windows still has the cache, and there the identity rests on
size, mtime and file index.

**Racy window (as in git).** A miss records `now = _clock()` (`time.time_ns`)
before its first stat. The result is **not stored** when
`now - max(mtime_ns, ctime_ns) < RACY_WINDOW_NS` (3 s), or when that difference
is negative (a timestamp in the future). The `max` matters: an old file
whose mtime was just restored with `utime` has a recent ctime and stays
untrusted. A file written within the last 3 s could be written again within
the same timestamp tick without changing its identity. Such a file is simply
read fresh on every lookup until it ages out of the window. On POSIX, any later
write sets `ctime` to a tick after the stored one, because the stored `ctime`
was at least 3 s old. So the write changes the identity.

**Stat twice.** A miss stats again after the read and stores only if both
stats agree. A file that changed during the read is not stored under either
identity. The second stat is `os.stat` on the path, not `fstat` on the
descriptor that was read. `read_front_matter` wraps `read_bounded` and does
not expose that descriptor, and widening the pure read API for this is not
worth it (see Known limits).

**A failing stat** (`OSError`, such as a missing file or a permission error)
falls through to an uncached `read_front_matter`. It stores nothing and evicts
the entry (C6).

### Known limits

Each limit below can leave a stale entry. It is corrected by the file's own
save event, which reads fresh (C6), or by the next rescan (C7).

- **No change time (Windows, FAT, exFAT).** Two cases are served from the cache when no event is delivered:
  - a permission or ACL change with no content edit, such as a file made unreadable;
  - a same-size content edit whose mtime is then restored with `utime`.
- **The clock.** These cases can leave a stale entry:
  - The wall clock steps backwards. Later writes can then get timestamps equal to a stored entry's.
  - A network filesystem's server clock runs more than 3 s behind this machine. Two same-size writes within one server tick can then look alike.
- **Symlink swap during a read.** The path is swapped to another target between the first stat and the open, then swapped back before the second stat. The content of another file can then be stored under the first target's identity. That content was still read with `read_bounded` and parsed with `bounded_safe_load`, so this is a staleness limit, not a safety one.

### C3: scope, lifetime and bounds

The agent (tray or headless) is one long-lived process. It runs every
registered repository's watcher callbacks, each on its own debounce-timer
thread, plus the schema-rescan thread and the dashboard's registration scans.
The CLI's `add` and `rescan` also fill the store, for their one scan, and the
store goes when the process exits.

The store is one `OrderedDict` per process, keyed by `(root key, rel)`:
- `rel` is the repo-relative POSIX path the caller already holds.
- The root key is `os.path.realpath(repo_root)`, memoised per root. `forget` normalises its argument the same way, so `forget(Path("repo/"))` and `forget(Path("./repo"))` drop the same entries.

One `threading.Lock` guards the dict and the counters. Neither the parse nor
any `deepcopy` runs under the lock. Two threads can both miss on one file and
both parse it. The second store then replaces the first, and both are equal.

One store with one bound is preferred to one store per repository. Per-repo
bounds would multiply the worst case by the number of repositories. A removed
repository's entries are never forgotten explicitly. They stay valid-or-ignored
by identity, are never served for another root, and age out through the LRU.
That is safe and bounded.

The bounds are constants, not config:
- `MAX_ENTRIES = 20_000`;
- `MAX_BYTES = 64 MiB`;
- `MAX_ENTRY_BYTES = 1 MiB`. A larger entry is not stored, so one file cannot flush the store.

An entry's charge is `ENTRY_OVERHEAD` (512 bytes, for the key tuple, the
identity tuple and the dict slot), plus `sys.getsizeof` of its `rel` string
(a hostile repository controls its length), plus the approximate deep size of
its parsed value:
a walk memoised by `id` that sums `sys.getsizeof` over containers, keys and
leaves. A shared `&x` list is therefore charged once, and the walk terminates.
The charge is computed once at store time, outside the lock. The walk is
bounded by `YAML_MAX_NODES` (10,000), because `bounded_safe_load` already caps
the alias-expanded size. Storing evicts from the least recently used end until
both bounds hold, and replacing an entry first subtracts its old charge. A hit
moves its entry to the most recently used end.

`forget(repo_root)` drops one repository's entries. It runs at the start of
`full_scan`, which is also how a schema change is applied (the schema rescan
calls `full_scan`). The apply then refills the store, so the first save after
a rescan is already warm. Nothing is written to disk.

### C4: a hit equals a fresh read, and nothing aliases the store

- A miss and `read_fresh` return the parse they just made, and store a private `deepcopy` of it.
- A hit takes the stored reference under the lock and returns a `deepcopy` made outside it.

The stored object is never handed out, so no caller can mutate it. A copy costs
microseconds per file against hundreds for a parse. `deepcopy` keeps alias
sharing, so a copy has the same shape as a fresh `bounded_safe_load`.

The invariant is that **every read through the cache returns exactly what
`read_front_matter` returns for the file's current bytes**, outside the known
limits. Every graph-level check therefore holds unchanged, and it is
exercised with the cache storing (see the plan):
- `assert_matches_fresh_apply`, whose fresh side is fully uncached;
- the order-independence tests;
- the spy bounds on engine `$paths`.

### C5: what is not cached

These are recomputed on every lookup, and evict any entry:
- `read_bounded` failures: not a regular file, too large, `OSError`;
- invalid YAML;
- non-mapping front matter.

Caching only `(values, None)` keeps the cache from turning a transient failure,
such as a permission error or a file in the middle of a write, into a lasting
one.

### C6: batch files bypass the cache, and misses evict

The watcher has just reported the batch's files as changed, so
`read_fresh(root, rel, path)` reads them without a lookup. It stores the
result when C2 allows it. **Whenever a lookup or `read_fresh` does not store,
it removes the entry**: on a failed read, a refusal, a failed stat, a racy
timestamp, a stat mismatch, or an oversize entry. So a file whose own event
was delivered is never served an older parse afterwards, on any platform.

`stats()` returns `hits`, `misses`, `fresh`, `entries` and `bytes`:
- `hits` and `misses` count `read` lookups;
- `fresh` counts `read_fresh` calls, which are neither hits nor misses.

## Safety

A miss is the existing read, unchanged: `docs.read_front_matter`, called
through the module attribute, so the existing spies on it keep counting real
parses. That means `read_bounded`, `bounded_safe_load` with `YAML_MAX_NODES`
and `raw_keys`, and the same refusals and caps. The cache adds:
- a `stat`;
- a `deepcopy` of a value that a bounded parse already produced;
- an LRU of bounded size.

Nothing from the repository is executed, interpolated or used as anything but
a dictionary key. A hostile repository can make every lookup miss, which
makes the cache no faster than today, and it can fill the store only up to
`MAX_BYTES`.

## Where it plugs in

- **New module** `devgraph/indexer/providers/docs_cache.py`. It holds the stateful store, so `docs.py` stays pure. It exposes:
  - `read(repo_root, rel, path)`;
  - `read_fresh(repo_root, rel, path)`;
  - `forget(repo_root)`;
  - `stats()`.

  Both reads return `read_front_matter`'s tuple.
- **`docs.read_selected(spec, files, *, read=None)`.** `read` is a `(rel, path)` reader and is resolved at call time. With none, the function calls `read_front_matter(files[rel])` through the module global, so monkeypatching `docs.read_front_matter` still works. `dispatch._read_reusing` takes the same reader.
- **Dispatch:**
  - `_keyed_view` reads through `docs_cache.read`, and so do its three users: `_read_docs_batch`, `_relink_docs` and `_take_over_keys`.
  - `_relink_docs` also reads path-keyed files through `read`, so the O(matched files) relink re-read stops re-parsing unchanged files.
  - `_read_docs_batch` reads the batch through `read_fresh`.
  - `_prune_docs` (apply) gains `repo_root` and reads through `read`, after `full_scan` has forgotten the repository.
  - `full_scan` calls `forget` first.
- **Doctor** (`source_report`) never touches `docs_cache`.

## Cost after caching

A warm save at N id-keyed files still runs the walk (`indexable_paths`) and,
per file, `repo_relative`. Each of those calls `Path.resolve()` on the file
and again on the root, a syscall per path component. These calls, plus one
`stat` and one `deepcopy` per file, now dominate the save. The parse of the
batch's own files is a small part of it. This is the expected speed-up, not
the walk's floor.

**Named follow-up:** in `walk.repo_relative` and `_disk_files`, resolve the
root once per walk and resolve only paths that are symlinks.

## Observability

After each `index_paths` and `remove_paths` that touched docs, dispatch logs
`stats()` at DEBUG: "docs read cache: %d hits, %d misses, %d fresh, %d
entries, %d bytes". The counts are process totals. Doctor shows nothing,
because it runs in its own process.

## Docs to update

- README "What it doesn't do yet": the id-keyed per-save line drops "Reads are not cached between saves yet". It says instead:
  - unchanged files are not re-parsed;
  - the walk, and the path resolution for each file, remain and dominate;
  - the first save after the agent starts reads every file once.
- PROJECT_STATUS: replace the read cache in "Still open" with the walk follow-up.
- The front-matter keys addendum §4 Cost and §7: point to this spec.

## Out of scope

- Caching the walk (`indexable_paths`). It is needed to see creates and deletes. The resolve follow-up above is the next cost.
- A persisted cache, or one shared between processes.
- Caching in `source_report`.
