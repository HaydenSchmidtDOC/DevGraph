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
- **Within the container, the dashboard runs as its own OS process, not a
  thread.** This was revisited once the Observability section (below) added
  a write path into the dashboard: every MCP tool call now fire-and-forget
  POSTs a query-event to it, which gets persisted to SQLite. The watcher is
  load-bearing (every MCP query depends on the graph it keeps fresh); the
  dashboard is not. A bug in the dashboard's event-ingestion or aggregation
  path (an unhandled exception in the SQLite writer, a stuck query) must
  not take the watcher down with it — sharing a thread inside the watcher's
  process would let that happen. Two sibling processes in one container
  (e.g. via a small supervisor or the entrypoint script backgrounding both)
  gets independent crash isolation and separate log streams without any of
  the overhead a whole new container would add.
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
| Per-tool enable/disable beyond `run_cypher` | **Decided: build it.** New capability — every tool beyond `run_cypher` is currently always-registered in `mcp/server.py`; add a per-tool boolean alongside `enable_run_cypher` and gate tool registration on it, surfaced as one row per tool in the dashboard. Propagation to already-running MCP processes is via the shared config/registry store each process already reads independently (same pattern as `watch_enabled` today) — not a live push from the dashboard, since there's no existing channel for the dashboard to reach into a running MCP process, and building one would be new complexity the shared-store read already avoids. |
| Per-model/per-client MCP access control | **Skipped for this pass.** The MCP server is stdio-transport, spawned 1:1 per connecting client, with no allowlisting concept anywhere in the codebase — a real implementation needs a transport rethink first. Stays a separate, later spec; not folded into the dashboard rebuild. |

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
| Password/auth on the dashboard | **Skipped for this pass.** Zero auth middleware exists in `devgraph/dashboard/app.py` today, and none is being added. Stays loopback-only (`127.0.0.1` default); if `dashboard_host` is ever changed to a non-loopback address, that's the trigger to revisit auth as its own piece of work — not before. |
| IP allowlist beyond the bind address | Not being built this pass — same reasoning as auth above. |

### 6. Neo4j

| Feature | Status |
|---|---|
| `neo4j_uri` / `neo4j_user` / `neo4j_password` | Exist as settings, env-var/`.env`-only; never surfaced in CLI or dashboard. **Decided: display-only, "restart required" to change.** Confirmed in code, not just inferred: `GraphEngine.__init__` (`graph/engine.py:87-88`) builds one `neo4j.Driver` at construction and never rebuilds it, and every process that touches the graph (dashboard, tray, headless watcher, each spawned MCP server) holds its own separate `GraphEngine` instance built from its own settings read. A live-reconnect on the dashboard's driver wouldn't propagate to the others regardless — a runtime edit can't actually take effect process-wide without a restart, so there's no safe editable path to build even if wanted. |

### 7. Global settings

| Feature | Status |
|---|---|
| `telemetry_enabled` | Exists, env-var-only |
| `cloud_sync` | Exists, env-var-only |
| `mentions_ambiguous_mode` | Exists, env-var-only |
| `git_recency_track_author` | Exists, env-var-only |
| `registry_db_path` | Exists, env-var-only; likely display-only in any UI (changing it live is not a safe runtime operation) |

### 8. Observability

New section — none of this exists today. Motivation: watching what the AI
is actually doing to the graph in real time (trust/debugging), not graph
exploration.

| Feature | Status |
|---|---|
| Git history timeline (commits/authors/timestamps per repo) | New UI — data already collected by the sync engine for Plan #7 recency tracking, no new collection needed |
| Entity count readouts (per-label `COUNT`) | New UI — trivial query against the existing Neo4j connection |
| Live query log (exact tool call + its Cypher, per invocation) | New capability — see below |
| Node "light up" on query hit | New capability — depends on the same event source as the query log |
| Cypher query count over time (line graph) | New UI — aggregation over the persisted query log |

**Query log persistence.** Must survive dashboard restarts and back-date
before "since dashboard started" — this is a durable log, not an in-memory
buffer. Source: instrument the MCP tool wrapper layer in
`mcp/server.py` (one choke point, all tools pass through it) to capture
`{tool_name, repo_id, cypher_text, matched node ids/labels, timestamp}`
per invocation, plus the same instrumentation on the dashboard's own
internal queries (search, graph load) so the log is complete, not
MCP-only.

Two problems this creates that graph rendering doesn't have:

1. **Cross-process delivery.** Each MCP client (Claude Code, an IDE, etc.)
   spawns its own `mcp/server.py` process with its own `GraphEngine` —
   separate from whatever process is running the dashboard. Query events
   have to leave that process to reach the dashboard and get persisted.
   Proposed: each `GraphEngine`/MCP process fire-and-forget POSTs the
   event to the dashboard's HTTP API; the dashboard is the single writer
   to the log store. If the dashboard isn't running, the event is dropped
   (no local buffering/retry in the MCP process — keeps that side simple,
   and a live dashboard is already required for the highlight feature to
   mean anything).

   **Transport: HTTP POST with a persistent keep-alive client, not
   websocket.** Considered and rejected websocket for this link —
   reasoning below, since it's not obvious given how much else in this
   design is push-based:
   - The traffic shape is one-shot, one-directional events (tool call
     happened → notify), not a continuous stream — even a
     high-frequency agentic session firing tool calls back-to-back is
     bursts of a few events/second, not sustained throughput. That's
     within reach of plain HTTP; it doesn't need a protocol built for
     high-frequency framing.
   - A websocket requires the connection to be *maintained* for the
     MCP process's whole lifetime: reconnect/backoff logic if the
     dashboard restarts mid-session, and a wait-and-retry story if the
     dashboard isn't up yet when the MCP process starts. `mcp/server.py`
     processes are spawned per-client and can be short-lived — that
     lifecycle-management cost buys nothing here since events tolerate
     being dropped anyway.
   - Each MCP process holds one persistent HTTP client
     (`requests.Session()`/`httpx.Client()`) for its lifetime and reuses
     it across POSTs, so repeat calls aren't paying a fresh TCP+HTTP
     handshake each time — this gets the same connection-reuse
     efficiency a websocket would provide, on loopback, without any of
     the reconnect complexity.
   - Set a short client-side timeout (~200ms) on every POST so a slow or
     unresponsive dashboard can never block or slow down the MCP tool
     call the user is actually waiting on.
   - This is a fully separate wire from MCP protocol itself. MCP
     protocol exists only between an `mcp/server.py` process and its one
     connected client, over stdio — the dashboard is never a party to
     that. The query-event POST is the MCP process acting as an ordinary
     HTTP client toward an internal service, as a side effect of
     handling a tool call — the same pattern as a request handler that
     also emits a metrics/webhook ping. It doesn't extend or dual-purpose
     MCP itself.
   - The dashboard's other two channels are unrelated to this one:
     dashboard→browser stays SSE (unchanged, pre-existing), and
     browser↔dashboard for settings/repo-management (section 2/3) is
     ordinary request/response HTTP — a settings save has never needed a
     push, so it was never a websocket candidate either. Four channels
     total in this design (stdio MCP session, MCP→dashboard POST,
     browser↔dashboard REST, dashboard→browser SSE), each doing exactly
     one job, none of them crossed.
2. **Storage.** This is telemetry, not graph structure — doesn't belong in
   Neo4j as node data. Proposed: a local SQLite file alongside
   `registry_db_path`, one row per query event, indexed by timestamp for
   the line graph and by repo for filtering. Needs a retention policy
   (row cap or age-based prune) so it doesn't grow unbounded.
   **Decided: age-based prune, default 30 days, configurable via a
   setting** (mirrors the `registry_db_path`-adjacent settings pattern —
   env var, not a dashboard control in this pass). A background sweep on
   dashboard startup (and periodically thereafter) deletes rows older
   than the cutoff.

Node highlighting and the log panel both read from this same store/stream;
highlighting additionally requires the matched node to already be in the
dashboard's currently-loaded graph slice (no live fetch-and-insert for
off-screen nodes in this pass).

## Open questions

1. **UI framework choice** — being decided directly in Open Design, working
   from the codebase and this spec, in parallel with this doc. Not blocking
   the rest of this spec; fold the outcome in once decided.

All other previously-open items are resolved above: auth/per-model MCP
access are skipped for this pass (see sections 3 and 5), per-tool
enable/disable is being built (section 3), and Neo4j settings are
display-only with a restart-required note, confirmed against the code
(section 6).

## Explicitly out of scope for this pass

- Building the per-model MCP access control system itself (design question
  only, implementation is a separate future spec).
- Adding dashboard auth or an IP allowlist beyond the loopback bind address.
- Any visual/layout decisions — owned by the Claude Design/Open Design pass.
- Changing where Neo4j credentials live (still `.env`/env vars) or making
  them live-editable.
