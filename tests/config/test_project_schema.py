"""Tests for the optional per-project graph schema loader.

Pure unit tests over `tmp_path` repositories -- no Neo4j, no indexing, and
nothing here provisions a constraint; `constraint_statements()` is compared
as text.

Note on packaging: this is deliberately the only `tests/` subdirectory
without an `__init__.py`. The issue's approved paths cover exactly four
files and an initializer is not one of them, and collection works without
one (pytest's default prepend import mode gives this directory as the
basedir and `test_project_schema` is a repo-unique module name). Do not
"fix" the inconsistency in isolation -- add the initializer only as part of
a change that is scoped to touch it.
"""

import re
import sys
import textwrap
from pathlib import Path

import pytest

from devgraph.config import project_schema
from devgraph.config.project_schema import (
    EXTENDS_MODES,
    LABEL_PATTERN,
    MAX_IDENTIFIER_LENGTH,
    METADATA_TYPES,
    PROPERTY_NAME_PATTERN,
    PROVIDER_KINDS,
    RELATIONSHIP_TYPE_PATTERN,
    SCHEMA_FILENAME,
    SCHEMA_VERSION,
    ProjectSchemaError,
    load_project_schema,
    project_schema_json_schema,
    project_schema_path,
    resolve_declaration,
    resolve_effective_schema,
)
from devgraph.graph.schema import (
    NODE_LABELS,
    RELATIONSHIP_TYPES,
    RESERVED_NODE_PROPERTIES,
    constraint_statements,
)


WIDGET = """
    version: 1
    node_types:
      - label: Widget
        key: [slug]
        metadata:
          - name: slug
            type: string
            required: true
"""


def write_schema(repo_root: Path, content: str) -> Path:
    """Write a schema file from a dedent-able block and return the repo root."""
    (repo_root / SCHEMA_FILENAME).write_text(textwrap.dedent(content), encoding="utf-8")
    return repo_root


# --- Acceptance 1: a missing file preserves today's behaviour exactly ----


def test_missing_file_loads_as_none(tmp_path):
    assert load_project_schema(tmp_path) is None


def test_missing_file_preserves_labels_and_relationship_types(tmp_path):
    effective = resolve_effective_schema(tmp_path)

    assert effective.node_labels == NODE_LABELS
    assert effective.relationship_types == RELATIONSHIP_TYPES
    assert effective.extends == "default"


def test_missing_file_preserves_constraint_statements_exactly(tmp_path):
    effective = resolve_effective_schema(tmp_path)

    # Full list equality: order matters, and the Class/Function/Service DROP
    # statements must still precede their replacements.
    assert effective.constraint_statements() == constraint_statements()
    assert any(s.startswith("DROP CONSTRAINT") for s in effective.constraint_statements())


def test_missing_file_declares_nothing(tmp_path):
    effective = resolve_effective_schema(tmp_path)

    assert effective.node_types == ()
    assert effective.relationships == ()


def test_resolving_no_declaration_matches_the_missing_file_path(tmp_path):
    assert resolve_declaration(None) == resolve_effective_schema(tmp_path)


def test_project_schema_path_is_the_repo_root_file(tmp_path):
    assert project_schema_path(tmp_path) == tmp_path / SCHEMA_FILENAME
    assert SCHEMA_FILENAME == "devgraph.schema.yaml"


# --- Acceptance 2: extends resolution ------------------------------------


def test_extends_default_inherits_builtins_and_appends_user_labels(tmp_path):
    effective = resolve_effective_schema(write_schema(tmp_path, WIDGET))

    assert effective.node_labels == NODE_LABELS + ("Widget",)
    assert effective.relationship_types == RELATIONSHIP_TYPES
    assert effective.constraint_statements()[: len(constraint_statements())] == (
        constraint_statements()
    )


def test_extends_defaults_to_default_when_omitted(tmp_path):
    declaration = load_project_schema(write_schema(tmp_path, WIDGET))

    assert declaration.extends == "default"


def test_extends_default_may_be_stated_explicitly(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        extends: default
        node_types:
          - label: Widget
            key: [slug]
            metadata:
              - name: slug
        """,
    )

    assert resolve_effective_schema(repo).node_labels == NODE_LABELS + ("Widget",)


def test_extends_none_inherits_nothing(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        extends: none
        node_types:
          - label: Widget
            key: [slug]
            metadata:
              - name: slug
        """,
    )

    effective = resolve_effective_schema(repo)

    assert effective.node_labels == ("Widget",)
    assert effective.relationship_types == ()
    assert effective.constraint_statements() == [
        "CREATE CONSTRAINT widget_repo_key IF NOT EXISTS "
        "FOR (n:Widget) REQUIRE (n.repo_id, n.slug) IS UNIQUE"
    ]


def test_extends_none_without_node_types_fails(tmp_path):
    repo = write_schema(tmp_path, "version: 1\nextends: none\n")

    with pytest.raises(ProjectSchemaError, match="at least one"):
        load_project_schema(repo)


@pytest.mark.parametrize("value", ["", "defaults", "builtin", "None", "0"])
def test_unknown_extends_value_fails(tmp_path, value):
    repo = write_schema(tmp_path, f"version: 1\nextends: {value!r}\n")

    with pytest.raises(ProjectSchemaError, match="extends"):
        load_project_schema(repo)


@pytest.mark.parametrize("value", ["0", "2", "1.5", "null"])
def test_unsupported_version_fails(tmp_path, value):
    repo = write_schema(tmp_path, f"version: {value}\n")

    with pytest.raises(ProjectSchemaError, match="version"):
        load_project_schema(repo)


def test_missing_version_fails(tmp_path):
    repo = write_schema(tmp_path, "extends: default\n")

    with pytest.raises(ProjectSchemaError, match="version"):
        load_project_schema(repo)


# --- Acceptance 3: user keys and fixed metadata names --------------------


def test_node_type_requires_a_non_empty_key(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        node_types:
          - label: Widget
            key: []
            metadata:
              - name: slug
        """,
    )

    with pytest.raises(ProjectSchemaError, match="non-empty key"):
        load_project_schema(repo)


def test_key_component_must_be_declared_metadata(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        node_types:
          - label: Widget
            key: [slug]
            metadata:
              - name: title
        """,
    )

    with pytest.raises(ProjectSchemaError, match="not a declared metadata field"):
        load_project_schema(repo)


def test_duplicate_key_components_fail(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        node_types:
          - label: Widget
            key: [slug, slug]
            metadata:
              - name: slug
        """,
    )

    with pytest.raises(ProjectSchemaError, match="more than once"):
        load_project_schema(repo)


@pytest.mark.parametrize("reserved", sorted(RESERVED_NODE_PROPERTIES))
def test_reserved_metadata_name_is_rejected(tmp_path, reserved):
    repo = write_schema(
        tmp_path,
        f"""
        version: 1
        node_types:
          - label: Widget
            key: [slug]
            metadata:
              - name: slug
              - name: {reserved}
        """,
    )

    with pytest.raises(ProjectSchemaError, match="reserved by DevGraph"):
        load_project_schema(repo)


@pytest.mark.parametrize("reserved", sorted(RESERVED_NODE_PROPERTIES))
def test_reserved_key_component_is_rejected(tmp_path, reserved):
    repo = write_schema(
        tmp_path,
        f"""
        version: 1
        node_types:
          - label: Widget
            key: [{reserved}]
            metadata:
              - name: slug
        """,
    )

    with pytest.raises(ProjectSchemaError, match="reserved by DevGraph"):
        load_project_schema(repo)


def test_user_label_cannot_shadow_a_builtin_label(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        node_types:
          - label: Module
            key: [slug]
            metadata:
              - name: slug
        """,
    )

    with pytest.raises(ProjectSchemaError, match="built-in label"):
        load_project_schema(repo)


def test_user_label_cannot_shadow_a_builtin_label_by_case(tmp_path):
    # Constraint names are lower-cased, so MODULE would generate the same
    # name as the built-in Module constraint and silently no-op.
    repo = write_schema(
        tmp_path,
        """
        version: 1
        node_types:
          - label: MODULE
            key: [slug]
            metadata:
              - name: slug
        """,
    )

    with pytest.raises(ProjectSchemaError, match="built-in label"):
        load_project_schema(repo)


def test_user_labels_differing_only_by_case_are_rejected(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        node_types:
          - label: Widget
            key: [slug]
            metadata:
              - name: slug
          - label: WIDGET
            key: [slug]
            metadata:
              - name: slug
        """,
    )

    with pytest.raises(ProjectSchemaError, match="differ by more than case"):
        load_project_schema(repo)


def test_generated_constraint_names_never_collide_with_builtin_ones(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        node_types:
          - label: Widget
            key: [slug]
            metadata:
              - name: slug
          - label: Gadget
            key: [slug, revision]
            metadata:
              - name: slug
              - name: revision
                type: integer
        """,
    )

    statements = resolve_effective_schema(repo).constraint_statements()
    names = [re.match(r"^(?:CREATE|DROP) CONSTRAINT (\w+) ", s).group(1) for s in statements]
    creates = [n for n, s in zip(names, statements) if s.startswith("CREATE")]

    assert len(creates) == len(set(creates))
    assert "widget_repo_key" in creates
    assert "gadget_repo_key" in creates


def test_multi_component_key_prepends_repo_id_in_order(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        extends: none
        node_types:
          - label: Gadget
            key: [slug, revision]
            metadata:
              - name: slug
              - name: revision
                type: integer
        """,
    )

    assert resolve_effective_schema(repo).constraint_statements() == [
        "CREATE CONSTRAINT gadget_repo_key IF NOT EXISTS "
        "FOR (n:Gadget) REQUIRE (n.repo_id, n.slug, n.revision) IS UNIQUE"
    ]


# --- Acceptance 4: duplicate metadata and unknown providers --------------


def test_conflicting_duplicate_metadata_fails_with_context(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        node_types:
          - label: Widget
            key: [slug]
            metadata:
              - name: slug
                type: string
              - name: slug
                type: integer
        """,
    )

    with pytest.raises(ProjectSchemaError) as excinfo:
        load_project_schema(repo)

    message = str(excinfo.value)
    assert "twice with different definitions" in message
    assert "slug" in message
    assert "Widget" in message
    assert str(tmp_path) in message


def test_exact_duplicate_metadata_collapses(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        node_types:
          - label: Widget
            key: [slug]
            metadata:
              - name: slug
                type: string
              - name: slug
                type: string
        """,
    )

    node_type = load_project_schema(repo).node_types[0]

    assert list(node_type.metadata_by_name) == ["slug"]


@pytest.mark.parametrize("kind", ["plugin", "script", "BUILTIN", ""])
def test_unknown_provider_kind_fails(tmp_path, kind):
    repo = write_schema(
        tmp_path,
        f"""
        version: 1
        relationships:
          - type: CONTAINS
            from: Module
            to: Function
            provider: {kind!r}
        """,
    )

    with pytest.raises(ProjectSchemaError, match="provider"):
        load_project_schema(repo)


def test_builtin_provider_must_name_a_known_relationship_type(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        relationships:
          - type: TOUCHES
            from: Module
            to: Function
            provider: builtin
        """,
    )

    with pytest.raises(ProjectSchemaError) as excinfo:
        load_project_schema(repo)

    message = str(excinfo.value)
    assert "not a built-in relationship type" in message
    # Useful context: the known types are listed.
    assert "CONTAINS" in message


def test_builtin_provider_must_not_declare_a_custom_block(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        relationships:
          - type: CONTAINS
            from: Module
            to: Function
            provider: builtin
            custom:
              name: widget_linker
        """,
    )

    with pytest.raises(ProjectSchemaError, match="must not declare a custom block"):
        load_project_schema(repo)


def test_custom_provider_requires_a_custom_block(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        relationships:
          - type: TRACKS
            from: Module
            to: Function
            provider: custom
        """,
    )

    with pytest.raises(ProjectSchemaError, match="requires a custom block"):
        load_project_schema(repo)


def test_custom_provider_cannot_redeclare_a_builtin_relationship_type(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        relationships:
          - type: CONTAINS
            from: Module
            to: Function
            provider: custom
            custom:
              name: widget_linker
        """,
    )

    with pytest.raises(ProjectSchemaError, match="is built-in and cannot be redeclared"):
        load_project_schema(repo)


def test_relationship_endpoints_must_resolve_to_effective_labels(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        node_types:
          - label: Widget
            key: [slug]
            metadata:
              - name: slug
        relationships:
          - type: CONTAINS
            from: Widget
            to: Gadget
            provider: builtin
        """,
    )

    with pytest.raises(ProjectSchemaError) as excinfo:
        resolve_effective_schema(repo)

    message = str(excinfo.value)
    assert "is not a node label in the effective schema" in message
    assert "Gadget" in message
    assert str(tmp_path) in message


def test_builtin_endpoints_are_unavailable_under_extends_none(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        extends: none
        node_types:
          - label: Widget
            key: [slug]
            metadata:
              - name: slug
        relationships:
          - type: CONTAINS
            from: Widget
            to: Module
            provider: builtin
        """,
    )

    with pytest.raises(ProjectSchemaError, match="not a node label in the effective schema"):
        resolve_effective_schema(repo)


def test_relationship_types_accumulate_declared_types(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        node_types:
          - label: Widget
            key: [slug]
            metadata:
              - name: slug
        relationships:
          - type: CONTAINS
            from: Module
            to: Widget
            provider: builtin
          - type: TRACKS
            from: Widget
            to: Module
            provider: custom
            custom:
              name: widget_tracker
        """,
    )

    effective = resolve_effective_schema(repo)

    assert effective.relationship_types == RELATIONSHIP_TYPES + ("TRACKS",)
    assert [r.type for r in effective.relationships] == ["CONTAINS", "TRACKS"]


# --- Acceptance 5: malformed input and unknown keys fail closed ----------


def test_malformed_yaml_fails(tmp_path):
    repo = write_schema(tmp_path, "version: 1\nnode_types: [unclosed\n")

    with pytest.raises(ProjectSchemaError, match="malformed YAML"):
        load_project_schema(repo)


def test_empty_file_is_not_treated_as_absent(tmp_path):
    repo = write_schema(tmp_path, "")

    with pytest.raises(ProjectSchemaError, match="the file is empty"):
        load_project_schema(repo)


def test_whitespace_only_file_is_not_treated_as_absent(tmp_path):
    repo = write_schema(tmp_path, "   \n\n   \n")

    with pytest.raises(ProjectSchemaError, match="the file is empty"):
        load_project_schema(repo)


def test_comment_only_file_is_not_treated_as_absent(tmp_path):
    repo = write_schema(tmp_path, "# nothing to see here\n")

    with pytest.raises(ProjectSchemaError, match="the file is empty"):
        load_project_schema(repo)


def test_scalar_document_fails(tmp_path):
    repo = write_schema(tmp_path, "just a string\n")

    with pytest.raises(ProjectSchemaError, match="expected a YAML mapping"):
        load_project_schema(repo)


def test_sequence_document_fails(tmp_path):
    repo = write_schema(tmp_path, "- version: 1\n")

    with pytest.raises(ProjectSchemaError, match="expected a YAML mapping"):
        load_project_schema(repo)


def test_unknown_top_level_key_fails(tmp_path):
    repo = write_schema(tmp_path, "version: 1\nnode_kinds: []\n")

    with pytest.raises(ProjectSchemaError, match="Extra inputs are not permitted"):
        load_project_schema(repo)


def test_unknown_nested_key_fails(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        node_types:
          - label: Widget
            key: [slug]
            indexed: true
            metadata:
              - name: slug
        """,
    )

    with pytest.raises(ProjectSchemaError, match="Extra inputs are not permitted"):
        load_project_schema(repo)


def test_unknown_metadata_key_fails(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        node_types:
          - label: Widget
            key: [slug]
            metadata:
              - name: slug
                default: abc
        """,
    )

    with pytest.raises(ProjectSchemaError, match="Extra inputs are not permitted"):
        load_project_schema(repo)


def test_unknown_metadata_type_fails(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        node_types:
          - label: Widget
            key: [slug]
            metadata:
              - name: slug
                type: datetime
        """,
    )

    with pytest.raises(ProjectSchemaError, match="type"):
        load_project_schema(repo)


@pytest.mark.parametrize(
    "label",
    [
        "Widget) REQUIRE n.x IS UNIQUE; DROP DATABASE neo4j //",
        "Widget`",
        "Widget Gadget",
        "1Widget",
        "_Widget",
        "A" * (MAX_IDENTIFIER_LENGTH + 1),
    ],
)
def test_unsafe_labels_are_rejected(tmp_path, label):
    repo = write_schema(
        tmp_path,
        f"""
        version: 1
        node_types:
          - label: {label!r}
            key: [slug]
            metadata:
              - name: slug
        """,
    )

    with pytest.raises(ProjectSchemaError, match="not a valid identifier"):
        load_project_schema(repo)


@pytest.mark.parametrize(
    "name",
    ["slug) IS UNIQUE //", "Slug", "1slug", "slug-id", "s" * (MAX_IDENTIFIER_LENGTH + 1)],
)
def test_unsafe_property_names_are_rejected(tmp_path, name):
    repo = write_schema(
        tmp_path,
        f"""
        version: 1
        node_types:
          - label: Widget
            key: [{name!r}]
            metadata:
              - name: {name!r}
        """,
    )

    with pytest.raises(ProjectSchemaError, match="not a valid identifier"):
        load_project_schema(repo)


@pytest.mark.parametrize("rel_type", ["Tracks", "TRACKS THINGS", "1TRACKS", "TRACKS]->()//"])
def test_unsafe_relationship_types_are_rejected(tmp_path, rel_type):
    repo = write_schema(
        tmp_path,
        f"""
        version: 1
        relationships:
          - type: {rel_type!r}
            from: Module
            to: Function
            provider: custom
            custom:
              name: widget_tracker
        """,
    )

    with pytest.raises(ProjectSchemaError, match="not a valid identifier"):
        load_project_schema(repo)


def test_generated_statements_are_injection_safe(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        node_types:
          - label: Widget
            key: [slug, revision]
            metadata:
              - name: slug
              - name: revision
                type: integer
        """,
    )
    safe = re.compile(
        r"^CREATE CONSTRAINT [a-z][a-z0-9_]*_repo_key IF NOT EXISTS "
        r"FOR \(n:[A-Za-z][A-Za-z0-9_]*\) "
        r"REQUIRE \(n\.repo_id(?:, n\.[a-z][a-z0-9_]*)+\) IS UNIQUE$"
    )

    effective = resolve_effective_schema(repo)
    generated = effective.constraint_statements()[len(constraint_statements()) :]

    assert generated
    assert all(safe.fullmatch(statement) for statement in generated)


def test_invalid_document_returns_no_partial_schema(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        node_types:
          - label: Widget
            key: [slug]
            metadata:
              - name: slug
          - label: Gadget
            key: []
        """,
    )

    with pytest.raises(ProjectSchemaError):
        load_project_schema(repo)
    # The good half of the document is not observable anywhere: the only
    # accessor raises too.
    with pytest.raises(ProjectSchemaError):
        resolve_effective_schema(repo)


# --- Acceptance 6: a custom provider is data, and nothing runs -----------


def test_custom_provider_validates_as_data(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        node_types:
          - label: Widget
            key: [slug]
            metadata:
              - name: slug
        relationships:
          - type: TRACKS
            from: Widget
            to: Widget
            provider: custom
            custom:
              name: widget_tracker
              params:
                glob: '*.widget'
                depth: 3
                strict: true
                ratio: 0.5
                fallback: null
        """,
    )

    relationship = resolve_effective_schema(repo).relationships[0]

    assert relationship.provider == "custom"
    assert relationship.custom.name == "widget_tracker"
    assert relationship.custom.params == {
        "glob": "*.widget",
        "depth": 3,
        "strict": True,
        "ratio": 0.5,
        "fallback": None,
    }


def test_custom_provider_params_must_be_scalars(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        relationships:
          - type: TRACKS
            from: Module
            to: Function
            provider: custom
            custom:
              name: widget_tracker
              params:
                nested:
                  key: value
        """,
    )

    with pytest.raises(ProjectSchemaError, match="params"):
        load_project_schema(repo)


def test_custom_provider_param_names_are_identifiers(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        relationships:
          - type: TRACKS
            from: Module
            to: Function
            provider: custom
            custom:
              name: widget_tracker
              params:
                'bad name': 1
        """,
    )

    with pytest.raises(ProjectSchemaError, match="not a valid identifier"):
        load_project_schema(repo)


def test_provider_name_is_never_imported(tmp_path, monkeypatch):
    """A provider named after a real importable module stays untouched."""
    sentinel = tmp_path / "sentinel.txt"
    (tmp_path / "sentinel_provider_20.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    repo = write_schema(
        tmp_path,
        """
        version: 1
        node_types:
          - label: Widget
            key: [slug]
            metadata:
              - name: slug
        relationships:
          - type: TRACKS
            from: Widget
            to: Widget
            provider: custom
            custom:
              name: sentinel_provider_20
        """,
    )

    resolve_effective_schema(repo)

    assert "sentinel_provider_20" not in sys.modules
    assert not sentinel.exists()


def test_loading_starts_no_process(tmp_path, monkeypatch):
    import os
    import subprocess

    def fail(*args, **kwargs):
        raise AssertionError("loading a project schema must not start a process")

    monkeypatch.setattr(subprocess, "Popen", fail)
    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(os, "system", fail)
    repo = write_schema(
        tmp_path,
        """
        version: 1
        node_types:
          - label: Widget
            key: [slug]
            metadata:
              - name: slug
        relationships:
          - type: TRACKS
            from: Widget
            to: Widget
            provider: custom
            custom:
              name: widget_tracker
              params:
                command: 'rm -rf /'
        """,
    )

    effective = resolve_effective_schema(repo)

    assert effective.relationships[0].custom.params["command"] == "rm -rf /"


def test_python_object_tags_are_rejected(tmp_path):
    repo = write_schema(
        tmp_path,
        """
        version: 1
        extends: !!python/object/apply:os.system ["echo owned"]
        """,
    )

    with pytest.raises(ProjectSchemaError, match="malformed YAML"):
        load_project_schema(repo)


def test_loader_module_holds_no_execution_primitives():
    """Weak but cheap guard, scoped to the loader module only."""
    source = Path(project_schema.__file__).read_text(encoding="utf-8")
    forbidden = (
        "exec(",
        "eval(",
        "__import__",
        "importlib",
        "subprocess",
        "Popen",
        "os.system",
        "runpy",
        "pickle",
    )

    assert [token for token in forbidden if token in source] == []


def test_loader_module_copies_no_builtin_names():
    """Built-in labels and relationship types must be imported, not copied."""
    source = Path(project_schema.__file__).read_text(encoding="utf-8")
    copied = [
        name
        for name in NODE_LABELS + RELATIONSHIP_TYPES
        if f'"{name}"' in source or f"'{name}'" in source
    ]

    assert copied == []


# --- Supporting invariants ----------------------------------------------


def test_json_schema_is_emitted_and_documented_as_descriptive():
    emitted = project_schema_json_schema()

    assert emitted["properties"]["version"]["const"] == SCHEMA_VERSION
    assert set(emitted["properties"]) == {
        "version",
        "extends",
        "node_types",
        "relationships",
    }
    assert "strictly stronger" in project_schema_json_schema.__doc__


def test_json_schema_literals_track_the_constants():
    emitted = project_schema_json_schema()
    defs = emitted["$defs"]

    assert emitted["properties"]["extends"]["enum"] == list(EXTENDS_MODES)
    assert defs["RelationshipDecl"]["properties"]["provider"]["enum"] == list(PROVIDER_KINDS)
    assert defs["MetadataField"]["properties"]["type"]["enum"] == list(METADATA_TYPES)
    assert "from" in defs["RelationshipDecl"]["properties"]


def test_identifier_patterns_are_anchored_and_bounded():
    for pattern in (LABEL_PATTERN, RELATIONSHIP_TYPE_PATTERN, PROPERTY_NAME_PATTERN):
        assert pattern.fullmatch("A" * (MAX_IDENTIFIER_LENGTH + 1)) is None
        assert pattern.fullmatch("a" * (MAX_IDENTIFIER_LENGTH + 1)) is None
        assert pattern.fullmatch("") is None
        assert pattern.fullmatch("ok\nOK") is None


def test_builtin_constraint_names_are_all_recoverable():
    """Guards the name-extraction pattern the collision check depends on."""
    names = project_schema._builtin_constraint_names()

    assert len(names) == len(constraint_statements())
