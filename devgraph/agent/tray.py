"""DevGraph tray app: the always-on shell around watcher + incremental indexing
+ Neo4j health + the live web dashboard.

A thin shell per the Implementation Plan — it owns startup/shutdown wiring
and surfaces health via the tray icon, but the registry/watcher/graph
components underneath are what do the real work. No filesystem path is ever
touched here directly; everything routes through RepoRegistry/WatcherManager.
Also starts the dashboard (devgraph/dashboard/) on its own daemon thread, per
Implementation Plan #5 — a second, independent read-only consumer of the
same GraphEngine/RepoRegistry this app already owns.

Does NOT run the MCP server: DevGraph's MCP server uses the stdio transport
(devgraph/mcp/server.py), which is inherently 1:1 with a single client's
stdin/stdout — an MCP client (e.g. Claude Code) spawns that process itself
per DEVGRAPH-CLIENT.md, rather than the tray app hosting a shared server
process. The tray app's job is keeping the graph itself current.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import pystray
import uvicorn
from PIL import Image, ImageDraw

from devgraph.config import get_settings
from devgraph.dashboard.app import build_app
from devgraph.dashboard.events import EventBroadcaster
from devgraph.graph.engine import GraphEngine
from devgraph.indexer.dispatch import index_paths, remove_paths
from devgraph.indexer.git_history.extractor import sync_git_history
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
        self._watcher = WatcherManager(self._registry, on_changes=self._on_changes, on_git_state_changed=self._on_git_state_changed)
        self._paused = False
        self._healthy = True
        self._stop_event = threading.Event()
        self._icon: pystray.Icon | None = None
        self._last_seen_registry_change = self._registry.last_changed_at()
        self._events = EventBroadcaster()
        self._dashboard_loop: asyncio.AbstractEventLoop | None = None
        self._dashboard_server: uvicorn.Server | None = None
        self._dashboard_thread: threading.Thread | None = None

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

    def _on_git_state_changed(self, repo_id: str) -> None:
        """Route git state change events to the git history syncer.

        Called when .git directory state changes (file save, branch switch, etc.),
        debounced via WatcherManager. Syncs git history and updates recency
        accordingly — handles append-only fast path as well as history rewrites
        (rebase, reset, amend).
        """
        logger.info("git state changed for %s, syncing history", repo_id)
        try:
            result = sync_git_history(self._engine, self._registry, repo_id)
            logger.info(
                "git history synced for %s: mode=%s, indexed=%d, deleted=%d",
                repo_id,
                result.get("mode"),
                result.get("commits_indexed", 0),
                result.get("commits_deleted", 0),
            )
            self._events.publish(
                {
                    "type": "git_history_synced",
                    "repo_id": repo_id,
                    "mode": result.get("mode"),
                    "commits_indexed": result.get("commits_indexed", 0),
                    "commits_deleted": result.get("commits_deleted", 0),
                }
            )
        except Exception:
            logger.warning("git history sync failed for %s", repo_id, exc_info=True)

    def _health_check_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._engine.verify_connectivity()
                self._healthy = True
            except Exception:
                logger.warning("Neo4j health check failed", exc_info=True)
                self._healthy = False
            # Each of these is individually guarded: a failure in any one
            # (e.g. a registry read race, a watcher issue-scan error, or a
            # pystray icon mutation) must not escape this daemon thread and
            # take the whole tray process -- and with it the watcher and
            # indexer -- down.
            try:
                self._check_registry_changes()
            except Exception:
                logger.warning("registry-change check failed", exc_info=True)
            try:
                self._refresh_icon()
            except Exception:
                logger.warning("tray icon refresh failed", exc_info=True)
            try:
                self._write_heartbeat()
            except Exception:
                logger.warning("heartbeat write failed", exc_info=True)
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
        
        Also publishes repo_issues event if any repos have path problems.
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
                    self._events.publish({"type": "registry_changed"})
                except Exception:
                    logger.warning("watcher refresh after registry change failed", exc_info=True)
        
        # Always check for repo issues and broadcast them
        try:
            repo_issues = self._watcher.get_repo_issues()
            if repo_issues:
                self._events.publish({
                    "type": "repo_issues",
                    "issues": repo_issues,
                })
        except Exception:
            logger.warning("failed to check repo issues", exc_info=True)

    def _write_heartbeat(self) -> None:
        """Write a UTC timestamp `status`/`doctor` read to report tray liveness.

        Same directory as registry.sqlite3 — no new settings field needed.
        Also write repo_issues.json with any repos that have path problems.
        """
        heartbeat_path = self._settings.registry_db_path.parent / "tray_heartbeat.txt"
        try:
            heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            heartbeat_path.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        except OSError:
            logger.warning("failed to write tray heartbeat file", exc_info=True)
        
        # Write repo issues to a JSON file for the CLI to read
        issues_path = self._settings.registry_db_path.parent / "repo_issues.json"
        try:
            repo_issues = self._watcher.get_repo_issues()
            if repo_issues:
                issues_path.write_text(json.dumps(repo_issues), encoding="utf-8")
            elif issues_path.exists():
                # Clear the file if there are no more issues
                issues_path.unlink()
        except Exception:
            logger.warning("failed to write repo_issues.json", exc_info=True)

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
        try:
            if self._paused:
                self._watcher.stop()
            else:
                self._watcher.start()
        except Exception:
            # A watcher start/stop failure (e.g. a repo path vanished) must
            # not crash the pystray event loop; revert the flag so the menu
            # state stays truthful.
            logger.warning("watcher pause/resume failed", exc_info=True)
            self._paused = not self._paused
        self._refresh_icon()

    def _open_dashboard(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        url = f"http://{self._settings.dashboard_host}:{self._settings.dashboard_port}"
        try:
            webbrowser.open(url)
        except Exception:
            logger.warning("failed to open dashboard in browser", exc_info=True)

    def _run_dashboard(self) -> None:
        """Runs on its own daemon thread with its own asyncio loop, hosting
        uvicorn.Server -- kept off the tray's pystray main loop (which
        already owns the process's signal handling) and off the health
        check thread. See Implementation Plan #5's dashboard architecture.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._dashboard_loop = loop
        self._events.bind_loop(loop)

        app = build_app(self._engine, self._registry, self._events)
        # The tray's pystray main loop keeps owning the process's signal
        # handling. uvicorn's Server.capture_signals() already detects it is
        # not running on the main thread and skips installing its own
        # handlers in that case (Server.run/serve does this automatically as
        # of uvicorn>=0.32) -- so there is nothing to opt out of here.
        config = uvicorn.Config(
            app,
            host=self._settings.dashboard_host,
            port=self._settings.dashboard_port,
            loop="asyncio",
            log_level="critical",
            access_log=False,
        )
        server = uvicorn.Server(config)
        self._dashboard_server = server
        try:
            logger.info(
                "dashboard on http://%s:%d", self._settings.dashboard_host, self._settings.dashboard_port
            )
            loop.run_until_complete(server.serve())
        except Exception:
            # Additive feature: a bind failure (port already in use, another
            # instance already running, etc.) must not take down the
            # watcher/indexer loop, which is the tray's core job.
            logger.warning("dashboard failed to start; continuing without it", exc_info=True)
        finally:
            # Drop the broadcaster's reference to this loop before closing
            # it: otherwise a later publish() from the watcher/health-check
            # threads would call_soon_threadsafe on a closed loop and raise
            # RuntimeError in a background thread.
            self._events.unbind_loop(loop)
            loop.close()

    def _quit(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self._stop_event.set()
        self._watcher.stop()
        if self._dashboard_server is not None:
            self._dashboard_server.should_exit = True
            if self._dashboard_thread is not None:
                self._dashboard_thread.join(timeout=5)
        # Best-effort teardown: a failure closing the engine/registry must
        # not prevent the tray icon from stopping (which would leave a
        # zombie tray process the PID file still points at). Each close is
        # guarded so one failure doesn't mask the others.
        try:
            self._engine.close()
        except Exception:
            logger.warning("error closing graph engine on quit", exc_info=True)
        try:
            self._registry.close()
        except Exception:
            logger.warning("error closing registry on quit", exc_info=True)
        icon.stop()

    def start(self) -> None:
        self._watcher.start()
        health_thread = threading.Thread(target=self._health_check_loop, daemon=True)
        health_thread.start()

        if self._settings.dashboard_enabled:
            self._dashboard_thread = threading.Thread(target=self._run_dashboard, daemon=True)
            self._dashboard_thread.start()

        menu = pystray.Menu(
            pystray.MenuItem(lambda item: self._status_text(), self._open_dashboard),
            pystray.MenuItem(
                lambda item: "Resume watching" if self._paused else "Pause watching",
                self._toggle_pause,
            ),
            pystray.MenuItem("Quit", self._quit),
        )
        self._icon = pystray.Icon("devgraph", _make_icon(_OK_COLOR), "DevGraph", menu)
        try:
            self._icon.run()
        except Exception:
            logger.critical("pystray event loop crashed", exc_info=True)
            self._watcher.stop()
            try:
                self._engine.close()
            except Exception:
                logger.warning("error closing graph engine after crash", exc_info=True)
            try:
                self._registry.close()
            except Exception:
                logger.warning("error closing registry after crash", exc_info=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO, handlers=[])
    try:
        TrayApp().start()
    except Exception:
        # Last line of defense: if the tray fails to start (or crashes out of
        # pystray's loop before its own cleanup runs), log it and clear the
        # PID file so a stale PID doesn't make `devgraph tray start`/the MCP
        # auto-start think a dead tray is still running. Re-raise so the
        # process exits non-zero and the failure is visible to whoever
        # launched it.
        logger.critical("tray app failed to start", exc_info=True)
        try:
            from devgraph.agent import lifecycle

            lifecycle.tray_pid_path().unlink(missing_ok=True)
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
