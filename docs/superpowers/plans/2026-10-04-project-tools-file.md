# Project Tools File Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Define, load and fail-closed validate `devgraph.tools.yaml` (project Cypher tools), and report it through `devgraph config validate/show` and `devgraph doctor`. Nothing is served yet.

**Architecture:** New `devgraph/config/project_tools.py` mirrors `project_schema.py`: Pydantic models, safe YAML loader, static read-only Cypher check, JSON Schema. `devgraph/mcp/server.py` exposes `builtin_tool_names()`. `devgraph/cli/main.py` gains `_project_tools_findings`/`_tools_report` and wires them into `config validate`, `config show` and `doctor`.

**Tech Stack:** Python 3.13, Pydantic v2, PyYAML, Typer, pytest.

**Spec:** `docs/superpowers/specs/2026-10-04-project-tools-file-design.md`

**Working directory:** the repository root of this worktree (branch `epic1/c-tools-file`, stacked on `epic1/base` = upstream master + #27 + #28). No Neo4j needed.

## Global Constraints

- File name `devgraph.tools.yaml`; `version: 1`; unknown keys rejected; invalid → `ProjectToolsError`, never a partial result.
- Tool and parameter names fullmatch `[a-z][a-z0-9_]{0,63}`; tool names unique; parameter names unique and never `repo_id`.
- `description` non-blank, ≤ 1024 chars. `cypher` non-blank, references `$repo_id`, passes the read-only check (`CREATE MERGE SET DELETE DETACH REMOVE DROP FOREACH CALL USE`, `LOAD CSV`; strings, backtick identifiers and comments ignored).
- Used `$params` (minus `repo_id`) == declared parameters.
- Parameter `type` ∈ `string|integer|float|boolean` (default `string`); `required` default true; `default` only when `required: false` and must match the type (`bool` is never an integer/float; an integer is a valid float).
- `max_rows` 1–1000 default 100; `timeout_s` 1–60 default 10; booleans rejected for both.
- A tool named like a built-in MCP tool → non-failing warning finding.
- `config validate` exit 1 if the schema or tools file is invalid; `config show` exit 1 on an invalid tools file.
- Commit messages: plain imperative; no `Co-Authored-By` trailer, no AI attribution. Never commit `uv.lock`, real names or personal paths.

## Review Focus

1. A legitimate read query that mentions a write keyword inside a string, comment or backtick-quoted name → accepted — pinned in Task 1.
2. `$folder` appearing only inside a string literal → counts as unused (it is not a parameter) — pinned in Task 1.
3. YAML `max_rows: true` or `default: yes` for an integer parameter → rejected, not coerced to 1 — pinned in Task 1.
4. A repository with a valid schema and an invalid tools file → `config validate` exits 1 and names the tools file — pinned in Task 2.
5. `config show --global` → no tools section, no error — pinned in Task 2.

---

### Task 1: Tools file loader

**Files:**
- Create: `devgraph/config/project_tools.py`
- Test: `tests/config/test_project_tools.py`

**Interfaces:**
- Produces: `TOOLS_FILENAME`, `TOOLS_VERSION`, `INJECTED_PARAMETER = "repo_id"`, `PARAMETER_TYPES`, `ProjectToolsError`, `ToolParameter`, `CypherTool`, `ProjectTools`, `tools_file_path(repo_root) -> Path`, `load_project_tools(repo_root) -> ProjectTools | None`, `write_clauses(query) -> list[str]`, `query_parameters(query) -> set[str]`, `project_tools_json_schema() -> dict`.

- [ ] **Step 1: Write the failing tests** — create `tests/config/test_project_tools.py`:

```python
"""Loader and validator for devgraph.tools.yaml. Pure; no Neo4j."""

import textwrap

import pytest

from devgraph.config.project_tools import (
    TOOLS_FILENAME,
    ProjectToolsError,
    load_project_tools,
    project_tools_json_schema,
    query_parameters,
    write_clauses,
)

LIST_FOLDER = """
    version: 1
    tools:
      - name: list_folder
        description: List the files directly inside a folder.
        cypher: |
          MATCH (f:File {repo_id: $repo_id})-[:IS_CHILD_OF]->(:Folder {repo_id: $repo_id, path: $folder})
          RETURN f.path AS path ORDER BY path
        parameters:
          - name: folder
            description: Repo-relative folder path.
"""


def write(tmp_path, text):
    (tmp_path / TOOLS_FILENAME).write_text(textwrap.dedent(text))
    return tmp_path


def tool_text(cypher, params="", extra=""):
    lines = ["version: 1", "tools:", "  - name: t", "    description: d", "    cypher: |"]
    lines += ["      " + line for line in textwrap.dedent(cypher).strip().splitlines()]
    if params:
        lines += ["    parameters:"] + ["      " + line for line in textwrap.dedent(params).strip().splitlines()]
    if extra:
        lines += ["    " + line for line in textwrap.dedent(extra).strip().splitlines()]
    return "\n".join(lines) + "\n"


def load_text(tmp_path, text):
    (tmp_path / TOOLS_FILENAME).write_text(text)
    return load_project_tools(tmp_path)


def test_absent_file_is_none(tmp_path):
    assert load_project_tools(tmp_path) is None


def test_example_loads_with_defaults(tmp_path):
    tools = load_project_tools(write(tmp_path, LIST_FOLDER))
    (tool,) = tools.tools
    assert tool.name == "list_folder" and tool.max_rows == 100 and tool.timeout_s == 10
    (param,) = tool.parameters
    assert (param.name, param.type, param.required, param.default) == ("folder", "string", True, None)


@pytest.mark.parametrize("text", ["", "[1, 2]\n", "version: 2\ntools: []\n", "version: 1\ntools: []\nextra: 1\n"])
def test_bad_documents_fail_closed(tmp_path, text):
    with pytest.raises(ProjectToolsError):
        load_text(tmp_path, text)


def test_unknown_tool_keys_are_rejected(tmp_path):
    with pytest.raises(ProjectToolsError):
        load_text(tmp_path, tool_text("MATCH (n {repo_id: $repo_id}) RETURN n", extra="script: x.py"))


@pytest.mark.parametrize("name", ["Upper", "1st", "has-dash", "a" * 65])
def test_tool_names_are_identifiers(tmp_path, name):
    with pytest.raises(ProjectToolsError):
        load_text(tmp_path, tool_text("MATCH (n {repo_id: $repo_id}) RETURN n").replace("name: t", f"name: {name}"))


def test_tool_names_are_unique(tmp_path):
    one = tool_text("MATCH (n {repo_id: $repo_id}) RETURN n")
    body = one.split("tools:\n", 1)[1]
    with pytest.raises(ProjectToolsError, match="more than once"):
        load_text(tmp_path, one + body)


@pytest.mark.parametrize("description", ["''", "'   '", "'" + "x" * 1025 + "'"])
def test_descriptions_must_be_meaningful(tmp_path, description):
    with pytest.raises(ProjectToolsError):
        load_text(tmp_path, tool_text("MATCH (n {repo_id: $repo_id}) RETURN n").replace("description: d", f"description: {description}"))


def test_the_query_must_use_the_injected_repo_id(tmp_path):
    with pytest.raises(ProjectToolsError, match=r"\$repo_id"):
        load_text(tmp_path, tool_text("MATCH (n) RETURN n"))


def test_repo_id_cannot_be_declared(tmp_path):
    with pytest.raises(ProjectToolsError, match="repo_id"):
        load_text(tmp_path, tool_text("MATCH (n {repo_id: $repo_id}) RETURN n", params="- name: repo_id"))


def test_used_parameters_must_be_declared(tmp_path):
    with pytest.raises(ProjectToolsError, match="folder"):
        load_text(tmp_path, tool_text("MATCH (n {repo_id: $repo_id, path: $folder}) RETURN n"))


def test_declared_parameters_must_be_used(tmp_path):
    with pytest.raises(ProjectToolsError, match="folder"):
        load_text(tmp_path, tool_text("MATCH (n {repo_id: $repo_id}) RETURN n", params="- name: folder"))


def test_a_parameter_only_inside_a_string_is_not_used(tmp_path):
    with pytest.raises(ProjectToolsError, match="folder"):
        load_text(tmp_path, tool_text("MATCH (n {repo_id: $repo_id}) WHERE n.name = '$folder' RETURN n", params="- name: folder"))


@pytest.mark.parametrize(
    "clause",
    [
        "CREATE (m:X {repo_id: $repo_id})",
        "MERGE (m:X {repo_id: $repo_id})",
        "MATCH (m {repo_id: $repo_id}) SET m.x = 1",
        "MATCH (m {repo_id: $repo_id}) DELETE m",
        "MATCH (m {repo_id: $repo_id}) DETACH DELETE m",
        "MATCH (m {repo_id: $repo_id}) REMOVE m.x",
        "MATCH (m {repo_id: $repo_id}) FOREACH (x IN [1] | SET m.y = x)",
        "CALL db.labels() YIELD label MATCH (m {repo_id: $repo_id}) RETURN label",
        "LOAD CSV FROM 'file:///x' AS row MATCH (m {repo_id: $repo_id}) RETURN row",
        "USE system MATCH (m {repo_id: $repo_id}) RETURN m",
        "match (m {repo_id: $repo_id}) set m.x = 1",
    ],
)
def test_write_and_procedure_clauses_are_rejected(tmp_path, clause):
    with pytest.raises(ProjectToolsError, match="read-only"):
        load_text(tmp_path, tool_text(clause + " RETURN 1"))


def test_keywords_in_strings_comments_and_backticks_are_fine(tmp_path):
    cypher = """
        // DELETE nothing; this only reads
        MATCH (n {repo_id: $repo_id}) /* CREATE? no */
        WHERE n.name = 'SET or MERGE' AND n.note <> "DROP TABLE"
        RETURN n.`set` AS s, n.create_time AS t
    """
    assert load_text(tmp_path, tool_text(cypher)).tools[0].name == "t"


@pytest.mark.parametrize("extra", ["max_rows: 0", "max_rows: 1001", "max_rows: true", "timeout_s: 0", "timeout_s: 61", "timeout_s: true"])
def test_limits_are_bounded_and_never_booleans(tmp_path, extra):
    with pytest.raises(ProjectToolsError):
        load_text(tmp_path, tool_text("MATCH (n {repo_id: $repo_id}) RETURN n", extra=extra))


def test_limits_within_bounds_load(tmp_path):
    tool = load_text(tmp_path, tool_text("MATCH (n {repo_id: $repo_id}) RETURN n", extra="max_rows: 1000\ntimeout_s: 60")).tools[0]
    assert (tool.max_rows, tool.timeout_s) == (1000, 60)


@pytest.mark.parametrize(
    "param",
    [
        "- {name: p, type: integer, required: false, default: 'x'}",
        "- {name: p, type: integer, required: false, default: true}",
        "- {name: p, type: boolean, required: false, default: 1}",
        "- {name: p, type: string, required: false, default: 3}",
        "- {name: p, type: string, default: x}",  # required with a default
        "- {name: p, type: date}",
        "- {name: p}\n- {name: p}",
    ],
)
def test_bad_parameters_are_rejected(tmp_path, param):
    with pytest.raises(ProjectToolsError):
        load_text(tmp_path, tool_text("MATCH (n {repo_id: $repo_id, x: $p}) RETURN n", params=param))


def test_an_integer_default_is_a_valid_float(tmp_path):
    tool = load_text(
        tmp_path, tool_text("MATCH (n {repo_id: $repo_id, x: $p}) RETURN n", params="- {name: p, type: float, required: false, default: 2}")
    ).tools[0]
    assert tool.parameters[0].default == 2


def test_helpers_ignore_literals():
    assert write_clauses("MATCH (n) WHERE n.x = 'CREATE' RETURN n") == []
    assert write_clauses("MATCH (n) DETACH DELETE n") == ["DETACH", "DELETE"]
    assert write_clauses("LOAD   csv FROM 'x' AS r RETURN r") == ["LOAD CSV"]
    assert query_parameters("RETURN $a, '$b', $repo_id // $c") == {"a", "repo_id"}


def test_json_schema_describes_the_file():
    schema = project_tools_json_schema()
    assert "tools" in schema["properties"]
    assert "ToolParameter" in schema["$defs"] and "CypherTool" in schema["$defs"]
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/config/test_project_tools.py -q` → ModuleNotFoundError.

- [ ] **Step 3: Implement** — create `devgraph/config/project_tools.py`:

```python
"""Optional per-project MCP tool declarations: `devgraph.tools.yaml`.

A repository may declare Cypher tools for its agents at its root. This module
is the file's format, loader and fail-closed validator; it serves nothing --
the MCP tool plane that will is a later unit. Every check here is static and
defence in depth: when tools are served they also run in a read transaction
with the server injecting `$repo_id`.

Import this module directly, like `devgraph.config.project_schema`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

TOOLS_FILENAME = "devgraph.tools.yaml"
TOOLS_VERSION = 1

#: The parameter the server always supplies; never declared, never overridable.
INJECTED_PARAMETER = "repo_id"

PARAMETER_TYPES: tuple[str, ...] = ("string", "integer", "float", "boolean")
NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
MAX_DESCRIPTION_LENGTH = 1024
DEFAULT_MAX_ROWS = 100
MAX_ROWS_LIMIT = 1000
DEFAULT_TIMEOUT_S = 10
MAX_TIMEOUT_S = 60

#: Clauses a read-only tool may not contain. CALL is refused outright
#: (procedures and subqueries alike) to keep the rule simple to audit.
WRITE_KEYWORDS: tuple[str, ...] = (
    "CREATE", "MERGE", "SET", "DELETE", "DETACH", "REMOVE", "DROP", "FOREACH", "CALL", "USE",
)

ToolsVersion = Literal[TOOLS_VERSION]
ParameterType = Literal[PARAMETER_TYPES]
ScalarDefault = str | int | float | bool | None

# String literals, backtick-quoted names and comments: blanked before any
# keyword or parameter scan, so text inside them never counts.
_LITERALS = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|`[^`]*`|//[^\n]*|/\*.*?\*/", re.S)
_WRITES = re.compile(r"\bLOAD\s+CSV\b|\b(?:" + "|".join(WRITE_KEYWORDS) + r")\b", re.I)
_PARAMETERS = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


class ProjectToolsError(Exception):
    """A tools file is unreadable, malformed or invalid. Fail-closed."""


def _blank_literals(query: str) -> str:
    return _LITERALS.sub(lambda match: " " * len(match.group(0)), query)


def write_clauses(query: str) -> list[str]:
    """Write/procedure keywords in a query, in order of first appearance."""
    found: list[str] = []
    for match in _WRITES.finditer(_blank_literals(query)):
        word = " ".join(match.group(0).upper().split())
        if word not in found:
            found.append(word)
    return found


def query_parameters(query: str) -> set[str]:
    """`$name` parameters a query uses, ignoring strings and comments."""
    return set(_PARAMETERS.findall(_blank_literals(query)))


def _check_name(value: str, kind: str) -> str:
    if not NAME_PATTERN.fullmatch(value):
        raise ValueError(f"{kind} {value!r} must fullmatch {NAME_PATTERN.pattern}")
    return value


class ToolParameter(BaseModel):
    """One caller-supplied parameter of a project tool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    type: ParameterType = "string"
    required: bool = True
    default: ScalarDefault = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        _check_name(value, "parameter name")
        if value == INJECTED_PARAMETER:
            raise ValueError(
                f"parameter {INJECTED_PARAMETER!r} is supplied by DevGraph for the "
                f"calling repository and cannot be declared"
            )
        return value

    @model_validator(mode="after")
    def _valid_default(self) -> ToolParameter:
        if self.default is None:
            return self
        if self.required:
            raise ValueError(f"parameter {self.name!r} has a default, so it must be required: false")
        value = self.default
        ok = {
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "float": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
        }[self.type]
        if not ok:
            raise ValueError(f"parameter {self.name!r} default {value!r} is not a {self.type}")
        return self


class CypherTool(BaseModel):
    """A read-only Cypher query exposed to agents as an MCP tool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    cypher: str
    parameters: tuple[ToolParameter, ...] = ()
    max_rows: int = Field(DEFAULT_MAX_ROWS, ge=1, le=MAX_ROWS_LIMIT, strict=True)
    timeout_s: int = Field(DEFAULT_TIMEOUT_S, ge=1, le=MAX_TIMEOUT_S, strict=True)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        return _check_name(value, "tool name")

    @field_validator("description")
    @classmethod
    def _valid_description(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("description must not be blank: it is what an agent reads to choose the tool")
        if len(value) > MAX_DESCRIPTION_LENGTH:
            raise ValueError(f"description must be at most {MAX_DESCRIPTION_LENGTH} characters")
        return value

    @field_validator("cypher")
    @classmethod
    def _valid_cypher(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("cypher must not be blank")
        return value

    @model_validator(mode="after")
    def _valid_query(self) -> CypherTool:
        writes = write_clauses(self.cypher)
        if writes:
            raise ValueError(
                f"tool {self.name!r} must be read-only; found {', '.join(writes)} "
                f"(backtick-quote a property or label that happens to use one of these words)"
            )
        used = query_parameters(self.cypher)
        if INJECTED_PARAMETER not in used:
            raise ValueError(
                f"tool {self.name!r} must filter on $repo_id, which DevGraph supplies for the calling repository"
            )
        declared = [p.name for p in self.parameters]
        duplicates = sorted({n for n in declared if declared.count(n) > 1})
        if duplicates:
            raise ValueError(f"tool {self.name!r} declares parameter(s) more than once: {', '.join(duplicates)}")
        used.discard(INJECTED_PARAMETER)
        undeclared = sorted(used - set(declared))
        if undeclared:
            raise ValueError(f"tool {self.name!r} uses undeclared parameter(s): {', '.join(undeclared)}")
        unused = sorted(set(declared) - used)
        if unused:
            raise ValueError(f"tool {self.name!r} declares unused parameter(s): {', '.join(unused)}")
        return self


class ProjectTools(BaseModel):
    """A validated `devgraph.tools.yaml` document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: ToolsVersion
    tools: tuple[CypherTool, ...] = ()

    @model_validator(mode="after")
    def _unique_names(self) -> ProjectTools:
        names = [tool.name for tool in self.tools]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"tool name(s) declared more than once: {', '.join(duplicates)}")
        return self


def tools_file_path(repo_root: Path) -> Path:
    """Where a repository's optional tools file lives."""
    return Path(repo_root) / TOOLS_FILENAME


def load_project_tools(repo_root: Path) -> ProjectTools | None:
    """Load and validate `devgraph.tools.yaml`; None if and only if it is absent."""
    path = tools_file_path(repo_root)
    try:
        if not path.exists():
            return None
        if not path.is_file():
            raise ProjectToolsError(f"{path}: tools file is not a regular file")
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectToolsError(f"{path}: cannot be read: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ProjectToolsError(f"{path}: is not valid UTF-8: {exc}") from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProjectToolsError(f"{path}: malformed YAML: {exc}") from exc
    if document is None:
        raise ProjectToolsError(f"{path}: the file is empty; delete it, or declare 'version: {TOOLS_VERSION}'")
    if not isinstance(document, dict):
        raise ProjectToolsError(f"{path}: expected a YAML mapping at the document root, found {type(document).__name__}")
    try:
        return ProjectTools.model_validate(document)
    except ValidationError as exc:
        lines = [f"{path}: invalid tools file"]
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "<document>"
            lines.append(f"  {location}: {error['msg']}")
        raise ProjectToolsError("\n".join(lines)) from exc


def project_tools_json_schema() -> dict[str, Any]:
    """JSON Schema for editors. Descriptive only: the read-only check,
    parameter/query agreement and name uniqueness live in the validators."""
    return ProjectTools.model_json_schema()
```

Note: pydantic reports `ValueError` messages prefixed with "Value error, "; the tests use `match=` substrings that survive that prefix. If `match="read-only"` etc. fail because of formatting, check `str(exc)` and adjust the message — not the test intent.

- [ ] **Step 4: Run** — `uv run pytest tests/config -q` → all pass.
- [ ] **Step 5: Commit** — `git add devgraph/config/project_tools.py tests/config/test_project_tools.py` and `git commit -m "Load and validate project MCP tool declarations"`.

---

### Task 2: CLI and doctor reporting

**Files:**
- Modify: `devgraph/mcp/server.py` (add `builtin_tool_names()` after `_TOOL_CATALOG`)
- Modify: `devgraph/cli/main.py` (`_project_tools_findings` after `_project_schema_findings`; doctor section after "Project schemas"; `_tools_report` after `_schema_report`; `config_show`; `config_validate`)
- Modify: `README.md`, `PROJECT_STATUS.md`
- Test: `tests/cli/test_config_cli.py` (append)

**Interfaces:**
- Consumes: Task 1.
- Produces: `server.builtin_tool_names() -> frozenset[str]`; `_project_tools_findings(repos) -> list[dict]` (keys `repo_id`, `status` ∈ absent|valid|invalid|warning, `detail`, `failed`); `_tools_report(repo_root) -> dict` (`status`, `tools_file`, `tools`).

- [ ] **Step 1: Write the failing tests** — append to `tests/cli/test_config_cli.py` (it already has `runner`, `settings`, `write(repo, text)` for the schema file, `json`, `app`, `cli_main`, `SCHEMA_FILENAME`, `WIDGET`; add `from devgraph.config.project_tools import TOOLS_FILENAME` to the top imports):

```python
# ── project tools ─────────────────────────────────────────────────────────

TOOLS = """
    version: 1
    tools:
      - name: count_nodes
        description: Count this repository's nodes.
        cypher: |
          MATCH (n {repo_id: $repo_id}) RETURN count(n) AS n
"""
SHADOWING_TOOLS = TOOLS.replace("count_nodes", "search_component")


def write_tools(repo, text):
    (repo / TOOLS_FILENAME).write_text(textwrap.dedent(text))
    return repo


def test_validate_reports_tools_valid(runner, settings, tmp_path):
    result = runner.invoke(app, ["config", "validate", "--repo", str(write_tools(tmp_path, TOOLS))])
    assert result.exit_code == 0, result.output
    assert "count_nodes" in result.output


def test_validate_fails_on_an_invalid_tools_file_even_with_a_valid_schema(runner, settings, tmp_path):
    write(tmp_path, WIDGET)
    write_tools(tmp_path, TOOLS.replace("RETURN count(n) AS n", "SET n.x = 1 RETURN n"))
    result = runner.invoke(app, ["config", "validate", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert TOOLS_FILENAME in result.output and "read-only" in result.output


def test_validate_warns_when_a_tool_shadows_a_builtin(runner, settings, tmp_path):
    result = runner.invoke(app, ["config", "validate", "--repo", str(write_tools(tmp_path, SHADOWING_TOOLS))])
    assert result.exit_code == 0
    assert "warning" in result.output and "built-in" in result.output


def test_show_json_includes_tools(runner, settings, tmp_path):
    data = show_json(runner, "--repo", str(write_tools(tmp_path, TOOLS)))
    assert data["tools"]["status"] == "valid"
    assert [t["name"] for t in data["tools"]["tools"]] == ["count_nodes"]
    assert data["tools"]["tools"][0]["max_rows"] == 100


def test_show_without_a_tools_file(runner, settings, tmp_path):
    data = show_json(runner, "--repo", str(tmp_path))
    assert data["tools"] == {"status": "absent", "tools_file": None, "tools": []}


def test_show_global_has_no_tools_section(runner, settings, tmp_path):
    data = show_json(runner, "--global")
    assert data["tools"] is None


def test_show_fails_on_an_invalid_tools_file(runner, settings, tmp_path):
    write_tools(tmp_path, "version: 1\ntools: [oops\n")
    result = runner.invoke(app, ["config", "show", "--repo", str(tmp_path)])
    assert result.exit_code == 1 and "Invalid project tools" in result.output


def test_tools_findings_cover_absent_valid_invalid_and_warning(tmp_path):
    from types import SimpleNamespace

    a, b, c, d = (tmp_path / n for n in "abcd")
    for p in (a, b, c, d):
        p.mkdir()
    write_tools(b, TOOLS)
    write_tools(c, "version: 1\ntools: [oops\n")
    write_tools(d, SHADOWING_TOOLS)
    repos = [SimpleNamespace(repo_id=p.name, path=p) for p in (a, b, c, d)]
    findings = cli_main._project_tools_findings(repos)
    by_status = {(f["repo_id"], f["status"]) for f in findings}
    assert {("a", "absent"), ("b", "valid"), ("c", "invalid"), ("d", "valid"), ("d", "warning")} == by_status
    assert [f["failed"] for f in findings if f["status"] == "invalid"] == [True]
    assert not any(f["failed"] for f in findings if f["status"] == "warning")
```

(`textwrap` and `show_json` already exist in that file from the earlier schema tests; import `textwrap` at the top if it is not imported.)

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/cli/test_config_cli.py -q -k "tools"`.

- [ ] **Step 3: Implement**

`devgraph/mcp/server.py`, after `_TOOL_CATALOG`:

```python
def builtin_tool_names() -> frozenset[str]:
    """Names of DevGraph's own MCP tools: a project tool may not take one over."""
    return frozenset(entry["name"] for entry in _TOOL_CATALOG)
```

`devgraph/cli/main.py`, after `_project_schema_findings`:

```python
def _project_tools_findings(repos: list[Any]) -> list[dict[str, Any]]:
    """Per-repository `devgraph.tools.yaml` state, in the same shape as
    `_project_schema_findings`. A tool named like a built-in is a non-failing
    warning: the built-in is always used, as the tool plane will report."""
    from devgraph.config.project_tools import TOOLS_FILENAME, ProjectToolsError, load_project_tools
    from devgraph.mcp.server import builtin_tool_names

    builtin = builtin_tool_names()
    findings: list[dict[str, Any]] = []
    for repo in sorted(repos, key=lambda r: r.repo_id):
        try:
            declared = load_project_tools(repo.path)
        except ProjectToolsError as exc:
            findings.append({"repo_id": repo.repo_id, "status": "invalid", "detail": str(exc), "failed": True})
            continue
        if declared is None:
            findings.append({"repo_id": repo.repo_id, "status": "absent", "detail": f"no {TOOLS_FILENAME}", "failed": False})
            continue
        names = ", ".join(tool.name for tool in declared.tools)
        findings.append({"repo_id": repo.repo_id, "status": "valid", "detail": f"tools: {names or 'none'}", "failed": False})
        for tool in declared.tools:
            if tool.name in builtin:
                findings.append({
                    "repo_id": repo.repo_id,
                    "status": "warning",
                    "detail": f"{TOOLS_FILENAME}: tool {tool.name!r} has the name of a built-in tool; the built-in will be used",
                    "failed": False,
                })
    return findings
```

Doctor, right after the "Project schemas" block (same style; warnings in yellow, not failing):

```python
    console.print("[bold]Project tools[/bold]")
    tools_findings = _project_tools_findings(registered_repos)
    if not tools_findings:
        console.print("  [green][OK][/green] no registered repositories to check")
    for finding in tools_findings:
        subject = escape(str(finding["repo_id"]))
        if finding["failed"]:
            console.print(f"  [red][X] {subject}:[/red] {escape(finding['detail'])}")
            any_failed = True
        elif finding["status"] == "warning":
            console.print(f"  [yellow][!] {subject}:[/yellow] {escape(finding['detail'])}")
        else:
            console.print(f"  [green][OK][/green] {subject}: {escape(finding['detail'])}")
```

After `_schema_report`:

```python
def _tools_report(repo_root: Path) -> dict[str, Any]:
    """A repository's project tools for `config show`. Raises ProjectToolsError."""
    from devgraph.config.project_tools import load_project_tools, tools_file_path

    declared = load_project_tools(repo_root)
    if declared is None:
        return {"status": "absent", "tools_file": None, "tools": []}
    return {
        "status": "valid",
        "tools_file": str(tools_file_path(repo_root)),
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": [
                    {"name": p.name, "type": p.type, "required": p.required, "default": p.default}
                    for p in tool.parameters
                ],
                "max_rows": tool.max_rows,
                "timeout_s": tool.timeout_s,
            }
            for tool in declared.tools
        ],
    }
```

`config_show`: after the schema report succeeds, add

```python
    from devgraph.config.project_tools import TOOLS_FILENAME, ProjectToolsError

    if root is None:
        report["tools"] = None
    else:
        try:
            report["tools"] = _tools_report(root)
        except ProjectToolsError as exc:
            console.print(f"[red][X] Invalid project tools:[/red] {escape(str(exc))}")
            raise typer.Exit(code=1)
```

before the `--json` branch, and at the end of the human output:

```python
    tools = report["tools"]
    if tools is not None:
        if tools["status"] == "absent":
            console.print(f"No {TOOLS_FILENAME} — no project tools")
        else:
            table = Table(title=f"Project tools ({escape(tools['tools_file'])})")
            table.add_column("Name", style="cyan")
            table.add_column("Parameters")
            table.add_column("Max rows")
            table.add_column("Timeout")
            for tool in tools["tools"]:
                params = ", ".join(p["name"] + ("" if p["required"] else "?") for p in tool["parameters"]) or "—"
                table.add_row(tool["name"], params, str(tool["max_rows"]), f"{tool['timeout_s']}s")
            console.print(table)
```

Update the `config_show` docstring ("…and the repository's project tools").

`config_validate`: change `findings = _project_schema_findings(repos)` to `findings = _project_schema_findings(repos) + _project_tools_findings(repos)`; colour `"red" if failed else ("yellow" if status == "warning" else "green")`; docstring: "Fail-closed check of devgraph.schema.yaml and devgraph.tools.yaml."

Docs: README — add a short "Project tools (preview)" section after "Project schema": what the file declares (the spec's example), the rules (read-only, `$repo_id` injected, declared parameters, limits), that `devgraph config validate`/`show` and `doctor` check it, that a tool named like a built-in is reported and the built-in wins, and that **DevGraph does not serve these tools yet** (the tool plane is next). PROJECT_STATUS — a shipped bullet saying the same, and add `project_tools.py` to the `devgraph/config/` code-map bullet.

- [ ] **Step 4: Run** — `uv run pytest tests/cli tests/config tests/mcp -q` then `uv run pytest -q`.
- [ ] **Step 5: Commit** — stage the changed files; `git commit -m "Report project tool declarations in config and doctor"`.
