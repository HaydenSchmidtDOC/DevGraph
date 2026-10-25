"""A compose service's build context, resolved from the compose file's own
folder to a repository-relative one (containers/extractor.py), and the
files it owns (dispatch `_match_owning_service`). Fixture code is
fictional."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer import dispatch
from devgraph.indexer.containers.extractor import ContainerExtractor
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff


def _context(compose: str, path: str):
    (service,) = (
        ContainerExtractor("repo")
        .extract_from_compose_file(textwrap.dedent(compose), path)
        .services
    )
    return service.properties.get("build_context")


@pytest.mark.parametrize(
    "build, path, expected",
    [
        ("build: ./api", "services/compose.yml", "services/api"),
        ("build: api", "services/compose.yml", "services/api"),
        ("build:\n      context: ..", "deploy/docker-compose.yml", ""),
        ("build:\n      context: ../backend", "deploy/docker-compose.yml", "backend"),
        ("build: .", "docker-compose.yml", ""),
        ("build: .", "services/compose.yml", "services"),
        ("build: ./services/api/", "compose.yaml", "services/api"),
        # Outside the repository, or not a folder of it: no context.
        ("build: ../..", "deploy/compose.yml", None),
        ("build: /srv/app", "compose.yml", None),
        ("build: https://example.com/shop.git", "compose.yml", None),
    ],
)
def test_build_context_is_resolved_from_the_compose_files_folder(build, path, expected):
    assert _context(f"services:\n  app:\n    {build}\n", path) == expected


def test_dockerfile_fallback_is_resolved_in_a_nested_compose_file():
    """A shared context (the compose file's own folder) with a per-service
    Dockerfile: the Dockerfile's folder, relative to the context."""
    compose = """\
        services:
          worker:
            build:
              context: .
              dockerfile: ingestion/Dockerfile
    """
    assert _context(compose, "services/compose.yml") == "services/ingestion"
    # A Dockerfile without a folder leaves the context the service's own.
    assert (
        _context(
            compose.replace("ingestion/Dockerfile", "Dockerfile"),
            "services/compose.yml",
        )
        == "services"
    )


def test_dockerfile_fallback_applies_to_any_shared_context():
    """`context: ..` one level down with per-service Dockerfiles: each
    service owns its Dockerfile's folder, not the whole repository, so one
    service's datastores never go to another."""
    compose = """\
        services:
          api:
            build:
              context: ..
              dockerfile: services/api/Dockerfile
          worker:
            build:
              context: ..
              dockerfile: services/worker/Dockerfile
          agent:
            build:
              context: ..
              dockerfile: Dockerfile
    """
    services = (
        ContainerExtractor("repo")
        .extract_from_compose_file(textwrap.dedent(compose), "deploy/compose.yml")
        .services
    )
    assert {s.name: s.properties["build_context"] for s in services} == {
        "api": "services/api",
        "worker": "services/worker",
        "agent": "",
    }


def test_a_root_context_owns_every_file_no_deeper_context_claims():
    services = {"app": ("", "compose.yml"), "api": ("services/api", "compose.yml")}
    assert dispatch._match_owning_service(services, "main.py") == "app"
    assert dispatch._match_owning_service(services, "lib/util.py") == "app"
    assert dispatch._match_owning_service(services, "services/api/routes.py") == "api"


# --- live ---------------------------------------------------------------------


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
    repo = f"zz-buildctx-{uuid.uuid4().hex[:8]}"
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


def _uses(engine, repo_id):
    return sorted(
        (row["service"], row["store"])
        for row in engine.run_cypher(
            "MATCH (s:Service {repo_id: $r})-[:USES]->(d) WHERE NOT d:Volume RETURN s.name AS service, d.name AS store",
            {"r": repo_id},
        )
    )


COMPOSE = """\
    services:
      shop:
        build:
          context: ..
          dockerfile: Dockerfile
"""


def test_a_nested_compose_files_parent_context_owns_the_repository(
    engine, repo_id, tmp_path
):
    compose = write(tmp_path, "deploy/docker-compose.yml", COMPOSE)
    write(tmp_path, "shop/db.py", "import redis\n")
    write(
        tmp_path,
        "shop/api.py",
        """\
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/health")
        def health():
            pass
        """,
    )
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert _uses(engine, repo_id) == [("shop", "Redis")]
    calls = engine.run_cypher(
        "MATCH (e:Endpoint {repo_id: $r})-[:CALLS]->(s:Service) RETURN e.name AS e, s.name AS s",
        {"r": repo_id},
    )
    assert [(row["e"], row["s"]) for row in calls] == [("GET /health", "shop")]

    # The compose file moves to the root: `..` now leaves the repository.
    compose.unlink()
    moved = write(tmp_path, "docker-compose.yml", COMPOSE)
    remove_paths(engine, repo_id, tmp_path, {compose})
    index_paths(engine, repo_id, tmp_path, {moved})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert _uses(engine, repo_id) == []

    # And builds from its own folder, the root.
    write(tmp_path, "docker-compose.yml", COMPOSE.replace("context: ..", "context: ."))
    index_paths(engine, repo_id, tmp_path, {moved})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert _uses(engine, repo_id) == [("shop", "Redis")]
