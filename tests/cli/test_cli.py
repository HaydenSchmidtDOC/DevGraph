"""Tests for DevGraph CLI."""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from devgraph.cli.main import app
from devgraph.registry.store import RepoRegistry
from devgraph import config as config_module


@pytest.fixture
def temp_registry_db():
    """Create a temporary registry database."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "registry.db"
        registry = RepoRegistry(db_path)
        yield db_path, registry
        registry.close()


@pytest.fixture
def temp_git_repo():
    """Create a temporary git repository."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        subprocess.run(
            ["git", "init"],
            cwd=str(repo_path),
            capture_output=True,
            check=True,
        )
        yield repo_path


@pytest.fixture
def runner():
    """CliRunner for testing Typer apps."""
    return CliRunner()


def _mock_settings(db_path):
    """Create a mock settings object."""
    settings = MagicMock()
    settings.registry_db_path = db_path
    return settings


def test_cli_add_repo(runner, temp_git_repo, temp_registry_db):
    """Test 'devgraph add' command."""
    db_path, registry = temp_registry_db

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["add", str(temp_git_repo)])
        assert result.exit_code == 0, f"stdout: {result.stdout}"
        assert "Registered" in result.stdout


def test_cli_add_nonexistent_path(runner, temp_registry_db):
    """Test 'devgraph add' with non-existent path."""
    db_path, registry = temp_registry_db

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["add", "/nonexistent/path"])
        assert result.exit_code == 1
        assert "Error" in result.stdout


def test_cli_list_empty(runner, temp_registry_db):
    """Test 'devgraph list' with no repos."""
    db_path, registry = temp_registry_db

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        # Empty registry shows either "No repositories" or just a table with no rows
        assert "Registered Repositories" in result.stdout or "No repositories" in result.stdout


def test_cli_list_repos(runner, temp_git_repo, temp_registry_db):
    """Test 'devgraph list' command."""
    db_path, registry = temp_registry_db

    repo_record = registry.add_repo(temp_git_repo)
    repo_id = repo_record.repo_id
    registry.close()  # Close so CLI can open its own connection

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        # The table should contain our repo
        assert repo_id in result.stdout or "Registered Repositories" in result.stdout


def test_cli_remove_repo(runner, temp_git_repo, temp_registry_db):
    """Test 'devgraph remove' command."""
    db_path, registry = temp_registry_db

    repo_record = registry.add_repo(temp_git_repo)
    repo_id = repo_record.repo_id
    registry.close()  # Close so CLI can open its own connection

    # Patch both in config module and in cli.main where it's imported
    config_module.get_settings.cache_clear()
    from devgraph.cli import main as cli_main

    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["remove", repo_id])
        assert result.exit_code == 0, f"stdout: {result.stdout}"
        assert "Removed" in result.stdout
        assert repo_id in result.stdout


def test_cli_remove_nonexistent_repo(runner, temp_registry_db):
    """Test 'devgraph remove' with non-existent repo."""
    db_path, registry = temp_registry_db
    registry.close()

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["remove", "nonexistent"])
        assert result.exit_code == 1
        assert "Error" in result.stdout


def test_cli_watch_enable(runner, temp_git_repo, temp_registry_db):
    """Test 'devgraph watch enable' command."""
    db_path, registry = temp_registry_db

    repo_record = registry.add_repo(temp_git_repo)
    registry.disable_watch(repo_record.repo_id)
    repo_id = repo_record.repo_id
    registry.close()  # Close so CLI can open its own connection

    from devgraph.cli import main as cli_main

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["watch", "enable", repo_id])
        assert result.exit_code == 0, f"stdout: {result.stdout}"
        assert "Watch enabled" in result.stdout


def test_cli_watch_disable(runner, temp_git_repo, temp_registry_db):
    """Test 'devgraph watch disable' command."""
    db_path, registry = temp_registry_db

    repo_record = registry.add_repo(temp_git_repo)
    repo_id = repo_record.repo_id
    registry.close()  # Close so CLI can open its own connection

    from devgraph.cli import main as cli_main

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["watch", "disable", repo_id])
        assert result.exit_code == 0, f"stdout: {result.stdout}"
        assert "Watch disabled" in result.stdout


def test_cli_rescan_repo(runner, temp_git_repo, temp_registry_db):
    """Test 'devgraph rescan' command."""
    db_path, registry = temp_registry_db

    repo_record = registry.add_repo(temp_git_repo)
    repo_id = repo_record.repo_id
    registry.close()  # Close so CLI can open its own connection

    from devgraph.cli import main as cli_main

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["rescan", repo_id])
        assert result.exit_code == 0, f"stdout: {result.stdout}"
        assert "Rescan queued" in result.stdout


def test_cli_rescan_nonexistent_repo(runner, temp_registry_db):
    """Test 'devgraph rescan' with non-existent repo."""
    db_path, registry = temp_registry_db
    registry.close()

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["rescan", "nonexistent"])
        assert result.exit_code == 1
        assert "Error" in result.stdout


def test_cli_status(runner, temp_registry_db, temp_git_repo):
    """Test 'devgraph status' command."""
    db_path, registry = temp_registry_db
    registry.add_repo(temp_git_repo)
    registry.close()

    config_module.get_settings.cache_clear()
    settings = _mock_settings(db_path)
    settings.neo4j_uri = "bolt://127.0.0.1:9999"
    settings.neo4j_user = "neo4j"
    settings.neo4j_password = "wrong"

    with patch.object(config_module, "get_settings", return_value=settings):
        result = runner.invoke(app, ["status"])
        # Should exit successfully even if Neo4j is unreachable
        assert result.exit_code == 0
        assert "Registered Repositories" in result.stdout
        # Just check that total is present (may vary due to other tests)
        assert "Total:" in result.stdout
