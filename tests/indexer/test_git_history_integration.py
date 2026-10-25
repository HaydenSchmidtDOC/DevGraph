"""Integration tests for index_repo_history with live Neo4j and a real RepoRegistry."""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.indexer.git_history.extractor import index_repo_history, sync_git_history
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
def temp_git_repo():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        _run_git(repo_path, "init")
        _run_git(repo_path, "config", "user.email", "test@example.com")
        _run_git(repo_path, "config", "user.name", "Test Author")
        (repo_path / "service.py").write_text("x = 1\n")
        _run_git(repo_path, "add", "service.py")
        _run_git(repo_path, "commit", "-m", "Initial commit")
        yield repo_path


@pytest.fixture
def registry():
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = RepoRegistry(Path(tmpdir) / "registry.db")
        yield reg
        reg.close()


def test_index_repo_history_end_to_end(graph_engine, temp_git_repo, registry):
    repo_id = "_smoketest_git_history"
    record = registry.add_repo(temp_git_repo, repo_id=repo_id)
    graph_engine.upsert_node("Module", record.repo_id, "service.py", {})

    try:
        count = index_repo_history(graph_engine, registry, record.repo_id)
        assert count == 1

        result = graph_engine.run_cypher(
            "MATCH (c:Commit {repo_id: $repo_id}) RETURN c.message as message",
            {"repo_id": record.repo_id},
        )
        assert len(result) == 1
        assert result[0]["message"] == "Initial commit"

        result = graph_engine.run_cypher(
            "MATCH (c:Commit {repo_id: $repo_id})-[:MODIFIES]->(m:Module {name: 'service.py'}) "
            "RETURN COUNT(*) as count",
            {"repo_id": record.repo_id},
        )
        assert result[0]["count"] == 1

        # last_indexed_commit was persisted
        updated = registry.get(record.repo_id)
        assert updated.last_indexed_commit is not None

        # Re-running is a no-op (incremental, nothing new)
        count_again = index_repo_history(graph_engine, registry, record.repo_id)
        assert count_again == 0
    finally:
        graph_engine.delete_repository(record.repo_id)


def test_index_repo_history_unknown_repo_raises(graph_engine, registry):
    with pytest.raises(ValueError):
        index_repo_history(graph_engine, registry, "nonexistent")


def test_modifies_edge_resolves_for_nested_file(graph_engine, registry):
    """MODIFIES targets must be the full repo-relative path (matching how
    Module nodes are keyed since the multi-level relative-import fix), not
    bare filename — otherwise a commit touching a nested file's MODIFIES
    edge silently never resolves (blame_component would come back empty for
    every file except ones at the repo root).
    """
    from devgraph.indexer.python.extractor import index_file

    repo_id = "_smoketest_git_history_nested"
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        _run_git(repo_path, "init")
        _run_git(repo_path, "config", "user.email", "test@example.com")
        _run_git(repo_path, "config", "user.name", "Test Author")

        (repo_path / "services" / "api").mkdir(parents=True)
        nested_file = repo_path / "services" / "api" / "main.py"
        nested_file.write_text("x = 1\n")
        _run_git(repo_path, "add", "services/api/main.py")
        _run_git(repo_path, "commit", "-m", "Add nested main.py")

        record = registry.add_repo(repo_path, repo_id=repo_id)

        try:
            index_file(graph_engine, record.repo_id, nested_file, repo_root=repo_path)
            index_repo_history(graph_engine, registry, record.repo_id)

            result = graph_engine.run_cypher(
                "MATCH (c:Commit {repo_id: $repo_id})-[:MODIFIES]->"
                "(m:Module {name: 'services/api/main.py'}) RETURN COUNT(*) as count",
                {"repo_id": record.repo_id},
            )
            assert result[0]["count"] == 1
        finally:
            graph_engine.delete_repository(record.repo_id)


def test_reindex_preserves_modifies_edges(graph_engine, registry):
    """Re-indexing a file must NOT destroy its git-history MODIFIES edges.

    Regression for the bug where replace_file_nodes DETACH-DELETEd every
    node with the file's provenance on each reindex, silently destroying
    the Commit -> Module MODIFIES edges created by a prior history sync.
    The Module node must be MERGEd in place (keeping its incoming edges),
    and only stale Class/Function nodes pruned.
    """
    from devgraph.indexer.python.extractor import index_file

    repo_id = "_smoketest_git_history_reindex_preserves"
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        _run_git(repo_path, "init")
        _run_git(repo_path, "config", "user.email", "test@example.com")
        _run_git(repo_path, "config", "user.name", "Test Author")

        py_file = repo_path / "service.py"
        py_file.write_text("class Service:\n    pass\n")
        _run_git(repo_path, "add", "service.py")
        _run_git(repo_path, "commit", "-m", "Add service")

        record = registry.add_repo(repo_path, repo_id=repo_id)

        try:
            # Index the file, then sync history so the MODIFIES edge exists.
            index_file(graph_engine, record.repo_id, py_file, repo_root=repo_path)
            index_repo_history(graph_engine, registry, record.repo_id)
            result = graph_engine.run_cypher(
                "MATCH (c:Commit {repo_id: $repo_id})-[:MODIFIES]->"
                "(m:Module {name: 'service.py'}) RETURN COUNT(*) as count",
                {"repo_id": record.repo_id},
            )
            assert result[0]["count"] == 1, "MODIFIES edge should exist after history sync"

            # Re-index the same file (as a watcher/rescan would). The
            # MODIFIES edge must survive.
            index_file(graph_engine, record.repo_id, py_file, repo_root=repo_path)
            result = graph_engine.run_cypher(
                "MATCH (c:Commit {repo_id: $repo_id})-[:MODIFIES]->"
                "(m:Module {name: 'service.py'}) RETURN COUNT(*) as count",
                {"repo_id": record.repo_id},
            )
            assert result[0]["count"] == 1, "MODIFIES edge must survive a reindex"
        finally:
            graph_engine.delete_repository(record.repo_id)


def test_sync_git_history_initial_walk_stages_module_recency(graph_engine, temp_git_repo, registry):
    repo_id = "_smoketest_sync_initial"
    record = registry.add_repo(temp_git_repo, repo_id=repo_id)
    graph_engine.upsert_node("Module", record.repo_id, "service.py", {})

    try:
        outcome = sync_git_history(graph_engine, registry, record.repo_id)
        assert outcome["mode"] == "initial"
        assert outcome["commits_indexed"] == 1

        result = graph_engine.run_cypher(
            "MATCH (m:Module {repo_id: $repo_id, name: 'service.py'}) "
            "RETURN m.created_at AS created_at, m.last_modified_at AS last_modified_at",
            {"repo_id": record.repo_id},
        )
        assert result[0]["created_at"] is not None
        assert result[0]["created_at"] == result[0]["last_modified_at"]

        # HEAD unchanged -> no-op
        outcome_again = sync_git_history(graph_engine, registry, record.repo_id)
        assert outcome_again["mode"] == "noop"
    finally:
        graph_engine.delete_repository(record.repo_id)


def test_sync_git_history_fast_path_advances_recency(graph_engine, temp_git_repo, registry):
    repo_id = "_smoketest_sync_fast"
    record = registry.add_repo(temp_git_repo, repo_id=repo_id)
    graph_engine.upsert_node("Module", record.repo_id, "service.py", {})

    try:
        sync_git_history(graph_engine, registry, record.repo_id)

        (temp_git_repo / "service.py").write_text("x = 2\n")
        _run_git(temp_git_repo, "add", "service.py")
        _run_git(temp_git_repo, "commit", "-m", "Update service")

        outcome = sync_git_history(graph_engine, registry, record.repo_id)
        assert outcome["mode"] == "fast"
        assert outcome["commits_indexed"] == 1

        result = graph_engine.run_cypher(
            "MATCH (c:Commit {repo_id: $repo_id}) RETURN COUNT(*) as count",
            {"repo_id": record.repo_id},
        )
        assert result[0]["count"] == 2
    finally:
        graph_engine.delete_repository(record.repo_id)


def test_sync_git_history_reconcile_deletes_orphans_and_resets_recency(
    graph_engine, temp_git_repo, registry
):
    repo_id = "_smoketest_sync_reconcile"
    record = registry.add_repo(temp_git_repo, repo_id=repo_id)
    graph_engine.upsert_node("Module", record.repo_id, "service.py", {})

    try:
        sync_git_history(graph_engine, registry, record.repo_id)

        first_head = registry.get(record.repo_id).last_indexed_commit

        # Simulate a rebase/reset: amend the last commit so the previously
        # indexed SHA is no longer reachable from HEAD.
        (temp_git_repo / "service.py").write_text("x = 3\n")
        _run_git(temp_git_repo, "add", "service.py")
        _run_git(temp_git_repo, "commit", "--amend", "-m", "Rewritten initial commit")

        outcome = sync_git_history(graph_engine, registry, record.repo_id)
        assert outcome["mode"] == "reconcile"
        assert outcome["commits_deleted"] == 1

        result = graph_engine.run_cypher(
            "MATCH (c:Commit {repo_id: $repo_id, name: $sha}) RETURN COUNT(*) as count",
            {"repo_id": record.repo_id, "sha": first_head},
        )
        assert result[0]["count"] == 0

        result = graph_engine.run_cypher(
            "MATCH (c:Commit {repo_id: $repo_id}) RETURN COUNT(*) as count",
            {"repo_id": record.repo_id},
        )
        assert result[0]["count"] == 1
    finally:
        graph_engine.delete_repository(record.repo_id)


def test_sync_git_history_force_resyncs_even_when_head_unchanged(graph_engine, temp_git_repo, registry):
    """force=True must do a full re-walk even when HEAD hasn't moved.

    Regression for the repair path: a normal sync is a noop when
    last_indexed_commit == HEAD, so MODIFIES edges destroyed by a bug (the
    old replace_file_nodes blanket-delete) would never be re-created.
    force=True routes to the reconcile path regardless, re-creating them.
    """
    from devgraph.indexer.python.extractor import index_file

    repo_id = "_smoketest_sync_force"
    record = registry.add_repo(temp_git_repo, repo_id=repo_id)
    py_file = temp_git_repo / "service.py"

    try:
        # Index the file, sync history (MODIFIES edge created).
        index_file(graph_engine, record.repo_id, py_file, repo_root=temp_git_repo)
        sync_git_history(graph_engine, registry, record.repo_id)
        result = graph_engine.run_cypher(
            "MATCH (c:Commit {repo_id: $repo_id})-[:MODIFIES]->(m:Module {name: 'service.py'}) RETURN COUNT(*) as count",
            {"repo_id": record.repo_id},
        )
        assert result[0]["count"] == 1

        # HEAD unchanged: a normal sync is a noop.
        outcome = sync_git_history(graph_engine, registry, record.repo_id)
        assert outcome["mode"] == "noop"

        # Simulate the bug: destroy the MODIFIES edge (as the old
        # replace_file_nodes blanket-delete did).
        graph_engine.run_cypher(
            "MATCH (c:Commit {repo_id: $repo_id})-[r:MODIFIES]->(m:Module {name: 'service.py'}) DELETE r",
            {"repo_id": record.repo_id},
        )
        result = graph_engine.run_cypher(
            "MATCH (c:Commit {repo_id: $repo_id})-[:MODIFIES]->(m:Module {name: 'service.py'}) RETURN COUNT(*) as count",
            {"repo_id": record.repo_id},
        )
        assert result[0]["count"] == 0

        # force=True must re-create it even though HEAD hasn't moved.
        outcome = sync_git_history(graph_engine, registry, record.repo_id, force=True)
        assert outcome["mode"] == "reconcile"
        result = graph_engine.run_cypher(
            "MATCH (c:Commit {repo_id: $repo_id})-[:MODIFIES]->(m:Module {name: 'service.py'}) RETURN COUNT(*) as count",
            {"repo_id": record.repo_id},
        )
        assert result[0]["count"] == 1, "force=True must re-create destroyed MODIFIES edges"
    finally:
        graph_engine.delete_repository(record.repo_id)


def test_sync_git_history_restores_history_after_the_graph_is_wiped(graph_engine, temp_git_repo, registry):
    """A wiped or replaced graph no longer holds the commit the registry
    recorded; the next sync must read history again, not report a noop."""
    repo_id = "_smoketest_sync_wiped"
    record = registry.add_repo(temp_git_repo, repo_id=repo_id)
    graph_engine.upsert_node("Module", record.repo_id, "service.py", {})

    try:
        sync_git_history(graph_engine, registry, record.repo_id)

        # The wipe: the graph loses everything, the registry keeps its HEAD.
        graph_engine.delete_repository(record.repo_id)
        graph_engine.upsert_node("Module", record.repo_id, "service.py", {})

        outcome = sync_git_history(graph_engine, registry, record.repo_id)
        assert outcome["mode"] == "initial"
        result = graph_engine.run_cypher(
            "MATCH (c:Commit {repo_id: $repo_id})-[:MODIFIES]->(m:Module {name: 'service.py'}) "
            "RETURN COUNT(*) as count",
            {"repo_id": record.repo_id},
        )
        assert result[0]["count"] == 1
    finally:
        graph_engine.delete_repository(record.repo_id)


def test_sync_git_history_unknown_repo_raises(graph_engine, registry):
    with pytest.raises(ValueError):
        sync_git_history(graph_engine, registry, "nonexistent")


def test_recency_never_creates_a_node_for_a_file_the_graph_does_not_hold(graph_engine, temp_git_repo, registry):
    """A commit touching a file with no Module (README.md, an image, a deleted
    file) leaves no Module behind: recency only annotates indexed nodes, so
    the graph still equals a fresh scan's apart from recency itself."""
    (temp_git_repo / "README.md").write_text("# Demo\n")
    _run_git(temp_git_repo, "add", "README.md")
    _run_git(temp_git_repo, "commit", "-m", "Add a readme")
    record = registry.add_repo(temp_git_repo, repo_id="_smoketest_sync_no_stray")
    graph_engine.upsert_node("Module", record.repo_id, "service.py", {})

    try:
        assert sync_git_history(graph_engine, registry, record.repo_id)["mode"] == "initial"
        (temp_git_repo / "gone.py").write_text("y = 2\n")
        _run_git(temp_git_repo, "add", "gone.py")
        _run_git(temp_git_repo, "commit", "-m", "Add a module that is never indexed")
        assert sync_git_history(graph_engine, registry, record.repo_id)["mode"] == "fast"
        assert sync_git_history(graph_engine, registry, record.repo_id, force=True)["mode"] == "reconcile"
        graph_engine.set_recency("Function", record.repo_id, "missing", "2026-01-01", "2026-01-02", file="x.py")

        modules = graph_engine.run_cypher(
            "MATCH (n {repo_id: $repo_id}) WHERE NOT n:Commit RETURN labels(n)[0] AS label, n.name AS name",
            {"repo_id": record.repo_id},
        )
        assert {(m["label"], m["name"]) for m in modules} == {("Module", "service.py")}
        staged = graph_engine.run_cypher(
            "MATCH (m:Module {repo_id: $repo_id, name: 'service.py'}) RETURN m.created_at AS c",
            {"repo_id": record.repo_id},
        )
        assert staged[0]["c"] is not None
    finally:
        graph_engine.delete_repository(record.repo_id)


def _git_version() -> tuple[int, ...]:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True).stdout
    return tuple(int(n) for n in re.findall(r"\d+", out)[:2])


@pytest.mark.skipif(sys.platform == "win32", reason="the fake ssh command is a POSIX shell script")
@pytest.mark.skipif(_git_version() < (2, 44), reason="GIT_NO_LAZY_FETCH needs git 2.44 or later")
@pytest.mark.parametrize("filter_spec", ["blob:none", "tree:0"])
def test_sync_git_history_never_lazy_fetches_in_a_partial_clone(graph_engine, registry, tmp_path, filter_spec, caplog):
    """Diffing old commits and blaming a file read trees and blobs a partial clone
    lacks; git must not fetch them (network, credential prompts, no deadline), and
    the sync still completes."""
    src = tmp_path / "src"
    src.mkdir()
    _run_git(src, "init", "-q", "-b", "main")
    _run_git(src, "config", "user.email", "test@example.com")
    _run_git(src, "config", "user.name", "Test Author")
    _run_git(src, "config", "uploadpack.allowFilter", "true")
    (src / "a.py").write_text("def a():\n    return 1\n")
    (src / "b.py").write_text("def b():\n    return 1\n")
    (src / "old.py").write_text("".join(f"line_{i} = {i}\n" for i in range(40)))
    _run_git(src, "add", "-A")
    _run_git(src, "commit", "-q", "-m", "first")
    (src / "a.py").write_text("def a():\n    return 2\n")
    _run_git(src, "mv", "old.py", "new.py")
    (src / "new.py").write_text("".join(f"line_{i} = {i}\n" for i in range(39)) + "tail = 1\n")
    _run_git(src, "add", "-A")
    _run_git(src, "commit", "-q", "-m", "second")
    (src / "a.py").write_text("def a():\n    return 3\n")
    (src / "b.py").write_text("def b():\n    return 3\n")
    _run_git(src, "commit", "-q", "-am", "third")

    dst = tmp_path / "partial"
    _run_git(tmp_path, "clone", "-q", f"--filter={filter_spec}", f"file://{src}", str(dst))
    marker = tmp_path / "ssh-was-called"
    script = tmp_path / "fake-ssh"
    script.write_text(f"#!/bin/sh\necho called >> '{marker}'\nexit 1\n")
    script.chmod(0o755)
    _run_git(dst, "remote", "set-url", "origin", "ssh://git.example.invalid/repo.git")
    _run_git(dst, "config", "core.sshCommand", str(script))

    record = registry.add_repo(dst, repo_id=f"_smoketest_sync_partial_{filter_spec.replace(':', '_')}")
    graph_engine.upsert_node("Module", record.repo_id, "a.py", {})
    graph_engine.upsert_node("Function", record.repo_id, "a", {"file": "a.py", "start_line": 1, "end_line": 2})
    graph_engine.upsert_node("Module", record.repo_id, "b.py", {})
    graph_engine.upsert_node("Function", record.repo_id, "b", {"file": "b.py", "start_line": 1, "end_line": 2})
    try:
        caplog.set_level("WARNING", logger="devgraph.indexer.git_history.extractor")
        outcome = sync_git_history(graph_engine, registry, record.repo_id)
        assert outcome["mode"] == "initial"
        assert outcome["commits_indexed"] == 3
        assert sync_git_history(graph_engine, registry, record.repo_id, force=True)["mode"] == "reconcile"
        assert not marker.exists()
        # Each sync logs its missing objects once, not once per file or commit.
        blame = [r.message for r in caplog.records if "git blame failed" in r.message]
        # (A treeless clone's initial sync records no MODIFIES, so blames nothing.)
        assert len(blame) == (2 if filter_spec == "blob:none" else 1)
        assert all("2 file(s)" in line for line in blame)
        trees = [r.message for r in caplog.records if "trees are missing" in r.message]
        if filter_spec == "tree:0":
            assert len(trees) == 2 and "run `devgraph rescan --full`" in trees[0]
        else:
            assert trees == []
        edges = graph_engine.run_cypher(
            "MATCH (:Commit {repo_id: $repo_id})-[r:MODIFIES]->(:Module {name: 'a.py'}) RETURN count(r) AS n",
            {"repo_id": record.repo_id},
        )
        # A blobless clone holds every tree, so every commit's paths are read without
        # rename detection; a treeless one lacks every parent tree, so no commit's paths are read.
        assert edges[0]["n"] == (3 if filter_spec == "blob:none" else 0)
    finally:
        graph_engine.delete_repository(record.repo_id)


def _git_log_modifies(repo_path: Path) -> dict[str, set[str]]:
    """sha -> files, as `git log --name-only` shows them (a shallow
    boundary commit lists its whole tree, like a root commit)."""
    out = subprocess.run(
        ["git", "log", "--name-only", "--format=@%H"], cwd=str(repo_path), capture_output=True, text=True, check=True
    ).stdout
    modifies: dict[str, set[str]] = {}
    sha = None
    for line in out.splitlines():
        if line.startswith("@"):
            sha = line[1:]
            modifies[sha] = set()
        elif line:
            modifies[sha].add(line)
    return modifies


def test_deepening_a_shallow_clone_corrects_its_history(graph_engine, registry, tmp_path):
    """At depth 1 the one commit is a boundary and counts every file as
    touched. After `git fetch --deepen`, HEAD hasn't moved, but the sync
    notices the new boundary and re-reads: each commit's MODIFIES edges are
    replaced, so the old boundary keeps only the file it changed."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _run_git(origin, "init")
    _run_git(origin, "config", "user.email", "test@example.com")
    _run_git(origin, "config", "user.name", "Test Author")
    for name in ("a", "b", "c", "d"):
        (origin / f"{name}.py").write_text(f"{name} = 1\n")
        _run_git(origin, "add", ".")
        _run_git(origin, "commit", "-m", f"Add {name}")
    (origin / "a.py").write_text("a = 2\n")
    _run_git(origin, "commit", "-am", "Edit a")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", "--depth=1", f"file://{origin}", str(clone)], check=True)
    record = registry.add_repo(clone, repo_id="_shallow_deepen_test")
    for name in ("a", "b", "c", "d"):
        graph_engine.upsert_node("Module", record.repo_id, f"{name}.py", {})

    def graphed() -> dict[str, set[str]]:
        rows = graph_engine.run_cypher(
            "MATCH (c:Commit {repo_id: $r}) OPTIONAL MATCH (c)-[:MODIFIES]->(m:Module) "
            "RETURN c.name AS sha, collect(m.name) AS files",
            {"r": record.repo_id},
        )
        return {row["sha"]: set(row["files"]) for row in rows}

    try:
        assert sync_git_history(graph_engine, registry, record.repo_id)["mode"] == "initial"
        assert graphed() == _git_log_modifies(clone)
        _run_git(clone, "fetch", "--quiet", "--deepen=2")
        assert sync_git_history(graph_engine, registry, record.repo_id)["mode"] == "reconcile"
        assert graphed() == _git_log_modifies(clone)
        assert len(graphed()) == 3
        assert sync_git_history(graph_engine, registry, record.repo_id)["mode"] == "noop"
    finally:
        graph_engine.delete_repository(record.repo_id)


def test_same_named_methods_of_one_file_each_get_their_own_recency(graph_engine, registry, tmp_path):
    """Two classes' `act` in one file are two nodes (owners A and B): each
    gets the date of the commit that last touched its own lines, whatever
    order the rows are written in."""
    from devgraph.graph.engine import provision_repository_schema
    from devgraph.indexer.dispatch import full_scan

    root = tmp_path / "repo"
    root.mkdir()
    _run_git(root, "init")
    _run_git(root, "config", "user.email", "test@example.com")
    _run_git(root, "config", "user.name", "Test Author")
    (root / "svc.py").write_text("class A:\n    def act(self):\n        return 1\n")
    _run_git(root, "add", "svc.py")
    _run_git(root, "commit", "-m", "Add A", "--date", "2020-01-01T00:00:00+00:00")
    (root / "svc.py").write_text(
        "class A:\n    def act(self):\n        return 1\n\n\nclass B:\n    def act(self):\n        return 2\n"
    )
    _run_git(root, "commit", "-am", "Add B", "--date", "2021-01-01T00:00:00+00:00")
    record = registry.add_repo(root, repo_id="_smoketest_sync_owner")

    def recency():
        rows = graph_engine.run_cypher(
            "MATCH (f:Function {repo_id: $r, name: 'act'}) RETURN f.owner AS owner, f.last_modified_at AS at",
            {"r": record.repo_id},
        )
        return {row["owner"]: (row["at"] or "")[:4] for row in rows}

    try:
        provision_repository_schema(graph_engine, root)
        graph_engine.upsert_repository(record.repo_id, record.repo_id, str(root))
        full_scan(graph_engine, record.repo_id, root)
        assert sync_git_history(graph_engine, registry, record.repo_id)["mode"] == "initial"
        assert recency() == {"A": "2020", "B": "2021"}
        assert sync_git_history(graph_engine, registry, record.repo_id, force=True)["mode"] == "reconcile"
        assert recency() == {"A": "2020", "B": "2021"}
        rows = [
            {"name": "act", "file": "svc.py", "owner": owner, "created_at": None, "last_modified_at": at,
             "last_modified_by": None}
            for owner, at in (("A", "2019-01-01"), ("B", "2018-01-01"))
        ]
        for ordered in (rows, rows[::-1]):
            graph_engine.write_recency("Function", record.repo_id, ordered, overwrite=True)
            assert recency() == {"A": "2019", "B": "2018"}
    finally:
        graph_engine.delete_repository(record.repo_id)
