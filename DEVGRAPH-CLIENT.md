# Using DevGraph from another repository

This file is also served live, once a client has connected to DevGraph's
MCP server, as the `devgraph://client-guide` resource (and a machine-
readable per-tool summary is separately available at
`devgraph://tool-catalog`) — an already-connected client can
`list_resources()`/`read_resource()` either one directly instead of relying
on this file having been copied anywhere. This file still exists for the
one thing a resource read can't do on its own: getting a *new* client
connected in the first place (steps 1-3 below happen before any MCP call
is possible), and as a copy-paste fallback for MCP clients that don't
support resources. It's meant to be copied into (or pasted/summarized into)
another repo's `CLAUDE.md`/`AGENTS.md`, or handed directly to a Claude Code
instance working there, so it knows how to register that repo with
DevGraph and use DevGraph's MCP tools as part of its normal workflow —
instead of re-reading the whole repo from scratch on every architecture/
dependency question.

DevGraph itself lives wherever it's checked out on this machine — run
`devgraph client-config` from that repo to get the exact paths/commands for
this checkout; do not hardcode a path here, DevGraph's install location can
differ machine to machine.

Everything below assumes that repo's Python environment and Neo4j container already exist
(they're a one-time setup — see that repo's own `README.md` if they don't
yet; the interactive installer is the normal setup path). This file does
not duplicate DevGraph's own internals; it only covers what a client repo
needs to know to use it.

---

## What this gets you

DevGraph builds a queryable graph of a repo's structure — modules, classes,
functions, containers, API endpoints, datastores, design decisions,
requirements, mentions, and git history — for **Python, JavaScript/
TypeScript, C#, C++, Java, Kotlin, Rust, and Go source**, detected per file by
extension (a repo doesn't need to be single-language; each file is routed
to the matching extractor automatically) — and exposes it through purpose-built MCP tools
(`search_component`, `find_callers`, `impact_analysis`,
`impact_analysis_for_diff`, `explain_architecture`, `blame_component`,
`find_requirements_for`, `find_mentions`, `list_recent_changes`, `god_nodes`,
`get_source`, and others), plus the opt-in `run_cypher` escape hatch. Read
`devgraph://tool-catalog` for the live tool list and identifier expectations.
Once a repo is registered and indexed, an AI assistant can answer questions
like "what depends on this module" or "why was this decision made" by
querying the graph directly, instead of grepping/reading the whole tree
every time.

**It does not replace normal file reading.** Use DevGraph for
structural/relationship/history questions where a pre-built graph is faster
and more accurate than re-scanning; use normal Read/Grep for anything about
current file content, correctness, or making actual edits.

## 0. One-time: confirm the DevGraph Neo4j container is running

DevGraph's graph lives in an isolated Podman container, not in this repo.
If DevGraph was installed via its interactive setup (`.\scripts\install.ps1`
or `.\scripts\setup-menu.ps1`), the container is already up — that flow
brings up Neo4j, initializes its schema, and runs a health check as part of
install, so this step is normally already satisfied by the time a client
repo needs it.

To confirm, from any shell:

```bash
podman ps --filter name=devgraph-neo4j
```

If it's not running, the quickest fix is re-running `.\scripts\setup-menu.ps1`
from the DevGraph repo (idempotent — safe to re-run, brings the container
back up without re-registering anything that's already registered). Falling
back to a manual `podman run` (see DevGraph's own `README.md`) still works
too, if you'd rather not run the interactive menu again — do this from the
DevGraph repo, not from here. **Never** create or touch a container with a
different name prefix; if you see other `podman ps` entries, they belong to
unrelated projects and are out of scope.

## 1. Register (and automatically index) this repo with DevGraph

Run once per machine (DevGraph's registry is local to the machine, not per
client-repo). Run `devgraph client-config` from the DevGraph repo on this
machine to get the exact resolved venv-python path for the commands below —
do not hardcode a path here, DevGraph's install location can differ machine
to machine:

```bash
"<DevGraph repo's resolved venv python, from devgraph client-config>" -P -m devgraph.cli.main register "<absolute path to this repo>"
```

Keep the `-P` flag in every command here: it stops Python from putting the
current directory on its module path, so a `devgraph/` folder inside this
repo cannot be imported in place of DevGraph itself.

Add `--full` to also index git commit history in the same step (equivalent
to a separate `index-history` call):

```bash
"<venv python>" -P -m devgraph.cli.main register "<absolute path to this repo>" --full
```

This registers the repo **and runs a full initial scan** — Python source,
container/compose files, API routes, datastore usage all get indexed in one
pass via `devgraph/indexer/dispatch.py`. It prints the `repo_id` assigned
(defaults to the folder name, deduped if already taken) and how many files
were indexed. **Record that `repo_id`**: you'll need it to pin the MCP
session to this repository (`DEVGRAPH_MCP_REPO`, below) or to pass it
explicitly to a tool. If Neo4j isn't reachable at registration time, `register` still succeeds
(registration and indexing are decoupled) and tells you to run
`devgraph rescan <repo_id>` once it's up.

Confirm registration:

```bash
"<venv python>" -P -m devgraph.cli.main list
```

Only paths registered this way are ever watched or indexed by DevGraph —
there is no automatic or recursive discovery. Registering this repo does not
expose it to any other repo's queries unless someone explicitly passes
`cross_repo: true` on a tool call.

## 2. Re-index after changes, and index optional extras

`devgraph rescan <repo_id>` re-runs the same full scan as `register` — idempotent
(`MERGE`-based), safe to run repeatedly; existing nodes update in place
rather than duplicating. Use it any time you want the graph refreshed after
a batch of changes:

```bash
"<venv python>" -P -m devgraph.cli.main rescan <repo_id>
```

**Git history** (separate command — not part of the file-scan above;
incremental, only walks new commits since the last run — or fold it into
`register`/`rescan` with `--full` instead of calling this separately):

```bash
"<venv python>" -P -m devgraph.cli.main index-history <repo_id>
```

**Docs / design decisions** (optional — only if this repo has Markdown notes
with `type: requirement|design_decision|architecture_note` front-matter;
`rescan` picks these up automatically too, once `--docs-path` is set):

```bash
"<venv python>" -P -m devgraph.cli.main annotate <repo_id> --docs-path <repo-relative docs folder>
"<venv python>" -P -m devgraph.cli.main annotate <repo_id> --note <repo-relative note file>   # index one note immediately
```

**PR/issue history** is opt-in and talks to an external service (GitHub,
etc.) — do not enable it without the repo owner's explicit go-ahead:

```bash
"<venv python>" -P -m devgraph.cli.main pr-source enable <repo_id>
"<venv python>" -P -m devgraph.cli.main issue-source enable <repo_id>
```

Enabling the flags above doesn't fetch anything by itself yet — actually
pulling PR/issue data currently requires a short Python script calling
`devgraph.indexer.pr_issues.extractor.index_pr_issues` with a configured
`GitHubSource`; there's no CLI command that performs the fetch yet.

## 3. Connect DevGraph as an MCP server

If DevGraph was installed via its interactive setup
(`.\scripts\install.ps1` / `.\scripts\setup-menu.ps1`), this step is likely
already done — that flow detects Claude Code and VS Code on the machine and
registers DevGraph with whichever ones you selected. Re-running it is safe
(idempotent) if you're not sure, or if you want to register a client that
wasn't picked the first time.

To register (or re-register) manually, run `devgraph client-config` from the
DevGraph repo on this machine to get the exact paths/commands for this
checkout — do not hardcode a path here, DevGraph's install location can
differ machine to machine. It supports `--target claude|vscode|both`
(default `both`). Its default output is a ready-to-paste block shaped like
this (example only — always use the actual output from running the command,
not this literal text):

```
## Connect DevGraph as an MCP server

- **command**: <resolved path to DevGraph's venv python.exe>
- **args**: -P -m devgraph.mcp.server
- **cwd**: <resolved DevGraph repo root>

claude mcp add devgraph -- "<resolved venv python.exe>" -P -m devgraph.mcp.server

VS Code (user mcp.json at <resolved path>):
{ "servers": { "devgraph": { "type": "stdio", "command": "...", "args": [...], "cwd": "..." } } }
```

A Claude Code registration made before `-P` was added is left as-is by
`--run` (an existing entry is skipped); run `claude mcp remove devgraph` and
register again to pick it up. The VS Code entry is rewritten in place.

`devgraph client-config --claude-mcp-add-only` prints just the Claude Code
one-liner; `devgraph client-config --run` also executes registration for the
selected `--target`(s) — for Claude Code via the `claude` CLI if one is on
PATH, for VS Code by merging the entry into VS Code's user-level `mcp.json`
(opt-in, print-only is the default). Both are safe to re-run: an
already-registered Claude Code entry is detected and skipped rather than
erroring, and the VS Code entry is an upsert that never touches other
servers already in that file. Exact `claude mcp add` flags depend on the
Claude Code version in use — check `claude mcp add --help` if the printed
command doesn't match; the important part is the command/args/cwd above, not
the specific CLI invocation.

Once connected, the registered tools are scoped by a `repo_id` argument.
Read `devgraph://tool-catalog` instead of relying on a copied tool list.

**Repository scope.** Each MCP session has a repository: `DEVGRAPH_MCP_REPO`
if set, otherwise the repository containing the server's working directory.
The server instructions name it, and so does `devgraph://project-tools`.

- You can omit `repo_id` from a built-in tool call to use
  this session's repository.
- When you omit it, the response carries `repo_id` and a notice saying which
  repository was used. Check them before relying on the answer.
- `find_requirements_for` and `blame_component` return lists, which carry no notice.
  For those, the repository is the one named in the server instructions and
  in `devgraph://project-tools`.
- Pass `repo_id` explicitly when you want a different repository, or when
  working across several repositories. An explicit `repo_id` is used exactly
  as given.
- If the session has no repository, a call without `repo_id` fails. The
  error lists the active registered repositories; pass one of them as `repo_id`.
  A repository registered after the server started is only picked up once
  the MCP server restarts.

Never pass `cross_repo: true` unless the user explicitly asks for a
cross-repository answer — the default is (and must stay) scoped to one
repository only.

**Identifier-type nuance**: `find_callers`, `impact_analysis`, and
`find_related_files` match against function/class **names** only (a file
path like `shared/utils/x.py` returns empty; the function name defined in
that file, e.g. `batch_retrieve_payloads`, returns real results).
`blame_component` is the opposite — it wants a **file path**, not a function
name. `get_source` also takes a function/class **name**. Passing the wrong
kind of identifier looks like an empty/broken result but is a usage
mismatch, not a graph gap.

**Response shape (`count`/`results`/`truncated`)**: `search_component`,
`find_callers`, `list_services`, `find_related_prs`, `issue_history_for`
return `{"count": N, "results": [...], "truncated": bool}` instead of a bare
list — read the match set from `result["results"]`, and check `truncated`
before assuming you've seen everything. `find_related_files` and
`impact_analysis` apply the same envelope to each of their list-valued
fields individually (e.g. `impact["direct_dependents"]["results"]`). Pass
`max_results` (default 15) to widen the sample when you genuinely need more
than the default. `search_component`'s `count` maxes out at 50 (its own
Cypher cap) even if more matches exist beyond that.

**Project tools**: if this repo has a `devgraph.tools.yaml`, the MCP session
also serves its tools for the session's repository (`DEVGRAPH_MCP_REPO`, else
the server's working directory). Read `devgraph://project-tools` for what is
served. The server checks the file every 2 seconds and serves the new set
without a restart, telling the client its tool list changed (clients that
support `tools/list_changed` re-list automatically). Any edit changes the file's
bytes, so it stops serving project tools until re-trusted; only trusted bytes that
fail to parse keep the last good tools, with a notice (see `devgraph://project-tools`). Pin a project with
`claude mcp add devgraph -e DEVGRAPH_MCP_REPO=<repo_id> -- "<venv python>" -P -m devgraph.mcp.server`.
The pin also sets the repository built-in tools use when `repo_id` is omitted.
A path value must be absolute (a relative value is read as a repo id), and a value
that matches no active registered repository serves nothing. A repository whose project config is disabled (`devgraph config disable`) serves no project tools; `devgraph://project-tools` says so. If `devgraph` is already
registered in that project, run `claude mcp remove devgraph` first.

**Global tools**: tools defined once with `devgraph config tools ... --global`
are also served in every session scoped to a repository (an unscoped session
serves none). Built-in tools win, then project tools, then global tools. Two
notices can appear in a tool's response envelope: `resolved: project override
of global tool '<name>'` (the project's tool replaced a global one) and `used
global tool '<name>': <reason>` (the global tool is served because the project
tool of that name could not be). A built-in tool whose name a project or global
tool also uses says so in a `notices` list in each response:
`ignored: project tool '<name>' shadows a locked tool; using the fixed
implementation` (or `global tool`). Built-ins that return a list
(`find_requirements_for`, `blame_component`, `run_cypher`) can't carry it;
`devgraph://project-tools` reports it for them. `devgraph://project-tools` lists each served
tool's origin and the global tools file.

`run_cypher` will not appear unless DevGraph's own config has
`enable_run_cypher=true` set. If it's missing and you need something the
purpose-built tools genuinely can't express, that's a signal a new high-level
tool should be added to DevGraph — not that raw Cypher should be turned on
as a workaround.

## 4. Using the tools day-to-day

Prefer these over re-reading files when the question is structural:

| Question shape | Tool |
|---|---|
| "What is X / where is it?" | `search_component` |
| "What is X, but only recently modified?" | `search_component` with `modified_within_commits=N` (only matches entities touched in the last N commits) |
| "What calls X?" | `find_callers` (name, not path) |
| "What calls X, but only recently modified?" | `find_callers` with `modified_within_commits=N` (filters results to entities touched in the last N commits) |
| "What calls X, but only from within class Y?" | `find_callers` with `scope_to_class=Y` (cuts noise from unrelated same-named methods elsewhere in the repo) |
| "What breaks if I change X?" | `impact_analysis` (name, not path) |
| "What breaks across this whole PR/diff?" | `impact_analysis_for_diff` (base_ref/head_ref, both must exist locally — never fetches) |
| "What does X depend on?" | `get_service_dependencies` (service name), `find_related_files` (function/class name, not path) |
| "Trace a request from this endpoint through services/datastores" | `trace_request_flow` (endpoint name) |
| "What changed recently?" | `list_recent_changes` (lists entities touched in the last N commits, most-recent first; filters by optional entity type) |
| "What's the overall architecture?" | `explain_architecture`, `summarise_repository` |
| "What changed between two branches?" | `compare_branches` — **stub**: registered and callable, but not yet fully wired to git metadata; treat results as unreliable until DevGraph's own docs say otherwise |
| "Why was X built this way?" | `explain_decision`, `trace_design_rationale` |
| "What requirements does X satisfy?" | `find_requirements_for` |
| "Which docs mention X?" | `find_mentions` (only useful if Markdown mentions indexing was enabled) |
| "Who changed X and when?" | `blame_component` (file path, not name) |
| "What PRs/issues touched X?" | `find_related_prs`, `issue_history_for` (only useful if PR/issue ingestion was enabled in step 2) |
| "Show me X's actual code" | `get_source` (name, not path — returns source text + full docstring; reads live from disk using the last-indexed line range, so rescan first if the file may have changed) |

If a tool returns empty/sparse results, check whether the repo has actually
been scanned (step 1/2) before concluding the graph has nothing to say — an
unindexed repo will legitimately return empty results, that's not a tool
failure.

## Recency filtering and git-derived properties

When you index a repo with `--full` (step 1), or run `index-history` separately (step 2), DevGraph stages git-derived recency properties on nodes:

- **`Module` nodes** get `created_at` (earliest commit touching that file) and `last_modified_at` (most recent commit touching it). Both are aggregated from `Commit` nodes linked via `MODIFIES` edges.
- **`Function` and `Class` nodes** get `last_modified_at` only — the commit whose changes most recently touched that function/class's source line range (via `git blame`). No `created_at` at this granularity — it would require `git log -L`, which is too fragile for practical use in an always-on indexer.
- **Author tracking** (`last_modified_by`: the name of the author who last modified that entity) is opt-in and off by default. Enable it by setting `DEVGRAPH_GIT_RECENCY_TRACK_AUTHOR=true` in DevGraph's own environment, or via DevGraph's config.

**Automatic sync**: Once a repo is indexed via `--full` or `index-history` at least once, its history is **automatically kept in sync** whenever `.git` changes (branch switches, rebases, resets, new commits). No manual command needed — the background watcher detects `.git` state changes and reconciles the graph correctly, including deletion of orphaned `Commit` nodes when history is rewritten. Use `modified_within_commits=N` on `find_callers`, `search_component`, or `list_recent_changes` to query only entities touched in the last N commits.

Previously documented gaps here (`find_callers`/`impact_analysis` seeing no
call edges, `explain_architecture` returning no `uses`/`calls`, and relative
imports not resolving) have been fixed and checked against a reference repository.
Two things worth knowing about how they work:

- **Call graph is name-based, not type-resolved, in every language
  DevGraph supports.** `self.foo()`, `this->foo()`, `obj.Foo()`, and a bare
  `foo()` all link to whichever `Function` node is named `foo` — there's no
  type inference, so same-named methods on unrelated classes/types will
  over-link rather than under-link. Treat `find_callers` results as "things
  that call something named X", not a guaranteed-precise call graph. When a
  call was made from inside a method body, its `CALLS` edge records the
  caller's enclosing class as `caller_class` — pass
  `find_callers(..., scope_to_class="ThatClass")` to filter down to just
  that class's own callers when a common method name (`get`, `run`,
  `close`) is otherwise drowning in unrelated matches.
- **Import/include resolution is a same-repo best-effort guess, per
  language, and it fails silently by design.** Each language extractor
  resolves its own dominant intra-repo import convention (Python's dotted
  imports, JS/TS's relative paths and `node_modules`, C#'s namespace/folder
  convention, Java's package/folder convention, Rust's `mod`/`use`, Go's
  module-path convention) to a same-repo file guess. A wrong guess doesn't
  corrupt the graph — it simply never becomes an edge (`IMPORTS` edges only
  materialize when the guessed target actually exists as an indexed node) —
  but it does mean `find_related_files`'s `imported_modules` can legitimately
  under-report for any language, not just Python. **C++ is a special case:**
  `#include` resolution has no build-system truth to lean on (no compiler
  flags/include paths available to DevGraph), so only same-directory quoted
  includes (`#include "local.h"`) get a guess at all — angle-bracket
  includes (`#include <foo.h>`) are skipped entirely. Treat C++'s `IMPORTS`
  graph as thin by design, not as a sign indexing failed; its
  Module/Class/Function/CALLS/EXTENDS extraction is unaffected by this.
- **Service cross-linking depends on the compose file's `build`/`context`
  matching each service's actual source directory.** If a compose service
  has no `build:` key (image-only services like databases) or an
  unconventional build layout DevGraph's heuristic doesn't recognize, its
  `uses`/`calls` data in `explain_architecture` will be sparse — that's a
  real limit of directory-based inference, not a sign indexing failed.
- **`find_related_files`'s `imported_modules` can be sparse for repos that
  resolve local imports via manual `sys.path` manipulation** rather than
  dotted package imports (`from services.api.clients import X`) or standard
  relative imports (`from .clients import X`) — a known, bounded gap, not a
  sign indexing failed.

## 5. Keeping the graph current

`devgraph register`/`rescan` do a full scan. For ongoing changes, connecting an
MCP client (Claude Code, etc.) auto-starts the tray app (watcher +
incremental indexer, `devgraph.agent.TrayApp`) as a detached background
process if one isn't already running — no manual step needed in the common
case. It watches every registered repo and reindexes changed files as
they're saved, and **shuts back down automatically when the last connected
MCP client disconnects** — each MCP server process registers itself as a
"holder" of the shared tray process on connect and releases that hold on
clean shutdown, so with a single client, closing it stops indexing right
along with it; with multiple clients connected at once, the tray keeps
running until all of them have disconnected, not just the first one to go.
You can also manage it directly with `devgraph tray start`/`stop`/`status`
(e.g. to run it without any MCP client attached, or to force-stop it
immediately regardless of any still-connected clients) — the MCP server and
the CLI share the same PID-tracked process, so either one starting it is
enough. It is **not** registered to survive reboot/logout — after a reboot,
either reconnect an MCP client or run `devgraph tray start` once. If for any
reason it isn't running, nothing watches this repo live: re-run `devgraph
rescan <repo_id>` after a meaningful batch of changes, or check `devgraph
status`/`devgraph tray status` to see whether it's already running. A stale
graph gives stale answers, so don't rely on it for anything time-sensitive
without confirming liveness or refreshing first.

For diagnosis, use `devgraph status` for a quick connectivity and repository
check, `devgraph doctor` for environment drift, and `devgraph mcp doctor` for
Claude Code registration problems. `devgraph info <repo_id>` and
`devgraph stats [repo_id]` inspect indexed state; `devgraph logs` shows recent
tray/dashboard output.

## Non-negotiables when working with DevGraph from this repo

Carried over from DevGraph's own design constraints — these aren't
suggestions:

- Only ever register/index paths inside this repo. Never point DevGraph at
  a path outside it, and never suggest DevGraph "just scan" something wider.
- Never enable `cross_repo: true` without the user explicitly asking for a
  cross-repository answer.
- Never enable PR/issue ingestion (step 2) without the repo owner's explicit
  go-ahead — it's the one DevGraph feature that makes outbound network calls.
- Never touch DevGraph's own container (`devgraph-neo4j`) or its data beyond
  what's described here — it's shared infrastructure other repos may also be
  using.
