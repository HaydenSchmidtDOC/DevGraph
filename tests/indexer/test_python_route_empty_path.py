"""An unprefixed FastAPI router's `""` and `"/"` routes stay apart when
another file mounts it (apis/extractor.py, apis/routes.py `mount_routes`):
`GET /items` and `GET /items/` each implement their own handler alone, and
editing either file equals a fresh `full_scan`. Fixture code is fictional."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.apis.extractor import APIExtractor
from devgraph.indexer.dispatch import full_scan, index_paths
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff

ITEMS = """\
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("")
    def list_items():
        pass

    @router.get("/")
    def list_items_slash():
        pass
"""

MAIN = """\
    from fastapi import FastAPI
    from shop.items import router

    app = FastAPI()
    app.include_router(router, prefix="/items")
"""


def test_an_unprefixed_routers_empty_and_slash_routes_are_two_endpoints():
    result = APIExtractor("repo").extract_from_source(textwrap.dedent(ITEMS), "shop/items.py")
    assert sorted(e.name for e in result.endpoints) == ["GET ? (shop/items.py)", "GET ?/ (shop/items.py)"]
    assert sorted(
        (r.source_name, r.target_name) for r in result.relationships if r.to_file
    ) == [("GET ? (shop/items.py)", "list_items"), ("GET ?/ (shop/items.py)", "list_items_slash")]


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
    repo = f"zz-pyempty-{uuid.uuid4().hex[:8]}"
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


def _implemented(engine, repo_id):
    return sorted(
        (row["endpoint"], row["handler"])
        for row in engine.run_cypher(
            "MATCH (e:Endpoint {repo_id: $r})-[:IMPLEMENTS]->(f) WHERE f.file IS NOT NULL "
            "RETURN e.name AS endpoint, f.name AS handler",
            {"r": repo_id},
        )
    )


def test_each_mounted_route_implements_its_own_handler(engine, repo_id, tmp_path):
    write(tmp_path, "shop/__init__.py", "")
    items = write(tmp_path, "shop/items.py", ITEMS)
    main = write(tmp_path, "shop/main.py", MAIN)
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert _implemented(engine, repo_id) == [
        ("GET /items", "list_items"), ("GET /items/", "list_items_slash"),
    ]

    # The mount's prefix changes in the mounting file alone.
    write(tmp_path, "shop/main.py", MAIN.replace('"/items"', '"/goods"'))
    index_paths(engine, repo_id, tmp_path, {main})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert _implemented(engine, repo_id) == [
        ("GET /goods", "list_items"), ("GET /goods/", "list_items_slash"),
    ]

    # A handler is renamed in the router file alone.
    write(tmp_path, "shop/items.py", ITEMS.replace("def list_items_slash", "def browse_items"))
    index_paths(engine, repo_id, tmp_path, {items})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert _implemented(engine, repo_id) == [("GET /goods", "list_items"), ("GET /goods/", "browse_items")]
