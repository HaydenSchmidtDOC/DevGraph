# `devgraph config` CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `devgraph config` into a command group with `settings`, `show`, `validate` and `eject`, keeping the bare settings view working.

**Architecture:** In `devgraph/cli/main.py`, the existing `config` command becomes a Typer sub-app (`config_app`) with a custom `TyperGroup` that redirects the old positional form; its body moves into `_show_settings` (with a masking fix). `show`/`validate` read schemas through `devgraph/config/project_schema.py` and reuse `_project_schema_findings`. `eject` writes `starter_schema_text()`, a new pure function in `project_schema.py`.

**Tech Stack:** Python 3.13, Typer 0.27 / Click 8.5, Rich, Pydantic v2, pytest + `typer.testing.CliRunner`.

**Spec:** `docs/superpowers/specs/2026-10-04-config-cli-design.md`

**Working directory for every command:** the repository root of this worktree (branch `feat/config-cli`, cut from upstream `master`). Python via `uv run ...`. No Neo4j needed.

## Global Constraints

- `devgraph config` with no subcommand prints the settings exactly as before (same options `--show-defaults`, `--json`), except secret fields are now masked.
- `devgraph config <setting-name>` exits 2 with a message pointing to `devgraph config settings <setting-name>`.
- `show`/`validate`/`eject` take `--repo PATH` (a directory; default current directory), never resolved to a git root or registry entry.
- `validate` exits 1 if any checked repository is invalid or `--all` finds a cross-repository conflict; 0 otherwise.
- `eject` never overwrites: exclusive create; an existing file → exit 1 naming the path, file untouched. The written file and its uncommented example must both load with `load_project_schema`.
- Built-in labels/relationship types in the starter are generated from `devgraph.graph.schema` (`NODE_LABELS`, `RELATIONSHIP_TYPES`), never restated.
- Fields whose name contains `password`, `secret` or `token` are masked (`****`, or `(empty)` when empty) in table and JSON output, values and defaults.
- New machine-readable output uses `typer.echo(json.dumps(..., indent=2))`.
- Commit messages: plain imperative summary; no `Co-Authored-By` trailer, no AI attribution. Never commit `uv.lock` (untracked), real names or personal paths.

## Review Focus

1. `devgraph config --json` and `devgraph config settings --json` must both work — the group callback and the subcommand each own a `--json` — pinned in Task 1.
2. A user who runs `devgraph config neo4j_uri` from muscle memory gets a pointer, not "No such command" — pinned in Task 1.
3. `--repo` pointing at a file or missing directory → clear error, exit 1, no traceback — pinned in Tasks 2 and 3.
4. `eject` racing an existing file (or a symlink named `devgraph.schema.yaml`) never clobbers it — pinned in Task 2.
5. `validate --all` with no registered repositories → says so, exit 0 — pinned in Task 3.

---

## File Structure

| File | Responsibility |
| :--- | :--- |
| `devgraph/cli/main.py` (modify) | `config_app`, `_ConfigGroup`, `_show_settings`, `show`/`validate`/`eject` commands |
| `devgraph/config/project_schema.py` (modify) | `starter_schema_text()` |
| `tests/cli/test_config_cli.py` (create) | all CLI tests for this slice |
| `README.md`, `PROJECT_STATUS.md` (modify) | docs |

---

### Task 1: Command group, settings subcommand and masking

**Files:**
- Modify: `devgraph/cli/main.py` — the existing `@app.command() def config(...)` (~line 1362); imports at the top.
- Test: `tests/cli/test_config_cli.py` (create)

**Interfaces:**
- Produces: `config_app: typer.Typer` registered as `devgraph config`; `_show_settings(key: str | None, show_defaults: bool, as_json: bool) -> None`; `_is_secret_setting(name: str) -> bool`; later tasks add commands with `@config_app.command("<name>")`.

- [ ] **Step 1: Write the failing tests**

Create `tests/cli/test_config_cli.py`:

```python
"""`devgraph config` group: settings view, schema show/validate, eject."""

import json

import pytest
from typer.testing import CliRunner

from devgraph.cli import main as cli_main
from devgraph.cli.main import app
from devgraph.config.settings import Settings


@pytest.fixture
def runner():
    # Wide terminal so Rich never wraps or truncates table cells under test.
    return CliRunner(env={"COLUMNS": "200"})


@pytest.fixture
def settings(monkeypatch, tmp_path):
    fake = Settings(_env_file=None, neo4j_password="s3cret-pw", registry_db_path=tmp_path / "registry.sqlite3")
    monkeypatch.setattr(cli_main, "get_settings", lambda: fake)
    return fake


# ── settings ──────────────────────────────────────────────────────────────


def test_bare_config_still_prints_the_settings_table(runner, settings):
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0, result.output
    assert "DevGraph Configuration" in result.output
    assert "neo4j_uri" in result.output and "dashboard_port" in result.output


def test_settings_subcommand_matches_the_bare_view(runner, settings):
    bare = runner.invoke(app, ["config"])
    sub = runner.invoke(app, ["config", "settings"])
    assert sub.exit_code == 0 and sub.output == bare.output


def test_settings_subcommand_shows_one_key(runner, settings):
    result = runner.invoke(app, ["config", "settings", "dashboard_port"])
    assert result.exit_code == 0
    assert "dashboard_port" in result.output and "neo4j_uri" not in result.output


def test_unknown_setting_is_an_error(runner, settings):
    result = runner.invoke(app, ["config", "settings", "nope"])
    assert result.exit_code == 1 and "Unknown setting" in result.output


@pytest.mark.parametrize("args", [["config", "--json"], ["config", "settings", "--json"]])
def test_json_works_on_both_forms_and_masks_secrets(runner, settings, args):
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["neo4j_password"] == "****"
    assert data["dashboard_port"] == settings.dashboard_port
    assert "s3cret-pw" not in result.output


def test_table_masks_secret_values_and_defaults(runner, settings):
    result = runner.invoke(app, ["config", "--show-defaults"])
    assert result.exit_code == 0
    assert "s3cret-pw" not in result.output
    assert "devgraph-local-dev" not in result.output  # the password's default is a secret too
    assert "****" in result.output


def test_the_old_positional_form_points_at_settings(runner, settings):
    result = runner.invoke(app, ["config", "neo4j_uri"])
    assert result.exit_code == 2
    assert "devgraph config settings neo4j_uri" in result.output


def test_an_unknown_word_is_still_no_such_command(runner, settings):
    result = runner.invoke(app, ["config", "frobnicate"])
    assert result.exit_code == 2
    assert "devgraph config settings" not in result.output


def test_group_help_lists_the_subcommands(runner, settings):
    result = runner.invoke(app, ["config", "--help"])
    assert result.exit_code == 0
    assert "settings" in result.output
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/cli/test_config_cli.py -q`
Expected: failures — `config settings` is not a command (the old command reads `settings` as a key → "Unknown setting"), `--json` output contains the clear password, the positional form prints a table instead of exiting 2.

- [ ] **Step 3: Implement**

In `devgraph/cli/main.py`:

1. Imports: add `import click` and `from typer.core import TyperGroup` with the other third-party imports.

2. Replace the whole existing `@app.command() def config(...)` (decorator through the end of its body) with:

```python
class _ConfigGroup(TyperGroup):
    """`devgraph config` once took a setting name positionally; point that
    old form at `config settings` instead of a bare "No such command"."""

    def resolve_command(self, ctx: click.Context, args: list[str]):  # type: ignore[override]
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            from devgraph.config.settings import Settings

            if args[0] in Settings.model_fields:
                raise click.UsageError(
                    f"'{args[0]}' is a setting, not a subcommand: use "
                    f"`devgraph config settings {args[0]}`",
                    ctx=ctx,
                )
        return super().resolve_command(ctx, args)


config_app = typer.Typer(
    cls=_ConfigGroup,
    invoke_without_command=True,
    help="View DevGraph settings, or inspect and scaffold a repository's devgraph.schema.yaml.",
)
app.add_typer(config_app, name="config")

# Substrings that mark a setting as secret: masked wherever settings are shown.
_SECRET_MARKERS = ("password", "secret", "token")


def _is_secret_setting(name: str) -> bool:
    return any(marker in name.lower() for marker in _SECRET_MARKERS)


def _shown_value(name: str, value: Any) -> Any:
    if _is_secret_setting(name):
        return "****" if value else "(empty)"
    return value


def _show_settings(key: str | None, show_defaults: bool, as_json: bool) -> None:
    """The settings view behind `devgraph config` and `devgraph config settings`."""
    settings = get_settings()

    fields: list[tuple[str, Any, Any]] = []
    for field_name in settings.model_fields:
        field_info = settings.model_fields[field_name]
        fields.append((field_name, getattr(settings, field_name), field_info.default))

    if key:
        fields = [(n, v, d) for n, v, d in fields if n == key]
        if not fields:
            console.print(f"[red][X] Unknown setting:[/red] {key}")
            raise typer.Exit(code=1)

    if as_json:
        data = {n: _shown_value(n, v) for n, v, _ in fields}
        console.print_json(json.dumps(data, default=str))
        return

    table = Table(title="DevGraph Configuration")
    table.add_column("Key", style="cyan")
    table.add_column("Value", style="green")
    if show_defaults:
        table.add_column("Default", style="yellow")
    for field_name, value, default in fields:
        row = [field_name, str(_shown_value(field_name, value))]
        if show_defaults:
            row.append(str(_shown_value(field_name, default)))
        table.add_row(*row)
    console.print(table)


@config_app.callback()
def config(
    ctx: typer.Context,
    show_defaults: bool = typer.Option(False, "--show-defaults", help="Also show the default value for each setting."),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """With no subcommand, show DevGraph's settings (same as `config settings`)."""
    if ctx.invoked_subcommand is None:
        _show_settings(None, show_defaults, as_json)


@config_app.command("settings")
def config_settings(
    key: Optional[str] = typer.Argument(None, help="Setting key to show (e.g. 'neo4j_uri'). Omit to show all."),
    show_defaults: bool = typer.Option(False, "--show-defaults", help="Also show the default value for each setting."),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Show DevGraph's settings, or one setting. Secrets are masked."""
    _show_settings(key, show_defaults, as_json)
```

Keep this block at the position of the old command (after `update`, before `logs`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/cli -q`
Expected: all pass (existing CLI tests included). If `test_settings_subcommand_matches_the_bare_view` differs only by Rich table width, report it — do not weaken the assertion silently.

- [ ] **Step 5: Commit**

```bash
git add devgraph/cli/main.py tests/cli/test_config_cli.py
git commit -m "Make devgraph config a command group and mask secret settings"
```

---

### Task 2: Starter schema and `config eject`

**Files:**
- Modify: `devgraph/config/project_schema.py` (append `starter_schema_text` and its template after `project_schema_json_schema`)
- Modify: `devgraph/cli/main.py` (new `config_eject` command after `config_settings`)
- Test: `tests/cli/test_config_cli.py` (append)

**Interfaces:**
- Consumes: `config_app` (Task 1); `project_schema_path`, `load_project_schema`, `SCHEMA_VERSION`; `NODE_LABELS`, `RELATIONSHIP_TYPES`.
- Produces: `project_schema.starter_schema_text() -> str`; `devgraph config eject [--repo PATH]`; helper `_repo_dir(repo: Path) -> Path` in main.py (resolves and requires an existing directory; exits 1 otherwise) — Task 3 reuses it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/cli/test_config_cli.py`:

```python
# ── eject ─────────────────────────────────────────────────────────────────

from devgraph.config.project_schema import SCHEMA_FILENAME, load_project_schema, starter_schema_text
from devgraph.graph.schema import NODE_LABELS, RELATIONSHIP_TYPES


def uncommented_example(text):
    """The starter with its commented example switched on: every line after
    `extends: default` loses its leading '# '."""
    lines = text.splitlines()
    start = lines.index("extends: default") + 1
    return "\n".join(lines[:start] + [line[2:] if line.startswith("# ") else line for line in lines[start:]]) + "\n"


def test_starter_lists_every_builtin_and_loads(tmp_path):
    text = starter_schema_text()
    for label in NODE_LABELS:
        assert f"#   {label}\n" in text
    for rel in RELATIONSHIP_TYPES:
        assert f"#   {rel}\n" in text
    (tmp_path / SCHEMA_FILENAME).write_text(text)
    declaration = load_project_schema(tmp_path)
    assert declaration.extends == "default" and declaration.node_types == ()


def test_the_uncommented_example_is_valid(tmp_path):
    (tmp_path / SCHEMA_FILENAME).write_text(uncommented_example(starter_schema_text()))
    declaration = load_project_schema(tmp_path)
    assert [n.label for n in declaration.node_types] == ["Runbook"]
    assert [r.type for r in declaration.relationships] == ["DOCUMENTS"]


def test_eject_writes_the_starter(runner, settings, tmp_path):
    result = runner.invoke(app, ["config", "eject", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / SCHEMA_FILENAME).read_text() == starter_schema_text()
    assert "devgraph config validate" in result.output


def test_eject_never_overwrites(runner, settings, tmp_path):
    existing = tmp_path / SCHEMA_FILENAME
    existing.write_text("version: 1\n# mine\n")
    result = runner.invoke(app, ["config", "eject", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert "already exists" in result.output and SCHEMA_FILENAME in result.output
    assert existing.read_text() == "version: 1\n# mine\n"


def test_eject_never_follows_a_symlink(runner, settings, tmp_path):
    target = tmp_path / "elsewhere.yaml"
    target.write_text("keep me\n")
    (tmp_path / SCHEMA_FILENAME).symlink_to(target)
    result = runner.invoke(app, ["config", "eject", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert target.read_text() == "keep me\n"


@pytest.mark.parametrize("make", ["missing", "file"])
def test_eject_needs_an_existing_directory(runner, settings, tmp_path, make):
    repo = tmp_path / "repo"
    if make == "file":
        repo.write_text("not a dir")
    result = runner.invoke(app, ["config", "eject", "--repo", str(repo)])
    assert result.exit_code == 1 and "not a directory" in result.output
    assert "Traceback" not in result.output
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/cli/test_config_cli.py -q -k "eject or starter or uncommented"`
Expected: ImportError for `starter_schema_text`.

- [ ] **Step 3: Implement `starter_schema_text`**

Append to `devgraph/config/project_schema.py` (it already imports `NODE_LABELS`, `RELATIONSHIP_TYPES` and defines `SCHEMA_FILENAME`, `SCHEMA_VERSION`):

```python
_STARTER_TEMPLATE = """\
# DevGraph project schema ({filename})
#
# Declares extra node types and relationships for this repository on top of
# DevGraph's built-in schema. Check it with `devgraph config validate`, see
# the effective schema with `devgraph config show`, and apply it with
# `devgraph rescan <repo_id>`. Today a declared node type gets a uniqueness
# constraint on its key; extraction of user-defined types comes later.
#
# Built-in node labels (inherited with `extends: default`; never redeclare one):
{labels}
#
# Built-in relationship types (reuse one between your own types with
# `provider: builtin`):
{relationships}

version: {version}
extends: default

# node_types:
#   - label: Runbook
#     key: [slug]
#     metadata:
#       - name: slug
#         type: string
#         required: true
#       - name: owner
#
# relationships:
#   - type: DOCUMENTS
#     provider: custom
#     custom: {{name: runbook_links}}
#     from: Runbook
#     to: Service
"""


def starter_schema_text() -> str:
    """A valid, commented starter `devgraph.schema.yaml` for `devgraph config eject`.

    The built-in labels and relationship types are rendered from
    `devgraph.graph.schema`, so the comments can't drift from the code. The
    commented example validates once uncommented. Built-ins are listed, not
    redeclared: the loader rejects a project file that redeclares one.
    """
    return _STARTER_TEMPLATE.format(
        filename=SCHEMA_FILENAME,
        labels="\n".join(f"#   {label}" for label in NODE_LABELS),
        relationships="\n".join(f"#   {rel}" for rel in RELATIONSHIP_TYPES),
        version=SCHEMA_VERSION,
    )
```

- [ ] **Step 4: Implement `config eject`**

In `devgraph/cli/main.py`, after `config_settings`:

```python
def _repo_dir(repo: Path) -> Path:
    """`--repo` as an absolute directory, or exit 1 with a plain message."""
    root = repo.expanduser().resolve()
    if not root.is_dir():
        console.print(f"[red][X] Error:[/red] {root} is not a directory")
        raise typer.Exit(code=1)
    return root


@config_app.command("eject")
def config_eject(
    repo: Path = typer.Option(Path("."), "--repo", help="Repository root (default: current directory)."),
) -> None:
    """Write a commented starter devgraph.schema.yaml. Never overwrites an existing file."""
    from devgraph.config.project_schema import project_schema_path, starter_schema_text

    path = project_schema_path(_repo_dir(repo))
    try:
        # Exclusive create: also refuses a symlink or a file that appeared
        # after any check we could have made.
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(starter_schema_text())
    except FileExistsError:
        console.print(
            f"[red][X] Error:[/red] {path} already exists; eject never overwrites a "
            f"project schema. Edit it, or move it aside and eject again."
        )
        raise typer.Exit(code=1)
    console.print(f"[green][OK][/green] Wrote {path}")
    console.print("  Edit it, then run `devgraph config validate` and `devgraph rescan <repo_id>`.")
```

(`open(..., "x")` raises `FileExistsError` for a dangling or live symlink at that path too, so the target is never written.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/cli tests/config -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add devgraph/config/project_schema.py devgraph/cli/main.py tests/cli/test_config_cli.py
git commit -m "Add devgraph config eject with a commented starter schema"
```

---

### Task 3: `config show` and `config validate`

**Files:**
- Modify: `devgraph/cli/main.py` (two commands after `config_eject`)
- Test: `tests/cli/test_config_cli.py` (append)

**Interfaces:**
- Consumes: `config_app`, `_repo_dir` (Tasks 1–2); `_project_schema_findings(repos)` (existing, returns dicts with `repo_id`, `status` ∈ absent|valid|invalid|conflict, `detail`, `failed`); `load_project_schema`, `resolve_declaration`, `project_schema_path`, `ProjectSchemaError`, `SCHEMA_FILENAME`; `NODE_LABELS`, `RELATIONSHIP_TYPES`; `_get_registry()`.
- Produces: `_schema_report(repo_root: Path | None) -> dict` and the two commands.

- [ ] **Step 1: Write the failing tests**

Append to `tests/cli/test_config_cli.py`:

```python
# ── show / validate ───────────────────────────────────────────────────────

import textwrap

from devgraph.registry.store import RepoRegistry

WIDGET = """
    version: 1
    node_types:
      - label: Widget
        key: [slug]
        metadata: [{name: slug}]
    relationships:
      - type: LINKS
        provider: custom
        custom: {name: linker}
        from: Widget
        to: Module
"""
WIDGET_ONLY = """
    version: 1
    extends: none
    node_types:
      - label: Widget
        key: [slug]
        metadata: [{name: slug}]
"""


def write(repo, text):
    (repo / SCHEMA_FILENAME).write_text(textwrap.dedent(text))
    return repo


def show_json(runner, *args):
    result = runner.invoke(app, ["config", "show", "--json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_show_without_a_file_is_the_builtin_schema(runner, settings, tmp_path):
    data = show_json(runner, "--repo", str(tmp_path))
    assert data["status"] == "absent" and data["extends"] == "default"
    assert [n["label"] for n in data["node_types"]] == list(NODE_LABELS)
    assert {n["origin"] for n in data["node_types"]} == {"built-in"}
    assert [r["type"] for r in data["relationships"]] == list(RELATIONSHIP_TYPES)


def test_show_marks_where_each_entry_comes_from(runner, settings, tmp_path):
    data = show_json(runner, "--repo", str(write(tmp_path, WIDGET)))
    assert data["status"] == "valid" and data["schema_file"].endswith(SCHEMA_FILENAME)
    widget = next(n for n in data["node_types"] if n["label"] == "Widget")
    assert widget == {"label": "Widget", "origin": SCHEMA_FILENAME, "key": ["slug"]}
    links = next(r for r in data["relationships"] if r["type"] == "LINKS")
    assert links == {"type": "LINKS", "origin": SCHEMA_FILENAME, "from": "Widget", "to": "Module", "provider": "custom"}
    module = next(n for n in data["node_types"] if n["label"] == "Module")
    assert module == {"label": "Module", "origin": "built-in", "key": None}


def test_show_with_extends_none_has_no_builtins(runner, settings, tmp_path):
    data = show_json(runner, "--repo", str(write(tmp_path, WIDGET_ONLY)))
    assert data["extends"] == "none"
    assert [n["label"] for n in data["node_types"]] == ["Widget"]
    assert data["relationships"] == []


def test_show_global_ignores_the_repo_file(runner, settings, tmp_path):
    data = show_json(runner, "--global", "--repo", str(write(tmp_path, WIDGET)))
    assert data["status"] == "global" and data["schema_file"] is None
    assert "Widget" not in [n["label"] for n in data["node_types"]]


def test_show_human_output_names_the_file_and_origins(runner, settings, tmp_path):
    result = runner.invoke(app, ["config", "show", "--repo", str(write(tmp_path, WIDGET))])
    assert result.exit_code == 0, result.output
    assert "Widget" in result.output and "built-in" in result.output and SCHEMA_FILENAME in result.output


def test_show_reports_an_invalid_schema(runner, settings, tmp_path):
    write(tmp_path, "version: 1\nnode_types: [oops\n")
    result = runner.invoke(app, ["config", "show", "--repo", str(tmp_path)])
    assert result.exit_code == 1 and "malformed YAML" in result.output


def test_show_needs_a_directory(runner, settings, tmp_path):
    result = runner.invoke(app, ["config", "show", "--repo", str(tmp_path / "missing")])
    assert result.exit_code == 1 and "not a directory" in result.output


def test_validate_one_repo(runner, settings, tmp_path):
    absent = runner.invoke(app, ["config", "validate", "--repo", str(tmp_path)])
    assert absent.exit_code == 0 and "absent" in absent.output
    valid = runner.invoke(app, ["config", "validate", "--repo", str(write(tmp_path, WIDGET))])
    assert valid.exit_code == 0 and "valid" in valid.output
    write(tmp_path, "version: 2\n")
    invalid = runner.invoke(app, ["config", "validate", "--repo", str(tmp_path)])
    assert invalid.exit_code == 1 and "invalid" in invalid.output


def test_validate_all_checks_every_registered_repo_and_conflicts(runner, settings, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    write(a, WIDGET)
    write(b, WIDGET.replace("key: [slug]", "key: [code]").replace("{name: slug}", "{name: code}"))
    registry = RepoRegistry(settings.registry_db_path)
    try:
        registry.add_repo(a, repo_id="repo-a")
        registry.add_repo(b, repo_id="repo-b")
    finally:
        registry.close()
    result = runner.invoke(app, ["config", "validate", "--all"])
    assert result.exit_code == 1, result.output
    assert "repo-a" in result.output and "repo-b" in result.output and "conflict" in result.output


def test_validate_all_with_no_repos(runner, settings):
    result = runner.invoke(app, ["config", "validate", "--all"])
    assert result.exit_code == 0 and "No registered repositories" in result.output


def test_validate_rejects_repo_and_all_together(runner, settings, tmp_path):
    result = runner.invoke(app, ["config", "validate", "--all", "--repo", str(tmp_path)])
    assert result.exit_code == 2
```

Note: `RepoRegistry.add_repo(path, repo_id=None)` may require the path to be a git repository; read its signature first and, if it does, `git init` the two temp dirs in the test (`subprocess.run(["git", "init", "-q"], cwd=..., check=True)`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/cli/test_config_cli.py -q -k "show or validate"`
Expected: exit code 2 "No such command 'show'/'validate'".

- [ ] **Step 3: Implement**

In `devgraph/cli/main.py`, after `config_eject`:

```python
def _schema_report(repo_root: Path | None) -> dict[str, Any]:
    """The effective schema and where each entry comes from.

    `repo_root=None` reports the built-in schema alone. Raises
    ProjectSchemaError for an unreadable or invalid project file.
    """
    from devgraph.config.project_schema import (
        SCHEMA_FILENAME,
        load_project_schema,
        project_schema_path,
        resolve_declaration,
    )
    from devgraph.graph.schema import NODE_LABELS, RELATIONSHIP_TYPES

    declaration = None if repo_root is None else load_project_schema(repo_root)
    origin = None if repo_root is None else str(project_schema_path(repo_root))
    effective = resolve_declaration(declaration, origin=origin or SCHEMA_FILENAME)
    inherits = effective.extends == "default"

    node_types: list[dict[str, Any]] = [
        {"label": label, "origin": "built-in", "key": None} for label in (NODE_LABELS if inherits else ())
    ]
    node_types += [
        {"label": n.label, "origin": SCHEMA_FILENAME, "key": list(n.key)} for n in effective.node_types
    ]
    relationships: list[dict[str, Any]] = [
        {"type": rel, "origin": "built-in", "from": None, "to": None, "provider": "builtin"}
        for rel in (RELATIONSHIP_TYPES if inherits else ())
    ]
    relationships += [
        {"type": r.type, "origin": SCHEMA_FILENAME, "from": r.from_, "to": r.to, "provider": r.provider}
        for r in effective.relationships
    ]
    if repo_root is None:
        status = "global"
    else:
        status = "absent" if declaration is None else "valid"
    return {
        "repo": None if repo_root is None else str(repo_root),
        "schema_file": origin if declaration is not None else None,
        "status": status,
        "extends": effective.extends,
        "node_types": node_types,
        "relationships": relationships,
    }


@config_app.command("show")
def config_show(
    repo: Path = typer.Option(Path("."), "--repo", help="Repository root (default: current directory)."),
    global_only: bool = typer.Option(False, "--global", help="Show only DevGraph's built-in schema."),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Show the effective graph schema for a repository and where each entry comes from."""
    from devgraph.config.project_schema import SCHEMA_FILENAME, ProjectSchemaError

    root = None if global_only else _repo_dir(repo)
    try:
        report = _schema_report(root)
    except ProjectSchemaError as exc:
        console.print(f"[red][X] Invalid project schema:[/red] {exc}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps(report, indent=2))
        return

    if report["status"] == "global":
        console.print("Built-in schema (no global config file yet)")
    elif report["status"] == "absent":
        console.print(f"{report['repo']}: no {SCHEMA_FILENAME} — built-in schema")
    else:
        console.print(f"{report['repo']}: {report['schema_file']} (valid, extends: {report['extends']})")

    nodes = Table(title="Node types")
    nodes.add_column("Label", style="cyan")
    nodes.add_column("Origin")
    nodes.add_column("Key")
    for node in report["node_types"]:
        nodes.add_row(node["label"], node["origin"], ", ".join(node["key"]) if node["key"] else "—")
    console.print(nodes)

    rels = Table(title="Relationships")
    rels.add_column("Type", style="cyan")
    rels.add_column("From")
    rels.add_column("To")
    rels.add_column("Provider")
    rels.add_column("Origin")
    for rel in report["relationships"]:
        rels.add_row(rel["type"], rel["from"] or "any", rel["to"] or "any", rel["provider"], rel["origin"])
    console.print(rels)


@config_app.command("validate")
def config_validate(
    repo: Optional[Path] = typer.Option(None, "--repo", help="Repository root (default: current directory)."),
    all_repos: bool = typer.Option(False, "--all", help="Check every registered repository, and conflicts between them."),
) -> None:
    """Fail-closed check of devgraph.schema.yaml. Exits 1 if anything is invalid or conflicting."""
    from types import SimpleNamespace

    if all_repos and repo is not None:
        raise typer.BadParameter("use either --repo or --all, not both")
    if all_repos:
        registry = _get_registry()
        try:
            repos = registry.list_repos()
        finally:
            registry.close()
        if not repos:
            console.print("No registered repositories.")
            return
    else:
        root = _repo_dir(repo or Path("."))
        repos = [SimpleNamespace(repo_id=str(root), path=root)]

    findings = _project_schema_findings(repos)
    for finding in findings:
        colour = "red" if finding["failed"] else "green"
        subject = finding["repo_id"] or "cross-repository"
        console.print(f"[{colour}]{finding['status']}[/{colour}] {subject}: {finding['detail']}")
    if any(finding["failed"] for finding in findings):
        raise typer.Exit(code=1)
```

`typer.BadParameter` exits with code 2, which `test_validate_rejects_repo_and_all_together` expects.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/cli tests/config -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add devgraph/cli/main.py tests/cli/test_config_cli.py
git commit -m "Add devgraph config show and validate for project schemas"
```

---

### Task 4: Docs and verification

**Files:**
- Modify: `README.md` (CLI table row "View configuration or tray logs", ~line 54; the "Project schema constraints" section, ~line 69), `PROJECT_STATUS.md` (CLI bullet ~line 28; a shipped bullet)

- [ ] **Step 1: Docs**

README.md:
- Change the CLI table row to: `| View settings, project schema, or tray logs | `devgraph config`, `devgraph config show / validate / eject`, `devgraph logs` |` (keep the table's existing format).
- In the "Project schema constraints" section, add a bullet: "`devgraph config validate` checks the file (or every registered repository's with `--all`) and exits non-zero on an invalid schema or a cross-repository conflict; `devgraph config show` prints the effective schema and where each entry comes from; `devgraph config eject` writes a commented starter file and never overwrites an existing one."
- Add one more bullet: "`devgraph config` alone still shows DevGraph's settings; a single setting is now `devgraph config settings <key>`, and secret settings are masked."

PROJECT_STATUS.md:
- In the CLI bullet, ensure "configuration" mentions the `config` group (settings, schema show/validate/eject).
- Add a shipped bullet: "`devgraph config` command group (#1, CLI slice) shipped: `config settings` (bare `config` unchanged; secrets now masked), `config show` (effective schema with per-entry origin), `config validate` (fail-closed, `--all` with cross-repository conflicts, CI-friendly exit codes) and `config eject` (commented starter `devgraph.schema.yaml`; refuses to overwrite). `config enable/disable`, `config tools` and `config schema` editing remain open."

- [ ] **Step 2: Full suite**

Run: `uv run pytest -q` — expected all pass.

- [ ] **Step 3: Manual check**

In a scratch directory (the session scratchpad), run: `uv run devgraph config eject --repo <dir>`, `uv run devgraph config validate --repo <dir>`, `uv run devgraph config show --repo <dir>`, a second `eject` (refused), `uv run devgraph config neo4j_uri` (pointer), `uv run devgraph config` (settings, password masked). Record the outputs. Delete the scratch directory.

- [ ] **Step 4: Commit**

```bash
git add README.md PROJECT_STATUS.md
git commit -m "Document the devgraph config command group"
```

- [ ] **Step 5: Push and open the PR — only after the user confirms**

```bash
git push -u origin feat/config-cli
gh pr create -R HaydenSchmidtDOC/DevGraph --base master --head <fork-owner>:feat/config-cli \
  --title "devgraph config: settings, schema show/validate, eject" --body-file <scratch>/pr-body.md
```

Body: the commands, the backward-compatible settings view and its masking fix, why eject writes a starter, what remains open, validation, and `Part of #1.` No AI attribution.
