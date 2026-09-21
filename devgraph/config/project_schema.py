"""Optional per-project graph schema declarations.

A repository may place a `devgraph.schema.yaml` file at its root to declare
extra node types and relationships on top of — or instead of — DevGraph's
built-in labels. This module is that file's format, loader and fail-closed
validator, and nothing more: resolving a project schema changes no indexing
behaviour, opens no file the declaration names, and runs no code. A custom
provider declaration is validated as inert data only.

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

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from devgraph.graph.schema import (
    NODE_LABELS,
    RELATIONSHIP_TYPES,
    RESERVED_NODE_PROPERTIES,
)
from devgraph.graph.schema import constraint_statements as builtin_constraint_statements

SCHEMA_FILENAME = "devgraph.schema.yaml"
SCHEMA_VERSION = 1

#: How a declaration relates to the built-in schema. "default" inherits the
#: built-in labels, relationship types and constraints; "none" inherits
#: nothing and must therefore declare at least one node type.
EXTENDS_MODES: tuple[str, ...] = ("default", "none")

#: Who produces a declared relationship. "builtin" reuses one of DevGraph's
#: own relationship types; "custom" names an out-of-tree provider that this
#: unit records as data and never loads.
PROVIDER_KINDS: tuple[str, ...] = ("builtin", "custom")

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

_CONSTRAINT_NAME_PATTERN = re.compile(r"^(?:CREATE|DROP) CONSTRAINT (\w+) ")

# Literal aliases are derived from the constants above rather than
# restating their values, so the emitted JSON Schema and the constants
# cannot drift apart.
SchemaVersion = Literal[SCHEMA_VERSION]
ExtendsMode = Literal[EXTENDS_MODES]
ProviderKind = Literal[PROVIDER_KINDS]
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


class NodeTypeDecl(BaseModel):
    """A user-declared node label with a stable key and metadata fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    key: tuple[str, ...]
    metadata: tuple[MetadataField, ...] = ()
    description: str | None = None

    @field_validator("label")
    @classmethod
    def _check_label(cls, value: str) -> str:
        return _require_identifier(value, LABEL_PATTERN, "node type label")

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
        return self


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
    from_: str = Field(alias="from")
    to: str
    provider: ProviderKind = "builtin"
    custom: CustomProvider | None = None

    @field_validator("type")
    @classmethod
    def _check_type(cls, value: str) -> str:
        return _require_identifier(value, RELATIONSHIP_TYPE_PATTERN, "relationship type")

    @field_validator("from_", "to")
    @classmethod
    def _check_endpoint(cls, value: str) -> str:
        return _require_identifier(value, LABEL_PATTERN, "relationship endpoint label")

    @model_validator(mode="after")
    def _check_provider(self) -> RelationshipDecl:
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
        else:
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
        statements.extend(_user_constraint_statement(n) for n in self.node_types)
        return statements


def _user_constraint_name(label: str) -> str:
    return f"{label.lower()}{USER_CONSTRAINT_SUFFIX}"


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


def load_project_schema(repo_root: Path) -> ProjectSchema | None:
    """Load and validate `devgraph.schema.yaml`, if the repository has one.

    Returns `None` if and only if the file is absent. An empty, malformed,
    non-mapping or invalid file raises `ProjectSchemaError`; no partial
    schema is ever returned.
    """
    path = project_schema_path(repo_root)
    try:
        if not path.exists():
            return None
        if not path.is_file():
            raise ProjectSchemaError(f"{path}: project schema is not a regular file")
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectSchemaError(f"{path}: cannot be read: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ProjectSchemaError(f"{path}: is not valid UTF-8: {exc}") from exc

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
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


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    lines = [f"{path}: invalid project schema"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<document>"
        lines.append(f"  {location}: {error['msg']}")
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
        for role, label in (("from", relationship.from_), ("to", relationship.to)):
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
