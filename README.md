# DevGraph

DevGraph is a local-first developer knowledge graph platform. It builds and maintains a structured architecture graph (Neo4j) of explicitly-registered repositories — code structure, container/API/datastore topology, design intent, and git/PR/issue history — and exposes it to coding assistants through an MCP server. Instead of an AI re-reading source files on every request, it asks relationship/dependency/impact questions against a pre-built graph.

## Core design principles

- **Explicit registration only.** DevGraph never scans, watches, or indexes a path that hasn't been explicitly registered (`devgraph add <path>`). No machine-wide or recursive discovery.
- **Local-first.** No cloud dependencies, no telemetry, no external API calls by default.
- **Repository isolation.** Every graph object is scoped to a `repo_id`; queries default to the current repo and never leak cross-repo results unless explicitly opted into.
- **AI-optimized surface.** The MCP layer exposes high-level, purpose-built tools (`find_callers`, `impact_analysis`, `explain_architecture`, etc.) rather than requiring raw Cypher.

## What it's for

Coding assistants working in a registered repository can query DevGraph's MCP tools to understand call graphs, service dependencies, design rationale, git/PR history, and the blast radius of a proposed change — grounded in a graph built from the actual codebase rather than inferred from a limited context window.

## Documentation

- [Blueprints/](Blueprints/) — numbered design docs: target architecture, phased roadmap, and build plans.
- [DEVGRAPH-CLIENT.md](DEVGRAPH-CLIENT.md) — how another repo's coding assistant connects to and uses a running DevGraph instance.
- [PROJECT_STATUS.md](PROJECT_STATUS.md) — current implementation state, commands, and structure.
- [CLAUDE.md](CLAUDE.md) — working agreement for AI agents contributing to this repo.
