"""Tests for the per-repo layout sidecar (devgraph/dashboard/layout_store.py)."""

from pathlib import Path

import pytest

from devgraph.dashboard import layout_store
from devgraph.config.settings import Settings


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    settings = Settings(registry_db_path=tmp_path / "registry.sqlite3")
    monkeypatch.setattr(layout_store, "get_settings", lambda: settings)
    return settings


def test_load_missing_file_returns_empty_dict(fake_settings):
    assert layout_store.load_layout("no_such_repo") == {}


def test_save_then_load_round_trips(fake_settings):
    positions = {"Function\x1frepo1\x1fmain\x1fa.py": [12.5, -3.0]}
    layout_store.save_layout("repo1", positions)
    assert layout_store.load_layout("repo1") == positions


def test_save_writes_expected_shape_on_disk(fake_settings, tmp_path):
    layout_store.save_layout("repo1", {"k": [1, 2]})
    path = tmp_path / "layouts" / "repo1.json"
    assert path.exists()
    import json

    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["version"] == 1
    assert "saved_at" in body
    assert body["positions"] == {"k": [1, 2]}


def test_load_corrupt_file_returns_empty_dict(fake_settings, tmp_path):
    path = tmp_path / "layouts" / "repo1.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert layout_store.load_layout("repo1") == {}


def test_load_empty_file_returns_empty_dict(fake_settings, tmp_path):
    path = tmp_path / "layouts" / "repo1.json"
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    assert layout_store.load_layout("repo1") == {}


def test_save_overwrites_previous_positions(fake_settings):
    layout_store.save_layout("repo1", {"a": [1, 1]})
    layout_store.save_layout("repo1", {"b": [2, 2]})
    assert layout_store.load_layout("repo1") == {"b": [2, 2]}


def test_separate_repos_get_separate_files(fake_settings, tmp_path):
    layout_store.save_layout("repo1", {"a": [1, 1]})
    layout_store.save_layout("repo2", {"b": [2, 2]})
    assert layout_store.load_layout("repo1") == {"a": [1, 1]}
    assert layout_store.load_layout("repo2") == {"b": [2, 2]}
