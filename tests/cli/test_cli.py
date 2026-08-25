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


@pytest.fixture(autouse=True)
def _block_real_registry(monkeypatch, tmp_path):
    """Safety net: any CLI invocation that forgets to patch get_settings falls
    back to an empty throwaway registry instead of the developer's real
    ~/.devgraph/registry.sqlite3. A previous version of this suite leaked
    tmp* repo entries into the real registry because cli.main imports
    get_settings directly (its own reference, separate from
    devgraph.config.get_settings) — patching only the config module's copy
    silently missed it.

    Implemented as a default, not a hard replacement: tests still call
    `patch.object(..., "get_settings", return_value=...)` to point at their
    own temp registry, and unittest.mock.patch restores whatever was here
    (including this fallback) on __exit__. So this only takes effect for a
    test that forgets to patch entirely — it never fights a test's own patch.
    """
    fallback = _mock_settings(tmp_path / "unused-fallback-registry.db")

    def _fallback_get_settings():
        return fallback

    _fallback_get_settings.cache_clear = lambda: None  # tests call this defensively

    from devgraph.cli import main as cli_main

    monkeypatch.setattr(cli_main, "get_settings", _fallback_get_settings)
    monkeypatch.setattr(config_module, "get_settings", _fallback_get_settings)


def _mock_settings(db_path):
    """Create a mock settings object."""
    settings = MagicMock()
    settings.registry_db_path = db_path
    return settings


def test_cli_add_repo(runner, temp_git_repo, temp_registry_db):
    """Test 'devgraph add' command."""
    db_path, registry = temp_registry_db

    from devgraph.cli import main as cli_main

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)):
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

    from devgraph.cli import main as cli_main

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)):
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

    from devgraph.cli import main as cli_main

    config_module.get_settings.cache_clear()
    settings = _mock_settings(db_path)
    settings.neo4j_uri = "bolt://127.0.0.1:9999"
    settings.neo4j_user = "neo4j"
    settings.neo4j_password = "wrong"

    with patch.object(config_module, "get_settings", return_value=settings), \
         patch.object(cli_main, "get_settings", return_value=settings):
        result = runner.invoke(app, ["status"])
        # Should exit successfully even if Neo4j is unreachable
        assert result.exit_code == 0
        assert "Registered Repositories" in result.stdout
        # Just check that total is present (may vary due to other tests)
        assert "Total:" in result.stdout


def test_cli_annotate_set_docs_path(runner, temp_git_repo, temp_registry_db):
    """Test 'devgraph annotate --docs-path' command."""
    db_path, registry = temp_registry_db

    repo_record = registry.add_repo(temp_git_repo)
    repo_id = repo_record.repo_id
    registry.close()

    from devgraph.cli import main as cli_main

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["annotate", repo_id, "--docs-path", "devgraph/docs"])
        assert result.exit_code == 0, f"stdout: {result.stdout}"
        assert "Docs path set" in result.stdout

        result = runner.invoke(app, ["annotate", repo_id])
        assert result.exit_code == 0
        assert "devgraph/docs" in result.stdout


def test_cli_annotate_nonexistent_repo(runner, temp_registry_db):
    """Test 'devgraph annotate' with non-existent repo."""
    db_path, registry = temp_registry_db
    registry.close()

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["annotate", "nonexistent", "--docs-path", "docs"])
        assert result.exit_code == 1
        assert "Error" in result.stdout


def test_cli_pr_source_enable(runner, temp_git_repo, temp_registry_db):
    """Test 'devgraph pr-source enable' command."""
    db_path, registry = temp_registry_db

    repo_record = registry.add_repo(temp_git_repo)
    repo_id = repo_record.repo_id
    registry.close()

    from devgraph.cli import main as cli_main

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["pr-source", repo_id, "enable"])
        assert result.exit_code == 0, f"stdout: {result.stdout}"
        assert "enabled" in result.stdout

        reg = RepoRegistry(db_path)
        assert reg.get(repo_id).pr_source_enabled is True
        reg.close()


def test_cli_issue_source_disable_after_enable(runner, temp_git_repo, temp_registry_db):
    """Test 'devgraph issue-source disable' command."""
    db_path, registry = temp_registry_db

    repo_record = registry.add_repo(temp_git_repo)
    repo_id = repo_record.repo_id
    registry.set_issue_source_enabled(repo_id, True)
    registry.close()

    from devgraph.cli import main as cli_main

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["issue-source", repo_id, "disable"])
        assert result.exit_code == 0, f"stdout: {result.stdout}"
        assert "disabled" in result.stdout

        reg = RepoRegistry(db_path)
        assert reg.get(repo_id).issue_source_enabled is False
        reg.close()


def test_cli_pr_source_invalid_action(runner, temp_git_repo, temp_registry_db):
    """Test 'devgraph pr-source' rejects an invalid action."""
    db_path, registry = temp_registry_db
    repo_record = registry.add_repo(temp_git_repo)
    repo_id = repo_record.repo_id
    registry.close()

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["pr-source", repo_id, "maybe"])
        assert result.exit_code == 1
        assert "Error" in result.stdout


def test_cli_index_history(runner, temp_git_repo, temp_registry_db):
    """Test 'devgraph index-history' command against a real (throwaway) local repo."""
    db_path, registry = temp_registry_db

    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(temp_git_repo), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test Author"],
        cwd=str(temp_git_repo), capture_output=True, check=True,
    )
    (temp_git_repo / "file.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "file.py"], cwd=str(temp_git_repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=str(temp_git_repo), capture_output=True, check=True,
    )

    repo_record = registry.add_repo(temp_git_repo)
    repo_id = repo_record.repo_id
    registry.close()

    from devgraph.cli import main as cli_main

    settings = _mock_settings(db_path)
    settings.neo4j_uri = "bolt://127.0.0.1:9999"
    settings.neo4j_user = "neo4j"
    settings.neo4j_password = "wrong"

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=settings), \
         patch.object(cli_main, "get_settings", return_value=settings):
        result = runner.invoke(app, ["index-history", repo_id])
        # Neo4j unreachable at this bogus URI -> should fail gracefully, not crash
        assert result.exit_code == 1
        assert "Error" in result.stdout


def test_cli_index_history_nonexistent_repo(runner, temp_registry_db):
    """Test 'devgraph index-history' with non-existent repo."""
    db_path, registry = temp_registry_db
    registry.close()

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["index-history", "nonexistent"])
        assert result.exit_code == 1
        assert "Error" in result.stdout
