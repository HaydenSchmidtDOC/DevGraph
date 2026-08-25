# Using DevGraph from another repository

This file is meant to be copied into (or pasted/summarized into) another
repo's `CLAUDE.md`/`AGENTS.md`, or handed directly to a Claude Code instance
working there, so it knows how to register that repo with DevGraph and use
DevGraph's MCP tools as part of its normal workflow — instead of re-reading
the whole repo from scratch on every architecture/dependency question.

DevGraph itself lives at:

```
C:\Daifuku RAG Dev\Neo4J CodeRag MCP
```

Everything below assumes that repo's venv and Neo4j container already exist
(they're a one-time setup — see that repo's own `README.md` if they don't
yet). This file does not duplicate DevGraph's own internals; it only covers
what a client repo needs to know to use it.

---

## What this gets you

DevGraph builds a queryable graph of a repo's structure — modules, classes,
functions, containers, API endpoints, datastores, design decisions,
requirements, and git history — and exposes it through 16 MCP tools
(`search_component`, `find_callers`, `impact_analysis`,
`explain_architecture`, `blame_component`, `find_requirements_for`, etc.).
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
From any shell:

```bash
podman ps --filter name=devgraph-neo4j
```

If it's not running, start it (see DevGraph's own `README.md` for the exact
`podman run` command) — do this from the DevGraph repo, not from here.
**Never** create or touch a container with a different name prefix; if you
see other `podman ps` entries, they belong to unrelated projects and are out
of scope.

## 1. Register (and automatically index) this repo with DevGraph

Run once per machine (DevGraph's registry is local to the machine, not per
client-repo):

```bash
"C:\Daifuku RAG Dev\Neo4J CodeRag MCP\.venv\Scripts\python.exe" -m devgraph.cli.main add "<absolute path to this repo>"
```

This registers the repo **and runs a full initial scan** — Python source,
container/compose files, API routes, datastore usage all get indexed in one
pass via `devgraph/indexer/dispatch.py`. It prints the `repo_id` assigned
(defaults to the folder name, deduped if already taken) and how many files
were indexed. **Record that `repo_id`** — every DevGraph MCP tool call needs
it. If Neo4j isn't reachable at registration time, `add` still succeeds
(registration and indexing are decoupled) and tells you to run
`devgraph rescan <repo_id>` once it's up.

Confirm registration:

```bash
"C:\Daifuku RAG Dev\Neo4J CodeRag MCP\.venv\Scripts\python.exe" -m devgraph.cli.main list
```

Only paths registered this way are ever watched or indexed by DevGraph —
there is no automatic or recursive discovery. Registering this repo does not
expose it to any other repo's queries unless someone explicitly passes
`cross_repo: true` on a tool call.

## 2. Re-index after changes, and index optional extras

`devgraph rescan <repo_id>` re-runs the same full scan as `add` — idempotent
(`MERGE`-based), safe to run repeatedly; existing nodes update in place
rather than duplicating. Use it any time you want the graph refreshed after
a batch of changes:

```bash
python -m devgraph.cli.main rescan <repo_id>
```

**Git history** (separate command — not part of the file-scan above;
incremental, only walks new commits since the last run):

```bash
python -m devgraph.cli.main index-history <repo_id>
```

**Docs / design decisions** (optional — only if this repo has Markdown notes
with `type: requirement|design_decision|architecture_note` front-matter;
`rescan` picks these up automatically too, once `--docs-path` is set):

```bash
python -m devgraph.cli.main annotate <repo_id> --docs-path <repo-relative docs folder>
python -m devgraph.cli.main annotate <repo_id> --note <repo-relative note file>   # index one note immediately
```

**PR/issue history** is opt-in and talks to an external service (GitHub,
etc.) — do not enable it without the repo owner's explicit go-ahead:

```bash
python -m devgraph.cli.main pr-source enable <repo_id>
python -m devgraph.cli.main issue-source enable <repo_id>
```

Enabling the flags above doesn't fetch anything by itself yet — actually
pulling PR/issue data currently requires a short Python script calling
`devgraph.indexer.pr_issues.extractor.index_pr_issues` with a configured
`GitHubSource`; there's no CLI command that performs the fetch yet.

## 3. Connect DevGraph as an MCP server

Register DevGraph as an MCP server for this session/project, pointing at its
stdio entry point:

- **command**: `C:\Daifuku RAG Dev\Neo4J CodeRag MCP\.venv\Scripts\python.exe`
- **args**: `-m devgraph.mcp.server`
- **cwd**: `C:\Daifuku RAG Dev\Neo4J CodeRag MCP` (required — that's where its config/`.env` resolve from)

With Claude Code's CLI, something like:

```bash
claude mcp add devgraph -- "C:\Daifuku RAG Dev\Neo4J CodeRag MCP\.venv\Scripts\python.exe" -m devgraph.mcp.server
```

(Exact flags depend on the Claude Code version in use — check `claude mcp add --help` if this doesn't match. The important part is the command/args/cwd above, not the specific CLI invocation.)

Once connected, 16 tools become available, all scoped by a `repo_id`
argument. **Always pass this repo's `repo_id` from step 1.** Never pass
`cross_repo: true` unless the user explicitly asks for a cross-repository
answer — the default is (and must stay) scoped to this repo only.

`run_cypher` will not appear unless DevGraph's own config has
`enable_run_cypher=true` set. If it's missing and you need something the
other 16 tools genuinely can't express, that's a signal a new high-level
tool should be added to DevGraph — not that raw Cypher should be turned on
as a workaround.

## 4. Using the tools day-to-day

Prefer these over re-reading files when the question is structural:

| Question shape | Tool |
|---|---|
| "What is X / where is it?" | `search_component` |
| "What calls X?" | `find_callers` |
| "What breaks if I change X?" | `impact_analysis` |
| "What does X depend on?" | `get_service_dependencies`, `find_related_files` |
| "What's the overall architecture?" | `explain_architecture`, `summarise_repository` |
| "Why was X built this way?" | `explain_decision`, `trace_design_rationale` |
| "What requirements does X satisfy?" | `find_requirements_for` |
| "Who changed X and when?" | `blame_component` |
| "What PRs/issues touched X?" | `find_related_prs`, `issue_history_for` (only useful if PR/issue ingestion was enabled in step 2) |

If a tool returns empty/sparse results, check whether the repo has actually
been scanned (step 1/2) before concluding the graph has nothing to say — an
unindexed repo will legitimately return empty results, that's not a tool
failure. Known real gaps below are a different thing: even a fully-indexed
repo won't populate these.

**Known current gaps** (confirmed against a real ~1300-node repo, not just
theoretical):

- `find_callers`/`impact_analysis` only see `CONTAINS`/`IMPORTS`/`EXTENDS`/`USES`
  relationships — the Python indexer doesn't yet extract actual call-site
  (`CALLS`) edges. "What calls X" will come back empty even for code that
  genuinely calls it.
- `explain_architecture`'s `uses`/`calls` output stays empty even on a
  fully-scanned repo: the container/API/datastore extractors run
  independently per file and don't cross-link their output — a `Service`
  node from a compose file and a `Database` node from Python source aren't
  automatically connected. `list_services` and `summarise_repository` (raw
  node counts) work correctly; the *relationship*-based architecture view
  doesn't yet.
- Multi-level relative imports (`from .sub.pkg import x`) don't resolve
  correctly — `Module` nodes are keyed by bare filename with no package
  path, so only single-level relative imports (`from . import x`,
  `from .sibling import y`) resolve to the right node.

## 5. Keeping the graph current

`devgraph add`/`rescan` do a full scan, and the watcher-to-indexer wiring
now exists (`devgraph.agent.TrayApp`, `python -m devgraph.agent.tray`) — but
the tray app is a separate always-on process most workflows won't have
running, and there's no CLI subcommand for it yet (it's a standalone script
entry point, not `devgraph <something>`). Without it running, nothing
watches this repo live: re-run `devgraph rescan <repo_id>` after a
meaningful batch of changes. A stale graph gives stale answers, so don't
rely on it for anything time-sensitive without refreshing first.

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
