"""delete_extracted_nodes / prune_extracted_nodes against a live Neo4j."""

import pytest

from devgraph.graph.engine import GraphEngine

REPO = "_smoketest_extracted_nodes"
OTHER = "_smoketest_extracted_nodes_other"


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    for repo in (REPO, OTHER):
        test_engine.delete_repository(repo)
    yield test_engine
    for repo in (REPO, OTHER):
        test_engine.delete_repository(repo)
    test_engine.close()


def seed(engine, repo, label, paths, extractor="filesystem"):
    engine.upsert_nodes([
        {"label": label, "repo_id": repo, "name": p, "properties": {"path": p, "extractor": extractor}}
        for p in paths
    ])


def names(engine, repo):
    rows = engine.run_cypher("MATCH (n {repo_id: $r}) RETURN labels(n)[0] + ':' + n.name AS k", {"r": repo})
    return sorted(r["k"] for r in rows)


def test_delete_removes_exact_paths_and_everything_below_a_directory(engine):
    seed(engine, REPO, "FsFile", ["src/a.py", "src/sub/b.py", "src2/c.py", "top.py"])
    seed(engine, REPO, "FsFolder", [".", "src", "src/sub", "src2"])
    engine.delete_extracted_nodes(REPO, "filesystem", ["src", "top.py"])
    assert names(engine, REPO) == ["FsFile:src2/c.py", "FsFolder:.", "FsFolder:src2"]


def test_delete_leaves_other_extractors_and_repos_alone(engine):
    seed(engine, REPO, "FsFile", ["src/a.py"])
    seed(engine, REPO, "FsFile", ["src/b.py"], extractor="other")
    engine.upsert_nodes([{"label": "Module", "repo_id": REPO, "name": "src/c.py", "properties": {"source_file": "src/c.py"}}])
    seed(engine, OTHER, "FsFile", ["src/a.py"])
    engine.delete_extracted_nodes(REPO, "filesystem", ["src"])
    assert names(engine, REPO) == ["FsFile:src/b.py", "Module:src/c.py"]
    assert names(engine, OTHER) == ["FsFile:src/a.py"]


def test_delete_with_no_paths_is_a_no_op(engine):
    seed(engine, REPO, "FsFile", ["a.py"])
    engine.delete_extracted_nodes(REPO, "filesystem", [])
    assert names(engine, REPO) == ["FsFile:a.py"]


def test_prune_keeps_only_listed_nodes_and_counts_the_rest(engine):
    seed(engine, REPO, "FsFile", ["a.py", "b.py"])
    seed(engine, REPO, "FsFolder", ["."])
    seed(engine, REPO, "OldType", ["x"])
    seed(engine, REPO, "FsFile", ["kept-by-other-extractor.py"], extractor="other")
    pruned = engine.prune_extracted_nodes(REPO, "filesystem", ["FsFile:a.py", "FsFolder:."])
    assert pruned == 2
    assert names(engine, REPO) == ["FsFile:a.py", "FsFile:kept-by-other-extractor.py", "FsFolder:."]


def test_prune_with_an_empty_keep_list_removes_every_provider_node(engine):
    seed(engine, REPO, "FsFile", ["a.py"])
    assert engine.prune_extracted_nodes(REPO, "filesystem", []) == 1
    assert names(engine, REPO) == []


def test_delete_also_removes_relationships(engine):
    seed(engine, REPO, "FsFile", ["d/a.py"])
    seed(engine, REPO, "FsFolder", ["d"])
    engine.upsert_relationships([{
        "from_label": "FsFile", "from_name": "d/a.py", "rel_type": "IN_DIR",
        "to_label": "FsFolder", "to_name": "d", "repo_id": REPO, "properties": {},
    }])
    engine.delete_extracted_nodes(REPO, "filesystem", ["d/a.py"])
    rels = engine.run_cypher("MATCH ({repo_id: $r})-[x]->() RETURN count(x) AS n", {"r": REPO})
    assert rels == [{"n": 0}]
