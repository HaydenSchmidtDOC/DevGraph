"""Dashboard FastAPI app: `build_app()` is the package's one entry point.

A second, independent read-only consumer of the same `GraphEngine`/
`RepoRegistry` instances the tray already owns -- never routes through the
MCP stdio server, which is inherently 1:1 with a single client's
stdin/stdout (see `mcp/server.py`'s module docstring).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from devgraph.dashboard.events import EventBroadcaster
from devgraph.dashboard.routes import build_router
from devgraph.graph.engine import GraphEngine
from devgraph.registry.store import RepoRegistry

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def build_app(engine: GraphEngine, registry: RepoRegistry, events: EventBroadcaster) -> FastAPI:
    app = FastAPI(title="DevGraph Dashboard")
    app.include_router(build_router(engine, registry, events))
    # Hand-written HTML/CSS/JS, no build step -- StaticFiles serves them
    # as-is (see Implementation Plan #5: no frontend framework in v1).
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(_STATIC_DIR / "index.html"))

    return app
