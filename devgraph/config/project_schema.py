"""Optional per-project graph schema declarations.

A repository may place a `devgraph.schema.yaml` file at its root to declare
extra node types and relationships on top of — or instead of — DevGraph's
built-in labels. This module is that file's format, loader and fail-closed
validator, and nothing more: resolving a project schema itself changes no
indexing behaviour, opens no file the declaration names, and runs no code.
Only the providers in devgraph/indexer/providers/ turn filesystem- and
docs-sourced declarations into indexing; a docs source is plain data (globs,
text conditions and a field map), never a pattern engine, because the file
ships inside the repository and is not trust-gated. A custom provider
declaration is validated as inert data only.

Built-in labels, relationship types and constraint statements are always
imported from `devgraph.graph.schema`, never restated here, so a repository
*without* a schema file resolves to exactly today's behaviour.

Import this module directly (`from devgraph.config.project_schema import
...`) rather than adding a re-export to `devgraph.config`: `get_settings`
is imported from that package by the watcher, the CLI, the indexer
dispatcher, the agent entry points and `scripts/query.py`, and re-exporting
the loader there would pull `devgraph.graph` — and with it the Neo4j driver
— into every one of them.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from devgraph.config.project_switch import project_config_enabled
from devgraph.config.project_tools import YAML_LOAD_ERRORS
from devgraph.config.yaml_bound import bounded_safe_load
from devgraph.graph.schema import (
    NODE_LABELS,
    RELATIONSHIP_TYPES,
    RESERVED_NODE_PROPERTIES,
)
from devgraph.graph.schema import constraint_statements as builtin_constraint_statements
from devgraph.paths import is_within, read_bounded

SCHEMA_FILENAME = "devgraph.schema.yaml"
SCHEMA_VERSION = 1

#: How a declaration relates to the built-in schema. "default" inherits the
#: built-in labels, relationship types and constraints; "none" inherits
#: nothing and must therefore declare at least one node type.
EXTENDS_MODES: tuple[str, ...] = ("default", "none")

#: Who produces a declared relationship. "builtin" reuses one of DevGraph's
#: own relationship types; "custom" names an out-of-tree provider that this
#: module records as data and never loads; "filesystem" links each
#: filesystem node to its parent folder; "docs" links a docs node to the
#: nodes a front-matter field names (devgraph/indexer/providers/).
PROVIDER_KINDS: tuple[str, ...] = ("builtin", "custom", "filesystem", "docs")

#: Where a user-declared node type's nodes come from: "filesystem" (one node
#: per file or folder) or "docs" (one node per matching Markdown file, filled
#: from its front matter). Built-in labels are produced by DevGraph's own
#: extractors and never declare a source.
NODE_SOURCE_PROVIDERS: tuple[str, ...] = ("filesystem", "docs")

#: What a filesystem-sourced node type represents.
FILESYSTEM_KINDS: tuple[str, ...] = ("file", "folder")

#: The one key every filesystem node type must declare: its repo-relative path.
FILESYSTEM_KEY: tuple[str, ...] = ("path",)

#: The plain text tests a docs `where` condition may use. There is
#: deliberately no regex: the schema is untrusted and `re` has no timeout.
CONDITION_OPERATORS: tuple[str, ...] = ("is", "starts_with", "contains", "like")

#: Bounds on a docs source, so a hostile schema can't make matching costly.
MAX_DOCS_PATHS = 20
MAX_GLOB_LENGTH = 200
MAX_GLOBSTARS = 2
MAX_CONDITIONS = 20
MAX_CONDITION_TEXT = 200
MAX_FIELD_MAP = 50
MAX_FRONT_MATTER_KEY_LENGTH = 64
#: Bounds across a whole schema, so the condition work per file stays small:
#: at most this many docs types and conditions in all, and `*`s per `like`.
MAX_DOCS_TYPES = 20
MAX_SCHEMA_CONDITIONS = 100
MAX_LIKE_STARS = 10

#: The whole-number range Neo4j stores, and the range docs values are compared and written in.
INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1

#: Value types a declared metadata field may hold.
METADATA_TYPES: tuple[str, ...] = ("string", "integer", "float", "boolean")

# Every label, relationship type and property name from a project file is
# interpolated into Cypher by `EffectiveSchema.constraint_statements`, so
# each one must fullmatch a conservative, explicitly length-bounded
# identifier pattern before it gets anywhere near a statement.
MAX_IDENTIFIER_LENGTH = 64
_BOUND = MAX_IDENTIFIER_LENGTH - 1

LABEL_PATTERN = re.compile(rf"[A-Za-z][A-Za-z0-9_]{{0,{_BOUND}}}")
RELATIONSHIP_TYPE_PATTERN = re.compile(rf"[A-Z][A-Z0-9_]{{0,{_BOUND}}}")
PROPERTY_NAME_PATTERN = re.compile(rf"[a-z][a-z0-9_]{{0,{_BOUND}}}")

#: Suffix for the uniqueness constraint generated for a user node type.
#: Deliberately distinct from the built-in `_repo_name`/`_repo_name_file`
#: suffixes, because a user key is not necessarily `name`.
USER_CONSTRAINT_SUFFIX = "_repo_key"

#: Suffix for the lookup index generated for a filesystem-sourced node type.
#: The provider MERGEs and MATCHes on (repo_id, name) while the declared key
#: constraint covers (repo_id, path), so without this every write is a label
#: scan across all repositories. Indexes share a name space with constraints.
FILESYSTEM_INDEX_SUFFIX = "_repo_name"

_CONSTRAINT_NAME_PATTERN = re.compile(r"^(?:CREATE|DROP) CONSTRAINT (\w+) ")

# Literal aliases are derived from the constants above rather than
# restating their values, so the emitted JSON Schema and the constants
# cannot drift apart.
SchemaVersion = Literal[SCHEMA_VERSION]
ExtendsMode = Literal[EXTENDS_MODES]
ProviderKind = Literal[PROVIDER_KINDS]
FilesystemKind = Literal[FILESYSTEM_KINDS]
MetadataType = Literal[METADATA_TYPES]

ScalarParam = str | int | float | bool | None


class ProjectSchemaError(Exception):
    """A project schema file is absent-but-unreadable, malformed or invalid.

    Raised instead of returning a partial schema: every failure path here
    is fail-closed.
    """


def _require_identifier(value: str, pattern: re.Pattern[str], kind: str) -> str:
    if not pattern.fullmatch(value):
        raise ValueError(
            f"{kind} {value!r} is not a valid identifier: it must fullmatch "
            f"{pattern.pattern} and be at most {MAX_IDENTIFIER_LENGTH} characters"
        )
    return value


class MetadataField(BaseModel):
    """One user-declared property on a user-declared node type."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    type: MetadataType = "string"
    required: bool = False
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        _require_identifier(value, PROPERTY_NAME_PATTERN, "metadata field")
        if value in RESERVED_NODE_PROPERTIES:
            raise ValueError(
                f"metadata field {value!r} is reserved by DevGraph and cannot be "
                f"redeclared; reserved names are "
                f"{', '.join(sorted(RESERVED_NODE_PROPERTIES))}"
            )
        return value


COLOR_PATTERN = r"^#[0-9a-fA-F]{6}$"


def _check_color(value: object) -> object:
    if isinstance(value, str) and not re.fullmatch(COLOR_PATTERN, value):
        raise ValueError(f"color {value!r} must be a hex colour written as #rrggbb")
    return value


def _has_control_character(text: str) -> bool:
    """Whether `text` holds a C0, DEL or C1 control character."""
    return any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in text)


def _has_lone_surrogate(text: str) -> bool:
    """Whether `text` holds a lone surrogate (from a YAML "\\ud800" escape), which can't be printed or encoded."""
    return any(0xD800 <= ord(c) <= 0xDFFF for c in text)


def _has_format_character(text: str) -> bool:
    """Whether `text` holds a format character (Unicode category Cf, such as U+202E), which reorders or hides text."""
    return any(unicodedata.category(c) == "Cf" for c in text)


def _front_matter_key_problem(key: str) -> str | None:
    """Why `key` can't name a front-matter key, in plain words, or None.

    A key that is too long is not echoed, so the message stays short.
    """
    if not key:
        return "an empty front-matter key"
    if len(key) > MAX_FRONT_MATTER_KEY_LENGTH:
        return f"a front-matter key longer than {MAX_FRONT_MATTER_KEY_LENGTH} characters"
    if _has_control_character(key):
        return f"a front-matter key with a control character ({key!r})"
    if _has_lone_surrogate(key):
        return "a front-matter key with a lone surrogate"
    if _has_format_character(key):
        return "a front-matter key with a format character (such as a right-to-left mark)"
    return None


def _glob_problem(glob: str) -> str | None:
    """Why `glob` can't be a docs `paths` entry, in plain words, or None.

    Globs are matched one folder at a time (devgraph/indexer/providers/docs.py),
    so `**` is capped: each one multiplies the folder positions tried.
    """
    if not glob:
        return "is empty"
    if len(glob) > MAX_GLOB_LENGTH:
        return f"is longer than {MAX_GLOB_LENGTH} characters"
    if _has_control_character(glob):
        return "contains a control character"
    if _has_lone_surrogate(glob):
        return "contains a lone surrogate"
    if _has_format_character(glob):
        return "contains a format character (such as a right-to-left mark)"
    if "\\" in glob:
        return f"({glob!r}) contains a backslash; separate folders with /"
    if glob.startswith("/"):
        return f"({glob!r}) must be repo-relative: it may not start with /"
    segments = glob.split("/")
    if ".." in segments:
        return f"({glob!r}) may not contain a '..' folder"
    if "." in segments:
        return (
            f"({glob!r}) has a '.' segment; write it relative to the repository root "
            f"without './' (e.g. runbooks/**/*.md)"
        )
    if "" in segments:
        return f"({glob!r}) has an empty folder name; separate folders with a single / and don't end with /"
    if segments.count("**") > MAX_GLOBSTARS:
        return f"({glob!r}) uses ** more than {MAX_GLOBSTARS} times"
    return None


class FilesystemSource(BaseModel):
    """A node type with one node per repository file or folder."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Literal["filesystem"]
    kind: FilesystemKind


class Condition(BaseModel):
    """One docs `where` test: a front-matter field compared with plain text.

    Exactly one operator is set (checked with the node type's label in
    `NodeTypeDecl`), and an operator written with no value (`is: null`) is
    refused there too rather than ignored. An integer or boolean operand is
    stored as its canonical text, because values are compared as text on
    both sides. `is` is a Python keyword, so its attribute is `is_` with
    the YAML name `is` as its alias.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    field: str
    is_: str | None = Field(default=None, alias="is")
    starts_with: str | None = None
    contains: str | None = None
    like: str | None = None

    @field_validator("is_", "starts_with", "contains", "like", mode="before")
    @classmethod
    def _canonical_text(cls, value: object) -> object:
        if type(value) is bool:
            return "true" if value else "false"
        if type(value) is int:
            # Checked before str(): YAML 1.1 hex, binary and sexagesimal
            # integers have no size limit, but str() refuses huge ones.
            if not INT64_MIN <= value <= INT64_MAX:
                raise ValueError("a whole number in a condition must fit in 64 bits")
            return str(value)
        return value

    @property
    def operators(self) -> tuple[str, ...]:
        """The operators set on this condition (exactly one once validated)."""
        values = (self.is_, self.starts_with, self.contains, self.like)
        return tuple(op for op, value in zip(CONDITION_OPERATORS, values) if value is not None)

    @property
    def operator(self) -> str:
        return self.operators[0]

    @property
    def text(self) -> str:
        return {"is": self.is_, "starts_with": self.starts_with, "contains": self.contains, "like": self.like}[
            self.operator
        ]


class DocsSource(BaseModel):
    """A node type with one node per matching Markdown file, from its front matter.

    `paths` selects files by glob, `where` filters them on front-matter
    values, and `fields` maps a metadata field to a differently named
    front-matter key. The bounds are checked, with the label in the message,
    by `NodeTypeDecl`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Literal["docs"]
    paths: tuple[str, ...]
    where: tuple[Condition, ...] = ()
    fields: dict[str, str] = Field(default_factory=dict)


#: Where a user-declared node type's nodes are extracted from.
NodeSource = Annotated[FilesystemSource | DocsSource, Field(discriminator="provider")]

#: How each source provider is named in messages.
_SOURCE_WORDS = {"filesystem": "the filesystem", "docs": "Markdown front matter"}


class NodeTypeDecl(BaseModel):
    """A user-declared node label with a stable key and metadata fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    key: tuple[str, ...]
    metadata: tuple[MetadataField, ...] = ()
    description: str | None = None
    source: NodeSource | None = None
    color: str | None = Field(default=None, pattern=COLOR_PATTERN)

    _check_color = field_validator("color", mode="before")(_check_color)

    @field_validator("label")
    @classmethod
    def _check_label(cls, value: str) -> str:
        return _require_identifier(value, LABEL_PATTERN, "node type label")

    @field_validator("source", mode="before")
    @classmethod
    def _check_source_provider(cls, value: object) -> object:
        # Before the discriminator sees it: pydantic's own message formats the
        # tag, which for a huge YAML integer fails with a stderr traceback.
        if isinstance(value, dict) and "provider" in value and value["provider"] not in NODE_SOURCE_PROVIDERS:
            raise ValueError(f"source provider must be one of {', '.join(NODE_SOURCE_PROVIDERS)}")
        return value

    @property
    def metadata_by_name(self) -> dict[str, MetadataField]:
        """Declared fields by name, with exact duplicates collapsed.

        Conflicting duplicates never reach here — they fail validation.
        """
        return {field_decl.name: field_decl for field_decl in self.metadata}

    @model_validator(mode="after")
    def _check_key_and_metadata(self) -> NodeTypeDecl:
        by_name: dict[str, MetadataField] = {}
        for field_decl in self.metadata:
            existing = by_name.get(field_decl.name)
            if existing is not None and existing != field_decl:
                raise ValueError(
                    f"node type {self.label!r} declares metadata field "
                    f"{field_decl.name!r} twice with different definitions"
                )
            by_name[field_decl.name] = field_decl

        if not self.key:
            raise ValueError(
                f"node type {self.label!r} must declare a non-empty key: "
                f"without one its nodes have no stable identity"
            )

        seen: set[str] = set()
        for component in self.key:
            _require_identifier(component, PROPERTY_NAME_PATTERN, "key component")
            if component in RESERVED_NODE_PROPERTIES:
                raise ValueError(
                    f"node type {self.label!r} key component {component!r} is "
                    f"reserved by DevGraph and cannot be redeclared"
                )
            if component in seen:
                raise ValueError(
                    f"node type {self.label!r} lists key component "
                    f"{component!r} more than once"
                )
            seen.add(component)
            if component not in by_name:
                raise ValueError(
                    f"node type {self.label!r} key component {component!r} is "
                    f"not a declared metadata field"
                )
        if isinstance(self.source, DocsSource):
            self._check_docs_key(by_name)
        elif self.source is not None:
            origin = _SOURCE_WORDS[self.source.provider]
            if self.key != FILESYSTEM_KEY:
                raise ValueError(
                    f"node type {self.label!r} is sourced from {origin}, so "
                    f"its key must be exactly [path]: the provider writes one node "
                    f"per repo-relative path"
                )
            if by_name["path"].type != "string":
                raise ValueError(
                    f"node type {self.label!r} is sourced from {origin}, so "
                    f"its path metadata field must be a string"
                )
        if isinstance(self.source, DocsSource):
            self._check_docs_source(self.source, by_name)
        return self

    def _check_docs_key(self, by_name: dict[str, MetadataField]) -> None:
        """A docs type is keyed on [path] or on one string field read from front matter."""
        label = self.label
        if len(self.key) != 1:
            raise ValueError(
                f"node type {label!r} is sourced from Markdown front matter, so its key must be "
                f"[path] or one string field read from front matter (such as [adr_id])"
            )
        (component,) = self.key
        if component != "path" and by_name[component].type != "string":
            raise ValueError(
                f"key field {component!r} of {label!r} must be a string: keys are compared as "
                f"text (use string; numbers like 12 still work)"
            )
        path = by_name.get("path")
        if path is None or path.type != "string":
            raise ValueError(
                f"node type {label!r} is sourced from Markdown front matter, so it must declare "
                f"a string 'path' field: every entry records its file there"
            )

    def _check_docs_source(self, source: DocsSource, by_name: dict[str, MetadataField]) -> None:
        label = self.label
        if not 1 <= len(source.paths) <= MAX_DOCS_PATHS:
            raise ValueError(
                f"paths of {label!r} must list 1 to {MAX_DOCS_PATHS} globs; found "
                f"{len(source.paths)} (use **/*.md for every folder)"
            )
        for index, glob in enumerate(source.paths):
            problem = _glob_problem(glob)
            if problem is not None:
                raise ValueError(f"paths[{index}] of {label!r} {problem}")

        if len(source.where) > MAX_CONDITIONS:
            raise ValueError(
                f"where of {label!r} lists {len(source.where)} conditions; at most {MAX_CONDITIONS}"
            )
        for index, condition in enumerate(source.where):
            for operator, attribute in zip(CONDITION_OPERATORS, ("is_", "starts_with", "contains", "like")):
                if attribute in condition.model_fields_set and getattr(condition, attribute) is None:
                    raise ValueError(
                        f"where[{index}] of {label!r} gives {operator!r} no value; give it text or remove it"
                    )
            if len(condition.operators) != 1:
                raise ValueError(
                    f"where[{index}] of {label!r} must use exactly one of "
                    f"{', '.join(CONDITION_OPERATORS)}"
                )
            if len(condition.text) > MAX_CONDITION_TEXT:
                raise ValueError(
                    f"where[{index}] of {label!r} compares with text longer than "
                    f"{MAX_CONDITION_TEXT} characters"
                )
            if _has_lone_surrogate(condition.text):
                raise ValueError(f"where[{index}] of {label!r} compares with text that has a lone surrogate")
            if condition.like is not None and condition.like.count("*") > MAX_LIKE_STARS:
                raise ValueError(f"where[{index}] of {label!r} uses * more than {MAX_LIKE_STARS} times")
            problem = _front_matter_key_problem(condition.field)
            if problem is not None:
                raise ValueError(f"where[{index}] of {label!r} names {problem}")

        if len(source.fields) > MAX_FIELD_MAP:
            raise ValueError(
                f"fields of {label!r} maps {len(source.fields)} fields; at most {MAX_FIELD_MAP}"
            )
        for name, key in source.fields.items():
            if name == "path":
                raise ValueError(
                    f"fields of {label!r} maps 'path', which is always the file's "
                    f"repo-relative path"
                )
            if name not in by_name:
                raise ValueError(
                    f"fields of {label!r} maps {name!r}, which is not a declared metadata field"
                )
            problem = _front_matter_key_problem(key)
            if problem is not None:
                raise ValueError(f"fields of {label!r} maps {name!r} to {problem}")


class CustomProvider(BaseModel):
    """An out-of-tree relationship provider, recorded purely as data.

    Nothing in this module resolves, loads or runs the named provider; a
    later unit decides what, if anything, `name` refers to.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    params: dict[str, ScalarParam] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _require_identifier(value, PROPERTY_NAME_PATTERN, "custom provider name")

    @field_validator("params")
    @classmethod
    def _check_params(cls, value: dict[str, ScalarParam]) -> dict[str, ScalarParam]:
        for key in value:
            _require_identifier(key, PROPERTY_NAME_PATTERN, "custom provider parameter")
        return value


class RelationshipDecl(BaseModel):
    """A user-declared relationship between two effective node labels."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str
    from_: str | tuple[str, ...] = Field(alias="from")
    to: str
    provider: ProviderKind = "builtin"
    custom: CustomProvider | None = None
    field: str | None = None
    color: str | None = Field(default=None, pattern=COLOR_PATTERN)

    _check_color = field_validator("color", mode="before")(_check_color)

    @field_validator("type")
    @classmethod
    def _check_type(cls, value: str) -> str:
        return _require_identifier(value, RELATIONSHIP_TYPE_PATTERN, "relationship type")

    @field_validator("to")
    @classmethod
    def _check_endpoint(cls, value: str) -> str:
        return _require_identifier(value, LABEL_PATTERN, "relationship endpoint label")

    @field_validator("from_")
    @classmethod
    def _check_from(cls, value: str | tuple[str, ...]) -> str | tuple[str, ...]:
        labels = (value,) if isinstance(value, str) else value
        if not labels:
            raise ValueError("relationship 'from' must name at least one label")
        if len(set(labels)) != len(labels):
            raise ValueError("relationship 'from' lists a label more than once")
        for label in labels:
            _require_identifier(label, LABEL_PATTERN, "relationship endpoint label")
        return value

    @property
    def from_labels(self) -> tuple[str, ...]:
        """`from` as a tuple, whether it was written as one label or a list."""
        return (self.from_,) if isinstance(self.from_, str) else tuple(self.from_)

    @model_validator(mode="after")
    def _check_provider(self) -> RelationshipDecl:
        if self.provider != "docs" and self.field is not None:
            raise ValueError(
                f"relationship {self.type!r} uses the {self.provider} provider; "
                f"only docs relationships take a 'field'"
            )
        if self.provider == "builtin":
            if self.custom is not None:
                raise ValueError(
                    f"relationship {self.type!r} uses the builtin provider, "
                    f"which must not declare a custom block"
                )
            if self.type not in RELATIONSHIP_TYPES:
                raise ValueError(
                    f"relationship {self.type!r} uses the builtin provider but is "
                    f"not a built-in relationship type; known types are "
                    f"{', '.join(RELATIONSHIP_TYPES)}"
                )
        elif self.provider == "custom":
            if self.custom is None:
                raise ValueError(
                    f"relationship {self.type!r} uses the custom provider, which "
                    f"requires a custom block naming it"
                )
            if self.type in RELATIONSHIP_TYPES:
                raise ValueError(
                    f"relationship type {self.type!r} is built-in and cannot be "
                    f"redeclared by a custom provider"
                )
        else:
            if self.custom is not None:
                raise ValueError(
                    f"relationship {self.type!r} uses the {self.provider} provider, "
                    f"which must not declare a custom block"
                )
            if self.type in RELATIONSHIP_TYPES:
                raise ValueError(
                    f"relationship type {self.type!r} is built-in and cannot be "
                    f"redeclared by the {self.provider} provider"
                )
            if self.provider == "docs":
                if self.field is None:
                    raise ValueError(
                        f"relationship {self.type!r} uses the docs provider, so it "
                        f"needs a 'field': the front-matter key naming the target"
                    )
                problem = _front_matter_key_problem(self.field)
                if problem is not None:
                    raise ValueError(f"relationship {self.type!r} field is {problem}")
        return self


class ProjectSchema(BaseModel):
    """A validated `devgraph.schema.yaml` document.

    Structural and intra-document invariants only; the cross-cutting ones
    that depend on `extends` live in `resolve_declaration`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: SchemaVersion
    extends: ExtendsMode = "default"
    node_types: tuple[NodeTypeDecl, ...] = ()
    relationships: tuple[RelationshipDecl, ...] = ()

    @model_validator(mode="after")
    def _check_labels(self) -> ProjectSchema:
        # Case-insensitive, and unconditional on `extends`: constraint names
        # are lower-cased, and built-in constraints exist in the same Neo4j
        # database whether or not this document inherits them.
        builtin_by_fold = {label.casefold(): label for label in NODE_LABELS}
        seen: dict[str, str] = {}
        for node_type in self.node_types:
            folded = node_type.label.casefold()
            builtin = builtin_by_fold.get(folded)
            if builtin is not None:
                raise ValueError(
                    f"node type label {node_type.label!r} collides with the "
                    f"built-in label {builtin!r}; built-in labels cannot be "
                    f"redeclared, and the comparison ignores case because "
                    f"generated constraint names are lower-cased"
                )
            other = seen.get(folded)
            if other is not None:
                raise ValueError(
                    f"node type label {node_type.label!r} collides with "
                    f"{other!r}; two labels must differ by more than case"
                )
            seen[folded] = node_type.label

        if self.extends == "none" and not self.node_types:
            raise ValueError(
                "extends: none inherits no built-in labels, so at least one "
                "node type must be declared"
            )
        return self

    @model_validator(mode="after")
    def _check_filesystem(self) -> ProjectSchema:
        by_kind: dict[str, str] = {}
        for node_type in self.node_types:
            if not isinstance(node_type.source, FilesystemSource):
                continue
            kind = node_type.source.kind
            if kind in by_kind:
                raise ValueError(
                    f"node types {by_kind[kind]!r} and {node_type.label!r} are both "
                    f"filesystem {kind} types; declare at most one"
                )
            by_kind[kind] = node_type.label

        filesystem_relationships = [r for r in self.relationships if r.provider == "filesystem"]
        if len(filesystem_relationships) > 1:
            raise ValueError(
                "at most one filesystem relationship may be declared; found "
                + ", ".join(repr(r.type) for r in filesystem_relationships)
            )
        filesystem_labels = set(by_kind.values())
        folder = by_kind.get("folder")
        for relationship in filesystem_relationships:
            if folder is None or relationship.to != folder:
                where = f" {folder!r}" if folder else ", and none is declared"
                raise ValueError(
                    f"filesystem relationship {relationship.type!r} must point to "
                    f"the filesystem folder node type{where}"
                )
            for label in relationship.from_labels:
                if label not in filesystem_labels:
                    raise ValueError(
                        f"filesystem relationship {relationship.type!r} from label "
                        f"{label!r} is not a filesystem node type"
                    )
        return self

    @model_validator(mode="after")
    def _check_docs(self) -> ProjectSchema:
        sources = [n.source for n in self.node_types if isinstance(n.source, DocsSource)]
        if len(sources) > MAX_DOCS_TYPES:
            raise ValueError(
                f"{len(sources)} node types are sourced from Markdown front matter; at most {MAX_DOCS_TYPES}"
            )
        conditions = sum(len(source.where) for source in sources)
        if conditions > MAX_SCHEMA_CONDITIONS:
            raise ValueError(
                f"the docs node types list {conditions} conditions in all; at most {MAX_SCHEMA_CONDITIONS}"
            )
        docs_labels = {n.label for n in self.node_types if isinstance(n.source, DocsSource)}
        for relationship in self.relationships:
            if relationship.provider != "docs":
                continue
            for label in relationship.from_labels:
                if label not in docs_labels:
                    raise ValueError(
                        f"docs relationship {relationship.type!r} from label {label!r} "
                        f"is not a node type sourced from Markdown front matter"
                    )
        return self


@dataclass(frozen=True, slots=True)
class EffectiveSchema:
    """Built-in defaults resolved against an optional project declaration."""

    extends: str
    node_labels: tuple[str, ...]
    relationship_types: tuple[str, ...]
    node_types: tuple[NodeTypeDecl, ...] = ()
    relationships: tuple[RelationshipDecl, ...] = ()

    def constraint_statements(self) -> list[str]:
        """Cypher uniqueness constraints for the effective schema.

        With `extends: default` the built-in statements come through
        byte-identically and first, so a repository with no project file
        provisions exactly what it always did.
        """
        statements = (
            list(builtin_constraint_statements()) if self.extends == "default" else []
        )
        for node_type in self.node_types:
            statements.append(_user_constraint_statement(node_type))
            if node_type.source is not None:
                statements.append(_filesystem_index_statement(node_type))
        return statements


def _user_constraint_name(label: str) -> str:
    return f"{label.lower()}{USER_CONSTRAINT_SUFFIX}"


def _filesystem_index_name(label: str) -> str:
    return f"{label.lower()}{FILESYSTEM_INDEX_SUFFIX}"


def _filesystem_index_statement(node_type: NodeTypeDecl) -> str:
    return (
        f"CREATE INDEX {_filesystem_index_name(node_type.label)} IF NOT EXISTS "
        f"FOR (n:{node_type.label}) ON (n.repo_id, n.name)"
    )


def _user_constraint_statement(node_type: NodeTypeDecl) -> str:
    # repo_id is prepended so a user type is scoped per repository exactly
    # like every built-in label.
    properties = ", ".join(f"n.{c}" for c in ("repo_id", *node_type.key))
    return (
        f"CREATE CONSTRAINT {_user_constraint_name(node_type.label)} IF NOT EXISTS "
        f"FOR (n:{node_type.label}) REQUIRE ({properties}) IS UNIQUE"
    )


def _builtin_constraint_names() -> set[str]:
    """Constraint names the built-in schema already uses.

    Derived from `constraint_statements()` rather than re-deriving the
    naming rule, so a change there cannot silently let a user type generate
    a colliding name.
    """
    names = set()
    for statement in builtin_constraint_statements():
        match = _CONSTRAINT_NAME_PATTERN.match(statement)
        if match is not None:
            names.add(match.group(1))
    return names


def project_schema_path(repo_root: Path) -> Path:
    """Where a repository's optional schema file lives."""
    return Path(repo_root) / SCHEMA_FILENAME


#: `schema_file_hash` of a repository with no schema file.
ABSENT_SCHEMA_HASH = "absent"


def schema_file_hash(repo_root: Path) -> str:
    """Fingerprint of the schema file's bytes: `sha256:<hex>`, or `absent`.

    An unreadable path (a directory, a permission error) gets a distinct
    `unreadable:<error>` value so it never equals a hash the graph was
    actually built with. A repository whose project config is switched off
    reports `absent`. A file that resolves outside the repository, is not a
    regular file (a FIFO, or a symlink to a device) or is larger than
    `MAX_CONFIG_BYTES` is unreadable and never read.
    """
    if not project_config_enabled(repo_root):
        return ABSENT_SCHEMA_HASH
    path = project_schema_path(repo_root)
    try:
        if os.path.lexists(path) and not is_within(path.resolve(), repo_root):
            return "unreadable:outside_repository"
        data = read_bounded(path)
    except FileNotFoundError:
        return ABSENT_SCHEMA_HASH
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}"
    return "sha256:" + hashlib.sha256(data).hexdigest()


def load_project_schema(repo_root: Path, *, respect_switch: bool = True) -> ProjectSchema | None:
    """Load and validate `devgraph.schema.yaml`, if the repository has one.

    Returns `None` if and only if the file is absent or the repository's
    project config is switched off (unless `respect_switch` is False). An
    empty, malformed, non-mapping or invalid file, or one that resolves
    outside the repository, raises `ProjectSchemaError`; no partial schema
    is ever returned.
    """
    if respect_switch and not project_config_enabled(repo_root):
        return None
    path = project_schema_path(repo_root)
    try:
        if not path.exists():
            return None
        if not path.is_file():
            raise ProjectSchemaError(f"{path}: project schema is not a regular file")
        if not is_within(path.resolve(), repo_root):
            raise ProjectSchemaError(f"{path}: project schema must be inside the repository")
        text = read_bounded(path).decode("utf-8")
    except OSError as exc:
        raise ProjectSchemaError(f"{path}: cannot be read: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ProjectSchemaError(f"{path}: is not valid UTF-8: {exc}") from exc

    return parse_project_schema(text, path)


def parse_project_schema(text: str, path: Path) -> ProjectSchema:
    """Parse and validate the text of a schema file; `path` only labels errors."""
    try:
        document = bounded_safe_load(text)
    except YAML_LOAD_ERRORS as exc:
        raise ProjectSchemaError(f"{path}: malformed YAML: {exc}") from exc

    if document is None:
        raise ProjectSchemaError(
            f"{path}: the file is empty; delete it to use the built-in schema, "
            f"or declare 'version: {SCHEMA_VERSION}'"
        )
    if not isinstance(document, dict):
        raise ProjectSchemaError(
            f"{path}: expected a YAML mapping at the document root, found "
            f"{type(document).__name__}"
        )

    try:
        return ProjectSchema.model_validate(document)
    except ValidationError as exc:
        raise ProjectSchemaError(_format_validation_error(path, exc)) from exc


#: Added where a name was expected but YAML 1.1 read an unquoted word as a boolean.
_BOOLEAN_WORD_HINT = (
    "YAML reads an unquoted on, off, yes or no as true or false; to mean the word, quote it: 'on'"
)


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    lines = [f"{path}: invalid project schema"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<document>"
        message = error["msg"]
        if error["type"] == "string_type" and type(error.get("input")) is bool:
            message = f"{message}; {_BOOLEAN_WORD_HINT}"
        lines.append(f"  {location}: {message}")
    return "\n".join(lines)


def resolve_declaration(
    declaration: ProjectSchema | None, *, origin: str = SCHEMA_FILENAME
) -> EffectiveSchema:
    """Resolve a declaration (or its absence) against the built-in schema.

    Pure: `origin` only labels error messages. `None` resolves to the
    built-in labels, relationship types and constraints unchanged.
    """
    if declaration is None:
        return EffectiveSchema(
            extends="default",
            node_labels=NODE_LABELS,
            relationship_types=RELATIONSHIP_TYPES,
        )

    inherits = declaration.extends == "default"
    node_labels = (NODE_LABELS if inherits else ()) + tuple(
        node_type.label for node_type in declaration.node_types
    )

    relationship_types = list(RELATIONSHIP_TYPES) if inherits else []
    for relationship in declaration.relationships:
        if relationship.type not in relationship_types:
            relationship_types.append(relationship.type)

    known_labels = set(node_labels)
    for relationship in declaration.relationships:
        endpoints = [("from", label) for label in relationship.from_labels] + [("to", relationship.to)]
        for role, label in endpoints:
            if label not in known_labels:
                raise ProjectSchemaError(
                    f"{origin}: relationship {relationship.type!r} {role} endpoint "
                    f"{label!r} is not a node label in the effective schema; "
                    f"declare it under node_types or use extends: default"
                )

    # Defence in depth behind the case-insensitive label checks: the
    # generated name is what Neo4j actually keys on, and a duplicate name
    # would make `CREATE CONSTRAINT ... IF NOT EXISTS` silently no-op,
    # shipping a node type with no uniqueness constraint at all.
    used_names = _builtin_constraint_names()
    for node_type in declaration.node_types:
        name = _user_constraint_name(node_type.label)
        if name in used_names:
            raise ProjectSchemaError(
                f"{origin}: node type {node_type.label!r} generates the constraint "
                f"name {name!r}, which is already in use; constraint names are "
                f"lower-cased, so labels must not differ only by case"
            )
        used_names.add(name)
        if node_type.source is not None:
            index_name = _filesystem_index_name(node_type.label)
            if index_name in used_names:
                raise ProjectSchemaError(
                    f"{origin}: node type {node_type.label!r} generates the index "
                    f"name {index_name!r}, which is already in use; index and "
                    f"constraint names share one name space"
                )
            used_names.add(index_name)

    return EffectiveSchema(
        extends=declaration.extends,
        node_labels=node_labels,
        relationship_types=tuple(relationship_types),
        node_types=declaration.node_types,
        relationships=declaration.relationships,
    )


def resolve_effective_schema(repo_root: Path) -> EffectiveSchema:
    """Load a repository's optional schema file and resolve it.

    A repository with no file resolves to the built-in schema exactly.
    """
    declaration = load_project_schema(repo_root)
    return resolve_declaration(declaration, origin=str(project_schema_path(repo_root)))


def project_schema_json_schema() -> dict[str, Any]:
    """Emit a JSON Schema for `devgraph.schema.yaml`, for editors and docs.

    Descriptive only. Loader validity is strictly stronger than
    JSON-Schema validity: the cross-field invariants — built-in label
    shadowing, key components matching declared metadata, conflicting
    duplicate metadata, provider/relationship-type agreement, endpoint
    resolution, generated constraint-name uniqueness — live in this
    module's validators and in `resolve_declaration`, and cannot be
    expressed in the emitted document. Never treat a JSON-Schema-valid
    file as loader-valid; call `load_project_schema`.
    """
    return ProjectSchema.model_json_schema()


_STARTER_TEMPLATE = """\
# DevGraph project schema ({filename})
#
# Declares extra node types and relationships for this repository on top of
# DevGraph's built-in schema. Check it with `devgraph config validate`, see
# the effective schema with `devgraph config show`, and apply it with
# `devgraph rescan <repo_id>`. A declared node type gets a uniqueness
# constraint on its key. Node types with a `source` are indexed:
# `provider: filesystem` makes one node per file or folder, and
# `provider: docs` makes one node per matching Markdown file, filled from its
# Markdown front matter (the `---` block at the top of the file). Nothing in
# this file is ever run as code.
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
#     key: [path]
#     metadata:
#       - {{name: path}}
#       - {{name: owner, required: true}}
#       - {{name: severity, type: integer}}
#       - {{name: on_call}}
#     color: "#1f77b4"   # optional #rrggbb display colour (also valid on relationships)
#     source:
#       provider: docs
#       paths: ["runbooks/**/*.md"]          # 1-20 repo-relative globs; **/*.md for every folder
#       where:                               # optional, at most 20; all must hold
#         - {{field: type, is: runbook}}       # also starts_with, contains, like (* wildcard)
#         - {{field: title, starts_with: "RB-"}}
#       fields: {{on_call: on-call-team}}      # optional; metadata name -> front-matter key
#
# relationships:
#   - type: RUNBOOK_FOR
#     provider: docs
#     from: Runbook
#     to: Service
#     field: service                         # front-matter key naming the target; links every
#                                            # Service of that name (one per compose file)
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
