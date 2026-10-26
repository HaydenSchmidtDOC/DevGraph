"""A Spring controller with a `@RequestMapping` of its own maps its
interface's methods under that prefix, which the interface's routes don't
name: its methods carry no interface key, so they implement none of them
(devgraph/indexer/java/extractor.py and kotlin/extractor.py
`_spring_routes`). Fixture code is fictional."""

from devgraph.indexer.java.extractor import extract_java_file
from devgraph.indexer.kotlin.extractor import extract_kotlin_file


def _keys(result) -> dict[str, list[str] | None]:
    return {
        n.name: n.properties.get("mkeys")
        for n in result.nodes if n.label == "Function" and n.properties.get("file")
    }


def test_java_controller_with_its_own_prefix_gets_no_key():
    source = """\
package shop.web;

import shop.api.CrudApi;

@RestController
@RequestMapping("/orders")
public class OrdersController implements CrudApi<Order, Long> {
    public Order get(Long id) { return null; }
    @GetMapping("/own") public String own() { return ""; }
}
"""
    result = extract_java_file(source, "src/main/java/shop/web/OrdersController.java", "r")
    assert _keys(result) == {"get": None, "own": None}
    assert {n.name for n in result.nodes if n.label == "Endpoint"} == {"GET /orders/own"}


def test_kotlin_controller_with_its_own_prefix_gets_no_key():
    source = """\
package shop.web

import shop.api.CrudApi

@RestController
@RequestMapping("/orders")
class OrdersController : CrudApi<Order, Long> {
    override fun get(id: Long): Order = TODO()
}
"""
    assert _keys(extract_kotlin_file(source, "src/main/kotlin/shop/web/OrdersController.kt", "r")) == {"get": None}
