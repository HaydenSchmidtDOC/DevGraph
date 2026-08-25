"""Integration tests for devgraph.indexer.dispatch — the orchestration layer
that routes changed/deleted files to the right extractor. This is the piece
that was previously missing entirely: extractors existed but nothing called
them from add/rescan/watch.
"""

import tempfile
from pathlib import Path

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.init_schema()
    yield test_engine
    test_engine.close()


@pytest.fixture
def temp_repo():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


class TestIndexPaths:
    def test_indexes_python_file(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_python"
        py_file = temp_repo / "service.py"
        py_file.write_text("class MyService:\n    pass\n")

        try:
            count = index_paths(engine, repo_id, temp_repo, {py_file})
            assert count == 1

            result = engine.run_cypher(
                "MATCH (c:Class {repo_id: $repo_id, name: 'MyService'}) RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert result[0]["c"] == 1
        finally:
            engine.delete_repository(repo_id)

    def test_indexes_datastore_usage_from_same_python_file(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_datastore"
        py_file = temp_repo / "db.py"
        py_file.write_text("import redis\ncache = redis.Redis()\n")

        try:
            index_paths(engine, repo_id, temp_repo, {py_file})
            result = engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) WHERE n.provider = 'Redis' OR n.name = 'Redis' "
                "RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert result[0]["c"] >= 1
        finally:
            engine.delete_repository(repo_id)

    def test_indexes_dockerfile(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_container"
        dockerfile = temp_repo / "Dockerfile"
        dockerfile.write_text("FROM python:3.13\n")

        try:
            count = index_paths(engine, repo_id, temp_repo, {dockerfile})
            assert count == 1
            result = engine.run_cypher(
                "MATCH (c:Container {repo_id: $repo_id}) RETURN COUNT(*) as c", {"repo_id": repo_id}
            )
            assert result[0]["c"] >= 1
        finally:
            engine.delete_repository(repo_id)

    def test_indexes_docs_note_under_docs_path(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_docs"
        docs_dir = temp_repo / "docs"
        docs_dir.mkdir()
        note = docs_dir / "req.md"
        note.write_text("---\ntype: requirement\nid: req-1\n---\n# A requirement\n")

        try:
            count = index_paths(engine, repo_id, temp_repo, {note}, docs_path="docs")
            assert count == 1
            result = engine.run_cypher(
                "MATCH (r:Requirement {repo_id: $repo_id, name: 'req-1'}) RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert result[0]["c"] == 1
        finally:
            engine.delete_repository(repo_id)

    def test_markdown_outside_docs_path_is_skipped(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_docs_skip"
        note = temp_repo / "README.md"
        note.write_text("---\ntype: requirement\nid: req-skip\n---\n# Should not be indexed\n")

        try:
            count = index_paths(engine, repo_id, temp_repo, {note}, docs_path="docs")
            assert count == 0
        finally:
            engine.delete_repository(repo_id)

    def test_path_outside_repo_root_is_skipped(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_outside"
        with tempfile.TemporaryDirectory() as other_dir:
            outside_file = Path(other_dir) / "evil.py"
            outside_file.write_text("class ShouldNotAppear:\n    pass\n")

            count = index_paths(engine, repo_id, temp_repo, {outside_file})
            assert count == 0

            result = engine.run_cypher(
                "MATCH (c:Class {repo_id: $repo_id, name: 'ShouldNotAppear'}) RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert result[0]["c"] == 0


class TestRemovePaths:
    def test_removes_nodes_for_deleted_python_file(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_remove"
        py_file = temp_repo / "gone.py"
        py_file.write_text("class Gone:\n    pass\n")

        try:
            index_paths(engine, repo_id, temp_repo, {py_file})
            result = engine.run_cypher(
                "MATCH (c:Class {repo_id: $repo_id, name: 'Gone'}) RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert result[0]["c"] == 1

            py_file.unlink()
            cleaned = remove_paths(engine, repo_id, temp_repo, {py_file})
            assert cleaned == 1

            result = engine.run_cypher(
                "MATCH (c:Class {repo_id: $repo_id, name: 'Gone'}) RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert result[0]["c"] == 0
        finally:
            engine.delete_repository(repo_id)


class TestFullScan:
    def test_full_scan_indexes_multiple_files(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_fullscan"
        (temp_repo / "a.py").write_text("class A:\n    pass\n")
        (temp_repo / "b.py").write_text("class B:\n    pass\n")
        (temp_repo / ".git").mkdir()
        (temp_repo / ".git" / "ignored.py").write_text("class ShouldBeSkipped:\n    pass\n")

        try:
            count = full_scan(engine, repo_id, temp_repo)
            assert count == 2

            result = engine.run_cypher(
                "MATCH (c:Class {repo_id: $repo_id}) RETURN c.name as name ORDER BY c.name",
                {"repo_id": repo_id},
            )
            names = [r["name"] for r in result]
            assert names == ["A", "B"]
        finally:
            engine.delete_repository(repo_id)


class TestServiceCrossLinking:
    """Previously the container extractor (compose-derived Service nodes)
    and the datastore/API extractors (per-file Database/Endpoint nodes)
    never cross-referenced each other — explain_architecture's Service
    'uses'/'calls' output stayed empty even on a fully-scanned repo. Fixed
    via build_context-based directory containment matching.
    """

    def test_full_scan_links_service_to_datastore_it_uses(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_service_link"
        (temp_repo / "docker-compose.yml").write_text(
            "services:\n"
            "  api:\n"
            "    build: ./services/api\n"
        )
        api_dir = temp_repo / "services" / "api"
        api_dir.mkdir(parents=True)
        (api_dir / "db.py").write_text("import redis\ncache = redis.Redis()\n")

        try:
            full_scan(engine, repo_id, temp_repo)

            result = engine.run_cypher(
                "MATCH (s:Service {repo_id: $repo_id, name: 'api'})-[:USES]->(d) "
                "RETURN labels(d) as labels, d.name as name",
                {"repo_id": repo_id},
            )
            assert any(r["name"] == "Redis" for r in result)
        finally:
            engine.delete_repository(repo_id)

    def test_full_scan_links_endpoint_to_owning_service(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_endpoint_link"
        (temp_repo / "docker-compose.yml").write_text(
            "services:\n"
            "  web:\n"
            "    build: ./services/web\n"
        )
        web_dir = temp_repo / "services" / "web"
        web_dir.mkdir(parents=True)
        (web_dir / "app.py").write_text(
            "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/health')\ndef health():\n    pass\n"
        )

        try:
            full_scan(engine, repo_id, temp_repo)

            result = engine.run_cypher(
                "MATCH (e:Endpoint {repo_id: $repo_id})-[:CALLS]->(s:Service {name: 'web'}) "
                "RETURN e.name as name",
                {"repo_id": repo_id},
            )
            assert any(r["name"] == "GET /health" for r in result)
        finally:
            engine.delete_repository(repo_id)

    def test_file_outside_any_build_context_is_not_linked(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_no_link"
        (temp_repo / "docker-compose.yml").write_text(
            "services:\n"
            "  api:\n"
            "    build: ./services/api\n"
        )
        (temp_repo / "services" / "api").mkdir(parents=True)
        # File lives outside the api service's build context (e.g. shared code).
        (temp_repo / "shared_db.py").write_text("import redis\ncache = redis.Redis()\n")

        try:
            full_scan(engine, repo_id, temp_repo)

            result = engine.run_cypher(
                "MATCH (s:Service {repo_id: $repo_id})-[:USES]->(d:Cache) RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert result[0]["c"] == 0
        finally:
            engine.delete_repository(repo_id)
