"""`devgraph config tools` (list/add/edit/delete/reset) and global-tool reporting."""

import json
import textwrap

import pytest
import click
from typer.testing import CliRunner

from devgraph.cli import main as cli_main
from devgraph.cli.main import app
from devgraph.config import global_tools
from devgraph.config.project_tools import TOOLS_FILENAME
from devgraph.config.settings import Settings


@pytest.fixture
def runner():
    return CliRunner(env={"COLUMNS": "200"})


@pytest.fixture
def settings(monkeypatch, tmp_path):
    fake = Settings(_env_file=None, neo4j_password="x", registry_db_path=tmp_path / "registry.sqlite3")
    monkeypatch.setattr(cli_main, "get_settings", lambda: fake)
    return fake


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return root


@pytest.fixture
def store(monkeypatch, tmp_path):
    path = tmp_path / "global-store" / global_tools.GLOBAL_TOOLS_FILENAME
    monkeypatch.setattr(global_tools, "_default_path", lambda: path)
    return path


def tool_yaml(name="count_nodes", extra=""):
    return textwrap.dedent(f"""\
        name: {name}
        description: Count this repository's nodes.
        cypher: |
          MATCH (n {{repo_id: $repo_id}}) RETURN count(n) AS n
        {extra}""")


def src(tmp_path, text, name="tool.yaml"):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def names(path):
    from devgraph.config.tools_edit import tool_mappings

    return [m["name"] for m in tool_mappings(path.read_text())]


def add(runner, tmp_path, scope, text=None, name="count_nodes"):
    return runner.invoke(app, ["config", "tools", "add", "--from", src(tmp_path, text or tool_yaml(name), name + ".yaml"), *scope])


# -- add ---------------------------------------------------------------------


def test_add_appends_to_the_project_file_keeping_comments(runner, repo, tmp_path):
    (repo / TOOLS_FILENAME).write_text(
        "# my tools\nversion: 1\ntools:\n  # first\n  - name: a_tool\n    description: A.\n    cypher: |\n      MATCH (n {repo_id: $repo_id}) RETURN n\n"
    )
    result = add(runner, tmp_path, ["--repo", str(repo)])
    assert result.exit_code == 0, result.output
    text = (repo / TOOLS_FILENAME).read_text()
    assert "# my tools" in text and "# first" in text
    assert names(repo / TOOLS_FILENAME) == ["a_tool", "count_nodes"]
    assert TOOLS_FILENAME in result.output and "2 seconds" in result.output


def test_add_creates_the_project_file(runner, repo, tmp_path):
    assert add(runner, tmp_path, ["--repo", str(repo)]).exit_code == 0
    assert names(repo / TOOLS_FILENAME) == ["count_nodes"]


def test_add_global_writes_the_store(runner, repo, tmp_path, store):
    result = add(runner, tmp_path, ["--global"])
    assert result.exit_code == 0, result.output
    assert json.loads(store.read_text())["tools"][0]["name"] == "count_nodes"
    assert not (repo / TOOLS_FILENAME).exists()


def test_add_duplicate_points_at_edit(runner, repo, tmp_path):
    add(runner, tmp_path, ["--repo", str(repo)])
    before = (repo / TOOLS_FILENAME).read_text()
    result = add(runner, tmp_path, ["--repo", str(repo)])
    assert result.exit_code == 1 and "devgraph config tools edit" in result.output
    assert (repo / TOOLS_FILENAME).read_text() == before


@pytest.mark.parametrize("scope", ["project", "global"])
def test_add_refuses_builtin_names(runner, repo, tmp_path, store, scope):
    flags = ["--global"] if scope == "global" else ["--repo", str(repo)]
    result = add(runner, tmp_path, flags, name="search_component")
    assert result.exit_code == 1 and "built-in" in result.output
    assert not store.exists() and not (repo / TOOLS_FILENAME).exists()


def test_add_invalid_tool_changes_nothing(runner, repo, tmp_path, store):
    bad = tool_yaml().replace("$repo_id", "$other")
    for flags, target in ((["--repo", str(repo)], repo / TOOLS_FILENAME), (["--global"], store)):
        result = add(runner, tmp_path, flags, text=bad)
        assert result.exit_code == 1 and "repo_id" in result.output
        assert not target.exists()


def test_add_from_stdin(runner, repo):
    result = runner.invoke(app, ["config", "tools", "add", "--from", "-", "--repo", str(repo)], input=tool_yaml())
    assert result.exit_code == 0, result.output
    assert names(repo / TOOLS_FILENAME) == ["count_nodes"]


def test_global_and_repo_together_is_a_usage_error(runner, repo, tmp_path):
    result = runner.invoke(app, ["config", "tools", "list", "--global", "--repo", str(repo)])
    assert result.exit_code == 2 and "--global or --repo" in result.output


# -- edit --------------------------------------------------------------------


def test_edit_from_file_replaces(runner, repo, tmp_path):
    add(runner, tmp_path, ["--repo", str(repo)])
    new = src(tmp_path, tool_yaml().replace("Count this", "Total"), "new.yaml")
    result = runner.invoke(app, ["config", "tools", "edit", "count_nodes", "--from", new, "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "Total" in (repo / TOOLS_FILENAME).read_text()


def test_edit_global_from_file(runner, tmp_path, store):
    add(runner, tmp_path, ["--global"])
    new = src(tmp_path, tool_yaml().replace("Count this", "Total"), "new.yaml")
    assert runner.invoke(app, ["config", "tools", "edit", "count_nodes", "--from", new, "--global"]).exit_code == 0
    assert "Total" in store.read_text()


def test_edit_in_editor_replaces(runner, repo, tmp_path, monkeypatch):
    add(runner, tmp_path, ["--repo", str(repo)])
    monkeypatch.setattr(click, "edit", lambda text, **kw: text.replace("Count this", "Edited"))
    result = runner.invoke(app, ["config", "tools", "edit", "count_nodes", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "Edited" in (repo / TOOLS_FILENAME).read_text()


@pytest.mark.parametrize("returned", ["same", None])
def test_edit_unchanged_writes_nothing(runner, repo, tmp_path, monkeypatch, returned):
    add(runner, tmp_path, ["--repo", str(repo)])
    before = (repo / TOOLS_FILENAME).read_text()
    monkeypatch.setattr(click, "edit", lambda text, **kw: text if returned == "same" else None)
    result = runner.invoke(app, ["config", "tools", "edit", "count_nodes", "--repo", str(repo)])
    assert result.exit_code == 0 and "No changes" in result.output
    assert (repo / TOOLS_FILENAME).read_text() == before


def test_edit_invalid_editor_result_writes_nothing(runner, repo, tmp_path, monkeypatch):
    add(runner, tmp_path, ["--repo", str(repo)])
    before = (repo / TOOLS_FILENAME).read_text()
    monkeypatch.setattr(click, "edit", lambda text, **kw: text.replace("$repo_id", "$nope"))
    result = runner.invoke(app, ["config", "tools", "edit", "count_nodes", "--repo", str(repo)])
    assert result.exit_code == 1
    assert (repo / TOOLS_FILENAME).read_text() == before


def test_edit_unknown_tool_fails(runner, repo):
    result = runner.invoke(app, ["config", "tools", "edit", "ghost", "--from", "-", "--repo", str(repo)], input=tool_yaml("ghost"))
    assert result.exit_code == 1 and "ghost" in result.output


# -- delete / reset ----------------------------------------------------------


@pytest.mark.parametrize("scope", ["project", "global"])
def test_delete_removes_one_tool(runner, repo, tmp_path, store, scope):
    flags = ["--global"] if scope == "global" else ["--repo", str(repo)]
    target = store if scope == "global" else repo / TOOLS_FILENAME
    add(runner, tmp_path, flags, name="one_tool")
    add(runner, tmp_path, flags, name="two_tool")
    result = runner.invoke(app, ["config", "tools", "delete", "one_tool", *flags])
    assert result.exit_code == 0, result.output
    assert names(target) == ["two_tool"]
    missing = runner.invoke(app, ["config", "tools", "delete", "one_tool", *flags])
    assert missing.exit_code == 1


def test_reset_project_deletes_the_file(runner, repo, tmp_path):
    add(runner, tmp_path, ["--repo", str(repo)])
    result = runner.invoke(app, ["config", "tools", "reset", "--repo", str(repo), "--yes"])
    assert result.exit_code == 0, result.output
    assert not (repo / TOOLS_FILENAME).exists()


def test_reset_global_empties_the_store(runner, tmp_path, store):
    add(runner, tmp_path, ["--global"])
    assert runner.invoke(app, ["config", "tools", "reset", "--global", "--yes"]).exit_code == 0
    assert json.loads(store.read_text())["tools"] == []


def test_reset_without_yes_asks_and_n_aborts(runner, repo, tmp_path, store):
    add(runner, tmp_path, ["--repo", str(repo)])
    add(runner, tmp_path, ["--global"])
    for flags in (["--repo", str(repo)], ["--global"]):
        result = runner.invoke(app, ["config", "tools", "reset", *flags], input="n\n")
        assert result.exit_code == 1 and "Aborted" in result.output
    assert (repo / TOOLS_FILENAME).exists() and names(store) == ["count_nodes"]


# -- list --------------------------------------------------------------------


def test_list_shows_builtin_global_and_project_with_overrides(runner, repo, tmp_path, store):
    add(runner, tmp_path, ["--global"], name="only_global")
    add(runner, tmp_path, ["--global"], name="shared_tool")
    add(runner, tmp_path, ["--repo", str(repo)], name="shared_tool")
    add(runner, tmp_path, ["--repo", str(repo)], name="only_project")
    result = runner.invoke(app, ["config", "tools", "list", "--repo", str(repo), "--json"])
    assert result.exit_code == 0, result.output
    rows = {t["name"]: t for t in json.loads(result.output)["tools"]}
    assert rows["search_component"] == {"name": "search_component", "origin": "built-in", "locked": True}
    assert rows["only_global"]["origin"] == "global" and not rows["only_global"]["locked"]
    assert rows["shared_tool"]["origin"] == "project (overrides global)"
    assert rows["only_project"]["origin"] == "project"
    text = runner.invoke(app, ["config", "tools", "list", "--repo", str(repo)]).output
    assert "locked" in text and "overrides global" in text


def test_list_global_shows_only_the_store(runner, repo, tmp_path, store):
    add(runner, tmp_path, ["--global"], name="only_global")
    add(runner, tmp_path, ["--repo", str(repo)], name="only_project")
    result = runner.invoke(app, ["config", "tools", "list", "--global", "--json"])
    assert [t["name"] for t in json.loads(result.output)["tools"]] == ["only_global"]


# -- show / validate / doctor ------------------------------------------------


def test_show_lists_global_tools_and_overrides(runner, settings, repo, tmp_path, store):
    add(runner, tmp_path, ["--global"], name="shared_tool")
    add(runner, tmp_path, ["--repo", str(repo)], name="shared_tool")
    data = json.loads(runner.invoke(app, ["config", "show", "--repo", str(repo), "--json"]).output)
    assert data["global_tools"]["tools"][0]["name"] == "shared_tool"
    assert data["global_tools"]["tools"][0]["overridden"] is True
    assert "Global tools" in runner.invoke(app, ["config", "show", "--repo", str(repo)]).output


def test_validate_reports_invalid_global_store_and_overrides(runner, settings, repo, tmp_path, store):
    add(runner, tmp_path, ["--global"], name="shared_tool")
    add(runner, tmp_path, ["--repo", str(repo)], name="shared_tool")
    result = runner.invoke(app, ["config", "validate", "--repo", str(repo)])
    assert result.exit_code == 0 and "overrides the global tool" in result.output
    store.write_text("{not valid")
    result = runner.invoke(app, ["config", "validate", "--repo", str(repo)])
    assert result.exit_code == 1 and "global" in result.output


def test_global_findings_for_doctor(settings, repo, tmp_path, store, runner):
    from types import SimpleNamespace

    add(runner, tmp_path, ["--global"], name="shared_tool")
    add(runner, tmp_path, ["--repo", str(repo)], name="shared_tool")
    findings = cli_main._global_tools_findings([SimpleNamespace(repo_id="r", path=repo)])
    assert [f["status"] for f in findings] == ["valid", "notice"] and not any(f["failed"] for f in findings)
    store.write_text("{not valid")
    findings = cli_main._global_tools_findings([])
    assert findings[0]["failed"] and findings[0]["status"] == "invalid"


# -- malformed YAML beyond yaml.YAMLError ------------------------------------

BAD_DATE = "version: 1\ntools:\n  - name: t\n    description: 2001-13-45\n"


@pytest.mark.parametrize("command", [["config", "validate"], ["config", "show"], ["config", "tools", "list"]])
@pytest.mark.parametrize("where", ["project", "global"])
def test_a_bad_date_is_a_clean_error(runner, settings, repo, store, command, where):
    if where == "project":
        (repo / TOOLS_FILENAME).write_text(BAD_DATE)
    else:
        store.parent.mkdir(parents=True)
        store.write_text('{"version": 1, "tools": [{"name": "t", "description": 2001-13-45}]}')
    result = runner.invoke(app, [*command, "--repo", str(repo)])
    assert result.exit_code == 1, result.output
    output = " ".join(result.output.split())  # Rich wraps long paths
    assert "malformed YAML" in output and "Traceback" not in output
    assert not isinstance(result.exception, ValueError)


def test_a_bad_schema_date_is_a_clean_validate_error(runner, settings, repo):
    (repo / "devgraph.schema.yaml").write_text("version: 1\nnode_types:\n  - label: X\n    description: 2001-13-45\n")
    result = runner.invoke(app, ["config", "validate", "--repo", str(repo)])
    assert result.exit_code == 1 and "malformed YAML" in " ".join(result.output.split())
    assert not isinstance(result.exception, ValueError)


@pytest.mark.parametrize("text", ["name: t\ndescription: 2001-13-45\n", "name: " + "[" * 5000 + "\n"], ids=["date", "deep"])
def test_add_with_an_unloadable_tool_is_a_clean_error(runner, repo, tmp_path, text):
    result = add(runner, tmp_path, ["--repo", str(repo)], text=text)
    assert result.exit_code == 1 and "malformed YAML" in result.output
    assert not (repo / TOOLS_FILENAME).exists()


def test_add_with_an_unloadable_existing_file_is_a_clean_error(runner, repo, tmp_path):
    (repo / TOOLS_FILENAME).write_text(BAD_DATE)
    result = add(runner, tmp_path, ["--repo", str(repo)])
    assert result.exit_code == 1 and "malformed YAML" in result.output
    assert (repo / TOOLS_FILENAME).read_text() == BAD_DATE


def test_add_global_with_a_date_value_is_a_clean_error(runner, tmp_path, store):
    result = add(runner, tmp_path, ["--global"], text=tool_yaml(extra="").replace("Count this repository's nodes.", "2001-01-02"))
    assert result.exit_code == 1, result.output
    assert not isinstance(result.exception, TypeError) and "description" in result.output
    assert not store.exists()
