# Declarative Providers: Docs Front Matter (Slice 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A `devgraph.schema.yaml` node type can declare `source: {provider: docs, paths, where, fields}`, and a relationship can declare `provider: docs` with a `field`. Matching Markdown files then become nodes, with properties mapped from front matter and edges found by key lookup. They are kept current by the watcher and rescans, reported by `devgraph doctor`, and editable in the Config page form. No repository code runs, and no user-supplied regex runs.

**Spec:** `docs/superpowers/specs/2026-10-06-declarative-providers-design.md`. Every task implements the sections it names. The filesystem provider spec (`2026-10-04-filesystem-provider-design.md`) and the schema rescan spec (`2026-10-04-schema-rescan-design.md`) still govern everything this slice reuses.

**Working directory:** this worktree, branch `epic1/declarative-providers` (from `epic1/combined`). Live tests use Neo4j at `bolt://127.0.0.1:7687` (`neo4j`/`devgraph-local-dev`), unique repo_ids and cleanup in teardown. Never touch `~/.devgraph`. Dashboard test clients use `base_url="http://127.0.0.1"` (Host guard).

## Global Constraints

- **Nothing executes.** No `re` on user-supplied patterns, and no `eval`, `exec`, subprocess, import, template engine or format-string expansion of user data. The only interpreters are `bounded_safe_load` and `fnmatch.fnmatchcase` (globs are matched segment by segment; `PurePosixPath.full_match` backtracks catastrophically and is not used). The schema is untrusted: it ships inside the repository and is not trust-gated.
- **Everything is bounded** (spec §3.2):
  - every file read goes through `read_bounded`;
  - every front-matter parse goes through `bounded_safe_load(..., YAML_MAX_NODES)` with `YAML_LOAD_ERRORS` caught;
  - all counts and lengths are capped;
  - `type(v) is int` checks, with int64 bounds.
- **Candidate files are `walk.indexable_paths`.** These are today's rules, moved out of `dispatch.py` and re-exported from it unchanged. No `.gitignore` logic.
- **Only declared names are written.** Labels, relationship types and property names come only from the validated `EffectiveSchema`, or are re-validated against their pattern when read back from the graph. Front-matter keys and values are only ever Cypher parameters.
- **The provider owns only its own data.** It deletes only nodes with `extractor = "docs"` and only outgoing non-built-in edges from them. Built-in and filesystem behaviour stays byte-identical. A repository without docs sources produces the same graph as before.
- **The provider fails closed** (spec §3.7). An invalid or pending schema, or an apply that returns False, skips every docs write and delete. Each docs pass is wrapped in `try/except` with a warning.
- **No regressions to existing rules.** No new MCP tool, no new route, no new static file. The `custom` provider stays inert. Existing schema files stay valid.
- **YAML stays the source of truth on the Config page.** The G2b form rules (`2026-10-05-form-editor-design.md`, `2026-10-05-relationship-form-design.md`) carry over unchanged: the form writes only `#configYaml`, refuses into YAML with a reason, and gives advisory hints only.
- **TDD.** Each task starts with failing tests, then the implementation, then `uv run pytest -q` (the full suite, with node available for the dashboard harnesses).
- **Commits.** Plain imperative messages, with no `Co-Authored-By` trailer and no AI attribution. Never commit `uv.lock`, real names or personal paths.

## Review Focus

Reviewers should check these first. They are where this slice could hang, write something undeclared, or delete something it does not own.
1. **No pattern engine on user input** (spec §3.1). Grep the diff for:
   - `re.` applied to schema values;
   - `eval`, `exec`, `subprocess`, `importlib`;
   - `format(` and f-strings carrying user values.
2. **Interpolation.** Every label, relationship type and property name that reaches Cypher comes from the validated schema, or is re-validated when read back for the property clear. Everything else is a parameter.
3. **Deletion scope.** The three new engine calls filter on `extractor = $extractor`. The shared-path test proves a filesystem `File` survives every docs operation, and the reverse.
4. **Relink correctness.** Relink fires only for added nodes (`previous_nodes` vs the extended `_batch_nodes`). The later-batch compose test and the delete-then-recreate test pass. `full_scan`'s final edge pass is skipped when apply returns False.
5. **Bounds and types.** Check the following: FIFO, oversized file, alias bomb, 101-item list, 4 KiB+ string, int64 overflow, bool-as-int, and one bad file in a batch with good ones.
6. **Provider-aware callers.** `pruned_types`, `schema_entry_notes`, `config schema list` and the drift guard all handle docs sources and filesystem↔docs transitions without `AttributeError` on `.kind`.
7. **Form fidelity.** An integer or boolean condition value round-trips. A `fields` order that differs from the metadata order refuses into YAML. A metadata rename carries its `fields` entry.

### Task 1: Schema: docs source, docs relationships and provider-aware callers

**Files:**
- `devgraph/config/project_schema.py`:
  - `FilesystemSource | DocsSource`, discriminated on `provider`, with `Condition`;
  - `NODE_SOURCE_PROVIDERS` and `PROVIDER_KINDS` gain `docs`;
  - `RelationshipDecl.field` and its per-relationship checks in `_check_provider`;
  - a new `ProjectSchema._check_docs` (docs `from` labels must be docs-sourced);
  - `_check_filesystem` restricted to filesystem sources;
  - a provider-aware `_check_key_and_metadata` message;
  - the `starter_schema_text` header and example.
- `devgraph/config/edits.py`: `pruned_types`/`schema_change_warnings` and `schema_entry_notes`, made provider-aware.
- `devgraph/cli/main.py`: the `config schema list` Source column and `--json`.
- `tests/dashboard/test_config_form_drift.py`: import `FilesystemSource`; split the `NodeSource` parametrisation. The docs part is completed in Task 5.

Tests:
- `tests/config/test_project_schema.py`;
- `tests/config/test_edits.py`;
- `tests/cli/test_config_schema_cli.py`.

- [ ] Write failing tests (spec §2 and the "callers" bullet of §5):
  - The spec's Runbook example validates.
  - Each rule fails with a plain-words message:
    - `paths` empty, more than 20, absolute, containing `..`, a backslash, or longer than 200;
    - a condition with zero or two operators, or text longer than 200;
    - `where` longer than 20, or `fields` longer than 50;
    - `fields` naming an undeclared field or `path`;
    - a front-matter key with a control character;
    - a key other than `[path]` (provider-named message);
    - a docs relationship without `field`, with `custom`, or with a built-in type;
    - a non-docs `from` label (from `_check_docs`);
    - `field` on another provider.
  - Integer and boolean condition values are canonicalised to text. A `regex` or `matches` key is refused as unknown.
  - Several docs node types are allowed. Existing filesystem files still validate, and two filesystem types of the same kind are still refused.
  - `resolve_declaration` provisions the `_repo_name` index for docs types.
  - The uncommented starter example validates.
  - `pruned_types` warns on each transition: filesystem→docs, docs→filesystem, docs→none, a docs `paths`/`where` change, and a filesystem kind change. It raises no `AttributeError`.
  - `schema_entry_notes` names both providers.
  - `config schema list` (table and `--json`) shows a docs source.
- [ ] Implement spec §2 and the callers bullet of §5 (no indexing).
- [ ] `uv run pytest -q tests/config tests/cli tests/dashboard/test_config_form_drift.py`, then `uv run pytest -q`. Commit "Declare docs front-matter sources in the project schema".

### Task 2: Shared walk module and the pure docs provider

**Files:**
- `devgraph/indexer/walk.py` (new). It holds `IGNORED_DIR_NAMES`, `is_ignored_dir_name`, `is_ignored_path`, `indexable_paths`, `links_outside` and `is_indexable_file`, moved from `dispatch.py`. `dispatch.py` re-exports them under their current names, so the watcher and existing tests keep working.
- `devgraph/indexer/providers/docs.py` (new). It provides:
  - `docs_spec(effective)`, which returns `None` when the schema has no docs sources;
  - `read_front_matter(path)`, using `read_bounded` and `bounded_safe_load` and importing `_FRONTMATTER_RE` from `docs/extractor.py`;
  - `build_nodes(spec, repo_id, files)`, which returns nodes plus per-file skip reasons;
  - `build_edges(spec, repo_id, files, targets=None)`;
  - `source_report(repo_root, effective, files)`.

Tests: `tests/indexer/test_walk.py` (the moved helpers, unchanged behaviour), `tests/indexer/test_docs_provider.py` (new).

- [ ] Write failing tests, with no Neo4j:
  - **Globs:** case-sensitivity, `**`, `.markdown`, and non-Markdown exclusion.
  - **Each operator** against a string, an integer, a boolean and a list item:
    - `is 1` matches `version: 1`;
    - `is true` matches `draft: yes`;
    - `like` with `*`;
    - floats, dates and strings over 4 KiB never match;
    - no front matter, with and without `where`.
  - **Fields:** `fields` renaming and the same-name default.
  - **Coercion:** every row of the spec table, including:
    - `True` not accepted as an integer;
    - int64 overflow left unset;
    - a 4 KiB+ string left unset;
    - YAML 1.1 `yes` written as `"true"` in a string field.
  - **Every declared field is emitted**, with `None` when absent or not coercible.
  - **Required fields:** a missing required field skips the node with the reason.
  - **Node shape:** exactly `name`, `path`, `extractor` and the declared fields.
  - **Edge values:**
    - `str` and `int` count, `bool` does not;
    - a leading `./` is removed;
    - lists are capped at 100;
    - `targets` filtering works.
  - **Hostile files:** a malformed, FIFO, oversized or alias-bomb file is skipped with its reason.
  - **`source_report`:** the wording from spec §5, at most 5 named files plus "and N more", and "entries" rather than "nodes". It does not import `devgraph.indexer.dispatch`; check with an import-graph test.
- [ ] Implement spec §2 (values and lookup), §3.1–3.4 and the report half of §5.
- [ ] `uv run pytest -q tests/indexer/test_walk.py tests/indexer/test_docs_provider.py`, then `uv run pytest -q`. Commit "Add the docs front-matter provider".

### Task 3: Engine: path-scoped prune, edge delete and property clear

**Files:** `devgraph/graph/engine.py`, as constants next to `_PRUNE_EXTRACTED_CYPHER`:
- `prune_extracted_at(repo_id, extractor, paths, keep)`;
- `delete_extracted_edges(repo_id, extractor, paths | None)`, which deletes outgoing relationships whose type is not in `RELATIONSHIP_TYPES` (passed as a parameter);
- `clear_extracted_properties(repo_id, extractor, label, keep)`. It reads distinct keys and removes those not in `keep`, not reserved and not `insight_*`. Each removed name is re-validated against `PROPERTY_NAME_PATTERN` before it is interpolated into `REMOVE`.

Tests: `tests/graph/test_engine_extracted_nodes.py` (live).

- [ ] Write failing live tests:
  - **`prune_extracted_at`** deletes only this extractor's nodes at those exact paths that are missing from `keep`. It leaves alone:
    - other paths and nothing below a path;
    - other extractors (a filesystem node at the same path);
    - other repos and built-ins.
  - **`delete_extracted_edges`** removes only outgoing non-built-in edges from this extractor's nodes, at the paths or repo-wide when `paths` is `None`. Incoming edges and built-in types stay.
  - **`clear_extracted_properties`** removes an undeclared property. It keeps declared, reserved and `insight_*` properties. It never interpolates a key that fails the pattern; seed one through a parameter to check.
  - Empty inputs make no call.
- [ ] Implement them.
- [ ] `uv run pytest -q tests/graph`, then `uv run pytest -q`. Commit "Add path-scoped extractor prune, edge delete and property clear".

### Task 4: Dispatch wiring: apply, watch, delete and relink

**Files:** `devgraph/indexer/dispatch.py`:
- `_provider_specs(repo_root)`, which replaces `_filesystem_spec` and resolves once per batch;
- the docs steps in `apply_project_schema` (spec §4: edges, properties, prune, upsert);
- the docs sync in `index_paths` after the filesystem sync;
- `_batch_nodes`/`previous_nodes` extended to compose and Containerfile Services and docs nodes;
- the relink on `added_nodes`;
- the docs delete in `remove_paths`;
- `full_scan`'s final `docs.sync_edges`, gated on apply returning True;
- `try/except` around each docs pass.

Tests: `tests/indexer/test_docs_provider_live.py` (new, live), `tests/indexer/test_schema_apply_live.py`.

- [ ] Write failing live tests on a temporary repository with `runbooks/` and a compose file declaring Service `api`:
  - **Full scan** creates the Runbook nodes and RUNBOOK_FOR edges. With two compose files both declaring `api`, there is one edge per Service (fan-out, D7).
  - **Edits:**
    - editing `owner` updates the property;
    - removing `owner` from the file removes it;
    - changing `service:` moves the edge;
    - breaking a `where` condition removes the node;
    - an incoming Runbook→Runbook edge survives an edit of its target.
  - **Relink:**
    - a runbook naming `api` is indexed first, the compose file is added in a later batch, and the edge appears;
    - the compose file is deleted and then recreated, and the edge comes back;
    - re-indexing an unchanged compose file triggers no relink, because nothing was added;
    - a docs-to-docs link: a runbook names an ADR path whose file is added in a later batch, and the edge appears.
  - **Shared path, reverse:** with a docs `Runbook` and a filesystem `File` over the same file, deleting the file and then a filesystem `sync_absent` leave no orphaned `Runbook` edges and remove only the matching nodes.
  - **Bad values:** one file with an int64-overflow value and one with a 200-item list, in a batch with good files. The good nodes are written and a warning is logged.
  - **Deletes:** deleting a file, and deleting the directory, removes the nodes.
  - **Schema changes:**
    - a new `fields` mapping is applied by the rescan;
    - a removed metadata declaration clears that property after the rescan;
    - a removed type, a removed relationship type, and an edge type no longer declared are all gone after the rescan;
    - an invalid schema leaves docs nodes untouched, and `full_scan` writes no edges.
  - **Shared path** (spec §3.6): a filesystem `File` and a docs `Runbook` both cover `runbooks/a.md`.
    - Docs un-match, file delete, Runbook type removal and an invalid schema each leave `File` as expected: it is removed only by the delete, which is the filesystem provider's own doing.
    - The reverse: a `File` type removal leaves Runbook alone.
  - **Scope:** ignored directories and an outside symlink are never read.
  - **No docs sources:** the graph snapshot equals the one without this change.
- [ ] Implement spec §4 and §3.7.
- [ ] `uv run pytest -q tests/indexer`, then `uv run pytest -q`. Commit "Index docs front-matter sources on scan, watch and rescan".

### Task 5: Doctor and Config page form

**Files:**
- `devgraph/cli/main.py`: the Project schemas section calls `docs.source_report` with `walk.indexable_paths`. When Neo4j is reachable, it adds the unmatched-target lines; otherwise "skipped: Neo4j is not reachable".
- `devgraph/dashboard/static/index.html`, Config page block:
  - `CONFIG_FORM_FIELDS`: the source providers, relationship providers, and docs source and condition fields;
  - the source select gains "Markdown front matter", which shows:
    - Paths rows, with placeholder `runbooks/**/*.md` and the `**/*.md` and case-sensitivity hint;
    - Conditions rows, with a plain-word Test select;
    - a Front-matter key input per metadata row, with the spec §5 help text;
  - the relationship provider gains "Markdown front matter", which shows a Field input;
  - a provider-specific `configFormFromEntry` presence check;
  - a metadata rename carries its `fields` entry;
  - new `CONFIG_FORM_REASONS` for unknown source keys and for `fields` order.

Tests:
- `tests/cli/test_cli.py` (doctor);
- `tests/dashboard/config_page_ui.js`, `config_form_dump.js`, `test_config_form_roundtrip.py`;
- `tests/dashboard/test_config_form_drift.py` (`DocsSource`, `Condition`, and the provider lists equal `NODE_SOURCE_PROVIDERS`/`PROVIDER_KINDS`).

- [ ] Write failing tests:
  - **Doctor:**
    - prints the OK, no-match and named-file lines of spec §5, with at most 5 files and "and N more";
    - prints the unmatched-target lines when Neo4j is reachable, and the skip line when it is not;
    - prints nothing for repositories without docs sources.
  - **Form round trip:**
    - the docs source and relationship are representable and round-trip by identity, including `where` order;
    - an integer or boolean condition value comes back unchanged when untouched;
    - an entry without `where`/`fields` is representable.
  - **Refusals, with reasons:** unknown source keys, and `fields` in a different order from the metadata.
  - **Form behaviour:**
    - renaming a metadata row moves its `fields` key;
    - provider switching shows and hides the docs controls, keeps focus, and never writes hidden keys;
    - every control is labelled, and hostile values land only in `.value`/`textContent`.
- [ ] Implement spec §5 (doctor and form).
- [ ] `uv run pytest -q` (node must run). Commit "Show docs sources in doctor and the Config form".

### Task 6: Docs, glossary and live verification

**Files:**
- `README.md`, schema section, covering:
  - the docs provider and its four mapping parts;
  - that matching is case-sensitive;
  - YAML booleans written `true`/`false`;
  - the Service fan-out;
  - that docs→docs edge values are file paths for now, with front-matter keys the next slice;
  - the safety list and the known limit.
- `PROJECT_STATUS.md`: declarative providers slice 1 done; `git`/`ast` and front-matter keys open.
- README.md: define **Declarative provider** and **Docs source** in the project-schema section. Do not create or edit `CONTEXT.md` (it is not part of this repository).

- [ ] Live, against a throwaway registry and repository:
  - Add the Runbook type and the RUNBOOK_FOR relationship through the Config page form.
  - Confirm with `devgraph config schema list --repo` and `git status` (file unstaged).
  - Watch the debounced rescan apply it.
  - Check the nodes and edge in the graph view.
  - Edit a runbook and see the change land live.
  - Add the compose file afterwards and see the edge appear.
  - Run `devgraph doctor` with a typo'd glob and a file missing a required field.
  - Take a browser screenshot if a browser is available.
- [ ] Update the docs. Run the full `uv run pytest -q`. Commit "Document docs front-matter sources".
