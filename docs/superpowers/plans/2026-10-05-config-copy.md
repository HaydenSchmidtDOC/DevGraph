# Dashboard Config Page Copy and Conflict Badges (G2b-2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** The dashboard Config page can copy a project node type, relationship or tool to another repository (or a project tool to the global store) through the existing validated add/replace routes; it badges node types that conflict across repositories, using the same finder as `devgraph doctor` and `config validate`, now in a shared module; and the fake "MCP tools" Settings pane is gone.

**Spec:** `docs/superpowers/specs/2026-10-05-config-copy-design.md` (every task implements the spec sections it names; the spec's file/line references were verified on the base). The G2a spec `docs/superpowers/specs/2026-10-05-config-page-design.md` §2.2–2.3 still governs every write.

**Working directory:** this worktree, branch `epic1/g2b2-config-copy` (stacked on #43, `epic1/g2b1-config-extras`). Live tests: Neo4j `bolt://127.0.0.1:7687` (`neo4j`/`devgraph-local-dev`), unique repo_ids, cleanup in teardown; never touch `~/.devgraph`. Dashboard test clients use `base_url="http://127.0.0.1"` (Host guard).

## Global Constraints

- No new write route and no new write semantics: a copy is an add, or (after a confirmed dry run) a replace, on the destination's existing route. Every G2a rule holds: `_reject_cross_site_config`, active registered repo or `__global__` only (never a path), strict `application/json` with the 64 KiB cap, `If-Match` required (the same fingerprint for the dry run and the write), whole-document validation, per-path lock and fingerprint CAS, symlink refusal, never stage or commit.
- The conflict finder lives in `devgraph/config/schema_findings.py` with no Rich/Typer import; the CLI imports it under its old private names, and its output stays byte-identical (existing CLI tests unchanged are the parity net).
- Badges are computed server-side; the page never re-implements conflict or resolution rules. Rendered text is always `textContent`.
- Conflicts are computed over all registered repositories (`registry.list_repos()`), as `doctor` does.
- TDD: each task starts with failing tests, then the implementation, then the full suite.
- Commit messages: plain imperative; no `Co-Authored-By` trailer, no AI attribution. Never commit `uv.lock`, real names or personal paths.

### Task 1: Shared schema findings module

**Files:** `devgraph/config/schema_findings.py` (new), `devgraph/cli/main.py` (remove `_enable_hint_id` and `_project_schema_findings` bodies; import the aliases), `tests/config/test_schema_findings.py` (new).

- [ ] Write failing tests (spec §4, `test_schema_findings.py`): absent/valid/invalid/disabled findings; a differently-keyed label in two repos is one `conflict` with `repo_ids`, `label` and `declarations` (`repo_id`, `label`, `key`, `disabled`); identical declarations are not a conflict; case-only difference is; `overrides` swaps one repo's declaration (or `None`) without reading its file; `schema_conflicts` returns only conflicts; `introduced_conflicts(repos, repo_id, before, after)` reports a conflict new to `repo_id` with the "Creates a schema conflict: " prefix, not a pre-existing one, and nothing when `after` drops the label.
- [ ] Implement spec §2.2 "Move" and "Additions": move the function and `enable_hint_id` verbatim, add `declarations`, `overrides`, `schema_conflicts`, `introduced_conflicts`; in main.py `from devgraph.config.schema_findings import project_schema_findings as _project_schema_findings` (and `enable_hint_id as _enable_hint_id` if anything else still uses it).
- [ ] `uv run pytest -q tests/config tests/cli` (test_cli.py finder, doctor and `config validate --all` tests green unchanged), then `uv run pytest -q`; commit with a plain message describing the task.

### Task 2: Conflict badges, dry-run conflict warnings and copy-ready `exists`

**Files:** `devgraph/config/edits.py` (`add_schema_entry` node type `exists` carries `name`; `ConfigEditError` docstring), `devgraph/dashboard/config_model.py` (`conflicts` parameter; `schema-conflict` badge in `_schema_block`; `run_cypher_enabled` in the global tools block), `devgraph/dashboard/routes.py` (`get_config`, `_config_scope` pass `schema_conflicts(registry.list_repos())`; `_apply_edit` appends `introduced_conflicts` for schema writes), `tests/config/test_edits.py`, `tests/dashboard/test_config_routes.py`.

- [ ] Write failing tests (spec §4, edits + routes): node type `exists` has `name`, relationship `exists` does not; two repos with differently keyed `Widget` → each row's `schema-conflict` error badge names the other, detail equals the CLI's text; identical → no badge; a disabled repo participates; single-scope GET carries the badge; a schema add dry run that would create a conflict warns "Creates a schema conflict:" and writes nothing, an edit keeping an existing conflict does not repeat it; copy-shaped route sequences (node type POST → 409 with `detail.name` → PUT dry run → PUT write; project tool POST to `__global__`; relationship with an endpoint the destination lacks → 422; stale destination → 412, bytes unchanged; no git); global block `run_cypher_enabled` follows the setting.
- [ ] Implement spec §2.1 "Backend change", §2.2 "Badges", "Routes", "Dry-run warning" and the `run_cypher_enabled` field of §2.3.
- [ ] `uv run pytest -q`; commit with a plain message describing the task.

### Task 3: Config page "Copy to…"

**Files:** `devgraph/dashboard/static/index.html` (Config block: `configEntryRows`, `openConfigEditor`, `configEditTarget`, `configModalTitle`, `configWarningText`, `configSave` reload rule, new `configCopyDestinations`, `configCanCopy`), `tests/dashboard/config_page_ui.js`, `tests/dashboard/test_config_page_ui.py`.

- [ ] Write failing JS harness tests (spec §4, `config_page_ui.js`): Copy button visibility rules; destinations exclude the source and offer the global store only for tools; `configEditTarget` add then replace after `crossName`; read-only YAML and "Copy …" title; 409 with `name` → replace warning and an armed second click; 409 without `name` → message, no write; the write reuses the dry run's `If-Match`; 412 → Reload of the destination keeps the dialog; every successful write triggers a quiet full reload; conflict badge text and hover; hostile names as text.
- [ ] Implement spec §2.1 frontend and §2.4.
- [ ] `uv run pytest -q` (node must run); commit with a plain message describing the task.

### Task 4: Retire the prototype MCP tools pane

**Files:** `devgraph/dashboard/static/index.html` (remove nav button, `#pane-mcp`, `loadMcpTools`, `toggleCypher` wiring and its `loadDashboardSettings` lines; `#linkOpenMcpSettings` → `#linkOpenConfig` targeting the Config pane; run_cypher line/badge in the Global Tools section), `tests/dashboard/header_register_button.js`, `tests/dashboard/config_page_ui.js`.

- [ ] Write failing tests (spec §4): header harness `PANES` without `mcp`, `#linkOpenConfig` opens `pane-config` and prevents navigation, and index.html contains no `data-pane="mcp"`, `pane-mcp`, `mcpToolList` or `toggleCypher`; Config harness shows the run_cypher off line and the on badge from `run_cypher_enabled`.
- [ ] Implement spec §2.3. `/api/mcp-tools` and its test stay.
- [ ] `uv run pytest -q`; commit with a plain message describing the task.

### Task 5: Docs and live verification

**Files:** `README.md` (Config page: Copy to…, `schema-conflict` row in the badge table, run_cypher line, dry-run conflict warning; drop copy, conflict badges and the MCP tools pane from "Not in this page yet"), `PROJECT_STATUS.md` (G2b-2 done; only the structured form editor remains).

- [ ] Live checks per spec §4 (copy node type A→B with unstaged file and rescan; replace confirm; conflict warning then badges on both cards matching `devgraph doctor`; project tool to global with "Overrides global tool" on A and a B session serving it within 2 s; no MCP tools nav, Graph link lands on Config; foreign-`Origin` `curl` → 403) with a throwaway registry; browser screenshot if a browser is available.
- [ ] Update the docs; full `uv run pytest -q`; commit with a plain message describing the task.
