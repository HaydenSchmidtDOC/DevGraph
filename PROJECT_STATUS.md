# Project status

## Summary

- Phases 1-3 complete and verified (registry, graph engine, Python indexer, container/API/datastore extractors, watcher, CLI, tray app, docs extractor, git/PR/issue history). Phase 4 (enterprise federation) is design-only, intentionally unbuilt.
- Implementation Plan #5 (live web dashboard) shipped: a FastAPI+uvicorn server on its own daemon thread inside the tray process, serving a hand-written HTML/CSS/JS UI (Cytoscape.js vendored as a single pinned static file) at `http://127.0.0.1:8765` with live push over Server-Sent Events. Read-only, loopback-only, no auth (single-user local tool).
- 18 MCP tools live over stdio (`devgraph/mcp/server.py`), including `impact_analysis_for_diff` from Implementation Plan #3.
- `devgraph add`/`rescan` run a real full scan via `devgraph/indexer/dispatch.py`; the watcher and tray app route live changes through the same dispatcher, including delete cleanup.
- Known extraction gaps closed: `CALLS` edge extraction, Module nodes keyed by repo-relative path, absolute dotted intra-repo import resolution, a Tree-sitter node-identity bug, and service cross-linking (`Service -[:USES]->`, `Endpoint -[:CALLS]->`).
- Remaining known gap: bare single-segment imports satisfied via manual `sys.path` manipulation don't resolve (intentionally unfixed, see Implementation Plan #2 item 10).
- Container runtime is Podman, not Docker. Git is local-only; no remote configured.

## Commands

- **Canonical setup**: `.\scripts\bootstrap.ps1` — venv, editable install, Neo4j container, schema init, `devgraph doctor`. Idempotent.
- Manual equivalent: `python -m venv .venv` → `.venv/Scripts/python -m pip install -e ".[dev]"` → start Neo4j:
  `podman run -d --name devgraph-neo4j -p 127.0.0.1:7474:7474 -p 127.0.0.1:7687:7687 -e NEO4J_AUTH=neo4j/devgraph-local-dev -v devgraph_neo4j_data:/data -v devgraph_neo4j_logs:/logs docker.io/library/neo4j:5.26-community`
  (`deploy/podman-compose.yml` is an untested alternative for compose-provider setups.)
- Tests: `.venv/Scripts/python -m pytest` (269 tests)
- CLI: `devgraph add <path> [--full]`, `list`, `status`, `doctor`, `client-config`, `rescan --full`, `annotate`, `index-history`, `pr-source`/`issue-source`, `tray start/stop/status`

**Podman note**: `podman.exe` may not be on PATH by default even when installed — check `%LOCALAPPDATA%\Programs\Podman` first. Never install software to the host; dependencies live in `.venv/` and containers. Never touch containers/volumes not prefixed `devgraph-`.

## Structure

- `Blueprints/` — numbered design docs. `Design Brief #1.md` is the architecture source of truth. `Design Brief #2.md` is an unbuilt Phase 4 sketch. `Implementation Plan #2.md` covers rollout readiness + extraction quality (implemented). `Design Brief #3.md` and `Implementation Plan #3.md` cover response envelopes, `CALLS` scope narrowing, `impact_analysis_for_diff`, and a deferred embedding-search item (implemented except the deferred item). `Implementation Plan #5.md` covers the live dashboard (implemented).
- `devgraph/config/` — Pydantic settings; all security-sensitive defaults off (telemetry, cloud sync, cross-repo, `run_cypher`).
- `devgraph/registry/` — SQLite-backed repo allowlist (`RepoRegistry`), thread-safe via `RLock`. Stores `docs_path`, PR/issue opt-in flags, `last_indexed_commit`.
- `devgraph/graph/` — `GraphEngine` (Neo4j driver, idempotent MERGE, schema constraints, per-file delete cleanup) and `schema.py` (canonical labels/relationship types — import from here, don't hardcode strings).
- `devgraph/indexer/python/` — Tree-sitter Python extractor. Modules keyed by repo-relative path. Extracts `CALLS` (name-based, with `caller_class` metadata for `find_callers(scope_to_class=...)`), `CONTAINS`, `IMPORTS` (relative + absolute-dotted-intra-repo resolution), `EXTENDS`, docstrings (`description`/`docstring_full`), and `start_line`/`end_line` for `get_source`.
- `devgraph/indexer/containers/`, `apis/`, `datastores/` — Podman Compose, route, and datastore-usage extractors. Container extractor captures `build_context` for service cross-linking.
- `devgraph/indexer/dispatch.py` — routes changed/deleted files to the right extractor (`index_paths`, `remove_paths`, `full_scan`); called by CLI, watcher, and tray app. Prunes stale nodes on re-index; links services to owning code via `build_context`.
- `devgraph/indexer/docs/` — Markdown front-matter extractor for `Requirement`/`DesignDecision`/`ArchitectureNote`.
- `devgraph/indexer/git_history/` — incremental commit history via GitPython, local only.
- `devgraph/indexer/pr_issues/` — opt-in GitHub PR/issue ingestion; hard-gated per-repo, off by default.
- `devgraph/watcher/` — `WatcherManager`, registry-scoped file/git watching with debounce; handles atomic rename-saves correctly.
- `devgraph/mcp/` — 18 MCP tools (`tools.py`) + server wiring (`server.py`). Most tools return `{count, results, truncated}` envelopes capped by `max_results`. All tools are read-only except `run_cypher` (gated behind config, off by default). Serves `devgraph://client-guide` and `devgraph://tool-catalog` resources for client handover. Auto-starts/stops the tray app via refcounted holder tracking on connect/disconnect.
- `devgraph/cli/` — Typer CLI; see Commands above.
- `devgraph/agent/` — `lifecycle.py`: PID-file/liveness/holder-refcounting for the tray app, shared by CLI and MCP server. `tray.py`: pystray shell wiring watcher + indexer + Neo4j health check + heartbeat file + the dashboard's own daemon thread.
- `devgraph/dashboard/` — FastAPI app (`app.py`: `build_app()`), `/api/*` read routes (`routes.py`), dashboard-shaped Cypher helpers (`queries.py`), SSE pub/sub (`events.py`: `EventBroadcaster`), and `static/` (hand-written HTML/CSS/JS, no build step, plus vendored `cytoscape.min.js`). A second, independent read-only consumer of the same `GraphEngine`/`RepoRegistry` the tray owns — never routes through the MCP stdio server.
- `tests/` — mirrors `devgraph/`; 269 tests.
- `deploy/podman-compose.yml` — declarative alternative to the `podman run` setup command.
- `DEVGRAPH-CLIENT.md` — portable doc for another repo's coding assistant to connect to this DevGraph instance.
