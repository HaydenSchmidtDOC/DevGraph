"""The compare_branches MCP tool: response shape, errors, the callers query and the server wiring.

Git runs for real on temporary repositories; the graph is a stub engine that records every read.
"""

import asyncio
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from neo4j.exceptions import ClientError, ServiceUnavailable

from devgraph.config.project_tools import DEFAULT_TIMEOUT_S
from devgraph.config.settings import Settings
from devgraph.indexer.git_history import compare
from devgraph.mcp import server as mcp_server
from devgraph.mcp.catalog import TOOL_CATALOG
from devgraph.mcp.tools import _COMPARE_CALLERS_CYPHER, compare_branches
from devgraph.registry.store import RepoRegistry
from tests.indexer.git_compare_helpers import git, two_branch_repo

LAST_INDEX = "impacted_callers come from the last index of the working tree, not from either ref"
SHALLOW = "; this is a shallow clone, so older commits may be missing: git fetch --unshallow"
EMPTY = {"count": 0, "results": [], "truncated": False}
KEYS = {
    "base", "head", "merge_base", "counts", "files", "symbol_counts",
    "impacted_callers", "truncated", "truncated_reasons", "notices",
}

BASE_M = "def keep():\n    return 1\n\ndef edit():\n    return 1\n\ndef gone():\n    return 1\n"
HEAD_M = "def keep():\n    return 1\n\ndef edit():\n    return 2\n\ndef fresh():\n    return 1\n"


class StubEngine:
    """Returns the canned `(rows, more)` responses in call order, or raises `error`; records every call."""

    def __init__(self, *responses, error=None):
        self.responses = list(responses)
        self.calls = []
        self.error = error

    def run_read_cypher(self, query, params, *, timeout_s, max_rows):
        self.calls.append({"query": query, "params": params, "timeout_s": timeout_s, "max_rows": max_rows})
        if self.error is not None:
            raise self.error
        return self.responses.pop(0) if self.responses else ([], False)

    def run_cypher(self, query, params=None):
        raise AssertionError("compare_branches must only read through run_read_cypher")


def registered(tmp_path, repo, repo_id="demo"):
    registry = RepoRegistry(tmp_path / "r.db")
    registry.add_repo(repo, repo_id)
    return registry


def run(tmp_path, base, branch, engine=None, **kwargs):
    repo = two_branch_repo(tmp_path, base, branch, **kwargs)
    engine = engine if engine is not None else StubEngine()
    return compare_branches(engine, registered(tmp_path, repo), "demo", "main", "feature"), engine, repo


def caller_row(i):
    return {"caller": f"c{i:02d}", "caller_type": "Function", "caller_file": "u.py", "calls": "edit", "calls_file": "m.py"}


def pairs(engine):
    (call,) = engine.calls
    return sorted((t["name"], t["file"]) for t in call["params"]["targets"])


def test_response_shape(tmp_path):
    result, _, repo = run(
        tmp_path,
        {"a.py": "def f():\n    return 1\n", "doc.md": "# d\n"},
        {"a.py": "def f():\n    return 2\n\ndef g():\n    return 1\n", "doc.md": None, "new.py": "class C:\n    pass\n"},
    )
    assert set(result) == KEYS
    assert result["base"] == {"ref": "main", "commit": git(repo, "rev-parse", "main")}
    assert result["head"] == {"ref": "feature", "commit": git(repo, "rev-parse", "feature")}
    assert result["merge_base"] == git(repo, "rev-parse", "main")
    assert result["counts"] == {"added": 1, "removed": 1, "modified": 1, "renamed": 0}
    files = result["files"]
    assert set(files) == {"count", "results", "truncated"}
    assert files["count"] == sum(result["counts"].values()) == 3
    assert files["truncated"] is False
    by_path = {f["path"]: f for f in files["results"]}
    assert [f["path"] for f in files["results"]] == ["a.py", "new.py", "doc.md"]  # code first
    assert by_path["a.py"] == {
        "path": "a.py", "status": "modified", "language": "py",
        "symbols": {
            "added": [{"kind": "Function", "name": "g", "container": None, "start_line": 4, "end_line": 5}],
            "removed": [],
            "changed": [{"kind": "Function", "name": "f", "container": None, "start_line": 1, "end_line": 2,
                         "old_start_line": 1, "old_end_line": 2}],
        },
    }
    assert by_path["doc.md"] == {
        "path": "doc.md", "status": "removed", "language": None, "symbols": None,
        "symbols_skipped": "unsupported_language",
    }
    listed = {k: sum(len(f["symbols"][k]) for f in files["results"] if f["symbols"]) for k in ("added", "removed", "changed")}
    assert result["symbol_counts"] == listed == {"added": 2, "removed": 0, "changed": 1}
    assert result["impacted_callers"] == EMPTY
    assert result["truncated"] is False and result["truncated_reasons"] == []
    assert result["notices"] == [LAST_INDEX]


def test_rename_carries_old_path(tmp_path):
    result, _, _ = run(tmp_path, {"x/old.py": "def k():\n    return 1\n"}, {"x/old.py": None, "y/new.py": "def k():\n    return 1\n"})
    (entry,) = result["files"]["results"]
    assert entry == {
        "path": "y/new.py", "status": "renamed", "old_path": "x/old.py", "language": "py",
        "symbols": {"added": [], "removed": [], "changed": []},
    }
    assert result["counts"]["renamed"] == 1


def _shallow(tmp_path, src):
    dst = Path(tmp_path, "shallow")
    git(tmp_path, "clone", "-q", "--depth", "1", "--branch", "feature", f"file://{src}", str(dst))
    return dst


def test_errors_are_tool_errors(tmp_path):
    repo = two_branch_repo(tmp_path, {"a.py": "a = 1\n"}, {"b.py": "b = 1\n"})
    registry = registered(tmp_path, repo)
    not_git = tmp_path / "plain"
    (not_git / ".git").mkdir(parents=True)  # passes registration, but is no repository
    registry.add_repo(not_git, "plain")
    registry.add_repo(_shallow(tmp_path, repo), "shallow")
    cases = [
        ("nope", "main", "feature",
         "no such repo_id: 'nope'; run devgraph list to see registered repositories"),
        ("plain", "main", "feature",
         "repository 'plain' is not a git repository at its registered root; "
         "compare_branches needs the repository's own .git"),
        ("demo", "-h", "feature", "branch_a '-h' is not a valid ref: it starts with '-'"),
        ("demo", "main", "nosuch",
         "branch_b 'nosuch' is not a branch, tag or commit in repository 'demo'; "
         "refs must exist locally (DevGraph never fetches)"),
        ("shallow", "main", "feature",
         "branch_a 'main' is not a branch, tag or commit in repository 'shallow'; "
         "refs must exist locally (DevGraph never fetches)" + SHALLOW),
    ]
    engine = StubEngine()
    for repo_id, a, b, message in cases:
        with pytest.raises(Exception) as err:
            compare_branches(engine, registry, repo_id, a, b)
        assert type(err.value) is ToolError, (repo_id, a, b, err.value)
        assert str(err.value) == message
    assert engine.calls == []


def test_callers_query_targets_changed_and_removed_only(tmp_path):
    rows = [caller_row(1), caller_row(2)]
    result, engine, _ = run(tmp_path, {"m.py": BASE_M}, {"m.py": HEAD_M}, engine=StubEngine((rows, False)))
    assert pairs(engine) == [("edit", "m.py"), ("gone", "m.py")]
    (call,) = engine.calls
    assert call["query"] == _COMPARE_CALLERS_CYPHER
    assert call["params"]["repo_id"] == "demo"
    assert set(call["params"]) == {"repo_id", "targets"}
    assert call["max_rows"] == 26 and call["timeout_s"] == DEFAULT_TIMEOUT_S
    assert result["impacted_callers"] == {"count": 2, "results": rows, "truncated": False}
    assert LAST_INDEX in result["notices"]


def test_no_targets_no_query(tmp_path):
    result, engine, _ = run(tmp_path, {"a.py": "a = 1\n"}, {"n.py": "def fresh():\n    return 1\n"})
    assert result["symbol_counts"]["added"] == 1
    assert engine.calls == []
    assert result["impacted_callers"] == EMPTY
    assert result["notices"] == []


@pytest.mark.parametrize(
    "response, count, shown, truncated",
    [(([caller_row(i) for i in range(26)], True), 26, 25, True), (([caller_row(i) for i in range(3)], False), 3, 3, False)],
)
def test_callers_capped(tmp_path, response, count, shown, truncated):
    result, _, _ = run(tmp_path, {"m.py": BASE_M}, {"m.py": HEAD_M}, engine=StubEngine(response))
    callers = result["impacted_callers"]
    assert callers["truncated"] is truncated
    assert callers["count"] == count
    assert callers["results"] == response[0][:shown]


def test_callers_cover_detailed_files_only(tmp_path, monkeypatch):
    monkeypatch.setattr(compare, "_COMPARE_MAX_FILES", 1)
    result, engine, _ = run(
        tmp_path,
        {"a.py": "def fa():\n    return 1\n", "b.py": "def fb():\n    return 1\n"},
        {"a.py": "def fa():\n    return 2\n", "b.py": "def fb():\n    return 2\n"},
    )
    assert pairs(engine) == [("fa", "a.py")]
    assert result["files"]["count"] == 2 and len(result["files"]["results"]) == 1
    assert result["files"]["truncated"] is True
    assert result["truncated_reasons"] == ["files"] and result["truncated"] is True
    assert result["symbol_counts"] == {"added": 0, "removed": 0, "changed": 1}


@pytest.mark.parametrize(
    "error, code",
    [
        (ServiceUnavailable("down"), "ServiceUnavailable"),
        (ClientError._hydrate_neo4j(code="Neo.ClientError.Transaction.TransactionTimedOut", message="slow"),
         "Neo.ClientError.Transaction.TransactionTimedOut"),
    ],
)
def test_graph_down_keeps_the_git_answer(tmp_path, error, code):
    result, engine, _ = run(tmp_path, {"m.py": BASE_M}, {"m.py": HEAD_M}, engine=StubEngine(error=error))
    assert len(engine.calls) == 1
    (entry,) = result["files"]["results"]
    assert entry["symbols"]["changed"][0]["name"] == "edit"
    assert result["symbol_counts"] == {"added": 1, "removed": 1, "changed": 1}
    assert result["impacted_callers"] is None
    assert result["notices"] == [f"impacted callers unavailable: {code}"]


def test_hostile_strings_are_sanitised(tmp_path):
    long_name = "f" * 600
    repo = two_branch_repo(tmp_path, {"a.py": f"def {long_name}():\n    return 1\n"}, {"a.py": f"def {long_name}():\n    return 2\n"})
    git(repo, "checkout", "-q", "feature")
    src = Path(tmp_path, "evil-src.py")
    src.write_bytes(b"def bad():\n    return 1\n")
    blob = git(repo, "hash-object", "-w", str(src))
    # Git for Windows refuses control characters in paths unless core.protectNTFS is off.
    git(repo, "-c", "core.protectNTFS=false", "update-index", "--add", "--cacheinfo", f"100644,{blob},evil\x07.py")
    git(repo, "commit", "-q", "-m", "evil")
    git(repo, "-c", "core.protectNTFS=false", "checkout", "-q", "main")
    hostile = {**caller_row(0), "caller": "x\x1b[2J" + "y" * 600}
    engine = StubEngine(([hostile], False))
    result = compare_branches(engine, registered(tmp_path, repo), "demo", "main", "feature")
    paths = [f["path"] for f in result["files"]["results"]]
    assert paths == ["a.py", "evil.py"]
    changed = result["files"]["results"][0]["symbols"]["changed"][0]
    assert changed["name"] == "f" * 500
    assert pairs(engine) == [(long_name, "a.py")]  # the query gets the raw name, the response the clean one
    (caller,) = result["impacted_callers"]["results"]
    assert caller["caller"] == "x[2J" + "y" * 496


# ── the server ─────────────────────────────────────────────────────────────


def test_mcp_call_defaults_repo_and_keeps_signature(tmp_path, monkeypatch):
    settings = Settings(registry_db_path=tmp_path / "s.sqlite3", enable_run_cypher=True)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    repo = two_branch_repo(tmp_path, {"m.py": BASE_M}, {"m.py": HEAD_M})
    registry = registered(tmp_path, repo)
    server = mcp_server.build_server(StubEngine(), registry, session_repo=registry.get("demo"), session_source="env")
    result = asyncio.run(server.call_tool("compare_branches", {"branch_a": "main", "branch_b": "feature"}))
    assert result.is_error is False
    content = result.structured_content
    assert content["repo_id"] == "demo"
    assert content["notices"] == [
        LAST_INDEX, "repo_id not given; used this session's repository 'demo' (from DEVGRAPH_MCP_REPO)"
    ]
    assert content["symbol_counts"] == {"added": 1, "removed": 1, "changed": 1}
    (tool,) = [t for t in asyncio.run(server.list_tools()) if t.name == "compare_branches"]
    schema = tool.input_schema if hasattr(tool, "input_schema") else tool.inputSchema
    assert list(schema["properties"]) == ["repo_id", "branch_a", "branch_b"]
    assert "Stub" not in (tool.description or "") and "Phase 3" not in (tool.description or "")


def test_catalog_entry():
    (entry,) = [e for e in TOOL_CATALOG if e["name"] == "compare_branches"]
    assert entry == {
        "name": "compare_branches",
        "identifier_kind": "two local git refs (branch_a = base, branch_b = head; compared from their merge base, like git diff a...b)",
        "envelope": False,
        "phase": 3,
        "note": "the response is not an envelope; files and impacted_callers inside it are {count, results, truncated}",
    }
    assert not [e for e in TOOL_CATALOG if "stub" in e.get("note", "")]
