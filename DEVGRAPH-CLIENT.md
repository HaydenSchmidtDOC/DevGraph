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
(`search_component`, `describe_node`, `find_callers`, `impact_analysis`,
`impact_analysis_for_diff`, `compare_branches`, `explain_architecture`, `list_endpoints`, `blame_component`,
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
- **cwd**: the repository you are working in (VS Code: ${workspaceFolder})

claude mcp add -s user devgraph -- "<resolved venv python.exe>" -P -m devgraph.mcp.server

VS Code (user mcp.json at <resolved path>):
{ "servers": { "devgraph": { "type": "stdio", "command": "...", "args": [...], "cwd": "${workspaceFolder}" } } }
```

Claude Code registration is user-scoped (`-s user`), so one registration
serves every project; `devgraph mcp add`, `client-config --run`, `mcp doctor`
and `mcp remove` all use that scope. Older versions ran a bare `claude mcp
add`, which registers in local scope, for the DevGraph checkout folder only:
`devgraph mcp add` and `mcp doctor` detect such a registration and print how to
move it (`claude mcp remove devgraph -s local` in the checkout, then `devgraph
mcp add`). A user-scope registration made before `-P` was added is left as-is
by `--run` (an existing entry is skipped); run `devgraph mcp remove` and
register again to pick it up. A user-scope registration whose command is
another checkout's Python is reported by `devgraph mcp add` and `mcp doctor`,
and fails both when that Python no longer exists (a moved or deleted
checkout); `devgraph mcp add --replace` removes it and registers this
checkout (the old entry is added back if that fails); a command set up
outside DevGraph (`uvx`, `pipx`, a console script) is left alone. The VS
Code entry is rewritten in place; its `cwd` is `${workspaceFolder}`, so a VS
Code session answers for the open workspace. That variable names one folder:
in a multi-root workspace, or a window with no folder open, it doesn't name
the repository you mean (or anything), so set `DEVGRAPH_MCP_REPO` in the
entry's `env` there.

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
- Pass `repo_id` explicitly when you want a different repository, or when
  working across several repositories. An explicit `repo_id` is used exactly
  as given.
- If the session has no repository, a call without `repo_id` fails. The
  error lists the active registered repositories; pass one of them as `repo_id`.
  A repository registered after the server started is only picked up once
  the MCP server restarts.

Never pass `cross_repo: true` unless the user explicitly asks for a
cross-repository answer — the default is (and must stay) scoped to one
repository only. It is also refused (a ToolError naming
`DEVGRAPH_ALLOW_CROSS_REPO`) unless the user has turned that setting on; when
it is on, results cover registered repositories only.

**Identifier-type nuance**: `find_callers`, `impact_analysis`, and
`find_related_files` match against function/class **names** only (a file
path like `shared/utils/x.py` returns empty; the function name defined in
that file, e.g. `batch_retrieve_payloads`, returns real results).
`blame_component` is the opposite — it wants a **file path**, not a function
name. `get_source` also takes a function/class **name**. `describe_node`
takes a node's exact **name**; a file, folder or docs node can also be named
by its repo-relative path, and a docs node keyed by a front-matter id by that
id (for example `ADR-013`). Passing the wrong
kind of identifier looks like an empty/broken result but is a usage
mismatch, not a graph gap.

**Same-named targets**: `find_callers`, `impact_analysis` and
`find_related_files` take an optional `file` (the target's repo-relative
path) to pick one of several functions or classes with the same name. Without
it, a name defined in more than one file is answered for all of them, with the
same rows as before, and the response adds `ambiguous_targets`, a
`{count, results, truncated}` envelope of the definitions' `{label, file}`
(`file` is null for a file-less one, such as a route's handler stub; `repo_id`
is added with `cross_repo`). Every `find_callers` and `find_related_files` row
adds `target_files`, the files of the targets it belongs to (at most 10, with
`target_file_count` past that; a file-less target is never listed); in
`find_related_files` a row becomes `{"name", "target_files"}`.
`impact_analysis` traces each file's target apart for up to five files: every
dependent adds `target_files` and `risk_level` is the riskiest one's. For a
name in more files (`get`, `run`) the dependents of all of them are listed
together, with a notice to pass `file`. Another definition of the same name
can itself appear as a dependent. A name defined in one file answers as
before, with no `target_files`. `file` takes `\` or `/`, with or without a
leading `./` (`get_source` also takes an absolute path inside the repository).

**Route handlers**: a route also writes a file-less handler stub of its
handler's name. `get_source`, `describe_node` and `trace_request_flow` answer
with the real function and leave the stub out, so a handler is never
`ambiguous` with its own stub and a flow lists it once; `describe_node` folds
the stub's relationships into the function's when that function is the only
one of its name with a file. A stub is only answered when no function of its
name has a file (the handler's definition wasn't found).

**Where an answer is, and one class's method**: every `find_callers` row
names the caller's `file`, `owner` (the classes or functions it is declared
in, `""` at the top level) and `start_line`, so `A.run` and `B.run` of one
file are two rows told apart; a Module caller has no owner or line. Every
`impact_analysis` dependent is `{name, type, file, owner}` (owner left out
and file null when it has none). `find_callers`, `impact_analysis` and
`find_related_files` take `owner` to pick one class's method, as `get_source`
and `describe_node` do, or a method named with its class (`A.run`,
`A::run`) when no node has that exact name; an owner with no such member is
an error. `get_source` on an unknown name (or one not in `file`, or of no
such `owner`) is an error with similar names, never an all-null answer.

**Response shape (`count`/`results`/`truncated`)**: `search_component`,
`find_callers`, `list_services`, `list_endpoints`, `find_related_prs`, `issue_history_for`,
`find_requirements_for` and `blame_component` return
`{"count": N, "results": [...], "truncated": bool}` instead of a bare
list — read the match set from `result["results"]`, and check `truncated`
before assuming you've seen everything. `find_related_files`,
`explain_architecture` and `impact_analysis` apply the same envelope to each
of their list-valued fields individually (e.g. `impact["direct_dependents"]["results"]`). Pass
`max_results` (default 15) to widen the sample when you genuinely need more
than the default. `search_component` ranks the whole query as the exact name
first (then the same name in other case), then names starting with or
containing the whole query, then names equal to, starting with or containing
one of its words (so `run_cypher` comes before the many functions named `run`),
then description-only matches; each
result carries its `file`, and a route's file-less handler stub is dropped
when a real function of that name exists. Its `count` is the true number of
matches, except when exact and prefix matches alone fill `max_results`: then
the response adds `count_is_lower_bound: true`. `max_results` is capped at
200. The whole query and each of its words (`user_service`, underscores kept)
are matched as well as its tokens. An exact name in unusual case (`gEt` for
`get`) is only found by the substring scan, so it can be missed when exact and
prefix matches alone fill the page. `describe_node`'s
response is not itself an envelope: each relationship group inside its
`outgoing` and `incoming` maps is one (`{"count", "results", "truncated"}`).

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
implementation` (or `global tool`). `run_cypher`, which returns a list,
can't carry it;
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
| "What is X / where is it?" | `search_component` (code, routes, datastores including caches, and compose containers: `redis` finds the Redis cache and the `redis` container) |
| "What is X connected to? / follow X's links" | `describe_node` (a name, or a path or id for file, folder and docs nodes) |
| "What is X, but only recently modified?" | `search_component` with `modified_within_commits=N` (only matches entities touched in the last N commits) |
| "What calls X?" | `find_callers` (name, not path; callers in test code come after production ones, `test_count` says how many there are, and `include_tests=False` leaves them out) |
| "What calls X, but only recently modified?" | `find_callers` with `modified_within_commits=N` (filters results to entities touched in the last N commits) |
| "What calls X, but only from within class Y?" | `find_callers` with `scope_to_class=Y` (cuts noise from unrelated same-named methods elsewhere in the repo) |
| "What definitely calls X?" | `find_callers` with `resolved_only=True` (Python callers resolved through scope or imports) |
| "What breaks if I change X?" | `impact_analysis` (name, not path; test-code dependents come last in each list, with `test_count`, and `include_tests=False` leaves them out) |
| "What breaks across this whole PR/diff?" | `impact_analysis_for_diff` (base_ref/head_ref, both must exist locally — never fetches) |
| "What does X depend on?" | `get_service_dependencies` (service name), `find_related_files` (function/class name, not path) |
| "Trace a request from this endpoint through services/datastores" | `trace_request_flow` (endpoint name like `GET /users/<id>`, or a bare path like `/users/<id>` for every method) |
| "What changed recently?" | `list_recent_changes` (lists entities touched in the last N commits, most-recent first; filters by optional entity type) |
| "What's the overall architecture?" | `explain_architecture` (each service's datastores: databases, vector stores, queues and caches), `summarise_repository` (node counts, `cache_count` included) |
| "Which routes does this service expose?" | `list_endpoints` (filters: `method`, `path_prefix` by whole segments, `framework`, `service`; each route with its handlers and owning service; a route whose mount its file doesn't show has `mounted: false` and a name like `GET ?/:id (routes/users.js)`) |
| "What changed between my branch and main?" | `compare_branches(branch_a="main", branch_b="<branch>")`: files, per-file symbols added, removed and changed, and the graph callers of what changed |
| "Why was X built this way?" | `explain_decision`, `trace_design_rationale` |
| "What requirements does X satisfy?" | `find_requirements_for` |
| "Which docs mention X?" | `find_mentions` (only useful if Markdown mentions indexing was enabled; otherwise an empty result whose `notice` names the enable command) |
| "Who changed X and when?" | `blame_component` (file path, not name) |
| "What PRs/issues touched X?" | `find_related_prs`, `issue_history_for` (only useful if PR/issue ingestion was enabled in step 2; otherwise they return an empty result whose `notice` names the enable command, and never contact GitHub) |
| "Show me X's actual code" | `get_source` (name, not path — returns source text + full docstring; reads live from disk using the last-indexed line range, so rescan first if the file may have changed; a name defined in several files returns `status: "ambiguous"` with `candidates`, so pass `file` to pick one; each candidate carries `owner`, which `get_source` also takes, to pick one of several same-named functions in one file; a method can be named `A.save` or `A::save`, and a Kotlin companion's member `Repo.create`) |

`compare_branches` and `impact_analysis_for_diff` both take two local refs
and never fetch. `compare_branches` compares from the merge base, like
`git diff main...branch` (what a pull request shows), and names the functions
and classes added, removed and changed in each file, with line numbers. Its
`impacted_callers` come from the last index of the working tree, not from
either ref, so read them as a hint. `impact_analysis_for_diff` compares the same
way and traces dependents, direct and transitive, of the functions and classes
changed or removed; added ones are listed in `added_symbols`, since nothing
depends on them yet. A changed code file whose symbols weren't diffed (past the
200-file cap, unreadable, or renamed) counts every indexed symbol in it, and a
notice says so; so does a changed symbol the index doesn't have, whose
dependents are then unknown. An unknown repository or ref is an error. Use `compare_branches` for "what did my
branch change?" and `impact_analysis_for_diff` for "what could this diff
break?".

Mistakes are errors, not empty answers: an unregistered `repo_id` fails with
`no such repo_id`; `find_callers`, `find_related_files`, `impact_analysis`,
`get_service_dependencies`, `trace_request_flow`, `explain_decision`,
`find_requirements_for` and `trace_design_rationale` on a name the graph
doesn't have, and `blame_component`, `find_related_prs` and `issue_history_for`
on a file it doesn't have, fail with `no ... named ...` and up to five similar
names (a name that exists but has no answer, such as a function nothing calls,
is still `count: 0`); `list_recent_changes` with an unknown `entity_type` fails
and lists the valid labels; `list_endpoints` with a `method` that is no HTTP
method token, or an unknown `framework` (naming the valid ones), fails, and with an unknown `service` like
`get_service_dependencies`; and a count
argument below its minimum (`max_results`, `within_commits`,
`modified_within_commits`, `members_per_community`, `max_per_type` below 1,
`max_length` below 2) fails and names the argument.

If Neo4j is down, every tool fails within a few seconds with `Neo4j isn't
reachable at <uri>; start Neo4j ...`: tell the user to start Neo4j (or run
`devgraph doctor`) rather than retrying the call in a loop.

If a tool returns empty/sparse results, check whether the repo has actually
been scanned (step 1/2) before concluding the graph has nothing to say — an
unindexed repo will legitimately return empty results, that's not a tool
failure.

### Walking the graph

`describe_node` shows one node and the nodes linked to it, so an assistant
can follow links one step at a time:

1. Find a starting node with `search_component` (it matches part of a name),
   or start from a name you already know.
2. Call `describe_node(name=...)`. It returns the node's fields and its
   links, grouped by relationship type, in `outgoing` and `incoming`.
3. Each linked node comes back as a ref, `{label, name, file}`. Pick one and
   call `describe_node(**ref)` to look at it in turn.

Things to know:

- It goes one hop per call. Each relationship type lists at most 50
  neighbours (`max_per_type`, default 10), and there is no paging.
- `count` is the full number of neighbours of that type, so check
  `truncated` to know whether you saw them all.
- To narrow a busy node, pass `direction` (`"out"` or `"in"`),
  `relationship_types` (for example `["CALLS"]`) or `neighbor_labels`
  (for example `["Module"]`).
- `status: "ambiguous"` means the name matched more than one node (say a
  `Module` and a `File` on the same path, or a `main` function in two
  files). The response lists the `candidates`; pick one and call
  `describe_node(**candidate)`. A candidate carries `file` only when it has
  one (a `Module` doesn't), and then `owner` too: the classes or functions
  it is declared in, `""` at the top level, which tells a top-level `save`,
  `A.save` and `B.save` of one file apart. Pass the candidate as it is. A
  method can also be named with its class: `describe_node(name="A.save")`
  or `"A::save"`.
- A name that matches nothing is an error that names the repository it
  searched and suggests up to five nodes whose name or path contains it
  (partial-name matches, not fuzzy ones).
- There is no `cross_repo`. Pass `repo_id` to look in another repository.

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

- **Calls are resolved through scope, imports and types, in every
  language.** A call links to the file its callee can be in: a function of
  the same file, the module, package, class file, crate module or header an
  import, `use` or `#include` names, `self`/`this`/`super` methods in the
  class or its bases, and methods on a value whose type a parameter, a
  local, a field or a constructor call gives. Each `CALLS` edge has a
  `confidence`: `resolved` (a file the caller names), `package` (a file its
  imports reach less exactly: under an imported package, a class file found
  by path suffix in another module, a C# class file `/T.cs`, a C++ header's
  same-stem source) or `name` (a method on a value nothing types, linked to
  every `Function` of that name, never back to its own caller). A library
  type's method (`.get`, `.push_back`, `List.Add`) on such a value, a call
  through a library type or an external package, and a bare name nothing
  defines or imports link nothing. `find_callers` returns each caller's
  `confidence`, most certain first; pass `resolved_only=True` to keep only
  `resolved`/`package` callers. When a call was made from inside a method
  body, its `CALLS` edge records the caller's enclosing class as
  `caller_class` — pass `find_callers(..., scope_to_class="ThatClass")` to
  filter down to just that class's own callers. Graph insights (PageRank,
  communities) leave out `name` and `package` calls and `package`
  imports. What stays out of reach: a C# class in a file not named after it, two C# types of one name
  in different namespaces (both get the edge), and a method a type inherits
  from a base declared in another file.
- **Import/include resolution reads only the importing file and the
  repository's resolver configuration** (tsconfig/jsconfig, go.mod/go.work,
  Cargo.toml). Python, JS/TS, Go, Java, Kotlin and Rust imports name files
  by each language's own rules; a C# `using` names a namespace, so C#
  `IMPORTS` edges go to the classes a file references; a C++ quoted
  `#include` names the file beside the includer, else the same path under
  another directory (a bare `"x.h"` only under an `include/` or `src/`
  directory), and `#include <...>` is never resolved (no compiler
  flags are available to DevGraph). An import of something outside the
  repository gets no edge, and a target that doesn't exist yet links when
  it is added.
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
