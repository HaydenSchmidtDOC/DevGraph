"""In-memory ring buffer of Cypher queries run through the dashboard's
`/api/cypher` endpoint.

Backs the "Cypher query rate" chart and query-log table in the dashboard UI.
Process-local and lost on restart by design -- it's a lightweight record of
what the dashboard's own Cypher console has run, not a substitute for real
Neo4j/MCP-level query telemetry (`dbms.listQueries`, per-tool instrumentation
across every MCP client), which stays explicitly "not wired" elsewhere in the
dashboard.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any

_MAX_ENTRIES = 500


@dataclass
class QueryLogEntry:
    ts: float
    repo_id: str | None
    query: str
    duration_ms: float
    ok: bool


class QueryLog:
    def __init__(self, max_entries: int = _MAX_ENTRIES) -> None:
        self._entries: deque[QueryLogEntry] = deque(maxlen=max_entries)

    def record(self, *, repo_id: str | None, query: str, duration_ms: float, ok: bool) -> None:
        self._entries.append(QueryLogEntry(time.time(), repo_id, query, duration_ms, ok))

    def recent(self, limit: int) -> list[dict[str, Any]]:
        entries = list(self._entries)[-limit:]
        entries.reverse()
        return [
            {
                "ts": e.ts,
                "repo_id": e.repo_id,
                "query": e.query,
                "duration_ms": e.duration_ms,
                "ok": e.ok,
            }
            for e in entries
        ]

    def rate(self, span_s: int, interval_s: int) -> list[dict[str, Any]]:
        """Bucket recorded queries into fixed-width `interval_s` windows over
        the trailing `span_s` seconds, oldest bucket first."""
        now = time.time()
        start = now - span_s
        # At least 2 buckets -- the frontend chart divides by (points - 1)
        # to lay points out along the x-axis, so a single bucket would be a
        # divide-by-zero there.
        n_buckets = max(2, int(span_s // interval_s))
        counts = [0] * n_buckets
        for e in self._entries:
            if e.ts < start:
                continue
            idx = min(n_buckets - 1, int((e.ts - start) // interval_s))
            counts[idx] += 1
        return [{"t": start + i * interval_s, "count": counts[i]} for i in range(n_buckets)]
