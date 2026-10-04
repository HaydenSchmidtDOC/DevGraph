"""Read half of the Config page API: `GET /api/config` and `GET /api/config/{scope}`.

Neo4j-free: a stub engine supplies the applied schema; the registry is a real
temporary SQLite file; the global store is redirected to tmp.
"""

import hashlib
import json
import textwrap
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from devgraph.config import global_tools, project_switch
from devgraph.config.project_schema import SCHEMA_FILENAME, schema_file_hash
from devgraph.config.project_tools import TOOLS_FILENAME
from devgraph.dashboard import routes
from devgraph.dashboard.app import build_app
from devgraph.dashboard.events import EventBroadcaster
from devgraph.registry.store import RepoRegistry

TOOL = """
    version: 1
    tools:
      - name: {name}
        description: A tool.
        cypher: |
          MATCH (n {{repo_id: $repo_id}}) RETURN n LIMIT 1
"""


class StubEngine:
    def __init__(self) -> None:
        self.applied: dict[str, dict | None] = {}

    def read_applied_schema(self, repo_id: str):
        return self.applied.get(repo_id)


@pytest.fixture
def registry(tmp_path: Path):
    reg = RepoRegistry(tmp_path / "registry.sqlite3")
    yield reg
    reg.close()


@pytest.fixture
def engine() -> StubEngine:
    return StubEngine()


@pytest.fixture
def global_store(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "home" / "global-tools.json"
    monkeypatch.setattr(global_tools, "_default_path", lambda: path)
    return path


@pytest.fixture
def client(registry, engine, global_store):
    app = FastAPI()
    app.include_router(routes.build_router(engine, registry, EventBroadcaster()))
    return TestClient(app, base_url="http://127.0.0.1")


def _repo(tmp_path: Path, registry: RepoRegistry, name: str = "repo-a"):
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    return registry.add_repo(root)


def _write(root: Path, filename: str, text: str) -> Path:
    path = root / filename
    path.write_text(textwrap.dedent(text).lstrip("\n"))
    return path


def _global_store(path: Path, *names: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tools = [{"name": n, "description": "G.", "cypher": "MATCH (n {repo_id: $repo_id}) RETURN n LIMIT 1"} for n in names]
    path.write_text(json.dumps({"version": 1, "tools": tools}))


def _kinds(badges: list[dict]) -> list[str]:
    return [b["kind"] for b in badges]


def test_empty_model_lists_locked_builtins_first(client):
    body = client.get("/api/config").json()

    assert body["projects"] == []
    glob = body["global"]
    labels = [n["label"] for n in glob["node_types"]]
    assert "Container" in labels and "Class" in labels
    assert all(n["locked"] for n in glob["node_types"])
    assert all(r["locked"] for r in glob["relationship_types"])
    assert "CALLS" in [r["type"] for r in glob["relationship_types"]]
    tools = glob["tools"]
    assert tools["state"] == "absent" and tools["fingerprint"] == "absent" and tools["entries"] == []
    builtin = {t["name"]: t for t in tools["builtin"]}
    assert builtin["find_callers"]["tool_id"] == "find_callers"
    assert builtin["find_callers"]["locked"] is True
    assert builtin["find_callers"]["description"]
    assert "run_cypher" not in builtin  # off by default, as in /mcp-tools


def test_global_tool_entries_ids_and_fingerprint(client, global_store):
    _global_store(global_store, "hot_paths")

    tools = client.get("/api/config/__global__").json()["tools"]

    assert tools["state"] == "valid"
    assert tools["fingerprint"] == "sha256:" + hashlib.sha256(global_store.read_bytes()).hexdigest()
    [entry] = tools["entries"]
    assert entry["name"] == "hot_paths" and entry["tool_id"] == "gl_hot_paths"
    assert "name: hot_paths" in entry["yaml"] and entry["badges"] == []


def test_invalid_global_store_reports_error_and_still_lists_readable_entries(client, global_store):
    global_store.parent.mkdir(parents=True)
    global_store.write_text(json.dumps({"version": 1, "tools": [{"name": "broken", "cypher": "RETURN 1"}]}))

    tools = client.get("/api/config/__global__").json()["tools"]

    assert tools["state"] == "invalid" and tools["error"]
    assert str(global_store.parent) not in tools["error"]
    assert _kinds(tools["badges"]) == ["file-invalid"] and tools["badges"][0]["level"] == "error"
    assert [e["name"] for e in tools["entries"]] == ["broken"]


def test_project_block_tools_ids_and_effect_notes(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    _write(record.path, TOOLS_FILENAME, TOOL.format(name="find_parents"))

    block = client.get("/api/config/repo-a").json()

    assert block["repo_id"] == "repo-a" and block["project_config_enabled"] is True
    assert block["display_path"]
    assert "2 seconds" in block["effect_notes"]["tools"] and block["effect_notes"]["schema"]
    tools = block["tools"]
    assert tools["state"] == "valid" and tools["file"] == TOOLS_FILENAME
    [entry] = tools["entries"]
    assert entry["tool_id"] == "repo-a_find_parents" and entry["origin"] == "project"
    assert entry["badges"] == []


def test_project_tool_overriding_global_badges_both_sides(client, registry, tmp_path, global_store):
    record = _repo(tmp_path, registry)
    _global_store(global_store, "hot_paths", "only_global")
    _write(record.path, TOOLS_FILENAME, TOOL.format(name="hot_paths"))

    model = client.get("/api/config").json()

    [entry] = model["projects"][0]["tools"]["entries"]
    assert entry["origin"] == "project (overrides global)"
    assert _kinds(entry["badges"]) == ["overrides-global"] and entry["badges"][0]["level"] == "info"
    by_name = {e["name"]: e for e in model["global"]["tools"]["entries"]}
    assert by_name["hot_paths"]["badges"][0]["kind"] == "overridden"
    assert "repo-a" in by_name["hot_paths"]["badges"][0]["text"]
    assert by_name["only_global"]["badges"] == []


def test_shadowing_a_builtin_is_a_warning_badge_in_both_layers(client, registry, tmp_path, global_store):
    record = _repo(tmp_path, registry)
    _write(record.path, TOOLS_FILENAME, TOOL.format(name="find_callers"))
    _global_store(global_store, "list_services")

    model = client.get("/api/config").json()

    [entry] = model["projects"][0]["tools"]["entries"]
    assert _kinds(entry["badges"]) == ["locked-shadow"] and entry["badges"][0]["level"] == "warn"
    assert entry["origin"] is None
    [g] = model["global"]["tools"]["entries"]
    assert _kinds(g["badges"]) == ["locked-shadow"]


def test_invalid_project_tools_file_badge_and_fallback_to_global(client, registry, tmp_path, global_store):
    record = _repo(tmp_path, registry)
    _global_store(global_store, "hot_paths")
    _write(record.path, TOOLS_FILENAME, """
        version: 1
        tools:
          - name: hot_paths
            description: Broken, no cypher.
    """)

    block = client.get("/api/config/repo-a").json()

    assert block["tools"]["state"] == "invalid"
    assert "MCP sessions keep their last good tools" in block["tools"]["badges"][0]["detail"]
    assert str(record.path) not in block["tools"]["error"]
    [entry] = block["tools"]["entries"]
    assert entry["origin"] == "global"
    assert _kinds(entry["badges"]) == ["fallback-global"]
    assert "invalid" in entry["badges"][0]["detail"]


def test_disabled_project_config_marks_tools_not_served(client, registry, tmp_path, monkeypatch):
    record = _repo(tmp_path, registry)
    _write(record.path, TOOLS_FILENAME, TOOL.format(name="find_parents"))
    registry.set_project_config_enabled("repo-a", False)
    monkeypatch.setattr(project_switch, "_registry_db_path", lambda: tmp_path / "registry.sqlite3")

    block = client.get("/api/config/repo-a").json()

    assert block["project_config_enabled"] is False
    assert block["tools"]["state"] == "disabled"
    assert _kinds(block["tools"]["entries"][0]["badges"]) == ["not-served"]
    assert block["tools"]["entries"][0]["badges"][0]["level"] == "muted"
    assert block["schema"]["state"] == "disabled"
    assert "disabled" in block["effect_notes"]["tools"]


SCHEMA = """
    version: 1
    node_types:
      - label: Widget
        key: [slug]
        metadata:
          - name: slug
            type: string
            required: true
    relationships:
      - type: HAS_PART
        provider: custom
        custom: {name: widget_parts}
        from: Widget
        to: Widget
"""


def test_schema_states_pending_never_absent_applied_invalid(client, registry, engine, tmp_path):
    record = _repo(tmp_path, registry)
    assert client.get("/api/config/repo-a").json()["schema"]["state"] == "absent"

    schema = _write(record.path, SCHEMA_FILENAME, SCHEMA)
    block = client.get("/api/config/repo-a").json()["schema"]
    assert block["state"] == "never" and _kinds(block["badges"]) == ["schema-never"]
    assert block["file"] == SCHEMA_FILENAME and block["extends"] == "default"
    [node] = block["node_types"]
    assert node["label"] == "Widget" and node["editable"] is True and "label: Widget" in node["yaml"]
    [rel] = block["relationships"]
    assert rel["type"] == "HAS_PART" and rel["editable"] is True
    assert block["fingerprint"] == "sha256:" + hashlib.sha256(schema.read_bytes()).hexdigest()

    engine.applied["repo-a"] = {"hash": "other", "labels": [], "relationship_types": []}
    assert client.get("/api/config/repo-a").json()["schema"]["state"] == "pending"
    assert _kinds(client.get("/api/config/repo-a").json()["schema"]["badges"]) == ["schema-pending"]

    engine.applied["repo-a"] = {"hash": schema_file_hash(record.path), "labels": ["Widget"], "relationship_types": []}
    applied = client.get("/api/config/repo-a").json()["schema"]
    assert applied["state"] == "applied" and applied["badges"] == []

    schema.write_text("version: 1\nnode_types: [oops")
    broken = client.get("/api/config/repo-a").json()["schema"]
    assert broken["state"] == "invalid" and broken["badges"][0]["level"] == "error"
    assert str(record.path) not in broken["error"]


def test_relationship_declared_twice_is_not_editable(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    _write(record.path, SCHEMA_FILENAME, """
        version: 1
        node_types:
          - label: Widget
            key: [slug]
            metadata:
              - name: slug
                type: string
                required: true
        relationships:
          - type: HAS_PART
            provider: custom
            custom: {name: widget_parts}
            from: Widget
            to: Widget
          - type: HAS_PART
            provider: custom
            custom: {name: other_parts}
            from: Widget
            to: Widget
    """)

    rels = client.get("/api/config/repo-a").json()["schema"]["relationships"]

    assert [r["editable"] for r in rels] == [False, False]
    assert _kinds(rels[0]["badges"]) == ["ambiguous"]


def test_hostile_names_are_data_only(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    _write(record.path, TOOLS_FILENAME, TOOL.format(name="'<img src=x onerror=alert(1)>'"))

    # An invalid tool name fails validation, but the entry is still listed as text.
    [entry] = client.get("/api/config/repo-a").json()["tools"]["entries"]

    assert entry["name"] == "<img src=x onerror=alert(1)>"


def test_only_active_repos_are_listed_and_scope_404s(client, registry, tmp_path):
    _repo(tmp_path, registry, "repo-a")
    _repo(tmp_path, registry, "repo-b")
    registry._set_flag("repo-b", "active", False)

    assert [p["repo_id"] for p in client.get("/api/config").json()["projects"]] == ["repo-a"]
    assert client.get("/api/config/repo-b").status_code == 404
    assert client.get("/api/config/nope").status_code == 404
    assert client.get("/api/config/__all__").status_code == 404
    assert client.get("/api/config/..%2Fx").status_code == 404


def test_model_leaves_the_repo_untouched(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    tools = _write(record.path, TOOLS_FILENAME, TOOL.format(name="find_parents"))
    before = tools.read_bytes()

    client.get("/api/config")

    assert tools.read_bytes() == before
    assert sorted(p.name for p in record.path.iterdir()) == sorted([".git", TOOLS_FILENAME])


def test_host_guard_covers_config_reads(registry, engine, global_store):
    client = TestClient(build_app(engine, registry, EventBroadcaster(), dashboard_host="127.0.0.1"), base_url="http://127.0.0.1")

    assert client.get("/api/config").status_code == 200
    assert client.get("/api/config", headers={"host": "evil.test:8765"}).status_code == 403
    rebinding = {"host": "evil.test:8765", "origin": "http://evil.test:8765"}
    assert client.get("/api/config/__global__", headers=rebinding).status_code == 403
