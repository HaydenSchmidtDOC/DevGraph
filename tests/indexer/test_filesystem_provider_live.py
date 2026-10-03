"""Worktree example end to end through dispatch, against a live Neo4j."""

import shutil
import textwrap

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer import dispatch
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths
from devgraph.indexer.providers import filesystem

REPO = "_smoketest_fs_provider"
WORKTREE = """
    version: 1
    node_types:
      - label: File
        key: [path]
        metadata: [{name: path}]
        source: {provider: filesystem, kind: file}
      - label: Folder
        key: [path]
        metadata: [{name: path}]
        source: {provider: filesystem, kind: folder}
    relationships:
      - type: IS_CHILD_OF
        provider: filesystem
        from: [File, Folder]
        to: Folder
"""


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.delete_repository(REPO)
    yield test_engine
    test_engine.delete_repository(REPO)
    test_engine.close()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg" / "sub").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("class Widget:\n    def run(self):\n        return 1\n")
    (root / "pkg" / "sub" / "util.py").write_text("def helper():\n    return 2\n")
    (root / "README.md").write_text("# Demo\n")
    (root / "node_modules" / "dep").mkdir(parents=True)
    (root / "node_modules" / "dep" / "index.js").write_text("module.exports = 1;\n")
    return root


def with_schema(root, text=WORKTREE):
    (root / "devgraph.schema.yaml").write_text(textwrap.dedent(text))
    return root


def fs_nodes(engine):
    rows = engine.run_cypher(
        "MATCH (n {repo_id: $r}) WHERE n.extractor = 'filesystem' RETURN labels(n)[0] + ':' + n.name AS k",
        {"r": REPO},
    )
    return sorted(r["k"] for r in rows)


def fs_edges(engine):
    rows = engine.run_cypher(
        "MATCH (a {repo_id: $r})-[x:IS_CHILD_OF]->(b {repo_id: $r}) RETURN a.name + '>' + b.name AS e",
        {"r": REPO},
    )
    return sorted(r["e"] for r in rows)


def snapshot(engine):
    nodes = engine.run_cypher(
        "MATCH (n {repo_id: $r}) RETURN labels(n) AS labels, n.name AS name, n.file AS file", {"r": REPO}
    )
    rels = engine.run_cypher(
        "MATCH (a {repo_id: $r})-[x]->(b {repo_id: $r}) "
        "RETURN labels(a)[0] AS a, a.name AS an, type(x) AS t, labels(b)[0] AS b, b.name AS bn",
        {"r": REPO},
    )
    return (
        sorted((tuple(sorted(n["labels"])), n["name"] or "", n["file"] or "") for n in nodes),
        sorted((r["a"], r["an"] or "", r["t"], r["b"], r["bn"] or "") for r in rels),
    )


def scan(engine, root):
    provision_repository_schema(engine, root)
    engine.upsert_repository(REPO, REPO, str(root))
    full_scan(engine, REPO, root)


def test_full_scan_builds_the_worktree_graph_beside_builtin_nodes(engine, repo):
    scan(engine, with_schema(repo))
    assert fs_nodes(engine) == [
        "File:README.md", "File:devgraph.schema.yaml", "File:pkg/mod.py", "File:pkg/sub/util.py",
        "Folder:.", "Folder:pkg", "Folder:pkg/sub",
    ]
    assert fs_edges(engine) == [
        "README.md>.", "devgraph.schema.yaml>.", "pkg/mod.py>pkg", "pkg/sub/util.py>pkg/sub",
        "pkg/sub>pkg", "pkg>.",
    ]
    modules = engine.run_cypher("MATCH (m:Module {repo_id: $r}) RETURN m.name AS n", {"r": REPO})
    assert "pkg/mod.py" in {m["n"] for m in modules}  # built-in extraction unaffected


def test_ignored_directories_never_become_nodes(engine, repo):
    scan(engine, with_schema(repo))
    index_paths(engine, REPO, repo, {repo / "node_modules" / "dep" / "index.js"})
    assert not [k for k in fs_nodes(engine) if "node_modules" in k]


def test_new_file_and_folder_are_added_incrementally(engine, repo):
    scan(engine, with_schema(repo))
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("# Guide\n")
    index_paths(engine, REPO, repo, {repo / "docs" / "guide.md"})
    assert {"File:docs/guide.md", "Folder:docs"} <= set(fs_nodes(engine))
    assert {"docs/guide.md>docs", "docs>."} <= set(fs_edges(engine))


def test_deleting_the_last_file_removes_emptied_folders(engine, repo):
    scan(engine, with_schema(repo))
    (repo / "pkg" / "sub" / "util.py").unlink()
    remove_paths(engine, REPO, repo, {repo / "pkg" / "sub" / "util.py"})
    nodes = fs_nodes(engine)
    assert "File:pkg/sub/util.py" not in nodes and "Folder:pkg/sub" not in nodes
    assert "Folder:pkg" in nodes and "Folder:." in nodes


def test_deleting_a_whole_directory_removes_everything_below_it(engine, repo):
    scan(engine, with_schema(repo))
    shutil.rmtree(repo / "pkg")
    remove_paths(engine, REPO, repo, {repo / "pkg"})
    assert not [k for k in fs_nodes(engine) if ":pkg" in k]
    assert "Folder:." in fs_nodes(engine)


def test_removing_the_schema_prunes_every_filesystem_node_on_rescan(engine, repo):
    scan(engine, with_schema(repo))
    (repo / "devgraph.schema.yaml").unlink()
    scan(engine, repo)
    assert fs_nodes(engine) == []


def test_an_invalid_schema_leaves_existing_filesystem_nodes_alone(engine, repo):
    scan(engine, with_schema(repo))
    before = fs_nodes(engine)
    (repo / "devgraph.schema.yaml").write_text("version: 1\nnode_types: [oops\n")
    (repo / "pkg" / "new.py").write_text("x = 1\n")
    full_scan(engine, REPO, repo)  # provisioning would refuse; the scan itself must not prune
    assert fs_nodes(engine) == before
    modules = engine.run_cypher("MATCH (m:Module {repo_id: $r}) RETURN m.name AS n", {"r": REPO})
    assert "pkg/new.py" in {m["n"] for m in modules}  # built-in indexing carried on


def test_without_a_schema_file_the_graph_is_unchanged(engine, repo, monkeypatch):
    scan(engine, repo)
    with_provider = snapshot(engine)
    assert fs_nodes(engine) == []

    engine.delete_repository(REPO)
    monkeypatch.setattr(filesystem, "sync_present", lambda *a, **k: None)
    monkeypatch.setattr(filesystem, "sync_absent", lambda *a, **k: None)
    monkeypatch.setattr(filesystem, "reconcile", lambda *a, **k: 0)
    scan(engine, repo)
    assert snapshot(engine) == with_provider


def _plan_operators(plan):
    ops = [plan["operatorType"]]
    for child in plan.get("children", []):
        ops += _plan_operators(child)
    return ops


def test_provider_writes_are_served_by_a_repo_name_index(engine, repo):
    provision_repository_schema(engine, with_schema(repo))

    for label in ("File", "Folder"):
        with engine._driver.session() as session:
            # A freshly created index is not planned against until it is online.
            session.run("CALL db.awaitIndexes(60)").consume()
            plan = session.run(
                f"EXPLAIN MERGE (n:{label} {{repo_id: $r, name: $n}}) RETURN n", r=REPO, n="x"
            ).consume().plan
        operators = _plan_operators(plan)
        assert any("IndexSeek" in op for op in operators), operators
        assert not any("NodeByLabelScan" in op for op in operators), operators


def _index_names(engine):
    return {r["name"] for r in engine.run_cypher("SHOW INDEXES YIELD name RETURN name")}


def test_saving_a_new_schema_syncs_the_whole_worktree(engine, repo):
    scan(engine, repo)  # registered with no schema
    assert fs_nodes(engine) == []
    schema = with_schema(repo) / "devgraph.schema.yaml"

    index_paths(engine, REPO, repo, {schema})  # the watcher's event for the save

    assert fs_nodes(engine) == [
        "File:README.md", "File:devgraph.schema.yaml", "File:pkg/mod.py", "File:pkg/sub/util.py",
        "Folder:.", "Folder:pkg", "Folder:pkg/sub",
    ]
    assert {"file_repo_name", "folder_repo_name"} <= _index_names(engine)


def test_renaming_a_label_in_the_schema_leaves_no_old_label_nodes(engine, repo):
    scan(engine, with_schema(repo))
    schema = repo / "devgraph.schema.yaml"
    schema.write_text(textwrap.dedent(WORKTREE).replace("File", "Entry"))

    index_paths(engine, REPO, repo, {schema})

    nodes = fs_nodes(engine)
    assert not [k for k in nodes if k.startswith("File:")]
    assert "Entry:pkg/mod.py" in nodes and "Folder:pkg" in nodes
    assert "pkg/mod.py>pkg" in fs_edges(engine)


def test_deleting_the_schema_prunes_every_filesystem_node(engine, repo):
    scan(engine, with_schema(repo))
    schema = repo / "devgraph.schema.yaml"
    schema.unlink()

    remove_paths(engine, REPO, repo, {schema})

    assert fs_nodes(engine) == []
    modules = engine.run_cypher("MATCH (m:Module {repo_id: $r}) RETURN m.name AS n", {"r": REPO})
    assert "pkg/mod.py" in {m["n"] for m in modules}  # built-in nodes untouched


def test_moving_a_file_in_one_batch_keeps_only_the_destination(engine, repo):
    scan(engine, with_schema(repo))
    (repo / "lib").mkdir()
    (repo / "pkg" / "mod.py").rename(repo / "lib" / "mod.py")

    # The watcher's order: the destination is indexed, then the source removed.
    index_paths(engine, REPO, repo, {repo / "lib" / "mod.py"})
    remove_paths(engine, REPO, repo, {repo / "pkg" / "mod.py"})

    nodes = fs_nodes(engine)
    assert "File:lib/mod.py" in nodes and "Folder:lib" in nodes
    assert "File:pkg/mod.py" not in nodes
    assert "Folder:pkg" in nodes and "File:pkg/sub/util.py" in nodes
