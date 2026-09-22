"""Constraint provisioning for a repository's optional project schema.

Deliberately Neo4j-free. What matters here is *which* statements a repository
resolves to and *when* they are resolved relative to any graph write, so a
recording stub engine pins both far more precisely than a live database could
-- and lets the invalid-schema path be triggered on demand. The statements
themselves are asserted as exact strings because they are the contract: a
repository without a schema file must keep provisioning byte-identically what
it always did.
"""

from pathlib import Path
from textwrap import dedent

import pytest

from devgraph.config.project_schema import (
    SCHEMA_FILENAME,
    ProjectSchemaError,
    resolve_effective_schema,
)
from devgraph.graph.engine import (
    provision_repository_schema,
    repository_constraint_statements,
)
from devgraph.graph.schema import constraint_statements

WIDGET_CONSTRAINT = (
    "CREATE CONSTRAINT widget_repo_key IF NOT EXISTS "
    "FOR (n:Widget) REQUIRE (n.repo_id, n.slug) IS UNIQUE"
)


class RecordingEngine:
    """Records what a repository would be provisioned with."""

    def __init__(self) -> None:
        self.provisioned: list[list[str]] = []

    def init_schema(self, effective=None) -> None:
        self.provisioned.append(repository_constraint_statements(effective))

    def upsert_repository(self, repo_id: str, name: str, path: str) -> None:
        raise AssertionError("upsert_repository must not run during provisioning")


def write_schema(repo: Path, body: str) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / SCHEMA_FILENAME).write_text(dedent(body), encoding="utf-8")
    return repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "sample-repo"
    path.mkdir()
    return path


def test_no_schema_file_provisions_the_builtin_statements_verbatim(repo):
    engine = RecordingEngine()

    provision_repository_schema(engine, repo)

    # Statement for statement, in order: an existing repository with no
    # configuration must see no change whatsoever.
    assert engine.provisioned == [constraint_statements()]


def test_omitting_the_effective_schema_provisions_only_the_builtins():
    """The ~15 callers that aren't repository-scoped keep today's behaviour."""
    assert repository_constraint_statements() == constraint_statements()
    assert repository_constraint_statements(None) == constraint_statements()


def test_a_declared_node_type_adds_exactly_its_constraint_after_the_builtins(repo):
    write_schema(
        repo,
        """
        version: 1
        node_types:
          - label: Widget
            key: [slug]
            metadata:
              - name: slug
        """,
    )
    engine = RecordingEngine()

    provision_repository_schema(engine, repo)

    assert engine.provisioned == [constraint_statements() + [WIDGET_CONSTRAINT]]


def test_extends_none_still_provisions_the_builtins(repo):
    """The database is shared and the indexer keeps writing built-in labels.

    `EffectiveSchema.constraint_statements()` drops the built-ins here, which
    is right for describing one declaration and wrong for provisioning: opting
    one repository out would leave every other repository's nodes unconstrained.
    """
    write_schema(
        repo,
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

    assert effective.constraint_statements() == [WIDGET_CONSTRAINT]
    assert repository_constraint_statements(effective) == (
        constraint_statements() + [WIDGET_CONSTRAINT]
    )


def test_declared_statements_are_appended_without_duplicating_the_builtins(repo):
    """`extends: default` replays the built-ins; they must not appear twice."""
    write_schema(
        repo,
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

    statements = repository_constraint_statements(resolve_effective_schema(repo))

    assert len(statements) == len(set(statements))
    assert statements[: len(constraint_statements())] == constraint_statements()


def test_every_provisioned_statement_is_guarded_so_reprovisioning_is_a_no_op(repo):
    """Idempotence is a property of the statements, not of a call count."""
    write_schema(
        repo,
        """
        version: 1
        node_types:
          - label: Widget
            key: [slug]
            metadata:
              - name: slug
        """,
    )

    for statement in repository_constraint_statements(resolve_effective_schema(repo)):
        assert statement.endswith(" IS UNIQUE") or statement.endswith(" IF EXISTS")
        assert "IF NOT EXISTS" in statement or statement.startswith("DROP CONSTRAINT")


def test_a_multi_component_key_is_scoped_by_repo_id_first(repo):
    write_schema(
        repo,
        """
        version: 1
        node_types:
          - label: Widget
            key: [slug, revision]
            metadata:
              - name: slug
              - name: revision
        """,
    )

    statements = repository_constraint_statements(resolve_effective_schema(repo))

    assert statements[-1] == (
        "CREATE CONSTRAINT widget_repo_key IF NOT EXISTS "
        "FOR (n:Widget) REQUIRE (n.repo_id, n.slug, n.revision) IS UNIQUE"
    )


def test_an_invalid_schema_fails_before_anything_is_provisioned(repo):
    write_schema(repo, "version: 1\nnode_types:\n  - label: Widget\n")
    engine = RecordingEngine()

    with pytest.raises(ProjectSchemaError) as excinfo:
        provision_repository_schema(engine, repo)

    # The graph was never touched: not one constraint, and (per
    # RecordingEngine) no upsert either.
    assert engine.provisioned == []
    assert SCHEMA_FILENAME in str(excinfo.value)


def test_a_malformed_schema_fails_with_the_loaders_own_message(repo):
    write_schema(repo, "version: 1\nnode_types: [oh: dear\n")
    engine = RecordingEngine()

    with pytest.raises(ProjectSchemaError, match="malformed YAML"):
        provision_repository_schema(engine, repo)

    assert engine.provisioned == []


def test_the_repository_root_may_be_a_string(repo):
    """`RepoRecord.path` is a Path, but callers elsewhere pass strings."""
    engine = RecordingEngine()

    provision_repository_schema(engine, str(repo))

    assert engine.provisioned == [constraint_statements()]
