"""chi `Mount` of a same-file router against a live Neo4j
(devgraph/indexer/go/extractor.py `_go_routes`): its routes are under the
mount prefix, and editing the prefix or moving the handler to another file
equals a fresh `full_scan`. Fixture code is fictional."""

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
    repo = f"zz-gomount-{uuid.uuid4().hex[:8]}"
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


ROUTES = """\
package web

import (
\t"net/http"

\t"github.com/go-chi/chi/v5"
)

func Routes() http.Handler {
\tr := chi.NewRouter()
\tr.Mount("{prefix}", adminRouter())
\treturn r
}

func adminRouter() http.Handler {
\tr := chi.NewRouter()
\tr.Get("/stats", stats)
\treturn r
}
"""


def _implements(engine, repo_id):
    return {
        (row["e"], row["f"])
        for row in engine.run_cypher(
            "MATCH (e:Endpoint {repo_id: $r})-[:IMPLEMENTS]->(f:Function) WHERE f.file IS NOT NULL "
            "RETURN e.name AS e, f.file AS f",
            {"r": repo_id},
        )
    }


def test_chi_mount_prefix_edit_and_handler_move_equal_a_fresh_scan(engine, repo_id, tmp_path):
    routes = write(tmp_path, "web/routes.go", ROUTES.replace("{prefix}", "/admin") +
                   "\nfunc stats(w http.ResponseWriter, r *http.Request) {}\n")
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert _implements(engine, repo_id) == {("GET /admin/stats", "web/routes.go")}

    write(tmp_path, "web/routes.go", ROUTES.replace("{prefix}", "/ops") +
          "\nfunc stats(w http.ResponseWriter, r *http.Request) {}\n")
    index_paths(engine, repo_id, tmp_path, {routes})
    assert _implements(engine, repo_id) == {("GET /ops/stats", "web/routes.go")}
    incremental_equals_fresh(engine, repo_id, tmp_path)

    # The handler moves to another file of the package.
    write(tmp_path, "web/routes.go", ROUTES.replace("{prefix}", "/ops"))
    stats = write(tmp_path, "web/stats.go", """\
        package web

        import "net/http"

        func stats(w http.ResponseWriter, r *http.Request) {}
        """)
    index_paths(engine, repo_id, tmp_path, {routes, stats})
    assert _implements(engine, repo_id) == {("GET /ops/stats", "web/stats.go")}
    incremental_equals_fresh(engine, repo_id, tmp_path)

    stats.unlink()
    remove_paths(engine, repo_id, tmp_path, {stats})
    incremental_equals_fresh(engine, repo_id, tmp_path)
