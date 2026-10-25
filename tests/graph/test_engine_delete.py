"""Tests for GraphEngine.delete_nodes_by_source_file — the file-delete
cleanup path that was previously entirely missing (only whole-repo
delete_repository existed)."""

import uuid

import pytest

from devgraph.graph.engine import GraphEngine

# Unique per run, so sessions sharing the Neo4j never seed or delete each
# other's repositories; tests/conftest.py sweeps any a teardown leaves.
_TOKEN = uuid.uuid4().hex[:8]


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.init_schema()
    yield test_engine
    test_engine.close()


class TestDeleteNodesBySourceFile:
    def test_deletes_nodes_matching_source_file_property(self, engine):
        repo_id = f"_smoketest_delete_source_file_{_TOKEN}"
        engine.upsert_node("Module", repo_id, "gone.py", {"type": "module", "source_file": "gone.py"})
        engine.upsert_node("Class", repo_id, "GoneClass", {"file": "gone.py"})
        engine.upsert_node("Module", repo_id, "kept.py", {"type": "module", "source_file": "kept.py"})

        try:
            engine.delete_nodes_by_source_file(repo_id, "gone.py")

            remaining = engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) RETURN n.name as name", {"repo_id": repo_id}
            )
            names = {r["name"] for r in remaining}
            assert names == {"kept.py"}
        finally:
            engine.delete_repository(repo_id)

    def test_matches_source_property_variant(self, engine):
        """datastores/apis/containers extractors use `source` instead of `source_file`/`file`."""
        repo_id = f"_smoketest_delete_source_variant_{_TOKEN}"
        engine.upsert_node("Database", repo_id, "PostgreSQL", {"source": "db.py"})

        try:
            engine.delete_nodes_by_source_file(repo_id, "db.py")
            remaining = engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) RETURN COUNT(*) as c", {"repo_id": repo_id}
            )
            assert remaining[0]["c"] == 0
        finally:
            engine.delete_repository(repo_id)

    def test_scoped_to_repo_id(self, engine):
        repo_a = f"_smoketest_delete_scope_a_{_TOKEN}"
        repo_b = f"_smoketest_delete_scope_b_{_TOKEN}"
        engine.upsert_node("Module", repo_a, "shared_name.py", {"source_file": "shared_name.py"})
        engine.upsert_node("Module", repo_b, "shared_name.py", {"source_file": "shared_name.py"})

        try:
            engine.delete_nodes_by_source_file(repo_a, "shared_name.py")

            remaining_a = engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) RETURN COUNT(*) as c", {"repo_id": repo_a}
            )
            remaining_b = engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) RETURN COUNT(*) as c", {"repo_id": repo_b}
            )
            assert remaining_a[0]["c"] == 0
            assert remaining_b[0]["c"] == 1
        finally:
            engine.delete_repository(repo_a)
            engine.delete_repository(repo_b)

    def test_node_survives_while_another_file_still_claims_it(self, engine):
        """A Service/Database/... node can be co-produced by several files
        (e.g. docker-compose.yml and an override file both defining the same
        service). Deleting one producing file must not destroy a node the
        other file still produces — only delete once every claiming file is
        gone."""
        repo_id = f"_smoketest_delete_multi_source_{_TOKEN}"
        engine.upsert_node("Service", repo_id, "web", {"source": "docker-compose.yml"})
        engine.upsert_node("Service", repo_id, "web", {"source": "docker-compose.override.yml"})

        try:
            engine.delete_nodes_by_source_file(repo_id, "docker-compose.override.yml")
            remaining = engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) RETURN n.name as name, n.sources as sources",
                {"repo_id": repo_id},
            )
            assert len(remaining) == 1
            assert remaining[0]["name"] == "web"
            assert remaining[0]["sources"] == ["docker-compose.yml"]

            engine.delete_nodes_by_source_file(repo_id, "docker-compose.yml")
            remaining = engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) RETURN COUNT(*) as c", {"repo_id": repo_id}
            )
            assert remaining[0]["c"] == 0
        finally:
            engine.delete_repository(repo_id)

    def test_no_match_is_a_no_op(self, engine):
        repo_id = f"_smoketest_delete_no_match_{_TOKEN}"
        engine.upsert_node("Module", repo_id, "kept.py", {"source_file": "kept.py"})

        try:
            engine.delete_nodes_by_source_file(repo_id, "nonexistent.py")
            remaining = engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) RETURN COUNT(*) as c", {"repo_id": repo_id}
            )
            assert remaining[0]["c"] == 1
        finally:
            engine.delete_repository(repo_id)


def test_graph_repo_ids_includes_nodes_left_without_a_repository_node(engine):
    """A write that lands after `devgraph remove` deleted the graph leaves
    nodes whose Repository node is gone; `prune` and `doctor` find them here."""
    import uuid

    orphan = f"_smoketest_orphan_{uuid.uuid4().hex[:8]}"
    engine.upsert_node("Module", orphan, "late.py", {"type": "module", "source_file": "late.py"})
    try:
        assert orphan in engine.graph_repo_ids()
    finally:
        engine.delete_repository(orphan)
    assert orphan not in engine.graph_repo_ids()


def test_graph_repo_ids_skip_scans_every_label_and_scans_an_unindexed_one(engine):
    """Several repo_ids on one indexed label are each found (the skip scan
    steps past one to the next), as is one on a label no `repo_id` index
    covers."""
    import uuid

    tag = uuid.uuid4().hex[:8]
    ids = [f"_smoketest_graph_ids_{tag}_{i}" for i in range(3)]
    bare_label = f"ZzNoIndex{tag}"
    for repo_id in ids:
        engine.upsert_node("Function", repo_id, "f", {"file": "a.py"})
    engine.run_cypher(f"CREATE (:{bare_label} {{repo_id: $repo_id}})", {"repo_id": ids[0] + "_bare"})
    try:
        assert set(ids) | {ids[0] + "_bare"} <= engine.graph_repo_ids()
    finally:
        engine.run_cypher(f"MATCH (n:{bare_label}) DELETE n")
        for repo_id in ids:
            engine.delete_repository(repo_id)
    assert not {repo_id for repo_id in engine.graph_repo_ids() if tag in repo_id}


def test_graph_repo_ids_finds_nodes_no_composite_index_holds(engine):
    """A file-less handler-stub Function or C++ Class stub is in no
    `(repo_id, name, file)` index and a nameless Module in no `(repo_id,
    name)` one; an orphan made only of those is still found, as is an
    empty-string repo_id."""
    import uuid

    tag = uuid.uuid4().hex[:8]
    stubs, nameless = f"_smoketest_stubs_{tag}", f"_smoketest_nameless_{tag}"
    engine.run_cypher(
        "CREATE (:Function {repo_id: $stubs, name: 'handler', source: 'app.py'}), "
        "(:Class {repo_id: $stubs, name: 'Widget', source: 'widget.cpp'}), "
        "(:Module {repo_id: $nameless}), "
        "(:Endpoint {repo_id: '', name: $marker})",
        {"stubs": stubs, "nameless": nameless, "marker": f"_smoketest_empty_{tag}"},
    )
    try:
        assert {stubs, nameless, ""} <= engine.graph_repo_ids()
    finally:
        engine.delete_repository(stubs)
        engine.delete_repository(nameless)
        engine.run_cypher("MATCH (n:Endpoint {repo_id: '', name: $marker}) DELETE n", {"marker": f"_smoketest_empty_{tag}"})


def test_graph_repo_ids_reads_a_label_with_a_backtick(engine):
    import uuid

    tag = uuid.uuid4().hex[:8]
    repo_id = f"_smoketest_tick_{tag}"
    engine.run_cypher(f"CREATE (:`Zz``Tick{tag}` {{repo_id: $repo_id}})", {"repo_id": repo_id})
    try:
        assert repo_id in engine.graph_repo_ids()
    finally:
        engine.delete_repository(repo_id)
