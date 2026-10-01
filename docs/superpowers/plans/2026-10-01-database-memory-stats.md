# Database & Memory Stats Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the dashboard's Database & memory card into a full live readout (heap, RAM, swap, CPU, GC, configured page cache, on-disk store size) with an in-memory hour of sampled history and sparklines.

**Architecture:** A new `devgraph/dashboard/db_metrics.py` owns every reading: fixed JMX/config queries through `GraphEngine.run_cypher`, a filesystem walk of the Neo4j data directory, and a `MetricsHistory` ring buffer with a 15 s sampler thread. `build_app` starts/stops the sampler via a FastAPI lifespan; `routes.py` serves the latest snapshot and the history. `static/index.html` renders each group independently and draws sparklines on plain canvases.

**Tech Stack:** Python 3.13, FastAPI, pydantic-settings, pytest; hand-written HTML/JS (no build step), Node for headless UI tests.

**Spec:** `docs/superpowers/specs/2026-10-01-database-memory-stats-design.md`

**Working directory for every command:** the repository root (branch `feat/db-memory-stats`, cut from `upstream/master`). Run tests with `uv run pytest ...`.

## Global Constraints

- Neo4j target: `docker.io/library/neo4j:5.26-community`, stock (no APOC, no plugins, no custom image).
- Every value on the card is a real reading or explicitly unavailable with a reason — never estimated.
- Every Neo4j query is a fixed literal with no parameters; no caller input ever reaches one.
- `/api/database-stats` keeps its existing `heap` key and shape: `{"available", "used_bytes", "max_bytes", "used_percent"}`.
- `/api/database-stats` is always HTTP 200 and never contains driver error text.
- Sampler interval 15 s, ring buffer 240 samples (one hour); history `seconds` clamped to 60–3600.
- New setting: `neo4j_data_dir: Path | None = None` (`DEVGRAPH_NEO4J_DATA_DIR`); blank means unset.
- No new Python or JS dependencies.
- Commit messages: plain imperative summary; no `Co-Authored-By` trailer and no mention of Claude/the assistant (repo `CLAUDE.md`).

## Review Focus

1. Neo4j unreachable while the browser asks for stats → the request returns after at most one driver failure, not four (heap failure short-circuits the other Neo4j queries) — pinned in Task 1.
2. Neo4j rotates a transaction log while the store is being walked (file vanishes) → that file is skipped and store size stays available — pinned in Task 1.
3. Data directory unreadable (permissions) → store size reads "unreadable", never a silent undercount — pinned in Task 1.
4. `DEVGRAPH_NEO4J_DATA_DIR=` (blank) in an env file → treated as unset, not as the current directory — pinned in Task 3.
5. History with gaps (Neo4j down for some samples) → sparkline breaks at the gap instead of drawing a line across missing data — pinned in Task 4.

---

## File Structure

| File | Responsibility |
| :--- | :--- |
| `devgraph/dashboard/db_metrics.py` (create) | All parsing, the store walk, `collect_snapshot`, `MetricsHistory` |
| `devgraph/dashboard/routes.py` (modify) | Remove heap parser (moved); serve snapshot + history from a `MetricsHistory` |
| `devgraph/dashboard/app.py` (modify) | Create `MetricsHistory`, run it under a lifespan, expose on `app.state.metrics` |
| `devgraph/config/settings.py` (modify) | `neo4j_data_dir` setting |
| `devgraph/dashboard/static/index.html` (modify) | Card markup, CSS for sparklines, render/poll JS |
| `deploy/docker-compose.yml`, `deploy/podman-compose.yml` (modify) | Data-volume mount / setting guidance |
| `README.md`, `PROJECT_STATUS.md` (modify) | Docs |
| `tests/dashboard/test_db_metrics.py` (create) | Unit tests for `db_metrics.py` |
| `tests/dashboard/test_database_stats.py` (modify) | Route + app lifespan tests |
| `tests/dashboard/database_stats_ui.js` (rewrite) | Headless UI checks |

---

### Task 1: Readings and snapshot (`db_metrics.py`)

**Files:**
- Create: `devgraph/dashboard/db_metrics.py`
- Test: `tests/dashboard/test_db_metrics.py`

**Interfaces:**
- Consumes: `engine.run_cypher(query: str, parameters: dict | None = None) -> list[dict]` (from `devgraph/graph/engine.py`).
- Produces (module-level, public):
  - Constants `JMX_MEMORY_QUERY`, `JMX_OS_QUERY`, `JMX_GC_QUERY`, `PAGECACHE_CONFIG_QUERY: str`
  - `heap_unavailable() -> dict`, `heap_from_jmx_rows(rows) -> dict`
  - `system_from_jmx_rows(rows) -> dict` — keys `available, ram_total_bytes, ram_free_bytes, swap_total_bytes, swap_free_bytes, process_cpu_load, system_cpu_load`
  - `gc_from_jmx_rows(rows) -> dict` — keys `available, collection_count, collection_time_ms`
  - `pagecache_from_config_rows(rows) -> dict` — keys `available, configured, hit_ratio_available`
  - `store_from_data_dir(data_dir: Path | None) -> dict` — keys `available, reason, graph_bytes, tx_log_bytes, total_bytes`; `reason` ∈ `None | "not_configured" | "unreadable"`
  - `collect_snapshot(engine, data_dir: Path | None) -> dict` — keys `ts, heap, system, gc, pagecache, store`

- [ ] **Step 1: Confirm the driver-side row shapes against the live container**

Neo4j must be running locally (the dev container is `devgraph-neo4j`; check with `podman ps`). Run:

```bash
uv run python -c "
from devgraph.config.settings import get_settings
from devgraph.graph.engine import GraphEngine
s = get_settings()
e = GraphEngine(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
os_rows = e.run_cypher('CALL dbms.queryJmx(\"java.lang:type=OperatingSystem\") YIELD attributes RETURN attributes')
print(os_rows[0]['attributes']['TotalMemorySize'])
print(len(e.run_cypher('CALL dbms.queryJmx(\"java.lang:type=GarbageCollector,*\") YIELD attributes RETURN attributes')))
print(e.run_cypher('CALL dbms.listConfig() YIELD name, value WHERE name = \"server.memory.pagecache.size\" RETURN value'))
"
```

Expected (numbers will differ): `{'description': 'TotalMemorySize', 'value': 16665366528}`, then `3`, then `[{'value': '512.00MiB'}]`. If `GraphEngine`'s constructor signature differs, read `devgraph/graph/engine.py` and adapt the one-liner only. If the shapes differ from these, stop and report — the fixtures below assume them.

- [ ] **Step 2: Write the failing tests**

Create `tests/dashboard/test_db_metrics.py`:

```python
"""Unit tests for devgraph/dashboard/db_metrics.py.

Fixtures are the shapes stock neo4j:5.26-community returns through the
Python driver (`record.data()`): scalar JMX attributes as
`{"description", "value"}`, composites as `{"value": {"properties": {...}}}`.
A query-keyed stub engine stands in for GraphEngine so the denied, empty and
malformed paths are deterministic.
"""

import os
import stat

import pytest

from devgraph.dashboard import db_metrics
from devgraph.dashboard.db_metrics import (
    JMX_GC_QUERY,
    JMX_MEMORY_QUERY,
    JMX_OS_QUERY,
    PAGECACHE_CONFIG_QUERY,
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


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/dashboard/test_db_metrics.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'devgraph.dashboard.db_metrics'`.

- [ ] **Step 4: Implement `db_metrics.py` (readings + snapshot)**

Create `devgraph/dashboard/db_metrics.py`. The heap functions are moved verbatim from `devgraph/dashboard/routes.py` (lines ~54–121), renamed without the leading underscore; `routes.py` itself is changed in Task 3.

```python
"""Readings behind the dashboard's Database & memory card.

Every value comes from a fixed, parameterless query against Neo4j or from a
walk of the Neo4j data directory -- nothing is estimated. Each group carries
its own `available` flag so one missing source (a denied procedure, an unset
data directory) never blanks the others.

Verified against stock neo4j:5.26-community, which exposes only the JVM's own
java.lang JMX beans: no org.neo4j beans and no metrics endpoint (that is
Enterprise). Page cache hit ratio therefore has no source; only the
configured page cache size is reported.
"""

from __future__ import annotations

import logging
import math
import os
import stat
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Fixed literals so no caller input can ever steer a query. dbms.queryJmx and
# dbms.listConfig are both read-only procedures.
JMX_MEMORY_QUERY = 'CALL dbms.queryJmx("java.lang:type=Memory") YIELD attributes RETURN attributes'
JMX_OS_QUERY = 'CALL dbms.queryJmx("java.lang:type=OperatingSystem") YIELD attributes RETURN attributes'
JMX_GC_QUERY = 'CALL dbms.queryJmx("java.lang:type=GarbageCollector,*") YIELD attributes RETURN attributes'
PAGECACHE_CONFIG_QUERY = (
    'CALL dbms.listConfig() YIELD name, value '
    'WHERE name = "server.memory.pagecache.size" RETURN value'
)


def jmx_number(value: Any) -> float | None:
    """A JMX attribute as a real number, or None if it isn't usable.

    `bool` is an `int` in Python, and a JSON `true` reaching an arithmetic
    path would silently become 1 byte, so it is rejected explicitly.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _first_attributes(rows: Any) -> dict[str, Any] | None:
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[0]
    if not isinstance(row, dict):
        return None
    attrs = row.get("attributes")
    return attrs if isinstance(attrs, dict) else None


def _scalar_attr(attrs: dict[str, Any], name: str) -> float | None:
    """A scalar JMX attribute, which Neo4j 5.26 wraps as {description, value}."""
    entry = attrs.get(name)
    if not isinstance(entry, dict):
        return None
    return jmx_number(entry.get("value"))


# ── heap (moved from routes.py, PR #19) ──────────────────────────────────


def heap_unavailable() -> dict[str, Any]:
    """The one shape the card renders as dashed/"unavailable"."""
    return {"available": False, "used_bytes": None, "max_bytes": None, "used_percent": None}


def heap_from_jmx_rows(rows: Any) -> dict[str, Any]:
    """Heap used/max out of `JMX_MEMORY_QUERY`'s rows, or the unavailable shape.

    Neo4j 5.26 returns each JMX composite attribute nested as
    `attributes.HeapMemoryUsage.value.properties.{used,max}` -- the flat
    `...value.used` shape the dashboard used to read never matches, which is
    why the card never went live. Anything that isn't that exact shape with
    two usable numbers (denied procedure, empty result, a future/other
    layout, a non-numeric or non-finite value) is reported as unavailable
    rather than guessed at.

    Zero is treated as unusable, not as a value: a running JVM reports
    neither zero heap used nor zero heap max, so a zero here means the
    attribute wasn't populated. `used > max` is likewise rejected -- a meter
    past 100% is a misread, not a reading. Because both are rejected,
    `used_percent` is always within 0-100 by construction.
    """
    if not isinstance(rows, list) or not rows:
        return heap_unavailable()
    row = rows[0]
    if not isinstance(row, dict):
        return heap_unavailable()
    node: Any = row
    for key in ("attributes", "HeapMemoryUsage", "value", "properties"):
        if not isinstance(node, dict):
            return heap_unavailable()
        node = node.get(key)
    if not isinstance(node, dict):
        return heap_unavailable()

    used = jmx_number(node.get("used"))
    heap_max = jmx_number(node.get("max"))
    if used is None or heap_max is None:
        return heap_unavailable()
    if used <= 0 or heap_max <= 0 or used > heap_max:
        return heap_unavailable()
    return {
        "available": True,
        "used_bytes": int(used),
        "max_bytes": int(heap_max),
        "used_percent": round(used / heap_max * 100, 1),
    }


# ── system ───────────────────────────────────────────────────────────────


def system_unavailable() -> dict[str, Any]:
    return {
        "available": False,
        "ram_total_bytes": None,
        "ram_free_bytes": None,
        "swap_total_bytes": None,
        "swap_free_bytes": None,
        "process_cpu_load": None,
        "system_cpu_load": None,
    }


def _load(attrs: dict[str, Any], name: str) -> float | None:
    """A 0-1 CPU load fraction; the JVM reports -1 when it has none."""
    value = _scalar_attr(attrs, name)
    if value is None or not 0.0 <= value <= 1.0:
        return None
    return value


def system_from_jmx_rows(rows: Any) -> dict[str, Any]:
    """RAM, swap and CPU from java.lang:type=OperatingSystem.

    RAM is the group's anchor: without a consistent total/free pair the
    whole group is unavailable. Swap and CPU are dropped individually when
    unusable. CPU uses `CpuLoad` (system) and `ProcessCpuLoad`; the older
    `SystemCpuLoad` is deprecated and reads 0.0 on current JDKs.
    """
    attrs = _first_attributes(rows)
    if attrs is None:
        return system_unavailable()
    total = _scalar_attr(attrs, "TotalMemorySize")
    free = _scalar_attr(attrs, "FreeMemorySize")
    if total is None or free is None or total <= 0 or free < 0 or free > total:
        return system_unavailable()
    swap_total = _scalar_attr(attrs, "TotalSwapSpaceSize")
    swap_free = _scalar_attr(attrs, "FreeSwapSpaceSize")
    if swap_total is None or swap_free is None or swap_total < 0 or swap_free < 0 or swap_free > swap_total:
        swap_total = swap_free = None
    return {
        "available": True,
        "ram_total_bytes": int(total),
        "ram_free_bytes": int(free),
        "swap_total_bytes": None if swap_total is None else int(swap_total),
        "swap_free_bytes": None if swap_free is None else int(swap_free),
        "process_cpu_load": _load(attrs, "ProcessCpuLoad"),
        "system_cpu_load": _load(attrs, "CpuLoad"),
    }


# ── gc ───────────────────────────────────────────────────────────────────


def gc_unavailable() -> dict[str, Any]:
    return {"available": False, "collection_count": None, "collection_time_ms": None}


def gc_from_jmx_rows(rows: Any) -> dict[str, Any]:
    """Collection count and time summed across every garbage collector.

    A collector reporting -1 (undefined) is skipped; at least one usable
    collector is required.
    """
    if not isinstance(rows, list):
        return gc_unavailable()
    count = time_ms = 0.0
    usable = 0
    for row in rows:
        attrs = row.get("attributes") if isinstance(row, dict) else None
        if not isinstance(attrs, dict):
            continue
        c = _scalar_attr(attrs, "CollectionCount")
        t = _scalar_attr(attrs, "CollectionTime")
        if c is None or t is None or c < 0 or t < 0:
            continue
        count += c
        time_ms += t
        usable += 1
    if not usable:
        return gc_unavailable()
    return {"available": True, "collection_count": int(count), "collection_time_ms": int(time_ms)}


# ── page cache ───────────────────────────────────────────────────────────


def pagecache_from_config_rows(rows: Any) -> dict[str, Any]:
    """The configured page cache size, as Neo4j reports it (e.g. "512.00MiB")."""
    value = None
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        raw = rows[0].get("value")
        if isinstance(raw, str) and raw.strip():
            value = raw.strip()
    return {"available": value is not None, "configured": value, "hit_ratio_available": False}


# ── store ────────────────────────────────────────────────────────────────


def store_unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason, "graph_bytes": None, "tx_log_bytes": None, "total_bytes": None}


def _raise(error: OSError) -> None:
    raise error


def _tree_bytes(root: Path) -> int:
    """Total size of the regular files under root.

    Symlinks are neither followed nor counted. A file that disappears between
    listing and stat (Neo4j rotating a transaction log) is skipped; any other
    error propagates, so an unreadable directory can't become an undercount.
    """
    total = 0
    for dirpath, _dirs, files in os.walk(root, onerror=_raise):
        for name in files:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except FileNotFoundError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
    return total


def store_from_data_dir(data_dir: Path | None) -> dict[str, Any]:
    """Graph store (`databases/`) and transaction logs (`transactions/`) on disk."""
    if data_dir is None:
        return store_unavailable("not_configured")
    databases = data_dir / "databases"
    transactions = data_dir / "transactions"
    try:
        if not databases.is_dir() or not transactions.is_dir():
            return store_unavailable("unreadable")
        graph = _tree_bytes(databases)
        tx_logs = _tree_bytes(transactions)
    except OSError as exc:
        logger.debug("Neo4j data directory unreadable: %s", exc)
        return store_unavailable("unreadable")
    return {
        "available": True,
        "reason": None,
        "graph_bytes": graph,
        "tx_log_bytes": tx_logs,
        "total_bytes": graph + tx_logs,
    }


# ── snapshot ─────────────────────────────────────────────────────────────


def collect_snapshot(engine: Any, data_dir: Path | None) -> dict[str, Any]:
    """One reading of every group.

    The heap query goes first; if it raises, Neo4j is treated as unreachable
    for this snapshot and the other queries are skipped, so a down database
    costs one driver failure rather than four.
    """
    snapshot: dict[str, Any] = {"ts": time.time()}
    try:
        heap_rows = engine.run_cypher(JMX_MEMORY_QUERY)
    except Exception as exc:  # neo4j driver raises its own exception hierarchy
        logger.debug("JMX heap query unavailable: %s", exc)
        snapshot.update(
            heap=heap_unavailable(),
            system=system_unavailable(),
            gc=gc_unavailable(),
            pagecache=pagecache_from_config_rows(None),
        )
    else:
        snapshot["heap"] = heap_from_jmx_rows(heap_rows)
        snapshot["system"] = system_from_jmx_rows(_rows_or_none(engine, JMX_OS_QUERY))
        snapshot["gc"] = gc_from_jmx_rows(_rows_or_none(engine, JMX_GC_QUERY))
        snapshot["pagecache"] = pagecache_from_config_rows(_rows_or_none(engine, PAGECACHE_CONFIG_QUERY))
    snapshot["store"] = store_from_data_dir(data_dir)
    return snapshot


def _rows_or_none(engine: Any, query: str) -> Any:
    try:
        return engine.run_cypher(query)
    except Exception as exc:  # neo4j driver raises its own exception hierarchy
        logger.debug("database stats query unavailable (%s): %s", query, exc)
        return None
```

`heap_unavailable`, `jmx_number` and `heap_from_jmx_rows` are `routes.py`'s `_heap_unavailable`, `_jmx_number` and `_heap_from_jmx_rows` moved without the underscore. Do not touch `routes.py` yet — Task 3 deletes the originals.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/dashboard/test_db_metrics.py -q`
Expected: all pass (the chmod test is skipped only when running as root).

- [ ] **Step 6: Commit**

```bash
git add devgraph/dashboard/db_metrics.py tests/dashboard/test_db_metrics.py
git commit -m "Add Neo4j memory, CPU, GC and store-size readings"
```

---

### Task 2: Sampled history (`MetricsHistory`)

**Files:**
- Modify: `devgraph/dashboard/db_metrics.py` (append)
- Test: `tests/dashboard/test_db_metrics.py` (append)

**Interfaces:**
- Consumes: `collect_snapshot(engine, data_dir) -> dict` (Task 1).
- Produces:
  - `class MetricsHistory(engine, data_dir: Path | None, *, interval_s: float = 15.0, max_samples: int = 240, collect=collect_snapshot)`
  - `.interval_s: float`, `.running: bool` (property)
  - `.sample_once() -> dict` (full snapshot), `.latest() -> dict` (full snapshot), `.since(seconds: float, now: float | None = None) -> list[dict]`
  - `.start() -> None`, `.stop() -> None`
  - Compact sample keys: `ts, heap_used_bytes, ram_used_bytes, process_cpu_load, store_total_bytes` (each value `None` when its group is unavailable).

- [ ] **Step 1: Write the failing tests**

In `tests/dashboard/test_db_metrics.py`, add `import threading` and `import time` to the top import block and `MetricsHistory` to the `from devgraph.dashboard.db_metrics import (...)` list. Then append:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/dashboard/test_db_metrics.py -q`
Expected: `ImportError: cannot import name 'MetricsHistory'`.

- [ ] **Step 3: Implement `MetricsHistory`**

Add `import threading` and `from collections import deque` to the imports of `devgraph/dashboard/db_metrics.py`, then append:

```python
# ── history ──────────────────────────────────────────────────────────────

_HISTORY_INTERVAL_S = 15.0
_HISTORY_MAX_SAMPLES = 240  # one hour at the default interval


def _compact(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The few numbers the sparklines plot, None where a group is unavailable."""
    system = snapshot["system"]
    ram_used = system["ram_total_bytes"] - system["ram_free_bytes"] if system["available"] else None
    return {
        "ts": snapshot["ts"],
        "heap_used_bytes": snapshot["heap"]["used_bytes"],
        "ram_used_bytes": ram_used,
        "process_cpu_load": system["process_cpu_load"],
        "store_total_bytes": snapshot["store"]["total_bytes"],
    }


class MetricsHistory:
    """In-memory ring buffer of snapshots, filled by a daemon sampler thread.

    Process-local and lost on restart by design, like query_log.py. While the
    sampler isn't running (tests, or before the app's lifespan starts it),
    `latest()` takes a fresh reading on every call rather than serving a
    cached one that would never update.
    """

    def __init__(
        self,
        engine: Any,
        data_dir: Path | None,
        *,
        interval_s: float = _HISTORY_INTERVAL_S,
        max_samples: int = _HISTORY_MAX_SAMPLES,
        collect=collect_snapshot,
    ) -> None:
        self.interval_s = interval_s
        self._engine = engine
        self._data_dir = data_dir
        self._collect = collect
        self._samples: deque[dict[str, Any]] = deque(maxlen=max_samples)
        self._latest: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None

    def sample_once(self) -> dict[str, Any]:
        snapshot = self._collect(self._engine, self._data_dir)
        with self._lock:
            self._latest = snapshot
            self._samples.append(_compact(snapshot))
        return snapshot

    def latest(self) -> dict[str, Any]:
        with self._lock:
            latest = self._latest
        if latest is None or not self.running:
            return self.sample_once()
        return latest

    def since(self, seconds: float, now: float | None = None) -> list[dict[str, Any]]:
        cutoff = (time.time() if now is None else now) - seconds
        with self._lock:
            return [s for s in self._samples if s["ts"] >= cutoff]

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="devgraph-db-metrics", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            # A sample blocked on an unreachable database can outlast this;
            # the thread is a daemon, so shutdown is never held hostage.
            thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample_once()
            except Exception:
                logger.debug("database stats sample failed", exc_info=True)
            self._stop.wait(self.interval_s)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/dashboard/test_db_metrics.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add devgraph/dashboard/db_metrics.py tests/dashboard/test_db_metrics.py
git commit -m "Sample database stats into an in-memory history"
```

---

### Task 3: Setting, routes, app lifespan, deploy and docs

**Files:**
- Modify: `devgraph/config/settings.py`
- Modify: `devgraph/dashboard/routes.py` (imports; delete lines ~54–121 heap helpers; `build_router` signature; `/database-stats` handler ~405–425; add history route)
- Modify: `devgraph/dashboard/app.py`
- Modify: `deploy/docker-compose.yml`, `deploy/podman-compose.yml`
- Modify: `README.md` (Dashboard section), `PROJECT_STATUS.md` (dashboard bullets)
- Test: `tests/dashboard/test_database_stats.py`

**Interfaces:**
- Consumes: `MetricsHistory`, `JMX_*`/`PAGECACHE_CONFIG_QUERY`, `heap_from_jmx_rows` (Tasks 1–2).
- Produces:
  - `Settings.neo4j_data_dir: Path | None`
  - `build_router(engine, registry, events, query_log=None, metrics: MetricsHistory | None = None)`
  - `GET /api/database-stats` → snapshot dict (`ts, heap, system, gc, pagecache, store`)
  - `GET /api/database-stats/history?seconds=N` → `{"interval_s": float, "samples": [compact...]}`
  - `app.state.metrics: MetricsHistory` on the app from `build_app`

- [ ] **Step 1: Update and extend the route tests**

In `tests/dashboard/test_database_stats.py`:

1. Replace the import line `from devgraph.dashboard.routes import _JMX_MEMORY_QUERY, _heap_from_jmx_rows, build_router` with:

```python
from devgraph.config.settings import Settings, get_settings
from devgraph.dashboard.app import build_app
from devgraph.dashboard.db_metrics import (
    JMX_GC_QUERY,
    JMX_MEMORY_QUERY,
    JMX_OS_QUERY,
    PAGECACHE_CONFIG_QUERY,
    heap_from_jmx_rows,
)
from devgraph.dashboard.routes import build_router
```

2. Replace `test_query_is_fixed_and_takes_no_parameters` with:

```python
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
```

3. Replace `test_driver_failure_is_unavailable_and_leaks_no_error_text` with:

```python
def test_driver_failure_is_unavailable_and_leaks_no_error_text():
    engine = StubEngine(error=RuntimeError("Neo4jError: Unsupported administration command SECRET"))
    response = make_client(engine).get("/api/database-stats")
    assert response.status_code == 200
    body = response.json()
    assert body["heap"] == {"available": False, "used_bytes": None, "max_bytes": None, "used_percent": None}
    assert not any(body[k]["available"] for k in ("system", "gc", "pagecache"))
    assert "Neo4jError" not in response.text and "SECRET" not in response.text
```

4. In `test_parser_is_usable_without_the_http_layer`, rename `_heap_from_jmx_rows` to `heap_from_jmx_rows` (two occurrences).

5. Append:

```python
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
    assert str(Settings(_env_file=None, neo4j_data_dir="/srv/neo4j").neo4j_data_dir) == "/srv/neo4j"


def test_app_lifespan_runs_the_sampler_with_the_configured_data_dir(tmp_path, monkeypatch):
    (tmp_path / "databases").mkdir()
    (tmp_path / "transactions").mkdir()
    (tmp_path / "databases" / "store").write_bytes(b"x" * 10)
    monkeypatch.setenv("DEVGRAPH_NEO4J_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        app = build_app(StubEngine(rows=NESTED_5_26_ROWS), registry=object(), events=EventBroadcaster())
        with TestClient(app) as client:
            assert app.state.metrics.running
            body = client.get("/api/database-stats").json()
            assert body["store"]["total_bytes"] == 10
        assert not app.state.metrics.running
    finally:
        get_settings.cache_clear()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/dashboard/test_database_stats.py -q`
Expected: failures/errors — `Settings` has no `neo4j_data_dir`, `build_router` has no `metrics`, `app.state.metrics` missing.

- [ ] **Step 3: Add the setting**

In `devgraph/config/settings.py`, change `from pydantic import Field` to `from pydantic import Field, field_validator`, and add after `neo4j_password`:

```python
    # Neo4j's data directory as this process can see it (a read-only mount
    # of the data volume, or the volume's host path). Optional: without it
    # the dashboard reports store size as unavailable.
    neo4j_data_dir: Path | None = None
```

and, inside the class after the last field:

```python
    @field_validator("neo4j_data_dir", mode="before")
    @classmethod
    def _blank_data_dir_is_unset(cls, value: object) -> object:
        # Path("") is the current directory, which would read as an
        # "unreadable" data dir rather than an unset one.
        if isinstance(value, str) and not value.strip():
            return None
        return value
```

(If `Field` is unused after this, leave the import as it was — don't touch unrelated imports.)

- [ ] **Step 4: Wire `routes.py`**

1. Delete from `routes.py` the block from the comment above `_JMX_MEMORY_QUERY` through the end of `_heap_from_jmx_rows` (the `_JMX_MEMORY_QUERY`, `_heap_unavailable`, `_jmx_number`, `_heap_from_jmx_rows` definitions). Remove `import math` if `grep -n 'math\.' devgraph/dashboard/routes.py` then shows no remaining use.
2. Add the import `from devgraph.dashboard.db_metrics import MetricsHistory` beside the other `devgraph.dashboard` imports.
3. Add a module constant next to `_SSE_KEEPALIVE_S`:

```python
# History window bounds for `/database-stats/history`: at least one minute,
# at most what the in-memory ring buffer holds (one hour).
_HISTORY_SECONDS_MIN = 60
_HISTORY_SECONDS_MAX = 3600
```

4. Change `build_router`'s signature and its first lines:

```python
def build_router(
    engine: GraphEngine,
    registry: RepoRegistry,
    events: EventBroadcaster,
    query_log: QueryLog | None = None,
    metrics: MetricsHistory | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api")
    query_log = query_log if query_log is not None else QueryLog()
    # Not started here: `build_app` runs the sampler under the app lifespan.
    # Unstarted, it takes a fresh reading per request.
    metrics = metrics if metrics is not None else MetricsHistory(engine, None)
```

5. Replace the whole `database_stats` handler with:

```python
    @router.get("/database-stats")
    def database_stats() -> dict[str, Any]:
        """Latest Database & memory snapshot (see dashboard/db_metrics.py).

        Read-only and fixed-query. Always 200, never driver error text: an
        unreachable database, a missing procedure, or a denied role are all
        the same thing to the card -- no reading -- and each group says so
        through its own `available` flag.
        """
        return metrics.latest()

    @router.get("/database-stats/history")
    def database_stats_history(seconds: int = _HISTORY_SECONDS_MAX) -> dict[str, Any]:
        window = max(_HISTORY_SECONDS_MIN, min(seconds, _HISTORY_SECONDS_MAX))
        return {"interval_s": metrics.interval_s, "samples": metrics.since(window)}
```

- [ ] **Step 5: Run the lifespan in `app.py`**

Replace `devgraph/dashboard/app.py`'s imports and `build_app` with:

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from devgraph.config.settings import get_settings
from devgraph.dashboard.db_metrics import MetricsHistory
from devgraph.dashboard.events import EventBroadcaster
from devgraph.dashboard.query_log import QueryLog
from devgraph.dashboard.routes import build_router
from devgraph.graph.engine import GraphEngine
from devgraph.registry.store import RepoRegistry

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def build_app(engine: GraphEngine, registry: RepoRegistry, events: EventBroadcaster) -> FastAPI:
    metrics = MetricsHistory(engine, get_settings().neo4j_data_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Sampling lives exactly as long as the server, for the tray and the
        # headless agent alike.
        metrics.start()
        try:
            yield
        finally:
            metrics.stop()

    app = FastAPI(title="DevGraph Dashboard", lifespan=lifespan)
    app.state.metrics = metrics
    app.include_router(build_router(engine, registry, events, QueryLog(), metrics))
    # Hand-written HTML/CSS/JS, no build step -- StaticFiles serves them
    # as-is (see Implementation Plan #5: no frontend framework in v1).
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(_STATIC_DIR / "index.html"))

    return app
```

Keep the module docstring at the top unchanged.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/dashboard/test_database_stats.py tests/dashboard/test_db_metrics.py -q`
Expected: all pass.

- [ ] **Step 7: Deploy files**

In `deploy/docker-compose.yml`, in the `devgraph` service add to `environment:`:

```yaml
      # Read-only view of Neo4j's data volume, for the dashboard's store-size reading.
      DEVGRAPH_NEO4J_DATA_DIR: "/neo4j-data"
```

and to its `volumes:` (above the "Mount each repo" comment):

```yaml
      - devgraph_neo4j_data:/neo4j-data:ro
```

In `deploy/podman-compose.yml`, append to the header comment block (before `version:`):

```yaml
#
# The DevGraph agent runs on the host (tray app), not in this file. To show
# store size on the dashboard, point it at this volume's host path:
#   DEVGRAPH_NEO4J_DATA_DIR=$(podman volume inspect devgraph_neo4j_data --format '{{.Mountpoint}}')
```

- [ ] **Step 8: Docs**

In `README.md`, append to the Dashboard section's second paragraph (after the `DEVGRAPH_DASHBOARD_PORT` sentence):

```markdown
The Database & memory card samples Neo4j every 15 seconds and keeps the last hour in memory: JVM heap, system RAM and swap, CPU, garbage collection, and the configured page cache size, all read through read-only JMX/config procedures. Store size on disk (graph store and transaction logs) needs `DEVGRAPH_NEO4J_DATA_DIR` pointing at Neo4j's data directory as DevGraph can see it; the Docker compose stack sets this, and with Podman it is the output of `podman volume inspect devgraph_neo4j_data --format '{{.Mountpoint}}'`. Page cache hit ratio is not shown: Neo4j Community exposes no source for it.
```

In `PROJECT_STATUS.md`, in the dashboard-shipped bullet (the one that begins "Implementation Plan #5 (live web dashboard) shipped"), append before its last sentence ("The force-directed graph settle…"):

```markdown
The Database & memory card is backed by `dashboard/db_metrics.py`: a 15-second sampler (one hour in memory) reading heap, RAM, swap, CPU and GC from the JVM's own JMX beans, the configured page cache size from `dbms.listConfig`, and store size from a walk of `DEVGRAPH_NEO4J_DATA_DIR`; each group is independently live or labelled unavailable, and page cache hit ratio has no source on Community.
```

and in the `devgraph/dashboard/` bullet under the code map, after `in-memory query telemetry for the dashboard's own Cypher console (\`query_log.py\`),` insert `database and memory readings plus their sampled history (\`db_metrics.py\`),`.

- [ ] **Step 9: Run the full dashboard suite**

Run: `uv run pytest tests/dashboard -q`
Expected: all pass (`database_stats_ui.js` still passes at this point — the UI is untouched).

- [ ] **Step 10: Commit**

```bash
git add devgraph/config/settings.py devgraph/dashboard/routes.py devgraph/dashboard/app.py \
  tests/dashboard/test_database_stats.py deploy/docker-compose.yml deploy/podman-compose.yml \
  README.md PROJECT_STATUS.md
git commit -m "Serve sampled database stats and history from the dashboard"
```

---

### Task 4: Dashboard card, polling and sparklines

**Files:**
- Modify: `devgraph/dashboard/static/index.html` (CSS near line ~330; card markup ~574–586; JS `setMemNote`/`setHeapUnavailable`/`attemptMemoryMetrics` ~3214–3255; `bootConnect` ~2600–2630)
- Rewrite: `tests/dashboard/database_stats_ui.js`
- Unchanged runner: `tests/dashboard/test_database_stats_ui.py`

**Interfaces:**
- Consumes: `GET /api/database-stats` snapshot and `GET /api/database-stats/history?seconds=3600` (Task 3).
- Produces (top-level JS in `index.html`): `MEM_POLL_MS`, `memPollTimer`, `STORE_REASONS`, `formatMemBytes(n)`, `setMemNote(text)`, `renderHeap(heap)`, `renderSystem(sys)`, `renderGc(gc)`, `renderPagecache(pc)`, `renderStore(store)`, `setMemUnavailable(why)`, `attemptMemoryMetrics(opts)`, `renderSpark(id, values)`, `attemptMemoryHistory()`, `startMemoryPolling()`. `setHeapUnavailable` is removed.

- [ ] **Step 1: Rewrite the headless UI test**

Replace `tests/dashboard/database_stats_ui.js` entirely with:

```js
/* Headless test of the Database & memory card, lifted verbatim out of
   index.html: the render/poll functions plus the bootConnect branch that
   decides how the card is first read. They reach only fetch, runCypher,
   setInterval and document.getElementById, so a handful of stubs exercises
   the real functions -- no browser, deterministic, re-runnable.

   What this exists to catch: a real reading that never reaches the card,
   and a partial or fabricated reading that does. Each group renders on its
   own, so one group's failure must not dash the others, and none may show a
   value it can't stand behind. */
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.join(__dirname, "..", "..", "devgraph", "dashboard", "static", "index.html"), "utf8");

const grab = (startRe, endMarker) => {
  const i = html.search(startRe);
  if (i < 0) throw new Error("could not find " + startRe);
  const j = html.indexOf(endMarker, i);
  if (j < 0) throw new Error("could not find end marker after " + startRe);
  return html.slice(i, j + endMarker.length);
};
const src = [
  grab(/^const MEM_POLL_MS\b/m, ";"),
  grab(/^let memPollTimer\b/m, ";"),
  grab(/^const STORE_REASONS\b/m, "};"),
  grab(/^function formatMemBytes\(/m, "\n}"),
  grab(/^function setMemNote\(/m, "\n}"),
  grab(/^function memVal\(/m, "\n}"),
  grab(/^function renderHeap\(/m, "\n}"),
  grab(/^function renderSystem\(/m, "\n}"),
  grab(/^function renderGc\(/m, "\n}"),
  grab(/^function renderPagecache\(/m, "\n}"),
  grab(/^function renderStore\(/m, "\n}"),
  grab(/^function setMemUnavailable\(/m, "\n}"),
  grab(/^async function attemptMemoryMetrics\(/m, "\n}"),
  grab(/^function renderSpark\(/m, "\n}"),
  grab(/^async function attemptMemoryHistory\(/m, "\n}"),
  grab(/^function startMemoryPolling\(/m, "\n}"),
].join("\n");
const bootSrc = src + "\n" + grab(/^async function bootConnect\(\)/m, "\n}");

// --- stubs ------------------------------------------------------------
const mkEl = initialClass => {
  const classes = new Set(initialClass ? initialClass.split(" ") : []);
  return {
    textContent: "", title: "", style: {},
    classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c) },
    get className() { return [...classes].join(" "); },
    set className(v) { classes.clear(); String(v).split(" ").filter(Boolean).forEach(c => classes.add(c)); },
  };
};
const mkCanvas = () => {
  const ops = [];
  const ctx = new Proxy({}, {
    get: (_t, name) => (typeof name === "string" && /^[a-z]/.test(name) && !["strokeStyle", "lineWidth", "lineJoin", "fillStyle"].includes(name))
      ? (...args) => ops.push([name, ...args]) : undefined,
    set: () => true,
  });
  return { ...mkEl(""), clientWidth: 100, width: 0, height: 0, ops, getContext: () => ctx };
};
let els = {};
let fetchCalls = [];
let routes = {};
let intervals = [];
const fetchStub = async (url, opts) => {
  fetchCalls.push({ url, opts });
  const handler = routes[url.split("?")[0]];
  if (!handler) return { ok: false, status: 404, json: async () => ({}) };
  return handler(url);
};
const sandboxGlobals = {
  document: { getElementById: id => els[id] || null },
  fetch: fetchStub,
  setInterval: (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; },
  console,
};
const api = new Function(...Object.keys(sandboxGlobals),
  src + "\nreturn { attemptMemoryMetrics, renderSpark, attemptMemoryHistory, startMemoryPolling, formatMemBytes };")(
  ...Object.values(sandboxGlobals));

let probe = async () => ({ results: [{ data: [{ row: [1] }] }] });
const bootGlobals = {
  ...sandboxGlobals,
  runCypher: async q => probe(q),
  neo4jConnected: false,
  neo4jStatus: mkEl(""),
  legendStatus: mkEl(""),
  populateRealRepos: async () => {},
  refreshGraph: async () => {},
  loadGitHistory: () => {},
  attemptQueryTelemetry: async () => {},
  attemptCommunityDetection: async () => {},
  cy: { add: () => {} },
  buildElements: () => [],
  forceDirectedSettle: () => {},
  focusRotation: () => {},
};
const boot = new Function(...Object.keys(bootGlobals),
  bootSrc + "\nreturn { bootConnect };")(...Object.values(bootGlobals));
const flush = () => new Promise(r => setTimeout(r, 0));

// --- helpers ----------------------------------------------------------
let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};
const VALUE_IDS = ["heapVal", "ramVal", "swapVal", "cpuVal", "gcVal", "pageCacheVal", "storeSizeVal"];
const reset = () => {
  els = { memPill: mkEl("sample-pill"), memNote: mkEl("placeholder-note"), repoSelect: mkEl(""),
          heapMeter: mkEl(""), ramMeter: mkEl(""), storeSplitVal: mkEl(""),
          heapSpark: mkCanvas(), ramSpark: mkCanvas(), storeSpark: mkCanvas() };
  for (const id of VALUE_IDS) { els[id] = mkEl("metric-val dim"); els[id].textContent = "—"; }
  els.cpuVal.textContent = "— / —";
  els.heapMeter.style.width = "0%";
  els.ramMeter.style.width = "0%";
  els.memPill.textContent = "Checking…";
  fetchCalls = [];
  intervals = [];
  routes = {};
};
const json = body => async () => ({ ok: true, status: 200, json: async () => body });
const serveStats = body => { routes["/api/database-stats"] = json(body); };
const LIVE_HEAP = { available: true, used_bytes: 536870912, max_bytes: 2147483648, used_percent: 25.0 };
const FULL = {
  ts: 1000,
  heap: LIVE_HEAP,
  system: { available: true, ram_total_bytes: 16e9, ram_free_bytes: 4e9, swap_total_bytes: 8e9,
            swap_free_bytes: 6e9, process_cpu_load: 0.05, system_cpu_load: 0.42 },
  gc: { available: true, collection_count: 1676, collection_time_ms: 17949 },
  pagecache: { available: true, configured: "512.00MiB", hit_ratio_available: false },
  store: { available: true, reason: null, graph_bytes: 4437739, tx_log_bytes: 538968064, total_bytes: 543405803 },
};
const dashed = id => /^—( \/ —)?$/.test(els[id].textContent) && els[id].classList.contains("dim");
const allDashed = () => VALUE_IDS.every(dashed) && els.heapMeter.style.width === "0%" && els.ramMeter.style.width === "0%";
const labelledUnavailable = () =>
  /unavailable/i.test(els.memPill.textContent) && /unavailable/i.test(els.memNote.textContent);
const strokes = id => els[id].ops.filter(o => o[0] === "stroke").length;
const opNames = id => els[id].ops.map(o => o[0]).filter(n => n === "moveTo" || n === "lineTo");

(async () => {
  // 1. a full live snapshot reaches every row
  reset();
  serveStats(FULL);
  await api.attemptMemoryMetrics();
  check("asks the dedicated endpoint, never the Cypher console",
    fetchCalls.length === 1 && fetchCalls[0].url === "/api/database-stats", JSON.stringify(fetchCalls.map(c => c.url)));
  const want = {
    heapVal: "537MB / 2147MB", ramVal: "12.0GB / 16.0GB", swapVal: "2.0GB / 8.0GB", cpuVal: "5% / 42%",
    gcVal: "1676 / 17.9s", pageCacheVal: "512.00MiB", storeSizeVal: "543MB",
  };
  for (const [id, text] of Object.entries(want)) {
    check(`${id} shows ${text}`, els[id].textContent === text && !els[id].classList.contains("dim"),
      els[id].textContent + " | " + els[id].className);
  }
  check("heap meter at the reported percentage", els.heapMeter.style.width === "25%", els.heapMeter.style.width);
  check("RAM meter at used/total", els.ramMeter.style.width === "75%", els.ramMeter.style.width);
  check("store split names graph store and transaction logs",
    els.storeSplitVal.textContent === "graph 4MB · transaction logs 539MB", els.storeSplitVal.textContent);
  check("pill is live", els.memPill.textContent === "Live" && els.memPill.className === "wired-pill",
    els.memPill.textContent + "|" + els.memPill.className);
  check("the note says live and claims nothing is unavailable",
    /live/i.test(els.memNote.textContent) && !/unavailable/i.test(els.memNote.textContent), els.memNote.textContent);
  check("the note is honest that hit ratio has no source",
    /hit ratio/i.test(els.memNote.textContent), els.memNote.textContent);

  // 2. heap meter bounds
  for (const [percent, w] of [[0.4, "0.4%"], [100, "100%"], [140, "100%"], [-5, "0%"]]) {
    reset();
    serveStats({ ...FULL, heap: { ...LIVE_HEAP, used_percent: percent } });
    await api.attemptMemoryMetrics();
    check(`a reported ${percent}% heap renders as ${w}`, els.heapMeter.style.width === w, els.heapMeter.style.width);
  }

  // 3. bad heap readings dash heap only
  const badHeaps = [
    ["reported unavailable", { available: false, used_bytes: null, max_bytes: null, used_percent: null }],
    ["missing", undefined],
    ["strings", { available: true, used_bytes: "537000000", max_bytes: "2e9", used_percent: "25" }],
    ["null max", { ...LIVE_HEAP, max_bytes: null }],
    ["infinite max", { ...LIVE_HEAP, max_bytes: Infinity }],
    ["negative max", { ...LIVE_HEAP, max_bytes: -1 }],
    ["used above max", { available: true, used_bytes: 4000, max_bytes: 1000, used_percent: 400 }],
    ["zero", { available: true, used_bytes: 0, max_bytes: 0, used_percent: 0 }],
  ];
  for (const [label, heap] of badHeaps) {
    reset();
    serveStats({ ...FULL, heap });
    await api.attemptMemoryMetrics();
    check(`heap ${label}: heap dashed, meter empty`, dashed("heapVal") && els.heapMeter.style.width === "0%",
      els.heapVal.textContent + " | " + els.heapMeter.style.width);
    check(`heap ${label}: other rows stay live`, els.ramVal.textContent === "12.0GB / 16.0GB", els.ramVal.textContent);
    check(`heap ${label}: note names heap as unavailable`, /JVM heap[^.]*unavailable/i.test(els.memNote.textContent),
      els.memNote.textContent);
  }

  // 4. every other group fails on its own
  const badGroups = [
    ["system", { available: true, ram_total_bytes: 1000, ram_free_bytes: 4000 }, ["ramVal", "swapVal", "cpuVal"]],
    ["gc", { available: true, collection_count: "1676", collection_time_ms: 1 }, ["gcVal"]],
    ["pagecache", { available: true, configured: "" }, ["pageCacheVal"]],
    ["store", { available: true, reason: null, graph_bytes: 10, tx_log_bytes: 20, total_bytes: 999 }, ["storeSizeVal"]],
  ];
  for (const [group, value, ids] of badGroups) {
    reset();
    serveStats({ ...FULL, [group]: value });
    await api.attemptMemoryMetrics();
    check(`bad ${group}: its rows are dashed`, ids.every(dashed), ids.map(id => els[id].textContent).join(" | "));
    check(`bad ${group}: heap stays live`, els.heapVal.textContent === "537MB / 2147MB", els.heapVal.textContent);
    check(`bad ${group}: the note says unavailable`, /unavailable/i.test(els.memNote.textContent), els.memNote.textContent);
  }
  reset();
  serveStats({ ...FULL, store: { available: false, reason: "not_configured", graph_bytes: null, tx_log_bytes: null, total_bytes: null } });
  await api.attemptMemoryMetrics();
  check("an unconfigured store names the setting that fixes it",
    /DEVGRAPH_NEO4J_DATA_DIR/.test(els.memNote.textContent), els.memNote.textContent);
  check("store split is cleared when store is unavailable", els.storeSplitVal.textContent === "", els.storeSplitVal.textContent);
  reset();
  serveStats({ ...FULL, system: { ...FULL.system, swap_total_bytes: 0, swap_free_bytes: 0, process_cpu_load: null } });
  await api.attemptMemoryMetrics();
  check("no swap reads as 'no swap', not a dash", els.swapVal.textContent === "no swap", els.swapVal.textContent);
  check("a missing process CPU load is a dash beside a real system load", els.cpuVal.textContent === "— / 42%", els.cpuVal.textContent);

  // 5. the whole request failing dashes everything and says so
  const outright = [
    ["HTTP error", async () => ({ ok: false, status: 500, json: async () => ({ detail: "Neo4jError: boom" }) })],
    ["network failure", async () => { throw new Error("Failed to fetch"); }],
    ["non-JSON body", async () => ({ ok: true, status: 200, json: async () => { throw new Error("not json"); } })],
    ["null body", json(null)],
  ];
  for (const [label, handler] of outright) {
    reset();
    routes["/api/database-stats"] = handler;
    await api.attemptMemoryMetrics();
    check(`${label}: every value dashed`, allDashed(), VALUE_IDS.map(id => els[id].textContent).join(" | "));
    check(`${label}: labelled unavailable`, labelledUnavailable(), els.memPill.textContent + " | " + els.memNote.textContent);
    check(`${label}: no raw backend text`, !/Neo4jError/.test(els.memNote.textContent + els.memPill.title), els.memNote.textContent);
  }

  // 6. a later failure takes live values back down
  reset();
  serveStats(FULL);
  await api.attemptMemoryMetrics();
  routes["/api/database-stats"] = async () => { throw new Error("Failed to fetch"); };
  await api.attemptMemoryMetrics();
  check("a later failure re-dashes a live card", allDashed() && labelledUnavailable(),
    VALUE_IDS.map(id => els[id].textContent).join(" | "));

  // 7. source hygiene
  check("never writes the card through innerHTML", !/\.innerHTML\s*\+?=/.test(src), "innerHTML found");
  check("the browser never issues a JMX call itself", !/queryJmx/.test(html), "index.html contains dbms.queryJmx");
  check("the memory path never calls runCypher", !/runCypher/.test(src), "runCypher found in memory path");
  check("the card no longer ships a 'Not wired' pill", !/id="memPill"[^>]*>Not wired</.test(html), "Not wired pill found");

  // 8. sparklines
  reset();
  api.renderSpark("heapSpark", []);
  check("an empty history draws no line", strokes("heapSpark") === 0, JSON.stringify(els.heapSpark.ops));
  reset();
  api.renderSpark("heapSpark", [5]);
  check("a single sample draws no line", strokes("heapSpark") === 0, JSON.stringify(els.heapSpark.ops));
  reset();
  api.renderSpark("heapSpark", [1, 2, 3]);
  check("a continuous history is one path", strokes("heapSpark") === 1 &&
    JSON.stringify(opNames("heapSpark")) === JSON.stringify(["moveTo", "lineTo", "lineTo"]), JSON.stringify(opNames("heapSpark")));
  reset();
  api.renderSpark("heapSpark", [1, 2, null, 4, 5]);
  check("a gap breaks the line instead of bridging it",
    JSON.stringify(opNames("heapSpark")) === JSON.stringify(["moveTo", "lineTo", "moveTo", "lineTo"]), JSON.stringify(opNames("heapSpark")));
  reset();
  api.renderSpark("heapSpark", ["5", 6, 7]);
  check("non-numeric samples are skipped, not coerced",
    JSON.stringify(opNames("heapSpark")) === JSON.stringify(["moveTo", "lineTo"]), JSON.stringify(opNames("heapSpark")));

  reset();
  routes["/api/database-stats/history"] = json({ interval_s: 15, samples: [
    { ts: 1, heap_used_bytes: 10, ram_used_bytes: 20, process_cpu_load: 0.1, store_total_bytes: 30 },
    { ts: 2, heap_used_bytes: 11, ram_used_bytes: 21, process_cpu_load: 0.1, store_total_bytes: 31 },
  ] });
  await api.attemptMemoryHistory();
  check("history asks for the last hour", fetchCalls.some(c => c.url === "/api/database-stats/history?seconds=3600"),
    JSON.stringify(fetchCalls.map(c => c.url)));
  check("history draws all three sparklines",
    strokes("heapSpark") === 1 && strokes("ramSpark") === 1 && strokes("storeSpark") === 1,
    [strokes("heapSpark"), strokes("ramSpark"), strokes("storeSpark")].join(","));
  reset();
  routes["/api/database-stats/history"] = async () => { throw new Error("Failed to fetch"); };
  await api.attemptMemoryHistory();
  check("a failed history fetch leaves sparklines empty",
    strokes("heapSpark") === 0 && strokes("ramSpark") === 0 && strokes("storeSpark") === 0, "drew on failure");

  // 9. polling
  reset();
  serveStats(FULL);
  api.startMemoryPolling();
  api.startMemoryPolling();
  check("polling is scheduled once, every 15 s", intervals.length === 1 && intervals[0].ms === 15000,
    JSON.stringify(intervals.map(i => i.ms)));

  /* 10. boot path. The boot instance keeps its own memPollTimer across
     calls, so the reachable boot runs first: it is the one that must
     schedule polling. */
  reset();
  probe = async () => ({ results: [{ data: [{ row: [1] }] }] });
  serveStats(FULL);
  await boot.bootConnect();
  await flush();
  check("boot reads live stats once Neo4j is reachable",
    els.heapVal.textContent === "537MB / 2147MB" && els.memPill.textContent === "Live", els.heapVal.textContent);
  check("boot starts polling", intervals.length === 1 && intervals[0].ms === 15000, String(intervals.length));
  for (const [label, failure] of [
    ["the probe request fails outright", async () => { throw new Error("Failed to fetch"); }],
    ["the probe comes back with a Neo4j error", async () => ({ errors: [{ message: "ServiceUnavailable" }] })],
  ]) {
    reset();
    probe = failure;
    serveStats(FULL);   // the server offers live Neo4j values; the card must not trust them on this branch
    await boot.bootConnect();
    await flush();
    check(`boot with ${label}: Neo4j rows dashed`,
      ["heapVal", "ramVal", "swapVal", "cpuVal", "gcVal", "pageCacheVal"].every(dashed),
      VALUE_IDS.map(id => els[id].textContent).join(" | "));
    check(`boot with ${label}: store size still shown`, els.storeSizeVal.textContent === "543MB", els.storeSizeVal.textContent);
    check(`boot with ${label}: note says Neo4j is unreachable`,
      /unreachable/i.test(els.memNote.textContent) && !/ServiceUnavailable/.test(els.memNote.textContent + els.memPill.title),
      els.memNote.textContent);
    check(`boot with ${label}: pill is not stuck on Checking…`, els.memPill.textContent !== "Checking…", els.memPill.textContent);
  }

  console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/dashboard/test_database_stats_ui.py -q`
Expected: FAIL with `could not find /^const MEM_POLL_MS\b/m`.

- [ ] **Step 3: Card markup and CSS**

In `devgraph/dashboard/static/index.html`, after the `.metric-block .metric-label { ... }` CSS rule (~line 332), add:

```css
.spark { display: block; width: 100%; height: 28px; margin-top: 6px; }
.metric-sub { font-family: var(--font-mono); font-size: 11px; color: var(--meta); margin-top: 2px; }
```

Replace the whole `<div class="card" data-od-id="db-memory-card"> ... </div>` block (~574–586) with:

```html
      <div class="card" data-od-id="db-memory-card">
        <div class="card-head"><h3>Database &amp; memory</h3><span class="sample-pill" id="memPill" title="Sampled every 15 s from Neo4j's JMX beans and config, and from its data directory; anything without a usable reading stays dashed">Checking…</span></div>
        <div class="metric-block">
          <div class="metric-label"><span>JVM heap</span><span class="metric-val dim" id="heapVal">—</span></div>
          <div class="meter"><i id="heapMeter" style="width:0%"></i></div>
          <canvas class="spark" id="heapSpark" height="56"></canvas>
        </div>
        <div class="metric-block">
          <div class="metric-label"><span>System RAM<span class="qmark" data-tip="Physical memory as the Neo4j JVM sees it: the host's RAM, or the container's memory limit if one is set.">?</span></span><span class="metric-val dim" id="ramVal">—</span></div>
          <div class="meter"><i id="ramMeter" style="width:0%"></i></div>
          <canvas class="spark" id="ramSpark" height="56"></canvas>
        </div>
        <div class="metric-row"><span class="metric-label">Swap used</span><span class="metric-val dim" id="swapVal">—</span></div>
        <div class="metric-row"><span class="metric-label">CPU (Neo4j / system)</span><span class="metric-val dim" id="cpuVal">— / —</span></div>
        <div class="metric-row"><span class="metric-label">GC collections / time<span class="qmark" data-tip="Totals since Neo4j started, summed across the JVM's garbage collectors.">?</span></span><span class="metric-val dim" id="gcVal">—</span></div>
        <div class="metric-row"><span class="metric-label">Page cache (configured)<span class="qmark" data-tip="server.memory.pagecache.size as Neo4j reports it. Hit ratio needs Neo4j Enterprise metrics and has no source on Community.">?</span></span><span class="metric-val dim" id="pageCacheVal">—</span></div>
        <div class="metric-block">
          <div class="metric-label"><span>Store size on disk</span><span class="metric-val dim" id="storeSizeVal">—</span></div>
          <div class="metric-sub" id="storeSplitVal"></div>
          <canvas class="spark" id="storeSpark" height="56"></canvas>
        </div>
        <div class="placeholder-note" id="memNote">Reading database and memory stats…</div>
      </div>
```

- [ ] **Step 4: Card JavaScript**

Replace everything from `function setMemNote(reason) {` through the closing `}` of `async function attemptMemoryMetrics() { ... }` (this includes `setHeapUnavailable`; keep the `/* ── Database & memory / Query telemetry / Community ... */` comment above it) with:

```js
const MEM_POLL_MS = 15000;
let memPollTimer = null;
/* Card-side wording for the server's store `reason` codes. */
const STORE_REASONS = {
  not_configured: "it needs DEVGRAPH_NEO4J_DATA_DIR pointing at Neo4j's data directory",
  unreadable: "the Neo4j data directory could not be read",
};
function formatMemBytes(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(1) + "GB";
  if (n >= 1e6) return (n / 1e6).toFixed(0) + "MB";
  if (n >= 1e3) return (n / 1e3).toFixed(0) + "KB";
  return n + "B";
}
function setMemNote(text) {
  document.getElementById("memNote").textContent = text;
}
/* One value cell: real text undims it, null puts the dash back. */
function memVal(id, text, dash = "—") {
  const el = document.getElementById(id);
  if (text === null) { el.textContent = dash; el.classList.add("dim"); }
  else { el.textContent = text; el.classList.remove("dim"); }
}
/* Each render* takes one group of the server's snapshot and returns whether
   it went live. Number.isFinite, not Number(): a string or null from an
   unexpected server build must read as no value, not coerce into one. */
function renderHeap(heap) {
  const used = heap?.used_bytes, heapMax = heap?.max_bytes, percent = heap?.used_percent;
  const ok = heap?.available === true && Number.isFinite(used) && Number.isFinite(heapMax) &&
    Number.isFinite(percent) && used > 0 && heapMax > 0 && used <= heapMax;
  memVal("heapVal", ok ? `${(used/1e6).toFixed(0)}MB / ${(heapMax/1e6).toFixed(0)}MB` : null);
  document.getElementById("heapMeter").style.width = ok ? Math.min(100, Math.max(0, percent)) + "%" : "0%";
  return ok;
}
function renderSystem(sys) {
  const total = sys?.ram_total_bytes, free = sys?.ram_free_bytes;
  const ok = sys?.available === true && Number.isFinite(total) && Number.isFinite(free) &&
    total > 0 && free >= 0 && free <= total;
  memVal("ramVal", ok ? `${formatMemBytes(total - free)} / ${formatMemBytes(total)}` : null);
  document.getElementById("ramMeter").style.width = ok ? Math.round((total - free) / total * 1000) / 10 + "%" : "0%";
  const st = sys?.swap_total_bytes, sf = sys?.swap_free_bytes;
  const swapOk = ok && Number.isFinite(st) && Number.isFinite(sf) && st >= 0 && sf >= 0 && sf <= st;
  memVal("swapVal", !swapOk ? null : st === 0 ? "no swap" : `${formatMemBytes(st - sf)} / ${formatMemBytes(st)}`);
  const pct = v => (Number.isFinite(v) && v >= 0 && v <= 1) ? (v * 100).toFixed(0) + "%" : null;
  const p = pct(sys?.process_cpu_load), s = pct(sys?.system_cpu_load);
  memVal("cpuVal", ok && (p || s) ? `${p ?? "—"} / ${s ?? "—"}` : null, "— / —");
  return ok;
}
function renderGc(gc) {
  const n = gc?.collection_count, ms = gc?.collection_time_ms;
  const ok = gc?.available === true && Number.isFinite(n) && Number.isFinite(ms) && n >= 0 && ms >= 0;
  memVal("gcVal", ok ? `${n} / ${(ms / 1000).toFixed(1)}s` : null);
  return ok;
}
function renderPagecache(pc) {
  const v = pc?.configured;
  const ok = pc?.available === true && typeof v === "string" && v.trim() !== "";
  memVal("pageCacheVal", ok ? v : null);
  return ok;
}
function renderStore(store) {
  const g = store?.graph_bytes, t = store?.tx_log_bytes, total = store?.total_bytes;
  const ok = store?.available === true && Number.isFinite(g) && Number.isFinite(t) &&
    Number.isFinite(total) && g >= 0 && t >= 0 && total === g + t;
  memVal("storeSizeVal", ok ? formatMemBytes(total) : null);
  document.getElementById("storeSplitVal").textContent =
    ok ? `graph ${formatMemBytes(g)} · transaction logs ${formatMemBytes(t)}` : "";
  return ok;
}
/* The whole request failed: nothing on the card can be stood behind. `why`
   is the card's own wording, never backend error text. */
function setMemUnavailable(why) {
  for (const render of [renderHeap, renderSystem, renderGc, renderPagecache, renderStore]) render(null);
  const pill = document.getElementById("memPill");
  pill.textContent = "Unavailable";
  pill.className = "sample-pill";
  pill.title = "Database and memory stats unavailable — " + why;
  setMemNote(`Database and memory stats unavailable — ${why}, so every value stays dashed rather than estimated.`);
}
/* opts.neo4jDown: boot already knows Neo4j didn't answer, so the Neo4j
   groups are shown as unreachable whatever the server sent; store size is
   read from disk and can still be live. Later polls trust the server. */
async function attemptMemoryMetrics(opts = {}) {
  const down = opts.neo4jDown === true;
  let stats;
  try {
    const res = await fetch("/api/database-stats");
    if (!res.ok) throw new Error("HTTP " + res.status);
    stats = await res.json();
    if (!stats || typeof stats !== "object") throw new Error("empty body");
  } catch (e) {
    setMemUnavailable(down ? "Neo4j is unreachable" : "the dashboard server returned no usable reading");
    return;
  }
  const live = {
    "JVM heap": renderHeap(down ? null : stats.heap),
    "system RAM, swap and CPU": renderSystem(down ? null : stats.system),
    "GC": renderGc(down ? null : stats.gc),
    "page cache size": renderPagecache(down ? null : stats.pagecache),
  };
  const storeLive = renderStore(stats.store);
  const missing = Object.keys(live).filter(k => !live[k]);
  const notes = [];
  if (missing.length) {
    notes.push(`${missing.join(", ")} unavailable — ${down ? "Neo4j is unreachable" : "this Neo4j returned no usable reading"}, so they stay dashed rather than estimated.`);
  }
  if (!storeLive) notes.push(`Store size unavailable — ${STORE_REASONS[stats.store?.reason] || "no usable reading"}.`);
  if (!notes.length) notes.push("All readings are live, sampled every 15 s.");
  notes.push("Page cache hit ratio needs Neo4j Enterprise metrics and has no source on Community.");
  setMemNote(notes.join(" "));
  const anyLive = storeLive || missing.length < Object.keys(live).length;
  const pill = document.getElementById("memPill");
  pill.textContent = anyLive ? "Live" : "Unavailable";
  pill.className = anyLive ? "wired-pill" : "sample-pill";
  pill.title = anyLive
    ? "Read live from Neo4j's JMX beans and config, and from its data directory"
    : "No database or memory reading is available";
}
/* Draws one sparkline. Non-finite samples are gaps: the path is broken
   there rather than bridged, so a stretch with Neo4j down never reads as a
   smooth trend. Fewer than two points draws nothing. */
function renderSpark(id, values) {
  const canvas = document.getElementById(id);
  const ctx = canvas.getContext("2d");
  const w = canvas.width = (canvas.clientWidth || 200) * 2, h = canvas.height = 56;
  ctx.clearRect(0, 0, w, h);
  const pts = values.map((v, i) => [i, v]).filter(([, v]) => Number.isFinite(v));
  if (pts.length < 2) return;
  const vs = pts.map(p => p[1]);
  const min = Math.min(...vs), range = (Math.max(...vs) - min) || 1;
  const span = Math.max(values.length - 1, 1), pad = 3;
  ctx.beginPath();
  let prev = -2;
  for (const [i, v] of pts) {
    const x = pad + (i / span) * (w - pad * 2), y = h - pad - ((v - min) / range) * (h - pad * 2);
    if (i === prev + 1) ctx.lineTo(x, y); else ctx.moveTo(x, y);
    prev = i;
  }
  ctx.strokeStyle = "#7170ff"; ctx.lineWidth = 2; ctx.lineJoin = "round";
  ctx.stroke();
}
async function attemptMemoryHistory() {
  let samples = [];
  try {
    const res = await fetch("/api/database-stats/history?seconds=3600");
    if (res.ok) {
      const body = await res.json();
      if (Array.isArray(body?.samples)) samples = body.samples;
    }
  } catch (e) { /* no history: sparklines stay empty, never invented */ }
  renderSpark("heapSpark", samples.map(s => s?.heap_used_bytes));
  renderSpark("ramSpark", samples.map(s => s?.ram_used_bytes));
  renderSpark("storeSpark", samples.map(s => s?.store_total_bytes));
}
function startMemoryPolling() {
  attemptMemoryHistory();
  if (memPollTimer !== null) return;
  memPollTimer = setInterval(() => { attemptMemoryMetrics(); attemptMemoryHistory(); }, MEM_POLL_MS);
}
```

Note: `renderSpark` with a single point after a gap leaves `prev` such that the next point starts a new subpath via `moveTo` — the test in Step 1 (`[1, 2, null, 4, 5]` → `moveTo, lineTo, moveTo, lineTo`) pins this.

- [ ] **Step 5: Wire `bootConnect`**

In `bootConnect` (~2600–2630):

1. In the success branch, replace the line `    attemptMemoryMetrics();` with:

```js
    attemptMemoryMetrics();
    startMemoryPolling();
```

2. In the `catch (e)` branch, replace the comment and `setHeapUnavailable("Neo4j is unreachable");` line with:

```js
    /* Neo4j didn't answer, so the card shows its Neo4j rows as unreachable
       instead of sitting on "Checking…"; store size is read from disk and
       can still be live. Polling picks Neo4j back up if it returns. */
    attemptMemoryMetrics({ neo4jDown: true });
    startMemoryPolling();
```

Then confirm nothing else references the removed function: `grep -n setHeapUnavailable devgraph/dashboard/static/index.html` must print nothing.

- [ ] **Step 6: Run the UI test and the dashboard suite**

Run: `uv run pytest tests/dashboard -q`
Expected: all pass. If `test_database_stats_ui.py` fails, run `node tests/dashboard/database_stats_ui.js` directly to see which `FAIL` lines print.

- [ ] **Step 7: Manual check against the live container**

Serve the app standalone on a spare port with an empty throwaway registry, so no real tray, watcher or registry is touched (`<scratch>` is the session scratchpad directory):

```bash
cat > <scratch>/serve_dashboard.py <<'EOF'
import uvicorn
from devgraph.config.settings import get_settings
from devgraph.dashboard.app import build_app
from devgraph.dashboard.events import EventBroadcaster
from devgraph.graph.engine import GraphEngine
from devgraph.registry.store import RepoRegistry

s = get_settings()
app = build_app(GraphEngine(s.neo4j_uri, s.neo4j_user, s.neo4j_password),
                RepoRegistry(s.registry_db_path), EventBroadcaster())
uvicorn.run(app, host="127.0.0.1", port=8799, log_level="warning")
EOF
DEVGRAPH_REGISTRY_DB_PATH=<scratch>/registry.sqlite3 \
DEVGRAPH_NEO4J_DATA_DIR=$(podman volume inspect devgraph_neo4j_data --format '{{.Mountpoint}}') \
  uv run python <scratch>/serve_dashboard.py
```

Run it in the background. If `GraphEngine`/`RepoRegistry` constructors differ, read `devgraph/agent/headless.py` (it builds both) and match it. Then:

```bash
curl -s http://127.0.0.1:8799/api/database-stats | python -m json.tool
sleep 35; curl -s 'http://127.0.0.1:8799/api/database-stats/history?seconds=60' | python -m json.tool
```

Expected: every group `"available": true`, store `total_bytes` close to `du -sb` of `databases/` + `transactions/` in that volume, and at least 2 history samples. Open `http://127.0.0.1:8799` in a browser and confirm the card shows Live, real values, and sparklines appearing after a minute. Stop the server afterwards.

- [ ] **Step 8: Commit**

```bash
git add devgraph/dashboard/static/index.html tests/dashboard/database_stats_ui.js
git commit -m "Show live database and memory stats with sparklines"
```

---

### Task 5: Full verification and PR

- [ ] **Step 1: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass (live-Neo4j tests need the container up; display-bound tests skip without an X display).

- [ ] **Step 2: Push to the fork and open the PR upstream**

```bash
git push -u origin feat/db-memory-stats
gh pr create -R HaydenSchmidtDOC/DevGraph --base master --head <fork-owner>:feat/db-memory-stats \
  --title "Live database and memory stats with sampled history" --body-file /tmp/pr-body.md
```

Write `/tmp/pr-body.md` (use the session scratchpad directory instead of `/tmp` if one is given) summarising: what the card now shows, that page cache hit ratio has no Community source, the `DEVGRAPH_NEO4J_DATA_DIR` setting and compose mount, the 15 s / 1 h in-memory history, validation (`uv run pytest` count, manual check against `neo4j:5.26-community`), and `Addresses #3`. Do not mention Claude/the assistant in the body. Confirm with the user before running `git push` and `gh pr create`.
