"""DevGraph tray app: the always-on shell around watcher + incremental indexing
+ Neo4j health.

A thin shell per the Implementation Plan — it owns startup/shutdown wiring
and surfaces health via the tray icon, but the registry/watcher/graph
components underneath are what do the real work. No filesystem path is ever
touched here directly; everything routes through RepoRegistry/WatcherManager.

Does NOT run the MCP server: DevGraph's MCP server uses the stdio transport
(devgraph/mcp/server.py), which is inherently 1:1 with a single client's
stdin/stdout — an MCP client (e.g. Claude Code) spawns that process itself
per DEVGRAPH-CLIENT.md, rather than the tray app hosting a shared server
process. The tray app's job is keeping the graph itself current.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from devgraph.config import get_settings
from devgraph.graph.engine import GraphEngine
from devgraph.indexer.dispatch import index_paths, remove_paths
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
        self._last_seen_registry_change = self._registry.last_changed_at()

    def _on_changes(self, repo_id: str, changed_paths: set[Path], deleted_paths: set[Path]) -> None:
        """Route watcher events to the indexer. This is the piece that closes the
        "developer saves file -> graph refreshed" loop from the Design Brief —
        previously the watcher only logged changes and nothing consumed them.
        """
        logger.info(
            "changes detected for %s: %d changed, %d deleted",
            repo_id,
            len(changed_paths),
            len(deleted_paths),
        )
        try:
            repo = self._registry.get(repo_id)
            if repo is None:
                return  # repo was removed between the event firing and now
            if changed_paths:
                index_paths(self._engine, repo_id, repo.path, changed_paths, docs_path=repo.docs_path)
            if deleted_paths:
                remove_paths(self._engine, repo_id, repo.path, deleted_paths)
            self._registry.mark_indexed(repo_id)
        except Exception:
            logger.warning("incremental reindex failed for %s", repo_id, exc_info=True)

    def _health_check_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._engine.verify_connectivity()
                self._healthy = True
            except Exception:
                logger.warning("Neo4j health check failed", exc_info=True)
                self._healthy = False
            self._check_registry_changes()
            self._refresh_icon()
            self._write_heartbeat()
            self._stop_event.wait(self._settings.health_check_interval_s)

    def _check_registry_changes(self) -> None:
        """Pick up add/remove/watch-flag changes made by another `devgraph`
        CLI invocation while this tray process has been running.

        The tray owns the only live WatcherManager, but registry mutations
        happen in whatever short-lived process ran the CLI command -- there
        is no direct call path between them, only this shared SQLite file
        plus the marker RepoRegistry touches on every such mutation (see
        registry/store.py). Without this poll, a repo added or re-enabled
        after the tray started stays fully unwatched (though still
        indexable on demand via `rescan`) until the tray is restarted.
        """
        try:
            current = self._registry.last_changed_at()
        except Exception:
            return
        if current != self._last_seen_registry_change:
            self._last_seen_registry_change = current
            if not self._paused:
                try:
                    self._watcher.refresh()
                except Exception:
                    logger.warning("watcher refresh after registry change failed", exc_info=True)

    def _write_heartbeat(self) -> None:
        """Write a UTC timestamp `status`/`doctor` read to report tray liveness.

        Same directory as registry.sqlite3 — no new settings field needed.
        """
        heartbeat_path = self._settings.registry_db_path.parent / "tray_heartbeat.txt"
        try:
            heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            heartbeat_path.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        except OSError:
            logger.warning("failed to write tray heartbeat file", exc_info=True)

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
