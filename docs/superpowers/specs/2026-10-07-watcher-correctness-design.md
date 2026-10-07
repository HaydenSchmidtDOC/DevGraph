# Watcher correctness — design

Epic #1 slice. Upstream epic: HaydenSchmidtDOC/DevGraph#1. Revised after a
code-checked review. The deadlock (C1) and dead-watch (C2) findings were
confirmed with watchdog probes.

## Problem

The watcher keeps the graph current only for in-place edits, creates and
single-file deletes. Checked against `devgraph/watcher/manager.py` and a live
probe on Linux (watchdog 6.0.0, inotify):

| Action | What reaches `on_changes` today | Result |
| --- | --- | --- |
| Rename `pkg/a.py` → `pkg/a2.py` | changed `{pkg/a2.py}`, deleted `{}` | `pkg/a.py`'s nodes stay. `on_moved` never queues the source. |
| Rename nested `pkg/sub` → `pkg/sub2` | changed `{pkg/sub2/b.py}` (watchdog's sub-moved events) | `pkg/sub/b.py`'s nodes stay. |
| Trash or move `pkg/sub2` out of the repo | nothing | Every file's nodes stay. Directory events are dropped (`is_directory` returns at lines 311, 330, 349, 373). |
| Rename a top-level directory `pkg` → `lib` | nothing | The old nodes stay. The old emitter keeps running and reports `lib/` files as `pkg/...`, so later edits are attributed to paths that do not exist and are dropped. |
| Delete a top-level directory, then recreate it | nothing | The inotify emitter stops itself on `IN_DELETE_SELF` but stays in `observer._emitter_for_watch`, so `schedule()` on the same path is a no-op. Probed: rmtree, mkdir, schedule, then an edit gave no event. |
| Create a new top-level directory with files | nothing | The root is watched non-recursively, so nothing below a new top-level directory is ever seen. |
| Edits, `git pull` or a checkout while the agent is off | nothing | Nothing reconciles on start; only `devgraph rescan` does. |

On Windows a deleted directory arrives as `FileDeletedEvent(dir)`
(`read_directory_changes.py` cannot stat what is gone). It reaches
`remove_paths`, which cleans filesystem- and docs-provider nodes below it but
not language nodes, which are deleted by exact `source_file`.

Related defects found while reading:

- `_fire_changes` calls `on_changes` while holding the handler's event lock.
  The observer's dispatch thread then blocks for as long as a batch indexes.
- `last_indexed` is written when a scan or batch *finishes* (every
  `mark_indexed` caller). An edit made during a scan can be older than the
  stamp and still unindexed.
- `remove_paths`' provider passes (`filesystem.sync_absent` and
  `delete_extracted_nodes`) delete everything at or below a gone path, with no
  disk check. A folder deleted and recreated in one batch therefore loses the
  provider nodes `index_paths` has just written for it.
- `engine.list_indexed_files`, the graph side of `prune_stale_files`, returns
  only `source_file`, `file` and `Module.name`. Provider nodes carry `path`.
  A file that only a provider represents (for example `logo.png` as a `File`)
  is never pruned.

## Goal

After any sequence of file operations, whether the agent was running or not,
the repository's graph equals a fresh `full_scan` of the same files. The
exceptions are listed under "Limits" below. No new config knob.

## Decisions

| # | Decision |
| --- | --- |
| W1 | **A rename is a delete of the source plus a create of the destination**, in one batch when both are seen by one emitter. |
| W2 | **Deletes are expanded to exact file paths in `remove_paths`**, from the graph's own file list, which includes provider paths. Directory creates and in-repo moves are walked. |
| W3 | **Top-level directory changes trigger a watch reconcile**, which runs on its own thread, replaces dead or stale watches, then walks new folders. **Handler code never takes `manager._lock`.** |
| W4 | **One batch lock per repository, owned by the manager**, shared by live batches, catch-up, the schema rescan and dashboard registration. Events keep collecting while a batch runs. |
| W5 | **Catch-up is incremental**, with `since` snapshotted before the watch starts. It runs whenever the watcher starts watching a repository. A never-indexed repository is not scanned. |
| W6 | **The debounce is enough for delivered events. A git operation also triggers a catch-up**, from the start of the git burst, to repair dropped events. |
| W7 | **`last_indexed` is the start of the work it covers**, held back by a per-repository floor while a batch has failed. |
| W8 | **Docs provider unchanged.** The watcher delivers the pair; front-matter keys already handle both orders. |
| W9 | **Say it plainly**: log lines a non-developer can read, and a "catching up" state on the tray and the dashboard. |
| W10 | **Shared-node attribution does not depend on claim order.** `sources` stays sorted, and the attributed properties come from the claim of `min(sources)`. |

### W1: rename and move of a file

`on_moved` (file), after an explicit containment check that both paths are
inside the repository root (`is_within`):

- **Source.** Queue it for deletion unless it is ignored, outside the
  repository, or empty (a Windows rename pair split across two reads reports
  an empty source). Drop it from `changed`.
- **Destination.** If it is a tracked file inside the repository, queue it as
  changed and drop it from `deleted`. Otherwise nothing more happens: a move
  out of the repository, or into an ignored directory, is a delete.
- **Case-only rename on Windows** (`normcase(src) == normcase(dest)`). The
  destination is a change. The source's exact lexical key is removed in
  `remove_paths` (W2), which never resolves the missing leaf, so the new
  file's nodes are not touched.
- **Move in from outside the repository.** inotify reports an unmatched
  `IN_MOVED_TO` and ReadDirectoryChangesW reports an add, so both arrive as a
  create, which is already handled.
- **Move between top-level directories.** watchdog pairs `IN_MOVED_FROM` and
  `IN_MOVED_TO` by cookie only within one inotify instance, and every
  scheduled watch has its own instance. A move between two top-level
  directories therefore arrives as a delete and a create from two emitters
  into the same handler. They land in one batch when both arrive inside the
  debounce window, which is the usual case. If they split, each batch is
  still correct on its own (W6).

Atomic saves still work: the destination is cleared from `deleted`, and the
temp source's delete removes nothing.

Order inside a batch is unchanged: `index_paths(changed)`, then
`remove_paths(deleted)`.

### W2: deletes and directory events

**The watcher has no engine, so expansion lives in `remove_paths`.**

1. **Gone keys are computed lexically.** Resolve the parent directory only,
   then append the leaf name. The missing leaf is never resolved: on Windows,
   resolving a missing `Foo.py` can return an existing `foo.py`.
2. **One graph query per call.** It is made whenever `paths` is non-empty.
   `_graph_files(engine, repo_id, repo_root)` returns:
   - `list_indexed_files` (language keys);
   - docs-provider nodes' `path`;
   - filesystem-provider nodes' `path` for the file-kind label of the applied
     schema. Folder-kind nodes are never included.

   The provider paths are included only when the schema is not pending, which
   matches when the provider passes run.
3. **Each gone key `g` expands** to every graph file equal to `g` or strictly
   below it (`g + "/"` prefix; never for `.`).
4. **A path is kept when it is present on disk.** An expanded path that is an
   indexable file on disk is dropped from the removal. Presence is checked
   case-exactly **for every path component**. Starting at the root, each
   component must appear exactly in its parent's `os.listdir` names.
   - Checking the leaf alone is not enough. After a Windows folder rename
     `Pkg` → `pkg`, `Pkg/a.py` still opens, so a leaf-only check would keep
     the stale `Pkg/**` nodes.
   - The listings are memoised per call.
5. **Removals are exact.**
   - **Language deletes** run per exact file path.
   - **The provider passes** (`sync_absent`, `_take_over_keys`,
     `delete_extracted_nodes`) receive the exact file paths too. The gone
     directory itself is added only when no indexable file remains below it on
     disk, so that its now-empty folder nodes go.
   - **Folder nodes** emptied on disk are still found by `sync_absent`'s
     ancestor check.

This fixes:

- Windows' `FileDeletedEvent(dir)`, without asking what kind of path it was;
- a folder deleted and recreated in one batch, as a checkout does;
- a junction: its files are keyed by their target, never by the junction's
  lexical path, so removing a junction expands to nothing.

`prune_stale_files` uses `_graph_files` as its graph side, so provider-only
files deleted while the agent was off are pruned too.

**Watcher side:**

- **`DirDeletedEvent`.** Queue the directory (unless ignored) as deleted. This
  covers trash, and a move out of the repository.
- **`DirMovedEvent` inside the repository.** Queue the source as deleted.
  Walk the destination with `walk.indexable_paths_under(repo_root, dest)`,
  which applies `_walk`'s ignore, symlink and junction rules relative to the
  root, and queue its files as changed.
  - The per-child sub-moved events from watchdog are harmless duplicates.
  - A destination under an ignored directory is a delete only, and an ignored
    source is a create only.
- **`DirCreatedEvent`** (new, or moved in from outside). Walk it and queue its
  files.
- **Windows cross-folder move of a directory.** It arrives as
  `FileDeletedEvent(pkg/sub)` plus `DirCreatedEvent(tools/sub)` (with
  sub-created events), and is handled by the two rules above.
- **Walks run outside the handler's `_lock`.** The paths are added under it
  afterwards.

### W3: top-level directories

The per-child watch layout stays. It exists to keep `.venv` and `build` out
of ReadDirectoryChangesW's buffer.

**Lock rule: handler code never takes `manager._lock`.** watchdog holds
`observer._lock` while it dispatches to the handler. `stop()` holds
`manager._lock` and calls `observer.stop()`, which takes `observer._lock`. A
handler that took `manager._lock` would deadlock against it.

1. **Signal.** Any create, delete or move event, of either kind, whose path or
   destination is a direct child of the root calls
   `manager._request_reconcile(repo_id)`.
   - It takes only a small `_reconcile_lock`.
   - It sets a pending flag and starts a coalescing timer (default 0.2 s).
   - "Either kind" matters because Windows reports a deleted directory as
     `FileDeletedEvent`.
2. **Reconcile.** It runs on the timer thread under `manager._lock`, and
   returns at once if the manager is stopping or the repository is no longer
   in `_observers`.
   - **Desired directories** are the root's children that are directories,
     with no ignored names, no symlinked directories, and no junction whose
     target is outside the repository or under an ignored directory. This
     mirrors `walk._walk`. Today `child.is_dir()` follows symlinks and
     junctions.
   - **A watch is kept** only if its emitter `is_alive()`, its path exists,
     and its path is still a desired directory. Every other watch is
     unscheduled (`observer.unschedule(watch)`, which works on a dead emitter
     because it is still registered). That covers a deleted directory, a
     renamed directory's stale emitter, and a directory that became ignored.
   - **Missing desired directories** are scheduled first, and then walked
     (outside `manager._lock`). Their files are queued as changed, so files
     written between the event and the new watch are not lost.
3. **Cancellation.** `stop()` cancels a pending reconcile timer. The reconcile
   timer's `_reconcile_lock` is never held while calling into the observer.

### W4: batch lock

`WatcherManager._batch_locks: dict[repo_id, threading.Lock]` is created on
demand, so it survives a handler being recreated by stop and start or by
refresh.

- `_fire_changes` takes the repository's batch lock, swaps the sets under
  the handler's short `_lock`, releases `_lock`, and calls `on_changes`.
- `run_exclusive(repo_id, fn)` looks the lock up under `manager._lock`,
  releases `manager._lock`, and then runs `fn` under the batch lock.
- `SchemaRescanScheduler` takes an optional `run_exclusive`, and the agents
  pass the watcher's, so a schema-applying `full_scan` no longer interleaves
  with a live batch.
- Dashboard registration runs inside the agent process, because the tray
  and headless agents serve the dashboard.
  - `build_app` takes an optional `run_exclusive`, and registration's
    `full_scan` and stamp run through it.
  - A watcher that picks up the new repository mid-scan therefore waits for
    the scan, and its catch-up then sees the stamp (W5).
- **Not covered:** writers in other processes (`devgraph add`, `devgraph
  rescan`) and the git-history sync. The lock serialises this process's file
  indexing for one repository, not every writer.

**Timers.**

- Debounce, reconcile and post-git timers come from an injectable
  `timer_factory` (default `threading.Timer`).
- `_RepoEventHandler.flush()` fires pending changes now, if there are any.
- `stop()` cancels every pending debounce timer. The changes they held are
  repaired by the catch-up that runs on the next start or Resume.
- A catch-up that is already running is not interrupted by Pause or Quit. It
  runs on a daemon thread, `stop()` does not wait for it, and its failure
  warning is suppressed while the agent is stopping.

### W5: catch-up

**Measured** on this machine, on a copy of `devgraph/` and `docs/` (134
indexable files) against local Neo4j:

| Operation | Time |
| --- | --- |
| `full_scan` | 44–54 s |
| Incremental, nothing changed (walk, stat, one query, prune) | 0.04 s |
| Incremental, 20 `.py` files touched (64 indexed after the reverse-dependent expansion) | 17–18 s |

A full scan costs about 0.35 s per file, so about 12 minutes on every start
for a 2,000-file repository. **Catch-up is incremental.**

`dispatch.catch_up(engine, repo_id, repo_root, since, docs_path, mentions_enabled) -> CatchUp(indexed, pruned, checked, offered, unknown)`:

1. `prune_stale_files(...)` against `_graph_files`. This removes files gone
   from disk, provider-only ones included, through `remove_paths`, so docs key
   takeover applies.
2. Walk `keyed_indexable_paths(repo_root)`. Only a file `index_paths` would
   write something for is a candidate: a built-in extractor routes it, or a
   declared schema provider represents it (`_would_index`). A `.txt`, `.json`
   or `.png` with no filesystem type declared is never offered, so it does
   not cost a no-op catch-up anything. A candidate is due when either holds:
   - its key is not in `_graph_files`;
   - its change stamp is at or after `since - 5 s`.

   The change stamp is `max(st_mtime_ns, st_ctime_ns)` on POSIX and
   `max(st_mtime_ns, st_birthtime_ns)` on Windows. The second term catches
   files whose mtime was preserved, such as `cp -p`, tar, unzip or
   `rsync -a`. The margin covers FAT and SMB timestamp granularity, and the
   gap between an edit and its event.
3. `index_paths(due)`.

Because `_graph_files` includes provider paths, a `File`-only file is
"known", so it is not re-offered on every start. `offered` counts the due
files and `unknown` those of them the graph had no file for.

**When it runs, and with what `since`.**

- **Start.** `_start_single` reads `last_indexed` *before* `observer.start()`,
  then, once the observer runs, starts a daemon thread that calls
  `on_catch_up(repo_id, since=snapshot)` through `run_exclusive`.
  - A live batch that runs before the catch-up gets the lock cannot raise
    `since` past edits made while the agent was off.
  - This covers agent start, Resume, and `watch enable` or a registration
    picked up by `refresh()`. A repository with watching disabled gets no
    catch-up.
- **Inside the lock, a `None` snapshot is re-read.** Registration in this
  process may have just finished under the same lock.
- **Never indexed** (`last_indexed` still `None`). Skip, and log
  `DevGraph hasn't indexed <repo> yet; run "devgraph rescan <repo>"`.
  `devgraph add`, `rescan` and dashboard registration own the first scan, so
  there is never a parallel `full_scan`.
- **Live events that arrive during a catch-up** queue behind it and re-index
  whatever they name. Indexing is idempotent.

### W6: git operations while running

The debounce is trailing (500 ms, reset on every event). A checkout or pull
writes its files in well under that, so it lands in one batch. A slower one
splits.

- Each event names a path's final state, and the batch sets keep only the
  last kind per path, so split batches converge.
- The only cost of a split is the existing by-name gap: a referrer indexed
  before its target stays unlinked until a rescan.

So the debounce is enough **for delivered events**. It is not enough when
events are dropped. ReadDirectoryChangesW drops every pending event for a
watch when its buffer overflows on a large checkout, and inotify can overflow
`max_queued_events`. A dropped event for a modified existing file is the hard
case: the live batches that did run have stamped `last_indexed` after its
mtime.

**Post-git catch-up.**

- **What is watched.** `.git` itself non-recursively (HEAD, `packed-refs`,
  `index.lock`), `.git/refs` non-recursively (`refs/stash`: `git stash pop`
  and `apply` move neither HEAD nor a branch), and `.git/refs/heads`
  recursively. Remote-tracking refs and tags are not watched, so a background
  fetch triggers nothing.

- **What a burst is.** Checkout, pull, merge, reset and stash take
  `.git/index.lock` before they write files. IDEs also run `git status`, which
  takes and releases the same lock without moving HEAD. So a lock counts only
  when it is tied to a HEAD or ref change.
  - Before its relevance filter, `_GitStateEventHandler` records the newest
    lock's creation time, and the time it was released (deleted, or renamed
    onto `index`).
  - When a relevant HEAD, `packed-refs` or `refs/` event arrives at `t_h`,
    `burst_start` is that lock's creation time if both hold:
    - it was created before `t_h`;
    - it is still held, or was released no more than `GIT_LOCK_WINDOW_S`
      (5 s) before `t_h`.

    Otherwise `burst_start` is absent. A fetch that only moves refs, or a
    `git status` lock from minutes ago, gives no `burst_start`.
  - The window is measured from the lock's release, not its creation, so a
    long checkout that holds the lock for a minute still counts.
  - Lock records older than the window are discarded. The burst state is
    cleared when the burst fires.
- When the burst's debounce fires, the manager schedules a catch-up 2 s later
  (`git_catch_up_delay_s`, injectable). Its `since` is
  `min(last_indexed, burst_start)`, or `last_indexed` when there is no
  `burst_start`.
- The catch-up runs through `run_exclusive`. Repeats coalesce.
- **The git-history sync runs after the catch-up**, in the same job under
  the batch lock, not when the burst fires. Its recency writes only
  annotate nodes that exist (MATCH, not MERGE), so a sync that ran while the
  checkout's files were still being indexed left their modules without
  recency or `MODIFIES` edges for good. A never-indexed repository, which
  gets no catch-up, syncs at once.
- **Every successful catch-up of a repository with a `.git` folder ends with
  the sync**, not only the post-git one: the start catch-up (so resume too)
  and a retry as well. A stop or pause within the 2 s delay cancels the
  post-git job, and a commit made while the agent was off has no burst at
  all; the next start's sync covers both. With HEAD unmoved the sync is a
  no-op. A failed catch-up (`on_catch_up` returns False) skips it: a
  fast-mode sync never revisits its commits, so it waits for the retry.
  `stop()` cancels a pending git burst; the next start covers it.
- When nothing was missed it costs about 0.04 s plus re-indexing what the
  checkout touched, which is bounded by the files git wrote.
- DevGraph never shells out to git for this. A `git diff` or `git status`
  could run programs the repository configures.

**Non-git overflows** are only partly repaired. Examples are a huge copy, or
a build writing into a watched folder. The next start's catch-up adds files
that are new and prunes files that are gone, but **a modified existing file
whose event was dropped is repaired only by `devgraph rescan`**, because its
mtime is older than the stamp. This is listed under Limits.

### W7: stamps and failure floors

`mark_indexed(repo_id, at=None)` gains an optional timestamp. **Every caller
stamps the start of the work it covers**:

- `cli/main.py` (register, about line 94; rescan, about line 274);
- `dashboard/routes.py` (registration, about line 320);
- `agent/schema_rescan.py` (about line 99);
- live batches;
- catch-up.

The agents' glue moves into one place, `devgraph/agent/sync.py` `RepoSync`.
Tray and headless each had an identical `_on_changes`, and both would
otherwise grow the same catch-up, floor and status code. `RepoSync` holds:

- `on_changes(repo_id, changed, deleted)`. It captures `started = now()` on
  entry. Under W4 that is the swap time. It indexes, then stamps
  `min(started, floor)`.
- **A per-repository floor**, kept in memory. While it is set, every stamp is
  `min(started, floor)`.
  - A failed live batch sets it to `min(started, last_indexed)`, the last good
    stamp, not to `started` alone. The batch's events were queued before it
    took the batch lock, and a catch-up or schema rescan can hold that lock
    for minutes, so they can predate `started` by far more than the 5 s
    margin. A floor at `started` would let the retry, and every later stamp,
    pass them for good.
  - A failed catch-up sets it to `min(floor, since)`.
  - A failure also asks the watcher for a catch-up, 30 s later.
  - The health loop asks again, at once, when Neo4j recovers. A request with
    a shorter delay replaces a pending one; otherwise requests coalesce.
  - A catch-up with `since <= floor` that succeeds clears the floor.
- `on_catch_up(repo_id, since)`. It uses `since = min(since, floor)`, captures
  `started`, runs `catch_up`, then stamps `min(started, floor-after)`. It
  publishes status (W9).
- `now` is injectable.

Tray and headless keep `_on_changes` as one-line delegates.

**Wiring.** `RepoSync` and `WatcherManager` depend on each other. The agent
builds `RepoSync` first, with
`request_catch_up=lambda *a: self._watcher.request_catch_up(*a)`, then builds
the watcher with `on_changes=self._sync.on_changes` and
`on_catch_up=self._sync.on_catch_up`. The lambda resolves `self._watcher`
only when it is called.

**`request_catch_up` never synchronously takes `manager._lock` or a batch
lock.** `RepoSync.on_changes` calls it while holding the batch lock. It
records the minimum `since` under its own small `_catch_up_lock` and starts or
keeps a timer. The timer thread then goes through `run_exclusive`.

### W10: shared-node attribution

Some nodes are produced by several files, for example `Datastore` and
`Endpoint`. These are the nodes written with a `source` property, through the
`sources` clauses in `engine.py` at about lines 323 and 488.

Today:

- `SET n += properties` makes `source`, `library` and every other written
  property last-writer-wins.
- `sources` is appended in claim order.
- `index_paths` sorts its batch, so a full scan writes in path order. A live
  batch writes in a different order (for example a rename re-claims last), so
  live and fresh results differ.

**Rule.** For a shared node, the engine keeps a per-claim record of what each
claiming file wrote, `claims` (a JSON string mapping the source path to its
properties, since Neo4j has no map properties).

- **On a claim**, it updates `claims[source]` and sets `sources` to the
  sorted keys. The node's written properties become exactly `claims[min(sources)]`,
  with `source = min(sources)`. Properties that the previous attribution had
  and the new one lacks are removed.
- **On an unclaim** (`delete_nodes_by_source_file`), it drops the entry. If
  none is left the node is deleted; otherwise the node is re-attributed from
  the new minimum the same way.
- **Both run read-modify-write in Python inside one write transaction.**
- **Legacy nodes.** A node written before this change has `sources` but no
  `claims`. Each listed source that has no `claims` entry is treated as
  claiming the node's current properties. The next rescan makes the node
  exact.

This is a behaviour change to existing shared-node writes. A full scan's
result is the same as today only where the alphabetically first file was
also the last writer. The comment in `dispatch.py` at about line 614 changes
to state the new rule.

### W8: docs read cache and "incremental equals fresh apply"

Nothing changes in `devgraph/indexer/providers/`.

- **A live rename of an id-keyed file** delivers the destination as changed
  and the source as deleted. `index_paths` merges the entry onto the new path,
  and `remove_paths`' takeover sees the id still owned. The keys spec,
  "Rename keeping the id, in either event order", already covers this, so the
  node and its incoming links survive.
- **A rename while the agent was off.** Catch-up prunes first (takeover moves
  the id) and then indexes, so the result is the same.
- **Exact provider paths.** W2's exact paths change what the provider passes
  receive. A gone directory becomes the exact files below it, plus the
  directory itself only when nothing indexable remains below it on disk.
  - Docs nodes are keyed by their owning file's `path`, so exact paths
    remove exactly the entries the at-or-below match removed before, minus
    any recreated file.
  - `_take_over_keys` sees the same exact list.
- **The read cache** keys by `(root, rel)` and validates by stat identity, so
  renames, catch-up and checkouts look like ordinary saves and deletes to it.
- **Tests.** `assert_matches_fresh_apply` and the docs fuzz keep passing
  unchanged. The new live tests call it as well as a whole-graph comparison.

### Windows

- **Renames** come as a paired `RENAMED_OLD_NAME` and `RENAMED_NEW_NAME`,
  classified by `os.path.isdir(dest)`.
  - If the destination is already gone, a directory rename arrives as a file
    move with an untracked destination, so the source becomes a delete. That
    is correct.
  - A pair split across reads gives an empty source, which W1 ignores.
  - Case-only renames are handled by W1 and W2.
- **Deleted directories** arrive as `FileDeletedEvent`. W2 expands them and W3
  reconciles watches on them.
- **The Recycle Bin** is a rename out of the watched tree, so only the top
  path is reported. W2 covers it.
- **Buffer overflow** is repaired by W6 for git operations, and partly by W5
  otherwise (see Limits).
- **Junctions.** One is watched only if its target is inside the repository
  and not ignored. Its files are keyed by the target.
- **Change stamp.** It uses `st_birthtime_ns`, not the deprecated Windows
  `st_ctime`.
- **CI.** The Windows CI job has no Neo4j. Every fake-event and reconcile unit
  test runs there, and the live tests skip.

### W9: UX

Log lines, at INFO unless noted:

- `Checking <repo> for changes made while DevGraph wasn't watching…`
- `<repo> is up to date (checked 1,234 files in 0.2 s)`
- `Caught up on <repo>: 12 files updated, 3 removed (4.1 s)`. "Updated" is
  `index_paths`' return value (files actually indexed, referrers included),
  and "removed" is `prune_stale_files`' return value.
- `Folder renamed in <repo>: pkg → lib` and `Folder removed from <repo>: pkg/sub`.
  One line per directory event, never per child.
- `<repo>: checked 3 files changed by a git operation`, when a post-git
  catch-up offered any file. Files git wrote are offered whether or not the
  live batches already indexed them, so this does not claim a miss.
- `<repo>: found 2 files the live watcher missed after a git operation; updated them`,
  only for what the live batches can't have handled: files the graph had no
  file for, and files pruned. Otherwise DEBUG.
- `DevGraph hasn't indexed <repo> yet; run "devgraph rescan <repo>"`
- WARNING `Couldn't update <repo>; DevGraph will retry, or run "devgraph rescan <repo>"`,
  with `exc_info`, once per failure streak; while it keeps failing, again
  every 5 minutes without the traceback, and at DEBUG in between. Not logged
  while stopping. The streak ends when a catch-up clears the floor.

The existing `changes detected for …` line stays.

Status:

- **Tray.** The tooltip reads `DevGraph (catching up)` while any catch-up
  runs. Paused and warning states take precedence.
- **Events.** `RepoSync` publishes
  `{"type": "catch_up", "repo_id", "state": "running" | "done" | "failed", "changed", "deleted"}`.
  `done` with a non-zero count is followed by the existing `reindexed` event.
- **Dashboard.** The Entities card's pill (`entityLivePill`) shows
  `Catching up…` while a `running` event for the selected repository (or any,
  under `__all__`) has no matching `done` or `failed`. A dashboard opened
  mid-catch-up does not know about it. That is acceptable, since a catch-up of
  normal size is short.

## Limits

These are documented, with `devgraph rescan` as the fallback:

- **Modified existing files whose events were dropped outside a git
  operation** are repaired only by `devgraph rescan` (W6).
- **Timestamps.** Catch-up trusts change stamps. It misses a file whose
  content changed while the agent was off but whose stamps all predate
  `last_indexed - 5 s`. Examples:
  - a clock moved backwards;
  - a network share with a skewed server clock;
  - on Windows, a file overwritten in place by a tool that restores its mtime
    (the creation time stays old).
- **By-name cross-batch referrer gaps** listed in PROJECT_STATUS.
- **Writers in other processes** (`devgraph add`, `devgraph rescan`) are not
  serialised with the agent (W4).
- **macOS (FSEvents)** is not a supported agent platform.
- **Linked worktrees and submodules** have a `.git` file, not a directory,
  so nothing under it is watched: no post-git catch-up (and no git-history
  sync) runs for them. Their file events are still watched, and the catch-up
  on start still runs.

## Docs to update

- **README.** Remove the "Known gaps" paragraph under **Live updates** and
  the folder limit from "Known limits of the filesystem provider". Add a short
  "Keeping up with changes" note: renames and folder moves, catch-up on start,
  after git operations, what the tray and dashboard show, and the Limits
  above.
- **PROJECT_STATUS.** Replace the watcher entry's "Known gaps" sentence,
  extend the `devgraph/watcher/` and `devgraph/agent/` entries, and add the
  slice to the shipped list with the Limits as "Still open". In the
  `index_paths` batch-ordering bullet, add W10's rule for shared nodes, and
  note that it is a behaviour change.
