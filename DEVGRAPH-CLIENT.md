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

## 1. Register this repo with DevGraph

Run once per machine (DevGraph's registry is local to the machine, not per
client-repo):

```bash
"C:\Daifuku RAG Dev\Neo4J CodeRag MCP\.venv\Scripts\python.exe" -m devgraph.cli.main add "<absolute path to this repo>"
```

This prints the `repo_id` assigned (defaults to the folder name, deduped if
already taken). **Record that `repo_id`** — every DevGraph MCP tool call
needs it. Confirm registration:

```bash
"C:\Daifuku RAG Dev\Neo4J CodeRag MCP\.venv\Scripts\python.exe" -m devgraph.cli.main list
```

Only paths registered this way are ever watched or indexed by DevGraph —
there is no automatic or recursive discovery. Registering this repo does not
expose it to any other repo's queries unless someone explicitly passes
`cross_repo: true` on a tool call.

## 2. Index this repo

DevGraph has no single "index everything" command yet — each extractor is
invoked separately. Run these from a shell with DevGraph's venv Python on
`PATH`, or by full path as above.

**Python source** (repeat per changed file, or loop over the tree on first
index):

```python
from devgraph.graph.engine import GraphEngine
from devgraph.indexer.python.extractor import index_file
from pathlib import Path

engine = GraphEngine("bolt://127.0.0.1:7687", "neo4j", "devgraph-local-dev")
engine.init_schema()
for f in Path(".").rglob("*.py"):
    index_file(engine, "<repo_id>", f)
engine.close()
```

**Git history** (incremental — safe to re-run, only walks new commits):

```bash
python -m devgraph.cli.main index-history <repo_id>
```

**Docs / design decisions** (optional — only if this repo has Markdown notes
with `type: requirement|design_decision|architecture_note` front-matter):

```bash
python -m devgraph.cli.main annotate <repo_id> --docs-path <repo-relative docs folder>
python -m devgraph.cli.main annotate <repo_id> --note <repo-relative note file>   # per note
```

**PR/issue history** is opt-in and talks to an external service (GitHub,
etc.) — do not enable it without the repo owner's explicit go-ahead:

```bash
python -m devgraph.cli.main pr-source enable <repo_id>
python -m devgraph.cli.main issue-source enable <repo_id>
```

Re-indexing is idempotent (`MERGE`-based) — running any of the above again
updates existing nodes in place rather than duplicating them, so it's safe
to re-run after making changes in this repo.

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

If a tool returns empty/sparse results, check whether the relevant extractor
has actually been run (step 2) before concluding the graph has nothing to
say — an unindexed repo (or one only partially indexed) will legitimately
return empty results, that's not a tool failure.

**Known current gap**: `find_callers`/`impact_analysis` only see
`CONTAINS`/`IMPORTS`/`EXTENDS`/`USES` relationships — the Python indexer
doesn't yet extract actual call-site (`CALLS`) edges. Don't be surprised if
"what calls X" comes back empty even for code you know calls it; that's a
known limitation of the current indexer, not a sign this repo wasn't
indexed.

## 5. Keeping the graph current

There is no automatic re-indexing wired up yet (the watcher emits
change events, but nothing currently consumes them to trigger a re-index).
Re-run the relevant step-2 commands after a meaningful batch of changes —
you don't need to do it after every single file edit, but a stale graph will
give stale answers, so don't rely on it for anything time-sensitive without
refreshing first.

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
