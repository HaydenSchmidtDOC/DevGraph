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

Phases 1-3 of the roadmap are implemented and verified (195 tests, run
against a live local Neo4j instance):

- **Phase 1** — repository registry, Tree-sitter Python indexer (including
  `CALLS` edge extraction, docstring extraction into `description`/
  `docstring_full`, and function/class line-range capture),
  container/API/datastore extractors, file/git watcher wired to the indexer
  (`devgraph add`/`rescan`, the watcher, and the tray app all invoke it
  automatically), graph engine, MCP server, CLI, tray app.
- **Phase 2** — `Requirement`/`DesignDecision`/`ArchitectureNote` nodes from
  Markdown front-matter, linked to code via `SATISFIES`/`DOCUMENTED_BY`.
- **Phase 3** — git commit history (local, incremental) and opt-in
  PR/issue ingestion (the first component that talks to an external
  service, off by default per repo).

17 MCP tools are registered and reachable over a real stdio transport (see
Interface below), including `get_source` for fetching a function/class's
actual source text plus its full docstring. Phase 4 (optional enterprise
federation) is design-only — see `Blueprints/Design Brief #2.md` — and should
not be implemented without a fresh design pass informed by real
multi-developer usage.

See [Blueprints/Implementation Plan #2.md](Blueprints/Implementation%20Plan%20%232.md)
for the rollout-readiness and extraction-quality work covered by this
README's Setup/`doctor`/`client-config`/`--full` sections.

## Prerequisites

- **Python 3.13+**
- **Git**
- **Podman** (container runtime for this project — not Docker)
- **Neo4j 5.26 Community Edition**, run as an isolated Podman container
  (see Commands below) — no native install needed

## Setup

```powershell
.\scripts\bootstrap.ps1
```

One command: checks Python/git/Podman are present (never installs them),
creates/reuses `.venv`, does the editable install, starts (or reuses)
DevGraph's isolated `devgraph-neo4j` container, waits for it to become
reachable, initializes the schema, and finishes by running `devgraph doctor`
to confirm everything actually worked. Idempotent — safe to re-run any time.

What it runs under the hood, for reference (or if the script itself is
unavailable):

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"

podman run -d --name devgraph-neo4j \
  -p 127.0.0.1:7474:7474 -p 127.0.0.1:7687:7687 \
  -e NEO4J_AUTH=neo4j/devgraph-local-dev \
  -v devgraph_neo4j_data:/data -v devgraph_neo4j_logs:/logs \
  docker.io/library/neo4j:5.26-community

.venv/Scripts/python -m pytest   # 195 tests, exercised against that container
```

On this machine `podman.exe` may not be on `PATH` by default — check
`%LOCALAPPDATA%\Programs\Podman` before assuming it isn't installed (the
bootstrap script checks both places itself). DevGraph's containers are always
named `devgraph-*`; never touch containers or volumes with a different
prefix. `deploy/podman-compose.yml` is an untested/best-effort alternative
for developers who already have a compose provider installed — the
bootstrap script above is the supported path and needs no compose provider.

## Using it: registering and indexing a repository

```bash
devgraph add <path-to-a-git-repo>          # registers it and runs a full scan (repo_id defaults to folder name)
devgraph add <path-to-a-git-repo> --full   # also indexes git commit history in the same step
devgraph list                              # see registered repos and their repo_id
devgraph status                            # Neo4j connectivity + repo counts + tray liveness
devgraph doctor                            # heavier environment-drift diagnostic (Python/mcp versions, Neo4j, Podman, tray)
devgraph client-config                     # print this machine's MCP registration command (portable, no hardcoded paths)
```

`devgraph add`/`rescan` run a real full scan automatically — Python source,
container/compose files, API routes, and datastore usage all get indexed in
one pass (`devgraph/indexer/dispatch.py`). `--full` additionally runs
incremental git-history indexing in the same step, equivalent to a separate
`index-history` call:

```bash
devgraph rescan <repo_id> --full                         # re-scan + history in one step
devgraph index-history <repo_id>                         # git commit history alone (local, incremental)
devgraph annotate <repo_id> --docs-path devgraph/docs     # register a docs folder
devgraph annotate <repo_id> --note devgraph/docs/x.md     # index one Requirement/DesignDecision/ArchitectureNote
devgraph pr-source enable <repo_id>                       # opt in before any PR/issue network call
devgraph issue-source enable <repo_id>
```

## Interface: connecting an MCP client (e.g. Claude Code)

DevGraph runs as a standard MCP stdio server:

```bash
.venv/Scripts/python -m devgraph.mcp.server
```

Point any MCP-capable client at that command. Run `devgraph client-config` to
print the exact command/args/cwd (and a ready-to-run `claude mcp add`
one-liner) for this checkout, using this machine's actual resolved venv
python path — don't hardcode a path, since DevGraph's install location can
differ machine to machine:

```bash
devgraph client-config                    # full Markdown block, portable across machines
devgraph client-config --claude-mcp-add-only   # just the one-liner
devgraph client-config --run              # also execute it via the 'claude' CLI (opt-in)
```

Once connected, the client sees all 17 tools (`search_component`,
`find_callers`, `impact_analysis`, `explain_architecture`,
`explain_decision`, `blame_component`, `get_source`, etc.) — every tool takes
a `repo_id` (the id shown by `devgraph list`) and defaults to that repo only;
`cross_repo=true` is an explicit opt-in per call, never automatic.
`find_callers`/`impact_analysis`/`find_related_files` match by function/class
**name**; `blame_component` matches by **file path** — passing the wrong kind
of identifier looks like an empty result, not a broken tool.
`run_cypher` is a raw-Cypher escape hatch that is **not** registered unless
`DEVGRAPH_ENABLE_RUN_CYPHER=true` is set — everything else should go through
the purpose-built tools.

## Security defaults

All off unless explicitly enabled: telemetry, cloud sync, cross-repo query
results, raw Cypher access, and (Phase 3) PR/issue ingestion — each is a
per-repo or per-instance opt-in, never a default. DevGraph only ever
watches/indexes paths that have gone through `devgraph add`; there is no
machine-wide or recursive discovery anywhere in the codebase.
