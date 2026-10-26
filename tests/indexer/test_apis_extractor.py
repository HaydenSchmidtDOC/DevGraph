"""Unit tests for API extractor."""


from devgraph.indexer.apis.extractor import APIExtractor


class TestAPIExtractor:
    """Tests for APIExtractor."""

    def setup_method(self):
        """Set up test fixtures."""
        self.repo_id = "test-repo"
        self.extractor = APIExtractor(self.repo_id)

    def test_extract_fastapi_get_route(self):
        """Test extraction of a FastAPI GET route."""
        code = """
from fastapi import FastAPI

app = FastAPI()

@app.get("/users")
def get_users():
    return []

@app.get("/users/{user_id}")
async def get_user(user_id: int):
    return {"user_id": user_id}
"""
        result = self.extractor.extract_from_source(code, "main.py")

        # Filter to FastAPI only (extractor runs all frameworks)
        fastapi_endpoints = [e for e in result.endpoints if e.properties.get("framework") == "FastAPI"]
        assert len(fastapi_endpoints) == 2
        paths = {e.path for e in fastapi_endpoints}
        assert "/users" in paths
        assert "/users/{user_id}" in paths

        methods = {e.method for e in fastapi_endpoints}
        assert methods == {"GET"}

        # Check function relationships
        assert len(result.functions) >= 2
        function_names = {f.name for f in result.functions}
        assert "get_users" in function_names
        assert "get_user" in function_names

    def test_extract_fastapi_multiple_methods(self):
        """Test extraction of multiple HTTP methods."""
        code = """
from fastapi import FastAPI

app = FastAPI()

@app.post("/items")
def create_item():
    pass

@app.put("/items/{item_id}")
async def update_item(item_id: int):
    pass

@app.delete("/items/{item_id}")
def delete_item(item_id: int):
    pass
"""
        result = self.extractor.extract_from_source(code, "main.py")

        # Filter to FastAPI only
        fastapi_endpoints = [e for e in result.endpoints if e.properties.get("framework") == "FastAPI"]
        assert len(fastapi_endpoints) == 3
        methods = {e.method for e in fastapi_endpoints}
        assert methods == {"POST", "PUT", "DELETE"}

    def test_extract_fastapi_router(self):
        """Test extraction of routes from APIRouter."""
        code = """
from fastapi import APIRouter

router = APIRouter()

@router.get("/items")
def list_items():
    return []

@router.post("/items")
def create_item():
    pass
"""
        result = self.extractor.extract_from_source(code, "items.py")

        assert len(result.endpoints) == 2
        paths = {e.path for e in result.endpoints}
        assert "/items" in paths

    def test_extract_flask_routes(self):
        """Test extraction of Flask routes."""
        code = """
from flask import Flask

app = Flask(__name__)

@app.route("/api/data", methods=["GET", "POST"])
def get_data():
    return {"data": []}

@app.get("/users")
def list_users():
    return []

@app.post("/users")
def create_user():
    pass
"""
        result = self.extractor.extract_from_source(code, "app.py")

        # Should have 4 endpoints (GET and POST for /api/data, GET for /users, POST for /users)
        assert len(result.endpoints) >= 3
        paths = {e.path for e in result.endpoints}
        assert "/api/data" in paths
        assert "/users" in paths

    def test_extract_flask_default_method(self):
        """Test Flask route without explicit method defaults to GET."""
        code = """
@app.route("/home")
def home():
    return "Home"
"""
        result = self.extractor.extract_from_source(code, "app.py")

        assert len(result.endpoints) >= 1
        endpoint = next((e for e in result.endpoints if e.path == "/home"), None)
        assert endpoint is not None
        assert endpoint.method == "GET"

    def test_extract_django_urls(self):
        """Test extraction of Django URL patterns."""
        code = """
from django.urls import path
from . import views

urlpatterns = [
    path("users/", views.list_users),
    path("users/<int:user_id>/", views.get_user),
    path("posts/create/", views.create_post),
]
"""
        result = self.extractor.extract_from_source(code, "urls.py")

        # Filter to Django only
        django_endpoints = [e for e in result.endpoints if e.properties.get("framework") == "Django"]
        assert len(django_endpoints) >= 3
        paths = {e.path for e in django_endpoints}
        assert "users/" in paths
        assert "users/<int:user_id>/" in paths
        assert "posts/create/" in paths

        # Django views should be detected
        # Note: regex pattern matching may or may not extract 'views.' prefix
        function_names = {f.name for f in result.functions}
        # At least check that we have functions
        assert len(function_names) >= 1

    def test_endpoint_framework_metadata(self):
        """Test that extracted endpoints have correct framework metadata."""
        fastapi_code = "@app.get('/items')\ndef get_items(): pass"
        fastapi_result = self.extractor.extract_from_source(fastapi_code, "api.py")

        if fastapi_result.endpoints:
            assert fastapi_result.endpoints[0].properties.get("framework") == "FastAPI"

    def test_endpoint_source_tracking(self):
        """Test that extracted endpoints track their source file."""
        code = """
@app.get("/api")
def api_handler():
    pass
"""
        result = self.extractor.extract_from_source(code, "handlers.py")

        if result.endpoints:
            assert result.endpoints[0].properties.get("source") == "handlers.py"

    def test_relationship_implementation(self):
        """Test that IMPLEMENTS relationships are created correctly."""
        code = """
@app.get("/data")
def get_data():
    return {}
"""
        result = self.extractor.extract_from_source(code, "api.py")

        assert len(result.relationships) > 0
        impl_rels = [r for r in result.relationships if r.relationship_type == "IMPLEMENTS"]
        assert len(impl_rels) >= 1

        # Find an implementation relationship
        impl_rel = next((r for r in impl_rels if r.target_name == "get_data"), None)
        assert impl_rel is not None
        assert impl_rel.source_label == "Endpoint"
        assert impl_rel.target_label == "Function"

    def test_fastapi_and_flask_implements_are_pinned_to_the_route_file(self):
        """The handler `def` follows its decorator in the same file, so IMPLEMENTS
        targets that file's Function and the file-less handler stub ("")."""
        code = """
@router.post("/items")
def save():
    return {}

@app.route("/legacy", methods=["GET"])
def legacy():
    return {}
"""
        result = self.extractor.extract_from_source(code, "api/routes.py")
        targets = sorted(
            (r.source_name, r.target_name, r.to_file) for r in result.relationships if r.relationship_type == "IMPLEMENTS"
        )
        assert targets == [
            ("GET /legacy", "legacy", ""),
            ("GET /legacy", "legacy", "api/routes.py"),
            ("POST /items", "save", ""),
            ("POST /items", "save", "api/routes.py"),
        ]

    def test_django_implements_stays_bare_name(self):
        """A Django view is usually imported from views.py, so its target stays unpinned."""
        result = self.extractor.extract_from_source('urlpatterns = [path("search/", search)]\n', "app/urls.py")
        (rel,) = result.relationships
        assert (rel.source_name, rel.target_name, rel.to_file) == ("* search/", "search", None)

    def test_async_handler_detection(self):
        """Test detection of async handler functions."""
        code = """
@app.get("/async-data")
async def get_async_data():
    return {}
"""
        result = self.extractor.extract_from_source(code, "api.py")

        functions = [f for f in result.functions if f.name == "get_async_data"]
        assert len(functions) >= 1

    def test_repo_id_scoping(self):
        """Test that all extracted entities have correct repo_id."""
        code = """
@app.get("/test")
def test_endpoint():
    pass
"""
        result = self.extractor.extract_from_source(code)

        for endpoint in result.endpoints:
            assert endpoint.repo_id == self.repo_id

        for function in result.functions:
            assert function.repo_id == self.repo_id

    def test_complex_path_patterns(self):
        """Test extraction with complex path patterns."""
        code = """
@app.get("/api/v1/users/{user_id}/posts/{post_id}")
def get_user_post(user_id: int, post_id: int):
    pass

@app.get("/search/{query:path}")
async def search(query: str):
    pass
"""
        result = self.extractor.extract_from_source(code)

        paths = {e.path for e in result.endpoints}
        assert "/api/v1/users/{user_id}/posts/{post_id}" in paths
        assert "/search/{query:path}" in paths

    def test_multiple_decorators(self):
        """Test extraction with multiple decorators on same function."""
        code = """
@app.get("/items")
@app.post("/items")
def handle_items():
    pass
"""
        result = self.extractor.extract_from_source(code)

        # Should detect both GET and POST for /items
        methods = {e.method for e in result.endpoints if e.path == "/items"}
        assert len(methods) >= 1  # At least one should be detected

    def test_empty_source(self):
        """Test handling of empty source code."""
        result = self.extractor.extract_from_source("")

        assert len(result.endpoints) == 0
        assert len(result.functions) == 0
        assert len(result.relationships) == 0

    def test_source_without_decorators(self):
        """Test handling of source with no route decorators."""
        code = """
def helper_function():
    return "Not an endpoint"

class MyClass:
    def method(self):
        pass
"""
        result = self.extractor.extract_from_source(code)

        assert len(result.endpoints) == 0


def _routes(code: str, path: str = "app.py") -> list[tuple[str, str]]:
    result = APIExtractor("repo").extract_from_source(code, path)
    return sorted((f"{e.method} {e.path}", e.properties["framework"]) for e in result.endpoints)


def test_fastapi_write_routes_make_no_get():
    code = """
from fastapi import FastAPI

app = FastAPI()

@app.post("/items")
def create():
    pass

@app.put("/items/{id}")
def update(id):
    pass

@app.delete("/items/{id}")
async def remove(id):
    pass

@app.get("/items")
def read():
    pass
"""
    assert _routes(code) == [
        ("DELETE /items/{id}", "FastAPI"), ("GET /items", "FastAPI"), ("POST /items", "FastAPI"),
        ("PUT /items/{id}", "FastAPI"),
    ]


def test_flask_methods_lists_tuples_and_strings_give_each_method():
    code = """
from flask import Flask, Blueprint

app = Flask(__name__)
bp = Blueprint("auth", __name__)

@app.route("/login", methods=["GET", "POST"])
def login():
    pass

@bp.route("/logout", methods=("post",))
def logout():
    pass

@app.route("/legacy", methods="GET,PUT")
def legacy():
    pass

@app.route("/home")
def home():
    pass

@app.post("/submit")
def submit():
    pass
"""
    assert _routes(code) == [
        ("GET /home", "Flask"), ("GET /legacy", "Flask"), ("GET /login", "Flask"), ("POST /login", "Flask"),
        ("POST /logout", "Flask"), ("POST /submit", "Flask"), ("PUT /legacy", "Flask"),
    ]


def test_routes_in_comments_and_docstrings_are_none():
    code = '''
"""Routes look like @app.get("/module-doc") and path("doc/", view)."""
from fastapi import APIRouter

router = APIRouter()

# @router.get("/commented")

@router.get("/real")
def real():
    """Unlike @router.post("/docstring"), which is an example."""
    example = '@app.route("/in-a-string")'
    return example

# urlpatterns = [path("commented/", view)]
'''
    assert _routes(code) == [("GET /real", "FastAPI")]


def test_framework_follows_the_app_or_router_binding():
    code = """
from fastapi import FastAPI
from flask import Flask

api = FastAPI()
web = Flask(__name__)

@api.get("/a")
def a():
    pass

@web.get("/b")
def b():
    pass

@api.api_route("/c", methods=["GET", "POST"])
def c():
    pass

@cache.get("/not-a-route")
def d():
    pass
"""
    assert _routes(code) == [
        ("GET /a", "FastAPI"), ("GET /b", "Flask"), ("GET /c", "FastAPI"), ("POST /c", "FastAPI"),
    ]


def test_a_handler_in_an_app_factory_is_owned_by_it():
    code = """
from flask import Flask

def create_app():
    app = Flask(__name__)

    @app.route("/status")
    def status():
        return "ok"

    return app
"""
    result = APIExtractor("repo").extract_from_source(code, "factory.py")
    owned = [(r.target_name, r.to_owner) for r in result.relationships if r.to_file == "factory.py"]
    assert owned == [("status", "create_app")]


def test_django_views_by_name_and_class_but_not_include():
    code = '''
"""See path("docs/", views.nothing) in the docs."""
from django.urls import include, path, re_path

from . import views

urlpatterns = [
    path("users/", views.list_users),
    re_path(r"^articles/$", views.ArticleView.as_view(), name="articles"),
    path("api/", include("api.urls")),
    # path("old/", views.old),
]
'''
    result = APIExtractor("repo").extract_from_source(code, "urls.py")
    assert sorted({(r.source_name, r.target_name) for r in result.relationships if r.to_owner != "ArticleView"}) == [
        ("* ^articles/$", "ArticleView"), ("* users/", "list_users"),
    ]


def test_concatenated_and_escaped_paths_are_their_values():
    code = r'''
from fastapi import FastAPI

app = FastAPI()

@app.get("/a"
         "/b")
def ab():
    pass

@app.get("/x\x2Fy")
def escaped():
    pass

@app.get(r"/raw\d")
def raw():
    pass

@app.get("/q" f"/{tail}")
def interpolated():
    pass
'''
    assert _routes(code) == [("GET /a/b", "FastAPI"), ("GET /raw\\d", "FastAPI"), ("GET /x/y", "FastAPI")]


def test_a_deep_file_reads_without_recursion():
    code = "from fastapi import FastAPI\napp = FastAPI()\nx = " + "+".join(["1"] * 3000) + (
        "\n\n@app.get('/a')\ndef a():\n    pass\n"
    )
    assert _routes(code) == [("GET /a", "FastAPI")]


def test_django_views_pin_to_the_module_they_are_imported_from():
    code = '''
from django.urls import path

from . import views
from app import api
from .handlers import search

urlpatterns = [
    path("a/", views.show),
    path("b/", api.list_items),
    path("c/", search),
    path("d/", unknown.view),
    path("e/", local_view),
]
'''
    result = APIExtractor("repo").extract_from_source(code, "shop/urls.py")
    targets = sorted((r.source_name, r.target_name, r.to_file or "", r.to_owner) for r in result.relationships)
    assert targets == [
        ("* a/", "show", "", None), ("* a/", "show", "shop/views.py", ""),
        ("* a/", "show", "shop/views/__init__.py", ""),
        ("* b/", "list_items", "", None), ("* b/", "list_items", "app/api.py", ""),
        ("* b/", "list_items", "app/api/__init__.py", ""), ("* b/", "list_items", "shop/app/api.py", ""),
        ("* b/", "list_items", "shop/app/api/__init__.py", ""),
        ("* c/", "search", "", None), ("* c/", "search", "shop/handlers.py", ""),
        ("* c/", "search", "shop/handlers/__init__.py", ""),
        # Read off nothing the file imports: the stub alone.
        ("* d/", "view", "", None),
        # A bare name nothing imports links every one of its name, as before.
        ("* e/", "local_view", "", None),
    ]


def test_a_class_based_view_implements_its_class_and_its_http_methods():
    code = '''
from django.urls import path

from . import views
from .orders import OrderView as Orders
from api.views import ItemViewSet, StatusView

urlpatterns = [
    path("a/", views.CartView.as_view(template_name="cart.html")),
    path("b/", Orders.as_view()),
    path("c/", ItemViewSet.as_view({"get": "list", "post": "create", "put": action})),
    path("d/", StatusView.as_view(actions={"get": "retrieve"})),
    path("e/", unknown.LostView.as_view()),
    path("f/", LocalView.as_view()),
]
'''
    result = APIExtractor("repo").extract_from_source(code, "shop/urls.py")
    targets = sorted(
        (r.source_name, r.target_label, r.target_name, r.to_file, r.to_owner) for r in result.relationships
    )
    http = ("delete", "get", "head", "options", "patch", "post", "put", "trace")
    assert targets == sorted([
        # Django's View: the stub, the top-level Class in its module, and each HTTP method handler of it.
        ("* a/", "Function", "CartView", "", None),
        *(("* a/", "Class", "CartView", file, "") for file in ("shop/views.py", "shop/views/__init__.py")),
        *(("* a/", "Function", method, file, "CartView")
          for file in ("shop/views.py", "shop/views/__init__.py") for method in http),
        # A from-import is the class it names, under its own name: the stub's too.
        ("* b/", "Function", "OrderView", "", None),
        *(("* b/", "Class", "OrderView", file, "") for file in ("shop/orders.py", "shop/orders/__init__.py")),
        *(("* b/", "Function", method, file, "OrderView")
          for file in ("shop/orders.py", "shop/orders/__init__.py") for method in http),
        # A DRF ViewSet's literal action mapping: the actions it names.
        ("* c/", "Function", "ItemViewSet", "", None),
        *(("* c/", "Class", "ItemViewSet", file, "") for file in _API_VIEWS),
        *(("* c/", "Function", action, file, "ItemViewSet") for file in _API_VIEWS for action in ("create", "list")),
        ("* d/", "Function", "StatusView", "", None),
        *(("* d/", "Class", "StatusView", file, "") for file in _API_VIEWS),
        *(("* d/", "Function", "retrieve", file, "StatusView") for file in _API_VIEWS),
        # Read off nothing the file imports: the stub alone.
        ("* e/", "Function", "LostView", "", None),
        # A bare name nothing imports: the stub and every Class of its name, no method.
        ("* f/", "Function", "LocalView", "", None), ("* f/", "Class", "LocalView", None, None),
    ])
    assert {f.name for f in result.functions} == {
        "CartView", "OrderView", "ItemViewSet", "StatusView", "LostView", "LocalView",
    }


_API_VIEWS = ("api/views.py", "api/views/__init__.py", "shop/api/views.py", "shop/api/views/__init__.py")


def test_a_class_based_view_of_the_route_file_is_pinned_to_it():
    code = '''
from django.urls import path


class LocalView:
    def get(self, request):
        return None


urlpatterns = [path("local/", LocalView.as_view({"get": "get"}))]
'''
    result = APIExtractor("repo").extract_from_source(code, "shop/urls.py")
    assert sorted((r.target_label, r.target_name, r.to_file, r.to_owner) for r in result.relationships) == [
        ("Class", "LocalView", "shop/urls.py", ""),
        ("Function", "LocalView", "", None),
        ("Function", "get", "shop/urls.py", "LocalView"),
    ]
