# DevGraph — Design Brief #3: Practical Usefulness & Token-Saving Improvements

Companion to `Design Brief #1.md` (target architecture) and `Implementation
Plan #2.md` (rollout readiness + extraction quality, implemented). This brief
proposes the next round of improvements, prioritized by how directly they cut
the token cost DevGraph exists to reduce, informed by live testing against
RAG4 and by patterns from comparable code-intelligence tools (Sourcegraph/
SCIP, Glean, LSP-based context servers, RAG-over-code products).

**This is a design brief, not a build-ready implementation plan** — like
Design Brief #1 before Implementation Plan #1, it establishes scope and
rationale; a future Implementation Plan #3 should turn whichever items are
approved into build-ready steps the way Implementation Plan #2 did for its
own scope. Nothing here should be implemented directly against this document
without that follow-up pass, per this repo's own working-style precedent.

## Context

Implementation Plan #2 closed the two gaps that separated "works" from
"ready for a developer to pick up without hand-holding": operational
friction (bootstrap, `doctor`, `client-config`, `--full`) and the two
highest-leverage extraction gaps (`description`/`docstring_full`,
`get_source`). With those landed, DevGraph is functionally complete against
Design Brief #1's Phase 1-3 scope and verified end-to-end against RAG4.

The question this brief answers: **given a working platform, what would
make it more practically useful to an agent working day-to-day, or save
more tokens than it already does?** Four items surfaced, each grounded in a
concrete limitation observed during live testing or a pattern borrowed from
a comparable tool:

1. **Token-budgeted result shaping** — cheapest, most direct token saving,
   no new infrastructure.
2. **Scope `CALLS` resolution to the enclosing class's MRO** — fixes a
   documented noise source using data already indexed.
3. **Diff/PR-scoped impact analysis** — composes existing tools into the
   single highest-value agentic workflow this class of tool is built for.
4. **Embedding-backed semantic search** — highest ceiling, but the only
   item that adds a new dependency/index, so it's scoped last and made
   explicitly optional.

Ranked by cost-to-value: **(1) > (2) > (3) > (4)**. Each item includes a
recommendation on whether it should proceed, and why, per the project's
"give a recommendation, not an exhaustive survey" working style.

---

## Item 1 — Token-budgeted result shaping

### The problem

Every list-returning MCP tool currently returns its full match set with, at
best, an arbitrary hard cap:

- `search_component` — `LIMIT 50`, always returns all 50 as full rows
  (`name`, `labels`, `repo_id`, `description`).
- `find_callers`, `list_services`, `find_related_prs`, `issue_history_for` —
  **no limit at all.** On a repo where a name-based `CALLS` edge fans out
  widely (see Item 2), `find_callers` can return dozens of rows for a
  commonly-named function.
- `find_related_files`, `impact_analysis` — return `COLLECT(DISTINCT ...)`
  lists with no cap; `impact_analysis`'s `transitive_dependents` in
  particular can grow combinatorially on a well-connected repo.

None of these tools tell the caller "there were more results than shown" —
a capped `search_component` result silently drops matches 51+, which is a
correctness gap as much as a token one.

### What comparable tools do

Sourcegraph/Cody and Glean's MCP-style code tools return a **count-first,
sample-second** shape by default: total match count, a bounded sample (10-20
items), and an explicit continuation signal (cursor or "N more not shown")
rather than either truncating silently or returning everything. The design
intent is that an agent asks a *broad* question first, sees the shape of the
answer, and only pays for the full list when it actually needs every item.

### Proposed change

Add a shared response envelope for DevGraph's highest-cardinality tools:

```json
{
  "count": 47,
  "results": [ /* up to max_results, default e.g. 15 */ ],
  "truncated": true
}
```

- Apply to: `find_callers`, `find_related_files` (each collected list),
  `impact_analysis` (each collected list), `list_services`,
  `find_related_prs`, `issue_history_for`. `search_component` already caps
  at 50 — lower the default sample and add the `count`/`truncated` fields
  rather than changing its cap.
- Add an optional `max_results: int` parameter (sane default, e.g. 15-25)
  so an agent that genuinely wants the full set can ask for it explicitly,
  rather than the cap being invisible and unchangeable.
- **Do not** apply this to tools that already return a single bounded
  object (`summarise_repository`, `explain_architecture`,
  `get_service_dependencies`, `explain_decision`, `trace_design_rationale`,
  `get_source`) — those aren't the token-cost source.

### Why this ranks first

No new infrastructure, no new dependency, touches only `devgraph/mcp/
tools.py`'s Cypher/return shaping. Directly reduces token spend on every
call to the tools an agent uses most, and fixes a real correctness gap
(silent truncation) as a side effect. Backward-compatible if `results` keys
stay the same and only `count`/`truncated` are additive — worth confirming
during implementation whether existing callers assume a bare list return
(Design Brief #1's principle 4 examples assume structured dicts already, so
this should be a low-risk shape change).

**Recommendation: build first.** Lowest cost, most direct alignment with
DevGraph's stated purpose ("without the AI re-reading the entire repository"
— Design Brief #1's Success Criteria).

---

## Item 2 — Scope `CALLS` resolution to the enclosing class's MRO

### The problem

`CALLS` edges are name-based, not type-resolved (documented in `CLAUDE.md`,
`DEVGRAPH-CLIENT.md`, and `devgraph/indexer/python/extractor.py`'s
`_extract_call_targets` docstring): `self.foo()`, `obj.foo()`, and a bare
`foo()` all link to *every* `Function` node named `foo` repo-wide. This is a
deliberate, documented tradeoff (over-link rather than under-link), but it's
the single most-cited noise source in live-test feedback that shaped
Implementation Plan #2's item 2 tool-docstring clarifications — an agent
calling `find_callers` on a common method name (`get`, `run`, `close`,
`validate`) gets a result set polluted with unrelated classes' same-named
methods, and has to manually filter by reading each match's `file` property.

### What comparable tools do

Full type resolution (what Sourcegraph's SCIP/LSIF-backed engine and a real
LSP `textDocument/references` do) requires a type checker or language
server — a materially different, heavier component than anything DevGraph's
Tree-sitter-based extractor does today, and explicitly out of scope per
`CLAUDE.md`'s IMPORTS-gap precedent (Implementation Plan #2 item 10 declined
a similarly-shaped fix for exactly this reason: "a different and riskier
class of extraction than anything else this indexer does").

### Proposed change — a partial, cheap win

DevGraph already indexes `EXTENDS` edges (class inheritance). For a
`self.foo()`/`obj.foo()` call made from *inside a method body*, the caller's
enclosing `Class` is already known at extraction time (`_visit_function`
receives `parent_name`/`parent_label`). Instead of resolving to every
`Function` named `foo` repo-wide, resolve preferentially to:

1. A `Function` named `foo` that is `CONTAINS`-linked to the enclosing
   class, or to any class reachable via that class's `EXTENDS` chain (the
   caller's own method-resolution order) — link **only** to these if any
   exist.
2. Fall back to the current repo-wide name match only when no MRO-scoped
   candidate exists (e.g. the call target genuinely isn't a method on the
   caller's own class hierarchy — a free function, a duck-typed call, an
   unrelated same-named method via composition rather than inheritance).

This does not attempt full type inference (`obj.foo()` where `obj`'s type
isn't the enclosing class is still unresolved/over-linked, honestly) — it
only tightens the one case DevGraph can resolve for free from data it
already has: `self.foo()` inside a method, which is also the single most
common call shape in real Python code.

**Bare `foo()` calls remain unchanged** — free-function calls have no
enclosing-class context to scope against, so they stay repo-wide name
matches as today.

### Cost/scope

Touches only `devgraph/indexer/python/extractor.py`'s `_extract_call_targets`
/ `_emit_call` (needs the enclosing class name and its resolved `EXTENDS`
chain in scope, which requires either a second graph read at extraction
time or restructuring extraction to a two-pass model: first pass builds
class hierarchy, second pass resolves calls against it). This is more
invasive than Item 1 but still self-contained to one extractor — no schema
change (still emits `CALLS` edges, just to a narrower target set), no new
MCP surface.

**Recommendation: build second.** Meaningfully improves the precision of
the most-used impact-analysis tools using only data already indexed, but
costs real extractor-restructuring work, so it should follow Item 1's
cheaper win. Flag directly to whoever picks this up: verify the two-pass
restructuring doesn't regress the existing 1207-edge CALLS extraction on
RAG4 before considering it done — this is exactly the kind of change
Implementation Plan #2's "verified end-to-end against RAG4" precedent
exists to guard against silently breaking.

---

## Item 3 — Diff/PR-scoped impact analysis

### The problem

"What does this PR touch, and what does that break?" is arguably the single
highest-value question an AI coding agent asks before or during a change —
and DevGraph already has every ingredient to answer it (git history via
`Commit`/`MODIFIES`, `impact_analysis`'s dependent-tracing Cypher) but no
tool composes them. Today an agent has to manually: diff the branch, extract
changed file paths, map each file to its contained Functions/Classes
(`find_related_files` per-file), then call `impact_analysis` once per
component and manually union the results. That's several round-trips of
tool calls and manual aggregation the agent pays token cost for on every
use — exactly the kind of repeated, composable query a purpose-built tool
should absorb (Design Brief #1 Principle 4: "AI-optimized surface... The MCP
layer should expose high-level, purpose-built tools").

### What comparable tools do

This is Glean's and Sourcegraph Cody's headline "AI agent" workflow —
PR-scoped or diff-scoped impact/blast-radius queries are the feature these
tools lead with for agentic use, specifically because it's the question an
agent asks immediately before proposing or reviewing a change.

### Proposed change

New MCP tool, `impact_analysis_for_diff`:

```python
def impact_analysis_for_diff(
    engine: GraphEngine,
    repo_id: str,
    base_ref: str,
    head_ref: str,
    cross_repo: bool = False,
) -> dict[str, Any]:
    """Analyze the combined impact of every component changed between two
    git refs. Composes local git diffing (GitPython, no network — same
    constraint as index-history) with the same dependent-tracing Cypher
    impact_analysis already uses, per changed component, then unions and
    deduplicates the result.

    Returns: changed_files, changed_components (Functions/Classes touched),
    direct_dependents (union across all changed components),
    transitive_dependents (union), and an overall risk_level rolled up from
    the highest individual risk_level among changed components.
    """
```

Implementation shape (build-ready detail left to a future Implementation
Plan #3, per this brief's own framing above):

- Diffing is **local only** — `git diff --name-only base_ref..head_ref` via
  GitPython against the registry-resolved repo root, matching
  `git_history/extractor.py`'s existing no-network constraint. Never
  fetches refs from a remote; both `base_ref`/`head_ref` must already exist
  locally (same trust boundary as any other git-history tool here).
- Changed file paths → repo-relative Module names (same keying the Python
  indexer already uses) → `CONTAINS`-linked Function/Class nodes via a
  single Cypher pass, not N separate `find_related_files` calls.
- Reuses `impact_analysis`'s existing dependent-tracing Cypher per
  component (or, better, restructure it as one parameterized query taking
  a list of component names, to avoid N round-trips inside the tool's own
  implementation — an internal Cypher optimization, not a new pattern).
- Subject to Item 1's response-shaping envelope once that lands, since a
  multi-file diff's unioned dependent set is exactly the kind of
  high-cardinality result Item 1 targets.

### Cost/scope

New tool in `tools.py` + `server.py` (18th tool), new Cypher composing
existing patterns, a git-diff read using the same GitPython dependency
already in `pyproject.toml` (no new dependency). No schema change. The
main design risk flagged for the follow-up implementation plan: this is the
first MCP tool that takes two git refs as input rather than a component
name — confirm `base_ref`/`head_ref` validation (must resolve locally,
never treated as shell input, same injection-safety posture as
`git_history/extractor.py`'s existing ref handling) gets explicit review
during implementation, not assumed safe by analogy.

**Recommendation: build third.** Highest workflow value of the four items,
but it's a genuinely new tool (not a fix to an existing one) and depends on
Item 1's response-shaping landing first so its unioned result sets don't
reintroduce the token-cost problem Item 1 exists to solve.

---

## Item 4 — Embedding-backed semantic search (optional)

### The problem

`search_component` today is a `CONTAINS`-substring match against `name`
and (as of Implementation Plan #2) `description`. This finds a component if
the caller already knows a name fragment or a docstring keyword, but not
"the code that handles retry backoff" when no component is named anything
close to "retry" or "backoff." Semantic search over code is the headline
feature of most RAG-over-code products, and DevGraph now has the raw
material for it — `description`/`docstring_full` populated on 642+ nodes on
RAG4 as of Implementation Plan #2 — that it didn't have before that plan
landed.

### What comparable tools do

Nearly every RAG-over-code tool (and Sourcegraph Cody's natural-language
search) embeds docstrings/symbol summaries into a vector index and blends
vector similarity with exact-match/keyword scoring. This is a different
retrieval mechanism than everything else DevGraph does (graph traversal),
so it's explicitly the odd one out among these four items.

### Proposed change — and why it should stay optional

- Embed each `description`(preferred, since it's already a short,
  information-dense PEP-257 summary — cheap to embed, avoids re-embedding
  full docstrings) for every node that has one, store the vector as a node
  property or in a lightweight local vector index (Design Brief #1's
  Principle 2 — local-first, no cloud dependency — rules out any hosted
  embedding API by default; a local embedding model, e.g. via
  `sentence-transformers` run once at index time, would be the
  local-first-compliant approach).
  - Neo4j 5.26 Community Edition — the pinned version — does **not**
    include native vector index support (that's a 5.x Enterprise / Neo4j
    AuraDB feature in most versions); confirm this constraint precisely
    before implementation, since it may mean either a separate lightweight
    local vector store (e.g. a flat file + numpy cosine similarity, given
    repo-scale node counts are in the low thousands, not millions) or
    reconsidering whether Neo4j Community actually supports what's needed
    at the pinned version — do not assume Enterprise-only vector indexing
    is available.
- `search_component` gains an optional `semantic: bool = False` parameter
  (opt-in, matching every other feature-flag pattern in this codebase —
  `cross_repo`, `enable_run_cypher`, `pr_source_enabled`) that blends vector
  similarity with the existing substring match rather than replacing it.
- **New dependency and new indexing step** — a local embedding model adds
  meaningful install/first-run cost (model download, embedding compute
  time on `rescan`) that none of DevGraph's current components require.
  This is a real tradeoff against Design Brief #1's "lightweight" framing,
  not a free win.

### Cost/scope

Meaningfully larger than Items 1-3: new dependency, new index maintenance
concern (embeddings must be regenerated when `description` changes — ties
into `rescan`'s existing idempotent-MERGE model, but is new surface area to
get right), and the Neo4j-Community vector-index question above needs to be
resolved before this can even be scoped concretely.

**Recommendation: worth doing, but treat as a separate, explicitly-scoped
follow-up rather than bundling into whatever implementation plan picks up
Items 1-3.** The value ceiling is real (this is the feature most likely to
make `search_component` feel qualitatively different, not just faster), but
it's the only item here that changes DevGraph's dependency footprint and
local-first posture, so it deserves its own design pass — confirming the
vector-storage approach and getting explicit sign-off on the new
dependency — rather than being implemented as a line item alongside three
much cheaper changes.

---

## Summary

| Item | What | New deps? | New schema/tools? | Recommendation |
|---|---|---|---|---|
| 1 | Token-budgeted result shaping | No | No (response shape only) | Build first |
| 2 | MRO-scoped `CALLS` resolution | No | No (narrows existing edges) | Build second |
| 3 | `impact_analysis_for_diff` | No | Yes (18th MCP tool) | Build third, after Item 1 |
| 4 | Embedding-backed semantic search | Yes | Possibly (vector storage) | Separate design pass |

None of these are implemented yet — this brief establishes scope and
rationale only, per its framing above. A future `Implementation Plan #3`
should turn the approved subset into build-ready steps at the same
granularity `Implementation Plan #2.md` did, including concrete file-level
edits, sequencing, and a live-verification plan against RAG4 — following
this repo's own established precedent rather than re-deriving that process.
