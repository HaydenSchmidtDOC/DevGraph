"""`devgraph config schema` (list/add/edit/delete/reset)."""

import json
import textwrap

import click
import pytest
from typer.testing import CliRunner

from devgraph.cli import main as cli_main
from devgraph.cli.main import app
from devgraph.config import project_switch
from devgraph.config.project_schema import SCHEMA_FILENAME
from devgraph.config.settings import Settings


@pytest.fixture
def runner():
    return CliRunner(env={"COLUMNS": "200"})


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    fake = Settings(_env_file=None, neo4j_password="x", registry_db_path=tmp_path / "registry.sqlite3")
    monkeypatch.setattr(cli_main, "get_settings", lambda: fake)
    monkeypatch.setattr(project_switch, "_registry_db_path", lambda: fake.registry_db_path)
    return fake


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return root


def register(settings, root, repo_id="demo", *, enabled=True, watched=True):
    from devgraph.registry.store import RepoRegistry

    (root / ".git").mkdir(exist_ok=True)
    registry = RepoRegistry(settings.registry_db_path)
    try:
        registry.add_repo(root, repo_id=repo_id)
        if not enabled:
            registry.set_project_config_enabled(repo_id, False)
        if not watched:
            registry.disable_watch(repo_id)
    finally:
        registry.close()


def flat(output):
    return " ".join(output.split())


SCHEMA = textwrap.dedent("""\
    # my schema
    version: 1
    node_types:
      # tickets
      - label: Ticket
        key: [id]
        metadata:
          - name: id
            type: string
    relationships:
      - type: USES
        from: Ticket
        to: Function
""")

TICKET = "label: Ticket\nkey: [id]\nmetadata:\n  - name: id\n    type: string\n"
EPIC = "label: Epic\nkey: [id]\nmetadata:\n  - name: id\n    type: string\n"
REL = "type: DEPENDS_ON\nfrom: Ticket\nto: Function\n"


def src(tmp_path, text, name="entry.yaml"):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def schema_file(repo):
    return repo / SCHEMA_FILENAME


def run(runner, *args):
    return runner.invoke(app, ["config", "schema", *args])


def parsed(repo):
    from devgraph.config.project_schema import parse_project_schema

    path = schema_file(repo)
    return parse_project_schema(path.read_text(), path)


# -- list --------------------------------------------------------------------


def test_list_shows_builtin_and_project_entries(runner, settings, repo):
    register(settings, repo)
    schema_file(repo).write_text(SCHEMA)
    result = run(runner, "list", "--repo", str(repo))
    assert result.exit_code == 0, result.output
    out = flat(result.output)
    assert "Function" in out and "built-in" in out
    assert "Ticket" in out and "project" in out and "USES" in out


def test_list_json_shape(runner, settings, repo):
    register(settings, repo)
    schema_file(repo).write_text(SCHEMA)
    result = run(runner, "list", "--repo", str(repo), "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    ticket = next(n for n in data["node_types"] if n["label"] == "Ticket")
    assert ticket == {"label": "Ticket", "origin": "project", "key": ["id"], "source": None, "color": None}
    assert any(n["label"] == "Function" and n["origin"] == "built-in" for n in data["node_types"])
    rel = next(r for r in data["relationships"] if r["origin"] == "project")
    assert rel == {"type": "USES", "from": ["Ticket"], "to": "Function", "provider": "builtin", "field": None, "origin": "project", "color": None}


def test_list_disabled_repo_says_so(runner, settings, repo):
    register(settings, repo, enabled=False)
    schema_file(repo).write_text(SCHEMA)
    result = run(runner, "list", "--repo", str(repo))
    assert result.exit_code == 0, result.output
    out = flat(result.output)
    assert "disabled" in out and "Function" in out and "Ticket" not in out


def test_list_json_and_table_include_colour(runner, settings, repo):
    register(settings, repo)
    schema_file(repo).write_text(
        SCHEMA.replace("- label: Ticket", '- label: Ticket\n    color: "#1f77b4"').replace(
            "- type: USES", '- type: USES\n    color: "#FF0000"'
        )
    )
    data = json.loads(run(runner, "list", "--repo", str(repo), "--json").output)
    assert next(n for n in data["node_types"] if n["label"] == "Ticket")["color"] == "#1f77b4"
    assert next(r for r in data["relationships"] if r["origin"] == "project")["color"] == "#FF0000"
    assert next(n for n in data["node_types"] if n["label"] == "Function")["color"] is None
    out = flat(run(runner, "list", "--repo", str(repo)).output)
    assert "Colour" in out and "#1f77b4" in out


# -- add ---------------------------------------------------------------------


def test_add_node_type_and_relationship_keep_comments(runner, settings, repo, tmp_path):
    register(settings, repo)
    schema_file(repo).write_text(SCHEMA)
    result = run(runner, "add", "--from", src(tmp_path, EPIC), "--repo", str(repo))
    assert result.exit_code == 0, result.output
    assert "devgraph rescan demo --now" in flat(result.output)
    result = run(runner, "add", "--from", src(tmp_path, REL), "--repo", str(repo))
    assert result.exit_code == 0, result.output
    text = schema_file(repo).read_text()
    assert "# my schema" in text and "# tickets" in text
    schema = parsed(repo)
    assert [n.label for n in schema.node_types] == ["Ticket", "Epic"]
    assert [r.type for r in schema.relationships] == ["USES", "DEPENDS_ON"]


def test_add_creates_a_new_file(runner, repo, tmp_path):
    result = run(runner, "add", "--from", src(tmp_path, TICKET), "--repo", str(repo))
    assert result.exit_code == 0, result.output
    assert schema_file(repo).read_text().startswith("version: 1\nnode_types:\n")
    assert [n.label for n in parsed(repo).node_types] == ["Ticket"]


def test_add_existing_label_points_at_edit(runner, repo, tmp_path):
    schema_file(repo).write_text(SCHEMA)
    result = run(runner, "add", "--from", src(tmp_path, TICKET), "--repo", str(repo))
    assert result.exit_code == 1 and "devgraph config schema edit" in flat(result.output)
    assert schema_file(repo).read_text() == SCHEMA


def test_add_builtin_label_refused(runner, repo, tmp_path):
    result = run(runner, "add", "--from", src(tmp_path, EPIC.replace("Epic", "Function")), "--repo", str(repo))
    assert result.exit_code == 1
    assert not schema_file(repo).exists()


def test_add_key_not_in_metadata_refused(runner, repo, tmp_path):
    result = run(runner, "add", "--from", src(tmp_path, EPIC.replace("key: [id]", "key: [nope]")), "--repo", str(repo))
    assert result.exit_code == 1 and "nope" in result.output
    assert not schema_file(repo).exists()


@pytest.mark.parametrize("text", ["description: x\n", "label: A\ntype: B\n", "- a\n"])
def test_add_needs_exactly_one_of_label_or_type(runner, repo, tmp_path, text):
    result = run(runner, "add", "--from", src(tmp_path, text), "--repo", str(repo))
    assert result.exit_code == 1
    assert not schema_file(repo).exists()


def test_add_relationship_sharing_a_type_allowed_unless_identical(runner, repo, tmp_path):
    schema_file(repo).write_text(SCHEMA)
    other = "type: USES\nfrom: Ticket\nto: Class\n"
    assert run(runner, "add", "--from", src(tmp_path, other), "--repo", str(repo)).exit_code == 0
    before = schema_file(repo).read_text()
    result = run(runner, "add", "--from", src(tmp_path, other), "--repo", str(repo))
    assert result.exit_code == 1 and "already" in result.output
    assert schema_file(repo).read_text() == before


def test_add_from_stdin(runner, repo):
    result = runner.invoke(app, ["config", "schema", "add", "--from", "-", "--repo", str(repo)], input=TICKET)
    assert result.exit_code == 0, result.output


# -- edit --------------------------------------------------------------------


def test_edit_from_file(runner, repo, tmp_path):
    schema_file(repo).write_text(SCHEMA)
    new = TICKET.replace("key: [id]", "key: [id]\ndescription: Work item").replace("Ticket", "Ticket")
    result = run(runner, "edit", "Ticket", "--from", src(tmp_path, new), "--repo", str(repo))
    assert result.exit_code == 0, result.output
    assert parsed(repo).node_types[0].description == "Work item"
    assert "# my schema" in schema_file(repo).read_text()


def test_edit_in_editor(runner, repo, monkeypatch):
    schema_file(repo).write_text(SCHEMA)
    seen = {}

    def fake_edit(text, **kwargs):
        seen["text"] = text
        return text.replace("key: [id]", "key: [id]\ndescription: Edited") if "key: [id]" in text else text.replace("- id", "- id\ndescription: Edited")

    monkeypatch.setattr(click, "edit", fake_edit)
    result = run(runner, "edit", "Ticket", "--repo", str(repo))
    assert result.exit_code == 0, result.output
    assert "label: Ticket" in seen["text"]
    assert parsed(repo).node_types[0].description == "Edited"


def test_edit_unchanged_says_no_changes(runner, repo, monkeypatch):
    schema_file(repo).write_text(SCHEMA)
    monkeypatch.setattr(click, "edit", lambda text, **kw: text)
    result = run(runner, "edit", "Ticket", "--repo", str(repo))
    assert result.exit_code == 0 and "No changes." in result.output
    assert schema_file(repo).read_text() == SCHEMA


def test_edit_invalid_leaves_file(runner, repo, tmp_path):
    schema_file(repo).write_text(SCHEMA)
    result = run(runner, "edit", "Ticket", "--from", src(tmp_path, TICKET.replace("[id]", "[zzz]")), "--repo", str(repo))
    assert result.exit_code == 1
    assert schema_file(repo).read_text() == SCHEMA


def test_edit_unknown_name(runner, repo):
    schema_file(repo).write_text(SCHEMA)
    result = run(runner, "edit", "Nope", "--from", "-", "--repo", str(repo), )
    assert result.exit_code == 1 and "Nope" in result.output


def test_edit_duplicated_relationship_type_refused(runner, repo, tmp_path):
    schema_file(repo).write_text(SCHEMA + "  - type: USES\n    from: Ticket\n    to: Class\n")
    result = run(runner, "edit", "USES", "--from", src(tmp_path, "type: USES\nfrom: Ticket\nto: Function\n"), "--repo", str(repo))
    assert result.exit_code == 1 and "edit the file by hand" in flat(result.output)


def test_edit_rename_warns_about_removed_type(runner, settings, repo, tmp_path):
    register(settings, repo)
    schema_file(repo).write_text(SCHEMA.split("relationships:")[0])
    result = run(runner, "edit", "Ticket", "--from", src(tmp_path, TICKET.replace("Ticket", "Story")), "--repo", str(repo))
    assert result.exit_code == 0, result.output
    assert "next rescan deletes" in flat(result.output) and "Ticket" in result.output


def test_edit_rename_in_editor_warns(runner, settings, repo, monkeypatch):
    register(settings, repo)
    schema_file(repo).write_text(SCHEMA.split("relationships:")[0])
    monkeypatch.setattr(click, "edit", lambda text, **kw: text.replace("Ticket", "Story"))
    result = run(runner, "edit", "Ticket", "--repo", str(repo))
    assert result.exit_code == 0, result.output
    assert "next rescan deletes" in flat(result.output)


def test_edit_description_only_does_not_warn(runner, repo, tmp_path):
    schema_file(repo).write_text(SCHEMA)
    new = TICKET + "description: Work item\n"
    result = run(runner, "edit", "Ticket", "--from", src(tmp_path, new), "--repo", str(repo))
    assert result.exit_code == 0, result.output
    assert "Warning" not in result.output


# -- delete ------------------------------------------------------------------


def test_delete_relationship(runner, repo):
    schema_file(repo).write_text(SCHEMA)
    result = run(runner, "delete", "USES", "--repo", str(repo))
    assert result.exit_code == 0, result.output
    assert parsed(repo).relationships == ()
    assert "relationship" in flat(result.output) and "USES" in result.output


def test_delete_node_type_warns_about_rescan(runner, settings, repo):
    register(settings, repo)
    schema_file(repo).write_text(SCHEMA.split("relationships:")[0])
    result = run(runner, "delete", "Ticket", "--repo", str(repo))
    assert result.exit_code == 0, result.output
    out = flat(result.output)
    assert "next rescan deletes" in out and "Ticket" in out
    assert parsed(repo).node_types == ()


def test_delete_endpoint_node_type_refused(runner, repo):
    schema_file(repo).write_text(SCHEMA)
    result = run(runner, "delete", "Ticket", "--repo", str(repo))
    assert result.exit_code == 1 and "endpoint" in flat(result.output)
    assert schema_file(repo).read_text() == SCHEMA


def test_delete_unknown_name(runner, repo):
    schema_file(repo).write_text(SCHEMA)
    assert run(runner, "delete", "Nope", "--repo", str(repo)).exit_code == 1


# -- ambiguity ---------------------------------------------------------------

AMBIGUOUS = "version: 1\nnode_types:\n  - label: Dual\n    key: [id]\n    metadata:\n      - name: id\n        type: string\nrelationships:\n  - type: Dual\n    from: Dual\n    to: Function\n"


def test_ambiguous_name_needs_a_flag(runner, repo):
    schema_file(repo).write_text(AMBIGUOUS)
    result = run(runner, "delete", "Dual", "--repo", str(repo))
    assert result.exit_code == 1
    assert "--node-type" in result.output and "--relationship" in result.output
    assert schema_file(repo).read_text() == AMBIGUOUS
    result = run(runner, "delete", "Dual", "--relationship", "--repo", str(repo))
    assert result.exit_code == 0, result.output
    assert parsed(repo).relationships == () and parsed(repo).node_types[0].label == "Dual"


# -- reset -------------------------------------------------------------------


def test_reset_yes_deletes_and_warns(runner, settings, repo):
    register(settings, repo)
    schema_file(repo).write_text(SCHEMA)
    result = run(runner, "reset", "--yes", "--repo", str(repo))
    assert result.exit_code == 0, result.output
    assert not schema_file(repo).exists()
    out = flat(result.output)
    assert "next rescan deletes" in out and "Ticket" in out


def test_reset_declined_aborts(runner, repo):
    schema_file(repo).write_text(SCHEMA)
    result = runner.invoke(app, ["config", "schema", "reset", "--repo", str(repo)], input="n\n")
    assert result.exit_code != 0
    assert schema_file(repo).read_text() == SCHEMA


def test_reset_without_file(runner, repo):
    result = run(runner, "reset", "--yes", "--repo", str(repo))
    assert result.exit_code == 0 and "Nothing to reset" in result.output


# -- effect notes and scope --------------------------------------------------


def test_disabled_repo_note(runner, settings, repo, tmp_path):
    register(settings, repo, enabled=False)
    result = run(runner, "add", "--from", src(tmp_path, TICKET), "--repo", str(repo))
    assert result.exit_code == 0, result.output
    assert "not applied while the project config is disabled" in flat(result.output).lower()


def test_unregistered_directory_warns(runner, repo, tmp_path):
    result = run(runner, "add", "--from", src(tmp_path, TICKET), "--repo", str(repo))
    assert result.exit_code == 0, result.output
    assert "not a registered repository" in flat(result.output)


def test_default_scope_from_subdirectory(runner, settings, repo, tmp_path, monkeypatch):
    register(settings, repo)
    sub = repo / "pkg" / "inner"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    result = run(runner, "add", "--from", src(tmp_path, TICKET))
    assert result.exit_code == 0, result.output
    assert schema_file(repo).exists() and not (sub / SCHEMA_FILENAME).exists()


# -- accurate warnings (final review) ----------------------------------------

FS_DOC = "label: Doc\nkey: [path]\nmetadata:\n  - name: path\n    type: string\nsource:\n  provider: filesystem\n  kind: file\n"
FS_DOC_NO_SOURCE = FS_DOC.split("source:")[0]
FS_DOC_FOLDER = FS_DOC.replace("kind: file", "kind: folder")
FS_SCHEMA = "version: 1\nnode_types:\n  - " + FS_DOC.replace("\n", "\n    ").rstrip(" ")


def test_removed_types_ignores_builtin_relationship_types():
    from devgraph.cli.main import _removed_types
    from devgraph.config.project_schema import parse_project_schema
    from pathlib import Path

    text = SCHEMA + "  - type: CONTAINS\n    from: Ticket\n    to: Function\n"
    before = parse_project_schema(text, Path("x"))
    after = parse_project_schema(SCHEMA, Path("x"))
    assert _removed_types(before, after) == ([], [])


def test_delete_builtin_relationship_type_does_not_warn(runner, repo):
    schema_file(repo).write_text(SCHEMA + "  - type: CONTAINS\n    from: Ticket\n    to: Function\n")
    result = run(runner, "delete", "CONTAINS", "--repo", str(repo))
    assert result.exit_code == 0, result.output
    assert "Warning" not in result.output


def test_edit_dropping_source_warns_nodes_pruned(runner, settings, repo, tmp_path):
    register(settings, repo)
    schema_file(repo).write_text(FS_SCHEMA)
    result = run(runner, "edit", "Doc", "--from", src(tmp_path, FS_DOC_NO_SOURCE), "--repo", str(repo))
    assert result.exit_code == 0, result.output
    out = flat(result.output)
    assert "next rescan deletes the nodes of node type(s) whose filesystem source changed: Doc (source removed)" in out


def test_edit_changing_kind_warns_nodes_pruned(runner, settings, repo, tmp_path):
    register(settings, repo)
    schema_file(repo).write_text(FS_SCHEMA)
    result = run(runner, "edit", "Doc", "--from", src(tmp_path, FS_DOC_FOLDER), "--repo", str(repo))
    assert result.exit_code == 0, result.output
    assert "Doc (kind file -> folder)" in flat(result.output)


def test_edit_description_only_on_filesystem_type_does_not_warn(runner, repo, tmp_path):
    schema_file(repo).write_text(FS_SCHEMA)
    result = run(runner, "edit", "Doc", "--from", src(tmp_path, FS_DOC + "description: d\n"), "--repo", str(repo))
    assert result.exit_code == 0, result.output
    assert "Warning" not in result.output and "no provider" not in result.output


def test_add_and_edit_without_source_note_nothing_populates_it(runner, repo, tmp_path):
    result = run(runner, "add", "--from", src(tmp_path, TICKET), "--repo", str(repo))
    assert result.exit_code == 0, result.output
    out = flat(result.output)
    assert "no provider produces Ticket nodes yet" in out and "source: {provider: filesystem}" in out
    result = run(runner, "edit", "Ticket", "--from", src(tmp_path, TICKET + "description: d\n"), "--repo", str(repo))
    assert "no provider produces Ticket nodes yet" in flat(result.output)


def test_add_with_source_has_no_source_note(runner, repo, tmp_path):
    result = run(runner, "add", "--from", src(tmp_path, FS_DOC), "--repo", str(repo))
    assert result.exit_code == 0, result.output
    assert "no provider" not in result.output


def test_effect_note_watched_repo(runner, settings, repo, tmp_path):
    register(settings, repo)
    result = run(runner, "add", "--from", src(tmp_path, TICKET), "--repo", str(repo))
    out = flat(result.output)
    assert (
        "is applied about 5 minutes after the last edit while the DevGraph agent (tray or headless) is running, "
        "or now with `devgraph rescan demo --now`"
    ) in out


def test_effect_note_unwatched_repo(runner, settings, repo, tmp_path):
    register(settings, repo, watched=False)
    result = run(runner, "add", "--from", src(tmp_path, TICKET), "--repo", str(repo))
    out = flat(result.output)
    assert "run `devgraph rescan demo --now` to apply it" in out and "5 minutes" not in out


def test_conflict_with_another_registered_repo_warns(runner, settings, repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    register(settings, repo, "demo")
    register(settings, other, "other")
    other_text = "version: 1\nnode_types:\n  - label: Ticket\n    key: [code]\n    metadata:\n      - name: code\n        type: string\n"
    (other / SCHEMA_FILENAME).write_text(other_text)
    result = run(runner, "add", "--from", src(tmp_path, TICKET), "--repo", str(repo))
    assert result.exit_code == 0, result.output
    out = flat(result.output)
    assert "Warning" in out and "incompatible declarations of label 'ticket'" in out
    assert "demo declares Ticket keyed on (id)" in out and "other declares Ticket keyed on (code)" in out


def test_no_conflict_warning_for_unrelated_repos(runner, settings, repo, tmp_path):
    register(settings, repo)
    result = run(runner, "add", "--from", src(tmp_path, TICKET), "--repo", str(repo))
    assert "incompatible" not in result.output


def test_removal_warning_when_unregistered_says_when_applied(runner, repo):
    schema_file(repo).write_text(SCHEMA.split("relationships:")[0])
    result = run(runner, "delete", "Ticket", "--repo", str(repo))
    out = flat(result.output)
    assert "applying this schema deletes the nodes of the removed node type(s): Ticket" in out
    assert "next rescan deletes" not in out


def test_removal_warning_when_disabled_says_when_applied(runner, settings, repo):
    register(settings, repo, enabled=False)
    schema_file(repo).write_text(SCHEMA.split("relationships:")[0])
    result = run(runner, "reset", "--yes", "--repo", str(repo))
    out = flat(result.output)
    assert "applying this schema deletes" in out and "next rescan deletes" not in out


def test_add_invalid_entry_prefixed(runner, repo, tmp_path):
    result = run(runner, "add", "--from", src(tmp_path, EPIC.replace("key: [id]", "key: [nope]")), "--repo", str(repo))
    assert result.exit_code == 1 and "the new entry is invalid:" in flat(result.output)


def test_edit_invalid_entry_prefixed(runner, repo, tmp_path):
    schema_file(repo).write_text(SCHEMA)
    result = run(runner, "edit", "Ticket", "--from", src(tmp_path, TICKET.replace("[id]", "[zzz]")), "--repo", str(repo))
    assert result.exit_code == 1 and "the new entry is invalid:" in flat(result.output)


def test_delete_referenced_node_type_hints(runner, repo):
    schema_file(repo).write_text(SCHEMA)
    result = run(runner, "delete", "Ticket", "--repo", str(repo))
    out = flat(result.output)
    assert result.exit_code == 1 and "delete or edit the relationships that use it first" in out
    assert "the new entry is invalid" not in out


def test_list_shows_source(runner, settings, repo):
    register(settings, repo)
    schema_file(repo).write_text(FS_SCHEMA)
    data = json.loads(run(runner, "list", "--repo", str(repo), "--json").output)
    doc = next(n for n in data["node_types"] if n["label"] == "Doc")
    assert doc["source"] == {"provider": "filesystem", "kind": "file"}
    assert next(n for n in data["node_types"] if n["label"] == "Function")["source"] is None
    out = flat(run(runner, "list", "--repo", str(repo)).output)
    assert "Source" in out and "filesystem (file)" in out and "\u2014" in out


def test_edit_changing_key_warns_constraint_keeps_old_key(runner, repo, tmp_path):
    schema_file(repo).write_text(SCHEMA.split("relationships:")[0])
    new = "label: Ticket\nkey: [code]\nmetadata:\n  - name: code\n    type: string\n"
    result = run(runner, "edit", "Ticket", "--from", src(tmp_path, new), "--repo", str(repo))
    assert result.exit_code == 0, result.output
    out = flat(result.output)
    assert "uniqueness constraint on Ticket keeps the old key (id) until this schema is applied" in out
    assert "every repository declaring Ticket uses the new key" in out


def test_add_duplicate_relationship_compares_validated_form(runner, repo, tmp_path):
    schema_file(repo).write_text(SCHEMA)
    result = run(runner, "add", "--from", src(tmp_path, "type: USES\nfrom: [Ticket]\nto: Function\n"), "--repo", str(repo))
    assert result.exit_code == 1 and "already exists" in result.output


def test_symlinked_schema_file_is_refused_untouched(runner, settings, repo, tmp_path):
    real = tmp_path / "real.yaml"
    real.write_text(SCHEMA)
    link = repo / SCHEMA_FILENAME
    link.symlink_to(real)
    result = runner.invoke(app, ["config", "schema", "add", "--from", src(tmp_path, EPIC), "--repo", str(repo)])
    assert result.exit_code == 1 and "is a symlink to" in flat(result.output)
    assert link.is_symlink() and real.read_text() == SCHEMA


def test_reset_refuses_a_symlinked_file(runner, repo, tmp_path):
    real = tmp_path / "real.yaml"
    real.write_text(SCHEMA)
    schema_file(repo).symlink_to(real)
    result = run(runner, "reset", "--yes", "--repo", str(repo))
    assert result.exit_code == 1 and "symlink" in flat(result.output)
    assert schema_file(repo).is_symlink() and real.read_text() == SCHEMA


DOCS_SCHEMA = textwrap.dedent("""\
    version: 1
    node_types:
      - label: Runbook
        key: [path]
        metadata:
          - {name: path}
          - {name: on_call}
        source:
          provider: docs
          paths: ["runbooks/**/*.md", "ops/*.md"]
          where:
            - {field: version, is: 1}
          fields: {on_call: on-call-team}
    relationships:
      - type: RUNBOOK_FOR
        provider: docs
        from: Runbook
        to: Service
        field: service
""")


def test_list_shows_a_docs_source(runner, settings, repo):
    register(settings, repo)
    schema_file(repo).write_text(DOCS_SCHEMA)
    data = json.loads(run(runner, "list", "--repo", str(repo), "--json").output)
    runbook = next(n for n in data["node_types"] if n["label"] == "Runbook")
    assert runbook["source"] == {
        "provider": "docs",
        "paths": ["runbooks/**/*.md", "ops/*.md"],
        "where": [{"field": "version", "is": "1"}],
        "fields": {"on_call": "on-call-team"},
    }
    rel = next(r for r in data["relationships"] if r["type"] == "RUNBOOK_FOR")
    assert rel["provider"] == "docs" and rel["field"] == "service"
    assert all(r["field"] is None for r in data["relationships"] if r["type"] != "RUNBOOK_FOR")
    out = flat(run(runner, "list", "--repo", str(repo)).output)
    assert "docs (runbooks/**/*.md, ops/*.md)" in out
    assert "docs (service)" in out


def test_edit_moving_a_type_to_docs_warns_it_is_rebuilt(runner, settings, repo, tmp_path):
    register(settings, repo)
    schema_file(repo).write_text(FS_SCHEMA)
    docs_doc = FS_DOC.split("source:")[0] + "source:\n  provider: docs\n  paths: ['**/*.md']\n"
    result = run(runner, "edit", "Doc", "--from", src(tmp_path, docs_doc), "--repo", str(repo))
    assert result.exit_code == 0, result.output
    out = flat(result.output)
    assert "whose filesystem source changed: Doc (filesystem -> docs)" in out
    assert "the next rescan rebuilds Doc entries from Markdown front matter" in out


def test_list_escapes_markup_in_docs_globs(runner, settings, repo):
    register(settings, repo)
    schema_file(repo).write_text(DOCS_SCHEMA.replace('"ops/*.md"', '"[red]ops[/red]/*.md"'))
    out = flat(run(runner, "list", "--repo", str(repo)).output)
    assert "[red]ops[/red]/*.md" in out
