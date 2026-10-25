"""list_endpoints against a live Neo4j: a scanned repository's routes, of
every framework, with their handlers and owning compose service, filtered
by method, path prefix, framework and service. Fixture code is fictional."""

import textwrap
import uuid

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan
from devgraph.mcp.tools import list_endpoints


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


FILES = {
    "compose.yaml": """\
        services:
          shop:
            build: ./shop
          admin:
            build: ./admin
        """,
    "shop/app.ts": """\
        import express from 'express';
        import { listOrders } from './orders';
        const app = express();
        app.get('/api/orders', listOrders);
        app.post('/api/orders/:id/cancel', cancelOrder);
        app.get('/health', (req, res) => res.end());
        function cancelOrder(req, res) {}
        """,
    "shop/orders.ts": "export function listOrders(req, res) {}\n",
    "shop/loose.ts": """\
        import { Router } from 'express';
        const looseRouter = Router();
        looseRouter.get('/:id', show);
        function show(req, res) {}
        """,
    "admin/main.py": """\
        from fastapi import APIRouter
        router = APIRouter()

        @router.get("/api/users")
        def list_users():
            return []
        """,
    "admin/urls.py": """\
        from django.urls import path

        def show(request, id):
            return None

        urlpatterns = [path('users/<id>/', show)]
        """,
}


@pytest.fixture
def repo(engine, tmp_path):
    repo_id = f"zz-endpoints-{uuid.uuid4().hex[:8]}"
    for rel, text in FILES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    yield repo_id
    engine.delete_repository(repo_id)


def names(result):
    return [row["name"] for row in result["results"]]


def test_lists_every_route_with_its_handler_and_service(engine, repo):
    result = list_endpoints(engine, repo, max_results=50)
    assert result["truncated"] is False
    assert names(result) == [
        "* users/<id>/", "GET /api/orders", "GET /api/users", "GET /health", "GET ?/:id (shop/loose.ts)",
        "POST /api/orders/:id/cancel",
    ]
    by_name = {row["name"]: row for row in result["results"]}
    assert by_name["GET /api/orders"] == {
        "name": "GET /api/orders", "method": "GET", "path": "/api/orders", "mounted": True, "framework": "Express",
        "files": ["shop/app.ts"],
        "handlers": [{"label": "Function", "name": "listOrders", "file": "shop/orders.ts", "owner": ""}],
        "services": ["shop"], "repo_id": repo,
    }
    assert by_name["GET /api/users"]["framework"] == "FastAPI"
    assert by_name["GET /api/users"]["services"] == ["admin"]
    # A Django pattern's path gains its leading "/".
    assert (by_name["* users/<id>/"]["method"], by_name["* users/<id>/"]["path"]) == ("*", "/users/<id>/")
    # An inline handler implements nothing.
    assert by_name["GET /health"]["handlers"] == []
    loose = by_name["GET ?/:id (shop/loose.ts)"]
    assert (loose["method"], loose["path"], loose["mounted"]) == ("GET", "/:id", False)
    assert loose["handlers"] == [{"label": "Function", "name": "show", "file": "shop/loose.ts", "owner": ""}]


def test_filters(engine, repo):
    assert names(list_endpoints(engine, repo, method="post")) == ["POST /api/orders/:id/cancel"]
    assert names(list_endpoints(engine, repo, path_prefix="/api/orders")) == [
        "GET /api/orders", "POST /api/orders/:id/cancel",
    ]
    # Whole segments only, and an unmounted route never matches a prefix.
    assert names(list_endpoints(engine, repo, path_prefix="/api/ord")) == []
    assert "GET ?/:id (shop/loose.ts)" not in names(list_endpoints(engine, repo, path_prefix="/"))
    assert names(list_endpoints(engine, repo, framework="fastapi")) == ["GET /api/users"]
    assert names(list_endpoints(engine, repo, service="admin")) == ["* users/<id>/", "GET /api/users"]
    assert names(list_endpoints(engine, repo, path_prefix="/users")) == ["* users/<id>/"]
    assert names(list_endpoints(engine, repo, method="*")) == ["* users/<id>/"]
    # Any HTTP method token is a filter, one no route has an empty one.
    assert list_endpoints(engine, repo, method="propfind")["count"] == 0
    capped = list_endpoints(engine, repo, max_results=2)
    assert (capped["count"], len(capped["results"]), capped["truncated"]) == (6, 2, True)


def test_mistakes_are_errors(engine, repo):
    with pytest.raises(ToolError, match="method must be an HTTP method"):
        list_endpoints(engine, repo, method="GET /x")
    with pytest.raises(ToolError, match="framework must be one of"):
        list_endpoints(engine, repo, framework="Rails")
    with pytest.raises(ToolError, match="no service named 'shopp'"):
        list_endpoints(engine, repo, service="shopp")
    with pytest.raises(ToolError, match="max_results must be at least 1"):
        list_endpoints(engine, repo, max_results=0)


def test_cross_repo_lists_this_repository_first(engine, repo):
    result = list_endpoints(engine, repo, cross_repo=True, max_results=5)
    assert [row["repo_id"] for row in result["results"]] == [repo] * 5


def test_a_caller_without_a_file_keeps_its_file_key(engine, repo):
    """An Endpoint calling its Service has no file: its find_callers row says so, as null."""
    from devgraph.mcp.tools import find_callers

    rows = find_callers(engine, repo, "shop", max_results=50)["results"]
    assert rows and all(row["file"] is None and "owner" not in row for row in rows)
