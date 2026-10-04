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
