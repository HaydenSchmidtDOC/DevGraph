# Global Tools, `config tools` and Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A global tools store, `devgraph config tools list/add/edit/delete/reset` for both scopes, and MCP resolution built-in > project > global with the epic's notices.

**Architecture:** `devgraph/config/global_tools.py` stores global tools as JSON beside the registry. `devgraph/config/tools_edit.py` edits a tools document: text splicing for `devgraph.tools.yaml` (comments preserved), plain JSON for the global store. The MCP tool plane resolves both layers, polls both files and attaches per-tool notices. The CLI gains a `config tools` sub-group.

**Tech Stack:** Python 3.13, PyYAML (`compose` for node line ranges, `safe_dump` with a literal-block representer), Typer, mcp 2.3.0, pytest.

**Spec:** `docs/superpowers/specs/2026-10-04-global-tools-design.md`

**Working directory:** this worktree, branch `epic1/f2-global-tools` (stacked on `epic1/f1-config-enable`).

## Global Constraints

- Global store: `registry_db_path.parent / "global-tools.json"`, content `{"version": 1, "tools": [...]}`, validated with the tools-file loader (`parse_project_tools`), written atomically (temp file in the same directory + `os.replace`). Tests never touch the real `~/.devgraph`: extend the autouse fixture in `tests/conftest.py` to point the global store at a missing path under `tmp_path`.
- Precedence: built-in (locked) > project > global. Global tools are served only when the session has a repository; never hidden by `config disable`.
- Notices (exact prefixes): project overriding global → envelope `notices: ["resolved: project override of global tool '<name>'"]`; global used because the project tool couldn't be served → `notices: ["used global tool '<name>': <reason>"]`; built-in name in either layer → status notice `ignored: <global|project> tool '<name>' has the name of a built-in tool; using the built-in`.
- Envelope `notices` key only present when non-empty (D1 convention).
- Invalid global file: keep last good (as the project file); invalid at startup → none + notice.
- CLI writes validate the whole resulting document first; never write an invalid file; never stage/commit.
- Commit messages: plain imperative; no `Co-Authored-By` trailer, no AI attribution. Never commit `uv.lock`, real names or personal paths.

## Review Focus

1. A project file with comments between and inside tools → `add`/`edit`/`delete` leave every comment outside the edited tool byte-identical — pinned in Task 1.
2. `tools: [...]` written as a flow list, or a file with Windows line endings / no trailing newline → refused clearly or handled, never corrupted — pinned in Task 1.
3. A project tool named like a global one that fails to register → the global is served with the `used global tool` notice; never "no such tool" — pinned in Task 2.
4. Editing the global store while a session runs → `tools/list_changed` and the new set within one poll — pinned in Task 2.
5. `config tools reset` without `--yes` in a non-interactive shell → aborts without deleting — pinned in Task 3.

---

### Task 1: Global store and tools-document editing

**Files:**
- Create: `devgraph/config/global_tools.py`, `devgraph/config/tools_edit.py`
- Modify: `tests/conftest.py` (autouse: global store path)
- Test: `tests/config/test_global_tools.py`, `tests/config/test_tools_edit.py` (create)

**Interfaces (produces):**
- `global_tools.GLOBAL_TOOLS_FILENAME = "global-tools.json"`; `global_tools_path() -> Path` (via a module-level `_default_path()` reading settings, which tests monkeypatch); `load_global_tools(path: Path | None = None) -> ProjectTools | None` (None iff absent; `ProjectToolsError` if invalid); `global_tools_fingerprint(path=None) -> bytes | str` (bytes / `"absent"` / `"unreadable:<Exc>"`); `save_global_tools(tool_mappings: list[dict], path=None) -> None` (validates, atomic write, creates the directory).
- `tools_edit.ToolsEditError(Exception)`; `tool_mappings(text: str) -> list[dict]` (the raw tool mappings of a tools document, `[]` for empty/absent `tools`); `add_tool_text(text: str, tool: dict) -> str`; `replace_tool_text(text: str, name: str, tool: dict) -> str`; `delete_tool_text(text: str, name: str) -> str`; `dump_tool(tool: dict) -> str` (YAML for one tool mapping, multi-line strings as `|` blocks, key order kept). `text` may be `""` (no file): `add_tool_text("", t)` returns `"version: 1\ntools:\n- ...\n"`. Each function raises `ToolsEditError` with a clear message for: unknown name (replace/delete), duplicate name (add), non-mapping root, `tools` not a sequence, a non-empty flow-style `tools` list ("edit it by hand or rewrite it as a block list"). An empty flow list `tools: []` is turned into a block list on add. Line endings: preserve `\r\n` if the input uses it; always end with a newline. The functions do not validate tool semantics — callers validate the result with `parse_project_tools`.

- [ ] **Step 1: Failing tests.** Global store: absent → None / `"absent"`; save then load round-trips; invalid content → `ProjectToolsError`; save of an invalid tool raises and leaves the existing file untouched; save is atomic (monkeypatch `os.replace` to raise → original file intact, no temp file left). Editing (use a fixture document with a header comment, a comment between two tools, a comment inside the second tool, a trailing comment):

```python
DOC = """\
# Project tools for demo
version: 1
tools:
  # finds files
  - name: list_files
    description: List files.
    cypher: |
      MATCH (f:File {repo_id: $repo_id})
      RETURN f.path AS path
  # counts them
  - name: count_files  # inline note
    description: Count files.
    cypher: MATCH (f:File {repo_id: $repo_id}) RETURN count(f) AS n
# end of file
"""
```

  Tests: `tool_mappings(DOC)` names; `add_tool_text` appends after `count_files` with the same `  - ` indentation and every original line still present in order; `delete_tool_text(DOC, "list_files")` removes exactly that tool's lines and keeps `# Project tools for demo`, `# counts them`, `# end of file`; `replace_tool_text` swaps `list_files` and keeps the other comments; results parse with `parse_project_tools` (import from `devgraph.config.project_tools`) — add `$repo_id`-valid tools in tests; flow list `tools: [{name: a, ...}]` → `ToolsEditError`; `tools: []` + add → block list; `""` + add → new document; CRLF input stays CRLF; unknown/duplicate names → `ToolsEditError`; `dump_tool` renders a multi-line `cypher` as a `|` block.

  How to find ranges: `node = yaml.compose(text)`; the `tools` value is a `SequenceNode`; each item's first line is `item.start_mark.line` (0-based; the `- ` is on that line); item *i* spans `[start(i), start(i+1))`, the last item spans `[start(last), end)` where `end = seq.end_mark.line + (1 if seq.end_mark.column > 0 else 0)` — then trim trailing lines of that range that are blank or start (after whitespace) with `#` **at a column less than or equal to the item's dash column**, so a following top-level comment (like `# end of file`) is not swallowed. The dash column is `item.start_mark.column - 2`. New items are rendered as `" " * dash_col + "- " + first line`, following lines indented by `dash_col + 2`.

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** the two modules to the interfaces above (module docstrings explain why text splicing: comments matter in a committed, hand-edited file). `dump_tool` uses a `yaml.SafeDumper` subclass with a `str` representer choosing `style="|"` for strings containing `\n`, `sort_keys=False`, `default_flow_style=False`, `allow_unicode=True`.
- [ ] **Step 4: Run** `uv run pytest tests/config -q`.
- [ ] **Step 5: Commit** `"Add the global tools store and comment-preserving tools file edits"`.

---

### Task 2: Resolution in the MCP tool plane

**Files:**
- Modify: `devgraph/mcp/tool_plane.py`, `devgraph/mcp/server.py` (status resource / catalog wiring if needed)
- Test: `tests/mcp/test_tool_resolution.py` (create; reuse helpers from `tests/mcp/test_tool_reload.py` by copying the small stubs)

**Interfaces:**
- Consumes: `global_tools_fingerprint`, `global_tools_path`, `GLOBAL_TOOLS_FILENAME` (Task 1); existing `tools_fingerprint`, `_parse_fingerprint`, `_serve_repository`, `ProjectToolPlane`, `make_tool_function`, `ToolPlaneStatus`.
- Produces: `ProjectToolPlane.reload_if_changed()` also reloads when the global store changes; `ToolPlaneStatus.to_dict()` gains `global_tools_file` (path or None) and `origins: {name: "global" | "project" | "project (overrides global)"}`; `make_tool_function(tool, engine, repo_id, notices: list[str] | None = None)` puts `notices` in the envelope when non-empty.

- [ ] **Step 1: Failing tests** (stub engine; point the global store at `tmp_path` with the autouse/monkeypatch; write it with `save_global_tools`):
  - Scoped session, global `g_count` only → served, origin `global`.
  - Unscoped session → no global tools, status notice saying global tools need a repository.
  - Project and global both define `list_files` → project's definition served (check description), origin `project (overrides global)`, a call's envelope has `notices == ["resolved: project override of global tool 'list_files'"]`.
  - Project tool `list_files` fails to register (monkeypatch `make_tool_function` or `add_tool` to raise for the project definition only) while a global `list_files` exists → global served, envelope notice starts `used global tool 'list_files':`.
  - Project file invalid at startup + global `list_files` → global served with `used global tool` notice naming the invalid file.
  - Global tool with a built-in name → not served, status notice `ignored: global tool 'search_component' ...`.
  - `config disable` of the repo (registry + `_registry_db_path` monkeypatch as in `test_tool_reload.py`) → project tools gone, global tools still served.
  - Reload: change the global store → `reload_if_changed()` True and the new set listed; invalid global store after a good one → last good global tools kept with a notice; removing the global store → global tools gone.
  - Existing D1/D2 tests still pass unchanged.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement.** Restructure `_serve_repository` into: parse each layer (project via its fingerprint; global via `global_tools_fingerprint`, honouring keep-last-good per layer), then resolve names (built-in refused per layer; project over global; a project tool that fails to build/register falls back to the global one with the notice), then register. Keep `register_project_tools`' external behaviour for existing callers/tests (no global store → identical results). Track both fingerprints in `ProjectToolPlane`; `reload_if_changed` returns True when the resolved served definitions (name → (origin, definition)) change. Keep the code readable — a small `_Layer` dataclass (fingerprint, declared tools, last good) per file is fine.
- [ ] **Step 4: Run** `uv run pytest tests/mcp -q` and `uv run pytest -q`.
- [ ] **Step 5: Commit** `"Resolve project and global tools in MCP sessions"`.

---

### Task 3: `devgraph config tools`

**Files:**
- Modify: `devgraph/cli/main.py`
- Test: `tests/cli/test_config_tools_cli.py` (create)

- [ ] **Step 1: Failing tests** (CliRunner; repo = a tmp dir with a git-free tools file; global store via the autouse/monkeypatch):
  - `config tools list --repo R` shows built-in tools as locked, global and project tools with origin, overrides marked; `--json` has `{"tools": [{"name", "origin", "locked"}...]}`; `--global` lists only the store.
  - `add --from tool.yaml --repo R` appends (comments preserved, file valid); `--global` writes the store; duplicate → exit 1 "use `devgraph config tools edit`"; built-in name → exit 1; invalid tool (e.g. missing `$repo_id`) → exit 1 and the file unchanged; `--from -` reads stdin.
  - `edit NAME --from new.yaml` replaces; `edit NAME` with `EDITOR` set to a script that rewrites the temp file (or monkeypatch `typer.edit`/`click.edit`) → replaced; unchanged editor result → "no changes", nothing written; invalid editor result → exit 1, nothing written.
  - `delete NAME` in both scopes; unknown → exit 1.
  - `reset --repo R --yes` deletes `devgraph.tools.yaml`; `reset --global --yes` empties the store; without `--yes` and input `n` → aborted, nothing changed.
  - `--global` with `--repo` → usage error.
  - `config show` lists global tools and overrides; `config validate` and `doctor` report an invalid global store (failure) and overrides (non-failing notice).
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** a `tools_app = typer.Typer(help=...)` added to `config_app` as `tools`, using Task 1's functions; validate every result with `parse_project_tools` before writing; project writes go to `tools_file_path(root)`; print the path written and a reminder that running MCP sessions pick changes up within 2 s. Escape user strings in Rich output.
- [ ] **Step 4: Run** `uv run pytest tests/cli -q` and `uv run pytest -q`.
- [ ] **Step 5: Commit** `"Add devgraph config tools commands"`.

---

### Task 4: Docs and a manual check

- [ ] **Step 1: Docs.** README: global tools (where stored, scoped sessions only, precedence and notices) and the `config tools` commands; DEVGRAPH-CLIENT.md: the two envelope notices and that global tools appear in scoped sessions; PROJECT_STATUS: shipped bullet, update the open-items text.
- [ ] **Step 2: Full suite** `uv run pytest -q`.
- [ ] **Step 3: Manual check** (scratchpad; throwaway registry via `DEVGRAPH_REGISTRY_DB_PATH`, so the global store lands in the scratch dir): `config tools add --global --from` a tool; a repo with a commented `devgraph.tools.yaml`; `add`/`edit` via `--from`/`delete` in the repo and show the file diff keeps comments; `list --repo`; build the MCP server like `main()` (no tray) with `DEVGRAPH_MCP_REPO` and call an overriding project tool and a global tool, printing envelopes with notices. Clean up.
- [ ] **Step 4: Commit** `"Document global tools and config tools commands"`.
