# Graph Accuracy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** after any sequence of edits, deletes, restores, renames and symbol moves, in single or split batches, the graph equals the one a fresh `full_scan` of the same files produces. A `full_scan` equals the expected graph, and an existing graph upgrades itself. Bugs fixed:

1. same-named sources steal edges, together with 1b: stale outgoing edges, docs-note edges included;
2. a removed compose service stays;
3. a removed mention keeps its `MENTIONS`;
4. a deleted-and-restored (or later-added) target loses its by-name edges.

**Spec:** `docs/superpowers/specs/2026-10-08-graph-accuracy-design.md`. Every task implements the decisions it names (G1–G7).

**Working directory:** this worktree, branch `epic1/graph-accuracy` (from `epic1/test-isolation`). Run `uv sync --extra dev` once, then `uv run` from inside the worktree, because the editable install otherwise imports another checkout (check `devgraph.__file__`). Live tests need the local Neo4j (`bolt://127.0.0.1:7687`, `neo4j`/`devgraph-local-dev`) and skip without it. Never touch `~/.devgraph`; the suite already isolates it.

## Global Constraints

- **Order.**
  - Task 1 lands first, with `origins`, the unclaim primitive and sets (a) and (d): every later task writes or relies on them.
  - Task 2 needs Task 1's single-scan replace.
  - Task 4 needs pinned sources and `origins`, and adds G2 set (b), because that set reads `name_ref_sources`. Task 2 adds set (c), alongside moving the Python API writes into the replace.
  - Task 5 needs Tasks 1–4, because the format bump is what makes their rescan happen.
  - Task 6 runs last, over everything.
- **Ownership is a sorted `origins` list on the edge (G2).**
  - Every built-in extracted-edge writer passes `origin`. The upsert does a sorted insert into `r.origins`, and it is idempotent.
  - Retraction uses one primitive, *unclaim edge*:
    - remove `$f`, with `coalesce(r.origins, [$f])` for legacy edges, except in set (b);
    - delete the edge only when the list is empty.
  - It applies only to the four anchored, `repo_id`-scoped sets of G2:
    - (a) owned sources;
    - (b) old `name_ref_sources`;
    - (c) `(:Service)-[:USES]` and `(:Endpoint)-[:CALLS|IMPLEMENTS]` for a Python file;
    - (d) the docs note's own nodes.
  - A full mentions index deletes `MENTIONS` out of its `Document`.
  - No unanchored or cross-repo edge scan, and no per-label exceptions.
  - `upsert_relationships` without `origin` leaves `origins` unchanged.
  - File deletion (`delete_nodes_by_source_file`) applies (b) and (c) before it deletes.
- **Re-claim, never recreate (G3).** No replace may `DETACH DELETE` a shared node that the new extraction still claims.
- **One transaction per file.** Each retraction runs in the same write transaction as the re-upsert it precedes. The G2 code retraction and the stale-node delete are one scan over the file's owned nodes. No new autocommit writes.
- **`name_refs` (G5).**
  - Sorted, de-duplicated `\x1f`-joined strings. The field order is fixed in the spec, and an empty `caller_class` is an empty field.
  - All three lists are written on every Module, empty when there is nothing to record.
  - They are written only by the pass-1 replace, never by the pass-2 re-upsert.
  - The G5 read filters on `name_ref_targets`/`name_ref_sources` before it splits an entry.
  - `full_scan` never runs G5.
  - No file is read by G5.
- **Regression shape.**
  - Every bug's live test ends in "incremental equals a fresh `full_scan`" or "`full_scan` equals the expected graph".
  - The comparison uses `graph_snapshot`, extended in Task 1 so that each edge carries both ends' files and its `origin`, and `fresh_snapshot(..., mentions_enabled=, docs_path=)`.
  - Where the bug is "even after `full_scan`", the test also runs `full_scan` over a polluted graph.
- **Fixtures.**
  - Unique repo ids: `f"zz-accuracy-{uuid.uuid4().hex[:8]}"`.
  - `engine.delete_repository` before the test and in a finaliser, for the repo and its `_fresh` twin.
  - `tmp_path` repositories only.
  - Fictional names only: `app.py`, `worker.py`, `helper`, `api`, `db`, `notes.md`, `Foo`.
- **No new config knob, and no new dependency.** The only new environment variables are the fuzz's opt-in length knobs (Task 6), which are test-only. `index_paths` gains one keyword, `relink_outside: bool = True`, passed only by `full_scan`.
- **TDD.** Each task starts with failing tests, then the implementation, then `uv run pytest -q` (the full suite, live tests included).
- **Commits.** Plain imperative messages, with no `Co-Authored-By` trailer and no AI attribution. Never stage `uv.lock`, real names or personal paths.

## Review Focus

Each item names the test that proves it.

1. **No wrong source.** Across all eight languages, by-name edges from a file's own `Function`/`Class` are pinned. Module sources and sources that aren't this file's nodes are not pinned. Tests:
   - `test_edges_owned_by_the_parsed_file[<lang>]`;
   - `test_same_named_functions_keep_their_own_calls`.
2. **Retraction is scoped by `origins`.**
   - A docs-note `DOCUMENTED_BY` survives re-indexing the code file. Test: `test_docs_note_edge_survives_code_reindex`.
   - A Rust `impl Display for Foo` written in `conv.rs` survives re-indexing `foo.rs`. Test: `test_foreign_impl_edge_survives_owner_reindex`.
   - `Service -USES-> Datastore` survives a compose re-index. Test: `test_compose_reindex_keeps_owning_service_uses`.
   - Incoming, provider, `MODIFIES` and `IMPLEMENTS` edges survive. Test: `test_reindex_keeps_incoming_and_foreign_edges`.
   - A foreign-source edge its writer stopped writing retracts, incrementally and on `full_scan`. Test: `test_removed_foreign_impl_is_retracted`.
   - A multi-writer edge stays until its last writer stops, and that is deterministic. Test: `test_shared_uses_edge_has_two_writers`.
   - `replace_doc_note` touches only its own repository. Test: `test_doc_note_replace_is_repo_scoped`.
3. **Shared nodes are re-claimed in place.**
   - `MENTIONS` to a single-claimant `Container` survives a Dockerfile re-index. Test: `test_container_mentions_survive_dockerfile_reindex`.
   - The same holds for a `Datastore` across a Python re-index. Test: `test_datastore_mentions_survive_python_reindex`.
   - A removed service unclaims its image. Test: `test_removed_service_unclaims_its_image`.
4. **Polluted graphs heal (G7).** Seeded wrong, stale and legacy (no-`origins`) edges equal a fresh scan after one `full_scan`. Tests: the `test_full_scan_*` cases in Tasks 1–3.
5. **Cross-batch relink.** Each of these equals a fresh scan, with no file read:
   - delete-then-restore;
   - a module added later;
   - a supertype added in another directory;
   - a second same-named function;
   - a restored `foo.rs` regaining `conv.rs`'s `impl` edge;
   - a symbol moved between files in one batch.

   Tests: Task 4.
6. **Cost.** `test_name_refs_are_compact` checks the size bound, and `test_name_ref_relink_benchmark` checks about 5k Modules within budget. `test_full_scan_skips_relink` and `test_pass_two_omits_name_refs` check the two skips.
7. **Upgrade.** An outdated repository is rescanned by the scheduler with no quiet period, and by the start catch-up, and `devgraph status` says "rescan pending". Tests: Task 5.
8. **Everything together.** `test_graph_accuracy_fuzz[seed]` (Task 6) and every existing `tests/watcher/test_watcher_live.py` scenario pass with the stricter snapshot.

### Task 1: Origin stamps, pinned sources and owned-edge retraction (bugs 1, 1b, docs notes; G1, G2)

**Files:**
- `devgraph/indexer/common.py`:
  - `GraphRelationship.origin: str | None = None`, also in `to_dict`.
  - `own_edges(result, file_path) -> ExtractionResult`, per G1: pin the sources that are this file's `file` nodes, and stamp `origin` on every relationship. Leave an already-set `from_file` as it is.
  - Update the `GraphRelationship` docstring.
- Each of `devgraph/indexer/{python,jsts,java,csharp,cpp,go,rust,kotlin}/extractor.py`: return `own_edges(result, file_path)` from the top-level `extract_*_file`, early returns included.
- `devgraph/graph/engine.py`:
  - `_group_rels_by_triple` carries `origin`. `_upsert_relationships_tx` sorted-inserts it, in Cypher with no APOC:
    ```
    SET r.origins = CASE
      WHEN row.origin IS NULL OR row.origin IN coalesce(r.origins, []) THEN r.origins
      ELSE [x IN coalesce(r.origins, []) WHERE x < row.origin] + [row.origin]
         + [x IN coalesce(r.origins, []) WHERE x > row.origin] END
    ```
  - `_UNCLAIM_EDGE` is a shared Cypher fragment, applied to a bound `r`:
    ```
    WITH r, [x IN coalesce(r.origins, [$f]) WHERE x <> $f] AS left
    FOREACH (_ IN CASE WHEN size(left) = 0 THEN [1] ELSE [] END | DELETE r)
    FOREACH (_ IN CASE WHEN size(left) > 0 THEN [1] ELSE [] END | SET r.origins = left)
    ```
    Set (b) uses a variant that first requires `$f IN r.origins`, with no legacy rule.
  - `_replace_file_nodes_tx`, set (a): one statement over `(n {repo_id})` owned by the file (`n.file = $f OR n.source_file = $f OR (n:Module AND n.name = $f)`). Inside a `CALL { ... }` subquery it unclaims `(n)-[r]->()`, then `DETACH DELETE`s the stale non-Module nodes (today's `keep` rule). This replaces the separate `_DELETE_STALE_FILE_NODES_CYPHER` run.
  - `replace_doc_note(repo_id, file_name, nodes, rels)`, set (d): one transaction. It anchors on `MATCH (n {repo_id: $repo_id, source_file: $f})`, unclaims `(n)<-[r:DOCUMENTED_BY|SATISFIES]-()` and `(n)-[r:SUPERSEDES|DECIDED_BY]->()`, then upserts the nodes and the rels. There is no unanchored edge scan.
- `devgraph/indexer/docs/extractor.py`: `index_file` builds dicts with `origin = source_key(...)` and calls `engine.replace_doc_note`.
- `devgraph/indexer/dispatch.py`:
  - `_relationship_dict(rel, repo_id, origin)`. `_index_apis` and `_owning_service_relationships` pass the Python file's `rel_path`; the compose and Containerfile paths pass theirs.
  - Update the docstrings.
- `tests/watcher/live_helpers.py`:
  - `graph_snapshot`: edges gain `coalesce(a.file, a.source_file, a.path, '')`, the same for `b`, and `coalesce(x.origins, [])`.
  - `fresh_snapshot(engine, repo_id, root, mentions_enabled=False, docs_path=None)` passes both to `full_scan` (lines ~69-83).
- `tests/indexer/test_own_edges.py` (new, unit).
- `tests/indexer/test_graph_accuracy_live.py` (new, live), holding `incremental_equals_fresh(engine, repo_id, root, mentions_enabled=False, docs_path=None)`. It shows a readable diff (reuse `wait_until_equal`'s formatting) and does not poll.

- [ ] Write failing unit tests:
  - **`test_edges_owned_by_the_parsed_file[<lang>]`**, parametrised over the eight extractors. Each source has `main` → `helper`, a subtype of `Base`, and, where the language has one, a nested function.
    - Every `CALLS`/`EXTENDS`/`CONTAINS` from a `Function`/`Class` that has `file == path` in the result is pinned to `path`.
    - Every relationship has `origin == path`.
    - `to_file` is unchanged.
  - **`test_unowned_sources_stay_bare`.**
    - A Python module-level call keeps `from_file is None`.
    - Rust: `conv.rs` with `impl Display for Foo {}` (no `struct Foo`) has an `EXTENDS` from `Foo` with `from_file is None` and `origin == "conv.rs"`.
    - C++: `void Foo::bar() { baz(); }` alone pins `bar`'s `CALLS`. The file-less stub `Foo` gains no `file`. The existing stub `CONTAINS` keeps the `from_file` the extractor already sets; it is a known gap, and the test asserts the current value so a change is deliberate.
  - **`test_origins_are_a_sorted_set`** (live engine).
    - Upsert one edge with `origin="b.py"`, then `"a.py"`, then `"b.py"` again, then with no origin: `r.origins == ["a.py", "b.py"]`.
    - Unclaim `"a.py"`: the result is `["b.py"]`.
    - Unclaim `"b.py"`: the edge is gone.
    - A legacy edge (no `origins`) unclaimed for any file is gone.
- [ ] Write failing live tests:
  - **`test_same_named_functions_keep_their_own_calls`.**
    - `src/app.py` (`helper`, `main` → `helper`) and `src/worker.py` (`main` returning `2`).
    - `full_scan` equals the expected edge set: the only `CALLS` is `main@src/app.py → helper@src/app.py`.
    - After `index_paths({src/worker.py})`, incremental equals fresh.
  - **`test_same_named_classes_keep_their_own_bases`.** `a.py` `class K(Base)`, `b.py` `class K:`, `base.py` `class Base:`. Only `K@a.py` EXTENDS `Base`.
  - **`test_removed_call_base_and_import_are_retracted`.**
    - `m.py` with `import pkg.util`, `class K(Base)`, and `main` → `helper`. Edit all three away.
    - `index_paths({m.py})`: incremental equals fresh.
    - `full_scan` equals the expected graph.
  - **`test_full_scan_heals_a_polluted_graph`.**
    - After the app/worker `full_scan`, seed the old wrong edge `main@src/worker.py -CALLS-> helper@src/app.py` with no `origins`, using `run_cypher`.
    - Seed a stale no-`origins` `CALLS` out of `helper@src/app.py`.
    - `full_scan` equals a fresh scan.
  - **`test_docs_note_edge_survives_code_reindex`.**
    - `docs_path="docs"`, and `docs/adr-1.md` with `links: [src/app.py]`. `full_scan`.
    - Touch `src/app.py` and `index_paths({src/app.py})`: `Module src/app.py -DOCUMENTED_BY-> ADR-1` remains, and incremental equals fresh (`docs_path`).
  - **`test_removed_note_links_are_retracted`.**
    - Drop `links` from `docs/adr-1.md`, then `index_paths`: incremental equals fresh.
    - Do the same with `supersedes` between two `DesignDecision` notes.
    - `full_scan` over a graph seeded with a legacy no-`origins` `DOCUMENTED_BY` equals the expected graph.
  - **`test_doc_note_replace_is_repo_scoped`.**
    - Two repositories (`<id>` and `<id>-other`), each with `docs/adr-1.md` linking `src/app.py`, both `full_scan`ned.
    - Drop the link in the first repository and `index_paths`.
    - The other repository's `DOCUMENTED_BY` and its `origins` are unchanged, and both repositories equal their fresh scans.
  - **`test_foreign_impl_edge_survives_owner_reindex`.**
    - `foo.rs` `pub struct Foo;`, `display.rs` `pub trait Display {}`, and `conv.rs` `impl Display for Foo {}`. `full_scan`.
    - Touch `foo.rs` and `index_paths({foo.rs})`: `Foo@foo.rs -EXTENDS-> Display` remains, with `origins == ["conv.rs"]`, and incremental equals fresh.
  - **`test_reindex_keeps_incoming_and_foreign_edges`.** With mentions on and a docs-provider schema (reuse `tests/indexer/test_docs_provider_live.py`'s scaffolding), set up:
    - a `notes.md` mentioning `` `helper` ``;
    - a seeded `Commit -MODIFIES-> Module src/app.py`;
    - a provider edge into `Module src/app.py`;
    - a FastAPI route in `src/app.py`.

    After `index_paths({src/app.py})`, all four kinds of edge remain, and incremental equals fresh. Assert `MODIFIES` directly, because the snapshot leaves out `Commit`.
- [ ] Implement G1 and G2 so that both files pass.
- [ ] `uv run pytest tests/watcher -q` with the stricter snapshot. A newly failing scenario is a real accuracy bug: fix it or report it; never loosen the projection.
- [ ] `uv run pytest -q`. Commit "Track edge origins and retract a file's own stale edges".

### Task 2: Re-claim shared nodes in place; compose files and Containerfiles replace (bug 2; G3)

**Files:**
- `devgraph/graph/engine.py`:
  - `_unclaim_source_tx(tx, repo_id, file_name, keep=None)`: `_UNCLAIM_SOURCE_CYPHER` gains `AND NOT any(p IN $keep WHERE labels(n)[0] = p[0] AND n.name = p[1])`.
  - `_replace_file_nodes_tx` passes the claimed `(label, name)` pairs of `nodes` (no `file`, `properties.source == file_name`).
  - `_delete_by_source_file_tx` passes `[]`.
  - Set (c): `_replace_file_nodes_tx(..., service_api=False)`, and the Python branch passes `True`. When it is set, the same transaction unclaims `(:Service {repo_id})-[r:USES]->()` and `(:Endpoint {repo_id})-[r:CALLS|IMPLEMENTS]->()` for `$f`. `_delete_by_source_file_tx` always applies set (c) for a `.py` key. Add a comment: the owning-service edges come back in `index_paths`' final service pass, which needs every compose file in the batch, so a reader can briefly miss them.
- `devgraph/indexer/dispatch.py`:
  - `_upsert_container_result(engine, repo_id, result, rel_path)` builds nodes and rels (with `origin=rel_path`), and calls `engine.replace_file_nodes`.
  - The Python branch of `_index_single_path` extracts the datastore and API nodes and rels first: `_index_datastores` and `_index_apis` become `_datastore_nodes` and `_api_nodes_and_rels`, which return dicts and write nothing. It passes them, with the language nodes and rels, to the one `replace_file_nodes` call.
  - `py_extractions` keeps the same combined lists for pass 2.
- `tests/indexer/test_graph_accuracy_live.py`.

- [ ] Write failing live tests:
  - **`test_removed_service_is_retracted`.**
    - `compose.yaml` with `api` (`python:3.12`) and `db` (`postgres:16`). `full_scan`, drop `db`, `index_paths({compose.yaml})`.
    - Incremental equals fresh.
    - `Service db`, `Container postgres` and `db -RUNS-> postgres` are gone.
  - **`test_full_scan_drops_a_removed_service`.** The same edit, followed by `full_scan` alone, equals the expected graph.
  - **`test_removed_service_unclaims_its_image`.**
    - `compose.yaml` and `compose.override.yaml` both run `postgres:16`. Remove `db` from `compose.yaml`.
    - `Container postgres` survives with `sources == ["compose.override.yaml"]`, and incremental equals fresh.
  - **`test_container_mentions_survive_dockerfile_reindex`.**
    - Mentions on. `Dockerfile` `FROM postgres:16`, and `notes.md` mentioning `` `postgres` ``. `full_scan`.
    - Append a `RUN` line to `Dockerfile` and `index_paths({Dockerfile})`: `MENTIONS -> Container postgres` remains, the node's `elementId` is unchanged, and incremental equals fresh.
  - **`test_datastore_mentions_survive_python_reindex`.** The same shape, with `app.py` using `redis.from_url("redis://cache:6379/0")` and `notes.md` naming the Datastore in a code span.
  - **`test_compose_reindex_keeps_owning_service_uses`.**
    - `compose.yaml` `api` with `build: ./services/api`, and `services/api/app.py` using redis. `full_scan`: `Service api -USES-> <Datastore>` exists.
    - Add an unrelated service and `index_paths({compose.yaml})`: the edge remains, and incremental equals fresh.
  - **`test_shared_uses_edge_has_two_writers`.**
    - `compose.yaml` `api` with `build: ./services/api`, and two files under it, `a.py` and `b.py`, both using the same redis URL. `full_scan`: the one `Service api -USES-> <Datastore>` has `origins == ["services/api/a.py", "services/api/b.py"]`.
    - Remove the redis use from `a.py` and `index_paths`: the edge stays, with `["services/api/b.py"]`, and incremental equals fresh.
    - Delete `b.py` (`remove_paths`): the edge is gone, and incremental equals fresh.
    - Restore both, then delete `a.py` first: the mirror image, also equal to fresh.
  - **`test_removed_route_retracts_endpoint_edges`.** Remove a FastAPI route from `services/api/a.py`: `Endpoint -IMPLEMENTS->` and `Endpoint -CALLS-> Service api` go when no other file writes them, and incremental equals fresh.
  - **`test_containerfile_retracts_a_removed_stage`.** A two-`FROM` `Containerfile` loses one stage: incremental equals fresh.
- [ ] Implement G3 and G2 set (c).
- [ ] `uv run pytest -q`. Commit "Re-claim shared nodes in place and retract removed services".

### Task 3: A full mentions re-index replaces the Document's MENTIONS (bug 3; G4)

**Files:**
- `devgraph/graph/engine.py`: `replace_mentions(repo_id, file_name, nodes, rels)` uses the same signature and retry wrapper as `replace_file_nodes`. It deletes `(:Document {repo_id, name: $f})-[:MENTIONS]->()`, then upserts the nodes and the rels.
- `devgraph/indexer/mentions/extractor.py`:
  - `index_file` builds the dicts once, with `origin = doc_name`.
  - `names is None` → `replace_mentions`. Otherwise, today's additive upserts, also passing `origin`.
  - Update the docstring.
- `tests/indexer/test_graph_accuracy_live.py`.

- [ ] Write failing live tests:
  - **`test_removed_mention_is_retracted`.**
    - `app.py` (`helper`, `other`), and `notes.md` mentioning `` `helper` `` and `` `other` ``. Edit it to mention `` `other` `` only.
    - `index_paths({notes.md}, mentions_enabled=True)`: incremental equals fresh.
  - **`test_full_scan_drops_a_removed_mention`.** The same edit, followed by `full_scan` alone, equals the expected graph.
  - **`test_mention_relink_stays_additive`.** Add `def later()` while `notes.md` already mentions `` `later` ``: `MENTIONS later` is added, and `MENTIONS other` is kept.
  - **`test_mentions_replace_keeps_the_docs_note`.**
    - `docs_path="docs"`, mentions on, and `docs/adr-1.md` (`id: ADR-1`) mentioning `` `helper` ``.
    - Drop the mention and `index_paths`: the note node survives, and incremental equals fresh.
- [ ] Implement G4.
- [ ] `uv run pytest -q`. Commit "Replace a document's mentions on re-index".

### Task 4: Relink by-name edges when a batch adds their endpoint (bug 4; G5, G6)

**Files:**
- `devgraph/indexer/common.py`: `name_ref_properties(rels) -> dict` returns the three lists per the spec's G5 format. The entries are sorted and de-duplicated, with `\x1f` as the separator.
- `devgraph/indexer/dispatch.py`:
  - **Module properties.** `_with_name_refs(nodes, rels, rel_path) -> list[dict]` returns a copy of `nodes` in which the Module (`name == rel_path`) carries `name_ref_properties(rels)`. The eight code branches pass that copy to `replace_file_nodes`, and store the plain `nodes` in `*_extractions` for pass 2.
  - **`added` by file.** `list_file_nodes` returns `(label, name, coalesce(n.file, n.source_file, n.path))`, with `n.name` for a Module. `_batch_nodes` returns triples with the same `file`:
    - a code node: `properties.file`, or `rel_path` for its Module;
    - a docs note: `path.relative_to(root).as_posix()`, because the note's extraction is keyed by `path.name` but `index_doc_file` writes the repo-relative `source_file`;
    - a `Document`: its repo-relative path;
    - a `Service`: its compose file. `batch_services` becomes a set of `("Service", name, rel_path)` triples, filled in `_index_single_path`;
    - a provider node: its `path`, so the docs batch nodes and filesystem files carry it. `added = triples - previous - {t for t in triples if t[:2] in docs_batch.existing}`. `_find_referrers`, `_mention_referrers` and `_relink_docs` get `{t[:2] for t in added}`.
  - **The relink step.** `_relink_name_refs(engine, repo_id, added, skip)`:
    1. It returns at once when `added` is empty.
    2. It calls `engine.find_name_refs(repo_id, names, skip)`.
    3. It parses each entry and keeps those whose `(to_label, to_name)`, or unpinned non-Module `(from_label, from_name)`, is in `added`'s pairs.
    4. It builds rel dicts with `origin = module name`, and makes one `upsert_relationships` call.
  - **Where it runs.** After the eight pass-2 loops and before the service pass, when `relink_outside` is true. `skip = set(by_rel_path) | referrers`.
  - **`index_paths(..., relink_outside: bool = True)`.** `full_scan` passes `False`.
- `devgraph/graph/engine.py`, set (b): `_replace_file_nodes_tx` (and `_delete_by_source_file_tx`) first reads the Module's old `name_ref_sources`, in the same transaction and before the overwrite. It then unclaims `(a {repo_id: $repo_id})-[r]->()`, where `a.name IN $old_sources`, using the `$f IN r.origins` variant.
- `devgraph/graph/engine.py`: `find_name_refs(repo_id, names, skip) -> list[tuple[str, list[str]]]`, passing `sep="\x1f"` as a parameter:
  ```
  MATCH (m:Module {repo_id: $repo_id})
  WHERE NOT m.name IN $skip
    AND (any(t IN m.name_ref_targets WHERE t IN $names) OR any(s IN m.name_ref_sources WHERE s IN $names))
  RETURN m.name AS origin,
         [e IN m.name_refs WHERE split(e, $sep)[5] IN $names OR split(e, $sep)[2] IN $names] AS refs
  ```
- `devgraph/graph/schema.py`: the three properties go into `RESERVED_NODE_PROPERTIES`.
- `devgraph/mcp/tools.py`: the three properties go into `_DESCRIBE_HIDDEN`.
- Tests: `tests/indexer/test_graph_accuracy_live.py`, `tests/indexer/test_dispatch.py`, `tests/mcp/test_describe_node.py`, `tests/watcher/test_watcher_live.py`.

- [ ] Write failing tests:
  - **`test_import_target_deleted_and_restored`** (live).
    - `pkg/a.py` (`from pkg.b import helper`, `main` → `helper`) and `pkg/b.py`.
    - `full_scan`, then two batches: `unlink` + `remove_paths({pkg/b.py})`, then restore + `index_paths({pkg/b.py})`.
    - Incremental equals fresh, and the `IMPORTS` and `CALLS` are back, with `origins == ["pkg/a.py"]`.
  - **`test_module_added_after_its_importer`.** `pkg/a.py` imports `lib.b`; add `lib/b.py` later. Incremental equals fresh.
  - **`test_supertype_added_in_another_directory`.** Python (`app/k.py` `class K(Base)` from `base.b`), and Java (`app/K.java` `extends Base` with an import, `base/Base.java` added later). Both: incremental equals fresh.
  - **`test_second_same_named_function_links_outside_callers`.** `a.py` calls `helper` (defined in `b.py`). Add `c.py` with its own `helper`: `main@a.py -CALLS-> helper@c.py` exists, and incremental equals fresh.
  - **`test_restored_type_regains_foreign_impl`.** Use Task 1's Rust trio. `remove_paths({foo.rs})`, restore, `index_paths({foo.rs})`: `Foo@foo.rs -EXTENDS-> Display` is back (through `name_ref_sources`), and incremental equals fresh.
  - **`test_removed_foreign_impl_is_retracted`.**
    - Use Task 1's Rust trio. Delete the `impl Display for Foo` from `conv.rs` and `index_paths({conv.rs})`.
    - `Foo -EXTENDS-> Display` is gone (set (b), via `conv.rs`'s old `name_ref_sources`), and incremental equals fresh.
    - Repeat the edit with a `full_scan` only: it equals the expected graph.
    - Repeat with `conv.rs` deleted (`remove_paths`): the edge is gone.
  - **`test_symbol_moved_between_files_in_one_batch`.** `a.py` `main` → `helper`, with no import; `helper` moves from `b.py` to `c.py`; `index_paths({b.py, c.py})`. `main@a.py -CALLS-> helper@c.py` exists, and incremental equals fresh. Add `notes.md` mentioning `` `helper` `` (mentions on): its `MENTIONS` to `helper@c.py` matches fresh too.
  - **`test_relink_reads_no_files`** (live, docs and mentions off).
    - Count `Path.read_text` and `dispatch._read_text` calls on paths outside the batch, while a batch adds a name that a non-batch Module refers to.
    - There are zero such reads, and the edge exists.
  - **`test_full_scan_skips_relink`.** Spy on `GraphEngine.find_name_refs`: `full_scan` never calls it, and `index_paths` of an adding batch calls it once.
  - **`test_pass_two_omits_name_refs`.** Spy on `upsert_nodes` during `index_paths`: no Module dict carries `name_refs`. The Module in the graph does.
  - **`test_name_refs_written_empty`.** A file with no by-name edges has `name_refs == []`, and so do the other two lists. A file that loses all its calls is overwritten to `[]`.
  - **`test_name_refs_are_compact`** (unit). Extract `devgraph/cli/main.py` (a real file in this repo):
    - the entries are unique and sorted;
    - no entry contains `{` or the repo id;
    - the total encoded size is under 1/4 of the measured 348 KiB JSON (assert `< 90_000` bytes);
    - two extractions with shuffled rels give equal lists.
  - **`test_name_ref_relink_benchmark`** (live).
    - Seed 5,000 Modules with `upsert_nodes`. Each carries 60 `name_refs` drawn from 2,000 names, with matching target and source lists.
    - Time `find_name_refs` for 20 added names, plus the parse, plus the upsert of the result.
    - Assert the time is under 2.0 s, and log it.
  - **`test_name_refs_hidden_from_describe_node`** (unit).
  - **`test_an_import_target_deleted_and_restored`** (live watcher).
    - Before `start`, write and commit `tools/use.py` (`from pkg.sub.util import helper`, `def go(): return helper()`).
    - `start`, delete `pkg/sub/util.py`, `converges`. Then `git checkout -- pkg/sub/util.py`, `converges`.
    - Branch-switch form: on a new branch delete it and commit, `git checkout -q main`, `converges`.
- [ ] Implement G5, G6 and G2 set (b).
- [ ] `uv run pytest -q`. Commit "Relink by-name edges when their endpoint is added".

### Task 5: Automatic index upgrade (G7)

**Files:**
- `devgraph/indexer/dispatch.py`:
  - `INDEX_FORMAT = 2`.
  - `index_outdated(engine, repo_id) -> bool`.
  - `full_scan` calls `engine.set_index_format(repo_id, INDEX_FORMAT)` last.
  - `catch_up`, when `index_outdated`, returns `CatchUp(indexed=full_scan(...), pruned=0, checked=0, offered=0, unknown=0)` in place of the incremental pass.
- `devgraph/graph/engine.py`:
  - `set_index_format(repo_id, version)`: a `MERGE` on the Repository node that `SET`s `index_format`.
  - `index_format(repo_id) -> int | None`.
- `devgraph/agent/schema_rescan.py`, in `run_once`:
  - Before the `schema_pending` check, `index_outdated(...)` → `self._run_exclusive(repo.repo_id, lambda: self._rescan(repo))`, with no quiet period, then `on_rescanned`, logging "upgraded the graph index of <repo> with a full rescan (N files)".
  - The rest of the loop runs only when the repository is not outdated.
  - An exception follows the existing `_failing` path.
- `devgraph/cli/main.py`: `status` adds a "Graph Index" section, when Neo4j is reachable. It lists each active repository whose index is outdated as `"<repo_id>: rescan pending (the agent rescans it automatically, or run 'devgraph rescan')"`, or prints `up to date`.
- Tests: `tests/agent/test_schema_rescan.py`, `tests/indexer/test_catch_up.py`, `tests/cli/` (the existing `status` test module), and `tests/indexer/test_graph_accuracy_live.py`.

- [ ] Write failing tests:
  - **`test_outdated_index_rescans_without_quiet_period`** (unit, stub engine and registry). The first `run_once` rescans an outdated repository through `run_exclusive`, and calls `on_rescanned`. A current one is untouched.
  - **`test_full_scan_stamps_index_format`** (live). After `full_scan`, `index_format(repo_id) == INDEX_FORMAT`, and `index_outdated` is false. Remove the property: it is true again.
  - **`test_catch_up_upgrades_an_outdated_index`** (live). Pollute a graph as in Task 1, and remove `index_format`. `catch_up(...)` leaves the graph equal to a fresh scan and stamped.
  - **`test_status_shows_rescan_pending`.**
- [ ] Implement G7.
- [ ] Docs:
  - **PROJECT_STATUS.**
    - Rewrite the "Remaining gaps" by-name sentence to describe the `origins`-scoped retraction and the `name_refs` relink.
    - Drop the three watcher-fuzz pre-existing bugs.
    - Correct the skip-mode sentence.
    - Say the upgrade is automatic.
    - Add the spec's "Out of scope" list.
  - **`HANDOVER_node_identity.md`.** Pinned sources, `origins`, the C#/TS partial-`EXTENDS` note, and Go/Rust/C++ cross-file methods.
- [ ] `uv run pytest -q`. Commit "Upgrade outdated graph indexes automatically".

### Task 6: Seeded multi-language fuzz

**Files:**
- `tests/indexer/test_graph_accuracy_fuzz.py` (new, live).

**The repository:**
- `py/` (three modules that import and call each other, a class hierarchy);
- `rs/` (`foo.rs`, `display.rs`, `conv.rs` with an `impl`);
- `java/` (two packages, an `extends` across them);
- `go/` (`go.mod`, two packages);
- `compose.yaml` (two services, `image:` only, no `build:`);
- `Dockerfile`;
- `notes.md` (code-span mentions of symbols from every language);
- `docs/adr-1.md` and `docs/adr-2.md` (`links`, `supersedes`).

`docs_path="docs"`, and mentions on.

**The operations,** drawn by `random.Random(seed)`:
- add or remove a call, a base or an import;
- add or remove the `impl Display for Foo` in `conv.rs` (a foreign-source edge; always among the drawn operations for every CI seed, so set (b) is exercised);
- add or remove a function;
- move a function between two files of the same language;
- rename a file;
- delete a file;
- restore the last deleted file;
- add or remove a service or a `FROM`;
- add or remove a mention;
- add or remove a note's `links`/`supersedes`.

**Exclusions** (spec "Out of scope"):
- no `build:`;
- no datastore or route code;
- no method moved out of its type's file;
- no C++;
- the default `mentions_ambiguous_mode` (`all`).

**Batching:** each step either applies one batch (`remove_paths` of the deleted paths, then `index_paths` of the changed paths) or splits the same changes into one batch per path, in a random order.

- [ ] Write the fuzz:
  - **`test_graph_accuracy_fuzz[seed]`**, parametrised over seeds `1, 2, 3`, with 15 steps each. After every step it asserts `incremental_equals_fresh(..., mentions_enabled=True, docs_path="docs")`. On failure, the message names the seed, the step and the operation log.
  - `DEVGRAPH_ACCURACY_FUZZ_STEPS` (default 15) and `DEVGRAPH_ACCURACY_FUZZ_SEEDS` (a comma list) lengthen it locally. Without them, CI runs the three fixed seeds.
- [ ] Run it with a long setting once, for example `DEVGRAPH_ACCURACY_FUZZ_SEEDS=1,2,...,20 DEVGRAPH_ACCURACY_FUZZ_STEPS=60`. Every divergence is either fixed in the task it belongs to, or added to the spec's "Out of scope" with a matching exclusion. Never weaken the assertion.
- [ ] `uv run pytest -q`. Commit "Add a seeded multi-language graph accuracy fuzz".
