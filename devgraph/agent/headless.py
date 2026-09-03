"""Headless variant of the tray app: watcher + incremental indexing + Neo4j
health + the live web dashboard, with no pystray icon.

Containers have no desktop tray to attach to, so this is the entrypoint used
by the Docker image (see Dockerfile / deploy/docker-compose.yml) instead of
`devgraph.agent.tray`. It shares all the same underlying logic (registry,
watcher, indexer, dashboard) and differs only in how it's driven: a
foreground loop woken by SIGTERM/SIGINT rather than a pystray menu.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path

import uvicorn

from devgraph.config import get_settings
from devgraph.dashboard.app import build_app
from devgraph.dashboard.events import EventBroadcaster
from devgraph.graph.engine import GraphEngine
from devgraph.indexer.dispatch import index_paths, remove_paths
from devgraph.registry.store import RepoRegistry
from devgraph.watcher.manager import WatcherManager

logger = logging.getLogger(__name__)


class HeadlessAgent:
    """Same responsibilities as `devgraph.agent.tray.TrayApp`, minus the icon."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._registry = RepoRegistry(self._settings.registry_db_path)
        self._engine = GraphEngine(
            self._settings.neo4j_uri, self._settings.neo4j_user, self._settings.neo4j_password
        )
        self._watcher = WatcherManager(self._registry, on_changes=self._on_changes)
        self._healthy = True
        self._stop_event = threading.Event()
        self._last_seen_registry_change = self._registry.last_changed_at()
        self._events = EventBroadcaster()
        self._dashboard_server: uvicorn.Server | None = None
        self._dashboard_thread: threading.Thread | None = None

    def _on_changes(self, repo_id: str, changed_paths: set[Path], deleted_paths: set[Path]) -> None:
        logger.info(
            "changes detected for %s: %d changed, %d deleted",
            repo_id,
            len(changed_paths),
            len(deleted_paths),
        )
        try:
            repo = self._registry.get(repo_id)
            if repo is None:
                return
            if changed_paths:
                index_paths(self._engine, repo_id, repo.path, changed_paths, docs_path=repo.docs_path, mentions_enabled=repo.mentions_enabled)
            if deleted_paths:
                remove_paths(self._engine, repo_id, repo.path, deleted_paths)
            self._registry.mark_indexed(repo_id)
            self._events.publish(
                {
                    "type": "reindexed",
                    "repo_id": repo_id,
                    "changed": len(changed_paths),
                    "deleted": len(deleted_paths),
                }
            )
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
            self._write_heartbeat()
            self._stop_event.wait(self._settings.health_check_interval_s)

    def _check_registry_changes(self) -> None:
        try:
            current = self._registry.last_changed_at()
        except Exception:
            return
        if current != self._last_seen_registry_change:
            self._last_seen_registry_change = current
            try:
                self._watcher.refresh()
                self._events.publish({"type": "registry_changed"})
            except Exception:
                logger.warning("watcher refresh after registry change failed", exc_info=True)

    def _write_heartbeat(self) -> None:
        heartbeat_path = self._settings.registry_db_path.parent / "tray_heartbeat.txt"
        try:
            heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            heartbeat_path.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        except OSError:
            logger.warning("failed to write heartbeat file", exc_info=True)

    def _run_dashboard(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._events.bind_loop(loop)

        app = build_app(self._engine, self._registry, self._events)
        config = uvicorn.Config(
            app,
            host=self._settings.dashboard_host,
            port=self._settings.dashboard_port,
            loop="asyncio",
            log_level="warning",
        )
        server = uvicorn.Server(config)
        self._dashboard_server = server
        try:
            logger.info(
                "dashboard on http://%s:%d", self._settings.dashboard_host, self._settings.dashboard_port
            )
            loop.run_until_complete(server.serve())
        except Exception:
            logger.warning("dashboard failed to start; continuing without it", exc_info=True)
        finally:
            loop.close()

    def stop(self) -> None:
        self._stop_event.set()
        self._watcher.stop()
        if self._dashboard_server is not None:
            self._dashboard_server.should_exit = True
            if self._dashboard_thread is not None:
                self._dashboard_thread.join(timeout=5)
        self._engine.close()
        self._registry.close()

    def start(self) -> None:
        self._watcher.start()
        health_thread = threading.Thread(target=self._health_check_loop, daemon=True)
        health_thread.start()

        if self._settings.dashboard_enabled:
            self._dashboard_thread = threading.Thread(target=self._run_dashboard, daemon=True)
            self._dashboard_thread.start()

        self._stop_event.wait()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    agent = HeadlessAgent()

    def _handle_signal(signum, frame) -> None:
        logger.info("received signal %s, shutting down", signum)
        agent.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    agent.start()


if __name__ == "__main__":
    main()
