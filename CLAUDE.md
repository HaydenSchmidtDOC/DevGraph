# DevGraph

Local-first developer knowledge graph platform. Builds and maintains a structured architecture graph (Neo4j) of explicitly-registered repositories, exposed to coding assistants through an MCP layer — relationship/dependency/impact intelligence instead of plain semantic document retrieval. Full design intent lives in [Blueprints/Design Brief #1.md](Blueprints/Design%20Brief%20%231.md); read it before proposing architecture.

## Status

Phase 1 implementation in progress. `devgraph/` package scaffold, venv, and a local Neo4j container exist. Treat claims about specific extractors/tools as complete only once their code and tests actually land — check `devgraph/` before assuming a component works.

Git is initialized locally; no remote is configured yet. Container runtime is **Podman** (not Docker) — see the Design Brief and Implementation Plan.

## Commands

- Create venv: `python -m venv .venv`
- Install (editable, with dev deps): `.venv/Scripts/python -m pip install -e ".[dev]"`
- Run tests: `.venv/Scripts/python -m pytest`
- Start DevGraph's Neo4j (isolated container, not shared with other projects):
  `podman run -d --name devgraph-neo4j -p 127.0.0.1:7474:7474 -p 127.0.0.1:7687:7687 -e NEO4J_AUTH=neo4j/devgraph-local-dev -v devgraph_neo4j_data:/data -v devgraph_neo4j_logs:/logs docker.io/library/neo4j:5.26-community`
  (see `deploy/podman-compose.yml` for the declarative form; requires a compose provider to use directly — `podman run` above needs none)
- CLI entrypoint (once installed): `devgraph add <path>`, `devgraph list`, `devgraph status`

**Podman note**: on this machine `podman.exe` may not be on PATH by default even though it's installed — check `%LOCALAPPDATA%\Programs\Podman` before assuming it's missing. Never install new software to the host; DevGraph's dependencies live in `.venv/` and containers only. Never touch containers/volumes belonging to other projects (e.g. anything prefixed differently from `devgraph-`) — DevGraph's containers are named `devgraph-*` specifically to stay isolated from other services running on the same machine.

## Structure

- `Blueprints/` — numbered design docs for planned/completed work. `Design Brief #1.md` is the current source of truth for target architecture (principles, graph schema, MCP tool surface, roadmap phases). Check here before proposing new architecture; add new numbered briefs for major design changes rather than rewriting history in place.
- Root-level `CLAUDE.md` / `AGENTS.md` — working agreement and agent-facing instructions for this repo specifically.

## Core design principles (from the Design Brief — hold these as constraints, not suggestions)

- **Explicit registration only.** Never inspect, scan, or index a path the developer hasn't explicitly mounted. No machine-wide or recursive discovery, ever — including from inside a tool call you're implementing.
- **Local-first.** No cloud dependencies, no telemetry by default, no external API calls from the platform itself.
- **Repository isolation.** Every graph object carries a `repo_id`; queries default to the current workspace's repo and never leak cross-repo results unless the developer opts in.
- **AI-optimized surface.** The MCP layer should expose high-level, purpose-built tools (e.g. `find_callers`, `impact_analysis`, `explain_architecture`) — Cypher is an advanced escape hatch, not the primary interface.

## Working style

- **This repo has no `code-review-graph` / knowledge-graph MCP tool.** If such a tool ever appears in your available tools list for this session, it belongs to a different project — do not assume it applies here, and do not reference it in docs or code for this repo. (`.claude/skills/` and the hook-based `.claude/settings.json` that referenced it were removed as stale carry-over from another repo — don't re-add graph-tool skills/hooks until DevGraph's own MCP layer actually exists.)
- Read [AGENTS.md](AGENTS.md) for how to work in a docs-only / pre-code repo.
- When implementation starts, prefer targeted edits over rewriting whole files, and don't duplicate logic — but there's no `shared/` or established convention yet, so the first services you write **set** the convention; choose deliberately since later code will follow the pattern, not the other way around.

## Privacy

- **Never leave personal notes, data, or identifying items in any file in this repo.** No real names, no vendor/business names from personal documents, no personal local file paths (drive letters, `C:\Users\<name>\...`), no filenames from any personal document corpus. Use generic placeholders in examples (e.g. "the source repo path" instead of a real drive path). This holds even in a private repo — don't rely on repo visibility as the only safeguard.
- This matters especially here because the platform's own job is to index source trees and file paths — example paths, sample repo names, and mock graph data written into this repo should be fictional, not lifted from a real machine.
