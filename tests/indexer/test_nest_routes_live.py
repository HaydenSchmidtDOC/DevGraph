"""NestJS controllers and Fastify route objects against a live Neo4j
(jsts/extractor.py `_nest_routes`, `_route_options`): an edit of a
controller's decorators renames its Endpoints, and a Fastify route's
imported handler follows its module, each equal to a fresh `full_scan`.
Fixture code is fictional."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths
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
    repo = f"zz-nest-{uuid.uuid4().hex[:8]}"
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


def _scan(engine, repo_id, root):
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)


def _implemented(engine, repo_id):
    return sorted(
        (row["endpoint"], row["name"], row["file"], row["owner"])
        for row in engine.run_cypher(
            "MATCH (e:Endpoint {repo_id: $r})-[:IMPLEMENTS]->(f) WHERE f.file IS NOT NULL "
            "RETURN e.name AS endpoint, f.name AS name, f.file AS file, f.owner AS owner",
            {"r": repo_id},
        )
    )


CONTROLLER = """\
    import { Controller, Get, Post } from '@nestjs/common';
    import { UsersService } from './users.service';

    @Controller('users')
    export class UsersController {
      constructor(private readonly users: UsersService) {}

      @Get(':id')
      findOne() {
        return this.users.find();
      }

      @Post()
      create() {}
    }
"""

SERVICE = """\
    export class UsersService {
      find() {}
    }
"""


def test_editing_a_controllers_decorators_renames_its_endpoints(engine, repo_id, tmp_path):
    controller = write(tmp_path, "src/users.controller.ts", CONTROLLER)
    write(tmp_path, "src/users.service.ts", SERVICE)
    _scan(engine, repo_id, tmp_path)
    path = "src/users.controller.ts"
    assert _implemented(engine, repo_id) == [
        ("GET /users/:id", "findOne", path, "UsersController"),
        ("POST /users", "create", path, "UsersController"),
    ]

    write(tmp_path, "src/users.controller.ts", CONTROLLER.replace("@Controller('users')", "@Controller('members')")
          .replace("@Post()", "@Post('new')"))
    index_paths(engine, repo_id, tmp_path, {controller})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert _implemented(engine, repo_id) == [
        ("GET /members/:id", "findOne", path, "UsersController"),
        ("POST /members/new", "create", path, "UsersController"),
    ]


def test_a_fastify_route_objects_imported_handler(engine, repo_id, tmp_path):
    write(tmp_path, "srv/server.js", """\
        import Fastify from 'fastify';
        import { listOrders } from './orders.js';
        const fastify = Fastify();
        fastify.route({ method: ['GET', 'HEAD'], url: '/orders', handler: listOrders });
    """)
    orders = write(tmp_path, "srv/orders.js", "export function listOrders(request, reply) {}\n")
    _scan(engine, repo_id, tmp_path)
    assert _implemented(engine, repo_id) == [
        ("GET /orders", "listOrders", "srv/orders.js", ""),
        ("HEAD /orders", "listOrders", "srv/orders.js", ""),
    ]

    # The handler goes from its module and comes back, in that file alone.
    write(tmp_path, "srv/orders.js", "export function other() {}\n")
    index_paths(engine, repo_id, tmp_path, {orders})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert _implemented(engine, repo_id) == []
    write(tmp_path, "srv/orders.js", "export function listOrders(request, reply) {}\n")
    index_paths(engine, repo_id, tmp_path, {orders})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert len(_implemented(engine, repo_id)) == 2
