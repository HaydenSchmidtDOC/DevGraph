"""The Config page API: reads (`GET /api/config[/{scope}]`) and entry writes (POST/PUT/DELETE).

Neo4j-free: a stub engine supplies the applied schema; the registry is a real
temporary SQLite file; the global store is redirected to tmp.
"""

import hashlib
import os
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


# --- writes --------------------------------------------------------------------------------------

NEW_TOOL = "name: find_parents\ndescription: Parents.\ncypher: |\n  MATCH (n {repo_id: $repo_id}) RETURN n LIMIT 1\n"
WIDGET = "label: Gadget\nkey: [slug]\nmetadata:\n  - name: slug\n    type: string\n    required: true\n"


def _tool_yaml(name: str, description: str = "A tool.") -> str:
    return NEW_TOOL.replace("find_parents", name).replace("Parents.", description)


def _fp(client, scope: str, part: str = "tools") -> str:
    block = client.get(f"/api/config/{scope}").json()
    return (block["tools"] if scope == "__global__" else block[part])["fingerprint"]


def _send(client, method: str, url: str, fingerprint: str | None, body=None, headers=None, **kw):
    all_headers = {**({"if-match": f'"{fingerprint}"'} if fingerprint is not None else {}), **(headers or {})}
    if body is not None and not isinstance(body, (str, bytes)):
        kw["json"] = body
    elif body is not None:
        kw["content"] = body
        all_headers.setdefault("content-type", "application/json")
    return client.request(method, url, headers=all_headers, **kw)


def _snapshot(base: Path) -> dict[str, bytes]:
    """Every file under `base` except the registry database, by relative path."""
    return {
        str(p.relative_to(base)): p.read_bytes()
        for p in sorted(base.rglob("*"))
        if p.is_file() and not p.is_symlink() and "registry.sqlite3" not in p.name
    }


def test_add_project_tool_writes_the_file_and_returns_the_refreshed_scope(client, registry, tmp_path):
    record = _repo(tmp_path, registry)

    response = _send(client, "POST", "/api/config/repo-a/tools", "absent", {"yaml": NEW_TOOL})

    assert response.status_code == 201
    body = response.json()
    assert body["ok"] is True and body["written"] is True and body["file"] == TOOLS_FILENAME
    assert body["warnings"] == []
    assert any("2 seconds" in n for n in body["notes"])
    assert any("not committed" in n for n in body["notes"])
    text = (record.path / TOOLS_FILENAME).read_text()
    assert "name: find_parents" in text
    tools = body["scope"]["tools"]
    assert [e["name"] for e in tools["entries"]] == ["find_parents"]
    assert tools["fingerprint"] == "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def test_replace_rename_and_delete_project_tool(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    path = _write(record.path, TOOLS_FILENAME, "# keep\n" + textwrap.dedent(TOOL.format(name="find_parents")).lstrip("\n"))

    put = _send(client, "PUT", "/api/config/repo-a/tools/find_parents", _fp(client, "repo-a"),
                {"yaml": _tool_yaml("find_parents", "Edited.")})
    assert put.status_code == 200 and put.json()["written"] is True
    assert "description: Edited." in path.read_text() and path.read_text().startswith("# keep\n")

    renamed = _send(client, "PUT", "/api/config/repo-a/tools/find_parents", _fp(client, "repo-a"),
                    {"yaml": _tool_yaml("find_kids")})
    assert renamed.status_code == 200
    assert [e["name"] for e in renamed.json()["scope"]["tools"]["entries"]] == ["find_kids"]

    deleted = _send(client, "DELETE", "/api/config/repo-a/tools/find_kids", _fp(client, "repo-a"))
    assert deleted.status_code == 200 and deleted.json()["scope"]["tools"]["entries"] == []
    assert "find_kids" not in path.read_text()


def test_global_store_add_replace_delete(client, global_store):
    added = _send(client, "POST", "/api/config/__global__/tools", "absent", {"yaml": _tool_yaml("hot_paths")})
    assert added.status_code == 201 and added.json()["file"] == "global-tools.json"
    assert [t["name"] for t in json.loads(global_store.read_text())["tools"]] == ["hot_paths"]
    assert added.json()["scope"]["tools"]["entries"][0]["tool_id"] == "gl_hot_paths"

    put = _send(client, "PUT", "/api/config/__global__/tools/hot_paths", _fp(client, "__global__"),
                {"yaml": _tool_yaml("hot_paths", "Edited.")})
    assert put.status_code == 200
    assert json.loads(global_store.read_text())["tools"][0]["description"] == "Edited."

    deleted = _send(client, "DELETE", "/api/config/__global__/tools/hot_paths", _fp(client, "__global__"))
    assert deleted.status_code == 200 and json.loads(global_store.read_text())["tools"] == []


def test_saving_a_global_tool_to_a_project_writes_a_project_override(client, registry, tmp_path, global_store):
    record = _repo(tmp_path, registry)
    _global_store(global_store, "hot_paths")
    global_before = global_store.read_bytes()
    entry_yaml = client.get("/api/config/__global__").json()["tools"]["entries"][0]["yaml"]

    response = _send(client, "POST", "/api/config/repo-a/tools", _fp(client, "repo-a"), {"yaml": entry_yaml})

    assert response.status_code == 201
    assert "name: hot_paths" in (record.path / TOOLS_FILENAME).read_text()
    assert global_store.read_bytes() == global_before
    [entry] = response.json()["scope"]["tools"]["entries"]
    assert entry["origin"] == "project (overrides global)"


def test_dry_run_writes_nothing(client, registry, tmp_path):
    record = _repo(tmp_path, registry)

    response = _send(client, "POST", "/api/config/repo-a/tools", "absent", {"yaml": NEW_TOOL, "dry_run": True})

    assert response.status_code == 200
    assert response.json()["written"] is False
    assert not (record.path / TOOLS_FILENAME).exists()


def test_schema_add_replace_and_delete_with_dry_run_warnings(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    schema = _write(record.path, SCHEMA_FILENAME, SCHEMA)

    added = _send(client, "POST", "/api/config/repo-a/schema/node_types", _fp(client, "repo-a", "schema"),
                  {"yaml": WIDGET})
    assert added.status_code == 201, added.text
    assert "label: Gadget" in schema.read_text()
    assert any("no provider produces Gadget" in n for n in added.json()["notes"])
    assert [n["label"] for n in added.json()["scope"]["schema"]["node_types"]] == ["Widget", "Gadget"]

    rel = "type: FEEDS\nprovider: custom\ncustom: {name: gadget_feeds}\nfrom: Widget\nto: Gadget\n"
    rel_added = _send(client, "POST", "/api/config/repo-a/schema/relationships", _fp(client, "repo-a", "schema"),
                      {"yaml": rel})
    assert rel_added.status_code == 201, rel_added.text

    changed_key = WIDGET.replace("key: [slug]", "key: [slug, code]").replace(
        "required: true\n", "required: true\n  - name: code\n    type: string\n    required: true\n")
    dry = _send(client, "PUT", "/api/config/repo-a/schema/node_types/Gadget", _fp(client, "repo-a", "schema"),
                {"yaml": changed_key, "dry_run": True})
    assert dry.status_code == 200, dry.text
    assert dry.json()["written"] is False and any("keeps the old key" in w for w in dry.json()["warnings"])

    before = schema.read_bytes()
    dry_delete = _send(client, "DELETE", "/api/config/repo-a/schema/relationships/FEEDS?dry_run=1",
                       _fp(client, "repo-a", "schema"))
    assert dry_delete.status_code == 200 and dry_delete.json()["written"] is False
    assert any("FEEDS" in w for w in dry_delete.json()["warnings"])
    assert schema.read_bytes() == before

    deleted = _send(client, "DELETE", "/api/config/repo-a/schema/relationships/FEEDS", _fp(client, "repo-a", "schema"))
    assert deleted.status_code == 200 and "FEEDS" not in schema.read_text()


def test_schema_entry_in_the_wrong_section_is_refused(client, registry, tmp_path):
    record = _repo(tmp_path, registry)

    response = _send(client, "POST", "/api/config/repo-a/schema/relationships", "absent", {"yaml": WIDGET})

    assert response.status_code == 422 and response.json()["detail"]["code"] == "invalid"
    assert not (record.path / SCHEMA_FILENAME).exists()


@pytest.mark.parametrize("body", [
    "[1, 2]",
    "{}",
    '{"yaml": 3}',
    '{"yaml": "- a\\n- b\\n"}',
    '{"yaml": "name: [oops"}',
    '{"yaml": "name: x", "dry_run": "yes"}',
    "not json",
])
def test_bad_bodies_are_400(client, registry, tmp_path, body):
    record = _repo(tmp_path, registry)

    response = _send(client, "POST", "/api/config/repo-a/tools", "absent", body)

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "bad_request"
    assert not (record.path / TOOLS_FILENAME).exists()


def test_yaml_tags_are_not_constructed(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    hostile = "!!python/object/apply:os.system ['touch pwned']"

    response = _send(client, "POST", "/api/config/repo-a/tools", "absent", {"yaml": hostile})

    assert response.status_code == 400
    assert not (record.path / TOOLS_FILENAME).exists()


def test_non_json_body_is_415(client, registry, tmp_path):
    record = _repo(tmp_path, registry)

    response = client.post("/api/config/repo-a/tools", content=b"yaml=x",
                           headers={"if-match": '"absent"', "content-type": "application/x-www-form-urlencoded"})

    assert response.status_code == 415 and response.json()["detail"]["code"] == "media_type"
    assert not (record.path / TOOLS_FILENAME).exists()


def test_oversized_body_is_413(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    big = json.dumps({"yaml": NEW_TOOL + "# " + "x" * 70_000 + "\n"})

    response = _send(client, "POST", "/api/config/repo-a/tools", "absent", big)

    assert response.status_code == 413 and response.json()["detail"]["code"] == "too_large"
    assert not (record.path / TOOLS_FILENAME).exists()


def test_oversized_body_without_content_length_is_413(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    big = json.dumps({"yaml": NEW_TOOL + "# " + "x" * 70_000 + "\n"}).encode()

    def chunks():
        yield big

    response = client.post("/api/config/repo-a/tools", content=chunks(),
                           headers={"if-match": '"absent"', "content-type": "application/json"})

    assert response.status_code == 413
    assert not (record.path / TOOLS_FILENAME).exists()


def test_missing_if_match_is_428(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    path = _write(record.path, TOOLS_FILENAME, TOOL.format(name="find_parents"))
    before = path.read_bytes()

    for method, url, body in (
        ("POST", "/api/config/repo-a/tools", {"yaml": _tool_yaml("other")}),
        ("PUT", "/api/config/repo-a/tools/find_parents", {"yaml": _tool_yaml("find_parents", "X.")}),
        ("DELETE", "/api/config/repo-a/tools/find_parents", None),
    ):
        response = _send(client, method, url, None, body)
        assert response.status_code == 428, (method, response.text)
        assert response.json()["detail"]["code"] == "precondition_required"
    assert path.read_bytes() == before


def test_stale_if_match_is_412_with_the_current_scope(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    path = _write(record.path, TOOLS_FILENAME, TOOL.format(name="find_parents"))
    seen = _fp(client, "repo-a")
    path.write_text(path.read_text() + "# edited elsewhere\n")
    before = path.read_bytes()

    response = _send(client, "DELETE", "/api/config/repo-a/tools/find_parents", seen)

    assert response.status_code == 412
    detail = response.json()["detail"]
    assert detail["code"] == "stale" and str(record.path) not in detail["message"]
    assert detail["scope"]["tools"]["fingerprint"] == "sha256:" + hashlib.sha256(before).hexdigest()
    assert path.read_bytes() == before
    # "absent" for a file that now exists is stale too: no blind create-over.
    assert _send(client, "POST", "/api/config/repo-a/tools", "absent", {"yaml": _tool_yaml("other")}).status_code == 412


def test_unknown_inactive_and_reserved_scopes_are_404(client, registry, tmp_path):
    _repo(tmp_path, registry, "repo-b")
    registry._set_flag("repo-b", "active", False)
    before = _snapshot(tmp_path)

    for method, url, body in (
        ("POST", "/api/config/nope/tools", {"yaml": NEW_TOOL}),
        ("POST", "/api/config/repo-b/tools", {"yaml": NEW_TOOL}),
        ("DELETE", "/api/config/repo-b/tools/find_parents", None),
        ("POST", "/api/config/__all__/tools", {"yaml": NEW_TOOL}),
        ("POST", "/api/config/__global__/schema/node_types", {"yaml": WIDGET}),
        ("DELETE", "/api/config/__global__/schema/node_types/Widget", None),
    ):
        response = _send(client, method, url, "absent", body)
        assert response.status_code == 404, (url, response.text)
        assert response.json()["detail"]["code"] == "not_found"
    assert _snapshot(tmp_path) == before


def test_bad_section_and_unknown_entry_are_404(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    _write(record.path, SCHEMA_FILENAME, SCHEMA)
    fp = _fp(client, "repo-a", "schema")

    assert _send(client, "POST", "/api/config/repo-a/schema/tools", fp, {"yaml": WIDGET}).status_code == 404
    assert _send(client, "DELETE", "/api/config/repo-a/schema/node_types/Nope", fp).status_code == 404
    assert _send(client, "DELETE", "/api/config/repo-a/tools/nope", "absent").status_code == 404


def test_conflicts_are_409(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    _write(record.path, TOOLS_FILENAME, TOOL.format(name="find_parents") + "      - name: find_kids\n"
           "        description: K.\n        cypher: \"MATCH (n {repo_id: $repo_id}) RETURN n LIMIT 1\"\n")
    fp = _fp(client, "repo-a")

    exists = _send(client, "POST", "/api/config/repo-a/tools", fp, {"yaml": NEW_TOOL})
    assert exists.status_code == 409 and exists.json()["detail"]["code"] == "exists"
    # the taken name, so the page can offer to replace that entry instead
    assert exists.json()["detail"]["name"] == "find_parents"
    taken = _send(client, "PUT", "/api/config/repo-a/tools/find_kids", fp, {"yaml": NEW_TOOL})
    assert taken.status_code == 409 and taken.json()["detail"]["code"] == "exists"
    assert taken.json()["detail"]["name"] == "find_parents"
    locked = _send(client, "POST", "/api/config/repo-a/tools", fp, {"yaml": _tool_yaml("find_callers")})
    assert locked.status_code == 409 and locked.json()["detail"]["code"] == "locked"
    renamed = _send(client, "PUT", "/api/config/repo-a/tools/find_kids", fp, {"yaml": _tool_yaml("find_callers")})
    assert renamed.status_code == 409 and renamed.json()["detail"]["code"] == "locked"
    glob = _send(client, "POST", "/api/config/__global__/tools", "absent", {"yaml": _tool_yaml("find_callers")})
    assert glob.status_code == 409 and glob.json()["detail"]["code"] == "locked"


def test_relationship_declared_twice_is_409_ambiguous(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    extra = "  - type: HAS_PART\n    provider: custom\n    custom: {name: other_parts}\n    from: Widget\n    to: Widget\n"
    _write(record.path, SCHEMA_FILENAME, SCHEMA)
    (record.path / SCHEMA_FILENAME).write_text((record.path / SCHEMA_FILENAME).read_text() + extra)
    path = record.path / SCHEMA_FILENAME
    before = path.read_bytes()
    rel = "type: HAS_PART\nprovider: custom\ncustom: {name: x}\nfrom: Widget\nto: Widget\n"

    response = _send(client, "PUT", "/api/config/repo-a/schema/relationships/HAS_PART",
                     _fp(client, "repo-a", "schema"), {"yaml": rel})

    assert response.status_code == 409 and response.json()["detail"]["code"] == "ambiguous", response.text
    assert path.read_bytes() == before


def test_invalid_result_is_422_and_writes_nothing(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    path = _write(record.path, TOOLS_FILENAME, TOOL.format(name="find_parents"))
    before = path.read_bytes()

    response = _send(client, "POST", "/api/config/repo-a/tools", _fp(client, "repo-a"),
                     {"yaml": "name: other\ndescription: No cypher.\n"})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "invalid" and TOOLS_FILENAME in detail["message"]
    assert str(record.path) not in detail["message"] and str(tmp_path) not in detail["message"]
    assert detail["scope"]["repo_id"] == "repo-a"
    assert path.read_bytes() == before


def test_write_failure_is_a_generic_500(client, registry, tmp_path, monkeypatch):
    from devgraph.config import edits

    record = _repo(tmp_path, registry)

    def fail(path, text):
        raise OSError(f"disk full at {path}")

    monkeypatch.setattr(edits, "write_atomically", fail)
    response = _send(client, "POST", "/api/config/repo-a/tools", "absent", {"yaml": NEW_TOOL})

    assert response.status_code == 500
    assert response.json()["detail"]["message"] == f"could not write {TOOLS_FILENAME}"
    assert not (record.path / TOOLS_FILENAME).exists()


@pytest.mark.parametrize("headers", [
    {"origin": "http://evil.test"},
    {"sec-fetch-site": "cross-site"},
    {"sec-fetch-site": "same-site"},
])
def test_cross_site_writes_are_403_and_leave_the_file_untouched(client, registry, tmp_path, global_store, headers):
    record = _repo(tmp_path, registry)
    path = _write(record.path, TOOLS_FILENAME, TOOL.format(name="find_parents"))
    _write(record.path, SCHEMA_FILENAME, SCHEMA)
    _global_store(global_store, "hot_paths")
    before = _snapshot(tmp_path)
    fp = _fp(client, "repo-a")

    for method, url, body in (
        ("POST", "/api/config/repo-a/tools", {"yaml": _tool_yaml("other")}),
        ("PUT", "/api/config/repo-a/tools/find_parents", {"yaml": _tool_yaml("find_parents", "X.")}),
        ("DELETE", "/api/config/repo-a/tools/find_parents", None),
        ("POST", "/api/config/__global__/tools", {"yaml": _tool_yaml("other")}),
        ("DELETE", "/api/config/__global__/tools/hot_paths", None),
        ("POST", "/api/config/repo-a/schema/node_types", {"yaml": WIDGET}),
        ("PUT", "/api/config/repo-a/schema/node_types/Widget", {"yaml": WIDGET.replace("Gadget", "Widget")}),
        ("DELETE", "/api/config/repo-a/schema/node_types/Widget", None),
    ):
        response = _send(client, method, url, fp, body, headers=headers)
        assert response.status_code == 403, (method, url, response.text)
    assert _snapshot(tmp_path) == before
    assert path.exists()


def test_dns_rebinding_write_is_refused_by_the_host_guard(registry, engine, global_store, tmp_path):
    record = _repo(tmp_path, registry)
    app = build_app(engine, registry, EventBroadcaster(), dashboard_host="127.0.0.1")
    client = TestClient(app, base_url="http://127.0.0.1")
    rebinding = {"host": "evil.test:8765", "origin": "http://evil.test:8765"}

    response = _send(client, "POST", "/api/config/repo-a/tools", "absent", {"yaml": NEW_TOOL}, headers=rebinding)

    assert response.status_code == 403
    assert not (record.path / TOOLS_FILENAME).exists()
    # Same-origin from the real host still works through the full app.
    ok = _send(client, "POST", "/api/config/repo-a/tools", "absent", {"yaml": NEW_TOOL},
               headers={"origin": "http://127.0.0.1", "sec-fetch-site": "same-origin"})
    assert ok.status_code == 201, ok.text


@pytest.mark.parametrize("name", ["..%2F..%2Fevil", "..", "%2E%2E", "..%5Cevil", "a%2Fb", "%2Fetc%2Fpasswd"])
def test_path_like_entry_names_are_only_names(client, registry, tmp_path, name):
    record = _repo(tmp_path, registry)
    _write(record.path, TOOLS_FILENAME, TOOL.format(name="find_parents"))
    _write(record.path, SCHEMA_FILENAME, SCHEMA)
    before = _snapshot(tmp_path)
    fp_tools, fp_schema = _fp(client, "repo-a"), _fp(client, "repo-a", "schema")

    for method, url, fp, body in (
        ("PUT", f"/api/config/repo-a/tools/{name}", fp_tools, {"yaml": _tool_yaml("find_parents")}),
        ("DELETE", f"/api/config/repo-a/tools/{name}", fp_tools, None),
        ("PUT", f"/api/config/repo-a/schema/node_types/{name}", fp_schema, {"yaml": WIDGET}),
        ("DELETE", f"/api/config/repo-a/schema/node_types/{name}", fp_schema, None),
        ("DELETE", f"/api/config/{name}/tools/find_parents", fp_tools, None),
    ):
        response = _send(client, method, url, fp, body)
        assert response.status_code in (404, 405), (method, url, response.status_code, response.text)
    assert _snapshot(tmp_path) == before


def test_path_like_names_in_the_body_are_rejected_by_validation(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    before = _snapshot(tmp_path)

    for yaml_text in (_tool_yaml("../../evil"), _tool_yaml("/etc/passwd")):
        response = _send(client, "POST", "/api/config/repo-a/tools", "absent", {"yaml": yaml_text})
        assert response.status_code in (409, 422), response.text
    assert _snapshot(tmp_path) == before
    assert not (record.path / TOOLS_FILENAME).exists()


def test_symlinked_target_is_409_and_the_link_target_is_untouched(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    elsewhere = tmp_path / "elsewhere.yaml"
    elsewhere.write_text(textwrap.dedent(TOOL.format(name="find_parents")).lstrip("\n"))
    (record.path / TOOLS_FILENAME).symlink_to(elsewhere)
    before = elsewhere.read_bytes()

    response = _send(client, "POST", "/api/config/repo-a/tools", _fp(client, "repo-a"), {"yaml": _tool_yaml("other")})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "not_regular" and str(tmp_path) not in detail["message"]
    assert elsewhere.read_bytes() == before and (record.path / TOOLS_FILENAME).is_symlink()


def test_missing_repo_directory_is_404(client, registry, tmp_path):
    import shutil

    record = _repo(tmp_path, registry)
    shutil.rmtree(record.path)

    response = _send(client, "POST", "/api/config/repo-a/tools", "absent", {"yaml": NEW_TOOL})

    assert response.status_code == 404
    assert not record.path.exists()


def test_disabled_project_config_still_writes_and_says_so(client, registry, tmp_path, monkeypatch):
    record = _repo(tmp_path, registry)
    registry.set_project_config_enabled("repo-a", False)
    monkeypatch.setattr(project_switch, "_registry_db_path", lambda: tmp_path / "registry.sqlite3")

    response = _send(client, "POST", "/api/config/repo-a/tools", "absent", {"yaml": NEW_TOOL})

    assert response.status_code == 201
    assert (record.path / TOOLS_FILENAME).exists()
    assert any("disabled" in n for n in response.json()["notes"])


def test_writes_never_touch_git(client, registry, tmp_path, monkeypatch):
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not installed")
    root = tmp_path / "real-repo"
    root.mkdir()
    git = ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    schema = _write(root, SCHEMA_FILENAME, SCHEMA)
    subprocess.run([*git, "add", SCHEMA_FILENAME], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    registry.add_repo(root)
    head = (root / ".git" / "HEAD").read_bytes()
    index = (root / ".git" / "index").read_bytes()

    def no_subprocess(*args, **kwargs):
        raise AssertionError(f"config write ran a subprocess: {args!r}")

    with monkeypatch.context() as m:
        m.setattr(subprocess, "Popen", no_subprocess)
        fp = _fp(client, "real-repo", "schema")
        assert _send(client, "POST", "/api/config/real-repo/tools", "absent", {"yaml": NEW_TOOL}).status_code == 201
        assert _send(client, "POST", "/api/config/real-repo/schema/node_types", fp, {"yaml": WIDGET}).status_code == 201

    assert (root / ".git" / "index").read_bytes() == index and (root / ".git" / "HEAD").read_bytes() == head
    status = subprocess.run([*git, "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
    assert sorted(status.splitlines()) == sorted([f" M {SCHEMA_FILENAME}", f"?? {TOOLS_FILENAME}"])
    staged = subprocess.run([*git, "diff", "--cached", "--name-only"], capture_output=True, text=True, check=True).stdout
    assert staged == ""
    assert "label: Gadget" in schema.read_text()


# --- hardening -----------------------------------------------------------------------------------


def test_unicode_line_breaks_in_values_cannot_inject_keys(client, registry, tmp_path):
    import yaml

    record = _repo(tmp_path, registry)
    entry = {"name": "find_parents", "description": "a\nb     max_rows: 7",
             "cypher": "MATCH (n {repo_id: $repo_id}) RETURN n LIMIT 1"}

    response = _send(client, "POST", "/api/config/repo-a/tools", "absent", {"yaml": yaml.safe_dump(entry)})

    assert response.status_code == 201, response.text
    [written] = yaml.safe_load((record.path / TOOLS_FILENAME).read_text())["tools"]
    assert written == entry


def test_a_splice_that_diverges_from_the_submitted_entry_is_422(client, registry, tmp_path, monkeypatch):
    import devgraph.config.list_edit as list_edit

    record = _repo(tmp_path, registry)
    path = _write(record.path, TOOLS_FILENAME, TOOL.format(name="find_parents"))
    before = path.read_bytes()
    monkeypatch.setattr(list_edit, "dump_entry", lambda e: NEW_TOOL.replace("find_parents", "other") + "max_rows: 7\n")

    response = _send(client, "POST", "/api/config/repo-a/tools", _fp(client, "repo-a"), {"yaml": _tool_yaml("other")})

    assert response.status_code == 422 and response.json()["detail"]["code"] == "invalid"
    assert path.read_bytes() == before


@pytest.mark.parametrize("yaml_text", [
    "name: !!binary aGk=\ndescription: x\ncypher: RETURN 1\n",
    "name: 12\ndescription: x\ncypher: RETURN 1\n",
])
def test_non_string_identity_is_422(client, registry, tmp_path, yaml_text):
    record = _repo(tmp_path, registry)

    response = _send(client, "POST", "/api/config/repo-a/tools", "absent", {"yaml": yaml_text})

    assert response.status_code == 422 and response.json()["detail"]["code"] == "invalid"
    assert not (record.path / TOOLS_FILENAME).exists()


def test_repo_root_replaced_by_a_symlink_is_409(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    real = tmp_path / "moved"
    record.path.rename(real)
    record.path.symlink_to(real, target_is_directory=True)

    response = _send(client, "POST", "/api/config/repo-a/tools", "absent", {"yaml": NEW_TOOL})

    assert response.status_code == 409 and response.json()["detail"]["code"] == "not_regular"
    assert not (real / TOOLS_FILENAME).exists()


@pytest.mark.parametrize("kind", ["dir", "fifo"])
def test_non_regular_target_is_409(client, registry, tmp_path, kind):
    record = _repo(tmp_path, registry)
    target = record.path / TOOLS_FILENAME
    target.mkdir() if kind == "dir" else os.mkfifo(target)
    fp = _fp(client, "repo-a")  # the read model must not block on a FIFO either

    response = _send(client, "POST", "/api/config/repo-a/tools", fp, {"yaml": NEW_TOOL})

    assert response.status_code == 409 and response.json()["detail"]["code"] == "not_regular"


@pytest.mark.parametrize("value,status", [("yes", 400), ("", 400), ("TRUE", 200), ("1", 200), ("0", 200), ("False", 200)])
def test_delete_dry_run_flag_values(client, registry, tmp_path, value, status):
    record = _repo(tmp_path, registry)
    path = _write(record.path, TOOLS_FILENAME, TOOL.format(name="find_parents"))
    before = path.read_bytes()

    response = _send(client, "DELETE", f"/api/config/repo-a/tools/find_parents?dry_run={value}", _fp(client, "repo-a"))

    assert response.status_code == status, response.text
    if status == 400:
        assert response.json()["detail"]["code"] == "bad_request"
    if status == 400 or value.lower() in ("1", "true"):
        assert path.read_bytes() == before
    else:
        assert "find_parents" not in path.read_text()


def test_response_fingerprint_is_the_written_files(client, registry, tmp_path):
    record = _repo(tmp_path, registry)

    body = _send(client, "POST", "/api/config/repo-a/tools", "absent", {"yaml": NEW_TOOL}).json()

    expected = "sha256:" + hashlib.sha256((record.path / TOOLS_FILENAME).read_bytes()).hexdigest()
    assert body["fingerprint"] == expected == body["scope"]["tools"]["fingerprint"]
    follow_up = _send(client, "PUT", "/api/config/repo-a/tools/find_parents", body["fingerprint"],
                      {"yaml": _tool_yaml("find_parents", "Again.")})
    assert follow_up.status_code == 200


def test_concurrent_writers_with_the_same_if_match_one_wins(client, registry, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    record = _repo(tmp_path, registry)

    def write(i: int) -> int:
        return _send(client, "POST", "/api/config/repo-a/tools", "absent", {"yaml": _tool_yaml(f"tool_{i}")}).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = sorted(pool.map(write, range(8)))

    assert statuses == [201] + [412] * 7
    assert len(yaml_tools(record.path)) == 1


def yaml_tools(root: Path) -> list:
    import yaml

    return yaml.safe_load((root / TOOLS_FILENAME).read_text())["tools"]


def test_node_type_rename_to_a_taken_label_is_409(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    _write(record.path, SCHEMA_FILENAME, SCHEMA)
    assert _send(client, "POST", "/api/config/repo-a/schema/node_types", _fp(client, "repo-a", "schema"),
                 {"yaml": WIDGET}).status_code == 201
    before = (record.path / SCHEMA_FILENAME).read_bytes()

    response = _send(client, "PUT", "/api/config/repo-a/schema/node_types/Gadget", _fp(client, "repo-a", "schema"),
                     {"yaml": WIDGET.replace("Gadget", "Widget")})

    assert response.status_code == 409 and response.json()["detail"]["code"] == "exists"
    assert (record.path / SCHEMA_FILENAME).read_bytes() == before


# --- final-review fixes ------------------------------------------------------------------------


class DownEngine(StubEngine):
    """Neo4j unreachable: every applied-schema read raises the driver's own error."""

    def read_applied_schema(self, repo_id: str):
        from neo4j.exceptions import ServiceUnavailable

        raise ServiceUnavailable("Couldn't connect to localhost:7687")


@pytest.fixture
def down_client(registry, global_store):
    app = FastAPI()
    app.include_router(routes.build_router(DownEngine(), registry, EventBroadcaster()))
    return TestClient(app, base_url="http://127.0.0.1")


def test_neo4j_down_model_marks_schema_state_unknown(down_client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    _write(record.path, SCHEMA_FILENAME, SCHEMA)
    _repo(tmp_path, registry, "repo-b")
    _write(tmp_path / "repo-b", SCHEMA_FILENAME, "version: 1\nnode_types: [oops")

    response = down_client.get("/api/config")

    assert response.status_code == 200, response.text
    a, b = response.json()["projects"]
    assert a["schema"]["state"] == "unknown"
    [graph] = a["schema"]["badges"]
    assert graph["kind"] == "graph-unavailable" and graph["level"] == "muted"
    assert "graph unavailable" in graph["text"].lower()
    assert [n["label"] for n in a["schema"]["node_types"]] == ["Widget"]
    # a file-derived state does not need the graph
    assert b["schema"]["state"] == "invalid" and _kinds(b["schema"]["badges"]) == ["schema-invalid"]
    assert down_client.get("/api/config/repo-a").json()["schema"]["state"] == "unknown"


def test_neo4j_down_writes_still_answer_normally(down_client, registry, tmp_path):
    record = _repo(tmp_path, registry)

    tool = _send(down_client, "POST", "/api/config/repo-a/tools", "absent", {"yaml": NEW_TOOL})
    assert tool.status_code == 201, tool.text
    assert "name: find_parents" in (record.path / TOOLS_FILENAME).read_text()
    assert tool.json()["scope"]["schema"]["state"] == "unknown"

    node = _send(down_client, "POST", "/api/config/repo-a/schema/node_types", "absent", {"yaml": WIDGET})
    assert node.status_code == 201, node.text
    assert "label: Gadget" in (record.path / SCHEMA_FILENAME).read_text()

    stale = _send(down_client, "DELETE", "/api/config/repo-a/tools/find_parents", "absent")
    assert stale.status_code == 412 and stale.json()["detail"]["scope"]["schema"]["state"] == "unknown"


def test_only_driver_errors_are_tolerated(registry, global_store, tmp_path):
    class Broken(StubEngine):
        def read_applied_schema(self, repo_id):
            raise RuntimeError("a bug, not an outage")

    app = FastAPI()
    app.include_router(routes.build_router(Broken(), registry, EventBroadcaster()))
    _repo(tmp_path, registry)

    with pytest.raises(RuntimeError):
        TestClient(app, base_url="http://127.0.0.1").get("/api/config")


def test_get_config_resolves_each_repo_once(client, registry, tmp_path, global_store, monkeypatch):
    from devgraph.mcp import tool_plane

    _repo(tmp_path, registry, "repo-a")
    _repo(tmp_path, registry, "repo-b")
    _global_store(global_store, "hot_paths")
    calls: list[str] = []
    real = tool_plane.resolve_tools

    def spy(repo, *args, **kwargs):
        calls.append(repo.repo_id)
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(tool_plane, "resolve_tools", spy)

    assert client.get("/api/config").status_code == 200
    assert sorted(calls) == ["repo-a", "repo-b"]


class _RefusingServer:
    """A live server whose SDK refuses some tools at registration (add_tool raises)."""

    def __init__(self, refuse: set[str]) -> None:
        self.refuse = refuse

    def add_tool(self, fn, name, description, annotations):
        if description in self.refuse:
            raise ValueError("cannot build an input schema")


def test_registration_failures_give_fallback_and_not_served_badges(registry, tmp_path, global_store):
    from devgraph.dashboard import config_model
    from devgraph.mcp.tool_plane import resolve_tools

    record = _repo(tmp_path, registry)
    _global_store(global_store, "hot_paths")
    _write(record.path, TOOLS_FILENAME, TOOL.format(name="hot_paths") + "      - name: lonely\n"
           "        description: L.\n        cypher: \"MATCH (n {repo_id: $repo_id}) RETURN n LIMIT 1\"\n")
    live = resolve_tools(record, server=_RefusingServer({"A tool.", "L."}))
    schema_info = lambda r: {"state": "absent", "error": None}

    block = config_model.build_project(record, schema_info, status=live)

    entries = {e["name"]: e for e in block["tools"]["entries"]}
    assert entries["hot_paths"]["origin"] == "global"
    [fallback] = entries["hot_paths"]["badges"]
    assert fallback["kind"] == "fallback-global" and "could not be served" in fallback["detail"]
    assert "cannot build an input schema" in fallback["detail"]
    [missing] = entries["lonely"]["badges"]
    assert missing["kind"] == "not-served" and missing["level"] == "error"
    assert "could not be served" in missing["detail"]
    # the dry resolution the page uses cannot see a registration failure
    dry = config_model.build_project(record, schema_info)
    assert [_kinds(e["badges"]) for e in dry["tools"]["entries"]] == [["overrides-global"], []]


def test_builtin_name_shadowed_in_both_layers(client, registry, tmp_path, global_store):
    from devgraph.mcp.tool_plane import resolve_tools

    record = _repo(tmp_path, registry)
    _write(record.path, TOOLS_FILENAME, TOOL.format(name="find_callers"))
    _global_store(global_store, "find_callers")

    shadowed = resolve_tools(record).shadowed["find_callers"]
    assert len(shadowed) == 2
    assert any("global tool 'find_callers'" in n for n in shadowed)
    assert any("project tool 'find_callers'" in n for n in shadowed)
    model = client.get("/api/config").json()
    [project_entry] = model["projects"][0]["tools"]["entries"]
    [global_entry] = model["global"]["tools"]["entries"]
    assert _kinds(project_entry["badges"]) == ["locked-shadow"]
    assert _kinds(global_entry["badges"]) == ["locked-shadow"]


def test_invalid_project_and_invalid_global_together(client, registry, tmp_path, global_store):
    record = _repo(tmp_path, registry)
    global_store.parent.mkdir(parents=True)
    global_store.write_text("{not json")
    _write(record.path, TOOLS_FILENAME, "version: 1\ntools: [oops\n")

    response = client.get("/api/config")

    assert response.status_code == 200
    model = response.json()
    assert model["global"]["tools"]["state"] == "invalid"
    assert _kinds(model["global"]["tools"]["badges"]) == ["file-invalid"]
    project_tools = model["projects"][0]["tools"]
    assert project_tools["state"] == "invalid" and _kinds(project_tools["badges"]) == ["file-invalid"]
    assert str(record.path) not in project_tools["error"] and str(global_store.parent) not in model["global"]["tools"]["error"]


def test_reserved_repo_ids_do_not_collide_with_config_scopes(client, registry, tmp_path):
    root = tmp_path / "__global__"
    (root / ".git").mkdir(parents=True)
    record = registry.add_repo(root)
    assert record.repo_id != "__global__"

    model = client.get("/api/config").json()
    assert [p["repo_id"] for p in model["projects"]] == [record.repo_id]
    assert "repo_id" not in client.get("/api/config/__global__").json()
    assert client.get(f"/api/config/{record.repo_id}").json()["repo_id"] == record.repo_id


def test_global_value_json_cannot_store_is_422(client, global_store):
    text = "name: blob\ndescription: !!binary aGk=\ncypher: |\n  MATCH (n {repo_id: $repo_id}) RETURN n LIMIT 1\n"

    response = _send(client, "POST", "/api/config/__global__/tools", "absent", {"yaml": text})

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "invalid"
    assert "JSON cannot store" in response.json()["detail"]["message"]
    assert not global_store.exists()


def test_exists_messages_say_edit_it_instead_without_cli_commands(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    _write(record.path, TOOLS_FILENAME, TOOL.format(name="find_parents"))
    _write(record.path, SCHEMA_FILENAME, SCHEMA)

    tool = _send(client, "POST", "/api/config/repo-a/tools", _fp(client, "repo-a"), {"yaml": NEW_TOOL})
    node = _send(client, "POST", "/api/config/repo-a/schema/node_types", _fp(client, "repo-a", "schema"),
                 {"yaml": WIDGET.replace("Gadget", "Widget")})

    for response in (tool, node):
        assert response.status_code == 409
        message = response.json()["detail"]["message"]
        assert "already exists" in message and message.endswith("— edit it instead")
        assert "devgraph config" not in message
