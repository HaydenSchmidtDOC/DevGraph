"""Tests for the per-repository project tools trust record and lookup."""

import sqlite3
import subprocess

import pytest

from devgraph.registry.store import RepoRegistry

TOOLS = b"version: 1\ntools: []\n"


@pytest.fixture
def trust(real_project_trust, tmp_path, monkeypatch):
    db = tmp_path / "r.sqlite3"
    monkeypatch.setattr(real_project_trust, "_registry_db_path", lambda: db)
    real_project_trust.db = db
    return real_project_trust


def _register(db, repo):
    repo.mkdir(exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    reg = RepoRegistry(db)
    try:
        return reg.add_repo(repo).repo_id
    finally:
        reg.close()


def _set(db, repo_id, sha):
    reg = RepoRegistry(db)
    try:
        reg.set_project_tools_sha256(repo_id, sha)
    finally:
        reg.close()


def test_registry_records_and_clears_the_approved_hash(tmp_path):
    db = tmp_path / "r.sqlite3"
    repo_id = _register(db, tmp_path / "repo")
    reg = RepoRegistry(db)
    try:
        assert reg.get(repo_id).project_tools_sha256 is None
        reg.set_project_tools_sha256(repo_id, "ab" * 32)
        assert reg.get(repo_id).project_tools_sha256 == "ab" * 32
        reg.set_project_tools_sha256(repo_id, None)
        assert reg.get(repo_id).project_tools_sha256 is None
        with pytest.raises(ValueError):
            reg.set_project_tools_sha256("nope", None)
    finally:
        reg.close()


def test_registry_migrates_an_old_database(tmp_path):
    db = tmp_path / "r.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE repos (repo_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1,"
                 " watch_enabled INTEGER NOT NULL DEFAULT 1, last_indexed TEXT)")
    conn.execute("INSERT INTO repos (repo_id, path) VALUES ('old', '/nowhere')")
    conn.commit()
    conn.close()
    reg = RepoRegistry(db)
    try:
        assert reg.get("old").project_tools_sha256 is None
    finally:
        reg.close()


def test_states(trust, tmp_path):
    repo = tmp_path / "repo"
    assert trust.project_tools_trust(repo, TOOLS) == "untrusted"  # no database at all
    repo_id = _register(trust.db, repo)
    assert trust.project_tools_trust(repo, TOOLS) == "untrusted"  # registered, never approved
    assert trust.project_tools_trust(tmp_path / "other", TOOLS) == "untrusted"  # not registered
    _set(trust.db, repo_id, trust.tools_sha256(TOOLS))
    assert trust.project_tools_trust(repo, TOOLS) == "trusted"
    assert trust.project_tools_trust(repo, TOOLS + b"# edit\n") == "changed"
    _set(trust.db, repo_id, None)
    assert trust.project_tools_trust(repo, TOOLS) == "untrusted"


def test_a_registry_error_fails_closed(trust, tmp_path):
    trust.db.write_bytes(b"this is not a sqlite database at all" * 100)
    assert trust.project_tools_trust(tmp_path / "repo", TOOLS) == "error"


def test_a_registry_without_the_column_fails_closed(trust, tmp_path):
    conn = sqlite3.connect(trust.db)
    conn.execute("CREATE TABLE repos (repo_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE)")
    conn.execute("INSERT INTO repos VALUES ('repo', ?)", (str((tmp_path / 'repo').resolve()),))
    conn.commit()
    conn.close()
    assert trust.project_tools_trust(tmp_path / "repo", TOOLS) == "error"


def test_sha256_is_of_the_exact_bytes(trust):
    assert trust.tools_sha256(b"a") != trust.tools_sha256(b"a\n")
    assert len(trust.tools_sha256(TOOLS)) == 64
