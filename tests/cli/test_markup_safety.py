"""Repository text reaches the CLI's Rich output literally, never as markup.

A folder named like a markup tag (`[bold]`, `[/]`), or containing an emoji
code (`:x:`) or backslashes, must print back unchanged and never raise a
MarkupError. The guard test below keeps new `console.print` calls honest.
"""

import ast
import os
from pathlib import Path

import pytest
from rich.errors import MarkupError
from typer.testing import CliRunner

from devgraph.cli import main as cli_main
from devgraph.cli.main import app
from devgraph.config.settings import Settings
from devgraph.registry.store import RepoRegistry

# Windows forbids ':' in a file name and treats '\' as the separator, so the
# Windows path gets its backslashes from the separators alone.
if os.name == "nt":
    MARKUP_DIRS = ("[bold]b[/]", "[x]", "c")
else:
    MARKUP_DIRS = ("[bold]b[/]", "[x]\\", "d\\[red]:x: e")


@pytest.fixture
def markup_repo(tmp_path, monkeypatch):
    """A registered repository, and the registry itself, under folders named like markup."""
    root = tmp_path.joinpath(*MARKUP_DIRS)
    (root / ".git").mkdir(parents=True)
    db_path = root / "registry.db"
    registry = RepoRegistry(db_path)
    try:
        registry.add_repo(root)
    finally:
        registry.close()
    settings = Settings(_env_file=None, registry_db_path=db_path, neo4j_uri="bolt://127.0.0.1:9")
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)

    def unreachable(self):
        raise ConnectionError(f"Neo4j is unreachable from {root}")

    monkeypatch.setattr(cli_main.GraphEngine, "verify_connectivity", unreachable)
    monkeypatch.chdir(root)
    return root.resolve()


COMMANDS = [
    ["list"],
    ["doctor"],
    ["status"],
    ["config", "settings"],
    ["config", "validate"],
    ["config", "show"],
    ["config", "schema", "list"],
    ["config", "tools", "list"],
]


@pytest.mark.parametrize("args", COMMANDS, ids=" ".join)
def test_command_prints_markup_like_paths_literally(markup_repo, args):
    result = CliRunner().invoke(app, args)
    assert not isinstance(result.exception, MarkupError), result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), result.output
    # The table titles wrap at the table's width, so compare the text without line breaks.
    flat = "".join(line.strip(" │┃") for line in result.output.splitlines())
    assert str(markup_repo) in flat, result.output


# ── guard: new console output must escape what it interpolates ─────────────

# Interpolated expressions known to be fixed text, numbers, or already escaped.
SAFE_EXPRESSIONS = {
    "count", "pid", "existing_pid", "started_pid", "node_count", "total_nodes", "total_rels",
    "marker", "liveness", "label", "verb", "word", "noun", "colour", "consequence", "origin",
    "subject", "detail",  # escaped when assigned
    "active_str", "watch_str", "*row",  # row: escaped when built
    "SCHEMA_FILENAME", "TOOLS_FILENAME", "_GLOBAL_TOOLS_NOTE", "obj.kind", "sys.version.split()[0]",
    "' '.join(MCP_SERVER_ARGS)", "finding['status']", "row['origin']",
    "result['commits_indexed']", "result['commits_deleted']",
    "summary['community_count']", "summary['modularity']", "summary['node_count']",
    "tool['max_rows']", "tool['timeout_s']",
}


def _safe(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in ("escape", "len"):
            return True
        if node.func.id == "str" and len(node.args) == 1:
            return _safe(node.args[0])
    if isinstance(node, ast.BinOp):
        return _safe(node.left) and _safe(node.right)
    if isinstance(node, ast.IfExp):
        return _safe(node.body) and _safe(node.orelse)
    if isinstance(node, ast.JoinedStr):
        return all(_safe(v.value) for v in node.values if isinstance(v, ast.FormattedValue))
    return ast.unparse(node) in SAFE_EXPRESSIONS


def _markup_arguments(tree: ast.AST):
    """Every argument Rich parses as markup: console.print(...), Table.add_row(...), Table(title=...)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ("print", "add_row"):
            yield from node.args
        elif isinstance(func, ast.Name) and func.id == "Table":
            yield from (k.value for k in node.keywords if k.arg == "title")


def test_cli_escapes_everything_it_interpolates_into_markup():
    cli_dir = Path(cli_main.__file__).parent
    unsafe = []
    for module in sorted(cli_dir.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for arg in _markup_arguments(tree):
            # A bare renderable (a Table) is not markup.
            if isinstance(arg, ast.Name) and arg.id in ("table", "nodes", "rels", "node_table", "rel_table"):
                continue
            if not _safe(arg):
                unsafe.append(f"{module.name}:{arg.lineno}: {ast.unparse(arg)}")
    assert not unsafe, "wrap these in escape() (or add a fixed value to SAFE_EXPRESSIONS):\n" + "\n".join(unsafe)
