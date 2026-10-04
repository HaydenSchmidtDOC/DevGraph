"""Tests for the dashboard's /api/* routes via FastAPI's TestClient.

Reuses the seeded-graph fixture pattern from tests/mcp/test_tools.py: a real
GraphEngine against the local test Neo4j (bolt://127.0.0.1:7687), seeded
with two repos to also verify repo_id scoping holds through this second
entry point into the same engine.
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from devgraph.dashboard.app import build_app
from devgraph.dashboard.events import EventBroadcaster
from devgraph.graph.engine import GraphEngine
from devgraph.registry.store import RepoRegistry


@pytest.fixture
def engine():
    test_engine = GraphEngine(
        uri="bolt://127.0.0.1:7687",
        user="neo4j",
        password="devgraph-local-dev",
    )
    test_engine.verify_connectivity()
    test_engine.init_schema()
    yield test_engine
    test_engine.close()


@pytest.fixture
def seeded_graph(engine):
    engine.upsert_repository("dash_repo_a", "Dash Repo A", "/path/to/repo_a")
    engine.upsert_node("Service", "dash_repo_a", "UserService")
    engine.upsert_node("Service", "dash_repo_a", "AuthService")
    engine.upsert_node("Module", "dash_repo_a", "auth.py")
    engine.upsert_node("Class", "dash_repo_a", "AuthHandler")
    engine.upsert_relationship("Service", "UserService", "CALLS", "Service", "AuthService", "dash_repo_a")
    engine.upsert_relationship("Module", "auth.py", "CONTAINS", "Class", "AuthHandler", "dash_repo_a")

    engine.upsert_repository("dash_repo_b", "Dash Repo B", "/path/to/repo_b")
    engine.upsert_node("Service", "dash_repo_b", "NotificationService")

    yield engine

    engine.delete_repository("dash_repo_a")
    engine.delete_repository("dash_repo_b")


@pytest.fixture
def registry():
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = RepoRegistry(Path(tmpdir) / "registry.sqlite3")
        yield reg
        reg.close()


def _init_git_repo(path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)


@pytest.fixture
def client(seeded_graph, registry):
    # add_repo requires a real (empty is fine) git repo on disk and
    # slugifies its name into a repo_id -- register two throwaway repos
    # under explicit repo_ids matching the fixture graph's seeded data
    # ("dash_repo_a"/"dash_repo_b") rather than letting it derive one from
    # a temp directory name.
    with tempfile.TemporaryDirectory() as dir_a, tempfile.TemporaryDirectory() as dir_b:
        _init_git_repo(Path(dir_a))
        _init_git_repo(Path(dir_b))
        record_a = registry.add_repo(dir_a, repo_id="dash_repo_a")
        record_b = registry.add_repo(dir_b, repo_id="dash_repo_b")
        assert record_a.repo_id == "dash_repo_a"
        assert record_b.repo_id == "dash_repo_b"

        events = EventBroadcaster()
        app = build_app(seeded_graph, registry, events)
        yield TestClient(app)


def test_list_repos(client):
    res = client.get("/api/repos")
    assert res.status_code == 200
    body = res.json()
    assert "repos" in body
    assert "issues" in body
    repo_ids = {r["repo_id"] for r in body["repos"]}
    assert {"dash_repo_a", "dash_repo_b"} <= repo_ids
    repo_a = next(r for r in body["repos"] if r["repo_id"] == "dash_repo_a")
    assert repo_a["node_count"] >= 4  # 2 services + 1 module + 1 class


def test_summary_counts_known_seeded_data(client):
    res = client.get("/api/repos/dash_repo_a/summary")
    assert res.status_code == 200
    body = res.json()
    assert body["nodes_by_label"]["Service"] == 2
    assert body["nodes_by_label"]["Module"] == 1
    assert body["nodes_by_label"]["Class"] == 1
    assert body["relationships_by_type"]["CALLS"] == 1
    assert body["relationships_by_type"]["CONTAINS"] == 1


def test_summary_does_not_leak_cross_repo(client):
    res = client.get("/api/repos/dash_repo_a/summary")
    body = res.json()
    assert "NotificationService" not in body["nodes_by_label"]
    assert body["nodes_by_label"].get("Service") == 2  # not 3


def test_graph_endpoint_shape(client):
    res = client.get("/api/repos/dash_repo_a/graph")
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == {"nodes", "edges"}
    assert len(body["nodes"]) >= 4
    node = body["nodes"][0]
    assert set(node["data"].keys()) == {"id", "label", "name", "key"}
    edge = body["edges"][0]
    assert set(edge["data"].keys()) == {"id", "source", "target", "type"}


def test_graph_endpoint_label_filter(client):
    res = client.get("/api/repos/dash_repo_a/graph", params={"label": "Service"})
    assert res.status_code == 200
    body = res.json()
    assert all(n["data"]["label"] == "Service" for n in body["nodes"])


def test_graph_endpoint_unknown_label_rejected(client):
    res = client.get("/api/repos/dash_repo_a/graph", params={"label": "NotARealLabel"})
    assert res.status_code == 400


def test_graph_endpoint_limit_capping(client):
    res = client.get("/api/repos/dash_repo_a/graph", params={"limit": 1})
    assert res.status_code == 200
    assert len(res.json()["nodes"]) <= 1


def test_search_endpoint(client):
    res = client.get("/api/repos/dash_repo_a/search", params={"q": "Auth"})
    assert res.status_code == 200
    names = [r["name"] for r in res.json()["results"]]
    assert "AuthService" in names
    assert "NotificationService" not in names


def test_unknown_repo_id_404s(client):
    for path in (
        "/api/repos/does-not-exist/summary",
        "/api/repos/does-not-exist/graph",
        "/api/repos/does-not-exist/search?q=x",
    ):
        res = client.get(path)
        assert res.status_code == 404


def test_cypher_endpoint_returns_neo4j_http_shaped_graph(client):
    res = client.post(
        "/api/cypher",
        json={"query": "MATCH (n:Service {repo_id: 'dash_repo_a'}) RETURN n ORDER BY n.name"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["errors"] == []
    data = body["results"][0]["data"]
    assert len(data) == 2
    names = {n["properties"]["name"] for row in data for n in row["graph"]["nodes"]}
    assert names == {"AuthService", "UserService"}


def test_cypher_graph_nodes_carry_the_identity_key(client, seeded_graph):
    """The canvas keys its Cytoscape elements on this, not on the internal
    `id` beside it, so it has to survive the round trip. Also the only place
    the file-scoped and non-file-scoped forms are exercised over real HTTP
    against a real node rather than as a unit call."""
    seeded_graph.upsert_node(
        "Function", "dash_repo_a", "handler", {"file": "svc/api.py"}
    )
    res = client.post(
        "/api/cypher",
        json={"query": "MATCH (n {repo_id: 'dash_repo_a'}) WHERE n:Service OR n:Function RETURN n"},
    )
    keys = {n["key"] for row in res.json()["results"][0]["data"] for n in row["graph"]["nodes"]}
    assert "Service\x1fdash_repo_a\x1fUserService" in keys
    assert "Function\x1fdash_repo_a\x1fhandler\x1fsvc/api.py" in keys


def test_cypher_graph_key_is_absent_for_an_unkeyable_node(client, seeded_graph):
    """A node with no `name` cannot be keyed. It must come back as null
    rather than as a key built from an empty string, which would collide
    with every other nameless node of the same label."""
    seeded_graph.run_cypher("CREATE (:Scratch {repo_id: 'dash_repo_a'})")
    try:
        res = client.post(
            "/api/cypher", json={"query": "MATCH (n:Scratch) RETURN n"}
        )
        nodes = [n for row in res.json()["results"][0]["data"] for n in row["graph"]["nodes"]]
        assert nodes and all(n["key"] is None for n in nodes)
    finally:
        seeded_graph.run_cypher("MATCH (n:Scratch) DETACH DELETE n")


def test_cypher_endpoint_reports_errors_without_raising(client):
    res = client.post("/api/cypher", json={"query": "NOT VALID CYPHER"})
    assert res.status_code == 200
    body = res.json()
    assert body["results"] == []
    assert body["errors"][0]["message"]


def test_cypher_endpoint_rejects_empty_query(client):
    res = client.post("/api/cypher", json={"query": "  "})
    assert res.status_code == 400


def test_cypher_endpoint_only_logs_when_record_true(client):
    client.post("/api/cypher", json={"query": "RETURN 1", "record": False})
    assert client.get("/api/query-log").json()["entries"] == []

    client.post("/api/cypher", json={"query": "RETURN 1", "repo_id": "dash_repo_a", "record": True})
    entries = client.get("/api/query-log").json()["entries"]
    assert len(entries) == 1
    assert entries[0]["query"] == "RETURN 1"
    assert entries[0]["repo_id"] == "dash_repo_a"
    assert entries[0]["ok"] is True


def test_query_rate_endpoint_shape(client):
    client.post("/api/cypher", json={"query": "RETURN 1", "record": True})
    res = client.get("/api/query-rate", params={"span": 3600, "interval": 60})
    assert res.status_code == 200
    buckets = res.json()["buckets"]
    assert len(buckets) >= 2
    assert sum(b["count"] for b in buckets) == 1


def test_mcp_tools_endpoint_has_real_descriptions(client):
    res = client.get("/api/mcp-tools")
    assert res.status_code == 200
    tools = {t["name"]: t for t in res.json()}
    assert "search_component" in tools
    assert tools["search_component"]["description"]
    # enable_run_cypher defaults to False -- the escape-hatch tool shouldn't
    # be advertised unless it's actually registered on the MCP server.
    assert "run_cypher" not in tools


def test_git_log_endpoint_on_empty_repo_returns_empty_list(client):
    # The client fixture's registered repos are `git init`-only, no commits.
    res = client.get("/api/repos/dash_repo_a/git-log")
    assert res.status_code == 200
    assert res.json() == []


def test_git_status_endpoint_shape(client):
    res = client.get("/api/repos/dash_repo_a/git-status")
    assert res.status_code == 200
    body = res.json()
    assert "branch" in body
    assert body["uncommitted"] == []  # nothing was ever added/committed


def test_git_endpoints_unknown_repo_404s(client):
    assert client.get("/api/repos/does-not-exist/git-log").status_code == 404
    assert client.get("/api/repos/does-not-exist/git-status").status_code == 404


@pytest.fixture
def fake_layout_settings(tmp_path, monkeypatch):
    from devgraph.config.settings import Settings
    from devgraph.dashboard import layout_store

    settings = Settings(registry_db_path=tmp_path / "registry.sqlite3")
    monkeypatch.setattr(layout_store, "get_settings", lambda: settings)
    return settings


def test_layout_endpoint_empty_when_unsaved(client, fake_layout_settings):
    res = client.get("/api/repos/dash_repo_a/layout")
    assert res.status_code == 200
    assert res.json() == {}


def test_layout_round_trips_through_put_and_get(client, fake_layout_settings):
    positions = {"Service\x1fdash_repo_a\x1fUserService": [10, 20]}
    put_res = client.put("/api/repos/dash_repo_a/layout", json=positions)
    assert put_res.status_code == 200

    get_res = client.get("/api/repos/dash_repo_a/layout")
    assert get_res.status_code == 200
    assert get_res.json() == positions


def test_layout_endpoints_unknown_repo_404s(client, fake_layout_settings):
    assert client.get("/api/repos/does-not-exist/layout").status_code == 404
    assert client.put("/api/repos/does-not-exist/layout", json={}).status_code == 404


def test_all_repos_layout_scope_is_accepted(client, fake_layout_settings):
    """The canvas's "All Repos" view is not a registered repo but still has a
    layout worth persisting, so its reserved id bypasses the registry check
    that every other id goes through."""
    positions = {"Service\x1fdash_repo_a\x1fUserService": [3, 4]}
    assert client.put("/api/repos/__all__/layout", json=positions).status_code == 200
    assert client.get("/api/repos/__all__/layout").json() == positions


def test_all_repos_layout_is_separate_from_a_real_repos(client, fake_layout_settings):
    client.put("/api/repos/__all__/layout", json={"k": [1, 1]})
    client.put("/api/repos/dash_repo_a/layout", json={"k": [2, 2]})
    assert client.get("/api/repos/__all__/layout").json() == {"k": [1, 1]}
    assert client.get("/api/repos/dash_repo_a/layout").json() == {"k": [2, 2]}


def test_layout_put_rejects_oversized_payload(client, fake_layout_settings):
    huge = {f"key-{i}": [0, 0] for i in range(200_000)}
    res = client.put("/api/repos/dash_repo_a/layout", json=huge)
    assert res.status_code == 413


def test_settings_endpoint_never_exposes_password(client):
    res = client.get("/api/settings")
    assert res.status_code == 200
    body = res.json()
    assert "neo4j_password" not in body
    assert body["neo4j_uri"] == "bolt://127.0.0.1:7687"


# --- GET /api/repos/{repo_id}/schema ---------------------------------------

_FS_SCHEMA = """\
version: 1
node_types:
  - label: File
    key: [path]
    metadata: [{name: path}]
    source: {provider: filesystem, kind: file}
    color: "#112233"
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
def schema_repo(engine, registry, tmp_path):
    """A registered, scanned repo whose schema file declares File/Folder/IS_CHILD_OF."""
    from devgraph.indexer.dispatch import full_scan

    root = tmp_path / "schema_repo"
    root.mkdir()
    _init_git_repo(root)
    (root / "a.txt").write_text("x")
    (root / "devgraph.schema.yaml").write_text(_FS_SCHEMA)
    record = registry.add_repo(str(root), repo_id="dash_schema")
    try:
        engine.upsert_repository("dash_schema", "Dash Schema", str(root))
        full_scan(engine, "dash_schema", root)
        client = TestClient(build_app(engine, registry, EventBroadcaster()))
        yield client, root, record
    finally:
        engine.delete_repository("dash_schema")


def test_schema_unknown_repo_404s(client):
    assert client.get("/api/repos/nope/schema").status_code == 404


def test_schema_without_a_file_is_the_builtins(client):
    from devgraph.graph.schema import NODE_LABELS, RELATIONSHIP_TYPES

    body = client.get("/api/repos/dash_repo_a/schema").json()
    assert [t["label"] for t in body["node_types"]] == list(NODE_LABELS)
    assert all(t["origin"] == "builtin" and t["color"] is None for t in body["node_types"])
    assert [t["type"] for t in body["relationship_types"]] == list(RELATIONSHIP_TYPES)
    assert all(t["origin"] == "builtin" and t["color"] is None for t in body["relationship_types"])
    assert body["schema_state"] == "absent"
    assert body["notices"] == []
    counts = {t["label"]: t["count"] for t in body["node_types"]}
    assert counts["Service"] == 2 and counts["Module"] == 1


def test_schema_applied_lists_project_types_with_colours_and_counts(schema_repo):
    client, _, _ = schema_repo
    body = client.get("/api/repos/dash_schema/schema").json()
    assert body["schema_state"] == "applied" and body["notices"] == []
    project = {t["label"]: t for t in body["node_types"] if t["origin"] == "project"}
    assert set(project) == {"File", "Folder"}
    assert project["File"]["color"] == "#112233"
    assert project["File"]["count"] == 2  # a.txt and the schema file itself
    assert project["Folder"]["count"] >= 1
    import re
    assert re.fullmatch(r"#[0-9a-f]{6}", project["Folder"]["color"])
    rels = {t["type"]: t for t in body["relationship_types"] if t["origin"] == "project"}
    assert set(rels) == {"IS_CHILD_OF"} and rels["IS_CHILD_OF"]["color"]
    # Deterministic: asking again gives the same colours.
    again = client.get("/api/repos/dash_schema/schema").json()
    assert again == body


def test_schema_changed_after_apply_is_pending_and_hides_new_labels(schema_repo):
    client, root, _ = schema_repo
    (root / "devgraph.schema.yaml").write_text(
        _FS_SCHEMA.replace(
            "relationships:",
            "  - label: Widget\n    key: [sku]\n    metadata: [{name: sku}]\nrelationships:",
        )
    )
    body = client.get("/api/repos/dash_schema/schema").json()
    assert body["schema_state"] == "pending"
    labels = {t["label"] for t in body["node_types"]}
    assert "Widget" not in labels and {"File", "Folder"} <= labels
    assert body["notices"]


def test_schema_invalid_file_reports_invalid_with_notice(schema_repo):
    client, root, _ = schema_repo
    (root / "devgraph.schema.yaml").write_text("version: [unclosed")
    body = client.get("/api/repos/dash_schema/schema").json()
    assert body["schema_state"] == "invalid"
    assert body["notices"] and "invalid" in body["notices"][0].lower()
    assert {"File", "Folder"} <= {t["label"] for t in body["node_types"]}


def test_schema_disabled_repo_shows_builtins_only(schema_repo, registry):
    client, _, _ = schema_repo
    registry.set_project_config_enabled("dash_schema", False)
    body = client.get("/api/repos/dash_schema/schema").json()
    assert body["schema_state"] == "disabled"
    assert all(t["origin"] == "builtin" for t in body["node_types"])
    assert all(t["origin"] == "builtin" for t in body["relationship_types"])


def test_schema_never_applied_file_shows_builtins(engine, registry, tmp_path):
    root = tmp_path / "never"
    root.mkdir()
    _init_git_repo(root)
    (root / "devgraph.schema.yaml").write_text(_FS_SCHEMA)
    registry.add_repo(str(root), repo_id="dash_never")
    try:
        engine.upsert_repository("dash_never", "Dash Never", str(root))
        c = TestClient(build_app(engine, registry, EventBroadcaster()))
        body = c.get("/api/repos/dash_never/schema").json()
        assert body["schema_state"] == "never"
        assert all(t["origin"] == "builtin" for t in body["node_types"])
    finally:
        engine.delete_repository("dash_never")


def test_schema_all_repos_is_the_union(schema_repo):
    client, _, _ = schema_repo
    body = client.get("/api/repos/__all__/schema").json()
    project = {t["label"] for t in body["node_types"] if t["origin"] == "project"}
    assert project == {"File", "Folder"}
    assert {t["type"] for t in body["relationship_types"] if t["origin"] == "project"} == {"IS_CHILD_OF"}
    assert body["schema_state"] == "applied"
