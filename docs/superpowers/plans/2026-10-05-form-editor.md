# Dashboard Config Page Form Editor (G2b-3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** The Config editor gets a Form | YAML switch for tools (global and project) and project node types. The form is a view over the existing YAML textarea: every form edit re-serialises the entry into `#configYaml`, and Save, the dry run, the confirmation, the text binding and the busy lock are unchanged. Entries the form cannot represent exactly open in YAML with a notice; relationships stay YAML.

**Spec:** `docs/superpowers/specs/2026-10-05-form-editor-design.md` (every task implements the spec sections it names; the spec's file/line references were verified on the base). The G2a spec `docs/superpowers/specs/2026-10-05-config-page-design.md` §2.2–2.3 still governs every write.

**Working directory:** this worktree, branch `epic1/g2b3-form-editor` (stacked on #44, `epic1/g2b2-config-copy`). Live tests: Neo4j `bolt://127.0.0.1:7687` (`neo4j`/`devgraph-local-dev`), unique repo_ids, cleanup in teardown; never touch `~/.devgraph`. Dashboard test clients use `base_url="http://127.0.0.1"` (Host guard).

## Global Constraints

- YAML is the source of truth. The form only ever writes `#configYaml.value` (then runs the textarea's own change handler); `configSave`, `configWriteRequest`, `configEditTarget`, the dry run, `edit.dryYaml`, the confirmation, `CONFIG_ARM_MS` and the 412 flow are not modified. No new route, no new request shape.
- No YAML parsing in the browser: the form is built from the server's `entry` mapping (or a section template's `entry`); YAML → Form is allowed only for text whose mapping is known (spec §2.4).
- Never drop data: an entry the form cannot represent exactly opens in YAML with a notice; the form never writes keys it does not show.
- Client-side checks are advisory hints only and never disable Save; the server's dry run and 422 text are authoritative.
- Every entry value is assigned through `.value`, `textContent` or `option.value`; nothing from an entry reaches `innerHTML`. Element ids come from a counter, never entry data.
- Every control has a label; the editor is fully usable by keyboard; Tab is never captured.
- All new browser code is inside the Config page block of `devgraph/dashboard/static/index.html` (between `/* ── Config page` and `/* ── end Config page ── */`), so the harness can grab it; no new static file.
- TDD: each task starts with failing tests, then the implementation, then the full suite.
- Commit messages: plain imperative; no `Co-Authored-By` trailer, no AI attribution. Never commit `uv.lock`, real names or personal paths.

### Task 1: Parsed entries in the Config page model

**Files:** `devgraph/dashboard/config_model.py` (new `form_entry`; `entry` on global tool rows in `_global_entry`, project tool rows in `_project_tool_entry`, node type rows in `_schema_block`), `tests/dashboard/test_config_routes.py`.

- [ ] Write failing tests (spec §4, routes): `GET /api/config` global tool, project tool and node type rows carry `entry` equal to the file's mapping (including key order); relationship rows have no `entry`; a tool whose description is a YAML date, a node type with a `.nan` value and a tool with a 2^60 `max_rows` have `entry: null`; `form_entry` unit cases (nested lists/dicts of plain scalars kept; non-string key, date, non-finite float, integer beyond ±(2^53 − 1) give `None`; `bool` is kept as a boolean).
- [ ] Implement spec §2.2 (server half).
- [ ] `uv run pytest -q tests/dashboard`, then `uv run pytest -q`; commit with a plain message describing the task.

### Task 2: Pure form functions, serialiser round-trip and drift guard

**Files:** `devgraph/dashboard/static/index.html` (Config page block: `CONFIG_FORM_FIELDS`, `CONFIG_FORM_LIMITS`, `entry` on `CONFIG_SECTIONS.tools` and `.node_types`, `configFormFromEntry`, `configEntryFromForm`, `configYamlScalar`, `configEntryYaml`, `configFormHints`, `configFormSwitch`), `tests/dashboard/config_page_ui.js`, `tests/dashboard/config_form_dump.js` (new), `tests/dashboard/test_config_form_roundtrip.py` (new), `tests/dashboard/test_config_form_drift.py` (new).

- [ ] Write failing tests (spec §4, pure functions): in `config_page_ui.js`, representability (accepted tool and node type fixtures; each refusal with its reason text), form ↔ mapping identity on representable fixtures, typed-field coercion (integer/float text to numbers, other text kept as string), omission and default-preservation rules, key order (opened order, then model order), `configEntryYaml` scalar styles and block choice, every hint and none for a valid entry, `configFormSwitch` for the opening text, the form's last text, and hand-edited text. In `config_form_dump.js` + `test_config_form_roundtrip.py`: for ordinary and hostile fixtures `yaml.safe_load(configEntryYaml(m)) == m`, valid fixtures pass `CypherTool.model_validate` / `NodeTypeDecl.model_validate`, and each template's `entry` equals `yaml.safe_load(template)`. In `test_config_form_drift.py`: `CONFIG_FORM_FIELDS` equals the JSON Schema `properties` of `CypherTool`, `ToolParameter`, `NodeTypeDecl`, `MetadataField`, `NodeSource`; the enums equal `PARAMETER_TYPES`, `METADATA_TYPES`, `FILESYSTEM_KINDS`; `CONFIG_FORM_LIMITS` equals the Python patterns and limits. Both new Python tests skip when `node` is missing.
- [ ] Implement spec §2.3, §2.5, §2.6, §2.8, §2.9 and the template `entry` of §2.2.
- [ ] `uv run pytest -q tests/dashboard` (node must run), then `uv run pytest -q`; commit with a plain message describing the task.

### Task 3: Form view in the Config editor

**Files:** `devgraph/dashboard/static/index.html` (`#configModal` markup: Editor switch and `<fieldset id="configForm">`; `.cfg-form` CSS; `renderConfigForm`, `configFormChanged`, `configTextChanged` (factored out of the textarea's `oninput`), `configSetEditorMode`, `configEditorFocusTarget`; `openConfigEditor` takes `entry` and sets the mode state; `renderConfigModal` shows/hides the form, disables the fieldset and switch while busy, and focuses `configEditorFocusTarget()` when the button label changes; `configEntryRows` and the Add handler pass `item.entry` / the template `entry`), `tests/dashboard/config_page_ui.js` (stub DOM gains `fieldset`, `input`, `label`, `legend`, `setAttribute`/`getAttribute`), `tests/dashboard/test_config_page_ui.py` (docstring).

- [ ] Write failing harness tests (spec §4, DOM flow): form mode for a tool and a node type, YAML mode with the notice for an unrepresentable entry and for relationships, read-only YAML for delete and copy, no editor on the global warning step; a form edit writes the serialised text into `#configYaml`, clears `crossName` and drops a pending confirmation like a textarea edit; Save from form mode sends the textarea text (same URL, method, `If-Match`, body as YAML mode) and a form edit after a warning confirmation forces a new dry run; open-then-save without changes sends the model's `yaml` byte for byte; fieldset and switch disabled while busy; Form button disabled with its reason after a hand edit, Discard restores the form text and drops a confirmation; labels/legends on every control, Remove names include the row name, focus moves to the new row after Add; hostile values only in `.value`/`textContent`.
- [ ] Implement spec §2.1, §2.4, §2.7, §2.10 and §2.11.
- [ ] `uv run pytest -q` (node must run); commit with a plain message describing the task.

### Task 4: Docs and live verification

**Files:** `README.md` (Config page: the Form/YAML switch, which entries have a form, that it writes the same YAML through the same dry run and confirmation, hand-edited YAML stays in YAML mode; replace "Not in this page yet: a structured form editor" with the relationship form and YAML read-back), `PROJECT_STATUS.md` (G2b-3 done; relationship form and YAML read-back open).

- [ ] Live checks per spec §4 (form-added project tool with two parameters is listed by `devgraph config tools list --repo`, served by a `devgraph mcp` session within 2 s, file unstaged in `git status`; node type key change by ticks shows the dry run's key-change warning and needs the second click; unknown-key entry opens in YAML with the notice; Form → YAML → hand edit → Form button explains, Discard returns; keyboard-only pass through the tool form) with a throwaway registry; browser screenshot if a browser is available.
- [ ] Update the docs; full `uv run pytest -q`; commit with a plain message describing the task.
