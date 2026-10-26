"""A Spring interface nested in a class against a live Neo4j
(java/extractor.py and kotlin/extractor.py `_spring_routes`): a Kotlin
controller importing the nested interface and a Java one importing the
outer class's members by wildcard implement its route, and switching an
import equals a fresh `full_scan`. Fixture code is fictional."""

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
    repo = f"zz-springnest-{uuid.uuid4().hex[:8]}"
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
        (row["e"], row["file"], row["owner"])
        for row in engine.run_cypher(
            "MATCH (e:Endpoint {repo_id: $r})-[:IMPLEMENTS]->(f:Function) WHERE f.file IS NOT NULL "
            "RETURN e.name AS e, f.file AS file, f.owner AS owner",
            {"r": repo_id},
        )
    }


API = """\
package shop.api;

public class Outer {
    public interface Api {
        @GetMapping("/orders")
        String list();
    }
}
"""
KOTLIN = """\
package shop.web

import {imported}

@RestController
class KotlinOrders : Api {
    override fun list(): String = ""
}
"""
JAVA = """\
package shop.web;

import shop.api.Outer.*;

@RestController
public class JavaOrders implements Api {
    public String list() { return ""; }
}
"""
KOTLIN_PATH = "src/main/kotlin/shop/web/KotlinOrders.kt"
JAVA_PATH = "src/main/java/shop/web/JavaOrders.java"


def _files(engine, repo_id):
    return {file for e, file, _owner in _implements(engine, repo_id) if e == "GET /orders"}


def test_controllers_of_a_nested_interface(engine, repo_id, tmp_path):
    write(tmp_path, "src/main/java/shop/api/Outer.java", API)
    write(tmp_path, JAVA_PATH, JAVA)
    kotlin = write(tmp_path, KOTLIN_PATH, KOTLIN.replace("{imported}", "shop.api.Outer.Api"))
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert _files(engine, repo_id) == {"src/main/java/shop/api/Outer.java", JAVA_PATH, KOTLIN_PATH}

    write(tmp_path, KOTLIN_PATH, KOTLIN.replace("{imported}", "shop.other.Outer.Api"))
    index_paths(engine, repo_id, tmp_path, {kotlin})
    assert KOTLIN_PATH not in _files(engine, repo_id)
    incremental_equals_fresh(engine, repo_id, tmp_path)
