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
from logging.handlers import RotatingFileHandler

import uvicorn
from PIL import Image, ImageDraw

from devgraph.agent.schema_rescan import SchemaRescanScheduler
from devgraph.agent.shutdown import shutdown
from devgraph.agent.sync import RepoSync
from devgraph.analytics.insights import InsightsScheduler
from devgraph.config import get_settings
from devgraph.config.settings import LOG_BACKUP_COUNT, LOG_MAX_BYTES
from devgraph.dashboard.app import build_app
from devgraph.dashboard.serving import DashboardFailure, serve_dashboard
from devgraph.dashboard.url import dashboard_url
from devgraph.dashboard.events import EventBroadcaster
from devgraph.graph.engine import GraphEngine
from devgraph.indexer.git_history.extractor import sync_git_history
from devgraph.registry.store import RepoRegistry
from devgraph.watcher.manager import WatcherManager

logger = logging.getLogger(__name__)

try:
    import pystray
except Exception as _exc:  # no display (Xlib DisplayNameError), or no tray backend for this desktop
    pystray = None
    _PYSTRAY_PROBLEM: str | None = f"{type(_exc).__name__}: {_exc}"
else:
    _PYSTRAY_PROBLEM = None

#: Typographic characters the tray's own text uses, spelled in ASCII.
_ASCII_PUNCTUATION = str.maketrans({"…": "...", "—": "-", "–": "-", "‘": "'", "’": "'", "“": '"', "”": '"'})


def pystray_text(text: str) -> str:
    """`text` made safe for every pystray backend: pystray's X11 backend
    encodes the icon title as latin-1 and raised UnicodeEncodeError on the
    ellipsis in "preparing graph indexes…", killing the tray on start. Common
    punctuation becomes its ASCII spelling; anything else outside latin-1
    becomes "?". Every string handed to pystray goes through here."""
    return text.translate(_ASCII_PUNCTUATION).encode("latin-1", "replace").decode("latin-1")


_OK_COLOR = (46, 160, 67)
_WARN_COLOR = (200, 60, 60)


def _configure_logging() -> None:
    """Wire Python logging to a rotating file handler at `settings.log_file`
    (rolled over at `LOG_MAX_BYTES`, keeping `LOG_BACKUP_COUNT` older files).

    The tray runs detached (pythonw.exe, stdout/stderr = DEVNULL), so a
    failure inside a background thread — e.g. the dashboard failing to bind
    its port — is otherwise invisible. A `basicConfig(..., handlers=[])`
    (the prior behavior) disables logging outright, which made exactly that
    dashboard failure undiagnosable: `devgraph logs` reported "no log file"
    and the tray reported "running" while the dashboard thread had silently
    died. Write to the configured file so `devgraph logs` can actually read
    what happened.
    """
    log_path = get_settings().log_file
    if log_path is None:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            RotatingFileHandler(log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
        ],
    )


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
        self._events = EventBroadcaster()
        # RepoSync and the watcher need each other: the lambda resolves
        # self._watcher only when a catch-up is requested.
        self._sync = RepoSync(
            self._engine, self._registry, self._on_sync_event,
            request_catch_up=lambda *a: self._watcher.request_catch_up(*a),
        )
        self._watcher = WatcherManager(
            self._registry,
            on_changes=self._sync.on_changes,
            on_git_state_changed=self._on_git_state_changed,
            on_catch_up=self._sync.on_catch_up,
            on_removed=self._sync.on_removed,
        )
        self._paused = False
        self._healthy = True
        self._schema_ready = False
        self._preparing = False
        self._stop_event = threading.Event()
        self._icon: pystray.Icon | None = None  # type: ignore[valid-type]
        self._last_seen_registry_change = self._registry.last_changed_at()
        self._schema_rescans = SchemaRescanScheduler(
            self._engine, self._registry, on_rescanned=self._on_schema_rescanned,
            is_paused=lambda: self._paused, run_exclusive=self._watcher.run_exclusive,
        )
        self._insights = InsightsScheduler(self._engine, self._registry, on_refreshed=self._on_insights_refreshed)
        self._dashboard_loop: asyncio.AbstractEventLoop | None = None
        self._dashboard_server: uvicorn.Server | None = None
        self._dashboard_problem: DashboardFailure | None = None
        self._dashboard_thread: threading.Thread | None = None

    def _on_schema_rescanned(self, repo_id: str, files: int) -> None:
        self._events.publish({"type": "reindexed", "repo_id": repo_id, "changed": files, "deleted": 0})

    def _on_sync_event(self, event: dict) -> None:
        """Publish RepoSync's events, and show a catch-up on the icon."""
        self._events.publish(event)
        if event["type"] == "catch_up":
            try:
                self._refresh_icon()
            except Exception:
                logger.warning("tray icon refresh failed", exc_info=True)

    def _on_insights_refreshed(self, repo_id: str) -> None:
        self._events.publish({"type": "insights_refreshed", "repo_id": repo_id})

    def _on_git_state_changed(self, repo_id: str) -> None:
        """Route git state change events to the git history syncer.

        Called by WatcherManager after every catch-up of a git repository
        (on start, after a git operation, on a retry), under its batch lock. Syncs git history and updates recency
        accordingly — handles append-only fast path as well as history rewrites
        (rebase, reset, amend).
        """
        logger.debug("syncing git history for %s", repo_id)
        try:
            result = sync_git_history(
                self._engine, self._registry, repo_id,
                on_initial=lambda count: logger.info(
                    "Reading the git history of %s for the first time (%s commits); "
                    "live updates resume when it finishes", repo_id, f"{count:,}",
                ),
            )
            # Every catch-up ends with a sync, so one with HEAD unmoved is quiet.
            logger.log(
                logging.DEBUG if result.get("mode") == "noop" else logging.INFO,
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

    def _provision_schema(self) -> None:
        """Create the built-in constraints and indexes and wait until they are
        built (`GraphEngine.init_schema`), so an upgraded database has its
        lookup indexes before the first catch-up. Retried by the health check
        while it fails."""
        try:
            self._engine.init_schema()
            self._schema_ready = True
        except Exception:
            if self._stop_event.is_set():
                return  # the engine refuses new sessions once shutdown closes it
            logger.warning("could not provision the graph schema; retrying when Neo4j is reachable", exc_info=True)

    def _start_watching(self) -> None:
        """Provision the graph indexes and wait for them, then start the
        watcher and the schedulers. Run on its own thread from `start`, so
        an upgrade's index build (up to minutes) never holds up the tray
        icon and its Quit."""
        try:
            self._provision_schema()
        finally:
            self._preparing = False
        if self._stop_event.is_set():
            return
        if not self._paused:
            self._watcher.start()
        self._schema_rescans.start()
        self._insights.start()
        self._refresh_icon()

    def _health_check_loop(self) -> None:
        # Beat once before the first Neo4j check, which can take a while
        # (driver retries): `devgraph tray start` waits for this to say OK.
        self._write_heartbeat()
        while not self._stop_event.is_set():
            try:
                self._engine.verify_connectivity()
                recovered = not self._healthy
                self._healthy = True
                if not self._schema_ready and not self._preparing:
                    self._provision_schema()
                if recovered:
                    self._sync.retry_failed()
            except Exception:
                if self._stop_event.is_set():
                    break  # the engine refuses new sessions once shutdown closes it
                logger.warning("Neo4j health check failed", exc_info=True)
                self._healthy = False
            if self._stop_event.is_set():
                break
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
        self._icon.title = pystray_text(self._status_text())

    def _status_text(self) -> str:
        if self._preparing:
            state = "preparing graph indexes..."
        elif self._paused:
            state = "paused"
        elif not self._healthy:
            state = "warning"
        else:
            state = "catching up" if self._sync.running else "ok"
        if self._dashboard_problem is not None:
            state = f"{state}; {self._dashboard_problem.summary}"
        return f"DevGraph ({state})"

    def _toggle_pause(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:  # type: ignore[valid-type]
        self._paused = not self._paused
        self._sync.stopping = self._paused
        try:
            if self._paused:
                # Waits up to STOP_WAIT_S (3 s) for a running batch or
                # catch-up, so the menu can block that long.
                self._watcher.stop()
            elif not self._preparing:  # else _start_watching starts it, once the indexes are built
                self._watcher.start()
        except Exception:
            # A watcher start/stop failure (e.g. a repo path vanished) must
            # not crash the pystray event loop; revert the flag so the menu
            # state stays truthful.
            logger.warning("watcher pause/resume failed", exc_info=True)
            self._paused = not self._paused
            self._sync.stopping = self._paused
        self._refresh_icon()

    def _open_dashboard(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:  # type: ignore[valid-type]
        url = dashboard_url(self._settings)
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

        app = build_app(
            self._engine, self._registry, self._events, self._settings.dashboard_host,
            run_exclusive=self._watcher.run_exclusive,
        )
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
            # An open /api/events stream ends when shutdown closes the
            # broadcaster; this bounds any other connection still open.
            timeout_graceful_shutdown=1,
        )
        server = uvicorn.Server(config)
        self._dashboard_server = server
        try:
            logger.info("dashboard on %s", dashboard_url(self._settings))
            self._dashboard_problem = serve_dashboard(server, loop)
            if self._dashboard_problem is not None:
                # A port another program holds: said once, and shown on the icon.
                logger.warning("%s", self._dashboard_problem.detail)
                self._refresh_icon()
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

    def _quit(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:  # type: ignore[valid-type]
        self._stop_event.set()
        self._sync.stopping = True
        # Best-effort teardown: a failure closing the engine/registry must
        # not prevent the tray icon from stopping (which would leave a
        # zombie tray process the PID file still points at). Each close is
        # guarded so one failure doesn't mask the others.
        try:
            shutdown(
                self._engine, self._watcher, self._schema_rescans, self._insights,
                dashboard_server=self._dashboard_server, dashboard_thread=self._dashboard_thread,
                events=self._events,
            )
        except Exception:
            logger.warning("error closing graph engine on quit", exc_info=True)
        try:
            self._registry.close()
        except Exception:
            logger.warning("error closing registry on quit", exc_info=True)
        icon.stop()

    def start(self) -> None:
        self._preparing = True
        threading.Thread(target=self._start_watching, daemon=True).start()
        health_thread = threading.Thread(target=self._health_check_loop, daemon=True)
        health_thread.start()

        if self._settings.dashboard_enabled:
            self._dashboard_thread = threading.Thread(target=self._run_dashboard, daemon=True)
            self._dashboard_thread.start()

        menu = pystray.Menu(
            pystray.MenuItem(lambda item: pystray_text(self._status_text()), self._open_dashboard),
            pystray.MenuItem(
                lambda item: pystray_text("Resume watching" if self._paused else "Pause watching"),
                self._toggle_pause,
            ),
            pystray.MenuItem(pystray_text("Quit"), self._quit),
        )
        self._icon = pystray.Icon("devgraph", _make_icon(_OK_COLOR), pystray_text(self._status_text()), menu)
        try:
            if self._icon:
                self._icon.run()
        except Exception:
            logger.critical("pystray event loop crashed", exc_info=True)
            self._stop_event.set()
            self._sync.stopping = True
            try:
                shutdown(
                    self._engine, self._watcher, self._schema_rescans, self._insights,
                    dashboard_server=self._dashboard_server, dashboard_thread=self._dashboard_thread,
                    events=self._events,
                )
            except Exception:
                logger.warning("error closing graph engine after crash", exc_info=True)
            try:
                self._registry.close()
            except Exception:
                logger.warning("error closing registry after crash", exc_info=True)


def main() -> None:
    _configure_logging()
    if pystray is None:
        # No display or no tray backend (a server, an SSH session, a desktop
        # without a tray): run the same agent without an icon rather than
        # failing. `devgraph tray stop` stops it the same way (SIGTERM).
        from devgraph.agent import headless

        logger.warning("no system tray available (%s); running the headless agent instead", _PYSTRAY_PROBLEM)
        headless.main()
        return
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
