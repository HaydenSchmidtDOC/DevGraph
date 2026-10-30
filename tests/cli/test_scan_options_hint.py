"""`devgraph mentions` and `devgraph annotate --docs-path` say when the change
takes effect (audit 10 item 2): only a full scan applies it."""

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from devgraph import config as config_module
from devgraph.cli import main as cli_main
from devgraph.cli.main import app
from devgraph.registry.store import RepoRegistry


def _invoke(tmp_path, args):
    db_path = tmp_path / "registry.db"
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    registry = RepoRegistry(db_path)
    repo_id = registry.add_repo(root).repo_id
    registry.close()
    settings = MagicMock(registry_db_path=db_path)
    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=settings), \
         patch.object(cli_main, "get_settings", return_value=settings):
        result = CliRunner().invoke(app, [args[0], repo_id, *args[1:]])
    assert result.exit_code == 0, result.stdout
    return repo_id, " ".join(result.stdout.split())


def test_mentions_enable_says_a_rescan_applies_it(tmp_path):
    repo_id, out = _invoke(tmp_path, ["mentions", "enable"])
    assert "Mentions enabled" in out
    assert f"devgraph rescan {repo_id}" in out
    assert "agent" in out


def test_mentions_disable_says_a_rescan_applies_it(tmp_path):
    repo_id, out = _invoke(tmp_path, ["mentions", "disable"])
    assert f"devgraph rescan {repo_id}" in out


def test_a_docs_path_change_says_a_rescan_applies_it(tmp_path):
    repo_id, out = _invoke(tmp_path, ["annotate", "--docs-path", "docs"])
    assert "Docs path set" in out
    assert f"devgraph rescan {repo_id}" in out


def test_pr_source_says_nothing_about_a_rescan(tmp_path):
    _, out = _invoke(tmp_path, ["pr-source", "enable"])
    assert "rescan" not in out
