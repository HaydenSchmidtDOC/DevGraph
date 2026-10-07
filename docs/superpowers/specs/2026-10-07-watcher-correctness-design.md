# Watcher correctness — design

Epic #1 slice. Upstream epic: HaydenSchmidtDOC/DevGraph#1.

## Problem

The watcher keeps the graph current only for in-place edits, creates and
single-file deletes. Checked against `devgraph/watcher/manager.py` and a live
probe on Linux (watchdog 6.0.0, inotify):

| Action | What reaches `on_changes` today | Result |
| --- | --- | --- |
| Rename `pkg/a.py` → `pkg/a2.py` | changed `{pkg/a2.py}`, deleted `{}` | `pkg/a.py`'s nodes stay. `on_moved` never queues the source. |
| Rename nested `pkg/sub` → `pkg/sub2` | changed `{pkg/sub2/b.py}` (watchdog's sub-moved events) | `pkg/sub/b.py`'s nodes stay. |
| Trash or move `pkg/sub2` out of the repo | nothing | Every file's nodes stay. Directory events are dropped (`is_directory` returns at lines 311, 330, 349, 373). |
| Rename a top-level directory `pkg` → `lib` | nothing | The old nodes stay, **and later edits under `lib/` are never seen.** Each top-level directory has its own recursive watch, scheduled once at start. |
| Create a new top-level directory with files | nothing | The root is watched non-recursively, so nothing below a new top-level directory is ever seen. |
| Edits, `git pull` or a checkout while the agent is off | nothing | Nothing reconciles on start; only `devgraph rescan` does. |

On Windows a deleted directory arrives as `FileDeletedEvent(dir)`
(`read_directory_changes.py` cannot stat what is gone). It reaches
`remove_paths`, which cleans filesystem- and docs-provider nodes below it but
not language nodes, which are deleted by exact `source_file`.

README "Known limits of the filesystem provider" admits the folder case.

Two further defects surfaced while reading:

- `_fire_changes` calls `on_changes` while holding the handler's event lock.
  The observer's dispatch thread therefore blocks for as long as a batch
  indexes, which is how batches for one repository happen to be serialized
  today.
- `last_indexed` is written when a batch *finishes*. An edit whose event was
  still pending when the agent stopped can have an mtime older than that.

## Goal

After any sequence of file operations, whether the agent was running or not,
the repository's graph equals a fresh `full_scan` of the same files. This
excludes `Commit` and `Repository` nodes, and the by-name cross-batch gaps
PROJECT_STATUS already lists. No new config knob.

## Decisions

| # | Decision |
| --- | --- |
| W1 | **A rename is one delete plus one create in the same batch.** |
| W2 | **Directory deletes expand in `remove_paths`, from the graph's own file list.** Directory creates and in-repo moves are walked. |
| W3 | **Top-level directory changes reschedule that directory's watch.** |
| W4 | **One batch lock per repository**, shared by live batches and catch-up. Events keep collecting while a batch runs. |
| W5 | **Catch-up is incremental**: prune, then index what is new or newer than the last batch. It runs whenever the watcher starts watching a repository. |
| W6 | **The debounce is enough for delivered events. A git state change also triggers a catch-up**, to repair dropped events. |
| W7 | **No docs-provider change.** The watcher delivers the pair; front-matter keys already handle both orders. |
| W8 | **Say it plainly**: log lines a non-developer can read, a "catching up" state on the tray and the dashboard. |

### W1: rename and move of a file

`on_moved` (file):

- **Source.** Queue it for deletion unless it is ignored, or empty (Windows
  can split a rename pair across two reads and report an empty source). Drop
  it from `changed`.
- **Destination.** If it is a tracked file, queue it as changed and drop it
  from `deleted`. If it is outside the repository or under an ignored
  directory, nothing more happens: a move out is a delete.
- A move in from outside the repository arrives as a create, from both
  inotify and ReadDirectoryChangesW, and is already handled.
- A move between two top-level directories crosses two watches. It arrives as
  a delete and a create in the same handler, so in the same batch.

The atomic-save case still works. The destination is cleared from `deleted`,
and the temp source's delete is a no-op in `remove_paths`.

Order inside a batch is unchanged: `index_paths(changed)`, then
`remove_paths(deleted)`. `test_moving_a_file_in_one_batch_keeps_only_the_destination`
already pins that order for the filesystem provider.

### W2: directory delete, move and create

The watcher has no engine, so expansion of a deleted directory lives where
the engine is:

- **`remove_paths` expands every gone path.** One `engine.list_indexed_files`
  query per call gives every indexed path below a gone path (`rel + "/"`
  prefix). Those are added to the paths removed, except any that is an
  indexable file on disk again, such as a checkout that removes and recreates
  a directory in one batch.
  - This also fixes Windows' `FileDeletedEvent(dir)` without asking the
    watcher what kind of path it was.
  - A junction's files are keyed by their target, never by the junction's
    lexical path, so removing a junction expands to nothing, which is right.
- **Watcher, `DirDeletedEvent`.** Queue the directory path (unless ignored)
  as deleted. This covers trash, and a move out of the repository, which
  inotify reports as an unmatched `IN_MOVED_FROM`.
- **Watcher, `DirMovedEvent` inside the repository.** Queue the source as
  deleted, and the destination's indexable files as changed. They come from
  `walk.indexable_paths_under(repo_root, dest)`, which applies the walk's
  ignore, symlink and junction rules relative to the repository root.
  - inotify and Windows also emit per-child sub-moved events for recursive
    watches. They are harmless duplicates in the same sets.
  - The root watch is non-recursive, so for a top-level move the walk is the
    only source.
  - A destination under an ignored directory is a delete only, and an ignored
    source is a create only.
- **Watcher, `DirCreatedEvent`** (new, or moved in from outside). Walk it and
  queue its indexable files. Empty directories produce nothing, as in a full
  scan.

### W3: top-level directories

The per-child watch layout stays. It exists to keep `.venv` and `build` out
of ReadDirectoryChangesW's buffer.

- When the handler sees a directory create, delete or move whose path (or
  destination) is a direct child of the repository root, it asks the manager
  to reconcile that repository's watches.
- The manager unschedules watches whose directory is gone or renamed, and
  schedules any missing ones.
- Scheduling mirrors `walk._walk`. It skips ignored names, skips symlinked
  directories, and skips junctions whose target is outside the repository.
  Today `child.is_dir()` follows both.

### W4: batch lock

`_RepoEventHandler` gets a second lock, `_batch_lock`.

1. `_fire_changes` takes `_batch_lock`.
2. It swaps the sets under the short `_lock`, and releases `_lock`.
3. It calls `on_changes`.

The observer thread therefore only ever waits for the swap, never for
indexing. Batches for one repository stay serialized.

`WatcherManager.run_exclusive(repo_id, fn)` runs `fn` under the same
`_batch_lock`. Catch-up uses it.

Out of scope: the schema rescan scheduler's `full_scan` still runs outside
this lock, as it does today.

### W5: startup catch-up

**Measured** on this machine, on a copy of `devgraph/` and `docs/` (134
indexable files) against local Neo4j:

| Operation | Time |
| --- | --- |
| `full_scan` | 44–54 s |
| Incremental, nothing changed (walk, stat, one `list_indexed_files`, prune) | 0.04 s |
| Incremental, 20 `.py` files touched (64 indexed after the reverse-dependent expansion) | 17–18 s |

A full scan costs about 0.35 s per file. A mid-size repository of 2,000 files
would take about 12 minutes on every start, against well under a second when
nothing changed. **Recommend incremental**, with a full scan only when the
repository has never been indexed (`last_indexed` is `None`).

`dispatch.catch_up(engine, repo_id, repo_root, since, docs_path, mentions_enabled) -> CatchUp(indexed, pruned, checked)`:

1. `since is None`: run `full_scan` and return.
2. `prune_stale_files(...)`. This removes files gone from disk, including the
   old paths of renames made while the agent was off, through `remove_paths`,
   so docs key takeover applies.
3. Walk `keyed_indexable_paths(repo_root)`. A file is due when either holds:
   - its key is not in `engine.list_indexed_files(repo_id)`;
   - its change stamp is at or after `since - 2 s`.

   The change stamp is `max(st_mtime_ns, st_ctime_ns)` on POSIX, and
   `max(st_mtime_ns, st_birthtime_ns)` on Windows. The second term catches
   files whose mtime was preserved, such as `cp -p`, tar, unzip or `rsync -a`.
   Their change time on POSIX, and their creation time on Windows, is the
   moment they landed. The 2 s covers FAT and SMB timestamp granularity.
4. `index_paths(due)`.

**`last_indexed` becomes "every edit before this moment has been indexed or
is in a pending event".** `mark_indexed(repo_id, at=None)` gains an optional
timestamp.

- `_on_changes` captures `started` on entry, before indexing. Under W4 that
  is the moment of the swap, so it passes `at=started`.
- Catch-up passes its own start time.
- An edit whose event was still pending at shutdown has an mtime after the
  last swap, so the next catch-up finds it.

Files never represented in `list_indexed_files` are offered on every
catch-up. Examples are Markdown with mentions off, and images. In the
measurement that was 44 files and 0.05 s, so this is accepted.

**When it runs.** It runs after `_start_single` has started the observer, on a
daemon thread, through `run_exclusive`. That covers agent start, "Resume
watching", and `watch enable` or a new registration picked up by `refresh()`.

- Live events that arrive meanwhile queue behind it and then re-index
  whatever they name. Indexing is idempotent, so the overlap costs time, not
  correctness.
- A repository with watching disabled gets no catch-up, which matches today's
  meaning of `watch disable`.

`WatcherManager` gains `on_catch_up: Callable[[str], None] | None`. The tray
and headless agents implement it next to `_on_changes`, calling
`dispatch.catch_up`, then `mark_indexed`, then publishing events. That matches
how `_on_changes` is wired today, rather than adding a shared module.

### W6: git operations while running

The debounce is trailing (500 ms, reset on every event). A checkout or pull
writes its files in well under that, so it lands in one batch. A slower one
splits into several.

- Each event names a path's final state, and the batch sets keep only the
  last kind per path, so split batches converge.
- The only cost of a split is the existing by-name gap. A referrer indexed in
  an earlier batch than its target stays unlinked until a rescan, as for any
  two separate saves.

So the debounce is enough **for events that are delivered**. It is not enough
when events are dropped. ReadDirectoryChangesW overflows its buffer on a large
checkout and drops every pending event for that watch. inotify can overflow
`max_queued_events`.

Since `_GitStateEventHandler` already fires on `HEAD` and branch-ref changes,
it also schedules a catch-up for the repository, 2 s after its own debounce,
through `run_exclusive`.

- Because live batches stamp `last_indexed` with their start, files a live
  batch already indexed are not re-indexed.
- The catch-up only picks up what the batches missed, and costs about 0.04 s
  when they missed nothing.

### W7: docs read cache and "incremental equals fresh apply"

Nothing changes in `devgraph/indexer/providers/`.

- **Live rename of an id-keyed file.** It delivers `{dest}` changed and
  `{src}` deleted in one batch. `index_paths` merges the entry onto the new
  path, and `remove_paths`' takeover sees the id still owned. The keys spec,
  "Rename keeping the id, in either event order", already covers this, so the
  node and its incoming links survive.
- **Rename while the agent is off.** Catch-up prunes first (takeover moves the
  id to the surviving file) and then indexes, so the result is the same.
- **Directory expansion.** It adds paths to `remove_paths`' language-node
  deletes only. The docs and filesystem passes already treat a gone directory
  as "at or below", and `gone` is unchanged for them.
- **Cache.** The read cache keys by `(root, rel)` and validates by stat
  identity. Renames, catch-up and checkouts all look like ordinary saves and
  deletes to it.
- **Tests.** `assert_matches_fresh_apply` and the docs fuzz keep passing
  unchanged. The new live tests call it as well as a whole-graph comparison.

### Windows

- **Renames** come as a paired `RENAMED_OLD_NAME` and `RENAMED_NEW_NAME`.
  watchdog classifies them by `os.path.isdir(dest)`.
  - If the destination is already gone, a directory rename arrives as a file
    move whose destination is not tracked, so the source becomes a delete.
    That is correct, since nothing is left to index.
  - A pair split across reads gives an empty source. W1 ignores it and keeps
    the create.
- **Deleted directories** arrive as `FileDeletedEvent`. W2's expansion in
  `remove_paths` handles them regardless of the event's kind.
- **The Recycle Bin** is a rename out of the watched tree on the same volume,
  so it arrives as a removal of the top path only. W2 covers it.
- **A buffer overflow** drops events silently. W6's post-git catch-up and W5's
  start-up catch-up repair it. Overflows not caused by git (for example a huge
  copy) are repaired at the next start, or by `devgraph rescan`.
- **Junctions.** Scheduling follows `_walk`: a junction is watched only if its
  target is inside the repository and not ignored. Its files are keyed by the
  target, so events under it index the target's key, and deleting the
  junction removes nothing.
- **Change stamp.** It uses `st_birthtime_ns` (Python 3.12+), not the
  deprecated Windows `st_ctime`.
- **CI.** The Windows CI job has no Neo4j. Every fake-event unit test runs
  there, and the live tests skip.

macOS (FSEvents) is not a supported agent platform. Nothing here depends on
it.

### W8: UX

Log lines, at INFO unless noted. Repository ids are shown as stored.

- `Checking <repo> for changes made while DevGraph wasn't watching…`
- `<repo> is up to date (checked 1,234 files in 0.2 s)`
- `Caught up on <repo>: 12 files updated, 3 removed (4.1 s)`
- `Folder renamed in <repo>: pkg → lib` and `Folder removed from <repo>: pkg/sub`.
  These are logged by the watcher, once per directory event, never per child.
- `<repo>: found 5 files the live watcher missed after a git operation; updated them`.
  This is logged only when it finds something; otherwise DEBUG.
- WARNING `Couldn't catch up on <repo>; run "devgraph rescan <repo>" to fix it`,
  with `exc_info`.

The existing `changes detected for …` line stays.

Status:

- **Tray.** The tooltip reads `DevGraph (catching up)` while any catch-up runs.
  Paused and warning states take precedence.
- **Events.** The agents publish
  `{"type": "catch_up", "repo_id", "state": "running" | "done" | "failed", "changed", "deleted"}`.
  `done` with a non-zero count is followed by the existing `reindexed` event,
  so the graph refreshes through the path that is already wired.
- **Dashboard.** The Entities card's pill (`entityLivePill`) shows
  `Catching up…` while a `running` event for the selected repository (or any,
  under `__all__`) has no matching `done` or `failed`. A dashboard opened
  mid-catch-up does not know about it. That is acceptable, since a catch-up of
  normal size is short.

## Not in this slice

- Serializing `SchemaRescanScheduler`'s `full_scan` with live batches.
- The by-name cross-batch referrer gaps listed in PROJECT_STATUS.
- Overflow detection (watchdog surfaces none on Windows).
- macOS.

## Docs to update

- **README.** Remove the "Known gaps" paragraph under **Live updates** and
  the folder limit from "Known limits of the filesystem provider". Add a short "Keeping up with changes" note: renames and folder
  moves, catch-up on start, after git operations, and what the tray and
  dashboard show.
- **PROJECT_STATUS.** Replace the watcher entry's "Known gaps" sentence, extend
  the `devgraph/watcher/` and `devgraph/agent/` entries, and add the slice to the shipped list with what stays open.
