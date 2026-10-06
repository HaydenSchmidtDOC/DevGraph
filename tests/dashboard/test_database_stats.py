"""Tests for `GET /api/database-stats` and the JMX heap parser behind it.

Deliberately does NOT use tests/dashboard/test_routes.py's live-Neo4j
fixture: what this endpoint has to get right is the *shape* of a Neo4j 5.26
JMX response and every way that shape can fail to arrive, none of which a
healthy local database will ever produce on demand. A stub engine pins the
exact nested payload the real server returns (the flat shape the dashboard
used to read is the bug being fixed here) and lets the denied/empty/
malformed paths be exercised deterministically.
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from devgraph.config.settings import Settings, get_settings
from devgraph.dashboard.app import build_app
from devgraph.dashboard.db_metrics import (
    JMX_GC_QUERY,
    JMX_MEMORY_QUERY,
    JMX_OS_QUERY,
    PAGECACHE_CONFIG_QUERY,
    heap_from_jmx_rows,
)
from devgraph.dashboard.events import EventBroadcaster
from devgraph.dashboard.routes import build_router

# The literal shape Neo4j 5.26 Community returns for
# CALL dbms.queryJmx("java.lang:type=Memory") YIELD attributes RETURN attributes
# -- each composite attribute is wrapped in {description, value: {properties}},
# trimmed here to the attributes the card reads plus their siblings.
NESTED_5_26_ROWS = [
    {
        "attributes": {
            "ObjectPendingFinalizationCount": {"description": "...", "value": 0},
            "HeapMemoryUsage": {
                "description": "HeapMemoryUsage",
                "value": {
                    "properties": {
                        "committed": 1073741824,
                        "init": 1073741824,
                        "max": 2147483648,
                        "used": 536870912,
                    }
                },
            },
            "NonHeapMemoryUsage": {
                "description": "NonHeapMemoryUsage",
                "value": {"properties": {"committed": 100, "init": 10, "max": -1, "used": 90}},
            },
        }
    }
]


class StubEngine:
    """Stands in for GraphEngine: records the call, returns rows or raises."""

    def __init__(self, rows=None, error=None):
        self.rows = rows
        self.error = error
        self.calls = []

    def run_cypher(self, query, parameters=None):
        self.calls.append((query, parameters))
        if self.error is not None:
            raise self.error
        return self.rows


def make_client(engine):
    app = FastAPI()
    app.include_router(build_router(engine, registry=object(), events=EventBroadcaster()))
    return TestClient(app, base_url="http://127.0.0.1")


def get_heap(engine):
    response = make_client(engine).get("/api/database-stats")
    assert response.status_code == 200
    return response.json()["heap"]


def test_returns_heap_from_the_nested_neo4j_5_26_shape():
    heap = get_heap(StubEngine(rows=NESTED_5_26_ROWS))
    assert heap == {
        "available": True,
        "used_bytes": 536870912,
        "max_bytes": 2147483648,
        "used_percent": 25.0,
    }


def test_flat_value_shape_is_not_accepted_as_a_reading():
    # The shape the dashboard used to read. Reporting it unavailable is what
    # keeps a wrong-but-plausible number off the card.
    rows = [{"attributes": {"HeapMemoryUsage": {"value": {"used": 5, "max": 10}}}}]
    assert get_heap(StubEngine(rows=rows))["available"] is False


def test_queries_are_fixed_and_take_no_parameters():
    engine = StubEngine(rows=NESTED_5_26_ROWS)
    get_heap(engine)
    assert engine.calls == [
        (JMX_MEMORY_QUERY, None),
        (JMX_OS_QUERY, None),
        (JMX_GC_QUERY, None),
        (PAGECACHE_CONFIG_QUERY, None),
    ]
    assert JMX_MEMORY_QUERY == (
        'CALL dbms.queryJmx("java.lang:type=Memory") YIELD attributes RETURN attributes'
    )


def test_endpoint_is_read_only():
    client = make_client(StubEngine(rows=NESTED_5_26_ROWS))
    assert client.post("/api/database-stats", json={}).status_code == 405


def test_percent_is_bounded_and_consistent_with_the_byte_values():
    rows = [{"attributes": {"HeapMemoryUsage": {"value": {"properties": {"used": 999, "max": 1000}}}}}]
    heap = get_heap(StubEngine(rows=rows))
    assert 0 <= heap["used_percent"] <= 100
    assert heap["used_percent"] == 99.9
    full = [{"attributes": {"HeapMemoryUsage": {"value": {"properties": {"used": 8, "max": 8}}}}}]
    assert get_heap(StubEngine(rows=full))["used_percent"] == 100.0


def test_driver_failure_is_unavailable_and_leaks_no_error_text():
    engine = StubEngine(error=RuntimeError("Neo4jError: Unsupported administration command SECRET"))
    response = make_client(engine).get("/api/database-stats")
    assert response.status_code == 200
    body = response.json()
    assert body["heap"] == {"available": False, "used_bytes": None, "max_bytes": None, "used_percent": None}
    assert not any(body[k]["available"] for k in ("system", "gc", "pagecache"))
    assert "Neo4jError" not in response.text and "SECRET" not in response.text


def test_permission_denied_reads_as_unavailable():
    # Neo4j raises rather than returning rows when the role can't call the
    # procedure, and a denied read is the same non-reading as a missing one.
    engine = StubEngine(error=PermissionError("Executing procedure is not allowed for user 'neo4j'"))
    assert get_heap(engine)["available"] is False


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([], id="empty result"),
        pytest.param(None, id="no rows at all"),
        pytest.param([{}], id="row without attributes"),
        pytest.param([{"attributes": None}], id="null attributes"),
        pytest.param([{"attributes": {}}], id="attributes without the memory bean"),
        pytest.param([{"attributes": {"HeapMemoryUsage": {"value": {}}}}], id="value without properties"),
        pytest.param(
            [{"attributes": {"HeapMemoryUsage": {"value": {"properties": {"used": 1}}}}}],
            id="properties missing max",
        ),
        pytest.param(
            [{"attributes": {"HeapMemoryUsage": {"value": {"properties": {"max": 1}}}}}],
            id="properties missing used",
        ),
        pytest.param(
            [{"attributes": {"HeapMemoryUsage": {"value": {"properties": {"used": "536870912", "max": "2147483648"}}}}}],
            id="numbers as strings",
        ),
        pytest.param(
            [{"attributes": {"HeapMemoryUsage": {"value": {"properties": {"used": True, "max": True}}}}}],
            id="booleans are not one byte",
        ),
        pytest.param(
            [{"attributes": {"HeapMemoryUsage": {"value": {"properties": {"used": 1, "max": float("inf")}}}}}],
            id="non-finite max",
        ),
        pytest.param(
            [{"attributes": {"HeapMemoryUsage": {"value": {"properties": {"used": 0, "max": 2147483648}}}}}],
            id="zero used",
        ),
        pytest.param(
            [{"attributes": {"HeapMemoryUsage": {"value": {"properties": {"used": 0, "max": 0}}}}}],
            id="zero max would divide by zero",
        ),
        pytest.param(
            [{"attributes": {"HeapMemoryUsage": {"value": {"properties": {"used": 100, "max": -1}}}}}],
            id="unbounded max reported as -1",
        ),
        pytest.param(
            [{"attributes": {"HeapMemoryUsage": {"value": {"properties": {"used": 4000, "max": 1000}}}}}],
            id="used above max would overfill the meter",
        ),
        pytest.param("not rows at all", id="non-list result"),
        pytest.param(["not a mapping"], id="non-mapping row"),
    ],
)
def test_unusable_jmx_values_report_unavailable(rows):
    assert get_heap(StubEngine(rows=rows)) == {
        "available": False,
        "used_bytes": None,
        "max_bytes": None,
        "used_percent": None,
    }


def test_parser_is_usable_without_the_http_layer():
    assert heap_from_jmx_rows(NESTED_5_26_ROWS)["used_bytes"] == 536870912
    assert heap_from_jmx_rows([])["available"] is False


def test_snapshot_has_every_group_and_store_is_unconfigured_by_default():
    body = make_client(StubEngine(rows=NESTED_5_26_ROWS)).get("/api/database-stats").json()
    assert set(body) == {"ts", "heap", "system", "gc", "pagecache", "store"}
    assert body["store"]["reason"] == "not_configured"
    assert body["pagecache"]["hit_ratio_available"] is False


class RecordingHistory:
    interval_s = 15.0

    def __init__(self):
        self.asked = []

    def since(self, seconds, now=None):
        self.asked.append(seconds)
        return [{"ts": 1.0, "heap_used_bytes": 5, "ram_used_bytes": 6, "process_cpu_load": 0.1, "store_total_bytes": 7}]

    def latest(self):
        raise AssertionError("not used here")


def history_client(history):
    app = FastAPI()
    app.include_router(build_router(StubEngine(rows=[]), registry=object(), events=EventBroadcaster(), metrics=history))
    return TestClient(app)


@pytest.mark.parametrize(("asked", "used"), [(None, 3600), (5, 60), (600, 600), (99999, 3600)])
def test_history_window_is_clamped(asked, used):
    history = RecordingHistory()
    url = "/api/database-stats/history" + ("" if asked is None else f"?seconds={asked}")
    response = history_client(history).get(url)
    assert response.status_code == 200
    assert response.json()["interval_s"] == 15.0
    assert len(response.json()["samples"]) == 1
    assert history.asked == [used]


def test_history_endpoint_is_read_only():
    assert history_client(RecordingHistory()).post("/api/database-stats/history").status_code == 405


def test_blank_data_dir_setting_means_unset():
    assert Settings(_env_file=None, neo4j_data_dir="").neo4j_data_dir is None
    assert Settings(_env_file=None, neo4j_data_dir="  ").neo4j_data_dir is None
    assert Settings(_env_file=None, neo4j_data_dir="/srv/neo4j").neo4j_data_dir == Path("/srv/neo4j")


def test_app_lifespan_runs_the_sampler_with_the_configured_data_dir(tmp_path, monkeypatch):
    (tmp_path / "databases").mkdir()
    (tmp_path / "transactions").mkdir()
    (tmp_path / "databases" / "store").write_bytes(b"x" * 10)
    monkeypatch.setenv("DEVGRAPH_NEO4J_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        app = build_app(StubEngine(rows=NESTED_5_26_ROWS), registry=object(), events=EventBroadcaster())
        with TestClient(app, base_url="http://127.0.0.1") as client:
            assert app.state.metrics.running
            body = client.get("/api/database-stats").json()
            assert body["store"]["total_bytes"] == 10
        assert not app.state.metrics.running
    finally:
        get_settings.cache_clear()
