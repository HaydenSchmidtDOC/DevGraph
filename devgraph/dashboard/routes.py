"""The dashboard's `/api/*` read endpoints.

Every repo-scoped handler validates `repo_id` against the registry first and
404s if unknown -- the same allowlist discipline `mcp/server.py` applies,
since this is a second entry point into the same engine/registry the tray
already owns (see Implementation Plan #5's "Data comes from GraphEngine
directly" decision).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from devgraph.dashboard import queries
from devgraph.dashboard.events import EventBroadcaster
from devgraph.graph.engine import GraphEngine
from devgraph.graph.schema import NODE_LABELS
from devgraph.registry.store import RepoRegistry

logger = logging.getLogger(__name__)

_GRAPH_LIMIT_DEFAULT = 500
# Hard ceiling so a large repo's full graph can't hang the browser tab, per
# Implementation Plan #5 Item 1.
_GRAPH_LIMIT_CEILING = 2000
_SSE_KEEPALIVE_S = 15


def build_router(engine: GraphEngine, registry: RepoRegistry, events: EventBroadcaster) -> APIRouter:
    router = APIRouter(prefix="/api")

    def _require_repo(repo_id: str) -> None:
        if registry.get(repo_id) is None:
            raise HTTPException(status_code=404, detail=f"no such repo_id: {repo_id}")

    @router.get("/repos")
    def list_repos() -> list[dict[str, Any]]:
        return [
            {
                "repo_id": repo.repo_id,
                "path": str(repo.path),
                "active": repo.active,
                "watch_enabled": repo.watch_enabled,
                "last_indexed": repo.last_indexed,
                "node_count": queries.count_nodes(engine, repo.repo_id),
            }
            for repo in registry.list_repos()
        ]

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
                {"data": {"id": n["id"], "label": n["label"], "name": n["name"]}} for n in nodes
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
