"""`devgraph export` output is usable as written (audit 10, item 5).

- To stdout, the text is written as is: Rich's console folded lines at 80
  columns when stdout is not a terminal, splitting JSON and Cypher strings.
- DOT node ids are quoted: Neo4j element ids (`4:<uuid>:12`) are no bare ids.
- Edges are not capped at the node limit, and a node cap that cuts the
  export says so on stderr; `--limit 0` exports every node.
"""

import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from devgraph import config as config_module
from devgraph.cli import main as cli_main
from devgraph.cli.main import app
from devgraph.graph.engine import GraphEngine
from devgraph.registry.store import RepoRegistry

LONG_PATH = "src/very_long_package_name_for_testing/another_quite_long_subpackage_name/module_file.py"
FUNCTIONS = ["alpha", "beta", "gamma", "delta"]


@pytest.fixture
def runner(monkeypatch):
    # What a redirected stdout gives Rich: 80 columns (tests/conftest.py
    # widens the console to 1000 for every other test).
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setattr(cli_main.console, "_width", 80)
    return CliRunner()


@pytest.fixture
def repo_id(tmp_path, monkeypatch):
    engine = GraphEngine("bolt://127.0.0.1:7687", "neo4j", "devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    engine.init_schema()

    settings = MagicMock()
    settings.registry_db_path = tmp_path / "registry.db"
    settings.neo4j_uri = "bolt://127.0.0.1:7687"
    settings.neo4j_user = "neo4j"
    settings.neo4j_password = "devgraph-local-dev"
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)

    rid = f"zz-a10c-export-{uuid.uuid4().hex[:8]}"
    (tmp_path / rid / ".git").mkdir(parents=True)
    registry = RepoRegistry(settings.registry_db_path)
    try:
        assert registry.add_repo(tmp_path / rid).repo_id == rid
    finally:
        registry.close()

    # 6 nodes (Repository, Module, 4 Functions) and 16 edges: 4 CONTAINS and
    # every Function calling every other.
    engine.upsert_repository(rid, rid, str(tmp_path / rid))
    engine.upsert_node("Module", rid, LONG_PATH, {"source_file": LONG_PATH})
    for name in FUNCTIONS:
        engine.upsert_node("Function", rid, name, {"file": LONG_PATH})
        engine.upsert_relationship("Module", LONG_PATH, "CONTAINS", "Function", name, rid)
    for a in FUNCTIONS:
        for b in FUNCTIONS:
            if a != b:
                engine.upsert_relationship("Function", a, "CALLS", "Function", b, rid)
    yield rid
    engine.delete_repository(rid)
    engine.close()


def _export(runner, repo_id, *args):
    result = runner.invoke(app, ["export", repo_id, *args])
    assert result.exit_code == 0, result.output
    return result


def test_json_to_stdout_parses_with_a_path_longer_than_80_columns(runner, repo_id):
    data = json.loads(_export(runner, repo_id).stdout)
    assert LONG_PATH in {n["name"] for n in data["nodes"]}


def test_cypher_to_stdout_keeps_one_statement_per_line(runner, repo_id):
    lines = _export(runner, repo_id, "--format", "cypher").stdout.splitlines()
    assert lines and all(re.fullmatch(r"(CREATE|MATCH) .*;", line) for line in lines), lines
    assert any(f'name: "{LONG_PATH}"' in line for line in lines)


def test_dot_output_parses(runner, repo_id, tmp_path):
    out = _export(runner, repo_id, "--format", "dot").stdout
    dot = shutil.which("dot")
    if dot is None:
        assert re.search(r'^  "[^"]+" -> "[^"]+" \[label="CALLS"\];$', out, re.MULTILINE), out
        return
    res = subprocess.run([dot, "-Tplain"], input=out, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert sum(line.startswith("edge ") for line in res.stdout.splitlines()) == 16


def test_edges_are_not_capped_at_the_node_limit(runner, repo_id):
    data = json.loads(_export(runner, repo_id, "--limit", "10").stdout)
    assert len(data["nodes"]) == 6
    assert len(data["edges"]) == 16


def test_a_node_cap_that_cuts_the_export_says_so_on_stderr(runner, repo_id):
    result = _export(runner, repo_id, "--limit", "2")
    assert len(json.loads(result.stdout)["nodes"]) == 2
    assert "2 of 6 nodes" in " ".join(result.stderr.split())


def test_a_complete_export_prints_no_notice(runner, repo_id):
    assert _export(runner, repo_id, "--limit", "6").stderr == ""


def test_limit_zero_exports_every_node(runner, repo_id):
    data = json.loads(_export(runner, repo_id, "--limit", "0").stdout)
    assert len(data["nodes"]) == 6 and len(data["edges"]) == 16


def test_output_file_reports_a_cut_export_too(runner, repo_id, tmp_path):
    target = tmp_path / "out.json"
    result = _export(runner, repo_id, "--limit", "2", "--output", str(target))
    assert len(json.loads(Path(target).read_text(encoding="utf-8"))["nodes"]) == 2
    assert "2 of 6 nodes" in " ".join(result.stderr.split())
