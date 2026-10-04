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
