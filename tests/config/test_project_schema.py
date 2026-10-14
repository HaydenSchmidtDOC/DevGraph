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
    ABSENT_SCHEMA_HASH,
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
    schema_file_hash,
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


def test_schema_file_symlinked_outside_the_repo_is_refused(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_text("version: 1\nmarker_outside_content: true\n", encoding="utf-8")
    (repo / SCHEMA_FILENAME).symlink_to(outside)

    with pytest.raises(ProjectSchemaError, match="must be inside the repository") as excinfo:
        load_project_schema(repo)
    assert "marker_outside_content" not in str(excinfo.value)
    assert str(outside) not in str(excinfo.value)


def test_schema_file_symlinked_within_the_repo_loads(tmp_path):
    (tmp_path / "config").mkdir()
    write_schema(tmp_path / "config", WIDGET)
    (tmp_path / SCHEMA_FILENAME).symlink_to(tmp_path / "config" / SCHEMA_FILENAME)

    assert load_project_schema(tmp_path) is not None


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


def test_user_label_cannot_be_the_datastore_cache_label(tmp_path):
    # The datastore extractor writes Cache nodes (not in NODE_LABELS), with
    # their own built-in indexes.
    repo = write_schema(
        tmp_path,
        """
        version: 1
        node_types:
          - label: cache
            key: [slug]
            metadata:
              - name: slug
        """,
    )

    with pytest.raises(ProjectSchemaError, match="built-in label 'Cache'"):
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


# --- Filesystem provider (worktree example) --------------------------------

WORKTREE = """
    version: 1
    node_types:
      - label: File
        key: [path]
        metadata: [{name: path}]
        source: {provider: filesystem, kind: file}
      - label: Folder
        key: [path]
        metadata: [{name: path}]
        source: {provider: filesystem, kind: folder}
    relationships:
      - type: IS_CHILD_OF
        provider: filesystem
        from: [File, Folder]
        to: Folder
"""


def test_worktree_example_loads_and_resolves(tmp_path):
    effective = resolve_effective_schema(write_schema(tmp_path, WORKTREE))
    assert {"File", "Folder"} <= set(effective.node_labels)
    assert "IS_CHILD_OF" in effective.relationship_types
    (relationship,) = effective.relationships
    assert relationship.provider == "filesystem"
    assert relationship.from_labels == ("File", "Folder")
    kinds = {n.label: (n.source.provider, n.source.kind) for n in effective.node_types}
    assert kinds == {"File": ("filesystem", "file"), "Folder": ("filesystem", "folder")}


def test_a_single_from_label_is_still_accepted(tmp_path):
    effective = resolve_effective_schema(write_schema(tmp_path, WORKTREE.replace("from: [File, Folder]", "from: File")))
    assert effective.relationships[0].from_labels == ("File",)


def test_filesystem_node_types_must_be_keyed_on_path(tmp_path):
    text = WORKTREE.replace(
        "      - label: File\n        key: [path]\n        metadata: [{name: path}]",
        "      - label: File\n        key: [slug]\n        metadata: [{name: slug}]",
    )
    with pytest.raises(ProjectSchemaError, match=r"key must be exactly \[path\]"):
        load_project_schema(write_schema(tmp_path, text))


def test_filesystem_path_must_be_a_string(tmp_path):
    text = WORKTREE.replace(
        "      - label: File\n        key: [path]\n        metadata: [{name: path}]",
        "      - label: File\n        key: [path]\n        metadata: [{name: path, type: integer}]",
    )
    with pytest.raises(ProjectSchemaError, match="must be a string"):
        load_project_schema(write_schema(tmp_path, text))


def test_at_most_one_node_type_per_filesystem_kind(tmp_path):
    text = WORKTREE.replace(
        "    relationships:",
        "      - label: Doc\n        key: [path]\n        metadata: [{name: path}]\n"
        "        source: {provider: filesystem, kind: file}\n    relationships:",
    )
    with pytest.raises(ProjectSchemaError, match="both filesystem file types"):
        load_project_schema(write_schema(tmp_path, text))


def test_filesystem_relationship_must_point_to_the_folder_type(tmp_path):
    with pytest.raises(ProjectSchemaError, match="must point to the filesystem folder node type"):
        load_project_schema(write_schema(tmp_path, WORKTREE.replace("to: Folder", "to: File")))


def test_filesystem_relationship_needs_a_folder_type(tmp_path):
    text = """
        version: 1
        node_types:
          - label: File
            key: [path]
            metadata: [{name: path}]
            source: {provider: filesystem, kind: file}
        relationships:
          - type: IS_CHILD_OF
            provider: filesystem
            from: File
            to: File
    """
    with pytest.raises(ProjectSchemaError, match="none is declared"):
        load_project_schema(write_schema(tmp_path, text))


def test_filesystem_relationship_children_must_be_filesystem_types(tmp_path):
    text = WORKTREE.replace("from: [File, Folder]", "from: [File, Widget]").replace(
        "    relationships:",
        "      - label: Widget\n        key: [slug]\n        metadata: [{name: slug}]\n    relationships:",
    )
    with pytest.raises(ProjectSchemaError, match="'Widget' is not a filesystem node type"):
        load_project_schema(write_schema(tmp_path, text))


def test_filesystem_relationship_rejects_a_custom_block(tmp_path):
    text = WORKTREE.replace("        to: Folder", "        to: Folder\n        custom: {name: tree}")
    with pytest.raises(ProjectSchemaError, match="filesystem provider, which must not declare a custom block"):
        load_project_schema(write_schema(tmp_path, text))


def test_filesystem_relationship_cannot_reuse_a_builtin_type(tmp_path):
    with pytest.raises(ProjectSchemaError, match="cannot be redeclared by the filesystem provider"):
        load_project_schema(write_schema(tmp_path, WORKTREE.replace("type: IS_CHILD_OF", "type: CONTAINS")))


def test_at_most_one_filesystem_relationship(tmp_path):
    # Same indentation as WORKTREE's own relationship items (6 spaces).
    text = WORKTREE + (
        "      - type: IN_FOLDER\n"
        "        provider: filesystem\n"
        "        from: File\n"
        "        to: Folder\n"
    )
    with pytest.raises(ProjectSchemaError, match="at most one filesystem relationship"):
        load_project_schema(write_schema(tmp_path, text))


@pytest.mark.parametrize("from_value", ["[]", "[File, File]"])
def test_relationship_from_list_must_be_non_empty_and_unique(tmp_path, from_value):
    with pytest.raises(ProjectSchemaError):
        load_project_schema(write_schema(tmp_path, WORKTREE.replace("[File, Folder]", from_value)))


@pytest.mark.parametrize(
    "source",
    ["{provider: git, kind: file}", "{provider: filesystem, kind: symlink}", "{provider: filesystem}"],
)
def test_unknown_node_sources_are_rejected(tmp_path, source):
    text = WORKTREE.replace("{provider: filesystem, kind: file}", source)
    with pytest.raises(ProjectSchemaError):
        load_project_schema(write_schema(tmp_path, text))


def test_extractor_is_a_reserved_property(tmp_path):
    assert "extractor" in RESERVED_NODE_PROPERTIES
    # WIDGET's metadata list items sit at 10 spaces; this adds a second one.
    text = WIDGET + "          - name: extractor\n"
    with pytest.raises(ProjectSchemaError, match="reserved"):
        load_project_schema(write_schema(tmp_path, text))


def test_every_label_in_a_from_list_must_resolve(tmp_path):
    text = """
        version: 1
        node_types:
          - label: Widget
            key: [slug]
            metadata: [{name: slug}]
        relationships:
          - type: LINKS
            provider: custom
            custom: {name: linker}
            from: [Widget, Gadget]
            to: Widget
    """
    with pytest.raises(ProjectSchemaError, match="'Gadget' is not a node label"):
        resolve_effective_schema(write_schema(tmp_path, text))


def test_json_schema_describes_node_sources_and_list_from():
    defs = project_schema_json_schema()["$defs"]
    assert defs["FilesystemSource"]["properties"]["kind"]["enum"] == ["file", "folder"]
    assert "source" in defs["NodeTypeDecl"]["properties"]
    from_schema = defs["RelationshipDecl"]["properties"]["from"]
    assert {variant.get("type") for variant in from_schema["anyOf"]} == {"string", "array"}


def test_filesystem_types_get_a_repo_name_index_beside_their_key_constraint(tmp_path):
    effective = resolve_effective_schema(write_schema(tmp_path, WORKTREE))
    statements = effective.constraint_statements()
    builtin = constraint_statements()

    assert statements[: len(builtin)] == builtin
    assert statements[len(builtin) :] == [
        "CREATE CONSTRAINT file_repo_key IF NOT EXISTS "
        "FOR (n:File) REQUIRE (n.repo_id, n.path) IS UNIQUE",
        "CREATE INDEX file_repo_name IF NOT EXISTS FOR (n:File) ON (n.repo_id, n.name)",
        "CREATE CONSTRAINT folder_repo_key IF NOT EXISTS "
        "FOR (n:Folder) REQUIRE (n.repo_id, n.path) IS UNIQUE",
        "CREATE INDEX folder_repo_name IF NOT EXISTS FOR (n:Folder) ON (n.repo_id, n.name)",
    ]


def test_types_without_a_source_get_no_index(tmp_path):
    effective = resolve_effective_schema(write_schema(tmp_path, WORKTREE))
    plain = resolve_effective_schema(
        write_schema(
            tmp_path,
            """
            version: 1
            node_types:
              - label: Widget
                key: [slug]
                metadata: [{name: slug}]
            """,
        )
    )

    assert not any("CREATE INDEX" in s for s in plain.constraint_statements())
    assert any("CREATE INDEX" in s for s in effective.constraint_statements())


def test_a_generated_index_name_may_not_collide_with_an_existing_name(tmp_path, monkeypatch):
    monkeypatch.setattr(project_schema, "_builtin_constraint_names", lambda: {"file_repo_name"})
    with pytest.raises(ProjectSchemaError, match="file_repo_name"):
        resolve_effective_schema(write_schema(tmp_path, WORKTREE))


def test_schema_file_hash_tracks_content(tmp_path):
    assert schema_file_hash(tmp_path) == ABSENT_SCHEMA_HASH
    write_schema(tmp_path, WIDGET)
    first = schema_file_hash(tmp_path)
    assert first.startswith("sha256:") and len(first) == len("sha256:") + 64
    assert schema_file_hash(tmp_path) == first
    (tmp_path / SCHEMA_FILENAME).write_text("version: 1\n")
    assert schema_file_hash(tmp_path) != first


def test_an_unreadable_schema_never_hashes_like_a_real_one(tmp_path):
    (tmp_path / SCHEMA_FILENAME).mkdir()
    value = schema_file_hash(tmp_path)
    assert value.startswith("unreadable:") and value != ABSENT_SCHEMA_HASH


@pytest.mark.parametrize(
    "text",
    ["version: 1\nnode_types:\n  - label: X\n    description: 2001-13-45\n", "version: 1\nnode_types: " + "[" * 5000 + "\n"],
    ids=["bad date", "deep nesting"],
)
def test_any_yaml_load_failure_is_a_schema_error(tmp_path, text):
    (tmp_path / SCHEMA_FILENAME).write_text(text, encoding="utf-8")
    with pytest.raises(ProjectSchemaError, match="malformed YAML"):
        load_project_schema(tmp_path)


def test_parse_project_schema_matches_loader_errors(tmp_path):
    path = tmp_path / SCHEMA_FILENAME
    text = "version: [unclosed\n"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ProjectSchemaError) as via_loader:
        load_project_schema(tmp_path)
    with pytest.raises(ProjectSchemaError) as via_parse:
        project_schema.parse_project_schema(text, path)
    assert str(via_parse.value) == str(via_loader.value)
    assert "malformed YAML" in str(via_parse.value)
    assert project_schema.parse_project_schema(textwrap.dedent(WIDGET), path).node_types[0].label == "Widget"


# --- Display colour -------------------------------------------------------

COLOURED = """
    version: 1
    node_types:
      - label: Widget
        color: "{node}"
        key: [slug]
        metadata:
          - name: slug
    relationships:
      - type: USES
        color: "{rel}"
        from: Widget
        to: Module
"""


def test_colour_is_accepted_on_node_type_and_relationship(tmp_path):
    repo = write_schema(tmp_path, COLOURED.format(node="#1f77b4", rel="#AbCdEf"))
    declaration = load_project_schema(repo)
    assert declaration.node_types[0].color == "#1f77b4"
    assert declaration.relationships[0].color == "#AbCdEf"


def test_colour_defaults_to_none(tmp_path):
    declaration = load_project_schema(write_schema(tmp_path, WIDGET))
    assert declaration.node_types[0].color is None


@pytest.mark.parametrize("bad", ["blue", "#12345", "#gggggg", "#1234567"])
@pytest.mark.parametrize("where", ["node", "rel"])
def test_bad_colour_is_rejected_naming_field_and_format(tmp_path, bad, where):
    good = "#1f77b4"
    repo = write_schema(
        tmp_path,
        COLOURED.format(node=bad if where == "node" else good, rel=bad if where == "rel" else good),
    )
    with pytest.raises(ProjectSchemaError, match=r"color: .*#rrggbb"):
        load_project_schema(repo)


def test_json_schema_documents_colour_pattern():
    defs = project_schema_json_schema()["$defs"]
    for name in ("NodeTypeDecl", "RelationshipDecl"):
        branches = defs[name]["properties"]["color"]["anyOf"]
        assert any(b.get("pattern") == "^#[0-9a-fA-F]{6}$" for b in branches)


def test_starter_template_mentions_colour():
    assert 'color: "#1f77b4"' in project_schema.starter_schema_text()


# --- Docs provider (Markdown front matter) ---------------------------------

RUNBOOK = """
    version: 1
    node_types:
      - label: Runbook
        key: [path]
        metadata:
          - {name: path}
          - {name: owner, required: true}
          - {name: severity, type: integer}
          - {name: on_call}
        source:
          provider: docs
          paths: ["runbooks/**/*.md"]
          where:
            - {field: type, is: runbook}
            - {field: title, starts_with: "RB-"}
          fields: {on_call: on-call-team}
    relationships:
      - type: RUNBOOK_FOR
        provider: docs
        from: Runbook
        to: Service
        field: service
"""


def runbook_with(old: str, new: str) -> str:
    assert old in RUNBOOK, old
    return RUNBOOK.replace(old, new)


def test_the_runbook_example_validates(tmp_path):
    effective = resolve_effective_schema(write_schema(tmp_path, RUNBOOK))
    (runbook,) = effective.node_types
    assert runbook.source.provider == "docs"
    assert runbook.source.paths == ("runbooks/**/*.md",)
    assert [(c.field, c.operator, c.text) for c in runbook.source.where] == [
        ("type", "is", "runbook"),
        ("title", "starts_with", "RB-"),
    ]
    assert runbook.source.fields == {"on_call": "on-call-team"}
    (relationship,) = effective.relationships
    assert (relationship.provider, relationship.field, relationship.to) == ("docs", "service", "Service")
    assert "RUNBOOK_FOR" in effective.relationship_types


def test_docs_is_a_node_source_and_relationship_provider():
    assert project_schema.NODE_SOURCE_PROVIDERS == ("filesystem", "docs")
    assert "docs" in PROVIDER_KINDS


def test_where_and_fields_are_optional(tmp_path):
    text = """
        version: 1
        node_types:
          - label: Note
            key: [path]
            metadata: [{name: path}]
            source: {provider: docs, paths: ["**/*.md"]}
    """
    (note,) = load_project_schema(write_schema(tmp_path, text)).node_types
    assert note.source.where == () and note.source.fields == {}


@pytest.mark.parametrize(
    "paths, message",
    [
        ("[]", r"paths of 'Runbook' must list 1 to 20 globs"),
        ("[" + ", ".join(f'"d{i}/*.md"' for i in range(21)) + "]", r"paths of 'Runbook' must list 1 to 20 globs"),
        ('["/etc/*.md"]', r"paths\[0\] of 'Runbook' .*repo-relative"),
        ('["docs/../../*.md"]', r"paths\[0\] of 'Runbook' .*'\.\.'"),
        ('["..", "x.md"]', r"paths\[0\] of 'Runbook' .*'\.\.'"),
        ('["docs\\\\*.md"]', r"paths\[0\] of 'Runbook' .*backslash"),
        ('[""]', r"paths\[0\] of 'Runbook' is empty"),
        ('["ok.md", "' + "a" * 201 + '"]', r"paths\[1\] of 'Runbook' is longer than 200 characters"),
        ('["./runbooks/*.md"]', r"paths\[0\] of 'Runbook' .*has a '\.' segment; write it relative to the repository root without '\./' \(e\.g\. runbooks/\*\*/\*\.md\)"),
        ('["runbooks/./x.md"]', r"paths\[0\] of 'Runbook' .*has a '\.' segment"),
        ('["runbooks//x.md"]', r"paths\[0\] of 'Runbook' .*has an empty folder name"),
        ('["runbooks/"]', r"paths\[0\] of 'Runbook' .*has an empty folder name"),
        ('["a/**/b/**/c/**/*.md"]', r"paths\[0\] of 'Runbook' .*uses \*\* more than 2 times"),
        ('["' + "**/*/" * 30 + 'x"]', r"paths\[0\] of 'Runbook' .*uses \*\* more than 2 times"),
        ('["run\\tbooks/*.md"]', r"paths\[0\] of 'Runbook' contains a control character"),
        ('["run\\x7fbooks/*.md"]', r"paths\[0\] of 'Runbook' contains a control character"),
        ('["run\\x85books/*.md"]', r"paths\[0\] of 'Runbook' contains a control character"),
        ('["run\\u202ebooks/*.md"]', r"paths\[0\] of 'Runbook' contains a format character"),
    ],
    ids=["empty", "21", "absolute", "dotdot", "bare dotdot", "backslash", "empty glob", "long",
         "dot slash", "dot segment", "double slash", "trailing slash", "three globstars", "thirty globstars",
         "tab", "DEL", "C1", "format"],
)
def test_docs_paths_rules(tmp_path, paths, message):
    text = runbook_with('paths: ["runbooks/**/*.md"]', f"paths: {paths}")
    with pytest.raises(ProjectSchemaError, match=message):
        load_project_schema(write_schema(tmp_path, text))


def test_a_glob_of_exactly_200_characters_and_20_globs_are_accepted(tmp_path):
    paths = "[" + ", ".join(f'"{i:02d}' + "a" * 195 + '.md"' for i in range(20)) + "]"
    text = runbook_with('paths: ["runbooks/**/*.md"]', f"paths: {paths}")
    assert len(load_project_schema(write_schema(tmp_path, text)).node_types[0].source.paths) == 20


@pytest.mark.parametrize(
    "condition, message",
    [
        ("{field: type}", r"where\[0\] of 'Runbook' must use exactly one of is, starts_with, contains, like"),
        ("{field: type, is: a, contains: b}", r"where\[0\] of 'Runbook' must use exactly one of is, starts_with, contains, like"),
        ("{field: type, like: " + "a" * 201 + "}", r"where\[0\] of 'Runbook' compares with text longer than 200 characters"),
        ('{field: "", is: a}', r"where\[0\] of 'Runbook' names an empty front-matter key"),
        ("{field: " + "k" * 65 + ", is: a}", r"where\[0\] of 'Runbook' names a front-matter key longer than 64 characters$"),
        ('{field: "ty\\tpe", is: a}', r"where\[0\] of 'Runbook' .*control character"),
        ('{field: "ty\\x7fpe", is: a}', r"where\[0\] of 'Runbook' names a front-matter key with a control character"),
        ('{field: "ty\\x85pe", is: a}', r"where\[0\] of 'Runbook' names a front-matter key with a control character"),
        ('{field: "ty\\u202epe", is: a}', r"where\[0\] of 'Runbook' names a front-matter key with a format character"),
        ("{field: type, like: '" + "*a" * 11 + "'}", r"where\[0\] of 'Runbook' uses \* more than 10 times"),
        ("{field: type, is: null, like: a}", r"where\[0\] of 'Runbook' gives 'is' no value; give it text or remove it"),
        ("{field: type, starts_with: null}", r"where\[0\] of 'Runbook' gives 'starts_with' no value"),
        ("{field: type, is: 0x" + "f" * 5000 + "}", r"a whole number in a condition must fit in 64 bits"),
    ],
    ids=["no operator", "two operators", "long text", "empty field", "long field", "control char", "DEL", "C1",
         "format char", "eleven stars", "null beside another", "null alone", "huge int"],
)
def test_docs_condition_rules(tmp_path, condition, message):
    text = runbook_with("- {field: type, is: runbook}", f"- {condition}")
    with pytest.raises(ProjectSchemaError, match=message):
        load_project_schema(write_schema(tmp_path, text))


@pytest.mark.parametrize("operator", ["regex", "matches"])
def test_regex_and_matches_operators_are_refused_as_unknown(tmp_path, operator):
    text = runbook_with("- {field: type, is: runbook}", f"- {{field: type, {operator}: 'run.*'}}")
    with pytest.raises(ProjectSchemaError, match=rf"where\.0\.{operator}: Extra inputs are not permitted"):
        load_project_schema(write_schema(tmp_path, text))


@pytest.mark.parametrize(
    "value, text",
    [("1", "1"), ('"1"', "1"), ("-42", "-42"), ("true", "true"), ("false", "false"), ("yes", "true"), ("runbook", "runbook")],
)
def test_condition_values_are_stored_as_canonical_text(tmp_path, value, text):
    source = runbook_with("- {field: type, is: runbook}", f"- {{field: type, is: {value}}}")
    condition = load_project_schema(write_schema(tmp_path, source)).node_types[0].source.where[0]
    assert condition.is_ == text and condition.text == text and condition.operator == "is"


@pytest.mark.parametrize("value", ["1.5", "2024-01-01", "[a]", "{a: b}", "null"])
def test_condition_values_must_be_text_numbers_or_booleans(tmp_path, value):
    text = runbook_with("- {field: type, is: runbook}", f"- {{field: type, is: {value}}}")
    with pytest.raises(ProjectSchemaError):
        load_project_schema(write_schema(tmp_path, text))


def test_where_holds_at_most_20_conditions(tmp_path):
    many = "\n".join(f"            - {{field: f{i}, is: x}}" for i in range(21))
    text = runbook_with(
        "            - {field: type, is: runbook}\n            - {field: title, starts_with: \"RB-\"}", many
    )
    with pytest.raises(ProjectSchemaError, match=r"where of 'Runbook' lists 21 conditions; at most 20"):
        load_project_schema(write_schema(tmp_path, text))


def test_a_like_text_may_use_ten_stars(tmp_path):
    text = runbook_with("- {field: title, starts_with: \"RB-\"}", "- {field: title, like: '" + "*a" * 10 + "'}")
    assert load_project_schema(write_schema(tmp_path, text)) is not None


def docs_types(count: int, conditions: int) -> str:
    """A schema with `count` docs-sourced node types of `conditions` conditions each."""
    where = ", ".join(f"{{field: f{i}, is: x}}" for i in range(conditions))
    types = "".join(
        f"""
          - label: Doc{t}
            key: [path]
            metadata: [{{name: path}}]
            source: {{provider: docs, paths: ["**/*.md"], where: [{where}]}}"""
        for t in range(count)
    )
    return "version: 1\nnode_types:" + textwrap.dedent(types) + "\n"


def test_a_schema_sources_at_most_20_node_types_from_docs(tmp_path):
    assert load_project_schema(write_schema(tmp_path, docs_types(20, 0))) is not None
    with pytest.raises(ProjectSchemaError, match=r"21 node types are sourced from Markdown front matter; at most 20"):
        load_project_schema(write_schema(tmp_path, docs_types(21, 0)))


def test_a_schema_lists_at_most_100_conditions_in_all(tmp_path):
    assert load_project_schema(write_schema(tmp_path, docs_types(5, 20))) is not None
    with pytest.raises(ProjectSchemaError, match=r"the docs node types list 102 conditions in all; at most 100"):
        load_project_schema(write_schema(tmp_path, docs_types(6, 17)))


def test_fields_holds_at_most_50_entries(tmp_path):
    metadata = "\n".join(f"          - {{name: f{i}}}" for i in range(51))
    mapping = ", ".join(f"f{i}: k{i}" for i in range(51))
    text = runbook_with("          - {name: on_call}", "          - {name: on_call}\n" + metadata).replace(
        "fields: {on_call: on-call-team}", f"fields: {{{mapping}}}"
    )
    with pytest.raises(ProjectSchemaError, match=r"fields of 'Runbook' maps 51 fields; at most 50"):
        load_project_schema(write_schema(tmp_path, text))


@pytest.mark.parametrize(
    "mapping, message",
    [
        ("{owner_name: owner}", r"fields of 'Runbook' maps 'owner_name', which is not a declared metadata field"),
        ("{path: file}", r"fields of 'Runbook' maps 'path', which is always the file's repo-relative path"),
        ('{owner: "own\\u0007er"}', r"fields of 'Runbook' maps 'owner' to a front-matter key with a control character"),
        ('{owner: ""}', r"fields of 'Runbook' maps 'owner' to an empty front-matter key"),
        ("{owner: " + "k" * 65 + "}", r"fields of 'Runbook' maps 'owner' to a front-matter key longer than 64 characters$"),
        ('{owner: "own\\u200eer"}', r"fields of 'Runbook' maps 'owner' to a front-matter key with a format character"),
    ],
    ids=["undeclared", "path", "control char", "empty key", "long key", "format char"],
)
def test_docs_fields_rules(tmp_path, mapping, message):
    text = runbook_with("fields: {on_call: on-call-team}", f"fields: {mapping}")
    with pytest.raises(ProjectSchemaError, match=message):
        load_project_schema(write_schema(tmp_path, text))


@pytest.mark.parametrize(
    "old, new",
    [
        ("fields: {on_call: on-call-team}", "fields: {on_call: on}"),
        ("          - {name: on_call}", "          - {name: on_call}\n          - {name: on}"),
        ("        field: service\n", "        field: yes\n"),
    ],
    ids=["fields value", "metadata name", "relationship field"],
)
def test_an_unquoted_yaml_boolean_word_gets_a_plain_hint(tmp_path, old, new):
    text = runbook_with(old, new)
    with pytest.raises(ProjectSchemaError, match=r"YAML reads an unquoted on, off, yes or no as true or false; to mean the word, quote it: 'on'"):
        load_project_schema(write_schema(tmp_path, text))


@pytest.mark.parametrize(
    "old, new, message",
    [
        ('paths: ["runbooks/**/*.md"]', 'paths: ["runbooks/\\ud800/*.md"]', r"paths\[0\] of 'Runbook' contains a lone surrogate"),
        ("- {field: type, is: runbook}", '- {field: "ty\\ud800pe", is: runbook}', r"where\[0\] of 'Runbook' names a front-matter key with a lone surrogate"),
        ("- {field: type, is: runbook}", '- {field: type, is: "run\\ud800"}', r"where\[0\] of 'Runbook' compares with text that has a lone surrogate"),
        ("- {field: type, is: runbook}", '- {field: type, starts_with: "run\\ud800"}', r"where\[0\] of 'Runbook' compares with text that has a lone surrogate"),
        ("- {field: type, is: runbook}", '- {field: type, contains: "\\udfff"}', r"where\[0\] of 'Runbook' compares with text that has a lone surrogate"),
        ("- {field: type, is: runbook}", '- {field: type, like: "*\\ud800*"}', r"where\[0\] of 'Runbook' compares with text that has a lone surrogate"),
        ("fields: {on_call: on-call-team}", 'fields: {on_call: "on\\ud800call"}', r"fields of 'Runbook' maps 'on_call' to a front-matter key with a lone surrogate"),
        ("        field: service\n", '        field: "serv\\ud800ice"\n', r"relationship 'RUNBOOK_FOR' field .*lone surrogate"),
    ],
    ids=["glob", "where field", "is", "starts_with", "contains", "like", "fields key", "relationship field"],
)
def test_lone_surrogates_are_refused(tmp_path, old, new, message):
    text = runbook_with(old, new)
    with pytest.raises(ProjectSchemaError, match=message) as caught:
        load_project_schema(write_schema(tmp_path, text))
    assert all(not "\ud800" <= c <= "\udfff" for c in str(caught.value))


@pytest.mark.parametrize(
    "provider",
    ["0x" + "f" * 5000, "1", "[docs]", "{a: b}", "true", "null"],
    ids=["huge int", "int", "list", "map", "bool", "null"],
)
def test_a_non_text_source_provider_gets_a_plain_message(tmp_path, capfd, provider):
    text = runbook_with("          provider: docs\n", f"          provider: {provider}\n")
    with pytest.raises(ProjectSchemaError, match=r"source provider must be one of filesystem, docs") as caught:
        load_project_schema(write_schema(tmp_path, text))
    assert "Exceeds the limit" not in str(caught.value)
    assert capfd.readouterr().err == ""


ADR = """
    version: 1
    node_types:
      - label: Adr
        key: [adr_id]
        metadata:
          - {name: path}
          - {name: adr_id}
          - {name: title}
          - {name: status}
        source:
          provider: docs
          paths: ["decisions/**/*.md"]
          fields: {adr_id: id}
    relationships:
      - type: REPLACES
        provider: docs
        from: Adr
        to: Adr
        field: supersedes
"""


def adr_with(old: str, new: str) -> str:
    assert old in ADR, old
    return ADR.replace(old, new)


def test_the_adr_example_validates(tmp_path):
    effective = resolve_effective_schema(write_schema(tmp_path, ADR))
    (adr,) = effective.node_types
    assert adr.key == ("adr_id",)
    assert adr.source.fields == {"adr_id": "id"}
    (relationship,) = effective.relationships
    assert (relationship.from_labels, relationship.to, relationship.field) == (("Adr",), "Adr", "supersedes")


def test_a_docs_key_field_may_keep_its_own_name(tmp_path):
    text = ADR.replace("adr_id", "id").replace("          fields: {id: id}\n", "")
    (adr,) = resolve_effective_schema(write_schema(tmp_path, text)).node_types
    assert adr.key == ("id",)
    assert adr.source.fields == {}


DOCS_KEY_SHAPE = (
    r"node type 'Adr' is sourced from Markdown front matter, so its key must be \[path\] or one string field "
    r"read from front matter \(such as \[adr_id\]\)"
)


@pytest.mark.parametrize(
    "old, new, message",
    [
        ("key: [adr_id]", "key: [adr_id, title]", DOCS_KEY_SHAPE),
        ("key: [adr_id]", "key: [path, adr_id]", DOCS_KEY_SHAPE),
        (
            "- {name: adr_id}",
            "- {name: adr_id, type: integer}",
            r"key field 'adr_id' of 'Adr' must be a string: keys are compared as text "
            r"\(use string; numbers like 12 still work\)",
        ),
        (
            "- {name: adr_id}",
            "- {name: adr_id, type: boolean}",
            r"key field 'adr_id' of 'Adr' must be a string: keys are compared as text "
            r"\(use string; numbers like 12 still work\)",
        ),
        ("key: [adr_id]", "key: [number]", r"node type 'Adr' key component 'number' is not a declared metadata field"),
        (
            "          - {name: path}\n",
            "",
            r"node type 'Adr' is sourced from Markdown front matter, so it must declare a string 'path' field: "
            r"every entry records its file there",
        ),
        (
            "- {name: path}",
            "- {name: path, type: integer}",
            r"node type 'Adr' is sourced from Markdown front matter, so it must declare a string 'path' field: "
            r"every entry records its file there",
        ),
    ],
    ids=["composite", "path plus field", "integer key", "boolean key", "undeclared key", "no path", "non-string path"],
)
def test_docs_key_rules(tmp_path, old, new, message):
    with pytest.raises(ProjectSchemaError, match=message):
        load_project_schema(write_schema(tmp_path, adr_with(old, new)))


def test_a_path_keyed_docs_type_still_needs_a_string_path(tmp_path):
    text = runbook_with("- {name: path}", "- {name: path, type: integer}")
    with pytest.raises(ProjectSchemaError, match="so it must declare a string 'path' field"):
        load_project_schema(write_schema(tmp_path, text))


def test_a_field_keyed_docs_type_gets_its_key_constraint_and_name_index(tmp_path):
    statements = resolve_effective_schema(write_schema(tmp_path, ADR)).constraint_statements()
    assert statements[len(constraint_statements()) :] == [
        "CREATE CONSTRAINT adr_repo_key IF NOT EXISTS FOR (n:Adr) REQUIRE (n.repo_id, n.adr_id) IS UNIQUE",
        "CREATE INDEX adr_repo_name IF NOT EXISTS FOR (n:Adr) ON (n.repo_id, n.name)",
    ]


def test_filesystem_key_message_still_names_the_filesystem(tmp_path):
    text = WORKTREE.replace(
        "      - label: File\n        key: [path]\n        metadata: [{name: path}]",
        "      - label: File\n        key: [slug]\n        metadata: [{name: slug}]",
    )
    with pytest.raises(ProjectSchemaError, match=r"sourced from the filesystem, so its key must be exactly \[path\]"):
        load_project_schema(write_schema(tmp_path, text))


@pytest.mark.parametrize(
    "old, new, message",
    [
        ("        field: service\n", "", r"relationship 'RUNBOOK_FOR' uses the docs provider, so it needs a 'field'"),
        ("        field: service\n", "        field: service\n        custom: {name: linker}\n", "docs provider, which must not declare a custom block"),
        ("type: RUNBOOK_FOR", "type: DOCUMENTED_BY", "cannot be redeclared by the docs provider"),
        ("        field: service\n", '        field: "ser\\nvice"\n', r"relationship 'RUNBOOK_FOR' field .*control character"),
        ("        field: service\n", '        field: "ser\\u202evice"\n', r"relationship 'RUNBOOK_FOR' field .*format character"),
    ],
    ids=["no field", "custom block", "builtin type", "control char", "format char"],
)
def test_docs_relationship_rules(tmp_path, old, new, message):
    with pytest.raises(ProjectSchemaError, match=message):
        load_project_schema(write_schema(tmp_path, runbook_with(old, new)))


def test_docs_relationship_from_labels_must_be_docs_sourced(tmp_path):
    text = runbook_with("from: Runbook", "from: [Runbook, Widget]").replace(
        "    relationships:",
        "      - label: Widget\n        key: [slug]\n        metadata: [{name: slug}]\n    relationships:",
    )
    with pytest.raises(
        ProjectSchemaError,
        match=r"docs relationship 'RUNBOOK_FOR' from label 'Widget' is not a node type sourced from Markdown front matter",
    ):
        load_project_schema(write_schema(tmp_path, text))


def test_a_filesystem_type_is_not_a_docs_relationship_source(tmp_path):
    text = WORKTREE + (
        "      - type: DESCRIBES\n"
        "        provider: docs\n"
        "        from: File\n"
        "        to: Folder\n"
        "        field: folder\n"
    )
    with pytest.raises(ProjectSchemaError, match="from label 'File' is not a node type sourced from Markdown front matter"):
        load_project_schema(write_schema(tmp_path, text))


@pytest.mark.parametrize("provider", ["builtin", "filesystem", "custom"])
def test_field_is_only_for_docs_relationships(tmp_path, provider):
    extra = {"builtin": "", "filesystem": "", "custom": "        custom: {name: linker}\n"}[provider]
    rel_type = "DOCUMENTED_BY" if provider == "builtin" else "IS_CHILD_OF"
    text = WORKTREE.replace("type: IS_CHILD_OF", f"type: {rel_type}").replace(
        "        provider: filesystem\n        from: [File, Folder]\n        to: Folder\n",
        f"        provider: {provider}\n        from: [File, Folder]\n        to: Folder\n{extra}        field: folder\n",
    )
    with pytest.raises(ProjectSchemaError, match=rf"relationship '{rel_type}' uses the {provider} provider; only docs relationships take a 'field'"):
        load_project_schema(write_schema(tmp_path, text))


def test_several_docs_node_types_are_allowed_beside_filesystem_types(tmp_path):
    text = WORKTREE.replace(
        "    relationships:",
        "      - label: Adr\n        key: [path]\n        metadata: [{name: path}]\n"
        "        source: {provider: docs, paths: ['adr/*.md']}\n"
        "      - label: Policy\n        key: [path]\n        metadata: [{name: path}]\n"
        "        source: {provider: docs, paths: ['**/*.md'], where: [{field: kind, is: policy}]}\n"
        "    relationships:",
    )
    effective = resolve_effective_schema(write_schema(tmp_path, text))
    providers = {n.label: n.source.provider for n in effective.node_types}
    assert providers == {"File": "filesystem", "Folder": "filesystem", "Adr": "docs", "Policy": "docs"}


def test_docs_types_do_not_count_toward_the_filesystem_kind_limit(tmp_path):
    # A docs type beside a filesystem file type is fine; two filesystem file types are not.
    text = WORKTREE.replace(
        "    relationships:",
        "      - label: Doc\n        key: [path]\n        metadata: [{name: path}]\n"
        "        source: {provider: docs, paths: ['**/*.md']}\n    relationships:",
    )
    load_project_schema(write_schema(tmp_path, text))
    with pytest.raises(ProjectSchemaError, match="both filesystem file types"):
        load_project_schema(write_schema(tmp_path, text.replace("source: {provider: docs, paths: ['**/*.md']}", "source: {provider: filesystem, kind: file}")))


def test_docs_types_get_a_repo_name_index_beside_their_key_constraint(tmp_path):
    effective = resolve_effective_schema(write_schema(tmp_path, RUNBOOK))
    statements = effective.constraint_statements()
    assert statements[len(constraint_statements()) :] == [
        "CREATE CONSTRAINT runbook_repo_key IF NOT EXISTS FOR (n:Runbook) REQUIRE (n.repo_id, n.path) IS UNIQUE",
        "CREATE INDEX runbook_repo_name IF NOT EXISTS FOR (n:Runbook) ON (n.repo_id, n.name)",
    ]


def test_a_docs_index_name_may_not_collide_with_an_existing_name(tmp_path, monkeypatch):
    monkeypatch.setattr(project_schema, "_builtin_constraint_names", lambda: {"runbook_repo_name"})
    with pytest.raises(ProjectSchemaError, match="runbook_repo_name"):
        resolve_effective_schema(write_schema(tmp_path, RUNBOOK))


def test_json_schema_describes_the_docs_source():
    defs = project_schema_json_schema()["$defs"]
    assert set(defs["DocsSource"]["properties"]) == {"provider", "paths", "where", "fields"}
    assert set(defs["Condition"]["properties"]) == {"field", "is", "starts_with", "contains", "like"}
    assert "field" in defs["RelationshipDecl"]["properties"]


def test_starter_example_is_the_runbook_example():
    text = project_schema.starter_schema_text()
    assert "provider: docs" in text and "RUNBOOK_FOR" in text and "provider: custom" not in text
    assert "comes later" not in text and "Markdown front matter" in text


def test_starter_header_says_a_docs_type_may_be_keyed_on_a_front_matter_field():
    text = project_schema.starter_schema_text()
    header = text[:text.index("version:")]
    assert "key: [adr_id]" in header and "supersedes: ADR-012" in header
    assert "key: [path]" in text[text.index("version:"):]  # the example stays the Runbook
