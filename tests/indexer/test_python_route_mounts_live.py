"""FastAPI routers and Flask blueprints mounted from another file, against a
live Neo4j (apis/extractor.py, apis/routes.py MountTable): their routes are
named by the mounts reaching them, list_endpoints and trace_request_flow
find them by their full path, and an edit of either file alone equals a
fresh `full_scan`. Fixture code is fictional."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths
from devgraph.mcp.tools import explain_architecture, list_endpoints, trace_request_flow
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff


@pytest.fixture
def engine():
    test_engine = GraphEngine(
        uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev"
    )
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


@pytest.fixture
def repo_id(engine):
    repo = f"zz-pymounts-{uuid.uuid4().hex[:8]}"
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
        pytest.fail(
            "graph does not equal a fresh full_scan:\n"
            + snapshot_diff(expected, actual)
        )


def _scan(engine, repo_id, root):
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)


def _endpoints(engine, repo_id):
    return sorted(
        row["name"]
        for row in engine.run_cypher(
            "MATCH (e:Endpoint {repo_id: $r}) RETURN e.name AS name", {"r": repo_id}
        )
    )


ORDERS = """\
    from fastapi import APIRouter

    router = APIRouter(prefix="/orders")

    @router.get("/{order_id}")
    def get_order(order_id: int):
        pass

    @router.get("/")
    def list_orders():
        pass
"""

USERS = """\
    from fastapi import APIRouter

    router = APIRouter(prefix="/users")

    @router.get("/")
    def list_users():
        pass
"""

MAIN = """\
    from fastapi import FastAPI
    from .routers import orders
    from .routers.users import router as users_router

    app = FastAPI()
    app.include_router(orders.router, prefix="/api")
    app.include_router(users_router, prefix="/api")
"""


def test_include_router_across_files_and_its_edits(engine, repo_id, tmp_path):
    write(tmp_path, "compose.yaml", "services:\n  shop:\n    build: ./shop\n")
    write(tmp_path, "shop/__init__.py", "")
    orders = write(tmp_path, "shop/routers/orders.py", ORDERS)
    write(tmp_path, "shop/routers/users.py", USERS)
    main = write(tmp_path, "shop/main.py", MAIN)
    _scan(engine, repo_id, tmp_path)
    # Two routers' `GET /` stay apart, each under its full path.
    assert _endpoints(engine, repo_id) == [
        "GET /api/orders/",
        "GET /api/orders/{order_id}",
        "GET /api/users/",
    ]
    listed = list_endpoints(engine, repo_id, path_prefix="/api")["results"]
    assert [row["name"] for row in listed] == [
        "GET /api/orders/",
        "GET /api/orders/{order_id}",
        "GET /api/users/",
    ]
    assert all(row["mounted"] and row["services"] == ["shop"] for row in listed)
    flow = trace_request_flow(engine, repo_id, "GET /api/orders/{order_id}")
    assert {"name": "get_order", "labels": ["Function"], "repo_id": repo_id} in flow[
        "components"
    ]
    calls = explain_architecture(engine, repo_id)["endpoints"]["results"]
    assert sorted(calls[0]["endpoints"]) == [
        "GET /api/orders/",
        "GET /api/orders/{order_id}",
        "GET /api/users/",
    ]

    # The mount's prefix changes in the mounting file alone.
    write(
        tmp_path,
        "shop/main.py",
        MAIN.replace('orders.router, prefix="/api"', 'orders.router, prefix="/v2"'),
    )
    index_paths(engine, repo_id, tmp_path, {main})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert "GET /v2/orders/{order_id}" in _endpoints(engine, repo_id)

    # The router's own prefix changes in the router file alone.
    write(
        tmp_path, "shop/routers/orders.py", ORDERS.replace('"/orders"', '"/purchases"')
    )
    index_paths(engine, repo_id, tmp_path, {orders})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert "GET /v2/purchases/{order_id}" in _endpoints(engine, repo_id)

    # The mounting file goes: the routers' routes are their files' own again.
    main.unlink()
    remove_paths(engine, repo_id, tmp_path, {main})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert _endpoints(engine, repo_id) == [
        "GET ?/purchases/ (shop/routers/orders.py)",
        "GET ?/purchases/{order_id} (shop/routers/orders.py)",
        "GET ?/users/ (shop/routers/users.py)",
    ]

    # And comes back.
    write(tmp_path, "shop/main.py", MAIN)
    index_paths(engine, repo_id, tmp_path, {main})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert "GET /api/purchases/{order_id}" in _endpoints(engine, repo_id)


def test_nested_routers_across_three_files(engine, repo_id, tmp_path):
    write(tmp_path, "app/users.py", USERS)
    api = write(
        tmp_path,
        "app/api.py",
        """\
        from fastapi import APIRouter
        from .users import router as users_router

        api_router = APIRouter(prefix="/v1")
        api_router.include_router(users_router, prefix="/admin")
    """,
    )
    write(
        tmp_path,
        "app/main.py",
        """\
        from fastapi import FastAPI
        from . import api

        app = FastAPI()
        app.include_router(api.api_router, prefix="/api")
    """,
    )
    _scan(engine, repo_id, tmp_path)
    assert _endpoints(engine, repo_id) == ["GET /api/v1/admin/users/"]

    write(tmp_path, "app/api.py", api.read_text().replace('"/v1"', '"/v2"'))
    index_paths(engine, repo_id, tmp_path, {api})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert _endpoints(engine, repo_id) == ["GET /api/v2/admin/users/"]


def test_flask_blueprint_registered_from_another_file(engine, repo_id, tmp_path):
    shop = write(
        tmp_path,
        "web/shop.py",
        """\
        from flask import Blueprint

        bp = Blueprint("shop", __name__, url_prefix="/store")

        @bp.route("/cart", methods=["GET", "POST"])
        def cart():
            pass
    """,
    )
    app = write(
        tmp_path,
        "web/app.py",
        """\
        from flask import Flask
        from .shop import bp

        app = Flask(__name__)
        app.register_blueprint(bp)
    """,
    )
    _scan(engine, repo_id, tmp_path)
    assert _endpoints(engine, repo_id) == ["GET /store/cart", "POST /store/cart"]

    # url_prefix at registration replaces the blueprint's own.
    write(
        tmp_path,
        "web/app.py",
        app.read_text().replace(
            "register_blueprint(bp)", 'register_blueprint(bp, url_prefix="/s")'
        ),
    )
    index_paths(engine, repo_id, tmp_path, {app})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert _endpoints(engine, repo_id) == ["GET /s/cart", "POST /s/cart"]

    write(tmp_path, "web/shop.py", shop.read_text().replace('"/cart"', '"/basket"'))
    index_paths(engine, repo_id, tmp_path, {shop})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert _endpoints(engine, repo_id) == ["GET /s/basket", "POST /s/basket"]
