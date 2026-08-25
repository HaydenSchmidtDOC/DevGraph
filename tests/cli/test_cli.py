"""Tests for DevGraph CLI."""

import json
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


@pytest.fixture
def require_neo4j():
    """Skip the test if the local devgraph-neo4j instance isn't reachable.

    add/rescan now run a real indexing scan against Neo4j, unlike before.
    """
    from devgraph.graph.engine import GraphEngine

    engine = GraphEngine("bolt://127.0.0.1:7687", "neo4j", "devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    finally:
        engine.close()


@pytest.fixture(autouse=True)
def _block_real_registry(monkeypatch, tmp_path):
    """Safety net: any CLI invocation that forgets to patch get_settings falls
    back to an empty throwaway registry instead of the developer's real
    ~/.devgraph/registry.sqlite3 (and, critically for the tray commands, the
    developer's real tray.pid/tray_holders — without this, a tray test can
    read/kill/report on a genuinely-running tray process on the machine
    running the tests). A previous version of this suite leaked tmp* repo
    entries into the real registry because cli.main imports get_settings
    directly (its own reference, separate from devgraph.config.get_settings)
    — patching only the config module's copy silently missed it. The same
    pitfall recurred when tray lifecycle logic moved into
    devgraph.agent.lifecycle, which has its own get_settings reference too.

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

    from devgraph.agent import lifecycle
    from devgraph.cli import main as cli_main

    monkeypatch.setattr(cli_main, "get_settings", _fallback_get_settings)
    monkeypatch.setattr(config_module, "get_settings", _fallback_get_settings)
    monkeypatch.setattr(lifecycle, "get_settings", _fallback_get_settings)


def _mock_settings(db_path):
    """Create a mock settings object pointed at the real test Neo4j instance.

    add/rescan now run a real indexing scan, so they need a genuinely
    reachable Neo4j — same instance every other live-Neo4j test in this repo
    uses. Tests that want an unreachable instance (e.g. status's failure
    path) override neo4j_uri/user/password after calling this.
    """
    settings = MagicMock()
    settings.registry_db_path = db_path
    settings.neo4j_uri = "bolt://127.0.0.1:7687"
    settings.neo4j_user = "neo4j"
    settings.neo4j_password = "devgraph-local-dev"
    settings.enable_run_cypher = False
    return settings


def test_cli_add_repo(runner, temp_git_repo, temp_registry_db, require_neo4j):
    """Test 'devgraph add' command — now runs a real initial scan."""
    db_path, registry = temp_registry_db

    (temp_git_repo / "module.py").write_text("class Foo:\n    pass\n")

    from devgraph.cli import main as cli_main

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["add", str(temp_git_repo)])
        assert result.exit_code == 0, f"stdout: {result.stdout}"
        assert "Registered" in result.stdout
        assert "Indexed" in result.stdout

    from devgraph.graph.engine import GraphEngine
    from devgraph.registry.store import RepoRegistry

    verify_registry = RepoRegistry(db_path)
    repo_id = verify_registry.list_repos()[0].repo_id
    verify_registry.close()

    engine = GraphEngine("bolt://127.0.0.1:7687", "neo4j", "devgraph-local-dev")
    try:
        found = engine.run_cypher(
            "MATCH (n {repo_id: $repo_id}) RETURN COUNT(*) as c", {"repo_id": repo_id}
        )
        assert found[0]["c"] > 0
    finally:
        engine.delete_repository(repo_id)
        engine.close()


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


def test_cli_rescan_repo(runner, temp_git_repo, temp_registry_db, require_neo4j):
    """Test 'devgraph rescan' command — now runs a real full scan."""
    db_path, registry = temp_registry_db

    (temp_git_repo / "module.py").write_text("class Bar:\n    pass\n")

    repo_record = registry.add_repo(temp_git_repo)
    repo_id = repo_record.repo_id
    registry.close()  # Close so CLI can open its own connection

    from devgraph.cli import main as cli_main

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["rescan", repo_id])
        assert result.exit_code == 0, f"stdout: {result.stdout}"
        assert "Rescanned" in result.stdout

    from devgraph.graph.engine import GraphEngine

    engine = GraphEngine("bolt://127.0.0.1:7687", "neo4j", "devgraph-local-dev")
    try:
        found = engine.run_cypher(
            "MATCH (c:Class {repo_id: $repo_id, name: 'Bar'}) RETURN COUNT(*) as c", {"repo_id": repo_id}
        )
        assert found[0]["c"] == 1
    finally:
        engine.delete_repository(repo_id)
        engine.close()


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


def test_cli_add_full_flag_also_indexes_history(runner, temp_registry_db, require_neo4j):
    """Test 'devgraph add --full' runs both the file scan and history indexing."""
    db_path, registry = temp_registry_db

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        subprocess.run(["git", "init"], cwd=str(repo_path), capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(repo_path), capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test Author"],
            cwd=str(repo_path), capture_output=True, check=True,
        )
        (repo_path / "module.py").write_text("class Foo:\n    pass\n")
        subprocess.run(["git", "add", "module.py"], cwd=str(repo_path), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=str(repo_path), capture_output=True, check=True,
        )

        from devgraph.cli import main as cli_main

        config_module.get_settings.cache_clear()
        with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
             patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)):
            result = runner.invoke(app, ["add", str(repo_path), "--full"])
            assert result.exit_code == 0, f"stdout: {result.stdout}"
            assert "Indexed" in result.stdout
            assert "commit(s)" in result.stdout

        from devgraph.graph.engine import GraphEngine

        verify_registry = RepoRegistry(db_path)
        repo_id = verify_registry.list_repos()[0].repo_id
        verify_registry.close()

        engine = GraphEngine("bolt://127.0.0.1:7687", "neo4j", "devgraph-local-dev")
        try:
            found = engine.run_cypher(
                "MATCH (c:Commit {repo_id: $repo_id}) RETURN COUNT(*) as c", {"repo_id": repo_id}
            )
            assert found[0]["c"] >= 1
        finally:
            engine.delete_repository(repo_id)
            engine.close()


def test_cli_doctor_runs_without_crashing(runner, temp_registry_db):
    """Test 'devgraph doctor' runs all checks and reports pass/fail without crashing."""
    db_path, registry = temp_registry_db
    registry.close()

    from devgraph.cli import main as cli_main

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["doctor"])
        assert "Python" in result.stdout
        assert "Neo4j" in result.stdout
        assert "Live Watcher" in result.stdout


def test_cli_client_config_prints_resolved_paths(runner, temp_registry_db):
    """Test 'devgraph client-config' prints a portable command, not a hardcoded path."""
    db_path, registry = temp_registry_db
    registry.close()

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["client-config"])
        assert result.exit_code == 0, f"stdout: {result.stdout}"
        assert "devgraph.mcp.server" in result.stdout
        assert "claude mcp add" in result.stdout


def test_cli_client_config_mcp_add_only(runner, temp_registry_db):
    """Test 'devgraph client-config --claude-mcp-add-only' prints just the one-liner."""
    db_path, registry = temp_registry_db
    registry.close()

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["client-config", "--claude-mcp-add-only"])
        assert result.exit_code == 0, f"stdout: {result.stdout}"
        # Rich's console may soft-wrap a long line across multiple terminal
        # rows — join before asserting on content rather than counting lines.
        collapsed = " ".join(l.strip() for l in result.stdout.strip().splitlines())
        assert collapsed.startswith("claude mcp add devgraph")
        assert "devgraph.mcp.server" in collapsed


def test_cli_client_config_vscode_creates_new_mcp_json(runner, temp_registry_db, monkeypatch):
    """'devgraph client-config --target vscode --run' creates mcp.json when none exists."""
    db_path, registry = temp_registry_db
    registry.close()

    with tempfile.TemporaryDirectory() as appdata_dir:
        monkeypatch.setenv("APPDATA", appdata_dir)

        config_module.get_settings.cache_clear()
        with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)):
            result = runner.invoke(app, ["client-config", "--target", "vscode", "--run"])
            assert result.exit_code == 0, f"stdout: {result.stdout}"

        mcp_json = Path(appdata_dir) / "Code" / "User" / "mcp.json"
        assert mcp_json.exists()
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        assert data["servers"]["devgraph"]["args"] == ["-m", "devgraph.mcp.server"]
        assert data["servers"]["devgraph"]["type"] == "stdio"


def test_cli_client_config_vscode_preserves_existing_servers(runner, temp_registry_db, monkeypatch):
    """Registering devgraph must not clobber other servers already in mcp.json."""
    db_path, registry = temp_registry_db
    registry.close()

    with tempfile.TemporaryDirectory() as appdata_dir:
        monkeypatch.setenv("APPDATA", appdata_dir)
        mcp_dir = Path(appdata_dir) / "Code" / "User"
        mcp_dir.mkdir(parents=True)
        existing = {"servers": {"other-server": {"type": "stdio", "command": "other.exe", "args": []}}}
        (mcp_dir / "mcp.json").write_text(json.dumps(existing), encoding="utf-8")

        config_module.get_settings.cache_clear()
        with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)):
            result = runner.invoke(app, ["client-config", "--target", "vscode", "--run"])
            assert result.exit_code == 0, f"stdout: {result.stdout}"

        data = json.loads((mcp_dir / "mcp.json").read_text(encoding="utf-8"))
        assert "other-server" in data["servers"]
        assert data["servers"]["other-server"]["command"] == "other.exe"
        assert "devgraph" in data["servers"]


def test_cli_client_config_vscode_idempotent(runner, temp_registry_db, monkeypatch):
    """Running vscode registration twice produces the same devgraph entry."""
    db_path, registry = temp_registry_db
    registry.close()

    with tempfile.TemporaryDirectory() as appdata_dir:
        monkeypatch.setenv("APPDATA", appdata_dir)

        config_module.get_settings.cache_clear()
        with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)):
            runner.invoke(app, ["client-config", "--target", "vscode", "--run"])
            first = json.loads((Path(appdata_dir) / "Code" / "User" / "mcp.json").read_text(encoding="utf-8"))
            runner.invoke(app, ["client-config", "--target", "vscode", "--run"])
            second = json.loads((Path(appdata_dir) / "Code" / "User" / "mcp.json").read_text(encoding="utf-8"))

        assert first == second


def test_cli_client_config_claude_skips_when_already_registered(runner, temp_registry_db):
    """'client-config --target claude --run' must not fail when 'claude mcp add'
    would error because the server is already registered (real observed
    behavior: 'claude mcp add' exits 1, not 0, for an existing same-path
    entry) -- check via 'claude mcp get' first and skip cleanly instead."""
    db_path, registry = temp_registry_db
    registry.close()

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        if cmd[1:3] == ["mcp", "get"]:
            result.returncode = 0  # already registered
        else:
            result.returncode = 1  # would fail if actually called
        return result

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch("devgraph.cli.main.shutil.which", return_value="/usr/bin/claude"), \
         patch("devgraph.cli.main.subprocess.run", side_effect=fake_run) as mock_run:
        result = runner.invoke(app, ["client-config", "--target", "claude", "--run"])
        assert result.exit_code == 0, f"stdout: {result.stdout}"
        add_calls = [c for c in mock_run.call_args_list if "add" in c.args[0]]
        assert not add_calls, "should not call 'claude mcp add' when already registered"


def test_cli_client_config_invalid_target(runner, temp_registry_db):
    """An unrecognized --target value fails cleanly."""
    db_path, registry = temp_registry_db
    registry.close()

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["client-config", "--target", "bogus"])
        assert result.exit_code != 0


def test_cli_tray_status_not_running(runner, temp_registry_db):
    """'devgraph tray status' reports not-running when no PID file exists."""
    db_path, registry = temp_registry_db
    registry.close()

    from devgraph.agent import lifecycle

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(lifecycle, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["tray", "status"])
        assert result.exit_code == 0, f"stdout: {result.stdout}"
        assert "not running" in result.stdout
        assert "devgraph tray start" in result.stdout


def test_cli_tray_start_then_status_then_stop(runner, temp_registry_db):
    """'devgraph tray start' records a PID that 'status'/'stop' then recognize.

    Spawning the real tray app would require pystray/a live Neo4j and a GUI
    tray context, so this patches subprocess.Popen with a fake process object
    and patches the liveness probe to track that fake PID as alive until
    'stop' is called — exercising the PID-file lifecycle without the
    real OS-level process.
    """
    db_path, registry = temp_registry_db
    registry.close()

    from devgraph.agent import lifecycle
    from devgraph.cli import main as cli_main

    fake_pid = 999999
    fake_process = MagicMock()
    fake_process.pid = fake_pid
    alive = {fake_pid}

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(lifecycle, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(lifecycle.subprocess, "Popen", return_value=fake_process), \
         patch.object(lifecycle, "pid_is_running", side_effect=lambda pid: pid in alive), \
         patch.object(lifecycle, "resolve_venv_python", return_value=Path("python")), \
         patch.object(lifecycle, "resolve_repo_root", return_value=Path(".")):
        start_result = runner.invoke(app, ["tray", "start"])
        assert start_result.exit_code == 0, f"stdout: {start_result.stdout}"
        assert str(fake_pid) in start_result.stdout

        status_result = runner.invoke(app, ["tray", "status"])
        assert "running" in status_result.stdout
        assert str(fake_pid) in status_result.stdout

        # Starting again while "alive" should be a no-op, not a second spawn.
        restart_result = runner.invoke(app, ["tray", "start"])
        assert "already running" in restart_result.stdout
        lifecycle.subprocess.Popen.assert_called_once()

        with patch.object(cli_main.os, "kill") as fake_kill:
            stop_result = runner.invoke(app, ["tray", "stop"])
            assert stop_result.exit_code == 0, f"stdout: {stop_result.stdout}"
            fake_kill.assert_called_once_with(fake_pid, cli_main.signal.SIGTERM)
        alive.discard(fake_pid)

        final_status = runner.invoke(app, ["tray", "status"])
        assert "not running" in final_status.stdout
