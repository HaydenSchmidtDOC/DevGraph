"""The routes the non-Python extractors find (devgraph/indexer/apis/routes.py):
each framework's forms as Endpoints, their handler stubs and IMPLEMENTS edges,
on fictional fixture code."""

from devgraph.indexer.apis.routes import join_route
from devgraph.indexer.jsts.extractor import extract_js_file


def _endpoints(result) -> dict[str, str]:
    return {n.name: n.properties["framework"] for n in result.nodes if n.label == "Endpoint"}


def _implements(result) -> set[tuple]:
    """(endpoint, handler, to_file, to_owner) of every IMPLEMENTS edge."""
    return {
        (r.from_name, r.to_name, r.to_file, r.to_owner) for r in result.relationships if r.rel_type == "IMPLEMENTS"
    }


def _stubs(result) -> set[str]:
    return {n.name for n in result.nodes if n.label == "Function" and n.properties.get("type") == "handler"}


def test_join_route():
    assert join_route("/api/", "users/") == "/api/users"
    assert join_route("", "") == "/"
    assert join_route("api", "/{id}") == "/api/{id}"


EXPRESS = """\
import express from 'express';
import { listUsers as list } from './handlers';
import orders from '../controllers/orders';

const router = express.Router();
const app = express();

function showUser(req, res) { res.send('u'); }

router.get('/:id', showUser);
router.post('/', requireLogin, list);
router.put(`/${version}/x`, showUser);
router.delete('/a/' + suffix, showUser);
app.route('/books').get(orders.index).post((req, res) => res.end());
app.all('/health', function (req, res) {});
app.use('/api/users', router);

cache.get('/key', fallback);
axios.get('/users', { params });
$.get('/legacy', function () {});
"""


def test_express_routes_and_handlers():
    result = extract_js_file(EXPRESS, "src/routes/users.ts", "r")
    assert _endpoints(result) == {
        "GET /api/users/:id": "Express",
        "POST /api/users": "Express",
        "GET /books": "Express",
        "POST /books": "Express",
        "* /health": "Express",
    }
    implements = _implements(result)
    # A function of the file: its node there (top-level) and the stub.
    assert {("GET /api/users/:id", "showUser", "", None),
            ("GET /api/users/:id", "showUser", "src/routes/users.ts", "")} <= implements
    # An ES named import: the exported name, in the files the import can name.
    assert ("POST /api/users", "listUsers", "src/routes/handlers.ts", "") in implements
    assert ("POST /api/users", "listUsers", "", None) in implements
    # A member of a default import: any `index` in its files.
    assert ("GET /books", "index", "src/controllers/orders.ts", None) in implements
    # An inline handler implements nothing.
    assert not [e for e in implements if e[0] in ("POST /books", "* /health")]
    assert _stubs(result) == {"showUser", "listUsers", "index"}
    # Every route edge is the file's own.
    assert {r.origin for r in result.relationships if r.rel_type == "IMPLEMENTS"} == {"src/routes/users.ts"}


def test_express_router_mounted_elsewhere_is_the_files_own_endpoint():
    source = """\
const { Router } = require('express');
const orders = Router();
orders.get('/', listOrders);
async function listOrders(req, res) {}
module.exports = orders;
"""
    result = extract_js_file(source, "routes/orders.js", "r")
    # Its prefix is another file's: never merged with another file's `GET /`.
    assert _endpoints(result) == {"GET ?/ (routes/orders.js)": "Express"}
    assert ("GET ?/ (routes/orders.js)", "listOrders", "routes/orders.js", "") in _implements(result)


def test_express_needs_a_router_receiver_and_a_handler():
    source = """\
const client = makeClient();
client.get('/users', onDone);
app.get('/users');
app.get('title');
app.get('/users', 'text');
userRouter.patch('/users/:id', update);
const update = (req, res) => {};
"""
    result = extract_js_file(source, "app.js", "r")
    assert _endpoints(result) == {"PATCH ?/users/:id (app.js)": "Express"}
    assert ("PATCH ?/users/:id (app.js)", "update", "app.js", "") in _implements(result)


def test_koa_and_fastify_routes():
    source = """\
import Router from '@koa/router';
import Fastify from 'fastify';
const router = new Router();
const fastify = Fastify();
router.get('user', '/users/:id', show);
fastify.post('/items', { schema }, create);
function show(ctx) {}
function create(request, reply) {}
"""
    result = extract_js_file(source, "server.js", "r")
    # The Koa router is mounted where the file doesn't show (`app.use(router.routes())`).
    assert _endpoints(result) == {"GET ?/users/:id (server.js)": "Express", "POST /items": "Express"}
    assert _stubs(result) == {"show", "create"}


def test_express_router_held_by_a_class():
    source = """\
import express, { Router } from 'express';

export class UsersController {
  private router = Router();

  constructor(private app = express()) {
    this.app = express();
    this.router.get('/:id', this.show);
    this.app.use('/users', this.router);
    this.other.get('/ignored', this.show);
  }

  show(req, res) {}
}
"""
    result = extract_js_file(source, "src/users.controller.ts", "r")
    assert _endpoints(result) == {"GET /users/:id": "Express"}
    assert _stubs(result) == {"show"}


SPRING_JAVA = """\
package com.example.shop.web;

@RestController
@RequestMapping("/api/orders")
public class OrderController {
    @GetMapping("/{id}")
    public Order show(@PathVariable long id) { return null; }

    @PostMapping
    public Order create(@RequestBody Order order) { return order; }

    @RequestMapping(value = {"/a", "/b"}, method = {RequestMethod.PUT, RequestMethod.PATCH})
    public void change() {}

    @RequestMapping(path = "/any")
    public void any() {}

    @GetMapping(BASE + "/dynamic")
    public void dynamic() {}

    public void helper() {}

    @RestController
    public static class Nested {
        @DeleteMapping("/gone")
        public void gone() {}
    }
}

class NotAController {
    @GetMapping("/ignored")
    public void ignored() {}
}
"""


def test_spring_java_routes():
    from devgraph.indexer.java.extractor import extract_java_file

    result = extract_java_file(SPRING_JAVA, "src/main/java/com/example/shop/web/OrderController.java", "r")
    path = "src/main/java/com/example/shop/web/OrderController.java"
    assert _endpoints(result) == {
        "GET /api/orders/{id}": "Spring",
        "POST /api/orders": "Spring",
        "PUT /api/orders/a": "Spring",
        "PATCH /api/orders/a": "Spring",
        "PUT /api/orders/b": "Spring",
        "PATCH /api/orders/b": "Spring",
        "* /api/orders/any": "Spring",
        "DELETE /gone": "Spring",
    }
    implements = _implements(result)
    assert {("GET /api/orders/{id}", "show", path, "OrderController"),
            ("GET /api/orders/{id}", "show", "", None),
            ("DELETE /gone", "gone", path, "OrderController.Nested")} <= implements
    assert _stubs(result) == {"show", "create", "change", "any", "gone"}


SPRING_KOTLIN = """\
package com.example.shop.web

@RestController
@RequestMapping("/api/carts", produces = ["application/json"])
class CartController(private val carts: CartService) {
    @GetMapping("/{id}")
    fun show(@PathVariable id: Long): Cart = carts.find(id)

    @RequestMapping(value = ["/x", "/y"], method = [RequestMethod.POST])
    fun post() {}

    @PutMapping("/${'$'}{version}/z")
    fun templated() {}

    @DeleteMapping
    fun clear() {}
}
"""


def test_spring_kotlin_routes():
    from devgraph.indexer.kotlin.extractor import extract_kotlin_file

    path = "src/main/kotlin/com/example/shop/web/CartController.kt"
    result = extract_kotlin_file(SPRING_KOTLIN, path, "r")
    assert _endpoints(result) == {
        "GET /api/carts/{id}": "Spring",
        "POST /api/carts/x": "Spring",
        "POST /api/carts/y": "Spring",
        "DELETE /api/carts": "Spring",
    }
    assert ("GET /api/carts/{id}", "show", path, "CartController") in _implements(result)
    assert _stubs(result) == {"show", "post", "clear"}


ASPNET_CONTROLLER = """\
namespace Shop.Api.Controllers;

[ApiController]
[Route("api/[controller]")]
public class InvoicesController : ControllerBase
{
    [HttpGet]
    public IActionResult List() => Ok();

    [HttpGet("{id:int}", Name = "invoice")]
    public IActionResult Show(int id) => Ok();

    [HttpPost, Route("[action]")]
    public async Task<IActionResult> ImportAsync() => Ok();

    [HttpDelete("/admin/purge")]
    public void Purge() {}

    [Route(Paths.Archive)]
    public void Archive() {}

    public void NotRouted() {}
}
"""


def test_aspnet_attribute_routes():
    from devgraph.indexer.csharp.extractor import extract_csharp_file

    path = "src/Shop.Api/Controllers/InvoicesController.cs"
    result = extract_csharp_file(ASPNET_CONTROLLER, path, "r")
    assert _endpoints(result) == {
        "GET /api/Invoices": "ASP.NET Core",
        "GET /api/Invoices/{id:int}": "ASP.NET Core",
        "POST /api/Invoices/Import": "ASP.NET Core",
        "DELETE /admin/purge": "ASP.NET Core",
    }
    implements = _implements(result)
    assert {("GET /api/Invoices/{id:int}", "Show", path, "InvoicesController"),
            ("GET /api/Invoices/{id:int}", "Show", "", None),
            ("POST /api/Invoices/Import", "ImportAsync", path, "InvoicesController")} <= implements
    # The stubs never get the match keys a C# Function has.
    assert all("mkeys" not in n.properties for n in result.nodes if n.properties.get("type") == "handler")


ASPNET_MINIMAL = """\
var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();

var todos = app.MapGroup("/todos").WithTags("Todos");
todos.MapGet("/", TodoEndpoints.GetAll);
todos.MapPost("/{id}", (int id) => Results.Ok(id));
app.MapDelete("/items/{id}", TodoEndpoints.Remove);
app.MapGet("/health", Health);
app.MapGet("/" + prefix, TodoEndpoints.GetAll);

app.Run();

public static class TodoEndpoints
{
    public static IResult GetAll() => Results.Ok();
    public static IResult Remove(int id) => Results.Ok();
}
"""


def test_aspnet_minimal_api_routes():
    from devgraph.indexer.csharp.extractor import extract_csharp_file

    result = extract_csharp_file(ASPNET_MINIMAL, "src/Todo/Program.cs", "r")
    assert _endpoints(result) == {
        "GET /todos": "ASP.NET Core",
        "POST /todos/{id}": "ASP.NET Core",
        "DELETE /items/{id}": "ASP.NET Core",
        "GET /health": "ASP.NET Core",
    }
    implements = _implements(result)
    assert ("GET /todos", "GetAll", "src/Todo/Program.cs", "TodoEndpoints") in implements
    assert ("DELETE /items/{id}", "Remove", "src/Todo/Program.cs", "TodoEndpoints") in implements
    # `Health` is no method of the file: its stub alone.
    assert {e for e in implements if e[0] == "GET /health"} == {("GET /health", "Health", "", None)}
    assert not [e for e in implements if e[0] == "POST /todos/{id}"]


GO_ROUTES = """\
package api

import (
\t"net/http"

\t"example.com/shop/internal/handlers"
)

func listItems(w http.ResponseWriter, r *http.Request) {}

func Register(mux *http.ServeMux, r *gin.Engine, c chi.Router) {
\tmux.HandleFunc("GET /items/{id}", listItems)
\thttp.HandleFunc("/legacy", listItems)
\tmux.Handle("POST shop.example.com/items", http.HandlerFunc(handlers.Create))
\tmux.HandleFunc("/inline", func(w http.ResponseWriter, r *http.Request) {})
\tv1 := r.Group("/v1")
\tv1.GET("/users/:id", handlers.ShowUser)
\tr.Any("/ping", listItems)
\tr.Handle("DELETE", "/users/:id", handlers.DeleteUser)
\tc.Route("/orders", func(c chi.Router) {
\t\tc.Get("/", listItems)
\t\tc.Method("PUT", "/{id}", http.HandlerFunc(listItems))
\t})
\tclient.Post("/upload", "text/plain", body)
\tcache.Get("/key", &value)
\tmux.HandleFunc(base+"/x", listItems)
}
"""


def test_go_routes():
    from devgraph.indexer.go.extractor import extract_go_file

    path = "internal/api/routes.go"
    result = extract_go_file(GO_ROUTES, path, "r")
    assert _endpoints(result) == {
        "GET /items/{id}": "Go",
        "* /legacy": "Go",
        "POST /items": "Go",
        "* /inline": "Go",
        "GET /v1/users/:id": "Go",
        "* /ping": "Go",
        "DELETE /users/:id": "Go",
        "GET /orders": "Go",
        "PUT /orders/{id}": "Go",
    }
    implements = _implements(result)
    # A function of the file, as a call to it resolves: the file, owner "".
    assert ("GET /items/{id}", "listItems", path, "") in implements
    assert ("GET /items/{id}", "listItems", "", None) in implements
    assert not [e for e in implements if e[0] == "* /inline"]
    assert _stubs(result) == {"listItems", "Create", "ShowUser", "DeleteUser"}
