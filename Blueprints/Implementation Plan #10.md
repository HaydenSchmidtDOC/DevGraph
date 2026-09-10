# DevGraph — Implementation Plan #10: Closing the gap with Graphify, phased

**Status: planning — no code written yet.** This plan came out of a comparison
audit against Graphify (an external, much larger open-source code-graph tool)
run to answer one question: is DevGraph worth continuing to build on top of,
or should we adopt/fork something else instead? Conclusion of that audit:
keep building DevGraph. Its two structural advantages over Graphify —
(1) a real live Neo4j backend instead of a static JSON file re-parsed on every
query, and (2) being self-authored, already-known code rather than a 300K+
line external dependency needing a full security sign-off — matter more for
this environment (anti-MCP, security-cautious office) than Graphify's larger
feature surface. This plan is the list of what to build to close that feature
gap without giving up either advantage.

Every item below is something Graphify already does that DevGraph doesn't.
Where the note says "port the idea," that means: reimplement the concept
against our own architecture (Neo4j-backed, MCP *and* CLI), not adapt or copy
their code. Graphify is Apache-2.0 licensed, so copying would be legally fine,
but their implementation is JSON-graph-shaped (NetworkX in memory) and ours is
Neo4j-shaped (Cypher) — a direct port wouldn't compile against our engine
regardless of license.

## How to read the three phases

- **Phase 1 — easy levers.** Small, self-contained, no open design questions.
  Buildable in one pass without checking back in. Do these first regardless
  of what happens with Phase 2/3.
- **Phase 2 — the big stuff.** Each item needs a short scoping conversation
  before starting (confirm priority, confirm approach) because the change
  touches core extraction/graph-write paths and is expensive to redo if the
  first attempt guesses wrong. Heavier diffs, real test-suite impact.
- **Phase 3 — gated.** Each item requires a decision that isn't really an
  engineering decision — it's a policy/architecture call (bring in an LLM?
  support a second storage backend? open a network listener?) that changes
  what DevGraph *is*, not just what it does. Don't start any of these without
  an explicit go-ahead, even if the engineering itself would be easy.

---

## Phase 1 — Easy levers to pull

One-shot, no back-and-forth needed. Ordered roughly by value first.

1. **CLI query commands wrapping the existing MCP tools.**
   `devgraph query find-callers <name>`, `devgraph query impact <name>`,
   `devgraph query explain <name>`, etc. — one subcommand per tool already
   implemented in `devgraph/mcp/tools.py`. This is the actual fix for
   "anti-MCP company can't use this tool at all" — everything else in this
   plan is quality, this one is the adoption blocker. No new query logic,
   just a thin CLI wrapper around functions that already exist.

2. **PreToolUse-style hook nudging toward the new CLI query commands**,
   mirroring Graphify's pattern (soft nudge by default, an opt-in strict
   mode that blocks the first raw file read once per session and redirects
   to `devgraph query`). Depends on #1 existing first. Zero MCP involved —
   this is a pure CLI + hook feature.

3. **Graph integrity validation**, extending the existing `doctor` command:
   orphaned nodes, dangling edges, entities claimed by extraction but never
   resolved. Graphify has a dedicated `validate` command; ours can just be a
   new check inside `doctor` rather than a whole new command.

4. **God-node analysis** (most-connected entities in a repo). Trivial once
   the graph is already in Neo4j — a degree-count Cypher query exposed as a
   new MCP tool + CLI query subcommand. Half a day, not a project.

5. **GraphML export.** Standard format (opens in Gephi/yEd). One export
   function reading straight from the existing graph.

6. **SVG export** of the dependency/architecture graph, same category as
   GraphML — an export format, not a new capability.

7. **Token-reduction self-measurement command** (`devgraph benchmark` or
   similar) — runs a query against DevGraph vs. a baseline (grep/full-file
   read) and reports the token delta. Useful for internal sign-off: answers
   "does this actually save tokens" with a number instead of a claim. Pairs
   with item #8.

8. **Office-safety sign-off doc.** Not code — a short markdown artifact
   stating what's already true: no outbound network calls by default, full
   dependency list, what data (if any) ever leaves the machine (answer:
   nothing, in the default configuration). Cheap now, avoids a scramble
   later when someone in security asks.

9. **Manifest ingestion** (package.json / requirements.txt / pyproject.toml
   / Cargo.toml, etc. → dependency nodes with version info, queryable).
   Same node/edge shape as our existing extractors, no architecture change.

10. **SCIP ingestion** — read an existing SCIP index (a standard code-
    intelligence format some language servers/tools already produce) into
    the graph, as an alternative or supplement to our own Tree-sitter
    extraction for a given file. This is an *ingest* path, not a new
    extractor — cheap to add, and gives free precision wherever a SCIP
    producer already exists for a language.

---

## Phase 2 — The big stuff (item by item, confirm approach first)

Each of these gets a short check-in before work starts — not because the
work itself is unclear, but because the *first* implementation choice is
expensive to undo (schema shape, which stage owns the logic, etc).

1. **Fix the node-identity collision.**
   `upsert_node` currently MERGEs `Function`/`Class` nodes on `(repo_id,
   name)` only, so two files defining a same-named function collapse into
   one graph node — already scoped in `HANDOVER_node_identity.md`. Fix is a
   compound-key MERGE on `(repo_id, file, name)`, extended to `containers`
   (`Service`/`Container`/`Volume` have the same collision across two
   compose files). This is a correctness bug affecting `get_source` and
   `find_callers` today, not a nice-to-have — should be near the front of
   this phase. Touches every extractor's write path; needs a full
   482-test rerun after, and the open sub-question (does a same-file
   sibling-class collision also need closing?) needs a decision before
   the schema change lands, not after.

2. **Edge confidence tagging** (`EXTRACTED` vs `INFERRED`, Graphify's
   concept). We already know which of our own edges are heuristic
   guesses — the whole "name-based, not type-resolved" call-graph caveat
   documented in `DEVGRAPH-CLIENT.md` — we just don't surface it. Adds a
   property to `CALLS`/`IMPORTS` edges and a backfill pass per language
   extractor. Directly answers the "graph transparency" goal from the
   original audit.

3. **Symbol-resolution as its own stage**, replacing the current per-
   extractor "guess a same-repo file, silently drop the edge if wrong"
   approach baked into each language module. Pulling this into a shared
   post-extraction resolver stage means accuracy improvements apply to
   every language at once instead of one extractor at a time. Bigger
   refactor than it sounds — touches the dispatch/extraction pipeline,
   not just one file.

4. **Type-aware call-graph resolution**, to cut the documented false-
   positive over-linking on common method names (`get`, `run`, `close`
   all resolving to the same node regardless of which class they're on).
   Currently mitigated with `scope_to_class` as a query-time filter;
   this item is about fixing it at index time instead. Real design
   question: how much type inference is worth doing without a full
   compiler front-end per language — needs a scoping conversation before
   starting, likely one language at a time rather than all at once.

5. **Community detection** (Leiden or equivalent), reusing an existing
   graph library rather than writing clustering from scratch — this is a
   solved-algorithm problem, not something to reimplement. Writes a
   `community` property onto nodes, exposed via a new MCP tool/CLI query.
   Prerequisite for Phase-2 item #8 (wiki export) below.

6. **Batched Cypher writes** for large-repo indexing performance. Current
   write path is per-node/per-edge `session.run` calls in a loop (same
   pattern Graphify's Neo4j push uses, and it's slow for the same
   reason). Batching writes would matter once indexing a genuinely large
   monorepo becomes a real scenario — confirm that's an actual near-term
   need before prioritizing this over the correctness items above it.

7. **PR/issue ingestion — actually implement the fetch.** Currently the
   opt-in flags exist (`pr-source enable`, `issue-source enable`) but
   there's no CLI command that performs the fetch — it requires a short
   hand-written Python script today. Build the real fetch path
   (`devgraph pr-source fetch <repo_id>` or similar) using the existing
   `GitHubSource`/`index_pr_issues` machinery. **Do not enable or run
   this against any repo without the repo owner's explicit go-ahead** —
   this is the one part of DevGraph that makes outbound network calls,
   per the existing non-negotiables in `DEVGRAPH-CLIENT.md`. Building
   the command is a dev task; using it needs a human decision every time,
   not just once.

8. **Agent-crawlable markdown wiki export** (index.md + one article per
   community — a zero-tool-call consumption mode, good fit for an
   anti-MCP shop since nothing needs to be invoked at all, just read).
   Depends on #5 (community detection) existing first.

9. **Work-memory / reflection feedback loop.** Graphify's `save-result` /
   `reflect` idea: record how a past query's answer actually turned out
   (`useful` / `dead_end` / `corrected`), aggregate that into a recency-
   weighted "preferred/tentative/contested" hint attached to graph nodes,
   and surface it on future queries with a "code changed since — re-
   verify" flag when the underlying source has moved. No code to port
   here — it's a feedback-loop design, not a language-specific
   implementation — but it's a genuinely new subsystem (a small side
   store plus a query-result annotation step), not a small patch.
   Highest-differentiation item in this phase: nothing else here makes
   the graph get smarter from actual use.

10. **Finish or drop `compare_branches`.** Currently registered and
    callable but explicitly documented as a stub not wired to real git
    metadata. Pick one — either finish it properly or remove it so it
    stops returning unreliable results silently.

---

## Phase 3 — Gated behind a user or architectural decision

None of these should be started on engineering judgment alone — each one
changes a property of the system (what data can leave the machine, whether
a second backend exists, whether a network listener runs) that needs an
explicit decision first.

1. **Alternative JSON/flat-file storage backend, alongside Neo4j.**
   Architectural decision: introduce a `GraphEngine` interface with two
   implementations (Neo4j, JSON/NetworkX-backed), rather than assuming
   Neo4j everywhere. This is the single biggest remaining adoption-
   friction item after the MCP problem — day one currently requires
   Podman + a running Neo4j container, which is exactly the kind of new-
   service-running-in-the-background thing a security-cautious office is
   slow to approve. A JSON-backed mode would remove that barrier
   entirely: nothing running, just files on disk.
   **Trade-offs to accept knowingly, not by accident:**
   - Slower at scale — a flat-file backend reloads/re-parses the full
     graph on every query rather than using Neo4j's indexes; fine for a
     single small-to-medium repo, noticeably slower on a large monorepo
     queried repeatedly in one session.
   - The `run_cypher` escape hatch has no equivalent without a real graph
     DB — JSON mode would need its own, smaller query surface.
   - The live web dashboard (Cytoscape + SSE, built against a Cypher-
     proxy) would either need a JSON-native equivalent or become a
     Neo4j-only feature — decide which before starting.
   - Concurrent access needs its own story (file locking) since Neo4j's
     concurrency guarantees go away.
   **What's gained beyond easier install:** several Phase-2 items (Leiden
   clustering, shortest-path) are *already-solved* problems in this mode
   via NetworkX — the same library Graphify itself uses — so building
   the analysis layer against an abstract graph interface (rather than
   Cypher-only) means those features work on both backends for roughly
   one implementation's worth of effort, not two.
   **Recommendation: yes, worth doing**, but sequence it *after* Phase 2's
   query-CLI and confidence-tagging work, since those need to be backend-
   agnostic anyway once this lands — building the abstraction first and
   retrofitting Cypher-only tools onto it later is less churn than the
   reverse order.

2. **Docs/PDF/Office/Google Sheets ingestion via an LLM semantic pass.**
   This is Graphify's design, not an oversight of ours — it's a direct
   fork from DevGraph's "zero LLM calls, fully deterministic, nothing
   leaves the machine" principle. Adding it means picking a model backend
   (local via Ollama, or an API key) and accepting that this specific
   ingestion path is the one place data leaves the machine. Needs an
   explicit decision on whether that trade is acceptable at all, and if
   so, which backend is allowed (self-hosted/local-only vs. any external
   API). Do not build this speculatively.

3. **Video/audio transcription ingestion.** Same gate as #2 — an LLM/ML
   inference dependency (faster-whisper) with real compute cost, only
   worth building if a real use case shows up, not preemptively.

4. **External URL ingestion** (fetch-and-index an arxiv paper, transcribe-
   and-index a YouTube video). Same gate as #2/#3, plus a scope question:
   is "index things that aren't in the repo" actually in charter for a
   codebase-structure tool, or scope creep borrowed from Graphify's
   broader "knowledge graph for everything" ambition. Needs a product
   decision, not just a technical green light.

5. **MCP HTTP transport for a team-shared server** (vs. today's stdio-
   only, one-process-per-client model). Only relevant if/when multiple
   people need to query one shared instance instead of each running their
   own local DevGraph — currently a single-user local tool by design.
   Opening any network listener at all is exactly the kind of change that
   needs a security conversation first, even with auth and DNS-rebinding
   protection built in.

6. **Live Postgres/Cargo introspection** (reading a running database's
   live schema, not just static manifest files). Gated on whether
   pointing DevGraph at a live instance — potentially a production-
   adjacent one — is an access pattern anyone actually wants to allow.
   Different risk profile than parsing files already sitting in the repo.

7. **PR triage / conflict-detection features** (`graphify prs --triage`
   ranks a review queue using whatever model backend is configured).
   Explicitly LLM-inference-gated — don't build ahead of Phase-2 item #7
   (the basic PR fetch path) existing, and don't build at all without
   deciding which model backend, if any, is allowed to see PR content.

8. **Obsidian vault export.** Lowest-stakes item on this list, but still
   gated on one simple question: does anyone here actually use Obsidian.
   No point building a niche export with zero users.

---

## Suggested sequencing across phases

Phase 1 can start immediately and finish in roughly one to two weeks of
focused work, item order doesn't matter much beyond #1 unblocking #2.
Phase 2 should be scoped one item at a time as check-ins happen — don't
batch-approve the whole phase, since items #1 (node-identity fix) and #3
(symbol resolution) are large enough that getting the first design
decision wrong is expensive. Phase 3 has no default sequencing — each item
sits parked until its specific gate is explicitly cleared; the JSON-backend
item is the only one likely worth raising proactively, since it's the
second-biggest adoption blocker after the MCP problem Phase 1 fixes.
