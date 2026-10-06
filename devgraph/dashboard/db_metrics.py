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
import threading
import time
from collections import deque
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
