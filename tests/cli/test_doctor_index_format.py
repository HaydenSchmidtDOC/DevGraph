"""`devgraph doctor` checks each indexed repository's graph index (audit 9
item 8c): a registered repository whose graph was wiped, or whose index
predates the current format, awaits a rescan, as `devgraph status` already
says, so doctor no longer ends with "All checks passed" for it."""

from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

from typer.testing import CliRunner

from devgraph.cli import main as cli_main
from devgraph.indexer.dispatch import INDEX_FORMAT
from devgraph.registry.store import RepoRegistry


class _IndexedEngine:
    """A reachable Neo4j recording each repository's index format (None: no index)."""

    formats: ClassVar[dict[str, int | None]] = {}
    #: Repositories whose Repository node is there without a format (an index older than formats).
    unstamped: ClassVar[set[str]] = set()

    def __init__(self, *args, **kwargs):
        pass

    def verify_connectivity(self):
        pass

    def init_schema(self):
        pass

    def close(self):
        pass

    def index_format(self, repo_id):
        return self.formats.get(repo_id)

    def graph_repo_ids(self):
        return {repo_id for repo_id, fmt in self.formats.items() if fmt is not None} | self.unstamped

    def run_cypher(self, query, params=None):
        assert "Repository" in query
        return [{"present": params["repo_id"] in self.unstamped}]


def _doctor(tmp_path, formats, never_indexed=(), unstamped=()):
    registry = RepoRegistry(tmp_path / "registry.sqlite3")
    for repo_id in [*formats, *never_indexed]:
        root = tmp_path / repo_id
        (root / ".git").mkdir(parents=True)
        registry.add_repo(root, repo_id=repo_id)
        if repo_id in formats:
            registry.mark_indexed(repo_id)
    registry.close()
    settings = SimpleNamespace(
        registry_db_path=tmp_path / "registry.sqlite3",
        neo4j_uri="bolt://127.0.0.1:7687", neo4j_user="neo4j", neo4j_password="devgraph-local-dev",
        health_check_interval_s=30,
    )
    engine = type("Engine", (_IndexedEngine,), {"formats": formats, "unstamped": set(unstamped)})
    with patch.object(cli_main, "get_settings", return_value=settings), \
         patch.object(cli_main, "GraphEngine", engine), \
         patch.object(cli_main, "resolve_podman", return_value=None), \
         patch.object(cli_main, "_schema_drift_findings", return_value=[]), \
         patch.object(cli_main, "_docs_source_findings", return_value=[]), \
         patch.object(cli_main, "_stale_schema_objects", return_value=[]), \
         patch("devgraph.indexer.schema_constraints.constraint_drift", return_value=[]):
        result = CliRunner().invoke(cli_main.app, ["doctor"])
    return " ".join(result.stdout.split()), result


def _index_lines(out):
    return out.split("Graph index", 1)[1].split("Live Watcher", 1)[0]


def test_a_wiped_or_outdated_index_is_not_all_checks_passed(tmp_path):
    out, result = _doctor(
        tmp_path, {"zz-wiped": None, "zz-old": INDEX_FORMAT - 1, "zz-current": INDEX_FORMAT},
        never_indexed=("zz-new",),
    )
    lines = _index_lines(out)
    assert "[!] zz-wiped: no index in the graph, rescan pending" in lines
    assert f"[!] zz-old: index format {INDEX_FORMAT - 1} < {INDEX_FORMAT}, rescan pending" in lines
    assert "devgraph rescan" in lines
    # Up to date, or never indexed yet (its first scan stamps the format).
    assert "zz-current" not in lines and "zz-new" not in lines
    assert "All checks passed" not in out
    assert "2 repositories await a rescan" in out
    assert result.exit_code == 0, out


def test_a_current_index_passes(tmp_path):
    out, result = _doctor(tmp_path, {"zz-current": INDEX_FORMAT})
    assert "[OK]" in _index_lines(out)
    assert "All checks passed" in out
    assert result.exit_code == 0, out


def test_an_index_older_than_formats_is_told_from_a_wiped_one(tmp_path):
    out, result = _doctor(tmp_path, {"zz-wiped": None, "zz-unstamped": None}, unstamped=("zz-unstamped",))
    lines = _index_lines(out)
    assert "[!] zz-wiped: no index in the graph, rescan pending" in lines
    assert "[!] zz-unstamped: no index format recorded, rescan pending" in lines
    assert "2 repositories await a rescan" in out
    assert result.exit_code == 0, out
