# DevGraph Dashboard Rebuild — Design

Status: draft, in review with user. Covers hosting/lifecycle, graph
rendering, and the full configuration surface for a from-scratch dashboard
rebuild. UI visual design is being done separately in Claude Design — this
doc scopes what the UI needs to expose and control, not what it looks like.

## Why rebuild

The current dashboard (`devgraph/dashboard/`) has two problems:

1. **Unwanted "regeneration."** `app.js` destroys and rebuilds the entire
   Cytoscape.js graph — including a full `cose` force-layout recompute —
   on every `reindexed` SSE event (`renderGraph()`, called from
   `connectEvents()`'s message handler). With Plan #7's new git-state
   watcher now triggering syncs on every commit/branch change, this makes
   the graph visibly jump around while someone is looking at it. This is a
   usage bug (destroy/rebuild instead of diff/patch), not a library
   limitation.
2. **Process coupling.** Dashboard startup (`_run_dashboard` in
   `devgraph/agent/tray.py`, mirrored in `headless.py`) is owned entirely by
   whichever watcher process happens to be running. Kill the tray, the
   dashboard dies — there's no reason for that coupling to exist.
3. **Thin, ungoverned UI.** No auth, no way to filter the graph beyond one
   label, and the vast majority of DevGraph's actual configuration surface
   (repo management, MCP tool exposure, watcher control, global settings)
   is CLI-only or env-var-only today, invisible to the dashboard entirely.

Decision: bin the current dashboard's frontend and backend routes, keep
nothing but the underlying `GraphEngine`/`RepoRegistry` it already reads
from, and design the replacement properly.

## Decisions already made

### Hosting & lifecycle

Dashboard startup becomes its own reusable entry point (`devgraph dashboard
[--host HOST] [--port PORT]`), not a method the tray/headless agent calls on
itself.

- **Windows/tray**: tray keeps starting it as a daemon thread by default
  (today's convenience, zero extra process) — but it's no longer
  architecturally required. `devgraph dashboard` also works standalone from
  a terminal, tray running or not.
- **Docker stack**: dashboard keeps living inside the existing `devgraph`
  agent container — not Neo4j's container, not a new one. Rejected running
  it inside the Neo4j container outright: that image has no Python runtime,
  and one-process-per-container is the right default for restart/log/
  resource-limit isolation between "database" and "our web app." Rejected a
  brand-new dedicated container too: the actual complaint was tray
  *coupling*, not container topology, and a new container buys nothing here
  once the entry point is decoupled — same effective RAM/CPU either way. If
  cost/isolation ever justifies genuinely separate lifecycle management
  later, it's a one-line `docker-compose.yml` service pointing at the same
  entry point; nothing about this design blocks that.
- Access model stays what it is today: bind to `dashboard_host:
  dashboard_port` (default `127.0.0.1:8765`), open that in a browser. No
  change to the fundamental "it's a local website" shape.

### Graph rendering library

**Cytoscape.js** (already embedded, MIT-licensed) over Neo4j's own NVL:

- Independent GraphRAG/knowledge-graph projects surveyed (Microsoft
  GraphRAG's own visualizer, several GraphRAG UI forks, an open-source
  "knowledge graph" UI component, open-source semantic-search graph
  explorers) consistently reach for D3.js or Cytoscape.js — not NVL. NVL
  usage in the wild is essentially confined to Neo4j's own products
  (Bloom).
- NVL ships under Neo4j's own custom license (not MIT/Apache) and sends
  telemetry to Segment by default — every init call needs
  `disableTelemetry: true`, and one missed launch path (tray thread, bare
  CLI, Docker) silently leaks, conflicting with `telemetry_enabled: bool =
  False`'s local-first default. A `Content-Security-Policy: connect-src
  'self'` header would close that gap path-independently if NVL were ever
  chosen later, but it's an extra piece of infra Cytoscape.js doesn't need
  at all.
- Cytoscape.js's real weakness (framerate degradation past ~10k elements)
  isn't a practical concern for a single repo/module/service view.

**Action item before implementation**: pull a reference configuration from
an existing Cytoscape.js + graph-database dashboard setup (layout options,
stylesheet structure, incremental-update pattern) rather than building the
integration from a blank page. Specifically confirm the fix for the
regen bug: diff-based updates (`cy.add()`/`cy.remove()`/`cy.json()`
targeting only changed elements) plus `cose` with `randomize: false` so a
partial reindex doesn't reposition unaffected nodes — this is the
documented Cytoscape.js pattern for "update without visual disruption,"
not a new invention.

### UI framework

Visual design happening separately in Claude Design first; this doc doesn't
block on that. Whether the implementation stays framework-free (current
convention, hand-written HTML/CSS/JS, no build step) or introduces a light
framework is an open question to resolve once the visual design exists and
its component needs are known (see Open Questions).

## Full configuration & feature surface

Everything the dashboard should be able to show or control. Grouped by
area; each item notes whether it's genuinely new capability or just
surfacing something that already exists CLI-side/env-var-side today.

### 1. Graph view

| Feature | Status |
|---|---|
| Filter by entity type (any of the 17 `NODE_LABELS` in `graph/schema.py`) | Partial today — single-label filter exists (`routes.py`'s `repo_graph`), needs multi-select |
| Filter by relationship type (any of the 18 `RELATIONSHIP_TYPES`) | New — no relationship filter exists today |
| Filter by repo | Exists (repo selector already in the UI) |
| Filter by name/description substring | Exists (`search_components`) |
| Filter by `modified_within_commits` (Plan #7 recency window) | New — staged data exists, dashboard never queries it |
| Cross-repo view toggle | New — `allow_cross_repo`/per-tool `cross_repo` exist at the engine/MCP layer, dashboard never exposes it |
| Node/edge count ceiling per view | Exists but hardcoded (`_GRAPH_LIMIT_CEILING = 2000`); should be a visible, adjustable control |
| Layout choice/tuning | Exists but hardcoded to `cose`; expose layout name + key parameters |
| Live-update behavior (auto-refresh on reindex vs. manual refresh) | New control — today it's always-on and is exactly what causes the regen complaint; the fix (diffed updates) makes always-on safe, but a manual-refresh option is cheap to add alongside it |

### 2. Repo management

| Feature | Status |
|---|---|
| Add a repo | New to the dashboard — CLI-only today (`devgraph add`) |
| Remove a repo | New to the dashboard — CLI-only today (`devgraph remove`) |
| Toggle `watch_enabled` per repo | New to the dashboard — CLI-only today (`devgraph watch enable/disable`) |
| Toggle `mentions_enabled` per repo | New to the dashboard — CLI-only today (`devgraph mentions enable/disable`) |
| Toggle `pr_source_enabled` / `issue_source_enabled` per repo | New to the dashboard — CLI-only today |
| Set/clear `docs_path` per repo | New to the dashboard — CLI-only today |
| Trigger rescan / `index-history` on demand | New to the dashboard — CLI-only today |
| View `last_indexed`, `last_indexed_commit`, per-label node counts | Partial — node counts already shown; the two timestamps aren't |

### 3. MCP tool exposure

| Feature | Status |
|---|---|
| `enable_run_cypher` global toggle | Exists as a setting, never surfaced in any UI |
| Per-tool enable/disable beyond `run_cypher` | Doesn't exist anywhere — every other tool is always registered; would be new capability if wanted, not just new UI |
| Per-model/per-client MCP access control | **Does not exist at all.** The MCP server is stdio-transport, spawned 1:1 per connecting client, with no allowlisting concept anywhere in the codebase. This is net-new design work (see Open Questions), not a config value waiting to be surfaced. |

### 4. Watcher / tray

| Feature | Status |
|---|---|
| Per-repo watch on/off | Exists (`watch_enabled`), CLI-only today |
| Global watcher/tray start-stop | Exists as `devgraph tray start/stop`, CLI-only; there's no single "global watch" flag independent of per-repo flags — a dashboard control here should call the same start/stop path the CLI does, not invent a new global setting |
| `watch_debounce_ms` | Exists as a setting, never surfaced |
| `health_check_interval_s` | Exists as a setting, never surfaced |

### 5. Network / access

| Feature | Status |
|---|---|
| `dashboard_host` / `dashboard_port` | Exist as settings, never surfaced anywhere (including CLI) |
| Password/auth on the dashboard | **Does not exist.** Zero auth middleware in `devgraph/dashboard/app.py` today. Net-new (see Open Questions). |
| IP allowlist beyond the bind address | Doesn't exist; bind address itself (`127.0.0.1` by default) is the only current access control |

### 6. Neo4j

| Feature | Status |
|---|---|
| `neo4j_uri` / `neo4j_user` / `neo4j_password` | Exist as settings, env-var/`.env`-only; never surfaced in CLI or dashboard |

### 7. Global settings

| Feature | Status |
|---|---|
| `telemetry_enabled` | Exists, env-var-only |
| `cloud_sync` | Exists, env-var-only |
| `mentions_ambiguous_mode` | Exists, env-var-only |
| `git_recency_track_author` | Exists, env-var-only |
| `registry_db_path` | Exists, env-var-only; likely display-only in any UI (changing it live is not a safe runtime operation) |

## Open questions

1. **Auth mechanism.** If the dashboard gets a password, what's the
   threat model? `127.0.0.1`-only binding already stops anything off-box.
   Auth only starts to matter if `dashboard_host` is ever changed to
   `0.0.0.0`/a LAN address — worth deciding whether to (a) add real auth
   now, (b) refuse to bind to a non-loopback address without a password set
   (fail closed), or (c) leave it loopback-only and drop the password idea
   entirely until multi-machine access is an actual requirement.
2. **Per-model/per-client MCP access control.** Needs its own design pass —
   what does "which models can access MCP" even mean given the current
   1:1 stdio-per-client architecture? Likely requires rethinking the
   transport (stdio can't easily carry an identity to check against an
   allowlist) before any UI for it makes sense. Recommend treating this as
   a separate, later spec rather than folding it into the dashboard
   rebuild.
3. **Per-tool enable/disable.** Is this actually wanted, or does
   `enable_run_cypher`-style handling (the one tool that's genuinely
   dangerous) cover the real need? Enumerating this in the config surface
   doesn't mean building a settings row per tool.
4. **UI framework choice** — deferred until the Claude Design visual design
   exists (see UI framework decision above).
5. **Neo4j settings surfaced read-only vs. editable** — changing
   `neo4j_uri`/credentials at runtime while the engine holds a live
   connection isn't a simple form-save; likely display-only with a
   "restart required" note, but worth confirming.

## Explicitly out of scope for this pass

- Building the per-model MCP access control system itself (design question
  only, implementation is a separate future spec).
- Building per-tool enable/disable beyond the existing `enable_run_cypher`.
- Any visual/layout decisions — owned by the Claude Design pass.
- Changing where Neo4j credentials live (still `.env`/env vars).
