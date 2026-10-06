# Docs Front-Matter Keys Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A docs-sourced node type can be keyed on one front-matter field (`key: [adr_id]` with `fields: {adr_id: id}`). Docs links can then name the target by id (`supersedes: ADR-012`). Ownership by file, duplicates, renames, re-ids, deletes and key switches all behave deterministically.

**Spec:** `docs/superpowers/specs/2026-10-06-docs-frontmatter-keys-design.md` (the addendum). Every task implements the sections it names. The slice 1 spec (`2026-10-06-declarative-providers-design.md`) and `2026-10-05-schema-constraint-cleanup-design.md` still govern everything this slice reuses.

**Working directory:** this worktree, branch `epic1/docs-frontmatter-keys` (from `epic1/declarative-providers`). Run `uv run` from inside it, because the editable install otherwise imports another checkout. Live tests use Neo4j at `bolt://127.0.0.1:7687` (`neo4j`/`devgraph-local-dev`), unique repo_ids and cleanup in teardown. Never touch `~/.devgraph`. Dashboard test clients use `base_url="http://127.0.0.1"`.

## Global Constraints

- **Nothing new executes.** Slice 1's rules hold:
  - no `re` on user-supplied patterns;
  - no `eval`, `exec`, subprocess or import;
  - no format-string expansion of user data.

  Key values are Cypher parameters only. Labels and property names come from the validated schema.
- **Every slice 1 bound still applies:**
  - `read_bounded` and `bounded_safe_load(..., YAML_MAX_NODES)`;
  - the 4 KiB string and int64 caps, and the condition caps;
  - the glob matcher;
  - `type(v) is int`, with bool never an int.

  Key values also refuse empty text, control characters (C0, DEL, C1), format characters (Cf), lone surrogates and a leading `./` (addendum §3).
- **Keys are text.** Compare exactly and case-sensitively. Never trim or case-fold.
- **Ownership comes from disk, never the graph** (K6). Winner selection is a pure function of the files a type selects. The graph is read only for snapshots (`previous_nodes`, `existing_node_names`, `extracted_nodes_at`), never to decide a winner.
- **Path-keyed and filesystem behaviour stays identical.** Switching the path-scoped engine calls to `n.path` must not change any slice 1 test. A repository without field-keyed types writes the same graph as before. The expansion runs only for touched field-keyed types.
- **The provider still owns only its own data and fails closed** (slice 1 §3.6–3.7). Each new docs step is inside the existing `try/except` passes.
- **YAML stays the source of truth on the Config page.** The form only writes `#configYaml`, and its hints are advisory.
- **TDD.** Each task starts with failing tests, then the implementation, then `uv run pytest -q` (the full suite, with node available).
- **Commits.** Plain imperative messages, with no `Co-Authored-By` trailer and no AI attribution. Never stage `uv.lock`, real names or personal paths.

## Review Focus

1. **Determinism (K3, K6).** The winner for a key is the first claimant by path in every order of events:
   - full scan;
   - copy saved first, or original saved first;
   - rename as delete-then-create, or create-then-delete;
   - re-id of the owner with a later claimant waiting.

   The same files must give the same graph.
2. **Edge preservation on move.** An id that survives (rename, takeover, successor) is MERGEd onto its new `path`, so incoming edges are kept, not rebuilt by relink. The old owner's outgoing edges are deleted after the move, not before (the `_sync_docs` reorder).
3. **Deletion scope.** Every path-scoped engine call matches `n.path` and still filters on `extractor`. The slice 1 shared-path tests (filesystem `File` plus docs `Runbook`) still pass unchanged. `remove_paths` deletes nothing whose id moved elsewhere.
4. **Key switch.** Path→field and field→path, on a repository with duplicate ids under path keying, end with:
   - the new constraint in place;
   - every entry written;
   - links rebuilt.

   The deferred upsert runs only for labels whose recorded key differs. Its failure is a warning, not a crash.
5. **Key value refusals.** Check that each of these is left out with a reason:
   - bool, float, list, date;
   - int64 overflow, empty, 4 KiB+;
   - U+202E, a C0/C1 control, a lone surrogate;
   - a leading `./`.

   No refused value reaches a parameter.
6. **Cost.** The expansion reads a touched type's files once per batch (reusing front matter already read). It writes only the claimants of affected keys. An untouched keyed type costs nothing.

### Task 1: Schema: field keys for docs sources

**Files:**
- `devgraph/config/project_schema.py`: in `_check_key_and_metadata`, a docs source accepts `[path]` or one declared `string` field other than `path`, and requires a declared string `path` field. These are the three messages of addendum §2. Filesystem sources are unchanged.
- `devgraph/config/edits.py`: `schema_change_warnings` adds the docs key-change line (addendum §6) for a docs type whose key changed.

Tests: `tests/config/test_project_schema.py`, `tests/config/test_edits.py`.

- [ ] Write failing tests:
  - **Valid.** The addendum's Adr example validates. So do `fields: {adr_id: id}` and the same-name default.
  - **Refused, each with its plain-words message:**
    - a composite key `[adr_id, title]`;
    - a key on an `integer` field;
    - a key on an undeclared field;
    - a field-keyed docs type without a `path` field;
    - one whose `path` is not a string.
  - **Unchanged.** Every existing `[path]` docs and filesystem fixture still validates. A filesystem type keyed `[name]` is still refused with the old message.
  - **Provisioning.** `resolve_declaration`/`constraint_statements` emit `(repo_id, adr_id)` uniqueness plus the `_repo_name` index for a field-keyed docs type.
  - **Warnings.** `schema_change_warnings` gives the docs key-change line for path→field and field→path. It does not give it for a filesystem type or an unchanged key. The existing key-change constraint warning stays.
- [ ] Implement addendum §2 and the warnings bullet of §6.
- [ ] `uv run pytest -q tests/config`, then `uv run pytest -q`. Commit "Allow docs node types keyed on a front-matter field".

### Task 2: Pure provider: key values, first-path-wins and claims

**Files:** `devgraph/indexer/providers/docs.py`:
- `DocsType.key: DocsField | None`, set by `docs_spec`;
- `_key_text(raw)`, which returns `(text, None)` or `(None, reason)`;
- `_node` names the node by the key and writes the key field with the same text;
- `_evaluate` tracks the first claimant per `(label, key)` and yields a loser problem naming the winner;
- `keyed_claims(spec, selected)`;
- `unmatched_report`'s "named by 'id', not by file path" hint for field-keyed targets.

Tests: `tests/indexer/test_docs_provider.py`.

- [ ] Write failing tests, with no Neo4j:
  - **Node shape.** A field-keyed node has `name = "ADR-012"`, `path = <rel>`, `extractor`, and `adr_id = "ADR-012"`. Path-keyed nodes are unchanged.
  - **Key text.**
    - `id: 12` gives `"12"`, and `supersedes: 12` makes an edge to `"12"`.
    - Each of these is left out with its exact reason: bool, float, list, map, date, null, absent, empty, int64 overflow, 4 KiB+ string, U+202E, `\x07`, `\x85`, a lone surrogate, and `./ADR-1`.
    - `required: false` on the key field still leaves the file out.
  - **Duplicates.** Three files claim `ADR-012`. Only the first by path gets a node and edges. Each other one gets a problem naming the winner. The result is the same whatever the order of `files`.
  - **Scope.** Two types (`Adr`, `Rfc`) may share a key value.
  - **Conditions and skips.** A file failing `where`, or skipped for a required field, never claims a key, so the next claimant wins.
  - **`keyed_claims`.** It returns claimants in path order per `(label, key)`, and nothing for path-keyed types.
  - **`source_report`.** It shows the duplicate and missing-id lines of addendum §6, under the existing five-file cap.
  - **`unmatched_report`.** It adds the field-key hint only when the `to` type is field-keyed.
  - **Edges.** An edge from a field-keyed node uses the key as `from_name`. An edge to a field-keyed type uses the value as `to_name`, and the `./` normalisation of edge values is unchanged.
- [ ] Implement addendum §3 and the doctor bullet of §6.
- [ ] `uv run pytest -q tests/indexer/test_docs_provider.py`, then `uv run pytest -q`. Commit "Key docs entries on a front-matter field, first path wins".

### Task 3: Engine: path-scoped provider calls match `path`

**Files:** `devgraph/graph/engine.py`:
- `_DELETE_EXTRACTED_PATHS_CYPHER`, `_PRUNE_EXTRACTED_AT_CYPHER`, `_DELETE_EXTRACTED_EDGES_CYPHER` and the provider branch of `list_file_nodes` use `n.path` in place of `n.name`;
- a new `extracted_nodes_at(repo_id, extractor, paths) -> set[tuple[str, str]]`, which returns `(label, name)` at or below the paths with the delete's predicate;
- the comment above these constants describes `path` as the owner and `name` as the key.

Tests: `tests/graph/test_engine_extracted_nodes.py` (live).

- [ ] Write failing live tests. Seed a docs node with `name = "ADR-012"` and `path = "decisions/a.md"`:
  - **Deletes and prunes.** `delete_extracted_nodes(["decisions"])` deletes it. `prune_extracted_at(["decisions/a.md"], keep=[])` prunes it, and `keep=["Adr:ADR-012"]` keeps it.
  - **Edges.** `delete_extracted_edges(["decisions/a.md"])` removes its outgoing non-built-in edges.
  - **Snapshot.** `list_file_nodes(["decisions/a.md"])` returns `("Adr", "ADR-012")`.
  - **`extracted_nodes_at`.** It finds the node at and below a folder, and returns nothing for `decisions2/`, another extractor, or another repo.
  - **Unchanged.** Every existing test in the file passes unchanged (filesystem and path-keyed nodes have `path = name`).
- [ ] Implement it.
- [ ] `uv run pytest -q tests/graph`, then `uv run pytest -q`. Commit "Scope provider deletes and prunes by path, not name".

### Task 4: Dispatch: expansion, takeover on delete and key-switch apply

**Files:** `devgraph/indexer/dispatch.py`:
- **`index_paths`.** For each touched field-keyed type, work out the affected keys K. Read the type's files, then extend the docs batch's `Selected` with K's claimants (helper in `docs.py`, reusing front matter already read). Extend `previous_nodes` with `existing_node_names` for K.
- **`_sync_docs` order.** Upsert nodes, then delete edges at paths, then `prune_extracted_at`, then upsert edges.
- **`remove_paths`.** Run `extracted_nodes_at(gone)`. For field-keyed labels, upsert the winners among the claimants on disk and rebuild their edges, before `delete_extracted_nodes`. Wrap this in `try/except` and fall through to the delete.
- **`_apply_project_schema`.** Upsert the docs nodes of labels whose recorded key differs from the declared key after `realign_keys`, in `try/except` with a warning naming doctor's Schema constraints section.

Tests: `tests/indexer/test_docs_provider_live.py`, `tests/indexer/test_schema_apply_live.py`.

- [ ] Write failing live tests on a temporary repository with `decisions/` (Adr keyed on `adr_id` from `id`, and `REPLACES` from `supersedes`) and a path-keyed `Runbook` type:
  - **Linking.**
    - A full scan links `adr-013.md` (`supersedes: ADR-012`) to the ADR-012 entry.
    - A list value `[ADR-010, ADR-011]` makes two edges.
  - **Duplicates, in both orders.**
    - Save `copy-of-adr-012.md` (`id: ADR-012`) after the original: the original keeps the node and its incoming edge.
    - Then create `adr-000-copy.md` (`id: ADR-012`), which sorts first: it takes over the same node in place. The incoming edge survives, and its outgoing edges are the new owner's.
    - Delete the copy: the original takes the id back in place.
  - **Rename keeping the id.** Delivered as `remove_paths` then `index_paths`, and in the reverse order. Either way, `path` is updated and the incoming `REPLACES` edge survives, with no relink re-read on the create (assert through a spy on `_relink_docs` targets).
  - **Re-id.**
    - Changing `id: ADR-012` to `id: ADR-099` prunes ADR-012, or hands it to a waiting claimant.
    - A file naming `ADR-099` that was indexed earlier gets its edge by relink.
  - **Missing or invalid id.** The file is left out and the rest of the batch is written.
  - **Delete.** Deleting the owner with no other claimant removes the node and its edges. Deleting the `decisions/` folder removes every entry.
  - **Relink.** A runbook `adr: ADR-020` is indexed first, then `adr-020.md` is added in a later batch, and the edge appears.
  - **Key switch.** Two files share `id: ADR-1` under `[path]` keying:
    - switching to `[adr_id]` gives one entry, the first path's;
    - switching back to `[path]` gives two entries under the replaced `(repo_id, path)` constraint;
    - in both directions, links are rebuilt by the full scan's edge pass.
  - **Disagreement.** A second repository records `Adr` keyed `[adr_id]` while this one switches to `[path]` with duplicate ids. Apply returns True, logs the warning, and does not raise.
  - **Unchanged.**
    - The slice 1 live suite passes unchanged, including the shared-path filesystem/docs tests.
    - A repository with only path-keyed docs types reads only its batch files on a save (spy on `read_selected` inputs).
- [ ] Implement addendum §4.
- [ ] `uv run pytest -q tests/indexer`, then `uv run pytest -q`. Commit "Keep field-keyed docs entries owned by the first file on disk".

### Task 5: Config form, README and live verification

**Files:**
- `devgraph/dashboard/static/index.html`, Config page form, docs mode:
  - the source hint accepts `[path]` or one ticked string field other than `path`;
  - the Key help text;
  - the missing-`path` hint (addendum §6).

  Filesystem hints are unchanged.
- `README.md`, docs-source section:
  - the Adr example and its front matter, with `supersedes: ADR-012`;
  - duplicates (first path wins, doctor names both files);
  - a missing or invalid id;
  - rename, re-id and delete;
  - the key-switch warning;
  - the new O(type's files) cost under "What it doesn't do yet";
  - point 3 ("The key must be exactly `[path]`") is rewritten;
  - the "comes next" bullet is removed.
- `PROJECT_STATUS.md`: front-matter keys done.
- `devgraph/config/project_schema.py` starter template: the header comment mentions field keys. The example stays the Runbook.

Tests:
- `tests/dashboard/config_page_ui.js`, `tests/dashboard/test_config_form_roundtrip.py`;
- `tests/cli/test_cli.py` (doctor lines through the real CLI).

- [ ] Write failing tests:
  - **Form validity.** A docs Adr with Key ticked on `adr_id` shows no source hint. Two ticked keys, a ticked integer field, or no `path` row each show their hint.
  - **Form round trip.** The Adr entry round-trips by identity, and a filesystem type keyed on a non-path field still shows its old hint.
  - **Doctor.** Doctor prints the duplicate and missing-id lines, and the unmatched-by-path hint when Neo4j is reachable.
  - **Starter template.** The uncommented starter example still validates.
- [ ] Implement them.
- [ ] Live, against a throwaway registry and repository:
  - add the Adr type through the Config page form (tick Key on `adr_id`, Front-matter key `id`) and the `REPLACES` relationship;
  - watch the rescan apply it;
  - check the `REPLACES` edge in the graph view;
  - save a copy with the same id, and check that doctor names both files and the graph keeps the original;
  - rename the original, and check that the edge survives;
  - take a browser screenshot if a browser is available.
- [ ] Update the docs. Run the full `uv run pytest -q`. Commit "Document docs front-matter keys".
