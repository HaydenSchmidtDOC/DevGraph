"""`devgraph annotate --note` refuses a note a scan would remove: with no docs
path set, or outside it (audit 10 item 2 review)."""

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from devgraph import config as config_module
from devgraph.cli import main as cli_main
from devgraph.cli.main import app
from devgraph.registry.store import RepoRegistry

NOTE = "---\ntype: requirement\nid: REQ-1\n---\n# Req\n"


def _invoke(tmp_path, docs_path, args):
    db_path = tmp_path / "registry.db"
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    for rel in ("docs/r.md", "notes/r.md"):
        (root / rel).parent.mkdir(exist_ok=True)
        (root / rel).write_text(NOTE)
    registry = RepoRegistry(db_path)
    repo_id = registry.add_repo(root).repo_id
    if docs_path is not None:
        registry.set_docs_path(repo_id, docs_path)
    registry.close()
    settings = MagicMock(registry_db_path=db_path)
    config_module.get_settings.cache_clear()
    with patch.object(config_module, "get_settings", return_value=settings), \
         patch.object(cli_main, "get_settings", return_value=settings), \
         patch.object(cli_main, "GraphEngine") as engine, \
         patch.object(cli_main, "index_doc_file") as index:
        # A never-indexed repository's id holds no other repository's data.
        engine.return_value.repository_owner.return_value = (False, None)
        result = CliRunner().invoke(app, ["annotate", repo_id, *args])
    return result, " ".join(result.stdout.split()), engine, index


def test_a_note_with_no_docs_path_is_refused(tmp_path):
    result, out, engine, index = _invoke(tmp_path, None, ["--note", "docs/r.md"])
    assert result.exit_code == 1
    assert "no docs path is set" in out and "--docs-path" in out
    assert not index.called and not engine.called


def test_a_note_outside_the_docs_path_is_refused(tmp_path):
    result, out, _, index = _invoke(tmp_path, "docs", ["--note", "notes/r.md"])
    assert result.exit_code == 1
    assert "outside the docs path" in out
    assert not index.called


def test_a_note_inside_the_docs_path_is_indexed(tmp_path):
    result, out, _, index = _invoke(tmp_path, "docs", ["--note", "docs/r.md"])
    assert result.exit_code == 0, out
    assert "Indexed note" in out and index.called


def test_a_docs_path_given_with_the_note_counts(tmp_path):
    result, out, _, index = _invoke(tmp_path, None, ["--docs-path", "notes", "--note", "notes/r.md"])
    assert result.exit_code == 0, out
    assert index.called
