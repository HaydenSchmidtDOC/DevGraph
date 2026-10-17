"""Deeply nested code is indexed, not dropped (audit 6, item 1).

A long `else if` chain or `a + b + ...` expression is one tree level per
branch or term, and the extractors walk trees recursively: 500 branches or
1,000 terms used to raise RecursionError, so the file was skipped and a live
batch kept the file's old nodes while a fresh scan wrote none. Such a file is
now extracted again with a large stack (`dispatch._run_deep`); one deeper
than that allows is left out and reported, live and fresh alike.
"""

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer import dispatch
from devgraph.indexer.cpp.extractor import extract_cpp_file
from devgraph.indexer.csharp.extractor import extract_csharp_file
from devgraph.indexer.dispatch import TOO_DEEP, full_scan, index_paths
from devgraph.indexer.go.extractor import extract_go_file
from devgraph.indexer.java.extractor import extract_java_file
from devgraph.indexer.jsts.extractor import extract_js_file
from devgraph.indexer.kotlin.extractor import extract_kotlin_file
from devgraph.indexer.python.extractor import extract_python_file
from devgraph.indexer.rust.extractor import extract_rust_file
from tests.indexer.deep_sources import DEEP_FUNCTIONS, DEEP_SOURCES
from tests.watcher.live_helpers import (
    fresh_snapshot,
    graph_snapshot,
    live_repo_id,
    snapshot_diff,
    sweep_stale_live_repos,
)

TERMS, BRANCHES = 1000, 500


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


@pytest.fixture
def repo_id(engine):
    sweep_stale_live_repos(engine, "zz-deep")
    repo = live_repo_id("zz-deep")
    yield repo
    engine.delete_repository(repo)


def functions(engine, repo_id):
    rows = engine.run_cypher("MATCH (f:Function {repo_id: $r}) RETURN f.file AS file, f.name AS name", {"r": repo_id})
    found: dict[str, set[str]] = {}
    for row in rows:
        found.setdefault(row["file"], set()).add(row["name"])
    return found


def assert_fresh(engine, repo_id, root):
    expected = fresh_snapshot(engine, repo_id, root)
    actual = graph_snapshot(engine, repo_id)
    assert actual == expected, snapshot_diff(expected, actual)


@pytest.mark.parametrize("name", sorted(DEEP_SOURCES))
def test_run_deep_extracts_every_function_of_a_deep_file(name):
    """The extractors themselves, in every language, through `_run_deep`."""
    extract = {
        "py": extract_python_file, "js": extract_js_file, "ts": extract_js_file, "java": extract_java_file,
        "cs": extract_csharp_file, "cpp": extract_cpp_file, "go": extract_go_file, "rs": extract_rust_file,
        "kt": extract_kotlin_file,
    }[name.rsplit(".", 1)[1]]
    for terms, branches in ((TERMS, 2), (2, BRANCHES)):
        source = DEEP_SOURCES[name](terms, branches)
        result = dispatch._run_deep(lambda: extract(source, name, "deep-unit"))
        assert DEEP_FUNCTIONS[name] <= {n.name for n in result.nodes if n.label == "Function"}


def test_deep_files_are_indexed_in_every_language_live_and_fresh(engine, repo_id, tmp_path):
    for name in DEEP_SOURCES:
        (tmp_path / name).write_text(DEEP_SOURCES[name](2, 2))
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    # Live: every file turns deep in one batch.
    for name in DEEP_SOURCES:
        (tmp_path / name).write_text(DEEP_SOURCES[name](TERMS, BRANCHES))
    skipped: dict[str, str] = {}
    index_paths(engine, repo_id, tmp_path, {tmp_path / name for name in DEEP_SOURCES}, skipped=skipped)
    assert skipped == {}
    found = functions(engine, repo_id)
    for name, expected in DEEP_FUNCTIONS.items():
        assert expected <= found.get(name, set()), name
    assert_fresh(engine, repo_id, tmp_path)


def test_a_file_too_deep_even_for_the_retry_is_left_out_live_and_fresh(engine, repo_id, tmp_path, monkeypatch):
    """With no headroom for the retry, the deep file is skipped and reported,
    and its old nodes go: the graph a fresh scan makes, which has none."""
    (tmp_path / "deep.py").write_text("def keep_me():\n    return 1\n")
    (tmp_path / "other.py").write_text("def other():\n    return 2\n")
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert functions(engine, repo_id)["deep.py"] == {"keep_me"}

    monkeypatch.setattr(dispatch, "_DEEP_RECURSION_LIMIT", 0)  # the retry gets no more than the first try
    (tmp_path / "deep.py").write_text(DEEP_SOURCES["deep.py"](TERMS, 2))
    skipped: dict[str, str] = {}
    index_paths(engine, repo_id, tmp_path, {tmp_path / "deep.py"}, skipped=skipped)
    assert skipped == {"deep.py": TOO_DEEP}
    assert "deep.py" not in functions(engine, repo_id)
    assert_fresh(engine, repo_id, tmp_path)

    rescanned: dict[str, str] = {}
    full_scan(engine, repo_id, tmp_path, skipped=rescanned)
    assert rescanned == {"deep.py": TOO_DEEP}
    assert functions(engine, repo_id) == {"other.py": {"other"}}
