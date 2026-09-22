"""Tests for find_dependency_cycles: the bounded, GDS-free circular-dependency
tool and its registration on the MCP server.

Neo4j-free by construction. Every test here drives a stub engine that records
the Cypher it was handed and replays canned path rows, because what needs
pinning down is the part this module owns: which query is built (and that an
unsupported relationship never produces one at all), and how raw paths are
turned into deduplicated cycles. The graph-side behaviour of a variable-length
match is Neo4j's, not ours, and tests/mcp/test_server.py already covers the
live-database path — but its assertions skip wholesale when Neo4j is absent,
so the tool-surface invariants are also asserted here where nothing can skip.
"""

import asyncio
import json

import pytest

from devgraph.config.settings import Settings
from devgraph.graph import schema
from devgraph.mcp import server as mcp_server
from devgraph.mcp import tools as devgraph_tools
from devgraph.mcp.tools import find_dependency_cycles


class _StubEngine:
    """Records the last query and replays canned rows; never touches a database."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, dict]] = []

    def run_cypher(self, cypher, params=None):
        self.calls.append((cypher, params or {}))
        return self.rows

    @property
    def cypher(self) -> str:
        assert len(self.calls) == 1, f"expected exactly one query, got {len(self.calls)}"
        return self.calls[0][0]

    @property
    def params(self) -> dict:
        assert len(self.calls) == 1, f"expected exactly one query, got {len(self.calls)}"
        return self.calls[0][1]


class _StubRegistry:
    """build_server only stows the registry away for tools needing a repo root."""


def _node(name, *, repo_id="demo", file="src/app.py", labels=("Module",)):
    return {"name": name, "labels": list(labels), "repo_id": repo_id, "file": file}


def _closed_path(*nodes):
    """One raw row as the query returns it: a path whose start node repeats at
    the end. Arguments are either names or full node dicts."""
    resolved = [_node(n) if isinstance(n, str) else n for n in nodes]
    return {"nodes": resolved + [resolved[0]]}


def _names(result):
    return [[n["name"] for n in row["nodes"]] for row in result["results"]]


class TestQueryConstruction:
    def test_only_the_allowlisted_relationship_and_clamped_length_are_interpolated(self):
        engine = _StubEngine()

        find_dependency_cycles(engine, "demo", relationship="DEPENDS_ON", max_length=4)

        assert "[:DEPENDS_ON*2..4]" in engine.cypher
        # Caller data never reaches the query text.
        assert "demo" not in engine.cypher
        assert engine.params == {"repo_id": "demo"}

    def test_relationship_is_case_insensitive_and_normalised(self):
        engine = _StubEngine()

        find_dependency_cycles(engine, "demo", relationship="  imports  ")

        assert "[:IMPORTS*2..5]" in engine.cypher

    def test_the_raw_path_expansion_is_capped(self):
        engine = _StubEngine()

        find_dependency_cycles(engine, "demo")

        assert f"LIMIT {devgraph_tools._CYCLE_RAW_PATH_LIMIT}" in engine.cypher
        assert devgraph_tools._CYCLE_RAW_PATH_LIMIT == 500

    def test_raw_paths_are_deterministically_ordered_before_the_cap(self):
        engine = _StubEngine()

        find_dependency_cycles(engine, "demo")

        order = engine.cypher.index("ORDER BY")
        limit = engine.cypher.index("LIMIT")
        assert order < limit
        assert "[n IN nodes(path) | elementId(n)]" in engine.cypher
        assert "[r IN relationships(path) | elementId(r)]" in engine.cypher

    def test_start_nodes_are_prefiltered_to_those_with_an_edge_each_way(self):
        """Every node on a cycle has both an incoming and an outgoing edge of
        the traversed type, so this prefilter narrows the scan losslessly."""
        engine = _StubEngine()

        find_dependency_cycles(engine, "demo", relationship="CALLS")

        assert "(start)-[:CALLS]->()" in engine.cypher
        assert "()-[:CALLS]->(start)" in engine.cypher

    @pytest.mark.parametrize(
        "requested,expected",
        [(0, 2), (1, 2), (2, 2), (5, 5), (8, 8), (9, 8), (500, 8), (-3, 2)],
    )
    def test_max_length_is_clamped_to_2_through_8(self, requested, expected):
        engine = _StubEngine()

        find_dependency_cycles(engine, "demo", max_length=requested)

        assert f"*2..{expected}]" in engine.cypher

    def test_a_non_integer_max_length_is_coerced_before_clamping(self):
        engine = _StubEngine()

        find_dependency_cycles(engine, "demo", max_length=5.9)

        assert "*2..5]" in engine.cypher

    def test_the_allowlist_is_a_subset_of_the_schemas_relationship_types(self):
        assert set(devgraph_tools._CYCLE_RELATIONSHIPS) <= set(schema.RELATIONSHIP_TYPES)
        # Containment/provenance/intent edges are not dependencies; a "cycle"
        # over them would be meaningless.
        assert not set(devgraph_tools._CYCLE_RELATIONSHIPS) & {
            "CONTAINS", "MODIFIES", "MENTIONS", "SATISFIES", "DOCUMENTED_BY",
        }


class TestRelationshipValidation:
    @pytest.mark.parametrize(
        "relationship",
        ["CONTAINS", "MENTIONS", "MODIFIES", "IMPORT", "", "IMPORTS|CALLS", "*"],
    )
    def test_an_unsupported_relationship_raises_before_any_query_runs(self, relationship):
        engine = _StubEngine()

        with pytest.raises(ValueError):
            find_dependency_cycles(engine, "demo", relationship=relationship)

        assert engine.calls == []

    def test_an_injection_attempt_never_reaches_the_engine(self):
        engine = _StubEngine()

        with pytest.raises(ValueError):
            find_dependency_cycles(engine, "demo", relationship="IMPORTS] ) DETACH DELETE n //")

        assert engine.calls == []

    def test_the_error_does_not_echo_the_rejected_input_back(self):
        """The message lands in a model's context; the argument is
        caller-controlled and unbounded, so it is described, not repeated."""
        engine = _StubEngine()

        with pytest.raises(ValueError) as excinfo:
            find_dependency_cycles(engine, "demo", relationship="zz-" + "x" * 5000)

        assert "zz-" not in str(excinfo.value)
        assert "IMPORTS" in str(excinfo.value)


class TestRepositoryScoping:
    def test_every_node_is_scoped_to_the_repo_by_default(self):
        engine = _StubEngine()

        find_dependency_cycles(engine, "demo")

        assert "ALL(n IN nodes(path) WHERE n.repo_id = $repo_id)" in engine.cypher
        # Also pinned at the scan, so the expansion does not start from every
        # node in the database and filter whole paths away afterwards.
        assert "start.repo_id = $repo_id" in engine.cypher
        assert engine.params == {"repo_id": "demo"}

    def test_cross_repo_drops_every_repo_predicate(self):
        engine = _StubEngine()

        find_dependency_cycles(engine, "demo", cross_repo=True)

        # The projection still reports each node's repo_id; what goes is every
        # predicate that would restrict the match to one repository.
        assert "$repo_id" not in engine.cypher
        assert "ALL(n IN nodes(path)" not in engine.cypher
        assert engine.params == {}


class TestCanonicalisation:
    def test_one_cycle_is_reported_once_whatever_node_it_was_found_from(self):
        """The same three-node ring, discovered from each of its members."""
        engine = _StubEngine([
            _closed_path("a", "b", "c"),
            _closed_path("b", "c", "a"),
            _closed_path("c", "a", "b"),
        ])

        result = find_dependency_cycles(engine, "demo")

        assert result["count"] == 1
        assert result["truncated"] is False
        assert _names(result) == [["a", "b", "c"]]

    def test_rotations_arriving_in_any_order_produce_the_same_answer(self):
        rotations = [_closed_path("b", "c", "a"), _closed_path("c", "a", "b"), _closed_path("a", "b", "c")]

        first = find_dependency_cycles(_StubEngine(list(rotations)), "demo")
        second = find_dependency_cycles(_StubEngine(list(reversed(rotations))), "demo")

        assert first == second
        assert _names(first) == [["a", "b", "c"]]

    def test_a_mutual_pair_found_from_both_ends_collapses_to_one_cycle(self):
        engine = _StubEngine([_closed_path("a", "b"), _closed_path("b", "a")])

        result = find_dependency_cycles(engine, "demo")

        assert result["count"] == 1
        assert result["results"][0]["length"] == 2
        assert _names(result) == [["a", "b"]]

    def test_opposite_direction_cycles_over_the_same_nodes_stay_distinct(self):
        """a->b->c->a and a->c->b->a are different dependency chains, not
        rotations of one another — collapsing them would hide a real cycle."""
        engine = _StubEngine([_closed_path("a", "b", "c"), _closed_path("a", "c", "b")])

        result = find_dependency_cycles(engine, "demo")

        assert result["count"] == 2
        assert _names(result) == [["a", "b", "c"], ["a", "c", "b"]]

    def test_the_canonical_key_is_null_safe_and_label_order_independent(self):
        """`file` is absent on Module/Endpoint nodes, and labels() ordering is
        not guaranteed stable — two rotations of one cycle must still match."""
        a1 = _node("a", file=None, labels=("Module", "Indexed"))
        a2 = _node("a", file=None, labels=("Indexed", "Module"))
        b1 = _node("b", file=None, labels=("Module",))
        b2 = _node("b", file=None, labels=("Module",))
        engine = _StubEngine([_closed_path(a1, b1), _closed_path(b2, a2)])

        result = find_dependency_cycles(engine, "demo")

        assert result["count"] == 1
        assert _names(result) == [["a", "b"]]

    def test_results_are_sorted_by_length_then_canonically(self):
        engine = _StubEngine([
            _closed_path("x", "y", "z"),
            _closed_path("m", "n"),
            _closed_path("a", "b"),
        ])

        result = find_dependency_cycles(engine, "demo")

        assert _names(result) == [["a", "b"], ["m", "n"], ["x", "y", "z"]]
        assert [row["length"] for row in result["results"]] == [2, 2, 3]

    def test_nodes_of_the_same_name_in_different_repos_are_not_conflated(self):
        engine = _StubEngine([
            _closed_path(_node("a", repo_id="one"), _node("b", repo_id="one")),
            _closed_path(_node("a", repo_id="two"), _node("b", repo_id="two")),
        ])

        result = find_dependency_cycles(engine, "demo", cross_repo=True)

        assert result["count"] == 2


class TestNonSimplePaths:
    def test_a_parallel_self_loop_traversed_twice_is_not_a_cycle(self):
        """Neo4j's variable-length match forbids repeating a relationship, not
        a node: two parallel a->a edges satisfy *2.. and come back as a path.
        The duplicate-node rejection, not the length floor, is what stops it."""
        engine = _StubEngine([{"nodes": [_node("a"), _node("a"), _node("a")]}])

        result = find_dependency_cycles(engine, "demo")

        assert result == {"count": 0, "results": [], "truncated": False}

    def test_a_single_edge_self_loop_is_not_a_cycle(self):
        engine = _StubEngine([{"nodes": [_node("a"), _node("a")]}])

        assert find_dependency_cycles(engine, "demo")["count"] == 0

    def test_a_path_revisiting_a_node_is_rejected_as_non_simple(self):
        """a->b->a->c->a closes on a but is two cycles pasted together, with no
        unique smallest member to rotate to."""
        engine = _StubEngine([_closed_path("a", "b", "a", "c")])

        result = find_dependency_cycles(engine, "demo")

        assert result["count"] == 0

    def test_a_genuine_cycle_alongside_a_rejected_path_still_comes_back(self):
        engine = _StubEngine([_closed_path("a", "b", "a", "c"), _closed_path("d", "e")])

        result = find_dependency_cycles(engine, "demo")

        assert _names(result) == [["d", "e"]]


class TestEnvelope:
    def test_acyclic_data_returns_an_empty_envelope(self):
        engine = _StubEngine([])

        assert find_dependency_cycles(engine, "demo") == {
            "count": 0,
            "results": [],
            "truncated": False,
        }

    def test_max_results_truncates_without_losing_the_count(self):
        engine = _StubEngine([_closed_path(f"n{i}", f"m{i}") for i in range(10)])

        result = find_dependency_cycles(engine, "demo", max_results=3)

        assert result["count"] == 10
        assert len(result["results"]) == 3
        assert result["truncated"] is True

    def test_max_results_is_clamped_to_at_least_one(self):
        engine = _StubEngine([_closed_path("a", "b"), _closed_path("c", "d")])

        result = find_dependency_cycles(engine, "demo", max_results=0)

        assert result["count"] == 2
        assert len(result["results"]) == 1

    def test_hitting_the_raw_path_cap_reports_truncated_even_when_count_is_small(self):
        """All 500 raw paths are rotations of one cycle, so the deduplicated
        count is 1 — well under max_results — but the expansion was clipped and
        cycles past the clip were never seen, so the count is a lower bound."""
        engine = _StubEngine([_closed_path("a", "b", "c")] * devgraph_tools._CYCLE_RAW_PATH_LIMIT)

        result = find_dependency_cycles(engine, "demo", max_results=15)

        assert result["count"] == 1
        assert len(result["results"]) == 1
        assert result["truncated"] is True

    def test_staying_under_the_raw_path_cap_does_not_report_truncated(self):
        rows = [_closed_path("a", "b", "c")] * (devgraph_tools._CYCLE_RAW_PATH_LIMIT - 1)
        engine = _StubEngine(rows)

        assert find_dependency_cycles(engine, "demo")["truncated"] is False

    def test_nested_node_dicts_are_sanitised(self):
        """The shared envelope sanitiser only reaches an item's own string
        values, never a nested list of dicts, so the nodes are sanitised by the
        tool itself — a corpus-derived name cannot smuggle control sequences
        or an oversized payload into the model's context."""
        engine = _StubEngine([
            _closed_path(_node("a\x07lert" + "x" * 900), _node("b", file="s\x00rc.py"))
        ])

        nodes = find_dependency_cycles(engine, "demo")["results"][0]["nodes"]

        by_file = {n["file"] for n in nodes}
        smuggled = next(n for n in nodes if n["name"].startswith("a"))
        assert "\x07" not in smuggled["name"]
        assert len(smuggled["name"]) == devgraph_tools._MAX_LABEL_LEN
        assert "src.py" in by_file


class TestServerRegistration:
    """Deliberately duplicated from tests/mcp/test_server.py, which asserts the
    same invariants against live Neo4j and therefore skips entirely when no
    database is present — the tool surface must not be able to regress
    unnoticed on a machine without one."""

    @pytest.fixture
    def server(self, tmp_path, monkeypatch):
        fake = Settings(registry_db_path=tmp_path / "registry.sqlite3")
        monkeypatch.setattr(mcp_server, "get_settings", lambda: fake)
        return mcp_server.build_server(_StubEngine(), _StubRegistry())

    def test_the_tool_is_registered_as_the_twenty_second_tool(self, server):
        tools = asyncio.run(server.list_tools())
        names = {t.name for t in tools}

        assert len(tools) == 22
        assert "find_dependency_cycles" in names

    def test_every_registered_tool_still_carries_the_read_only_annotation(self, server):
        for tool in asyncio.run(server.list_tools()):
            assert tool.annotations is not None, f"{tool.name} has no annotations"
            assert tool.annotations.read_only_hint is True, f"{tool.name} is not read_only_hint=True"

    def test_the_catalog_resource_matches_the_registered_tools(self, server):
        tool_names = {t.name for t in asyncio.run(server.list_tools())}
        content = asyncio.run(server.read_resource("devgraph://tool-catalog"))
        catalog = json.loads(content[0].content)

        assert {entry["name"] for entry in catalog} == tool_names

    def test_calling_the_tool_through_the_server_returns_the_envelope(self, server):
        result = asyncio.run(
            server.call_tool("find_dependency_cycles", {"repo_id": "demo"})
        )

        assert result.is_error is False
        assert result.structured_content == {"count": 0, "results": [], "truncated": False}

    def test_an_unsupported_relationship_never_reads_as_no_cycles(self, server):
        """However the SDK chooses to surface a raising tool — as an error
        result or as a propagating exception — what must not happen is the
        call coming back as a successful empty envelope, which reads as a
        clean bill of health on a typo'd argument."""
        try:
            result = asyncio.run(
                server.call_tool(
                    "find_dependency_cycles", {"repo_id": "demo", "relationship": "CONTAINS"}
                )
            )
        except Exception:
            return

        assert result.is_error is True
        assert result.structured_content != {"count": 0, "results": [], "truncated": False}
