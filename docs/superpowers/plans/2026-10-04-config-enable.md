# Project Config Switch and Drift Reporting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `devgraph config enable|disable <repo_id>` turns a repository's project config files on/off (default on); disabled behaves exactly like no files. `devgraph doctor` reports schema drift.

**Architecture:** A registry column stores the switch. A new dependency-light `devgraph/config/project_switch.py` looks it up by repository path (read-only SQLite) so path-based loaders can honour it. It is consulted in `load_project_schema`, `schema_file_hash` and the MCP tool plane's `tools_fingerprint`; existing rescan/reload machinery handles transitions.

**Tech Stack:** Python 3.13, sqlite3, Typer, pytest, live Neo4j for the rescan/drift tests.

**Spec:** `docs/superpowers/specs/2026-10-04-config-enable-design.md`

**Working directory:** this worktree, branch `epic1/f1-config-enable` (stacked on `epic1/f-base`). Live tests use Neo4j at `bolt://127.0.0.1:7687` (`neo4j` / `devgraph-local-dev`) and must run, not skip.

## Global Constraints

- Column `project_config_enabled INTEGER NOT NULL DEFAULT 1`, added by the existing `_MIGRATIONS` pattern; `RepoRecord.project_config_enabled: bool = True`; `RepoRegistry.set_project_config_enabled(repo_id, enabled)` via `_set_flag`.
- `project_config_enabled(repo_root) -> bool`: True unless a registered repository whose resolved path equals `Path(repo_root).resolve()` has the flag 0. Read-only (`sqlite3.connect(f"file:{db}?mode=ro", uri=True)`); a missing DB, table or column → True. Never creates or migrates the DB. The DB path comes from `get_settings().registry_db_path` via a module-level `_registry_db_path()` that tests monkeypatch — tests must never read the real user registry.
- Disabled: `load_project_schema(root)` → `None` (unless `respect_switch=False`); `schema_file_hash(root)` → `ABSENT_SCHEMA_HASH`; tool plane `tools_fingerprint(root)` → `"disabled"` → no project tools + notice `project config is disabled for repository '<id>'; enable it with 'devgraph config enable <id>'`.
- `config validate` and `config show` read files with `respect_switch=False` and report the switch.
- Commit messages: plain imperative; no `Co-Authored-By` trailer, no AI attribution. Never commit `uv.lock`, real names or personal paths.

## Review Focus

1. Tests or CLI accidentally reading/migrating the user's real registry → the lookup is read-only and tests monkeypatch `_registry_db_path` — pinned in Task 1.
2. A repository registered by a path with a trailing slash/symlink → lookup compares resolved paths — pinned in Task 1.
3. Disabling a repo whose graph has user types → next rescan removes them; enabling restores them — pinned in Task 2 (live).
4. A running MCP session when the switch flips → tools vanish/return within one poll, with the notice — pinned in Task 2.
5. `doctor` when Neo4j is down → drift check skipped with a note, doctor still completes — pinned in Task 3.

---

### Task 1: Registry switch and path lookup; loaders honour it

**Files:**
- Modify: `devgraph/registry/store.py` (column, migration, record field, `_COLUMNS`, `_row_to_record`, setter)
- Create: `devgraph/config/project_switch.py`
- Modify: `devgraph/config/project_schema.py` (`load_project_schema(repo_root, *, respect_switch=True)`, `schema_file_hash`)
- Test: `tests/registry/test_store.py` (append; find the existing registry test file by `ls tests/registry`), `tests/config/test_project_switch.py` (create)

- [ ] **Step 1: Failing tests.** Registry: a new registry has `project_config_enabled` True for an added repo; `set_project_config_enabled(id, False)` persists across reopen; an existing pre-migration DB (create the old table without the column with raw sqlite, then open `RepoRegistry`) gains the column with default True. Lookup (`tests/config/test_project_switch.py`), with `monkeypatch.setattr(project_switch, "_registry_db_path", lambda: tmp_path / "r.sqlite3")`:

```python
def test_unregistered_and_missing_db_are_enabled(tmp_path, monkeypatch): ...
def test_a_disabled_repo_is_disabled_by_any_spelling_of_its_path(tmp_path, monkeypatch):
    # register tmp_path/"repo" via RepoRegistry, disable it, then check
    # project_config_enabled(tmp_path/"repo"), (tmp_path/"repo/"), a symlink to it -> all False
def test_the_lookup_never_creates_a_database(tmp_path, monkeypatch):
    # db path does not exist -> True, and it still does not exist afterwards
def test_load_project_schema_and_hash_honour_the_switch(tmp_path, monkeypatch):
    # repo with a valid devgraph.schema.yaml (copy a minimal valid one from tests/config fixtures)
    # disabled -> load_project_schema(root) is None, load_project_schema(root, respect_switch=False) is not None,
    #             schema_file_hash(root) == ABSENT_SCHEMA_HASH; enabled -> sha256:...
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement.** `project_switch.py`:

```python
"""Whether a repository's project config files are switched on (`devgraph config enable|disable`).

Looked up by path because the schema and tools loaders only know a path. Reads
the registry read-only and never creates it; anything unknown is "enabled".
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _registry_db_path() -> Path:
    from devgraph.config.settings import get_settings

    return get_settings().registry_db_path


def project_config_enabled(repo_root: Path | str) -> bool:
    db = _registry_db_path()
    if not db.exists():
        return True
    target = Path(repo_root).expanduser().resolve()
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return True
    try:
        rows = conn.execute("SELECT path, project_config_enabled FROM repos").fetchall()
    except sqlite3.Error:  # no table or no column yet
        return True
    finally:
        conn.close()
    for path, enabled in rows:
        try:
            if Path(path).expanduser().resolve() == target:
                return bool(enabled)
        except OSError:
            continue
    return True
```

  In `project_schema.py`: `load_project_schema(repo_root, *, respect_switch: bool = True)` returns `None` first thing when `respect_switch and not project_config_enabled(repo_root)` (docstring: "or when the repository's project config is switched off"); `schema_file_hash` returns `ABSENT_SCHEMA_HASH` first thing when switched off (docstring likewise). Import lazily or at top — `project_switch` must not import `project_schema`.
- [ ] **Step 4: Run** `uv run pytest tests/registry tests/config -q` → pass.
- [ ] **Step 5: Commit** `"Add a per-repository project config switch"`.

---

### Task 2: Behaviour while switched off (indexer, rescan, tool plane)

**Files:**
- Modify: `devgraph/mcp/tool_plane.py` (`tools_fingerprint`, `_serve_repository`)
- Test: `tests/indexer/test_schema_apply_live.py` (append; mirror its fixtures), `tests/mcp/test_tool_reload.py` (append)

- [ ] **Step 1: Failing tests.**
  - Live (mirror the existing live schema-apply tests' setup, registry path monkeypatched as in Task 1): a repo with a schema declaring a filesystem-provider type is full-scanned (user nodes exist, `schema_pending` False); disable it → `schema_pending` True; `full_scan` → user nodes gone, applied hash `absent`; enable → pending → `full_scan` → user nodes back.
  - Tool plane: a scoped server with ONE served; disable the repo (registry write + monkeypatched `_registry_db_path`) → `reload_if_changed()` True, no project tools, notice contains `devgraph config enable demo`; enable → `reload_if_changed()` True, tools back, notice gone. (The test `build` helper already creates the repo; register it in a tmp registry with `RepoRegistry(tmp_path/"r.sqlite3").add_repo(repo, repo_id="demo")`.)
- [ ] **Step 2: Run → fail** (the live schema test may already pass through Task 1 — if so, note it in the report and keep it as the pin; the tool plane test must fail).
- [ ] **Step 3: Implement.** `tools_fingerprint`: after the root check, `if not project_config_enabled(repo_path): return "disabled"`. `_serve_repository`: `if fingerprint == "disabled": status.notices.append(f"project config is disabled for repository {repo.repo_id!r}; enable it with 'devgraph config enable {repo.repo_id}'"); return`. Also treat `"disabled"` like `"absent"`/`"root-missing"` in `reload_if_changed`'s keep-last-good guard (switching off must drop the tools, not keep them).
- [ ] **Step 4: Run** `uv run pytest tests/mcp tests/indexer/test_schema_apply_live.py -q -rs` → pass, live tests not skipped.
- [ ] **Step 5: Commit** `"Honour the project config switch in rescans and the tool plane"`.

---

### Task 3: CLI and reporting

**Files:**
- Modify: `devgraph/cli/main.py`
- Test: `tests/cli/test_config_cli.py` (append), `tests/cli/test_cli.py` (doctor/list, append where those are tested)

- [ ] **Step 1: Failing tests** (mirror the existing config CLI tests' registry fixture — e.g. `temp_registry_db` — and also monkeypatch `devgraph.config.project_switch._registry_db_path` to the same DB):
  - `config disable demo` → exit 0, output says the project config is disabled, mentions `devgraph rescan demo --now` and that MCP sessions pick it up within 2 s; registry flag False. Again → exit 0 "already disabled". `config enable demo` mirrors. Unknown id → exit 1 naming it.
  - `config validate --repo <root>` of a disabled repo with a valid schema → still `valid`, plus a line saying the project config is disabled; `config show --repo <root>` says disabled and lists only built-in node types; `--json` has `"project_config": "disabled"`.
  - `devgraph list` shows the switch for a disabled repo (find how `list` renders flags; add a column or marker consistently).
  - `doctor`: "Project schemas" section marks a disabled repo; a new drift line per active repo — with a stub/monkeypatched engine whose `read_applied_schema` returns `{"hash": "sha256:old", ...}` while the file hashes differently → `pending` warning naming `devgraph rescan <id> --now`; same hash → `applied`; `None` with a schema file → `never applied`; Neo4j unreachable → drift skipped with a note (find how doctor already handles Neo4j being down).
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** `@config_app.command("enable")` / `("disable")` taking `repo_id: str` (help text per the spec), using `_get_registry()` and `set_project_config_enabled`; validate/show/list/doctor reporting and the doctor drift check (use `schema_file_hash` + `engine.read_applied_schema`; mirror `schema_pending`'s rule for `None`). Update the `config` group help text to mention enable/disable.
- [ ] **Step 4: Run** `uv run pytest tests/cli -q` and `uv run pytest -q` → pass.
- [ ] **Step 5: Commit** `"Add config enable/disable and schema drift to doctor"`.

---

### Task 4: Docs and a manual check

- [ ] **Step 1: Docs.** README: `devgraph config enable|disable <repo_id>` in the config CLI section (what it does, when changes apply), doctor drift in the doctor description; PROJECT_STATUS: shipped bullet, and remove "doctor drift reporting" from the open items in the schema rescan bullet; DEVGRAPH-CLIENT.md: one line under project tools that a disabled repository serves none (see `devgraph://project-tools`).
- [ ] **Step 2: Full suite** `uv run pytest -q`.
- [ ] **Step 3: Manual check** (scratchpad, throwaway registry via `DEVGRAPH_REGISTRY_DB_PATH`): git repo with a schema declaring a filesystem-provider folder type; `devgraph add`; `devgraph config disable <id>`; `devgraph doctor` shows pending; `devgraph rescan <id> --now`; doctor shows applied and the user nodes are gone (`devgraph` query or a Cypher count); enable; rescan; nodes back. Record output; `devgraph remove`; delete scratch.
- [ ] **Step 4: Commit** `"Document the project config switch and drift reporting"`.
