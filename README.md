# DevGraph

Local-first developer knowledge graph platform. DevGraph builds and maintains
a structured architecture graph (Neo4j) of explicitly-registered
repositories — code structure, container/API/datastore topology, design
intent, and git/PR/issue history — and exposes it to coding assistants
through an MCP server, so an AI can ask relationship/dependency/impact
questions against a pre-built graph instead of re-reading source on every
request.

See [CLAUDE.md](CLAUDE.md) for the repo's working agreement, and
[Blueprints/Design Brief #1.md](Blueprints/Design%20Brief%20%231.md) /
[Blueprints/Implementation Plan #1.md](Blueprints/Implementation%20Plan%20%231.md)
for the full design and build plan. [Blueprints/Design Brief #2.md](Blueprints/Design%20Brief%20%232.md)
is a deliberately-unbuilt sketch of the optional Phase 4 (enterprise
federation) — nothing there is implemented.

## Status

Phases 1-3 of the roadmap are implemented and verified (154 tests, run
against a live local Neo4j instance):

- **Phase 1** — repository registry, Tree-sitter Python indexer,
  container/API/datastore extractors, file/git watcher, graph engine, MCP
  server, CLI, tray app.
- **Phase 2** — `Requirement`/`DesignDecision`/`ArchitectureNote` nodes from
  Markdown front-matter, linked to code via `SATISFIES`/`DOCUMENTED_BY`.
- **Phase 3** — git commit history (local, incremental) and opt-in
  PR/issue ingestion (the first component that talks to an external
  service, off by default per repo).

16 MCP tools are registered and reachable over a real stdio transport (see
Interface below). Phase 4 (optional enterprise federation) is design-only —
see `Blueprints/Design Brief #2.md` — and should not be implemented without a
fresh design pass informed by real multi-developer usage.

Not yet built: `CALLS` edge extraction (so `find_callers`/`impact_analysis`
only see `CONTAINS`/`IMPORTS`/`EXTENDS`/`USES` today, not actual call
graphs), a CLI wrapper for the container/API/datastore extractors (currently
invoked as Python functions, not `devgraph` subcommands), and the tray app's
watcher→indexer wiring (the watcher emits changed-file events; nothing
currently listens and re-indexes automatically).

## Prerequisites

- **Python 3.13+**
- **Git**
- **Podman** (container runtime for this project — not Docker)
- **Neo4j 5.26 Community Edition**, run as an isolated Podman container
  (see Commands below) — no native install needed

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"

podman run -d --name devgraph-neo4j \
  -p 127.0.0.1:7474:7474 -p 127.0.0.1:7687:7687 \
  -e NEO4J_AUTH=neo4j/devgraph-local-dev \
  -v devgraph_neo4j_data:/data -v devgraph_neo4j_logs:/logs \
  docker.io/library/neo4j:5.26-community

.venv/Scripts/python -m pytest   # 154 tests, exercised against that container
```

On this machine `podman.exe` may not be on `PATH` by default — check
`%LOCALAPPDATA%\Programs\Podman` before assuming it isn't installed.
DevGraph's containers are always named `devgraph-*`; never touch containers
or volumes with a different prefix.

## Using it: registering and indexing a repository

```bash
devgraph add <path-to-a-git-repo>      # registers it (repo_id defaults to folder name)
devgraph list                          # see registered repos and their repo_id
devgraph status                        # Neo4j connectivity + repo counts
```

Indexing is invoked per extractor today (no single "index everything"
command yet):

```python
from devgraph.graph.engine import GraphEngine
from devgraph.indexer.python.extractor import index_file

engine = GraphEngine("bolt://127.0.0.1:7687", "neo4j", "devgraph-local-dev")
engine.init_schema()
index_file(engine, "<repo_id>", "path/to/module.py")   # one call per .py file
```

```bash
devgraph index-history <repo_id>                        # git commit history (local, incremental)
devgraph annotate <repo_id> --docs-path devgraph/docs    # register a docs folder
devgraph annotate <repo_id> --note devgraph/docs/x.md    # index one Requirement/DesignDecision/ArchitectureNote
devgraph pr-source enable <repo_id>                      # opt in before any PR/issue network call
devgraph issue-source enable <repo_id>
```

## Interface: connecting an MCP client (e.g. Claude Code)

DevGraph runs as a standard MCP stdio server:

```bash
.venv/Scripts/python -m devgraph.mcp.server
```

Point any MCP-capable client at that command. For Claude Code, add it as an
MCP server (`claude mcp add` or the equivalent config entry) with:

- **command**: the absolute path to this repo's `.venv/Scripts/python.exe`
- **args**: `-m devgraph.mcp.server`
- **cwd**: this repo's root (so `devgraph`'s config/`.env` resolve correctly)

Once connected, the client sees all 16 tools (`search_component`,
`find_callers`, `impact_analysis`, `explain_architecture`,
`explain_decision`, `blame_component`, etc.) — every tool takes a `repo_id`
(the id shown by `devgraph list`) and defaults to that repo only;
`cross_repo=true` is an explicit opt-in per call, never automatic.
`run_cypher` is a raw-Cypher escape hatch that is **not** registered unless
`DEVGRAPH_ENABLE_RUN_CYPHER=true` is set — everything else should go through
the purpose-built tools.

## Security defaults

All off unless explicitly enabled: telemetry, cloud sync, cross-repo query
results, raw Cypher access, and (Phase 3) PR/issue ingestion — each is a
per-repo or per-instance opt-in, never a default. DevGraph only ever
watches/indexes paths that have gone through `devgraph add`; there is no
machine-wide or recursive discovery anywhere in the codebase.
