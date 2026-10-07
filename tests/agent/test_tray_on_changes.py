"""Tests for TrayApp._on_changes — the previously-missing wiring between the
watcher and the indexer. Uses a mocked engine/registry so this doesn't
require live Neo4j or an actual pystray icon.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from devgraph.registry.store import RepoRecord


@pytest.fixture
def tray_app():
    with patch("devgraph.agent.tray.get_settings") as mock_get_settings, \
         patch("devgraph.agent.tray.RepoRegistry") as mock_registry_cls, \
         patch("devgraph.agent.tray.GraphEngine") as mock_engine_cls, \
         patch("devgraph.agent.tray.WatcherManager"):
        mock_get_settings.return_value = MagicMock()
        mock_registry = MagicMock()
        mock_registry_cls.return_value = mock_registry
        mock_engine = MagicMock()
        mock_engine_cls.return_value = mock_engine

        from devgraph.agent.tray import TrayApp

        app = TrayApp()
        yield app


class TestOnChanges:
    def test_routes_changed_paths_to_index_paths(self, tray_app):
        repo_id = "test-repo"
        repo_path = Path(tempfile.gettempdir())
        tray_app._registry.get.return_value = RepoRecord(
            repo_id, repo_path, True, True, None, docs_path=None
        )

        with patch("devgraph.agent.sync.index_paths") as mock_index_paths, \
             patch("devgraph.agent.sync.remove_paths") as mock_remove_paths:
            changed = {repo_path / "a.py"}
            tray_app._on_changes(repo_id, changed, set())

            mock_index_paths.assert_called_once_with(
                tray_app._engine, repo_id, repo_path, changed, docs_path=None, mentions_enabled=False
            )
            mock_remove_paths.assert_not_called()
            tray_app._registry.mark_indexed.assert_called_once()
            assert tray_app._registry.mark_indexed.call_args.args == (repo_id,)
            assert tray_app._registry.mark_indexed.call_args.kwargs["at"] is not None

    def test_routes_deleted_paths_to_remove_paths(self, tray_app):
        repo_id = "test-repo"
        repo_path = Path(tempfile.gettempdir())
        tray_app._registry.get.return_value = RepoRecord(
            repo_id, repo_path, True, True, None, docs_path=None
        )

        with patch("devgraph.agent.sync.index_paths") as mock_index_paths, \
             patch("devgraph.agent.sync.remove_paths") as mock_remove_paths:
            deleted = {repo_path / "gone.py"}
            tray_app._on_changes(repo_id, set(), deleted)

            mock_remove_paths.assert_called_once_with(tray_app._engine, repo_id, repo_path, deleted)
            mock_index_paths.assert_not_called()

    def test_unknown_repo_id_is_a_noop(self, tray_app):
        tray_app._registry.get.return_value = None

        with patch("devgraph.agent.sync.index_paths") as mock_index_paths:
            tray_app._on_changes("gone-repo", {Path("x.py")}, set())
            mock_index_paths.assert_not_called()

    def test_indexing_failure_is_caught_not_raised(self, tray_app):
        repo_id = "test-repo"
        repo_path = Path(tempfile.gettempdir())
        tray_app._registry.get.return_value = RepoRecord(
            repo_id, repo_path, True, True, None, docs_path=None
        )

        with patch("devgraph.agent.sync.index_paths", side_effect=RuntimeError("boom")):
            # Should not raise — a failed reindex shouldn't crash the watcher thread.
            tray_app._on_changes(repo_id, {repo_path / "a.py"}, set())


class TestWiring:
    def test_the_watcher_is_wired_to_repo_sync_and_back(self, tray_app):
        from datetime import datetime, timezone

        from devgraph.agent import tray

        kwargs = tray.WatcherManager.call_args.kwargs
        assert kwargs["on_changes"] == tray_app._sync.on_changes
        assert kwargs["on_catch_up"] == tray_app._sync.on_catch_up
        since = datetime(2026, 10, 7, tzinfo=timezone.utc)
        tray_app._sync.retry_failed()  # nothing floored yet
        tray_app._watcher.request_catch_up.assert_not_called()
        tray_app._sync._request_catch_up("r", since, 30.0)
        tray_app._watcher.request_catch_up.assert_called_once_with("r", since, 30.0)

    def test_status_shows_catching_up_unless_paused_or_unhealthy(self, tray_app):
        assert tray_app._status_text() == "DevGraph (ok)"
        tray_app._sync._running = 1
        assert tray_app._status_text() == "DevGraph (catching up)"
        tray_app._healthy = False
        assert tray_app._status_text() == "DevGraph (warning)"
        tray_app._paused = True
        assert tray_app._status_text() == "DevGraph (paused)"

    def test_pause_and_quit_mark_the_sync_as_stopping(self, tray_app):
        tray_app._toggle_pause(MagicMock(), MagicMock())
        assert tray_app._sync.stopping is True
        tray_app._toggle_pause(MagicMock(), MagicMock())
        assert tray_app._sync.stopping is False
        with patch.object(tray_app, "_schema_rescans"), patch.object(tray_app, "_insights"):
            tray_app._quit(MagicMock(), MagicMock())
        assert tray_app._sync.stopping is True

    def test_recovering_health_retries_failed_repositories(self, tray_app):
        tray_app._healthy = False
        tray_app._engine.verify_connectivity.return_value = None
        calls = []
        tray_app._sync.retry_failed = lambda: calls.append(1)

        def stop_after_one(_interval):
            tray_app._stop_event.set()

        tray_app._stop_event.wait = stop_after_one
        tray_app._health_check_loop()
        assert calls == [1] and tray_app._healthy is True


class TestHeartbeat:
    def test_write_heartbeat_creates_timestamp_file(self, tray_app):
        with tempfile.TemporaryDirectory() as tmpdir:
            tray_app._settings.registry_db_path = Path(tmpdir) / "registry.sqlite3"
            tray_app._write_heartbeat()

            heartbeat_path = Path(tmpdir) / "tray_heartbeat.txt"
            assert heartbeat_path.exists()
            from datetime import datetime

            # Should parse as a valid ISO timestamp.
            datetime.fromisoformat(heartbeat_path.read_text(encoding="utf-8").strip())


class TestOpenDashboard:
    def test_wildcard_bind_opens_the_loopback_address(self, tray_app):
        """The Host guard refuses `0.0.0.0`; the menu must open an address it accepts."""
        from devgraph.config.settings import Settings

        tray_app._settings = Settings(dashboard_host="0.0.0.0", dashboard_port=8765)
        with patch("devgraph.agent.tray.webbrowser.open") as mock_open:
            tray_app._open_dashboard(MagicMock(), MagicMock())
        mock_open.assert_called_once_with("http://127.0.0.1:8765")
