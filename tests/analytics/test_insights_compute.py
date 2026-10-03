"""Pure graph-insights computation: no Neo4j, synthetic graphs only."""

import random

import pytest

from devgraph.analytics import insights
from devgraph.analytics.insights import compute_insights, pagerank


def node(nid, file=None, name=None, label="Function"):
    return {"id": nid, "name": name or nid, "labels": [label], "file": file}


def edge(source, target, kind="CALLS"):
    return {"source": source, "target": target, "type": kind}


# Two tightly knit groups (a triangle each) joined by one C -> D call.
TWO_SUBSYSTEMS_NODES = [
    node("A", "auth/a.py"), node("B", "auth/b.py"), node("C", "auth/c.py"),
    node("D", "billing/d.py"), node("E", "billing/e.py"), node("F", "billing/f.py"),
]
TWO_SUBSYSTEMS_EDGES = [
    edge("A", "B"), edge("B", "C"), edge("A", "C"),
    edge("D", "E"), edge("E", "F"), edge("D", "F"),
    edge("C", "D"),
]


def rows_by_id(result):
    return {row["id"]: row for row in result.node_rows}


def test_two_subsystems_become_two_labelled_communities():
    result = compute_insights(TWO_SUBSYSTEMS_NODES, TWO_SUBSYSTEMS_EDGES)
    assert result.communities == [
        {"community": 0, "label": "auth", "size": 3},
        {"community": 1, "label": "billing", "size": 3},
    ]
    rows = rows_by_id(result)
    assert {rows[n]["community"] for n in "ABC"} == {0}
    assert {rows[n]["community"] for n in "DEF"} == {1}
    assert result.node_count == 6
    assert result.modularity == pytest.approx(0.3571, abs=1e-3)


def test_bridge_endpoints_rank_highest_for_betweenness():
    rows = rows_by_id(compute_insights(TWO_SUBSYSTEMS_NODES, TWO_SUBSYSTEMS_EDGES))
    ranked = sorted(rows.values(), key=lambda r: -r["betweenness"])
    assert {ranked[0]["id"], ranked[1]["id"]} == {"C", "D"}
    assert ranked[0]["betweenness"] == pytest.approx(0.6)


def test_pagerank_favours_the_hub_and_sums_to_one():
    nodes = [node("H"), node("X1"), node("X2"), node("X3")]
    edges = [edge("X1", "H"), edge("X2", "H"), edge("X3", "H")]
    rows = rows_by_id(compute_insights(nodes, edges))
    assert max(rows.values(), key=lambda r: r["pagerank"])["id"] == "H"
    assert sum(r["pagerank"] for r in rows.values()) == pytest.approx(1.0)


def test_pagerank_handles_empty_and_dangling_graphs():
    assert pagerank([], []) == {}
    ranks = pagerank(["a", "b"], [("a", "b")])
    assert ranks["b"] > ranks["a"]
    assert sum(ranks.values()) == pytest.approx(1.0)


def test_containment_groups_members_but_does_not_rank_the_container():
    nodes = [node("M", "pkg/m.py", label="Module"), node("f1", "pkg/m.py"), node("f2", "pkg/m.py")]
    edges = [edge("M", "f1", "CONTAINS"), edge("M", "f2", "CONTAINS"), edge("f1", "f2")]
    rows = rows_by_id(compute_insights(nodes, edges))
    assert rows["M"]["community"] == rows["f1"]["community"] == rows["f2"]["community"] == 0
    assert rows["M"]["pagerank"] is None and rows["M"]["betweenness"] is None
    assert rows["f2"]["pagerank"] > rows["f1"]["pagerank"]


def test_history_edges_self_loops_and_foreign_endpoints_are_ignored():
    nodes = [node("A", "a.py"), node("B", "b.py")]
    edges = [edge("A", "B", "MODIFIES"), edge("A", "A"), edge("A", "ghost")]
    result = compute_insights(nodes, edges)
    assert result.node_rows == [] and result.communities == []
    assert result.modularity == 0.0 and result.node_count == 0


def test_empty_input_is_a_valid_empty_result():
    result = compute_insights([], [])
    assert (result.node_rows, result.communities, result.modularity, result.node_count) == ([], [], 0.0, 0)


def test_results_do_not_depend_on_input_order():
    first = compute_insights(TWO_SUBSYSTEMS_NODES, TWO_SUBSYSTEMS_EDGES)
    nodes, edges = list(TWO_SUBSYSTEMS_NODES), list(TWO_SUBSYSTEMS_EDGES)
    random.Random(7).shuffle(nodes)
    random.Random(7).shuffle(edges)
    assert compute_insights(nodes, edges) == first


def test_label_falls_back_to_the_top_member_when_no_member_has_a_file():
    nodes = [node("H", name="Hub"), node("X1"), node("X2")]
    edges = [edge("X1", "H"), edge("X2", "H")]
    assert compute_insights(nodes, edges).communities[0]["label"] == "Hub"


def test_files_at_the_repository_root_are_labelled_root():
    nodes = [node("A", "main.py"), node("B", "setup.py")]
    assert compute_insights(nodes, [edge("A", "B")]).communities[0]["label"] == "(root)"


def test_betweenness_is_sampled_only_above_256_nodes(monkeypatch):
    seen = []
    real = insights.nx.betweenness_centrality

    def spy(graph, **kwargs):
        seen.append(kwargs.get("k"))
        return real(graph, **kwargs)

    monkeypatch.setattr(insights.nx, "betweenness_centrality", spy)
    chain = lambda n: ([node(f"n{i:03}") for i in range(n)], [edge(f"n{i:03}", f"n{i + 1:03}") for i in range(n - 1)])
    compute_insights(*chain(10))
    compute_insights(*chain(300))
    assert seen == [None, 256]


def test_non_blocking_refresh_skips_while_a_run_holds_the_repo_lock():
    class NeverCalled:
        def load_insight_graph(self, *args):
            raise AssertionError("must not load while another run holds the lock")

    lock = insights._repo_lock("busy-repo")
    with lock:
        assert insights.refresh_insights(NeverCalled(), "busy-repo", blocking=False) is None
    assert insights._repo_lock("busy-repo") is lock


def test_read_insights_tolerates_a_corrupt_communities_value():
    class Stub:
        def read_insights_summary(self, repo_id):
            return {"computed_at": "2026-01-01T00:00:00+00:00", "node_count": 1, "community_count": 1,
                    "modularity": 0.0, "communities": "{not json"}

    assert insights.read_insights(Stub(), "r")["communities"] == []
