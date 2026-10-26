"""Dashboard GET parameters are bounded.

A GET needs no cross-site check, so any page the user visits can request
`<img src="http://127.0.0.1:<port>/api/query-rate?span=...">`. An unbounded
`span` made the agent allocate one bucket per `interval` seconds of it
(gigabytes for a large span), so the route caps the bucket count and span.

No Neo4j needed: query-rate never touches the engine, and the search bound
is checked at the query layer with `ranked_search` stubbed.
"""

import time

import pytest
from fastapi.testclient import TestClient

from devgraph.config.settings import Settings
from devgraph.dashboard import layout_store, queries
from devgraph.dashboard.app import build_app
from devgraph.dashboard.events import EventBroadcaster
from devgraph.registry.store import RepoRegistry


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = Settings(registry_db_path=tmp_path / "registry.sqlite3")
    monkeypatch.setattr(layout_store, "get_settings", lambda: settings)
    registry = RepoRegistry(settings.registry_db_path)
    app = build_app(object(), registry, EventBroadcaster())
    yield TestClient(app, base_url="http://127.0.0.1")
    registry.close()


def test_query_rate_caps_the_bucket_count_for_a_huge_span(client):
    started = time.monotonic()
    res = client.get("/api/query-rate", params={"span": 10**6, "interval": 1})
    assert res.status_code == 200
    buckets = res.json()["buckets"]
    assert 2 <= len(buckets) <= 1440
    assert time.monotonic() - started < 5
    # The span itself is capped to a week: the oldest bucket starts no
    # earlier than seven days ago.
    assert buckets[0]["t"] >= time.time() - 7 * 86400 - 60


def test_query_rate_defaults_are_unchanged(client):
    buckets = client.get("/api/query-rate").json()["buckets"]
    assert len(buckets) == 60
    assert buckets[1]["t"] - buckets[0]["t"] == pytest.approx(60)


def test_query_rate_keeps_a_requested_interval_within_the_cap(client):
    buckets = client.get("/api/query-rate", params={"span": 86400, "interval": 300}).json()["buckets"]
    assert len(buckets) == 288
    assert buckets[1]["t"] - buckets[0]["t"] == pytest.approx(300)


def test_search_max_results_is_capped(monkeypatch):
    from devgraph.mcp import tools

    seen = {}

    def fake_ranked_search(engine, labels, terms, variants, max_results, **kwargs):
        seen["max_results"] = max_results
        return [], 0, False

    monkeypatch.setattr(tools, "ranked_search", fake_ranked_search)
    queries.search_components(object(), "zz-repo", "x", 10**9)
    assert seen["max_results"] == tools.SEARCH_MAX_RESULTS
