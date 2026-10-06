# Docs Front-Matter Keys Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A docs-sourced node type can be keyed on one front-matter field (`key: [adr_id]` with `fields: {adr_id: id}`). Docs links can then name the target by id (`supersedes: ADR-012`). One owner map, computed from disk, decides which file owns each id. Duplicates, renames, re-ids, deletes and key switches end in the same graph that a fresh full apply of the same files produces.

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

  Key values also refuse:
  - empty text, and leading or trailing whitespace;
  - control characters (C0, DEL, C1), format characters (Cf) and lone surrogates;
  - a leading `./` (addendum §3).
- **Keys are text.** Compare them exactly and case-sensitively. Never trim them, case-fold them or normalise their Unicode.
- **Ownership comes only from `keyed_owners`** (K6). It is computed over every file of every field-keyed type, from disk. Never compute a winner from a partial file set, from `_evaluate`'s iteration order, or from the graph. Every node and edge pass for a field-keyed type takes `owners`: batch, relink, delete takeover, apply and doctor.
- **Invariant.** After any sequence of events, the docs part of the graph equals the result of a fresh full apply of the same files. That covers every docs node's label, name and path, and every docs edge. The shared helper `assert_matches_fresh_apply` (Task 4) checks this.
- **One walk and one read per batch.** A batch that touches a field-keyed type builds one `KeyedView`, and relink and the delete takeover reuse it. No file is read twice in a batch.
- **Bounded writes.** Engine `$paths` lists hold at most |batch| + |K| paths. The batch path writes no loser problems.
- **Path-keyed and filesystem behaviour stays identical.** Switching the path-scoped engine calls to `n.path` must not change any slice 1 test. A repository without field-keyed types writes the same graph, and does the same reads, as before.
- **The provider still owns only its own data and fails closed** (slice 1 §3.6–3.7). Each new docs step sits inside a `try/except` pass.
- **YAML stays the source of truth on the Config page.** The form only writes `#configYaml`, and its hints are advisory.
- **TDD.** Each task starts with failing tests, then the implementation, then `uv run pytest -q` (the full suite, with node available).
- **Commits.** Plain imperative messages, with no `Co-Authored-By` trailer and no AI attribution. Never stage `uv.lock`, real names or personal paths.

## Review Focus

1. **Owner gating (K6).** Grep for every `_evaluate`, `build_nodes` and `build_edges` call: each one passes `owners` built from a full `KeyedView` or full `Selected`. Check these cases:
   - a file selected by both `Adr` and `Rfc` never gets a node under a key it does not own in either type;
   - a relink with a loser outside the batch and the owner inside it writes no edge from the loser.
2. **Claimant order (K3).** `adr-012.md` beats `adr-012 copy.md`, `adr-012 - Copy.md`, `adr-012 (1).md` and `adr-012-v2.md`. The tie-break is the full path.
3. **Invariant.** `assert_matches_fresh_apply` runs after each of these sequences:
   - the C1 cases;
   - copies;
   - duplicates in both save orders;
   - a rename, in both event orders;
   - a re-id with a waiting claimant;
   - a delete with a successor.
4. **Edge preservation on move.** An id that survives is MERGEd onto its new `path`, and its incoming edges are kept without a relink. The previous owner's outgoing edges are deleted after the move (the `_sync_docs` reorder).
5. **Bounds.** Spy tests assert that:
   - every engine `$paths` list is at most |batch| + |K|;
   - each file is read at most once per batch;
   - path-keyed-only repositories read only batch files.
6. **Deletion scope.** Every path-scoped engine call matches `n.path` and filters on `extractor`. The slice 1 shared-path tests (filesystem `File` plus docs `Runbook`) still pass unchanged.
7. **Key switch (K7).** Deferral is decided from `generated_objects`. Each deferred label is upserted in its own transaction. Disagreement and first-apply-under-a-foreign-constraint end with:
   - the asserted graph;
   - a warning naming the other repository and saying the entries were not written;
   - a doctor `conflict` line.

### Task 1: Schema: field keys for docs sources

**Files:**
- `devgraph/config/project_schema.py`: in `_check_key_and_metadata`, a docs source accepts `[path]` or one declared `string` field other than `path`, and requires a declared string `path` field. These are the three messages of addendum §2, and the integer case says "use string; numbers like 12 still work". Filesystem sources are unchanged.
- `devgraph/config/edits.py`: `schema_change_warnings` adds the docs key-change line (addendum §6).

Tests: `tests/config/test_project_schema.py`, `tests/config/test_edits.py`.

- [ ] Write failing tests:
  - **Valid.** The addendum's Adr example validates. So do `fields: {adr_id: id}` and the same-name default.
  - **Refused, each with its message:**
    - a composite key `[adr_id, title]`;
    - an `integer` key field (the message includes "numbers like 12 still work");
    - an undeclared key field;
    - no `path` field;
    - a non-string `path`.
  - **Unchanged.** Every existing `[path]` docs and filesystem fixture still validates. A filesystem type keyed `[name]` keeps its old message.
  - **Provisioning.** `constraint_statements` emits `(repo_id, adr_id)` uniqueness and the `_repo_name` index for a field-keyed docs type.
  - **Warnings.** `schema_change_warnings` gives the docs key-change line for path→field and field→path. It does not give it for filesystem types or an unchanged key. The existing constraint warning stays.
- [ ] Implement addendum §2 and the warnings part of §6.
- [ ] `uv run pytest -q tests/config`, then `uv run pytest -q`. Commit "Allow docs node types keyed on a front-matter field".

### Task 2: Pure provider: key text, claimant order and the owner map

**Files:** `devgraph/indexer/providers/docs.py`:
- `DocsType.key: DocsField | None`;
- `_key_text`;
- `claimant_order(rel)`;
- `keyed_claims(spec, selected)` and `keyed_owners(claims)`;
- `owners` threaded through `_evaluate`, `build_nodes` and `build_edges` (required whenever the spec has a field-keyed type);
- an unwritten `from_path` on every edge dict;
- `source_report`: loser lines, which use claims, plus a duplicate count;
- `unmatched_report`: uses `from_path` and adds the field-key hint;
- a `KeyedView` dataclass (files, `Selected`, claims, owners) and `expand_to_owners(view, batch_selected, keys)`, which returns the batch's `Selected` plus the owners of `keys`.

Tests: `tests/indexer/test_docs_provider.py`.

- [ ] Write failing tests, with no Neo4j:
  - **Node shape.** An owner's node has `name = "ADR-012"`, `path`, `extractor` and `adr_id = "ADR-012"`. Path-keyed nodes are unchanged.
  - **Key text.**
    - `id: 12` gives `"12"`, and `supersedes: 12` makes an edge to it.
    - Each of these is refused with its exact reason: bool, float, list, map, date, null, absent, empty, `" ADR-1"`, `"ADR-1 "`, int64 overflow, a 4 KiB+ string, U+202E, `\x07`, `\x85`, a lone surrogate, `./ADR-1`.
    - `required: false` on the key field still leaves the file out.
    - NFC and NFD spellings are distinct keys.
  - **Claimant order.** `decisions/adr-012.md` comes first among:
    - `adr-012 copy.md` (the Finder style);
    - `adr-012 - Copy.md`;
    - `adr-012 (1).md`;
    - `adr-012-v2.md`.

    `x.md` and `x.markdown` tie-break on the full path.
  - **Owners.** Duplicates give one node and its edges, for the owner only. The result is the same for any order of `files`.
  - **Two types (C1).** A file selected by both `Adr` and `Rfc`, which owns `ADR-1` in `Rfc` but loses it in `Adr`, yields only the `Rfc` node and edges. A file with no claim in a type yields nothing for it.
  - **Conditions.** A file failing `where`, or skipped for a required field, never claims, so the next claimant owns the key.
  - **`expand_to_owners`.** It adds only owners, and its output size is at most |batch| + |keys|.
  - **`source_report`.** It prints the M3 duplicate and missing-id lines and the "1 duplicate id" summary, under the five-file cap.
  - **`unmatched_report`.** It names `from_path`, not the id, and adds the hint only for field-keyed targets.
  - **Path-keyed spec.** `owners` may be omitted, and the output is byte-identical to before.
- [ ] Implement addendum §3 and the doctor part of §6 (not the `conflict` line).
- [ ] `uv run pytest -q tests/indexer/test_docs_provider.py`, then `uv run pytest -q`. Commit "Gate docs entries on a front-matter key owner map".

### Task 3: Engine and constraint drift: path ownership and the conflict status

**Files:**
- `devgraph/graph/engine.py`:
  - `_DELETE_EXTRACTED_PATHS_CYPHER`, `_PRUNE_EXTRACTED_AT_CYPHER`, `_DELETE_EXTRACTED_EDGES_CYPHER` and the provider branch of `list_file_nodes` match `n.path`;
  - new `extracted_nodes_at`;
  - the comment says that `path` owns the entry and `name` is the key.
- `devgraph/indexer/schema_constraints.py`: `constraint_drift` gains `conflict` (addendum §4, key switch).
- `devgraph/cli/main.py`: the Schema constraints section prints the `conflict` line.

Tests: `tests/graph/test_engine_extracted_nodes.py`, `tests/indexer/test_schema_constraints_live.py`, `tests/cli/test_cli.py` (all live).

- [ ] Write failing tests. Seed a docs node with `name = "ADR-012"` and `path = "decisions/a.md"`:
  - **Path-scoped calls.**
    - `delete_extracted_nodes(["decisions"])` deletes it.
    - `prune_extracted_at(["decisions/a.md"], keep=[])` prunes it, and `keep=["Adr:ADR-012"]` keeps it.
    - `delete_extracted_edges(["decisions/a.md"])` removes its outgoing non-built-in edges.
    - `list_file_nodes(["decisions/a.md"])` returns `("Adr", "ADR-012")`.
  - **`extracted_nodes_at`.** It finds the node at and below `decisions`. It does not find it for `decisions2/`, another extractor or another repo.
  - **Existing tests.** The existing engine tests pass unchanged.
  - **`constraint_drift`.**
    - It reports `conflict` when repo A records `Adr:adr_id`, repo B records `Adr:path`, and the constraint is `(repo_id, path)`.
    - It still reports `blocked` when duplicates exist, and nothing when the definitions agree.
    - Doctor prints the `conflict` line.
- [ ] Implement them.
- [ ] `uv run pytest -q tests/graph tests/indexer/test_schema_constraints_live.py`, then `uv run pytest -q`. Commit "Scope provider deletes by path and report key conflicts".

### Task 4: Dispatch: KeyedView, owner-gated sync, relink, takeover and key switch

**Files:** `devgraph/indexer/dispatch.py`:
- **`KeyedView`.** Built once per batch that touches a field-keyed type, from one `indexable_paths` walk and one read. The batch's own front matter is reused.
- **`index_paths`.**
  - Compute the affected keys K.
  - Expand to owners only (`expand_to_owners`).
  - Extend `previous_nodes` with `existing_node_names` for K.
  - Run `_sync_docs(…, owners)` in the order: upsert, edge delete, prune, edges.
- **`_relink_docs`.** Reuses the view's walk and front matter, and passes `owners`. Path-keyed files the view has not read are read once.
- **`remove_paths`.** Run `extracted_nodes_at(gone)`, then upsert each affected key's owner from the view and rebuild its edges, before `delete_extracted_nodes`. Wrap this in `try/except` and fall through to the delete.
- **`_apply_project_schema`.**
  - Pass full-repository `owners`.
  - Defer the labels whose generated constraint differs from the declared key (`generated_objects`).
  - Upsert each deferred label in its own transaction after `realign_keys`. On failure, warn and name the other repositories from `recorded_declarations`, with "… entries were not written".

Tests:
- `tests/indexer/test_docs_provider_live.py`;
- `tests/indexer/test_schema_apply_live.py`;
- a shared `assert_matches_fresh_apply(engine, repo_id, repo_root)` in the live tests' helpers. It snapshots the docs nodes (label, name, path) and docs edges, runs a fresh full apply on a second repo_id over the same files, and compares the two.

- [ ] Write failing live tests on a temporary repository with `decisions/` (Adr keyed `adr_id` from `id`, `REPLACES` from `supersedes`), an `Rfc` type keyed on `id` over `**/*.md` with `where: {field: kind, is: rfc}`, and a path-keyed `Runbook`. Run `assert_matches_fresh_apply` after each sequence:
  - **Linking.** A full scan links `adr-013.md` (`supersedes: ADR-012`), and a list value makes two edges.
  - **C1, two types.** `decisions/x.md` has `kind: rfc` and `id: ADR-1`, and loses `ADR-1` in `Adr` to `decisions/adr-1.md` while owning it in `Rfc`. After saving `x.md` alone, it has an `Rfc` node and no `Adr` node or edges.
  - **C1, relink.** A loser `decisions/adr-012 copy.md` sits outside the batch. The owner `decisions/adr-012.md` is created in the batch, and a runbook naming `ADR-012` is indexed earlier. The relink makes edges only from the owner, and none from the loser.
  - **I1.** Save `decisions/adr-012 copy.md` (`id: ADR-012`) after, and then before, the original. Either way the original owns the node, and doctor names both files.
  - **Takeover.**
    - Creating `decisions/adr-000.md` (`id: ADR-012`), which sorts first, takes over the node in place. The incoming edge survives, and the outgoing edges are the new owner's.
    - Deleting it hands the node back.
  - **Rename keeping the id.** Delivered as `remove_paths` then `index_paths`, and in the reverse order. Either way, `path` is updated and the incoming edge survives, with no relink of that id.
  - **Re-id.** `ADR-012` becomes `ADR-099` with a waiting claimant of `ADR-012`. The claimant takes over, and an earlier file naming `ADR-099` relinks.
  - **Missing or invalid id.** The file is left out and the rest of the batch is written.
  - **Delete.** Deleting the owner with no claimant removes the node and its edges. Deleting the folder removes every entry.
  - **Bounds (I2, M6).** Spy on the engine calls and on reads:
    - with 50 duplicate claimants of one id, every `$paths` list stays at most |batch| + |K|;
    - each file is read at most once per batch;
    - a repository with only path-keyed docs types reads only batch files.
  - **Key switch.** Two files share `id: ADR-1` under `[path]`:
    - switching to `[adr_id]` gives one entry (the owner's);
    - switching back gives two entries under the replaced constraint;
    - links are rebuilt in both directions.
  - **Disagreement (I3).** Repo B records `Adr` keyed `[adr_id]` while this repo switches to `[path]` with duplicate ids. Apply returns True, and the `Adr` label holds none of this repo's entries. The warning names repo B and says the entries were not written, and doctor prints `conflict`. Other labels are written: each deferred label has its own transaction.
  - **First apply under a foreign constraint (I3).** Repo B created `Adr` keyed `[path]`. This repo's first apply declares `[adr_id]` with unique ids. The deferral is detected from `generated_objects`, the graph and doctor output are asserted, and no crash occurs.
  - **Slice 1.** The live suite passes unchanged, including the shared-path filesystem/docs tests.
- [ ] Implement addendum §4.
- [ ] `uv run pytest -q tests/indexer`, then `uv run pytest -q`. Commit "Keep field-keyed docs entries owned by their file on disk".

### Task 5: Config form, README and live verification

**Files:**
- `devgraph/dashboard/static/index.html`, Config page form, docs mode (addendum §6):
  - the source hint accepts `[path]` or one ticked string field other than `path`;
  - the Key help;
  - a ticked key row shows Required as ticked and disabled, without changing the YAML;
  - the integer-key hint;
  - the missing-`path` hint.

  Filesystem hints are unchanged.
- `README.md`, docs-source section (addendum §6):
  - the Adr example, with `supersedes: ADR-012`;
  - quoting leading-zero ids;
  - duplicates and the stem-aware rule;
  - missing ids and ids with whitespace;
  - rename, re-id and delete;
  - the key-switch warning and the `conflict` doctor line;
  - mentions;
  - the per-save walk and read cost;
  - point 3 ("The key must be exactly `[path]`") and point 4 (a docs node's name) rewritten, and the "comes next" bullet removed.
- `PROJECT_STATUS.md`: front-matter keys done, with the optional read cache open.
- `devgraph/config/project_schema.py` starter template: the header mentions field keys, and the example stays the Runbook.

Tests:
- `tests/dashboard/config_page_ui.js`;
- `tests/dashboard/test_config_form_roundtrip.py`;
- `tests/cli/test_cli.py` (doctor's duplicate, missing-id and unmatched-by-path lines, through the real CLI).

- [ ] Write failing tests:
  - **Form validity.** A docs Adr with Key on `adr_id` shows no source hint, and Required shows ticked and disabled on the key row. Two ticked keys, a ticked integer field, or no `path` row each show their hint.
  - **Round trip.** The Adr entry round-trips by identity, including `required` being absent from the key row.
  - **Filesystem.** A filesystem type keeps its old hint.
  - **Doctor.** It prints the M3 lines, and the `from_path` unmatched line when Neo4j is reachable.
  - **Starter.** The uncommented starter example still validates.
- [ ] Implement them.
- [ ] Live, against a throwaway registry and repository:
  - add the Adr type through the form (Key on `adr_id`, Front-matter key `id`) and `REPLACES`;
  - watch the rescan apply it;
  - check the edge in the graph view;
  - save `adr-012 copy.md`, and check that doctor names both files and the original keeps the node;
  - rename the original, and check that the edge survives;
  - take a browser screenshot if a browser is available.
- [ ] Update the docs. Run the full `uv run pytest -q`. Commit "Document docs front-matter keys".
