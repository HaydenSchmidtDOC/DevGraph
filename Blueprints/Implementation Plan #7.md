# DevGraph — Implementation Plan #7: Automatic git-history sync + recency staging

Build-ready plan for two things, shipped together because the second isn't
trustworthy without the first: (1) making git-history indexing run
automatically and correctly even when history is rewritten (branch
switches, rebases, resets, abandoned branches), and (2) denormalizing
git-derived recency (`created_at`/`last_modified_at`/optionally
`last_modified_by`) onto `Module`/`Class`/`Function` nodes, plus a recency
filter on two existing MCP tools and one new tool.

## Context

Git history is already indexed (`devgraph/indexer/git_history/extractor.py`):
`Commit` nodes with `authored_date`, linked `MODIFIES -> Module` for every
file a commit touched. Two things are true about it today that surprised us
during design and change the shape of this plan:

1. **It is not automatic at all.** `index_repo_history` is only ever called
   from CLI commands (`devgraph add --full`, `devgraph rescan --full`
   (history), `devgraph index-history <repo_id>`). The watcher/tray never
   calls it — unlike file content, which reindexes live as you edit, git
   history only advances when a human remembers to run a command.
2. **It assumes append-only, linear history.** `index_repo_history` always
   walks `since_sha=repo.last_indexed_commit` forward. If a repo is indexed
   while checked out on a branch that later gets rebased, reset, amended, or
   abandoned in favor of a different branch, the graph's `Commit` nodes for
   now-unreachable commits become permanently orphaned garbage — there is no
   reset/reconciliation path anywhere in the codebase today.

The original ask was "stage recency so an LLM doesn't have to traverse
git history manually." Denormalizing a value from data that can go silently
stale is worse than not having it — a stale `last_modified_at` looks exactly
like a correct, current answer, whereas a stale orphaned `Commit` node is at
least a visible anomaly if inspected. So this plan fixes both problems
together: git-history sync becomes automatic and self-correcting, and
recency staging is built on top of that corrected foundation from day one.

Decisions already made (do not re-litigate without a reason):

- **A new, narrow watch on `.git/HEAD` and `.git/refs/heads/`** triggers
  git-history sync automatically — no manual command required for the
  common case. `.git` stays excluded from the existing recursive
  file-content watch (unchanged, and for the same reason documented in
  `devgraph/watcher/manager.py`: recursively watching a directory as busy
  as `.git` risks overflowing the OS notification buffer on Windows).
  Watching only `.git` non-recursively (catches `HEAD` and `packed-refs`)
  plus `.git/refs/heads/` recursively (small, low-churn directory of ref
  files) avoids that risk while still catching every case that matters:
  commits, checkouts, merges, rebases, resets, amends, new branches.
- **Two distinct engine write modes, not one flag-driven method**:
  `stage_recency` (forward-only ratchet: `created_at` only moves earlier,
  `last_modified_at` only moves later — safe for the common
  "new commit appended" case where history is genuinely append-only) and
  `set_recency` (plain overwrite — used only during reconciliation, when
  we've just computed the authoritative truth from a fresh full walk and
  must be able to move a value backward, not just forward).
- **Fast path vs. reconcile path, decided per sync**: if the previously
  indexed commit is still an ancestor of the new HEAD (`git merge-base
  --is-ancestor`), do the cheap incremental walk (unchanged from today,
  using `stage_recency`). If it isn't (or can't be verified — e.g. the SHA
  no longer exists), do a full reconciliation: diff the fully-reachable
  commit set against what's graphed, delete orphans, re-upsert everything
  reachable (idempotent either way), and recompute (via `set_recency`, not
  ratchet) every affected `Module`/`Function`/`Class`'s recency from
  scratch. Reconciliation does not cap commit count the way a first-time
  full walk safely can (see `git_history/extractor.py`'s existing
  `max_count`) — correctness matters more than speed for what should be a
  rare event, and capping it risks leaving real orphans undetected.
- **One orchestration entry point** (`sync_git_history`, new) used by CLI,
  tray, and headless-agent call sites alike, replacing direct calls to the
  lower-level `index_repo_history`/`extract_new_commits` — one code path,
  not two that can drift apart. The existing extractor internals aren't
  thrown away; `sync_git_history` sits above them the same way
  `dispatch.py`'s `index_paths`/`full_scan` already sit above the
  per-file extractors.
- **`Module`-level `created_at`/`last_modified_at`**: aggregated from
  `Commit.authored_date` among commits `MODIFIES`-linked to that Module —
  already-collected data, no new git calls in the fast path.
- **`Function`/`Class`-level gets `last_modified_at` only, via `git blame`
  on the file's current state** — one `blame` call per file, mapped
  against each contained `Function`/`Class`'s already-stored
  `start_line`/`end_line`. This works because both the blame output and
  the stored line ranges describe the *same* current file state — no
  line-drift problem, and it naturally self-corrects on reconciliation
  since blame is always relative to whatever HEAD currently is.
- **`created_at` at `Function`/`Class` granularity is out of scope for this
  pass.** Getting it right needs `git log -L <start>,<end>:<file>` (walks a
  line range's own history) — a separate git subprocess per function with
  fragile porcelain-text parsing, meaningfully more failure-prone than
  blame. Leave the property absent rather than approximate it wrong.
- **`last_modified_by` (author name) is opt-in, off by default**, via a
  new global setting — same pattern as `enable_run_cypher`/`allow_cross_repo`.
- **No new registry flag for any of this.** Recency staging and automatic
  sync both ride on git history's existing opt-in: if a repo was ever
  indexed with `--full`/`index-history`, it now also gets the automatic
  watcher and the staged properties. There is no "history but not synced"
  or "history but no recency" state to support.
- **Query surface stays narrow**: `modified_within_commits` added to
  `find_callers` and `search_component` only, plus one new
  `list_recent_changes` tool for the broad "what changed recently" case —
  not sprayed across all 19 tools, and not a precedent for a bespoke tool
  per future filter idea. Discussed and confirmed with the user: staging
  the properties is unambiguously worth it regardless of query surface;
  the surface itself is deliberately kept small.

---

## Architecture

```
WatcherManager (per registered, watch-enabled repo)
 ├─ (existing) file-content watch -> on_changes(repo_id, changed, deleted)
 └─ (new) git-state watch: .git (non-recursive) + .git/refs/heads (recursive)
          -> debounced -> on_git_state_changed(repo_id)
                            └─ TrayApp/HeadlessAgent's new handler
                                 -> devgraph.indexer.git_history.sync_git_history(engine, registry, repo_id)

sync_git_history(engine, registry, repo_id):
    head_sha = current HEAD of repo at registry.get(repo_id).path
    last = registry.get(repo_id).last_indexed_commit
    if last is None:
        -> full initial walk (existing extract_new_commits(since_sha=None), capped by max_count)
        -> stage_recency (ratchet) for every touched Module
        -> blame-based stage_recency (ratchet) for every touched .py file's Function/Class
    elif last == head_sha:
        -> no-op
    elif is_ancestor(last, head_sha):
        -> FAST PATH: extract_new_commits(since_sha=last)  [unchanged from today]
        -> stage_recency (ratchet) for every touched Module
        -> blame-based stage_recency (ratchet) for every touched .py file's Function/Class
    else:
        -> RECONCILE PATH: full reachable-set walk (no cap), diff against
           graphed Commit SHAs, delete_commits() for orphans, re-upsert
           everything reachable (idempotent)
        -> set_recency (overwrite) recomputed from scratch for every
           currently-tracked Module
        -> blame-based set_recency (overwrite) for every currently-indexed
           .py file's Function/Class
    registry.set_last_indexed_commit(repo_id, head_sha)
```

New/changed pieces:

- `devgraph/graph/engine.py`: `stage_recency` (ratchet), `set_recency`
  (overwrite), `delete_commits(repo_id, shas)`.
- `devgraph/indexer/git_history/blame.py` (new): blame-to-line-range
  mapping for `Function`/`Class` recency.
- `devgraph/indexer/git_history/extractor.py`: new `sync_git_history`
  orchestration function; existing `GitHistoryExtractor`/
  `extract_new_commits` stay as the low-level primitive it already is.
- `devgraph/watcher/manager.py`: git-state watch + new
  `on_git_state_changed` callback on `WatcherManager`.
- `devgraph/agent/tray.py`, `devgraph/agent/headless.py`: wire the new
  callback to `sync_git_history`; existing `_on_changes` unaffected.
- `devgraph/cli/main.py`: `add --full`, `rescan --full`'s history step, and
  `index-history` all call `sync_git_history` instead of `index_repo_history`
  directly.
- `devgraph/config/settings.py`: `git_recency_track_author: bool = False`.
- `devgraph/mcp/tools.py`, `devgraph/mcp/server.py`: `modified_within_commits`
  on `find_callers`/`search_component`; new `list_recent_changes` tool.

---

## Item 1 — Engine: `stage_recency`, `set_recency`, `delete_commits`

**Files:** modify `devgraph/graph/engine.py`.

- `stage_recency(label, repo_id, name, created_at=None, last_modified_at=None, last_modified_by=None)`:
  ```python
  MERGE (n:{label} {repo_id: $repo_id, name: $name})
  SET n.created_at = CASE WHEN $created_at IS NULL THEN n.created_at
        WHEN n.created_at IS NULL OR $created_at < n.created_at THEN $created_at
        ELSE n.created_at END,
      n.last_modified_at = CASE WHEN $last_modified_at IS NULL THEN n.last_modified_at
        WHEN n.last_modified_at IS NULL OR $last_modified_at > n.last_modified_at THEN $last_modified_at
        ELSE n.last_modified_at END,
      n.last_modified_by = CASE
        WHEN $last_modified_by IS NULL THEN n.last_modified_by
        WHEN n.last_modified_at IS NULL OR $last_modified_at > n.last_modified_at THEN $last_modified_by
        ELSE n.last_modified_by END
  ```
  Note the `last_modified_by` CASE deliberately mirrors the `last_modified_at`
  comparison, not a separate condition — this is what stops an
  out-of-chronological-order call (possible across incremental batches)
  from clobbering a newer commit's author with an older one's. Always
  passing `last_modified_by=None` when `git_recency_track_author` is off
  means that branch is a no-op (existing value preserved).
- `set_recency(label, repo_id, name, created_at=None, last_modified_at=None, last_modified_by=None)`:
  same MERGE target, but a plain `SET n.created_at = $created_at,
  n.last_modified_at = $last_modified_at` (+ `last_modified_by` when not
  None) — no CASE guards. Used only by the reconcile path, where the
  caller has just computed the authoritative value from a fresh full walk
  and must be able to move it backward.
- `delete_commits(repo_id, shas: list[str])`: `MATCH (c:Commit {repo_id: $repo_id}) WHERE c.sha IN $shas DETACH DELETE c`.
- Add `git_recency_track_author: bool = False` to `devgraph/config/settings.py`.

## Item 2 — `sync_git_history` orchestration + Module recency

**Files:** modify `devgraph/indexer/git_history/extractor.py`.

- Implement `sync_git_history(engine, registry, repo_id, max_count=None) -> dict`
  per the Architecture section's pseudocode. Use GitPython's
  `repo.is_ancestor(candidate_rev, rev)` for the ancestor check; wrap the
  whole check in a try/except (a pruned/garbage-collected SHA, or a
  first-ever run with `last is None`, are both legitimate — reread the
  Architecture pseudocode for the exact branching) and treat "can't verify"
  the same as "not an ancestor" (reconcile), never the same as "is an
  ancestor" (fast path) — an unsafe assumption here is exactly the bug this
  plan exists to close.
- Fast path and initial-walk path: after `extract_new_commits` returns,
  group its `MODIFIES` relationships by target `Module` name, compute
  min/max `authored_date` per module among *this batch's* commits, call
  `engine.stage_recency("Module", repo_id, module_name, created_at=..., last_modified_at=..., last_modified_by=...)`
  (author only when `get_settings().git_recency_track_author`).
- Reconcile path: walk `repo.iter_commits()` fully (no `max_count`) to get
  the true reachable SHA set; diff against `MATCH (c:Commit {repo_id: $repo_id}) RETURN c.sha`;
  call `delete_commits` for the orphaned set; re-run the same
  commit/`MODIFIES` upsert `extract_new_commits` already does for every
  reachable commit (idempotent MERGE — re-upserting ones already present is
  harmless); then, for every `Module` currently linked to any `Commit` via
  `MODIFIES` in this repo, recompute true min/max `authored_date` from
  scratch (a fresh aggregation query, not the batch-local one the fast path
  uses) and call `set_recency` (overwrite).
- Both paths finish by calling `registry.set_last_indexed_commit(repo_id, head_sha)`.
- Keep `GitHistoryExtractor`/`extract_new_commits` as-is — `sync_git_history`
  is a new orchestration layer above it, not a rewrite of it.

## Item 3 — Blame-based Function/Class recency

**Files:** new `devgraph/indexer/git_history/blame.py`; modify
`devgraph/indexer/git_history/extractor.py`.

- `compute_function_recency(repo, file_path: str) -> list[FunctionRecency]`
  where `FunctionRecency` is `{start_line, end_line, last_modified_at, last_modified_by}`
  per blamed hunk. `Repo.blame(rev, file)` (GitPython) returns
  `[(Commit, [line, line, ...]), ...]` covering the whole file in order —
  walk it once with a running line counter to know each hunk's line range.
- In `sync_git_history`, for every `.py` file touched (fast path: touched
  by this batch's commits; reconcile path: every currently-indexed `.py`
  file, since reconciliation recomputes everything) that has `Function`/
  `Class` nodes already indexed: run blame once, fetch that file's current
  entities (name + `start_line`/`end_line`) via one `run_cypher` call
  scoped to `repo_id` and the file's `Module`, and call `stage_recency`
  (fast path) or `set_recency` (reconcile path) per entity whose range
  overlaps a blamed hunk, using the latest such hunk's date/author. Calling
  once per overlapping hunk (letting `stage_recency`'s own MAX-guard
  converge) instead of pre-aggregating is also correct, just less
  efficient — implementer's call.
- Only for `.py` files with already-indexed `Function`/`Class` nodes — skip
  everything else (no blame-based recency for non-Python files this pass).
- If `git blame` fails for a file (deleted since, not tracked at this rev,
  etc.), log a warning and skip that file — never fail the whole sync over
  one file's blame error.

## Item 4 — Watcher: git-state detection

**Files:** modify `devgraph/watcher/manager.py`.

- New `_GitStateEventHandler(FileSystemEventHandler)`, mirroring
  `_RepoEventHandler`'s debounce pattern but simpler — no changed/deleted
  path tracking needed, just "something fired, debounce, then call
  `on_git_state_changed(repo_id)` once."
- `WatcherManager.__init__` gains `on_git_state_changed: Callable[[str], None]`.
- `_start_single`: after the existing file-content `observer.schedule`
  calls, add `observer.schedule(git_handler, str(repo.path / ".git"), recursive=False)`
  and, if `repo.path / ".git" / "refs" / "heads"` exists,
  `observer.schedule(git_handler, str(repo.path / ".git" / "refs" / "heads"), recursive=True)`.
  Guard for `.git` being a *file* rather than a directory (a linked
  worktree's `.git` is a pointer file, not the real git dir) — skip
  git-state watching gracefully in that case rather than raising; DevGraph's
  own registered repos are ordinary checkouts, but this must not crash if
  someone registers a worktree.
- Do not schedule a recursive watch on the whole `.git` directory — same
  Windows notification-buffer-overflow rationale already documented for
  `.venv`/`build`/etc. in this file.

## Item 5 — Tray/headless/CLI wiring

**Files:** modify `devgraph/agent/tray.py`, `devgraph/agent/headless.py`,
`devgraph/cli/main.py`.

- `TrayApp`/`HeadlessAgent`: pass `on_git_state_changed=self._on_git_state_changed`
  to `WatcherManager(...)`; add `_on_git_state_changed(self, repo_id: str) -> None`
  calling `sync_git_history(self._engine, self._registry, repo_id)`, same
  error-tolerance spirit as `_on_changes` (log and continue, never crash the
  watcher thread over one repo's sync failure).
- `devgraph/cli/main.py`: replace all three existing call sites that call
  `index_repo_history` — `add --full` (~line 71), `rescan --full` (~line
  189), and the standalone `index-history` command (~line 392) — with
  `sync_git_history`. No new call sites; `rescan` without `--full` still
  does not touch git history at all, unchanged.

## Item 6 — Query surface

**Files:** modify `devgraph/mcp/tools.py`, `devgraph/mcp/server.py`.

- Add `modified_within_commits: int | None = None` to `find_callers` and
  `search_component`. When given: resolve a cutoff timestamp via
  `MATCH (c:Commit {repo_id: $repo_id}) RETURN c.authored_date AS d ORDER BY d DESC SKIP $skip LIMIT 1`
  (`skip = within_commits - 1`; fewer than `within_commits` commits
  repo-wide means no cutoff — everything with a `last_modified_at` passes),
  then add `AND target.last_modified_at >= $cutoff` to the query. An entity
  with no `last_modified_at` (anything not covered by Items 2-3) never
  matches when this filter is active — document plainly in both
  docstrings, never silently include everything.
- New tool `list_recent_changes(engine, repo_id, within_commits, entity_type=None, cross_repo=False, max_results=15) -> dict[str, Any]`:
  same cutoff resolution, `entity_type` optionally restricts to one label
  (validate against `schema.NODE_LABELS`, same pattern `find_mentions`
  established), returns entities with `last_modified_at >= cutoff` ordered
  `DESC`, envelope-shaped. Register in `server.py`'s `_TOOL_CATALOG`
  (`phase: 3`) and as a `@server.tool(annotations=_READ_ONLY)` wrapper,
  following `find_mentions`'s registration exactly.

## Item 7 — Tests

**Files:** new `tests/indexer/test_git_history_sync.py` (or extend
`test_git_history_extractor.py`/`test_git_history_integration.py` if either
fits better — check both first), new `tests/indexer/test_blame.py`, new
`tests/watcher/test_git_state_watch.py` (check whether `tests/watcher/`
exists first; create if not, following whatever fixture pattern
`tests/agent/test_tray_on_changes.py` uses for mocking), plus MCP tool test
extensions for the two changed tools and the new one.

- `stage_recency`/`set_recency` unit tests: ratchet only ever advances
  correctly in both directions independently; out-of-order calls converge;
  `last_modified_by` only updates alongside a winning `last_modified_at`;
  `set_recency` can move a value backward (the behavior `stage_recency`
  deliberately can't).
- `sync_git_history` tests using a real temp git repo (mirror
  `test_git_history_integration.py`'s fixture): (a) fast path — new commits
  appended, recency advances correctly; (b) reconcile path — commit on a
  branch, index it, then reset/checkout to drop that commit, sync again,
  confirm the orphaned `Commit` node is gone AND recency for affected
  files/entities is corrected (not left stuck at the abandoned value); (c)
  blame-based `Function`/`Class` recency reflecting the actual commit that
  touched that function's lines, not just the file's latest commit.
- Watcher test: `.git/HEAD`/`.git/refs/heads/*` changes trigger
  `on_git_state_changed`, debounced the same way file changes already are;
  a repo whose `.git` is a file (not a directory) doesn't crash watcher
  startup.
- `modified_within_commits`/`list_recent_changes` tests: window inclusion/
  exclusion, entities with no `last_modified_at` excluded, `entity_type`
  filter, envelope shape, cross_repo behavior.

## Item 8 — Docs

**Files:** `README.md`, `PROJECT_STATUS.md`, `DEVGRAPH-CLIENT.md`.

- Document: git history is now automatically kept in sync (once opted into
  via `--full`/`index-history` at least once) including correct behavior
  across branch switches/rebases/resets; the staged recency properties and
  which labels get what; why function-level `created_at` is deliberately
  absent; the `git_recency_track_author` setting; the new tool and the two
  tools' new parameter. Bump the MCP tool count (19 → 20).

---

## Explicitly out of scope for this pass

- `created_at` at `Function`/`Class` granularity (needs `git log -L`).
- Recency for `Service`/`Endpoint`/`Database`/`VectorStore`/`Queue` nodes —
  no git-file-line backing to derive it from in the same way.
- Adding `modified_within_commits` to any tool beyond `find_callers` and
  `search_component` — deliberately narrow, per the brainstorm discussion.
- Wall-clock-based recency windows (e.g. "modified in the last 7 days") —
  this plan's filter is commit-count-based throughout.
- Watching anything under `.git` beyond `HEAD`/`refs/heads` (e.g.
  `refs/tags`, `refs/remotes`) — a tag or remote-tracking-ref change doesn't
  move what "the repo's current state" means for indexing purposes the way
  `HEAD`/local-branch moves do.
- Making `rescan` (without `--full`) trigger history sync if it doesn't
  already — only existing `index_repo_history` call sites are being
  redirected to `sync_git_history`, not new ones added.
