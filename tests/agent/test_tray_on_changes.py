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
        app._registry = mock_registry
        app._engine = mock_engine
        yield app


class TestOnChanges:
    def test_routes_changed_paths_to_index_paths(self, tray_app):
        repo_id = "test-repo"
        repo_path = Path(tempfile.gettempdir())
        tray_app._registry.get.return_value = RepoRecord(
            repo_id, repo_path, True, True, None, docs_path=None
        )

        with patch("devgraph.agent.tray.index_paths") as mock_index_paths, \
             patch("devgraph.agent.tray.remove_paths") as mock_remove_paths:
            changed = {repo_path / "a.py"}
            tray_app._on_changes(repo_id, changed, set())

            mock_index_paths.assert_called_once_with(
                tray_app._engine, repo_id, repo_path, changed, docs_path=None
            )
            mock_remove_paths.assert_not_called()
            tray_app._registry.mark_indexed.assert_called_once_with(repo_id)

    def test_routes_deleted_paths_to_remove_paths(self, tray_app):
        repo_id = "test-repo"
        repo_path = Path(tempfile.gettempdir())
        tray_app._registry.get.return_value = RepoRecord(
            repo_id, repo_path, True, True, None, docs_path=None
        )

        with patch("devgraph.agent.tray.index_paths") as mock_index_paths, \
             patch("devgraph.agent.tray.remove_paths") as mock_remove_paths:
            deleted = {repo_path / "gone.py"}
            tray_app._on_changes(repo_id, set(), deleted)

            mock_remove_paths.assert_called_once_with(tray_app._engine, repo_id, repo_path, deleted)
            mock_index_paths.assert_not_called()

    def test_unknown_repo_id_is_a_noop(self, tray_app):
        tray_app._registry.get.return_value = None

        with patch("devgraph.agent.tray.index_paths") as mock_index_paths:
            tray_app._on_changes("gone-repo", {Path("x.py")}, set())
            mock_index_paths.assert_not_called()

    def test_indexing_failure_is_caught_not_raised(self, tray_app):
        repo_id = "test-repo"
        repo_path = Path(tempfile.gettempdir())
        tray_app._registry.get.return_value = RepoRecord(
            repo_id, repo_path, True, True, None, docs_path=None
        )

        with patch("devgraph.agent.tray.index_paths", side_effect=RuntimeError("boom")):
            # Should not raise — a failed reindex shouldn't crash the watcher thread.
            tray_app._on_changes(repo_id, {repo_path / "a.py"}, set())
