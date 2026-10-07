"""Dashboard FastAPI app: `build_app()` is the package's one entry point.

A second, independent read-only consumer of the same `GraphEngine`/
`RepoRegistry` instances the tray already owns -- never routes through the
MCP stdio server, which is inherently 1:1 with a single client's
stdin/stdout (see `mcp/server.py`'s module docstring).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from devgraph.config.settings import get_settings
from devgraph.dashboard.db_metrics import MetricsHistory
from devgraph.dashboard.events import EventBroadcaster
from devgraph.dashboard.query_log import QueryLog
from devgraph.dashboard.routes import build_router
from devgraph.dashboard.url import WILDCARD_HOSTNAMES
from devgraph.graph.engine import GraphEngine
from devgraph.registry.store import RepoRegistry

_STATIC_DIR = Path(__file__).resolve().parent / "static"

_LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def _allowed_hostnames(dashboard_host: str) -> frozenset[str]:
    """Loopback names plus the configured bind host, unless that's a wildcard.

    A wildcard bind (`0.0.0.0`/`::`) does not widen the allowlist: the
    machine's other names would have to be guessed, and guessing wrong in
    the permissive direction is the bug this guards against. To reach the
    dashboard by a LAN address, bind to that address instead.
    """
    configured = dashboard_host.strip().strip("[]").lower()
    if configured in WILDCARD_HOSTNAMES:
        return _LOOPBACK_HOSTNAMES
    return _LOOPBACK_HOSTNAMES | {configured}


def _hostname_of(host_header: str) -> str | None:
    """The hostname in a `Host` header, lowercased, or None if malformed.

    IPv6 literals must be bracketed (`[::1]:8765`); a port, when present,
    must be numeric. The port itself isn't checked -- a rebinding page is
    already on the dashboard's port, so it tells the two apart no better.
    """
    if host_header.startswith("["):
        end = host_header.find("]")
        if end == -1:
            return None
        hostname, rest = host_header[1:end], host_header[end + 1 :]
    else:
        hostname, sep, port = host_header.partition(":")
        rest = sep + port
    if rest and not (rest.startswith(":") and rest[1:].isdigit()):
        return None
    return hostname.lower() or None


class _LocalHostOnlyMiddleware:
    """Refuse any request whose `Host` isn't the local machine.

    The dashboard has no authentication and trusts that only this machine's
    browser can reach it. DNS rebinding breaks that: a page on a hostile
    domain re-points the domain at 127.0.0.1 and is then same-origin with
    the dashboard, so `_reject_cross_site`'s Origin check passes and every
    GET was never checked at all. The browser still sends the hostile
    domain as `Host`, which is what this rejects -- for every route, static
    files and the SSE stream included.
    """

    def __init__(self, app: ASGIApp, allowed_hostnames: frozenset[str]) -> None:
        self.app = app
        self.allowed_hostnames = allowed_hostnames

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # HTTP only: there are no WebSocket routes, so a WebSocket handshake
        # is already refused by the router, and an HTTP response sent on a
        # WebSocket scope would only surface as a server error.
        if scope["type"] == "http":
            hostname = _hostname_of(Headers(scope=scope).get("host", ""))
            if hostname not in self.allowed_hostnames:
                await PlainTextResponse("host not allowed", status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app(
    engine: GraphEngine,
    registry: RepoRegistry,
    events: EventBroadcaster,
    dashboard_host: str | None = None,
    run_exclusive: Callable[[str, Callable[[], Any]], Any] | None = None,
) -> FastAPI:
    """`dashboard_host` is the address the server binds to (default: the
    `dashboard_host` setting); it joins the loopback names in the Host
    allowlist unless it is a wildcard. `run_exclusive` is the agent's
    watcher's, for registration (see `build_router`)."""
    settings = get_settings()
    if dashboard_host is None:
        dashboard_host = settings.dashboard_host
    metrics = MetricsHistory(engine, settings.neo4j_data_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Sampling lives exactly as long as the server, for the tray and the
        # headless agent alike.
        metrics.start()
        try:
            yield
        finally:
            metrics.stop()

    app = FastAPI(title="DevGraph Dashboard", lifespan=lifespan)
    app.state.metrics = metrics
    app.add_middleware(_LocalHostOnlyMiddleware, allowed_hostnames=_allowed_hostnames(dashboard_host))
    app.include_router(build_router(engine, registry, events, QueryLog(), metrics, run_exclusive))
    # Hand-written HTML/CSS/JS, no build step -- StaticFiles serves them
    # as-is (see Implementation Plan #5: no frontend framework in v1).
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(_STATIC_DIR / "index.html"))

    return app
