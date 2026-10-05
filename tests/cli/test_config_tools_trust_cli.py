"""`devgraph config tools trust|untrust`, the trust state in `config tools list`, and doctor's report."""

import json
import textwrap

import pytest

from devgraph.cli import main as cli_main
from devgraph.cli.main import app
from devgraph.config.project_tools import TOOLS_FILENAME
from devgraph.registry.store import RepoRegistry
from tests.cli import test_config_tools_cli as base
from tests.cli.test_config_tools_cli import register

# the `config tools` CLI tests' fixtures: a CliRunner, and the CLI's registry in tmp_path
runner, settings = base.runner, base.settings

TOOLS = textwrap.dedent("""\
    version: 1
    tools:
      - name: count_nodes
        description: Count this repository's nodes.
        cypher: |
          MATCH (n {repo_id: $repo_id}) RETURN count(n) AS n
""")


@pytest.fixture
def trust(real_project_trust, settings, monkeypatch):
    monkeypatch.setattr(real_project_trust, "_registry_db_path", lambda: settings.registry_db_path)
    return real_project_trust


@pytest.fixture
def repo(tmp_path, settings):
    root = tmp_path / "repo"
    root.mkdir()
    (root / TOOLS_FILENAME).write_text(TOOLS)
    register(settings, root)
    return root


def recorded(settings):
    registry = RepoRegistry(settings.registry_db_path)
    try:
        return registry.get("demo").project_tools_sha256
    finally:
        registry.close()


def sha(root):
    from devgraph.config.project_trust import tools_sha256

    return tools_sha256((root / TOOLS_FILENAME).read_bytes())


def test_trust_shows_the_tools_and_hash_then_asks_on_a_tty(runner, trust, repo, settings, monkeypatch):
    monkeypatch.setattr(cli_main, "_stdin_is_tty", lambda: True)
    result = runner.invoke(app, ["config", "tools", "trust", "demo"], input="n\n")
    assert result.exit_code == 1, result.output
    out = result.output
    assert out.index("count_nodes") < out.index("MATCH (n {repo_id: $repo_id})") < out.index(sha(repo)) < out.index("Trust")
    assert "read the whole graph" in out
    assert recorded(settings) is None

    result = runner.invoke(app, ["config", "tools", "trust", "demo"], input="y\n")
    assert result.exit_code == 0, result.output
    assert recorded(settings) == sha(repo)
    assert trust.project_tools_trust(repo, (repo / TOOLS_FILENAME).read_bytes()) == "trusted"


def test_trust_with_the_matching_sha256_needs_no_tty(runner, trust, repo, settings):
    result = runner.invoke(app, ["config", "tools", "trust", "demo", "--sha256", sha(repo).upper()])
    assert result.exit_code == 0, result.output
    assert recorded(settings) == sha(repo)


def test_trust_by_path_and_from_inside_the_repository(runner, trust, repo, settings, monkeypatch):
    result = runner.invoke(app, ["config", "tools", "trust", str(repo), "--sha256", sha(repo)])
    assert result.exit_code == 0, result.output
    runner.invoke(app, ["config", "tools", "untrust", "demo"])
    monkeypatch.chdir(repo)
    result = runner.invoke(app, ["config", "tools", "trust", "--sha256", sha(repo)])
    assert result.exit_code == 0, result.output
    assert recorded(settings) == sha(repo)


def test_trust_with_a_wrong_sha256_is_refused(runner, trust, repo, settings):
    result = runner.invoke(app, ["config", "tools", "trust", "demo", "--sha256", "0" * 64])
    assert result.exit_code == 1
    assert "does not match" in result.output
    assert result.output.count(sha(repo)) == 1  # in the listing, not repeated by the error
    assert recorded(settings) is None


def test_trust_without_a_tty_or_sha256_is_refused(runner, trust, repo, settings, monkeypatch):
    monkeypatch.setattr(cli_main, "_stdin_is_tty", lambda: False)
    result = runner.invoke(app, ["config", "tools", "trust", "demo"], input="y\n")
    assert result.exit_code == 1
    assert "--sha256" not in result.output and "terminal" in result.output
    assert recorded(settings) is None


def test_trust_refuses_an_invalid_or_missing_file_and_an_unknown_repo(runner, trust, repo, settings):
    (repo / TOOLS_FILENAME).write_text("version: 1\ntools: [")
    result = runner.invoke(app, ["config", "tools", "trust", "demo", "--sha256", sha(repo)])
    assert result.exit_code == 1 and "invalid" in result.output
    (repo / TOOLS_FILENAME).unlink()
    result = runner.invoke(app, ["config", "tools", "trust", "demo", "--sha256", "0" * 64])
    assert result.exit_code == 1 and "no devgraph.tools.yaml" in result.output
    result = runner.invoke(app, ["config", "tools", "trust", "nope", "--sha256", "0" * 64])
    assert result.exit_code == 1 and "not a registered repository" in result.output
    assert recorded(settings) is None


def test_untrust_clears_the_approval(runner, trust, repo, settings):
    runner.invoke(app, ["config", "tools", "trust", "demo", "--sha256", sha(repo)])
    result = runner.invoke(app, ["config", "tools", "untrust", "demo"])
    assert result.exit_code == 0, result.output
    assert recorded(settings) is None
    result = runner.invoke(app, ["config", "tools", "untrust", "demo"])
    assert result.exit_code == 0 and "not trusted" in result.output


def test_list_shows_the_trust_state(runner, trust, repo, settings):
    def state():
        result = runner.invoke(app, ["config", "tools", "list", "--repo", str(repo), "--json"])
        assert result.exit_code == 0, result.output
        return json.loads(result.output)["trust"]

    assert state() == "untrusted"
    text = runner.invoke(app, ["config", "tools", "list", "--repo", str(repo)]).output
    assert "not trusted" in text and "devgraph config tools trust demo" in text
    runner.invoke(app, ["config", "tools", "trust", "demo", "--sha256", sha(repo)])
    assert state() == "trusted"
    (repo / TOOLS_FILENAME).write_text(TOOLS + "# edited\n")
    assert state() == "changed"


def test_a_cli_edit_says_the_tools_need_retrusting(runner, trust, repo, settings, tmp_path):
    runner.invoke(app, ["config", "tools", "trust", "demo", "--sha256", sha(repo)])
    result = runner.invoke(app, ["config", "tools", "delete", "count_nodes", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "devgraph config tools trust demo" in " ".join(result.output.split())


def test_doctor_reports_untrusted_and_changed_files(trust, repo, settings):
    from devgraph.cli.main import _project_tools_findings

    def finding():
        registry = RepoRegistry(settings.registry_db_path)
        try:
            return next(f for f in _project_tools_findings(registry.list_repos()) if f["repo_id"] == "demo")
        finally:
            registry.close()

    untrusted = finding()
    assert untrusted["status"] == "warning" and not untrusted["failed"]
    assert "not trusted" in untrusted["detail"] and "devgraph config tools trust demo" in untrusted["detail"]
    registry = RepoRegistry(settings.registry_db_path)
    registry.set_project_tools_sha256("demo", sha(repo))
    registry.close()
    assert finding()["status"] == "valid"
    (repo / TOOLS_FILENAME).write_text(TOOLS + "# edited\n")
    changed = finding()
    assert changed["status"] == "warning" and "changed since it was trusted" in changed["detail"]


SECRET_TOOLS = TOOLS.replace("count_nodes", "outside_secret_tool")


def test_trust_refuses_a_tools_file_outside_the_repository(runner, trust, repo, settings, tmp_path):
    outside = tmp_path / "elsewhere.yaml"
    outside.write_text(SECRET_TOOLS)
    (repo / TOOLS_FILENAME).unlink()
    (repo / TOOLS_FILENAME).symlink_to(outside)
    result = runner.invoke(app, ["config", "tools", "trust", "demo", "--sha256", sha(repo)])
    assert result.exit_code != 0
    assert "outside the repository" in result.output
    assert "outside_secret_tool" not in result.output and "elsewhere" not in result.output
    assert recorded(settings) is None


def test_trust_shows_descriptions_parameters_and_global_overrides(runner, trust, repo, settings):
    from devgraph.config.global_tools import save_global_tools

    (repo / TOOLS_FILENAME).write_text(textwrap.dedent("""\
        version: 1
        tools:
          - name: count_nodes
            description: Count this repository's nodes.
            cypher: |
              MATCH (n {repo_id: $repo_id, kind: $kind}) RETURN count(n) AS n
            parameters:
              - name: kind
                type: string
                description: Which kind of node to count.
    """))
    save_global_tools([{"name": "count_nodes", "description": "Global count.",
                        "cypher": "MATCH (n {repo_id: $repo_id}) RETURN count(n) AS n"}])
    result = runner.invoke(app, ["config", "tools", "trust", "demo", "--sha256", sha(repo)])
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "Count this repository's nodes." in out
    assert "kind" in out and "Which kind of node to count." in out
    assert "overrides the global tool 'count_nodes'" in out
