"""`devgraph config eject` writes where the other `config` commands read
(audit 10, item 8d): from a subdirectory of a registered repository, the
repository's root, not the current directory, where it would never be read."""

import pytest
from typer.testing import CliRunner

from devgraph.cli import main as cli_main
from devgraph.cli.main import app
from devgraph.config import project_switch
from devgraph.config.project_schema import SCHEMA_FILENAME
from devgraph.config.settings import Settings
from devgraph.registry.store import RepoRegistry


@pytest.fixture
def settings(monkeypatch, tmp_path):
    fake = Settings(_env_file=None, neo4j_password="x", registry_db_path=tmp_path / "registry.sqlite3")
    monkeypatch.setattr(cli_main, "get_settings", lambda: fake)
    monkeypatch.setattr(project_switch, "_registry_db_path", lambda: fake.registry_db_path)
    return fake


@pytest.fixture
def repo(settings, tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    registry = RepoRegistry(settings.registry_db_path)
    try:
        registry.add_repo(root, repo_id="demo")
    finally:
        registry.close()
    return root


def test_eject_from_a_subdirectory_writes_at_the_repository_root(settings, repo, monkeypatch):
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    result = CliRunner().invoke(app, ["config", "eject"])
    assert result.exit_code == 0, result.output
    assert (repo / SCHEMA_FILENAME).exists()
    assert not (sub / SCHEMA_FILENAME).exists()


def test_eject_outside_any_registered_repository_writes_in_the_current_directory(settings, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    result = CliRunner().invoke(app, ["config", "eject"])
    assert result.exit_code == 0, result.output
    assert (elsewhere / SCHEMA_FILENAME).exists()


def test_eject_with_repo_writes_there(settings, repo, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["config", "eject", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert (repo / SCHEMA_FILENAME).exists()
