# Dashboard Config Page (G2a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A Config pane in the dashboard: global then per-project view of node types, relationships and tools with locks, global flags and badges, plus validated, fingerprint-guarded add/edit/delete that writes files and never commits.

**Spec:** `docs/superpowers/specs/2026-10-05-config-page-design.md` (every task implements the spec sections it names; the spec's file/line references were verified on the base).

**Working directory:** this worktree, branch `epic1/g2a-config-page` (stacked on #39 with #40 merged). Live tests: Neo4j `bolt://127.0.0.1:7687` (`neo4j`/`devgraph-local-dev`), unique repo_ids, cleanup in teardown; never touch `~/.devgraph`. Dashboard test clients use `base_url="http://127.0.0.1"` (Host guard).

## Global Constraints

- Writes reuse the CLI's validate-then-atomic-write code (extracted in Task 1), never a second copy.
- Only `devgraph.schema.yaml`, `devgraph.tools.yaml` in a registered active repo, or the global store, are ever written; never git.
- Every write carries the fingerprint of the file the user saw; mismatch → 412, nothing written.
- Every state-changing route calls `_reject_cross_site`; the app-wide Host guard (#40) covers all routes.
- Commit messages: plain imperative; no `Co-Authored-By` trailer, no AI attribution. Never commit `uv.lock`, real names or personal paths.

### Task 1: Extract the validated write path

- [ ] New `devgraph/config/edits.py`; `devgraph/cli/main.py` tools/schema commands become thin wrappers (`_write_atomically`, schema-warning helpers, effect notes moved); new `tests/config/test_edits.py`. Gate: all of `tests/cli/` green with no test edits.
- [ ] TDD: failing tests first; `uv run pytest -q` before committing; commit with a plain message describing the task.

### Task 2: Dry tool resolution

- [ ] `devgraph/mcp/tool_plane.py`: `resolve_tools` usable with no server (pure resolution: origins, notices, shadowed, served); `tests/mcp/test_tool_plane.py` parity tests proving it matches what a real server registers.
- [ ] TDD: failing tests first; `uv run pytest -q` before committing; commit with a plain message describing the task.

### Task 3: Read routes and badges

- [ ] `devgraph/dashboard/routes.py`: `GET /api/config`, `GET /api/config/{scope}` (model builder per spec §2, badges, display-only tool ids, per-file fingerprints); `tests/dashboard/test_config_routes.py` read half, including cross-site/Host rejection (app-wide guard from #40).
- [ ] TDD: failing tests first; `uv run pytest -q` before committing; commit with a plain message describing the task.

### Task 4: Write routes

- [ ] `devgraph/dashboard/routes.py`: tools + schema POST/PUT/DELETE per spec §2–2.3 (If-Match fingerprint CAS under a per-path lock, dry run with schema warnings, error table, only registered active repos and the three file names, no symlinks, size caps, never git); tests: write half, every security rule in spec §2.3, a no-git assertion in a real `git init` repo.
- [ ] TDD: failing tests first; `uv run pytest -q` before committing; commit with a plain message describing the task.

### Task 5: Config pane UI

- [ ] `devgraph/dashboard/static/index.html`: nav button and pane in the Settings overlay, renderers (global first, then projects; locks, global flag, badges with hover detail), `#configModal` YAML editor, global-edit warning step, destination dropdown for saving a global tool to a repo, dry-run confirm, 412 reload flow; add `Container` to `BUILTIN_NODE_TYPES` and update `tests/dashboard/schema_types_ui.js`; new `tests/dashboard/config_page_ui.js` + `test_config_page_ui.py` (node must run).
- [ ] TDD: failing tests first; `uv run pytest -q` before committing; commit with a plain message describing the task.

### Task 6: Docs and live verification

- [ ] README dashboard section (Config page; writes the file, never commits), PROJECT_STATUS (G2a done, G2b list); live checks per spec §4 with a throwaway registry and a browser screenshot if a browser is available; full suite.
- [ ] TDD: failing tests first; `uv run pytest -q` before committing; commit with a plain message describing the task.
