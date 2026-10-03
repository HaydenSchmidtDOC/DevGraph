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
