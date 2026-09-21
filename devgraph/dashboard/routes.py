"""The dashboard's `/api/*` endpoints.

Every repo-scoped handler validates `repo_id` against the registry first and
404s if unknown -- the same allowlist discipline `mcp/server.py` applies,
since this is a second entry point into the same engine/registry the tray
already owns (see Implementation Plan #5's "Data comes from GraphEngine
directly" decision).

Read-only apart from two writes: the canvas layout (`PUT .../layout`) and
repository registration (`POST /repos`), which is the same add-then-initial-
scan sequence `devgraph add <path>` runs, against the same services.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from devgraph.config.settings import get_settings
from devgraph.dashboard import queries
from devgraph.dashboard.events import EventBroadcaster
from devgraph.dashboard.git_info import get_git_log, get_git_status
from devgraph.dashboard.layout_store import load_layout, save_layout
from devgraph.dashboard.query_log import QueryLog
from devgraph.graph.engine import GraphEngine, identity_key
from devgraph.graph.schema import NODE_LABELS
from devgraph.indexer.dispatch import full_scan
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

# Fixed, parameterless query behind `GET /database-stats`. The JVM's own
# java.lang:type=Memory MBean is the one heap source available on a stock
# Neo4j 5.26 Community container (no APOC, no metrics endpoint), and
# `dbms.queryJmx` is a read-only procedure. Held here as a literal so the
# endpoint can never be steered by caller input.
_JMX_MEMORY_QUERY = 'CALL dbms.queryJmx("java.lang:type=Memory") YIELD attributes RETURN attributes'


def _heap_unavailable() -> dict[str, Any]:
    """The one shape the card renders as dashed/"unavailable"."""
    return {"available": False, "used_bytes": None, "max_bytes": None, "used_percent": None}


def _jmx_number(value: Any) -> float | None:
    """A JMX attribute as a real number, or None if it isn't usable.

    `bool` is an `int` in Python, and a JSON `true` reaching an arithmetic
    path would silently become 1 byte, so it is rejected explicitly.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _heap_from_jmx_rows(rows: Any) -> dict[str, Any]:
    """Heap used/max out of `_JMX_MEMORY_QUERY`'s rows, or the unavailable shape.

    Neo4j 5.26 returns each JMX composite attribute nested as
    `attributes.HeapMemoryUsage.value.properties.{used,max}` -- the flat
    `...value.used` shape the dashboard used to read never matches, which is
    why the card never went live. Anything that isn't that exact shape with
    two usable numbers (denied procedure, empty result, a future/other
    layout, a non-numeric or non-finite value) is reported as unavailable
    rather than guessed at.

    Zero is treated as unusable, not as a value: a running JVM reports
    neither zero heap used nor zero heap max, so a zero here means the
    attribute wasn't populated. `used > max` is likewise rejected -- a meter
    past 100% is a misread, not a reading. Because both are rejected,
    `used_percent` is always within 0-100 by construction.
    """
    if not isinstance(rows, list) or not rows:
        return _heap_unavailable()
    row = rows[0]
    if not isinstance(row, dict):
        return _heap_unavailable()
    node: Any = row
    for key in ("attributes", "HeapMemoryUsage", "value", "properties"):
        if not isinstance(node, dict):
            return _heap_unavailable()
        node = node.get(key)
    if not isinstance(node, dict):
        return _heap_unavailable()

    used = _jmx_number(node.get("used"))
    heap_max = _jmx_number(node.get("max"))
    if used is None or heap_max is None:
        return _heap_unavailable()
    if used <= 0 or heap_max <= 0 or used > heap_max:
        return _heap_unavailable()
    return {
        "available": True,
        "used_bytes": int(used),
        "max_bytes": int(heap_max),
        "used_percent": round(used / heap_max * 100, 1),
    }


def _reject_cross_site(request: Request) -> None:
    """Refuse a state-changing request that another site's page initiated.

    The dashboard binds to loopback with no auth, so any page in the same
    browser can reach it; without this, a visited site could POST a path of
    its choosing into the registry (the browser attaches no credentials, but
    none are required here). Both signals are browser-supplied and only
    present on browser traffic: `Sec-Fetch-Site` (absent on older browsers)
    and `Origin` (sent on every cross-origin request, and on same-origin
    POSTs). A client that sends neither -- curl, the CLI, a test -- is not a
    browser being driven by a third-party page and is left alone; this is a
    browser-confused-deputy guard, not an authentication check.
    """
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        raise HTTPException(status_code=403, detail="cross-site request rejected")
    origin = request.headers.get("origin")
    if origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(status_code=403, detail="cross-origin request rejected")


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

    def _register_repo(path: str, repo_id: str | None) -> dict[str, Any]:
        """Register + initially scan, exactly as `devgraph add <path>` does.

        Blocking (SQLite write, Neo4j round trips, a full file walk), so it
        runs in a threadpool -- the event loop also serves the SSE stream the
        dashboard is watching while this runs.
        """
        try:
            record = registry.add_repo(path, repo_id)
        except ValueError as exc:
            # add_repo's own message names the actual problem (missing path,
            # not a git repo, already registered) and contains nothing the
            # caller didn't just supply.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("repo registration failed for %s", path)
            raise HTTPException(status_code=500, detail="registration failed") from exc

        indexed = False
        files_indexed: int | None = None
        warning: str | None = None
        try:
            engine.init_schema()
            engine.upsert_repository(record.repo_id, record.repo_id, str(record.path))
            files_indexed = full_scan(
                engine,
                record.repo_id,
                record.path,
                docs_path=record.docs_path,
                mentions_enabled=record.mentions_enabled,
            )
            registry.mark_indexed(record.repo_id)
            indexed = True
        except Exception as exc:
            # Registration already committed to SQLite; an indexing failure
            # (e.g. Neo4j down) must not undo it -- same call as the CLI's,
            # and `devgraph rescan <repo_id>` is the same retry.
            logger.warning("registered %s but the initial scan failed: %s", record.repo_id, exc)
            warning = (
                f"Registered, but the initial scan failed: {exc}. "
                f"Run 'devgraph rescan {record.repo_id}' once Neo4j is reachable."
            )

        # Report what was persisted, not what was asked for: add_repo
        # slugifies the id and can suffix it on collision, and mark_indexed
        # just wrote last_indexed.
        persisted = registry.get(record.repo_id)
        return {
            "repo_id": persisted.repo_id,
            "path": str(persisted.path),
            "active": persisted.active,
            "watch_enabled": persisted.watch_enabled,
            "last_indexed": persisted.last_indexed,
            "registered": True,
            "indexed": indexed,
            "files_indexed": files_indexed,
            "warning": warning,
        }

    @router.post("/repos", status_code=201)
    async def register_repo(request: Request) -> dict[str, Any]:
        _reject_cross_site(request)
        # Strict media type: a browser form post (or a text/plain body) is
        # what a cross-site page can send without a preflight, so anything
        # but JSON is refused before the body is even read.
        media_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if media_type != "application/json":
            raise HTTPException(status_code=415, detail="content-type must be application/json")
        try:
            payload = json.loads(await request.body())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="payload must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="payload must be a JSON object")

        raw_path = payload.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise HTTPException(status_code=400, detail="path is required")
        raw_repo_id = payload.get("repo_id")
        if raw_repo_id is not None and (not isinstance(raw_repo_id, str) or not raw_repo_id.strip()):
            raise HTTPException(status_code=400, detail="repo_id must be a non-empty string")

        return await run_in_threadpool(
            _register_repo,
            raw_path.strip(),
            raw_repo_id.strip() if isinstance(raw_repo_id, str) else None,
        )

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

    # The canvas's repo selector has an "All Repos" option that is not a
    # registered repo, and its layout is worth persisting like any other
    # view's. `_require_repo` is what keeps a repo_id safe to use as a
    # filename (it can only ever be an id the registry itself issued), so
    # this reserved id is matched by exact equality rather than being folded
    # into a pattern that would reopen that.
    _ALL_REPOS_LAYOUT_ID = "__all__"

    def _require_layout_scope(repo_id: str) -> None:
        if repo_id != _ALL_REPOS_LAYOUT_ID:
            _require_repo(repo_id)

    @router.get("/repos/{repo_id}/layout")
    def get_repo_layout(repo_id: str) -> dict[str, Any]:
        _require_layout_scope(repo_id)
        return load_layout(repo_id)

    @router.put("/repos/{repo_id}/layout")
    async def put_repo_layout(repo_id: str, request: Request) -> dict[str, Any]:
        _require_layout_scope(repo_id)
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

    @router.get("/database-stats")
    def database_stats() -> dict[str, Any]:
        """Live JVM heap for the dashboard's Database & memory card.

        Read-only and fixed-query: the browser used to send the JMX call
        through `/api/cypher` and parse driver records itself, which coupled
        a visible card to Neo4j's result shape (and broke on 5.26). This
        returns only the numbers the card renders plus whether they're real.

        Always 200, never the driver's error text: an unreachable database,
        a build without `dbms.queryJmx`, or a role that isn't allowed to
        call it are all the same thing to the card -- no reading -- and the
        browser has no use for a stack-shaped message it would have to
        decide not to display.
        """
        try:
            rows = engine.run_cypher(_JMX_MEMORY_QUERY)
        except Exception as exc:  # neo4j driver raises its own exception hierarchy
            logger.debug("JMX heap query unavailable: %s", exc)
            return {"heap": _heap_unavailable()}
        return {"heap": _heap_from_jmx_rows(rows)}

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
        rec = registry.get(repo_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"unknown repo: {repo_id}")
        repo_path = rec.path
        try:
            return get_git_log(repo_path, max(1, min(limit, 300)))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"git log failed: {exc}") from exc

    @router.get("/repos/{repo_id}/git-status")
    def repo_git_status(repo_id: str) -> dict[str, Any]:
        _require_repo(repo_id)
        rec = registry.get(repo_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"unknown repo: {repo_id}")
        repo_path = rec.path
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
