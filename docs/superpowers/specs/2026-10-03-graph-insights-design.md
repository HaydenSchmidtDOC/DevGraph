# Graph insights — design

Upstream issue: HaydenSchmidtDOC/DevGraph#4 ("God nodes, communities and other
goodies"). The dashboard's Community card is a placeholder that only checks for
the Neo4j GDS plugin, and the god-node leaderboard ranks by raw degree, which
containment and history edges inflate.

## Goals

- Communities: cluster each repository's code into subsystems (Louvain).
- Better god nodes: rank importance with PageRank over dependency edges only.
- Bridge nodes: rank betweenness centrality — entities that connect subsystems.
- Make all three readable by coding agents (MCP tools) and on the dashboard
  (Community card, leaderboard, "color by community" canvas toggle).
- Work on stock `neo4j:5.26-community` — no GDS, no APOC, no image change.

## Non-goals

- GDS integration, cross-repository communities, persisted history of results,
  user-tunable algorithm parameters.

## Computation

New package `devgraph/analytics/` with `insights.py`. Computed in Python; the
only new dependency is `networkx` (pure Python). PageRank is a small
hand-written power iteration because networkx's own needs scipy.

Per repository:

- **Dependency edges** (centrality): `CALLS, DEPENDS_ON, EXTENDS, IMPLEMENTS,
  IMPORTS, USES`, directed as stored (A CALLS B gives B importance).
- **Community edges**: dependency edges plus `CONTAINS`, undirected, so a
  file's members stay together and cross-file dependencies join files into
  subsystems. History/intent edges (`MODIFIES`, `MENTIONS`, `SATISFIES`, …)
  are ignored. `Repository` nodes are excluded.
- **Communities**: `networkx.community.louvain_communities(seed=0)` over the
  community graph; modularity via `networkx.community.modularity`. Communities
  are numbered 0.. by size descending (ties: smallest member name). Each gets a
  label: the most common directory of its members' `file`s (ties:
  lexicographically smallest; root files → `(root)`), or, if no member has a
  file, its highest-PageRank member's name.
- **PageRank**: damping 0.85 over the dependency graph, dangling mass spread
  uniformly; nodes with no dependency edge get no score.
- **Betweenness**: `networkx.betweenness_centrality` over the undirected
  dependency graph, normalized, sampled with `k=256, seed=0` when the graph has
  more than 256 nodes.
- Inputs are sorted before graph construction so results are deterministic.

## Storage

Results live in Neo4j, the one store both the agent/dashboard process and the
separate MCP server process already read:

- Node properties `insight_community` (int), `insight_pagerank` (float),
  `insight_betweenness` (float). They survive re-indexing (upserts use
  `SET n += props`). Each run first clears these properties across the repo,
  so values on nodes that lost their edges don't linger.
- Repository node properties `insights_computed_at` (UTC ISO-8601),
  `insights_node_count`, `insights_community_count`, `insights_modularity`,
  `insights_communities` (JSON string: the 50 largest communities as
  `{community, label, size}`).
- Graph access lives in `GraphEngine` (`load_insight_graph`, `write_insights`,
  `read_insights_summary`), matching the existing convention.

`refresh_insights(engine, repo_id, blocking=True)` loads, computes and writes
under a per-repository lock; with `blocking=False` it returns `None` instead of
waiting when a run is already in progress.

## Triggers

- **Agent scheduler**: `InsightsScheduler` runs on a daemon thread in both the
  tray and headless agents. Every 30 s it checks each active repository and
  recomputes when `registry.last_indexed` is newer than
  `insights_computed_at` (or there is none). This one rule covers watcher
  batches, CLI rescans and dashboard registrations, and backfills on startup.
  After a refresh it publishes an `insights_refreshed` SSE event. A failing
  check or run is logged and never kills the thread.
- **CLI**: `devgraph insights <repo_id>` computes immediately (no agent needed).
- **Dashboard**: a Recompute button (`POST /api/repos/{repo_id}/insights`).

## MCP tools (catalog grows from 22 to 24)

- `find_communities(repo_id, max_results=10, members_per_community=5)` →
  envelope of `{community, label, size, top_members: [{name, labels, file,
  pagerank}]}`, largest first.
- `key_nodes(repo_id, metric="pagerank", max_results=10)` — metric is
  `pagerank` or `betweenness` (anything else rejected without echoing input) →
  envelope of `{name, labels, file, score, community}`.
- Both raise a clear error naming `devgraph insights <repo_id>` when insights
  have never been computed for the repository. A computed repository with no
  dependency structure returns an empty envelope, not an error.
- `god_nodes` is unchanged (backward compatible).

## Dashboard

- `GET /api/repos/{repo_id}/insights` → `{"computed": false}` or
  `{"computed": true, computed_at, node_count, community_count, modularity,
  communities (top 8), key_nodes (top 6 PageRank), bridges (top 6
  betweenness)}`. 404 for an unknown repo; 503 with a fixed message when the
  graph is unreachable.
- `POST /api/repos/{repo_id}/insights` → recomputes and returns the same shape;
  cross-site guarded like registration; 409 when a run is already in progress.
- **Community card** (replaces the GDS placeholder): community count,
  modularity, last computed, the top communities, a Bridges list, and a
  Recompute button. "Select a repository" when the view is all repositories;
  "Not computed yet" before the first run.
- **God-node leaderboard**: shows PageRank when insights exist (heading says
  so), otherwise the existing degree ranking (heading says degree).
- **Canvas toggle** "Color by community": nodes take a 12-color cycling
  palette by `insight_community`; nodes without one are grey. Off by default.
- Refreshed on repo switch/graph refresh and on the `insights_refreshed` event.

## Error handling

- Empty or dependency-free repo: a valid computed result with 0 communities.
- Node deleted between load and write: its row is skipped (matched by
  `elementId` within the repo).
- Concurrent Recompute + scheduler: per-repo lock; the dashboard gets 409.
- Neo4j down: scheduler logs and retries next tick; routes return 503.

## Testing

- Pure computation on synthetic graphs (two triangles joined by a bridge →
  two communities, bridge endpoints top betweenness; star → hub top PageRank;
  determinism; empty input; label derivation; sampling above 256 nodes).
- Live-Neo4j round trip of load/write/clear/summary (skips without Neo4j).
- Scheduler decisions with stub engine/registry; lock behaviour.
- MCP tools with stub engines; tool-count and catalog tests updated to 24.
- Route tests with stubs; headless JS tests for the card, leaderboard,
  palette and canvas mapping.
- Manual: index this repository into local Neo4j and inspect results.

## Docs

README (dashboard + CLI), PROJECT_STATUS. MCP tools are documented through the
`devgraph://tool-catalog` resource, as for every other tool.
