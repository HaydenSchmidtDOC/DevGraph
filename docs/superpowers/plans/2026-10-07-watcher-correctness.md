# Watcher Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Renames, folder moves and deletes, new, deleted and renamed top-level folders, edits made while the agent was off, and git checkouts all leave the graph equal to a fresh `full_scan`. A catch-up on start costs well under a second when nothing changed.

**Spec:** `docs/superpowers/specs/2026-10-07-watcher-correctness-design.md`. Every task implements the decisions it names (W1–W9). The front-matter keys spec and the docs read cache spec still govern the docs provider, which this slice does not change.

**Working directory:** this worktree, branch `epic1/watcher` (from `epic1/tidy`). Run `uv sync --extra dev` once, then run `uv run` from inside the worktree, because the editable install otherwise imports another checkout (check `devgraph.__file__`). Live tests use Neo4j at `bolt://127.0.0.1:7687` (`neo4j`/`devgraph-local-dev`), unique repo_ids and cleanup in teardown. Never touch `~/.devgraph`.

## Global Constraints

- **Incremental equals fresh.** After every live scenario, the repository's graph equals a fresh `full_scan` of the same files under a second repo_id.
  - **Nodes** are compared as `(sorted labels, name, coalesce(source_file, file, path), props_hash)`. `props_hash` hashes every property except `repo_id`, keys ending in `_at`, `last_modified_by`, and the other git-recency properties `git_history` stages.
  - **Edges** are compared as `(label, name, type, label, name)`.
  - `Commit` and `Repository` nodes, and their edges, are excluded.
  - Docs-part equality is also checked with `assert_matches_fresh_apply`.
  - Fixture files have no cross-file by-name references other than docs links and one shared `source`-keyed node, so the known cross-batch gaps cannot cause a mismatch.
- **Lock order (W3, W4).**
  - Handler code (anything that runs on watchdog's dispatch thread) never takes `manager._lock`.
  - `_reconcile_lock` is never held while calling into the observer.
  - No callback runs under the handler's `_lock`.
  - `run_exclusive` never holds `manager._lock` while running `fn`.
- **The watcher never touches the graph.** Expansion of deletes lives in `dispatch.remove_paths` (W2). The watcher only walks the disk, for creates, in-repo moves and newly scheduled top-level folders. Walks run outside the handler's `_lock`.
- **Ignored stays ignored.** Every walk, expansion and schedule follows `walk`'s rules:
  - `is_ignored_path`;
  - symlinked directories are not followed;
  - a junction is followed only when its target is inside the repository and not ignored;
  - a containment check (`is_within`) applies to every move source and destination.

  No path outside a registered repository is ever watched, walked or passed on.
- **Keys are lexical (I5).** A gone path's key resolves the parent only. Presence on disk is checked case-exactly against `os.listdir(parent)`.
- **Stamps (W7).** Every `mark_indexed` caller passes `at=<start of the work>`. Catch-up uses `since - 5 s` against `max(mtime, ctime)` on POSIX and `max(mtime, birthtime)` on Windows. DevGraph never runs git to find changes.
- **Deterministic tests.**
  - Timers come from an injected `timer_factory`, and clocks from an injected `now`.
  - Fake-event tests drive `flush()` or a fake timer, never a real short debounce.
  - Live tests poll for an expected state against a deadline and never sleep for a fixed time.
  - "Exactly one batch" assertions belong only in fake-event tests.
- **No new config knob, no new dependency.**
- **Platforms.**
  - Fake-event and reconcile unit tests run everywhere, including Windows CI without Neo4j.
  - Live tests skip without Neo4j.
  - Tests that rely on POSIX `ctime` are skipped on `win32` with the reason "no change time; see spec W5".
- **Messages** use the spec's W9 wording.
- **TDD.** Each task starts with failing tests, then the implementation, then `uv run pytest -q` (the full suite).
- **Commits.** Plain imperative messages, with no `Co-Authored-By` trailer and no AI attribution. Never stage `uv.lock`, real names or personal paths.

## Review Focus

Each item names the test that proves it.

1. **Deadlock freedom.** Trace every path from a watchdog callback. It must reach only the handler's `_lock`, `_reconcile_lock` and timer starts. `stop()` must return while a reconcile is pending or running. Tests: Task 1, the stop-during-reconcile and lock-assertion cases.
2. **Dead and stale watches.** After a top-level delete and recreate, or a rename, edits under the new folder are seen and nothing is reported under the old name. Tests: Task 1 live reconcile cases.
3. **Rename atomicity and case-only renames.**
   - A rename's source is never in neither set, and its destination never in both.
   - An atomic save keeps the real path out of `deleted`.
   - A Windows case-only rename never deletes the new file's nodes.

   Tests: Task 1 fake events; Task 2 case-only `remove_paths`.
4. **Exact removal.**
   - Expansion covers language and provider keys.
   - Prefix safety: `pkg` against `pkg2/x.py`.
   - The root `.` never expands.
   - A recreated child is kept in every pass, the filesystem and docs providers included.
   - The gone directory itself reaches the provider passes only when nothing indexable remains below it.

   Tests: Task 2.
5. **Shared nodes across a rename.** Rename a file that co-produces a `source`-keyed shared node (for example a `Datastore` or `Endpoint` that two Python files both declare). Check that index-then-remove in one batch leaves exactly the fresh-scan result: the source list and `source`/`library` attribution are the same. Test: Task 4 rename scenario.
6. **Catch-up correctness.**
   - `since` is snapshotted before `observer.start()`.
   - The post-git `since` is `min(last_indexed, burst_start)`.
   - The floor holds stamps back after a failure and clears only after a catch-up that covers it.
   - `last_indexed is None` skips the catch-up.
   - Provider-only files are pruned and not re-offered.

   Tests: Task 3.
7. **Docs invariant.**
   - An id-keyed rename (live or while off) keeps the node and its incoming links.
   - The docs fuzz and every existing docs live test pass unchanged.

   Tests: Task 4.

### Task 1: Watcher events, timers, batch lock and top-level reconcile (W1, W2 watcher half, W3, W4)

**Files:**
- `devgraph/watcher/manager.py`:
  - **`_RepoEventHandler`**:
    - directory-aware `on_moved`, `on_deleted` and `on_created`;
    - case-only and empty-source moves;
    - containment checks;
    - `flush()`, which fires pending changes now if there are any;
    - `cancel()`, which drops the pending timer;
    - `timer_factory`;
    - the batch lock taken from the manager;
    - the top-level signal `request_reconcile`;
    - walks outside `_lock`;
    - the W9 folder log lines.
  - **`WatcherManager`**:
    - `timer_factory` and `reconcile_delay_s` parameters;
    - `_batch_locks`, with `run_exclusive(repo_id, fn)`;
    - `_reconcile_lock` and pending flags, `_request_reconcile(repo_id)` and `_reconcile(repo_id)`;
    - `_desired_dirs(root)`, which skips ignored names, symlinked directories and junctions outside the repository;
    - `stop()`, which sets `_stopping`, cancels debounce and reconcile timers, and then stops observers;
    - `_start_single`, which uses `_desired_dirs`.
- `devgraph/indexer/walk.py`: `indexable_paths_under(repo_root, directory) -> set[Path]`. `_walk` gains an optional `start`, and its behaviour is unchanged without it. It returns nothing for a directory outside the root or an ignored one.

Tests:
- `tests/watcher/test_watcher_events.py` (new). Fake events: a `_RepoEventHandler` over a `tmp_path` repository with a `FakeTimer` factory. A batch fires only on `flush()` or `FakeTimer.fire()`.
- `tests/watcher/test_watcher_reconcile.py` (new). A real `WatcherManager` (inotify or ReadDirectoryChangesW, no Neo4j) with `reconcile_delay_s=0` and a `FakeTimer` for the debounce, and helpers that poll until a predicate holds within 10 s.
- `tests/watcher/test_watcher_manager.py`: existing tests unchanged.

- [ ] Write failing tests (fake events):
  - **File rename.** `FileMovedEvent(pkg/a.py, pkg/a2.py)` gives exactly one batch, with changed `{a2}` and deleted `{a}`.
  - **Atomic save.** `FileDeletedEvent(real)`, then `FileMovedEvent(real.tmp, real)`: `real` is in changed only.
  - **Out, ignored, outside.**
    - `FileMovedEvent(pkg/a.py, build/a.py)` gives deleted `{pkg/a.py}`.
    - A destination outside the root (an absolute path in `tmp_path`) gives deleted `{src}` and nothing changed.
    - An ignored source with a tracked destination gives changed only.
  - **Empty source.** `FileMovedEvent("", pkg/a.py)` gives changed only.
  - **Case-only rename.** `FileMovedEvent(pkg/Foo.py, pkg/foo.py)`, with `foo.py` on disk, gives changed `{pkg/foo.py}` and deleted `{pkg/Foo.py}`. Normcase is monkeypatched to case-folding, so this runs everywhere.
  - **Directory delete.**
    - `DirDeletedEvent(pkg/sub)` gives deleted `{pkg/sub}`.
    - `DirDeletedEvent(node_modules/x)` gives nothing.
    - `DirDeletedEvent(<root>)` gives nothing.
  - **Windows cross-folder move.** `FileDeletedEvent(pkg/sub)` (no such path) plus `DirCreatedEvent(tools/sub)` (with `tools/sub/b.py` on disk) give deleted `{pkg/sub}` and changed `{tools/sub/b.py}`.
  - **Directory move.** Set up `pkg/sub2/{b.py, build/x.py}` on disk, then send `DirMovedEvent(pkg/sub, pkg/sub2)`. Deleted is `{pkg/sub}` and changed is `{pkg/sub2/b.py}`. The sub-moved `FileMovedEvent(pkg/sub/b.py, pkg/sub2/b.py)` adds nothing new.
  - **Directory created.** `DirCreatedEvent(pkg/new)` with `{c.py, d.md}` on disk gives changed `{c.py, d.md}`. An empty directory gives no batch.
  - **Disjoint sets.** A delete then a create of the same path gives changed only. A create then a delete gives deleted only.
  - **Top-level signal.** Each of these calls `request_reconcile` exactly once:
    - `DirCreatedEvent(<root>/newtop)`;
    - `DirMovedEvent(<root>/pkg, <root>/lib)`;
    - `DirDeletedEvent(<root>/pkg)`;
    - `FileDeletedEvent(<root>/pkg)`;
    - `FileCreatedEvent(<root>/x.py)`.

    `DirCreatedEvent(<root>/pkg/x)` does not call it.
  - **Locks.**
    - `on_changes` asserts `not handler._lock.locked()`.
    - A walk spy asserts the same.
    - An event delivered while `on_changes` blocks on an Event is accepted at once, and arrives in the next `flush()`.
  - **`flush()` and `cancel()`.** `flush()` with nothing pending makes no call. `cancel()` drops the timer, and a later `flush()` still fires what is pending.
  - **`indexable_paths_under`.**
    - It equals `indexable_paths(root)` filtered to the directory.
    - It is empty outside the root or for an ignored directory.
    - A symlinked subdirectory is not followed.
- [ ] Write failing tests (manager and reconcile, no Neo4j):
  - **Batch lock survives recreation.** `run_exclusive(r, fn)` and a handler's batch hold the same lock object before and after `stop()`/`start()`.
  - **Rename and nested move.** Poll for the union of batches, not for one batch:
    - `pkg/a.py` → `pkg/a2.py` gives `a2` changed and `a` deleted;
    - `pkg/sub` → `pkg/sub2` gives `pkg/sub` deleted and `pkg/sub2/b.py` changed.
  - **Folder trash.** `shutil.move(pkg/sub2, <outside>)` gives `pkg/sub2` deleted.
  - **Top-level delete, recreate, edit (C2).**
    1. `rmtree(pkg)`, then `mkdir pkg` and write `pkg/a.py`.
    2. Wait until no emitter for `pkg` is dead.
    3. Edit `pkg/a.py`. `pkg/a.py` is seen as changed.
  - **Top-level rename, then edit.**
    1. Rename `pkg` → `lib`. `pkg` is deleted and `lib/**` changed.
    2. Edit `lib/a.py`. It is seen.
    3. No later batch contains a path under `pkg/`.
  - **New top-level directory.** `mkdir newtop`, write `newtop/c.py`, then edit it. Both are seen, including a file written before the new watch existed (from the reconcile walk).
  - **Scheduling rules.**
    - A symlinked top-level directory pointing outside the root is never scheduled (POSIX skip).
    - An outside-repository junction is never scheduled. Monkeypatch `DirEntry.is_junction`/`Path.is_junction` and the target resolution, so this runs everywhere.
  - **Stop with a reconcile pending or running.** `stop()` returns within 5 s, and no reconcile runs afterwards. A reconcile that finds the repository gone from `_observers` returns without scheduling.
  - **Stop cancels debounce.** With a change pending (fake timer not fired), `stop()` cancels it, and no `on_changes` call follows.
- [ ] Implement (spec W1–W4).
- [ ] `uv run pytest -q tests/watcher`, then `uv run pytest -q`. Commit "Deliver renames and folder moves to the indexer".

### Task 2: Exact removal from the graph's own file list (W2 indexer half, W8)

**Files:**
- `devgraph/graph/engine.py`: `list_extracted_paths(repo_id, extractor, labels: list[str] | None) -> set[str]`, which returns the `path` of that provider's nodes, optionally limited to labels.
- `devgraph/indexer/dispatch.py`:
  - `_graph_files(engine, repo_id, repo_root)`: `list_indexed_files`, plus docs paths, plus filesystem file-label paths, the provider part only when not `schema_pending`;
  - `_gone_key(repo_root, path)`, lexical, resolving the parent only;
  - `_present(repo_root, rel)`, case-exact via `os.listdir(parent)` plus `_is_provider_file`;
  - `remove_paths` expands every gone key over `_graph_files` (one query per non-empty call; never for `.`), drops paths present on disk, deletes language nodes per exact path, and gives the provider passes the exact paths plus each gone directory that holds no indexable file on disk;
  - `prune_stale_files` uses `_graph_files`;
  - the docstrings say so.

Tests: `tests/indexer/test_dispatch.py`, `tests/indexer/test_filesystem_provider_live.py` and `tests/indexer/test_docs_provider_live.py` (live), and `tests/indexer/test_remove_keys.py` (unit, new).

- [ ] Write failing tests:
  - **Unit: lexical key.**
    - `_gone_key(root, root/"pkg"/"Foo.py")`, while `pkg/foo.py` exists, is `pkg/Foo.py`.
    - The parent is resolved, so a symlinked parent is keyed by its target.
    - `_present(root, "pkg/Foo.py")` is False when only `foo.py` is listed.
  - **Unit: case-only `remove_paths`.** With a stub engine whose graph files are `{pkg/Foo.py, pkg/foo.py}` and `foo.py` on disk, `remove_paths({pkg/Foo.py})` deletes only `pkg/Foo.py`'s language nodes and passes `{pkg/Foo.py}` to the provider passes.
  - **Unit: root and prefix.**
    - `remove_paths({root})` removes nothing.
    - With `pkg/x.py` and `pkg2/y.py` in the graph, removing `pkg` expands to `pkg/x.py` only.
  - **Live: a directory removes its language nodes.**
    1. Full-scan `pkg/mod.py` (a class) and `pkg/sub/util.py`.
    2. `rmtree(pkg)`, then `remove_paths({pkg})`.
    3. No node is left under `pkg/`, and the graph equals a fresh scan.
  - **Live: recreated child kept (C3).** With the filesystem `File`/`Folder` types and a path-keyed docs type over `pkg/**/*.md` declared:
    1. Full-scan `pkg/{mod.py, notes.md, sub/util.py}`.
    2. Delete `pkg` and recreate `pkg/mod.py` (new content) and `pkg/notes.md`.
    3. Call `index_paths({pkg/mod.py, pkg/notes.md})`, then `remove_paths({pkg})`.
    4. These remain: the new `pkg/mod.py` nodes, `File:pkg/mod.py`, `File:pkg/notes.md`, `Folder:pkg`, and the docs node for `pkg/notes.md`.
    5. These are gone: `pkg/sub/util.py`'s nodes and `Folder:pkg/sub`.
    6. The graph equals a fresh scan, and `assert_matches_fresh_apply` passes.
  - **Live: provider-only files pruned (C4).**
    1. With filesystem types declared, full-scan, including `notes.md` (mentions off) and `logo.png`.
    2. Delete both on disk, then `prune_stale_files`.
    3. `File:notes.md` and `File:logo.png` are gone, and the graph equals a fresh scan.
    4. A second `prune_stale_files` prunes 0.
  - **Live: one query.** `remove_paths({pkg/mod.py})` calls the graph-files query exactly once (a spy on the engine methods).
  - **Live: docs directory.** Remove a keyed docs directory, with entries in another directory linking into it. `assert_matches_fresh_apply` passes.
  - **Existing.** Every existing remove, filesystem and docs live test passes unchanged.
- [ ] Implement (spec W2).
- [ ] `uv run pytest -q tests/indexer`, then `uv run pytest -q`. Commit "Remove exactly the files below a deleted folder".

### Task 3: Stamps, floors, catch-up and the post-git reconcile (W4 schema rescan, W5, W6, W7, W9 agent half)

**Files:**
- `devgraph/registry/store.py`: `mark_indexed(repo_id, at: datetime | None = None)`.
- `devgraph/cli/main.py` (about lines 94 and 274), `devgraph/dashboard/routes.py` (about line 320) and `devgraph/agent/schema_rescan.py` (about line 99): capture the start before `full_scan` and pass `at=`. `SchemaRescanScheduler` also gains an optional `run_exclusive` and runs `full_scan` through it.
- `devgraph/indexer/dispatch.py`:
  - `CatchUp(indexed, pruned, checked)`;
  - `catch_up(engine, repo_id, repo_root, since, docs_path=None, mentions_enabled=False)`;
  - `_change_stamp_ns(st)`;
  - `CATCH_UP_MARGIN_NS = 5_000_000_000`.
- `devgraph/watcher/manager.py`:
  - **`on_catch_up: Callable[[str, datetime], None] | None`** and `git_catch_up_delay_s` (default 2.0).
  - **`_start_single`** snapshots `last_indexed` before `observer.start()`. It then starts a daemon thread that calls `run_exclusive(repo_id, lambda: on_catch_up(repo_id, snapshot))`, or logs the "hasn't indexed" line when the snapshot is `None`.
  - **`request_catch_up(repo_id, since, delay_s)`**. Requests coalesce, keeping the minimum `since`. Each is cancelled by `stop()`.
  - **`_GitStateEventHandler`** records the first `.git/index.lock` event time of a burst, before its filter. `_fire_change` hands `burst_start` to the manager, which calls `request_catch_up(repo_id, min(last_indexed, burst_start), git_catch_up_delay_s)`.
- `devgraph/agent/sync.py` (new), **`RepoSync(engine, registry, publish, request_catch_up, now=...)`**:
  - `on_changes`, with the stamp `min(started, floor)`, a floor set on failure, and `request_catch_up(…, delay 30 s)`;
  - `on_catch_up(repo_id, since)`, with `since = min(since, floor)`, the W9 logs, `catch_up` events and `reindexed`; a success with `since <= floor` clears the floor;
  - `retry_failed()`, for the health loop;
  - `running` (a count), for the tray;
  - `stopping`, which suppresses failure warnings.
- `devgraph/agent/tray.py` and `devgraph/agent/headless.py`:
  - build `RepoSync` and keep `_on_changes` as a delegate;
  - pass `on_catch_up` and `run_exclusive`;
  - the health loop calls `retry_failed()` on an unhealthy-to-healthy transition;
  - `_status_text` shows `catching up`;
  - quit, Pause and stop set `stopping`.
- `tests/agent/test_tray_on_changes.py`: patch targets move to `devgraph.agent.sync`, and `mark_indexed` is asserted with `at=`.

Tests:
- `tests/indexer/test_catch_up.py` (new). Unit tests stub the engine; live tests use Neo4j.
- `tests/agent/test_repo_sync.py` (new), with a mocked engine and registry and an injected `now`.
- `tests/watcher/test_watcher_reconcile.py`: catch-up scheduling with fake timers.
- `tests/agent/test_schema_rescan.py`, `tests/dashboard/test_register_repo.py` and the CLI tests: stamps.

- [ ] Write failing tests:
  - **Stamp (unit).**
    - On POSIX, mtime 1 h ago with ctime now is due for `since` = 1 min ago (skipped on win32).
    - Both old is not due.
    - 4 s before `since` is due, and 6 s before is not.
  - **Known set (unit).** A file missing from `_graph_files` is due. A provider-only `logo.png` present in `_graph_files`, with old stamps, is not.
  - **Callers stamp the start.** With an injected or patched clock, these stamp `at` equal to the time before their `full_scan` ran, not after:
    - register (CLI);
    - rescan (CLI);
    - dashboard registration;
    - the schema rescan.
  - **`RepoSync` stamps and floors** (injected `now`).
    1. `on_changes` stamps `at=started`.
    2. A failing `index_paths` leaves the stamp alone, sets the floor, and calls `request_catch_up(…, 30)`.
    3. A later successful batch stamps `min(started, floor)`.
    4. `on_catch_up(since > floor)` runs with `since = floor`, and the floor is cleared on success.
    5. A failing catch-up keeps the floor.
    6. `retry_failed()` requests one catch-up per floored repository.
    7. Failure while `stopping` logs nothing at WARNING.
  - **`RepoSync` events.** `running`, then `done` (plus `reindexed` when counts are non-zero), or `failed`. `running` is above 0 only during the call.
  - **Never indexed.** With `last_indexed` `None`, `_start_single` makes no `on_catch_up` call and logs the W9 line.
  - **Snapshot before start (I1).**
    1. `last_indexed` is T0.
    2. A live batch fires (fake timer) before the catch-up thread gets the batch lock, and stamps T1.
    3. `on_catch_up` receives T0.
  - **Pause then Resume with a debounce pending.**
    1. Make an edit (the fake timer is not fired).
    2. `stop()`: no `on_changes` call.
    3. `start()`: `on_catch_up` is called with `since <= ` the edit's time.
  - **Ordering.** While `on_catch_up` blocks on an Event, a file event produces no `on_changes` call until the catch-up returns, and then exactly one. An overlap counter never exceeds 1.
  - **Post-git.**
    1. `.git/index.lock` created at t=10, then `HEAD` at t=11, with `last_indexed` at t=20.
    2. One `request_catch_up(since=t10)`, fired after `git_catch_up_delay_s` (fake timer).
    3. Two bursts within the delay coalesce to the minimum `since`.
    4. A ref-only burst uses `last_indexed`.
    5. `stop()` cancels a pending request.
  - **Stop during a running catch-up.** `on_catch_up` blocks. `stop()` returns within 5 s, and the later failure logs no warning.
  - **Schema rescan.** `SchemaRescanScheduler.run_once` runs `full_scan` through the passed `run_exclusive` (a spy).
  - **Live catch-up, edits made while off.** Filesystem types and an id-keyed `Adr` docs type are declared.
    1. Full-scan, and stamp `since`.
    2. With no watcher running:
       - edit `a.py`, adding a class;
       - delete `b.py`, `notes.md` and `logo.png`;
       - add `c.py`;
       - rename `decisions/adr-1.md` to `decisions/0001-start.md`. `adr-2.md` links `supersedes: ADR-1` to it.
    3. Run `catch_up(since)`. The graph equals a fresh scan, `assert_matches_fresh_apply` passes, and `Adr:ADR-1` keeps its incoming `SUPERSEDES`.
  - **Live no-op.** A second `catch_up` gives `indexed == 0` and `pruned == 0`.
- [ ] Implement (spec W4–W7, W9).
- [ ] `uv run pytest -q tests/indexer tests/watcher tests/agent tests/dashboard tests/cli`, then `uv run pytest -q`. Commit "Catch up on changes made while DevGraph wasn't watching".

### Task 4: End-to-end live scenarios, dashboard status and docs (W8, W9 dashboard, Docs to update)

**Files:**
- `tests/watcher/live_helpers.py` (new, not a test module):
  - `graph_snapshot(engine, repo_id)`, using the Global Constraints projection, `props_hash` included;
  - `fresh_snapshot(engine, repo_id, root)`, which runs `full_scan` under `f"{repo_id}_fresh"` with the docs cache bypassed as in `assert_matches_fresh_apply`, then snapshots and deletes that repository;
  - `wait_until_equal(engine, repo_id, expected, timeout_s=30)`, which polls `graph_snapshot` every 0.2 s and, on timeout, fails with the diff;
  - a `live_agent` fixture: a `HeadlessAgent` built with settings pointed at a `tmp_path` registry and the test Neo4j, with the dashboard off and the insights scheduler not started. It starts `agent._watcher` only, and publishes into a list.
- `devgraph/dashboard/static/index.html`: a delimited `/* ── Catch-up pill ── */` block holding a pure reducer, `catchUpPillState(running, event, selectedRepo) -> {running, label}`, applied by the SSE handler to `entityLivePill`.
- `README.md` and `PROJECT_STATUS.md`.

Tests:
- `tests/watcher/test_watcher_live.py` (new; skipped without Neo4j). The fixture repository is a git repository with:
  - `devgraph.schema.yaml` declaring the filesystem `File`/`Folder` types and an id-keyed docs `Adr` type with a `supersedes` relationship (per-run labels as in `test_filesystem_provider_live.py`, with constraints dropped in a module teardown);
  - `pkg/mod.py` (a class, and a datastore URL also declared in `tools/run.py`, so a `source`-keyed `Datastore` is shared);
  - `pkg/sub/util.py`, `tools/run.py`, `README.md` and `logo.png`;
  - `decisions/adr-1.md` and `adr-2.md` (with `supersedes: ADR-1`);
  - `node_modules/dep/index.js`.
- `tests/dashboard/catch_up_ui.js` (new). It lifts the pill block out of `index.html` in the pattern of `config_form_dump.js`, and is driven from a Python test like the other harnesses.

- [ ] Write failing tests. Each scenario performs its operations, computes `expected = fresh_snapshot(...)` once, runs `wait_until_equal(..., expected)`, then `assert_matches_fresh_apply`.
  - **Rename.** Rename `pkg/mod.py` to `pkg/model.py` (the shared `Datastore` keeps the same sources as a fresh scan), and `decisions/adr-1.md` to `decisions/0001-start.md`. `Adr:ADR-1` keeps its incoming `SUPERSEDES`.
  - **Folder trash.** `shutil.move(pkg/sub, <tmp outside>)`.
  - **Folder move.**
    1. Move nested `pkg/sub` to `tools/sub`, and compare.
    2. Rename top-level `tools` to `scripts`, and compare.
    3. Edit `scripts/run.py` (add a function), and compare.
  - **Top-level delete and recreate.** `rmtree(pkg)`, compare, recreate `pkg/mod.py`, compare, edit it, compare.
  - **Edits while off.**
    1. `live_agent` stops.
    2. Edit `pkg/mod.py`, delete `tools/run.py` and `logo.png`, add `newtop/c.py`, and re-id `adr-2.md`.
    3. A new `live_agent` starts on the same registry, and the graph is compared.
    4. The published events include `catch_up` `running` and then `done`.
  - **Git checkout of another branch.**
    1. Commit everything on `main`.
    2. On `feature`, make several changes and commit them:
       - rename `pkg/` to `lib/`;
       - delete `decisions/adr-2.md`;
       - edit `README.md`;
       - add `lib/extra.py`.
    3. Go back to `main`, and start the agent.
    4. Run `git checkout feature`, then compare.
    5. Run `git checkout main`, then compare again.
  - **Dashboard pill (harness).** A `running` event for the selected repository shows `Catching up…`. `done` or `failed` restores the previous label. Another repository's event has no effect unless `__all__` is selected.
- [ ] Implement the pill and the helpers. If a scenario exposes a bug in Tasks 1–3, fix it in the module it belongs to, with a unit test there.
- [ ] Update docs (spec "Docs to update"):
  - **README.** Delete the "Known gaps" paragraph under **Live updates** and the folder line under "Known limits of the filesystem provider". Add "Keeping up with changes":
    - renames and folder moves are picked up live;
    - on start, and after a git checkout or pull, DevGraph checks for what it missed;
    - the tray says "catching up" and the dashboard shows "Catching up…";
    - the spec's Limits, with `devgraph rescan` as the fallback.
  - **PROJECT_STATUS.** Replace the watcher entry's "Known gaps" sentence. Extend the `devgraph/watcher/` and `devgraph/agent/` entries to cover:
    - renames and folder events;
    - the top-level reconcile;
    - the batch lock;
    - catch-up, the post-git reconcile and failure floors;
    - `devgraph/agent/sync.py`.

    Add a shipped line with the spec's Limits as "Still open".
- [ ] `uv run pytest -q`, plus the `tests/dashboard/*.js` harnesses. Commit "Test watcher scenarios end to end and show catch-up status".
