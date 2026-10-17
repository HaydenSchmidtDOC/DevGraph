"""Datastore clients beyond Python against a live Neo4j: each language's
service USES the stores its files' clients name, a store several files and
languages use is one shared node, and edits equal a fresh `full_scan`.
Fixture code is fictional."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths
from devgraph.mcp.tools import get_service_dependencies
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
    repo = f"zz-clients-{uuid.uuid4().hex[:8]}"
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


# One service per language: its folder, a file with clients, and the stores it uses.
SERVICES = {
    "web": ("web/db.ts", """\
        import { Pool } from 'pg';
        import Redis from 'ioredis';
        export const pool = new Pool();
        """, {("Database", "PostgreSQL"), ("Cache", "Redis")}),
    "billing": ("billing/src/main/java/billing/Events.java", """\
        package billing;

        import org.apache.kafka.clients.producer.KafkaProducer;

        public class Events {
            Connection db = DriverManager.getConnection("jdbc:postgresql://db:5432/billing");
        }
        """, {("Queue", "Kafka"), ("Database", "PostgreSQL")}),
    "ledger": ("ledger/Store.kt", """\
        package ledger

        import redis.clients.jedis.Jedis

        class Store
        """, {("Cache", "Redis")}),
    "orders": ("orders/OrdersContext.cs", """\
        using Microsoft.EntityFrameworkCore;
        using StackExchange.Redis;

        public class OrdersContext : DbContext
        {
            protected override void OnConfiguring(DbContextOptionsBuilder o) => o.UseNpgsql("Host=db");
        }
        """, {("Database", "EF Core"), ("Database", "PostgreSQL"), ("Cache", "Redis")}),
    "stock": ("stock/store.go", """\
        package stock

        import (
        \t"database/sql"

        \t_ "github.com/lib/pq"
        \t"go.mongodb.org/mongo-driver/mongo"
        )

        func open() (*sql.DB, error) { return sql.Open("postgres", "") }
        """, {("Database", "PostgreSQL"), ("Database", "MongoDB")}),
}


def uses(engine, repo_id, service):
    return {(dep["type"], dep["name"]) for dep in get_service_dependencies(engine, repo_id, service)["dependencies"]}


def test_each_language_s_service_uses_its_clients_stores(engine, repo_id, tmp_path):
    write(tmp_path, "compose.yaml", "services:\n" + "".join(
        f"  {name}:\n    build: ./{name}\n" for name in SERVICES
    ))
    paths = {name: write(tmp_path, rel, text) for name, (rel, text, _stores) in SERVICES.items()}
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    for name, (_rel, _text, stores) in SERVICES.items():
        assert uses(engine, repo_id, name) == stores, name
    # Every service's PostgreSQL is one shared node, claimed by each file.
    (row,) = engine.run_cypher(
        "MATCH (d:Database {repo_id: $r, name: 'PostgreSQL'}) RETURN d.sources AS sources", {"r": repo_id}
    )
    assert row["sources"] == sorted(SERVICES[name][0] for name in ("web", "billing", "orders", "stock"))

    # A file drops a client, another file is deleted, a third gains one: equal to fresh after each.
    rel, text, _stores = SERVICES["web"]
    write(tmp_path, rel, text.replace("import Redis from 'ioredis';\n", ""))
    index_paths(engine, repo_id, tmp_path, {paths["web"]})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert uses(engine, repo_id, "web") == {("Database", "PostgreSQL")}
    paths["ledger"].unlink()
    remove_paths(engine, repo_id, tmp_path, {paths["ledger"]})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert uses(engine, repo_id, "ledger") == set()
    rel, text, _stores = SERVICES["stock"]
    write(tmp_path, rel, text.replace('"go.mongodb.org/mongo-driver/mongo"', '"github.com/IBM/sarama"'))
    index_paths(engine, repo_id, tmp_path, {paths["stock"]})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert uses(engine, repo_id, "stock") == {("Database", "PostgreSQL"), ("Queue", "Kafka")}
