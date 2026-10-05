"""Optional per-project MCP tool declarations: `devgraph.tools.yaml`.

A repository may declare Cypher tools for its agents at its root. This module
is the file's format, loader and fail-closed validator; it serves nothing --
the MCP tool plane that will is a later unit. Every check here is static and
defence in depth: when tools are served they also run in a read transaction
with the server injecting `$repo_id`.

Note: the $repo_id check proves the query references the injected parameter,
not that it scopes every match — the tool plane's runtime (read transaction,
injected repo_id) remains the real gate.

Import this module directly, like `devgraph.config.project_schema`.
"""

from __future__ import annotations

import keyword
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator, model_validator

from devgraph.paths import is_within, read_bounded

from devgraph.config.yaml_bound import bounded_safe_load

TOOLS_FILENAME = "devgraph.tools.yaml"
TOOLS_VERSION = 1

#: The parameter the server always supplies; never declared, never overridable.
#: See module docstring for the presence-only guarantee.
INJECTED_PARAMETER = "repo_id"

#: Names starting with this collide with pydantic model internals when a tool schema is built.
RESERVED_PARAMETER_PREFIX = "model_"

PARAMETER_TYPES: tuple[str, ...] = ("string", "integer", "float", "boolean")
NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
MAX_DESCRIPTION_LENGTH = 1024
DEFAULT_MAX_ROWS = 100
MAX_ROWS_LIMIT = 1000
DEFAULT_TIMEOUT_S = 10
MAX_TIMEOUT_S = 60

#: Write, procedure and administration keywords a read-only tool may not
#: contain. CALL is refused outright (procedures and subqueries alike) to keep
#: the rule simple to audit; SHOW/TERMINATE would reach other sessions' queries.
WRITE_KEYWORDS: tuple[str, ...] = (
    "CREATE", "INSERT", "MERGE", "SET", "DELETE", "DETACH", "REMOVE", "DROP", "FOREACH", "CALL", "USE",
    "SHOW", "TERMINATE", "ALTER", "GRANT", "DENY", "REVOKE", "RENAME",
)

ToolsVersion = Literal[TOOLS_VERSION]  # Literal matches True/1.0 too; see ProjectTools._strict_version
ParameterType = Literal[PARAMETER_TYPES]
ScalarDefault = str | int | float | bool | None

# String literals, backtick-quoted names and comments: blanked before any
# keyword or parameter scan, so text inside them never counts.
_LITERALS = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|`[^`]*`|//[^\n]*|/\*.*?\*/", re.S)
# Lookarounds for keyword boundaries: not preceded by letter/underscore/dot/dollar (a property, projection or parameter), not followed by letter/digit/underscore.
_WRITES = re.compile(
    r"(?<![A-Za-z_.$])(?:LOAD\s+CSV)(?![A-Za-z0-9_])|(?<![A-Za-z_.$])(?:" + "|".join(WRITE_KEYWORDS) + r")(?![A-Za-z0-9_])",
    re.I
)
# Parameters: one comprehensive pattern to extract parameters without counting $ inside backtick identifiers.
# Alternatives in order: $`param` (captures param), $param (captures param), then non-capturing literals.
_PARAM_AND_LITERALS = re.compile(
    r"\$`([^`]*)`"  # backtick-quoted parameter: capture inside backticks
    r"|\$([^\W\d]\w*)"  # regular parameter: capture identifier (Unicode-aware)
    r"|" + _LITERALS.pattern,  # reuse literal pattern (matched but not captured)
    re.UNICODE | re.S  # Unicode-aware and DOTALL
)
# APOC references: apoc followed by optional whitespace and dot.
# Pattern checks negative lookbehind to avoid matching word characters, dots, or dollar.
_APOC = re.compile(r"(?<![A-Za-z_.$])apoc\s*\.", re.I)


# What `yaml.safe_load` can raise on bad input: besides yaml.YAMLError, a constructor
# raises ValueError/TypeError/AttributeError (e.g. `2001-13-45`, `!!int 0x`) and deep
# nesting raises RecursionError. Every one of them means "this file is malformed".
YAML_LOAD_ERRORS: tuple[type[BaseException], ...] = (yaml.YAMLError, ValueError, TypeError, AttributeError, RecursionError)


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
    """Extract `$name` and `$`name`` parameters, ignoring strings and comments."""
    result = set()
    for match in _PARAM_AND_LITERALS.finditer(query):
        # Groups 1 and 2 are parameter captures; remaining groups are from literal alternatives
        param = match.group(1) or match.group(2)
        if param:
            result.add(param)
    return result


def has_apoc(query: str) -> bool:
    """True if the query references apoc, ignoring strings and comments.

    Backticks are removed (not blanked) so `apoc` reads as apoc.
    """
    # Blank string literals and comments, but remove backtick characters
    blanked = re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|//[^\n]*|/\*.*?\*/",
                     lambda m: " " * len(m.group(0)), query, flags=re.S)
    blanked = blanked.replace("`", "")
    return bool(_APOC.search(blanked))


def _check_plain_text(value: Any, info: ValidationInfo) -> Any:
    """Refuse control characters (other than newline and tab), Unicode format characters,
    lone surrogates, and private-use or unassigned code points.

    The trust prompt shows these fields; a terminal escape or bidi control could make
    what it shows differ from what runs, and a lone surrogate can't be printed at all.
    """
    if isinstance(value, str):
        for offset, char in enumerate(value):
            category = unicodedata.category(char)
            if char not in "\n\t" and category in ("Cc", "Cf"):
                kind = "control or format character"
            elif category in ("Cs", "Co", "Cn"):
                kind = "surrogate, private-use or unassigned character"
            else:
                continue
            raise ValueError(f"{info.field_name} contains a {kind} (U+{ord(char):04X} at offset {offset}); remove it")
    return value


def _check_name(value: str, kind: str) -> str:
    if not NAME_PATTERN.fullmatch(value):
        raise ValueError(f"{kind} {value!r} must fullmatch {NAME_PATTERN.pattern}")
    return value


class ToolParameter(BaseModel):
    """One caller-supplied parameter of a project tool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    type: ParameterType = "string"
    required: bool = Field(True, strict=True)
    default: ScalarDefault = None
    description: str | None = None

    _plain_text = field_validator("name", "description", "default", mode="before")(_check_plain_text)

    @field_validator("description")
    @classmethod
    def _capped_description(cls, value: str | None) -> str | None:
        if value is not None and len(value) > MAX_DESCRIPTION_LENGTH:
            raise ValueError(f"description must be at most {MAX_DESCRIPTION_LENGTH} characters")
        return value

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        _check_name(value, "parameter name")
        if keyword.iskeyword(value):
            raise ValueError(f"parameter name {value!r} is a Python keyword and cannot be used")
        if value.startswith(RESERVED_PARAMETER_PREFIX):
            raise ValueError(
                f"parameter name {value!r} starts with {RESERVED_PARAMETER_PREFIX!r}, which is reserved "
                f"for the tool schema model, and cannot be used"
            )
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

    _plain_text = field_validator("name", "description", "cypher", mode="before")(_check_plain_text)

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
        if has_apoc(self.cypher):
            raise ValueError(
                f"tool {self.name!r} must be read-only; apoc references are not allowed"
            )
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

    @field_validator("version", mode="before")
    @classmethod
    def _strict_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError(f"version must be the integer {TOOLS_VERSION}")
        return value

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


def tools_file_outside(repo_root: Path) -> bool:
    """True when the tools file exists but resolves outside the repository (e.g. a symlink out).

    Such a file is treated as unreadable: never served, trusted or shown.
    """
    path = tools_file_path(repo_root)
    try:
        return os.path.lexists(path) and not is_within(path.resolve(), Path(repo_root))
    except (OSError, RuntimeError):
        return True


def load_project_tools(repo_root: Path) -> ProjectTools | None:
    """Load and validate `devgraph.tools.yaml`; None if and only if it is absent."""
    path = tools_file_path(repo_root)
    try:
        if not path.exists():
            return None
        if tools_file_outside(repo_root):
            raise ProjectToolsError(f"{path}: tools file must be inside the repository")
        if not path.is_file():
            raise ProjectToolsError(f"{path}: tools file is not a regular file")
        text = read_bounded(path).decode("utf-8")
    except OSError as exc:
        raise ProjectToolsError(f"{path}: cannot be read: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ProjectToolsError(f"{path}: is not valid UTF-8: {exc}") from exc
    return parse_project_tools(text, path)


def parse_project_tools(text: str, path: Path) -> ProjectTools:
    """Parse and validate the text of a tools file; `path` only labels errors."""
    try:
        document = bounded_safe_load(text)
    except YAML_LOAD_ERRORS as exc:
        raise ProjectToolsError(f"{path}: malformed YAML: {exc}") from exc
    if document is None:
        raise ProjectToolsError(f"{path}: the file is empty; delete it, or declare 'version: {TOOLS_VERSION}'")
    if not isinstance(document, dict):
        raise ProjectToolsError(f"{path}: expected a YAML mapping at the document root, found {type(document).__name__}")
    return validate_project_tools(document, path)


def validate_project_tools(document: dict, path: Path) -> ProjectTools:
    """Validate an already-loaded tools document; `path` only labels errors."""
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
