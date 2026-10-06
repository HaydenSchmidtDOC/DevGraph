# Docs Read Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A save that touches an id-keyed docs type stops re-parsing the front matter of every unchanged file. An in-process cache of parsed front matter serves the files outside the batch. It is validated by the file's full stat identity and a 3 s racy window. The graph stays exactly what it is without the cache.

**Spec:** `docs/superpowers/specs/2026-10-07-docs-read-cache-design.md`. Every task implements the decisions it names. The front-matter keys addendum (`2026-10-06-docs-frontmatter-keys-design.md`) and slice 1 still govern everything this reuses.

**Working directory:** this worktree, branch `epic1/docs-read-cache` (from `epic1/docs-frontmatter-keys`). Run `uv sync --extra dev` once, then run `uv run` from inside the worktree, because the editable install otherwise imports another checkout (check `devgraph.__file__`). Live tests use Neo4j at `bolt://127.0.0.1:7687` (`neo4j`/`devgraph-local-dev`), unique repo_ids and cleanup in teardown. Never touch `~/.devgraph`.

## Global Constraints

- **Every read equals a fresh read.** `docs_cache.read` and `docs_cache.read_fresh` return exactly what `docs.read_front_matter(path)` returns for the file's current bytes, outside the spec's Known limits.
- **The store is never aliased (C4).**
  - A miss and `read_fresh` return their own parse and store a private `deepcopy`.
  - A hit returns a `deepcopy`.
  - No `deepcopy`, deep-size walk or parse runs under `_lock`.
- **A miss goes through `docs.read_front_matter`, called through the module attribute** (`docs.read_front_matter(path)`, never a name imported into `docs_cache`). Existing spies therefore count real parses: `test_docs_provider.py` around lines 412–415 and 896, and the live `reads` fixture. Every guard applies on a miss, unchanged:
  - `read_bounded`;
  - `bounded_safe_load(..., YAML_MAX_NODES, raw_keys=True)`;
  - every cap and refusal reason.
- **Only `(values, None)` is stored (C5).** **Any lookup or `read_fresh` that does not store evicts the entry (C6).** This covers a failed stat, a failed read, a refusal, a racy timestamp, a stat mismatch, and an entry over the size cap.
- **Validity (C2).** A hit needs all of `(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)` from `_stat` (module attribute, `os.stat`) to equal the stored identity. A miss:
  1. takes `now = _clock()`;
  2. calls `_stat`;
  3. reads;
  4. calls `_stat` again.

  It stores only if both stats agree, `0 <= now - max(mtime_ns, ctime_ns)`, and that difference is at least `RACY_WINDOW_NS` (3 s). An `OSError` from `_stat` falls through to an uncached read.
- **Keys and bounds (C3).**
  - Entries are keyed by `(realpath(repo_root), rel)`, and `forget` normalises the same way.
  - `MAX_ENTRIES = 20_000`, `MAX_BYTES = 64 MiB` and `MAX_ENTRY_BYTES = 1 MiB`.
  - Each entry is charged `ENTRY_OVERHEAD` (512) plus the deep size of its value, memoised by `id`.
  - LRU eviction runs across one process-wide `OrderedDict` behind one `threading.Lock`.

  There is no config knob, nothing goes to disk, and there is no new dependency.
- **Ownership is still computed from every file on every batch (K6).** The cache changes how front matter is obtained, never which files are considered. Never cache verdicts, claims or owners.
- **`docs.py` stays pure.** `read_selected(spec, files, *, read=None)` resolves `read or (lambda rel, path: read_front_matter(path))` at call time, through the module global.
- **Invariant, with the cache storing.** After any sequence of events, the docs part of the graph equals a fresh full apply of the same files. The keyed live sequences run with the cache on and with it off. `assert_matches_fresh_apply`'s fresh side is fully uncached, and its `full_scan` does not `forget` the root under test.
- **Platforms.** Tests that rely on `ctime` changing are skipped when `sys.platform == "win32"`, with the reason "no change time; see spec Known limits". Every other test runs everywhere.
- **TDD.** Each task starts with failing tests, then the implementation, then `uv run pytest -q` (the full suite).
- **Commits.** Plain imperative messages, with no `Co-Authored-By` trailer and no AI attribution. Never stage `uv.lock`, real names or personal paths.

## Review Focus

Each item names the test that proves it.

1. **Staleness.** Find any path by which a read can return front matter that differs from the file's current bytes. Check the order `_clock`, `_stat`, read, `_stat`, and that `ctime_ns` and `st_ino` are in the identity. Tests:
   - same-size mtime restore;
   - atomic save;
   - symlink retarget;
   - changed during read;
   - the coarse-stat fuzz;
   - the forward-clock fuzz.
2. **The racy rule uses `max(mtime, ctime)`.** `_trusted` refuses an old mtime with a recent ctime, a difference just under 3 s, and a negative difference. Test: Racy rule.
3. **Eviction (C6).** After a cached entry's file is made unreadable, `read_fresh` fails, and `read` then does not return the old values. Every non-storing path evicts, a failing `_stat` included. Tests:
   - `read_fresh` evicts;
   - stat failure.
4. **Sharing (C4).** Mutating the result of a miss, of `read_fresh` or of a hit never changes the next hit. Test: Copies.
5. **Bounds.** Check these:
   - every store path enforces the entry cap, the byte cap and the per-entry cap;
   - a replacement subtracts its old charge;
   - `_deep_size` charges a shared `&x` list once and terminates on a self-sharing structure.

   Tests: Bounds, Deep size, Hostile size.
6. **Locking.** No parse, `deepcopy` or `_deep_size` runs while `_lock` is held. Each is wrapped in a spy that asserts `not docs_cache._lock.locked()`. Test: Threads.
7. **Wiring.** Check these:
   - batch files go through `read_fresh`;
   - `_keyed_view`, `_relink_docs`, `_take_over_keys` and `_prune_docs` go through `read`;
   - `full_scan` calls `forget` before anything reads;
   - doctor and `source_report` never touch `docs_cache`, so `stats()` is unchanged across a doctor run.

   Tests: Task 2's wiring tests.
8. **Invariant with the cache storing.** Check these:
   - the parametrized keyed sequences;
   - the graph-level fuzz;
   - the helper's fresh side reads only through `docs.read_front_matter`.

### Task 1: The cache: identity, racy window, eviction, bounds and copies

**Files:**
- `devgraph/indexer/providers/docs_cache.py` (new). It holds:
  - the constants `RACY_WINDOW_NS` (3 s), `MAX_ENTRIES`, `MAX_BYTES`, `MAX_ENTRY_BYTES` and `ENTRY_OVERHEAD`;
  - the module attributes `_clock = time.time_ns`, `_stat = os.stat` and `_lock`;
  - `_identity(st)` and `_trusted(st, now_ns)`, both pure;
  - `_deep_size(value)`, memoised by `id`;
  - `_root_key(root)`, which is `os.path.realpath`, memoised;
  - `read(repo_root, rel, path)` and `read_fresh(repo_root, rel, path)`;
  - `forget(repo_root)`;
  - `stats()`, which returns `hits`, `misses`, `fresh`, `entries` and `bytes`;
  - a module docstring stating the invariant, the bounds and the POSIX-only ctime guarantee (spec C2–C6).

Tests: `tests/indexer/test_docs_cache.py` (no Neo4j). Shared helpers:
- an autouse fixture that forgets every root it used;
- `aged` (patch `_clock` to real time + 10 s);
- `wait_ctime_advance(path, before_ns)`, which loops on `os.stat` until `st_ctime_ns` exceeds `before_ns`, so no test depends on timestamp ticks.

- [ ] Write failing tests:
  - **Hit and miss** (`aged`).
    - Two `read`s give equal results.
    - `stats()` shows 1 miss then 1 hit.
    - The second makes no `docs.read_front_matter` call (spy).
  - **Same size, mtime restored** (`aged`; skipped on win32).
    1. Read `id: ADR-0001`.
    2. Rewrite it in place as `id: ADR-0002`, and wait for ctime to advance.
    3. Call `os.utime(path, ns=(old_atime, old_mtime))`.
    4. The next `read` returns `ADR-0002`.
  - **Same size, real clock.** Write, read, rewrite at the same size and restore the mtime, all inside the window. The next `read` sees the change, and `stats()` entries is 0.
  - **Racy rule (`_trusted`, fake stat results).**
    - `now - max(...)` of 2.999 s is not trusted, and 3.0 s is.
    - A future mtime (negative difference) is not trusted.
    - **mtime 10 s old and ctime 1 s old: not trusted.** This proves the `max`.
    - Identities that differ only in `ctime_ns`, or only in `st_ino`, are unequal.
  - **Atomic save** (`aged`). Write a temp file of the same size, set the same mtime, and `os.replace` it over the cached file. The next `read` sees the new content.
  - **Symlink retarget** (`aged`). A cached symlink is pointed at a second file with the same size and mtime. The next `read` returns the second file's front matter.
  - **Changed during read** (`aged`). Spy on `docs.read_front_matter` so that it rewrites the file (with a size change) before returning. Nothing is stored, and the next `read` re-reads.
  - **Stat failure.** Patch `_stat` to raise `OSError`. `read` returns `docs.read_front_matter(path)`'s result. Nothing is stored, and a previously cached entry for that key is gone.
  - **`read_fresh` evicts** (`aged`; skipped on win32 and when running as root). Cache an entry with `read`, then `chmod 000` the file. `read_fresh` returns `(None, "could not be read")`. Restore the mode bits with `os.chmod`, then set the old mtime back with `utime`. Patch `_stat` to return the originally cached identity, so only eviction can save the read. `read` now re-parses (spy count 1) and does not return the stale values.
  - **Not cached (C5).** Each of these returns the same tuple as `read_front_matter`, and `stats()` entries stays 0:
    - invalid YAML;
    - non-mapping front matter;
    - a FIFO;
    - a file over `MAX_CONFIG_BYTES`;
    - a missing file.
  - **Copies** (`aged`). Mutate each returned value by adding a key and appending to a list value:
    - the result of a miss;
    - the result of `read_fresh`;
    - the result of a hit.

    After each, the next hit equals `read_front_matter(path)`. An aliased value (`a: &x [1]`, `b: *x`) keeps `result["a"] is result["b"]` on a hit, as a fresh parse does.
  - **Deep size.**
    - `_deep_size` of `{"a": x, "b": x}` charges `x` once.
    - A self-referencing list terminates.
    - The charge is at least `sys.getsizeof` of the top-level dict.
  - **Bounds** (constants monkeypatched small).
    - Storing past `MAX_ENTRIES` evicts the least recently used entry, and a hit refreshes recency.
    - Storing past `MAX_BYTES` evicts until the total fits.
    - An entry over `MAX_ENTRY_BYTES` is not stored, and evicts the old one.
    - Replacing an entry keeps `stats()["bytes"] == sum of the live charges`, including `ENTRY_OVERHEAD` each.
  - **Hostile size** (real constants, `aged`; module-scoped tmp dir).
    - 21,000 tiny files keep entries at or below `MAX_ENTRIES`.
    - About 70 files whose front matter is near `YAML_MAX_NODES`, each charged just under `MAX_ENTRY_BYTES`, keep bytes at or below `MAX_BYTES`.
  - **Keys.**
    - `forget(root_a)` drops only root_a's entries.
    - `read` through `Path("x/../repo")` and `forget(Path("repo/"))` hit the same namespace.
  - **Threads** (`aged`). Eight threads `read` 200 shared files concurrently. Spies on `docs.read_front_matter`, `copy.deepcopy` (as used in `docs_cache`) and `_deep_size` assert `not docs_cache._lock.locked()`. Every result equals `read_front_matter`, `hits + misses` equals the calls, and `bytes` equals the sum of the charges.
  - **Forward-clock fuzz** (`aged`; seeded; skipped on win32). 500 random steps over 20 files. Each step is one of:
    - an in-place same-size rewrite with the mtime restored;
    - a size-changing rewrite;
    - an atomic replace;
    - a delete;
    - a recreate;
    - a symlink retarget;
    - a chmod;
    - a no-op.

    After each write, wait until a probe's `st_ctime_ns` advances. After each step, `read(root, rel, p)` equals `docs.read_front_matter(p)` for every file, and so does `read_fresh` for one random file.
  - **Coarse-stat fuzz** (seeded; runs everywhere).
    - Patch `_stat` with a shim that truncates `st_mtime_ns` to 2 s, sets `st_ctime_ns := st_mtime_ns`, and keeps dev, ino and size.
    - Patch `_clock` with a fake clock `T` that advances by a random 0–2.5 s per step.
    - Every write sets the file's mtime to `T` with `os.utime`.

    The steps are the same-size and size-changing rewrites, atomic replaces and no-ops. After each step, every `read` equals `docs.read_front_matter`.
- [ ] Implement the module (spec C2–C6).
- [ ] `uv run pytest -q tests/indexer/test_docs_cache.py`, then `uv run pytest -q`. Commit "Add an in-process cache of parsed docs front matter".

### Task 2: Wire the cache into dispatch, and keep the invariant under a warm cache

**Files:**
- `devgraph/indexer/providers/docs.py`: `read_selected(spec, files, *, read=None)`. The reader is resolved at call time, and the docstring says a reader must return what `read_front_matter` returns.
- `devgraph/indexer/dispatch.py`:
  - `_read_reusing` takes `read`;
  - `_keyed_view` (and so `_read_docs_batch`, `_relink_docs` and `_take_over_keys`) and `_relink_docs`'s own read use `partial(docs_cache.read, repo_root)`;
  - `_read_docs_batch` reads the batch through `partial(docs_cache.read_fresh, repo_root)`;
  - `_prune_docs` gains `repo_root` and uses `docs_cache.read`;
  - `full_scan` calls `docs_cache.forget(repo_root)` before `prune_stale_files`;
  - `index_paths` and `remove_paths` log `docs_cache.stats()` at DEBUG after a docs pass.
- `tests/indexer/docs_live_helpers.py`:
  - `assert_matches_fresh_apply` runs its fresh `full_scan` under `unittest.mock.patch.object` on `docs_cache`:
    - `read` and `read_fresh` are replaced by `lambda root, rel, path: docs.read_front_matter(path)`;
    - `forget` becomes a no-op.

    The fresh side is then fully uncached, and the cache of the root under test survives.
  - A new `cache_on` fixture patches `docs_cache._clock` to real time + 10 s.
  - A new `cache_off` fixture patches `read` and `read_fresh` as above.
- `tests/indexer/test_docs_provider_live.py`: the `keyed` fixture is parametrized `params=["cache-on", "cache-off"]` and applies the matching fixture. Every keyed sequence then runs both ways. A test that edits a non-batch file in place calls `wait_ctime_advance` before the next read.
- `README.md`, "What it doesn't do yet": the id-keyed per-save line replaces "Reads are not cached between saves yet" with these points:
  - unchanged files are not re-parsed;
  - the walk and the path resolution for each file remain and dominate;
  - the first save after the agent starts reads every file once.
- `PROJECT_STATUS.md`: the read cache leaves "Still open", and "resolve the root once per walk and resolve only symlinks" joins it.
- `docs/superpowers/specs/2026-10-06-docs-frontmatter-keys-design.md`: §4 Cost and §7 point to the new spec.

Tests: `tests/indexer/test_docs_provider.py` (unit) and `tests/indexer/test_docs_provider_live.py` (live).

- [ ] Write failing tests:
  - **Unit: the default is unchanged.**
    - `read_selected(spec, files)` gives the same result as before.
    - With `docs.read_front_matter` monkeypatched after import, the patch is used. The existing spies at about lines 412–415 and 896 pass unchanged.
    - With `read=` a spy, every selected file goes through the spy and no unselected file does.
  - **Unit: the view uses the cache** (`cache_on`). Run `_keyed_view(root, spec)` twice over 50 Adr files. The second call makes 0 `docs.read_front_matter` calls.
  - **Live wiring.** Spy on `docs_cache.read` and `docs_cache.read_fresh`, and check each pass:
    - **Save.** `index_paths({adr-0003.md})` sends the saved file only through `read_fresh`, and every other Adr file only through `read`.
    - **Relink.** A batch that adds a link target sends the relink's reads through `read`.
    - **Takeover.** `remove_paths` sends the `_take_over_keys` reads through `read`.
    - **Apply.** `full_scan` calls `forget` before the first `read`, and `_prune_docs`'s reads go through `read`.
    - **Doctor.** `devgraph doctor` (`source_report` included) leaves `docs_cache.stats()` unchanged.
  - **Live timing** (`cache_on`).
    1. Write 2,000 Adr files (`decisions/adr-NNNN.md`, each with about 8 front-matter lines, an `id` and a `supersedes`) plus the schema. Run `full_scan`, then `docs_cache.forget(root)`.
    2. Make a same-length edit to `adr-0001.md`, then time `index_paths({adr-0001.md})` with `time.perf_counter`. This is the cold save.
    3. Do the same with `adr-0002.md`. This is the warm save.
    4. **Hard assertion:** the warm save makes exactly one `docs.read_front_matter` call, for the batch file. Spy on `docs.read_front_matter`, not `bounded_safe_load`, because the schema load also calls `bounded_safe_load`.
    5. **Ratio:** `cold >= 3 * warm`. The walk and the per-file `resolve()` dominate the warm save (spec "Cost after caching").
    6. `assert_matches_fresh_apply`.
  - **Live correctness: a same-size edit with the mtime restored is seen** (`cache_on`; skipped on win32).
    1. Start from a warm cache.
    2. Edit `decisions/adr-a.md` in place from `id: ADR-0001` to `id: ADR-0002`. The size stays the same.
    3. Wait for ctime to advance, then restore its mtime with `os.utime(ns=...)`. No event is delivered for it.
    4. Save `decisions/adr-b.md` (`id: ADR-0002`) with `index_paths({adr-b.md})`.
    5. Assert that `Adr:ADR-0002` has `path = "decisions/adr-a.md"`. A stale cache would have kept it at `adr-b.md`.
    6. Deliver `index_paths({adr-a.md})`, then `assert_matches_fresh_apply`.
  - **Live: an atomic save outside the batch.** As above, but replace `adr-a.md` by writing a temp file and calling `os.replace`, keeping the size and mtime. The same assertions apply, and the test runs on every platform.
  - **Live: a schema change** (`cache_on`). With the cache warm, change the schema's `where` so that half the Adr files are excluded. Run `full_scan`, then `assert_matches_fresh_apply`.
  - **Live: graph-level fuzz** (`cache_on`; seeded; about 60 steps).
    - The repository holds 8 Adr files whose ids come from a pool of 5, so duplicates and takeovers happen.
    - Each step is one of:
      - save (an edit plus `index_paths`);
      - an edit without an event (same size, mtime restored after the ctime advances; on win32 a size change instead);
      - an atomic replace, with or without an event;
      - delete plus `remove_paths`;
      - a re-id plus `index_paths`.
    - After every step that leaves no event pending, and once at the end after all pending events are delivered, run `assert_matches_fresh_apply`.
  - **Live: existing tests.** Every existing docs live test passes. The `keyed` ones pass under both `cache-on` and `cache-off`.
- [ ] Implement the wiring, the test helpers and the docs updates (spec "Where it plugs in", "Cost after caching", "Observability", "Docs to update").
- [ ] `uv run pytest -q tests/indexer`, then `uv run pytest -q`. Commit "Read unchanged docs front matter from the cache on each save".
