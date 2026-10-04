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
| View settings, project schema, or tray logs | `devgraph config`, `devgraph config show / validate / eject / enable / disable`, `devgraph config schema list / add / edit / delete / reset`, `devgraph config tools list / add / edit / delete / reset`, `devgraph logs` |
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

Every indexable file becomes a `File` node and every directory containing one a `Folder` node (`.` is the repository root), keyed by repo-relative path, with an `IS_CHILD_OF` edge to the parent folder. The watcher keeps them current; `search_component` finds them. A changed `devgraph.schema.yaml` is applied by a full rescan: the DevGraph agent runs it once the file has gone 5 minutes without further edits, and `devgraph rescan <repo_id>` (or `--now`) applies it immediately. The agent's automatic apply honours "Pause watching" and per-repo `devgraph watch disable` (`devgraph rescan` still applies immediately), and a schema that cannot be applied (invalid, or a constraint that cannot be created) is reported in the agent log and retried only after the file changes. Until then filesystem nodes stay as last applied. Applying also removes nodes and relationships of user types dropped from the schema; built-in types are never touched. It also keeps the generated constraints and indexes in step: once no repository's applied schema declares a label any more (and no node carries it), its `<label>_repo_key` constraint and `<label>_repo_name` index are dropped — a label two repositories share keeps them until the last one drops it — and a changed `key` replaces the constraint once every repository declaring the label uses the new key (a disagreement is left alone and logged; so is a new key that duplicate nodes would violate, which is checked before anything is dropped). Labels that differ only by case across repositories share one constraint name and converge on the next apply once every repository spells the label the same way. A constraint you create by hand with exactly DevGraph's generated name and shape (`<label>_repo_key` on `repo_id` plus properties, or a `<label>_repo_name` index on `(repo_id, name)`) is treated as DevGraph's. `devgraph remove` and `devgraph prune` release the labels of the repositories they delete the same way. Other user-declared node types are still constraint-only: nothing extracts them yet.

- A repository without the file behaves exactly as before.
- DevGraph's built-in constraints are always provisioned, including under `extends: none`, because every registered repository shares one Neo4j database.
- An invalid file fails before any graph write: `devgraph rescan` exits non-zero, while registration keeps the repository and reports a warning, the same way it already does when Neo4j is unreachable.
- `devgraph doctor` reports each repository's schema as absent, valid, or invalid, and flags two repositories that declare the same label with incompatible keys — a conflict that would otherwise leave one of them with no constraint at all. It also marks disabled repositories and has a "Schema drift" section: for each repository, whether the applied schema matches the file (applied), a change is waiting for a rescan (pending, a warning), or it was never applied. The section is skipped when Neo4j is down. A "Schema constraints" section (also skipped when Neo4j is down) warns about stale DevGraph-generated constraints/indexes — no repository's applied schema or schema file declares the label and no node carries it, e.g. left behind before cleanup shipped — and `devgraph config schema prune-constraints [--label <label>...] [--dry-run]` drops them. The same section warns when a repository's applied label has no constraint (`devgraph rescan <repo_id> --now` re-provisions it) and when a key change is blocked by duplicate nodes. Only objects with DevGraph's generated names on non-built-in labels are ever considered.
- An invalid file never removes filesystem nodes: indexing skips the provider and carries on with the built-in extractors.
- A node type or relationship may carry an optional display colour, `color: "#rrggbb"`. Quote it: in YAML an unquoted `#...` is a comment, so `color: #3b82f6` reads as no colour. The colour is display-only, shown in the Colour column of `config show` and `config schema list`, and used by the dashboard (a node type's colour tints its row and nodes, a relationship's colour tints its type chip); a type without one gets a stable colour derived from its name. Because the file's hash covers it, changing a colour counts as a schema change and is applied by a rescan like any other.
- The dashboard builds its node-type and relationship-type lists (colours, counts, isolate toggles) from `GET /api/repos/<repo_id>/schema`, which reflects the schema last applied to the graph, on load, on repository change and after rescans run by the agent (a `devgraph rescan --now` in another process does not notify an open dashboard; reload the page). Declared labels work with the graph `?label=` filter and search. Built-in types look exactly as before. When the file has changed since the last scan (pending) or cannot be read (invalid), a hint under the type lists says so while the last-applied types stay shown. The route reports `schema_state` as `applied`, `pending`, `never`, `absent`, `invalid` or `disabled`; for `__all__` it is the union of the repositories' types with the most attention-needing state (invalid, then pending, never, applied, disabled, absent).
- Known limits of the filesystem provider:
  - A folder moved or trashed out of the repository as a whole may leave stale nodes until `devgraph rescan`, because the watcher ignores directory events.
  - `search_component` can return both a `Module` and a `File` for the same path; with `cross_repo=True` only the calling repository's declared labels are searched.
  - On the dashboard, filesystem nodes share the unfiltered canvas with code nodes.
  - A symlinked file is represented at its target's path.
- `devgraph config validate` checks the file (or every registered repository's with `--all`) and exits non-zero on an invalid schema or a cross-repository conflict; `devgraph config show` prints the effective schema and where each entry comes from; `devgraph config eject` writes a commented starter file and never overwrites an existing one.
- `devgraph config disable <repo_id>` switches a repository's project config off: its `devgraph.schema.yaml` and `devgraph.tools.yaml` are ignored, so it gets the built-in schema and serves no project tools (`devgraph://project-tools` says so). `devgraph config enable <repo_id>` switches it back on. The switch is stored in the registry and shown in the `Project config` column of `devgraph list` (`on`/`off`); the schema change applies at the next rescan (watched repositories; otherwise `devgraph rescan <repo_id>`; `devgraph rescan <repo_id> --now` to apply immediately), while MCP sessions drop or regain project tools within 2 seconds. `config validate` still checks a disabled repository's files and reports it as disabled; `config show` says disabled and prints the built-in schema (`project_config: disabled` in JSON).
- `devgraph config schema list|add|edit|delete|reset` edits the repository's `devgraph.schema.yaml` from the command line. All take `--repo <path>` (default: the deepest registered repository containing the current directory). `list [--json]` shows the effective schema, each entry marked built-in or project. `add --from <file|->` adds one node type (a mapping with `label`) or relationship (a mapping with `type`) given as YAML or JSON; an existing label is refused (use `edit`). `edit <name> [--from <file|->]` replaces one entry by label or relationship type, opening it in `$EDITOR` without `--from`; an unchanged or invalid result writes nothing. `delete <name>` removes one entry and `reset [--yes]` deletes the file, returning the repository to the built-in schema. A name that is both a node type and a relationship type needs `--node-type` or `--relationship`; a relationship type declared more than once cannot be addressed by name (edit the file by hand). Every write is validated exactly as the indexer would validate it, is atomic, and splices only that entry's lines so comments elsewhere survive; the CLI never stages or commits. Edits apply like any other schema change, and the command says when: for a watched, enabled repository about 5 minutes after the last edit while the DevGraph agent (tray or headless) is running (the quiet period), or now with `devgraph rescan <repo_id> --now`; for an unwatched one only by running that rescan; not while the project config is disabled; an unregistered directory is not indexed. Applying a schema deletes nodes, so `delete`, `reset` and `edit` warn when they remove a node type, remove a user relationship type (built-in relationship types are never deleted), drop a type's `source`, or change its filesystem `kind`; for a disabled or unregistered repository the warning says "applying this schema" instead of "the next rescan". `add` and `edit` also note when a node type has no `source`: only `source: {provider: filesystem}` types are populated today, so no provider produces its nodes yet. Changing a type's `key` warns that the existing uniqueness constraint keeps the old key until the schema is applied and every repository declaring the label uses the new key. After a write the CLI also checks the other registered repositories and warns (without refusing) if this repository's label now conflicts with another's key. Validation failures from `add` and `edit` are prefixed "the new entry is invalid:", and `list` shows each node type's source (`filesystem (folder)` or `—`; `source` in JSON).
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

- **Opt-in per repository.** Project tools are off until you trust them for that repository: `devgraph config tools trust [<repo_id or path>]` shows each tool's name, description, parameters and Cypher (and any global tool it would override in that repository), then the SHA-256 of the file, and asks you to confirm at a terminal. The approval is stored in the registry database and pinned to the file's exact bytes, so any change to `devgraph.tools.yaml` (an edit, a `git pull`, a `config tools` or dashboard write) stops its tools being served until you trust it again. For CI, `--sha256 <hex>` approves without asking, only when it matches the file as it is now; without a terminal and without `--sha256`, `trust` refuses, since approval needs the user. `devgraph config tools untrust` revokes it. Until trusted, `devgraph doctor` and `config tools list` say `project tools not trusted (run devgraph config tools trust <repo_id>)`, `devgraph://project-tools` and the responses of a global tool standing in tell the model to ask the user to review the tools and run that command in a terminal, and global tools of the same names are served as normal. If the trust record can't be read (for example the registry database is locked or damaged), no project tools are served.
- **A repository cannot approve itself.** The trust record lives outside the repository: settings never come from a `.env` in the working directory (see [Configuration](#configuration)), a registry database that lies inside the session's repository is never used for trust (its project tools are not served) and a global tools store there serves no global tools, a `DEVGRAPH_REGISTRY_DB_PATH` that is not absolute is ignored (with a warning) in favour of `~/.devgraph/registry.sqlite3`, and registry rows whose stored path is not absolute are ignored, both for trust and for choosing the session's repository. A `devgraph.tools.yaml` that resolves outside its repository (a symlink out) is treated as unreadable: never served, trusted or shown. Names, descriptions, Cypher and parameter fields may not contain control characters (other than newline and tab), Unicode format characters such as bidi overrides, lone surrogates, or private-use or unassigned code points, so what the trust prompt shows is what runs. The tools file, schema file and global store are read only when they are regular files (never a FIFO or device, such as a symlink to `/dev/zero`) of at most 1 MiB; anything else is unreadable.
- **An enabled tool can read the whole graph.** Trust a tools file as you would trust code: once enabled, its queries run against the whole graph, every registered repository's data included, not only this one's. See the scope caveat below.
- **Serving.** The MCP server exposes a trusted repository's tools to clients, one MCP tool per entry. The session's repository is `DEVGRAPH_MCP_REPO` (a repo id, or an absolute path inside a registered repository), else the server's working directory if it is inside a registered repository (the deepest match wins), else none, in which case no project tools are served. A `DEVGRAPH_MCP_REPO` value that matches no registered repository also means no scope. To pin a project in Claude Code, run this in the repository: `claude mcp add devgraph -e DEVGRAPH_MCP_REPO=<repo_id> -- "<venv python>" -P -m devgraph.mcp.server`.
- **Calls.** DevGraph injects the session's `repo_id` into every call and runs the query in a read-only transaction with the tool's `timeout_s` and `max_rows`. Results use the same envelope as the built-in tools, `{count, results, truncated}`. Timeouts, write attempts and Neo4j errors come back as short, curated error messages. Built-in tool names are never taken over, a tool that fails registration is skipped without affecting the others, and an invalid file serves no project tools. The `devgraph://project-tools` resource lists what the session serves, and `devgraph://tool-catalog` includes them.
- **Hot reload.** The server checks the file every 2 seconds and serves the new set without a restart, telling the client its tool list changed (clients that support `tools/list_changed` re-list automatically). A save that changes the file's bytes is untrusted, so it serves no project tools until re-trusted (the last good tools are not kept for it). Only trusted bytes that fail to parse (including YAML that fails to load, such as an impossible date or runaway nesting) keep the last good tools, with a notice in `devgraph://project-tools`; a file that is invalid when the session starts serves no project tools until fixed. A file that can't be read (no permission, a directory, a symlink out of the repository) can't be checked against its approval, so it serves no project tools, and revoking trust always stops serving within 2 seconds. Both files are parsed before any served tool is removed, and a reload that fails for any other reason keeps the tools already served and is retried.
- **Scope caveat.** `$repo_id` is only required to be referenced: the rule is a convention, not a sandbox. Pinning a session controls which tools it gets, not which data a trusted tool's query reads, so a query that ignores `$repo_id` in its patterns (`WHERE $repo_id IS NOT NULL` passes) can read other repositories. That is why project tools need `devgraph config tools trust`; global tools are your own store and need no approval.
- Each query must be read-only (no `CREATE`, `INSERT`, `MERGE`, `SET`, `DELETE`, `DETACH`, `REMOVE`, `DROP`, `FOREACH`, `LOAD CSV`, `CALL`, `USE`, `SHOW`, `TERMINATE`, `ALTER`, `GRANT`, `DENY`, `REVOKE` or `RENAME`, and no `apoc` reference) and must reference `$repo_id`, which DevGraph injects. That check only confirms the query references `$repo_id`; see the scope caveat above. Parameter names may not be Python keywords (`from`, `in`, `class`, ...) or start with `model_`. Every other `$name` it uses must be a declared parameter (`string`, `integer`, `float` or `boolean`), and every declared parameter must be used. `max_rows` is 1-1000 (default 100) and `timeout_s` is 1-60 (default 10).
- An invalid file is rejected as a whole. `devgraph config validate` (or `--all`) exits non-zero on it, `devgraph config show` prints the declared tools and fails on an invalid file, and `devgraph doctor` reports each repository's tools as absent, valid or invalid.
- A tool named like one of DevGraph's built-in tools is reported as a warning, not an error; the built-in always wins.

### Global tools

Tools you want in every repository live in a global store, `global-tools.json`, in your DevGraph directory (next to the registry database, never the install location). It uses the same tool format and validation as `devgraph.tools.yaml` and is written atomically; manage it with `devgraph config tools ... --global` rather than by hand.

- **Scoped sessions only.** A Cypher tool needs a repository to inject as `$repo_id`, so global tools are served only in an MCP session scoped to a repository; an unscoped session serves none (`devgraph://project-tools` says so). `devgraph config disable` hides a repository's project tools, not global ones.
- **Precedence.** Built-in tools always win, then a project tool, then a global tool of the same name. When a project tool replaces a global one its responses carry the notice `resolved: project override of global tool '<name>'`; when a global tool is served in place of a project tool that failed to register (or whose file is invalid at startup), the response carries `used global tool '<name>': <reason>`. A project or global tool named like a built-in is ignored, and the built-in's responses carry `ignored: project tool '<name>' shadows a locked tool; using the fixed implementation` (or `global tool`) in `notices` for as long as the declaration stays (built-ins that return a list can't carry it; `devgraph://project-tools`, `config validate`, `config tools list` and `doctor` report it with the same wording). `devgraph://project-tools` reports `global_tools_file` and each served tool's `origins` (`global`, `project`, `project (overrides global)`). Edits to either file reload within 2 seconds; an invalid global file keeps the last good global tools.

### Managing tools: `devgraph config tools`

Every subcommand takes `--repo <path>` or `--global`, and validates the whole resulting file before writing; nothing invalid is written. Without either, the scope is the deepest registered repository containing the current directory (the same rule an MCP session uses), so running it from a subdirectory edits the repository root's `devgraph.tools.yaml`; outside any registered repository it uses the current directory and warns that MCP sessions won't serve its tools. `config show` and `config validate` default the same way. After a write the CLI says whether running sessions will pick it up: for an enabled registered repository, that the changed file is not served until `devgraph config tools trust <repo_id>` (then within 2 seconds); never for an unregistered or disabled one; and for `--global` only in sessions scoped to a registered repository.

- `list [--json]` shows the tools in effect: built-in (locked; `run_cypher` only when `enable_run_cypher` is on), global, and project, with overrides marked (`--global` lists only the store), and the repository's trust state (`trust` in JSON: `trusted`, `untrusted`, `changed`, `error`, or null without a file). A global or project tool with a built-in name is listed as `ignored: shadows a locked tool`.
- `trust [<repo>] [--sha256 <hex>]` and `untrust [<repo>]` approve and revoke a repository's project tools (see **Opt-in per repository** above). `<repo>` is a repo id or a path inside a registered repository (default: the one containing the current directory). `trust` refuses a missing or invalid file, and one that resolves outside the repository.
- `add --from <file|->` adds one tool from a YAML or JSON mapping (or stdin). It fails if the name exists in that scope (use `edit`) or is a built-in name.
- `edit <name> [--from <file|->]` replaces one tool, opening it in `$EDITOR` without `--from`. An unchanged or invalid result writes nothing.
- `delete <name>` removes one tool (unknown names exit 1).
- `reset [--yes]` removes every tool in the scope (deletes `devgraph.tools.yaml`, or empties the global store) and asks for confirmation unless `--yes`.

`config tools` and `config schema` writes refuse a symlinked `devgraph.tools.yaml`, `devgraph.schema.yaml` or global store (the atomic replace would swap the link for a plain file): the error names the link's target; edit that file directly.

A tools file that declares the same tool name twice is refused by every `config tools` write (`edit` and `delete` cannot tell which entry you mean), so fix the duplicate by hand in the file.

`devgraph.tools.yaml` is edited as text, splicing only the affected tool's lines, so comments and formatting elsewhere survive (comments inside an edited tool are lost). Both files are replaced atomically, but there is no locking: two `config tools` writes to the same file at once are last-writer-wins, so one of the edits can be lost. A file whose `tools` value is not a block sequence (for example a flow list) is refused rather than rewritten. The CLI never stages or commits. `config show`, `config validate` and `doctor` also report the global store and which project tools override global ones.

## Configuration

Settings come from `DEVGRAPH_*` environment variables (see `.env.example`). A `.env` file is read only from two fixed places, never from the directory a command starts in:

1. `~/.devgraph/.env` (or the directory of `DEVGRAPH_REGISTRY_DB_PATH`, when that is exported as an absolute or `~`-prefixed path; a relative value is ignored here with a warning), which takes precedence;
2. the root of the DevGraph checkout, when DevGraph runs from a source checkout.

Exported environment variables override both files.

**Migrating:** earlier versions also read `.env` from the current working directory. If you kept a `.env` anywhere other than the two places above, move it to `~/.devgraph/.env` or export the variables instead.

**Migrating:** DevGraph now runs its modules with `python -P`, so the working directory is not on the module path and a `devgraph/` folder in a repository can't stand in for DevGraph. An MCP client registered with `-m devgraph.mcp.server` should be re-registered with `-P -m devgraph.mcp.server` (see `devgraph client-config` and [DEVGRAPH-CLIENT.md](DEVGRAPH-CLIENT.md)).

## Dashboard

The tray app serves the dashboard at `http://127.0.0.1:8765`. It shows registered repositories, graph and git information, query telemetry, an interactive graph canvas, query-driven highlighting, and saved per-repository layouts. The repository picker can register a local path and run its initial scan; if indexing fails, the registration remains available for retry. Server-Sent Events refresh the view after indexing changes.

The service binds to loopback and has no authentication because it is intended as a single-user local tool. The browser never receives Neo4j credentials; graph queries run through the FastAPI backend. Use `DEVGRAPH_DASHBOARD_ENABLED=false` to disable it or `DEVGRAPH_DASHBOARD_PORT` to choose another port.

Because there is no authentication, the dashboard answers only requests addressed to the local machine: the `Host` header must name `127.0.0.1`, `localhost`, `[::1]`, or the configured `DEVGRAPH_DASHBOARD_HOST`; anything else gets `403 host not allowed`. This stops a web page from reaching the dashboard through DNS rebinding (re-pointing its own domain at 127.0.0.1). A wildcard bind (`0.0.0.0` or `::`) does not widen this list; to reach the dashboard by a LAN address, set `DEVGRAPH_DASHBOARD_HOST` to that address rather than a wildcard. Registering a repository, saving a layout, running console Cypher, and Config page writes additionally refuse cross-origin browser requests. The tray menu and `devgraph dashboard` link a wildcard bind to its loopback address (`http://127.0.0.1:<port>`, or `http://[::1]:<port>` for `::`, which binds IPv6-only).

### Config page

Settings > Config shows the configuration the MCP tool plane and the indexer actually use: the global scope first (built-in node types, relationship types and tools, locked, plus the global tools, flagged GLOBAL), then one block per active registered repository (project tools, node types and relationships from `devgraph.tools.yaml` and `devgraph.schema.yaml`). Tools show their display id (`gl_<name>` for global, `<repo_id>_<name>` for project, the bare name for built-ins); MCP telemetry records the same ids for each tool call, with the wire name and an origin (`builtin`, `global`, `project`; a project tool's id names its session's repository); it never records arguments, query text or results.

Badges use the same resolution rules as an MCP session, with the detail on hover:

| Badge | MCP behaviour |
|---|---|
| Ignored: shadows a locked tool | a project or global tool named like a built-in is not served; the built-in runs |
| Overrides global tool | the project tool is served instead of the global one |
| Overridden in `<repos>` | a global tool that some repositories replace |
| Not served: using the global tool | the project tool can't be served (its file is invalid, or registering it failed), so the global tool of the same name answers |
| `<file>` is invalid (file-invalid) | the tools file does not validate: an MCP session that already loaded it keeps its last good tools only while the file's bytes are trusted and readable; a new session serves none of that file's tools |
| Not served | registering the tool on the MCP server failed (rare: e.g. a parameter schema the SDK can't build) and no global tool of that name stands in. The page resolves tools without a live MCP server, which can't fail this way, so the page doesn't show this badge today; the failure appears in the session's `devgraph://project-tools` notices |
| Not served / Project config disabled | project config is switched off for the repository |
| Trusted / Not trusted / Changed since trusted / Trust state unreadable | whether `devgraph.tools.yaml` matches the SHA-256 approved with `devgraph config tools trust`; only Trusted serves the file's tools. A tool of an untrusted file shows "Not served: not trusted", or "Not served: using the global tool" when a global tool of its name answers |
| Key conflict with `<repos>` (schema-conflict) | another registered repository, active or not, declares the same node type label with a different key (a repository with project config off is marked `(disabled)`: its constraint stays in the database until its next rescan); the hover text is what `devgraph doctor` and `config validate --all` report |
| Schema change pending / never applied / invalid | the schema file differs from the applied one, was never scanned, or does not validate |
| Graph unavailable | Neo4j could not be reached, so whether the schema file is applied is unknown; badges that depend only on the file still show |

Each tool, node type and relationship can be added, edited or deleted from a YAML editor modal that holds the same text `devgraph config tools edit` and `devgraph config schema edit` show. Editing or deleting a global entry first shows a warning step. A global tool can be saved to a repository (destination dropdown), which writes a project override. Every save runs a dry run first; it asks for a confirmation only when the dry run returns warnings (schema changes that delete nodes, change a key, drop a source, or introduce a key conflict with another repository) or when saving a global tool to a repository would replace that repository's own tool of the same name. Otherwise the save goes straight through. The text and destination are locked while the dry run is in flight, and a confirmation covers only the text it checked: text changed afterwards is checked again before anything is written. Writes use the CLI's validate-then-atomic-write code, replace only the affected entry so comments elsewhere survive, and write the file only: **the dashboard never stages or commits**, so `git status` shows the change and you commit it yourself. Each write carries the fingerprint of the file you saw (`If-Match`); if the file changed since, the save is refused with 412 and the editor offers to reload.

The API behind it is `GET /api/config` and `GET /api/config/{scope}` (scope is `__global__` or a repo id), plus `POST`/`PUT`/`DELETE` on `/api/config/{scope}/tools[/{name}]` and `/api/config/{scope}/schema/{section}[/{name}]`. Writes are refused when `Origin`/`Sec-Fetch-Site` mark the request cross-site or cross-origin (403), on top of the `Host` check above. There is no authentication: if you set `DEVGRAPH_DASHBOARD_HOST` to a LAN address, anyone on that network can use these routes to write tool Cypher into your registered repositories and the global tools store.

Each section also has a **Reset** button that empties a whole file: `devgraph.tools.yaml` or `devgraph.schema.yaml` of a repository, or the global tools store. It lists what would be removed first (a dry run), then asks you to type the repository id (`global` for the global store) before the button arms. The reset is bound to the fingerprint of the dry run, so a file that changed after it was listed is refused with 412 and the dialog offers to re-check. Resetting a project file **deletes** `devgraph.tools.yaml` or `devgraph.schema.yaml` (`git status` shows it as deleted; it is never staged or committed), and resetting the global store **empties** it (the file stays, with no tools). Git can restore a tracked file; an untracked file or the global store cannot be restored. The routes are `POST /api/config/{scope}/reset/tools` and `POST /api/config/{repo_id}/reset/schema` (body `{"dry_run": true|false}`, `If-Match` required). The CLI's `config ... reset` refuses a symlinked file.

Trusting a repository's project tools is CLI-only: the page shows the trust badge on each repository's Tools heading and a **Revoke trust** button while an approval is recorded (`DELETE /api/config/{repo_id}/trust/tools`; there is no route that grants trust). Every project-tools write from the page (add, edit, delete, reset, Copy to...) changes the file, so its dry run and result say "Saving stops <repo>'s project tools being served until you run `devgraph config tools trust <repo>`". Copying a tool from a repository whose tools file is not trusted to the global store warns that the global store serves it in every repository without any trust approval, and that an enabled tool can read the whole graph.

Each repository card has a **Project config** switch, the dashboard form of `devgraph config enable|disable`: switching it off serves no project tools and gives the repository the built-in schema at the next rescan (a dry run shows any warnings first). It is `PUT /api/config/{repo_id}/project-config` with `{"enabled": true|false}` (naming the end state, so no `If-Match`).

Project node types, relationships and tools also have a **Copy to...** button: pick another active repository (a project tool can also go to the global store) and the page runs the same dry run and write as an add, with the destination locked while a check is in flight; changing it clears the confirmation. If the destination already has an entry of that name, the dialog warns "Replaces <destination>'s own <name>." and needs a second click to replace it. Copying a tool to the global store says whether it adds or replaces the global tool, that the source repository's own copy keeps overriding it there, and which other repositories have their own tool of that name. A node type saved or copied with a key that differs from another repository's warns "Creates a schema conflict", or "Joins an existing schema conflict" when it matches one side of a conflict that already exists. It is built on the existing add and replace routes, so it writes the destination file only and never stages or commits. The write carries the destination's `If-Match` fingerprint; a destination that changed meanwhile is refused with 412. Copying a relationship whose endpoint types the destination lacks is refused, and an identical relationship already there is reported rather than duplicated.

The prototype "MCP tools" pane is gone: the Global card now shows `run_cypher` read-only (off, with the `DEVGRAPH_ENABLE_RUN_CYPHER=true` setting that turns it on, or a "Raw Cypher enabled" badge when it is on). It reflects the dashboard process's environment: each MCP session reads the setting from the environment it was started with.

Tools (global and project) and project node types open in a **Form** view when you add or edit them, with a **Form / YAML** switch: labelled fields, a parameter list for tools, and metadata fields for node types. The form does not write anything itself: it serialises the entry into the YAML textarea, and Save then runs the same dry run, the same warning confirmation and the same text-bound confirmation as the YAML editor. Relationships, delete and Copy to... stay YAML-only. An entry the form cannot show exactly opens in YAML with the reason: unknown fields, line breaks in a single-line field, a carriage return in a text area, a node type whose `key` order differs from its metadata order, or a value YAML would read back differently (cycles, whole-number floats, dates, very large integers). Once you hand-edit the YAML the Form button is disabled with a reason, and **Discard YAML edits** returns to the form's last state. Typed defaults and Max rows/Timeout keep exactly what you type. Not in this page yet: a relationship form, and loading hand-edited YAML back into the form.

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
