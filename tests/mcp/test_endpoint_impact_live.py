"""The API endpoints a change reaches, against a live Neo4j (audit 9 item 1):
an Endpoint reaches its handler by IMPLEMENTS, so impact_analysis,
impact_analysis_for_diff and find_callers name the routes above a changed
function in their own list, apart from the dependents (risk_level still counts
direct dependents only). Fixture code is fictional."""

import subprocess
import textwrap
import uuid
from dataclasses import dataclass
from pathlib import Path

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


ORDERS = """\
    from fastapi import APIRouter, FastAPI

    app = FastAPI()
    router = APIRouter(prefix="/api/orders")


    @router.get("/")
    def list_orders():
        return helper()


    @router.post("/")
    def create_order():
        return 1


    def helper():
        return deep()


    def deep():
        return {VALUE}


    app.include_router(router)
    """


@dataclass
class _Repo:
    repo_id: str
    path: Path
    active: bool = True


class _Registry:
    def __init__(self, repos):
        self.repos = repos

    def get(self, repo_id):
        return next((r for r in self.repos if r.repo_id == repo_id), None)


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


@pytest.fixture(scope="module")
def repo(engine, tmp_path_factory):
    """A FastAPI router whose `GET /api/orders/` handler calls helper, which
    calls deep; the head commit edits deep."""
    root = tmp_path_factory.mktemp("endpoint-impact")
    repo_id = f"zz-endpoint-impact-{uuid.uuid4().hex[:8]}"
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "dev@example.com")
    _git(root, "config", "user.name", "Example Dev")
    (root / "orders.py").write_text(textwrap.dedent(ORDERS).replace("{VALUE}", "[]"))
    _git(root, "add", "orders.py")
    _git(root, "commit", "-q", "-m", "base")
    (root / "orders.py").write_text(textwrap.dedent(ORDERS).replace("{VALUE}", "[1]"))
    _git(root, "commit", "-q", "-am", "head")
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)
    yield repo_id, _Registry([_Repo(repo_id, root)])
    engine.delete_repository(repo_id)


def _names(envelope):
    return [row["name"] for row in envelope["results"]]


def test_impact_analysis_lists_the_endpoint_two_calls_above(engine, repo):
    repo_id, _ = repo
    result = tools.impact_analysis(engine, repo_id, "deep")
    assert _names(result["direct_dependents"]) == ["helper"]
    assert _names(result["transitive_dependents"]) == ["list_orders"]
    assert _names(result["endpoints"]) == ["GET /api/orders/"]
    (endpoint,) = result["endpoints"]["results"]
    assert endpoint["method"] == "GET" and endpoint["path"] == "/api/orders/"
    assert endpoint["framework"] == "FastAPI"
    assert [(h["name"], h["file"]) for h in endpoint["handlers"]] == [("list_orders", "orders.py")]
    assert result["risk_level"] == "low"


def test_impact_analysis_of_a_handler_lists_its_endpoint_not_as_a_dependent(engine, repo):
    repo_id, _ = repo
    result = tools.impact_analysis(engine, repo_id, "list_orders", file="orders.py")
    assert result["direct_dependents"]["count"] == 0
    assert _names(result["endpoints"]) == ["GET /api/orders/"]


def test_impact_analysis_for_diff_lists_the_endpoint_above_a_changed_callee(engine, repo):
    repo_id, registry = repo
    result = tools.impact_analysis_for_diff(engine, registry, repo_id, "HEAD~1", "HEAD")
    assert result["changed_components"] == ["deep"]
    assert _names(result["direct_dependents"]) == ["helper"]
    assert _names(result["endpoints"]) == ["GET /api/orders/"]
    assert result["risk_level"] == "low"


def test_find_callers_names_the_route_of_a_handler(engine, repo):
    repo_id, _ = repo
    result = tools.find_callers(engine, repo_id, "list_orders", file="orders.py")
    assert result["count"] == 0
    assert _names(result["routed_by"]) == ["GET /api/orders/"]
    assert tools.find_callers(engine, repo_id, "helper")["routed_by"]["count"] == 0
