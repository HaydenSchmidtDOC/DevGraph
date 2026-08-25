# DevGraph — Implementation Plan #2: Rollout Readiness & Extraction Quality

Companion to `Design Brief #1.md` and `Implementation Plan #1.md` (Phases 1-3, complete).
This is a **handover document for another developer** to pick up and implement — it is
build-ready to the same granularity as Implementation Plan #1's Phase 1 section. No code
has been written against this plan yet.

## Context

DevGraph works today — the MCP tool layer, indexers, and graph engine are solid and
verified against real data (RAG4, ~1300+ nodes; see CLAUDE.md's Status section and the
git history for the CALLS-extraction/Service-cross-linking/Module-keying fix passes). But
two separate gaps stand between "works" and "ready for a developer to pick up without
hand-holding":

1. **Operational friction.** Bootstrapping requires knowing four manual steps (venv, pip
   install, a hand-typed `podman run`, then remembering `index-history`/`annotate` as
   separate follow-ups). Registering DevGraph as an MCP server means hand-editing a
   `claude mcp add` command with a hardcoded absolute path that only works on the machine
   `DEVGRAPH-CLIENT.md` was written on. There's no single health check that would have
   caught the exact class of bug that shipped once already (`devgraph/mcp/server.py`
   written against the wrong `mcp` SDK API shape, only surfacing at runtime).
2. **Extraction quality gaps that cost the token savings DevGraph exists to provide.**
   A live-testing session against RAG4 confirmed the CALLS/Service-linking fixes hold up,
   but surfaced that `description` is unpopulated on every node (so "what does X do" still
   requires a full-file `Read`, not a graph query), there's no tool to fetch just a matched
   function's source lines (so "show me X" also falls back to a full-file `Read`), and
   `IMPORTS` resolution still misses RAG4's actual dominant import style
   (`sys.path`-manipulated bare-name imports), confirmed at 5 edges for 87 modules.

This plan covers both: closing the operational gaps (items 1-7) and closing the two
highest-leverage extraction gaps a live tester flagged as the "biggest token-saver left on
the table" (items 8-9), plus documenting a third gap that turned out to be genuinely hard
to fix cleanly rather than squeezing in a rushed fix (item 10).

**Decisions already made** (confirmed with the project owner during planning — do not
re-litigate without a reason):
- Item 2 (MCP registration) and item 6 (multi-machine path handling) merge into a single
  `devgraph client-config` command.
- Item 3: `--full` is an **opt-in flag** on `add`/`rescan`, not new default behavior.
- Item 4: `deploy/podman-compose.yml` stays as a labeled best-effort fallback, not deleted.
- Item 5: `doctor` stays a **separate command** from `status`, not merged.
- Item 6 (bootstrap.sh, POSIX): deferred — PowerShell-only for this pass.
- Item 7: tray liveness uses a **heartbeat timestamp file**, not a PID file or process-scan.
- Item 8: `description` holds a **PEP-257 summary line only** (with a ~100-120 char hard
  fallback truncation); the full docstring is captured separately into `docstring_full` so
  nothing is discarded and `get_source` (item 9) can return it without a second parse.
- Item 9: `get_source` covers both `Function` and `Class` nodes, and includes
  `docstring_full` in its response when present.
- README's stale status content (test count, false "not yet built" claims) gets fixed as
  part of item 1's edits, not a separate pass.

---

## Grounding notes (confirmed against the actual repo — cite these, don't re-derive)

- `devgraph/cli/main.py` (400 lines, Typer app) already has the helper pattern this plan
  leans on hardest: `_get_registry()` (settings → `RepoRegistry`) and the
  `GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)` /
  `engine.init_schema()` / `finally: engine.close()` construction block, repeated
  near-verbatim in `add`, `rescan`, `annotate`, `index-history`, and `status`.
- `devgraph/cli/main.py::status` (lines 365-396) is the existing health-check base: builds
  a `GraphEngine`, calls `engine.verify_connectivity()` in try/except, then reports
  registry counts via `_get_registry()`. `doctor` extends this shape, not a reinvention.
- `devgraph/cli/main.py::add` (lines 27-68) already separates registration success from
  indexing failure (registration commits to SQLite first; a scan failure afterward is
  caught separately and tells the user to `rescan` later) — `--full`'s history-indexing
  step should follow the same separation pattern for its own failure mode.
- `devgraph/indexer/dispatch.py::full_scan(engine, repo_id, repo_root, docs_path=None) -> int`
  is what `add`/`rescan` already call. `devgraph/indexer/git_history/extractor.py::
  index_repo_history(engine, registry, repo_id, max_count=None) -> int` is what
  `index-history` already calls (needs `registry`, not just `engine`). `add --full` calls
  both from inside the same `add` command body — already imported at the top of `main.py`.
- `devgraph/config/settings.py`: `Settings(BaseSettings)`, `env_prefix="DEVGRAPH_"`,
  `env_file=".env"`, `registry_db_path: Path = Path.home()/".devgraph"/"registry.sqlite3"`
  (already user-home-scoped, not repo-relative — the one piece of state that's already
  "any machine" safe). `.env.example` exists at repo root; bootstrap should not invent a
  new template.
- `devgraph/mcp/server.py::main()` (lines 133-147) docstring/body confirms the exact
  entrypoint: `.venv/Scripts/python -m devgraph.mcp.server`, calling
  `GraphEngine(...).verify_connectivity()` → `init_schema()` → `build_server(engine)` →
  `server.run("stdio")`. No console-script for it in `pyproject.toml` (`[project.scripts]`
  currently only has `devgraph = "devgraph.cli.main:app"`).
- `devgraph/agent/tray.py::TrayApp`: `_health_check_loop` (lines 86-95) already runs every
  `settings.health_check_interval_s` (default 30s), calling `verify_connectivity()` and
  updating `_healthy`/the tray icon. `__init__` directly constructs `RepoRegistry`/
  `GraphEngine` with no bootstrap/precondition checks. Module docstring is explicit that it
  does NOT host the MCP server (stdio is 1:1 per client — an MCP client spawns
  `devgraph/mcp/server.py` itself). No CLI wraps `python -m devgraph.agent.tray`.
- `deploy/podman-compose.yml` is a real, correct declarative mirror of the documented
  `podman run` command (same image/env/`devgraph-*` naming/localhost-only ports), but
  CLAUDE.md itself already flags it needs a compose provider that isn't confirmed
  installed. `podman` is not on PATH by default in a fresh shell on this machine, consistent
  with README/CLAUDE.md's own "check `%LOCALAPPDATA%\Programs\Podman`" caveat. No
  `scripts/` directory exists anywhere in the repo today.
- Installed `mcp` package (as of this plan) is version 2.1.0, Python 3.13.13 — the concrete
  "known-good" baseline `doctor` should check against, since `pyproject.toml` only pins
  `mcp>=2.0` (a floor, not a ceiling), and the bug this plan's `doctor` item targets
  (server.py originally written against the wrong SDK API shape) was exactly a
  floor-only-pin class of failure.
- **README.md is stale relative to CLAUDE.md/DEVGRAPH-CLIENT.md**: line 20 still says
  "154 tests" (current: 195); lines 37-42 claim CALLS extraction and tray
  watcher-to-indexer wiring are "Not yet built," which is false — both are implemented
  (`tray.py`'s `_on_changes` calls `index_paths`/`remove_paths`; CALLS extraction is in
  `python/extractor.py` and verified against RAG4); lines 80-90 say "Indexing is invoked
  per extractor today (no single 'index everything' command yet)" with a raw Python
  snippet, when `devgraph add` has run a full automatic scan since the wiring pass.
- `DEVGRAPH-CLIENT.md` is already up to date on the CALLS-edge/`explain_architecture`
  fixes (matches CLAUDE.md's "all previously-known gaps are now closed" section). The one
  genuinely undocumented gap there is the function-vs-file-path identifier inconsistency
  across `impact_analysis`/`find_related_files` (function/class name only) vs.
  `blame_component` (file path only) — confirmed by live-test feedback, not currently
  mentioned anywhere in the doc's tool table.
- **Confirmed live against the current graph** (RAG4, 1344 nodes):
  - `MATCH (n) WHERE n.description IS NOT NULL RETURN COUNT(*)` → **0**, and Neo4j itself
    warns the `description` property doesn't exist anywhere in the graph. Confirms item 8's
    premise exactly: nothing populates `description` today, even though `search_component`'s
    Cypher already searches it (`devgraph/mcp/tools.py::search_component`,
    `toLower(n.description) CONTAINS toLower($query)`).
  - `MATCH (m:Module)-[:IMPORTS]->(t) RETURN COUNT(*)` → **5**, confirming the retest
    feedback's number exactly, not an artifact of stale data.
  - **Root-caused** the IMPORTS gap by reading real RAG4 source: `services/api_gateway/
    hybrid_search.py:43` has `from sparse_encoder import get_sparse_encoder` — a **bare
    single-segment import**, resolved at runtime via a manual `sys.path.insert(0,
    _SHARED_UTILS_DIR)` a few lines earlier (line 40-41, comment: "Self-sufficient
    sys.path fixup — same pattern as shared/utils/"). The actual file is at
    `shared/utils/sparse_encoder.py`. The current extractor's dotted-path guess
    (`devgraph/indexer/python/extractor.py::_dotted_to_file_path`) only fires when
    `"." in dotted_name` — a bare single-segment name is explicitly skipped, so this
    class of import (confirmed present via `grep` in `api_gateway/hybrid_search.py` and
    `api_gateway/retrieval.py`, likely repo-wide given the "same pattern as shared/utils/"
    comment) can never resolve without also parsing the `sys.path.insert` calls earlier in
    the same file. See item 10 for why this is scoped as "document, don't chase."
  - Re-checked `_extract_call_targets`/`_callee_simple_name`
    (`devgraph/indexer/python/extractor.py` lines 125-175): chained/dependency-injected
    calls like `get_http_client().get(...)` already resolve correctly (the recursive
    `_callee_simple_name` call on a `call`-type function node lands on the inner call's
    callee). Live-test feedback's concern here is about **false precision** (any
    `self.foo()` linking to *every* Function node named `foo` repo-wide, not just the
    right class's method), which is a known, already-documented limitation
    (`DEVGRAPH-CLIENT.md`'s "Call graph is name-based, not type-resolved" callout), not a
    new missing-edges gap. No code change proposed for this.

---

## Sequencing rationale

Two pieces of shared infrastructure gate almost everything in the operational half of this
plan, so they land first:

1. **Path/environment resolution helper.** Item 2 (MCP registration), item 5 (`doctor`),
   and item 6 (`client-config`) all need the same three facts: the resolved absolute path
   to `.venv/Scripts/python.exe`, the resolved absolute repo root (`cwd` for the MCP
   server), and the Python/mcp/Neo4j version facts. Build this once as a small internal
   module (`devgraph/cli/_env.py`) and have `doctor`/`client-config` both import it.
2. **`doctor` before the bootstrap script's final step.** The bootstrap script's last step
   ("verify everything worked") should shell out to `devgraph doctor` rather than
   duplicating health-check logic in PowerShell. So `doctor` conceptually lands before
   item 1's script even though bootstrap is priority #1 — practically, design/implement
   both in the same pass, write `doctor` first.

Item 8 (docstring extraction) should land before item 9 (`get_source` tool) since item 9's
line-range capture is a natural extension of the same Tree-sitter node-visiting code item 8
touches — doing them as one indexer pass avoids walking the same AST twice for two separate
PRs.

**Suggested build order**: 5 (doctor + shared env-resolution helper) → 1 (bootstrap
script, calls doctor) → 2/6 (client-config, reuses env-resolution helper) → 3 (add --full)
→ 8 (docstring extraction) → 9 (get_source tool) → 4 (compose file doc decision) → 7
(status extended with tray liveness) → doc polish (DEVGRAPH-CLIENT.md tool table, README
staleness fix, item 10's documented-limitation note). Items 3, 7, 8, and 9 are independent
of the path-resolution work and could be parallelized.

---

## Item 1 — One-command bootstrap script

**New file:** `scripts/bootstrap.ps1` (PowerShell only for this pass — Windows-primary
project per CLAUDE.md's stated shell; a POSIX `.sh` mirror is explicitly deferred).

**Outline (each step: clear pass/fail message, non-zero exit on failure):**

1. **Preflight**: check `python` resolves and is ≥3.13, check `git` resolves. Never attempt
   to install either — print README's Prerequisites section and exit non-zero if missing.
2. **Podman resolution**: check `podman` on PATH; if not found, check the documented
   fallback (`%LOCALAPPDATA%\Programs\Podman`) the same way the docs already tell a human
   to. If found only at the fallback, use it via full path for this process's calls only —
   never mutate the user's persistent PATH. If not found anywhere, print the same guidance
   a human gets and exit non-zero — never attempt to install Podman.
3. **Venv create-or-reuse**: `python -m venv .venv` only if `.venv/` doesn't exist
   (idempotent re-run support).
4. **Editable install**: `.venv/Scripts/python.exe -m pip install -e ".[dev]"` — the one
   and only `pip install`-shaped step, always targeting `.venv/` explicitly.
5. **Container check-or-start**: `podman ps --filter name=devgraph-neo4j` to check
   running; `podman ps -a --filter name=devgraph-neo4j` + `podman start` if it exists but
   is stopped (avoids duplicate-name errors, preserves volumes); the full documented
   `podman run -d --name devgraph-neo4j ...` command (unmodified from README/CLAUDE.md) if
   it doesn't exist at all. Every Podman call is scoped to the literal name
   `devgraph-neo4j` — never a wildcard/list-all pattern.
6. **Health wait loop**: poll Neo4j Bolt reachability with a bounded retry (~30s total, 2s
   interval) since first boot after volume creation takes a few seconds. Fail loudly, not
   silently, if it never comes up.
7. **Schema init**: call `engine.init_schema()` once directly (no repo is registered yet on
   a fresh bootstrap, so nothing else would call it) — either a one-off `-c` invocation or
   folded into `doctor` itself running it idempotently.
8. **Final verification**: run `devgraph doctor` (item 5) and surface its output — the
   single "did bootstrap actually work" signal, reusing rather than duplicating checks.
9. **Next-steps message**: print the next commands (`devgraph add <path>`, then point at
   item 2/6's `client-config` command for MCP registration — not a hardcoded path).

**Existing files to edit:**
- `README.md`: replace the "## Setup" section's 3 manual commands with
  `.\scripts\bootstrap.ps1`; keep Prerequisites as-is (bootstrap checks, doesn't install);
  **fix the stale status content** (line 20's test count, lines 37-42's false "not yet
  built" claims, lines 80-90's "no single index-everything command" claim) to match
  CLAUDE.md's current, accurate status section.
- `CLAUDE.md`: update the "## Commands" section's venv/install/Neo4j bullets to point at
  the script as canonical, while keeping the raw `podman run` command visible (e.g. as
  "what the script does") since CLAUDE.md is also the agent-facing reference an agent may
  need if the script itself is broken/unavailable.

**Constraint compliance** (hold these as constraints, matching CLAUDE.md's Core Design
Principles — do not relax for convenience): no `pip install` outside `.venv/`; no system
package manager calls; every Podman command scoped to `devgraph-neo4j` literally; script
never calls `devgraph add` itself (stops at "Neo4j is up and schema exists," honoring
explicit-registration-only); no network calls beyond what `podman pull` needs for the
pinned Neo4j image on first run.

---

## Item 2 & 6 — `devgraph client-config` (merged)

**New CLI command in `devgraph/cli/main.py`.** Merges MCP registration and multi-machine
path handling into one command, since both need identical resolved-path logic and differ
only in output shape.

Behavior:
- Resolve `sys.executable` (the running interpreter — since this only runs after
  `pip install -e .` succeeded and `devgraph` is a console-script, `sys.executable` inside
  the running process already IS the venv python). This is what makes output
  machine-portable instead of `DEVGRAPH-CLIENT.md`'s current literal-path problem.
- Resolve the repo root robustly to any invoking cwd (walk up from `__file__` to the
  directory containing `pyproject.toml`, not a hardcoded relative-path guess).
- **Default output**: the full ready-to-paste Markdown block matching
  `DEVGRAPH-CLIENT.md`'s existing "## 3. Connect DevGraph as an MCP server" section shape
  (command/args/cwd bullets + the `claude mcp add` one-liner), with actual resolved paths
  substituted in for the machine it's run on.
- **`--claude-mcp-add-only` flag**: print just the one-liner.
- **`--run` flag (opt-in)**: shell out and execute the constructed `claude mcp add ...`
  command via `subprocess.run`, only if `claude` resolves on PATH first (clear error
  otherwise). Default stays print-only/side-effect-free.
- Does not attempt to verify the registration succeeded — that's `claude`'s responsibility;
  this command's job ends at constructing/optionally invoking the correct string.

**Documentation-side companion change (the actual multi-machine fix, not just the new
command):**
- `DEVGRAPH-CLIENT.md`: replace every hardcoded `C:\Daifuku RAG Dev\Neo4J CodeRag MCP`
  occurrence (repo-location callout, `add`/`list` commands' literal python.exe path, the
  "## 3. Connect..." bullet list, the `claude mcp add` example) with: *"Run
  `devgraph client-config` from the DevGraph repo on this machine to get the exact
  paths/commands for this checkout — do not hardcode a path here, DevGraph's install
  location can differ machine to machine."* Keep one clearly-labeled example block showing
  the output's *shape*, marked explicitly as an example.
- This makes the doc itself portable if copied verbatim into multiple client repos on
  multiple developers' machines — a hardcoded path becomes wrong the moment DevGraph is
  checked out anywhere else; "run `devgraph client-config`" doesn't.

**Fold in the live-test-feedback tool-identifier nuance here**: `DEVGRAPH-CLIENT.md`'s
"## 4. Using the tools day-to-day" table gets a new column or callout distinguishing
function/class-**name** tools from file-**path** tools:

> `impact_analysis` and `find_related_files` match against function/class **names** only
> (a file path like `shared/utils/x.py` returns empty; the function name defined in that
> file, e.g. `batch_retrieve_payloads`, returns real results). `blame_component` is the
> opposite — it wants a **file path**, not a function name. Passing the wrong kind of
> identifier looks like an empty/broken result but is a usage mismatch, not a graph gap.

Add a one-line annotation next to each affected tool in the table itself too, so the
nuance is visible at tool-selection time, not just in a separate callout.

**Existing files to edit:**
- `devgraph/cli/main.py`: new `client-config` command, following the existing
  `try/except ValueError → Exit(1)` / `except Exception → Exit(1)` pattern.
- `DEVGRAPH-CLIENT.md`: path-hardcoding removal (4 locations) + tool table clarification.
- `devgraph/mcp/server.py`'s `@server.tool()` docstrings (optional, but note this touches
  a more sensitive surface than a Markdown file since it changes what the MCP client's
  model sees at tool-selection time): add a one-clause hint like "(pass a function/class
  name, not a file path)" to `impact_analysis`/`find_related_files`, and "(pass a file
  path, not a function name)" to `blame_component`.

---

## Item 3 — `add`/`rescan` gain an opt-in `--full` flag

Confirmed decision: **opt-in flag, not new default behavior.**

**Implementation, in `devgraph/cli/main.py::add`:**
- Add `full: bool = typer.Option(False, "--full", help="Also run incremental git-history indexing after the file scan (local-only, no network).")`.
- After the existing `full_scan(...)` call and `registry.mark_indexed(...)` line, if `full`
  is true, call `index_repo_history(engine, registry, record.repo_id)` — the exact function
  `index-history` already calls, already imported at the top of `main.py`. Wrap in the same
  try/except-and-warn pattern used for the scan-failure case immediately above it
  (registration + file-scan success shouldn't be undone by a history-indexing failure) —
  distinct message: `"[yellow]Full scan complete but history indexing failed:[/yellow] {e}\n  Run 'devgraph index-history {repo_id}' to retry."`
- Do NOT fold `annotate --docs-path`/`--note` into `--full` — those require a docs-path
  argument with no reasonable default, and auto-discovering one would edge toward the
  auto-discovery the hard constraints forbid. Leave `annotate` manual; document clearly.
- `rescan` gets the same `--full` flag for symmetry (small addition, same pattern, same PR).

**Existing files to edit:**
- `devgraph/cli/main.py`: `add` and `rescan`.
- `DEVGRAPH-CLIENT.md`: update steps 1/2 to mention `--full`.
- `README.md`: update the "Using it" section's command list similarly.

---

## Item 4 — Resolve the Neo4j lifecycle ambiguity

Confirmed decision: **keep `deploy/podman-compose.yml` as a labeled best-effort fallback,
don't delete it.**

- The bootstrap script (item 1) becomes the single canonical way to start
  `devgraph-neo4j`, using `podman run` directly (matches today's documented approach,
  needs no compose provider).
- `deploy/podman-compose.yml` stays, with a header-comment change: from
  implicitly-parallel documentation to explicitly "this is an alternative for developers
  who already have a compose provider installed; the supported/tested path is
  `scripts/bootstrap.ps1`."
- `README.md`/`CLAUDE.md`: replace the current soft "(requires a compose provider...)"
  aside with a decisive statement that `scripts/bootstrap.ps1` is the supported path and
  the compose file is untested/best-effort.

No code changes — a documentation-ownership decision plus one comment edit.

---

## Item 5 — `devgraph doctor` command

**New CLI command in `devgraph/cli/main.py`, built as a superset of `status`'s existing
shape** (reuse `_get_registry()`, the `GraphEngine(...)`/`verify_connectivity()`/
`finally: close()` pattern) rather than a parallel implementation. Confirmed decision:
**kept separate from `status`** — `status` stays fast/lightweight for quick glances;
`doctor` is a heavier environment-drift diagnostic for bootstrap/troubleshooting moments,
calling the same helper functions `status` uses so there's no logic duplication.

Checks, in order, continuing past non-fatal failures so one run surfaces everything at
once:

1. **Python version** — `sys.version_info >= (3, 13)`, print `sys.executable` alongside
   (surfaces "which python is this actually running under" ambiguity directly).
2. **`mcp` package version** — `importlib.metadata.version("mcp")` vs. the `pyproject.toml`
   floor (`mcp>=2.0`).
3. **MCP server importability smoke check** — attempt `from devgraph.mcp.server import
   build_server` in a try/except, surfacing the exact ImportError/AttributeError if the
   installed `mcp` SDK's API shape doesn't match what `devgraph/mcp/server.py` expects.
   Directly targets the failure mode that already shipped once (server.py written against
   the wrong SDK API shape, only surfacing at runtime) — turns it into a `doctor`-time
   failure instead.
4. **Neo4j reachability** — identical to `status`'s existing check.
5. **Neo4j schema present** — re-run `init_schema()` idempotently (already MERGE-based /
   safe to re-run).
6. **Podman container state** — `podman ps --filter name=devgraph-neo4j`, reporting
   running/stopped/not-found distinctly. Needs the Podman-resolution logic shared with
   item 1's bootstrap script (see sequencing note — shared helper module).
7. **Registry reachability** — already covered by `status`'s existing registry-count check.
8. **Tray/watcher liveness** (item 7's ask, folded in as one of doctor's checks) — reads
   the heartbeat file from item 7.

**Existing files to edit:**
- `devgraph/cli/main.py`: new `doctor` command.

**New file:**
- `devgraph/cli/_env.py` — shared helper functions: `resolve_venv_python() -> Path`
  (wraps `sys.executable`), `resolve_repo_root() -> Path`, `resolve_podman() -> Path | None`
  (PATH-then-fallback-location check). Called from `doctor`/`client-config`. Full sharing
  across PowerShell and Python isn't possible directly, so `bootstrap.ps1`'s own Podman
  check should at minimum check the identical fallback path
  (`%LOCALAPPDATA%\Programs\Podman`) so behavior stays consistent even though the code
  can't be literally shared.

---

## Item 7 — Tray/watcher liveness visibility

Confirmed decision: **heartbeat timestamp file** (not PID file, not process-scan).

- `devgraph/agent/tray.py::TrayApp._health_check_loop`: after the existing
  `verify_connectivity()`/`_refresh_icon()` calls, write current UTC timestamp to
  `settings.registry_db_path.parent / "tray_heartbeat.txt"` (same directory as
  `registry.sqlite3`, no new settings field needed).
- `devgraph/cli/main.py::status` (and `doctor`, as one of its checks) gains a "Live
  Watcher" section: read the heartbeat file if present, compare its timestamp against
  `2 * settings.health_check_interval_s`; report "running" / "stale (process may have
  crashed)" / "not running" (file absent) accordingly.

**Existing files to edit:**
- `devgraph/agent/tray.py`: heartbeat-write in `_health_check_loop`.
- `devgraph/cli/main.py`: extend `status` with the heartbeat-read section; `doctor`
  includes the same check via a shared small helper (not duplicated read/compare logic).

**Out of scope for this plan** (visibility only, not lifecycle management): there's still
no `devgraph tray start`/`stop` wrapper — only the raw `python -m devgraph.agent.tray`
invocation. Flag as a natural follow-up, don't build it here.

---

## Item 8 — Extract docstrings into `description` (highest-leverage item)

`search_component`'s Cypher already searches `n.description`
(`devgraph/mcp/tools.py`, line ~37: `toLower(n.description) CONTAINS toLower($query)`),
but the property is never populated — every node's `description` is `null` across the
entire graph (confirmed: 0 of 1344 nodes on a live RAG4 scan have a non-null
`description`). This means every "what does X do" question still requires a name-based
search followed by a full-file `Read` — exactly the token cost DevGraph exists to
eliminate. This is the single highest-leverage fix in this plan.

**Confirmed design:** `description` holds a **summary line only**, not the full
docstring — PEP 257's convention (first line up to the first blank line or a terminating
period), with a hard fallback truncation (~100-120 chars) for docstrings that don't follow
that convention, so one degenerate giant-first-line docstring can't blow up a 50-row
`search_component` result set. Rationale: `search_component` matches via `CONTAINS` on
`description`, and a keyword search works identically whether the field holds one line or
twenty; list-style tools return many nodes at once, so a full multi-paragraph docstring in
every row would balloon token cost and defeat the point of using the graph instead of
`Read`; the full text is one hop away via `get_source` (item 9) once the right target is
confirmed, so it shouldn't be paid for on every scan-and-narrow call.

**Store the full docstring too, just not in `description`.** Capture the complete
docstring text into a separate property, `docstring_full`, at the same parse-time pass —
costs nothing extra since Tree-sitter has already surfaced the full string node, and
avoids `get_source` (item 9) needing a second extractor pass or a live file re-read just
to get documentation text the indexer already saw once.

**Implementation:**
- `devgraph/indexer/python/extractor.py`: in `_visit_class`/`_visit_function`, after
  locating the `body` node, check whether the first statement in `body` is an
  `expression_statement` wrapping a `string` node (Python's docstring convention —
  Tree-sitter exposes this directly as the body's first named child). If present, extract
  and clean it (strip quotes, dedent), then:
  - Compute the PEP-257 summary line (first line to first blank line/period, truncated to
    ~100-120 chars as a hard fallback) → store as `description`.
  - Store the full cleaned docstring text unmodified → store as `docstring_full`.
- Both go into the existing `properties` dict already passed to
  `GraphNode(..., properties={...})` for `Class`/`Function` nodes (and the `Module` node
  too, if the file has a module-level docstring — same first-statement-of-body check at
  the top of `extract_python_file`).
- No new Tree-sitter traversal needed — this rides on the exact same `_visit_class`/
  `_visit_function`/`extract_python_file` code paths that already exist; it's an
  additional field extraction inside functions the CALLS-extraction work already touches,
  not a new pass over the AST.

**Existing files to edit:**
- `devgraph/indexer/python/extractor.py`: `_visit_class`, `_visit_function`,
  `extract_python_file` (module-level docstring); a small shared helper (e.g.
  `_docstring_summary(full_text: str) -> str`) for the PEP-257 summary-line logic, since
  it's used identically in three call sites.
- No `GraphEngine`/schema change needed — `description` is already a property
  `search_component` reads; this just starts writing it, and `docstring_full` is a new
  property `get_source` (item 9) can read without any schema migration (properties are
  schemaless per-node in this codebase's `upsert_node` pattern).

**Verification**: re-run `devgraph rescan rag4` and confirm
`MATCH (n) WHERE n.description IS NOT NULL RETURN COUNT(*)` is no longer 0; confirm
`description` values are short summary lines (not full multi-paragraph text) and
`docstring_full` holds the complete original docstring; confirm
`search_component(engine, "rag4", "<a real docstring keyword from RAG4's source>")`
returns a match it wouldn't have matched by name alone.

---

## Item 9 — `get_source` MCP tool

Tree-sitter nodes already carry `start_point`/`end_point` (line/column) for free on every
node visited during extraction — no new parsing needed, only capturing values already
available at the exact point `_visit_class`/`_visit_function` already run.

**Confirmed scope**: covers both `Function` and `Class` nodes from the first pass (the
line-range capture applies identically to both node types), and includes `docstring_full`
(item 8) in its response when present.

**Implementation:**
- `devgraph/indexer/python/extractor.py`: in `_visit_class`/`_visit_function`, capture
  `node.start_point.row + 1` / `node.end_point.row + 1` (Tree-sitter rows are 0-indexed;
  +1 for human-readable line numbers) and store as `start_line`/`end_line` in the same
  `properties` dict `description` (item 8) is added to.
- `devgraph/mcp/tools.py`: new function `get_source(engine, repo_id, component_name,
  cross_repo=False) -> dict` — looks up the named `Function`/`Class` node's `file`
  property (already stored) plus the new `start_line`/`end_line`, reads that byte range
  from the actual file on disk (registry-scoped: resolve the repo's root path via
  `RepoRegistry.get(repo_id)`, same as every other file-touching operation in this
  codebase — never an arbitrary path), and returns the matched source text plus its
  file/line-range for citation. Also include the node's `docstring_full` property in the
  return dict when present — a caller who already used `get_source` to fetch code gets the
  full documentation alongside it in the same call, rather than needing a second lookup
  for text the indexer captured anyway.
- `devgraph/mcp/server.py`: register as a 17th `@server.tool()`, following the exact
  pattern every other tool in `build_server()` already uses.
- Since this tool needs filesystem access (not just graph queries), and the graph doesn't
  always reflect the current on-disk state (a component could have moved/been deleted
  since the last scan), document that `get_source` reads live from disk using the graph's
  *last-indexed* line numbers — a stale index could return the wrong lines. Recommend a
  `rescan` first if freshness matters, same caveat already documented for every other tool.

**Existing files to edit:**
- `devgraph/indexer/python/extractor.py`: `_visit_class`, `_visit_function` (capture
  start/end line, same functions item 8 touches).
- `devgraph/mcp/tools.py`: new `get_source` function.
- `devgraph/mcp/server.py`: register the new tool.
- `DEVGRAPH-CLIENT.md`: add `get_source` to the "## 4. Using the tools day-to-day" table
  ("Show me X's actual code" → `get_source`), with the staleness caveat above.

**Verification**: `get_source(engine, "rag4", "batch_retrieve_payloads")` should return
exactly that function's source text, matching a manual `Read` of the relevant line range
in the file where it's actually defined; confirm `docstring_full` is included in the
response when present, for both a Function and a Class.

---

## Item 10 — IMPORTS resolution gap: documented, not fixed in this pass

RAG4's `services/api_gateway/hybrid_search.py` (and `retrieval.py`, likely more repo-wide
per an in-code comment calling it "the same pattern as shared/utils/") uses bare
single-segment imports (`from sparse_encoder import get_sparse_encoder`) resolved at
runtime via a manual `sys.path.insert(0, _SHARED_UTILS_DIR)` a few lines earlier in the
same file. The actual file is at `shared/utils/sparse_encoder.py`.

**Why this is scoped as "document, don't fix" rather than folded into this pass as a
code change:**
- The current extractor's dotted-path guess (`_dotted_to_file_path`) only fires for
  dotted names by design — a bare single-segment name has no path structure to
  reinterpret at all. Resolving `sparse_encoder` → `shared/utils/sparse_encoder.py`
  requires **also parsing the `sys.path.insert(...)` call** earlier in the same file to
  learn what directory a bare-name import might resolve against, then correlating that
  against the actual repo layout on disk (not just AST-local information) — a materially
  different and more speculative kind of resolution than anything the extractor does
  today (which is purely syntactic, no cross-statement correlation, no filesystem lookups
  during parsing).
- This deserves a scoped, deliberate design pass of its own (how many `sys.path`
  manipulation styles to support, whether to attempt a filesystem-based fallback search,
  whether the risk of wrong matches — a bare name could genuinely collide with an
  unrelated same-named file elsewhere in a large repo — outweighs the benefit) rather than
  being squeezed into this pass as a rushed addition.
- The existing gap is **known, bounded, and doesn't regress anything** — RAG4's
  file-level and function-level graph data (`summarise_repository`, `find_callers`,
  `blame_component`, `explain_architecture`) all work correctly regardless of this
  specific import style not resolving; only `find_related_files`'s `imported_modules`
  output is incomplete for files using this pattern.

**What this plan does instead**: document the limitation precisely, so it's understood as
a known gap rather than re-discovered/re-diagnosed by a future tester.

**Existing files to edit:**
- `CLAUDE.md`: update the `indexer/python/` bullet's IMPORTS description to add: "Does not
  resolve bare single-segment imports satisfied via manual `sys.path` manipulation (a real
  pattern in at least one tested repo) — only dotted intra-repo imports and standard
  relative imports resolve to a same-repo Module node. Root-caused but intentionally not
  fixed; would require cross-statement correlation (parsing `sys.path.insert` calls) plus
  filesystem-based resolution, a different and riskier class of extraction than anything
  else this indexer does."
- `DEVGRAPH-CLIENT.md`: add one line to the known-gaps area near the tool-selection table:
  "`find_related_files`'s `imported_modules` can be sparse for repos that resolve local
  imports via manual `sys.path` manipulation rather than dotted package imports — a known,
  bounded gap, not a sign indexing failed."

No code changes for this item in this pass.

---

## Summary of all new/edited files

**New files:**
- `scripts/bootstrap.ps1` — one-command bootstrap (PowerShell; POSIX mirror deferred)
- `devgraph/cli/_env.py` — shared path/Podman/version-resolution helpers, used by
  `doctor` and `client-config`

**Edited files:**
- `devgraph/cli/main.py` — new `doctor` command; new `client-config` command; `add`/
  `rescan` gain `--full` flag; `status` gains tray-liveness section
- `devgraph/agent/tray.py` — heartbeat-file write in `_health_check_loop`
- `devgraph/indexer/python/extractor.py` — docstring extraction into `description`
  (PEP-257 summary line) and `docstring_full` (complete text)
  (`_visit_class`/`_visit_function`/`extract_python_file`); start/end line capture into
  `start_line`/`end_line` (same functions)
- `devgraph/mcp/tools.py` — new `get_source` function (returns source text +
  `docstring_full` when present)
- `devgraph/mcp/server.py` — register `get_source` as a 17th tool; optional docstring
  hints for the name-vs-path identifier nuance
- `deploy/podman-compose.yml` — header comment reframed as alternative/best-effort
- `README.md` — Setup section replaced with bootstrap-script pointer; stale status content
  corrected (test count, "not yet built" claims, indexing-workflow description); Using-it
  section updated for `--full`; Neo4j lifecycle language updated
- `CLAUDE.md` — Commands section updated to reference the bootstrap script; Podman-lifecycle
  language updated; IMPORTS-gap documentation added to the `indexer/python/` bullet
- `DEVGRAPH-CLIENT.md` — all hardcoded-path occurrences replaced with "run
  `devgraph client-config`"; step 1/2 updated for `--full`; new tool-selection
  identifier-type column/callout; `get_source` added to the tool table; IMPORTS-gap note
  added

**Not edited (explicitly, by decision):**
- `pyproject.toml` — no new console-script needed; every new command is a subcommand/flag
  of the existing `devgraph` entry point

---

## Verification plan (end-to-end, after implementation)

1. **Bootstrap**: on a clean checkout (or after `podman rm -f devgraph-neo4j` + deleting
   `.venv/`), run `.\scripts\bootstrap.ps1` start to finish; confirm it exits 0 and prints
   `devgraph doctor`'s output at the end with no failing checks.
2. **`doctor`**: run standalone; confirm it reports Python/mcp versions, a successful
   `build_server` import, Neo4j reachability, Podman container state, and tray liveness
   (expect "not running" if the tray app isn't started — that's a correct, not broken,
   result).
3. **`client-config`**: run from the DevGraph repo; confirm the printed `claude mcp add`
   line uses `sys.executable`'s actual resolved path, not a hardcoded string; if a working
   `claude` CLI is available, test `--run` against it (see Open Questions below — this is
   the one item that needs a real `claude` CLI to fully verify).
4. **`add --full`**: register a fresh repo with `--full`; confirm both the file-scan count
   and a new-commits-indexed count print; confirm `blame_component` returns real commit
   data afterward without a separate `index-history` call.
5. **Docstring extraction**: `devgraph rescan rag4`; confirm
   `MATCH (n) WHERE n.description IS NOT NULL RETURN COUNT(*)` is non-zero; confirm
   `description` values are short summary lines (not full multi-paragraph text) and
   `docstring_full` holds the complete original docstring; confirm `search_component` can
   find a component by a keyword that only appears in its docstring, not its name.
6. **`get_source`**: call it via the MCP tool (or directly via `devgraph.mcp.tools.
   get_source`) for a known function (e.g. `batch_retrieve_payloads`) and a known class;
   confirm the returned text matches a manual `Read` of that component's actual source
   lines, and confirm `docstring_full` is included in the response when present.
7. **Full test suite**: `.venv/Scripts/python -m pytest` — all existing 195 tests plus new
   tests for `doctor`/`client-config`/`--full`/docstring extraction/`get_source` must pass.
8. **Regression check on IMPORTS**: confirm the count is unchanged (still ~5 on RAG4) and
   that this is the expected, documented state per item 10 — not a new regression.

---

## Open question for whoever implements this

**`claude mcp add`'s actual CLI syntax (specifically whether/how it accepts a `cwd`) is
unconfirmed** — no environment with `claude` on PATH was available while writing this
plan, so the exact flags could not be verified against a real `claude mcp add --help`.
This blocks `client-config`'s `--run` flag specifically (the print-only default doesn't
need this confirmed, since it just states the requirement in prose the way
`DEVGRAPH-CLIENT.md` already does). Confirm the real invocation syntax against a working
`claude` CLI before implementing/testing `--run` — this can happen during implementation
rather than blocking the start of this work, since the print-only path (the default)
doesn't depend on it.
