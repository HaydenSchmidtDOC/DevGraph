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


def test_cli_remove_repo(runner, temp_git_repo, temp_registry_db, require_neo4j):
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


def test_cli_remove_repo_purges_graph_data(runner, temp_git_repo, temp_registry_db, require_neo4j):
    """'devgraph remove' must delete the repo's Neo4j nodes, not just its registry row."""
    from devgraph.graph.engine import GraphEngine

    db_path, registry = temp_registry_db

    repo_record = registry.add_repo(temp_git_repo)
    repo_id = repo_record.repo_id
    registry.close()

    engine = GraphEngine("bolt://127.0.0.1:7687", "neo4j", "devgraph-local-dev")
    try:
        engine.init_schema()
        engine.upsert_repository(repo_id, repo_id, str(temp_git_repo))

        config_module.get_settings.cache_clear()
        from devgraph.cli import main as cli_main

        with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
             patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)):
            result = runner.invoke(app, ["remove", repo_id])
            assert result.exit_code == 0, f"stdout: {result.stdout}"

        remaining = engine.run_cypher(
            "MATCH (n {repo_id: $repo_id}) RETURN count(n) AS c", {"repo_id": repo_id}
        )
        assert remaining[0]["c"] == 0
    finally:
        engine.delete_repository(repo_id)
        engine.close()


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
        # Neo4j unreachable at this bogus URI -> handled failure, not a crash.
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        assert "[X] Unexpected error:" in result.stdout
        assert "127.0.0.1:9999" in result.stdout
        assert "[OK]" not in result.stdout
        assert "Traceback" not in result.stdout


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
        assert "Indexer extractors" in result.stdout


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
        # These tests exercise merging/writing, independently of the host OS.
        monkeypatch.setattr("devgraph.cli.main._vscode_mcp_config_path",
                            lambda: Path(appdata_dir) / "Code" / "User" / "mcp.json")

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
        # These tests exercise merging/writing, independently of the host OS.
        monkeypatch.setattr("devgraph.cli.main._vscode_mcp_config_path",
                            lambda: Path(appdata_dir) / "Code" / "User" / "mcp.json")
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
        # These tests exercise merging/writing, independently of the host OS.
        monkeypatch.setattr("devgraph.cli.main._vscode_mcp_config_path",
                            lambda: Path(appdata_dir) / "Code" / "User" / "mcp.json")

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


@pytest.mark.parametrize("platform, environment, relative", [
    ("win32", {"APPDATA": "roaming"}, "roaming/Code/User/mcp.json"),
    ("darwin", {}, "home/Library/Application Support/Code/User/mcp.json"),
    ("linux", {}, "home/.config/Code/User/mcp.json"),
    ("linux", {"XDG_CONFIG_HOME": "xdg"}, "xdg/Code/User/mcp.json"),
    ("linux", {"XDG_CONFIG_HOME": ""}, "home/.config/Code/User/mcp.json"),
])
def test_vscode_mcp_config_path_by_platform(monkeypatch, tmp_path, platform, environment, relative):
    from types import SimpleNamespace
    from devgraph.cli import main as cli_main

    monkeypatch.setattr(cli_main, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    for name in ("APPDATA", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, str(tmp_path / value) if value else "")
    assert cli_main._vscode_mcp_config_path() == tmp_path / relative


def test_vscode_mcp_config_path_windows_requires_appdata(monkeypatch):
    from types import SimpleNamespace
    from devgraph.cli import main as cli_main

    monkeypatch.setattr(cli_main, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.delenv("APPDATA", raising=False)
    with pytest.raises(RuntimeError, match="APPDATA"):
        cli_main._vscode_mcp_config_path()


# --- Project schema constraint provisioning ---------------------------------
#
# These are Neo4j-free on purpose: they pin *when* a repository's optional
# `devgraph.schema.yaml` is resolved relative to the first graph write, which a
# live database can't show as precisely. A stub engine stands in for
# GraphEngine so an invalid file can be planted on demand.

WIDGET_SCHEMA = """\
version: 1
node_types:
  - label: Widget
    key: [slug]
    metadata:
      - name: slug
"""

# `key` is required, so this loads and then fails validation.
INVALID_SCHEMA = "version: 1\nnode_types:\n  - label: Widget\n"

WIDGET_CONSTRAINT = (
    "CREATE CONSTRAINT widget_repo_key IF NOT EXISTS "
    "FOR (n:Widget) REQUIRE (n.repo_id, n.slug) IS UNIQUE"
)


class _StubEngine:
    """Records provisioning and writes, in order, without touching Neo4j."""

    def __init__(self):
        self.calls = []
        self.effective_schemas = []

    def init_schema(self, effective=None):
        self.effective_schemas.append(effective)
        self.calls.append("init_schema")

    def upsert_repository(self, repo_id, name, path):
        self.calls.append("upsert_repository")

    def close(self):
        self.calls.append("close")


def _repo_with_schema(tmp_path, name, schema=None):
    """A directory `add_repo` accepts, optionally carrying a schema file.

    `add_repo` gates on a `.git` entry existing and nothing more, and the scan
    is stubbed here, so no `git init` subprocess is needed.
    """
    from devgraph.config.project_schema import SCHEMA_FILENAME

    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    if schema is not None:
        (repo / SCHEMA_FILENAME).write_text(schema, encoding="utf-8")
    return repo


def _collapsed(stdout):
    """Rich soft-wraps long lines; join before asserting on a phrase."""
    return " ".join(line.strip() for line in stdout.splitlines())


def test_cli_rescan_provisions_the_repositorys_declared_constraints(
    runner, temp_registry_db, tmp_path
):
    db_path, registry = temp_registry_db
    repo = _repo_with_schema(tmp_path, "widgets", WIDGET_SCHEMA)
    repo_id = registry.add_repo(repo).repo_id
    registry.close()

    from devgraph.cli import main as cli_main
    from devgraph.graph.engine import repository_constraint_statements
    from devgraph.graph.schema import constraint_statements

    engine = _StubEngine()
    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "GraphEngine", lambda *a, **k: engine), \
         patch.object(cli_main, "full_scan", lambda *a, **k: 3), \
         patch.object(cli_main, "sync_git_history",
                      lambda *a, **k: {"commits_indexed": 0, "commits_deleted": 0}):
        result = runner.invoke(app, ["rescan", repo_id])

    assert result.exit_code == 0, f"stdout: {result.stdout}"
    assert engine.calls == ["init_schema", "upsert_repository", "close"]
    statements = repository_constraint_statements(engine.effective_schemas[0])
    builtins = constraint_statements()
    assert statements[: len(builtins)] == builtins
    assert statements[len(builtins):] == [WIDGET_CONSTRAINT]


def test_cli_rescan_without_a_schema_file_provisions_only_the_builtins(
    runner, temp_registry_db, tmp_path
):
    """An already-registered repository with no configuration is unchanged."""
    db_path, registry = temp_registry_db
    repo = _repo_with_schema(tmp_path, "plain")
    repo_id = registry.add_repo(repo).repo_id
    registry.close()

    from devgraph.cli import main as cli_main
    from devgraph.graph.engine import repository_constraint_statements
    from devgraph.graph.schema import constraint_statements

    engine = _StubEngine()
    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "GraphEngine", lambda *a, **k: engine), \
         patch.object(cli_main, "full_scan", lambda *a, **k: 0), \
         patch.object(cli_main, "sync_git_history",
                      lambda *a, **k: {"commits_indexed": 0, "commits_deleted": 0}):
        result = runner.invoke(app, ["rescan", repo_id])

    assert result.exit_code == 0, f"stdout: {result.stdout}"
    # Resolution still happens (it is how "no file" is established), and it
    # resolves to exactly the built-in statements -- statement for statement.
    assert engine.calls == ["init_schema", "upsert_repository", "close"]
    assert repository_constraint_statements(engine.effective_schemas[0]) == constraint_statements()


def test_cli_rescan_with_an_invalid_schema_fails_before_any_graph_write(
    runner, temp_registry_db, tmp_path
):
    db_path, registry = temp_registry_db
    repo = _repo_with_schema(tmp_path, "broken", INVALID_SCHEMA)
    repo_id = registry.add_repo(repo).repo_id
    registry.close()

    from devgraph.cli import main as cli_main

    engine = _StubEngine()
    scans = []
    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "GraphEngine", lambda *a, **k: engine), \
         patch.object(cli_main, "full_scan", lambda *a, **k: scans.append(a)):
        result = runner.invoke(app, ["rescan", repo_id])

    assert result.exit_code == 1, f"stdout: {result.stdout}"
    assert "invalid project schema" in _collapsed(result.stdout)
    # Nothing was provisioned, nothing was upserted, nothing was scanned --
    # the engine was only ever closed.
    assert engine.calls == ["close"]
    assert scans == []

    verify = RepoRegistry(db_path)
    try:
        assert verify.get(repo_id).last_indexed is None
    finally:
        verify.close()


def test_cli_add_with_an_invalid_schema_keeps_the_repo_registered(
    runner, temp_registry_db, tmp_path
):
    """Same registered-with-warning contract `add` already has for a down Neo4j."""
    db_path, registry = temp_registry_db
    registry.close()
    repo = _repo_with_schema(tmp_path, "broken", INVALID_SCHEMA)

    from devgraph.cli import main as cli_main

    engine = _StubEngine()
    scans = []
    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "GraphEngine", lambda *a, **k: engine), \
         patch.object(cli_main, "full_scan", lambda *a, **k: scans.append(a)):
        result = runner.invoke(app, ["add", str(repo)])

    assert result.exit_code == 0, f"stdout: {result.stdout}"
    collapsed = _collapsed(result.stdout)
    assert "Registered but initial scan failed" in collapsed
    assert "invalid project schema" in collapsed
    assert engine.calls == ["close"]
    assert scans == []

    verify = RepoRegistry(db_path)
    try:
        record = verify.get("broken")
        assert record is not None
        assert record.last_indexed is None
    finally:
        verify.close()


def test_project_schema_findings_report_absent_valid_and_invalid(temp_registry_db, tmp_path):
    from devgraph.cli.main import _project_schema_findings

    db_path, registry = temp_registry_db
    registry.add_repo(_repo_with_schema(tmp_path, "plain"))
    registry.add_repo(_repo_with_schema(tmp_path, "widgets", WIDGET_SCHEMA))
    registry.add_repo(_repo_with_schema(tmp_path, "broken", INVALID_SCHEMA))

    findings = _project_schema_findings(registry.list_repos())

    by_repo = {f["repo_id"]: f for f in findings}
    assert by_repo["plain"]["status"] == "absent"
    assert by_repo["plain"]["failed"] is False
    assert by_repo["widgets"]["status"] == "valid"
    assert by_repo["widgets"]["failed"] is False
    assert "Widget" in by_repo["widgets"]["detail"]
    assert by_repo["broken"]["status"] == "invalid"
    assert by_repo["broken"]["failed"] is True
    assert "invalid project schema" in by_repo["broken"]["detail"]
    # Deterministic order, and no conflict between unrelated declarations.
    assert [f["repo_id"] for f in findings] == ["broken", "plain", "widgets"]


def test_project_schema_findings_are_empty_without_repositories():
    """An unreadable registry degrades to this too, rather than crashing."""
    from devgraph.cli.main import _project_schema_findings

    assert _project_schema_findings([]) == []


def test_project_schema_findings_flag_incompatible_same_label_keys(temp_registry_db, tmp_path):
    """Two repositories, one shared database, one constraint name."""
    from devgraph.cli.main import _project_schema_findings

    db_path, registry = temp_registry_db
    registry.add_repo(_repo_with_schema(tmp_path, "first", WIDGET_SCHEMA))
    registry.add_repo(
        _repo_with_schema(
            tmp_path,
            "second",
            "version: 1\n"
            "node_types:\n"
            "  - label: Widget\n"
            "    key: [code]\n"
            "    metadata:\n"
            "      - name: code\n",
        )
    )

    conflicts = [f for f in _project_schema_findings(registry.list_repos()) if f["status"] == "conflict"]

    assert len(conflicts) == 1
    assert conflicts[0]["failed"] is True
    assert conflicts[0]["label"] == "widget"
    assert "first declares Widget keyed on (slug)" in conflicts[0]["detail"]
    assert "second declares Widget keyed on (code)" in conflicts[0]["detail"]


def test_project_schema_findings_allow_an_identical_shared_declaration(temp_registry_db, tmp_path):
    """The same label with the same key provisions one identical constraint."""
    from devgraph.cli.main import _project_schema_findings

    db_path, registry = temp_registry_db
    registry.add_repo(_repo_with_schema(tmp_path, "first", WIDGET_SCHEMA))
    registry.add_repo(_repo_with_schema(tmp_path, "second", WIDGET_SCHEMA))

    findings = _project_schema_findings(registry.list_repos())

    assert [f["status"] for f in findings] == ["valid", "valid"]
    assert not any(f["failed"] for f in findings)


def test_project_schema_findings_flag_a_case_only_label_difference(temp_registry_db, tmp_path):
    """Constraint names are lower-cased, so `widget` would silently no-op."""
    from devgraph.cli.main import _project_schema_findings

    db_path, registry = temp_registry_db
    registry.add_repo(_repo_with_schema(tmp_path, "first", WIDGET_SCHEMA))
    registry.add_repo(
        _repo_with_schema(
            tmp_path,
            "second",
            "version: 1\n"
            "node_types:\n"
            "  - label: widget\n"
            "    key: [slug]\n"
            "    metadata:\n"
            "      - name: slug\n",
        )
    )

    conflicts = [f for f in _project_schema_findings(registry.list_repos()) if f["status"] == "conflict"]

    assert len(conflicts) == 1
    assert conflicts[0]["failed"] is True


def test_cli_doctor_reports_an_invalid_project_schema(runner, temp_registry_db, tmp_path):
    db_path, registry = temp_registry_db
    registry.add_repo(_repo_with_schema(tmp_path, "plain"))
    registry.add_repo(_repo_with_schema(tmp_path, "broken", INVALID_SCHEMA))
    registry.close()

    from devgraph.cli import main as cli_main

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["doctor"])

    collapsed = _collapsed(result.stdout)
    assert "Project schemas" in collapsed
    assert "built-in schema" in collapsed  # the repository with no file
    assert "invalid project schema" in collapsed
    # An invalid configuration is a failing check, whatever else this
    # environment reports (Podman, Neo4j and the tray are all independent).
    assert "doctor found one or more failing checks above." in collapsed
