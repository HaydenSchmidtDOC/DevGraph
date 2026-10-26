"""Flask's `add_url_rule` (a function view and a `MethodView`'s
`as_view`) and FastAPI's `add_api_route` as routes (apis/extractor.py),
under the router and blueprint prefixes the decorators' routes take.
Fixture code is fictional."""

import textwrap

from devgraph.indexer.apis.extractor import APIExtractor


def _extract(code, filename="shop/app.py"):
    return APIExtractor("repo").extract_from_source(textwrap.dedent(code), filename)


def _endpoints(result):
    return sorted(e.name for e in result.endpoints)


def _targets(result):
    return sorted(
        (r.source_name, r.target_label, r.target_name, r.to_file, r.to_owner) for r in result.relationships
    )


def test_a_function_view_is_a_get_route_of_the_file():
    result = _extract("""\
        from flask import Flask

        app = Flask(__name__)

        def index():
            return "home"

        def save():
            return "saved"

        app.add_url_rule("/", view_func=index)
        app.add_url_rule("/save", "save", save, methods=["POST", "PUT"])
    """)
    assert _endpoints(result) == ["GET /", "POST /save", "PUT /save"]
    assert _targets(result) == [
        ("GET /", "Function", "index", "", None), ("GET /", "Function", "index", "shop/app.py", ""),
        ("POST /save", "Function", "save", "", None), ("POST /save", "Function", "save", "shop/app.py", ""),
        ("PUT /save", "Function", "save", "", None), ("PUT /save", "Function", "save", "shop/app.py", ""),
    ]
    assert {e.properties["framework"] for e in result.endpoints} == {"Flask"}


def test_a_method_view_without_methods_is_one_route_of_every_method():
    # As a Django class-based view: one `*` route, its class's every HTTP
    # method handler linked, wherever the class is defined.
    result = _extract("""\
        from flask import Flask
        from flask.views import MethodView

        app = Flask(__name__)

        class BaseAPI(MethodView):
            def delete(self, id):
                pass

        class UserAPI(BaseAPI):
            def get(self, id):
                pass

            def post(self, id):
                pass

        app.add_url_rule("/users/<int:id>", view_func=UserAPI.as_view("user_api"))
    """)
    assert _endpoints(result) == ["* /users/<int:id>"]
    http = ("delete", "get", "head", "options", "patch", "post", "put", "trace")
    assert _targets(result) == sorted([
        ("* /users/<int:id>", "Class", "UserAPI", "shop/app.py", ""),
        ("* /users/<int:id>", "Function", "UserAPI", "", None),
        *(("* /users/<int:id>", "Function", method, "shop/app.py", "UserAPI") for method in http),
    ])


def test_a_method_view_with_methods_given_is_those_methods():
    result = _extract("""\
        from flask import Flask
        from flask.views import MethodView

        app = Flask(__name__)

        class ItemAPI(MethodView):
            def get(self):
                pass

            def post(self):
                pass

        app.add_url_rule("/items", view_func=ItemAPI.as_view("items"), methods=["GET"])
    """)
    assert _endpoints(result) == ["GET /items"]
    assert ("GET /items", "Function", "get", "shop/app.py", "ItemAPI") in _targets(result)
    assert not any(t[2] == "post" for t in _targets(result))


def test_an_imported_method_view_routes_every_method_to_its_class():
    result = _extract("""\
        from flask import Flask
        from .views import UserAPI

        app = Flask(__name__)
        app.add_url_rule("/users/<int:id>", view_func=UserAPI.as_view("user_api"))
    """)
    # Another file's class: its methods are not known here, so one route of every method.
    assert _endpoints(result) == ["* /users/<int:id>"]
    files = ("shop/views.py", "shop/views/__init__.py")
    http = ("delete", "get", "head", "options", "patch", "post", "put", "trace")
    assert _targets(result) == sorted([
        ("* /users/<int:id>", "Function", "UserAPI", "", None),
        *(("* /users/<int:id>", "Class", "UserAPI", file, "") for file in files),
        *(("* /users/<int:id>", "Function", method, file, "UserAPI") for file in files for method in http),
    ])


def test_an_imported_function_view_is_its_modules():
    result = _extract("""\
        from flask import Flask
        from .views import index as home

        app = Flask(__name__)
        app.add_url_rule("/", view_func=home)
    """)
    assert _endpoints(result) == ["GET /"]
    assert _targets(result) == [
        ("GET /", "Function", "index", "", None),
        ("GET /", "Function", "index", "shop/views.py", ""),
        ("GET /", "Function", "index", "shop/views/__init__.py", ""),
    ]


def test_a_blueprints_rule_is_under_its_prefix_and_its_mount():
    result = _extract("""\
        from flask import Blueprint, Flask

        app = Flask(__name__)
        auth = Blueprint("auth", __name__, url_prefix="/auth")
        admin = Blueprint("admin", __name__, url_prefix="/admin")

        def login():
            pass

        def panel():
            pass

        auth.add_url_rule("/login", view_func=login, methods=["GET", "POST"])
        admin.add_url_rule("/panel", view_func=panel)
        app.register_blueprint(auth)
    """)
    assert _endpoints(result) == [
        "GET /auth/login", "GET ?/admin/panel (shop/app.py)", "POST /auth/login",
    ]
    # The unmounted blueprint is another module's to mount, as a decorated route's is.
    assert result.route_exports == sorted([
        "admin\x1fGET\x1f/admin/panel", "admin:replaced\x1fGET\x1f/panel\x1f/admin/panel",
    ])


def test_fastapi_add_api_route_on_an_app_and_a_prefixed_router():
    result = _extract("""\
        from fastapi import APIRouter, FastAPI

        app = FastAPI()
        router = APIRouter(prefix="/orders")

        async def health():
            return {}

        def list_orders():
            pass

        def create_order():
            pass

        app.add_api_route("/health", health, methods=["GET"])
        router.add_api_route("/", list_orders)
        router.add_api_route(path="/", endpoint=create_order, methods=["POST"])
        app.include_router(router, prefix="/api")
    """)
    assert _endpoints(result) == ["GET /api/orders/", "GET /health", "POST /api/orders/"]
    assert ("POST /api/orders/", "Function", "create_order", "shop/app.py", "") in _targets(result)
    assert {e.properties["framework"] for e in result.endpoints} == {"FastAPI"}


def test_a_rule_on_anything_but_an_app_or_router_is_none():
    result = _extract("""\
        from flask import Flask

        registry = make_registry()

        def index():
            pass

        registry.add_url_rule("/", view_func=index)
        registry.add_api_route("/x", index)
    """)
    assert result.endpoints == []
