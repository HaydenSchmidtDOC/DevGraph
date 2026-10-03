# Graph Insights Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compute Louvain communities, PageRank and betweenness for each indexed repository, store them on the graph, and surface them through two MCP tools, a CLI command and the dashboard (Community card, PageRank leaderboard, color-by-community canvas toggle).

**Architecture:** `devgraph/analytics/insights.py` holds the pure computation (networkx Louvain + betweenness, hand-written PageRank), `refresh_insights` (load → compute → write under a per-repo lock), read helpers, and an `InsightsScheduler` thread that both agents run. `GraphEngine` gains three methods for the Neo4j side. MCP tools, a CLI command and dashboard routes all call into the analytics module.

**Tech Stack:** Python 3.13, networkx (new), neo4j driver, FastAPI, Typer, pytest; hand-written HTML/JS with Node for headless UI tests.

**Spec:** `docs/superpowers/specs/2026-10-03-graph-insights-design.md`

**Working directory for every command:** the repository root of this worktree (branch `feat/graph-insights`, cut from upstream `master`). Run Python with `uv run ...`. A local Neo4j 5.26 Community instance on `bolt://127.0.0.1:7687` (user `neo4j`, password `devgraph-local-dev`) backs the live tests; they skip when it's absent.

## Global Constraints

- Stock `neo4j:5.26-community`: no GDS, no APOC, no image change.
- Only new dependency: `networkx>=3.3` (pure Python). No scipy/numpy.
- Dependency edges: `CALLS, DEPENDS_ON, EXTENDS, IMPLEMENTS, IMPORTS, USES`; community edges add `CONTAINS`. `Repository` nodes never take part.
- Node properties: `insight_community` (int), `insight_pagerank` (float), `insight_betweenness` (float). Repository properties: `insights_computed_at` (UTC ISO-8601 from `datetime.now(timezone.utc).isoformat()`), `insights_node_count`, `insights_community_count`, `insights_modularity`, `insights_communities` (JSON string, at most 50 entries of `{community, label, size}`).
- Louvain `seed=0`; PageRank damping 0.85; betweenness sampled with `k=256, seed=0` only above 256 nodes. Results deterministic for the same graph.
- Scheduler interval 30 s; recompute when `last_indexed > insights_computed_at` or never computed.
- MCP tool count goes from 22 to 24 (`find_communities`, `key_nodes`); `god_nodes` unchanged.
- Dashboard POST is cross-site guarded (`_reject_cross_site`); 409 when a run is in progress; 503 with fixed text `graph unavailable` when Neo4j fails; never echo driver error text.
- Commit messages: plain imperative summary; no `Co-Authored-By` trailer, no mention of Claude/the assistant. Never commit `uv.lock` (untracked in this repo), real names or personal paths.

## Review Focus

1. A repository with no dependency or containment edges (fresh, docs-only) → computes successfully with 0 communities; MCP tools return an empty envelope, not the "not computed" error — pinned in Tasks 1 and 4.
2. Recompute pressed while the scheduler is mid-run for the same repo → 409, never two concurrent writes — pinned in Tasks 2 and 5.
3. Neo4j unreachable while the scheduler polls → the pass logs and the thread keeps running — pinned in Task 3.
4. A node's dependency edges disappear between runs → its old insight properties are cleared, not left stale — pinned in Task 2 (live).
5. Switching repos quickly in the dashboard → a slower response for the previous repo never overwrites the current repo's card — pinned in Task 6.

---

## File Structure

| File | Responsibility |
| :--- | :--- |
| `pyproject.toml` (modify) | add `networkx>=3.3` |
| `devgraph/analytics/__init__.py` (create) | package marker |
| `devgraph/analytics/insights.py` (create) | computation, refresh, read helpers, scheduler |
| `devgraph/graph/engine.py` (modify) | `load_insight_graph`, `write_insights`, `read_insights_summary` |
| `devgraph/agent/headless.py`, `devgraph/agent/tray.py` (modify) | run `InsightsScheduler`, publish `insights_refreshed` |
| `devgraph/cli/main.py` (modify) | `devgraph insights <repo_id>` |
| `devgraph/mcp/tools.py`, `devgraph/mcp/server.py` (modify) | `find_communities`, `key_nodes`, catalog |
| `devgraph/dashboard/routes.py` (modify) | GET/POST `/api/repos/{repo_id}/insights` |
| `devgraph/dashboard/static/index.html` (modify) | Community card, leaderboard basis, canvas toggle |
| `README.md`, `PROJECT_STATUS.md` (modify) | docs |
| `tests/analytics/__init__.py`, `tests/analytics/test_insights_compute.py`, `tests/analytics/test_insights_scheduler.py` (create) | unit tests |
| `tests/graph/test_engine_insights.py` (create) | live Neo4j round trip |
| `tests/mcp/test_tools_insights.py` (create) + count tests (modify) | MCP |
| `tests/dashboard/test_insights_routes.py`, `tests/dashboard/insights_ui.js`, `tests/dashboard/test_insights_ui.py` (create) | dashboard |
| `tests/cli/test_cli.py`, `tests/agent/test_insights_events.py` (modify/create) | CLI, agent wiring |

---

### Task 1: Pure computation

**Files:**
- Modify: `pyproject.toml` (dependencies list)
- Create: `devgraph/analytics/__init__.py`, `devgraph/analytics/insights.py`
- Test: `tests/analytics/__init__.py`, `tests/analytics/test_insights_compute.py`

**Interfaces:**
- Produces: `DEPENDENCY_RELATIONSHIPS: tuple[str, ...]`, `COMMUNITY_RELATIONSHIPS: tuple[str, ...]`, `InsightResult(node_rows: list[dict], communities: list[dict], modularity: float, node_count: int)`, `pagerank(node_ids: list[str], edges: list[tuple[str, str]]) -> dict[str, float]`, `compute_insights(nodes: list[dict], edges: list[dict]) -> InsightResult`.
  - `nodes` items: `{"id": str, "name": str | None, "labels": list[str], "file": str | None}`; `edges` items: `{"source": str, "target": str, "type": str}`.
  - `node_rows` items: `{"id", "community": int | None, "pagerank": float | None, "betweenness": float | None}`, sorted by id.
  - `communities` items: `{"community": int, "label": str, "size": int}`, largest first.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, add `"networkx>=3.3",` to `[project] dependencies` after `"uvicorn>=0.32",`. Run `uv sync --all-extras -q` and confirm `uv run python -c "import networkx; print(networkx.__version__)"` prints a version ≥ 3.3. (`uv.lock` is untracked here — do not commit it.)

- [ ] **Step 2: Write the failing tests**

Create empty `tests/analytics/__init__.py` and `tests/analytics/test_insights_compute.py`:

```python
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/analytics/test_insights_compute.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'devgraph.analytics'`.

- [ ] **Step 4: Implement the computation**

Create `devgraph/analytics/__init__.py` containing only `"""Derived analytics over the indexed graph."""`.

Create `devgraph/analytics/insights.py`:

```python
"""Graph insights: communities, PageRank and betweenness for one repository.

Computed in Python over the already-indexed graph -- stock Neo4j Community has
no Graph Data Science plugin, and the only dependency this adds is networkx
(pure Python). PageRank is a small hand-written power iteration because
networkx's own implementation needs scipy.

Results are written back onto the graph (see `GraphEngine.write_insights`),
the one store both the agent/dashboard process and the separate MCP server
process already read.
"""

from __future__ import annotations

import posixpath
from collections import Counter
from dataclasses import dataclass
from typing import Any

import networkx as nx

# Edges that mean "A depends on B", directed as stored: A CALLS B gives B
# importance. The cycle finder's dependency set plus IMPLEMENTS (an
# interface its implementations lean on).
DEPENDENCY_RELATIONSHIPS: tuple[str, ...] = ("CALLS", "DEPENDS_ON", "EXTENDS", "IMPLEMENTS", "IMPORTS", "USES")
# Communities also follow containment, so a file's members stay together
# and cross-file dependencies are what join files into subsystems.
COMMUNITY_RELATIONSHIPS: tuple[str, ...] = DEPENDENCY_RELATIONSHIPS + ("CONTAINS",)

_PAGERANK_DAMPING = 0.85
_PAGERANK_MAX_ITER = 200
_PAGERANK_TOLERANCE = 1e-10
# Exact betweenness is O(V*E); above this many nodes it is estimated from
# this many sampled sources, which keeps the ranking and bounds the cost.
_BETWEENNESS_SAMPLE = 256
_ROOT_LABEL = "(root)"
_LABEL_MAX_CHARS = 80


@dataclass
class InsightResult:
    node_rows: list[dict[str, Any]]
    communities: list[dict[str, Any]]
    modularity: float
    node_count: int


def pagerank(node_ids: list[str], edges: list[tuple[str, str]]) -> dict[str, float]:
    """PageRank by power iteration; dangling nodes spread their rank uniformly."""
    if not node_ids:
        return {}
    index = {nid: i for i, nid in enumerate(node_ids)}
    n = len(node_ids)
    out: list[list[int]] = [[] for _ in range(n)]
    for source, target in sorted(set(edges)):
        if source != target:
            out[index[source]].append(index[target])
    rank = [1.0 / n] * n
    for _ in range(_PAGERANK_MAX_ITER):
        dangling = sum(rank[i] for i in range(n) if not out[i])
        base = (1.0 - _PAGERANK_DAMPING) / n + _PAGERANK_DAMPING * dangling / n
        new = [base] * n
        for i in range(n):
            if out[i]:
                share = _PAGERANK_DAMPING * rank[i] / len(out[i])
                for j in out[i]:
                    new[j] += share
        delta = sum(abs(new[i] - rank[i]) for i in range(n))
        rank = new
        if delta < n * _PAGERANK_TOLERANCE:
            break
    return {nid: rank[index[nid]] for nid in node_ids}


def _community_label(members: list[dict[str, Any]], ranks: dict[str, float]) -> str:
    """Most common member directory, else the highest-PageRank member's name."""
    dirs = Counter(posixpath.dirname(m["file"]) or _ROOT_LABEL for m in members if m.get("file"))
    if dirs:
        top = max(dirs.values())
        label = min(d for d, count in dirs.items() if count == top)
    else:
        best = min(members, key=lambda m: (-ranks.get(m["id"], -1.0), m.get("name") or "", m["id"]))
        label = best.get("name") or best["id"]
    return label[:_LABEL_MAX_CHARS]


def compute_insights(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> InsightResult:
    """Communities over dependency+containment edges; PageRank and betweenness
    over dependency edges only. Inputs are sorted first so the same graph
    always gives the same answer, whatever order Neo4j returned it in."""
    by_id = {n["id"]: n for n in nodes}
    usable = sorted(
        (e["source"], e["target"], e["type"])
        for e in edges
        if e["type"] in COMMUNITY_RELATIONSHIPS
        and e["source"] in by_id
        and e["target"] in by_id
        and e["source"] != e["target"]
    )
    dependency = [(s, t) for s, t, kind in usable if kind in DEPENDENCY_RELATIONSHIPS]

    dep_nodes = sorted({nid for pair in dependency for nid in pair})
    ranks = pagerank(dep_nodes, dependency)
    dep_graph = nx.Graph()
    dep_graph.add_nodes_from(dep_nodes)
    dep_graph.add_edges_from(dependency)
    sample = _BETWEENNESS_SAMPLE if dep_graph.number_of_nodes() > _BETWEENNESS_SAMPLE else None
    betweenness = (
        nx.betweenness_centrality(dep_graph, k=sample, normalized=True, seed=0) if dep_nodes else {}
    )

    community_graph = nx.Graph()
    community_graph.add_edges_from((s, t) for s, t, _ in usable)
    if community_graph.number_of_edges():
        groups = nx.community.louvain_communities(community_graph, seed=0)
        modularity = float(nx.community.modularity(community_graph, groups))
    else:
        groups, modularity = [], 0.0
    ordered = sorted(
        (sorted(group) for group in groups),
        key=lambda members: (-len(members), min(by_id[m].get("name") or "" for m in members), members[0]),
    )

    community_of: dict[str, int] = {}
    communities: list[dict[str, Any]] = []
    for number, members in enumerate(ordered):
        for member in members:
            community_of[member] = number
        communities.append(
            {"community": number, "label": _community_label([by_id[m] for m in members], ranks), "size": len(members)}
        )

    scored = sorted(set(community_of) | set(ranks))
    node_rows = [
        {
            "id": nid,
            "community": community_of.get(nid),
            "pagerank": ranks.get(nid),
            "betweenness": betweenness.get(nid) if nid in ranks else None,
        }
        for nid in scored
    ]
    return InsightResult(node_rows, communities, modularity, len(scored))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/analytics/test_insights_compute.py -q`
Expected: all pass. (`test_results_do_not_depend_on_input_order` compares dataclasses with float fields for exact equality — that is intended: sorted inputs make the computation bit-for-bit repeatable.)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml devgraph/analytics/__init__.py devgraph/analytics/insights.py tests/analytics/__init__.py tests/analytics/test_insights_compute.py
git commit -m "Compute communities, PageRank and betweenness for a repository graph"
```

---

### Task 2: Store and read insights in Neo4j

**Files:**
- Modify: `devgraph/graph/engine.py` (Cypher constants near the other module-level constants; tx function next to `_replace_file_nodes_tx`; three methods after `delete_repository`)
- Modify: `devgraph/analytics/insights.py` (append)
- Test: `tests/graph/test_engine_insights.py` (create), `tests/analytics/test_insights_compute.py` (append lock test)

**Interfaces:**
- Consumes: `compute_insights`, `COMMUNITY_RELATIONSHIPS` (Task 1).
- Produces:
  - `GraphEngine.load_insight_graph(repo_id: str, relationship_types: tuple[str, ...]) -> tuple[list[dict], list[dict]]`
  - `GraphEngine.write_insights(repo_id: str, rows: list[dict], summary: dict) -> None` — `summary` keys: `computed_at, node_count, community_count, modularity, communities` (JSON string)
  - `GraphEngine.read_insights_summary(repo_id: str) -> dict | None` — same keys, `communities` still a JSON string
  - `insights.refresh_insights(engine, repo_id: str, *, blocking: bool = True) -> dict | None` — returns `{computed_at, node_count, community_count, modularity, communities: list[dict]}`, or `None` when `blocking=False` and a run is in progress
  - `insights.read_insights(engine, repo_id: str) -> dict | None` — summary with `communities` decoded to a list
  - `insights.INSIGHT_METRICS: dict[str, str]` = `{"pagerank": "insight_pagerank", "betweenness": "insight_betweenness"}`
  - `insights.top_nodes(engine, repo_id: str, metric: str, limit: int) -> list[dict]` — rows `{name, labels, file, score, community}`, best first
  - `insights.community_members(engine, repo_id: str, communities: list[int], per_community: int) -> dict[int, list[dict]]` — members `{name, labels, file, pagerank}`, highest PageRank first
  - `insights._repo_lock(repo_id: str) -> threading.Lock` (module-private; tests and Task 5 use it)

- [ ] **Step 1: Write the failing tests**

Create `tests/graph/test_engine_insights.py`:

```python
"""Live round trip of graph insights through Neo4j. Skips without Neo4j."""

import json

import pytest

from devgraph.analytics.insights import (
    COMMUNITY_RELATIONSHIPS,
    community_members,
    read_insights,
    refresh_insights,
    top_nodes,
)
from devgraph.graph.engine import GraphEngine

REPO = "_smoketest_graph_insights"


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.init_schema()
    test_engine.delete_repository(REPO)
    yield test_engine
    test_engine.delete_repository(REPO)
    test_engine.close()


def build_two_subsystems(engine):
    engine.upsert_repository(REPO, REPO, "/tmp/insights-demo")
    engine.run_cypher(
        "UNWIND $nodes AS row "
        "CREATE (n:Function {repo_id: $repo, name: row.name, file: row.file})",
        {
            "repo": REPO,
            "nodes": [
                {"name": "a", "file": "auth/a.py"}, {"name": "b", "file": "auth/b.py"}, {"name": "c", "file": "auth/c.py"},
                {"name": "d", "file": "billing/d.py"}, {"name": "e", "file": "billing/e.py"}, {"name": "f", "file": "billing/f.py"},
            ],
        },
    )
    engine.run_cypher(
        "UNWIND $pairs AS pair "
        "MATCH (s:Function {repo_id: $repo, name: pair[0]}), (t:Function {repo_id: $repo, name: pair[1]}) "
        "CREATE (s)-[:CALLS]->(t)",
        {"repo": REPO, "pairs": [["a", "b"], ["b", "c"], ["a", "c"], ["d", "e"], ["e", "f"], ["d", "f"], ["c", "d"]]},
    )
    # History edges and the Repository root must not take part.
    engine.run_cypher(
        "MATCH (r:Repository {repo_id: $repo}), (a:Function {repo_id: $repo, name: 'a'}) "
        "CREATE (c:Commit {repo_id: $repo, name: 'abc123'})-[:MODIFIES]->(a), (r)-[:CONTAINS]->(a)",
        {"repo": REPO},
    )


def test_load_returns_only_relevant_edges_and_their_nodes(engine):
    build_two_subsystems(engine)
    nodes, edges = engine.load_insight_graph(REPO, COMMUNITY_RELATIONSHIPS)
    assert sorted(n["name"] for n in nodes) == ["a", "b", "c", "d", "e", "f"]
    assert len(edges) == 7 and {e["type"] for e in edges} == {"CALLS"}


def test_refresh_writes_node_and_repository_properties(engine):
    build_two_subsystems(engine)
    summary = refresh_insights(engine, REPO)
    assert summary["community_count"] == 2 and summary["node_count"] == 6
    assert [c["label"] for c in summary["communities"]] == ["auth", "billing"]

    stored = read_insights(engine, REPO)
    assert stored["community_count"] == 2
    assert stored["communities"] == summary["communities"]
    assert stored["computed_at"] == summary["computed_at"]

    rows = engine.run_cypher(
        "MATCH (n:Function {repo_id: $repo}) RETURN n.name AS name, n.insight_community AS c, "
        "n.insight_pagerank AS pr, n.insight_betweenness AS bt ORDER BY name",
        {"repo": REPO},
    )
    assert [r["c"] for r in rows] == [0, 0, 0, 1, 1, 1]
    assert all(isinstance(r["pr"], float) and isinstance(r["bt"], float) for r in rows)
    commit = engine.run_cypher("MATCH (c:Commit {repo_id: $repo}) RETURN c.insight_community AS c", {"repo": REPO})
    assert commit == [{"c": None}]


def test_top_nodes_and_community_members(engine):
    build_two_subsystems(engine)
    refresh_insights(engine, REPO)
    bridges = top_nodes(engine, REPO, "betweenness", 2)
    assert {b["name"] for b in bridges} == {"c", "d"}
    assert set(bridges[0]) == {"name", "labels", "file", "score", "community"}
    members = community_members(engine, REPO, [0], 2)
    assert len(members[0]) == 2
    assert members[0][0]["pagerank"] >= members[0][1]["pagerank"]


def test_a_rerun_clears_properties_of_nodes_that_lost_their_edges(engine):
    build_two_subsystems(engine)
    refresh_insights(engine, REPO)
    engine.run_cypher("MATCH (:Function {repo_id: $repo})-[r:CALLS]->() DELETE r", {"repo": REPO})
    summary = refresh_insights(engine, REPO)
    assert summary["community_count"] == 0 and summary["node_count"] == 0
    leftover = engine.run_cypher(
        "MATCH (n {repo_id: $repo}) WHERE n.insight_community IS NOT NULL OR n.insight_pagerank IS NOT NULL "
        "OR n.insight_betweenness IS NOT NULL RETURN count(n) AS n",
        {"repo": REPO},
    )
    assert leftover == [{"n": 0}]


def test_read_insights_is_none_before_the_first_run(engine):
    engine.upsert_repository(REPO, REPO, "/tmp/insights-demo")
    assert read_insights(engine, REPO) is None


def test_communities_json_is_capped_at_fifty(engine):
    engine.upsert_repository(REPO, REPO, "/tmp/insights-demo")
    pairs = [[f"p{i}", f"q{i}"] for i in range(60)]
    engine.run_cypher(
        "UNWIND $pairs AS pair "
        "CREATE (:Function {repo_id: $repo, name: pair[0], file: pair[0] + '/x.py'})"
        "-[:CALLS]->(:Function {repo_id: $repo, name: pair[1], file: pair[0] + '/y.py'})",
        {"repo": REPO, "pairs": pairs},
    )
    summary = refresh_insights(engine, REPO)
    assert summary["community_count"] == 60
    raw = engine.read_insights_summary(REPO)
    assert len(json.loads(raw["communities"])) == 50
```

Append to `tests/analytics/test_insights_compute.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/graph/test_engine_insights.py tests/analytics/test_insights_compute.py -q`
Expected: ImportError for `community_members`/`read_insights`/`refresh_insights`/`top_nodes`, and `AttributeError` for `_repo_lock`.

- [ ] **Step 3: Add the engine methods**

In `devgraph/graph/engine.py`, add with the other module-level Cypher constants:

```python
# Graph insights (devgraph/analytics/insights.py). The Repository node is the
# scoping root, not code, so it never takes part in the graph that's analysed.
_LOAD_INSIGHT_EDGES_CYPHER = (
    "MATCH (a {repo_id: $repo_id})-[r]->(b {repo_id: $repo_id}) "
    "WHERE type(r) IN $types AND NOT a:Repository AND NOT b:Repository "
    "RETURN elementId(a) AS source, elementId(b) AS target, type(r) AS type"
)
_LOAD_INSIGHT_NODES_CYPHER = (
    "MATCH (n {repo_id: $repo_id}) WHERE elementId(n) IN $ids "
    "RETURN elementId(n) AS id, n.name AS name, labels(n) AS labels, n.file AS file"
)
_CLEAR_INSIGHTS_CYPHER = (
    "MATCH (n {repo_id: $repo_id}) "
    "WHERE n.insight_community IS NOT NULL OR n.insight_pagerank IS NOT NULL "
    "OR n.insight_betweenness IS NOT NULL "
    "REMOVE n.insight_community, n.insight_pagerank, n.insight_betweenness"
)
_WRITE_INSIGHTS_CYPHER = (
    "UNWIND $rows AS row MATCH (n) WHERE elementId(n) = row.id AND n.repo_id = $repo_id "
    "SET n.insight_community = row.community, n.insight_pagerank = row.pagerank, "
    "n.insight_betweenness = row.betweenness"
)
_WRITE_INSIGHTS_SUMMARY_CYPHER = (
    "MERGE (r:Repository {repo_id: $repo_id}) "
    "SET r.insights_computed_at = $computed_at, r.insights_node_count = $node_count, "
    "r.insights_community_count = $community_count, r.insights_modularity = $modularity, "
    "r.insights_communities = $communities"
)
_READ_INSIGHTS_SUMMARY_CYPHER = (
    "MATCH (r:Repository {repo_id: $repo_id}) WHERE r.insights_computed_at IS NOT NULL "
    "RETURN r.insights_computed_at AS computed_at, r.insights_node_count AS node_count, "
    "r.insights_community_count AS community_count, r.insights_modularity AS modularity, "
    "r.insights_communities AS communities"
)
```

Add next to `_replace_file_nodes_tx`:

```python
def _write_insights_tx(tx, repo_id: str, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    """Clear-then-write in one transaction, so a reader never sees a repo
    half old and half new, and a node that lost its edges loses its scores."""
    tx.run(_CLEAR_INSIGHTS_CYPHER, repo_id=repo_id)
    if rows:
        tx.run(_WRITE_INSIGHTS_CYPHER, repo_id=repo_id, rows=rows)
    tx.run(_WRITE_INSIGHTS_SUMMARY_CYPHER, repo_id=repo_id, **summary)
```

Add as `GraphEngine` methods after `delete_repository`:

```python
    def load_insight_graph(
        self, repo_id: str, relationship_types: tuple[str, ...]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """One repository's edges of `relationship_types` and the nodes they touch.

        Identity is `elementId`, which is only promised stable within a
        transaction; it is used for the `write_insights` that immediately
        follows, and a node deleted in between is simply not matched there.
        """
        with self._driver.session() as session:
            edge_result = _retry_transient(
                session.run, _LOAD_INSIGHT_EDGES_CYPHER, repo_id=repo_id, types=list(relationship_types)
            )
            edges = [record.data() for record in edge_result or []]
            ids = sorted({e["source"] for e in edges} | {e["target"] for e in edges})
            node_result = _retry_transient(session.run, _LOAD_INSIGHT_NODES_CYPHER, repo_id=repo_id, ids=ids)
            nodes = [record.data() for record in node_result or []]
        return nodes, edges

    def write_insights(self, repo_id: str, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
        """Replace a repository's insight properties (see `_write_insights_tx`)."""
        with self._driver.session() as session:
            session.execute_write(_write_insights_tx, repo_id, rows, summary)

    def read_insights_summary(self, repo_id: str) -> dict[str, Any] | None:
        """The Repository node's insight summary, or None if never computed."""
        with self._driver.session() as session:
            result = _retry_transient(session.run, _READ_INSIGHTS_SUMMARY_CYPHER, repo_id=repo_id)
            records = [record.data() for record in result or []]
        return records[0] if records else None
```

- [ ] **Step 4: Add refresh and read helpers**

In `devgraph/analytics/insights.py`, add `import json`, `import logging`, `import threading` and `from datetime import datetime, timezone` to the imports, `logger = logging.getLogger(__name__)` after them, and append:

```python
# ── storage ──────────────────────────────────────────────────────────────

# The Repository node keeps the largest communities for listing; every node
# still carries its own community number.
_MAX_STORED_COMMUNITIES = 50

INSIGHT_METRICS: dict[str, str] = {"pagerank": "insight_pagerank", "betweenness": "insight_betweenness"}

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _repo_lock(repo_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(repo_id, threading.Lock())


def refresh_insights(engine: Any, repo_id: str, *, blocking: bool = True) -> dict[str, Any] | None:
    """Load, compute and store one repository's insights.

    Serialized per repository within this process (the agent's scheduler and
    the dashboard share one). With `blocking=False`, returns None instead of
    waiting when a run is already in progress.
    """
    lock = _repo_lock(repo_id)
    if not lock.acquire(blocking=blocking):
        return None
    try:
        nodes, edges = engine.load_insight_graph(repo_id, COMMUNITY_RELATIONSHIPS)
        result = compute_insights(nodes, edges)
        stored = result.communities[:_MAX_STORED_COMMUNITIES]
        summary = {
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "node_count": result.node_count,
            "community_count": len(result.communities),
            "modularity": result.modularity,
            "communities": json.dumps(stored),
        }
        engine.write_insights(repo_id, result.node_rows, summary)
        logger.info(
            "graph insights for %s: %d communities over %d nodes", repo_id, len(result.communities), result.node_count
        )
        return {**summary, "communities": stored}
    finally:
        lock.release()


def read_insights(engine: Any, repo_id: str) -> dict[str, Any] | None:
    """The stored summary with `communities` decoded, or None if never computed."""
    raw = engine.read_insights_summary(repo_id)
    if raw is None:
        return None
    try:
        communities = json.loads(raw.get("communities") or "[]")
    except ValueError:
        communities = []
    if not isinstance(communities, list):
        communities = []
    return {**raw, "communities": communities}


def top_nodes(engine: Any, repo_id: str, metric: str, limit: int) -> list[dict[str, Any]]:
    """Highest-scoring nodes for one of INSIGHT_METRICS (validated by callers).

    The property name comes from the INSIGHT_METRICS allow-list, never from
    caller input, so it is the only thing interpolated into the query.
    """
    prop = INSIGHT_METRICS[metric]
    return engine.run_cypher(
        f"MATCH (n {{repo_id: $repo_id}}) WHERE n.{prop} IS NOT NULL "
        f"RETURN n.name AS name, labels(n) AS labels, n.file AS file, n.{prop} AS score, "
        f"n.insight_community AS community ORDER BY score DESC, name LIMIT $limit",
        {"repo_id": repo_id, "limit": limit},
    )


def community_members(
    engine: Any, repo_id: str, communities: list[int], per_community: int
) -> dict[int, list[dict[str, Any]]]:
    """Each community's top members by PageRank (containers without a score last)."""
    rows = engine.run_cypher(
        "MATCH (n {repo_id: $repo_id}) WHERE n.insight_community IN $communities "
        "WITH n ORDER BY coalesce(n.insight_pagerank, -1.0) DESC, n.name "
        "WITH n.insight_community AS community, "
        "collect({name: n.name, labels: labels(n), file: n.file, pagerank: n.insight_pagerank})[..$k] AS members "
        "RETURN community, members",
        {"repo_id": repo_id, "communities": communities, "k": per_community},
    )
    return {row["community"]: row["members"] for row in rows}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/graph/test_engine_insights.py tests/analytics -q`
Expected: all pass with Neo4j running (the live file skips without it — if it skips, start Neo4j and rerun; a skip is not a pass for this task).

- [ ] **Step 6: Commit**

```bash
git add devgraph/graph/engine.py devgraph/analytics/insights.py tests/graph/test_engine_insights.py tests/analytics/test_insights_compute.py
git commit -m "Store graph insights on nodes and the repository"
```

---

### Task 3: Scheduler, agent wiring and CLI command

**Files:**
- Modify: `devgraph/analytics/insights.py` (append `InsightsScheduler`)
- Modify: `devgraph/agent/headless.py`, `devgraph/agent/tray.py`
- Modify: `devgraph/cli/main.py` (new command after `rescan`)
- Test: `tests/analytics/test_insights_scheduler.py` (create), `tests/agent/test_insights_events.py` (create), `tests/cli/test_cli.py` (append)

**Interfaces:**
- Consumes: `refresh_insights`, `engine.read_insights_summary` (Task 2); `RepoRegistry.list_repos(active_only=True)` returning `RepoRecord`s with `repo_id` and `last_indexed: str | None`.
- Produces: `InsightsScheduler(engine, registry, on_refreshed: Callable[[str], None] | None = None, *, interval_s: float = 30.0)` with `.is_stale(repo) -> bool`, `.run_once() -> list[str]`, `.running: bool`, `.start()`, `.stop()`; agents' `_on_insights_refreshed(repo_id)` publishing `{"type": "insights_refreshed", "repo_id": repo_id}`; CLI `devgraph insights <repo_id>`.

- [ ] **Step 1: Write the failing tests**

Create `tests/analytics/test_insights_scheduler.py`:

```python
"""InsightsScheduler decisions, with stub engine and registry."""

import threading
from dataclasses import dataclass

from devgraph.analytics import insights
from devgraph.analytics.insights import InsightsScheduler


@dataclass
class Repo:
    repo_id: str
    last_indexed: str | None


class Registry:
    def __init__(self, repos):
        self.repos = repos

    def list_repos(self, active_only=False):
        assert active_only is True
        return list(self.repos)


class Engine:
    """Summaries per repo; records refreshes; can fail on demand."""

    def __init__(self, computed_at=None, fail_read=()):
        self.computed_at = dict(computed_at or {})
        self.fail_read = set(fail_read)

    def read_insights_summary(self, repo_id):
        if repo_id in self.fail_read:
            raise ConnectionError("neo4j down")
        at = self.computed_at.get(repo_id)
        return None if at is None else {"computed_at": at}


def fake_refresh(calls, result=True):
    def refresh(engine, repo_id, *, blocking=True):
        calls.append((repo_id, blocking))
        return {"computed_at": "x"} if result else None

    return refresh


def test_refreshes_never_computed_and_out_of_date_repos_only(monkeypatch):
    calls = []
    monkeypatch.setattr(insights, "refresh_insights", fake_refresh(calls))
    registry = Registry([
        Repo("never", "2026-10-01T10:00:00+00:00"),
        Repo("stale", "2026-10-01T10:00:00+00:00"),
        Repo("fresh", "2026-10-01T10:00:00+00:00"),
        Repo("unindexed", None),
    ])
    engine = Engine({"stale": "2026-10-01T09:00:00+00:00", "fresh": "2026-10-01T11:00:00+00:00"})
    refreshed = []
    scheduler = InsightsScheduler(engine, registry, on_refreshed=refreshed.append)
    assert scheduler.run_once() == ["never", "stale"]
    assert calls == [("never", False), ("stale", False)]
    assert refreshed == ["never", "stale"]


def test_a_repo_already_being_computed_is_skipped_not_reported(monkeypatch):
    calls = []
    monkeypatch.setattr(insights, "refresh_insights", fake_refresh(calls, result=False))
    scheduler = InsightsScheduler(Engine(), Registry([Repo("busy", "2026-10-01T10:00:00+00:00")]))
    assert scheduler.run_once() == []
    assert calls == [("busy", False)]


def test_one_failing_repo_does_not_stop_the_others(monkeypatch):
    calls = []
    monkeypatch.setattr(insights, "refresh_insights", fake_refresh(calls))
    registry = Registry([Repo("down", "2026-10-01T10:00:00+00:00"), Repo("ok", "2026-10-01T10:00:00+00:00")])
    scheduler = InsightsScheduler(Engine(fail_read={"down"}), registry)
    assert scheduler.run_once() == ["ok"]


def test_a_failing_callback_does_not_stop_the_pass(monkeypatch):
    monkeypatch.setattr(insights, "refresh_insights", fake_refresh([]))
    registry = Registry([Repo("a", "2026-10-01T10:00:00+00:00"), Repo("b", "2026-10-01T10:00:00+00:00")])

    def explode(repo_id):
        raise RuntimeError("subscriber gone")

    assert InsightsScheduler(Engine(), registry, on_refreshed=explode).run_once() == ["a", "b"]


def test_thread_keeps_running_through_failed_passes_and_stops():
    passes = []
    ran_twice = threading.Event()

    class FlakyRegistry:
        def list_repos(self, active_only=False):
            passes.append(1)
            if len(passes) >= 2:
                ran_twice.set()
            raise ConnectionError("registry unavailable")

    scheduler = InsightsScheduler(Engine(), FlakyRegistry(), interval_s=0.01)
    scheduler.start()
    try:
        assert scheduler.running
        assert ran_twice.wait(2), "scheduler died after a failed pass"
    finally:
        scheduler.stop()
    assert not scheduler.running
```

Create `tests/agent/test_insights_events.py`:

```python
"""Both agents announce refreshed insights on their dashboard event stream."""

from devgraph.agent.headless import HeadlessAgent
from devgraph.agent.tray import TrayApp


class Events:
    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)


def test_headless_agent_publishes_insights_refreshed():
    agent = HeadlessAgent.__new__(HeadlessAgent)
    agent._events = Events()
    agent._on_insights_refreshed("demo")
    assert agent._events.published == [{"type": "insights_refreshed", "repo_id": "demo"}]


def test_tray_app_publishes_insights_refreshed():
    app = TrayApp.__new__(TrayApp)
    app._events = Events()
    app._on_insights_refreshed("demo")
    assert app._events.published == [{"type": "insights_refreshed", "repo_id": "demo"}]
```

Append to `tests/cli/test_cli.py` (it already defines `runner`, `temp_registry_db`, `temp_git_repo`, `require_neo4j`, `_mock_settings`, and imports `patch`, `app`, `config_module`):

```python
def test_cli_insights_unknown_repo(runner, temp_registry_db):
    db_path, _ = temp_registry_db
    from devgraph.cli import main as cli_main

    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
         patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)):
        result = runner.invoke(app, ["insights", "no-such-repo"])
    assert result.exit_code == 1
    assert "no such repo_id" in result.stdout


def test_cli_insights_computes_for_a_registered_repo(runner, temp_git_repo, temp_registry_db, require_neo4j):
    db_path, registry = temp_registry_db
    repo_id = registry.add_repo(temp_git_repo).repo_id
    from devgraph.cli import main as cli_main
    from devgraph.graph.engine import GraphEngine

    engine = GraphEngine("bolt://127.0.0.1:7687", "neo4j", "devgraph-local-dev")
    try:
        engine.upsert_repository(repo_id, repo_id, str(temp_git_repo))
        engine.run_cypher(
            "CREATE (:Function {repo_id: $r, name: 'a', file: 'x/a.py'})-[:CALLS]->"
            "(:Function {repo_id: $r, name: 'b', file: 'x/b.py'})",
            {"r": repo_id},
        )
        config_module.get_settings.cache_clear()
        with patch.object(config_module, "get_settings", return_value=_mock_settings(db_path)), \
             patch.object(cli_main, "get_settings", return_value=_mock_settings(db_path)):
            result = runner.invoke(app, ["insights", repo_id])
        assert result.exit_code == 0, result.stdout
        output = " ".join(result.stdout.split())  # Rich may wrap the line at the runner's width
        assert "1 communities" in output and "2 nodes" in output
    finally:
        engine.delete_repository(repo_id)
        engine.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/analytics/test_insights_scheduler.py tests/agent/test_insights_events.py tests/cli/test_cli.py -q -k "insights or Insights"`
Expected: ImportError for `InsightsScheduler`; AttributeError for `_on_insights_refreshed`; CLI exit code 2 ("No such command 'insights'").

- [ ] **Step 3: Implement the scheduler**

In `devgraph/analytics/insights.py`, add `from collections.abc import Callable` to the imports and append:

```python
# ── scheduling ───────────────────────────────────────────────────────────

_SCHEDULER_INTERVAL_S = 30.0


class InsightsScheduler:
    """Keeps every active repository's insights no older than its last index.

    One rule covers every way a repository gets indexed -- watcher batches in
    this agent, CLI rescans, dashboard registrations -- because they all
    stamp `last_indexed` in the registry. The first pass runs as soon as the
    thread starts, which also backfills repositories never computed.
    """

    def __init__(
        self,
        engine: Any,
        registry: Any,
        on_refreshed: Callable[[str], None] | None = None,
        *,
        interval_s: float = _SCHEDULER_INTERVAL_S,
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._on_refreshed = on_refreshed
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None

    def is_stale(self, repo: Any) -> bool:
        if not repo.last_indexed:
            return False
        summary = self._engine.read_insights_summary(repo.repo_id)
        # Both are UTC ISO-8601 strings from datetime.isoformat(), so they
        # order correctly as strings.
        return summary is None or (summary.get("computed_at") or "") < repo.last_indexed

    def run_once(self) -> list[str]:
        refreshed: list[str] = []
        for repo in self._registry.list_repos(active_only=True):
            if self._stop.is_set():
                break
            try:
                if not self.is_stale(repo):
                    continue
                if refresh_insights(self._engine, repo.repo_id, blocking=False) is None:
                    continue  # another caller is computing it right now
            except Exception:
                logger.warning("graph insights refresh failed for %s", repo.repo_id, exc_info=True)
                continue
            refreshed.append(repo.repo_id)
            if self._on_refreshed is not None:
                try:
                    self._on_refreshed(repo.repo_id)
                except Exception:
                    logger.debug("insights_refreshed callback failed for %s", repo.repo_id, exc_info=True)
        return refreshed

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="devgraph-insights", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            # A refresh blocked on a slow database can outlast this; the
            # thread is a daemon, so shutdown is never held hostage.
            thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.warning("graph insights pass failed", exc_info=True)
            self._stop.wait(self._interval_s)
```

- [ ] **Step 4: Wire both agents**

In `devgraph/agent/headless.py`:
1. Import: `from devgraph.analytics.insights import InsightsScheduler`.
2. In `__init__`, right after `self._events = EventBroadcaster()`:
   ```python
           self._insights = InsightsScheduler(self._engine, self._registry, on_refreshed=self._on_insights_refreshed)
   ```
3. Add a method after `_on_changes`:
   ```python
       def _on_insights_refreshed(self, repo_id: str) -> None:
           self._events.publish({"type": "insights_refreshed", "repo_id": repo_id})
   ```
4. In `start()`, right after `self._watcher.start()`: `self._insights.start()`.
5. In `stop()`, right after `self._watcher.stop()`: `self._insights.stop()`.

In `devgraph/agent/tray.py`, do the same: the import; the `self._insights = ...` line right after `self._events = EventBroadcaster()` in `__init__`; the identical `_on_insights_refreshed` method after `_on_changes`; `self._insights.start()` right after `self._watcher.start()` in `start()`; and `self._insights.stop()` right after **every** `self._watcher.stop()` that is part of shutting down (`_quit`, and the pystray-crash path in `start()`). Do **not** add it after the `self._watcher.stop()` in the pause/resume toggle (~line 268) — pausing watching should not stop insights. Read each `_watcher.stop()` site before editing to tell them apart.

- [ ] **Step 5: Add the CLI command**

In `devgraph/cli/main.py`, add after the `rescan` command (match the module's existing imports of `GraphEngine` and `get_settings`):

```python
@app.command()
def insights(repo_id: str) -> None:
    """Compute graph insights (communities, PageRank, betweenness) for a repository now.

    The DevGraph agent recomputes them automatically after indexing; this
    runs the same computation on demand, with or without the agent.

    Args:
        repo_id: The repository ID.
    """
    # Imported here so networkx only loads for this command, not every CLI call.
    from devgraph.analytics.insights import refresh_insights

    try:
        registry = _get_registry()
        try:
            if registry.get(repo_id) is None:
                console.print(f"[red][X] Error:[/red] no such repo_id: {repo_id}")
                raise typer.Exit(code=1)
            settings = get_settings()
            engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
            try:
                summary = refresh_insights(engine, repo_id)
            finally:
                engine.close()
        finally:
            registry.close()
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[red][X] Error:[/red] {e}")
        raise typer.Exit(code=1)
    console.print(
        f"[green][OK][/green] Insights for {repo_id}: {summary['community_count']} communities "
        f"(modularity {summary['modularity']:.2f}) over {summary['node_count']} nodes"
    )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/analytics tests/agent tests/cli -q`
Expected: all pass (the live CLI test needs Neo4j).

- [ ] **Step 7: Commit**

```bash
git add devgraph/analytics/insights.py devgraph/agent/headless.py devgraph/agent/tray.py devgraph/cli/main.py \
  tests/analytics/test_insights_scheduler.py tests/agent/test_insights_events.py tests/cli/test_cli.py
git commit -m "Recompute graph insights after indexing and add the insights command"
```

---

### Task 4: MCP tools

**Files:**
- Modify: `devgraph/mcp/tools.py` (import + two functions after `find_dependency_cycles`)
- Modify: `devgraph/mcp/server.py` (two catalog entries after `find_dependency_cycles`'s; two registrations after its `@server.tool`)
- Modify: `tests/mcp/test_server.py` (~line 50), `tests/mcp/test_server_telemetry.py` (~line 266), `tests/mcp/test_tools_cycles.py` (~line 386)
- Test: `tests/mcp/test_tools_insights.py` (create)

**Interfaces:**
- Consumes: `read_insights`, `top_nodes`, `community_members`, `INSIGHT_METRICS` (Task 2).
- Produces: `tools.find_communities(engine, repo_id, max_results=10, members_per_community=5) -> dict`; `tools.key_nodes(engine, repo_id, metric="pagerank", max_results=10) -> dict`; MCP tools of the same names.

- [ ] **Step 1: Write the failing tests**

Create `tests/mcp/test_tools_insights.py`:

```python
"""find_communities and key_nodes: stub engines, no Neo4j."""

import asyncio
import json

import pytest

from devgraph.config.settings import Settings
from devgraph.mcp import server as mcp_server
from devgraph.mcp.tools import find_communities, key_nodes

COMMUNITIES = [
    {"community": 0, "label": "auth", "size": 3},
    {"community": 1, "label": "billing", "size": 3},
    {"community": 2, "label": "misc", "size": 1},
]


class StubEngine:
    def __init__(self, summary="computed", rows=None):
        self.summary = summary
        self.rows = rows if rows is not None else []
        self.queries = []

    def read_insights_summary(self, repo_id):
        if self.summary is None:
            return None
        return {"computed_at": "2026-10-01T00:00:00+00:00", "node_count": 7, "community_count": 3,
                "modularity": 0.36, "communities": json.dumps(COMMUNITIES)}

    def run_cypher(self, query, params=None):
        self.queries.append((query, params or {}))
        return self.rows


def test_communities_with_members_for_the_shown_ones_only():
    members = [{"community": 0, "members": [{"name": "login\x07", "labels": ["Function"], "file": "auth/a.py", "pagerank": 0.4}]},
               {"community": 1, "members": []}]
    engine = StubEngine(rows=members)
    result = find_communities(engine, "demo", max_results=2, members_per_community=3)
    assert result["count"] == 3 and result["truncated"] is True
    assert [r["label"] for r in result["results"]] == ["auth", "billing"]
    assert result["results"][0]["top_members"][0]["name"] == "login"  # control char stripped
    assert result["results"][1]["top_members"] == []
    (query, params), = engine.queries
    assert params["communities"] == [0, 1] and params["k"] == 3


def test_members_per_community_is_clamped():
    engine = StubEngine(rows=[])
    find_communities(engine, "demo", members_per_community=500)
    assert engine.queries[0][1]["k"] == 20
    find_communities(engine, "demo", members_per_community=0)
    assert engine.queries[1][1]["k"] == 1


def test_never_computed_is_an_error_that_names_the_command():
    with pytest.raises(ValueError, match="devgraph insights"):
        find_communities(StubEngine(summary=None), "demo")
    with pytest.raises(ValueError, match="devgraph insights"):
        key_nodes(StubEngine(summary=None), "demo")


def test_computed_but_empty_is_an_empty_envelope_not_an_error():
    class Empty(StubEngine):
        def read_insights_summary(self, repo_id):
            return {"computed_at": "x", "node_count": 0, "community_count": 0, "modularity": 0.0, "communities": "[]"}

    assert find_communities(Empty(), "demo") == {"count": 0, "results": [], "truncated": False}
    assert key_nodes(Empty(), "demo") == {"count": 0, "results": [], "truncated": False}


@pytest.mark.parametrize(("metric", "prop"), [("pagerank", "insight_pagerank"), ("BETWEENNESS", "insight_betweenness")])
def test_key_nodes_queries_the_allow_listed_property(metric, prop):
    engine = StubEngine(rows=[{"name": "hub", "labels": ["Class"], "file": "a.py", "score": 0.5, "community": 0}])
    result = key_nodes(engine, "demo", metric=metric, max_results=5)
    assert result["count"] == 1 and result["results"][0]["name"] == "hub"
    (query, params), = engine.queries
    assert f"n.{prop}" in query and params["repo_id"] == "demo"


@pytest.mark.parametrize("metric", ["degree", "pagerank; MATCH (x) DETACH DELETE x", "", None, 3])
def test_key_nodes_rejects_other_metrics_without_echoing_them(metric):
    engine = StubEngine()
    with pytest.raises(ValueError) as excinfo:
        key_nodes(engine, "demo", metric=metric)
    assert "DETACH" not in str(excinfo.value)
    assert engine.queries == []


class _StubRegistry:
    def get(self, repo_id):
        return None


@pytest.fixture
def server(tmp_path, monkeypatch):
    fake = Settings(registry_db_path=tmp_path / "registry.sqlite3")
    monkeypatch.setattr(mcp_server, "get_settings", lambda: fake)
    return mcp_server.build_server(StubEngine(rows=[]), _StubRegistry())


def test_both_tools_are_registered_read_only_and_catalogued(server):
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    for name in ("find_communities", "key_nodes"):
        assert tools[name].annotations.read_only_hint is True
    catalog = json.loads(asyncio.run(server.read_resource("devgraph://tool-catalog"))[0].content)
    assert {"find_communities", "key_nodes"} <= {entry["name"] for entry in catalog}
```

Update the count tests:
- `tests/mcp/test_server.py`: rename `test_all_22_tools_registered` to `test_all_24_tools_registered` and change `assert len(tools) == 22` to `24`.
- `tests/mcp/test_server_telemetry.py`: change `assert len(tools) == 22` to `24` in `test_instrumentation_leaves_the_registered_tool_surface_unchanged`.
- `tests/mcp/test_tools_cycles.py`: rename `test_the_tool_is_registered_as_the_twenty_second_tool` to `test_the_tool_is_registered` and change `assert len(tools) == 22` to `24`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/mcp -q`
Expected: ImportError for `find_communities`/`key_nodes`; the three count tests fail with `22 != 24`.

- [ ] **Step 3: Implement the tool functions**

In `devgraph/mcp/tools.py`, add the import `from devgraph.analytics.insights import INSIGHT_METRICS, community_members, read_insights, top_nodes` with the other `devgraph` imports, and add after `find_dependency_cycles`:

```python
_INSIGHTS_NOT_COMPUTED = (
    "graph insights have not been computed for this repository yet; the DevGraph agent "
    "computes them after indexing, or run `devgraph insights <repo_id>`"
)
_MAX_MEMBERS_PER_COMMUNITY = 20
# Rows pulled for key_nodes before the envelope trims to max_results.
_KEY_NODES_LIMIT = 50


def find_communities(
    engine: GraphEngine,
    repo_id: str,
    max_results: int = 10,
    members_per_community: int = 5,
) -> dict[str, Any]:
    """Return the repository's communities (Louvain over dependency and
    containment edges), largest first.

    Each result is {community, label, size, top_members}; `top_members`
    (highest PageRank first, each {name, labels, file, pagerank}) is filled
    for the communities inside max_results. `count` covers the largest 50
    communities the repository stores.

    Raises:
        ValueError: insights have never been computed for this repository.
    """
    summary = read_insights(engine, repo_id)
    if summary is None:
        raise ValueError(_INSIGHTS_NOT_COMPUTED)
    communities = summary["communities"]
    shown = [c["community"] for c in communities[:max(0, max_results)]]
    k = max(1, min(members_per_community, _MAX_MEMBERS_PER_COMMUNITY))
    members = community_members(engine, repo_id, shown, k) if shown else {}
    # Members are nested dicts, which _envelope's per-row sanitizing doesn't
    # reach, so they are sanitized here.
    rows = [
        {**c, "top_members": [_sanitize_row(m) for m in members.get(c["community"], [])]}
        if c["community"] in shown
        else c
        for c in communities
    ]
    return _envelope(rows, max_results)


def key_nodes(
    engine: GraphEngine,
    repo_id: str,
    metric: str = "pagerank",
    max_results: int = 10,
) -> dict[str, Any]:
    """Rank the repository's entities by PageRank over dependency edges (core
    abstractions) or betweenness (bridges between subsystems).

    Returns {count, results, truncated} of {name, labels, file, score,
    community}. `metric` is "pagerank" or "betweenness"; the property it
    selects comes from an allow-list, never from the argument itself.

    Raises:
        ValueError: unknown metric, or insights never computed.
    """
    metric_key = metric.strip().lower() if isinstance(metric, str) else ""
    if metric_key not in INSIGHT_METRICS:
        raise ValueError(f"metric must be one of: {', '.join(INSIGHT_METRICS)}")
    if read_insights(engine, repo_id) is None:
        raise ValueError(_INSIGHTS_NOT_COMPUTED)
    return _envelope(top_nodes(engine, repo_id, metric_key, _KEY_NODES_LIMIT), max_results)
```

Note: `test_key_nodes_rejects_other_metrics_without_echoing_them` asserts no query runs for a bad metric, so the metric check comes before `read_insights` — but `read_insights` calls `read_insights_summary`, not `run_cypher`, so either order satisfies `engine.queries == []`; keep the metric check first anyway (cheaper, and no database round trip for a malformed call).

- [ ] **Step 4: Register the tools**

In `devgraph/mcp/server.py`, add to `_TOOL_CATALOG` right after the `find_dependency_cycles` entry:

```python
    {"name": "find_communities", "identifier_kind": None, "envelope": True, "phase": 3, "note": "requires computed graph insights (automatic after indexing, or `devgraph insights`)"},
    {"name": "key_nodes", "identifier_kind": "metric (pagerank/betweenness), not a component name", "envelope": True, "phase": 3, "note": "requires computed graph insights (automatic after indexing, or `devgraph insights`)"},
```

and right after the `find_dependency_cycles` registration in `build_server`:

```python
    @server.tool(annotations=_READ_ONLY)
    def find_communities(
        repo_id: str,
        max_results: int = 10,
        members_per_community: int = 5,
    ) -> dict[str, Any]:
        """Return the repository's subsystems: Louvain communities over dependency and
        containment edges, largest first, as {count, results, truncated} of
        {community, label, size, top_members}. top_members (highest PageRank first) is
        filled for the communities within max_results. Errors if graph insights have
        never been computed for the repository (the agent computes them after indexing)."""
        return devgraph_tools.find_communities(engine, repo_id, max_results, members_per_community)

    @server.tool(annotations=_READ_ONLY)
    def key_nodes(
        repo_id: str,
        metric: str = "pagerank",
        max_results: int = 10,
    ) -> dict[str, Any]:
        """Rank the repository's entities by PageRank over dependency edges (the core
        abstractions everything leans on) or by betweenness (bridges between subsystems,
        where a change ripples furthest); returns {count, results, truncated} of
        {name, labels, file, score, community}. metric is pagerank or betweenness.
        Errors if graph insights have never been computed for the repository."""
        return devgraph_tools.key_nodes(engine, repo_id, metric, max_results)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/mcp -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add devgraph/mcp/tools.py devgraph/mcp/server.py tests/mcp/test_tools_insights.py \
  tests/mcp/test_server.py tests/mcp/test_server_telemetry.py tests/mcp/test_tools_cycles.py
git commit -m "Add find_communities and key_nodes MCP tools"
```

---

### Task 5: Dashboard routes

**Files:**
- Modify: `devgraph/dashboard/routes.py` (import; two constants near `_SSE_KEEPALIVE_S`; helper + two routes inside `build_router`, after the `/repos/{repo_id}/graph` route)
- Test: `tests/dashboard/test_insights_routes.py` (create)

**Interfaces:**
- Consumes: `read_insights`, `refresh_insights`, `top_nodes`, `_repo_lock` (Task 2).
- Produces: `GET /api/repos/{repo_id}/insights` and `POST /api/repos/{repo_id}/insights`, both returning `{"computed": False}` or `{"computed": True, "computed_at", "node_count", "community_count", "modularity", "communities": [≤8 {community,label,size}], "key_nodes": [≤6 {name,labels,file,score,community}], "bridges": [≤6 same]}`; POST publishes `{"type": "insights_refreshed", "repo_id"}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/dashboard/test_insights_routes.py`:

```python
"""GET/POST /api/repos/{repo_id}/insights with stub engine and registry."""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from devgraph.analytics import insights
from devgraph.dashboard.routes import build_router

COMMUNITIES = [{"community": i, "label": f"pkg{i}", "size": 20 - i} for i in range(12)]
TOP = [{"name": "hub", "labels": ["Class"], "file": "a.py", "score": 0.5, "community": 0}]


class StubEngine:
    def __init__(self, computed=True, fail=False):
        self.computed = computed
        self.fail = fail
        self.writes = []

    def read_insights_summary(self, repo_id):
        if self.fail:
            raise RuntimeError("Neo4jError: SECRET connection refused")
        if not self.computed:
            return None
        return {"computed_at": "2026-10-01T00:00:00+00:00", "node_count": 40, "community_count": 12,
                "modularity": 0.41, "communities": json.dumps(COMMUNITIES)}

    def run_cypher(self, query, params=None):
        return [dict(TOP[0], name=f"hub-{params['limit']}")]

    def load_insight_graph(self, repo_id, types):
        if self.fail:
            raise RuntimeError("Neo4jError: SECRET")
        nodes = [{"id": n, "name": n, "labels": ["Function"], "file": f"{n}.py"} for n in "ab"]
        return nodes, [{"source": "a", "target": "b", "type": "CALLS"}]

    def write_insights(self, repo_id, rows, summary):
        self.writes.append((repo_id, rows, summary))
        self.computed = True


class StubRegistry:
    def get(self, repo_id):
        return object() if repo_id == "demo" else None


class Events:
    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)


def client(engine, events=None):
    app = FastAPI()
    app.include_router(build_router(engine, StubRegistry(), events or Events()))
    return TestClient(app)


def test_not_computed_yet():
    response = client(StubEngine(computed=False)).get("/api/repos/demo/insights")
    assert response.status_code == 200 and response.json() == {"computed": False}


def test_computed_payload_is_trimmed_for_the_card():
    body = client(StubEngine()).get("/api/repos/demo/insights").json()
    assert body["computed"] is True
    assert body["community_count"] == 12 and body["node_count"] == 40 and body["modularity"] == 0.41
    assert body["communities"] == COMMUNITIES[:8]
    assert body["key_nodes"][0]["name"] == "hub-6" and body["bridges"][0]["name"] == "hub-6"


def test_unknown_repo_is_404_for_both_methods():
    c = client(StubEngine())
    assert c.get("/api/repos/nope/insights").status_code == 404
    assert c.post("/api/repos/nope/insights").status_code == 404


def test_graph_failure_is_503_without_driver_text():
    for method in ("get", "post"):
        response = getattr(client(StubEngine(fail=True)), method)("/api/repos/demo/insights")
        assert response.status_code == 503
        assert response.json() == {"detail": "graph unavailable"}
        assert "SECRET" not in response.text


def test_recompute_writes_publishes_and_returns_the_payload():
    engine, events = StubEngine(computed=False), Events()
    response = client(engine, events).post("/api/repos/demo/insights")
    assert response.status_code == 200 and response.json()["computed"] is True
    assert len(engine.writes) == 1
    assert events.published == [{"type": "insights_refreshed", "repo_id": "demo"}]


def test_recompute_while_a_run_holds_the_lock_is_409():
    engine = StubEngine()
    with insights._repo_lock("demo"):
        response = client(engine).post("/api/repos/demo/insights")
    assert response.status_code == 409
    assert engine.writes == []


def test_recompute_rejects_cross_site_requests():
    engine = StubEngine()
    response = client(engine).post("/api/repos/demo/insights", headers={"sec-fetch-site": "cross-site"})
    assert response.status_code == 403
    response = client(engine).post("/api/repos/demo/insights", headers={"origin": "https://evil.example"})
    assert response.status_code == 403
    assert engine.writes == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/dashboard/test_insights_routes.py -q`
Expected: failures with 404/405 for every request (routes don't exist).

- [ ] **Step 3: Implement the routes**

In `devgraph/dashboard/routes.py`:

1. Import: `from devgraph.analytics.insights import read_insights, refresh_insights, top_nodes`.
2. Constants next to `_SSE_KEEPALIVE_S`:
   ```python
   # How much of the graph-insights summary the Community card shows.
   _INSIGHT_COMMUNITY_LIMIT = 8
   _INSIGHT_LIST_LIMIT = 6
   ```
3. Inside `build_router`, after the `/repos/{repo_id}/graph` route:

```python
    def _insights_payload(repo_id: str) -> dict[str, Any]:
        summary = read_insights(engine, repo_id)
        if summary is None:
            return {"computed": False}
        return {
            "computed": True,
            "computed_at": summary.get("computed_at"),
            "node_count": summary.get("node_count"),
            "community_count": summary.get("community_count"),
            "modularity": summary.get("modularity"),
            "communities": summary["communities"][:_INSIGHT_COMMUNITY_LIMIT],
            "key_nodes": top_nodes(engine, repo_id, "pagerank", _INSIGHT_LIST_LIMIT),
            "bridges": top_nodes(engine, repo_id, "betweenness", _INSIGHT_LIST_LIMIT),
        }

    @router.get("/repos/{repo_id}/insights")
    def repo_insights(repo_id: str) -> dict[str, Any]:
        _require_repo(repo_id)
        try:
            return _insights_payload(repo_id)
        except Exception as exc:  # neo4j driver raises its own exception hierarchy
            logger.debug("graph insights unavailable for %s: %s", repo_id, exc)
            raise HTTPException(status_code=503, detail="graph unavailable") from exc

    @router.post("/repos/{repo_id}/insights")
    async def recompute_repo_insights(request: Request, repo_id: str) -> dict[str, Any]:
        """Recompute now. The only other write the dashboard makes besides the
        layout and registration, and it only replaces derived properties."""
        _reject_cross_site(request)
        _require_repo(repo_id)
        try:
            summary = await run_in_threadpool(refresh_insights, engine, repo_id, blocking=False)
            if summary is None:
                raise HTTPException(status_code=409, detail="insights are already being computed for this repository")
            events.publish({"type": "insights_refreshed", "repo_id": repo_id})
            return await run_in_threadpool(_insights_payload, repo_id)
        except HTTPException:
            raise
        except Exception as exc:  # neo4j driver raises its own exception hierarchy
            logger.warning("graph insights recompute failed for %s", repo_id, exc_info=True)
            raise HTTPException(status_code=503, detail="graph unavailable") from exc
```

Also update the module docstring's "Read-only apart from two writes" sentence to name the third: recomputing graph insights (`POST .../insights`), which replaces only derived properties.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/dashboard -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add devgraph/dashboard/routes.py tests/dashboard/test_insights_routes.py
git commit -m "Serve and recompute graph insights from the dashboard"
```

---

### Task 6: Dashboard UI and docs

**Files:**
- Modify: `devgraph/dashboard/static/index.html` — locate each region by content:
  - canvas toggles (`ctlShadowsEnabled` row, ~line 470)
  - god-node subsection (`data-od-id="god-nodes-section"`, ~line 512)
  - Community card (`data-od-id="community-card"`, ~line 600)
  - `catColor` (~line 848) and the `state` literal (~line 864)
  - `ctlShadowsEnabled` change handler (~line 874)
  - node style `"background-color"` (~line 1016)
  - `mapGraphResultToElements` (~line 2246)
  - `refreshGraph` (~line 2472), `bootConnect` (~line 2600), `connectLiveEvents` (~line 2642)
  - `attemptCommunityDetection` (~line 3413), `loadTopologyCounts`' god-node block (~line 3521)
- Modify: `README.md`, `PROJECT_STATUS.md`
- Test: `tests/dashboard/insights_ui.js`, `tests/dashboard/test_insights_ui.py` (create)

**Interfaces:**
- Consumes: `GET`/`POST /api/repos/{repo_id}/insights` (Task 5); `insights_refreshed` SSE event (Task 3); node property `insight_community` in `/api/cypher` graph results (Task 2).
- Produces (top-level JS): `COMMUNITY_PALETTE`, `communityColor(c)`, `insightRowsHtml(rows)`, `formatScore(v)`, `insightsRequest`, `clearInsights(note, pillText)`, `renderInsights(body)`, `loadInsights()`, `recomputeInsights()`; `state.colorByCommunity`. `attemptCommunityDetection` is removed.

- [ ] **Step 1: Write the failing headless test**

Create `tests/dashboard/test_insights_ui.py`:

```python
"""Runs the graph-insights UI checks (insights_ui.js) as part of the suite.

Skipped when node isn't on PATH -- the Python suite has to run everywhere."""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("insights_ui.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_insights_ui():
    result = subprocess.run([shutil.which("node"), str(_SCRIPT)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
```

Create `tests/dashboard/insights_ui.js`:

```js
/* Headless test of the graph-insights UI, lifted verbatim out of index.html:
   the Community card renderers, the leaderboard switch to PageRank, the
   community palette and the canvas mapping that carries a node's community.
   Only fetch and document.getElementById are reached, so stubs suffice. */
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
  grab(/^const COMMUNITY_PALETTE\b/m, "];"),
  grab(/^function communityColor\(/m, "\n}"),
  grab(/^function escapeHtmlVal\(/m, "}"),
  grab(/^function insightRowsHtml\(/m, "\n}"),
  grab(/^function formatScore\(/m, "\n}"),
  grab(/^let insightsRequest\b/m, ";"),
  grab(/^function clearInsights\(/m, "\n}"),
  grab(/^function renderInsights\(/m, "\n}"),
  grab(/^async function loadInsights\(/m, "\n}"),
  grab(/^async function recomputeInsights\(/m, "\n}"),
  grab(/^function mapGraphResultToElements\(/m, "\n}"),
].join("\n");

const mkEl = initialClass => {
  const classes = new Set(initialClass ? initialClass.split(" ") : []);
  return {
    textContent: "", innerHTML: "", title: "", disabled: false, value: "",
    classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c) },
    get className() { return [...classes].join(" "); },
    set className(v) { classes.clear(); String(v).split(" ").filter(Boolean).forEach(c => classes.add(c)); },
  };
};
let els = {};
let calls = [];
let routes = {};
const fetchStub = async (url, opts) => {
  calls.push({ url, method: opts?.method || "GET" });
  const handler = routes[(opts?.method || "GET") + " " + url];
  if (!handler) return { ok: false, status: 404, json: async () => ({}) };
  return handler();
};
const globals = {
  document: { getElementById: id => els[id] || null },
  fetch: fetchStub,
  NODE_TYPES: [{ id: "Function", cat: "code" }],
  stableNodeId: n => "s:" + n.id,
  console,
};
const api = new Function(...Object.keys(globals), src +
  "\nreturn { communityColor, COMMUNITY_PALETTE, loadInsights, recomputeInsights, mapGraphResultToElements, renderInsights };")(
  ...Object.values(globals));

let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};
const reset = repo => {
  els = {
    repoSelect: mkEl(""), communityPill: mkEl("sample-pill"), communityNote: mkEl("placeholder-note"),
    communityVal: mkEl("metric-val dim"), modularityVal: mkEl("metric-val dim"), insightsAtVal: mkEl("metric-val dim"),
    communityList: mkEl(""), bridgeList: mkEl(""), btnRecomputeInsights: mkEl("btn"),
    godNodes: mkEl(""), godNodesBasis: mkEl("lb-type"),
  };
  els.repoSelect.value = repo;
  els.godNodes.innerHTML = "DEGREE-LIST";
  els.godNodesBasis.textContent = "degree";
  calls = []; routes = {};
};
const json = (body, status = 200) => async () => ({ ok: status < 400, status, json: async () => body });
const COMPUTED = {
  computed: true, computed_at: "2026-10-01T12:00:00+00:00", node_count: 40, community_count: 3, modularity: 0.3571,
  communities: [{ community: 0, label: "<b>auth</b>", size: 12 }, { community: 1, label: "billing", size: 9 }],
  key_nodes: [{ name: "Hub", labels: ["Class"], file: "a.py", score: 0.123456, community: 0 }],
  bridges: [{ name: "Bridge", labels: ["Function"], file: "b.py", score: 0.6, community: 1 }],
};

(async () => {
  // 1. palette
  check("community 0 takes the first palette colour", api.communityColor(0) === api.COMMUNITY_PALETTE[0], api.communityColor(0));
  check("the palette cycles", api.communityColor(api.COMMUNITY_PALETTE.length + 1) === api.COMMUNITY_PALETTE[1], "");
  for (const bad of [null, undefined, -1, 1.5, "2"]) {
    check(`no community (${JSON.stringify(bad)}) is grey`, api.communityColor(bad) === "#5a5f68", api.communityColor(bad));
  }

  // 2. canvas mapping carries the community
  const els2 = api.mapGraphResultToElements([{ graph: { nodes: [
    { id: 1, labels: ["Function"], properties: { name: "f", insight_community: 3 } },
    { id: 2, labels: ["Function"], properties: { name: "g", insight_community: "3" } },
    { id: 3, labels: ["Function"], properties: { name: "h" } },
  ], relationships: [] } }]);
  const community = name => els2.find(e => e.data.label === name).data.community;
  check("an integer community is carried onto the node", community("f") === 3, JSON.stringify(els2));
  check("a non-integer community is dropped", community("g") === null, String(community("g")));
  check("a node without one has null", community("h") === null, String(community("h")));

  // 3. all-repos view asks nothing
  reset("__all__");
  await api.loadInsights();
  check("all-repos view makes no request", calls.length === 0, JSON.stringify(calls));
  check("all-repos view disables Recompute", els.btnRecomputeInsights.disabled === true, "");
  check("all-repos view says to pick a repository", /select a single repository/i.test(els.communityNote.textContent), els.communityNote.textContent);

  // 4. not computed
  reset("demo");
  routes["GET /api/repos/demo/insights"] = json({ computed: false });
  await api.loadInsights();
  check("not computed: pill says so", els.communityPill.textContent === "Not computed", els.communityPill.textContent);
  check("not computed: values dashed", ["communityVal", "modularityVal", "insightsAtVal"].every(id => els[id].textContent === "—" && els[id].classList.contains("dim")), "");
  check("not computed: leaderboard left on degree", els.godNodes.innerHTML === "DEGREE-LIST" && els.godNodesBasis.textContent === "degree", els.godNodesBasis.textContent);

  // 5. computed
  reset("my repo");
  routes["GET /api/repos/my%20repo/insights"] = json(COMPUTED);
  await api.loadInsights();
  check("the repo id is URL-encoded", calls[0].url === "/api/repos/my%20repo/insights", calls[0].url);
  check("community count shown", els.communityVal.textContent === "3" && !els.communityVal.classList.contains("dim"), els.communityVal.textContent);
  check("modularity to two places", els.modularityVal.textContent === "0.36", els.modularityVal.textContent);
  check("last computed is filled", els.insightsAtVal.textContent !== "—" && els.insightsAtVal.textContent !== "", els.insightsAtVal.textContent);
  check("community labels are escaped", els.communityList.innerHTML.includes("&lt;b&gt;auth&lt;/b&gt;") && !els.communityList.innerHTML.includes("<b>auth"), els.communityList.innerHTML);
  check("bridges are listed with their score", els.bridgeList.innerHTML.includes("Bridge") && els.bridgeList.innerHTML.includes("0.600"), els.bridgeList.innerHTML);
  check("leaderboard switches to PageRank", els.godNodes.innerHTML.includes("Hub") && els.godNodesBasis.textContent === "PageRank", els.godNodesBasis.textContent);
  check("pill is live", els.communityPill.textContent === "Live" && els.communityPill.className === "wired-pill", els.communityPill.className);

  // 6. computed with no structure
  reset("demo");
  routes["GET /api/repos/demo/insights"] = json({ ...COMPUTED, community_count: 0, communities: [], key_nodes: [], bridges: [] });
  await api.loadInsights();
  check("empty result says there is no structure", /no dependency structure/i.test(els.communityNote.textContent), els.communityNote.textContent);
  check("empty result keeps the degree leaderboard", els.godNodesBasis.textContent === "degree", els.godNodesBasis.textContent);

  // 7. failure
  reset("demo");
  routes["GET /api/repos/demo/insights"] = json({ detail: "graph unavailable" }, 503);
  await api.loadInsights();
  check("a failed read is labelled unavailable", els.communityPill.textContent === "Unavailable" && /unavailable/i.test(els.communityNote.textContent), els.communityNote.textContent);

  // 8. a slower response for the previous repo never wins
  reset("first");
  let releaseFirst;
  routes["GET /api/repos/first/insights"] = () => new Promise(r => { releaseFirst = () => r({ ok: true, status: 200, json: async () => ({ ...COMPUTED, community_count: 99 }) }); });
  routes["GET /api/repos/second/insights"] = json({ ...COMPUTED, community_count: 2 });
  const slow = api.loadInsights();
  els.repoSelect.value = "second";
  await api.loadInsights();
  releaseFirst();
  await slow;
  check("the newer repo's result stands", els.communityVal.textContent === "2", els.communityVal.textContent);

  // 9. recompute
  reset("demo");
  routes["POST /api/repos/demo/insights"] = json({ detail: "busy" }, 409);
  await api.recomputeInsights();
  check("409 says a run is already going", /already/i.test(els.communityNote.textContent), els.communityNote.textContent);
  check("the button is re-enabled after 409", els.btnRecomputeInsights.disabled === false, "");
  reset("demo");
  routes["POST /api/repos/demo/insights"] = json(COMPUTED);
  await api.recomputeInsights();
  check("recompute uses POST", calls.some(c => c.method === "POST" && c.url === "/api/repos/demo/insights"), JSON.stringify(calls));
  check("recompute renders the fresh result", els.communityVal.textContent === "3", els.communityVal.textContent);
  reset("demo");
  routes["POST /api/repos/demo/insights"] = json({ detail: "graph unavailable" }, 503);
  await api.recomputeInsights();
  check("a failed recompute says so", /failed/i.test(els.communityNote.textContent) && els.btnRecomputeInsights.disabled === false, els.communityNote.textContent);

  // 10. the GDS placeholder is gone
  check("no GDS probe remains", !/gds\.list/.test(html) && !/attemptCommunityDetection/.test(html), "index.html still probes for GDS");

  console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/dashboard/test_insights_ui.py -q`
Expected: FAIL with `could not find /^const COMMUNITY_PALETTE\b/m`.

- [ ] **Step 3: Markup**

1. Canvas toggle — insert before the `Node/edge shadows` toggle row:

```html
          <div class="toggle-row">
            <span class="lbl">Color by community<span class="qmark" data-tip="Colors nodes by their graph-insights community instead of entity type. Grey nodes have no community: no dependency or containment edges, or insights not computed yet.">?</span></span>
            <label class="switch"><input type="checkbox" id="ctlCommunityColors" /><span class="track"></span><span class="thumb"></span></label>
          </div>
```

2. God-node subhead — replace the `<div class="fc-subhead">Top god nodes<span class="qmark" ...>?</span></div>` line with:

```html
        <div class="fc-subhead">Top god nodes <span class="lb-type" id="godNodesBasis">degree</span><span class="qmark" data-tip="PageRank over dependency edges (calls, imports, extends, implements, uses, depends-on) once graph insights are computed; until then, raw degree — size((n)--()) — which containment and history edges inflate.">?</span></div>
```

3. Community card — replace the whole `<div class="card" data-od-id="community-card"> ... </div>` block with:

```html
      <div class="card" data-od-id="community-card">
        <div class="card-head"><h3>Communities</h3><span class="sample-pill" id="communityPill">Checking…</span></div>
        <div class="metric-row"><span class="metric-label">Communities<span class="qmark" data-tip="Louvain clusters over this repository's dependency and containment edges, computed by DevGraph itself (no Neo4j plugin) and refreshed automatically after indexing.">?</span></span><span class="metric-val dim" id="communityVal">—</span></div>
        <div class="metric-row"><span class="metric-label">Modularity<span class="qmark" data-tip="How cleanly the graph splits into these communities: near 0 means no real structure, above about 0.3 a clear split.">?</span></span><span class="metric-val dim" id="modularityVal">—</span></div>
        <div class="metric-row"><span class="metric-label">Last computed</span><span class="metric-val dim" id="insightsAtVal">—</span></div>
        <div id="communityList"></div>
        <div class="metric-label" style="margin-top:10px">Bridges<span class="qmark" data-tip="Highest betweenness over dependency edges: entities that sit between subsystems, where a change ripples furthest.">?</span></div>
        <div id="bridgeList"></div>
        <div class="placeholder-note" id="communityNote">Reading graph insights…</div>
        <button class="btn btn-sm" id="btnRecomputeInsights" style="margin-top:8px">Recompute</button>
      </div>
```

- [ ] **Step 4: JavaScript**

1. Right after `function catColor(cat) { ... }` add:

```js
/* Community colours for the "Color by community" toggle. Defined here, ahead
   of the Cytoscape style that calls communityColor, and cycled when a
   repository has more communities than colours. */
const COMMUNITY_PALETTE = ["#5e6ad2", "#26b5ce", "#4cb782", "#f2c94c", "#f2994a", "#eb5757", "#bb87fc", "#ff7eb6", "#6fcf97", "#56ccf2", "#c9a227", "#9b8afb"];
function communityColor(c) {
  return Number.isInteger(c) && c >= 0 ? COMMUNITY_PALETTE[c % COMMUNITY_PALETTE.length] : "#5a5f68";
}
```

2. In the `state` literal, add `colorByCommunity: false` (after `settleOnChange: true`).

3. After the `ctlShadowsEnabled` change handler add:

```js
document.getElementById("ctlCommunityColors").addEventListener("change", e => {
  state.colorByCommunity = e.target.checked;
  cy.style().update(); // background-color below is function-valued on state.colorByCommunity
});
```

4. In the Cytoscape node style, change the `"background-color"` line to:

```js
        "background-color": ele => state.colorByCommunity ? communityColor(ele.data("community")) : catColor(ele.data("cat")),
```

5. In `mapGraphResultToElements`, change the `nodeMap.set(...)` line to carry the community:

```js
      const community = Number.isInteger(n.properties?.insight_community) ? n.properties.insight_community : null;
      nodeMap.set(stableId, { data: { id: stableId, key: n.key ?? null, label: name, cat, community } });
```

6. In `refreshGraph`, right after `if (neo4jConnected) await loadTopologyCounts();` add:

```js
  if (neo4jConnected) loadInsights(); // after the degree leaderboard, which it replaces with PageRank when computed
```

7. In `loadTopologyCounts`, inside the branch that renders the degree leaderboard (`godEl.innerHTML = godJson.results[0].data.map(...)`), add right after that assignment:

```js
      document.getElementById("godNodesBasis").textContent = "degree";
```

8. In `bootConnect`, delete the line `attemptCommunityDetection();`. Delete the whole `async function attemptCommunityDetection() { ... }`, and update the comment block just above it (it names "Community") and the `loadTopologyCounts` header comment's "only Louvain/PageRank need the plugin" remark so neither mentions GDS anymore.

9. In `connectLiveEvents`'s `onmessage`, add a branch before the `registry_changed` branch:

```js
    } else if (event.type === "insights_refreshed" && neo4jConnected) {
      loadInsights(); // node colours pick the new communities up on the next graph load
```

10. Where `attemptCommunityDetection` was, add:

```js
/* ── Graph insights: Communities card, Bridges, and the PageRank leaderboard.
   Backed by /api/repos/{id}/insights, which the agent keeps current after
   indexing (devgraph/analytics/insights.py). Per repository only. ── */
function insightRowsHtml(rows) {
  return rows.map((r, i) => `<div class="leaderboard-row"><span class="lb-rank">${i + 1}</span><span class="lb-name">${escapeHtmlVal(r.name ?? "")}</span><span class="lb-type">${escapeHtmlVal(r.tag ?? "")}</span><span class="lb-val">${escapeHtmlVal(r.value ?? "")}</span></div>`).join("");
}
function formatScore(v) {
  return Number.isFinite(v) ? v.toFixed(3) : "—";
}
let insightsRequest = 0;
function clearInsights(note, pillText) {
  for (const id of ["communityVal", "modularityVal", "insightsAtVal"]) {
    const el = document.getElementById(id);
    el.textContent = "—";
    el.classList.add("dim");
  }
  document.getElementById("communityList").innerHTML = "";
  document.getElementById("bridgeList").innerHTML = "";
  const pill = document.getElementById("communityPill");
  pill.textContent = pillText;
  pill.className = "sample-pill";
  document.getElementById("communityNote").textContent = note;
}
function renderInsights(body) {
  if (!body?.computed) {
    clearInsights("Not computed yet — the DevGraph agent computes insights after indexing, or press Recompute.", "Not computed");
    return;
  }
  const set = (id, text) => {
    const el = document.getElementById(id);
    el.textContent = text;
    el.classList.remove("dim");
  };
  set("communityVal", Number.isFinite(body.community_count) ? String(body.community_count) : "—");
  set("modularityVal", Number.isFinite(body.modularity) ? body.modularity.toFixed(2) : "—");
  set("insightsAtVal", body.computed_at ? new Date(body.computed_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—");
  const communities = Array.isArray(body.communities) ? body.communities : [];
  document.getElementById("communityList").innerHTML =
    insightRowsHtml(communities.map(c => ({ name: c.label, tag: "#" + c.community, value: c.size })));
  const bridges = Array.isArray(body.bridges) ? body.bridges : [];
  document.getElementById("bridgeList").innerHTML = bridges.length
    ? insightRowsHtml(bridges.map(b => ({ name: b.name, tag: (b.labels || [])[0] || "", value: formatScore(b.score) })))
    : `<div class="count-note">No dependency edges to rank.</div>`;
  const keyNodes = Array.isArray(body.key_nodes) ? body.key_nodes : [];
  if (keyNodes.length) {
    document.getElementById("godNodes").innerHTML =
      insightRowsHtml(keyNodes.map(k => ({ name: k.name, tag: (k.labels || [])[0] || "", value: formatScore(k.score) })));
    document.getElementById("godNodesBasis").textContent = "PageRank";
  }
  const pill = document.getElementById("communityPill");
  pill.textContent = "Live";
  pill.className = "wired-pill";
  document.getElementById("communityNote").textContent = communities.length
    ? `Louvain over dependency and containment edges across ${body.node_count} nodes.`
    : "No dependency structure found in this repository yet.";
}
async function loadInsights() {
  const repo = document.getElementById("repoSelect").value;
  const btn = document.getElementById("btnRecomputeInsights");
  const ticket = ++insightsRequest;
  if (!repo || repo === "__all__") {
    btn.disabled = true;
    clearInsights("Select a single repository to see its communities, key nodes and bridges.", "All repos");
    return;
  }
  btn.disabled = false;
  let body;
  try {
    const res = await fetch(`/api/repos/${encodeURIComponent(repo)}/insights`);
    if (!res.ok) throw new Error("HTTP " + res.status);
    body = await res.json();
  } catch (e) {
    if (ticket === insightsRequest) clearInsights("Graph insights are unavailable right now — the graph could not be read.", "Unavailable");
    return;
  }
  if (ticket !== insightsRequest) return; // a newer repo switch superseded this one
  renderInsights(body);
}
async function recomputeInsights() {
  const repo = document.getElementById("repoSelect").value;
  if (!repo || repo === "__all__") return;
  const btn = document.getElementById("btnRecomputeInsights");
  const note = document.getElementById("communityNote");
  btn.disabled = true;
  note.textContent = "Recomputing…";
  try {
    const res = await fetch(`/api/repos/${encodeURIComponent(repo)}/insights`, { method: "POST" });
    if (res.status === 409) {
      note.textContent = "Already being computed — the result appears when it finishes.";
      return;
    }
    if (!res.ok) throw new Error("HTTP " + res.status);
    const body = await res.json();
    if (document.getElementById("repoSelect").value === repo) renderInsights(body);
  } catch (e) {
    note.textContent = "Recompute failed — the graph could not be read.";
  } finally {
    btn.disabled = false;
  }
}
document.getElementById("btnRecomputeInsights").addEventListener("click", recomputeInsights);
```

Note the test harness grabs each function up to the first line that starts with `}`; keep the indentation exactly as above so no inner line starts at column 0.

- [ ] **Step 5: Run the UI tests and the dashboard suite**

Run: `uv run pytest tests/dashboard -q`
Expected: all pass. The other headless harnesses (`database_stats_ui.js`, `mcp_telemetry_ui.js`) stub `attemptCommunityDetection` in their `bootGlobals`; an unused stub is harmless, so they should still pass — if one fails, read its output and fix only what this change broke, reporting it.

- [ ] **Step 6: Docs**

README.md:
- In the Dashboard section, add a paragraph after the existing ones: "The Communities card shows each repository's subsystems (Louvain communities over dependency and containment edges), with modularity and the bridges between them; the god-node list ranks by PageRank over dependency edges once these are computed. A canvas toggle colors nodes by community. DevGraph computes all of this itself — no Neo4j plugin — and the agent refreshes it after indexing; `devgraph insights <repo_id>` recomputes on demand."
- Where the README lists CLI capabilities (read it first; follow its existing format), mention `devgraph insights`. Do not list MCP tools by name — the README deliberately points to the `devgraph://tool-catalog` resource.

PROJECT_STATUS.md: add a bullet in the shipped section: "Graph insights (issue #4) shipped: `devgraph/analytics/insights.py` computes Louvain communities, PageRank and sampled betweenness per repository with networkx (no GDS), stores them as `insight_*` node properties plus an `insights_*` summary on the Repository node, and an `InsightsScheduler` in both agents recomputes whenever `last_indexed` is newer. Surfaced through the `find_communities` and `key_nodes` MCP tools (24 tools in all), `devgraph insights`, `GET/POST /api/repos/{id}/insights`, the dashboard's Communities card, a PageRank god-node leaderboard and a color-by-community canvas toggle." Also add `analytics/` to the code map (`devgraph/analytics/` — derived graph analytics: `insights.py`).

- [ ] **Step 7: Commit**

```bash
git add devgraph/dashboard/static/index.html tests/dashboard/insights_ui.js tests/dashboard/test_insights_ui.py README.md PROJECT_STATUS.md
git commit -m "Show communities, bridges and PageRank god nodes on the dashboard"
```

---

### Task 7: Full verification and PR

- [ ] **Step 1: Full suite**

Run: `uv run pytest -q` — expected all pass (Neo4j up; display-bound tests skip without X).

- [ ] **Step 2: Manual check on a real repository**

Index this repository into the local Neo4j with a throwaway registry so the user's real registry is untouched (`<scratch>` = the session scratchpad directory):

```bash
export DEVGRAPH_REGISTRY_DB_PATH=<scratch>/insights-registry.sqlite3
uv run devgraph add "$(pwd)"
uv run devgraph list   # note the repo_id
uv run devgraph insights <repo_id>
```

Expected: a line like `Insights for <repo_id>: N communities (modularity 0.xx) over M nodes` with N ≥ 5 and modularity > 0.3. Then check results look sane:

```bash
uv run python -c "
from devgraph.config.settings import get_settings
from devgraph.graph.engine import GraphEngine
from devgraph.analytics.insights import read_insights, top_nodes
s = get_settings(); e = GraphEngine(s.neo4j_uri, s.neo4j_user, s.neo4j_password)
print([ (c['label'], c['size']) for c in read_insights(e, '<repo_id>')['communities'][:8] ])
print([ r['name'] for r in top_nodes(e, '<repo_id>', 'pagerank', 8) ])
print([ r['name'] for r in top_nodes(e, '<repo_id>', 'betweenness', 8) ])
"
```

Record the output in the report. Then clean up: `uv run devgraph remove <repo_id>` (deletes the nodes) and delete the throwaway registry file.

- [ ] **Step 3: Push and open the PR — only after the user confirms**

```bash
git push -u origin feat/graph-insights
gh pr create -R HaydenSchmidtDOC/DevGraph --base master --head <fork-owner>:feat/graph-insights \
  --title "Graph insights: communities, PageRank and bridges" --body-file <scratch>/pr-body.md
```

PR body: what changed (computation, storage, scheduler, CLI, MCP tools, dashboard), that it needs no GDS and adds only networkx, validation (suite count, the manual check's community/PageRank/betweenness output), and `Addresses #4`. No mention of Claude/the assistant.
