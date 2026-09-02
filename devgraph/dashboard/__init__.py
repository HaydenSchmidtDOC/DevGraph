"""Live web dashboard: repo overview + node browser + graph canvas.

Served from inside the tray process (see `devgraph/agent/tray.py`) via
FastAPI + uvicorn, on loopback only. See `app.py` for `build_app()`, the
package's single entry point.
"""
