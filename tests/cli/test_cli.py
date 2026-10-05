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


@pytest.fixture
def purge_registered_repos(temp_registry_db, require_neo4j):
    """Delete from live Neo4j every repo this test registered, even if it failed.

    Yields a list; a test that removes its registry row itself should append
    the repo_id so teardown still purges the graph data.
    """
    extra_ids = []
    yield extra_ids
    from devgraph.graph.engine import GraphEngine

    db_path, _ = temp_registry_db
    reg = RepoRegistry(db_path)
    ids = {r.repo_id for r in reg.list_repos()} | set(extra_ids)
    reg.close()
    engine = GraphEngine("bolt://127.0.0.1:7687", "neo4j", "devgraph-local-dev")
    try:
        for repo_id in ids:
            engine.delete_repository(repo_id)
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


def test_cli_add_repo(runner, temp_git_repo, temp_registry_db, purge_registered_repos):
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


def test_cli_remove_repo_purges_graph_data(runner, temp_git_repo, temp_registry_db, purge_registered_repos):
    """'devgraph remove' must delete the repo's Neo4j nodes, not just its registry row."""
    from devgraph.graph.engine import GraphEngine

    db_path, registry = temp_registry_db

    repo_record = registry.add_repo(temp_git_repo)
    repo_id = repo_record.repo_id
    purge_registered_repos.append(repo_id)  # `remove` deletes the registry row
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


def test_cli_rescan_repo(runner, temp_git_repo, temp_registry_db, purge_registered_repos):
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


def test_cli_annotate_note_refuses_sibling_prefix_path(runner, temp_registry_db, tmp_path):
    """A note in a sibling directory that shares the repo's name prefix is
    outside the repository and must be refused before anything is indexed."""
    db_path, registry = temp_registry_db
    repo_path = tmp_path / "proj"
    repo_path.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_path), capture_output=True, check=True)
    sibling = tmp_path / "proj-private"
    sibling.mkdir()
    (sibling / "note.md").write_text("---\ntype: requirement\nid: req-x\n---\n# Note\n")

    repo_id = registry.add_repo(repo_path).repo_id
    registry.close()

    from devgraph.cli import main as cli_main

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "index_doc_file") as index_doc_file:
        result = runner.invoke(app, ["annotate", repo_id, "--note", "../proj-private/note.md"])

    assert result.exit_code == 1
    assert "note path must be inside the repository" in result.stdout
    index_doc_file.assert_not_called()


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


def test_cli_add_full_flag_also_indexes_history(runner, temp_registry_db, purge_registered_repos):
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
        assert "-m devgraph.mcp.server" not in result.stdout.replace("-P -m devgraph.mcp.server", "")
        assert '"-P",' in result.stdout


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
        assert collapsed.endswith('" -P -m devgraph.mcp.server')


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
        assert data["servers"]["devgraph"]["args"] == ["-P", "-m", "devgraph.mcp.server"]
        assert data["servers"]["devgraph"]["type"] == "stdio"
        assert Path(data["servers"]["devgraph"]["cwd"]).is_absolute()


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


def test_rescan_accepts_now(runner):
    result = runner.invoke(app, ["rescan", "--help"])
    assert result.exit_code == 0 and "--now" in result.output


def _doctor_with_engine(runner, db_path, engine_cls):
    from devgraph.cli import main as cli_main

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "GraphEngine", engine_cls), \
         patch.object(cli_main, "resolve_podman", return_value=None):
        return runner.invoke(app, ["doctor"])


def _stub_engine(applied):
    class StubEngine:
        def __init__(self, *args, **kwargs):
            pass

        def verify_connectivity(self):
            pass

        def init_schema(self):
            pass

        def read_applied_schema(self, repo_id):
            return applied

        def close(self):
            pass

    return StubEngine


def test_cli_doctor_marks_a_disabled_repo_and_reports_drift(runner, temp_registry_db, tmp_path, monkeypatch):
    from devgraph.config import project_switch

    db_path, registry = temp_registry_db
    monkeypatch.setattr(project_switch, "_registry_db_path", lambda: db_path)
    repo_id = registry.add_repo(_repo_with_schema(tmp_path, "widgets", WIDGET_SCHEMA)).repo_id
    registry.set_project_config_enabled(repo_id, False)
    registry.close()

    result = _doctor_with_engine(runner, db_path, _stub_engine({"hash": "sha256:old"}))
    collapsed = _collapsed(result.stdout)
    assert "project config disabled" in collapsed
    # Disabled means the file hashes as absent, so a graph built with the file is pending.
    assert "pending" in collapsed and f"devgraph rescan {repo_id} --now" in collapsed


def test_cli_doctor_drift_states(runner, temp_registry_db, tmp_path, monkeypatch):
    from devgraph.config import project_switch
    from devgraph.config.project_schema import schema_file_hash

    db_path, registry = temp_registry_db
    monkeypatch.setattr(project_switch, "_registry_db_path", lambda: db_path)
    root = _repo_with_schema(tmp_path, "widgets", WIDGET_SCHEMA)
    repo_id = registry.add_repo(root).repo_id
    registry.close()

    pending = _collapsed(_doctor_with_engine(runner, db_path, _stub_engine({"hash": "sha256:old"})).stdout)
    assert "pending" in pending and f"devgraph rescan {repo_id} --now" in pending

    applied = _collapsed(
        _doctor_with_engine(runner, db_path, _stub_engine({"hash": schema_file_hash(root)})).stdout
    )
    assert "applied" in applied and "pending" not in applied

    never = _collapsed(_doctor_with_engine(runner, db_path, _stub_engine(None)).stdout)
    assert "never applied" in never


def test_cli_doctor_skips_drift_when_neo4j_is_unreachable(runner, temp_registry_db, tmp_path):
    db_path, registry = temp_registry_db
    registry.add_repo(_repo_with_schema(tmp_path, "widgets", WIDGET_SCHEMA))
    registry.close()

    stub = _stub_engine({"hash": "sha256:old"})

    class Down(stub):
        def verify_connectivity(self):
            raise RuntimeError("connection refused")

    collapsed = _collapsed(_doctor_with_engine(runner, db_path, Down).stdout)
    assert "Schema drift" in collapsed and "skipped" in collapsed
    assert "pending" not in collapsed


def test_cli_list_shows_the_project_config_switch(runner, temp_registry_db, tmp_path):
    db_path, registry = temp_registry_db
    on = registry.add_repo(_repo_with_schema(tmp_path, "on")).repo_id
    off = registry.add_repo(_repo_with_schema(tmp_path, "off")).repo_id
    registry.set_project_config_enabled(off, False)
    registry.close()

    from devgraph.cli import main as cli_main

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["list"])
    assert "Project config" in result.stdout
    rows = {rid: line for rid in (on, off) for line in result.stdout.splitlines() if f"│ {rid} " in line}
    # Cells are `| id | path | active | watch | project config | last indexed |`.
    assert [rows[rid].split("│")[-3].strip() for rid in (on, off)] == ["on", "off"]


def _doctor_repo(runner, temp_registry_db, tmp_path, monkeypatch, *, disabled, schema=WIDGET_SCHEMA, tools=None):
    from devgraph.config import project_switch
    from devgraph.config.project_tools import TOOLS_FILENAME

    db_path, registry = temp_registry_db
    monkeypatch.setattr(project_switch, "_registry_db_path", lambda: db_path)
    root = _repo_with_schema(tmp_path, "widgets", schema)
    if tools is not None:
        (root / TOOLS_FILENAME).write_text(tools, encoding="utf-8")
    repo_id = registry.add_repo(root).repo_id
    if disabled:
        registry.set_project_config_enabled(repo_id, False)
    registry.close()
    return db_path, root, repo_id


LIST_FILES_TOOLS = """\
version: 1
tools:
  - name: list_files
    description: List files.
    cypher: |
      MATCH (f:File {repo_id: $repo_id}) RETURN f.path AS path
"""


def test_cli_doctor_reports_disabled_tools_as_not_served(runner, temp_registry_db, tmp_path, monkeypatch):
    db_path, _root, repo_id = _doctor_repo(
        runner, temp_registry_db, tmp_path, monkeypatch, disabled=True, tools=LIST_FILES_TOOLS
    )
    result = _doctor_with_engine(runner, db_path, _stub_engine({"hash": "absent"}))
    collapsed = _collapsed(result.stdout)
    assert f"[!] {repo_id}: tools: list_files (not served: project config disabled)" in collapsed
    assert f"devgraph config enable {repo_id}" in collapsed and "<repo_id>" not in collapsed


def test_cli_doctor_drift_wording_for_a_disabled_repo(runner, temp_registry_db, tmp_path, monkeypatch):
    db_path, _root, repo_id = _doctor_repo(runner, temp_registry_db, tmp_path, monkeypatch, disabled=True)
    in_sync = _collapsed(_doctor_with_engine(runner, db_path, _stub_engine({"hash": "absent"})).stdout)
    assert "project config disabled; built-in schema applied)" in in_sync
    assert "the schema changed" not in in_sync

    pending = _collapsed(_doctor_with_engine(runner, db_path, _stub_engine({"hash": "sha256:old"})).stdout)
    assert (
        "project config disabled; built-in schema applied at the next rescan "
        f"(devgraph rescan {repo_id} --now)"
    ) in pending
    assert "the schema changed" not in pending


def test_cli_doctor_reports_an_unreadable_schema_file_not_pending(runner, temp_registry_db, tmp_path, monkeypatch):
    from devgraph.config.project_schema import SCHEMA_FILENAME

    db_path, root, _repo_id = _doctor_repo(
        runner, temp_registry_db, tmp_path, monkeypatch, disabled=False, schema=None
    )
    (root / SCHEMA_FILENAME).mkdir()  # reading a directory fails with an OSError
    collapsed = _collapsed(_doctor_with_engine(runner, db_path, _stub_engine({"hash": "sha256:old"})).stdout)
    assert "schema file unreadable" in collapsed
    assert "the schema changed" not in collapsed and "pending" not in collapsed


def _constraint_engine_or_skip():
    from devgraph.graph.engine import GraphEngine

    engine = GraphEngine("bolt://127.0.0.1:7687", "neo4j", "devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        engine.close()
        pytest.skip(f"Neo4j not available: {e}")
    return engine


def _has_constraint(engine, name):
    return bool(engine.run_cypher("SHOW CONSTRAINTS YIELD name WHERE name = $n RETURN name", {"n": name}))


@pytest.fixture
def stale_label():
    """A random label with a DevGraph-named constraint no repository declares."""
    import uuid

    engine = _constraint_engine_or_skip()
    label = f"Zz{uuid.uuid4().hex[:10]}"
    name = f"{label.lower()}_repo_key"
    engine.run_cypher(f"CREATE CONSTRAINT {name} FOR (n:{label}) REQUIRE (n.repo_id, n.slug) IS UNIQUE")
    yield engine, label, name
    engine.run_cypher(f"DROP CONSTRAINT {name} IF EXISTS")
    engine.close()


def _invoke_live(runner, db_path, args):
    from devgraph.cli import main as cli_main

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "resolve_podman", return_value=None):
        return runner.invoke(app, args)


def test_cli_doctor_reports_a_stale_constraint_and_prune_constraints_drops_it(runner, temp_registry_db, stale_label):
    engine, label, name = stale_label
    db_path, registry = temp_registry_db
    registry.close()

    doctor = _collapsed(_invoke_live(runner, db_path, ["doctor"]).stdout)
    assert "Schema constraints" in doctor
    assert name in doctor and "devgraph config schema prune-constraints" in doctor

    dry = _invoke_live(runner, db_path, ["config", "schema", "prune-constraints", "--label", label, "--dry-run"])
    assert dry.exit_code == 0 and name in dry.stdout
    assert _has_constraint(engine, name)

    result = _invoke_live(runner, db_path, ["config", "schema", "prune-constraints", "--label", label])
    assert result.exit_code == 0 and name in result.stdout
    assert not _has_constraint(engine, name)


def test_cli_prune_constraints_keeps_a_label_a_registered_repo_declares(runner, temp_registry_db, tmp_path, stale_label):
    engine, label, name = stale_label
    db_path, registry = temp_registry_db
    root = _repo_with_schema(
        tmp_path, "declares",
        f"version: 1\nnode_types:\n  - label: {label}\n    key: [slug]\n    metadata: [{{name: slug}}]\n",
    )
    registry.add_repo(root)
    registry.close()

    result = _invoke_live(runner, db_path, ["config", "schema", "prune-constraints", "--label", label])
    assert result.exit_code == 0
    assert _has_constraint(engine, name)


def test_cli_remove_releases_the_constraints_of_the_repos_labels(runner, temp_registry_db, tmp_path, stale_label):
    engine, label, name = stale_label
    db_path, registry = temp_registry_db
    root = tmp_path / "leaving"
    (root / ".git").mkdir(parents=True)
    repo_id = registry.add_repo(root, repo_id=f"_smoketest_remove_{label.lower()}").repo_id
    registry.close()
    engine.upsert_repository(repo_id, repo_id, str(root))
    engine.record_applied_schema(repo_id, "sha256:x", [label], [], [f"{label}:slug"])
    try:
        result = _invoke_live(runner, db_path, ["remove", repo_id])
    finally:
        engine.delete_repository(repo_id)
    assert result.exit_code == 0, result.stdout
    assert not _has_constraint(engine, name)


def test_cli_doctor_reports_a_missing_or_blocked_generated_constraint(runner, temp_registry_db, stale_label):
    engine, label, name = stale_label  # constraint on (repo_id, slug)
    db_path, registry = temp_registry_db
    registry.close()
    blocked, missing = f"_smoketest_blocked_{label.lower()}", f"_smoketest_missing_{label.lower()}"
    other = f"{label}b"
    try:
        engine.upsert_repository(blocked, blocked, "/tmp/blocked")
        engine.record_applied_schema(blocked, "sha256:x", [label], [], [f"{label}:code"])
        engine.run_cypher(
            f"CREATE (:{label} {{repo_id: $r, slug: 'a', code: 'same'}}), (:{label} {{repo_id: $r, slug: 'b', code: 'same'}})",
            {"r": blocked},
        )
        engine.upsert_repository(missing, missing, "/tmp/missing")
        engine.record_applied_schema(missing, "sha256:x", [other], [], [f"{other}:slug"])
        doctor = _collapsed(_invoke_live(runner, db_path, ["doctor"]).stdout)
    finally:
        engine.delete_repository(blocked)
        engine.delete_repository(missing)
    assert "key change blocked by duplicate nodes" in doctor and label in doctor
    assert f"devgraph rescan {missing} --now" in doctor


def test_cli_dashboard_url_points_a_wildcard_bind_at_loopback(runner, temp_registry_db):
    """A wildcard bind address is refused by the dashboard's Host guard, so
    the printed URL must be the loopback address the server listens on."""
    db_path, _ = temp_registry_db
    from devgraph.cli import main as cli_main
    from devgraph.config.settings import Settings

    settings = Settings(registry_db_path=db_path, dashboard_host="0.0.0.0", dashboard_port=8765)
    with patch.object(cli_main, "get_settings", return_value=settings), \
         patch.object(cli_main, "_tray_liveness_text", return_value="running"):
        result = runner.invoke(app, ["dashboard", "--url-only"])
    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip() == "http://127.0.0.1:8765"


def test_cli_insights_unknown_repo(runner, temp_registry_db):
    db_path, _ = temp_registry_db
    from devgraph.cli import main as cli_main

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["insights", "no-such-repo"])
    assert result.exit_code == 1
    assert "no such repo_id" in result.stdout


def test_cli_insights_computes_for_a_registered_repo(runner, temp_git_repo, temp_registry_db, require_neo4j):
    db_path, registry = temp_registry_db
    repo_id = registry.add_repo(temp_git_repo).repo_id
    from devgraph.cli import main as cli_main
    from devgraph.graph.engine import GraphEngine

    engine = GraphEngine("bolt://127.0.0.1:7687", "neo4j", "devgraph-local-dev")
    try:
        engine.upsert_repository(repo_id, repo_id, str(temp_git_repo))
        engine.run_cypher(
            "CREATE (:Function {repo_id: $r, name: 'a', file: 'x/a.py'})-[:CALLS]->"
            "(:Function {repo_id: $r, name: 'b', file: 'x/b.py'})",
            {"r": repo_id},
        )
        config_module.get_settings.cache_clear()
        with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
             patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)):
            result = runner.invoke(app, ["insights", repo_id])
        assert result.exit_code == 0, result.stdout
        output = " ".join(result.stdout.split())  # Rich may wrap the line at the runner's width
        assert "1 communities" in output and "2 nodes" in output
    finally:
        engine.delete_repository(repo_id)
        engine.close()


def test_claude_mcp_add_passes_safe_path_flag(tmp_path):
    from devgraph.cli import main as cli_main

    def fake_run(cmd, *args, **kwargs):
        result = MagicMock()
        result.returncode = 1 if cmd[1:3] == ["mcp", "get"] else 0
        return result

    with patch("devgraph.cli.main.subprocess.run", side_effect=fake_run) as mock_run:
        assert cli_main._run_claude_mcp_add("/usr/bin/claude", Path("/venv/bin/python"), tmp_path)

    add_cmd = mock_run.call_args_list[-1].args[0]
    assert add_cmd[add_cmd.index("--") + 1:] == ["/venv/bin/python", "-P", "-m", "devgraph.mcp.server"]


def _write_shadow_package(workdir: Path, marker: str) -> None:
    pkg = workdir / "devgraph" / "mcp"
    pkg.mkdir(parents=True)
    (workdir / "devgraph" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "server.py").write_text(f"print({marker!r})\n")


def test_generated_mcp_command_ignores_devgraph_package_in_working_directory(tmp_path):
    """The generated server command must import the installed DevGraph even
    when started from a directory that contains its own `devgraph/` package."""
    import os
    import sys

    from devgraph.cli.main import MCP_SERVER_ARGS

    marker = "SHADOW_PACKAGE_LOADED"
    workdir = tmp_path / "client-repo"
    workdir.mkdir()
    _write_shadow_package(workdir, marker)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("DEVGRAPH_") and key != "PYTHONPATH"
    }
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "DEVGRAPH_REGISTRY_DB_PATH": str(home / ".devgraph" / "registry.sqlite3"),
        # Unreachable, so the real server exits at its connectivity check
        # before starting anything else.
        "DEVGRAPH_NEO4J_URI": "bolt://127.0.0.1:1",
    })

    def run(args):
        return subprocess.run(
            [sys.executable, *args], cwd=str(workdir), env=env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120,
        )

    # Control: without -P the working directory's package wins.
    unsafe = run(["-m", "devgraph.mcp.server"])
    assert marker in unsafe.stdout

    safe = run(list(MCP_SERVER_ARGS))
    assert marker not in safe.stdout
    assert safe.returncode != 0
    assert "ServiceUnavailable" in safe.stderr  # reached the real server
    assert str(workdir) not in safe.stderr
