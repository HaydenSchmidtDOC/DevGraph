# Agent instructions — DevGraph

This repo is pre-implementation (see [CLAUDE.md](CLAUDE.md) for status). There is no knowledge graph, indexer, or MCP server built yet for *this* project — don't call or reference a `code-review-graph`/DevGraph MCP tool here even if one happens to be available in your tool list; it belongs to a different project.

## How to work in this repo right now

Because there's no code yet, the usual "search the codebase first" workflow doesn't apply. Instead:

- **Ground every architectural suggestion in [Blueprints/Design Brief #1.md](Blueprints/Design%20Brief%20%231.md).** It's short enough to read in full — do that instead of relying on a summary, since the brief is the actual spec (graph schema, relationship types, MCP tool list, security requirements, roadmap phases).
- **Before proposing new architecture or a new blueprint**, check `Blueprints/` for an existing numbered brief that already covers it. Add a new numbered brief for major design decisions rather than editing #1 in place, so design history stays traceable.
- **Don't invent tooling, dependencies, or a tech stack ahead of need.** The brief lists a *recommended* stack (Python 3.13+, Neo4j Community, official Neo4j MCP server, Tree-sitter, Watchdog, Typer, SQLite, Pydantic) — treat it as a strong default, not a locked decision, and don't scaffold services speculatively without being asked.
- **When the first real code is added** (agent, indexer, registry, MCP server), update [CLAUDE.md](CLAUDE.md)'s Commands and Structure sections with what actually exists — don't leave it describing aspirational architecture once there's real code to point to.

## Hold these constraints on any code you write for this platform

These come directly from the Design Brief's Core Design Principles and Security Requirements — they're not optional style preferences:

- **Explicit repository registration only.** Every indexing/watching path must originate from a developer-registered repo (`devgraph add <path>`). Never implement or suggest recursive/machine-wide filesystem discovery.
- **No scanning outside mounted repositories**, no telemetry by default, no cloud calls, no outbound transmission of source code.
- **Every graph node/edge is repo-scoped** (`repo_id` property); MCP queries default to the current workspace's `repo_id` and must not blend in other repos' data unless the developer explicitly opts into cross-repo mode.
- **The MCP layer is the interface — not Cypher.** Design new MCP tools as high-level, task-shaped operations (e.g. `impact_analysis`, `find_callers`, `explain_architecture`), matching the tool surface sketched in the brief's "MCP Layer" section. Raw Cypher access is an advanced/back-door tool, never the primary path an AI assistant is expected to use.
- **Incremental indexing, not full rebuilds.** Any indexer design should update only changed files/objects on a git/file-save event, per the brief's Performance Requirements.

## Privacy

Same rule as CLAUDE.md: no real names, real paths, or real repo/vendor identifiers anywhere in this repo, including in example configs, sample graph data, or test fixtures for the indexer. Since this platform's purpose is literally to index file paths and repo contents, be extra deliberate that anything committed here — examples, screenshots, sample JSON — is fabricated, not pulled from a real mounted repository.
