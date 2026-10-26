"""A Django route's path, against a live Neo4j (audit 9 item 8b): a Django
pattern has no leading "/" (`* orders/`), which list_endpoints adds to its
path; explain_architecture names the route the same way (`* /orders/`), and
trace_request_flow takes that name too. Fixture code is fictional."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan
from devgraph.mcp import tools


@pytest.fixture(scope="module")
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


FILES = {
    "compose.yaml": "services:\n  shop:\n    build: .\n",
    "shop/urls.py": """\
        from django.urls import path

        def orders(request):
            return None

        urlpatterns = [path("orders/", orders)]
        """,
}


@pytest.fixture(scope="module")
def repo_id(engine, tmp_path_factory):
    root = tmp_path_factory.mktemp("django-paths")
    repo_id = f"zz-django-paths-{uuid.uuid4().hex[:8]}"
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)
    yield repo_id
    engine.delete_repository(repo_id)


def test_the_stored_name_has_no_leading_slash(engine, repo_id):
    rows = engine.run_cypher("MATCH (e:Endpoint {repo_id: $r}) RETURN e.name AS name", {"r": repo_id})
    assert [row["name"] for row in rows] == ["* orders/"]


def test_explain_architecture_names_a_django_route_with_its_leading_slash(engine, repo_id):
    endpoints = tools.explain_architecture(engine, repo_id)["endpoints"]["results"]
    assert endpoints == [{"endpoints": ["* /orders/"], "calls": "shop"}]
    assert [row["path"] for row in tools.list_endpoints(engine, repo_id)["results"]] == ["/orders/"]


@pytest.mark.parametrize("start", ["* /orders/", "/orders/", "* orders/", "orders/"])
def test_trace_request_flow_takes_either_spelling(engine, repo_id, start):
    flow = tools.trace_request_flow(engine, repo_id, start)
    assert {"type": "IMPLEMENTS", "from": "* orders/", "to": "orders"} in flow["edges"]
