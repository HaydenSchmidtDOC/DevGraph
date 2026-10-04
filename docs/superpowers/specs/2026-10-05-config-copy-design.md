# Dashboard Config page copy and conflict badges (G2b-2) — design

Stacked on #43 (G2b-1, `epic1/g2b1-config-extras`).
Epic refs: §8 (collision rules; reporting channel 3, "badge on the offending tool, detail on hover"), §10 (one Config page; "Copy across repos for node/relationship types and project tools"; save from a global entry offers global or a project).
Builds on `docs/superpowers/specs/2026-10-05-config-page-design.md` (the G2a spec) and `docs/superpowers/specs/2026-10-05-config-extras-design.md` (the G2b-1 spec): the G2a §2.2 API envelope and error table, §2.3 write-safety rules and the G2b-1 confirmation patterns apply unchanged unless this document says otherwise.
Line numbers were verified on the tip of #43.

---

## 1. Scope

### PR G2b-2 (this PR): "Copy entries across repositories, cross-repository schema conflict badges, and retire the prototype MCP tools pane"

In:
1. **Copy to…** on project node types, project relationships and project tools: copy one entry to another active registered repository, or a project tool to the global store. Client-side orchestration of the existing add/replace routes — dry run first, replace only after confirmation when the destination already has the entry, `If-Match` on the destination. No new write route and no new write semantics.
2. **Cross-repository schema conflict badges**: the conflict finder moves out of `devgraph/cli/main.py` into `devgraph/config/schema_findings.py` (no Rich, no Typer); the CLI (`config validate`, `doctor`, the post-write follow-up) and the Config model both use it. Each conflicting project node type gets an error badge naming the other repositories, with the CLI's text as hover detail. A schema write whose result would introduce a conflict warns in its dry run.
3. **Retire the prototype "MCP tools" pane**: remove its nav button, pane, fake per-tool toggles and the unpersisted `enable_run_cypher` switch; show `run_cypher`'s real state read-only on the Config page; retarget the Graph page's settings link to the Config pane.

Still deferred (unchanged from the G2a spec §1): the structured form editor.

Out of scope (recorded so they are not mistaken for gaps):
- Copying a node type does not copy relationships that reference it, nor anything a relationship references; the destination's validator reports a missing endpoint label (422) and the user copies that first.
- Copying several entries at once, or a whole file. One entry per copy keeps every write a reviewed dry run.
- Renaming during a copy (2.1, Q3).

---

## 2. Design

### 2.1 Copy to…

**Today.** The add routes (`POST /api/config/{scope}/tools`, `POST /api/config/{repo_id}/schema/{section}`, routes.py:823, 916) and replace routes (`PUT …/{name}`, routes.py:834, 931) already accept any entry's YAML for any active scope. G2a's "save a global tool to a repo" is exactly a cross-scope write: `configEditTarget` (index.html:3127) turns a POST into a PUT of the taken name once the dry run answers 409 `exists` with `detail.name`, and `configSave` (index.html:3245) asks for a second click with "Replaces <repo>'s own <name>." Copy reuses that machinery as a new editor op.

**Backend change (one line of behaviour).** `add_schema_entry` (devgraph/config/edits.py:589) raises `exists` for a node type without `name`; it passes `name=name`, as `add_tool` does (edits.py:294), so the client can offer to replace the destination's node type. The `ConfigEditError` docstring (edits.py:42) changes from "on a tool `exists`" to "on a tool or node type `exists`". A relationship `exists` deliberately stays without `name`: it means an *identical* relationship is already declared (edits.py:593), and relationships may share a type with different endpoints, so there is never anything to replace.

**Where Copy appears** (in `configEntryRows`, index.html:3021, as a third row action "Copy to…"):
- project node types, relationships and tools;
- not when the row is not `editable` (a relationship declared more than once: the source entry is ambiguous);
- not on a tool row with the `locked-shadow` badge (the destination would refuse it with 409 `locked`);
- not when there is no destination: schema entries need at least one other active repository; project tools always have the global store;
- **not on global tools**: G2a's editor already offers "Save to" a repository for a global tool (the epic's "save globally or save to a project via dropdown"); a second button for the same write would be two flows to keep in step.

**Destinations.** The `#configDest` select (index.html:802), relabelled "Copy to" for this op: every active registered repository except the source, plus "Global store" for tools only. Schema entries cannot go to the global store (there is no global schema store; `__global__` schema routes are 404). The first option is selected by default, never the source.

**The editor in copy mode** (`openConfigEditor({scope, section, op: "copy", name, yaml})`):
- Title "Copy <noun> <name> from <scope>" (`configModalTitle`, index.html:3144).
- The YAML textarea is shown **read-only** with the source entry's `yaml` from the page model (what the user saw). A copy is a copy; editing happens afterwards in the destination with Edit (Q3).
- Fingerprints: `fps` holds every possible destination's fingerprint for this section from the page model (as G2a does for a global tool's destinations, index.html:3212–3215).
- Warning text (`configWarningText`): for the global store as destination, "Adds <name> to the global store; <source>'s own <name> will override it in <source>." or, when the store already has the name, "Replaces the global tool <name> with <source>'s version: served in every repo without its own <name>; <source>'s own copy keeps overriding it there." (the separate "Replaces …" line is in the confirm list only), then "Will also be overridden in: …" naming every other repository with its own <name>, whatever that entry's origin. For a repository destination, "Writes to <dest>/<file>; <source> is unchanged." No extra warning *step*: the user picked the destination explicitly in the same dialog, and adding a new entry never had one in G2a.
- Changing the destination clears `crossName` and any confirm (as `dest.onchange` does today).

**Flow** (`configEditTarget` gains: `op === "copy"` → `{scope: dest, op: crossName ? "replace" : "add", name: crossName}`; `configSave` is otherwise unchanged):
1. Dry run: `POST` to the destination with `If-Match` = the destination's fingerprint from the model.
2. 409 `exists` with `detail.name` (a tool or node type of that name exists there) → `crossName` = that name, warning "Replaces <dest>'s own <noun> <name>.", then dry-run the `PUT`.
3. 409 `exists` without `name` (an identical relationship) → no write, message "<dest> already declares this identical relationship; nothing to copy." (`describeConfigError` gets the server's message, which says so).
4. 409 `locked`, 422 `invalid` (for example a relationship whose endpoint label the destination does not declare), 412 `stale`: shown as today; 412 offers Reload, which already reloads the *target* scope (`configReload`, index.html:3300) and keeps the text.
5. Dry-run `warnings` (schema: nodes deleted, key change, source pruned; conflicts introduced, 2.2) or a replace → a confirm list and a second, armed click ("Copy anyway"; the `CONFIG_ARM_MS` delay applies). No warnings and no replace → the copy goes straight through, as G2a's save does.
6. The real write sends the **same** `If-Match` the dry run sent. Anything that changed the destination between review and confirm is a 412; nothing is written.

**After a write**: `applyConfigScope(dest, res.body.scope)`, the status line shows the server's warnings and notes ("Written to <file>; not committed.", the effect note, "not served while project config is disabled" for a disabled destination), then a quiet full reload. Today `configSave` reloads the whole page only after a tools write (index.html:3293); it now reloads after **every** write, because a schema write can add or clear a conflict badge on another repository's card (2.2), and a tool copied to the global store changes the source repository's badge to "Overrides global tool". The full GET is filesystem reads plus one applied-schema read per repo; it is already what a tools write does.

**Security.** Unchanged and inherited: every request goes through the existing routes, so `_reject_cross_site_config`, `_write_record` (active registered repo or `__global__`, never a path; symlinked root refused), the strict JSON body and 64 KiB cap, `If-Match` (428 when missing), `refuse_builtin`, whole-document validation, the per-path lock and fingerprint CAS, symlink refusal and "never stage or commit" all apply. The source entry's YAML is page data rendered with `textContent` in a read-only textarea; the server re-parses it with `yaml.safe_load` like any other write.

### 2.2 Cross-repository schema conflicts

**Move.** `_project_schema_findings` (devgraph/cli/main.py:672–790) and its helper `_enable_hint_id` (main.py:650) move to a new **`devgraph/config/schema_findings.py`** as `project_schema_findings(repos, *, overrides=None)` and `enable_hint_id(repo)`, unchanged apart from the additions below. The CLI imports them under the old names (`from devgraph.config.schema_findings import project_schema_findings as _project_schema_findings`), the pattern main.py:28–32 already uses for `edits`, so `doctor` (main.py:1056), `config validate` (main.py:2114), `_schema_follow_up` (main.py:2425) and the existing tests (tests/cli/test_cli.py:1015–1106, which import `devgraph.cli.main._project_schema_findings`) keep working without edits. The module imports only `devgraph.config.project_schema` and `project_switch` (as the function already does lazily), so the dashboard can import it without pulling in the CLI.

**Additions** (additive; the existing keys and text are unchanged, so CLI output is byte-identical):
- A `conflict` finding gains `declarations: [{"repo_id", "label", "key": [...], "disabled": bool}]`, sorted as `detail` already is. The badge needs to know which repos declare *differently* from a given repo; parsing `detail` would be fragile.
- `overrides: dict[str, Any] | None` maps a repo id to a parsed declaration (or `None` for "no file") used instead of reading that repo's file. Only for the dry-run warning below.
- `schema_conflicts(repos, *, overrides=None) -> list[dict]`: the `conflict` findings only.
- `introduced_conflicts(repos, repo_id, before, after) -> list[str]`: the `detail` of each conflict naming `repo_id` with `after` as its declaration whose label did not conflict for `repo_id` with `before`, prefixed "Joins an existing schema conflict: " when `after` matches one side of a conflict the other repositories already had, "Creates a schema conflict: " otherwise. Each other repository's file and the project-config switches are read once per call. Pure (files only, no registry write, no graph).

**Which repositories.** The same set as `doctor` and `config validate --all`: `registry.list_repos()` (all registered, not only active), because they share one Neo4j database whatever the Config page lists (Q6). A disabled repo stays in, as the finder already documents (main.py:769).

**Badges** (`devgraph/dashboard/config_model.py`). `build_config` and `build_project` (config_model.py:283, 246) take `conflicts: list[dict]` (default `[]`); `_schema_block` (config_model.py:192) adds, to each node type whose `label.casefold()` is the `label` of a conflict whose `repo_ids` include this repo:

```python
badge("error", "schema-conflict", f"Key conflict with {names}", finding["detail"])  # names: "repo-b (disabled), repo-c"
```

`others` = the repo ids in `declarations` whose `(label, key)` differs from this repo's own; the detail is the CLI's text ("incompatible declarations of label 'widget' in one shared database: repo-a declares Widget keyed on (slug); repo-b (disabled) declares widget keyed on (code). Only the first provisioned constraint takes effect; align the key or rename one label."). Level `error` because the CLI marks the finding `failed` (doctor and validate exit 1). It contains repo ids, labels and keys only, never a path. Rendered by the existing `configBadgeEl` (text + `data-tip` hover).

**Routes.** `get_config` and `_config_scope` (routes.py:737, 729) compute `schema_conflicts(registry.list_repos())` once per request and pass it down; the global block is unaffected (built-in labels cannot conflict: project files cannot redeclare them).

**Dry-run warning.** In `_apply_edit` (routes.py:769), for `kind == "schema"` and a successful result: `warnings += introduced_conflicts(registry.list_repos(), scope, result.before, result.after)`. `EditResult.before`/`after` already hold the declarations of the old and new text (edits.py:61–62, 557–569), so a dry run sees the would-be conflict without writing, and the existing "confirm only when the dry run warns" rule makes it a second click — for an edit, an add, or a copy alike. On the real write the same warning appears in the response (the CLI's `_schema_follow_up` warns after the write; the page warns before and after). Deletes and resets cannot introduce conflicts and get none. It runs in the threadpool with the edit, outside the per-path lock (it reads other repositories' files only).

### 2.3 Retiring the prototype "MCP tools" pane

**Today.** Nav button `data-pane="mcp"` (index.html:653) and `#pane-mcp` (index.html:699–716): a header claiming "Per-tool toggles gate registration in mcp/server.py", an `enable_run_cypher` switch (`#toggleCypher`) whose change handler only shows a warning note (index.html:3577–3581) and which `loadDashboardSettings` sets from `/api/settings` (index.html:4373–4374) — nothing is ever saved — and `#mcpToolList`, filled by `loadMcpTools` (index.html:4350–4358) from `/api/mcp-tools` with a checked, unwired switch per tool. None of it does anything; the Config page now shows the real tool surface.

**Change:**
- Remove the nav button, the whole `#pane-mcp` markup, `loadMcpTools` and its call, the `toggleCypher`/`cypherWarnNote` wiring and the two `toggleCypher` lines in `loadDashboardSettings`.
- `run_cypher`'s real state moves to the Config page: the global tools block gains `"run_cypher_enabled": bool` (from `get_settings().enable_run_cypher`, already read by `_builtin_tools`, config_model.py:76). The Global Tools section shows, below the built-ins: when off, a muted locked row "run_cypher — off for MCP sessions started with this environment (the dashboard process's settings): raw Cypher for MCP clients. Set DEVGRAPH_ENABLE_RUN_CYPHER=true where an MCP session starts, then restart it, to serve it there."; when on, `run_cypher` is already listed with the built-ins and gets a `warn` badge "Raw Cypher enabled" with detail "MCP sessions started with this environment (the dashboard process's settings) can run arbitrary Cypher against the graph. The dashboard's own Cypher box is unaffected." (The setting is read from the dashboard's own environment; each MCP session reads its own.) Read-only: settings are env-only (the Global settings pane says so), so the page does not pretend to toggle it.
- The Graph page's `#linkOpenMcpSettings` (index.html:573, handler 2873) becomes `#linkOpenConfig`, opens the overlay on the Config pane, and its sentence reads "MCP clients' raw Cypher access (`run_cypher`) is shown in Settings → Config."
- `/api/mcp-tools` (routes.py:966) stays: it is a tested read route (tests/dashboard/test_routes.py:246) and removing an endpoint is not needed to retire the pane (Q8).
- The pane's banner about per-client MCP access control being out of scope goes with it; it described a non-feature.

**Tests that reference the pane:** tests/dashboard/header_register_button.js (`PANES` list, the `#linkOpenMcpSettings` grab and the "opens the overlay on the MCP pane" checks, lines 34–38, 68–84, 129, 155–159) is updated to `config` and `#linkOpenConfig`. No other test reads `pane-mcp`, `mcpToolList` or `toggleCypher`.

### 2.4 Frontend summary (devgraph/dashboard/static/index.html)

Pure functions the harness grabs by regex: `configCopyDestinations(model, scope, section) -> [{value, label}]` (other active repos; plus the global store for tools), `configCanCopy(model, scope, section, item) -> bool` (2.1 visibility rules); `configEditTarget`, `configModalTitle` and `configWarningText` gain the `copy` op. `configEntryRows` adds the button; `openConfigEditor` handles `op: "copy"` (read-only YAML, "Copy to" label, default destination). Every name, path and message stays `textContent`.

---

## 3. Design questions — recommended answers

1. **New copy route or client orchestration?** Client orchestration of the existing add/replace routes. A server copy route would need its own scope pair, body shape, two fingerprints and its own tests, for a write that is byte-for-byte an add or a replace in the destination; every guard already sits on those routes.
2. **Which version of the source is copied?** The YAML in the page model — what the user is looking at. The source file is not re-read or locked; the copy never writes the source, so a stale source can only copy what was on screen, which is what the user asked for.
3. **Editable YAML during a copy?** No, read-only. Editing in the copy dialog makes it an "edit and save elsewhere" whose result matches neither entry, and allows a rename that the destination's 409 handling cannot follow. Copy, then Edit in the destination.
4. **Destination already has the entry?** Tools and node types: dry-run the replace, show "Replaces <dest>'s own <noun> <name>." plus the dry run's warnings, require the armed second click. Relationships: an identical one is a no-op with a message; a same-type relationship with different endpoints is added alongside, as `config schema add` does.
5. **Copy on global tools?** No; the G2a editor's "Save to" is the same write (2.1).
6. **Conflicts across which repos?** All registered (`list_repos()`), matching `doctor` and `config validate --all`; disabled repos included, as the CLI does. A conflict naming a repository the page does not list (inactive) still badges the listed one, and the hover names the other.
7. **Warn before a write that creates a conflict?** Yes: `introduced_conflicts` in the dry run, which triggers the existing second-click rule. Existing conflicts are badges, not repeated warnings on every unrelated edit.
8. **Remove `/api/mcp-tools` too?** No. The page no longer calls it, but it is a working read route with its own test, and the retirement is about fake UI, not the API. Removing it is a separate, one-line decision if nothing else adopts it.
9. **Keep the `enable_run_cypher` switch somewhere?** No switch: settings are env-only and the switch never persisted. A read-only line with the env variable on the Config page tells the truth.
10. **Badge level for a conflict?** `error`: the CLI fails on it, and one repository's constraint silently does not exist.
11. **Refresh after a write?** Quiet full reload after every write (was: tools writes only), because conflict badges and override badges cross scopes. No new response field.

---

## 4. Test strategy

**`tests/config/test_schema_findings.py`** (new; no Neo4j): the moved function's absent/valid/invalid/disabled/conflict findings (CLI tests stay as the parity net); `declarations` lists each repo's label, key and disabled flag; `overrides` replaces one repo's file (a declaration, or `None` for absent) without touching disk; `schema_conflicts` returns only conflicts; `introduced_conflicts` reports a new conflict, not an existing one, and nothing when `after` removes the label; identical `(label, key)` in two repos is not a conflict; case-only label difference is.

**`tests/cli/`**: test_cli.py finder tests, doctor and `config validate --all` tests green unchanged (they import the alias from `devgraph.cli.main`).

**`tests/config/test_edits.py`**: node type `exists` carries `name`; relationship `exists` does not.

**`tests/dashboard/test_config_routes.py`**: GET model: two repos declaring `Widget` keyed differently → each repo's `Widget` row has a `schema-conflict` error badge naming the other, detail equals the CLI's text; identical declarations → no badge; a disabled repo still participates; `_config_scope` single-repo GET carries the badge. Schema add dry run that would create a conflict returns the "Creates a schema conflict:" warning and writes nothing; an edit that keeps an existing conflict does not repeat it. Copy as route calls: node type `POST` to the destination → 409 `exists` with `detail.name` when the label is taken, then `PUT` dry run and write; project tool `POST` to `__global__`; relationship whose endpoint the destination lacks → 422; stale destination fingerprint → 412 and bytes unchanged; no git. Global block has `run_cypher_enabled`. Test clients keep `base_url="http://127.0.0.1"`.

**`tests/dashboard/config_page_ui.js`**: Copy button visibility (project rows yes; global rows no; ambiguous relationship no; `locked-shadow` tool no; schema entry with one repo no); `configCopyDestinations` excludes the source, includes the global store only for tools; `configEditTarget` for `copy` (add, then replace after `crossName`); the dialog is read-only and titled "Copy …"; dry run 409 with name → replace warning and second click; 409 without name (relationship) → message, no write; the real write reuses the dry run's `If-Match`; 412 → Reload of the destination; after a write the whole page reloads; conflict badge renders with hover text; hostile names as text; run_cypher off line and on badge.

**`tests/dashboard/header_register_button.js`**: `PANES` without `mcp`; `#linkOpenConfig` opens the overlay on `pane-config` and prevents navigation. A new check: index.html has no `data-pane="mcp"`, `pane-mcp`, `mcpToolList` or `toggleCypher`.

**Live checks (before PR)**: headless agent + dev Neo4j, two throwaway registered repos: copy a node type with a filesystem source from repo A to repo B → B's file gains it, `git status` shows it unstaged, a rescan of B populates it; copy it again → replace confirm; change B's key in the editor → dry run warns "Creates a schema conflict", save → both cards show the badge with hover naming the other, and `devgraph doctor` reports the same conflict; copy a project tool to the global store → A's row shows "Overrides global tool", a `devgraph mcp` session in B serves it within 2 s; Settings nav has no "MCP tools", the Graph page link lands on Config; `curl` write with a foreign `Origin` → 403. Screenshot for the PR.

Docs: README (Config page: Copy to…, the conflict badge row in the badge table, run_cypher line; remove copy, conflict badges and the MCP pane from "Not in this page yet"), PROJECT_STATUS (G2b-2 done; only the structured form editor remains open).
