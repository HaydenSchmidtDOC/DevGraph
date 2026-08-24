"""DevGraph tray app: the always-on shell around watcher + Neo4j health.

A thin shell per the Implementation Plan — it owns startup/shutdown wiring
and surfaces health via the tray icon, but the registry/watcher/graph/MCP
components underneath are what do the real work. No filesystem path is ever
touched here directly; everything routes through RepoRegistry/WatcherManager.
"""

from __future__ import annotations

import logging
import threading
import time
from io import BytesIO

import pystray
from PIL import Image, ImageDraw

from devgraph.config import get_settings
from devgraph.graph.engine import GraphEngine
from devgraph.registry.store import RepoRegistry
from devgraph.watcher.manager import WatcherManager

logger = logging.getLogger(__name__)

_OK_COLOR = (46, 160, 67)
_WARN_COLOR = (200, 60, 60)


def _make_icon(color: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((8, 8, 56, 56), fill=color)
    return img


class TrayApp:
    """Owns the tray icon and the health-check loop.

    `start()` blocks the calling thread (pystray requirement on most
    platforms) — run it from `__main__`, not from inside other app logic.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._registry = RepoRegistry(self._settings.registry_db_path)
        self._engine = GraphEngine(
            self._settings.neo4j_uri, self._settings.neo4j_user, self._settings.neo4j_password
        )
        self._watcher = WatcherManager(self._registry, on_changes=self._on_changes)
        self._paused = False
        self._healthy = True
        self._stop_event = threading.Event()
        self._icon: pystray.Icon | None = None

    def _on_changes(self, repo_id: str, changed_paths: set) -> None:
        logger.info("changes detected for %s: %d file(s)", repo_id, len(changed_paths))

    def _health_check_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._engine.verify_connectivity()
                self._healthy = True
            except Exception:
                logger.warning("Neo4j health check failed", exc_info=True)
                self._healthy = False
            self._refresh_icon()
            self._stop_event.wait(self._settings.health_check_interval_s)

    def _refresh_icon(self) -> None:
        if self._icon is None:
            return
        color = _OK_COLOR if self._healthy else _WARN_COLOR
        self._icon.icon = _make_icon(color)
        self._icon.title = self._status_text()

    def _status_text(self) -> str:
        state = "paused" if self._paused else ("ok" if self._healthy else "warning")
        return f"DevGraph ({state})"

    def _toggle_pause(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self._paused = not self._paused
        if self._paused:
            self._watcher.stop()
        else:
            self._watcher.start()
        self._refresh_icon()

    def _quit(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self._stop_event.set()
        self._watcher.stop()
        self._engine.close()
        self._registry.close()
        icon.stop()

    def start(self) -> None:
        self._watcher.start()
        health_thread = threading.Thread(target=self._health_check_loop, daemon=True)
        health_thread.start()

        menu = pystray.Menu(
            pystray.MenuItem(lambda item: self._status_text(), None, enabled=False),
            pystray.MenuItem(
                lambda item: "Resume watching" if self._paused else "Pause watching",
                self._toggle_pause,
            ),
            pystray.MenuItem("Quit", self._quit),
        )
        self._icon = pystray.Icon("devgraph", _make_icon(_OK_COLOR), "DevGraph", menu)
        self._icon.run()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    TrayApp().start()


if __name__ == "__main__":
    main()
