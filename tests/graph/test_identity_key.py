"""Tests for identity_key -- the server-derived, elementId()-independent
layout-cache key. Pure unit tests (no Neo4j) since identity_key and
_group_nodes_by_label are both plain functions over dicts/args."""

from devgraph.graph.engine import _group_nodes_by_label, identity_key


def test_file_scoped_node_includes_file_in_key():
    key = identity_key("Function", "repo1", "main", "src/app.py")
    assert key == "Function\x1frepo1\x1fmain\x1fsrc/app.py"


def test_non_file_scoped_node_omits_file():
    key = identity_key("Service", "repo1", "UserService", None)
    assert key == "Service\x1frepo1\x1fUserService"


def test_key_disambiguates_same_name_different_files():
    key_a = identity_key("Function", "repo1", "main", "a.py")
    key_b = identity_key("Function", "repo1", "main", "b.py")
    assert key_a != key_b


def test_identity_key_is_file_scoped_exactly_when_merge_key_is():
    """The two must agree on every node, or the dashboard's cache key would
    silently point at the wrong node after a re-index."""
    nodes = [
        {"label": "Class", "repo_id": "r", "name": "A", "properties": {"file": "a.py"}},
        {"label": "Function", "repo_id": "r", "name": "f", "properties": {"file": "a.py"}},
        {"label": "Service", "repo_id": "r", "name": "UserService", "properties": {}},
        {"label": "Module", "repo_id": "r", "name": "a.py", "properties": {"source_file": "a.py"}},
    ]
    groups = _group_nodes_by_label(nodes)

    for (label, file_scoped), rows in groups.items():
        for row in rows:
            key = identity_key(label, row["repo_id"], row["name"], row["file"])
            has_file_in_key = key.count("\x1f") == 3
            assert has_file_in_key == file_scoped
