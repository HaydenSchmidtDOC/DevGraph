# Schema-driven graph rendering on the dashboard — design

Upstream issue: HaydenSchmidtDOC/DevGraph#1, §10 ("Graph view"). Stacked on
#35. First half of the dashboard slice (G1); the Config page is G2.

## Goal
The dashboard graph view takes each repository's node and relationship types and their colours from its
schema instead of hardcoded lists. Declared types (for example `File`, `Folder`, `IS_CHILD_OF`) show with
their own colour, count, isolate toggle, `?label=` filter and search. Switching repos swaps the type set
cleanly. A repo with no schema file looks exactly as it does today.

## In scope
- New read-only route `GET /api/repos/{repo_id}/schema` that returns
  `{node_types:[{label, origin: builtin|project, color, count}], relationship_types:[{type, origin, color}], schema_state: applied|pending|never|absent|invalid|disabled, notices:[]}`. For `__all__` the state is the most attention-needing one across repositories (invalid, pending, never, applied, disabled, absent).
- An optional display-only `color` field (`#rrggbb`) on `NodeTypeDecl` and `RelationshipDecl`, validated by
  pattern and included in the JSON Schema. It flows through `config schema add/edit --from` and `config show`
  unchanged.
- The frontend builds `NODE_TYPES`/`REL_TYPES` from that route on boot and on every repo change. Built-ins keep
  their current `CAT_COLORS`. User labels get the declared colour, or else a deterministic colour hashed from
  the label. Isolation is keyed by label, not by the shared `cat`.
- `repo_graph` accepts declared labels for that repo. `queries.search` includes the declared labels.
- Update README line 100 and PROJECT_STATUS.

## Out of scope
- The Config page and any edit, add, delete or reset UI (that is G2).
- Scoped tool IDs (follow-up c, also G2).
- Per-relationship styling beyond colour.
- Any change to the schema hash.

## Files touched
- `devgraph/dashboard/routes.py`: the new route, and the label check at line 286.
- `devgraph/dashboard/queries.py`: `_SEARCHABLE_LABELS` (line 20) and per-label counts (`summary_counts` already
  groups over any label).
- `devgraph/dashboard/static/index.html`: `NODE_TYPES`/`REL_TYPES`/`CAT_COLORS` (812-848), the count rows and chips
  (930-960), `mapGraphResultToElements` (2254), `currentStateQuery` (2354), autocomplete (2860), 3438-3450, and the
  repo `change` handler (2753).
- `devgraph/config/project_schema.py`: the `color` field on `NodeTypeDecl` (164) and `RelationshipDecl` (265),
  plus starter text (709).
- `devgraph/graph/engine.py`: read only. It already has `read_applied_schema` (558).
- Tests: `tests/dashboard/test_routes.py`, `tests/config/test_project_schema.py`, and a new
  `tests/dashboard/schema_types_ui.js` with `test_schema_types_ui.py`. These follow the existing
  `mcp_telemetry_ui.js` pattern: node-driven and skipped when node is not installed.
- Docs: `README.md`, `PROJECT_STATUS.md`.

## Decisions
1. **Where do the type lists come from: the file or the graph?** Build them from the **applied** schema, meaning
   `schema_labels`/`schema_relationship_types` on the Repository node via `read_applied_schema`, merged with
   the built-ins. That is what is actually in the graph. Take colours from the file, falling back to the hash
   colour. Report `schema_state` so the UI can show a "schema change pending rescan" hint. Do not show types
   that exist only in a pending file.
2. **Should a colour edit trigger a full rescan?** Today it would, because the hash covers the raw bytes
   (`schema_file_hash`, `project_schema.py:502`). Keep it that way. Excluding display fields from the hash
   would change every stored hash and force a one-time rescan on upgrade, and a rescan is correct, only
   redundant. Document the cost. Revisit only if users complain.
3. **What should `__all__` (the all-repos view) show?** The union of the built-ins and every enabled
   repository's applied types. If two repos give the same label different colours, the first registered repo
   wins.
4. **What about the `cat` model in the JS?** Keep `cat` for built-ins so the CSS and colours stay unchanged.
   Set `cat = "user:" + label` for user types, so isolation and highlighting work per label without
   restructuring.
5. **Is there a security concern?** Labels are already restricted to an identifier pattern (`LABEL_PATTERN`),
   but the JS interpolates the label into Cypher (`currentStateQuery`). Keep using `escapeCypherStr`, and have
   the route only accept labels the schema allows.

## Test strategy
- Route tests (live Neo4j, using the existing `require_neo4j` style):
  - A repo with no file returns exactly the built-in list.
  - A repo with File/Folder applied returns them as `project` with colours.
  - A pending schema reports `pending` and leaves out the new label.
  - `?label=File` is accepted for that repo and rejected (400) for a repo that does not declare it.
- Schema tests: `color` is accepted, a malformed colour is rejected with a clear message, and the JSON Schema
  includes the field.
- JS harness:
  - Given stubbed `/schema` responses for two repos, switching repos rebuilds the rows and chips.
  - User labels get distinct, stable colours.
  - Isolating `File` produces `labels(n)[0] = "File"`.
  - The no-file payload renders the same rows as today's constants, as a snapshot of the 16 labels and 18 rel
    types.
- Manual check with the `run` skill on a scratch repo that has the worktree schema: take a screenshot with the
  repo switched.

## Task split
1. **Schema `color` field.** Add it to the model, the JSON Schema and the starter text, and make `config show`
   and `list --json` echo it, with tests.
2. **`/api/repos/{id}/schema` route.** Cover applied plus built-ins, colours, counts, `schema_state`, and the
   `__all__` union, with route tests.
3. **Make the backend label-aware.** Have `repo_graph` and dashboard search accept and search the declared
   labels, with tests.
4. **Frontend.** Replace the hardcoded `NODE_TYPES`/`REL_TYPES` with the fetched data, key isolation per label,
   add a pending-schema hint, and add the node JS harness test.
5. **Docs and verification.** Update README and PROJECT_STATUS, run the full suite, and do the manual
   dashboard check.
