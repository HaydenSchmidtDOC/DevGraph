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
