"""Tests for where Settings reads its .env file from."""

import os

import pytest

from devgraph.config.settings import Settings, devgraph_home, env_files


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    """Point HOME at a tmp dir and drop any exported DEVGRAPH_* variables."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # what Path.home() reads on Windows
    for key in list(os.environ):
        if key.startswith("DEVGRAPH_"):
            monkeypatch.delenv(key)
    return home


def test_env_file_in_working_directory_is_ignored(clean_env, tmp_path, monkeypatch):
    workdir = tmp_path / "project"
    workdir.mkdir()
    (workdir / ".env").write_text("DEVGRAPH_NEO4J_URI=bolt://cwd.example:7687\n", encoding="utf-8")
    monkeypatch.chdir(workdir)

    assert workdir / ".env" not in env_files()
    assert Settings().neo4j_uri != "bolt://cwd.example:7687"


def test_env_file_in_devgraph_home_is_honoured(clean_env):
    home_dir = clean_env / ".devgraph"
    home_dir.mkdir()
    (home_dir / ".env").write_text("DEVGRAPH_NEO4J_URI=bolt://home.example:7687\n", encoding="utf-8")

    assert devgraph_home() == home_dir
    assert Settings().neo4j_uri == "bolt://home.example:7687"


def test_devgraph_home_follows_exported_registry_path(clean_env, tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / ".env").write_text("DEVGRAPH_NEO4J_URI=bolt://state.example:7687\n", encoding="utf-8")
    monkeypatch.setenv("DEVGRAPH_REGISTRY_DB_PATH", str(state_dir / "registry.sqlite3"))

    assert devgraph_home() == state_dir
    assert Settings().neo4j_uri == "bolt://state.example:7687"


def test_exported_environment_variable_wins_over_home_env_file(clean_env, monkeypatch):
    home_dir = clean_env / ".devgraph"
    home_dir.mkdir()
    (home_dir / ".env").write_text("DEVGRAPH_NEO4J_URI=bolt://home.example:7687\n", encoding="utf-8")
    monkeypatch.setenv("DEVGRAPH_NEO4J_URI", "bolt://exported.example:7687")

    assert Settings().neo4j_uri == "bolt://exported.example:7687"


def test_home_env_file_wins_over_checkout_env_file(clean_env, tmp_path, monkeypatch):
    import devgraph.config.settings as settings_module

    checkout = tmp_path / "checkout"
    (checkout / "devgraph" / "config").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("", encoding="utf-8")
    (checkout / ".env").write_text(
        "DEVGRAPH_NEO4J_URI=bolt://checkout.example:7687\nDEVGRAPH_NEO4J_USER=checkout-user\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings_module, "__file__", str(checkout / "devgraph" / "config" / "settings.py"))
    home_dir = clean_env / ".devgraph"
    home_dir.mkdir()
    (home_dir / ".env").write_text("DEVGRAPH_NEO4J_URI=bolt://home.example:7687\n", encoding="utf-8")

    assert env_files() == (checkout / ".env", home_dir / ".env")
    settings = Settings()
    assert settings.neo4j_uri == "bolt://home.example:7687"
    assert settings.neo4j_user == "checkout-user"


def test_relative_registry_path_never_loads_env_from_working_directory(clean_env, tmp_path, monkeypatch, caplog):
    workdir = tmp_path / "project"
    workdir.mkdir()
    (workdir / ".env").write_text("DEVGRAPH_NEO4J_URI=bolt://cwd.example:7687\n", encoding="utf-8")
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("DEVGRAPH_REGISTRY_DB_PATH", "reg.sqlite3")

    with caplog.at_level("WARNING", logger="devgraph.config.settings"):
        assert devgraph_home() == clean_env / ".devgraph"
    assert "relative DEVGRAPH_REGISTRY_DB_PATH" in caplog.text
    assert all(path.parent.resolve() != workdir.resolve() for path in env_files())
    assert Settings().neo4j_uri != "bolt://cwd.example:7687"


def test_tilde_in_registry_path_expands_the_same_for_home_and_setting(clean_env, monkeypatch):
    monkeypatch.setenv("DEVGRAPH_REGISTRY_DB_PATH", "~/state/registry.sqlite3")

    assert devgraph_home() == clean_env / "state"
    assert Settings().registry_db_path == clean_env / "state" / "registry.sqlite3"


def test_relative_registry_path_setting_falls_back_to_the_home_default(clean_env, tmp_path, monkeypatch, caplog):
    workdir = tmp_path / "project"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("DEVGRAPH_REGISTRY_DB_PATH", "reg.sqlite3")

    with caplog.at_level("WARNING", logger="devgraph.config.settings"):
        assert Settings().registry_db_path == clean_env / ".devgraph" / "registry.sqlite3"
    assert "relative DEVGRAPH_REGISTRY_DB_PATH" in caplog.text
