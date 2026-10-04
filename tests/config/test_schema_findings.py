from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from devgraph.config.project_schema import SCHEMA_FILENAME, parse_project_schema
from devgraph.config.schema_findings import (
    introduced_conflicts,
    project_schema_findings,
    schema_conflicts,
)


def _schema(label: str, key: str) -> str:
    return (
        "version: 1\n"
        "node_types:\n"
        f"  - label: {label}\n"
        f"    key: [{key}]\n"
        "    metadata:\n"
        f"      - name: {key}\n"
    )


def _repo(tmp_path: Path, repo_id: str, schema: str | None = None):
    root = tmp_path / repo_id
    root.mkdir()
    if schema is not None:
        (root / SCHEMA_FILENAME).write_text(schema, encoding="utf-8")
    return SimpleNamespace(repo_id=repo_id, path=root)


def _decl(text: str | None):
    return None if text is None else parse_project_schema(text, Path(SCHEMA_FILENAME))


def test_absent_valid_invalid_and_disabled(tmp_path, monkeypatch):
    plain = _repo(tmp_path, "plain")
    widgets = _repo(tmp_path, "widgets", _schema("Widget", "slug"))
    broken = _repo(tmp_path, "broken", "not: [valid")

    findings = project_schema_findings([plain, widgets, broken])
    by_repo = {f["repo_id"]: f for f in findings}
    assert by_repo["plain"]["status"] == "absent"
    assert by_repo["widgets"]["status"] == "valid"
    assert by_repo["broken"]["status"] == "invalid" and by_repo["broken"]["failed"]

    monkeypatch.setattr("devgraph.config.project_switch.project_config_enabled", lambda _path: False)
    disabled = project_schema_findings([plain])
    assert [f["status"] for f in disabled] == ["disabled", "absent"]
    assert "devgraph config enable plain" in disabled[0]["detail"]


def test_differently_keyed_label_is_one_conflict_with_declarations(tmp_path):
    a = _repo(tmp_path, "a", _schema("Widget", "slug"))
    b = _repo(tmp_path, "b", _schema("widget", "code"))

    conflicts = schema_conflicts([a, b])

    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict["status"] == "conflict" and conflict["label"] == "widget"
    assert conflict["repo_ids"] == ["a", "b"]
    assert conflict["declarations"] == [
        {"repo_id": "a", "label": "Widget", "key": ["slug"], "disabled": False},
        {"repo_id": "b", "label": "widget", "key": ["code"], "disabled": False},
    ]


def test_declarations_flag_disabled_repos(tmp_path, monkeypatch):
    a = _repo(tmp_path, "a", _schema("Widget", "slug"))
    b = _repo(tmp_path, "b", _schema("Widget", "code"))
    monkeypatch.setattr(
        "devgraph.config.project_switch.project_config_enabled", lambda path: Path(path).name != "b"
    )
    declarations = schema_conflicts([a, b])[0]["declarations"]
    assert [(d["repo_id"], d["disabled"]) for d in declarations] == [("a", False), ("b", True)]


def test_identical_declarations_are_not_a_conflict(tmp_path):
    a = _repo(tmp_path, "a", _schema("Widget", "slug"))
    b = _repo(tmp_path, "b", _schema("Widget", "slug"))
    assert schema_conflicts([a, b]) == []


def test_case_only_difference_is_a_conflict(tmp_path):
    a = _repo(tmp_path, "a", _schema("Widget", "slug"))
    b = _repo(tmp_path, "b", _schema("widget", "slug"))
    # Same key, different case: constraint names collide but (label, key) differ.
    assert len(schema_conflicts([a, b])) == 1


def test_overrides_replace_a_repos_declaration_without_reading_its_file(tmp_path):
    a = _repo(tmp_path, "a", _schema("Widget", "slug"))
    b = _repo(tmp_path, "b", "not: [valid")  # unreadable: must not be read

    assert schema_conflicts([a, b], overrides={"b": _decl(_schema("Widget", "code"))})[0]["repo_ids"] == ["a", "b"]
    assert schema_conflicts([a, b], overrides={"b": None}) == []
    statuses = {f["repo_id"]: f["status"] for f in project_schema_findings([a, b], overrides={"b": None})}
    assert statuses == {"a": "valid", "b": "absent"}


def test_introduced_conflicts_reports_only_new_ones(tmp_path):
    a = _repo(tmp_path, "a", _schema("Widget", "slug"))
    b = _repo(tmp_path, "b")
    before, after = _decl(None), _decl(_schema("Widget", "code"))

    messages = introduced_conflicts([a, b], "b", before, after)
    assert len(messages) == 1
    assert messages[0].startswith("Creates a schema conflict: ")
    assert "incompatible declarations of label 'widget'" in messages[0]

    # Already conflicting before: not reported again.
    assert introduced_conflicts([a, b], "b", after, after) == []
    # Dropping the label creates nothing.
    assert introduced_conflicts([a, b], "b", after, None) == []
