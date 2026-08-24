# DevGraph — Implementation Plan
Companion to `Design Brief #1.md`. Covers all four roadmap phases; Phase 1 is
detailed to build-ready granularity, Phases 2-4 are staged blueprints to be
detailed in their own pass when scheduled.

## Context

The design brief specifies DevGraph, a local-first personal knowledge-graph
platform that lets a coding assistant query a Neo4j-backed architecture graph
of explicitly-mounted repositories instead of re-reading source on every
request. As of this plan, the project directory contains only the brief —
this is a greenfield build, nothing has been scaffolded yet.

Decisions already made (do not re-litigate without a reason):
- **Runtime**: System Tray Application for v1, not a Windows Service. Simpler
  lifecycle, visible status, no installer needed yet. A service can replace
  the tray shell later without touching registry/watcher/indexer/graph/MCP code.
- **Parsing**: Tree-sitter (not Python's `ast` module), per the brief's
  recommended stack — grammar-based, incremental-parse friendly, and the only
  option that generalizes to non-Python languages later.
- **Delivery order**: Phase 1 built and verified end-to-end before Phase 2
  starts. Phases 3-4 are not implemented in the first pass.

The graph is the source of truth; the AI is the interface. Every component
below is designed around that: MCP tool calls over raw Cypher, incremental
(save-triggered) reindexing over full rebuilds, and `repo_id` scoping as a
hard boundary, not a convention.

---

## Tech Stack (per brief)

- Python 3.13+
- Neo4j Community Edition (local, single instance, multi-tenant via `repo_id`)
- Official Neo4j MCP Server as the transport layer (DevGraph exposes tools on top)
- Tree-sitter (`tree-sitter`, `tree-sitter-python` grammar first)
- Watchdog (filesystem events)
- Typer (CLI: `devgraph add`, `devgraph remove`, `devgraph rescan`, ...)
- SQLite (local repo registry + job/index metadata — not graph data)
- Pydantic (schema/config models)
- `pystray` + `Pillow` (system tray icon/menu) for the always-on agent shell
- Podman Compose YAML parsers (`pyyaml` + light custom parsing)

### Proposed repo layout

```
devgraph/
  agent/           # tray app + orchestrator lifecycle
  registry/        # SQLite-backed repo registry (add/remove/enable/disable/rescan)
  watcher/         # watchdog-based git/file watcher, scoped strictly to mounted paths
  indexer/
    python/        # tree-sitter Python extractor
    containers/    # Podman Compose parsers
    apis/          # FastAPI/Flask/Django route extractors
    datastores/    # connection-string / client-usage detectors
  graph/           # Neo4j schema, upsert/merge logic, repo_id scoping
  mcp/             # MCP server exposing high-level tools (search_component, etc.)
  cli/             # Typer entrypoints
  config/          # Pydantic settings, security defaults (telemetry off, etc.)
tests/
pyproject.toml
```

---

## Phase 1 — Repository Graph, Code Structure, Container Structure, MCP Access

The only phase intended to become working code in the first implementation pass.

### 1. Repository Registry (`devgraph/registry/`)
- SQLite schema: `repos(repo_id TEXT PK, path TEXT, active BOOL, watch_enabled BOOL, last_indexed TIMESTAMP)`.
- Functions per brief: `add_repo(path)`, `remove_repo(repo_id)`, `enable_watch(repo_id)`, `disable_watch(repo_id)`, `rescan(repo_id)`.
- `add_repo` validates the path exists, is a git repo, and is not already registered; derives `repo_id` from folder name (with collision suffixing) unless explicitly overridden.
- **Security invariant**: the registry is the single allowlist. The watcher and indexer must only ever operate on paths pulled from this table — no path parameter from an MCP tool call may bypass it.

### 2. Git Watcher (`devgraph/watcher/`)
- One `watchdog.Observer` per active, watch-enabled repo, scoped to that repo's root only (no drive/profile-wide watches — this is a hard requirement from the brief, not a performance optimization).
- Watches for file save events plus `.git/HEAD` and `.git/refs` changes to detect commit/branch/merge/checkout/pull.
- Debounces bursts (e.g. 500ms) and enqueues a repo-scoped incremental index job rather than indexing per-event.
- Watcher list is rebuilt whenever the registry changes (add/remove/enable/disable), not on a poll loop.

### 3. Incremental Indexer (`devgraph/indexer/`)
- **Python extractor** (Tree-sitter): parse each changed `.py` file, extract module/class/function/decorator/import/inheritance nodes with source spans. Use Tree-sitter's incremental parsing so unchanged files are skipped and only diffed files are re-walked.
- **Container extractor**: parse `Containerfile`, `podman-compose.yml` (and `compose.yaml` variants) for container/image/network/volume/service definitions.
- **API extractor**: pattern-match FastAPI (`@app.get/post/...`, `APIRouter`), Flask (`@app.route`), Django (`urls.py` patterns) to pull endpoint/route/method/handler-function edges.
- **Datastore extractor**: detect client library usage/imports (`psycopg2`, `qdrant_client`, `redis`, `neo4j`, `pika`/`kombu`, `kafka-python`, etc.) and connection strings to link services to `Database`/`VectorStore`/`Queue` nodes.
- Full scan on first `add_repo`/`rescan`; subsequent runs only touch files reported by the watcher's changed-file set (diffed by mtime+hash against SQLite-tracked file state, so restarts don't force a full rebuild).

### 4. Graph Schema & Engine (`devgraph/graph/`)
- Node labels from the brief: `Repository`, `Container`, `Service`, `Module`, `Class`, `Function`, `Endpoint`, `Database`, `VectorStore`, `Queue`.
- Every node carries `repo_id`; `Repository` node is the scoping root.
- Relationships: `CONTAINS`, `CALLS`, `IMPORTS`, `USES`, `RUNS`, `WRITES_TO`, `READS_FROM`, `IMPLEMENTS`, `DEPENDS_ON`, `EXTENDS`.
- All writes are idempotent `MERGE` keyed on `(repo_id, stable_identifier)` so incremental reindexing updates in place rather than duplicating; deleted files trigger node/edge removal scoped to that file's provenance.
- Uniqueness constraints per label on `(repo_id, name)` (or equivalent), created on first startup.

### 5. MCP Layer (`devgraph/mcp/`)
Expose the brief's high-level tools (implemented as parameterized Cypher behind a stable tool interface, never raw Cypher passthrough by default):
`search_component`, `trace_request_flow`, `get_service_dependencies`, `find_callers`, `find_related_files`, `summarise_repository`, `compare_branches`, `impact_analysis`, `explain_architecture`, `list_services`.
- Default scoping: every tool call filters `repo_id` to the workspace currently open in the IDE (resolved via the registry's active repo or an explicit `repo_id` argument).
- Cross-repo mode: tools accept an explicit `cross_repo: true` opt-in flag; without it, results never leak across `repo_id`.
- A separate, clearly-labeled `run_cypher` advanced tool exists for direct queries, off by default in tool listings unless explicitly enabled in config.

### 6. DevGraph Agent / Tray App (`devgraph/agent/`)
- `pystray`-based tray icon with menu: status (running/paused), open registry, pause/resume watching, quit.
- On startup: reads registry, starts watchers for active repos, starts/health-checks the local Neo4j connection, starts the MCP server process.
- Health monitoring: periodic (e.g. 30s) check that Neo4j is reachable and watchers are alive; surface via tray icon state (ok/warning) — no external telemetry.

### 7. CLI (`devgraph/cli/`)
Typer commands: `devgraph add <path>`, `devgraph remove <repo_id>`, `devgraph list`, `devgraph rescan <repo_id>`, `devgraph watch enable|disable <repo_id>`, `devgraph status`.

### 8. Security defaults (`devgraph/config/`)
- Pydantic `Settings`: `telemetry_enabled: bool = False`, `allow_cross_repo: bool = False`, `cloud_sync: bool = False` — all default-off per brief; no env var should silently enable outbound network calls.
- No code path constructs a filesystem path for watching/indexing except by reading it from the registry.

### Verification (Phase 1)
- Unit tests per extractor (Python/container/API/datastore) against small fixture repos.
- Integration test: `devgraph add` a fixture repo → assert graph node/edge counts match expected schema.
- Incremental test: modify a fixture file → assert only affected nodes update (via `last_indexed`/node version check), not a full rebuild.
- MCP tool smoke test: call each of the 10 tools against the fixture graph and assert `repo_id` scoping holds (no cross-repo leakage without opt-in).
- Manual: run tray app, open VS Code on a mounted repo, ask Claude/Copilot the four "Success Criteria" questions from the brief and confirm graph-backed answers.

---

## Phase 2 — Requirements, Design Decisions, Architecture Notes

Extends the graph with human-authored intent, linked to the code graph.

- New node types: `Requirement`, `DesignDecision`, `ArchitectureNote`, sourced from a `devgraph/docs/` convention (e.g. Markdown files with front-matter) or explicit `devgraph annotate` CLI commands linking a note to a `Module`/`Service`/`Endpoint`.
- New relationships: `SATISFIES` (Module → Requirement), `DOCUMENTED_BY` (Service → ArchitectureNote), `DECIDED_BY`/`SUPERSEDES` for design-decision history.
- Indexer gains a docs-extractor watching a configured docs path per repo (still registry-scoped).
- New MCP tools: `explain_decision`, `find_requirements_for`, `trace_design_rationale`.
- Depends directly on Phase 1's file-watcher/registry infrastructure — no new watcher mechanism needed, just a new file-type handler plugged into the existing indexer dispatch.

## Phase 3 — Git History, PR Knowledge, Issue Tracking

- Git history extractor walks commit log (via `GitPython` or `pygit2`) incrementally (new commits since `last_indexed`), creating `Commit` nodes linked `MODIFIES` to `Module`/`Function` nodes touched, enabling "why did this change" queries.
- PR/issue ingestion is source-specific (GitHub/GitLab/Azure DevOps API) and must remain **opt-in per repo** (extends the registry schema with `pr_source_enabled`, `issue_source_enabled`) — this is the first component that talks to an external service. No automatic outbound calls without explicit per-repo configuration; this preserves Principle 2 (local-first, no cloud dependencies by default).
- New nodes: `Commit`, `PullRequest`, `Issue`; relationships: `MODIFIES`, `RESOLVES`, `REFERENCES`.
- New MCP tools: `blame_component`, `find_related_prs`, `issue_history_for`.

## Phase 4 — Enterprise Knowledge Federation (Optional)

- Explicitly optional per brief. Design as a pluggable federation layer that lets multiple developers' local Neo4j instances (or a shared central instance) merge graphs under a namespacing scheme extending `repo_id` scoping (e.g. `org_id/repo_id`).
- Requires its own opt-in gate (default off, matching Principle 2) and a sync protocol (push/pull of graph deltas) — sketch only, no implementation planned until real usage patterns emerge from Phases 1-3.

---

## Delivery Order

Build and verify Phase 1 completely (registry → watcher → indexer → graph →
MCP → tray → CLI) before starting Phase 2. Phases 3 and 4 remain design-only
blueprints here and should get their own detailed implementation-plan pass
when they're actually scheduled — the further-out phases are more likely to
shift once Phase 1 is in real use, so don't over-invest in their detail now.

---

## Handover Notes for a New Developer

**State of the project as of this plan**: nothing has been scaffolded. The
only files that exist are `Blueprints/Design Brief #1.md` (the source spec)
and this plan. There is no `pyproject.toml`, no `devgraph/` package, no repo
git-initialized yet. Read the design brief first — it's short and every
constraint in this plan traces back to a specific line in it.

**Non-negotiables baked into the brief — do not relax these for convenience:**
1. **No path outside the registry may ever be watched or indexed.** This is
   a stated security/privacy requirement (Principle 1), not a nice-to-have.
   If you're tempted to add a "scan this folder too" convenience shortcut
   anywhere (CLI, MCP tool, config default), don't — route it through
   `add_repo` instead.
2. **`repo_id` scoping is a hard boundary, not a UI filter.** Cross-repo
   query results must require an explicit opt-in flag on the MCP tool call.
   Test this — the plan's verification section calls out an explicit
   no-leakage smoke test for a reason.
3. **Telemetry, cloud sync, and outbound network calls default to off** and
   should require explicit user action to enable, per Principle 2. Phase 3's
   PR/issue ingestion is the first legitimate reason to talk to an external
   API — keep it opt-in per repo, not global.
4. **The AI should never need to write Cypher for normal use.** The 10 MCP
   tools listed in Phase 1 are the intended interface; `run_cypher` is an
   escape hatch, not the primary API. If you find yourself wanting to add
   "just let the AI query directly" as a shortcut, that's a sign a new
   high-level tool is missing, not that the escape hatch should become
   the default.

**Suggested build order within Phase 1** (matches dependency order, not the
numbering above): registry → graph schema/engine (so there's somewhere to
write) → Python indexer (the core extractor) → MCP layer with a couple of
tools working end-to-end against real data → watcher (wire up incremental
triggering) → container/API/datastore extractors → CLI → tray app last (it's
just a shell around everything else and is the easiest thing to fake/stub
while the rest is under development).

**Watch out for:**
- Tree-sitter's Python grammar needs to be pinned and vendored/installed
  deliberately (`tree-sitter-python` version compatibility with the
  `tree-sitter` core library has bitten people before — check compatible
  version pairs when adding the dependency).
- Neo4j Community Edition doesn't support multiple databases, which is
  exactly why every node carries `repo_id` instead of using separate
  databases per repo (see brief, Principle 3) — don't "fix" this by
  reaching for Enterprise-only multi-database features later without
  discussing it first, since that would break the stated design intent.
- Incremental reindexing correctness (idempotent `MERGE`, stale-node
  cleanup on file delete) is easy to get subtly wrong. Write the
  incremental test from the verification section early, not as an
  afterthought — it will catch duplicate-node bugs that a full-rebuild-only
  test suite won't.

**Open questions not yet resolved by the brief or this plan** — flag to the
project owner before making an irreversible choice:
- Exact `repo_id` derivation/collision policy when two mounted repos share a
  folder name.
- Where the "official Neo4j MCP Server" fits exactly — brief calls it out as
  recommended stack, but Phase 1 above assumes DevGraph's own MCP layer is
  what the IDE/assistant talks to. Confirm whether DevGraph should run its
  own MCP server process (as planned) or extend the official one.
- Local Neo4j deployment mechanism (bundled/managed by the tray app vs.
  developer-installed prerequisite) isn't specified in the brief — decide
  before building the agent's health-check/startup logic.
