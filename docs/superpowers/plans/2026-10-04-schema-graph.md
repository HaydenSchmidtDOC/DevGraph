# Schema-Driven Graph Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The dashboard graph view takes each repository's node and relationship types (and colours) from its applied schema instead of hardcoded lists; a repository with no schema file looks exactly as today.

**Architecture:** An optional display-only `color` on schema declarations; a read-only `GET /api/repos/{repo_id}/schema` route merging built-ins with the repository's applied schema (`read_applied_schema`) plus colours from the file; the backend accepts and searches declared labels; the single-page frontend (`devgraph/dashboard/static/index.html`) builds its type tables from that route on boot and on repo change.

**Tech Stack:** Python 3.13, FastAPI, Neo4j, vanilla JS (Cytoscape) in one static HTML file, node-driven JS tests (skipped without node), pytest.

**Spec:** `docs/superpowers/specs/2026-10-04-schema-graph-design.md`

**Working directory:** this worktree, branch `epic1/g1-schema-graph` (stacked on `epic1/f3-config-schema`). Live tests use Neo4j at `bolt://127.0.0.1:7687` (`neo4j` / `devgraph-local-dev`) and must run, not skip; leave no data behind (use fixtures with teardown).

## Global Constraints

- No schema file → the route returns exactly today's built-in node labels and relationship types, and the UI renders exactly today's rows/colours (snapshot test).
- Types shown come from the **applied** schema (what is in the graph); colours from the file (fallback: deterministic hash colour per label); `schema_state` ∈ `applied | pending | never | absent | invalid | disabled`.
- `color` is display-only, `#rrggbb`, validated; changing it changes the schema hash (accepted; documented).
- Labels reaching Cypher are restricted to the repository's allowed set and escaped as today (`escapeCypherStr`).
- `__all__` view: union of built-ins and every enabled repository's applied types; first registered repo's colour wins on conflict.
- Commit messages: plain imperative; no `Co-Authored-By` trailer, no AI attribution. Never commit `uv.lock`, real names or personal paths.

## Review Focus

1. A label that only exists in a pending (not yet applied) schema → not offered in the UI; a "schema change pending rescan" hint shows — Task 2/4.
2. Switching between two repos with different user labels → rows, chips, isolation and search swap cleanly, no stale user rows — Task 4.
3. `?label=` with a label another repo declares but this one doesn't → 400 — Task 3.
4. A disabled repository → built-ins only, `schema_state: disabled` — Task 2.
5. User label colours stable across reloads and distinct from built-in category colours — Task 4.

---

### Task 1: Display colour on schema declarations

**Files:** `devgraph/config/project_schema.py` (`NodeTypeDecl`, `RelationshipDecl`, starter text), `devgraph/cli/main.py` (`config show` and `config schema list` echo colour in table and JSON), tests in `tests/config/` and `tests/cli/`.

- [ ] Failing tests: `color: "#1f77b4"` accepted on a node type and a relationship; `"blue"`, `"#12345"`, `"#gggggg"` rejected with a message naming the field and the `#rrggbb` format; the JSON Schema has `color` with the pattern; `config schema list --json` and `config show --json` include `color` (null when absent).
- [ ] Implement (`color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")`), extend the starter-template comments with one example line.
- [ ] `uv run pytest -q`; commit `"Allow a display colour on schema node and relationship types"`.

### Task 2: `GET /api/repos/{repo_id}/schema`

**Files:** `devgraph/dashboard/routes.py`, `devgraph/dashboard/queries.py` (counts helper if needed), tests in `tests/dashboard/test_routes.py`.

- [ ] Failing tests (live Neo4j, mirroring existing route tests' fixtures and cleanup): unknown repo → 404; no schema file → `node_types` == built-in labels (each `origin: "builtin"`, `color` null), `relationship_types` == built-in types, `schema_state: "absent"`, `notices: []`; a repo with a filesystem schema applied (full_scan) → `File`/`Folder`/`IS_CHILD_OF` (whatever the fixture declares) with `origin: "project"`, declared colour or a hash colour, per-label `count`; file changed after apply → `schema_state: "pending"` and the new label absent; invalid file → `invalid` with a notice; disabled repo → `disabled`, built-ins only; `__all__` → union.
- [ ] Implement using `engine.read_applied_schema`, `schema_pending`/`schema_file_hash`, `load_project_schema` (colours; tolerate invalid), the built-in label/type constants, and the project switch. Hash colour: a small deterministic function (e.g. HSL from a stable hash of the label) shared with the frontend via the payload (the backend sends the final colour so the JS needs no hash function).
- [ ] `uv run pytest tests/dashboard -q`; commit `"Serve each repository's graph types to the dashboard"`.

### Task 3: Label-aware graph and search endpoints

**Files:** `devgraph/dashboard/routes.py` (`repo_graph` label check ~286), `devgraph/dashboard/queries.py` (`_SEARCHABLE_LABELS` ~20), tests.

- [ ] Failing tests: `?label=File` accepted for a repo whose applied schema declares it, 400 for a repo that doesn't; dashboard search finds a declared-label node by name; built-in behaviour unchanged.
- [ ] Implement: allowed labels = built-ins ∪ that repo's applied user labels (for `__all__`, the union).
- [ ] `uv run pytest tests/dashboard -q`; commit `"Accept declared labels in dashboard graph and search"`.

### Task 4: Frontend

**Files:** `devgraph/dashboard/static/index.html` (`NODE_TYPES`/`REL_TYPES`/`CAT_COLORS` ~812-848, count rows/chips ~930-960, `mapGraphResultToElements` ~2254, `currentStateQuery` ~2354, repo change handler ~2753, autocomplete ~2860, ~3438-3450), new `tests/dashboard/schema_types_ui.js` + `tests/dashboard/test_schema_types_ui.py` (follow the existing `mcp_telemetry_ui.js` harness pattern).

- [ ] Failing JS-harness tests: given stubbed `/schema` payloads for two repos, boot renders built-in rows identical to today's constants (snapshot of labels, rel types and colours) plus user rows; switching repo rebuilds rows/chips without stale user rows; user labels use the payload colour; isolating `File` builds a query with `labels(n)[0] = "File"`; a `pending` payload shows the pending hint.
- [ ] Implement: fetch the route on boot and repo change; build `NODE_TYPES`/`REL_TYPES` from it; keep `cat` for built-ins, `cat = "user:" + label` for user labels with per-label colour; isolation and highlighting keyed by label; counts from the payload; pending hint near the type list. Keep `escapeCypherStr` for every interpolated label.
- [ ] `uv run pytest tests/dashboard -q` (the harness runs when node is installed — check `node --version`; it is required here, don't let it skip); commit `"Build the dashboard's type lists from the repository schema"`.

### Task 5: Docs and a manual check

- [ ] README (remove the known-limit note about hardcoded dashboard types; document `color` and the pending hint), PROJECT_STATUS.
- [ ] `uv run pytest -q`.
- [ ] Manual: scratch registry + repo with a filesystem schema with colours, `devgraph add`, start the dashboard against it (throwaway port), fetch `/api/repos/<id>/schema` and `/api/repos/<id>/graph?label=<Label>` with curl, and, if a browser is available, a screenshot of the graph view after switching repos; otherwise record the JSON. Clean up.
- [ ] Commit `"Document schema-driven graph rendering"`.
