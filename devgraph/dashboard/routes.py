"""The dashboard's `/api/*` read endpoints.

Every repo-scoped handler validates `repo_id` against the registry first and
404s if unknown -- the same allowlist discipline `mcp/server.py` applies,
since this is a second entry point into the same engine/registry the tray
already owns (see Implementation Plan #5's "Data comes from GraphEngine
directly" decision).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from devgraph.config.settings import get_settings
from devgraph.dashboard import queries
from devgraph.dashboard.events import EventBroadcaster
from devgraph.dashboard.git_info import get_git_log, get_git_status
from devgraph.dashboard.layout_store import load_layout, save_layout
from devgraph.dashboard.query_log import QueryLog
from devgraph.graph.engine import GraphEngine, identity_key
from devgraph.graph.schema import NODE_LABELS
from devgraph.mcp import tools as devgraph_tools
from devgraph.registry.store import RepoRegistry

logger = logging.getLogger(__name__)

_GRAPH_LIMIT_DEFAULT = 500
# Hard ceiling so a large repo's full graph can't hang the browser tab, per
# Implementation Plan #5 Item 1.
_GRAPH_LIMIT_CEILING = 2000
# A saved layout has at most one entry per node the canvas could ever have
# fetched, i.e. _GRAPH_LIMIT_CEILING entries; budget generously per entry
# (a long identity_key plus an [x, y] pair) and round up, so a legitimate
# full-graph save never gets rejected while a malformed/hostile PUT still
# can't write an unbounded file to disk.
_LAYOUT_PAYLOAD_LIMIT_BYTES = _GRAPH_LIMIT_CEILING * 1024
_SSE_KEEPALIVE_S = 15


def build_router(
    engine: GraphEngine,
    registry: RepoRegistry,
    events: EventBroadcaster,
    query_log: QueryLog | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api")
    query_log = query_log if query_log is not None else QueryLog()

    def _require_repo(repo_id: str) -> None:
        """Ensure repo_id is registered. Raises 404 if not."""
        if registry.get(repo_id) is None:
            raise HTTPException(status_code=404, detail=f"unknown repo: {repo_id}")

    # Load repo issues from file (written by tray app)
    def _get_repo_issues() -> dict[str, str]:
        """Load repo issues from the repo_issues.json file if it exists."""
        settings = get_settings()
        issues_path = settings.registry_db_path.parent / "repo_issues.json"
        if issues_path.exists():
            try:
                return json.loads(issues_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    @router.get("/repos")
    def list_repos() -> dict[str, Any]:
        repo_issues = _get_repo_issues()
        return {
            "repos": [
                {
                    "repo_id": repo.repo_id,
                    "path": str(repo.path),
                    "active": repo.active,
                    "watch_enabled": repo.watch_enabled,
                    "last_indexed": repo.last_indexed,
                    "node_count": queries.count_nodes(engine, repo.repo_id),
                    "issue": repo_issues.get(repo.repo_id),  # Include issue if any
                }
                for repo in registry.list_repos()
            ],
            "issues": repo_issues,  # Also return all issues as a summary
        }

    @router.get("/repos/{repo_id}/summary")
    def repo_summary(repo_id: str) -> dict[str, Any]:
        _require_repo(repo_id)
        return queries.summary_counts(engine, repo_id)

    @router.get("/repos/{repo_id}/graph")
    def repo_graph(repo_id: str, label: str | None = None, limit: int = _GRAPH_LIMIT_DEFAULT) -> dict[str, Any]:
        _require_repo(repo_id)
        if label is not None and label not in NODE_LABELS:
            raise HTTPException(status_code=400, detail=f"unknown label: {label}")
        capped_limit = max(1, min(limit, _GRAPH_LIMIT_CEILING))
        nodes, edges = queries.graph_slice(engine, repo_id, label, capped_limit)
        return {
            "nodes": [
                {
                    "data": {
                        "id": n["id"],
                        "label": n["label"],
                        "name": n["name"],
                        "key": identity_key(n["label"], repo_id, n["name"], n["file"]),
                    }
                }
                for n in nodes
            ],
            "edges": [
                {
                    "data": {
                        "id": f"{e['source']}->{e['rel_type']}->{e['target']}",
                        "source": e["source"],
                        "target": e["target"],
                        "type": e["rel_type"],
                    }
                }
                for e in edges
            ],
        }

    @router.get("/repos/{repo_id}/search")
    def repo_search(repo_id: str, q: str, max_results: int = 15) -> dict[str, Any]:
        _require_repo(repo_id)
        return {"results": queries.search_components(engine, repo_id, q, max_results)}

    @router.get("/repos/{repo_id}/layout")
    def get_repo_layout(repo_id: str) -> dict[str, Any]:
        _require_repo(repo_id)
        return load_layout(repo_id)

    @router.put("/repos/{repo_id}/layout")
    async def put_repo_layout(repo_id: str, request: Request) -> dict[str, Any]:
        _require_repo(repo_id)
        # Declaring a `payload: dict[str, Any]` parameter (the previous
        # shape) makes Starlette buffer and json-decode the entire body
        # before this function ever runs, so the size check below couldn't
        # actually stop that work -- only the eventual write to disk. Taking
        # the raw `Request` instead means the body is only read here, after
        # Content-Length has already rejected an oversized request; a caller
        # that omits or lies about the header (chunked transfer, no header at
        # all) still hits the len(body) check right after the read, before
        # any JSON parsing happens.
        content_length = request.headers.get("content-length")
        if content_length is not None and content_length.isdigit() and int(content_length) > _LAYOUT_PAYLOAD_LIMIT_BYTES:
            raise HTTPException(status_code=413, detail="layout payload too large")
        body = await request.body()
        if len(body) > _LAYOUT_PAYLOAD_LIMIT_BYTES:
            raise HTTPException(status_code=413, detail="layout payload too large")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="payload must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="payload must be a JSON object")
        save_layout(repo_id, payload)
        return {"ok": True}

    @router.post("/cypher")
    def run_cypher(payload: dict[str, Any]) -> dict[str, Any]:
        """Server-side Cypher execution for the dashboard's Cypher console.

        Replaces the prototype's direct browser->Neo4j HTTP connection (see
        Implementation Plan #5 rebuild) -- the browser no longer holds or
        sends Neo4j credentials. Trust boundary is the same one the rest of
        the dashboard already relies on: loopback-only by default (see the
        Network/access settings pane), not a second gate layered on top of
        `Settings.enable_run_cypher` (that flag is documented as
        agent/MCP-only and is orthogonal to a human typing a query into the
        dashboard they're already running locally).

        `record: true` opts the call into the query log/rate telemetry --
        only the console's own explicit Run/isolate/history actions set it,
        so background polling (repo list, topology counts, live glow preview
        on every keystroke) doesn't flood the log.
        """
        query = (payload.get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="query is required")
        params = payload.get("params") or {}
        repo_id = payload.get("repo_id")
        should_record = bool(payload.get("record"))

        start = time.monotonic()
        try:
            result = engine.run_cypher_graph(query, params)
        except Exception as exc:  # neo4j driver raises its own exception hierarchy
            if should_record:
                query_log.record(
                    repo_id=repo_id, query=query, duration_ms=(time.monotonic() - start) * 1000, ok=False
                )
            return {"results": [], "errors": [{"code": exc.__class__.__name__, "message": str(exc)}]}

        if should_record:
            query_log.record(
                repo_id=repo_id, query=query, duration_ms=(time.monotonic() - start) * 1000, ok=True
            )
        return {"results": [result], "errors": []}

    @router.get("/query-log")
    def get_query_log(limit: int = 100) -> dict[str, Any]:
        return {"entries": query_log.recent(max(1, min(limit, 500)))}

    @router.get("/query-rate")
    def get_query_rate(span: int = 3600, interval: int = 60) -> dict[str, Any]:
        return {"buckets": query_log.rate(max(1, span), max(1, interval))}

    @router.get("/mcp-tools")
    def get_mcp_tools() -> list[dict[str, Any]]:
        # Imported lazily: devgraph.mcp.server pulls in devgraph.agent (for
        # lifecycle), and devgraph.agent.tray imports dashboard.app at module
        # scope -- importing mcp.server at routes.py's own module scope would
        # create app -> routes -> mcp.server -> agent -> tray -> app.
        from devgraph.mcp.server import _TOOL_CATALOG

        settings = get_settings()
        catalog = _TOOL_CATALOG if settings.enable_run_cypher else [
            t for t in _TOOL_CATALOG if t["name"] != "run_cypher"
        ]
        return [
            {**tool, "description": (inspect.getdoc(getattr(devgraph_tools, tool["name"], None)) or "").split("\n")[0]}
            for tool in catalog
        ]

    @router.get("/settings")
    def get_dashboard_settings() -> dict[str, Any]:
        s = get_settings()
        return {
            "neo4j_uri": s.neo4j_uri,
            "neo4j_user": s.neo4j_user,
            "dashboard_host": s.dashboard_host,
            "dashboard_port": s.dashboard_port,
            "enable_run_cypher": s.enable_run_cypher,
            "allow_cross_repo": s.allow_cross_repo,
            "telemetry_enabled": s.telemetry_enabled,
            "cloud_sync": s.cloud_sync,
            "mentions_ambiguous_mode": s.mentions_ambiguous_mode,
            "git_recency_track_author": s.git_recency_track_author,
            "registry_db_path": str(s.registry_db_path),
            "watch_debounce_ms": s.watch_debounce_ms,
            "health_check_interval_s": s.health_check_interval_s,
        }

    @router.get("/repos/{repo_id}/git-log")
    def repo_git_log(repo_id: str, limit: int = 60) -> list[dict[str, Any]]:
        _require_repo(repo_id)
        repo_path = registry.get(repo_id).path
        try:
            return get_git_log(repo_path, max(1, min(limit, 300)))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"git log failed: {exc}") from exc

    @router.get("/repos/{repo_id}/git-status")
    def repo_git_status(repo_id: str) -> dict[str, Any]:
        _require_repo(repo_id)
        repo_path = registry.get(repo_id).path
        try:
            return get_git_status(repo_path)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"git status failed: {exc}") from exc

    @router.get("/events")
    async def stream_events(request: Request) -> StreamingResponse:
        queue = events.subscribe()

        async def event_source():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=_SSE_KEEPALIVE_S)
                    except asyncio.TimeoutError:
                        # Idle-timeout keep-alive so intermediary buffering
                        # doesn't silently drop a quiet connection.
                        yield ": keep-alive\n\n"
                        continue
                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                events.unsubscribe(queue)

        return StreamingResponse(event_source(), media_type="text/event-stream")

    return router
