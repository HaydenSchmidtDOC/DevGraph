"""List-style built-ins cap the rows they read and say so, instead of failing
when a repository has more matches than the row cap."""

import pytest

from devgraph.mcp import tools


class _Overflowing:
    """Every list query has more rows than it may return."""

    def __init__(self):
        self.queries = []

    def run_read_cypher(self, query, params, *, timeout_s, max_rows):
        self.queries.append((query, max_rows))
        if max_rows != tools.LIST_ROW_LIMIT:  # list_recent_changes' commit cutoff lookup
            return [], False
        return [{"name": f"n{i}", "type": ["Function"], "repo_id": "demo", "file": "a.py"} for i in range(max_rows)], True

    def run_cypher(self, query, params=None):
        raise AssertionError("must not use the unbounded run_cypher")


CALLS = {
    "find_callers": lambda e: tools.find_callers(e, "demo", "get"),
    "find_mentions": lambda e: tools.find_mentions(e, "demo", "get"),
    "list_recent_changes": lambda e: tools.list_recent_changes(e, "demo", 5),
}


@pytest.mark.parametrize("name", sorted(CALLS))
def test_a_list_tool_over_the_cap_returns_a_truncated_envelope(name):
    engine = _Overflowing()
    result = CALLS[name](engine)
    assert result["truncated"] is True
    assert result["count"] == tools.LIST_ROW_LIMIT
    assert len(result["results"]) == 15
    assert f"more than {tools.LIST_ROW_LIMIT}" in result["notice"]
    query, max_rows = engine.queries[-1]
    assert max_rows == tools.LIST_ROW_LIMIT and f"LIMIT {tools.LIST_ROW_LIMIT + 1}" in query


def test_blame_component_returns_at_most_the_cap():
    engine = _Overflowing()
    rows = tools.blame_component(engine, "demo", "a.py")
    assert len(rows) == tools.LIST_ROW_LIMIT
    assert f"LIMIT {tools.LIST_ROW_LIMIT + 1}" in engine.queries[-1][0]
