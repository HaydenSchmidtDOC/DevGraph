"""Tests for the per-repository project config switch lookup."""

import logging
import sqlite3
import subprocess

from devgraph.config import project_switch
from devgraph.config.project_schema import (
    ABSENT_SCHEMA_HASH,
    SCHEMA_FILENAME,
    load_project_schema,
    schema_file_hash,
)
from devgraph.config.project_switch import project_config_enabled
from devgraph.registry.store import RepoRegistry

VALID_SCHEMA = """\
version: 1
node_types:
  - label: Widget
    key: [slug]
    metadata:
      - name: slug
        type: string
        required: true
"""


def _use_db(monkeypatch, tmp_path):
    db = tmp_path / "r.sqlite3"
    monkeypatch.setattr(project_switch, "_registry_db_path", lambda: db)
    return db


def _register(db, repo, enabled=True):
    repo.mkdir(exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    reg = RepoRegistry(db)
    try:
        rec = reg.add_repo(repo)
        if not enabled:
            reg.set_project_config_enabled(rec.repo_id, False)
    finally:
        reg.close()


def test_unregistered_and_missing_db_are_enabled(tmp_path, monkeypatch):
    db = _use_db(monkeypatch, tmp_path)
    assert project_config_enabled(tmp_path / "repo") is True
    _register(db, tmp_path / "repo")
    assert project_config_enabled(tmp_path / "repo") is True
    assert project_config_enabled(tmp_path / "other") is True


def test_a_disabled_repo_is_disabled_by_any_spelling_of_its_path(tmp_path, monkeypatch):
    db = _use_db(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    _register(db, repo, enabled=False)
    link = tmp_path / "link"
    link.symlink_to(repo)
    assert project_config_enabled(repo) is False
    assert project_config_enabled(str(repo) + "/") is False
    assert project_config_enabled(link) is False


def test_the_lookup_never_creates_a_database(tmp_path, monkeypatch):
    db = _use_db(monkeypatch, tmp_path)
    assert project_config_enabled(tmp_path) is True
    assert not db.exists()


def test_load_project_schema_and_hash_honour_the_switch(tmp_path, monkeypatch):
    db = _use_db(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    _register(db, repo)
    (repo / SCHEMA_FILENAME).write_text(VALID_SCHEMA, encoding="utf-8")
    assert load_project_schema(repo) is not None
    assert schema_file_hash(repo).startswith("sha256:")

    reg = RepoRegistry(db)
    reg.set_project_config_enabled(reg.list_repos()[0].repo_id, False)
    reg.close()

    assert load_project_schema(repo) is None
    assert load_project_schema(repo, respect_switch=False) is not None
    assert schema_file_hash(repo) == ABSENT_SCHEMA_HASH


def test_a_registry_path_with_uri_special_characters_still_applies(tmp_path, monkeypatch):
    odd = tmp_path / "a#b ?c%20"
    odd.mkdir()
    db = odd / "r.sqlite3"
    monkeypatch.setattr(project_switch, "_registry_db_path", lambda: db)
    _register(db, tmp_path / "repo", enabled=False)
    assert project_config_enabled(tmp_path / "repo") is False


def test_the_lookup_gives_up_quickly_on_a_locked_registry(tmp_path, monkeypatch):
    db = _use_db(monkeypatch, tmp_path)
    _register(db, tmp_path / "repo", enabled=False)
    seen = {}
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(project_switch.sqlite3, "connect", spy)
    assert project_config_enabled(tmp_path / "repo") is False
    assert seen["timeout"] == 0.5


def test_an_sqlite_error_falls_back_to_enabled_and_is_logged(tmp_path, monkeypatch, caplog):
    db = _use_db(monkeypatch, tmp_path)
    _register(db, tmp_path / "repo", enabled=False)

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(project_switch.sqlite3, "connect", locked)
    with caplog.at_level(logging.DEBUG, logger=project_switch.logger.name):
        assert project_config_enabled(tmp_path / "repo") is True
    assert "database is locked" in caplog.text
