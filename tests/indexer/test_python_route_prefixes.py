"""Python routes under a router's or blueprint's prefix (apis/extractor.py):
FastAPI `APIRouter(prefix=)` and `include_router(prefix=)`, Flask
`Blueprint(url_prefix=)` and `register_blueprint(url_prefix=)`, in the
route's own file and, as `route_mounts`/`route_exports`, across files
(apis/routes.py MountTable). Fixture code is fictional."""

import textwrap

from devgraph.indexer.apis.extractor import APIExtractor
from devgraph.indexer.apis.routes import (
    MOUNT_PIN_SEP,
    MOUNT_SEP,
    MountTable,
    mount_routes,
)


def _extract(code: str, path: str = "app.py"):
    return APIExtractor("repo").extract_from_source(textwrap.dedent(code), path)


def _names(code: str, path: str = "app.py") -> list[str]:
    return sorted({endpoint.name for endpoint in _extract(code, path).endpoints})


def _split(entries):
    return sorted(tuple(entry.split(MOUNT_SEP)) for entry in entries or [])


def test_fastapi_router_prefix_and_include_router_prefix_join():
    code = """
        from fastapi import FastAPI, APIRouter

        app = FastAPI(root_path="/proxy")
        router = APIRouter(prefix="/orders")

        @router.get("/{order_id}")
        def get_order(order_id: int):
            pass

        @router.post("/")
        def create_order():
            pass

        @app.get("/health")
        def health():
            pass

        app.include_router(router, prefix="/api")
    """
    assert _names(code) == [
        "GET /api/orders/{order_id}",
        "GET /health",
        "POST /api/orders/",
    ]
    result = _extract(code)
    implements = {
        (r.source_name, r.target_name)
        for r in result.relationships
        if r.to_file == "app.py"
    }
    assert ("GET /api/orders/{order_id}", "get_order") in implements
    assert result.route_mounts is None and result.route_exports is None


def test_router_included_without_a_prefix_keeps_its_own():
    code = """
        from fastapi import FastAPI, APIRouter

        app = FastAPI()
        router = APIRouter(prefix="/users")

        @router.get("/me")
        def me():
            pass

        app.include_router(router)
    """
    assert _names(code) == ["GET /users/me"]


def test_nested_routers_in_one_file():
    code = """
        from fastapi import FastAPI, APIRouter

        app = FastAPI()
        api = APIRouter(prefix="/v1")
        users = APIRouter(prefix="/users")

        @users.get("/{user_id}")
        def get_user(user_id: int):
            pass

        api.include_router(users)
        app.include_router(api, prefix="/api")
    """
    assert _names(code) == ["GET /api/v1/users/{user_id}"]


def test_flask_blueprint_url_prefix():
    code = """
        from flask import Flask, Blueprint

        app = Flask(__name__)
        auth = Blueprint("auth", __name__, url_prefix="/auth")
        admin = Blueprint("admin", __name__, url_prefix="/admin")

        @auth.route("/login", methods=["POST"])
        def login():
            pass

        @admin.route("/users")
        def users():
            pass

        app.register_blueprint(auth)
        app.register_blueprint(admin, url_prefix="/staff")
    """
    # register_blueprint's url_prefix replaces the blueprint's own.
    assert _names(code) == ["GET /staff/users", "POST /auth/login"]


def test_same_relative_path_on_two_routers_stays_two_endpoints():
    """Each file's router is mounted where it doesn't show: its routes are
    its own, under its own prefix, never one shared `GET /`."""
    one = """
        from fastapi import APIRouter

        router = APIRouter(prefix="/orders")

        @router.get("/")
        def index():
            pass
    """
    two = one.replace("/orders", "/users")
    assert _names(one, "a/one.py") == ["GET ?/orders/ (a/one.py)"]
    assert _names(two, "a/two.py") == ["GET ?/users/ (a/two.py)"]
    assert _split(_extract(one, "a/one.py").route_exports) == [
        ("router", "GET", "/orders/")
    ]


def test_two_routers_of_one_file_mounted_at_their_prefixes_stay_apart():
    code = """
        from fastapi import FastAPI, APIRouter

        app = FastAPI()
        orders = APIRouter(prefix="/orders")
        users = APIRouter(prefix="/users")

        @orders.get("/")
        def list_orders():
            pass

        @users.get("/")
        def list_users():
            pass

        app.include_router(orders)
        app.include_router(users)
    """
    assert _names(code) == ["GET /orders/", "GET /users/"]


def test_a_router_with_a_dynamic_prefix_is_unmounted_and_a_dynamic_mount_is_none():
    """A prefix read from settings is unknown: the router's routes are the
    file's own, under no prefix, and no mount from another file renames
    them (they are no `route_exports`), even mounted on the file's app."""
    code = """
        from fastapi import FastAPI, APIRouter

        app = FastAPI()
        dynamic = APIRouter(prefix=PREFIX)
        router = APIRouter(prefix="/r")

        @dynamic.get("/x")
        def x():
            pass

        @router.get("/y")
        def y():
            pass

        app.include_router(router, prefix=settings.API)
        app.include_router(dynamic)
    """
    assert _names(code) == ["GET ?/r/y (app.py)", "GET ?/x (app.py)"]
    assert [entry.split(MOUNT_SEP)[0] for entry in _extract(code).route_exports] == [
        "router"
    ]


def test_mounts_of_imported_routers_are_route_mounts():
    code = """
        from fastapi import FastAPI, APIRouter
        from flask import Blueprint
        from .routers import orders
        from .users import router as users_router
        from .shop import bp

        app = FastAPI()
        app.include_router(orders.router, prefix="/api")
        app.include_router(users_router)
        api = APIRouter(prefix="/v1")
        api.include_router(users_router, prefix="/u")
        app.register_blueprint(bp, url_prefix="/shop")
        app.include_router(build_router())
    """
    mounts = _split(_extract(code, "svc/main.py").route_mounts)
    users = MOUNT_PIN_SEP.join(["svc/users.py", "svc/users/__init__.py"])
    assert mounts == sorted(
        [
            ("", "/", "router", users),
            (
                "",
                "/api",
                "router",
                MOUNT_PIN_SEP.join(
                    ["svc/routers/orders.py", "svc/routers/orders/__init__.py"]
                ),
            ),
            (
                "",
                "/shop",
                "bp:replaced",
                MOUNT_PIN_SEP.join(["svc/shop.py", "svc/shop/__init__.py"]),
            ),
            # Relative to the api router's own mounts, under its own prefix.
            ("api", "/v1/u", "router", users),
        ]
    )


def test_an_imported_router_mounted_across_files_takes_the_mount_prefix():
    router_file = _extract(
        """
        from fastapi import APIRouter

        router = APIRouter(prefix="/orders")

        @router.get("/{order_id}")
        def get_order(order_id: int):
            pass
    """,
        "svc/routers/orders.py",
    )
    main = _extract(
        """
        from fastapi import FastAPI
        from .routers import orders

        app = FastAPI()
        app.include_router(orders.router, prefix="/api")
    """,
        "svc/main.py",
    )
    table = MountTable({"svc/main.py": main.route_mounts})
    prefixes = table.route_prefixes("svc/routers/orders.py", router_file.route_exports)
    nodes = [
        {"label": "Endpoint", "name": e.name, "properties": {}}
        for e in router_file.endpoints
    ]
    mounted, _rels = mount_routes(
        nodes, [], "svc/routers/orders.py", router_file.route_exports, prefixes
    )
    assert [node["name"] for node in mounted] == ["GET /api/orders/{order_id}"]


def test_a_blueprint_registered_with_its_own_prefix_replaced_across_files():
    blueprint = _extract(
        """
        from flask import Blueprint

        bp = Blueprint("shop", __name__, url_prefix="/store")

        @bp.route("/cart")
        def cart():
            pass
    """,
        "web/shop.py",
    )
    assert _names(
        """
        from flask import Blueprint

        bp = Blueprint("shop", __name__, url_prefix="/store")

        @bp.route("/cart")
        def cart():
            pass
    """,
        "web/shop.py",
    ) == ["GET ?/store/cart (web/shop.py)"]

    def named(main_code):
        main = _extract(main_code, "web/app.py")
        table = MountTable({"web/app.py": main.route_mounts})
        prefixes = table.route_prefixes("web/shop.py", blueprint.route_exports)
        nodes = [
            {"label": "Endpoint", "name": e.name, "properties": {}}
            for e in blueprint.endpoints
        ]
        return [
            node["name"]
            for node in mount_routes(
                nodes, [], "web/shop.py", blueprint.route_exports, prefixes
            )[0]
        ]

    head = "from flask import Flask\nfrom .shop import bp\napp = Flask(__name__)\n"
    assert named(head + "app.register_blueprint(bp)\n") == ["GET /store/cart"]
    assert named(head + 'app.register_blueprint(bp, url_prefix="/s")\n') == [
        "GET /s/cart"
    ]


def test_an_unmounted_route_sharing_an_endpoint_with_an_exported_one_stays_unmounted():
    """As for Express: the Endpoint is the file's own while another of its
    routes, which no mount can rename, has the same name."""
    code = """
        from fastapi import APIRouter

        router = APIRouter()

        @router.get("/x")
        def a():
            pass

        class Views:
            def __init__(self):
                self.router = APIRouter()

                @self.router.get("/x")
                def b():
                    pass
    """
    result = _extract(code, "r.py")
    assert _names(code, "r.py") == ["GET ?/x (r.py)"]
    assert result.route_exports is None


def test_a_router_mounted_twice_in_one_file_has_a_route_per_mount():
    code = """
        from fastapi import FastAPI, APIRouter
        from .users import router as users_router

        app = FastAPI()
        api = APIRouter(prefix="/items")

        @api.get("/{item_id}")
        def get_item(item_id: int):
            pass

        app.include_router(api, prefix="/v1")
        app.include_router(api, prefix="/v2")
        v = APIRouter()
        v.include_router(users_router, prefix="/u")
        app.include_router(v, prefix="/a")
        app.include_router(v, prefix="/b")
    """
    assert _names(code, "svc/main.py") == [
        "GET /v1/items/{item_id}",
        "GET /v2/items/{item_id}",
    ]
    # A router mounted twice mounts another file's router at both prefixes.
    assert [
        entry[:2] for entry in _split(_extract(code, "svc/main.py").route_mounts)
    ] == [("", "/a/u"), ("", "/b/u")]


def test_a_route_keeps_its_trailing_slash_under_a_prefix():
    """As FastAPI serves them: "" under /o is /o, and "/" is /o/."""
    code = """
        from fastapi import FastAPI, APIRouter

        app = FastAPI()
        router = APIRouter(prefix="/o")
        loose = APIRouter(prefix="/l")

        @router.get("")
        def index():
            pass

        @router.get("/")
        def slash():
            pass

        @router.get("/items/")
        def items():
            pass

        @loose.get("")
        def loose_index():
            pass

        @loose.get("/")
        def loose_slash():
            pass

        app.include_router(router)
    """
    assert _names(code) == [
        "GET /o",
        "GET /o/",
        "GET /o/items/",
        "GET ?/l (app.py)",
        "GET ?/l/ (app.py)",
    ]


def test_a_route_keeps_its_trailing_slash_under_another_files_mount():
    router_file = _extract(
        """
        from fastapi import APIRouter

        router = APIRouter()

        @router.get("")
        def index():
            pass

        @router.get("/")
        def slash():
            pass
        """,
        "svc/r.py",
    )
    main = _extract(
        """
        from fastapi import FastAPI
        from .r import router

        app = FastAPI()
        app.include_router(router, prefix="/api")
        """,
        "svc/main.py",
    )
    prefixes = MountTable({"svc/main.py": main.route_mounts}).route_prefixes(
        "svc/r.py", router_file.route_exports
    )
    nodes = [
        {"label": "Endpoint", "name": e.name, "properties": {}}
        for e in router_file.endpoints
    ]
    mounted, _rels = mount_routes(
        nodes, [], "svc/r.py", router_file.route_exports, prefixes, keep_slash=True
    )
    assert sorted(node["name"] for node in mounted) == ["GET /api", "GET /api/"]
