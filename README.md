# DevGraph

DevGraph is a local-first developer knowledge graph platform. It builds and maintains a structured architecture graph (Neo4j) of explicitly-registered repositories — code structure, container/API/datastore topology, design intent, and git/PR/issue history — and exposes it to coding assistants through an MCP server. Instead of an AI re-reading source files on every request, it asks relationship/dependency/impact questions against a pre-built graph.

## Quickstart

Requires [Podman](https://podman.io/) and [Git](https://git-scm.com/) already
installed (see your organization's software portal if Podman isn't
approved/available yet). Run this from a PowerShell prompt:

```powershell
irm https://raw.githubusercontent.com/HaydenSchmidtDOC/DevGraph/master/scripts/install.ps1 | iex
```

This clones DevGraph, sets up its Python environment and Neo4j container,
and walks you through an interactive menu to register it with whichever AI
clients (Claude Code, VS Code) it finds on your machine. Nothing outside
that one command is required to get a working MCP connection.

Already have a local clone? Run `.\scripts\setup-menu.ps1` directly instead
of the one-liner above — same result, no re-clone. `.\scripts\bootstrap.ps1`
remains available for just the environment/Neo4j setup without the
interactive client-registration menu.

The one manual step the installer deliberately does not automate is
registering a repository to index — DevGraph never scans anything you
haven't explicitly pointed it at:

```powershell
devgraph add <path-to-a-git-repo>
```

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
