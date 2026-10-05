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


# --- path-scoped prune, edge delete and property clear (declarative docs provider) ---


def rel(engine, repo, from_label, from_name, rel_type, to_label, to_name):
    engine.upsert_relationships([{
        "from_label": from_label, "from_name": from_name, "rel_type": rel_type,
        "to_label": to_label, "to_name": to_name, "repo_id": repo, "properties": {},
    }])


def edges(engine, repo):
    rows = engine.run_cypher(
        "MATCH (a {repo_id: $r})-[x]->(b) RETURN a.name + '-' + type(x) + '->' + b.name AS e", {"r": repo}
    )
    return sorted(r["e"] for r in rows)


def keys_of(engine, repo, label, name):
    rows = engine.run_cypher(
        "MATCH (n {repo_id: $r, name: $n}) WHERE labels(n)[0] = $l RETURN keys(n) AS k",
        {"r": repo, "n": name, "l": label},
    )
    return sorted(rows[0]["k"])


def test_prune_at_deletes_only_unkept_nodes_at_those_exact_paths(engine):
    seed(engine, REPO, "Runbook", ["rb/a.md", "rb/b.md", "rb/c.md"], extractor="docs")
    seed(engine, REPO, "Guide", ["rb/a.md"], extractor="docs")
    seed(engine, REPO, "Runbook", ["rb/a.md/below.md"], extractor="docs")
    pruned = engine.prune_extracted_at(REPO, "docs", ["rb/a.md", "rb/b.md"], ["Runbook:rb/a.md"])
    assert pruned == 2
    assert names(engine, REPO) == ["Runbook:rb/a.md", "Runbook:rb/a.md/below.md", "Runbook:rb/c.md"]


def test_prune_at_leaves_other_extractors_repos_and_builtins_alone(engine):
    seed(engine, REPO, "Runbook", ["rb/a.md"], extractor="docs")
    seed(engine, REPO, "FsFile", ["rb/a.md"], extractor="filesystem")
    engine.upsert_nodes([
        {"label": "Document", "repo_id": REPO, "name": "rb/a.md", "properties": {"source_file": "rb/a.md"}}
    ])
    seed(engine, OTHER, "Runbook", ["rb/a.md"], extractor="docs")
    assert engine.prune_extracted_at(REPO, "docs", ["rb/a.md"], []) == 1
    assert names(engine, REPO) == ["Document:rb/a.md", "FsFile:rb/a.md"]
    assert names(engine, OTHER) == ["Runbook:rb/a.md"]


def test_delete_edges_removes_only_outgoing_non_builtin_edges_at_the_paths(engine):
    seed(engine, REPO, "Runbook", ["rb/a.md", "rb/b.md"], extractor="docs")
    seed(engine, REPO, "FsFile", ["x.py"], extractor="filesystem")
    engine.upsert_nodes([{"label": "Service", "repo_id": REPO, "name": "pay", "properties": {}}])
    rel(engine, REPO, "Runbook", "rb/a.md", "RUNBOOK_FOR", "Service", "pay")
    rel(engine, REPO, "Runbook", "rb/a.md", "MENTIONS", "Service", "pay")
    rel(engine, REPO, "Runbook", "rb/b.md", "RUNBOOK_FOR", "Service", "pay")
    rel(engine, REPO, "Service", "pay", "OWNED_BY", "Runbook", "rb/a.md")
    rel(engine, REPO, "FsFile", "x.py", "OWNED_BY", "Runbook", "rb/a.md")
    deleted = engine.delete_extracted_edges(REPO, "docs", ["rb/a.md"])
    assert deleted == 1
    assert edges(engine, REPO) == [
        "pay-OWNED_BY->rb/a.md",
        "rb/a.md-MENTIONS->pay",
        "rb/b.md-RUNBOOK_FOR->pay",
        "x.py-OWNED_BY->rb/a.md",
    ]


def test_delete_edges_repo_wide_when_paths_is_none(engine):
    seed(engine, REPO, "Runbook", ["rb/a.md", "rb/b.md"], extractor="docs")
    seed(engine, REPO, "FsFile", ["x.py"], extractor="filesystem")
    engine.upsert_nodes([{"label": "Service", "repo_id": REPO, "name": "pay", "properties": {}}])
    seed(engine, OTHER, "Runbook", ["rb/a.md"], extractor="docs")
    engine.upsert_nodes([{"label": "Service", "repo_id": OTHER, "name": "pay", "properties": {}}])
    rel(engine, REPO, "Runbook", "rb/a.md", "RUNBOOK_FOR", "Service", "pay")
    rel(engine, REPO, "Runbook", "rb/b.md", "FORMER_TYPE", "Runbook", "rb/a.md")
    rel(engine, REPO, "Runbook", "rb/b.md", "DOCUMENTED_BY", "Service", "pay")
    rel(engine, REPO, "FsFile", "x.py", "OWNED_BY", "Service", "pay")
    rel(engine, OTHER, "Runbook", "rb/a.md", "RUNBOOK_FOR", "Service", "pay")
    assert engine.delete_extracted_edges(REPO, "docs", None) == 2
    assert edges(engine, REPO) == ["rb/b.md-DOCUMENTED_BY->pay", "x.py-OWNED_BY->pay"]
    assert edges(engine, OTHER) == ["rb/a.md-RUNBOOK_FOR->pay"]


def test_clear_properties_removes_undeclared_and_keeps_declared_reserved_and_insights(engine):
    engine.upsert_nodes([
        {"label": "Runbook", "repo_id": REPO, "name": "rb/a.md", "properties": {
            "path": "rb/a.md", "extractor": "docs", "owner": "ops", "severity": 2,
            "old_field": "x", "insight_pagerank": 0.5, "source": "kept",
        }},
        {"label": "Guide", "repo_id": REPO, "name": "g.md", "properties": {
            "path": "g.md", "extractor": "docs", "old_field": "other label",
        }},
        {"label": "Runbook", "repo_id": REPO, "name": "fs.md", "properties": {
            "path": "fs.md", "extractor": "filesystem", "old_field": "other extractor",
        }},
    ])
    engine.upsert_nodes([{"label": "Runbook", "repo_id": OTHER, "name": "rb/a.md", "properties": {
        "path": "rb/a.md", "extractor": "docs", "old_field": "other repo",
    }}])
    removed = engine.clear_extracted_properties(REPO, "docs", "Runbook", ["path", "owner"])
    assert removed == ["old_field", "severity"]
    assert keys_of(engine, REPO, "Runbook", "rb/a.md") == [
        "extractor", "insight_pagerank", "name", "owner", "path", "repo_id", "source", "sources",
    ]
    assert "old_field" in keys_of(engine, REPO, "Guide", "g.md")
    assert "old_field" in keys_of(engine, REPO, "Runbook", "fs.md")
    assert "old_field" in keys_of(engine, OTHER, "Runbook", "rb/a.md")


def test_clear_properties_never_interpolates_a_key_that_fails_the_pattern(engine):
    seed(engine, REPO, "Runbook", ["rb/a.md"], extractor="docs")
    bad = {"Bad-Key": 1, "x` = 1 DETACH DELETE n //": 2, "stale": 3}
    engine.run_cypher(
        "MATCH (n {repo_id: $r, name: 'rb/a.md'}) SET n += $props", {"r": REPO, "props": bad}
    )
    assert engine.clear_extracted_properties(REPO, "docs", "Runbook", ["path"]) == ["stale"]
    keys = keys_of(engine, REPO, "Runbook", "rb/a.md")
    assert "Bad-Key" in keys and "x` = 1 DETACH DELETE n //" in keys and "stale" not in keys


def test_clear_properties_with_nothing_to_clear_removes_nothing(engine):
    seed(engine, REPO, "Runbook", ["rb/a.md"], extractor="docs")
    assert engine.clear_extracted_properties(REPO, "docs", "Runbook", ["path"]) == []
    assert engine.clear_extracted_properties(REPO, "docs", "Absent", []) == []


class _NoDriver:
    def session(self):
        raise AssertionError("no query should run")


def test_empty_inputs_make_no_call():
    offline = GraphEngine.__new__(GraphEngine)
    offline._driver = _NoDriver()
    assert offline.prune_extracted_at(REPO, "docs", [], ["Runbook:a.md"]) == 0
    assert offline.delete_extracted_edges(REPO, "docs", []) == 0
