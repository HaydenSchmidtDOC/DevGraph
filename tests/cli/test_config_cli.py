"""`devgraph config` group: settings view, schema show/validate, eject."""

import json
import subprocess
import sys
import textwrap

import pytest
from typer.testing import CliRunner

from devgraph.cli import main as cli_main
from devgraph.cli.main import app
from devgraph.config.project_schema import SCHEMA_FILENAME, load_project_schema, starter_schema_text
from devgraph.config.project_tools import TOOLS_FILENAME
from devgraph.config.settings import Settings
from devgraph.graph.schema import NODE_LABELS, RELATIONSHIP_TYPES
from devgraph.registry.store import RepoRegistry

# Written before the per-repository opt-in: these tests assume project tools are served.
pytestmark = pytest.mark.usefixtures("trusted_project_tools")


@pytest.fixture
def runner():
    # Wide terminal so Rich never wraps or truncates table cells under test.
    return CliRunner(env={"COLUMNS": "200"})


@pytest.fixture
def settings(monkeypatch, tmp_path):
    fake = Settings(_env_file=None, neo4j_password="s3cret-pw", registry_db_path=tmp_path / "registry.sqlite3")
    monkeypatch.setattr(cli_main, "get_settings", lambda: fake)
    return fake


# ── settings ──────────────────────────────────────────────────────────────


def test_bare_config_still_prints_the_settings_table(runner, settings):
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0, result.output
    assert "DevGraph Configuration" in result.output
    assert "neo4j_uri" in result.output and "dashboard_port" in result.output


def test_settings_subcommand_matches_the_bare_view(runner, settings):
    bare = runner.invoke(app, ["config"])
    sub = runner.invoke(app, ["config", "settings"])
    assert sub.exit_code == 0 and sub.output == bare.output


def test_settings_subcommand_shows_one_key(runner, settings):
    result = runner.invoke(app, ["config", "settings", "dashboard_port"])
    assert result.exit_code == 0
    assert "dashboard_port" in result.output and "neo4j_uri" not in result.output


def test_unknown_setting_is_an_error(runner, settings):
    result = runner.invoke(app, ["config", "settings", "nope"])
    assert result.exit_code == 1 and "Unknown setting" in result.output


@pytest.mark.parametrize("args", [["config", "--json"], ["config", "settings", "--json"]])
def test_json_works_on_both_forms_and_masks_secrets(runner, settings, args):
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["neo4j_password"] == "****"
    assert data["dashboard_port"] == settings.dashboard_port
    assert "s3cret-pw" not in result.output


def test_table_masks_secret_values_and_defaults(runner, settings):
    result = runner.invoke(app, ["config", "--show-defaults"])
    assert result.exit_code == 0
    assert "s3cret-pw" not in result.output
    assert "devgraph-local-dev" not in result.output  # the password's default is a secret too
    assert "****" in result.output


def test_the_old_positional_form_points_at_settings(runner, settings):
    result = runner.invoke(app, ["config", "neo4j_uri"])
    assert result.exit_code == 2
    assert "devgraph config settings neo4j_uri" in result.output
    assert "Invalid value" not in result.output


def test_an_unknown_word_is_still_no_such_command(runner, settings):
    result = runner.invoke(app, ["config", "frobnicate"])
    assert result.exit_code == 2
    assert "devgraph config settings" not in result.output


def test_group_help_lists_the_subcommands(runner, settings):
    result = runner.invoke(app, ["config", "--help"])
    assert result.exit_code == 0
    assert "settings" in result.output


# ── eject ─────────────────────────────────────────────────────────────────


def uncommented_example(text):
    """The starter with its commented example switched on: every line after
    `extends: default` loses its leading '# '."""
    lines = text.splitlines()
    start = lines.index("extends: default") + 1
    return "\n".join(lines[:start] + [line[2:] if line.startswith("# ") else line for line in lines[start:]]) + "\n"


def test_starter_lists_every_builtin_and_loads(tmp_path):
    text = starter_schema_text()
    for label in NODE_LABELS:
        assert f"#   {label}\n" in text
    for rel in RELATIONSHIP_TYPES:
        assert f"#   {rel}\n" in text
    (tmp_path / SCHEMA_FILENAME).write_text(text)
    declaration = load_project_schema(tmp_path)
    assert declaration.extends == "default" and declaration.node_types == ()


def test_the_uncommented_example_is_valid(tmp_path):
    (tmp_path / SCHEMA_FILENAME).write_text(uncommented_example(starter_schema_text()))
    declaration = load_project_schema(tmp_path)
    (runbook,) = declaration.node_types
    assert runbook.label == "Runbook" and runbook.source.provider == "docs"
    assert [(r.type, r.provider, r.field) for r in declaration.relationships] == [("RUNBOOK_FOR", "docs", "service")]


def test_eject_writes_the_starter(runner, settings, tmp_path):
    result = runner.invoke(app, ["config", "eject", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / SCHEMA_FILENAME).read_text() == starter_schema_text()
    assert "devgraph config validate" in result.output


def test_eject_never_overwrites(runner, settings, tmp_path):
    existing = tmp_path / SCHEMA_FILENAME
    existing.write_text("version: 1\n# mine\n")
    result = runner.invoke(app, ["config", "eject", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert "already exists" in result.output and SCHEMA_FILENAME in result.output
    assert existing.read_text() == "version: 1\n# mine\n"


def test_eject_never_follows_a_symlink(runner, settings, tmp_path):
    target = tmp_path / "elsewhere.yaml"
    target.write_text("keep me\n")
    (tmp_path / SCHEMA_FILENAME).symlink_to(target)
    result = runner.invoke(app, ["config", "eject", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert target.read_text() == "keep me\n"


@pytest.mark.parametrize("make", ["missing", "file"])
def test_eject_needs_an_existing_directory(runner, settings, tmp_path, make):
    repo = tmp_path / "repo"
    if make == "file":
        repo.write_text("not a dir")
    result = runner.invoke(app, ["config", "eject", "--repo", str(repo)])
    assert result.exit_code == 1 and "not a directory" in result.output
    assert "Traceback" not in result.output


# ── show / validate ───────────────────────────────────────────────────────

WIDGET = """
    version: 1
    node_types:
      - label: Widget
        key: [slug]
        metadata: [{name: slug}]
    relationships:
      - type: LINKS
        provider: custom
        custom: {name: linker}
        from: Widget
        to: Module
"""
WIDGET_ONLY = """
    version: 1
    extends: none
    node_types:
      - label: Widget
        key: [slug]
        metadata: [{name: slug}]
"""


def write(repo, text):
    (repo / SCHEMA_FILENAME).write_text(textwrap.dedent(text))
    return repo


def show_json(runner, *args):
    result = runner.invoke(app, ["config", "show", "--json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_show_without_a_file_is_the_builtin_schema(runner, settings, tmp_path):
    data = show_json(runner, "--repo", str(tmp_path))
    assert data["status"] == "absent" and data["extends"] == "default"
    assert [n["label"] for n in data["node_types"]] == list(NODE_LABELS)
    assert {n["origin"] for n in data["node_types"]} == {"built-in"}
    assert [r["type"] for r in data["relationships"]] == list(RELATIONSHIP_TYPES)


def test_show_marks_where_each_entry_comes_from(runner, settings, tmp_path):
    data = show_json(runner, "--repo", str(write(tmp_path, WIDGET)))
    assert data["status"] == "valid" and data["schema_file"].endswith(SCHEMA_FILENAME)
    widget = next(n for n in data["node_types"] if n["label"] == "Widget")
    assert widget == {"label": "Widget", "origin": SCHEMA_FILENAME, "key": ["slug"], "color": None}
    links = next(r for r in data["relationships"] if r["type"] == "LINKS")
    assert links == {"type": "LINKS", "origin": SCHEMA_FILENAME, "from": "Widget", "to": "Module", "provider": "custom", "color": None}
    module = next(n for n in data["node_types"] if n["label"] == "Module")
    assert module == {"label": "Module", "origin": "built-in", "key": None, "color": None}


def test_show_with_extends_none_has_no_builtins(runner, settings, tmp_path):
    data = show_json(runner, "--repo", str(write(tmp_path, WIDGET_ONLY)))
    assert data["extends"] == "none"
    assert [n["label"] for n in data["node_types"]] == ["Widget"]
    assert data["relationships"] == []


def test_show_global_ignores_the_repo_file(runner, settings, tmp_path):
    write(tmp_path, WIDGET)
    data = show_json(runner, "--global")
    assert data["status"] == "global" and data["schema_file"] is None
    assert "Widget" not in [n["label"] for n in data["node_types"]]


def test_show_rejects_global_and_repo_together(runner, settings, tmp_path):
    result = runner.invoke(app, ["config", "show", "--global", "--repo", str(tmp_path)])
    assert result.exit_code == 2
    assert "Invalid value" not in result.output


def test_show_labels_builtin_keys_in_the_table(runner, settings, tmp_path):
    result = runner.invoke(app, ["config", "show", "--repo", str(tmp_path)])
    assert result.exit_code == 0 and "built-in identity" in result.output


def test_show_human_output_names_the_file_and_origins(runner, settings, tmp_path):
    result = runner.invoke(app, ["config", "show", "--repo", str(write(tmp_path, WIDGET))])
    assert result.exit_code == 0, result.output
    assert "Widget" in result.output and "built-in" in result.output and SCHEMA_FILENAME in result.output


def test_show_reports_an_invalid_schema(runner, settings, tmp_path):
    write(tmp_path, "version: 1\nnode_types: [oops\n")
    result = runner.invoke(app, ["config", "show", "--repo", str(tmp_path)])
    assert result.exit_code == 1 and "malformed YAML" in result.output


def test_show_needs_a_directory(runner, settings, tmp_path):
    result = runner.invoke(app, ["config", "show", "--repo", str(tmp_path / "missing")])
    assert result.exit_code == 1 and "not a directory" in result.output


def test_validate_one_repo(runner, settings, tmp_path):
    absent = runner.invoke(app, ["config", "validate", "--repo", str(tmp_path)])
    assert absent.exit_code == 0 and "absent" in absent.output
    valid = runner.invoke(app, ["config", "validate", "--repo", str(write(tmp_path, WIDGET))])
    assert valid.exit_code == 0 and "valid" in valid.output
    write(tmp_path, "version: 2\n")
    invalid = runner.invoke(app, ["config", "validate", "--repo", str(tmp_path)])
    assert invalid.exit_code == 1 and "invalid" in invalid.output


def test_validate_all_checks_every_registered_repo_and_conflicts(runner, settings, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    for d in (a, b):
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    write(a, WIDGET)
    write(b, WIDGET.replace("key: [slug]", "key: [code]").replace("{name: slug}", "{name: code}"))
    registry = RepoRegistry(settings.registry_db_path)
    try:
        registry.add_repo(a, repo_id="repo-a")
        registry.add_repo(b, repo_id="repo-b")
    finally:
        registry.close()
    result = runner.invoke(app, ["config", "validate", "--all"])
    assert result.exit_code == 1, result.output
    assert "repo-a" in result.output and "repo-b" in result.output and "conflict" in result.output


def test_validate_all_with_no_repos(runner, settings):
    result = runner.invoke(app, ["config", "validate", "--all"])
    assert result.exit_code == 0 and "No registered repositories" in result.output


def test_validate_rejects_repo_and_all_together(runner, settings, tmp_path):
    result = runner.invoke(app, ["config", "validate", "--all", "--repo", str(tmp_path)])
    assert result.exit_code == 2
    assert "Invalid value" not in result.output


# ── output hardening ──────────────────────────────────────────────────────

BAD_LABEL = 'version: 1\nnode_types:\n  - label: "[/bad]"\n    key: [slug]\n'


def test_validate_survives_markup_in_a_schema_error(runner, settings, tmp_path):
    write(tmp_path, BAD_LABEL)
    result = runner.invoke(app, ["config", "validate", "--repo", str(tmp_path)])
    assert result.exit_code == 1, result.output
    assert "[/bad]" in result.output
    assert "Traceback" not in result.output and "MarkupError" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_show_survives_markup_in_a_schema_error(runner, settings, tmp_path):
    write(tmp_path, BAD_LABEL)
    result = runner.invoke(app, ["config", "show", "--repo", str(tmp_path)])
    assert result.exit_code == 1, result.output
    assert "[/bad]" in result.output
    assert "MarkupError" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


@pytest.mark.xfail(
    sys.platform == "win32",
    reason="rich.markup.escape leaves a backslash before a non-tag '[' unescaped, so Rich drops the path separator",
)
def test_validate_survives_markup_in_the_repo_path(runner, settings, tmp_path):
    repo = tmp_path / "[x]" / "[/x]"
    repo.mkdir(parents=True)
    result = runner.invoke(app, ["config", "validate", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "[/x]" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_group_json_before_a_subcommand_is_rejected(runner, settings):
    result = runner.invoke(app, ["config", "--json", "show"])
    assert result.exit_code == 2
    assert "devgraph config show --json" in result.output
    assert runner.invoke(app, ["config", "--json"]).exit_code == 0


# ── project tools ─────────────────────────────────────────────────────────

TOOLS = """
    version: 1
    tools:
      - name: count_nodes
        description: Count this repository's nodes.
        cypher: |
          MATCH (n {repo_id: $repo_id}) RETURN count(n) AS n
"""
SHADOWING_TOOLS = TOOLS.replace("count_nodes", "search_component")


def write_tools(repo, text):
    (repo / TOOLS_FILENAME).write_text(textwrap.dedent(text))
    return repo


def test_validate_reports_tools_valid(runner, settings, tmp_path):
    result = runner.invoke(app, ["config", "validate", "--repo", str(write_tools(tmp_path, TOOLS))])
    assert result.exit_code == 0, result.output
    assert "count_nodes" in result.output


def test_validate_fails_on_an_invalid_tools_file_even_with_a_valid_schema(runner, settings, tmp_path):
    write(tmp_path, WIDGET)
    write_tools(tmp_path, TOOLS.replace("RETURN count(n) AS n", "SET n.x = 1 RETURN n"))
    result = runner.invoke(app, ["config", "validate", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert TOOLS_FILENAME in result.output and "read-only" in result.output


def test_validate_warns_when_a_tool_shadows_a_builtin(runner, settings, tmp_path):
    result = runner.invoke(app, ["config", "validate", "--repo", str(write_tools(tmp_path, SHADOWING_TOOLS))])
    assert result.exit_code == 0
    assert "warning" in result.output and "shadows a locked tool" in result.output


def test_show_json_includes_tools(runner, settings, tmp_path):
    data = show_json(runner, "--repo", str(write_tools(tmp_path, TOOLS)))
    assert data["tools"]["status"] == "valid"
    assert [t["name"] for t in data["tools"]["tools"]] == ["count_nodes"]
    assert data["tools"]["tools"][0]["max_rows"] == 100


def test_show_without_a_tools_file(runner, settings, tmp_path):
    data = show_json(runner, "--repo", str(tmp_path))
    assert data["tools"] == {"status": "absent", "tools_file": None, "tools": []}


def test_show_global_has_no_tools_section(runner, settings, tmp_path):
    data = show_json(runner, "--global")
    assert data["tools"] is None


def test_show_fails_on_an_invalid_tools_file(runner, settings, tmp_path):
    write_tools(tmp_path, "version: 1\ntools: [oops\n")
    result = runner.invoke(app, ["config", "show", "--repo", str(tmp_path)])
    assert result.exit_code == 1 and "Invalid project tools" in result.output


def test_tools_findings_cover_absent_valid_invalid_and_warning(tmp_path):
    from types import SimpleNamespace

    a, b, c, d = (tmp_path / n for n in "abcd")
    for p in (a, b, c, d):
        p.mkdir()
    write_tools(b, TOOLS)
    write_tools(c, "version: 1\ntools: [oops\n")
    write_tools(d, SHADOWING_TOOLS)
    repos = [SimpleNamespace(repo_id=p.name, path=p) for p in (a, b, c, d)]
    findings = cli_main._project_tools_findings(repos)
    by_status = {(f["repo_id"], f["status"]) for f in findings}
    assert {("a", "absent"), ("b", "valid"), ("c", "invalid"), ("d", "valid"), ("d", "warning")} == by_status
    assert [f["failed"] for f in findings if f["status"] == "invalid"] == [True]
    assert not any(f["failed"] for f in findings if f["status"] == "warning")


# ── enable / disable ──────────────────────────────────────────────────────


@pytest.fixture
def demo_repo(settings, tmp_path, monkeypatch):
    """A registered repo `demo` with a valid schema; the switch lookup reads the same registry."""
    from devgraph.config import project_switch

    monkeypatch.setattr(project_switch, "_registry_db_path", lambda: settings.registry_db_path)
    root = tmp_path / "demo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    write(root, WIDGET)
    registry = RepoRegistry(settings.registry_db_path)
    try:
        registry.add_repo(root, repo_id="demo")
    finally:
        registry.close()
    return root


def _flag(settings) -> bool:
    registry = RepoRegistry(settings.registry_db_path)
    try:
        return registry.get("demo").project_config_enabled
    finally:
        registry.close()


def test_disable_then_enable(runner, settings, demo_repo):
    result = runner.invoke(app, ["config", "disable", "demo"])
    assert result.exit_code == 0, result.output
    assert "disabled" in result.output
    assert "devgraph rescan demo --now" in result.output and "2 s" in result.output
    assert _flag(settings) is False

    again = runner.invoke(app, ["config", "disable", "demo"])
    assert again.exit_code == 0 and "already disabled" in again.output

    result = runner.invoke(app, ["config", "enable", "demo"])
    assert result.exit_code == 0, result.output
    assert "enabled" in result.output and "devgraph rescan demo --now" in result.output
    assert _flag(settings) is True

    again = runner.invoke(app, ["config", "enable", "demo"])
    assert again.exit_code == 0 and "already enabled" in again.output


@pytest.mark.parametrize("verb", ["enable", "disable"])
def test_enable_disable_unknown_repo(runner, settings, verb):
    result = runner.invoke(app, ["config", verb, "nope"])
    assert result.exit_code == 1 and "nope" in result.output


def test_validate_reports_a_disabled_repo_but_still_checks_the_file(runner, settings, demo_repo):
    runner.invoke(app, ["config", "disable", "demo"])
    result = runner.invoke(app, ["config", "validate", "--repo", str(demo_repo)])
    assert result.exit_code == 0, result.output
    assert "valid" in result.output and "disabled" in result.output
    write(demo_repo, "version: 2\n")
    assert runner.invoke(app, ["config", "validate", "--repo", str(demo_repo)]).exit_code == 1


def test_show_reports_a_disabled_repo_with_only_builtin_types(runner, settings, demo_repo):
    runner.invoke(app, ["config", "disable", "demo"])
    result = runner.invoke(app, ["config", "show", "--repo", str(demo_repo)])
    assert result.exit_code == 0, result.output
    assert "disabled" in result.output and "widget" not in result.output.lower()
    data = json.loads(runner.invoke(app, ["config", "show", "--repo", str(demo_repo), "--json"]).stdout)
    assert data["project_config"] == "disabled"
    assert {n["origin"] for n in data["node_types"]} == {"built-in"}


def test_show_json_enabled(runner, settings, demo_repo):
    data = json.loads(runner.invoke(app, ["config", "show", "--repo", str(demo_repo), "--json"]).stdout)
    assert data["project_config"] == "enabled"


def test_tools_findings_flag_a_disabled_repo_as_not_served(runner, settings, demo_repo):
    from types import SimpleNamespace

    write_tools(demo_repo, TOOLS)
    runner.invoke(app, ["config", "disable", "demo"])
    findings = cli_main._project_tools_findings([SimpleNamespace(repo_id="demo", path=demo_repo)])
    assert [(f["status"], f["failed"]) for f in findings] == [("disabled", False)]
    assert findings[0]["detail"] == "tools: count_nodes (not served: project config disabled)"
    # An invalid file in a disabled repo is still a failure.
    write_tools(demo_repo, "version: 1\ntools: [oops\n")
    findings = cli_main._project_tools_findings([SimpleNamespace(repo_id="demo", path=demo_repo)])
    assert [f["failed"] for f in findings] == [True]


def test_validate_reports_disabled_tools_as_not_served(runner, settings, demo_repo):
    write_tools(demo_repo, TOOLS)
    runner.invoke(app, ["config", "disable", "demo"])
    for args in (["--repo", str(demo_repo)], ["--all"]):
        result = runner.invoke(app, ["config", "validate", *args])
        assert result.exit_code == 0, result.output
        assert "tools: count_nodes (not served: project config disabled)" in " ".join(result.output.split())
        assert "valid" not in [line.split()[0] for line in result.output.splitlines() if "tools:" in line]


def test_validate_all_hint_names_the_registered_repo(runner, settings, demo_repo):
    runner.invoke(app, ["config", "disable", "demo"])
    result = runner.invoke(app, ["config", "validate", "--all"])
    assert "devgraph config enable demo" in " ".join(result.output.split())
    assert "<repo_id>" not in result.output


def test_validate_and_show_by_path_look_up_the_registered_id(runner, settings, demo_repo):
    runner.invoke(app, ["config", "disable", "demo"])
    for cmd in ("validate", "show"):
        result = runner.invoke(app, ["config", cmd, "--repo", str(demo_repo)])
        assert "devgraph config enable demo" in " ".join(result.output.split()), result.output
        assert "<repo_id>" not in result.output


def test_validate_all_marks_a_disabled_repo_in_a_conflict(runner, settings, tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    write(a, WIDGET)
    write(b, WIDGET.replace("key: [slug]", "key: [code]").replace("{name: slug}", "{name: code}"))
    registry = RepoRegistry(settings.registry_db_path)
    try:
        registry.add_repo(a, repo_id="repo-a")
        registry.add_repo(b, repo_id="repo-b")
        registry.set_project_config_enabled("repo-b", False)
    finally:
        registry.close()
    from devgraph.config import project_switch

    monkeypatch.setattr(project_switch, "_registry_db_path", lambda: settings.registry_db_path)
    result = runner.invoke(app, ["config", "validate", "--all"])
    assert result.exit_code == 1, result.output
    text = " ".join(result.output.split())
    assert "repo-b (disabled) declares" in text and "repo-a (disabled)" not in text


def test_show_json_includes_colour(runner, settings, tmp_path):
    text = WIDGET.replace("- label: Widget", "- label: Widget\n        color: \"#1f77b4\"")
    data = show_json(runner, "--repo", str(write(tmp_path, text)))
    assert next(n for n in data["node_types"] if n["label"] == "Widget")["color"] == "#1f77b4"
    assert next(r for r in data["relationships"] if r["type"] == "LINKS")["color"] is None


def test_show_text_tables_have_a_colour_column(runner, settings, tmp_path):
    text = WIDGET.replace("- label: Widget", "- label: Widget\n        color: \"#1f77b4\"")
    result = runner.invoke(app, ["config", "show", "--repo", str(write(tmp_path, text))])
    assert result.exit_code == 0, result.output
    assert result.output.count("Colour") == 2
    assert "#1f77b4" in result.output
