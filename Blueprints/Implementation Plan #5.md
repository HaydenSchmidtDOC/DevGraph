# DevGraph — Implementation Plan #5: Live Dashboard (v1)

Build-ready plan for a tray-served, live-updating web dashboard: a repo
overview/browser plus an interactive graph canvas, delivered with zero
build tooling and only "airtight" dependencies (company-laptop policy — see
Context).

## Context

DevGraph has no visual surface today — everything is CLI/MCP-tool text
output. The user wants a "nice looking dashboard" as a first step toward a
broader GraphRAG-style UI (clustering, semantic search, etc., out of scope
here), with two hard constraints set in discussion:

1. **Company-laptop package policy.** Only "basic"/"absolutely air tight"
   packages may be installed — no unapproved/unusual dependencies. This
   plan adds exactly two new pyproject dependencies (`fastapi`, `uvicorn`,
   no extras) and vendors one frontend library as a single pinned static
   file (Cytoscape.js) rather than pulling in npm/a build step/a CDN at
   runtime.
2. **Tasteful, not "AI slop."** Visual direction is grounded in three
   concrete references researched and agreed with the user (not invented
   at build time):
   - **Linear's visual language** (via its published design-system
     analyses) — near-black dark surfaces, restrained 400–510 font
     weights, hairline 0.5px borders instead of shadow-based separation,
     one accent color, tight information density.
   - **GitHub Primer's public design-token *values*** (color/spacing/type
     scale, as documented at primer.style) as the numeric basis for our
     token file — we hand-write a small `tokens.css` using Primer's
     published scale rather than installing `primer/css` (npm/Sass-only,
     low-activity "KTLO" maintenance) or `primer/primitives` (generated
     build output) as a package.
   - **Tabler's layout conventions** (sidebar + topbar + content grid,
     responsive breakpoints) as a structural reference only — its default
     Bootstrap-y visual skin is not used; we restyle with our own tokens.

   No dependency or file is pulled from any of these repos at build or
   runtime — they are references a human read to inform hand-written CSS,
   which keeps the "airtight packages" constraint intact.

Decisions already made (do not re-litigate without a reason):

- **FastAPI + uvicorn, run inside the tray process**, not a separate
  process. `TrayApp.start()` launches a daemon thread that runs its own
  asyncio event loop hosting `uvicorn.Server` with
  `install_signal_handlers=False` (the tray's existing pystray main loop
  keeps owning the process's signal handling). `TrayApp._quit()` stops the
  uvicorn server before closing the engine/registry, same shutdown order
  already used for `WatcherManager`.
- **Bind to `127.0.0.1` only**, fixed local port (see Settings below). This
  is a single-user local tool per Design Brief #1 Principle 1 — no auth
  layer is added; network exposure is the sandbox, not a login.
- **Server-Sent Events (SSE), not WebSockets**, for live push. Starlette
  (already a FastAPI dependency) supports `StreamingResponse` with a
  `text/event-stream` media type natively — zero extra package. One-way
  push (server → browser) is all the dashboard needs.
- **No frontend framework, no build step.** Hand-written HTML/CSS/JS served
  as static files by FastAPI's `StaticFiles`. The one exception is
  Cytoscape.js, vendored as a single pinned, minified `.js` file checked
  into the repo (MIT licensed, no runtime dependencies, no transpilation
  needed to use as a `<script>` tag) — not installed via npm/CDN.
- **Both overview and interactive graph canvas ship in v1**, per the
  user's explicit answer ("Both from day one") — this is not staged into a
  v1/v2 split.
- **Data comes from `GraphEngine` directly**, not by looping the dashboard
  through the MCP stdio server (which is inherently 1:1 with a single
  MCP client's stdin/stdout per `tray.py`'s module docstring and
  `DEVGRAPH-CLIENT.md`). The dashboard is a second, independent consumer
  of the same `GraphEngine`/`RepoRegistry` instances the tray already
  owns — reusing query patterns from `devgraph/mcp/server.py` where
  sensible (e.g. `summarise_repository`'s aggregation shape) rather than
  calling through the MCP protocol.

---

## Architecture

```
TrayApp.start()
 ├─ WatcherManager.start()                (existing)
 ├─ health_check_loop thread              (existing, now also feeds SSE — see Item 4)
 └─ dashboard thread (new)
      └─ asyncio event loop
           └─ uvicorn.Server running devgraph.dashboard.app:build_app(engine, registry)
                ├─ GET  /                          -> static/index.html (shell)
                ├─ GET  /static/*                   -> StaticFiles (css/js/vendor)
                ├─ GET  /api/repos                  -> repo list + basic counts
                ├─ GET  /api/repos/{repo_id}/summary-> node/edge counts by label/type
                ├─ GET  /api/repos/{repo_id}/graph  -> nodes+edges for Cytoscape
                ├─ GET  /api/repos/{repo_id}/search -> component search (reuses search_component logic)
                └─ GET  /api/events                 -> SSE stream, live push
```

New package: `devgraph/dashboard/`
- `app.py` — `build_app(engine, registry, events) -> FastAPI`, wires routers + `StaticFiles`.
- `routes.py` — the `/api/*` handlers above.
- `events.py` — `EventBroadcaster`: a small in-process pub/sub (asyncio
  `Queue` per connected SSE client) that `TrayApp` pushes into whenever the
  graph changes.
- `queries.py` — thin Cypher helpers used by `routes.py` (graph slice for a
  repo, summary counts) — kept separate from `graph/engine.py` since these
  are dashboard-shaped read queries, not general-purpose engine methods.
- `static/index.html`, `static/css/tokens.css`, `static/css/app.css`,
  `static/js/app.js`, `static/vendor/cytoscape.min.js` (vendored, pinned
  version noted in a header comment with source URL and version).

### Settings additions (`devgraph/config/`)

Add to the existing `Settings` model (mirror how `health_check_interval_s`
etc. are already defined there):
- `dashboard_enabled: bool = True`
- `dashboard_host: str = "127.0.0.1"`
- `dashboard_port: int = 8765`

### pyproject.toml

Add under `dependencies`: `"fastapi>=0.115"`, `"uvicorn>=0.32"` (no
`[standard]` extras — pulling in `uvloop`/`httptools` C extensions is
exactly the kind of "not absolutely air tight" addition the constraint
rules out; the pure-Python asyncio loop is plenty for a single-user local
server).

---

## Item 1 — Dashboard FastAPI app + read endpoints

**Files:** new `devgraph/dashboard/app.py`, `devgraph/dashboard/routes.py`,
`devgraph/dashboard/queries.py`.

- `build_app(engine: GraphEngine, registry: RepoRegistry, events: EventBroadcaster) -> FastAPI`:
  constructs the app, mounts `/static` via `StaticFiles(directory=...)`,
  includes the API router, and serves `index.html` at `/`.
- `GET /api/repos`: `registry.list_repos()` plus, per repo, a cheap node
  count from `queries.count_nodes(engine, repo_id)`.
- `GET /api/repos/{repo_id}/summary`: node counts grouped by label and
  relationship counts grouped by type, scoped to `repo_id` — same shape of
  aggregation `summarise_repository` already does in
  `devgraph/mcp/server.py`; reuse that logic (extract to a shared helper if
  it's cheap to do without touching the MCP server's existing contract, or
  duplicate the query if extraction would risk that contract — judgment
  call at implementation time, favor not touching `mcp/server.py`).
- `GET /api/repos/{repo_id}/graph?label=...&limit=500`: returns
  `{"nodes": [...], "edges": [...]}` shaped for Cytoscape.js
  (`{data: {id, label, ...}}` per element) — capped at `limit` (default
  500, hard ceiling e.g. 2000) so a large repo's full graph doesn't hang
  the browser tab; `label` optionally filters to one node type (Function,
  Class, Module, Service, etc.).
- `GET /api/repos/{repo_id}/search?q=...`: wraps the same lookup
  `search_component` uses, returns lightweight `{id, name, label, file}`
  rows for the node browser's search box.
- All handlers validate `repo_id` against `registry.get(repo_id)` first and
  404 if unknown — same allowlist discipline as `mcp/server.py`'s
  `repo_id` handling, since this is a second entry point into the same
  engine.

**Content boundary reminder for whoever implements this:** these endpoints
return structure (ids, labels, counts, file paths, symbol names) — the
existing MCP tools already return this same class of data today, so no new
boundary is crossed. Do not add an endpoint that returns extracted
docstrings/summaries/full source text beyond what `get_source`-style tools
already expose, without checking with the user first.

## Item 2 — Live push (SSE)

**Files:** new `devgraph/dashboard/events.py`; modify
`devgraph/agent/tray.py`.

- `EventBroadcaster`: holds a set of `asyncio.Queue` objects (one per
  connected SSE client). `publish(event: dict)` puts the event on every
  queue (drop-oldest or bounded-queue policy if a slow client falls
  behind — don't let one stalled browser tab back-pressure the publisher).
  `subscribe()` / `unsubscribe()` register/deregister a client's queue.
- `GET /api/events` (in `routes.py`): a `StreamingResponse` with
  `media_type="text/event-stream"` that awaits the client's queue and
  yields `data: {json}\n\n` per event; sends a `: keep-alive\n\n` comment
  line periodically (e.g. every 15s) so intermediary buffering/idle
  timeouts don't silently drop the connection.
- **Wiring into existing tray logic** — two call sites already exist and
  just need one line added each:
  - `TrayApp._on_changes` (tray.py:65-86): after a successful
    `index_paths`/`remove_paths` + `mark_indexed`, call
    `self._events.publish({"type": "reindexed", "repo_id": repo_id, "changed": len(changed_paths), "deleted": len(deleted_paths)})`.
  - `TrayApp._check_registry_changes` (tray.py:101-123): after a
    successful `self._watcher.refresh()`, publish
    `{"type": "registry_changed"}` so the dashboard's repo list can
    refetch `/api/repos` without a manual reload.
- Threading note: `EventBroadcaster.publish` is called from the
  health-check thread and the watcher's debounce-timer threads (not the
  dashboard's asyncio loop thread). Use `asyncio.run_coroutine_threadsafe`
  against the dashboard loop (store a reference to it when the dashboard
  thread starts) to hand the event to the right event loop safely — do not
  touch `asyncio.Queue` directly from a non-loop thread.
- Frontend (`app.js`): opens `new EventSource("/api/events")`, on message
  re-fetches the affected repo's summary/graph slice (simple "refetch on
  signal" — no client-side diffing/patching in v1, that's a v2 concern if
  it turns out to matter).

## Item 3 — Tray wiring (start/stop the dashboard with the tray)

**Files:** modify `devgraph/agent/tray.py`.

- `TrayApp.__init__`: construct `self._events = EventBroadcaster()`; store
  `self._dashboard_loop: asyncio.AbstractEventLoop | None = None` and
  `self._dashboard_server: uvicorn.Server | None = None`.
- `TrayApp.start()`: if `self._settings.dashboard_enabled`, spawn a new
  daemon thread `dashboard_thread` running a small `_run_dashboard()`
  method that: creates a fresh asyncio loop, sets it as current via
  `asyncio.set_event_loop`, stores it on `self._dashboard_loop`, builds
  `uvicorn.Config(app, host=..., port=..., loop="asyncio", install_signal_handlers=False)`,
  constructs `uvicorn.Server(config)`, stores it on
  `self._dashboard_server`, and calls `loop.run_until_complete(server.serve())`.
  Log the bound URL at startup (`logger.info("dashboard on http://127.0.0.1:%d", port)`).
- `TrayApp._quit()`: before `self._engine.close()`, if a dashboard server
  is running, request shutdown (`self._dashboard_server.should_exit = True`)
  and give the dashboard thread a short join timeout — same
  best-effort-graceful-shutdown spirit as the rest of `_quit()`.
- Port-in-use handling: if binding fails (another instance already
  running, or the port is taken), log a warning and continue without the
  dashboard rather than crashing the whole tray — the dashboard is additive,
  the watcher/indexer loop is the tray's core job and must keep running
  regardless.

## Item 4 — Frontend shell: overview + node browser + graph canvas

**Files:** new `devgraph/dashboard/static/index.html`,
`static/css/tokens.css`, `static/css/app.css`, `static/js/app.js`,
`static/vendor/cytoscape.min.js`.

- **`tokens.css`**: hand-written CSS custom properties using Primer's
  published scale as the numeric source (cite `primer.style` in a header
  comment) for spacing (`--space-1` … `--space-8`, 4px base) and a
  restrained gray/near-black color ramp; layered with the Linear-derived
  visual choices agreed with the user: dark-first surface
  (`--color-canvas: #0b0c0d`-ish, adjust while eyeballing against Linear's
  documented `#08090a`), paper-white text at reduced opacity for secondary
  text, a single accent color for interactive/active state, `0.5px`
  hairline borders (`--border-hairline: 0.5px solid var(--color-border)`)
  used instead of box-shadows for card/panel separation, font-weight scale
  capped at 500 (no bold anywhere in chrome text).
- **Layout** (Tabler-inspired structure, not its skin): fixed left sidebar
  (repo list from `/api/repos`, click to select active repo) + top bar
  (search box wired to `/api/repos/{id}/search`, live-status dot fed by
  `/api/events` connection state) + main content area with two panels:
  overview (summary counts as a small stat grid, from `/api/repos/{id}/summary`)
  and graph canvas (Cytoscape.js instance below/beside it, loaded from
  `/api/repos/{id}/graph`).
- **Graph canvas**: Cytoscape.js with a simple `cose` or `breadthfirst`
  layout to start (no layout-tuning rabbit hole in v1), node color by
  label (Function/Class/Module/Service/etc. — small fixed palette drawn
  from the same token file), click-to-highlight-neighbors, no editing.
- **No client-side router/framework** — a single page, `app.js` does
  direct DOM manipulation (`document.querySelector`, template literals for
  the stat grid rows) and one `fetch`-based data layer function per
  endpoint. Keep it small and readable over clever.

## Item 5 — Tests

**Files:** new `tests/dashboard/test_routes.py`,
`tests/dashboard/test_events.py`.

- `test_routes.py`: use FastAPI's `TestClient` (ships with FastAPI, no
  extra dependency) against `build_app()` wired to a test `GraphEngine`/
  `RepoRegistry` (reuse whatever fixture pattern `tests/mcp/` already uses
  for a seeded test graph, if one exists — check before inventing a new
  one). Cover: repo list, summary counts against known seeded data, graph
  endpoint's node/edge shape and `limit` capping, search endpoint,
  unknown-`repo_id` 404s.
- `test_events.py`: unit-test `EventBroadcaster` in isolation (publish
  reaches subscribed queues, unsubscribe stops delivery, a slow/never-read
  queue doesn't grow unbounded) without needing a live server.
- Do **not** add a browser/E2E test in this pass — manual verification
  (start tray, open `http://127.0.0.1:8765`, confirm live update after
  editing a watched file) is enough for v1; revisit if the dashboard grows
  complex enough to justify Playwright etc., which would itself need a
  separate airtight-package conversation with the user.

## Item 6 — Docs

**Files:** `README.md`, `PROJECT_STATUS.md`.

- `README.md`: short "Dashboard" section — it starts automatically with
  the tray, URL, one screenshot-free description of what it shows (no
  screenshot required for this pass).
- `PROJECT_STATUS.md`: record the dashboard as shipped, matching this
  repo's existing convention of tracking what's implemented vs. planned.

---

## Explicitly out of scope for this pass

- Auth/multi-user access (single-user local tool, loopback-only bind is
  the boundary).
- Clustering/community detection, semantic/embedding search, LLM-generated
  summaries in the UI — these were discussed conceptually as later
  dashboard features, not part of v1.
- Editing the graph from the UI (read-only in v1).
- Any UI settings/persistence (theme toggle, layout memory, etc.) beyond
  the single dark visual direction agreed above.
- Packaging the dashboard as a separate installable/CDN-hosted asset —
  it's served only from the local tray process.
