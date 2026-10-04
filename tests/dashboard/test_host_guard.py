"""The dashboard only answers requests addressed to the local machine.

A DNS-rebinding page (a hostile domain re-pointed at 127.0.0.1 after load)
is same-origin with itself, so its requests carry a matching `Origin` and
`Host` -- the per-route cross-site check can't tell it apart from the real
dashboard. The `Host` header is what gives it away: it names the hostile
domain, not a loopback address or the configured bind host.

No Neo4j needed: a stub engine serves the one Cypher call.
"""

import pytest
from fastapi.testclient import TestClient

from devgraph.config.settings import Settings
from devgraph.dashboard import app as dashboard_app
from devgraph.dashboard import layout_store
from devgraph.dashboard.app import build_app
from devgraph.dashboard.events import EventBroadcaster
from devgraph.registry.store import RepoRegistry


class _StubEngine:
    def __init__(self):
        self.queries = []

    def run_cypher_graph(self, query, params=None):
        self.queries.append(query)
        return {"data": []}


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    registries = []

    def _make(dashboard_host="127.0.0.1"):
        settings = Settings(registry_db_path=tmp_path / "registry.sqlite3")
        monkeypatch.setattr(layout_store, "get_settings", lambda: settings)
        registry = RepoRegistry(settings.registry_db_path)
        registries.append(registry)
        engine = _StubEngine()
        app = build_app(engine, registry, EventBroadcaster(), dashboard_host=dashboard_host)
        return TestClient(app), engine, registry

    yield _make
    for registry in registries:
        registry.close()


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.0.0.1:8765", "localhost", "localhost:8765", "LOCALHOST:8765", "[::1]", "[::1]:8765"],
)
def test_loopback_hosts_are_served(make_client, host):
    client, _, _ = make_client()
    assert client.get("/api/query-log", headers={"host": host}).status_code == 200
    assert client.get("/", headers={"host": host}).status_code == 200


@pytest.mark.parametrize("host", ["192.168.1.20", "192.168.1.20:8765"])
def test_the_configured_dashboard_host_is_served(make_client, host):
    client, _, _ = make_client(dashboard_host="192.168.1.20")
    assert client.get("/api/query-log", headers={"host": host}).status_code == 200


def test_a_configured_ipv6_dashboard_host_is_served_bracketed(make_client):
    client, _, _ = make_client(dashboard_host="fd00::20")
    assert client.get("/api/query-log", headers={"host": "[fd00::20]:8765"}).status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "evil.test",
        "evil.test:8765",
        "127.0.0.1.evil.test:8765",
        "localhost.evil.test",
        "testserver",
        "[::2]:8765",
        "::1",  # an IPv6 literal must be bracketed in Host
        "[::1",
        "127.0.0.1:notaport",
        "",
    ],
)
def test_other_hosts_are_rejected_on_every_route(make_client, host):
    client, _, _ = make_client()
    for path in ("/", "/static/index.html", "/api/repos", "/api/settings", "/api/query-log", "/api/events"):
        res = client.get(path, headers={"host": host})
        assert res.status_code == 403, (path, host)
        assert res.text == "host not allowed"


def test_the_allowed_host_defaults_to_the_configured_setting(tmp_path, monkeypatch):
    settings = Settings(registry_db_path=tmp_path / "registry.sqlite3", dashboard_host="192.168.1.20")
    monkeypatch.setattr(dashboard_app, "get_settings", lambda: settings)
    registry = RepoRegistry(settings.registry_db_path)
    try:
        client = TestClient(build_app(_StubEngine(), registry, EventBroadcaster()))
        assert client.get("/api/query-log", headers={"host": "192.168.1.20:8765"}).status_code == 200
        assert client.get("/api/query-log", headers={"host": "evil.test:8765"}).status_code == 403
    finally:
        registry.close()


@pytest.mark.parametrize("dashboard_host", ["0.0.0.0", "::"])
def test_a_wildcard_bind_still_accepts_only_loopback_names(make_client, dashboard_host):
    client, _, _ = make_client(dashboard_host=dashboard_host)
    assert client.get("/api/query-log", headers={"host": "127.0.0.1:8765"}).status_code == 200
    for host in ("evil.test:8765", "0.0.0.0:8765", "[::]:8765"):
        assert client.get("/api/query-log", headers={"host": host}).status_code == 403, host


# The rebinding case proper: Origin and Host agree with each other, so the
# per-route same-origin check alone would let these through.
_REBOUND = {"host": "evil.test:8765", "origin": "http://evil.test:8765", "sec-fetch-site": "same-origin"}


def test_rebound_register_repo_is_rejected_before_writing(make_client, tmp_path):
    client, _, registry = make_client()
    res = client.post("/api/repos", json={"path": str(tmp_path)}, headers=_REBOUND)
    assert res.status_code == 403
    assert registry.list_repos() == []


def test_rebound_layout_write_is_rejected(make_client, tmp_path):
    client, _, _ = make_client()
    res = client.put("/api/repos/__all__/layout", json={"positions": {}}, headers=_REBOUND)
    assert res.status_code == 403
    assert not any(tmp_path.rglob("*.json"))


def test_rebound_cypher_is_rejected_before_running(make_client):
    client, engine, _ = make_client()
    res = client.post("/api/cypher", json={"query": "MATCH (n) DETACH DELETE n"}, headers=_REBOUND)
    assert res.status_code == 403
    assert engine.queries == []


_CROSS_SITE = [
    {"host": "127.0.0.1:8765", "origin": "http://evil.test"},
    {"host": "127.0.0.1:8765", "sec-fetch-site": "cross-site"},
]


@pytest.mark.parametrize("headers", _CROSS_SITE, ids=["cross-origin", "cross-site"])
def test_layout_write_rejects_a_cross_site_request(make_client, tmp_path, headers):
    client, _, _ = make_client()
    res = client.put("/api/repos/__all__/layout", json={"positions": {}}, headers=headers)
    assert res.status_code == 403
    assert not any(tmp_path.rglob("*.json"))


def test_layout_write_allows_a_same_origin_request(make_client, tmp_path):
    client, _, _ = make_client()
    res = client.put(
        "/api/repos/__all__/layout",
        json={"positions": {}},
        headers={"host": "127.0.0.1:8765", "origin": "http://127.0.0.1:8765", "sec-fetch-site": "same-origin"},
    )
    assert res.status_code == 200
    assert any(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("headers", _CROSS_SITE, ids=["cross-origin", "cross-site"])
def test_cypher_rejects_a_cross_site_request(make_client, headers):
    client, engine, _ = make_client()
    res = client.post("/api/cypher", json={"query": "RETURN 1"}, headers=headers)
    assert res.status_code == 403
    assert engine.queries == []


def test_cypher_allows_a_same_origin_request(make_client):
    client, engine, _ = make_client()
    res = client.post(
        "/api/cypher",
        json={"query": "RETURN 1"},
        headers={"host": "127.0.0.1:8765", "origin": "http://127.0.0.1:8765", "sec-fetch-site": "same-origin"},
    )
    assert res.status_code == 200
    assert engine.queries == ["RETURN 1"]


def test_origin_matches_host_case_insensitively(make_client):
    # Hostnames are case-insensitive; the Host guard already lowercases, so
    # the same-origin check must not refuse what the guard let through.
    client, engine, _ = make_client()
    res = client.post(
        "/api/cypher",
        json={"query": "RETURN 1"},
        headers={"host": "LocalHost:8765", "origin": "http://localhost:8765"},
    )
    assert res.status_code == 200
    assert engine.queries == ["RETURN 1"]
