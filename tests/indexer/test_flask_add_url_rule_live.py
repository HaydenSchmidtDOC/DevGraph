"""Flask `add_url_rule` routes across files, against a live Neo4j
(apis/extractor.py `_rule_views`): an imported `MethodView`'s route
implements its Class and the HTTP methods it has, a blueprint's rule takes
the prefix another file registers it at, and editing either file equals a
fresh `full_scan`. Fixture code is fictional."""

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
    repo = f"zz-flaskrule-{uuid.uuid4().hex[:8]}"
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
        (row["endpoint"], row["label"], row["name"], row["file"], row["owner"])
        for row in engine.run_cypher(
            "MATCH (e:Endpoint {repo_id: $r})-[:IMPLEMENTS]->(f) WHERE f.file IS NOT NULL "
            "RETURN e.name AS endpoint, labels(f)[0] AS label, f.name AS name, f.file AS file, f.owner AS owner",
            {"r": repo_id},
        )
    )


APP = """\
    from flask import Flask
    from .views import UserAPI

    app = Flask(__name__)
    app.add_url_rule("/users/<int:id>", view_func=UserAPI.as_view("user_api"))
"""

VIEWS = """\
    from flask.views import MethodView


    class UserAPI(MethodView):
        def get(self, id):
            pass

        def render(self):
            pass
"""


def test_an_imported_method_view_and_an_edit_of_its_methods(engine, repo_id, tmp_path):
    write(tmp_path, "web/__init__.py", "")
    write(tmp_path, "web/app.py", APP)
    views = write(tmp_path, "web/views.py", VIEWS)
    # A same-named class elsewhere is not the view.
    write(tmp_path, "other/views.py", "class UserAPI:\n    def get(self, id):\n        pass\n")
    _scan(engine, repo_id, tmp_path)
    assert _implemented(engine, repo_id) == [
        ("* /users/<int:id>", "Class", "UserAPI", "web/views.py", ""),
        ("* /users/<int:id>", "Function", "get", "web/views.py", "UserAPI"),
    ]

    # The class gains a `post` in its own file alone.
    write(tmp_path, "web/views.py", VIEWS.replace("def render(self)", "def post(self, id)"))
    index_paths(engine, repo_id, tmp_path, {views})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert _implemented(engine, repo_id) == [
        ("* /users/<int:id>", "Class", "UserAPI", "web/views.py", ""),
        ("* /users/<int:id>", "Function", "get", "web/views.py", "UserAPI"),
        ("* /users/<int:id>", "Function", "post", "web/views.py", "UserAPI"),
    ]


def test_a_blueprints_rule_registered_from_another_file(engine, repo_id, tmp_path):
    write(tmp_path, "web/__init__.py", "")
    write(tmp_path, "web/auth.py", """\
        from flask import Blueprint

        bp = Blueprint("auth", __name__, url_prefix="/auth")

        def login():
            pass

        bp.add_url_rule("/login", view_func=login, methods=["GET", "POST"])
    """)
    app = write(tmp_path, "web/app.py", """\
        from flask import Flask
        from .auth import bp

        app = Flask(__name__)
        app.register_blueprint(bp)
    """)
    _scan(engine, repo_id, tmp_path)
    assert _implemented(engine, repo_id) == [
        ("GET /auth/login", "Function", "login", "web/auth.py", ""),
        ("POST /auth/login", "Function", "login", "web/auth.py", ""),
    ]

    # The registration replaces the blueprint's own prefix, in the app file alone.
    write(tmp_path, "web/app.py", app.read_text().replace("register_blueprint(bp)", 'register_blueprint(bp, url_prefix="/v2")'))
    index_paths(engine, repo_id, tmp_path, {app})
    incremental_equals_fresh(engine, repo_id, tmp_path)
    assert [row[0] for row in _implemented(engine, repo_id)] == ["GET /v2/login", "POST /v2/login"]
