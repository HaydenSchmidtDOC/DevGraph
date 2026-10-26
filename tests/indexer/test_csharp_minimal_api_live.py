"""C# minimal-API handlers against a live Neo4j
(devgraph/indexer/csharp/extractor.py `_aspnet_routes`): a handler in
another class and file, and a top-level `Program.cs` local function, are
what the route IMPLEMENTS, and moving or editing the handler's file equals
a fresh `full_scan`. Fixture code is fictional."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff


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
    repo = f"zz-csmin-{uuid.uuid4().hex[:8]}"
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)
    yield repo
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def incremental_equals_fresh(engine, repo_id, root):
    expected = fresh_snapshot(engine, repo_id, root)
    actual = graph_snapshot(engine, repo_id)
    if actual != expected:
        pytest.fail("graph does not equal a fresh full_scan:\n" + snapshot_diff(expected, actual))


def _implements(engine, repo_id):
    return {
        (row["e"], row["f"], row["file"], row["owner"])
        for row in engine.run_cypher(
            "MATCH (e:Endpoint {repo_id: $r})-[:IMPLEMENTS]->(f:Function) WHERE f.file IS NOT NULL "
            "RETURN e.name AS e, f.name AS f, f.file AS file, f.owner AS owner",
            {"r": repo_id},
        )
    }


PROGRAM = """\
using Todo.Api;

var app = WebApplication.CreateBuilder(args).Build();
app.MapGet("/todos", GetTodos);
app.MapGet("/todos/{id}", TodoHandlers.GetById);
app.Run();

static IResult GetTodos() => Results.Ok();
"""
HANDLERS = """\
namespace Todo.Api;

public static class TodoHandlers
{
    public static IResult GetById(int id) => Results.Ok(id);
}
"""


def test_minimal_api_handlers_across_files(engine, repo_id, tmp_path):
    write(tmp_path, "src/Api/Program.cs", PROGRAM)
    handlers = write(tmp_path, "src/Api/TodoHandlers.cs", HANDLERS)
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert _implements(engine, repo_id) == {
        ("GET /todos", "GetTodos", "src/Api/Program.cs", ""),
        ("GET /todos/{id}", "GetById", "src/Api/TodoHandlers.cs", "TodoHandlers"),
    }

    # The handler class's file renamed: delete the old path, index the new.
    moved = write(tmp_path, "src/Api/Handlers/Todos.cs", HANDLERS)
    handlers.unlink()
    remove_paths(engine, repo_id, tmp_path, {handlers})
    index_paths(engine, repo_id, tmp_path, {moved})
    assert ("GET /todos/{id}", "GetById", "src/Api/Handlers/Todos.cs", "TodoHandlers") in _implements(engine, repo_id)
    incremental_equals_fresh(engine, repo_id, tmp_path)

    # Another namespace: the route's keys no longer reach it.
    write(tmp_path, "src/Api/Handlers/Todos.cs", HANDLERS.replace("Todo.Api", "Todo.Other"))
    index_paths(engine, repo_id, tmp_path, {moved})
    assert not [row for row in _implements(engine, repo_id) if row[1] == "GetById"]
    incremental_equals_fresh(engine, repo_id, tmp_path)
