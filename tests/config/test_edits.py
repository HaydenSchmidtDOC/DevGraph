"""The validated config write path shared by the CLI and the dashboard."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from devgraph.config import edits, global_tools
from devgraph.config.edits import ConfigEditError

CYPHER = "MATCH (n {repo_id: $repo_id}) RETURN count(n) AS n"
TOOL = {"name": "count_things", "description": "Count things", "cypher": CYPHER}
TOOLS_FILE = (
    "# keep me\nversion: 1\ntools:\n  - name: count_things\n    description: Count things\n"
    f"    cypher: {CYPHER!r}  # inline\n"
)
SCHEMA_FILE = "# schema\nversion: 1\nnode_types:\n  - label: Ticket\n    key: [id]\n    metadata:\n      - name: id\n        type: string\n"
TICKET = {"label": "Ticket", "key": ["id"], "metadata": [{"name": "id", "type": "string"}]}
META = [{"name": "id", "type": "string"}, {"name": "n", "type": "string"}]
STORY = {"label": "Story", "key": ["id"], "metadata": [{"name": "id", "type": "string"}]}


@pytest.fixture
def store(monkeypatch, tmp_path):
    path = tmp_path / "home" / "global-tools.json"
    path.parent.mkdir()
    monkeypatch.setattr(global_tools, "_default_path", lambda: path)
    return path


def record(**kw):
    base = dict(repo_id="repo-a", active=True, project_config_enabled=True, watch_enabled=True)
    return SimpleNamespace(**{**base, **kw})


def code(excinfo) -> str:
    return excinfo.value.code


# --- project tools -------------------------------------------------------------------------------


def test_add_tool_preserves_comments(tmp_path):
    (tmp_path / "devgraph.tools.yaml").write_text(TOOLS_FILE)
    result = edits.add_tool(tmp_path, {**TOOL, "name": "other"})
    text = (tmp_path / "devgraph.tools.yaml").read_text()
    assert text.startswith("# keep me\n") and "# inline" in text and "name: other" in text
    assert result.path == tmp_path / "devgraph.tools.yaml" and result.text == text and result.written


def test_add_tool_creates_file(tmp_path):
    edits.add_tool(tmp_path, TOOL)
    assert "name: count_things" in (tmp_path / "devgraph.tools.yaml").read_text()


def test_add_existing_tool_is_refused(tmp_path):
    (tmp_path / "devgraph.tools.yaml").write_text(TOOLS_FILE)
    with pytest.raises(ConfigEditError) as exc:
        edits.add_tool(tmp_path, TOOL)
    assert code(exc) == "exists"
    assert (tmp_path / "devgraph.tools.yaml").read_text() == TOOLS_FILE


def test_builtin_name_is_refused(tmp_path):
    from devgraph.mcp.catalog import builtin_tool_names

    name = sorted(builtin_tool_names())[0]
    with pytest.raises(ConfigEditError) as exc:
        edits.add_tool(tmp_path, {**TOOL, "name": name})
    assert code(exc) == "locked"
    assert not (tmp_path / "devgraph.tools.yaml").exists()


def test_replace_and_delete_tool(tmp_path):
    (tmp_path / "devgraph.tools.yaml").write_text(TOOLS_FILE)
    edits.replace_tool(tmp_path, "count_things", {**TOOL, "description": "Edited"})
    assert "description: Edited" in (tmp_path / "devgraph.tools.yaml").read_text()
    edits.delete_tool(tmp_path, "count_things")
    assert "name: count_things" not in (tmp_path / "devgraph.tools.yaml").read_text()


def test_rename_to_a_taken_name_is_refused(tmp_path):
    (tmp_path / "devgraph.tools.yaml").write_text(TOOLS_FILE)
    edits.add_tool(tmp_path, {**TOOL, "name": "other"})
    before = (tmp_path / "devgraph.tools.yaml").read_bytes()
    with pytest.raises(ConfigEditError) as exc:
        edits.replace_tool(tmp_path, "other", TOOL)
    assert code(exc) == "exists"
    assert (tmp_path / "devgraph.tools.yaml").read_bytes() == before


def test_replace_and_delete_unknown_tool(tmp_path):
    (tmp_path / "devgraph.tools.yaml").write_text(TOOLS_FILE)
    for call in (lambda: edits.replace_tool(tmp_path, "nope", TOOL), lambda: edits.delete_tool(tmp_path, "nope")):
        with pytest.raises(ConfigEditError) as exc:
            call()
        assert code(exc) == "not_found"


def test_invalid_result_writes_nothing(tmp_path):
    (tmp_path / "devgraph.tools.yaml").write_text(TOOLS_FILE)
    before = (tmp_path / "devgraph.tools.yaml").read_bytes()
    with pytest.raises(ConfigEditError) as exc:
        edits.add_tool(tmp_path, {"name": "bad"})
    assert code(exc) == "invalid"
    assert (tmp_path / "devgraph.tools.yaml").read_bytes() == before
    assert not list(tmp_path.glob(".*.tmp"))


def test_stale_fingerprint_is_refused(tmp_path):
    path = tmp_path / "devgraph.tools.yaml"
    path.write_text(TOOLS_FILE)
    stale = edits.file_fingerprint(path)
    path.write_text(TOOLS_FILE + "\n# changed elsewhere\n")
    with pytest.raises(ConfigEditError) as exc:
        edits.add_tool(tmp_path, {**TOOL, "name": "other"}, expected_fingerprint=stale)
    assert code(exc) == "stale"
    edits.add_tool(tmp_path, {**TOOL, "name": "other"}, expected_fingerprint=edits.file_fingerprint(path))


def test_fingerprint_scheme(tmp_path):
    path = tmp_path / "f"
    assert edits.file_fingerprint(path) == "absent"
    path.write_text("x")
    assert edits.file_fingerprint(path).startswith("sha256:")


def test_dry_run_writes_nothing(tmp_path):
    result = edits.add_tool(tmp_path, TOOL, dry_run=True)
    assert not result.written and "name: count_things" in result.text
    assert not (tmp_path / "devgraph.tools.yaml").exists()


def test_symlinked_target_is_refused(tmp_path):
    real = tmp_path / "real.yaml"
    real.write_text(TOOLS_FILE)
    (tmp_path / "devgraph.tools.yaml").symlink_to(real)
    with pytest.raises(ConfigEditError) as exc:
        edits.add_tool(tmp_path, {**TOOL, "name": "other"})
    assert code(exc) == "not_regular"
    assert real.read_text() == TOOLS_FILE


def test_write_atomically_keeps_mode(tmp_path):
    path = tmp_path / "f.yaml"
    path.write_text("a")
    os.chmod(path, 0o640)
    edits.write_atomically(path, "b")
    assert path.read_text() == "b" and path.stat().st_mode & 0o777 == 0o640


# --- global store --------------------------------------------------------------------------------


def test_global_store_add_replace_delete(store):
    edits.add_tool(None, TOOL)
    assert [t["name"] for t in json.loads(store.read_text())["tools"]] == ["count_things"]
    edits.replace_tool(None, "count_things", {**TOOL, "description": "Edited"})
    assert json.loads(store.read_text())["tools"][0]["description"] == "Edited"
    edits.delete_tool(None, "count_things")
    assert json.loads(store.read_text())["tools"] == []


def test_global_store_invalid_and_dry_run(store):
    with pytest.raises(ConfigEditError) as exc:
        edits.add_tool(None, {"name": "bad"})
    assert code(exc) == "invalid" and not store.exists()
    result = edits.add_tool(None, TOOL, dry_run=True)
    assert not result.written and not store.exists()


def test_tools_effect_notes(tmp_path):
    assert "within 2 seconds" in edits.tools_effect_note(tmp_path, record())
    assert "not a registered repository" in edits.tools_effect_note(tmp_path, None)
    assert "not a registered repository" in edits.tools_effect_note(tmp_path, record(active=False))
    assert "devgraph config enable repo-a" in edits.tools_effect_note(tmp_path, record(project_config_enabled=False))
    assert "registered repository" in edits.tools_effect_note(None, None)


# --- schema --------------------------------------------------------------------------------------


def test_schema_add_replace_delete(tmp_path):
    path = tmp_path / "devgraph.schema.yaml"
    path.write_text(SCHEMA_FILE)
    result = edits.add_schema_entry(tmp_path, STORY)
    assert "label: Story" in path.read_text() and path.read_text().startswith("# schema\n")
    assert result.warnings == [] and "no provider produces Story" in result.notes[0]
    edits.replace_schema_entry(tmp_path, "Story", {**STORY, "key": ["id", "slug"], "metadata": [*STORY["metadata"], {"name": "slug", "type": "string"}]})
    assert "slug" in path.read_text()
    edits.delete_schema_entry(tmp_path, "Story")
    assert "Story" not in path.read_text()


def test_schema_add_duplicate_and_wrong_shape(tmp_path):
    (tmp_path / "devgraph.schema.yaml").write_text(SCHEMA_FILE)
    with pytest.raises(ConfigEditError) as exc:
        edits.add_schema_entry(tmp_path, TICKET)
    assert code(exc) == "exists"
    with pytest.raises(ConfigEditError) as exc:
        edits.add_schema_entry(tmp_path, {"label": "X", "type": "Y"})
    assert code(exc) == "invalid"


def test_schema_exists_carries_the_name_for_node_types_only(tmp_path):
    (tmp_path / "devgraph.schema.yaml").write_text(SCHEMA_FILE)
    with pytest.raises(ConfigEditError) as exc:
        edits.add_schema_entry(tmp_path, TICKET)
    assert exc.value.name == "Ticket"
    rel = {"type": "FEEDS", "provider": "custom", "custom": {"name": "f"}, "from": "Ticket", "to": "Ticket"}
    edits.add_schema_entry(tmp_path, rel)
    with pytest.raises(ConfigEditError) as exc:
        edits.add_schema_entry(tmp_path, rel)
    assert code(exc) == "exists" and exc.value.name is None


def test_schema_invalid_result_writes_nothing(tmp_path):
    path = tmp_path / "devgraph.schema.yaml"
    path.write_text(SCHEMA_FILE)
    with pytest.raises(ConfigEditError) as exc:
        edits.add_schema_entry(tmp_path, {"label": "Bad"})
    assert code(exc) == "invalid" and exc.value.args[0].startswith("the new entry is invalid: ")
    assert path.read_text() == SCHEMA_FILE


def test_schema_delete_unknown_and_stale(tmp_path):
    path = tmp_path / "devgraph.schema.yaml"
    path.write_text(SCHEMA_FILE)
    with pytest.raises(ConfigEditError) as exc:
        edits.delete_schema_entry(tmp_path, "Nope")
    assert code(exc) == "not_found"
    with pytest.raises(ConfigEditError) as exc:
        edits.delete_schema_entry(tmp_path, "Ticket", expected_fingerprint="sha256:00")
    assert code(exc) == "stale"


def test_schema_delete_warns_about_removed_type(tmp_path):
    (tmp_path / "devgraph.schema.yaml").write_text(SCHEMA_FILE)
    result = edits.delete_schema_entry(tmp_path, "Ticket", record=record(), dry_run=True)
    assert not result.written
    assert result.warnings == ["the next rescan deletes the nodes of the removed node type(s): Ticket."]
    assert (tmp_path / "devgraph.schema.yaml").read_text() == SCHEMA_FILE


def test_schema_warning_wording_without_record(tmp_path):
    (tmp_path / "devgraph.schema.yaml").write_text(SCHEMA_FILE)
    result = edits.delete_schema_entry(tmp_path, "Ticket")
    assert result.warnings[0].startswith("applying this schema deletes")


def test_schema_key_change_warning_and_unpopulated_note(tmp_path):
    (tmp_path / "devgraph.schema.yaml").write_text(SCHEMA_FILE)
    result = edits.replace_schema_entry(tmp_path, "Ticket", {"label": "Ticket", "key": ["id", "n"], "metadata": META})
    assert "uniqueness constraint on Ticket keeps the old key (id)" in result.warnings[0]
    assert result.notes and "no provider produces Ticket nodes yet" in result.notes[0]


def test_schema_effect_notes(tmp_path):
    assert "minutes after the last edit" in edits.schema_effect_note(tmp_path, record())
    assert "Not watched" in edits.schema_effect_note(tmp_path, record(watch_enabled=False))
    assert "Not applied while the project config is disabled" in edits.schema_effect_note(
        tmp_path, record(project_config_enabled=False)
    )
    assert "does not index it" in edits.schema_effect_note(tmp_path, None)


def test_duplicate_relationship_normalises_from():
    assert edits.duplicate_relationship(
        {"type": "USES", "from": "Ticket", "to": "Function"}, [{"type": "USES", "from": ["Ticket"], "to": "Function"}]
    )


# --- fingerprint first, fingerprints returned, splice error codes ----------------------------------


def test_symlink_message_names_the_target(tmp_path):
    real = tmp_path / "real.yaml"
    real.write_text(TOOLS_FILE)
    (tmp_path / "devgraph.tools.yaml").symlink_to(real)
    with pytest.raises(ConfigEditError) as exc:
        edits.delete_tool(tmp_path, "count_things")
    assert exc.value.message == f"devgraph.tools.yaml is a symlink to {real.resolve()}; edit {real.resolve()} directly"


def test_stale_wins_over_missing_name_and_duplicates(tmp_path):
    (tmp_path / "devgraph.tools.yaml").write_text(TOOLS_FILE)
    (tmp_path / "devgraph.schema.yaml").write_text(SCHEMA_FILE)
    calls = [
        lambda: edits.replace_tool(tmp_path, "nope", TOOL, expected_fingerprint="sha256:00"),
        lambda: edits.delete_tool(tmp_path, "nope", expected_fingerprint="sha256:00"),
        lambda: edits.add_tool(tmp_path, TOOL, expected_fingerprint="sha256:00"),
        lambda: edits.add_schema_entry(tmp_path, TICKET, expected_fingerprint="sha256:00"),
        lambda: edits.replace_schema_entry(tmp_path, "Nope", TICKET, expected_fingerprint="sha256:00"),
        lambda: edits.delete_schema_entry(tmp_path, "Nope", expected_fingerprint="sha256:00"),
    ]
    for call in calls:
        with pytest.raises(ConfigEditError) as exc:
            call()
        assert code(exc) == "stale"


def test_result_fingerprint(tmp_path):
    path = tmp_path / "devgraph.tools.yaml"
    written = edits.add_tool(tmp_path, TOOL)
    assert written.fingerprint == edits.file_fingerprint(path)
    dry = edits.add_tool(tmp_path, {**TOOL, "name": "other"}, dry_run=True)
    assert dry.fingerprint.startswith("sha256:") and dry.fingerprint != written.fingerprint
    after = edits.add_tool(tmp_path, {**TOOL, "name": "other"})
    assert after.fingerprint == dry.fingerprint  # a dry run predicts the written file


def test_splice_error_codes_come_from_the_splicer(tmp_path):
    from devgraph.config import list_edit

    path = tmp_path / "devgraph.tools.yaml"
    path.write_text("tools: [{name: a}]\n")
    with pytest.raises(ConfigEditError) as exc:
        edits.add_tool(tmp_path, TOOL)
    assert code(exc) == "flow_list"
    path.write_text("tools: [\n")
    with pytest.raises(ConfigEditError) as exc:
        edits.add_tool(tmp_path, TOOL)
    assert code(exc) == "malformed"
    path.write_text("- not a mapping\n")
    with pytest.raises(ConfigEditError) as exc:
        edits.add_tool(tmp_path, TOOL)
    assert code(exc) == "invalid"
    path.write_text("version: 1\ntools:\n  - name: a\n  - name: a\n")
    with pytest.raises(ConfigEditError) as exc:
        edits.delete_tool(tmp_path, "a")
    assert code(exc) == "ambiguous"
    with pytest.raises(list_edit.ListEditError) as exc2:
        list_edit.add_entry_text("tools:\n  - name: a\n", {"name": "a"}, key="tools", ident="name", version=1)
    assert exc2.value.code == "exists"
    with pytest.raises(list_edit.ListEditError) as exc2:
        list_edit.delete_entry_text("tools:\n  - name: a\n", "b", key="tools", ident="name")
    assert exc2.value.code == "not_found"
    with pytest.raises(list_edit.ListEditError) as exc2:
        list_edit.replace_entry_text("tools:\n  - name: b\n    v: &x 1\n  - name: c\n    w: *x\n", "b", {"name": "b"}, key="tools", ident="name")
    assert exc2.value.code == "anchor"


def test_node_type_rename_to_a_taken_label_is_refused(tmp_path):
    (tmp_path / "devgraph.schema.yaml").write_text(SCHEMA_FILE)
    edits.add_schema_entry(tmp_path, STORY)
    before = (tmp_path / "devgraph.schema.yaml").read_bytes()
    with pytest.raises(ConfigEditError) as exc:
        edits.replace_schema_entry(tmp_path, "Story", {**STORY, "label": "Ticket"})
    assert code(exc) == "exists"
    assert (tmp_path / "devgraph.schema.yaml").read_bytes() == before


@pytest.mark.parametrize("kind", ["dir", "fifo"])
def test_non_regular_target_is_refused_without_opening_it(tmp_path, kind):
    target = tmp_path / "devgraph.tools.yaml"
    if kind == "dir":
        target.mkdir()
    else:
        os.mkfifo(target)  # opening a FIFO for reading would block forever
    fingerprint = edits.file_fingerprint(target)
    assert fingerprint == "not_regular"
    with pytest.raises(ConfigEditError) as exc:
        edits.read_text(target)
    assert code(exc) == "not_regular"
    with pytest.raises(ConfigEditError) as exc:
        edits.add_tool(tmp_path, TOOL, expected_fingerprint=fingerprint)
    assert code(exc) == "not_regular"


@pytest.mark.parametrize("dry_run", [False, True])
def test_global_value_json_cannot_store_is_invalid(store, dry_run):
    with pytest.raises(ConfigEditError) as exc:
        edits.add_tool(None, {**TOOL, "description": b"hi"}, dry_run=dry_run)
    assert code(exc) == "invalid" and "JSON cannot store" in exc.value.message
    assert not store.exists()


def test_exists_says_edit_it_instead(tmp_path):
    (tmp_path / "devgraph.tools.yaml").write_text(TOOLS_FILE)
    with pytest.raises(ConfigEditError) as exc:
        edits.add_tool(tmp_path, TOOL)
    assert exc.value.message.endswith("already exists in this scope — edit it instead")


# --- whole-file reset ----------------------------------------------------------------------------


def test_reset_tools_project_dry_run_then_write(tmp_path):
    path = tmp_path / "devgraph.tools.yaml"
    path.write_text(TOOLS_FILE)
    fp = edits.file_fingerprint(path)
    dry = edits.reset_tools(tmp_path, expected_fingerprint=fp, dry_run=True)
    assert not dry.written and dry.removed == {"tools": ["count_things"]} and dry.fingerprint == fp
    assert path.read_text() == TOOLS_FILE
    done = edits.reset_tools(tmp_path, expected_fingerprint=fp)
    assert done.written and done.fingerprint == "absent" and not path.exists()


def test_reset_tools_project_notes_global_takeover(tmp_path, store):
    (tmp_path / "devgraph.tools.yaml").write_text(TOOLS_FILE)
    edits.add_tool(None, TOOL)
    result = edits.reset_tools(tmp_path, record=record(), dry_run=True)
    assert result.notes == ["After the reset, global tool count_things is served in repo-a."]


def test_reset_tools_takeover_note_only_when_the_global_tool_is_served(tmp_path, store, monkeypatch):
    (tmp_path / "devgraph.tools.yaml").write_text(TOOLS_FILE)
    edits.add_tool(None, TOOL)
    off = edits.reset_tools(tmp_path, record=record(project_config_enabled=False), dry_run=True)
    assert off.notes == []
    from devgraph.mcp import catalog

    monkeypatch.setattr(catalog, "builtin_tool_names", lambda: frozenset({"count_things"}))
    assert edits.reset_tools(tmp_path, record=record(), dry_run=True).notes == []


def test_reset_tools_global_empties_the_store(store):
    edits.add_tool(None, TOOL)
    dry = edits.reset_tools(None, dry_run=True)
    assert not dry.written and dry.removed == {"tools": ["count_things"]}
    assert "Removes 1 global tool(s)" in dry.notes[0]
    assert json.loads(store.read_text())["tools"] == [TOOL]
    done = edits.reset_tools(None, expected_fingerprint=dry.fingerprint)
    assert done.written and json.loads(store.read_text())["tools"] == []
    assert done.fingerprint == edits.file_fingerprint(store)


def test_reset_tools_stale_and_symlink_and_absent(tmp_path):
    path = tmp_path / "devgraph.tools.yaml"
    assert edits.reset_tools(tmp_path).written is False
    assert edits.reset_tools(tmp_path).notes == [f"Nothing to reset: {path} does not exist."]
    path.write_text(TOOLS_FILE)
    with pytest.raises(ConfigEditError) as exc:
        edits.reset_tools(tmp_path, expected_fingerprint="sha256:nope")
    assert code(exc) == "stale" and path.exists()
    path.unlink()
    target = tmp_path / "real.yaml"
    target.write_text(TOOLS_FILE)
    path.symlink_to(target)
    with pytest.raises(ConfigEditError) as exc:
        edits.reset_tools(tmp_path)
    assert code(exc) == "not_regular" and path.is_symlink() and target.exists()


def test_reset_tools_unreadable_file_still_resets(tmp_path):
    path = tmp_path / "devgraph.tools.yaml"
    path.write_text("tools: [unclosed\n")
    dry = edits.reset_tools(tmp_path, dry_run=True)
    assert dry.removed == {"tools": None}
    assert edits.reset_tools(tmp_path).written and not path.exists()


def test_reset_schema_dry_run_write_and_warnings_match_the_cli(tmp_path):
    path = tmp_path / "devgraph.schema.yaml"
    path.write_text(SCHEMA_FILE)
    dry = edits.reset_schema(tmp_path, record=record(), dry_run=True)
    assert not dry.written and dry.removed == {"node_types": ["Ticket"], "relationships": []}
    assert dry.warnings == edits.schema_change_warnings(edits.schema_declaration(SCHEMA_FILE, path), None, record())
    assert dry.warnings == ["the next rescan deletes the nodes of the removed node type(s): Ticket."]
    assert path.read_text() == SCHEMA_FILE
    done = edits.reset_schema(tmp_path, record=record(), expected_fingerprint=dry.fingerprint)
    assert done.written and done.fingerprint == "absent" and not path.exists()


def test_reset_schema_invalid_file_gets_the_generic_warning(tmp_path):
    path = tmp_path / "devgraph.schema.yaml"
    path.write_text("version: 1\nnode_types:\n  - label: Bad\n")
    result = edits.reset_schema(tmp_path, dry_run=True)
    assert [w[:31] for w in result.warnings] == ["The file is invalid, so what it"]
    path.write_text("node_types: [unclosed\n")
    assert edits.reset_schema(tmp_path, dry_run=True).removed == {"node_types": None, "relationships": None}
    assert edits.reset_schema(tmp_path).written and not path.exists()


def test_reset_schema_stale_symlink_absent(tmp_path):
    path = tmp_path / "devgraph.schema.yaml"
    assert edits.reset_schema(tmp_path).notes == [f"Nothing to reset: {path} does not exist."]
    path.write_text(SCHEMA_FILE)
    with pytest.raises(ConfigEditError) as exc:
        edits.reset_schema(tmp_path, expected_fingerprint="sha256:nope")
    assert code(exc) == "stale" and path.exists()
    path.unlink()
    target = tmp_path / "real.yaml"
    target.write_text(SCHEMA_FILE)
    path.symlink_to(target)
    with pytest.raises(ConfigEditError) as exc:
        edits.reset_schema(tmp_path)
    assert code(exc) == "not_regular" and path.is_symlink()


# --- project config switch -------------------------------------------------------------------------


@pytest.fixture
def no_registry(monkeypatch, tmp_path):
    from devgraph.config import project_switch

    monkeypatch.setattr(project_switch, "_registry_db_path", lambda: tmp_path / "absent.sqlite3")


def test_project_config_notes_match_the_cli_wording():
    assert edits.project_config_notes("demo") == [
        "schema: applied at the next rescan (`devgraph rescan demo --now` to apply now)",
        "project tools: picked up by running MCP sessions within 2 s",
    ]


def test_disabling_warns_about_project_node_types_and_unserved_tools(tmp_path, no_registry, store):
    (tmp_path / "devgraph.schema.yaml").write_text(SCHEMA_FILE)
    (tmp_path / "devgraph.tools.yaml").write_text(TOOLS_FILE)

    warnings, notes = edits.project_config_change(record(path=tmp_path), False)

    assert any("Ticket" in w and "next rescan" in w for w in warnings)
    assert "Project tools no longer served in repo-a: count_things" in warnings
    assert notes == edits.project_config_notes("repo-a")


def test_disabling_reads_the_tools_file_whatever_the_switch_says_and_names_global_takeovers(tmp_path, no_registry, store):
    (tmp_path / "devgraph.tools.yaml").write_text(TOOLS_FILE)
    edits.add_tool(None, TOOL)

    warnings, _ = edits.project_config_change(record(path=tmp_path, project_config_enabled=False), False)

    assert "Project tools no longer served in repo-a: count_things" in warnings
    assert "Global tools of the same name take over in repo-a: count_things" in warnings


def test_disabling_with_an_invalid_tools_file_says_the_last_good_file_may_still_be_served(tmp_path, no_registry, store):
    (tmp_path / "devgraph.tools.yaml").write_text("tools: [unclosed")

    warnings, _ = edits.project_config_change(record(path=tmp_path), False)

    assert any("invalid" in w and "may still be served from the last good file until the session restarts" in w for w in warnings)
    assert not any("no longer served" in w for w in warnings)


def test_enabling_and_bare_repos_have_no_warnings(tmp_path, no_registry, store):
    assert edits.project_config_change(record(path=tmp_path), False)[0] == []
    (tmp_path / "devgraph.schema.yaml").write_text(SCHEMA_FILE)
    assert edits.project_config_change(record(path=tmp_path, project_config_enabled=False), True)[0] == []
