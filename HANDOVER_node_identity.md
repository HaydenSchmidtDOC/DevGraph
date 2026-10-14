# Node-identity collision — resolved

## What shipped

`Function`/`Class`/`Service`/`Container`/`Volume` nodes now MERGE on
`(repo_id, name, file)` instead of `(repo_id, name)` alone, whenever `file`
is set (`_is_file_scoped` in
[devgraph/graph/engine.py](devgraph/graph/engine.py), driving both the
Cypher MERGE key in `_upsert_nodes_tx`/`upsert_node` and the dashboard's
`identity_key` cache key, so the two can't drift apart). Two files each
defining a same-named function/class — or two compose files defining the
same service — now produce two nodes instead of colliding into one.

`name` stays the bare identifier everywhere (no synthesized id string), so
`find_callers`/`get_source`/`search_component` needed zero changes. `CALLS`,
`EXTENDS`, and `MENTIONS`-target edges stay matched by `(repo_id, name)`
only, unchanged — deliberate over-linking, not a bug (see docstring at
`devgraph/indexer/python/extractor.py:72-89`).

**Design chosen: file-only**, not file+scope. Two classes in the *same*
file with same-named methods still collide — deliberately deferred, not
reported as a real-world problem.

## A related but separate bug, also fixed

Fixing collisions on *upsert* didn't fix a matching bug on *delete*:
`delete_nodes_by_source_file` did a blind `DETACH DELETE` on any node whose
`source` property matched the edited file. For the node types that stay on
the bare `(repo_id, name)` key — `Container`/`Service`/`Network`/`Volume`
and the API/Database-ish nodes, all provenance-tracked via `source` rather
than `file` — several files can legitimately co-produce the same node (the
same docker-compose-override scenario above), so deleting *either* file
destroyed the node outright.

Fixed by having those nodes accumulate every producing file into a
`sources` list on upsert; deleting a file now unclaims it from `sources`,
and the node is only removed once no file claims it. See
`_UNCLAIM_SOURCE_CYPHER` in `devgraph/graph/engine.py` and
`tests/graph/test_engine_delete.py`.

## Pinned sources, `origins` (graph-accuracy slice)

- Edge sources are pinned to their file: an extractor emits
  `(label, name, file)` for the source, and only targets stay bare-name
  (`CALLS`, `EXTENDS`, `MENTIONS` targets). Same-named functions or classes
  in two files keep their own edges.
- One exception: a Rust `impl` or a Go method defined in another file than
  its type keeps a bare-name source, because the type isn't a node of the
  file being parsed. Its edges come out of every same-named type. The
  file's Module records those names in `name_ref_sources`, so a re-index
  still retracts the edges it no longer writes.
- A route's handler stub is its own file-less `Function`. It never claims
  a file-scoped `Function` of the same name, so the `Endpoint`
  `IMPLEMENTS` both the stub and every real function of that name.
- Every extracted edge carries a sorted `origins` list of the files that
  wrote it. A file's re-index removes itself from `origins` of the edges it
  no longer writes and deletes an edge when the list is empty. An edge with
  no `origins` (written by an older index) counts as written by the file
  being re-indexed. `full_scan` stamps `Repository.index_format`; an older
  index is rescanned automatically.
- C#/TS partial classes: each file's `partial class Foo : Base` is its own
  node, so `EXTENDS` hangs off the declaring file's node only.
- Go/Rust methods defined in another file than their type, and C++
  out-of-class methods, get no `CONTAINS` (the type isn't in the method's
  file), fresh or incremental.

## Still open, not in scope of either fix

1. Same-file sibling collisions (file+scope design) — see "Design chosen"
   above.
2. **Cheap CALLS precision upgrade**: cross-reference the IMPORTS data
   extractors already collect to resolve direct `module.function()` calls
   to a specific file, no new dependency. Leaves instance dispatch
   (`self.x.foo()`) name-based, same as today.
3. **Full semantic resolution via language servers** (pyright/gopls/
   rust-analyzer/clangd/jdtls/OmniSharp/tsserver) — evaluated and rejected
   as a default: requires each project to have a working build environment,
   LSP project-indexing time can dominate scan time, and would be 7x the
   operational surface for a tool whose value is "zero-toolchain
   tree-sitter parsing." Could be a future explicit opt-in power mode,
   never the default.
