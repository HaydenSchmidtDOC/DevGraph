# DevGraph

A local-first, live-updating knowledge graph for codebases. DevGraph indexes explicitly registered repositories into Neo4j so coding assistants can query structure, dependencies, history, and design context without repeatedly scanning the whole tree.

It provides:

- purpose-built MCP tools for callers, impact, architecture, source, recency, requirements, mentions, and repository history;
- a CLI for setup, repository management, diagnostics, export, and MCP registration;
- a loopback-only dashboard for exploring the graph, git history, query results, and saved layouts; and
- a background watcher that keeps registered repositories current as files and git state change.

![DevGraph dashboard — a live graph of this repository](docs/dashboard.png)

Source extraction is selected per file, so one repository can mix **Python, JavaScript/TypeScript, C#, C++, Java, Kotlin, Rust, and Go**. DevGraph also recognizes container/compose files, API routes, datastore usage, Markdown annotations and mentions, and local git history.

## Quickstart

The interactive installer currently targets Windows PowerShell and requires Python 3.13 or newer, Git, and [Podman](https://podman.io/):

```powershell
irm https://raw.githubusercontent.com/HaydenSchmidtDOC/DevGraph/master/scripts/install.ps1 | iex
```

It clones DevGraph, creates its Python environment, starts Neo4j, verifies the installation, and offers to register DevGraph with detected Claude Code and VS Code clients.

From an existing clone, run:

```powershell
.\scripts\setup-menu.ps1
```

DevGraph never discovers repositories automatically. Register each repository explicitly; `register` defaults to the current directory and `add` remains an alias:

```powershell
devgraph register C:\path\to\repo --full
devgraph list
devgraph dashboard
```

Registration runs the initial source scan. `--full` also indexes local git history so recency queries work immediately.

## Common commands

Run `devgraph --help` or `devgraph <command> --help` for the complete, current interface.

| Task | Command |
|---|---|
| Register and initially index a repository | `devgraph register [path] [--full]` |
| Refresh source and reconcile git history | `devgraph rescan <repo_id> [--full]` |
| Inspect registered repositories | `devgraph list`, `devgraph info <repo_id>`, `devgraph stats [repo_id]` |
| Check installation and graph health | `devgraph status`, `devgraph doctor`, `devgraph self-test [repo_id]` |
| Open the dashboard | `devgraph dashboard` |
| Configure an MCP client | `devgraph client-config`, `devgraph mcp add`, `devgraph mcp doctor` |
| View settings, project schema, or tray logs | `devgraph config`, `devgraph config show / validate / eject`, `devgraph logs` |
| Export a repository graph | `devgraph export <repo_id> --format json|cypher|dot` |
| Update DevGraph | `devgraph update` |

`devgraph update` is the normal update path. It fast-forwards the configured branch, reinstalls DevGraph, runs `doctor`, and restarts the tray app if it was running. Commit or stash local changes first; `--force` only suppresses the dirty-tree guard. The older `scripts/update.ps1` entry point remains available for existing Windows installations.

## Operating model

- **Explicit registration.** DevGraph scans and watches only paths added with `devgraph register` or `devgraph add`.
- **Local-first.** Neo4j, the registry, source reads, and git history stay on the local machine. Telemetry, cloud sync, cross-repository queries, and raw Cypher are off by default.
- **Repository isolation.** Every graph object carries a `repo_id`. MCP queries stay within that repository unless the caller explicitly opts into a cross-repository query.
- **Live updates.** Connecting an MCP client starts the tray app when needed. The watcher reindexes file and git-state changes; manual rescans remain safe and idempotent.
- **Purpose-built queries.** MCP clients should use the registered tools and the live `devgraph://tool-catalog` resource rather than relying on a hand-maintained tool count.

## Project schema

A repository may declare extra node types in an optional `devgraph.schema.yaml` at its root. Registration and `devgraph rescan` resolve that file and provision a uniqueness constraint for each declared node type, keyed on `repo_id` plus the declared key (plus a `(repo_id, name)` lookup index for filesystem-sourced types, which the provider writes by `name`). Node types can be sourced from the repository's own files and folders with the filesystem provider:

```yaml
version: 1
node_types:
  - label: File
    key: [path]
    metadata: [{name: path}]
    source: {provider: filesystem, kind: file}
  - label: Folder
    key: [path]
    metadata: [{name: path}]
    source: {provider: filesystem, kind: folder}
relationships:
  - type: IS_CHILD_OF
    provider: filesystem
    from: [File, Folder]
    to: Folder
```

Every indexable file becomes a `File` node and every directory containing one a `Folder` node (`.` is the repository root), keyed by repo-relative path, with an `IS_CHILD_OF` edge to the parent folder. The watcher keeps them current; `search_component` finds them. A changed `devgraph.schema.yaml` is applied by a full rescan: the DevGraph agent runs it once the file has gone 5 minutes without further edits, and `devgraph rescan <repo_id>` (or `--now`) applies it immediately. The agent's automatic apply honours "Pause watching" and per-repo `devgraph watch disable` (`devgraph rescan` still applies immediately), and a schema that cannot be applied (invalid, or a constraint that cannot be created) is reported in the agent log and retried only after the file changes. Until then filesystem nodes stay as last applied. Applying also removes nodes and relationships of user types dropped from the schema; built-in types are never touched. Other user-declared node types are still constraint-only: nothing extracts them yet.

- A repository without the file behaves exactly as before.
- DevGraph's built-in constraints are always provisioned, including under `extends: none`, because every registered repository shares one Neo4j database.
- An invalid file fails before any graph write: `devgraph rescan` exits non-zero, while registration keeps the repository and reports a warning, the same way it already does when Neo4j is unreachable.
- `devgraph doctor` reports each repository's schema as absent, valid, or invalid, and flags two repositories that declare the same label with incompatible keys — a conflict that would otherwise leave one of them with no constraint at all.
- An invalid file never removes filesystem nodes: indexing skips the provider and carries on with the built-in extractors.
- Known limits of the filesystem provider:
  - A folder moved or trashed out of the repository as a whole may leave stale nodes until `devgraph rescan`, because the watcher ignores directory events.
  - `search_component` can return both a `Module` and a `File` for the same path; with `cross_repo=True` only the calling repository's declared labels are searched.
  - On the dashboard, filesystem nodes share the unfiltered canvas with code nodes, and the `?label=` filter accepts built-in labels only.
  - A symlinked file is represented at its target's path.
- `devgraph config validate` checks the file (or every registered repository's with `--all`) and exits non-zero on an invalid schema or a cross-repository conflict; `devgraph config show` prints the effective schema and where each entry comes from; `devgraph config eject` writes a commented starter file and never overwrites an existing one.
- `devgraph config` alone still shows DevGraph's settings; a single setting is now `devgraph config settings <key>`, and secret settings are masked.

## Project tools (preview)

A repository may declare read-only Cypher tools in an optional `devgraph.tools.yaml` at its root:

```yaml
version: 1
tools:
  - name: list_folder
    description: List the files directly inside a folder.
    cypher: |
      MATCH (f:File {repo_id: $repo_id})-[:IS_CHILD_OF]->(:Folder {repo_id: $repo_id, path: $folder})
      RETURN f.path AS path ORDER BY path
    parameters:
      - name: folder
        type: string
        required: true
        description: Repo-relative folder path ("." for the root).
    max_rows: 100
    timeout_s: 10
```

- **Serving.** The MCP server exposes a repository's tools to clients, one MCP tool per entry. The session's repository is `DEVGRAPH_MCP_REPO` (a repo id, or an absolute path inside a registered repository), else the server's working directory if it is inside a registered repository (the deepest match wins), else none, in which case no project tools are served. A `DEVGRAPH_MCP_REPO` value that matches no registered repository also means no scope. To pin a project in Claude Code, run this in the repository: `claude mcp add devgraph -e DEVGRAPH_MCP_REPO=<repo_id> -- "<venv python>" -m devgraph.mcp.server`.
- **Calls.** DevGraph injects the session's `repo_id` into every call and runs the query in a read-only transaction with the tool's `timeout_s` and `max_rows`. Results use the same envelope as the built-in tools, `{count, results, truncated}`. Timeouts, write attempts and Neo4j errors come back as short, curated error messages. Built-in tool names are never taken over, a tool that fails registration is skipped without affecting the others, and an invalid file serves no project tools. The `devgraph://project-tools` resource lists what the session serves, and `devgraph://tool-catalog` includes them.
- **Hot reload.** The server checks the file every 2 seconds and serves the new set without a restart, telling the client its tool list changed (clients that support `tools/list_changed` re-list automatically). An invalid save keeps the last good tools and records a notice in `devgraph://project-tools` (a file that is invalid when the session starts serves no project tools until fixed).
- **Scope caveat.** `$repo_id` is only required to be referenced. Pinning a session controls which tools it gets, not which data a trusted tool author's query reads, so a query that ignores `$repo_id` in its patterns can still read other repositories. Registering a repository means trusting its tools file as you trust its code.
- Each query must be read-only (no `CREATE`, `INSERT`, `MERGE`, `SET`, `DELETE`, `DETACH`, `REMOVE`, `DROP`, `FOREACH`, `LOAD CSV`, `CALL`, `USE`, `SHOW`, `TERMINATE`, `ALTER`, `GRANT`, `DENY`, `REVOKE` or `RENAME`, and no `apoc` reference) and must reference `$repo_id`, which DevGraph injects. That check only confirms the query references `$repo_id`; see the scope caveat above. Parameter names may not be Python keywords (`from`, `in`, `class`, ...) or start with `model_`. Every other `$name` it uses must be a declared parameter (`string`, `integer`, `float` or `boolean`), and every declared parameter must be used. `max_rows` is 1-1000 (default 100) and `timeout_s` is 1-60 (default 10).
- An invalid file is rejected as a whole. `devgraph config validate` (or `--all`) exits non-zero on it, `devgraph config show` prints the declared tools and fails on an invalid file, and `devgraph doctor` reports each repository's tools as absent, valid or invalid.
- A tool named like one of DevGraph's built-in tools is reported as a warning, not an error; the built-in always wins.

## Dashboard

The tray app serves the dashboard at `http://127.0.0.1:8765`. It shows registered repositories, graph and git information, query telemetry, an interactive graph canvas, query-driven highlighting, and saved per-repository layouts. The repository picker can register a local path and run its initial scan; if indexing fails, the registration remains available for retry. Server-Sent Events refresh the view after indexing changes.

The service binds to loopback and has no authentication because it is intended as a single-user local tool. The browser never receives Neo4j credentials; graph queries run through the FastAPI backend. Use `DEVGRAPH_DASHBOARD_ENABLED=false` to disable it or `DEVGRAPH_DASHBOARD_PORT` to choose another port.

## Optional indexing

- `devgraph annotate` configures Markdown requirements, design decisions, and architecture notes.
- `devgraph mentions <repo_id> enable` opts a repository into Markdown-to-entity mention indexing.
- `devgraph pr-source` and `devgraph issue-source` control the explicit opt-ins for external PR and issue ingestion.
- `devgraph index-history` initializes or manually refreshes local commit history; after initialization, the watcher reconciles history automatically when git state changes.

External PR and issue ingestion remains opt-in and requires a configured source. Enabling its registry flag does not itself contact a remote service.

## Containerized deployment

The day-to-day path uses Podman for Neo4j and runs DevGraph from its local Python environment. A full Docker Compose deployment is also defined in [deploy/docker-compose.yml](deploy/docker-compose.yml):

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml exec devgraph devgraph register /repos/<name>
```

Bind-mount each allowed repository under `/repos` before registering it. The local Python environment with Podman-hosted Neo4j is the regularly exercised path; treat the full Compose deployment as an alternative that still needs validation in your environment. The MCP server uses stdio and is spawned by each client; see [DEVGRAPH-CLIENT.md](DEVGRAPH-CLIENT.md).

## Current limitations

- Call edges are name-based rather than fully type-resolved, so common method names can over-link.
- Import resolution is a best-effort same-repository guess. C++ extraction is intentionally structural because reliable include resolution needs build-system context.
- `compare_branches` is registered but remains a stub; use `impact_analysis_for_diff` for local-ref impact analysis.
- Enterprise federation and semantic search are design directions rather than shipped capabilities.

## Contributing

Read [CLAUDE.md](CLAUDE.md) for the working agreement and [PROJECT_STATUS.md](PROJECT_STATUS.md) for implementation details and known gaps. Numbered design documents live under [Blueprints/](Blueprints/).

Keep changes focused, update documentation when behavior changes, and open an issue before building a speculative extractor, heuristic, or MCP tool. Indexer changes should be checked against a real public repository in the target language as well as the automated tests.

## Documentation

- [DEVGRAPH-CLIENT.md](DEVGRAPH-CLIENT.md) — connecting another repository and using DevGraph from an AI assistant.
- [PROJECT_STATUS.md](PROJECT_STATUS.md) — current implementation, commands, structure, and limitations.
- [Blueprints/](Blueprints/) — design briefs and implementation plans; some describe future work.
- `devgraph --help` — the authoritative CLI surface.
- `devgraph://tool-catalog` — the authoritative MCP tool catalog once connected.
- `devgraph://project-tools` — the session's repository scope, the project tools it serves, and notices.
