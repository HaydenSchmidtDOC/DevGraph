# DevGraph — Implementation Plan #4: One-Command Install & Interactive Client Registration

Build-ready plan for collapsing today's multi-step install/registration
sequence into a single public-GitHub install command that ends in an
interactive, Claude-Code-style console menu.

## Context

Today's install/run cycle is already largely automated but split across
disconnected pieces the developer has to know exist:

1. Prerequisites (Python 3.13+, Git, Podman) installed manually — nothing
   auto-installs these, and this plan does not change that.
2. Clone the repo, run `scripts/bootstrap.ps1`, which handles venv creation,
   editable install, the `devgraph-neo4j` container (create-or-reuse-or-start),
   a Bolt-reachability poll, schema init, and a final `devgraph doctor` check.
   It then *prints* (does not run) two next steps.
3. Developer separately runs `devgraph client-config --run`, which today only
   registers with Claude Code (`claude mcp add devgraph -- <venv-python> -m
   devgraph.mcp.server`). There is no VS Code registration path.
4. Developer separately runs `devgraph add <repo>` per repo to index — this
   stays manual by design (explicit-registration-only, see `CLAUDE.md` /
   `README.md` core principles) and is out of scope for this plan.

Decisions already made in this pass (do not re-litigate without a reason):

- **Neo4j/Podman stays.** Evaluated and rejected switching to an embedded
  graph DB (Kuzu): not org-approved, and embedded engines don't support the
  concurrent multi-process access DevGraph's multi-client MCP model depends
  on (today, each Claude Code / VS Code window is its own OS process talking
  to one shared Neo4j server over Bolt — an embedded single-writer file store
  can't serve that). No architecture change here, only install-experience
  changes.
- **No auto-install of system software.** Podman is detected, not installed.
  If missing, point the developer at the company portal and stop — matches
  `bootstrap.ps1`'s existing rule and the org's software-approval process.
- **`devgraph add <repo>` stays manual.** Explicit registration is a stated
  design principle (`README.md` "Core design principles"), not friction to
  remove.
- **This plan wraps existing automation, it does not replace it.**
  `bootstrap.ps1`'s internals (venv/install/container/schema/doctor) are
  reused as-is; this plan adds a distribution entry point, an interactive
  menu shell around it, and a second registration target (VS Code).

---

## Item 1 — GitHub-based install entry point

**Files:** new `scripts/install.ps1` (or equivalent top-level entry script —
exact name TBD at implementation time), `README.md`.

### Design

Add a single documented command in `README.md`'s Quickstart section that a
developer can run with nothing pre-cloned — the standard pattern for a
PowerShell-based GitHub installer is a one-liner that fetches and executes a
bootstrap script via `irm | iex` (Invoke-RestMethod piped to
Invoke-Expression) against the raw GitHub URL of `scripts/install.ps1`.

`install.ps1` itself:

1. If not already inside a DevGraph checkout, clones the repo (default
   target: `$env:USERPROFILE\devgraph`, override via a `-Path` param) via
   `git clone`.
2. `cd`s into the checkout and hands off to Item 2's interactive menu.

Keep `bootstrap.ps1` as the underlying automation — `install.ps1` is a thin
distribution wrapper, not a rewrite. Existing developers who already have a
local clone can keep using `bootstrap.ps1` directly exactly as today; nothing
about Item 1 breaks that path.

**Verification:** run the documented one-liner against a clean temp
directory (no prior clone, no `.venv`) on a machine with Podman already
installed; confirm it reaches the Item 2 menu without manual intervention
beyond the initial command.

---

## Item 2 — Interactive console menu

**Files:** new `devgraph/cli/setup_menu.py` (or `scripts/`-resident
PowerShell menu — language choice below), wired as the target of
`install.ps1` and optionally exposed as `devgraph setup` for re-runs on an
existing checkout.

### Design choice: PowerShell menu, not a new Python CLI command

Prefer implementing the interactive menu itself in PowerShell
(`scripts/setup-menu.ps1`), invoked by `install.ps1` after clone, rather than
a new `devgraph setup` Typer command — because the menu's first two steps
(Podman detection, venv bootstrap) must run *before* a venv exists to run
Python code in, so the natural home for the outer shell is PowerShell (which
`bootstrap.ps1` already is). Once the venv exists, later menu steps shell out
to `$venvPython -m devgraph.cli.main ...` exactly as `bootstrap.ps1` already
does for `doctor`. This avoids a chicken-and-egg dependency (Python code
needing the venv it's supposed to help create) and keeps one PowerShell
entry point rather than splitting the flow across two languages
mid-sequence.

### Menu flow

1. **Podman detection** (reuse `bootstrap.ps1`'s existing resolution logic:
   PATH first, then `%LOCALAPPDATA%\Programs\Podman\podman.exe` fallback).
   - Found: proceed silently.
   - Missing: print a message pointing to the company software portal (exact
     portal name/link supplied by the user at implementation time — do not
     hardcode a guess), then exit non-zero. No install attempt, per the
     "decisions already made" note above.
2. **Run existing bootstrap automation.** Invoke the same steps
   `bootstrap.ps1` already performs (venv create-or-reuse, editable install,
   container create-or-start, Bolt health-wait, schema init, `devgraph
   doctor`) — refactor `bootstrap.ps1`'s steps 3-8 into a reusable function/
   script (e.g. `scripts/_bootstrap-core.ps1`, dot-sourced by both
   `bootstrap.ps1` and the new menu script) so the logic exists in exactly
   one place rather than being duplicated between the two entry points.
   `bootstrap.ps1` itself becomes a thin wrapper that dot-sources the shared
   core and stops (unchanged external behavior for existing users who invoke
   it directly, including its current printed next-steps message when run
   standalone).
3. **Detect installed, MCP-compatible AI CLIs.** Check for:
   - `claude` on PATH (Claude Code)
   - VS Code: `code` on PATH, or presence of `%APPDATA%\Code\User`
   - Any other CLI confirmed MCP-stdio-compatible at implementation time —
     keep the detection list small and explicit rather than guessing at
     tools that haven't been verified to support MCP registration the same
     way; extending the list later is cheap.
   If none detected, print a manual-registration fallback (the existing
   `client-config` printed-instructions output) and exit 0 — don't treat
   "no supported client found" as an error, since bootstrap itself still
   succeeded.
4. **Present a multi-select menu** of detected clients (simple numbered
   toggle-list prompt, consistent with the plain-text style already used by
   `bootstrap.ps1`'s `Write-Step`/`Write-Ok` — no new TUI dependency
   introduced for this).
5. **Register against each selected target** — see Item 3 for the
   per-client mechanics. Report success/failure per target rather than
   failing the whole run if one registration fails (e.g. VS Code installed
   but its config directory is unexpectedly unwritable shouldn't block
   Claude Code registration succeeding).
6. **Final message**: reuses `bootstrap.ps1`'s existing "Next steps" block,
   minus the now-automated `client-config` line, keeping only:
   `devgraph add <path-to-a-git-repo>`.

**Re-run safety:** the whole menu must be safe to run again on an existing
checkout (e.g. `devgraph setup` alias, or re-running `install.ps1` in a
directory that's already a clone) — detection steps should recognize
"already registered" and "already running" states without erroring or
duplicating entries (ties into Item 3's idempotency requirement).

**Verification:** run end-to-end on a clean machine state (no `.venv`, no
container, no prior MCP registration, both Claude Code and VS Code present)
and confirm: Podman detected, bootstrap completes, both clients detected and
offered, selecting both registers both, and a Claude Code (or VS Code) query
against a `devgraph`-registered tool succeeds afterward. Then re-run the
whole flow a second time on the same machine and confirm no duplicate
registrations and no errors from already-existing state.

---

## Item 3 — VS Code MCP registration support

**Files:** `devgraph/cli/main.py` (`client_config` command).

### Design

Add a `--target claude|vscode|both` option to the existing `client-config`
command (default `both`), reusing the already-computed `python_path` and
`repo_root`.

- `claude` target: unchanged existing behavior (`claude mcp add devgraph --
  ...`, or print-only without `--run`).
- `vscode` target: locate the user-level MCP config file
  (`%APPDATA%\Code\User\mcp.json` on Windows — confirm exact path/schema
  against current VS Code MCP documentation at implementation time, since
  this is a newer, evolving VS Code feature). Read the file if it exists
  (empty/default structure if not), upsert a `devgraph` entry under its
  servers map with `{"command": python_path, "args": ["-m",
  "devgraph.mcp.server"], "cwd": repo_root}`, write back. **Merge, never
  overwrite** — preserve any other server entries already present.
- `both`: run both, reporting each independently (matches Item 2's
  per-target success/failure reporting).

**Idempotency:** re-running the upsert with the same `repo_root` should be a
no-op diff (same values written); re-running `claude mcp add` for an
already-registered `devgraph` name should not error the whole flow — check
existing registration first (e.g. `claude mcp list` / equivalent) and skip
with a clear "already registered" message rather than letting a duplicate-
add error surface as a failure.

**Verification:** extend whatever test coverage exists for `client_config`
(add if none exists today) with a `--target vscode` case asserting: (a) a
missing `mcp.json` is created with a valid minimal structure, (b) an
existing `mcp.json` with unrelated server entries keeps those entries and
gains/updates only the `devgraph` key, (c) running it twice produces an
identical file (idempotent). Manually verify against a real VS Code install
that the registered server actually appears and connects in VS Code's MCP
view.

---

## Sequencing

1. Item 3 (VS Code registration) — independent, needed as a dependency of
   Item 2's registration step; build and verify first.
2. Item 1 (GitHub install entry point) — independent of Item 3, can be built
   in parallel.
3. Item 2 (interactive menu) — depends on both: needs Item 3's VS Code
   registration to offer as a menu option, and is invoked by Item 1's
   install script.

## Out of scope (explicit)

- Any change to the Neo4j/Podman architecture itself, or to the tray/
  watcher's existing auto-lifecycle (refcounted holder start/stop) — already
  works, not touched by this plan.
- Auto-installing Podman, Python, Git, or VS Code — detection and a
  company-portal pointer only, per org software-approval constraints.
- Automating `devgraph add <repo>` — stays a deliberate manual step per the
  explicit-registration-only design principle.
- Support for AI CLIs beyond Claude Code and VS Code until a specific
  additional client is confirmed MCP-stdio-compatible — the detection list
  in Item 2 is intentionally left extensible rather than guessed at now.
- Non-Windows install paths (macOS/Linux shell equivalents of
  `install.ps1`/`setup-menu.ps1`) — this plan is scoped to the PowerShell
  path matching the existing `bootstrap.ps1`; a POSIX-shell mirror is a
  future pass if/when non-Windows developers need it.
