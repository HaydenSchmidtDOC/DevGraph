"""Integration tests for impact_analysis_for_diff (Implementation Plan #3, Item 3)."""

import subprocess
import tempfile
from pathlib import Path

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.indexer.python.extractor import index_file
from devgraph.mcp.tools import impact_analysis_for_diff
from devgraph.registry.store import RepoRegistry


def _run_git(repo_path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo_path), capture_output=True, check=True)


@pytest.fixture
def graph_engine():
    engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    engine.init_schema()
    yield engine
    engine.close()


@pytest.fixture
def registry():
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = RepoRegistry(Path(tmpdir) / "registry.db")
        yield reg
        reg.close()


@pytest.fixture
def diff_repo():
    """A real git repo with two commits: base has helper()+caller(), head
    modifies helper() (touching helper.py) so the diff has exactly one
    changed file with a known dependent (caller, via a CALLS edge)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        _run_git(repo_path, "init")
        _run_git(repo_path, "config", "user.email", "test@example.com")
        _run_git(repo_path, "config", "user.name", "Test Author")

        (repo_path / "helper.py").write_text("def helper():\n    return 1\n")
        (repo_path / "caller.py").write_text("from helper import helper\n\ndef caller():\n    helper()\n")
        _run_git(repo_path, "add", "helper.py", "caller.py")
        _run_git(repo_path, "commit", "-m", "base commit")
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo_path), capture_output=True, text=True, check=True
        ).stdout.strip()

        (repo_path / "helper.py").write_text("def helper():\n    return 2\n")
        _run_git(repo_path, "add", "helper.py")
        _run_git(repo_path, "commit", "-m", "head commit")
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo_path), capture_output=True, text=True, check=True
        ).stdout.strip()

        yield repo_path, base_sha, head_sha


def test_impact_analysis_for_diff_end_to_end(graph_engine, registry, diff_repo):
    repo_path, base_sha, head_sha = diff_repo
    repo_id = "_smoketest_impact_diff"
    registry.add_repo(repo_path, repo_id=repo_id)

    index_file(graph_engine, repo_id, repo_path / "helper.py", repo_root=repo_path)
    index_file(graph_engine, repo_id, repo_path / "caller.py", repo_root=repo_path)

    try:
        result = impact_analysis_for_diff(graph_engine, registry, repo_id, base_sha, head_sha)

        assert "error" not in result
        assert result["changed_files"] == ["helper.py"]
        assert "helper" in result["changed_components"]

        direct_names = {d["name"] for d in result["direct_dependents"]["results"]}
        assert "caller" in direct_names
        assert result["risk_level"] in ("low", "medium", "high")
    finally:
        graph_engine.delete_repository(repo_id)


def test_impact_analysis_for_diff_invalid_ref_returns_error(graph_engine, registry, diff_repo):
    repo_path, base_sha, _head_sha = diff_repo
    repo_id = "_smoketest_impact_diff_bad_ref"
    registry.add_repo(repo_path, repo_id=repo_id)

    try:
        result = impact_analysis_for_diff(graph_engine, registry, repo_id, base_sha, "not-a-real-ref-xyz")
        assert "error" in result
        assert result["changed_files"] == []
        assert result["risk_level"] == "low"
    finally:
        graph_engine.delete_repository(repo_id)


def test_impact_analysis_for_diff_empty_diff_returns_empty(graph_engine, registry, diff_repo):
    repo_path, base_sha, _head_sha = diff_repo
    repo_id = "_smoketest_impact_diff_empty"
    registry.add_repo(repo_path, repo_id=repo_id)

    try:
        result = impact_analysis_for_diff(graph_engine, registry, repo_id, base_sha, base_sha)
        assert "error" not in result
        assert result["changed_files"] == []
        assert result["changed_components"] == []
        assert result["risk_level"] == "low"
    finally:
        graph_engine.delete_repository(repo_id)


def test_impact_analysis_for_diff_unregistered_repo_returns_error(graph_engine, registry):
    result = impact_analysis_for_diff(graph_engine, registry, "_no_such_repo", "HEAD", "HEAD")
    assert "error" in result
    assert result["changed_files"] == []
