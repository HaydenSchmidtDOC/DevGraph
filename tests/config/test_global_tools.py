import json
import os

import pytest

from devgraph.config import global_tools
from devgraph.config.global_tools import (
    GLOBAL_TOOLS_FILENAME,
    global_tools_fingerprint,
    global_tools_path,
    load_global_tools,
    save_global_tools,
)
from devgraph.config.project_tools import ProjectToolsError

TOOL = {"name": "count_files", "description": "Count files.", "cypher": "MATCH (f:File {repo_id: $repo_id}) RETURN count(f) AS n"}
BAD = {"name": "writes", "description": "Bad.", "cypher": "CREATE (n {repo_id: $repo_id})"}


def test_default_path_is_next_to_registry(monkeypatch, tmp_path):
    monkeypatch.undo()  # drop the autouse isolation: check the real derivation
    from devgraph.config.settings import get_settings

    assert global_tools._default_path() == get_settings().registry_db_path.parent / GLOBAL_TOOLS_FILENAME


def test_global_tools_path_uses_default(tmp_path):
    assert global_tools_path().name == "global-tools.json"


def test_absent_store(tmp_path):
    path = tmp_path / "x.json"
    assert load_global_tools(path) is None
    assert global_tools_fingerprint(path) == "absent"


def test_save_then_load_round_trips_and_creates_directory(tmp_path):
    path = tmp_path / "sub" / "g.json"
    save_global_tools([TOOL], path)
    loaded = load_global_tools(path)
    assert [t.name for t in loaded.tools] == ["count_files"]
    assert json.loads(path.read_text())["version"] == 1
    assert global_tools_fingerprint(path) == path.read_bytes()


def test_default_path_round_trip():
    save_global_tools([TOOL])
    assert load_global_tools().tools[0].name == "count_files"


def test_invalid_content_raises(tmp_path):
    path = tmp_path / "g.json"
    path.write_text('{"version": 1, "tools": [{"name": "x"}]}')
    with pytest.raises(ProjectToolsError):
        load_global_tools(path)


def test_unreadable_fingerprint(tmp_path):
    path = tmp_path / "dir"
    path.mkdir()
    assert global_tools_fingerprint(path).startswith("unreadable:")


def test_invalid_save_leaves_existing_file(tmp_path):
    path = tmp_path / "g.json"
    save_global_tools([TOOL], path)
    before = path.read_bytes()
    with pytest.raises(ProjectToolsError):
        save_global_tools([BAD], path)
    assert path.read_bytes() == before
    assert os.listdir(tmp_path) == ["g.json"]


def test_save_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "g.json"
    save_global_tools([TOOL], path)
    before = path.read_bytes()

    def boom(*args):
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        save_global_tools([{**TOOL, "name": "other"}], path)
    assert path.read_bytes() == before
    assert os.listdir(tmp_path) == ["g.json"]
