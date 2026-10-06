# `devgraph config schema` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `devgraph config schema list/add/edit/delete/reset` for a repository's `devgraph.schema.yaml`, with comment-preserving edits and full validation before every write.

**Architecture:** Generalise #34's tools-file splicer (`devgraph/config/tools_edit.py`) into a list editor parameterised by top-level key and identity field, keeping the tools API as thin wrappers. Split the schema loader so text can be validated before writing. Add a `config schema` sub-group reusing `config tools`' scope, write and editor helpers.

**Tech Stack:** Python 3.13, PyYAML, Typer, pytest.

**Spec:** `docs/superpowers/specs/2026-10-04-config-schema-design.md`

**Working directory:** this worktree, branch `epic1/f3-config-schema` (stacked on `epic1/f2-global-tools`).

## Global Constraints

- `tools_edit`'s public functions and behaviour unchanged (all existing tests pass untouched).
- Node types identified by `label`, relationships by `type`; a relationship type declared more than once can't be edited/deleted by name.
- Every schema write is validated exactly as the indexer would: `parse_project_schema(text, path)` then `resolve_declaration(...)`; nothing invalid is written; writes atomic, mode kept.
- Default scope: deepest registered repository containing cwd, else cwd + warning (reuse `config tools`' helper).
- Commit messages: plain imperative; no `Co-Authored-By` trailer, no AI attribution. Never commit `uv.lock`, real names or personal paths.

## Review Focus

1. A schema file with both `node_types` and `relationships` lists and comments between them → editing one list never touches the other or its comments — pinned in Task 1.
2. Deleting a node type that a relationship still uses as an endpoint → refused by validation with a clear message, file unchanged — pinned in Task 2.
3. A relationship type declared twice (different endpoints) → `edit`/`delete` by name refused with "edit by hand" — pinned in Task 1/2.
4. `reset` on a repository whose graph has user types → message says the next rescan removes their nodes — pinned in Task 2.
5. A label equal to a relationship type → refused unless `--node-type`/`--relationship` — pinned in Task 2.

---

### Task 1: Generalised list editor and schema text validation

**Files:**
- Create: `devgraph/config/list_edit.py` (the generic splicer moved from `tools_edit.py`)
- Modify: `devgraph/config/tools_edit.py` (thin wrappers: same public names and behaviour), `devgraph/config/project_schema.py` (split `parse_project_schema(text, path)` out of `load_project_schema`, as `parse_project_tools` was split in #34)
- Test: `tests/config/test_list_edit.py` (create), existing `tests/config/test_tools_edit.py` unchanged, `tests/config/test_project_schema*.py` (append a parse test)

**Interfaces (produces):**
- `list_edit.ListEditError(Exception)` (`tools_edit.ToolsEditError` becomes a subclass or alias so existing `except ToolsEditError` keeps working).
- `entries(text, *, key) -> list[dict]`; `add_entry_text(text, entry, *, key, ident, version) -> str`; `replace_entry_text(text, name, entry, *, key, ident) -> str`; `delete_entry_text(text, name, *, key, ident) -> str`; `dump_entry(entry) -> str`. Same splicing rules, comment/anchor/keep-chomp/CRLF/BOM handling and refusals as today; a name matching more than one entry raises `ListEditError("... is declared N times; edit the file by hand")`; adding to a document that lacks `key` appends `key:` at the end of the document; a new document starts with `version: <version>`.
- `project_schema.parse_project_schema(text: str, path: Path) -> ProjectSchema` (raises `ProjectSchemaError`, same messages as the loader).

- [ ] **Step 1: Failing tests** for `list_edit` with a schema document containing a header comment, `node_types` (two entries, comments between), a comment line, `relationships` (two entries, one type declared twice with different `to`), and a trailing comment: add/replace/delete a node type leaves the `relationships` block and all comments outside the entry byte-identical; add a relationship to a doc with no `relationships` key appends it at the end; duplicate-type relationship → `ListEditError` on replace/delete by that type; results parse with `parse_project_schema`. A `parse_project_schema` test mirrors an existing loader test (same error text for a malformed file).
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement.** Move `_Doc` and helpers into `list_edit.py` parameterised by `key`/`ident`; `tools_edit` functions call them with `key="tools", ident="name", version=TOOLS_VERSION`. Keep messages that existing tests assert (adapt only where they mention "tool" generically — prefer passing an `noun="tool"` parameter so tools messages are unchanged).
- [ ] **Step 4: Run** `uv run pytest tests/config -q` and `uv run pytest -q`.
- [ ] **Step 5: Commit** `"Generalise the comment-preserving editor to any config list"`.

---

### Task 2: `devgraph config schema`

**Files:**
- Modify: `devgraph/cli/main.py`
- Test: `tests/cli/test_config_schema_cli.py` (create)

- [ ] **Step 1: Failing tests** (CliRunner; registered repos via the existing registry fixtures and the autouse isolation):
  - `list --repo R` shows built-in and project entries with origin; `--json` shape `{"node_types": [{"label","origin","key"}], "relationships": [{"type","from","to","provider","origin"}]}`; disabled repo says so.
  - `add --from nt.yaml` (node type with `label`) and a relationship (`type`) — appended, comments preserved, file valid; existing label → exit 1 "use `devgraph config schema edit`"; built-in label (e.g. `Function`) → exit 1 and file unchanged; key not in metadata → exit 1, unchanged; a mapping with neither/both `label` and `type` → exit 1.
  - `edit Label --from new.yaml`; `edit` via `$EDITOR` (monkeypatch `click.edit`); unchanged → "No changes."; invalid → exit 1, unchanged; duplicated relationship type → exit 1 "edit the file by hand".
  - `delete Label` → removed, output warns the next rescan deletes that type's nodes; deleting a node type used as a relationship endpoint → exit 1 (validation), unchanged.
  - Ambiguous name (label == relationship type) → exit 1 mentioning `--node-type`/`--relationship`; with the flag → works.
  - `reset --yes` deletes the file and warns about removed types; without `--yes` and input `n` → aborted.
  - Effect notes: registered+enabled → "next rescan … `devgraph rescan <id> --now`"; disabled → "not applied while the project config is disabled"; unregistered cwd → warning.
  - Default scope from a subdirectory of a registered repo edits the repo root's file.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** `schema_app` under `config_app` as `schema`, reusing `config tools`' scope helper, atomic write helper and `$EDITOR` flow; validate with `parse_project_schema` + `resolve_declaration` before writing.
- [ ] **Step 4: Run** `uv run pytest tests/cli -q` and `uv run pytest -q`.
- [ ] **Step 5: Commit** `"Add devgraph config schema commands"`.

---

### Task 3: Docs and a manual check

- [ ] **Step 1: Docs.** README config CLI section: `config schema` commands, when edits apply, that `delete`/`reset` remove nodes at the next rescan; PROJECT_STATUS: shipped bullet and open items.
- [ ] **Step 2: Full suite** `uv run pytest -q`.
- [ ] **Step 3: Manual check** (scratchpad; throwaway registry; live Neo4j): repo with a commented schema declaring a filesystem folder type; `devgraph add`; `config schema add` a second filesystem type via `--from`; `devgraph rescan <id> --now`; count its nodes; `config schema delete` it; rescan; nodes gone; show the file diff keeping comments. Clean up.
- [ ] **Step 4: Commit** `"Document devgraph config schema"`.
