"""Dashboard FastAPI app: `build_app()` is the package's one entry point.

A second, independent read-only consumer of the same `GraphEngine`/
`RepoRegistry` instances the tray already owns -- never routes through the
MCP stdio server, which is inherently 1:1 with a single client's
stdin/stdout (see `mcp/server.py`'s module docstring).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from devgraph.config.settings import get_settings
from devgraph.dashboard.db_metrics import MetricsHistory
from devgraph.dashboard.events import EventBroadcaster
from devgraph.dashboard.query_log import QueryLog
from devgraph.dashboard.routes import build_router
from devgraph.graph.engine import GraphEngine
from devgraph.registry.store import RepoRegistry

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def build_app(engine: GraphEngine, registry: RepoRegistry, events: EventBroadcaster) -> FastAPI:
    metrics = MetricsHistory(engine, get_settings().neo4j_data_dir)

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
    app.include_router(build_router(engine, registry, events, QueryLog(), metrics))
    # Hand-written HTML/CSS/JS, no build step -- StaticFiles serves them
    # as-is (see Implementation Plan #5: no frontend framework in v1).
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(_STATIC_DIR / "index.html"))

    return app
