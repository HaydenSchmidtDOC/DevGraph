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
| C2 | **Validity is the file's full identity**: `(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)` from `os.stat`, plus a **racy window**. |
| C3 | **One process-wide store, namespaced by repository root**, with a single LRU bound on entries and bytes. Never persisted. |
| C4 | **A hit returns a deep copy**, equal to and independent of a fresh parse. |
| C5 | **Only successful parses are cached.** Every refusal and read failure is recomputed each time. |
| C6 | **The batch's own files are always read fresh.** The cache serves only the files outside the batch. |
| C7 | **A full scan forgets its repository first.** This also covers a schema change, which is applied by the rescan. |

### C1: what to cache

Front matter depends only on the file's bytes. Verdicts, claims and owners
also depend on the schema and on every other file. A cache of them would need
the schema hash in its key and would still have to recompute owners across
files. The parse is the cost (§Problem), and conditions, keys and owners over
cached values are cheap pure functions. So caching the parse captures almost
all of the speed-up. It also keeps the K6 rule intact: owners are still
computed from every file, on every batch.

### C2: validity key

An entry records the `os.stat` result taken just before its read. A lookup
stats the path again and is a hit only if all five fields are equal. `os.stat`
follows symlinks, as `read_bounded`'s `open` does, so the identity is the
target's.

| Case | Caught by |
| --- | --- |
| Ordinary edit | `mtime_ns`, `ctime_ns` (and usually `size`) |
| Atomic save (write a temp file, rename over) | `st_ino` |
| Symlink retargeted | `st_dev`/`st_ino` of the target |
| Same size, mtime restored with `utime` (tools, `git checkout`, `touch -r`) | `ctime_ns`: any write or `utime` sets it to now, and it cannot be set back |
| Same size, same second, on a coarse-timestamp filesystem (FAT 2 s, HFS+/ext3 1 s) | the racy window |
| Permission change | `ctime_ns` |
| Schema change | not needed (C1). A full scan forgets anyway (C7). |

**Racy window (as in git).** The read records `now = time.time_ns()` before its
`stat`. The result is **not stored** when `now - max(mtime_ns, ctime_ns) < 2 s`
(`RACY_WINDOW_NS`), or when that difference is negative (a timestamp in the
future). A file written within the last two seconds could be written again
within the same timestamp tick without changing its identity. Such a file is
simply read fresh on every lookup until it ages out of the window. A write
after an entry is stored sets `ctime` to a later tick than the stored one,
because the stored `ctime` was at least 2 s old. So the write always changes
the identity.

**Stat twice.** A miss stats again after the read and stores only if both
stats agree. A file that changed during the read is never stored under either
identity.

**Known limit.** On a network filesystem whose server clock runs more than 2 s
behind this machine, two same-size writes within one server tick could look
alike. Such an edit is picked up by that file's own save event, which reads it
fresh (C6), or by the next rescan (C7). Local filesystems are not affected.

### C3: scope, lifetime and bounds

The agent (tray or headless) is one long-lived process. It runs every
registered repository's watcher callbacks, each on its own debounce-timer
thread, plus the schema-rescan thread and the dashboard's registration scans.
The CLI is short-lived, and the cache does nothing there.

The store is one `OrderedDict` per process, keyed by `(repo root, absolute
path)`, behind one `threading.Lock`. Parsing happens outside the lock, so two
threads can both miss on one file and both parse it. The second store then
replaces the first, and both results are equal.

One store with one bound is preferred to one store per repository. Per-repo
stores each with a bound multiply the worst case by the number of
repositories, and keep a removed repository's entries alive for the life of
the process. Namespacing still lets one repository be forgotten on its own.

The bounds are constants, not config:
- `MAX_ENTRIES = 20_000`;
- `MAX_BYTES = 64 MiB`;
- `MAX_ENTRY_BYTES = 1 MiB`. A larger entry is not stored, so one file cannot flush the store.

An entry's charge is the approximate deep size of its parsed value: a walk
memoised by `id` that sums `sys.getsizeof` over containers, keys and leaves. It
is computed once at store time. The walk is bounded by `YAML_MAX_NODES`
(10,000), because `bounded_safe_load` already caps the alias-expanded size.
Storing evicts from the least recently used end until both bounds hold. A hit
moves its entry to the most recently used end.

`forget(repo_root)` drops one repository's entries. It runs at the start of
`full_scan`, which is also how a schema change is applied (the schema rescan
calls `full_scan`). The apply then refills the store, so the first save after
a rescan is already warm. Nothing is written to disk.

### C4: a hit equals a fresh read

A hit returns `copy.deepcopy` of the stored value. Nothing in the provider
mutates front matter today, but the cache cannot rely on that. A copy costs
microseconds per file against hundreds for a parse. `deepcopy` keeps alias
sharing, so the copy has the same shape as a fresh `bounded_safe_load`.

The invariant is that **a cached read returns exactly what
`read_front_matter` returns for the file's current bytes.** Every
graph-level check therefore holds unchanged:
- the "incremental equals fresh apply" helper `assert_matches_fresh_apply`;
- the order-independence tests;
- the spy bounds on engine `$paths`.

### C5: what is not cached

These are recomputed on every lookup:
- `read_bounded` failures: not a regular file, too large, `OSError`;
- invalid YAML;
- non-mapping front matter.

They are rare, cheap except for invalid YAML, and some of them are transient,
such as a permission error or a file in the middle of a write. Caching only
`(values, None)` keeps the cache from ever turning a transient failure into a
lasting one.

### C6: batch files bypass the cache

The watcher has just reported the batch's files as changed, so they are read
fresh. The fresh result then replaces their entry when it may be stored. This
costs |batch| parses. It means the file the user just saved is never served
from the cache, whatever the filesystem's timestamps do.

## Safety

A miss is the existing read, unchanged: `read_bounded`, `bounded_safe_load`
with `YAML_MAX_NODES` and `raw_keys`, and the same refusals and caps. The
cache adds:
- a `stat`;
- a `deepcopy` of a value that a bounded parse already produced;
- an LRU of bounded size.

Nothing from the repository is executed, interpolated or used as anything but
a dictionary key. The path keys are the walk's own absolute paths. A hostile
repository can make every lookup miss, which makes the cache no faster than
today, and it can fill the store only up to `MAX_BYTES`.

## Where it plugs in

- **New module** `devgraph/indexer/providers/docs_cache.py`. It holds the stateful store, so `docs.py` stays pure. It exposes:
  - `read(repo_root, path)`, which is cached;
  - `read_fresh(repo_root, path)`, which reads and then stores when it may;
  - `forget(repo_root)`;
  - `stats()`, which returns hits, misses, entries and bytes.

  Both reads return `read_front_matter`'s tuple.
- **`docs.read_selected`** gains a keyword `read=read_front_matter`, so the pure module keeps its default. `dispatch._read_reusing` takes the same reader.
- **Dispatch:**
  - `_keyed_view` and `_relink_docs` read through `docs_cache.read`. The relink reads path-keyed files too, so its O(matched files) re-read also stops re-parsing unchanged files;
  - `_read_docs_batch` reads the batch through `read_fresh`;
  - `_prune_docs` (apply) reads through `read`, after `full_scan` has forgotten the repository;
  - `full_scan` calls `forget` first.
- **Doctor** (`source_report`) is unchanged. It runs in its own CLI process, where the store is always empty.

## Observability

After each `index_paths` and `remove_paths` that touched docs, dispatch logs
`stats()` at DEBUG: "docs read cache: %d hits, %d misses, %d entries, %d
bytes". The counts are process totals. Doctor shows nothing, because it runs
in a different process and its counts would always be zero.

## Docs to update

- README "What it doesn't do yet": the id-keyed per-save line drops "Reads are not cached between saves yet". It says instead that the walk remains, unchanged files are not re-parsed, and the first save after the agent starts reads them all once.
- PROJECT_STATUS: remove the read cache from "Still open".
- The front-matter keys addendum §4 Cost and §7: point to this spec.

## Out of scope

- Caching the walk (`indexable_paths`). It is cheap next to the parse, and it is needed to see creates and deletes.
- A persisted cache, or one shared between processes.
- Caching in `source_report` and the CLI.
