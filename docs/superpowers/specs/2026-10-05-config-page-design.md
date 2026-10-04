# Dashboard Config page (G2a) — design

Stacked on #39 with #40 (dashboard Host guard) merged in.
Epic refs: §8 (resolution, notices, scoped tool IDs, dashboard badge channel), §10 (one Config page).
All line numbers below were verified on this tip.

---

## 1. Recommended scope

### PR G2a (this PR): "Config page: read-only global + per-project view with badges, and validated YAML editing of tools and schema entries"

In:
- **One Config pane** in the existing Settings overlay: Global (built-in node types, built-in relationship types, built-in + global tools), then one block per registered active repo (project node types, relationships, tools from its in-repo files).
- **Lock icon** on built-ins (node types, relationship types, tools); **global flag** on global entries.
- **Badges with hover detail** (§8 channel 3) from the same resolution rules the MCP tool plane uses: shadows-locked (ignored), overrides-global, falls-back-to-global, invalid file (no tools served), project config disabled (not served), schema state pending/invalid/never.
- **Add / edit / delete** for global tools, project tools, project node types, project relationships, through a popup with a YAML text area per entry, written by the **same validate-then-atomic-write code as the CLI** (extracted, not duplicated).
- **Save from a global tool**: destination dropdown "Global store" | each registered repo (writes a project override into that repo's `devgraph.tools.yaml`). No git, ever.
- **Warning step before editing/deleting a global entry** (concrete wording in §3).
- **Pre-save schema warnings** (removed types delete nodes, key change keeps old constraint, source removal prunes) via a dry run, shown before the user confirms.
- **Optimistic concurrency**: every write carries the fingerprint of the file the user saw; a changed file is a 412, never a silent overwrite.
- Hardening the write routes against DNS rebinding (see §2.3; the existing guard is bypassable for this case).
- Fold-in: `Container` added to the dashboard's `BUILTIN_NODE_TYPES` (G1 deferral) — one line plus snapshot update; it is needed anyway because the Config page's Global/Nodes list comes from `NODE_LABELS`, which has `Container` (devgraph/graph/schema.py:12).

Deferred to PR G2b:
- **Scoped tool IDs in telemetry** (`gl_<name>`, `<repo_id>_<name>`) — touches the MCP server's metadata-only promise (see Q7); G2a shows the IDs in the UI only (computed, display-only).
- **Copy across repos** for node/relationship types and project tools (the add route already supports it; G2b is just a "Copy to…" menu).
- **Whole-file reset** (`config tools reset` / `config schema reset` equivalents) and the **project-config enable/disable toggle** (small, but a registry write + rescan semantics; keep G2a to file writes). If G2a lands with room, the toggle is the first thing to pull forward — the route is ~15 lines over `registry.set_project_config_enabled` (devgraph/registry/store.py:261).
- **Structured form editor** (fields for name/description/parameters/cypher) — YAML first.
- Cross-repo schema conflict badges (`_project_schema_findings` "conflict", devgraph/cli/main.py:665) — needs that finder moved out of the CLI module first.
- Retiring/merging the prototype "MCP tools" pane (its per-tool toggles are fake; index.html:653–668, 3628–3636).

Rationale: the risky, novel part is a browser-driven write into a repository; G2a lands that path once, on top of the CLI's already-tested validators, with every guard and test in place. Everything deferred is either additive UI over the same routes (copy, reset) or a cross-cutting change with its own privacy decision (telemetry IDs). "Reset next to edited native" in §10 has no target today: built-in labels cannot be redeclared (`ProjectSchema._check_labels`, devgraph/config/project_schema.py:371) and built-in tools are locked, so the only per-entry "reset" is deleting a project override (which G2a's delete covers; the UI labels it "Remove override — falls back to global").

---

## 2. Design

### 2.1 Backend: extract the CLI write path into a pure module

The CLI helpers raise `typer.Exit` and print with Rich, so the dashboard cannot call them. Extract (move, not copy) into **`devgraph/config/edits.py`**, raising `ConfigEditError(message, code)`; the CLI keeps thin wrappers that translate to `_tools_fail` / console output, so existing CLI tests (tests/cli/test_config_tools_cli.py, test_config_schema_cli.py) are the regression net.

| Move from devgraph/cli/main.py | To edits.py as | Notes |
|---|---|---|
| `_write_atomically` (2243) | `write_atomically(path, text)` | CLI imports it |
| `_tools_store_path` (2172), `_tools_text` (2207), `_write_tools` (2215) | `tools_path(root)`, `read_text(path)`, `write_tools(root, edit, *, expected_fingerprint, dry_run) -> EditResult` (`EditResult` also carries the written file's `fingerprint`) | root `None` = global store (JSON, via `save_global_tools`, devgraph/config/global_tools.py:68) |
| `_refuse_builtin` (2267) | `refuse_builtin(name)` | code `locked` |
| add/edit/delete bodies (2341–2416) | `add_tool(root, entry)`, `replace_tool(root, name, entry)`, `delete_tool(root, name)` | existence checks: code `exists` / `not_found` |
| `_schema_text` (2485), `_schema_declaration` (2492), `_entry_section` (2512), `_locate_entry` (2519), `_schema_edit` (2547), `_duplicate_relationship` (2769) | same names, public | `schema_edit(path, edit, ...) -> (old text, new text)` keeps "validate whole doc with `parse_project_schema` + `resolve_declaration` before write" |
| `_removed_types` (2569), `_pruned_types` (2589), `_changed_keys` (2606) and the strings in `_warn_removed` (2614) / `_schema_follow_up` (2636) | `schema_change_warnings(before, after, record) -> list[str]` (plus `schema_entry_notes(entry)`; `record` only chooses the wording) | CLI prints them as today; dashboard returns them. Conflict findings stay CLI-only in G2a |
| new | `file_fingerprint(path) -> str` | `"absent"` or `"sha256:<hex>"`; same scheme as `schema_file_hash` (project_schema.py:517) but switch-independent |

Each mutate function takes `expected_fingerprint: str | None` and `dry_run: bool`: it reads the text, compares fingerprint (code `stale`), computes the new text, validates the whole document, and only then writes. Dry run returns the would-be warnings without writing. A module-level `threading.Lock` keyed by resolved target path serialises the read-compare-write within the dashboard process.

Also add a **dry resolution helper** in `devgraph/mcp/tool_plane.py`: `resolve_tools(repo) -> ToolPlaneStatus`, calling the existing `_project_layer` (274) / `_global_layer` (302) / `_register_layers` (351) with a null server (`add_tool` no-op), `engine=None`, identity `instrument`. `make_tool_function` (161) only builds a closure, so no Neo4j is touched. This gives the dashboard `origins`, `notices` and `shadowed` with exact parity to what an MCP session for that repo would serve — badges cannot drift from runtime behaviour. (Import it lazily in routes, like `/mcp-tools` does at routes.py:577–583, to avoid the app→routes→mcp cycle; tool_plane itself does not import `devgraph.agent`, so no new cycle.)

### 2.2 Routes (all under the existing `/api` router, `build_router`, routes.py:168)

Scope token: a registered repo_id, or the reserved `__global__` (matched by exact equality before any registry lookup, the same convention as `_ALL_REPOS_SCOPE`, routes.py:155/391). A repo can never be addressed by path.

**Read**
- `GET /api/config` → page model:
  ```json
  {
    "global": {
      "node_types":         [{"label": "Container", "locked": true}],
      "relationship_types": [{"type": "CALLS", "locked": true}],
      "tools": {
        "file": "global-tools.json", "state": "absent|valid|invalid", "error": null,
        "fingerprint": "sha256:…",
        "builtin": [{"name": "find_callers", "tool_id": "find_callers", "locked": true, "description": "…"}],
        "entries": [{"name": "hot_paths", "tool_id": "gl_hot_paths", "yaml": "name: hot_paths\n…",
                     "badges": [{"level": "info", "kind": "overridden", "text": "Overridden in repo-a", "detail": "…"}]}]
      }
    },
    "projects": [{
      "repo_id": "repo-a", "display_path": "~/src/repo-a", "project_config_enabled": true,
      "effect_notes": {"tools": "Running MCP sessions pick this up within 2 seconds.", "schema": "…applied about 5 minutes after…"},
      "schema": {"file": "devgraph.schema.yaml", "state": "absent|applied|pending|never|invalid|disabled",
                 "error": null, "fingerprint": "…", "extends": "default",
                 "node_types":    [{"label": "File", "yaml": "…", "editable": true, "badges": []}],
                 "relationships": [{"type": "IS_CHILD_OF", "yaml": "…", "editable": true, "badges": []}]},
      "tools":  {"file": "devgraph.tools.yaml", "state": "…", "error": null, "fingerprint": "…",
                 "entries": [{"name": "find_parents", "tool_id": "repo-a_find_parents", "yaml": "…",
                              "origin": "project (overrides global)", "badges": [...]}]}
    }]
  }
  ```
  Built from: `builtin_tool_names()` (devgraph/mcp/catalog.py:40) + the docstring summaries already computed by `/mcp-tools`; `NODE_LABELS`/`RELATIONSHIP_TYPES`; `list_edit.entries` + `dump_entry` (list_edit.py:40/54) for per-entry YAML (so an invalid file still lists what can be read, with the file-level error badge); `_repo_schema(record)` (routes.py:355) for schema state; `resolve_tools(repo)` for tool origins/notices; the effect-note wording moved out of `_tools_scope_note` (cli 2158) / `_schema_effect_note` (cli 2468) into edits.py. Only `registry.list_repos(active_only=True)`. `editable: false` (with a badge) for a relationship type declared more than once — mirrors the CLI's "edit the file by hand" refusal (cli edit, ~2852).
- `GET /api/config/{scope}` → just that scope's block (the client re-renders one block after a write).

**Write** (JSON body `{"yaml": "<one entry>", "dry_run": false}`; `If-Match: "<fingerprint>"` header required)
- `POST   /api/config/{scope}/tools` — add
- `PUT    /api/config/{scope}/tools/{name}` — replace (rename allowed; new name must be free and not built-in)
- `DELETE /api/config/{scope}/tools/{name}`
- `POST   /api/config/{repo_id}/schema/{section}` — add; `section` ∈ `node_types|relationships` (anything else 404; `__global__` 404 — there is no global schema store)
- `PUT    /api/config/{repo_id}/schema/{section}/{name}` — replace
- `DELETE /api/config/{repo_id}/schema/{section}/{name}` (honours `?dry_run=1`)

"Save from global to a project" is just `POST|PUT /api/config/{repo_id}/tools[/{name}]` with the global entry's (possibly edited) YAML — the client picks PUT when the target repo already has that name, after a confirm ("replaces repo-a's own `hot_paths`").

**Success** `200`/`201`:
```json
{"ok": true, "written": true, "file": "devgraph.tools.yaml", "warnings": ["…"], "notes": ["…effect note…"], "scope": { …refreshed scope block… }}
```
(`written: false` for a dry run.)

**Errors** — FastAPI's existing `{"detail": …}` envelope; for config writes `detail` is an object so the UI can branch without parsing prose:
```json
{"detail": {"code": "invalid", "message": "devgraph.tools.yaml: tools.0.cypher: …", "scope": {…}}}
```
| status | code | when |
|---|---|---|
| 400 | `bad_request` | body not a JSON object, `yaml` missing/not str, YAML not one mapping, malformed YAML |
| 403 | `forbidden` | cross-site / cross-origin / non-loopback Host (2.3) |
| 404 | `not_found` | unknown scope, unknown entry, bad section, `__global__` schema |
| 409 | `exists` / `ambiguous` / `locked` | name taken, duplicate relationship, multiply-declared type, built-in tool name |
| 412 | `stale` | `If-Match` ≠ current fingerprint; body carries the current `scope` so the UI can show "file changed on disk — reload" |
| 413 | `too_large` | body > 64 KiB (checked on Content-Length then on read, like layout PUT, routes.py:489–495) |
| 415 | `media_type` | non-JSON body (same strict check as register, routes.py:276–281) |
| 422 | `invalid` | the resulting document fails validation (message = validator text, absolute repo path replaced by the file name) |
| 428 | `precondition_required` | `If-Match` missing |
| 500 | — | `OSError` on write: generic "could not write <file>" (no internals, as register does) |

### 2.3 Write-safety rules

1. **Browser-origin guard**: call `_reject_cross_site(request)` (routes.py:134) on every config write, plus the strict `application/json` check for routes with a body. PUT/DELETE also force a CORS preflight, which this app never answers.
2. **DNS-rebinding guard (new, required)**: `_reject_cross_site` compares `Origin` to `request.url.netloc`, which comes from the `Host` header — a rebinding page at `http://evil.test:8765` sends `Origin: http://evil.test:8765` and `Host: evil.test:8765`, so it passes. Add `_require_loopback_host(request)`: the Host's hostname must be `127.0.0.1`, `localhost`, `::1` or the configured `dashboard_host`. Apply to **all** `/api/config*` routes, GETs included (they return tool Cypher and repo paths). Keep it route-scoped in G2a (app-wide `TrustedHostMiddleware` would break every existing TestClient test that uses the `testserver` host; worth a follow-up issue).
3. **No client paths**: the target is `Path(record.path).resolve() / SCHEMA_FILENAME|TOOLS_FILENAME`, or `global_tools_path()`. Only those three file names are ever written. Scope must be an **active** registered repo (`registry.get`, `record.active`).
4. **Filesystem checks before write**: repo root `is_dir()`; target, if present, is a regular file and **not a symlink** (`os.replace` would replace the link with a file, silently diverging from what the link pointed at — refuse with 409 `not_regular`); the resolved target's parent equals the resolved root.
5. **Validate the whole resulting document** with the indexer's/tool plane's own parsers before writing (`parse_project_tools`, `parse_project_schema` + `resolve_declaration`, `save_global_tools`' internal validate); nothing is written on any failure; atomic temp-file + `os.replace`, mode preserved (existing `_write_atomically`).
6. **Built-in names locked**: tool add/rename to a built-in name → 409 `locked` (schema built-in labels are already refused by the schema validator).
7. **Fingerprint CAS** under a per-path lock (2.1). Residual race with an external editor between read and `os.replace` is milliseconds and is documented, not engineered around.
8. **Never commit or stage**; no subprocess, no git import on these routes. The success note says "Written to <file>; not committed."
9. **Size caps**: request body 64 KiB; per-entry YAML parsed with `yaml.safe_load` only.
10. Disabled project config: writes are allowed (files are the user's), response `notes` says they are not served/applied until enabled — the same wording as the CLI.

### 2.4 Frontend fit (devgraph/dashboard/static/index.html)

The app is a single page: the graph stage plus a **Settings overlay** with a left nav and panes (nav markup 616–624; panes from 626; switching handler 2861–2864 toggles `.active` on `#pane-<data-pane>`; deep-link pattern 2811/2860 clicks the nav button). The MCP telemetry is a card on the main page (`attemptMcpTelemetry`, 3495), not a pane.

- Add nav button `<button data-pane="config">Config</button>` right after "Repos", and `<div class="settings-pane" id="pane-config">`. Loading is lazy: fetch `/api/config` on first activation and after each successful write (re-render only the returned scope block).
- Layout per §10: `Global` card (Nodes / Relationships / Tools sub-lists), then one card per project (Nodes / Relationships / Tools). Rows reuse `.tool-row` (CSS 382), badges reuse `.pending-badge` / `.field label .badge` styling (367, 371) with a new `.cfg-badge.warn|info|error` modifier; hover detail through the existing `data-tip` + `wireTooltip` mechanism (764). Lock is an inline SVG with `title="Built-in — locked"`; the global flag is a small "GLOBAL" badge.
- Edit popup: reuse `.modal-overlay`/`.modal-box` (393–397) as a new `#configModal` (wider box): YAML `<textarea>`, destination `<select>` (only for global tools), warning panel, inline error area, Cancel / Save. Two-step for global entries and for dry-run warnings (button text changes to "Save anyway"). On 412, the modal keeps the user's text and offers "Reload" (refresh scope, keep textarea).
- All names/paths/messages rendered with `textContent` or `escapeHtmlVal` (2272) — config text is attacker-controllable via a cloned repo.
- Code organisation, matching the harness convention (tests grab top-level functions out of index.html by regex): pure functions `configBadgeText(badge)`, `renderConfigScope(scopeModel) -> Element`, `configWriteRequest(op, scope, section, name, yaml, fingerprint, dryRun) -> {url, init}`, `describeConfigError(status, detail) -> string`, plus one `openConfigEditor(...)` that does DOM wiring. Keep them inline in index.html (no new static file — the app has no build step and the tests read only index.html).
- Fold-in: add `{ id:"Container", cat:"…" }` to `BUILTIN_NODE_TYPES` (817) in `NODE_LABELS` order (after Repository), pick an existing category/colour (likely `service`), and update `SNAPSHOT_NODES` in tests/dashboard/schema_types_ui.js.

---

## 3. Key design questions — recommended answers

1. **Structured form vs YAML text area?** YAML text area per entry, pre-filled with `dump_entry` output (multi-line Cypher as `|` blocks, list_edit.py:32–45). It is exactly what `config tools edit` / `config schema edit` show in `$EDITOR`, so validation, messages and docs are shared and every field (parameters, sources, custom providers) is editable on day one. A form is a G2b nicety over the same routes.
2. **Concurrency with on-disk edits?** `If-Match` with a content fingerprint (`sha256:` of bytes, or `absent`) from the GET; 412 + current scope on mismatch; never auto-merge. Also refresh the open pane on the existing SSE stream (`/api/events`) if a reindex event arrives? Not needed — the CAS is the guarantee; a manual "Reload" is enough.
3. **What are "warnings before editing global entries"?** Clicking Edit/Delete on a global tool opens the modal in a warning state first: "Global tools are served in every registered repository's MCP sessions (N repos). Overridden in: repo-a." (from `resolve_tools` per repo). The destination dropdown defaults to "Global store"; choosing a repo changes the warning to "Writes a project override to repo-a/devgraph.tools.yaml; the global tool is unchanged." Built-in (locked) entries have no Edit button at all — only the lock and a tooltip.
4. **What does "save to a project" mean for node/relationship types?** Nothing in G2a: the global node/relationship lists are the built-ins, which cannot be redeclared in a project file. The destination dropdown appears only for global tools.
5. **Schema destructive changes?** Every schema write runs a dry run first (`dry_run: true`); if `warnings` is non-empty (nodes deleted, sources pruned, key change), the modal shows them and requires a second click. Same text as the CLI's follow-up warnings, so docs stay one story.
6. **Badges — computed where?** Server-side, from `resolve_tools(repo)` + file states, so the browser never re-implements resolution. Kinds: `locked-shadow` (warn: "ignored: shadows a locked tool; the fixed implementation is used"), `overrides-global` (info), `overridden` (info, on the global entry, lists repos), `fallback-global` (warn: project tool unservable, global used, with reason), `file-invalid` (error, with message; "no tools served" vs "last good kept" is per MCP session and is *not* claimed by the dashboard — say "MCP sessions keep their last good tools if they had any"), `not-served` (muted: project config disabled / repo inactive), schema `pending`/`never`/`invalid`.
7. **Scoped tool IDs?** In G2a, display-only: `tool_id` in the payload (`gl_<name>`, `<repo_id>_<name>`, bare name for built-ins) shown as a monospace subtitle. Writing them into telemetry is G2b because `record_tool_call` (devgraph/mcp/server.py:113) promises "no arguments — not even the repo_id", and the dashboard card says "no … repository" (index.html ~3487). Recommended G2b shape: add an `origin` field (`builtin|global|project`) and `tool_id` to the record, built in `_register_layers` where origin is known; record `<repo_id>_<name>` only for project tools and update both the docstring and the card note — an explicit, reviewed change to the privacy wording.
8. **Which repo list?** Active registered repos only (`list_repos(active_only=True)`); inactive ones are omitted, as the CLI treats them as unregistered (`_schema_record`, cli 2462).
9. **Error presentation?** The 422 message is the validator's own text (it already names the field path), shown verbatim (as text) under the textarea; the user's YAML is never discarded.
10. **New-entry template?** "Add" pre-fills a minimal valid skeleton (tool: name/description/cypher with `$repo_id`; node type: `label`, `key`; relationship: `type`, `from`, `to`), taken from `starter_schema_text()` (project_schema.py:725) where possible.

---

## 4. Test strategy

**Python unit — `tests/config/test_edits.py`** (new; no Neo4j): add/replace/delete for project tools, global store, node types, relationships; fingerprint mismatch → `stale`; invalid result → nothing written (bytes unchanged); comments preserved (re-use cases from tests/cli/test_config_tools_cli.py); symlink target refused; dry run writes nothing and returns warnings; built-in name refused. The existing CLI suites must stay green unchanged — they are the parity test for the extraction.

**`resolve_tools` — `tests/mcp/test_tool_plane.py`**: dry resolution's `origins`/`notices`/`shadowed` equal those of a real `_serve_repository` on the same fixtures (override, shadow, invalid-project fallback).

**Route tests — `tests/dashboard/test_config_routes.py`** (new). Use the `StubEngine` + tmp registry pattern of tests/dashboard/test_register_repo.py:60–104 (no Neo4j); stub `engine.read_applied_schema`. Point `get_settings().registry_db_path` at tmp so the global store is in tmp. Cover:
- GET model shape: built-ins locked (incl. `Container`), global flag, project entries, badges for override/shadow/fallback/invalid/disabled, `tool_id`s.
- Each write route happy path → file bytes as expected, response carries refreshed scope.
- Every error row of the table in 2.2 (400/403/404/409/412/413/415/422/428).
- Security: `Origin: http://evil.test`, `Sec-Fetch-Site: cross-site`, `Host: evil.test:8765` with matching Origin (rebinding) → 403 and file untouched; GET also 403 on bad Host; unknown/inactive repo → 404; `__global__` schema → 404; path-ish names (`../x`, URL-encoded slashes) can only ever be entry names, never paths — assert no file outside the repo changed; symlinked `devgraph.tools.yaml` → 409.
- No git: run in a real `git init` tmp repo and assert `git status --porcelain` shows the file as modified/untracked but the index is untouched (nothing staged).
TestClient base URL `http://127.0.0.1` for these tests so the Host guard passes by default.

**JS harness — `tests/dashboard/config_page_ui.js` + `test_config_page_ui.py`** following schema_types_ui.js / test_schema_types_ui.py (grab functions out of index.html, stub `fetch` and a minimal DOM, skip when `node` missing): renders global-then-projects order; lock and global flag present; badge text/tooltip from payload; hostile names (`<img onerror>`) rendered as text; `configWriteRequest` produces the right method/URL/`If-Match`/body; 412 path keeps textarea text and offers reload; dry-run warnings require a second confirm; global edit shows the warning step; destination dropdown lists repos and switches POST↔PUT by existence. Update schema_types_ui.js snapshot for `Container`.

**Live checks (before PR)**: start the headless agent against the dev Neo4j, open the dashboard in the browser (claude-in-chrome), with a throwaway registered repo: add a project tool → confirm a running `devgraph mcp` session serves it within 2 s and the file is uncommitted (`git status`); edit a global tool with "save to repo" → project override badge appears, `config tools list --repo` agrees; add a node type with a filesystem source → watcher applies after the quiet period (or `rescan --now`) and the graph legend shows it; edit the file in an editor while the modal is open → Save gives the 412 reload flow; `curl -H 'Host: evil.test:8765' -H 'Origin: http://evil.test:8765' -X POST …` → 403. Screenshot for the PR.

Docs: README dashboard section (Config page; "writes the file, never commits"), PROJECT_STATUS (G2a done, G2b list), and a short spec/plan pair under docs/superpowers/{specs,plans}/ matching G1's.

---


## Note on the Host guard

#40 now applies an app-wide Host allowlist to every dashboard route, so the route-scoped `_require_loopback_host` described above is unnecessary: the Config routes rely on the app-wide guard plus `_reject_cross_site` on every write.
