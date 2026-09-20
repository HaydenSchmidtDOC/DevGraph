# DevGraph

A local-first knowledge graph for your codebase — built for AI coding assistants to query instead of re-reading source files on every request.

DevGraph builds and live-updates a structured graph (Neo4j) of explicitly-registered repositories: code structure, container/API/datastore topology, design intent, and git/PR/issue history. It exposes that graph to coding assistants through an MCP server, and to you through a live web dashboard. A background watcher keeps everything current as files change — no manual rescans, no stale answers.

![DevGraph dashboard — a live graph of this repository](docs/dashboard.png)

## Why this exists

Ask a coding assistant "what calls this function" or "what breaks if I change this interface," and it either greps blindly or reasons from a context window that doesn't fit your codebase. DevGraph answers those questions from a pre-built graph instead: `find_callers`, `impact_analysis`, `explain_architecture`, `trace_request_flow`, and 16 other purpose-built MCP tools, all grounded in the actual repository rather than inferred from whatever fit in context.

Source extraction covers **Python, JavaScript/TypeScript, C#, C++, Java, Rust, and Go**, detected per file rather than per repo — a polyglot repo gets every file routed to the matching extractor automatically.

## Quickstart

Requires [Podman](https://podman.io/) and [Git](https://git-scm.com/). Run from PowerShell:

```powershell
irm https://raw.githubusercontent.com/HaydenSchmidtDOC/DevGraph/master/scripts/install.ps1 | iex
```

This clones DevGraph, sets up its Python environment and Neo4j container, and walks you through registering it with whichever AI clients (Claude Code, VS Code) it finds on your machine. That's the whole install.

Already have a clone? Skip the one-liner:

```powershell
.\scripts\setup-menu.ps1
```

The one thing the installer deliberately won't do for you is point DevGraph at a repo — it never scans anything you haven't explicitly registered:

```powershell
devgraph add <path-to-a-git-repo>
```

### Updating

```powershell
.\scripts\update.ps1
```

Pulls the latest `master`, reinstalls dependencies, re-verifies the environment (`devgraph doctor`), and restarts the tray app if it was running. Refuses to run over a dirty working tree rather than guessing what you want to keep.

## Core principles

- **Explicit registration only.** DevGraph never scans, watches, or indexes a path you haven't run `devgraph add` on. No machine-wide or recursive discovery.
- **Local-first.** No cloud dependencies, no telemetry, no external API calls by default.
- **Repository isolation.** Every graph object is scoped to a `repo_id`; queries default to the current repo and never leak cross-repo results unless explicitly opted into.
- **AI-optimized surface.** The MCP layer exposes high-level tools (`find_callers`, `impact_analysis`, `explain_architecture`) instead of requiring raw Cypher from the client.
- **Idempotent everything.** Setup, updates, and re-indexing are all safe to re-run — nothing here assumes it's the first time.

## Dashboard

A live-updating dashboard starts automatically with the tray app at `http://127.0.0.1:8765` (loopback only, no auth — this is a single-user local tool). It shows registered repos, entity/relationship counts, a searchable component index, git history, and an interactive graph canvas you can click through to explore neighbors. It updates itself over Server-Sent Events whenever the watcher reindexes a change — no manual refresh. Read-only by design; there's no way to edit the graph from the UI.

Disable it with `DEVGRAPH_DASHBOARD_ENABLED=false`, or move it off the default port with `DEVGRAPH_DASHBOARD_PORT`.

## Docker

Prefer Docker over Podman, or want the whole stack (Neo4j + watcher/indexer/dashboard) containerized instead of running in a local venv? A compose file exists at [deploy/docker-compose.yml](deploy/docker-compose.yml):

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml exec devgraph devgraph add /repos/<name>
```

**This path is currently untested** — Podman + the local venv is what's actually exercised day to day. Bind-mount each repo you want indexed under `/repos` first (same explicit-registration rule applies). The MCP server itself still runs via stdio, spawned directly by your client per [DEVGRAPH-CLIENT.md](DEVGRAPH-CLIENT.md) — it's not part of this stack either way.

## Where this is headed

DevGraph is under active development. Roughly, in order of what's next:

- **Broader language coverage.** Seven languages are extracted today; more Tree-sitter grammars are a template away (see [Adding a language](.claude/skills/adding-a-language/SKILL.md)) — Ruby, PHP, Kotlin, and Swift are reasonable next candidates.
- **Better call-graph resolution.** Today's `CALLS`/`IMPORTS` edges are name-based heuristics, not fully type-resolved. Tightening that per language (starting wherever false-positive edges hurt query quality most) is worth more than adding new tools.
- **Semantic/embedding search** over the graph, as a complement to the existing structural queries — flagged but deliberately deferred in [Implementation Plan #3](Blueprints/Implementation%20Plan%20%233.md).
- **Enterprise federation** (Phase 4, cross-team/cross-repo graph sharing) — currently design-only and intentionally unbuilt; see [Design Brief #2](Blueprints/Design%20Brief%20%232.md).

None of this is a fixed roadmap — it's a starting point for where contributions are most useful.

## Contributing

Issues and PRs are welcome. If you're picking this up cold:

1. Read [CLAUDE.md](CLAUDE.md) for the working agreement (minimal diffs, no speculative abstraction, keep docs current).
2. Check [PROJECT_STATUS.md](PROJECT_STATUS.md) for what's actually built vs. designed, and [Blueprints/](Blueprints/) for the design docs behind it.
3. Work in a branch, open a PR against `master`. Golden-repo verification (run the extractor against a real open-source project in that language) is the bar for anything touching an indexer — see the multi-language skill above for the pattern.
4. For anything speculative — a new language, a new extraction heuristic, a new MCP tool — open an issue first. Cheaper to align on scope before the diff exists than after.

Bug reports, gaps in extraction accuracy, and "this MCP tool returned something wrong" reports are all fair game for issues, not just feature work.

## Documentation

- [Blueprints/](Blueprints/) — numbered design docs: target architecture, phased roadmap, and build plans.
- [DEVGRAPH-CLIENT.md](DEVGRAPH-CLIENT.md) — how another repo's coding assistant connects to and uses a running DevGraph instance.
- [PROJECT_STATUS.md](PROJECT_STATUS.md) — current implementation state, commands, and structure.
- [CLAUDE.md](CLAUDE.md) — working agreement for AI agents contributing to this repo.
