# Watcher Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Renames, folder moves and deletes, new top-level folders, edits made while the agent was off, and git checkouts all leave the graph equal to a fresh `full_scan`. A rename is atomic from the graph's point of view. A catch-up on start costs well under a second when nothing changed.

**Spec:** `docs/superpowers/specs/2026-10-07-watcher-correctness-design.md`. Every task implements the decisions it names (W1–W8). The front-matter keys spec (`2026-10-06-docs-frontmatter-keys-design.md`, "Rename keeping the id, in either event order") and the docs read cache spec still govern the docs provider, which this slice does not change.

**Working directory:** this worktree, branch `epic1/watcher` (from `epic1/tidy`). Run `uv sync --extra dev` once, then run `uv run` from inside the worktree, because the editable install otherwise imports another checkout (check `devgraph.__file__`). Live tests use Neo4j at `bolt://127.0.0.1:7687` (`neo4j`/`devgraph-local-dev`), unique repo_ids and cleanup in teardown. Never touch `~/.devgraph`.

## Global Constraints

- **Incremental equals fresh.** After every live scenario settles, the repository's graph equals a fresh `full_scan` of the same files under a second repo_id. The comparison covers:
  - nodes as `(sorted labels, name, coalesce(source_file, file, path))`;
  - edges as `(label, name, type, label, name)`.

  It excludes `Commit` and `Repository` nodes and their edges. Docs-part equality is also checked with the existing `assert_matches_fresh_apply`. Fixture files have no cross-file by-name references other than docs links, so the known cross-batch gaps in PROJECT_STATUS cannot cause a mismatch.
- **One batch per rename (W1).** A rename inside the repository reaches `on_changes` as the destination in `changed` and the source in `deleted`, in the same call. `changed` and `deleted` are always disjoint.
- **The watcher never touches the graph.** Directory expansion of deletes lives in `dispatch.remove_paths` (W2). The watcher only walks the disk, for creates and in-repo moves.
- **Ignored stays ignored.** Every new walk, expansion and schedule goes through `walk`'s rules: `is_ignored_path`, symlinked directories not followed, and junctions only when the target is inside the repository. No path outside a registered repository is ever watched, walked or passed on.
- **No callback under the event lock (W4).** `on_changes`, `on_catch_up` and any `run_exclusive` function run under the repository's `_batch_lock` only, never under `_lock`. The observer thread waits only for a set swap.
- **Catch-up is incremental (W5).** It is a full scan only when `last_indexed` is `None`. The change stamp is `max(mtime, ctime)` on POSIX and `max(mtime, birthtime)` on Windows, compared with `since - 2 s`. `mark_indexed` is stamped with the batch's start, not its end.
- **No new config knob, no new dependency.** The debounce stays `watch_debounce_ms`.
- **Platforms.** Fake-event unit tests construct watchdog event objects directly and run everywhere, including Windows CI without Neo4j. Live tests skip without Neo4j, as the existing ones do. Tests that rely on POSIX `ctime` are skipped on `win32` with the reason "no change time; see spec W5".
- **Messages.** Log lines and UI strings are the spec's W8 wording, written for a non-developer.
- **TDD.** Each task starts with failing tests, then the implementation, then `uv run pytest -q` (the full suite).
- **Commits.** Plain imperative messages, with no `Co-Authored-By` trailer and no AI attribution. Never stage `uv.lock`, real names or personal paths.

## Review Focus

Each item names the test that proves it.

1. **Rename atomicity.**
   - Find any path by which a rename's source can end up in neither set, or its destination in both.
   - Atomic save (temp → real) must still leave the real path out of `deleted`.
   - Tests: Task 1 rename and atomic-save cases; Task 4 rename scenario.
2. **Expansion safety.** `remove_paths` expands a gone path only to indexed paths strictly below it (`rel + "/"`), and never removes one that is an indexable file on disk.
   - Watch for prefix bugs: `pkg` against `pkg2/x.py`.
   - Watch for the root `.`.
   - Tests: Task 2.
3. **Language nodes keyed by name across a rename.** If an extractor MERGEs a node on a key that does not include the file, check that index-then-remove in one batch leaves exactly the fresh-scan result.
   - Test: Task 4 rename scenario, with a class in the renamed file.
4. **Top-level reschedule.** After a top-level rename, edits under the new name are seen, and nothing is reported under the old name.
   - Watches are never scheduled on symlinks, outside-repo junctions or ignored names.
   - Tests: Task 1 reschedule cases.
5. **Locking and races.**
   - No callback runs under `_lock`.
   - Catch-up and live batches never overlap for one repository.
   - A live event that arrives during catch-up is indexed after it.
   - `stop()` during a catch-up does not hang.
   - Tests: Task 3 lock tests.
6. **Catch-up threshold.**
   - `since` is the batch start.
   - The stamp includes ctime or birthtime.
   - The 2 s margin is applied once.
   - A never-indexed repository gets a full scan.
   - Tests: Task 3 catch-up unit and live tests.
7. **Docs invariant.**
   - An id-keyed rename (live or while off) keeps the node and its incoming links.
   - The docs fuzz and every existing docs live test pass unchanged.
   - Tests: Task 4.

### Task 1: Watcher events: renames, directory events, top-level reschedule (W1, W2 watcher half, W3, W4 lock split)

**Files:**
- `devgraph/watcher/manager.py`:
  - `_RepoEventHandler.on_moved`, `on_deleted` and `on_created` handle directories;
  - `_fire_changes` splits into a `_batch_lock` and the swap;
  - a `_on_root_dirs_changed` hook;
  - `WatcherManager._reconcile_watches(repo_id)` and `run_exclusive(repo_id, fn)`;
  - scheduling skips symlinked directories and outside-repo junctions;
  - the W8 "Folder renamed/removed" log lines.
- `devgraph/indexer/walk.py`: `indexable_paths_under(repo_root, directory) -> set[Path]`. It is `_walk`'s rules, started at `directory`, with keys relative to `repo_root`. It returns nothing if `directory` is outside the root or ignored. `_walk` gains an optional `start` parameter, and its behaviour is unchanged without it.

Tests:
- `tests/watcher/test_watcher_events.py` (new). These are fake-event unit tests: build `_RepoEventHandler` over a `tmp_path` repo with `debounce_ms=10`, feed it `watchdog.events.*Event` objects directly, and capture batches. They run everywhere.
- `tests/watcher/test_watcher_manager.py`: live inotify or ReadDirectoryChangesW cases, with no Neo4j.

- [ ] Write failing tests (fake events):
  - **File rename.** `FileMovedEvent(pkg/a.py, pkg/a2.py)`, with `a2.py` on disk, gives one batch with changed `{a2}` and deleted `{a}`.
  - **Atomic save is unchanged.** These events:
    1. `FileDeletedEvent(real)`;
    2. `FileMovedEvent(real.tmp, real)`.

    The batch has `real` in changed only. `real.tmp` may be in deleted, and `real` is not.
  - **Move out or into ignored.** `FileMovedEvent(pkg/a.py, build/a.py)` gives deleted `{pkg/a.py}` and nothing changed. An ignored source with a tracked destination gives changed only.
  - **Empty source (Windows split pair).** `FileMovedEvent("", pkg/a.py)` gives changed `{pkg/a.py}` and nothing in deleted.
  - **Directory delete.** `DirDeletedEvent(pkg/sub)` gives deleted `{pkg/sub}`. `DirDeletedEvent(node_modules/x)` gives nothing.
  - **Windows-shaped directory delete.** `FileDeletedEvent(pkg/sub)` with no such path on disk gives deleted `{pkg/sub}`. This is today's behaviour, pinned.
  - **Directory move in the repository.** Set up `pkg/sub2/{b.py, build/x.py}` on disk, then send `DirMovedEvent(pkg/sub, pkg/sub2)`. Deleted is `{pkg/sub}` and changed is `{pkg/sub2/b.py}`, with the ignored `build/` left out. Adding the sub-moved `FileMovedEvent(pkg/sub/b.py, pkg/sub2/b.py)` leaves the same sets.
  - **Directory created or moved in.** `DirCreatedEvent(pkg/new)` with `pkg/new/{c.py, d.md}` on disk gives changed `{c.py, d.md}`. An empty directory gives no batch.
  - **Disjoint sets.** A delete, then a create, of the same path in one window: changed only. A create, then a delete: deleted only.
  - **Top-level hook.** `DirCreatedEvent(<root>/newtop)`, `DirMovedEvent(<root>/pkg, <root>/lib)` and `DirDeletedEvent(<root>/pkg)` each call `on_root_dirs_changed` once. `DirCreatedEvent(<root>/pkg/x)` does not call it.
  - **No callback under `_lock`.** `on_changes` asserts `not handler._lock.locked()`. An event delivered from another thread while `on_changes` blocks on a `threading.Event` is accepted at once, and arrives in the next batch.
  - **`indexable_paths_under`.** It matches `indexable_paths(root)` filtered to the directory. It returns nothing for a directory outside the root, or for an ignored one. A symlinked subdirectory is not followed.
- [ ] Write failing tests (live watcher, no Neo4j):
  - **File rename** `pkg/a.py` → `pkg/a2.py` gives one batch with changed `{a2}` and deleted `{a}`.
  - **Nested directory rename** `pkg/sub` → `pkg/sub2` gives deleted `{pkg/sub}` and changed `{pkg/sub2/b.py}`.
  - **Folder trash.** `shutil.move(pkg/sub2, <outside>)` gives deleted `{pkg/sub2}`.
  - **Top-level rename, then edit.** `pkg` → `lib` gives deleted `{pkg}` and changed `lib/**`. A later write to `lib/a.py` gives changed `{lib/a.py}`, and no path under `pkg/` is reported again.
  - **New top-level directory.** `mkdir newtop`, then write `newtop/c.py`: `newtop/c.py` is changed. A later edit to it is also seen.
  - **Symlinked top-level directory.** One pointing outside the repository is not scheduled. Check `observer.emitters` paths, with a POSIX-only skip.
- [ ] Implement (spec W1–W4).
- [ ] `uv run pytest -q tests/watcher`, then `uv run pytest -q`. Commit "Deliver renames and folder moves to the indexer".

### Task 2: Expand deleted directories from the graph (W2 indexer half)

**Files:**
- `devgraph/indexer/dispatch.py`, `remove_paths`. Before the per-path loop, compute the gone rels. If any gone rel has no exact match in `engine.list_indexed_files(repo_id)` (one query, made only then), add every indexed rel strictly below a gone rel. Leave out any whose `repo_root / rel` is an indexable file on disk.
  - The provider passes' `gone` set stays the caller's paths.
  - The docstring states the expansion.

Tests: `tests/indexer/test_dispatch.py` (live, in the existing remove-paths class) and `tests/indexer/test_filesystem_provider_live.py`.

- [ ] Write failing tests:
  - **A directory removes its language nodes.**
    1. Full-scan a repository with `pkg/mod.py` (a class) and `pkg/sub/util.py`.
    2. `shutil.rmtree(pkg)`, then `remove_paths({pkg})`.
    3. No node remains with `source_file`, `file` or a `Module` name under `pkg/`, and the graph equals a fresh scan.
  - **Prefix safety.** With `pkg/` and `pkg2/` both indexed, removing `pkg` leaves `pkg2/**`.
  - **Recreated child kept.** Delete `pkg`, recreate `pkg/mod.py` with different content, then call `index_paths({pkg/mod.py})` and `remove_paths({pkg})`. `pkg/mod.py`'s new nodes remain, and `pkg/sub/util.py`'s are gone.
  - **Exact file path costs no expansion query.** `remove_paths({pkg/mod.py})` for an indexed file calls `list_indexed_files` at most once, and removes only that file. Use a spy on the engine.
  - **Docs directory.** Expansion leaves the docs graph equal to `assert_matches_fresh_apply` after a keyed docs directory is removed, with entries in another directory linking into it.
- [ ] Implement (spec W2).
- [ ] `uv run pytest -q tests/indexer`, then `uv run pytest -q`. Commit "Remove every file below a deleted folder".

### Task 3: Batch-start stamps, catch-up and the post-git reconcile (W4 run_exclusive, W5, W6, W8 agent half)

**Files:**
- `devgraph/registry/store.py`: `mark_indexed(repo_id, at: datetime | None = None)`.
- `devgraph/indexer/dispatch.py`:
  - `CatchUp` (a NamedTuple of `indexed`, `pruned`, `checked`, `full`);
  - `catch_up(engine, repo_id, repo_root, since, docs_path=None, mentions_enabled=False)`;
  - `_change_stamp_ns(stat_result)`, which is `max(mtime, ctime)` on POSIX and `max(mtime, birthtime)` on win32;
  - `CATCH_UP_MARGIN_NS = 2_000_000_000`.
- `devgraph/watcher/manager.py`:
  - the `on_catch_up: Callable[[str], None] | None` parameter;
  - after a successful `_start_single`, a daemon thread runs `run_exclusive(repo_id, on_catch_up)`;
  - `_GitStateEventHandler._fire_change` also schedules the same catch-up, 2 s later, coalescing repeats;
  - `stop()` sets a stop event that pending catch-up timers and threads check before they start.
- `devgraph/agent/tray.py` and `devgraph/agent/headless.py`:
  - `_on_changes` captures `started` and calls `mark_indexed(repo_id, at=started)`;
  - a new `_on_catch_up(repo_id)` reads the record, calls `catch_up(since=parse(last_indexed))`, then `mark_indexed(at=started)`, publishes `catch_up` (`running`, then `done` or `failed`) and `reindexed` when there are counts, and logs the W8 lines;
  - the tray counts running catch-ups for `_status_text` (`DevGraph (catching up)`, with paused and warning states first).
- `tests/agent/test_tray_on_changes.py`: `mark_indexed` is now asserted with `at=ANY`.

Tests:
- `tests/indexer/test_catch_up.py` (new). Unit tests stub the engine; live tests use Neo4j.
- `tests/watcher/test_watcher_events.py`: lock and scheduling tests.
- `tests/agent/test_tray_on_changes.py`: catch-up glue, with a mocked engine.

- [ ] Write failing tests:
  - **Stamp (unit).** On POSIX, a file with mtime 1 h ago and ctime now is due for `since` = 1 min ago (`os.utime` back-dating; skipped on win32). A file with both old is not due. A stamp 1.5 s before `since` is due (the margin), and one 2.5 s before is not.
  - **Unknown files are due (unit).** A file whose key is missing from the stubbed `list_indexed_files` is due whatever its stamp.
  - **Never indexed (unit).** `since=None` calls `full_scan` and nothing else, and reports `full=True`.
  - **Live catch-up, edits made while off.**
    1. Full-scan and stamp `since`.
    2. With no watcher running: edit `a.py` (adding a class), delete `b.py`, add `c.py`, and rename the id-keyed `decisions/adr-1.md` to `decisions/0001-start.md`. Another doc links `supersedes: ADR-1` to it.
    3. Run `catch_up`.
    4. The graph equals a fresh scan, and `assert_matches_fresh_apply` passes. The ADR node still has its incoming `SUPERSEDES`. `checked` is the file count, and `indexed` is `{a.py, c.py, 0001-start.md}` plus their reverse dependents.
  - **Live no-op.** With nothing changed, `catch_up` indexes only files never represented in the graph. `pruned == 0`.
  - **Glue.** `_on_catch_up` passes `since` parsed from `last_indexed`, stamps `mark_indexed(at=<start>)` and publishes `running` then `done`, plus `reindexed` when counts are non-zero. On an exception it publishes `failed`, logs the W8 warning, and does not call `mark_indexed`. `_on_changes` stamps `at` with a time taken before `index_paths` runs (a spy on `datetime`). `_status_text` shows `catching up` while the counter is above 0.
  - **Ordering (fake events).**
    1. Start a manager whose `on_catch_up` blocks on an Event.
    2. A file event during that time produces no `on_changes` call until the catch-up returns, and then exactly one.
    3. `on_catch_up` and `on_changes` never run at the same time; check with an overlap counter.
  - **Start, resume, refresh.** `start()`, `stop()` then `start()`, and `refresh()` picking up a newly watch-enabled repository each call `on_catch_up` once per repository. A repository with watching disabled never gets the call.
  - **Post-git.** Two `_GitStateEventHandler` fires within 2 s give one `on_catch_up`. `stop()` before the timer fires gives none.
- [ ] Implement (spec W4–W6, W8).
- [ ] `uv run pytest -q tests/indexer tests/watcher tests/agent`, then `uv run pytest -q`. Commit "Catch up on changes made while DevGraph wasn't watching".

### Task 4: End-to-end live scenarios, dashboard status and docs (W7, W8 dashboard, Docs to update)

**Files:**
- `tests/watcher/live_helpers.py` (new, not a test module):
  - `graph_snapshot(engine, repo_id)`, using the Global Constraints projection;
  - `assert_graph_matches_fresh_scan(engine, repo_id, root)`, which runs a fresh `full_scan` under `f"{repo_id}_fresh"` with the docs cache bypassed as in `assert_matches_fresh_apply`, compares, and deletes the fresh repository;
  - a `live_agent` fixture: a `HeadlessAgent` built with settings pointed at a `tmp_path` registry and the test Neo4j, with the dashboard off and only `agent._watcher` started (no schedulers);
  - `settle(agent, quiet_s=1.5, timeout_s=30)`, which waits until no batch or catch-up has run for `quiet_s`, by counting calls through wrapped `_on_changes` and `_on_catch_up`.
- `devgraph/dashboard/static/index.html`: a delimited `/* ── Catch-up pill ── */` block holding a pure reducer, `catchUpPillState(running, event, selectedRepo) -> {running, label}`. The SSE handler applies it to `entityLivePill`.
- `README.md` and `PROJECT_STATUS.md`.

Tests:
- `tests/watcher/test_watcher_live.py` (new; skipped without Neo4j). The fixture repository is a git repository with:
  - `devgraph.schema.yaml` declaring the filesystem `File`/`Folder` types and an id-keyed docs `Adr` type with a `supersedes` relationship (per-run labels, as in `test_filesystem_provider_live.py`, with constraints dropped in a module teardown);
  - `pkg/mod.py` (a class), `pkg/sub/util.py`, `tools/run.py`, `README.md`, and `decisions/adr-1.md` and `adr-2.md` (with `supersedes: ADR-1`);
  - `node_modules/dep/index.js`.
- `tests/dashboard/catch_up_ui.js` (new). It lifts the block out of `index.html` in the pattern of `config_form_dump.js`, and is driven from a Python test the way the other harnesses are.

- [ ] Write failing tests. Each scenario ends with `settle`, then `assert_graph_matches_fresh_scan` and `assert_matches_fresh_apply`.
  - **Rename.** Rename `pkg/mod.py` to `pkg/model.py`, and `decisions/adr-1.md` to `decisions/0001-start.md`. `Adr:ADR-1` keeps its incoming `SUPERSEDES` from `adr-2`.
  - **Folder trash.** `shutil.move(pkg/sub, <tmp outside>)`. No node is left under `pkg/sub`.
  - **Folder move.** Move nested `pkg/sub` to `tools/sub`, and then top-level `tools` to `scripts`. Then edit `scripts/run.py`, adding a function, and check that the edit appears (the reschedule works).
  - **Edits while off.**
    1. Stop the agent's watcher.
    2. Edit `pkg/mod.py`, delete `tools/run.py`, add `newtop/c.py`, and re-id `adr-2.md`.
    3. Start a fresh `live_agent` on the same registry.
    4. Assert that the `catch_up` events `running` and then `done` were published.
  - **Git checkout of another branch.**
    1. Commit everything on `main`.
    2. On `feature`, make several changes and commit them:
       - rename `pkg/` to `lib/`;
       - delete `decisions/adr-2.md`;
       - edit `README.md`;
       - add `lib/extra.py`.
    3. Go back to `main`, and start the agent and settle.
    4. Run `git checkout feature`, then settle and compare.
    5. Run `git checkout main`, then settle and compare again.
  - **Dashboard pill (harness).** A `catch_up` `running` event for the selected repository shows `Catching up…`, and `done` or `failed` restores the previous pill. An event for another repository has no effect unless `__all__` is selected.
- [ ] Implement the dashboard handler and the test helpers. If a scenario exposes a bug in Tasks 1–3, fix it in the module it belongs to, with a unit test there.
- [ ] Update docs (spec "Docs to update"):
  - **README.** Delete the "Known gaps" paragraph under **Live updates** and the folder line under "Known limits of the filesystem provider". Add a short "Keeping up with changes" paragraph near the watcher description. It covers:
    - renames and folder moves are picked up live;
    - on start, and after a git checkout or pull, DevGraph checks for changes it missed;
    - the tray says "catching up" and the dashboard shows "Catching up…";
    - `devgraph rescan` is the fallback.
  - **PROJECT_STATUS.** Replace the "Known gaps" sentence in the `devgraph/watcher/` entry. In the `devgraph/watcher/` and `devgraph/agent/` entries, cover:
    - renames and folder events;
    - top-level reschedule;
    - the batch lock;
    - catch-up, and the post-git reconcile.

    Add a shipped line. Under "Still open", list:
    - the schema rescan running outside the batch lock;
    - overflow detection;
    - macOS.
- [ ] `uv run pytest -q`, plus the `tests/dashboard/*.js` harnesses. Commit "Test watcher scenarios end to end and show catch-up status".
