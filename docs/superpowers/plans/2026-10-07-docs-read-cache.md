# Docs Read Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A save that touches an id-keyed docs type stops re-parsing the front matter of every unchanged file. An in-process cache of parsed front matter, validated by the file's full stat identity and a 2 s racy window, serves the files outside the batch. The graph stays exactly what it is without the cache.

**Spec:** `docs/superpowers/specs/2026-10-07-docs-read-cache-design.md`. Every task implements the decisions it names. The front-matter keys addendum (`2026-10-06-docs-frontmatter-keys-design.md`) and slice 1 still govern everything this reuses.

**Working directory:** this worktree, branch `epic1/docs-read-cache` (from `epic1/docs-frontmatter-keys`). Run `uv sync --extra dev` once, then run `uv run` from inside the worktree, because the editable install otherwise imports another checkout (check `devgraph.__file__`). Live tests use Neo4j at `bolt://127.0.0.1:7687` (`neo4j`/`devgraph-local-dev`), unique repo_ids and cleanup in teardown. Never touch `~/.devgraph`.

## Global Constraints

- **A hit equals a fresh read.** `docs_cache.read(root, path)` returns exactly what `docs.read_front_matter(path)` returns for the file's current bytes, as an object independent of the store (a `deepcopy`). Nothing downstream may tell the difference.
- **Every existing guard applies on a miss, unchanged:**
  - `read_bounded`;
  - `bounded_safe_load(..., YAML_MAX_NODES, raw_keys=True)`;
  - the 4 KiB and int64 caps, and the condition caps;
  - every refusal reason.

  The cache only wraps `read_front_matter`. It never re-implements it.
- **Only `(values, None)` results are stored** (C5). Read failures, invalid YAML and non-mapping front matter are recomputed on every lookup.
- **Validity (C2).** A hit needs all of `(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)` from `os.stat` to equal the stored identity. A miss:
  1. records `time.time_ns()`;
  2. stats;
  3. reads;
  4. stats again.

  It stores only if both stats agree and `now - max(mtime_ns, ctime_ns)` is at least `RACY_WINDOW_NS` (2 s) and not negative.
- **Bounds are constants:** `MAX_ENTRIES = 20_000`, `MAX_BYTES = 64 MiB` and `MAX_ENTRY_BYTES = 1 MiB`. LRU eviction applies across one process-wide store keyed by `(repo root, absolute path)`, behind one `threading.Lock`. Parsing happens outside the lock. There is no config knob, nothing goes to disk, and there is no new dependency.
- **Ownership is still computed from every file on every batch (K6).** The cache changes how front matter is obtained, never which files are considered. Never cache verdicts, claims or owners.
- **The batch's own files are read fresh (C6).** `full_scan` forgets its repository first (C7).
- **Invariant.** After any sequence of events, the docs part of the graph equals a fresh full apply of the same files (`assert_matches_fresh_apply`). Every existing docs test passes unchanged.
- **`docs.py` stays pure.** The store lives in `devgraph/indexer/providers/docs_cache.py`. `read_selected` takes a `read=` keyword whose default is `read_front_matter`.
- **TDD.** Each task starts with failing tests, then the implementation, then `uv run pytest -q` (the full suite).
- **Commits.** Plain imperative messages, with no `Co-Authored-By` trailer and no AI attribution. Never stage `uv.lock`, real names or personal paths.

## Review Focus

1. **Staleness.** Find any path by which a lookup can return front matter that differs from the file's current bytes. Check:
   - the order of the stat, the read and the second stat;
   - the racy comparison: it uses `max(mtime, ctime)`, refuses a negative difference, and uses `<` against the window;
   - that `ctime_ns` and `st_ino` are both in the identity.
2. **Sharing.** A hit hands out a `deepcopy`, never the stored object. Mutating a returned value must not change the next hit.
3. **Bounds.** Every store path enforces both caps and the per-entry cap, including a store that replaces an existing entry, which must subtract the old charge. The deep-size walk is memoised by `id`, so an aliased structure is not counted once per reference into an unbounded loop.
4. **Wiring.** Grep for every `read_front_matter` and `read_selected` call in `dispatch.py`. Check that:
   - batch files use `read_fresh`;
   - the view, the relink and the apply use `read`;
   - `source_report` and the CLI are untouched;
   - `full_scan` calls `forget` before anything reads.
5. **Threads.** Every access to the dict and the counters holds the lock. No parse runs under it.

### Task 1: The cache: identity, racy window, bounds and copies

**Files:**
- `devgraph/indexer/providers/docs_cache.py` (new). It holds:
  - the constants `RACY_WINDOW_NS`, `MAX_ENTRIES`, `MAX_BYTES` and `MAX_ENTRY_BYTES`;
  - `_identity(st)`;
  - `_trusted(st, now_ns)`, the racy rule as a pure function;
  - `_deep_size(value)`;
  - `read(repo_root, path)`, `read_fresh(repo_root, path)`, `forget(repo_root)` and `stats()`;
  - `_clock = time.time_ns`, a module attribute that tests can monkeypatch;
  - a module docstring stating the invariant and the bounds (spec C2–C5).

Tests: `tests/indexer/test_docs_cache.py` (no Neo4j). An autouse fixture forgets every root it uses.

- [ ] Write failing tests:
  - **Hit and miss.** On a file with an old ctime (`_clock` patched to real time + 10 s):
    - two `read`s give equal results;
    - `stats()` shows 1 miss then 1 hit;
    - the second read does not call `bounded_safe_load` (spy).
  - **Same size, same second, mtime restored.** Under the patched clock:
    1. read `id: ADR-0001`;
    2. rewrite it in place as `id: ADR-0002` (same size);
    3. `os.utime(path, ns=(old_atime, old_mtime))`.

    The next `read` returns `ADR-0002`. The `ctime` check catches it.
  - **Same size, same second, real clock.** Write, read, rewrite at the same size and restore the mtime, all within the racy window. The next `read` sees the change, and nothing was stored (`stats()` entries is 0).
  - **Coarse timestamps.** `_trusted` and `_identity` get fake stat results with times truncated to whole seconds (FAT-style 2 s, ext3-style 1 s):
    - `now - max(mtime, ctime)` of 1.999 s is not trusted;
    - 2.0 s is trusted;
    - a negative difference (a future mtime) is not trusted;
    - two identities differing only in `ctime_ns` or `st_ino` are unequal.
  - **Atomic save.** Write a temp file of the same size with the same mtime, then `os.replace` it over the cached file. The next `read` sees the new content (`st_ino`).
  - **Symlink retarget.** A cached symlink is pointed at a second file with the same size and mtime. The next `read` returns the second file's front matter.
  - **Changed during read.** Monkeypatch `docs.read_front_matter` so that it rewrites the file before returning. Nothing is stored, and the next `read` re-reads.
  - **Not cached (C5):**
    - invalid YAML;
    - non-mapping front matter;
    - a FIFO (`is not a regular file`);
    - a file over `MAX_CONFIG_BYTES`;
    - a missing file.

    Each returns the same tuple as `read_front_matter`, and `stats()` entries stays 0.
  - **Copies.** Mutating a value returned by `read` (adding a key, appending to a list value) leaves the next `read` equal to `read_front_matter`. An aliased value (`a: &x [1]`, `b: *x`) keeps `result["a"] is result["b"]`, as a fresh parse does.
  - **Bounds.** With the constants monkeypatched small:
    - storing past `MAX_ENTRIES` evicts the least recently used entry, and a hit refreshes recency;
    - storing past `MAX_BYTES` evicts until the total fits;
    - an entry over `MAX_ENTRY_BYTES` is not stored;
    - replacing an entry keeps `stats()` bytes equal to the sum of the live charges.
  - **Hostile size.** At the real constants, with the clock patched:
    - 21,000 tiny files keep `stats()` entries at or below `MAX_ENTRIES`;
    - 100 files whose front matter is near `YAML_MAX_NODES`, each charged just under `MAX_ENTRY_BYTES`, keep `stats()` bytes at or below `MAX_BYTES`.

    Write the files once in a module-scoped tmp dir.
  - **Namespaces.** `forget(root_a)` drops only root_a's entries.
  - **Threads.** Eight threads `read` 200 shared files concurrently. Every result equals `read_front_matter`, and the stats are consistent (hits + misses = calls, bytes = the sum of charges).
  - **Fuzz (equals fresh).** A seeded loop of 500 random steps over 20 files, with the clock patched forward. Each step is one of:
    - an in-place same-size rewrite with the mtime restored;
    - a size-changing rewrite;
    - an atomic replace;
    - a delete;
    - a recreate;
    - a symlink retarget;
    - a chmod;
    - a no-op.

    After each step, `read(root, p)` equals `docs.read_front_matter(p)` for every file.
- [ ] Implement the module (spec C2–C5).
- [ ] `uv run pytest -q tests/indexer/test_docs_cache.py`, then `uv run pytest -q`. Commit "Add an in-process cache of parsed docs front matter".

### Task 2: Wire the cache into dispatch, with timing and correctness checks

**Files:**
- `devgraph/indexer/providers/docs.py`: `read_selected(spec, files, *, read=read_front_matter)`. The docstring says the reader must return what `read_front_matter` returns.
- `devgraph/indexer/dispatch.py`:
  - `_read_reusing` takes `read`;
  - `_keyed_view` and `_relink_docs` use `partial(docs_cache.read, repo_root)`;
  - `_read_docs_batch` reads the batch through `partial(docs_cache.read_fresh, repo_root)`;
  - `_prune_docs` gains `repo_root` and uses `docs_cache.read`;
  - `full_scan` calls `docs_cache.forget(repo_root)` before `prune_stale_files`;
  - `index_paths` and `remove_paths` log `docs_cache.stats()` at DEBUG after a docs pass.
- `README.md`: in "What it doesn't do yet", the id-keyed per-save line replaces "Reads are not cached between saves yet" with: the walk remains, unchanged files are not re-parsed, and the first save after the agent starts reads them all once.
- `PROJECT_STATUS.md`: remove the read cache from "Still open".
- `docs/superpowers/specs/2026-10-06-docs-frontmatter-keys-design.md`: §4 Cost and §7 point to the new spec.

Tests:
- `tests/indexer/test_docs_provider.py` (unit);
- `tests/indexer/test_docs_provider_live.py`, with the timing test marked by a comment as live and slow-ish (about 2,000 files).

- [ ] Write failing tests:
  - **Unit: the default is unchanged.** `read_selected(spec, files)` with no `read` gives the same result as before. With `read=` a spy, every selected file goes through the spy and no unselected file does.
  - **Unit: the view uses the cache.** The clock is patched forward, and `_keyed_view(root, spec)` runs twice over 50 Adr files. The second call makes 0 `bounded_safe_load` calls (spy).
  - **Live: the batch is fresh.** Spy on `docs_cache.read_fresh` and `docs_cache.read` during `index_paths({adr-0003.md})`. The saved file goes only through `read_fresh`, and every other Adr file only through `read`.
  - **Live timing.** The second save at 2,000 files is at least 5× faster than the first:
    1. Write 2,000 Adr files (`decisions/adr-NNNN.md`, each with about 8 front-matter lines, an `id` and a `supersedes`) plus the schema.
    2. Patch `docs_cache._clock` to real time + 10 s, then run `full_scan`.
    3. Call `docs_cache.forget(root)`.
    4. Time `index_paths({adr-0001.md})` with `time.perf_counter` after a same-length content edit. This is the cold save.
    5. Edit `adr-0002.md` the same way and time `index_paths({adr-0002.md})`. This is the warm save.
    6. Assert `cold >= 5 * warm`.
    7. Count the `bounded_safe_load` calls in the warm save and assert that there is exactly 1, for the batch file.
    8. `assert_matches_fresh_apply`.
  - **Live correctness: a same-second, same-size edit is seen.** With the clock patched forward and `full_scan` done (cache warm):
    1. Edit `decisions/adr-a.md` in place from `id: ADR-0001` to `id: ADR-0002`, which keeps the size the same, and restore its mtime with `os.utime(ns=...)`. No event is delivered for it.
    2. Save `decisions/adr-b.md` (`id: ADR-0002`) with `index_paths({adr-b.md})`.
    3. Assert that the graph's `Adr:ADR-0002` has `path = "decisions/adr-a.md"`. `adr-a.md` now claims it and sorts first, and a stale cache would have kept it at `adr-b.md`.
    4. Deliver `index_paths({adr-a.md})` and run `assert_matches_fresh_apply`.
  - **Live: an atomic save outside the batch.** As above, but replace `adr-a.md` by writing a temp file and calling `os.replace`, keeping the size and mtime. Same assertions.
  - **Live: a schema change.** With the cache warm, change the schema's `where` so that half the Adr files are excluded. Run `full_scan` (the rescan path) and `assert_matches_fresh_apply`. Assert that `forget` was called before the first read (spy order).
  - **Live: every existing docs test passes unchanged.** This includes the keyed-file invariant sequences, which now run with a warm cache because the tests share one process.
- [ ] Implement the wiring and the docs updates (spec "Where it plugs in", "Observability", "Docs to update").
- [ ] `uv run pytest -q tests/indexer`, then `uv run pytest -q`. Commit "Read unchanged docs front matter from the cache on each save".
