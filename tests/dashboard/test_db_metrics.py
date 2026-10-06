"""Unit tests for devgraph/dashboard/db_metrics.py.

Fixtures are the shapes stock neo4j:5.26-community returns through the
Python driver (`record.data()`): scalar JMX attributes as
`{"description", "value"}`, composites as `{"value": {"properties": {...}}}`.
A query-keyed stub engine stands in for GraphEngine so the denied, empty and
malformed paths are deterministic.
"""

import os
import stat
import sys
import threading
import time

import pytest

from devgraph.dashboard import db_metrics
from devgraph.dashboard.db_metrics import (
    JMX_GC_QUERY,
    JMX_MEMORY_QUERY,
    JMX_OS_QUERY,
    PAGECACHE_CONFIG_QUERY,
    MetricsHistory,
    collect_snapshot,
    gc_from_jmx_rows,
    pagecache_from_config_rows,
    store_from_data_dir,
    system_from_jmx_rows,
)


def attr(name, value):
    return {"description": name, "value": value}


HEAP_ROWS = [
    {"attributes": {"HeapMemoryUsage": attr("HeapMemoryUsage", {"properties": {"used": 536870912, "max": 2147483648}})}}
]
OS_ROWS = [
    {
        "attributes": {
            "TotalMemorySize": attr("TotalMemorySize", 16_000_000_000),
            "FreeMemorySize": attr("FreeMemorySize", 4_000_000_000),
            "TotalSwapSpaceSize": attr("TotalSwapSpaceSize", 8_000_000_000),
            "FreeSwapSpaceSize": attr("FreeSwapSpaceSize", 6_000_000_000),
            "ProcessCpuLoad": attr("ProcessCpuLoad", 0.05),
            "CpuLoad": attr("CpuLoad", 0.42),
            "SystemCpuLoad": attr("SystemCpuLoad", 0.0),
        }
    }
]
GC_ROWS = [
    {"attributes": {"CollectionCount": attr("CollectionCount", 956), "CollectionTime": attr("CollectionTime", 10262)}},
    {"attributes": {"CollectionCount": attr("CollectionCount", 0), "CollectionTime": attr("CollectionTime", 0)}},
    {"attributes": {"CollectionCount": attr("CollectionCount", 720), "CollectionTime": attr("CollectionTime", 7687)}},
]
CONFIG_ROWS = [{"value": "512.00MiB"}]

ALL_ROWS = {
    JMX_MEMORY_QUERY: HEAP_ROWS,
    JMX_OS_QUERY: OS_ROWS,
    JMX_GC_QUERY: GC_ROWS,
    PAGECACHE_CONFIG_QUERY: CONFIG_ROWS,
}


class QueryStub:
    """GraphEngine stand-in: rows per query, or an exception per query."""

    def __init__(self, responses=None, errors=None):
        self.responses = responses or {}
        self.errors = errors or {}
        self.calls = []

    def run_cypher(self, query, parameters=None):
        self.calls.append((query, parameters))
        if query in self.errors:
            raise self.errors[query]
        return self.responses.get(query, [])


def os_rows_with(**overrides):
    attrs = dict(OS_ROWS[0]["attributes"])
    for name, value in overrides.items():
        if value is None:
            attrs.pop(name, None)
        else:
            attrs[name] = attr(name, value)
    return [{"attributes": attrs}]


# ── system ────────────────────────────────────────────────────────────────


def test_system_reads_ram_swap_and_cpu():
    assert system_from_jmx_rows(OS_ROWS) == {
        "available": True,
        "ram_total_bytes": 16_000_000_000,
        "ram_free_bytes": 4_000_000_000,
        "swap_total_bytes": 8_000_000_000,
        "swap_free_bytes": 6_000_000_000,
        "process_cpu_load": 0.05,
        "system_cpu_load": 0.42,
    }


def test_cpu_load_of_minus_one_means_no_reading_not_a_negative_load():
    system = system_from_jmx_rows(os_rows_with(ProcessCpuLoad=-1.0, CpuLoad=-1.0))
    assert system["available"] is True
    assert system["process_cpu_load"] is None and system["system_cpu_load"] is None


def test_no_swap_is_a_reading_of_zero():
    system = system_from_jmx_rows(os_rows_with(TotalSwapSpaceSize=0, FreeSwapSpaceSize=0))
    assert system["swap_total_bytes"] == 0 and system["swap_free_bytes"] == 0


def test_inconsistent_swap_drops_only_swap():
    system = system_from_jmx_rows(os_rows_with(FreeSwapSpaceSize=9_000_000_000))
    assert system["available"] is True
    assert system["swap_total_bytes"] is None and system["swap_free_bytes"] is None


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param(None, id="no rows"),
        pytest.param([], id="empty"),
        pytest.param([{"attributes": None}], id="null attributes"),
        pytest.param(os_rows_with(TotalMemorySize=None), id="missing total"),
        pytest.param(os_rows_with(FreeMemorySize=None), id="missing free"),
        pytest.param(os_rows_with(TotalMemorySize=0), id="zero total"),
        pytest.param(os_rows_with(FreeMemorySize=17_000_000_000), id="free above total"),
        pytest.param(os_rows_with(TotalMemorySize=True), id="boolean total"),
        pytest.param(os_rows_with(TotalMemorySize="16000000000"), id="string total"),
        pytest.param(os_rows_with(TotalMemorySize=float("nan")), id="nan total"),
    ],
)
def test_unusable_ram_reports_system_unavailable(rows):
    assert system_from_jmx_rows(rows) == {
        "available": False,
        "ram_total_bytes": None,
        "ram_free_bytes": None,
        "swap_total_bytes": None,
        "swap_free_bytes": None,
        "process_cpu_load": None,
        "system_cpu_load": None,
    }


# ── gc ────────────────────────────────────────────────────────────────────


def test_gc_sums_every_collector():
    assert gc_from_jmx_rows(GC_ROWS) == {"available": True, "collection_count": 1676, "collection_time_ms": 17949}


def test_gc_skips_a_collector_that_reports_undefined():
    rows = GC_ROWS + [
        {"attributes": {"CollectionCount": attr("CollectionCount", -1), "CollectionTime": attr("CollectionTime", -1)}}
    ]
    assert gc_from_jmx_rows(rows)["collection_count"] == 1676


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param(None, id="no rows"),
        pytest.param([], id="empty"),
        pytest.param(["not a mapping"], id="non-mapping row"),
        pytest.param(
            [{"attributes": {"CollectionCount": attr("CollectionCount", -1), "CollectionTime": attr("CollectionTime", -1)}}],
            id="only undefined collectors",
        ),
    ],
)
def test_unusable_gc_reports_unavailable(rows):
    assert gc_from_jmx_rows(rows) == {"available": False, "collection_count": None, "collection_time_ms": None}


# ── page cache ────────────────────────────────────────────────────────────


def test_pagecache_reports_the_configured_size_and_never_a_hit_ratio():
    assert pagecache_from_config_rows(CONFIG_ROWS) == {
        "available": True,
        "configured": "512.00MiB",
        "hit_ratio_available": False,
    }


@pytest.mark.parametrize("rows", [None, [], [{"value": None}], [{"value": "  "}], [{"value": 512}]])
def test_unusable_pagecache_config_reports_unavailable(rows):
    assert pagecache_from_config_rows(rows) == {
        "available": False,
        "configured": None,
        "hit_ratio_available": False,
    }


# ── store ─────────────────────────────────────────────────────────────────


def make_data_dir(root):
    (root / "databases" / "neo4j").mkdir(parents=True)
    (root / "databases" / "neo4j" / "neostore").write_bytes(b"x" * 100)
    (root / "databases" / "system").mkdir()
    (root / "databases" / "system" / "neostore").write_bytes(b"x" * 20)
    (root / "transactions" / "neo4j").mkdir(parents=True)
    (root / "transactions" / "neo4j" / "neostore.transaction.db.0").write_bytes(b"x" * 300)
    return root


def test_store_splits_graph_store_from_transaction_logs(tmp_path):
    assert store_from_data_dir(make_data_dir(tmp_path)) == {
        "available": True,
        "reason": None,
        "graph_bytes": 120,
        "tx_log_bytes": 300,
        "total_bytes": 420,
    }


def test_store_without_a_data_dir_is_not_configured():
    store = store_from_data_dir(None)
    assert store["available"] is False and store["reason"] == "not_configured"
    assert store["total_bytes"] is None


def test_store_in_a_directory_that_is_not_neo4j_data_is_unreadable(tmp_path):
    assert store_from_data_dir(tmp_path)["reason"] == "unreadable"
    assert store_from_data_dir(tmp_path / "missing")["reason"] == "unreadable"


def test_store_does_not_follow_or_count_symlinks(tmp_path):
    data = make_data_dir(tmp_path / "data")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x" * 5000)
    (data / "databases" / "neo4j" / "link").symlink_to(outside)
    assert store_from_data_dir(data)["graph_bytes"] == 120


@pytest.mark.skipif(sys.platform == "win32", reason="chmod cannot make a directory unreadable on Windows")
@pytest.mark.skipif(sys.platform != "win32" and os.geteuid() == 0, reason="root can read anything")
def test_unreadable_subdirectory_is_unreadable_not_an_undercount(tmp_path):
    data = make_data_dir(tmp_path)
    locked = data / "databases" / "neo4j"
    locked.chmod(0)
    try:
        store = store_from_data_dir(data)
    finally:
        locked.chmod(stat.S_IRWXU)
    assert store["available"] is False and store["reason"] == "unreadable"


def test_file_rotated_away_mid_walk_is_skipped(tmp_path, monkeypatch):
    data = make_data_dir(tmp_path)
    real_lstat = os.lstat

    def vanishing_lstat(path, *args, **kwargs):
        if str(path).endswith("neostore.transaction.db.0"):
            raise FileNotFoundError(path)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(db_metrics.os, "lstat", vanishing_lstat)
    store = store_from_data_dir(data)
    assert store["available"] is True
    assert store["tx_log_bytes"] == 0 and store["graph_bytes"] == 120


# ── snapshot ──────────────────────────────────────────────────────────────


def test_snapshot_collects_every_group_with_fixed_queries(tmp_path):
    engine = QueryStub(responses=ALL_ROWS)
    snapshot = collect_snapshot(engine, make_data_dir(tmp_path))
    assert set(snapshot) == {"ts", "heap", "system", "gc", "pagecache", "store"}
    assert all(snapshot[k]["available"] for k in ("heap", "system", "gc", "pagecache", "store"))
    assert engine.calls == [
        (JMX_MEMORY_QUERY, None),
        (JMX_OS_QUERY, None),
        (JMX_GC_QUERY, None),
        (PAGECACHE_CONFIG_QUERY, None),
    ]


def test_one_failing_group_does_not_blank_the_others(tmp_path):
    engine = QueryStub(responses=ALL_ROWS, errors={JMX_GC_QUERY: RuntimeError("denied")})
    snapshot = collect_snapshot(engine, make_data_dir(tmp_path))
    assert snapshot["gc"]["available"] is False
    assert snapshot["heap"]["available"] and snapshot["system"]["available"] and snapshot["pagecache"]["available"]


def test_unreachable_neo4j_costs_one_failed_query_and_store_stays_live(tmp_path):
    engine = QueryStub(errors={q: ConnectionError("unreachable") for q in ALL_ROWS})
    snapshot = collect_snapshot(engine, make_data_dir(tmp_path))
    assert engine.calls == [(JMX_MEMORY_QUERY, None)]
    assert not any(snapshot[k]["available"] for k in ("heap", "system", "gc", "pagecache"))
    assert snapshot["store"]["available"] is True


# ── history ───────────────────────────────────────────────────────────────


def fake_collect(ts_values):
    """A collect() that returns full snapshots with the given timestamps in order."""
    it = iter(ts_values)

    def collect(engine, data_dir):
        snap = collect_snapshot(QueryStub(responses=ALL_ROWS), None)
        snap["ts"] = next(it)
        return snap

    return collect


def test_samples_are_compact_and_derive_ram_used():
    history = MetricsHistory(None, None, collect=fake_collect([1000.0]))
    history.sample_once()
    assert history.since(60, now=1000.0) == [
        {
            "ts": 1000.0,
            "heap_used_bytes": 536870912,
            "ram_used_bytes": 12_000_000_000,
            "process_cpu_load": 0.05,
            "store_total_bytes": None,
        }
    ]


def test_since_returns_only_samples_inside_the_window_oldest_first():
    history = MetricsHistory(None, None, collect=fake_collect([100.0, 500.0, 900.0]))
    for _ in range(3):
        history.sample_once()
    assert [s["ts"] for s in history.since(500, now=1000.0)] == [500.0, 900.0]


def test_ring_buffer_keeps_only_the_newest_samples():
    history = MetricsHistory(None, None, max_samples=3, collect=fake_collect([1.0, 2.0, 3.0, 4.0, 5.0]))
    for _ in range(5):
        history.sample_once()
    assert [s["ts"] for s in history.since(10, now=5.0)] == [3.0, 4.0, 5.0]


def test_unavailable_groups_become_none_in_the_compact_sample():
    def collect(engine, data_dir):
        return collect_snapshot(QueryStub(errors={JMX_MEMORY_QUERY: ConnectionError()}), None)

    history = MetricsHistory(None, None, collect=collect)
    history.sample_once()
    (sample,) = history.since(60)
    assert sample["heap_used_bytes"] is None and sample["ram_used_bytes"] is None
    assert sample["process_cpu_load"] is None and sample["store_total_bytes"] is None


def test_latest_samples_fresh_on_every_call_when_the_sampler_is_not_running():
    history = MetricsHistory(None, None, collect=fake_collect([1.0, 2.0]))
    assert history.latest()["ts"] == 1.0
    assert history.latest()["ts"] == 2.0


def test_sampler_thread_samples_survives_a_failing_read_and_stops():
    calls = []
    sampled = threading.Event()

    def collect(engine, data_dir):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        if len(calls) >= 3:
            sampled.set()
        return collect_snapshot(QueryStub(responses=ALL_ROWS), None)

    history = MetricsHistory(None, None, interval_s=0.01, collect=collect)
    history.start()
    try:
        assert history.running
        assert sampled.wait(2), "sampler never recovered from the failing read"
        assert history.latest()["heap"]["available"] is True
    finally:
        history.stop()
    assert not history.running
    settled = len(calls)
    time.sleep(0.05)
    assert len(calls) == settled, "sampler kept running after stop()"
