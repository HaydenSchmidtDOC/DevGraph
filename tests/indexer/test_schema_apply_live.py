"""Applying a project schema: recorded state, pending pause, removed-type cleanup."""

import textwrap
import uuid

import pytest

from devgraph.config.project_schema import ABSENT_SCHEMA_HASH, schema_file_hash
from devgraph.graph.engine import GraphEngine
from devgraph.indexer.dispatch import apply_project_schema, full_scan, index_paths, schema_pending

REPO = "_smoketest_schema_apply"
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

# Unique per run: these tests drop and re-create the labels' generated
# constraints, which are database-wide and shared with real repositories.
_TOKEN = uuid.uuid4().hex[:8]
FILE, FOLDER, ENTRY = (f"ZzFile{_TOKEN}", f"ZzFolder{_TOKEN}", f"ZzEntry{_TOKEN}")
GADGET = f"ZzGadget{_TOKEN}"
_SHOWN = {FILE: "File", FOLDER: "Folder", ENTRY: "Entry"}


@pytest.fixture(scope="module", autouse=True)
def _drop_generated_constraints():
    yield
    cleanup = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        for label in (FILE, FOLDER, ENTRY, GADGET):
            cleanup.run_cypher(f"DROP CONSTRAINT {label.lower()}_repo_key IF EXISTS")
            cleanup.run_cypher(f"DROP INDEX {label.lower()}_repo_name IF EXISTS")
    except Exception:
        pass  # Neo4j unavailable: the tests were skipped
    finally:
        cleanup.close()


def _shown(key):
    """`Label:name` with the per-run label shown as the plain name the assertions use."""
    label, _, name = key.partition(":")
    return f"{_SHOWN.get(label, label)}:{name}"

WORKTREE = WORKTREE.replace("File", FILE).replace("Folder", FOLDER)
WIDGETS = """
      - label: ZzGadget
        key: [slug]
        metadata: [{name: slug}]
"""
LINKS = """
      - type: ZZ_LINKS
        provider: custom
        custom: {name: linker}
        from: ZzGadget
        to: ZzGadget
"""
WIDGETS = WIDGETS.replace("ZzGadget", GADGET)
LINKS = LINKS.replace("ZzGadget", GADGET)


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
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("def run():\n    return 1\n")
    return root


def write_schema(root, text):
    (root / "devgraph.schema.yaml").write_text(textwrap.dedent(text))


def worktree_with_gadgets(links=True):
    text = WORKTREE.replace("    relationships:", WIDGETS.rstrip("\n") + "\n    relationships:")
    return text + (LINKS if links else "")


def fs_nodes(engine):
    rows = engine.run_cypher(
        "MATCH (n {repo_id: $r}) WHERE n.extractor = 'filesystem' RETURN labels(n)[0] + ':' + n.name AS k", {"r": REPO}
    )
    return sorted(_shown(r["k"]) for r in rows)


def scan(engine, root):
    engine.upsert_repository(REPO, REPO, str(root))
    full_scan(engine, REPO, root)


def test_a_repo_without_a_schema_is_never_pending(engine, repo):
    engine.upsert_repository(REPO, REPO, str(repo))
    assert not schema_pending(engine, REPO, repo)  # no state recorded yet, no file
    scan(engine, repo)
    assert engine.read_applied_schema(REPO)["hash"] == ABSENT_SCHEMA_HASH
    assert not schema_pending(engine, REPO, repo)


def test_full_scan_applies_and_records_the_schema(engine, repo):
    write_schema(repo, worktree_with_gadgets())
    scan(engine, repo)
    state = engine.read_applied_schema(REPO)
    assert state["hash"] == schema_file_hash(repo)
    assert state["labels"] == [FILE, FOLDER, GADGET]
    assert state["relationship_types"] == ["IS_CHILD_OF", "ZZ_LINKS"]
    assert not schema_pending(engine, REPO, repo)
    assert "File:pkg/mod.py" in fs_nodes(engine)


def test_editing_the_schema_pauses_provider_writes_until_applied(engine, repo):
    scan(engine, repo)
    write_schema(repo, WORKTREE)
    assert schema_pending(engine, REPO, repo)
    index_paths(engine, REPO, repo, {repo / "devgraph.schema.yaml"})  # the watcher's event for the save
    (repo / "pkg" / "new.py").write_text("x = 1\n")
    index_paths(engine, REPO, repo, {repo / "pkg" / "new.py"})
    assert fs_nodes(engine) == []  # nothing written under an unapplied schema
    modules = engine.run_cypher("MATCH (m:Module {repo_id: $r}) RETURN m.name AS n", {"r": REPO})
    assert "pkg/new.py" in {m["n"] for m in modules}  # built-in extraction carries on

    assert apply_project_schema(engine, REPO, repo)
    assert {"File:pkg/new.py", "Folder:pkg"} <= set(fs_nodes(engine))
    assert not schema_pending(engine, REPO, repo)


def test_removed_user_types_are_deleted_and_others_kept(engine, repo):
    write_schema(repo, worktree_with_gadgets())
    scan(engine, repo)
    engine.run_cypher(
        f"CREATE (a:{GADGET} {{repo_id: $r, slug: 'a', name: 'a'}})-[:ZZ_LINKS]->(b:{GADGET} {{repo_id: $r, slug: 'b', name: 'b'}})",
        {"r": REPO},
    )
    write_schema(repo, worktree_with_gadgets(links=False))  # drop the relationship type only
    assert apply_project_schema(engine, REPO, repo)
    assert engine.run_cypher("MATCH ({repo_id: $r})-[x:ZZ_LINKS]->() RETURN count(x) AS n", {"r": REPO}) == [{"n": 0}]
    assert engine.run_cypher(f"MATCH (n:{GADGET} {{repo_id: $r}}) RETURN count(n) AS n", {"r": REPO}) == [{"n": 2}]

    write_schema(repo, WORKTREE)  # now drop the node type too
    assert apply_project_schema(engine, REPO, repo)
    assert engine.run_cypher(f"MATCH (n:{GADGET} {{repo_id: $r}}) RETURN count(n) AS n", {"r": REPO}) == [{"n": 0}]
    assert "File:pkg/mod.py" in fs_nodes(engine)
    assert engine.run_cypher("MATCH (m:Module {repo_id: $r}) RETURN count(m) AS n", {"r": REPO})[0]["n"] > 0


def test_an_invalid_schema_changes_nothing(engine, repo):
    write_schema(repo, WORKTREE)
    scan(engine, repo)
    before = fs_nodes(engine)
    state = engine.read_applied_schema(REPO)
    (repo / "devgraph.schema.yaml").write_text("version: 1\nnode_types: [oops\n")
    assert apply_project_schema(engine, REPO, repo) is False
    full_scan(engine, REPO, repo)
    assert fs_nodes(engine) == before
    assert engine.read_applied_schema(REPO) == state
    assert schema_pending(engine, REPO, repo)


def test_a_tampered_label_list_never_reaches_cypher(engine, repo):
    write_schema(repo, WORKTREE)
    scan(engine, repo)
    engine.record_applied_schema(REPO, "sha256:old", [FILE, FOLDER, "Bad`) DETACH DELETE n //"], ["NOT VALID"])
    assert apply_project_schema(engine, REPO, repo)  # skips the invalid names, no Cypher error
    assert "File:pkg/mod.py" in fs_nodes(engine)


def test_switching_the_project_config_off_removes_user_nodes_on_rescan(engine, repo, tmp_path, monkeypatch):
    from devgraph.config import project_switch
    from devgraph.registry.store import RepoRegistry

    (repo / ".git").mkdir()
    registry = RepoRegistry(tmp_path / "switch.sqlite3")
    registry.add_repo(repo, repo_id=REPO)
    monkeypatch.setattr(project_switch, "_registry_db_path", lambda: tmp_path / "switch.sqlite3")

    write_schema(repo, WORKTREE)
    scan(engine, repo)
    assert "File:pkg/mod.py" in fs_nodes(engine)
    assert not schema_pending(engine, REPO, repo)

    registry.set_project_config_enabled(REPO, False)
    assert schema_pending(engine, REPO, repo)
    full_scan(engine, REPO, repo)
    assert fs_nodes(engine) == []
    assert engine.read_applied_schema(REPO)["hash"] == ABSENT_SCHEMA_HASH
    assert not schema_pending(engine, REPO, repo)

    registry.set_project_config_enabled(REPO, True)
    assert schema_pending(engine, REPO, repo)
    full_scan(engine, REPO, repo)
    assert "File:pkg/mod.py" in fs_nodes(engine)
