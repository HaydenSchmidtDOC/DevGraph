# Dashboard Config page extras (G2b-1) — design

Stacked on #42 (G2a, `epic1/g2a-config-page`).
Epic refs: §8 (scoped tool IDs: "identity in storage, logs, UI"; renaming mints a new id and orphans its telemetry history — accepted), §10 (one Config page).
Builds on `docs/superpowers/specs/2026-10-05-config-page-design.md` (the G2a spec, "G2a spec" below): its §1 deferrals, §2.2 API envelope and error table, §2.3 write-safety rules and §3 Q7 all apply unchanged unless this document says otherwise.
Line numbers were verified on the tip of #42.

---

## 1. Scope

### PR G2b-1 (this PR): "Scoped tool ids in MCP telemetry, whole-file reset and the project-config toggle on the Config page"

In:
1. **Telemetry by scoped tool id.** Built-in tools keep their bare name; global tools record `gl_<name>`, project tools `<repo_id>_<name>`. Arguments are still never recorded. The MCP telemetry API and card show the id and group by it. Existing stored records (bare names) stay readable.
2. **Whole-file reset** on the Config page for each of the three files: the global tools store (`__global__`), a repo's `devgraph.tools.yaml`, a repo's `devgraph.schema.yaml`. Goes through the existing validated write path (`devgraph/config/edits.py`), requires `If-Match`, starts with a dry run listing what the reset removes, and asks for a typed confirmation.
3. **Project-config enable/disable toggle** on each project card: a route over `registry.set_project_config_enabled` (devgraph/registry/store.py:267) behind `_reject_cross_site`, the CLI's effect notes, and a refresh of the affected badges.

Still deferred (G2b-2 or later, unchanged from the G2a spec §1): copy across repos, structured form editor, cross-repo schema conflict badges, retiring the prototype "MCP tools" pane.

---

## 2. Design

### 2.1 Scoped tool ids in telemetry

**Today.** `_instrument` (devgraph/mcp/server.py:202) wraps every served tool and calls `record_tool_call(tool=fn.__name__, …)` (server.py:113). Built-ins get it through the rebound `server.tool` chokepoint (server.py:304–309); declared tools get it in `_register_layers` (devgraph/mcp/tool_plane.py:431): `instrument(make_tool_function(tool, engine, repo.repo_id, notices, layer))`. The store keeps `_TELEMETRY_FIELDS = ("ts", "tool", "duration_ms", "ok")` (server.py:85), re-applied as an allow-list on read (`read_tool_telemetry`, server.py:169). The dashboard computes ids for display only (devgraph/dashboard/config_model.py:84, 117, 171).

**How the telemetry layer learns the origin.** The tool plane already knows it at the one place it builds a declared tool's function: `make_tool_function` (tool_plane.py:168) receives both `repo_id` and `layer` (`"project"` | `"global"`). It stamps two attributes on the function it returns:

```python
call.devgraph_tool_origin = layer                                  # "project" | "global"
call.devgraph_tool_id = scoped_tool_id(tool.name, layer, repo_id)  # "<repo_id>_<name>" | "gl_<name>"
```

`_instrument` reads them (`getattr(fn, "devgraph_tool_id", fn.__name__)`, `getattr(fn, "devgraph_tool_origin", "builtin")`). A built-in function has neither attribute, so it records its bare name and origin `builtin`. `functools.wraps` copies `__dict__`, so the attributes survive any wrapper, and the `instrument` callable keeps its one-argument signature (tests pass `instrument=lambda f: f`; tests/mcp/test_tool_plane.py:311, 334, 424, 444).

Rejected alternatives: changing `instrument` to `instrument(fn, tool_id)` (touches every caller and `ProjectToolPlane`, for no gain); deriving the origin in `_instrument` from `fn.__name__` (a name alone cannot tell global from project); parsing the id back (repo id slugs may contain `_` and may be `gl`, so ids are not reversible — see the `origin` field below).

**One id function.** New `scoped_tool_id(name, origin, repo_id=None) -> str` in `devgraph/mcp/catalog.py` (next to `builtin_tool_names`, catalog.py:40): `builtin` → `name`, `global` → `gl_<name>`, `project` → `<repo_id>_<name>`. `config_model.py` replaces its three inline f-strings with it, so the Config page and the telemetry store can never disagree.

**Record shape.** `_TELEMETRY_FIELDS = ("ts", "tool", "tool_id", "origin", "duration_ms", "ok")`:
- `tool` stays the **wire name** (what the agent called), so a per-name view across scopes is still possible.
- `tool_id` is the scoped id — the identity the card groups by.
- `origin` ∈ `builtin | global | project`. Stored rather than inferred because the id is not reversible: registry slugs (`_SLUG_RE`, store.py:56) allow `_`, so the repo id `gl` makes project tool `x` the id `gl_x`, the same as global tool `x`. The card groups by `(origin, tool_id)`, which separates that case. One ambiguity remains and is accepted: two *project* ids can coincide when one repo id is a `_`-prefix of another (repo `a` tool `b_c` and repo `a_b` tool `c` are both `a_b_c`). It needs underscore repo ids plus a matching tool-name split, it only merges two rows on a local card, and fixing it means changing the epic's id format (e.g. a separator a slug cannot contain), so it is documented rather than engineered around.

A line stays far below `PIPE_BUF` (repo ids and tool names are short identifiers), so the single-`write` append guarantee holds; ~180 bytes × 500 records stays below `_TELEMETRY_TRIM_AT_BYTES` (256 KiB).

**Privacy promise — the explicit, reviewed change (G2a spec Q7).** Still metadata only, still never arguments, Cypher or results. What changes: a project tool's id contains the repo id the **session** is scoped to. That value comes from the server's own configuration (`DEVGRAPH_MCP_REPO` or the working directory, `resolve_session_repo`, tool_plane.py:71), never from the call; the built-ins' `repo_id` argument stays unrecorded, and global tools record no repository. Updated in the same commit: `record_tool_call` / `_TELEMETRY_FIELDS` comments and the module docstring (server.py:27–31, 79–85, 113–131), the card's tooltip (index.html:615), its comment (index.html:3786–3790) and its note (index.html:3913), README and PROJECT_STATUS wording ("no … repository" becomes "no arguments; a project tool's id names its session's repository").

**Existing records (compat, no migration).** The store is a 500-record ring (`_TELEMETRY_MAX_ENTRIES`), so legacy lines age out on their own; rewriting them is not worth a migration path. `read_tool_telemetry` normalises at read time: a record without a string `tool_id` gets `tool_id = tool`; a record without a valid `origin` gets `builtin` when `tool` is a built-in name (built-in names are locked, so that is unambiguous) and otherwise `unscoped` (a declared tool recorded before this change; its scope was never written). The allow-list rebuild stays, so unknown keys are still dropped. Accepted consequence, as the epic accepts for renames: a project tool's pre-upgrade calls group under its bare name, its later calls under its scoped id.

**API.** `GET /api/mcp-telemetry` (routes.py:899) is unchanged apart from the two extra fields per entry.

**Card.** `summarizeMcpTelemetry` (index.html:3817) groups by `(origin, tool_id)` (falling back to `tool` when it is not a non-empty string, the same admission rule as today), shows ids in "Top tools" (truncation raised from 32 to 48 characters — ids are longer), and puts each top entry's wire name and origin in the row's hover (`title`, set as text). Legacy `unscoped` entries are labelled "(recorded before scoped ids)" in the hover. Everything is still written with `textContent`.

**Logs.** The telemetry store is the tool-call log; no other log line records tool calls. Tool-plane warnings and MCP response notices keep wire names, because they are agent- and user-facing and name the tool the agent called (epic §8: names stay bare on the wire).

### 2.2 Whole-file reset

**Backend: `devgraph/config/edits.py`.** Two new mutators with the same contract as the G2a mutators (`expected_fingerprint`, `dry_run`, `_guard` → per-path lock, fingerprint CAS, symlink/non-regular refusal; raises `ConfigEditError`):

- `reset_tools(root, *, expected_fingerprint=None, dry_run=False) -> EditResult` — project scope: unlink `devgraph.tools.yaml`; global scope (`root is None`): `save_global_tools([])` (devgraph/config/global_tools.py:82), as the CLI does today.
- `reset_schema(root, *, record=None, expected_fingerprint=None, dry_run=False) -> EditResult` — unlink `devgraph.schema.yaml`; `warnings = schema_change_warnings(before, None, record)` (edits.py:513), the CLI's exact text.

`EditResult` gains `removed: dict[str, list[str] | None]` (`{"tools": [...]}` or `{"node_types": [...], "relationships": [...]}`). The listing is best effort and **never blocks the reset** — reset is the escape hatch for a broken file: names are read with `yaml.safe_load` the way `_declared_names` (tool_plane.py:312) does, and `None` means "not readable" (the UI then says "the file is not valid YAML; its whole contents are removed"). For an invalid schema file, `before` is `None`, so instead of the per-type warnings the result carries one generic warning: "The file is invalid, so what it declared can't be listed; the next rescan returns this repository to the built-in schema and deletes the nodes of any project type applied earlier."

Reset-specific notes in the dry run (`notes`):
- project tools: for each removed name the valid global store also declares, "After the reset, global tool `x` is served in <repo_id>." — only when that global tool is actually served afterwards (not a built-in's name, project config enabled).
- global store: "Removes N global tools from every repository's MCP sessions; repositories with a project tool of the same name keep theirs." (counts from the same `_resolutions` the Config page builds, config_model.py:102).

After a project-file reset the fingerprint is `absent`; after a global reset it is the empty store's hash (computed under the lock, as `EditResult.fingerprint` is today). A reset of a file that does not exist returns `written: false` with the note "Nothing to reset: <file> does not exist." (CLI parity) — never an error; the UI hides the button for `absent` files anyway.

The CLI's `config tools reset` (devgraph/cli/main.py:2348) and `config schema reset` (main.py:2627) keep their prompts and output but call these helpers instead of unlinking themselves — one code path, the existing CLI tests are the regression net.

**Routes** (`POST …/reset/{kind}`: `reset` sits where entry routes have `tools`/`schema`, so no entry path — including a name that normalises to `..` — can resolve to it; the bare collection `DELETE`s do not exist and answer 405):
- `POST /api/config/{scope}/reset/tools`, body `{"dry_run": bool}` — `scope` is `__global__` or an active registered repo.
- `POST /api/config/{repo_id}/reset/schema`, body `{"dry_run": bool}` — `__global__` → 404 (no global schema store).

Order in each handler, as in G2a (routes.py:733–737): `_reject_cross_site_config`, `_write_record(scope[, schema=True])`, the body (`_json_payload`, then `_body_dry_run`), `_if_match`, then `run_in_threadpool(_apply_edit, …)`. `_apply_edit` already maps every `ConfigEditError` code to the G2a error table (412 `stale` with the current scope, 409 `not_regular`, 500 `io` …) and scrubs paths; it is extended to pass `removed` through and to add a `global` block (below). The body is strict `application/json` (an object; only `dry_run` is read, default false), like every other write, so a cross-site form post cannot send it, and `_reject_cross_site` refuses same-browser cross-site requests.

Success body = the G2a envelope plus `removed` and, for any tools write that can change another scope's badges, `global`:
```json
{"ok": true, "written": false, "file": "devgraph.tools.yaml", "fingerprint": "sha256:…",
 "removed": {"tools": ["hot_paths", "find_parents"]},
 "warnings": [], "notes": ["After the reset, global tool hot_paths is served in repo-a.", "…effect note…"],
 "scope": {…}, "global": {…}}
```

**Binding the confirm to what was reviewed.** The dry run returns the fingerprint of the exact bytes it listed (one read under the lock, not a later re-read of the scope); the confirming request sends that value in `If-Match`. If the file changed between review and confirm, the reset is a 412 and nothing is deleted.

### 2.3 Project-config toggle

**Backend.** Move the CLI's note text (main.py:1849–1853) into `edits.project_config_notes(repo_id) -> list[str]`:
- "schema: applied at the next rescan (`devgraph rescan <repo_id> --now` to apply now)"
- "project tools: picked up by running MCP sessions within 2 s"

The CLI prints them from there. Accurate for both directions: the switch changes `schema_file_hash` (project_schema.py:517–526), so a watched repo goes pending and `SchemaRescanScheduler` applies it after the quiet period, and `tools_fingerprint` (tool_plane.py:494) reads the switch on every 2-second poll.

Also `edits.project_config_change(record, enabled) -> (warnings, notes)`: the schema warnings of the change (`schema_change_warnings(decl, None, record)` when disabling a repo whose schema file is valid, `(None, decl)` when enabling), plus, when disabling, "Project tools no longer served in <repo_id>: a, b" (parsed from the tools file itself, so independent of the switch's current position; built-in names excluded; an invalid file instead warns that tools "may still be served from the last good file until the session restarts"), followed by "Global tools of the same name take over in <repo_id>: …" for global tools of those names. Pure; no write.

**Route.** `PUT /api/config/{repo_id}/project-config`, body `{"enabled": true|false, "dry_run": false}`.
- `_reject_cross_site_config`; strict `application/json` and the 64 KiB cap via a new `_json_object_body(request) -> dict` factored out of the first half of `_config_body` (routes.py:227–257), which `_config_body` then calls — no second copy of the checks.
- Scope: an active registered repo only (`_write_record(repo_id, schema=True)` — 404 for `__global__`, unknown or inactive; same symlinked-root refusal).
- `enabled` must be a bool (400 `bad_request`).
- Dry run: returns `{ok, written: false, changed, enabled, warnings, notes, scope, global}` without writing.
- Write: `registry.set_project_config_enabled(repo_id, enabled)` in the threadpool; `ValueError` (removed concurrently) → 404 `not_found`; `sqlite3.Error` → 500 generic "could not update the registry". Already in that state → `changed: false`, note "Project config for <repo_id> is already enabled." (CLI wording).
- **No `If-Match`.** The target is a registry flag, not a file, and the request names the end state, so it is idempotent: replaying or racing it cannot lose an edit. This is the one deliberate difference from the G2a write rules; every other rule (2.3 items 1, 3, 8, 9) holds.

**Badge refresh.** The response carries the repo's refreshed block (its tools/schema states become `disabled` with the existing `not-served` badges, config_model.py:151, 185, 253–262) **and** the global block, whose "Overridden in" badges depend on which repos serve project tools. The page re-renders both. Reset of a project tools file does the same.

### 2.4 Frontend (devgraph/dashboard/static/index.html)

- **Reset button** in the section header of each file-backed section (`configSection`, near index.html:3008): global Tools, project Nodes (the schema file — shown once, on Nodes, labelled "Reset devgraph.schema.yaml"), project Tools. Hidden when the file state is `absent`; shown for `invalid` (that is when it is most useful).
- **Reset modal** `#configResetModal` (reuses `.modal-overlay`/`.modal-box`): opening it runs the dry run; it lists `removed` (as text), warnings and notes, then a text input "Type <phrase> to reset". The phrase is the repo id for project files and `global` for the global store (the scope token `__global__` is not something to ask a person to type). The Reset button is disabled until the input equals the phrase exactly, and keeps the existing `CONFIG_ARM_MS` arming delay. Confirm sends `POST …/reset/{kind}` with `If-Match` = the dry run's fingerprint. On 412: "The file changed since this list was made" + Re-check (re-runs the dry run, clears the typed phrase). The dialog shows "Git can restore a tracked file; an untracked file or the global store cannot be restored." (for the global store: that it cannot be restored) before the typed phrase, and the Reset button is styled as destructive. The success note is the server's, keyed off `removed`: "Deleted <file>; not staged or committed." for a project file, "Emptied the global tools store." for the global store (never "Written to … not committed"); it shows in the top status and in the card.
- **Toggle**: a `.switch` labelled "Project config" in each project card header. Turning it off runs the dry run (turning it on applies directly); with no warnings it applies at once; with warnings (disabling a repo with project node types or project tools, 2.3), an inline confirm panel lists them with "Disable anyway" / Cancel (the G2a rule: confirm only when the dry run warns). The switch shows the server's state after the response, never the optimistic one; on error it snaps back and shows the error text. The two effect notes render under the card header.
- Pure functions (test harness grabs them by regex): `configResetRequest(scope, kind, fingerprint, dryRun) -> {url, init}`, `configResetPhrase(scope) -> string`, `configResetReady(typed, phrase) -> bool`, `configToggleRequest(repoId, enabled, dryRun) -> {url, init}`, `describeConfigReset(response) -> {removed: string[], warnings, notes}`; plus `openConfigReset(...)` and `toggleProjectConfig(...)` for DOM wiring. `applyConfigScope` is reused for `scope`, and for `global` when present.
- Every name, path and message rendered with `textContent`.

---

## 3. Design questions — recommended answers

1. **Where does telemetry learn a tool's origin?** From attributes `make_tool_function` stamps on the function (2.1); `_instrument` reads them. No signature change, built-ins need nothing.
2. **Replace `tool` with the id, or add fields?** Add `tool_id` and `origin`; `tool` stays the wire name. Grouping uses `(origin, tool_id)`; `origin` disambiguates the `gl` slug collision. The residual project-vs-project collision for `_`-prefixed repo ids (2.1) is accepted, not fixed — fixing it changes the epic's id format.
3. **Record the repo for project tools?** Yes — the epic requires the scoped id, and it is session configuration, not call data. Global tools and built-ins record no repository. The privacy wording changes in the same commit (2.1).
4. **Migrate old records?** No. Read-time normalisation (`tool_id = tool`; origin `builtin` or `unscoped`); the 500-record ring retires them.
5. **Reset routes' shape?** `POST /reset/tools` and `/reset/schema` with a JSON `{dry_run}` body and `If-Match`. Collection `DELETE` was rejected: an entry delete ending in `/..` normalises to it.
6. **Reset of an invalid file?** Allowed; listing is best effort; generic warning for an invalid schema.
7. **Reset of a symlinked file?** Refused (409 `not_regular`), as every G2a write. The CLI reset, now on the same helper, refuses it too (today it would unlink the link): the user removes a link they made by hand.
8. **Global reset: delete the store or empty it?** Empty it (`save_global_tools([])`) — CLI parity.
9. **Strong confirmation?** Typed phrase (repo id, or `global`) plus the arming delay; the dry run's fingerprint binds the confirm to what was shown. The armed-confirm pattern alone is for per-entry edits; a whole-file reset can drop an untracked file or the global store irrecoverably.
10. **`If-Match` on the toggle?** No: idempotent end-state on a registry flag (2.3).
11. **Confirm when disabling?** Only when the dry run warns, matching G2a's dry-run rule. Disabling warns when the repo has project node types (the next rescan deletes their nodes) or serves project tools ("Project tools no longer served in <repo_id>: …", 2.3), so either one asks for confirmation; enabling, and disabling a repo with neither, apply directly.
12. **Toggle for inactive repos?** No; the page lists active repos only (G2a Q8). The CLI still works for them.
13. **Logs carry the id?** The telemetry store is the tool-call log; notices and warnings keep wire names (agent-facing).

---

## 4. Test strategy

**`tests/mcp/test_server_telemetry.py`**: a built-in call records `tool == tool_id == name`, `origin == "builtin"`; a global tool call records `gl_<name>`/`global`; a project tool call records `<repo_id>_<name>`/`project`; a repo with id `gl` keeps `(origin, tool_id)` distinct from a global tool of the same name; "exactly the allowed fields" becomes six; the no-arguments test also asserts no argument value and, for built-ins, no `repo_id` value appears anywhere in the line; legacy lines read back with `tool_id = tool` and origin `builtin`/`unscoped`; a line with a non-string `tool_id` is normalised, extra keys still dropped.

**`tests/mcp/test_tool_plane.py`**: `make_tool_function` stamps the attributes; a reload (`ProjectToolPlane.reload_if_changed`) registers functions that still record the scoped id; `scoped_tool_id` unit cases. **`tests/dashboard/test_config_routes.py`**: existing `tool_id` assertions stay green through the shared helper.

**`tests/dashboard/test_mcp_telemetry_routes.py`**: entries carry `tool_id`/`origin`; legacy store lines are normalised.

**`tests/dashboard/mcp_telemetry_ui.js`**: grouping by `tool_id` (two repos' `find_parents` are two rows; legacy bare entries group by `tool`), hover text, hostile ids rendered as text, note no longer claims "no repository" and states the project-tool rule.

**`tests/config/test_edits.py`**: `reset_tools` (project, global), `reset_schema`: dry run writes nothing and lists `removed`; stale fingerprint → `stale` and file intact; symlink → `not_regular`; invalid/unparseable file still resets with `removed` `None`; absent file → `written: False` with the note; schema warnings equal the CLI's. `project_config_change` warnings/notes for both directions. **`tests/cli/`**: existing reset and enable/disable tests green; one new CLI test for the symlink refusal.

**`tests/dashboard/test_config_routes.py`** (reset + toggle): happy paths (files gone / store emptied, refreshed `scope` and `global`, `Deleted …; not staged or committed.` / `Emptied the global tools store.`); dry run leaves bytes unchanged; 403 (`Origin: http://evil.test`, `Sec-Fetch-Site: cross-site`); 404 (`__global__` schema, unknown/inactive repo, `__global__` toggle); 412 + current scope; 428 without `If-Match` on reset; toggle 400 (non-bool), 413, 415; toggle `changed: false`; disabling flips the repo's badges to not-served and removes it from the global entry's "Overridden in"; no git (index untouched in a `git init` repo). Test clients keep `base_url="http://127.0.0.1"`.

**`tests/dashboard/config_page_ui.js`**: Reset button presence per file state; `configResetRequest` method/URL/`If-Match`; `configResetReady` exact-match only (case, whitespace); 412 path clears the phrase and offers Re-check; `configToggleRequest` body; disable with warnings needs the second click, without warnings does not; switch reflects the server's state after an error; hostile names rendered as text.

**Live checks (before PR)**: headless agent + dev Neo4j, throwaway registered repo: call a project tool, a global tool and a built-in from a real `devgraph mcp` session and see three distinct ids on the card; reset the project tools file → the session stops serving it within 2 s, the global one takes over, `git status` shows the deletion unstaged; reset a schema file with a filesystem node type → the dry run named the type, the rescan deletes its nodes; disable → card badges go not-served, `devgraph list` shows `off`, the session drops project tools; `curl` with a foreign `Origin` → 403. Screenshot for the PR.

Docs: README (Config page: reset and toggle; MCP telemetry privacy wording), PROJECT_STATUS (G2b-1 done, G2b-2 list).
