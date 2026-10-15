"""A full scan's provider queries and the dashboard's whole-repository counts
start from labels, so Neo4j never plans a scan of every node in the database,
and they answer exactly as the label-less queries they replaced did, nodes of
a label no schema records included."""

import textwrap
import uuid

import pytest
from neo4j import GraphDatabase, ManagedTransaction, Session

from devgraph.dashboard import queries
from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer import dispatch
from devgraph.indexer.dispatch import full_scan

URI, AUTH = "bolt://127.0.0.1:7687", ("neo4j", "devgraph-local-dev")
# Unique per run: the schema's generated constraints are database-wide.
_TOKEN = uuid.uuid4().hex[:8]
FILE, NOTE = f"ZzPlanFile{_TOKEN}", f"ZzPlanNote{_TOKEN}"
# A label nothing records: a provider node left by an apply a crash cut short.
STRAY = f"ZzPlanStray{_TOKEN}"
SCHEMA = f"""
    version: 1
    node_types:
      - label: {FILE}
        key: [path]
        metadata: [{{name: path}}]
        source: {{provider: filesystem, kind: file}}
      - label: {NOTE}
        key: [path]
        metadata: [{{name: path}}, {{name: status}}]
        source: {{provider: docs, paths: ["notes/*.md"]}}
"""
# Schema statements have no plan to inspect.
_UNPLANNED = ("CREATE CONSTRAINT", "CREATE INDEX", "DROP ", "SHOW ")


@pytest.fixture(scope="module", autouse=True)
def _drop_generated_constraints():
    yield
    cleanup = GraphEngine(uri=URI, user=AUTH[0], password=AUTH[1])
    try:
        for label in (FILE, NOTE, STRAY):
            cleanup.run_cypher(f"DROP CONSTRAINT {label.lower()}_repo_key IF EXISTS")
            cleanup.run_cypher(f"DROP INDEX {label.lower()}_repo_name IF EXISTS")
    except Exception:
        pass  # Neo4j unavailable: the tests were skipped
    finally:
        cleanup.close()


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri=URI, user=AUTH[0], password=AUTH[1])
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


@pytest.fixture
def repo_id(engine):
    repo = f"zz-scan-plans-{uuid.uuid4().hex[:8]}"
    yield repo
    engine.delete_repository(repo)


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


def scan(engine, repo_id, root):
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)


def make_repo(root, schema):
    write(root, "app.py", "def helper():\n    return 1\n\ndef main():\n    return helper()\n")
    write(root, "notes/a.md", "---\nstatus: open\n---\n# A\n")
    write(root, "notes/b.md", "---\nstatus: done\n---\n# B\n")
    if schema:
        write(root, "devgraph.schema.yaml", SCHEMA)


def _operators(plan):
    yield plan["operatorType"].split("@")[0]
    for child in plan.get("children", []):
        yield from _operators(child)


def whole_database_scans(queries_run):
    """(operator, query) for every recorded query whose plan scans every node."""
    found = []
    with GraphDatabase.driver(URI, auth=AUTH) as driver, driver.session() as session:
        for query, parameters in queries_run:
            if query.lstrip().upper().startswith(_UNPLANNED):
                continue
            plan = session.run("EXPLAIN " + query, parameters).consume().plan
            found += [(op, " ".join(query.split())[:160]) for op in _operators(plan) if op == "AllNodesScan"]
    return found


@pytest.mark.parametrize("schema", [False, True], ids=["no schema", "provider schema"])
def test_full_scan_never_scans_every_node(engine, repo_id, tmp_path, monkeypatch, schema):
    make_repo(tmp_path, schema)
    recorded = []

    def recording(run):
        def record(self, query, parameters=None, *args, **kwargs):
            recorded.append((str(query), dict(parameters or {}, **kwargs)))
            return run(self, query, parameters, *args, **kwargs)
        return record

    monkeypatch.setattr(Session, "run", recording(Session.run))
    monkeypatch.setattr(ManagedTransaction, "run", recording(ManagedTransaction.run))
    scan(engine, repo_id, tmp_path)
    monkeypatch.undo()

    assert recorded
    if schema:  # the providers really ran
        assert engine.run_cypher(
            f"MATCH (n:{NOTE} {{repo_id: $r}}) RETURN count(n) AS n", {"r": repo_id}
        )[0]["n"] == 2
    assert whole_database_scans(recorded) == []


# The label-less forms the dashboard queries replaced: the answers must not change.
_OLD_COUNT = "MATCH (n {repo_id: $repo_id}) RETURN count(n) AS count"
_OLD_NODES = "MATCH (n {repo_id: $repo_id}) UNWIND labels(n) AS label RETURN label, count(*) AS count"
_OLD_RELS = "MATCH (a {repo_id: $repo_id})-[r]->(b {repo_id: $repo_id}) RETURN type(r) AS type, count(*) AS count"


def test_dashboard_counts_start_from_labels_and_match_the_old_answers(engine, repo_id, tmp_path):
    make_repo(tmp_path, schema=True)
    scan(engine, repo_id, tmp_path)
    leave_stray(engine, repo_id)  # counted too, though no record names its label
    params = {"repo_id": repo_id}

    old_nodes = {r["label"]: r["count"] for r in engine.run_cypher(_OLD_NODES, params)}
    old_rels = {r["type"]: r["count"] for r in engine.run_cypher(_OLD_RELS, params)}
    assert FILE in old_nodes and NOTE in old_nodes and STRAY in old_nodes and "Function" in old_nodes and old_rels
    assert queries.count_nodes(engine, repo_id) == engine.run_cypher(_OLD_COUNT, params)[0]["count"]
    assert queries.summary_counts(engine, repo_id) == {"nodes_by_label": old_nodes, "relationships_by_type": old_rels}
    assert queries.node_counts_by_label(engine, [repo_id]) == old_nodes
    nodes, edges = queries.graph_slice(engine, repo_id, None, 10_000)
    assert len(nodes) == sum(1 for _ in engine.run_cypher("MATCH (n {repo_id: $repo_id}) RETURN n", params))
    assert len(edges) == sum(old_rels.values())

    union = engine.repository_nodes([repo_id])
    plans = [(union + "RETURN count(n)", {"repo_ids": [repo_id]})]
    plans.append((union + "MATCH (n)-[r]->(b) WHERE b.repo_id = $repo_id RETURN count(r)",
                  {"repo_ids": [repo_id], "repo_id": repo_id}))
    assert whole_database_scans(plans) == []


def strays(engine, repo_id):
    return engine.run_cypher(f"MATCH (n:{STRAY} {{repo_id: $r}}) RETURN count(n) AS n", {"r": repo_id})[0]["n"]


def leave_stray(engine, repo_id):
    engine.run_cypher(
        f"CREATE (:{STRAY} {{repo_id: $r, name: 'notes/x.md', path: 'notes/x.md', extractor: 'docs'}})",
        {"r": repo_id},
    )


def test_a_provider_node_of_a_label_no_record_names_is_pruned(engine, repo_id):
    engine.upsert_repository(repo_id, repo_id, "/nowhere")
    leave_stray(engine, repo_id)
    assert engine.read_applied_schema(repo_id) is None
    assert engine.prune_extracted_nodes(repo_id, "docs", []) == 1
    assert strays(engine, repo_id) == 0


@pytest.mark.parametrize("schema", [False, True], ids=["no schema", "provider schema"])
def test_a_full_scan_prunes_a_provider_node_of_a_label_no_record_names(engine, repo_id, tmp_path, schema):
    make_repo(tmp_path, schema)
    scan(engine, repo_id, tmp_path)
    leave_stray(engine, repo_id)
    full_scan(engine, repo_id, tmp_path)
    assert strays(engine, repo_id) == 0


def test_an_apply_cut_short_records_its_labels_first(engine, repo_id, tmp_path, monkeypatch):
    """A crash between writing a new label's nodes and recording the schema
    used to leave them under a label no record named; once the schema
    dropped it, nothing deleted them."""
    make_repo(tmp_path, schema=True)
    scan(engine, repo_id, tmp_path)
    write(tmp_path, "devgraph.schema.yaml", SCHEMA.replace(NOTE, STRAY))

    def crash(*args, **kwargs):
        raise RuntimeError("killed")

    monkeypatch.setattr(engine, "record_applied_schema", crash)
    with pytest.raises(RuntimeError):
        dispatch.apply_project_schema(engine, repo_id, tmp_path)
    monkeypatch.undo()
    assert strays(engine, repo_id) == 2
    assert STRAY in engine.run_cypher(
        "MATCH (r:Repository {repo_id: $r}) RETURN r.schema_labels AS l", {"r": repo_id}
    )[0]["l"]

    write(tmp_path, "devgraph.schema.yaml", SCHEMA)
    full_scan(engine, repo_id, tmp_path)
    assert strays(engine, repo_id) == 0
