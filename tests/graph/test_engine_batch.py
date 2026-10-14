"""Tests for GraphEngine's batched write methods (upsert_nodes,
upsert_relationships, replace_file_nodes) — the UNWIND-grouped, single-
transaction alternative to upserting one node/edge per round-trip."""

import neo4j
import pytest

from devgraph.graph.engine import GraphEngine


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


class TestUpsertNodes:
    def test_creates_nodes_with_mixed_labels(self, engine):
        repo_id = "_smoketest_batch_nodes_mixed"
        nodes = [
            {"label": "Module", "repo_id": repo_id, "name": "a.py", "properties": {"type": "module"}},
            {"label": "Class", "repo_id": repo_id, "name": "A", "properties": {"file": "a.py"}},
            {"label": "Function", "repo_id": repo_id, "name": "f", "properties": {"file": "a.py"}},
        ]
        try:
            engine.upsert_nodes(nodes)
            rows = engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) RETURN labels(n)[0] as label, n.name as name",
                {"repo_id": repo_id},
            )
            assert {(r["label"], r["name"]) for r in rows} == {
                ("Module", "a.py"),
                ("Class", "A"),
                ("Function", "f"),
            }
        finally:
            engine.delete_repository(repo_id)

    def test_rerunning_the_same_batch_does_not_duplicate(self, engine):
        repo_id = "_smoketest_batch_nodes_idempotent"
        nodes = [{"label": "Module", "repo_id": repo_id, "name": "a.py", "properties": {}}]
        try:
            engine.upsert_nodes(nodes)
            engine.upsert_nodes(nodes)
            rows = engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) RETURN COUNT(*) as c", {"repo_id": repo_id}
            )
            assert rows[0]["c"] == 1
        finally:
            engine.delete_repository(repo_id)

    def test_empty_batch_is_a_no_op(self, engine):
        engine.upsert_nodes([])  # must not raise


class TestUpsertRelationships:
    def test_merges_edges_with_mixed_triples(self, engine):
        repo_id = "_smoketest_batch_rels_mixed"
        try:
            engine.upsert_nodes(
                [
                    {"label": "Module", "repo_id": repo_id, "name": "a.py", "properties": {}},
                    {"label": "Module", "repo_id": repo_id, "name": "b.py", "properties": {}},
                    {"label": "Function", "repo_id": repo_id, "name": "f", "properties": {}},
                    {"label": "Function", "repo_id": repo_id, "name": "g", "properties": {}},
                ]
            )
            engine.upsert_relationships(
                [
                    {
                        "from_label": "Module",
                        "from_name": "a.py",
                        "rel_type": "IMPORTS",
                        "to_label": "Module",
                        "to_name": "b.py",
                        "repo_id": repo_id,
                        "properties": {},
                    },
                    {
                        "from_label": "Function",
                        "from_name": "f",
                        "rel_type": "CALLS",
                        "to_label": "Function",
                        "to_name": "g",
                        "repo_id": repo_id,
                        "properties": {"caller_class": "C"},
                    },
                ]
            )
            rows = engine.run_cypher(
                "MATCH (a {repo_id: $repo_id})-[r]->(b {repo_id: $repo_id}) "
                "RETURN type(r) as rel_type, a.name as from_name, b.name as to_name, r.caller_class as caller_class",
                {"repo_id": repo_id},
            )
            by_type = {r["rel_type"]: r for r in rows}
            assert by_type["IMPORTS"]["from_name"] == "a.py" and by_type["IMPORTS"]["to_name"] == "b.py"
            assert by_type["CALLS"]["from_name"] == "f" and by_type["CALLS"]["caller_class"] == "C"
        finally:
            engine.delete_repository(repo_id)

    def test_edge_to_nonexistent_node_is_silently_absent(self, engine):
        repo_id = "_smoketest_batch_rels_dangling"
        try:
            engine.upsert_nodes([{"label": "Module", "repo_id": repo_id, "name": "a.py", "properties": {}}])
            engine.upsert_relationships(
                [
                    {
                        "from_label": "Module",
                        "from_name": "a.py",
                        "rel_type": "IMPORTS",
                        "to_label": "Module",
                        "to_name": "never_existed.py",
                        "repo_id": repo_id,
                        "properties": {},
                    }
                ]
            )
            rows = engine.run_cypher(
                "MATCH (a:Module {repo_id: $repo_id})-[r]->() RETURN COUNT(*) as c", {"repo_id": repo_id}
            )
            assert rows[0]["c"] == 0
        finally:
            engine.delete_repository(repo_id)

    def test_empty_batch_is_a_no_op(self, engine):
        engine.upsert_relationships([])  # must not raise


class TestReplaceFileNodes:
    def test_replaces_old_nodes_with_new_ones(self, engine):
        repo_id = "_smoketest_replace_file_nodes"
        try:
            engine.upsert_node("Function", repo_id, "old_func", {"source_file": "f.py"})
            engine.upsert_node("Module", repo_id, "unrelated.py", {"source_file": "unrelated.py"})

            engine.replace_file_nodes(
                repo_id,
                "f.py",
                nodes=[
                    {"label": "Module", "repo_id": repo_id, "name": "f.py", "properties": {"source_file": "f.py"}},
                    {"label": "Function", "repo_id": repo_id, "name": "new_func", "properties": {"source_file": "f.py"}},
                ],
                rels=[],
            )

            rows = engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) RETURN n.name as name", {"repo_id": repo_id}
            )
            names = {r["name"] for r in rows}
            assert names == {"f.py", "new_func", "unrelated.py"}
        finally:
            engine.delete_repository(repo_id)

    def test_calling_twice_with_different_content_leaves_no_stale_nodes(self, engine):
        repo_id = "_smoketest_replace_file_nodes_twice"
        try:
            engine.replace_file_nodes(
                repo_id,
                "f.py",
                nodes=[
                    {"label": "Module", "repo_id": repo_id, "name": "f.py", "properties": {"source_file": "f.py"}},
                    {"label": "Function", "repo_id": repo_id, "name": "func_v1", "properties": {"source_file": "f.py"}},
                ],
                rels=[],
            )
            engine.replace_file_nodes(
                repo_id,
                "f.py",
                nodes=[
                    {"label": "Module", "repo_id": repo_id, "name": "f.py", "properties": {"source_file": "f.py"}},
                    {"label": "Function", "repo_id": repo_id, "name": "func_v2", "properties": {"source_file": "f.py"}},
                ],
                rels=[],
            )

            rows = engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) RETURN n.name as name", {"repo_id": repo_id}
            )
            names = {r["name"] for r in rows}
            assert names == {"f.py", "func_v2"}
        finally:
            engine.delete_repository(repo_id)


class TestBatchingReducesRoundTrips:
    def test_replace_file_nodes_issues_a_small_constant_number_of_tx_run_calls(self, engine, monkeypatch):
        repo_id = "_smoketest_batch_round_trips"
        call_count = {"n": 0}
        original_run = neo4j.ManagedTransaction.run

        def counting_run(self, *args, **kwargs):
            call_count["n"] += 1
            return original_run(self, *args, **kwargs)

        monkeypatch.setattr(neo4j.ManagedTransaction, "run", counting_run)

        nodes = [
            {"label": "Function", "repo_id": repo_id, "name": f"func_{i}", "properties": {"source_file": "big.py"}}
            for i in range(20)
        ]
        rels = [
            {
                "from_label": "Function",
                "from_name": f"func_{i}",
                "rel_type": "CALLS",
                "to_label": "Function",
                "to_name": f"func_{i + 1}",
                "repo_id": repo_id,
                "properties": {},
            }
            for i in range(19)
        ]

        try:
            engine.replace_file_nodes(repo_id, "big.py", nodes, rels)
            # 1 read of the Module's old name_ref_sources (none here, so no
            # foreign-source unclaim) + 1 delete + 1 unclaim (source-tracked
            # nodes) + 1 UNWIND per node label (just "Function") + 1 UNWIND
            # per relationship (from_label, rel_type, to_label) triple (just
            # one triple here) = 5 tx.run calls total, independent of the 20
            # nodes / 19 edges — not the 39+ calls the old per-item loop cost.
            assert call_count["n"] == 5
        finally:
            engine.delete_repository(repo_id)


class TestNameLookupIndexes:
    """Bare-name edge ends and describe_node match Class/Function/Service on
    (repo_id, name); their uniqueness constraint indexes (repo_id, name, file)."""

    def test_init_schema_provisions_one_per_file_scoped_label(self, engine):
        from devgraph.graph.schema import FILE_SCOPED_LABELS

        engine.init_schema()  # a second run is a no-op
        rows = engine.run_cypher(
            "SHOW INDEXES YIELD name, type, labelsOrTypes, properties "
            "WHERE name ENDS WITH '_repo_name_lookup' RETURN name, type, labelsOrTypes, properties"
        )
        assert sorted((r["labelsOrTypes"][0], r["type"], tuple(r["properties"])) for r in rows) == sorted(
            (label, "RANGE", ("repo_id", "name")) for label in FILE_SCOPED_LABELS
        )

    def test_a_bare_name_match_is_an_index_seek(self, engine):
        with engine._driver.session() as session:
            summary = session.run(
                "EXPLAIN MATCH (b:Function {repo_id: $r, name: $n}) RETURN b", r="r", n="g"
            ).consume()

        def operators(node):
            yield node["operatorType"]
            for child in node.get("children", []):
                yield from operators(child)

        ops = list(operators(summary.plan))
        assert any(op.startswith("NodeIndexSeek") for op in ops), ops
        assert not any(op.startswith("NodeByLabelScan") for op in ops), ops

    def test_a_file_scoped_end_is_hinted_to_seek_its_index(self):
        """A plan made from index statistics sampled while the label was
        near-empty estimates 0 rows and can scan a whole index per row even
        under a plain USING INDEX hint (seen in CI: a 5,000-module relink at
        20 s instead of 0.5 s), so each end of a file-scoped label is hinted
        to seek: a pinned end the (repo_id, name, file) index, a bare-name end
        the (repo_id, name) one."""
        from devgraph.graph.engine import _upsert_relationships_tx

        class Tx:
            queries = []

            def run(self, query, **params):
                self.queries.append(query)

        rel = {"repo_id": "r", "rel_type": "CALLS", "properties": {}, "origin": "a.py"}
        _upsert_relationships_tx(Tx(), [
            rel | {"from_label": "Function", "from_name": "f", "from_file": "a.py",
                   "to_label": "Function", "to_name": "g", "to_file": "b.py"},
            rel | {"from_label": "Module", "from_name": "a.py", "to_label": "Function", "to_name": "g"},
        ])
        pinned, bare = Tx.queries
        assert "USING INDEX SEEK a:Function(repo_id, name, file)" in pinned
        assert "USING INDEX SEEK b:Function(repo_id, name, file)" in pinned
        assert "USING INDEX SEEK b:Function(repo_id, name) " in bare
        assert "USING INDEX SEEK a:" not in bare


def test_the_provenance_label_families_cover_every_extractor_label():
    """A per-file re-index seeks a file's nodes label by label, so a label an
    extractor writes with `source_file` or `source` must be in its family, or
    its nodes would never be found again."""
    from devgraph.graph.schema import CLAIMED_LABELS, NAMED_LABELS, SOURCE_FILE_LABELS
    from devgraph.indexer.datastores.extractor import DatastoreType
    from devgraph.indexer.docs.extractor import DOC_NOTE_LABELS

    assert set(DOC_NOTE_LABELS) | {"Module", "Document"} <= set(SOURCE_FILE_LABELS)
    assert {t.value for t in DatastoreType} | {"Container", "Endpoint", "Function"} <= set(CLAIMED_LABELS)
    assert set(CLAIMED_LABELS) | set(SOURCE_FILE_LABELS) <= set(NAMED_LABELS)


_HINT_FAILURE = (
    "Failed to fulfil the hints of the query. Could not solve these hints: "
    "`USING INDEX SEEK n:Class(repo_id, file)`"
)


def _fail_hinted_queries(monkeypatch, times):
    """Make the next `times` hinted queries fail as they do while their
    index is still POPULATING (a missing index only warns)."""
    left = {"n": times}
    for cls in (neo4j.ManagedTransaction, neo4j.Session):
        original = cls.run

        def run(self, query, *args, _original=original, **kwargs):
            if "USING INDEX SEEK" in query and left["n"]:
                left["n"] -= 1
                raise neo4j.exceptions.ClientError(_HINT_FAILURE)
            return _original(self, query, *args, **kwargs)

        monkeypatch.setattr(cls, "run", run)
    return left


class TestIndexesStillBuilding:
    def test_a_write_waits_for_the_index_and_is_not_dropped(self, engine, monkeypatch):
        import devgraph.graph.engine as engine_module

        repo_id = "_smoketest_index_building_write"
        monkeypatch.setattr(engine_module.time, "sleep", lambda _s: None)
        left = _fail_hinted_queries(monkeypatch, 2)
        try:
            engine.replace_file_nodes(repo_id, "a.py", [
                {"label": "Function", "repo_id": repo_id, "name": "f", "properties": {"file": "a.py"}},
            ], [])
            assert left["n"] == 0
            assert engine.list_file_nodes(repo_id, ["a.py"]) == {("Function", "f", "a.py")}
        finally:
            monkeypatch.undo()
            engine.delete_repository(repo_id)

    def test_an_index_that_stays_unbuilt_fails_the_call(self, engine, monkeypatch):
        import devgraph.graph.engine as engine_module

        clock = {"t": 0.0}
        monkeypatch.setattr(engine_module.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
        monkeypatch.setattr(engine_module.time, "monotonic", lambda: clock["t"])
        _fail_hinted_queries(monkeypatch, 10**6)
        with pytest.raises(engine_module.IndexesNotReady):
            engine.list_file_nodes("_smoketest_index_building_read", ["a.py"])
        assert clock["t"] >= engine_module.INDEX_WAIT_S

    def test_init_schema_waits_for_the_indexes_it_creates(self, engine, monkeypatch):
        """An upgraded database builds the new indexes in the background;
        init_schema returns only once they are ONLINE."""
        queries = []
        original = neo4j.Session.run

        def run(self, query, *args, **kwargs):
            queries.append(query)
            return original(self, query, *args, **kwargs)

        engine.run_cypher("DROP INDEX class_repo_file_lookup IF EXISTS")
        monkeypatch.setattr(neo4j.Session, "run", run)
        engine.init_schema()
        monkeypatch.undo()
        assert any("db.awaitIndexes" in q for q in queries), queries[-3:]
        rows = engine.run_cypher("SHOW INDEXES YIELD name, state WHERE name = 'class_repo_file_lookup' RETURN state")
        assert [r["state"] for r in rows] == ["ONLINE"]


def test_init_schema_without_wait_does_not_await_the_indexes(engine, monkeypatch):
    queries = []
    original = neo4j.Session.run

    def run(self, query, *args, **kwargs):
        queries.append(query)
        return original(self, query, *args, **kwargs)

    monkeypatch.setattr(neo4j.Session, "run", run)
    engine.init_schema(wait=False)
    monkeypatch.undo()
    assert queries and not any("db.awaitIndexes" in q for q in queries)
