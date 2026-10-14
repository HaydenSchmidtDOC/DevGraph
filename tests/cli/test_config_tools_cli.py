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


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    """Every test here: the CLI's registry lives in tmp_path (never the user's ~/.devgraph)."""
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


def register(settings, root, repo_id="demo", *, enabled=True):
    from devgraph.registry.store import RepoRegistry

    (root / ".git").mkdir(exist_ok=True)
    registry = RepoRegistry(settings.registry_db_path)
    try:
        registry.add_repo(root, repo_id=repo_id)
        if not enabled:
            registry.set_project_config_enabled(repo_id, False)
    finally:
        registry.close()


def flat(output):
    return " ".join(output.split())  # Rich wraps long lines


# -- add ---------------------------------------------------------------------


def test_add_appends_to_the_project_file_keeping_comments(runner, settings, repo, tmp_path):
    register(settings, repo)
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


# -- default scope -----------------------------------------------------------


def test_default_scope_is_the_registered_repo_containing_the_cwd(runner, settings, repo, tmp_path, monkeypatch):
    register(settings, repo)
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    result = add(runner, tmp_path, [])
    assert result.exit_code == 0, result.output
    assert names(repo / TOOLS_FILENAME) == ["count_nodes"] and not (sub / TOOLS_FILENAME).exists()
    assert "not a registered repository" not in result.output


def test_default_scope_picks_the_deepest_registered_repo(runner, settings, repo, tmp_path, monkeypatch):
    inner = repo / "vendor" / "inner"
    inner.mkdir(parents=True)
    register(settings, repo, "outer")
    register(settings, inner, "inner")
    (inner / "lib").mkdir()
    monkeypatch.chdir(inner / "lib")
    assert add(runner, tmp_path, []).exit_code == 0
    assert (inner / TOOLS_FILENAME).exists() and not (repo / TOOLS_FILENAME).exists()


def test_default_scope_outside_a_registered_repo_uses_the_cwd_and_warns(runner, tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    result = add(runner, tmp_path, [])
    assert result.exit_code == 0, result.output
    assert (plain / TOOLS_FILENAME).exists()
    assert "not a registered repository" in flat(result.output) and "won't serve" in flat(result.output)
    listed = runner.invoke(app, ["config", "tools", "list", "--json"])
    assert listed.exit_code == 0 and "count_nodes" in [t["name"] for t in json.loads(listed.stdout)["tools"]]
    assert "not a registered repository" in flat(listed.stderr)


def test_show_and_validate_default_to_the_containing_registered_repo(runner, settings, repo, monkeypatch):
    register(settings, repo)
    sub = repo / "src"
    sub.mkdir()
    monkeypatch.chdir(sub)
    shown = runner.invoke(app, ["config", "show", "--json"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["repo"] == str(repo.resolve())
    (repo / TOOLS_FILENAME).write_text(BAD_DATE)
    assert runner.invoke(app, ["config", "validate"]).exit_code == 1


# -- reload notes ------------------------------------------------------------


def test_a_global_write_says_where_global_tools_are_served(runner, tmp_path, store):
    output = flat(add(runner, tmp_path, ["--global"]).output)
    assert "only in MCP sessions scoped to a registered repository" in output and "2 seconds" in output


def test_an_unregistered_repo_write_says_it_is_not_served(runner, repo, tmp_path):
    output = flat(add(runner, tmp_path, ["--repo", str(repo)]).output)
    assert "not a registered repository" in output and "2 seconds" not in output


def test_a_disabled_repo_write_says_it_is_not_served(runner, settings, repo, tmp_path):
    register(settings, repo, enabled=False)
    output = flat(add(runner, tmp_path, ["--repo", str(repo)]).output)
    assert "project config is disabled" in output and "devgraph config enable demo" in output
    assert "2 seconds" not in output


def test_list_explains_where_global_tools_are_served(runner, tmp_path, store):
    add(runner, tmp_path, ["--global"])
    for flags in (["--global"], []):
        output = flat(runner.invoke(app, ["config", "tools", "list", *flags]).output)
        assert "only in MCP sessions scoped to a registered repository" in output


# -- minor -------------------------------------------------------------------


def test_add_reports_an_unreadable_file_once(runner, repo, tmp_path):
    (repo / TOOLS_FILENAME).mkdir()
    result = add(runner, tmp_path, ["--repo", str(repo)])
    assert result.exit_code == 1 and result.output.count("Error:") == 1 and "cannot be read" in result.output


def test_list_title_is_escaped(runner, tmp_path):
    odd = tmp_path / "re[b]po"
    odd.mkdir()
    output = flat(runner.invoke(app, ["config", "tools", "list", "--repo", str(odd)]).output)
    assert "re[b]po" in output


def test_global_tool_keys_keep_their_order(runner, tmp_path, store):
    add(runner, tmp_path, ["--global"], name="first_tool")
    add(runner, tmp_path, ["--global"], name="second_tool")
    tools = json.loads(store.read_text())["tools"]
    assert [list(t) for t in tools] == [["name", "description", "cypher"]] * 2


def test_list_shows_run_cypher_only_when_it_is_served(runner, repo, monkeypatch, tmp_path):
    def rows():
        result = runner.invoke(app, ["config", "tools", "list", "--repo", str(repo), "--json"])
        return {t["name"]: t for t in json.loads(result.stdout)["tools"]}

    assert "run_cypher" not in rows()
    fake = Settings(_env_file=None, neo4j_password="x", registry_db_path=tmp_path / "registry.sqlite3", enable_run_cypher=True)
    monkeypatch.setattr(cli_main, "get_settings", lambda: fake)
    assert rows()["run_cypher"]["origin"] == "built-in"


def test_list_shows_builtin_named_tools_as_ignored(runner, repo, store):
    global_tools.save_global_tools([{"name": "search_component", "description": "Mine.", "cypher": "MATCH (n {repo_id: $repo_id}) RETURN n"}])
    (repo / TOOLS_FILENAME).write_text(textwrap.dedent("""\
        version: 1
        tools:
          - name: run_cypher
            description: Mine.
            cypher: "MATCH (n {repo_id: $repo_id}) RETURN n"
        """))
    result = runner.invoke(app, ["config", "tools", "list", "--repo", str(repo), "--json"])
    rows = json.loads(result.stdout)["tools"]
    ignored = [r for r in rows if r.get("ignored")]
    assert {(r["name"], r["origin"], r["ignored"]) for r in ignored} == {
        ("search_component", "global", "shadows a locked tool"),
        ("run_cypher", "project", "shadows a locked tool"),
    }
    assert "ignored: shadows a locked tool" in flat(runner.invoke(app, ["config", "tools", "list", "--repo", str(repo)]).output)
    only_global = json.loads(runner.invoke(app, ["config", "tools", "list", "--global", "--json"]).stdout)["tools"]
    assert only_global == [{"name": "search_component", "origin": "global", "locked": False, "ignored": "shadows a locked tool"}]


def test_project_write_is_atomic_and_keeps_the_mode(runner, repo, tmp_path, monkeypatch):
    import os
    import stat

    add(runner, tmp_path, ["--repo", str(repo)], name="first_tool")
    path = repo / TOOLS_FILENAME
    if os.name != "nt":
        path.chmod(0o640)
    assert add(runner, tmp_path, ["--repo", str(repo)], name="second_tool").exit_code == 0
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o640
    before = path.read_bytes()

    def boom(*args):
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", boom)
    result = add(runner, tmp_path, ["--repo", str(repo)], name="third_tool")
    assert result.exit_code == 1 and "disk gone" in result.output
    assert path.read_bytes() == before
    assert sorted(p.name for p in repo.iterdir()) == [TOOLS_FILENAME]


# -- symlinked targets ---------------------------------------------------------


def test_symlinked_project_file_is_refused_untouched(runner, repo, tmp_path):
    real = tmp_path / "real.yaml"
    real.write_text("version: 1\ntools: []\n")
    link = repo / TOOLS_FILENAME
    link.symlink_to(real)
    result = add(runner, tmp_path, ["--repo", str(repo)])
    assert result.exit_code == 1 and "is a symlink to" in flat(result.output) and str(real.resolve()) in flat(result.output)
    assert link.is_symlink() and real.read_text() == "version: 1\ntools: []\n"


def test_symlinked_global_store_is_refused_untouched(runner, tmp_path, store):
    real = tmp_path / "real.json"
    real.write_text('{"version": 1, "tools": []}')
    store.parent.mkdir(exist_ok=True)
    store.symlink_to(real)
    result = add(runner, tmp_path, ["--global"])
    assert result.exit_code == 1 and "is a symlink to" in flat(result.output)
    assert store.is_symlink() and real.read_text() == '{"version": 1, "tools": []}'


def test_add_global_value_json_cannot_store_is_a_friendly_error(runner, tmp_path, store):
    text = tool_yaml().replace("description: Count this repository's nodes.", "description: !!binary aGk=")
    result = add(runner, tmp_path, ["--global"], text=text)
    assert result.exit_code == 1, result.output
    assert "a tool holds a value JSON cannot store" in flat(result.output)
    assert not store.exists()


def test_reset_refuses_a_symlinked_project_file(runner, repo, tmp_path):
    real = tmp_path / "real.yaml"
    real.write_text(tool_yaml("count_nodes"))
    (repo / TOOLS_FILENAME).symlink_to(real)
    result = runner.invoke(app, ["config", "tools", "reset", "--repo", str(repo), "--yes"])
    assert result.exit_code == 1 and "symlink" in " ".join(result.output.split())
    assert (repo / TOOLS_FILENAME).is_symlink() and real.exists()
