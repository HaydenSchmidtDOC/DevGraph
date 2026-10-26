"""list_endpoints filtered by the NestJS framework against a live Neo4j: a
NestJS controller's routes are found by `framework="NestJS"` (any case).
Fixture code is fictional."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan
from devgraph.mcp.tools import ENDPOINT_FRAMEWORKS, list_endpoints


def test_nestjs_is_a_known_framework():
    assert "NestJS" in ENDPOINT_FRAMEWORKS


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
    repo = f"zz-nestlist-{uuid.uuid4().hex[:8]}"
    engine.delete_repository(repo)
    yield repo
    engine.delete_repository(repo)


def test_list_endpoints_by_nestjs(engine, repo_id, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/users.controller.ts").write_text(textwrap.dedent("""\
        import { Controller, Get } from '@nestjs/common';

        @Controller('users')
        export class UsersController {
          @Get(':id')
          findOne() {}
        }
    """))
    (tmp_path / "src/app.js").write_text("const app = require('express')();\napp.get('/health', health);\nfunction health() {}\n")
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    for spelling in ("NestJS", "nestjs"):
        results = list_endpoints(engine, repo_id, framework=spelling)["results"]
        assert [(row["name"], row["framework"]) for row in results] == [("GET /users/:id", "NestJS")]
        assert results[0]["handlers"] == [
            {"label": "Function", "name": "findOne", "file": "src/users.controller.ts", "owner": "UsersController"},
        ]
