# Graph accuracy: wrong, stale and missing edges — design

Upstream epic: HaydenSchmidtDOC/DevGraph#1. Four bugs that reviewers' fuzzing
and real-client runs found. Each one exists on upstream `master` and on this
branch. The watcher-correctness fuzz listed bugs 2–4 as pre-existing
(PROJECT_STATUS, the watcher paragraph); bug 4 is the "Remaining gaps"
cross-batch referrer gap. Revised after a code-checked review (two live-confirmed
criticals: edge ownership and shared-node recreation).

## Problem

Each bug was reproduced with a scratch repository, `full_scan`, `index_paths`
and `remove_paths` against the local Neo4j. The edges below are
`label:name@file`.

| # | Bug | Reproduction | Wrong graph |
| --- | --- | --- | --- |
| 1 | Same-named functions in different files steal each other's call edges | `src/app.py`: `helper()` and `main()` calling `helper()`; `src/worker.py`: `main()` returning `2`; `full_scan` | an extra `Function:main@src/worker.py -CALLS-> Function:helper@src/app.py` |
| 1b | A call, base class or import removed from a file keeps its edge, even after `full_scan` | `m.py` with `class K(Base)` and `main()` calling `helper()`; edit both away; `full_scan` | `Class:K@m.py -EXTENDS-> Class:Base@m.py` and `Function:main@m.py -CALLS-> Function:helper@m.py` survive |
| 2 | A service removed from `compose.yaml` stays, even after `full_scan` | `compose.yaml` with `api` (`python:3.12`) and `db` (`postgres:16`); drop `db`; `index_paths`, then `full_scan` | `Service:db@compose.yaml`, `Container:postgres` and `db -RUNS-> postgres` survive |
| 3 | A mention removed from Markdown keeps its `MENTIONS` edge, even after `full_scan` | `notes.md` mentions `` `helper` `` and `` `other` ``; edit to mention only `` `other` ``; `index_paths`, then `full_scan` | `Document:notes.md -MENTIONS-> Function:helper@app.py` survives |
| 4 | Deleting and restoring an import target loses `IMPORTS`/`CALLS` | `pkg/a.py`: `from pkg.b import helper`, `main()` calls `helper()`; `remove_paths(pkg/b.py)`, then restore and `index_paths(pkg/b.py)` | `Module:pkg/a.py -IMPORTS-> Module:pkg/b.py` and `Function:main@pkg/a.py -CALLS-> Function:helper@pkg/b.py` missing. The same happens when `lib/b.py` is added after `pkg/a.py` imports it. A `full_scan` heals this one. |

Bug 1b is not in the reported list. It was found while reproducing bug 1,
and it is why bug 1 can't be healed by a rescan alone. Docs notes have the
same flaw: a `links`/`supersedes`/`decided_by` entry removed from a note keeps
its edge. That is now in scope (G2).

## Root causes

1. **The edge source is matched by bare name.**
   `_emit_call` (`devgraph/indexer/python/extractor.py:416-436`) emits
   `CALLS` with `from_file=None`. `_upsert_relationships_tx`
   (`devgraph/graph/engine.py:462-481`) then matches the source as
   `(a:Function {repo_id, name})`. That pattern matches every `main` in the
   repository, so each file's calls are written from every same-named
   function. The same holds for:
   - `EXTENDS` (`python/extractor.py:518-526`);
   - `CONTAINS` from a parent *function* to a nested one
     (`python/extractor.py:569-579`, where `from_file` is set only for a
     `Class` parent);
   - the same three emit sites in each of the other seven code extractors
     (`jsts`, `java`, `csharp`, `cpp`, `go`, `rust`, `kotlin`).

   The handover's "deliberate over-linking" refers to the *target* end,
   because a callee can live anywhere. The source end is a node of the file
   being parsed, except in one case: a Rust `impl Trait for Foo` (and a Go
   method) can sit in a different file from `Foo`. That makes the source a
   by-name reference too.
2. **A re-index never retracts edges.** This is the shared cause of 1b, 2,
   3 and the docs-note flaw.
   - **Code files (1b).** `_replace_file_nodes_tx`
     (`engine.py:493-517`) deletes only the file's nodes that are no longer
     extracted, then MERGEs the rest. A surviving node keeps every outgoing
     edge it ever had.
   - **Compose files and Containerfiles (2).** `_index_compose_file` and
     `_index_containerfile` (`devgraph/indexer/dispatch.py:1635-1646`) go
     through `_upsert_container_result` (`dispatch.py:1673-1693`), which
     only upserts.
   - **Markdown mentions (3).** `mentions.extractor.index_file`
     (`devgraph/indexer/mentions/extractor.py:387-400`) only upserts.
     PROJECT_STATUS's claim that "a rescan drops them" (for skip mode) is
     therefore wrong.
   - **Docs notes.** `docs.extractor.index_file`
     (`devgraph/indexer/docs/extractor.py:182-215`) only upserts.

   An edge carries nothing about who wrote it, so a retraction can't be
   scoped safely by the edge's source node. A docs note writes
   `Module -DOCUMENTED_BY-> note`, out of a code file's Module. `conv.rs`
   writes `Foo -EXTENDS-> Display`, out of `foo.rs`'s `Foo`. A compose file's
   `Service` gets `USES` edges from Python files. The reviewer confirmed
   live that a source-scoped retraction deletes the first two.
3. **The re-claim recreates shared nodes.** `_replace_file_nodes_tx` calls
   `_unclaim_source_tx(file)` (`engine.py:161-180`) for every claim the file
   holds. A node that only this file claims is `DETACH DELETE`d, then
   re-created by the upsert, which loses its incoming edges (`MENTIONS`,
   `Service -USES->`). The Python branch already does this to its
   `Datastore`/`Endpoint` claims on every save, because `_index_datastores`
   and `_index_apis` write them *after* the replace. G3 would add compose
   `Container`s to the same path.
4. **Nothing finds code referrers of an added node (4).**
   - `remove_paths` → `delete_nodes_by_source_file` (`engine.py:46-51`)
     `DETACH DELETE`s the removed file's Module and symbols, together with
     every incoming `IMPORTS`/`CALLS`/`EXTENDS`.
   - When the file comes back, `_expand_with_reverse_dependents`
     (`dispatch.py:1047-1090`) follows only *existing* `IMPORTS` edges, and
     there are none left. (Since index format 4 it followed only Java
     importers: a Python edge depends only on its writer's text, and G5
     relinks it; see 2026-10-10-python-call-resolution-design.md. Since
     format 7 it is gone: every code edge is a function of its writer's
     text, its resolver configuration and whether its target exists; see
     2026-10-11-nonpython-call-resolution-design.md.)
   - `_find_referrers` (`dispatch.py:1125-1152`) knows only docs notes,
     handler stubs and (until format 7, when unpinned `EXTENDS` relinked
     through G5 made it redundant) same-directory JVM subtypes.
   - "Added" is decided by `(label, name)` (`list_file_nodes`,
     `engine.py:1026-1047`). A symbol that moves between files in one batch
     is therefore not "added", even though every edge to its new node is
     missing.

## Decisions

| # | Decision |
| --- | --- |
| G1 | **Pin the source end, and stamp the writer.** A shared helper, `own_edges(result, file_path)` in `devgraph/indexer/common.py`, does two things. It sets `from_file = file_path` on every relationship whose source `(label, name)` is one of `result.nodes` with `properties.file == file_path`. It also sets `origin = file_path` (the writer, which G2 adds to the edge's `origins`) on every relationship. All eight code extractors call it before they return. A Module source is never pinned: it has `source_file`, not `file`, and its name is already the path. A source that isn't a file-scoped node of this file is never pinned either: the C++ out-of-class `Class` stub, or a Rust/Go type defined elsewhere. Target ends stay bare-name matched (over-linking by design). |
| G2 | **Ownership is "edges this file wrote", kept as a sorted `origins` list on the edge.** `GraphRelationship` and the rel dicts gain `origin` (the writing file). `_upsert_relationships_tx` adds it to `r.origins` with a sorted insert, in Cypher with no APOC: `[x IN coalesce(r.origins, []) WHERE x < row.origin] + [row.origin] + [x IN coalesce(r.origins, []) WHERE x > row.origin]`, skipped when it is already listed or `row.origin` is null. A multi-writer edge (two Python files of one service using one store) is therefore deterministic. Every built-in extracted-edge writer passes `origin`: the code extractors (G1), compose and Containerfile, the Python owning-service and API passes, docs notes, mentions, and the G5 relink (the referrer's path). **One retraction primitive, *unclaim edge*:** remove `$f` from `r.origins`, where an edge with no `origins` (legacy) counts as `[$f]` (`coalesce(r.origins, [$f])`), and delete the edge when the list is empty. It is applied to four anchored, `repo_id`-scoped edge sets, each in the transaction of the write that follows. **(a) Owned sources:** `(a)-[r]->()` where `a` is owned by the file (`a.file = F OR a.source_file = F OR (a:Module AND a.name = F)`). This runs in the same single scan as the stale-node delete in `_replace_file_nodes_tx`. **(b) Foreign sources:** before the Module is overwritten, the replace reads its *old* `name_ref_sources` (G5). It unclaims edges `(a {repo_id, name IN old_sources})-[r]->()` with `$f IN r.origins`. No legacy rule applies here: a legacy edge isn't attributable. Such an edge is healed by (a) on its source's file during the upgrade rescan, and then re-written with `origins` by its real writer in pass 2. This is how `conv.rs` dropping `impl Display for Foo` retracts. **(c) The Python service and API pass:** `(:Service {repo_id})-[r:USES]->()` and `(:Endpoint {repo_id})-[r:CALLS|IMPLEMENTS]->()`, unclaimed for a Python file's replace with the legacy rule. The owning-service edges are re-added in `index_paths`' final service pass, after every batch file is indexed. Between the file's replace and that pass, a reader can briefly miss them; this is accepted and commented, because the pass must see the whole batch's compose files. **(d) Docs notes:** `replace_doc_note(repo_id, file_name, nodes, rels)` anchors on `(n:Requirement|DesignDecision|ArchitectureNote {repo_id: $repo_id, source_file: $f})`, the labels the docs extractor writes, so it scans only notes. It unclaims the incoming `DOCUMENTED_BY`/`SATISFIES` into those nodes and the outgoing `SUPERSEDES`/`DECIDED_BY` from them, with the legacy rule, then upserts. Every docs-note edge touches the note's own node, so this is complete. It is never a cross-repo or unanchored scan. `delete_nodes_by_source_file` (file deletion) applies (b) and (c) before its `DETACH DELETE`, so a deleted file's foreign and service/API edges lose that file's origin too. Edges another file wrote out of an owned node keep their own origins and survive: `DOCUMENTED_BY`, a Rust `impl` edge, `Service -USES-> Datastore`. Edges with no built-in writer are never touched: provider, filesystem, `MODIFIES`, `RESOLVES`. |
| G3 | **Compose files and Containerfiles go through `replace_file_nodes`, and a replace re-claims instead of recreating.** `_upsert_container_result` calls `engine.replace_file_nodes(repo_id, rel_path, nodes, rels)`. `_unclaim_source_tx` gains `keep`: the `(label, name)` pairs of claimed nodes (no `file`, with `source == F`) in the new extraction. It drops only the claims that are gone, and the upsert then rewrites the kept claims' properties through `_claim_nodes_tx`. `delete_nodes_by_source_file` passes no keep (everything goes). The Python branch moves its `Datastore`/`Endpoint`/handler-stub node writes and its API relationships into the same `replace_file_nodes` call (extract first, write once), so a single-claimant `Datastore` keeps its `MENTIONS`. Compose writes only `Container` and `Service` nodes: `Volume`/`Network` are extracted but never upserted, and so are their `USES` edges. |
| G4 | **A full mentions re-index replaces the Document's `MENTIONS`.** `GraphEngine.replace_mentions(repo_id, file_name, nodes, rels)` (the same signature as `replace_file_nodes`) deletes `(:Document {name: file_name})-[:MENTIONS]->()` and writes the nodes and edges in one transaction. `index_file` uses it when `names is None`. The relink (`names=...`) stays additive. `replace_file_nodes` is not reused: it would delete a docs note at the same path as stale. |
| G5 | **Relink by-name edges when a batch adds their endpoint.** Three Module properties are written at extraction, always, as an explicit empty list when there is nothing to record. `name_refs` is the file's by-name edges as sorted, de-duplicated strings joined by `\x1f`: `rel_type, from_label, from_name, from_file, to_label, to_name, caller_class`. "By-name" means `to_file` is unset, or the source is an unpinned code symbol (`schema.FILE_SCOPED_LABELS`); a route's pinned `IMPLEMENTS` (G8) is not, since the route file writes both its ends. `name_ref_targets` is their sorted distinct `to_name`s. `name_ref_sources` is the sorted distinct `from_name`s of unpinned non-Module sources. These properties are written only by the pass-1 replace; the pass-2 node re-upsert omits them. "Added" is keyed by `(label, name, file)`, with the same `file` on both sides. `list_file_nodes` returns `coalesce(n.file, n.source_file, n.path)`, or `n.name` for a Module. `_batch_nodes` uses: a code node's `properties.file`, or the path for its Module; for a docs note, the note's repo-relative path (`path.relative_to(root).as_posix()`, not the `path.name` its extraction is keyed with), matching the `source_file` that `index_doc_file` writes; for a `Document`, its repo-relative path; for a compose `Service`, the compose file (`batch_services` becomes `(label, name, rel_path)` triples); and for a provider node, its `path`. After pass 2, `index_paths` runs one read: Modules outside the batch and outside the `_find_referrers` files whose targets or sources meet the added names. The read returns `[e IN m.name_refs WHERE split(e, $sep)[5] IN $names OR split(e, $sep)[2] IN $names]`, with `$sep = "\x1f"` passed as a query parameter (no Cypher escape), with the list-property name filter applied first. Python keeps the entries whose `(to_label, to_name)`, or unpinned `(from_label, from_name)`, matches an added node, and upserts them with `origin = m.name`. The end that matched is pinned to the added node's `file` when its label is file-scoped (`Class`/`Function`/`Service`, `schema.FILE_SCOPED_LABELS`, renamed from the private `_FILE_SCOPED_LABELS` for this); other labels, and the other end, stay bare. An added handler stub has `file` "", and an end pinned to "" matches only the file-less node of that name. A bare-name match can't use the `(repo_id, name, file)` index, so pinning takes the benchmark from 30 s to under 1 s, and it loses no edge: the referrer's edges to same-named nodes that already existed were written when it was indexed, or relinked when they were added. Old `name_ref_sources` also anchor G2(b). No file is read, and there is no cap. `full_scan` skips G5 (`index_paths(..., relink_outside=False)`), because it has no files outside the batch. Since index format 4 (2026-10-10-python-call-resolution-design.md) an entry also records an edge whose target is pinned to another file or to a package directory, one entry per source and target name, with two more fields: the target pins and the confidence. A seven-field entry still parses, as unpinned. A shared node (`file` "") the batch both had and has counts as added too when every claim on it came from the batch's files and one of them dropped its claim (`_written_back_shared`, from `list_shared_claims` before and after the batch): files go in path order, so the dropping file can delete it, with every edge into it (a Document's `MENTIONS`), before a later file claims it back. |
| G6 | **Hidden and reserved.** `name_refs`, `name_ref_targets` and `name_ref_sources` join `RESERVED_NODE_PROPERTIES` and `INTERNAL_NODE_PROPERTIES`, like `claims`. `INTERNAL_NODE_PROPERTIES` (`graph/schema.py`) is the one hidden list: `describe_node` hides it, and the dashboard's node inspector hides what the schema route serves as `hidden_properties`. |
| G7 | **Automatic upgrade.** `INDEX_FORMAT = 2` in `dispatch.py` (3 since route `IMPLEMENTS` were pinned, G8, so the same upgrade drops their cross-file edges; 4 since Python calls are resolved through imports). `full_scan` stamps `Repository.index_format = INDEX_FORMAT` when it finishes. `index_outdated(engine, repo_id)` is true when the Repository node's `coalesce(index_format, 1)` is below the constant. Three places act on it. `SchemaRescanScheduler.run_once` treats an outdated repository like a pending schema, but with no quiet period: it runs `full_scan` under `run_exclusive` and calls `on_rescanned`. `catch_up` runs `full_scan` instead of the incremental pass when the index is outdated, so the agent's start catch-up upgrades too. `devgraph status` lists every active repository with an outdated index as "rescan pending". No user action is needed: the first rescan stamps origins, drops the wrong and stale edges, and fills `name_refs`. The scheduler checks `index_outdated` again inside `run_exclusive`, so it skips a repository the start catch-up (or a dashboard registration) upgraded while it waited on the batch lock. A repository with no `last_indexed` (never indexed) is not outdated to the scheduler or `status`: its first scan belongs to `devgraph add`/`rescan`, which may be running in another process and stamps the format when it finishes. |
| G8 | **A handler stub claims only file-less nodes.** `_claim_nodes_tx` matches `(n:{label} {repo_id, name}) WHERE n.file IS NULL`, and creates the node when there is none, instead of `MERGE (n:{label} {repo_id, name})`. A route's handler stub is therefore its own file-less `Function`, whichever file is written first, and never claims another file's (or its own file's) `def search()`. A FastAPI or Flask `Endpoint` `IMPLEMENTS` the stub and its own file's `Function` of that name (the handler `def` follows its decorator), with `to_file` set to the route file and to "" (the file-less stub); a same-named `Function` in another file gets no edge. A Django `Endpoint`, whose view is usually imported from `views.py`, still `IMPLEMENTS` every `Function` of that name by bare name until imports are resolved. Fresh and incremental scans agree. A handler that loses its route decorator loses the stub with it (the unclaim deletes the stub's last claim), so the real `Function` never carries `type: handler`, `source`, `sources` or `claims`. Other claimed labels (`Datastore`, `Endpoint`, `Container`) never have a `file`, so nothing changes for them. A C++ out-of-class method (`void Widget::draw() {}`) claims a file-less `Class` stub for its qualifier the same way (`Class` is a claimed label): it never merges onto the header's `Class`, a by-name edge into `Widget` (a subclass's `EXTENDS`) relinks to it when it is added, and it goes with its last claimant. |

## Interactions checked

- **Watcher batch semantics.**
  - G2 runs inside each file's pass-1 transaction, and pass 2 re-creates
    in-batch edges as before.
  - A docs note and a code file it links to can be in the same batch. The
    note's edge lists only the note in `origins`, so the code file's replace
    can't delete it, and the note's docs pass re-writes it.
  - G5 runs after pass 2. A delete-then-restore split across two batches (a
    git checkout or a branch switch) is exactly its case.
  - A symbol moving between files within one batch is "added" under
    `(label, name, file)`. The mentions relink and `_find_referrers` get the
    pair projection of that set. A moved symbol therefore also gains the
    `MENTIONS` that a fresh scan gives it. Docs entries that `_read_docs_batch`
    reports as `existing` (field-keyed, moving between files) are still not
    "added".
- **Shared-node attribution.**
  - G2 never matches a claimed node: it has no `file`/`source_file`.
  - G3 keeps kept claims' nodes in place, re-attributed by `_claim_nodes_tx`,
    and deletes a node only when its last claim goes.
  - `Service -USES-> Datastore` and `Endpoint -CALLS-> Service` list the
    Python files that wrote them, so a compose re-index keeps them. When one
    of two writers stops writing, or is deleted, the edge stays with the
    other writer; it goes with the last.
- **Docs provider.**
  - Provider edges are non-built-in types, written without `origins` out of
    provider nodes, and every G2 set is anchored on built-in nodes or types,
    so no retraction matches them.
  - Provider edges into code nodes are incoming edges.
  - `_relink_docs` is unchanged.
- **Mentions.** The relink adds the doc to `origins`, as the full index does.
  Skip mode: adding a second node with an already-mentioned name still
  leaves the old edge until the doc is re-indexed (G4 now drops it then) or
  rescanned.
- **Cost.** `name_refs` was 348 KiB as JSON for `cli/main.py`. The compact,
  de-duplicated form drops the keys, `repo_id` and repeats. It is written
  once per file save and never re-upserted in pass 2. The G5 read filters
  Modules on the short name lists before splitting any entry. A benchmark
  pins the read at about 5,000 Modules.

## Out of scope (known gaps, recorded in PROJECT_STATUS)

- **Same-file sibling collisions** (file+scope identity) and IMPORTS-based
  call precision. These are the handover's open items 1 and 2. The first is
  resolved by owner identity (index formats 11 and 12, below), except the
  cases its Limits list.
- **A compose-only batch that adds `build:`.** It gets no owning-service
  `USES`/`CALLS` until the owned Python files are re-indexed. The fuzz
  excludes it.
- **Go/Rust methods defined in another file than their type.** Their
  `CONTAINS` is pinned to the method's file, where the type doesn't live,
  so it is never created (fresh and incremental alike). The C++
  out-of-class method has the same gap: its `CONTAINS` comes from a
  file-less `Class` stub, under a `from_file` the stub doesn't have.
- **C#/TS partial classes.** Each file's `partial class Foo : Base` is its
  own node, so `EXTENDS` hangs off the declaring file's node only (noted in
  `HANDOVER_node_identity.md`).
- **Recency and `MODIFIES` after delete-and-restore.** This still needs
  `devgraph rescan --full`.
- **Deletions don't relink.** For example, a skip-mode ambiguous name stays
  unlinked after the duplicate goes.
- **A docs note whose `id` changes in place leaves the old-id node.** A
  `full_scan` doesn't heal it either. The fuzz never changes a note's `id`.
- **A docs note moved to another path, indexed there before the old path is
  removed** (two batches: the new file, then the deletion). The note's node
  takes the new `source_file`, so the removal no longer matches it, and its
  edges keep the old path in `origins`. An edge the new file later stops
  writing then survives. A single batch (a rename seen as one change) and a
  `full_scan` are correct. The fuzz removes a renamed note's old path first
  when it splits a batch.

## Docs to update

- **PROJECT_STATUS.**
  - In "Remaining gaps", drop the `IMPORTS`/supertype/`CALLS` cross-batch
    gap.
  - In the watcher paragraph, drop the three "Pre-existing bugs found by the
    watcher fuzz".
  - Correct the skip-mode sentence.
  - Say the index upgrade is automatic (G7), and list "Out of scope" above.
- **`HANDOVER_node_identity.md`.**
  - Sources are pinned; only targets are bare-name.
  - Edges carry a sorted `origins` list.
  - The C#/TS partial-`EXTENDS` note.
- **Docstrings.** `GraphRelationship` (`common.py`), and `index_paths` and
  `_find_referrers` (`dispatch.py`).

## Same-file siblings: owner identity (index format 11, 12)

Audit 4, item 1. Two same-named methods in one file (`class A: def save`
and `class B: def save`; Go `String()` on two types; Rust `new` in two
`impl`s; a Kotlin interface's `go` and its implementation's) were one node:
the MERGE key `(repo_id, name, file)` fused them, so `get_source` returned
whichever was written last and `find_callers` could not tell them apart.
The extractors already emitted both; only identity lost one.

### Options weighed

| | A: qualify `name` (`A.save`) | B: add `owner` to the identity key |
|---|---|---|
| Bare-name lookups (`find_callers('save')`, `get_source`, `search_component`, `impact_analysis`) | break: each must also match a second "short name" property | unchanged: `name` stays `save` |
| CALLS/EXTENDS/IMPORTS targets, `name_refs`, relink, `name_ref_targets` | every by-name target end matches `name`, so every resolver, the relink and the G5 read would have to match the short property instead | unchanged: a target still matches `name`; a pinned target in a file with siblings links every sibling of the name, as an unpinned one links every same-named node |
| C# match keys (`Ns:Owner`) | unchanged | each sibling gets its own node, so its keys are its own owner's: a `@Ns:A` pin no longer reaches `B.Run` |
| MENTIONS (Markdown by bare name) | `save` matches nothing any more | unchanged: it mentions both |
| Dashboard `identity_key`, `compare_branches` | keys change for every method | the key gains the owner only for a node that has one |
| Constraint and migration | the `(repo_id, name, file)` constraint can stay | the constraint becomes `(repo_id, name, file, owner)` |

B is chosen: user-facing lookups by bare method name keep working and
resolution keeps matching `name`, so only the ends that must tell siblings
apart learn the owner.

### Decisions

| # | Decision |
|---|---|
| O1 | **`owner`.** Every `Class` and `Function` an extractor writes with a `file` carries `owner`, by this grammar: the names of the declarations enclosing it, classes and functions alike, outermost first, joined by `.`; `""` at the top level. A method `save` of `A` has owner `A`; of `Inner` nested in `Outer`, `Outer.Inner`; a function `helper` nested in method `A.save` has `A.save`, nested in a top-level `save`, `save`, so it never merges with a method `A.helper`, and two nested helpers of two functions never collide. A namespace, package, module or Rust inline `mod` is never part of an owner. The type is the one the language attaches the member to: a Go method's receiver type with its type parameters stripped (`*Stack[T]` is `Stack`); a Rust `impl` block's type; a Kotlin extension function's receiver type, after any enclosing classes (`fun A.save()` is owned by `A`, as a Go method by its receiver); a C++ out-of-class definition's qualifier classes, leading segments that name a namespace of the file dropped (`ui::Outer::Inner::run` in a file declaring `namespace ui` is `Outer.Inner`, as a member defined inside `Inner` nested in `Outer`; a qualifier naming a namespace declared elsewhere stays in it). A C++ explicit specialisation (`template<> class Stack<int>`) is the class `Stack`, so its inline members and an out-of-class `Stack<int>::push` agree on owner `Stack`, and a class nested in a class body (`class Inner { ... };`) is extracted. A compose `Service` (also keyed by file) has owner `""`. `common.member_owner(parent_label, parent_name, parent_owner)` is the rule. |
| O2 | **Identity.** A node with a `file` MERGEs on `(repo_id, name, file, owner)`; `owner` is always written (`""` when the extractor sets none), so the key is never null. The uniqueness constraint `<label>_repo_name_file` becomes `<label>_repo_name_file_owner` on Class, Function and Service (`constraint_statements` drops the old one first). An end pinned to a file seeks the new constraint's index: equality on all four keys when the row names the owner, else on the first three with `owner IS NOT NULL` (a directory pin by prefix the same way), which Neo4j 5 plans as a unique-index seek. There is deliberately no separate `(repo_id, name, file)` index: an older DevGraph creating its constraint of that shape would fail on it (`IndexAlreadyExists`). `identity_key` appends the owner when it is not `""`, so a top-level node's dashboard key is unchanged. |
| O3 | **Edge ends.** `GraphRelationship` gains `from_owner` and `to_owner`. On an end pinned to a file or a path, a non-null owner must equal the node's `owner`, so a wrong one links nothing; null matches every node of the name there (the pre-owner behaviour). The extractors set `from_owner` on every edge out of their own symbols; `own_edges` fills it in when the file has one node of that label and name, and sets a `CONTAINS` target's `to_owner` from its parent by O1. A resolver that knows the owner of a call target writes it as an *owned pin* (`common.owned_pin`, the pin and the owner joined by `OWNER_PIN_SEP`), which `call_rows` splits into `to_file` and `to_owner`, keeping the pins' precedence and exclusions as they were; a pin some call site reached with no owner links every member of the name. An owner is written only where it is certain. (a) A class of the writer's own file, whose methods and in-file bases the resolver reads: Python `self.m()`, `cls.m()`, `X().m()`, a receiver typed `X`; JS/TS `this.m()` and typed receivers; Java, Kotlin, C# calls into a class declared in the file; Go typed receivers; Rust `self.m()` and `T::f()` defined in an `impl T` of the file; the class (or the in-file base) that declares the method. (b) Another file's top-level function, which nothing inherits: a Python from-imported or star-imported name called, a module attribute `m.f()` and a submodule's; a JS/TS ES named import called, an ES namespace's member `ns.f()`; a Go package's function, `F()` or `pkg.F()`: owner `""`. Everything else names no owner and links every member of the name in the target files, as before owners: a method of another file's class (`from m import User; User().save()`, `User.create()`, a JS/TS or Java imported class, a typed receiver of one), since `save` can be inherited from a base declared in `User`'s file, which the writer doesn't read and whose change wouldn't re-resolve the writer (incremental would then differ from fresh); a JS/TS default import or `require` binding, which can name a class of another name or an object; an ALL_CAPS or other from-imported instance. A possible later fix for the first: record on each class node the in-file classes it inherits from, pin the member to the class and those bases, and relink a writer's entries when that list changes. C# narrows by owner through its match keys already (`Ns:Owner`, with its base classes' keys). A route's `IMPLEMENTS` names its handler's owner too: `""` for a module-level handler, the class for a method of a top-level class (a class-based view), else none. |
| O4 | **`name_refs`.** An entry written since owners (index format 12; format 11's own layout differed, so 12 rescans it) has a fixed layout of twelve fields, the last `NAME_REF_OWNED`: `rel_type, from_label, from_name, from_file, to_label, to_name, caller_class, from_owner, pins, confidence, keys, NAME_REF_OWNED` (so the G5 read's `split(e, $sep)[2]` and `[5]` are unchanged). `from_owner` is `=` and the source's owner, or empty when unknown; a pin names its `to_owner` as an owned pin; `keys` is always there, empty without match keys. An entry an older index wrote has no marker and is read by its length: seven fields (unpinned), nine (pinned), ten (pinned with keys), each with an unknown source owner and owner-less pins; any other entry is skipped with a debug line, never failing the batch. Relink keeps an owned pin's owner: it relinks an added node only when the pin names no owner or the node's. |
| O5 | **Batches.** "Added", the match-key snapshot and `write_match_keys` key a node by `(label, name, file, owner)` (`list_file_nodes` returns the owner; a shared or Module node has `""`, and a node an index older than owners wrote, None). Adding `B.save` next to an existing `A.save` is therefore an addition: the relink pins the matching end to `(file, owner)` of the added node, so it never re-links a sibling a key pin left out. A file whose nodes an upgrade scan left without an owner (it failed then) is indexed again with its nodes counted as added, so the edges other files' pinned ends skipped into them are relinked. The replace keeps a file's nodes by `(label, name, owner)`. |
| O6 | **Upgrade.** `INDEX_FORMAT` 11 (with audit 4 item 5's extractor fixes), then 12 (the final `name_refs` layout and owners); either upgrades the same way. (1) `init_schema`, which every start of the agent, MCP server, dashboard and CLI and every scan runs before indexing, drops the old constraints and creates the new ones; nothing writes a node first. (2) It can't meet a duplicate key: a node an older index wrote has no `owner`, so the new constraint leaves it out, and every node this version writes has one, kept unique by its MERGE; this version never creates the old constraint again. (3) Each statement is `IF EXISTS`/`IF NOT EXISTS`, so a start cut short (an old constraint dropped, its new one not yet created) is finished by the next, and two processes starting at once (the agent and the MCP server) each run the same idempotent statements; a deadlock between their schema writes is a transient error, retried. (4) Until the automatic rescan, a node without `owner` is outside every end pinned to a file. The rescan's replace keeps a file's old node when its `(label, name)` is kept with owner `""` (it is given that owner, so a top-level symbol keeps its node and its incoming edges) and deletes it otherwise (a method, now owned by its class, is replaced by one node per owner); a node is either given its owner or deleted, never both in one transaction, since Neo4j 5.26 then leaves it in the transaction's view of the new index (`EntityNotFound` on the next seek). The rescan rewrites every code edge, so only an edge no rescan writes into a method node (a provider's) is lost with it; MODIFIES ends at Modules. `test_owner_identity_live.py` starts from the old constraints and an owner-less format-10 graph. **Downgrade** is not supported: an older version re-creates its `(repo_id, name, file)` constraint at start, which works while no two nodes share those keys and fails once the graph holds same-file siblings, so that version cannot start against the database; going back means dropping the `*_repo_name_file_owner` constraints and registering the repositories again with it. |
| O7 | **Tools.** Lookups by bare name are unchanged and match every owner. `get_source` and `describe_node` also take a name qualified with its owner, `A.save`, `A::save`, `Outer.Inner.run` or `Outer::Inner::run`, when no node has that exact name, and both take `owner`. Every candidate and ref of a node with a `file` carries its `owner` (`""` for a top-level one), so a top-level `save` beside `A.save` and `B.save` is reachable and a ref is the next call's arguments as it is; `get_source` returns the owner too. The owner is read only off a node with a `file`: a declared type's own `owner` field (a runbook's front matter) is neither reserved nor read as one. `compare_branches` keys a changed symbol by name, file and owner and returns `caller_owner` and `calls_owner`; `impact_analysis_for_diff` matches by owner too. `search_component` rows and the dashboard's search rows carry `owner`. `find_callers('save')` still returns the callers of every `save`. |

### Interactions

- **MENTIONS** stay by bare name: a document mentioning `save` mentions
  both siblings, in a fresh scan and through the mentions relink alike.
- **C# match keys.** A file's same-named methods in two classes are two
  nodes, each with its own owner's key (`Ns:A`, `Ns:B`); overloads, and one
  class name in two namespaces of one file, are still one node with the
  union of their keys.
- **Handler stubs and C++ `Class` stubs** have no `file`, so no owner; the
  claimed nodes are unchanged.
- **Cost.** One more property per symbol; a pinned end seeks the new
  constraint's index (no index more than before), and `name_refs` grow by
  one field per entry (1 % to 3 % on the measurement fixtures).

### Limits

- **Rust trait methods of one type.** `impl Display for Foo` and `impl Debug
  for Foo` in one file each define `fmt`, owned by `Foo`: one node, as two
  overloads are. The trait is not part of the owner.
- **One type name in two namespaces of one file.** `namespace a { class X }`
  and `namespace b { class X }` (C++, C#), or two inline Rust `mod`s each
  declaring `X`, are one node: a namespace is no owner (C# keeps both
  namespaces' match keys on it).
- **C++ qualifiers.** An out-of-class definition's qualifier segments are
  read as classes unless the file declares a namespace of that name.
- **Cross-file owners** are known only where O3 lists them (another file's
  top-level functions); a method of another file's class links every
  same-named member of that file, inherited methods being unknown there.

### Fuzz

`test_graph_accuracy_fuzz.py` gains `op_twin`: a Python file gains or loses
a second class `Twin` whose `act` and `shared` repeat its other class's
method names, `act` calling `self.shared()`, both nesting a `Meta` with its
own base (EXTENDS out of a sibling). `op_shadow` adds a top-level `act`,
`op_nested` an `act` nested in a function, `op_go_twin` a Go type whose
methods are named like its package's functions; calls `b.Twin().act()`
reach one owner's member in another file. Its incremental graph must equal
a fresh scan, edges compared by `(label, name, file, owner)` at both ends.
