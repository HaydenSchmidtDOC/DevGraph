"""The Config page and project tools trust: the badge, revoking, and that the dashboard can never grant trust."""

import re

import pytest

from devgraph.config.project_tools import TOOLS_FILENAME
from devgraph.config.project_trust import tools_sha256
from tests.dashboard import test_config_routes as base
from tests.dashboard.test_config_routes import NEW_TOOL, TOOL, _fp, _kinds, _repo, _send, _write

# the Config route tests' fixtures: a real temporary registry, a stub engine, the global store in tmp
client, engine, global_store, registry = base.client, base.engine, base.global_store, base.registry


@pytest.fixture(autouse=True)
def trust(real_project_trust, tmp_path, monkeypatch):
    monkeypatch.setattr(real_project_trust, "_registry_db_path", lambda: tmp_path / "registry.sqlite3")
    return real_project_trust


def _trusted(registry, record):
    registry.set_project_tools_sha256(record.repo_id, tools_sha256((record.path / TOOLS_FILENAME).read_bytes()))


def _tools(client, scope="repo-a"):
    return client.get(f"/api/config/{scope}").json()["tools"]


def test_an_untrusted_file_shows_not_trusted_and_its_tools_not_served(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    _write(record.path, TOOLS_FILENAME, TOOL.format(name="hot_paths"))

    tools = _tools(client)

    assert tools["trust"] == {"state": "untrusted", "command": "devgraph config tools trust repo-a", "revocable": False}
    badge = next(b for b in tools["badges"] if b["kind"] == "not-trusted")
    assert badge["text"] == "Not trusted" and "devgraph config tools trust repo-a" in badge["detail"]
    assert "whole graph" in badge["detail"]
    [entry] = tools["entries"]
    assert entry["origin"] is None
    assert _kinds(entry["badges"]) == ["not-trusted"]
    assert "project tools not trusted (run devgraph config tools trust repo-a)" in entry["badges"][0]["detail"]


def test_a_trusted_file_shows_trusted_and_an_edit_shows_changed(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    _write(record.path, TOOLS_FILENAME, TOOL.format(name="hot_paths"))
    _trusted(registry, record)

    tools = _tools(client)
    assert tools["trust"]["state"] == "trusted" and tools["trust"]["revocable"] is True
    assert "trusted" in _kinds(tools["badges"])
    assert tools["entries"][0]["origin"] == "project"

    _send(client, "POST", "/api/config/repo-a/tools", _fp(client, "repo-a"), {"yaml": NEW_TOOL})
    tools = _tools(client)
    assert tools["trust"]["state"] == "changed"
    assert next(b for b in tools["badges"] if b["kind"] == "not-trusted")["text"] == "Changed since trusted"
    assert all(e["origin"] is None for e in tools["entries"])


def test_an_untrusted_project_tool_falls_back_to_the_global_one(client, registry, tmp_path, global_store):
    from tests.dashboard.test_config_routes import _global_store

    record = _repo(tmp_path, registry)
    _write(record.path, TOOLS_FILENAME, TOOL.format(name="hot_paths"))
    _global_store(global_store, "hot_paths")

    [entry] = _tools(client)["entries"]
    assert entry["origin"] == "global"
    badge = next(b for b in entry["badges"] if b["kind"] == "fallback-global")
    assert "project tools not trusted" in badge["detail"]
    glob = client.get("/api/config/__global__").json()["tools"]["entries"][0]
    assert "overridden" not in _kinds(glob["badges"])


@pytest.mark.parametrize("op", ["add", "replace", "delete", "reset"])
def test_project_tools_dry_runs_say_saving_needs_retrusting(client, registry, tmp_path, op):
    record = _repo(tmp_path, registry)
    _write(record.path, TOOLS_FILENAME, TOOL.format(name="hot_paths"))
    _trusted(registry, record)
    fp = _fp(client, "repo-a")
    response = {
        "add": lambda: _send(client, "POST", "/api/config/repo-a/tools", fp, {"yaml": NEW_TOOL, "dry_run": True}),
        "replace": lambda: _send(client, "PUT", "/api/config/repo-a/tools/hot_paths", fp,
                                 {"yaml": NEW_TOOL.replace("find_parents", "hot_paths"), "dry_run": True}),
        "delete": lambda: _send(client, "DELETE", "/api/config/repo-a/tools/hot_paths?dry_run=1", fp),
        "reset": lambda: _send(client, "POST", "/api/config/repo-a/reset/tools", fp, {"dry_run": True}),
    }[op]()

    assert response.status_code == 200, response.text
    notes = response.json()["notes"]
    assert ("Saving stops repo-a's project tools being served until you run "
            "`devgraph config tools trust repo-a`; running MCP sessions pick that up within 2 seconds.") in notes
    assert not any(re.search(r"[0-9a-f]{64}", n) or "--sha256" in n for n in notes)


def test_revoke_clears_the_approval(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    _write(record.path, TOOLS_FILENAME, TOOL.format(name="hot_paths"))
    _trusted(registry, record)

    response = client.delete("/api/config/repo-a/trust/tools")

    assert response.status_code == 200
    body = response.json()
    assert body["written"] is True
    assert registry.get("repo-a").project_tools_sha256 is None
    assert body["scope"]["tools"]["trust"]["state"] == "untrusted"
    assert any("devgraph config tools trust repo-a" in n for n in body["notes"])
    again = client.delete("/api/config/repo-a/trust/tools").json()
    assert again["written"] is False


def test_revoke_refuses_cross_site_and_the_global_scope(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    _write(record.path, TOOLS_FILENAME, TOOL.format(name="hot_paths"))
    _trusted(registry, record)
    for headers in ({"Origin": "http://evil.test"}, {"Sec-Fetch-Site": "cross-site"}):
        assert client.delete("/api/config/repo-a/trust/tools", headers=headers).status_code == 403
    assert registry.get("repo-a").project_tools_sha256 is not None
    assert client.delete("/api/config/__global__/trust/tools").status_code == 404


def test_the_dashboard_cannot_grant_trust(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    _write(record.path, TOOLS_FILENAME, TOOL.format(name="hot_paths"))
    digest = tools_sha256((record.path / TOOLS_FILENAME).read_bytes())
    for method in ("POST", "PUT", "PATCH"):
        response = client.request(method, "/api/config/repo-a/trust/tools", json={"sha256": digest, "trusted": True})
        assert response.status_code == 405
    assert registry.get("repo-a").project_tools_sha256 is None
    assert _tools(client)["trust"]["state"] == "untrusted"


def test_a_tools_file_outside_the_repository_is_not_shown(client, registry, tmp_path):
    record = _repo(tmp_path, registry)
    outside = tmp_path / "elsewhere.yaml"
    outside.write_text(TOOL.format(name="outside_secret"))
    (record.path / TOOLS_FILENAME).symlink_to(outside)

    response = client.get("/api/config/repo-a")
    assert response.status_code == 200
    assert "outside_secret" not in response.text and "elsewhere" not in response.text
    assert response.json()["tools"]["state"] == "invalid"


def _api_routes(routes_):
    """Every APIRoute, including those inside included routers."""
    from fastapi.routing import APIRoute

    for route in routes_:
        if isinstance(route, APIRoute):
            yield route
        nested = getattr(route, "routes", None) or getattr(getattr(route, "original_router", None), "routes", None)
        if nested:
            yield from _api_routes(nested)


def test_no_write_route_can_set_a_trust_value(registry, engine, global_store, tmp_path, monkeypatch):
    """Walk every route of the real app; the registry's trust setter may only ever be given None."""
    from fastapi.testclient import TestClient

    from devgraph.dashboard.app import build_app
    from devgraph.dashboard.events import EventBroadcaster
    from devgraph.registry.store import RepoRegistry

    record = _repo(tmp_path, registry)
    _write(record.path, TOOLS_FILENAME, TOOL.format(name="hot_paths"))
    digest = tools_sha256((record.path / TOOLS_FILENAME).read_bytes())
    registry.set_project_tools_sha256("repo-a", "0" * 64)  # so the revoke route has something to clear
    calls = []
    real_setter = RepoRegistry.set_project_tools_sha256

    def spy(self, repo_id, sha256):
        calls.append(sha256)
        return real_setter(self, repo_id, sha256)

    monkeypatch.setattr(RepoRegistry, "set_project_tools_sha256", spy)
    app = build_app(engine, registry, EventBroadcaster(), dashboard_host="127.0.0.1")
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    values = {"repo_id": "repo-a", "scope": "repo-a", "name": "hot_paths", "section": "node_types"}
    body = {"sha256": digest, "project_tools_sha256": digest, "trusted": True, "trust": True,
            "enabled": True, "path": str(record.path), "yaml": TOOL.format(name="hot_paths")}
    writes = 0
    for route in _api_routes(app.routes):
        for method in sorted(route.methods - {"GET", "HEAD", "OPTIONS"}):
            url = route.path.format(**{k: values.get(k, "x") for k in route.param_convertors})
            for scope in ("repo-a", "__global__"):
                client.request(method, url.replace("repo-a", scope), json=body,
                               params={"sha256": digest, "confirm": "true"})
                writes += 1
    assert writes >= 20  # every write route was exercised
    assert None in calls  # the revoke route reached the setter: the spy is live
    assert all(value is None for value in calls), calls
