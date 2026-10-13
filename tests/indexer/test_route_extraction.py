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


def test_express_router_mounted_elsewhere_keeps_its_own_paths():
    source = """\
const { Router } = require('express');
const orders = Router();
orders.get('/', listOrders);
async function listOrders(req, res) {}
module.exports = orders;
"""
    result = extract_js_file(source, "routes/orders.js", "r")
    assert _endpoints(result) == {"GET /": "Express"}


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
    assert _endpoints(result) == {"PATCH /users/:id": "Express"}
    assert ("PATCH /users/:id", "update", "app.js", "") in _implements(result)


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
    assert _endpoints(result) == {"GET /users/:id": "Express", "POST /items": "Express"}
    assert _stubs(result) == {"show", "create"}


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
