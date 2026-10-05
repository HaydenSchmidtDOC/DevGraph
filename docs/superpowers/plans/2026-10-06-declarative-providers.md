# Declarative Providers: Docs Front Matter (Slice 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A `devgraph.schema.yaml` node type can declare `source: {provider: docs, paths, where, fields}`, and a relationship can declare `provider: docs` with a `field`. Matching Markdown files then become nodes, with properties mapped from front matter and edges found by key lookup. They are kept current by the watcher and rescans, reported by `devgraph doctor`, and editable in the Config page form. No repository code runs.

**Spec:** `docs/superpowers/specs/2026-10-06-declarative-providers-design.md`. Every task implements the sections it names. The filesystem provider spec (`2026-10-04-filesystem-provider-design.md`) and the schema rescan spec (`2026-10-04-schema-rescan-design.md`) still govern everything this slice reuses.

**Working directory:** this worktree, branch `epic1/declarative-providers` (from `epic1/combined`). Live tests use Neo4j at `bolt://127.0.0.1:7687` (`neo4j`/`devgraph-local-dev`), unique repo_ids and cleanup in teardown. Never touch `~/.devgraph`. Dashboard test clients use `base_url="http://127.0.0.1"` (Host guard).

## Global Constraints

- **Nothing executes.** No `eval`, `exec`, subprocess, import, template engine or format-string expansion of user data. The only interpreters are `bounded_safe_load` and `re`.
- **Reads go through the bounded helpers.** Every file read uses `read_bounded`, and every front-matter parse uses `bounded_safe_load(..., YAML_MAX_NODES)` with `YAML_LOAD_ERRORS` caught. The candidate files are `_indexable_paths` (ignored directories and outside symlinks excluded). No new walk rules, and no `.gitignore` logic.
- **Only declared names are written.** Labels, relationship types and property names come only from the validated `EffectiveSchema`. Front-matter keys and values are only ever Cypher parameters, never interpolated.
- **The provider owns only its own data.** It deletes only nodes with `extractor = "docs"` and only edges of docs relationship types leaving them. Built-in and filesystem behaviour stays byte-identical. A repository without docs sources produces the same graph as before.
- **The provider fails closed.** An invalid schema skips the provider, prune included. A bad file is skipped and counted, never fatal to the batch.
- **No regressions to existing rules.** No new MCP tool, no new route, no new static file. The `custom` provider stays inert. Existing schema files stay valid.
- **YAML stays the source of truth on the Config page.** The G2b form rules (`2026-10-05-form-editor-design.md`, `2026-10-05-relationship-form-design.md`) carry over unchanged: the form writes only `#configYaml`, refuses into YAML with a reason, and gives advisory hints only.
- **TDD.** Each task starts with failing tests, then the implementation, then `uv run pytest -q` (the full suite, with node available for the dashboard harnesses).
- **Commits.** Plain imperative messages, with no `Co-Authored-By` trailer and no AI attribution. Never commit `uv.lock`, real names or personal paths.

## Review Focus

Reviewers should check these first. They are where this slice could run something, write something undeclared, or delete something it does not own.
1. **No execution path** (spec §3.1). Grep the diff for `eval`, `exec`, `subprocess`, `importlib`, `format(` and f-strings carrying user values. Regexes are compiled once, during validation.
2. **Interpolation.** Every label, relationship type and property name that reaches Cypher comes from the validated schema and fullmatches its pattern. The two new engine queries take everything else as parameters.
3. **Deletion scope.** `prune_extracted_at` and `delete_extracted_edges` filter on `extractor = $extractor` and on docs relationship types. An invalid schema reaches neither call. Built-in and filesystem nodes survive every docs test.
4. **Edges survive edits.** An edit MERGEs nodes in place, so incoming docs edges survive. Re-indexing a code file relinks docs edges to its recreated Module/Service. `full_scan` writes edges after the built-ins.
5. **Bounds.** Check the bounds on a FIFO, an oversized file, a YAML alias bomb, a 101-item list, a 1,001-character regex input and an outside symlink.
6. **Form fidelity.** An `equals` value that is not a string, and an unknown source key, open in YAML with a reason. The round trip keeps `where`/`fields` key order and leaf types. The drift guard covers `DocsSource`, `Condition` and the new constants.

### Task 1: Schema: docs source and docs relationships

**Files:**
- `devgraph/config/project_schema.py`:
  - `NodeSource` becomes `FilesystemSource | DocsSource`, discriminated on `provider`, with `Condition` added;
  - `NODE_SOURCE_PROVIDERS` and `PROVIDER_KINDS` gain `docs`;
  - `RelationshipDecl.field`;
  - `_check_filesystem` restricted to filesystem sources;
  - the docs rules in `_check_provider`;
  - the `starter_schema_text` example.
- Tests: `tests/config/test_project_schema.py`.

- [ ] Write failing tests (spec §2):
  - The spec's Runbook example validates.
  - Each rule fails with a plain-words message:
    - `paths` empty, more than 20, absolute, containing `..`, or longer than 200;
    - a condition with both or neither of `equals`/`matches`;
    - a pattern that does not compile, or is longer than 200;
    - a non-scalar `equals`;
    - `fields` naming an undeclared field or `path`;
    - a front-matter key with a control character;
    - a key other than `[path]`;
    - a docs relationship without `field`, with `custom`, with a built-in type, or with a non-docs `from` label;
    - `field` on another provider.
  - Several docs node types are allowed, and existing filesystem files still validate.
  - `resolve_declaration` provisions the `_repo_name` index for docs types.
  - The uncommented starter example validates.
- [ ] Implement spec §2 (format and validation only, with no indexing).
- [ ] `uv run pytest -q tests/config`, then `uv run pytest -q`. Commit "Declare docs front-matter sources in the project schema".

### Task 2: Pure docs provider: selection, mapping and edges

**Files:** `devgraph/indexer/providers/docs.py` (new). Tests: `tests/indexer/test_docs_provider.py` (new).

The module provides:
- `docs_spec(effective)`, which returns `None` when the schema has no docs sources;
- `read_front_matter(path)`, using `read_bounded` and `bounded_safe_load` and reusing `_FRONTMATTER_RE` from `docs/extractor.py` (import it, don't copy it);
- `build_nodes(spec, repo_id, files: dict[rel_path, Path])`, which returns nodes plus skip counts;
- `build_edges(spec, repo_id, files)`;
- `source_report(repo_root, effective)` for doctor.

- [ ] Write failing tests, with no Neo4j:
  - Glob matching, including `**`, `.markdown` and non-Markdown exclusion.
  - Each condition:
    - `equals` against a string, an integer, a boolean and a list item;
    - `matches` with `re.search` against a string and a list item;
    - no front matter, with and without `where`.
  - `fields` renaming and the same-name default.
  - Type coercion for each metadata type, with lists, maps and dates left unset.
  - A missing required field skips the node and is counted.
  - Node shape: exactly `name`, `path`, `extractor = "docs"` and the mapped fields.
  - Edge values: a string, an integer, a list, more than 100 items capped, and non-scalars ignored.
  - A malformed, FIFO, oversized or alias-bomb file is skipped and counted.
  - The `source_report` wording from spec §5.
- [ ] Implement spec §2 (property values and key lookup), §3.1–3.4 and the report half of §5.
- [ ] `uv run pytest -q tests/indexer/test_docs_provider.py`, then `uv run pytest -q`. Commit "Add the docs front-matter provider".

### Task 3: Engine: path-scoped prune and edge delete

**Files:** `devgraph/graph/engine.py` (new `prune_extracted_at(repo_id, extractor, paths, keep)` and `delete_extracted_edges(repo_id, extractor, paths | None, rel_types)`, as constants next to `_PRUNE_EXTRACTED_CYPHER`). Tests: `tests/graph/test_engine_extracted_nodes.py` (live).

- [ ] Write failing live tests:
  - `prune_extracted_at` deletes only this extractor's nodes at those exact paths that are missing from `keep`. It leaves other paths, other extractors, other repos and built-ins alone, and nothing below a path.
  - `delete_extracted_edges` removes only outgoing edges of the listed types from this extractor's nodes, at the paths or across the whole repo when `paths` is `None`. Incoming edges and other types stay.
  - Empty `paths` or `rel_types` make no call.
- [ ] Implement them. Relationship types reach Cypher only as parameters, filtered with `type(r) IN $rel_types`.
- [ ] `uv run pytest -q tests/graph`, then `uv run pytest -q`. Commit "Add path-scoped extractor prune and edge delete".

### Task 4: Dispatch wiring: apply, watch, delete and relink

**Files:** `devgraph/indexer/dispatch.py`:
- a `_docs_spec(repo_root)` mirroring `_filesystem_spec`;
- the docs reconcile in `apply_project_schema`;
- the docs sync in `index_paths` after the filesystem sync;
- the relink pass;
- the docs delete in `remove_paths`;
- a final `docs.sync_edges` in `full_scan`.

Tests: `tests/indexer/test_docs_provider_live.py` (new, live), `tests/indexer/test_schema_apply_live.py`.

- [ ] Write failing live tests on a temporary repository with `runbooks/` and a compose file declaring a Service:
  - A full scan creates the Runbook nodes and RUNBOOK_FOR edges to the Service.
  - Editing `owner` updates the property. Changing `service:` moves the edge. Breaking a `where` condition removes the node.
  - An incoming Runbook→Runbook edge survives an edit of its target.
  - Deleting a file, and deleting the directory, removes the nodes.
  - Re-indexing the compose file relinks the edge.
  - A schema edit (a new `fields` mapping, a removed type, a removed relationship type) is applied by the rescan.
  - An invalid schema leaves the docs nodes untouched.
  - Ignored directories and an outside symlink are never read.
  - A repository without docs sources gives a graph snapshot equal to master's.
- [ ] Implement spec §4.
- [ ] `uv run pytest -q tests/indexer`, then `uv run pytest -q`. Commit "Index docs front-matter sources on scan, watch and rescan".

### Task 5: Doctor and Config page form

**Files:**
- `devgraph/cli/main.py`: the Project schemas section calls `docs.source_report` per valid repository.
- `devgraph/dashboard/static/index.html`, Config page block:
  - `CONFIG_FORM_FIELDS`: `source_providers`, `relationship_providers` and the docs source and condition fields;
  - the source select gains "Docs front matter", which shows Paths rows, Conditions rows and a Front-matter key input per metadata row;
  - the relationship provider gains "Docs front matter", which shows a Field input;
  - new `CONFIG_FORM_REASONS` for a non-string `equals` and for unknown source keys;
  - hints for the key rule and for unknown `fields` names.

Tests:
- `tests/cli/test_cli.py` (doctor);
- `tests/dashboard/config_page_ui.js`, `config_form_dump.js`, `test_config_form_roundtrip.py`, `test_config_form_drift.py`.

- [ ] Write failing tests:
  - Doctor prints the OK, skipped and no-match lines of spec §5, and nothing for repositories without docs sources.
  - In the form, the docs source and relationship are representable and round-trip by identity, including `where` order and `fields`.
  - The refusals, each with its reason.
  - Provider switching shows and hides the docs controls, keeps focus, and never writes hidden keys.
  - In the drift guard, `DocsSource`/`Condition` properties match the form fields, and the provider lists equal `NODE_SOURCE_PROVIDERS`/`PROVIDER_KINDS`.
  - Every control is labelled, and hostile values land only in `.value`/`textContent`.
- [ ] Implement spec §5 (doctor and form).
- [ ] `uv run pytest -q` (node must run). Commit "Show docs sources in doctor and the Config form".

### Task 6: Docs and live verification

**Files:** `README.md` (schema section: the docs provider, its four mapping parts, the safety list and the known limit), `PROJECT_STATUS.md` (declarative providers slice 1 done; `git`/`ast` and field keys open).

- [ ] Live, against a throwaway registry and repository:
  - Add the Runbook type and the RUNBOOK_FOR relationship through the Config page form.
  - Confirm with `devgraph config schema list --repo` and `git status` (file unstaged).
  - Watch the debounced rescan apply it.
  - Check the nodes and edge in the graph view.
  - Edit a runbook and see the change land live.
  - Run `devgraph doctor` with a typo'd glob.
  - Take a browser screenshot if a browser is available.
- [ ] Update the docs. Run the full `uv run pytest -q`. Commit "Document docs front-matter sources".
