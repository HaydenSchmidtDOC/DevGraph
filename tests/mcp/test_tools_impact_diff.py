"""Integration tests for impact_analysis_for_diff (Implementation Plan #3, Item 3)."""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from devgraph.graph.engine import GraphEngine
from devgraph.indexer.python.extractor import index_file
from devgraph.mcp.tools import impact_analysis_for_diff
from devgraph.registry.store import RepoRegistry


def _run_git(repo_path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo_path), capture_output=True, check=True)


def _git_version() -> tuple[int, ...]:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True).stdout
    return tuple(int(n) for n in re.findall(r"\d+", out)[:2])


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


def test_impact_analysis_for_diff_invalid_ref_is_a_tool_error(graph_engine, registry, diff_repo):
    repo_path, base_sha, _head_sha = diff_repo
    repo_id = "_smoketest_impact_diff_bad_ref"
    registry.add_repo(repo_path, repo_id=repo_id)

    with pytest.raises(ToolError, match="head_ref 'not-a-real-ref-xyz' is not a branch, tag or commit"):
        impact_analysis_for_diff(graph_engine, registry, repo_id, base_sha, "not-a-real-ref-xyz")
    with pytest.raises(ToolError, match="base_ref '--output=x' is not a valid ref"):
        impact_analysis_for_diff(graph_engine, registry, repo_id, "--output=x", base_sha)


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


def test_impact_analysis_for_diff_unregistered_repo_is_a_tool_error(graph_engine, registry):
    with pytest.raises(ToolError, match="no such repo_id: '_no_such_repo'"):
        impact_analysis_for_diff(graph_engine, registry, "_no_such_repo", "HEAD", "HEAD")


@pytest.fixture
def symbol_repo(tmp_path):
    """Base: mod.py defines stable, edited and gone, each called from caller.py.
    Head: edits edited, deletes gone, adds brand_new; stable is untouched."""
    repo_path = tmp_path / "symbols"
    repo_path.mkdir()
    _run_git(repo_path, "init", "-q", "-b", "main")
    _run_git(repo_path, "config", "user.email", "test@example.com")
    _run_git(repo_path, "config", "user.name", "Test Author")
    (repo_path / "mod.py").write_text(
        "def stable():\n    return 1\n\n\ndef edited():\n    return 1\n\n\ndef gone():\n    return 1\n"
    )
    (repo_path / "caller.py").write_text(
        "from mod import stable, edited, gone\n\n\n"
        "def use_stable():\n    stable()\n\n\n"
        "def use_edited():\n    edited()\n\n\n"
        "def use_gone():\n    gone()\n"
    )
    _run_git(repo_path, "add", "-A")
    _run_git(repo_path, "commit", "-q", "-m", "base")
    return repo_path


def test_only_changed_and_removed_symbols_are_traced_and_added_ones_are_listed(graph_engine, registry, symbol_repo):
    repo_id = "_smoketest_impact_diff_symbols"
    registry.add_repo(symbol_repo, repo_id=repo_id)
    index_file(graph_engine, repo_id, symbol_repo / "mod.py", repo_root=symbol_repo)
    index_file(graph_engine, repo_id, symbol_repo / "caller.py", repo_root=symbol_repo)
    (symbol_repo / "mod.py").write_text(
        "def stable():\n    return 1\n\n\ndef edited():\n    return 2\n\n\ndef brand_new():\n    return 3\n"
    )
    _run_git(symbol_repo, "commit", "-q", "-am", "head")

    try:
        result = impact_analysis_for_diff(graph_engine, registry, repo_id, "HEAD~1", "HEAD")
    finally:
        graph_engine.delete_repository(repo_id)

    assert result["changed_files"] == ["mod.py"]
    assert [(s["name"], s["file"]) for s in result["changed_symbols"]] == [("edited", "mod.py")]
    assert [s["name"] for s in result["removed_symbols"]] == ["gone"]
    assert [s["name"] for s in result["added_symbols"]] == ["brand_new"]
    assert sorted(result["changed_components"]) == ["edited", "gone"]
    direct = {d["name"] for d in result["direct_dependents"]["results"]}
    assert direct == {"use_edited", "use_gone"}


class _NoGraph:
    def run_cypher(self, query, params=None):
        return []

    def run_read_cypher(self, query, params, *, timeout_s, max_rows):
        return self.run_cypher(query, params), False


@pytest.mark.skipif(sys.platform == "win32", reason="the fake ssh command is a POSIX shell script")
@pytest.mark.skipif(_git_version() < (2, 44), reason="GIT_NO_LAZY_FETCH needs git 2.44 or later")
def test_a_rename_with_an_edit_in_a_blobless_clone_lists_both_paths_without_fetching(registry, tmp_path):
    """Rename detection would read the old blob, which a blobless clone lacks."""
    src = tmp_path / "src"
    src.mkdir()
    _run_git(src, "init", "-q", "-b", "main")
    _run_git(src, "config", "user.email", "test@example.com")
    _run_git(src, "config", "user.name", "Test Author")
    _run_git(src, "config", "uploadpack.allowFilter", "true")
    (src / "old.py").write_text("".join(f"line_{i} = {i}\n" for i in range(40)))
    _run_git(src, "add", "-A")
    _run_git(src, "commit", "-q", "-m", "base")
    _run_git(src, "mv", "old.py", "new.py")
    (src / "new.py").write_text("".join(f"line_{i} = {i}\n" for i in range(39)) + "tail = 1\n")
    _run_git(src, "add", "-A")
    _run_git(src, "commit", "-q", "-m", "rename and edit")

    dst = tmp_path / "partial"
    _run_git(tmp_path, "clone", "-q", "--filter=blob:none", f"file://{src}", str(dst))
    marker = tmp_path / "ssh-was-called"
    script = tmp_path / "fake-ssh"
    script.write_text(f"#!/bin/sh\necho called >> '{marker}'\nexit 1\n")
    script.chmod(0o755)
    _run_git(dst, "remote", "set-url", "origin", "ssh://git.example.invalid/repo.git")
    _run_git(dst, "config", "core.sshCommand", str(script))
    record = registry.add_repo(dst, repo_id="_smoketest_impact_diff_partial")

    result = impact_analysis_for_diff(_NoGraph(), registry, record.repo_id, "HEAD~1", "HEAD")
    assert "error" not in result, result
    assert sorted(result["changed_files"]) == ["new.py", "old.py"]
    assert not marker.exists()


def _indexed(graph_engine, registry, repo_path, repo_id, *files):
    registry.add_repo(repo_path, repo_id=repo_id)
    for name in files:
        index_file(graph_engine, repo_id, repo_path / name, repo_root=repo_path)


def _direct(result):
    return {d["name"] for d in result["direct_dependents"]["results"]}


@pytest.fixture
def two_files(tmp_path):
    """Base: a.py defines fa, b.py defines fb, caller.py calls each. Head edits both."""
    root = tmp_path / "two"
    root.mkdir()
    _run_git(root, "init", "-q", "-b", "main")
    _run_git(root, "config", "user.email", "test@example.com")
    _run_git(root, "config", "user.name", "Test Author")
    (root / "a.py").write_text("def fa():\n    return 1\n")
    (root / "b.py").write_text("def fb():\n    return 1\n")
    (root / "caller.py").write_text(
        "from a import fa\nfrom b import fb\n\n\ndef use_fa():\n    fa()\n\n\ndef use_fb():\n    fb()\n"
    )
    _run_git(root, "add", "-A")
    _run_git(root, "commit", "-q", "-m", "base")
    return root


def _edit_both(root):
    (root / "a.py").write_text("def fa():\n    return 2\n")
    (root / "b.py").write_text("def fb():\n    return 2\n")
    _run_git(root, "commit", "-q", "-am", "head")


def test_files_past_the_detail_cap_count_every_indexed_symbol(graph_engine, registry, two_files, monkeypatch):
    from devgraph.indexer.git_history import compare as git_compare

    repo_id = "_smoketest_impact_diff_cap"
    _indexed(graph_engine, registry, two_files, repo_id, "a.py", "b.py", "caller.py")
    _edit_both(two_files)
    monkeypatch.setattr(git_compare, "_COMPARE_MAX_FILES", 1)
    try:
        result = impact_analysis_for_diff(graph_engine, registry, repo_id, "HEAD~1", "HEAD")
    finally:
        graph_engine.delete_repository(repo_id)
    assert [s["name"] for s in result["changed_symbols"]] == ["fa"]
    assert _direct(result) == {"use_fa", "use_fb"}
    assert "1 changed file(s) past the 1-file detail cap; every indexed symbol in them counts as changed" in (
        result["notices"]
    )
    assert result["truncated_reasons"] == ["files"]


def test_unreadable_symbols_count_every_indexed_symbol(graph_engine, registry, two_files, monkeypatch):
    from devgraph.indexer.git_history import compare as git_compare

    repo_id = "_smoketest_impact_diff_unread"
    _indexed(graph_engine, registry, two_files, repo_id, "a.py", "b.py", "caller.py")
    _edit_both(two_files)
    monkeypatch.setattr(git_compare, "_COMPARE_MAX_FILE_BYTES", 5)
    try:
        result = impact_analysis_for_diff(graph_engine, registry, repo_id, "HEAD~1", "HEAD")
    finally:
        graph_engine.delete_repository(repo_id)
    assert result["changed_symbols"] == []
    assert _direct(result) == {"use_fa", "use_fb"}
    assert (
        "symbols of 2 changed file(s) could not be read; every indexed symbol in them counts as changed"
        in result["notices"]
    )


def test_a_failed_symbol_detail_counts_every_indexed_symbol(graph_engine, registry, two_files, monkeypatch):
    from devgraph.indexer.git_history import compare as git_compare

    def fail(comparison):
        raise git_compare.CompareError("git object missing")

    repo_id = "_smoketest_impact_diff_detail_fails"
    _indexed(graph_engine, registry, two_files, repo_id, "a.py", "b.py", "caller.py")
    _edit_both(two_files)
    monkeypatch.setattr(git_compare, "symbol_detail", fail)
    try:
        result = impact_analysis_for_diff(graph_engine, registry, repo_id, "HEAD~1", "HEAD")
    finally:
        graph_engine.delete_repository(repo_id)
    assert _direct(result) == {"use_fa", "use_fb"}
    assert result["notices"] == ["git object missing; every indexed symbol in the changed files counts as changed"]


def test_a_pure_rename_traces_the_symbols_under_both_paths(graph_engine, registry, two_files):
    repo_id = "_smoketest_impact_diff_rename"
    _indexed(graph_engine, registry, two_files, repo_id, "a.py", "b.py", "caller.py")
    _run_git(two_files, "mv", "a.py", "a2.py")
    _run_git(two_files, "commit", "-q", "-m", "rename")
    try:
        result = impact_analysis_for_diff(graph_engine, registry, repo_id, "HEAD~1", "HEAD")
    finally:
        graph_engine.delete_repository(repo_id)
    assert result["changed_files"] == ["a.py", "a2.py"]
    assert result["changed_components"] == ["fa"]
    assert _direct(result) == {"use_fa"}
    assert any(n.startswith("1 renamed file(s)") for n in result["notices"])


def test_symbols_the_index_lacks_are_named_in_a_notice(graph_engine, registry, two_files):
    repo_id = "_smoketest_impact_diff_unindexed"
    registry.add_repo(two_files, repo_id=repo_id)
    _edit_both(two_files)
    try:
        result = impact_analysis_for_diff(graph_engine, registry, repo_id, "HEAD~1", "HEAD")
    finally:
        graph_engine.delete_repository(repo_id)
    assert result["direct_dependents"]["count"] == 0
    assert (
        "2 changed or removed symbol(s) match no indexed node, so their dependents are unknown "
        "(the index may already be at the head, or not yet cover them): fa (a.py), fb (b.py)"
    ) in result["notices"]


def test_a_merged_base_change_is_not_part_of_the_diff(graph_engine, registry, two_files):
    """main moves on after the branch point and is merged into the branch: only the
    branch's own change counts, from the merge base, as a pull request shows it."""
    repo_id = "_smoketest_impact_diff_merge"
    _indexed(graph_engine, registry, two_files, repo_id, "a.py", "b.py", "caller.py")
    _run_git(two_files, "checkout", "-q", "-b", "feature")
    (two_files / "a.py").write_text("def fa():\n    return 2\n")
    _run_git(two_files, "commit", "-q", "-am", "feature edit")
    _run_git(two_files, "checkout", "-q", "main")
    (two_files / "b.py").write_text("def fb():\n    return 3\n")
    _run_git(two_files, "commit", "-q", "-am", "main edit")
    _run_git(two_files, "checkout", "-q", "feature")
    _run_git(two_files, "merge", "-q", "--no-edit", "main")
    try:
        result = impact_analysis_for_diff(graph_engine, registry, repo_id, "main", "feature")
    finally:
        graph_engine.delete_repository(repo_id)
    assert result["changed_files"] == ["a.py"]
    assert [s["name"] for s in result["changed_symbols"]] == ["fa"]
    assert _direct(result) == {"use_fa"}
