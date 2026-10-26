"""The endpoints a change reaches, capped in the query (audit 9 review): a
helper under many routes reads only the `max_results` endpoints a tool shows,
however many there are, and still counts them all. Fixture code is fictional."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan
from devgraph.mcp import tools


@pytest.fixture(scope="module")
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


def _routes(prefix, count, body):
    return "".join(f'@app.get("/{prefix}/{i}")\ndef {prefix}{i}():\n    return {body}\n\n\n' for i in range(count))


HEAD = "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n"
FILES = {
    # Twelve routes over a.py's helper, two over b.py's.
    "a.py": HEAD + _routes("a", 12, "helper()") + "def helper():\n    return 1\n",
    "b.py": HEAD + _routes("b", 2, "helper()") + "def helper():\n    return 2\n",
    # One handler under eight routes, and a same-named one under another.
    "s.py": HEAD + "".join(f'@app.get("/s/{i}")\n' for i in range(8)) + "def shared():\n    return 1\n",
    "t.py": HEAD + '@app.get("/t/0")\ndef shared():\n    return 2\n',
}


@pytest.fixture(scope="module")
def repo_id(engine, tmp_path_factory):
    root = tmp_path_factory.mktemp("endpoint-caps")
    repo_id = f"zz-endpoint-caps-{uuid.uuid4().hex[:8]}"
    for rel, text in FILES.items():
        (root / rel).write_text(textwrap.dedent(text))
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)
    yield repo_id
    engine.delete_repository(repo_id)


@pytest.fixture
def read_rows(monkeypatch):
    """Every row each built-in query returned, as read from Neo4j."""
    seen = []
    query = tools._query

    def recording(engine, cypher, params):
        rows = query(engine, cypher, params)
        seen.append(rows)
        return rows

    monkeypatch.setattr(tools, "_query", recording)
    return seen


def _shipped(read_rows, key):
    return max(len(row[key]) for rows in read_rows for row in rows if key in row)


def test_impact_analysis_reads_only_the_endpoints_it_shows(engine, repo_id, read_rows):
    result = tools.impact_analysis(engine, repo_id, "helper", file="a.py", max_results=5)
    endpoints = result["endpoints"]
    assert endpoints["count"] == 12 and endpoints["truncated"] is True
    assert [row["name"] for row in endpoints["results"]] == sorted(f"GET /a/{i}" for i in range(12))[:5]
    assert _shipped(read_rows, "endpoints") == 5


def test_split_targets_count_their_endpoints_together(engine, repo_id, read_rows):
    result = tools.impact_analysis(engine, repo_id, "helper", max_results=5)
    assert result["ambiguous_targets"]["count"] == 2
    assert result["endpoints"]["count"] == 14 and len(result["endpoints"]["results"]) == 5
    assert _shipped(read_rows, "endpoints") == 5


def test_find_callers_reads_only_the_routes_it_shows(engine, repo_id, read_rows):
    routed_by = tools.find_callers(engine, repo_id, "shared", max_results=3)["routed_by"]
    assert routed_by["count"] == 9 and routed_by["truncated"] is True
    assert [row["name"] for row in routed_by["results"]] == ["GET /s/0", "GET /s/1", "GET /s/2"]
    assert _shipped(read_rows, "endpoints") == 3


def test_split_targets_tag_each_endpoint_with_its_targets_files(engine, repo_id):
    result = tools.impact_analysis(engine, repo_id, "helper", max_results=20)
    tags = {row["name"]: row["target_files"] for row in result["endpoints"]["results"]}
    assert tags == {
        **{f"GET /a/{i}": ["a.py"] for i in range(12)}, **{f"GET /b/{i}": ["b.py"] for i in range(2)},
    }
    # One target: nothing to tell apart.
    single = tools.impact_analysis(engine, repo_id, "helper", file="a.py")["endpoints"]["results"]
    assert not any("target_files" in row for row in single)


def test_find_callers_tags_routes_when_targets_are_ambiguous(engine, repo_id):
    routed_by = tools.find_callers(engine, repo_id, "shared", max_results=20)["routed_by"]["results"]
    assert {row["name"]: row["target_files"] for row in routed_by} == {
        **{f"GET /s/{i}": ["s.py"] for i in range(8)}, "GET /t/0": ["t.py"],
    }
    one = tools.find_callers(engine, repo_id, "shared", file="t.py")["routed_by"]["results"]
    assert [row["name"] for row in one] == ["GET /t/0"] and "target_files" not in one[0]
