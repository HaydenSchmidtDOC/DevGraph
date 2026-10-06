# Dashboard Config Page Extras (G2b-1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** MCP telemetry records scoped tool ids (`gl_<name>`, `<repo_id>_<name>`, bare built-ins) without ever recording arguments, and the dashboard Config page gains a dry-run-first, typed-confirmation whole-file reset and a project-config enable/disable toggle.

**Spec:** `docs/superpowers/specs/2026-10-05-config-extras-design.md` (every task implements the spec sections it names; the spec's file/line references were verified on the base). The G2a spec `docs/superpowers/specs/2026-10-05-config-page-design.md` §2.2–2.3 still governs every write.

**Working directory:** this worktree, branch `epic1/g2b1-config-extras` (stacked on #42, `epic1/g2a-config-page`). Live tests: Neo4j `bolt://127.0.0.1:7687` (`neo4j`/`devgraph-local-dev`), unique repo_ids, cleanup in teardown; never touch `~/.devgraph`. Dashboard test clients use `base_url="http://127.0.0.1"` (Host guard).

## Global Constraints

- Telemetry stays metadata only: never arguments, Cypher or results. The only new repository-bearing value is a project tool's id, built from the session's scope, never from the call. Privacy wording (server docstrings, card tooltip/comment/note, README, PROJECT_STATUS) changes in the same commit as the behaviour.
- One id function (`scoped_tool_id` in `devgraph/mcp/catalog.py`) for the store and the Config page.
- Reset goes through `devgraph/config/edits.py` (fingerprint CAS under the per-path lock, symlink refusal); the CLI reset commands use the same helpers. Only the three config files are ever removed or emptied; never git.
- Every new state-changing route calls `_reject_cross_site_config`; reset requires `If-Match`; the toggle is the one documented exception (idempotent registry flag). Bodies: strict `application/json`, 64 KiB cap, via the shared body helper.
- Responses use the G2a envelope and error table; rendered text in the page is always `textContent`.
- Commit messages: plain imperative; no `Co-Authored-By` trailer, no AI attribution. Never commit `uv.lock`, real names or personal paths.

### Task 1: Scoped tool ids in MCP telemetry

**Files:** `devgraph/mcp/catalog.py`, `devgraph/mcp/tool_plane.py`, `devgraph/mcp/server.py`, `devgraph/dashboard/config_model.py`, `devgraph/dashboard/static/index.html` (MCP card only), `tests/mcp/test_server_telemetry.py`, `tests/mcp/test_tool_plane.py`, `tests/dashboard/test_mcp_telemetry_routes.py`, `tests/dashboard/mcp_telemetry_ui.js`.

- [ ] Write failing tests (spec §4, telemetry bullets): built-in / global / project records with `tool`, `tool_id`, `origin`; repo id `gl` vs global tool of the same name stay distinct; six allowed fields; no argument value in any line; legacy lines normalised (`tool_id = tool`, origin `builtin`/`unscoped`); `make_tool_function` stamps `devgraph_tool_id`/`devgraph_tool_origin` and a reload keeps them; card groups by `(origin, tool_id)`, hover names wire name and origin, note states the project-tool rule.
- [ ] Implement spec §2.1: `scoped_tool_id`; stamp attributes in `make_tool_function`; `_instrument` reads them; `record_tool_call(tool_id=…, origin=…)`; `_TELEMETRY_FIELDS` and read-time normalisation in `read_tool_telemetry`; `config_model.py` uses `scoped_tool_id`; `summarizeMcpTelemetry`/`renderMcpTelemetry` grouping, 48-char truncation and wording; update every privacy comment/tooltip named in the spec.
- [ ] `uv run pytest -q`; commit with a plain message describing the task.

### Task 2: Whole-file reset — edits and routes

**Files:** `devgraph/config/edits.py`, `devgraph/cli/main.py` (`config tools reset`, `config schema reset` become wrappers), `devgraph/dashboard/routes.py`, `tests/config/test_edits.py`, `tests/cli/` (one symlink-refusal test), `tests/dashboard/test_config_routes.py`.

- [ ] Write failing tests (spec §4, edits + reset routes): `reset_tools` (project, global) and `reset_schema` dry run / write / stale / symlink / invalid file / absent file; `removed` listing and reset notes; schema warnings equal the CLI's; `POST /api/config/{scope}/reset/tools` and `POST /api/config/{repo_id}/reset/schema` happy path, `{"dry_run": true}`, 403/404/409/412/428, `global` block in the response, no git.
- [ ] Implement spec §2.2: `EditResult.removed`; `reset_tools`, `reset_schema`; CLI wrappers keep prompts and output; the two `POST …/reset/{kind}` routes through `_write_record`, `_if_match`, the body `dry_run`, `_apply_edit` (extended to return `removed` and, for tools writes, the refreshed `global` block). Existing CLI suites green with no edits except the new test.
- [ ] `uv run pytest -q`; commit with a plain message describing the task.

### Task 3: Project-config toggle route

**Files:** `devgraph/config/edits.py` (`project_config_notes`, `project_config_change`), `devgraph/cli/main.py` (`_set_project_config` prints the moved notes), `devgraph/dashboard/routes.py` (`_json_object_body` factored out of `_config_body`; `PUT /api/config/{repo_id}/project-config`), `tests/config/test_edits.py`, `tests/dashboard/test_config_routes.py`.

- [ ] Write failing tests (spec §4, toggle): notes text equals the CLI's; disable warnings list project node types and unserved project tools; route dry run writes nothing; enable/disable flips `registry.get(...).project_config_enabled`; `changed: false` when already in that state; refreshed repo block shows not-served badges and the global block drops the repo from "Overridden in"; 400 non-bool, 403, 404 (`__global__`, unknown, inactive), 413, 415; no `If-Match` needed.
- [ ] Implement spec §2.3. `tests/cli/` enable/disable tests green unchanged.
- [ ] `uv run pytest -q`; commit with a plain message describing the task.

### Task 4: Config page UI — reset and toggle

**Files:** `devgraph/dashboard/static/index.html` (Config block), `tests/dashboard/config_page_ui.js`, `tests/dashboard/test_config_page_ui.py`.

- [ ] Write failing JS harness tests (spec §4, `config_page_ui.js`): Reset button per file state; `configResetRequest`, `configResetPhrase`, `configResetReady` (exact match only); dry run listed before the phrase input; 412 clears the phrase and offers Re-check; `configToggleRequest`; disable-with-warnings needs a second click, without warnings it does not; switch shows the server's state after an error; `scope` and `global` blocks both re-rendered; hostile names as text.
- [ ] Implement spec §2.4: Reset buttons in `configSection` headers, `#configResetModal` (dry run → list → typed phrase → armed Reset with the dry run's fingerprint), project-card `.switch` with inline warning confirm and effect notes; reuse `applyConfigScope` for both blocks.
- [ ] `uv run pytest -q` (node must run); commit with a plain message describing the task.

### Task 5: Docs and live verification

**Files:** `README.md` (Config page: reset and toggle, remove them from "Not in this page yet"; MCP telemetry privacy wording), `PROJECT_STATUS.md` (G2b-1 done; remaining G2b list; dashboard telemetry wording).

- [ ] Live checks per spec §4 (real `devgraph mcp` session, three distinct ids on the card; project tools reset with global takeover and unstaged deletion; schema reset deleting nodes on rescan; disable/enable reflected in badges, `devgraph list` and the session; cross-origin `curl` → 403) with a throwaway registry; browser screenshot if a browser is available.
- [ ] Update the docs; full `uv run pytest -q`; commit with a plain message describing the task.
