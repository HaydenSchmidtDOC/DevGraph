"""describe_node against a live Neo4j: built-in, filesystem and docs nodes of one scanned repository."""

import textwrap
import time
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan
from devgraph.mcp.tools import describe_node

# Unique per run: the generated constraints are database-wide and shared with
# real repositories, and a concurrent run must not delete this run's repository.
_TOKEN = uuid.uuid4().hex[:8]
REPO = f"_smoketest_describe_node_{_TOKEN}"
FILE, FOLDER, RUNBOOK, ADR = (f"ZzFile{_TOKEN}", f"ZzFolder{_TOKEN}", f"ZzRunbook{_TOKEN}", f"ZzAdr{_TOKEN}")
CHILD, FOR, REPLACES = (f"IS_CHILD_OF_{_TOKEN.upper()}", f"RUNBOOK_FOR_{_TOKEN.upper()}", f"REPLACES_{_TOKEN.upper()}")
DECLARED = (FILE, FOLDER, RUNBOOK, ADR)
NEO4J = {"uri": "bolt://127.0.0.1:7687", "user": "neo4j", "password": "devgraph-local-dev"}

SCHEMA = """
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
      - label: Runbook
        key: [path]
        metadata: [{name: path}, {name: owner}]
        source:
          provider: docs
          paths: ["runbooks/*.md"]
          where: [{field: type, is: runbook}]
      - label: Adr
        key: [adr_id]
        metadata: [{name: path}, {name: adr_id}]
        source: {provider: docs, paths: ["decisions/*.md"], fields: {adr_id: id}}
    relationships:
      - {type: IS_CHILD_OF, provider: filesystem, from: [File, Folder], to: Folder}
      - {type: RUNBOOK_FOR, provider: docs, from: Runbook, to: Service, field: service}
      - {type: REPLACES, provider: docs, from: Adr, to: Adr, field: supersedes}
"""

HOSTILE = [
    {"relationship_types": ["X]->() DETACH DELETE n //"]},
    {"relationship_types": ["calls"]},
    {"neighbor_labels": ["File`) RETURN 1 //"]},
    {"relationship_types": [f"T{i}" for i in range(21)]},
    {"direction": "sideways"},
]


def _labels(text):
    for word, label in (("File", FILE), ("Folder", FOLDER), ("Runbook", RUNBOOK), ("Adr", ADR)):
        text = text.replace(f": {word}", f": {label}").replace(f"[{word}", f"[{label}").replace(f"{word}]", f"{label}]")
    for word, rel in (("IS_CHILD_OF", CHILD), ("RUNBOOK_FOR", FOR), ("REPLACES", REPLACES)):
        text = text.replace(f"type: {word}", f"type: {rel}")
    return text


def _md(root, rel, front):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{textwrap.dedent(front).strip()}\n---\n# Notes\n")


def _new_engine():
    engine = GraphEngine(**NEO4J)
    try:
        engine.verify_connectivity()
    except Exception as e:
        engine.close()
        pytest.skip(f"Neo4j not available: {e}")
    return engine


@pytest.fixture(scope="module")
def engine(tmp_path_factory, request):
    test_engine = _new_engine()

    def cleanup():
        try:
            test_engine.delete_repository(REPO)
            for label in DECLARED:
                test_engine.run_cypher(f"DROP CONSTRAINT {label.lower()}_repo_key IF EXISTS")
                test_engine.run_cypher(f"DROP INDEX {label.lower()}_repo_name IF EXISTS")
        finally:
            test_engine.close()

    request.addfinalizer(cleanup)
    test_engine.delete_repository(REPO)

    root = tmp_path_factory.mktemp("describe") / "repo"
    root.mkdir()
    (root / "devgraph.schema.yaml").write_text(textwrap.dedent(_labels(SCHEMA)))
    compose = "services:\n  api:\n    image: python:3.12\n"
    (root / "docker-compose.yml").write_text(compose)
    # The indexer reads compose files by exact name, so the second one sits in its own folder.
    (root / "deploy").mkdir()
    (root / "deploy" / "docker-compose.yml").write_text(compose)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def main():\n    return 1\n")
    (root / "src" / "worker.py").write_text("def main():\n    return 2\n")
    _md(root, "runbooks/api-outage.md", "type: runbook\nservice: api\nowner: platform-team")
    _md(root, "decisions/adr-012.md", "id: ADR-012")
    _md(root, "decisions/adr-013.md", "id: ADR-013\nsupersedes: ADR-012")
    (root / "many").mkdir()
    for i in range(12):
        (root / "many" / f"n{i:02d}.txt").write_text("x\n")

    test_engine.upsert_repository(REPO, "api", str(root))
    provision_repository_schema(test_engine, root)
    full_scan(test_engine, REPO, root)
    return test_engine


def describe(engine, **kwargs):
    kwargs.setdefault("declared_labels", DECLARED)
    return describe_node(engine, REPO, **kwargs)


def refs(result, direction, rel):
    return result[direction][rel]["results"]


def counts(engine, repo=REPO):
    nodes = engine.run_cypher("MATCH (n {repo_id: $r}) RETURN count(n) AS c", {"r": repo})[0]["c"]
    rels = engine.run_cypher("MATCH (n {repo_id: $r})-[x]->() RETURN count(x) AS c", {"r": repo})[0]["c"]
    return nodes, rels


def test_builtin_module_and_function(engine):
    result = describe(engine, name="main", label="Function", file="src/app.py")
    assert result["status"] == "found"
    assert result["node"]["file"] == "src/app.py"
    assert {"label": "Module", "name": "src/app.py"} in refs(result, "incoming", "CONTAINS")

    module = describe(engine, name="src/app.py", label="Module")
    assert module["status"] == "found"
    assert {"label": "Function", "name": "main", "file": "src/app.py"} in refs(module, "outgoing", "CONTAINS")


def test_filesystem_nodes(engine):
    result = describe(engine, name="src", label=FOLDER)
    assert result["status"] == "found"
    assert result["node"]["properties"]["path"] == "src"
    assert refs(result, "incoming", CHILD) == [
        {"label": FILE, "name": "src/app.py", "file": "src/app.py"},
        {"label": FILE, "name": "src/worker.py", "file": "src/worker.py"},
    ]
    assert refs(result, "outgoing", CHILD) == [{"label": FOLDER, "name": ".", "file": "."}]


def test_docs_path_keyed_node(engine):
    result = describe(engine, name="runbooks/api-outage.md", label=RUNBOOK)
    assert result["status"] == "found"
    assert result["node"]["properties"]["owner"] == "platform-team"
    assert refs(result, "outgoing", FOR) == [
        {"label": "Service", "name": "api", "file": "deploy/docker-compose.yml"},
        {"label": "Service", "name": "api", "file": "docker-compose.yml"},
    ]


def test_docs_id_keyed_node(engine):
    result = describe(engine, name="ADR-013")
    assert result["status"] == "found"
    assert refs(result, "outgoing", REPLACES) == [{"label": ADR, "name": "ADR-012", "file": "decisions/adr-012.md"}]

    by_path = describe(engine, name="decisions/adr-013.md", label=ADR)
    assert by_path["status"] == "found"
    assert (by_path["node"]["name"], by_path["node"]["label"]) == ("ADR-013", ADR)


def test_shared_node_shows_sources(engine):
    # A compose Service is file-scoped; the image's Container is the node both compose files share.
    service = describe(engine, name="api", label="Service", file="docker-compose.yml")
    assert service["node"]["properties"]["source"] == "docker-compose.yml"
    shared = describe(engine, name="python", label="Container")
    assert shared["status"] == "found"
    assert shared["node"]["properties"]["sources"] == ["deploy/docker-compose.yml", "docker-compose.yml"]
    assert "claims" not in shared["node"]["properties"]


def _all_refs(value):
    if isinstance(value, dict):
        if {"label", "name"} <= set(value) and set(value) <= {"label", "name", "file"}:
            yield value
        for v in value.values():
            yield from _all_refs(v)
    elif isinstance(value, list):
        for v in value:
            yield from _all_refs(v)


def test_repository_name_is_not_ambiguous(engine, tmp_path_factory):
    result = describe(engine, name="api", label="Service")
    assert result["status"] == "ambiguous"
    assert result["candidates"] == [
        {"label": "Service", "name": "api", "file": "deploy/docker-compose.yml"},
        {"label": "Service", "name": "api", "file": "docker-compose.yml"},
    ]
    anywhere = describe(engine, name="api")
    assert all(c["label"] != "Repository" for c in anywhere["candidates"])
    repo_path = engine.run_cypher("MATCH (r:Repository {repo_id: $r}) RETURN r.path AS p", {"r": REPO})[0]["p"]
    for res in (result, anywhere, describe(engine, name="src", label=FOLDER), describe(engine, name=".", label=FOLDER)):
        assert repo_path not in repr(res)
        assert all(ref["label"] != "Repository" for ref in _all_refs(res))


def test_ambiguity(engine):
    paths = describe(engine, name="src/app.py")
    assert paths["status"] == "ambiguous"
    assert {c["label"] for c in paths["candidates"]} == {"Module", FILE}
    mains = describe(engine, name="main")
    assert mains["status"] == "ambiguous"
    assert [c.get("file") for c in mains["candidates"]] == ["src/app.py", "src/worker.py"]
    for candidate in paths["candidates"] + mains["candidates"]:
        found = describe(engine, **candidate)
        assert found["status"] == "found"
        assert (found["node"]["label"], found["node"]["name"]) == (candidate["label"], candidate["name"])


def test_not_found(engine):
    with pytest.raises(ValueError) as caught:
        describe(engine, name="ap")
    assert REPO in str(caught.value)
    assert "'api'" in str(caught.value)


def test_filters(engine):
    folder = describe(engine, name="src", label=FOLDER, direction="in")
    assert folder["outgoing"] == {} and CHILD in folder["incoming"]

    file = describe(engine, name="src/app.py", label=FILE, relationship_types=[CHILD])
    assert list(file["outgoing"]) == [CHILD] and file["incoming"] == {}

    main = describe(engine, name="main", file="src/app.py", label="Function", neighbor_labels=["Module"])
    assert [r["label"] for group in main["incoming"].values() for r in group["results"]] == ["Module"]
    assert main["outgoing"] == {}

    none = describe(engine, name="src", label=FOLDER, relationship_types=["NO_SUCH_EDGE"])
    assert (none["outgoing"], none["incoming"]) == ({}, {})


def test_caps(engine):
    result = describe(engine, name="many", label=FOLDER, max_per_type=5)
    group = result["incoming"][CHILD]
    assert group["count"] == 12
    assert [r["name"] for r in group["results"]] == [f"many/n{i:02d}.txt" for i in range(5)]
    assert group["truncated"] is True


def test_many_edges_cap_and_exact_count(engine):
    engine.upsert_nodes(
        [{"label": "Function", "repo_id": REPO, "name": "hub", "properties": {"file": "gen.py"}}]
        + [{"label": "Function", "repo_id": REPO, "name": f"f{i:03d}", "properties": {"file": "gen.py"}}
           for i in range(500)]
    )
    engine.upsert_relationships(
        [{"from_label": "Function", "from_name": f"f{i:03d}", "from_file": "gen.py", "rel_type": "CALLS",
          "to_label": "Function", "to_name": "hub", "to_file": "gen.py", "repo_id": REPO} for i in range(500)]
    )
    engine.run_cypher(
        "MATCH (a:Function {repo_id: $r, name: 'f000', file: 'gen.py'}), "
        "(b:Function {repo_id: $r, name: 'hub', file: 'gen.py'}) CREATE (a)-[:CALLS]->(b)",
        {"r": REPO},
    )
    started = time.monotonic()
    result = describe(engine, name="hub", label="Function", file="gen.py", max_per_type=50)
    assert time.monotonic() - started < 10
    group = result["incoming"]["CALLS"]
    assert group["count"] == 500
    assert [r["name"] for r in group["results"]] == [f"f{i:03d}" for i in range(50)]
    assert group["truncated"] is True


def test_every_ref_round_trips(engine):
    for kwargs in ({"name": "src", "label": FOLDER}, {"name": "runbooks/api-outage.md", "label": RUNBOOK},
                   {"name": "ADR-013"}):
        result = describe(engine, **kwargs)
        for direction in ("outgoing", "incoming"):
            for group in result[direction].values():
                for ref in group["results"]:
                    again = describe(engine, **ref)
                    assert again["status"] == "found", ref
                    node = again["node"]
                    assert (node["label"], node["name"], node.get("file")) == (ref["label"], ref["name"], ref.get("file"))


def test_injection_attempt_changes_nothing(engine):
    before = counts(engine)
    for kwargs in HOSTILE:
        with pytest.raises(ValueError):
            describe(engine, name="src", **kwargs)
    with pytest.raises(ValueError, match="no node named"):
        describe(engine, name="x' OR 1=1 //")
    with pytest.raises(ValueError, match="no node named"):
        describe(engine, name="src", file="') DETACH DELETE n //")
    assert counts(engine) == before


@pytest.fixture
def other_repo(engine, request):
    other = f"{REPO}_other"
    request.addfinalizer(lambda: engine.delete_repository(other))
    engine.delete_repository(other)
    engine.upsert_nodes([
        {"label": "Service", "repo_id": other, "name": "api", "properties": {"file": "docker-compose.yml"}},
        {"label": RUNBOOK, "repo_id": other, "name": "runbooks/api-outage.md",
         "properties": {"path": "runbooks/api-outage.md"}},
    ])
    return other


def test_other_repo_is_invisible(engine, other_repo):
    result = describe(engine, name="api", label="Service")
    assert result["count"] == 2
    by_path = describe(engine, name="runbooks/api-outage.md")
    assert by_path["status"] == "ambiguous"
    assert sorted(c["label"] for c in by_path["candidates"]) == sorted([FILE, RUNBOOK])
    runbook = describe(engine, name="runbooks/api-outage.md", label=RUNBOOK)
    assert runbook["status"] == "found"
    other_ids = {
        row["id"] for row in engine.run_cypher(
            "MATCH (n {repo_id: $r}) RETURN elementId(n) AS id", {"r": other_repo}
        )
    }
    assert len(other_ids) == 2
    for res in (result, by_path, runbook, describe(engine, name="api", label="Service", file="docker-compose.yml")):
        assert other_repo not in repr(res)
    assert len(refs(runbook, "outgoing", FOR)) == 2
