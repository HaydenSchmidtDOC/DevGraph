"""Spring mappings declared on an interface (the OpenAPI-generator
pattern) in Java and Kotlin (devgraph/indexer/apis/routes.py
`spring_routes`): the interface's mapped methods are Endpoints, implemented
by the interface's method and, by a match key, the same-named method of
each controller class implementing it. Fixture code is fictional."""

from devgraph.indexer.java.extractor import extract_java_file
from devgraph.indexer.kotlin.extractor import extract_kotlin_file

IMPLEMENTERS = "/.?.java,.kt^t@spring:shop.api.OrdersApi"

JAVA_API = """\
package shop.api;

@RequestMapping("/api/v1")
public interface OrdersApi {
    @GetMapping("/orders/{id}")
    Order getOrder(Long id);

    @RequestMapping(value = "/orders", method = RequestMethod.POST)
    Order createOrder(Order order);

    Order unmapped();
}
"""

JAVA_CONTROLLER = """\
package shop.web;

import shop.api.OrdersApi;

@RestController
public class OrdersController implements OrdersApi, Auditable {
    public Order getOrder(Long id) { return null; }
    public Order createOrder(Order order) { return null; }
    private void helper() {}
}
"""


def _endpoints(result) -> set[str]:
    return {n.name for n in result.nodes if n.label == "Endpoint"}


def _implements(result) -> set[tuple]:
    return {
        (r.from_name, r.to_name, r.to_file, r.to_owner) for r in result.relationships if r.rel_type == "IMPLEMENTS"
    }


def _mkeys(result) -> dict[tuple[str, str], list[str]]:
    return {
        (n.name, n.properties["owner"]): n.properties.get("mkeys")
        for n in result.nodes if n.label == "Function" and n.properties.get("file")
    }


def test_java_interface_mappings_are_endpoints():
    path = "src/main/java/shop/api/OrdersApi.java"
    result = extract_java_file(JAVA_API, path, "r")
    assert _endpoints(result) == {"GET /api/v1/orders/{id}", "POST /api/v1/orders"}
    implements = _implements(result)
    assert ("GET /api/v1/orders/{id}", "getOrder", path, "OrdersApi") in implements
    assert ("GET /api/v1/orders/{id}", "getOrder", IMPLEMENTERS, None) in implements
    assert ("GET /api/v1/orders/{id}", "getOrder", "", None) in implements


def test_java_controller_methods_carry_the_interface_key():
    result = extract_java_file(JAVA_CONTROLLER, "src/main/java/shop/web/OrdersController.java", "r")
    keys = _mkeys(result)
    expected = ["spring:shop.api.OrdersApi", "spring:shop.web.Auditable"]
    assert keys[("getOrder", "OrdersController")] == expected
    assert keys[("helper", "OrdersController")] == expected
    assert _endpoints(result) == set()


def test_a_class_that_is_no_controller_gets_no_key():
    source = JAVA_CONTROLLER.replace("@RestController\n", "")
    assert all(not each for each in _mkeys(extract_java_file(source, "Orders.java", "r")).values())


def test_a_feign_client_interface_is_no_route():
    source = """\
@FeignClient(name = "orders")
public interface OrdersClient {
    @GetMapping("/orders/{id}")
    Order getOrder(Long id);
}
"""
    assert _endpoints(extract_java_file(source, "OrdersClient.java", "r")) == set()


KOTLIN_API = """\
package shop.api

@RequestMapping("/api/v1")
interface OrdersApi {
    @GetMapping("/orders/{id}")
    fun getOrder(id: Long): Order
}
"""

KOTLIN_CONTROLLER = """\
package shop.web

import shop.api.OrdersApi

@RestController
class OrdersController(private val service: OrderService) : OrdersApi {
    override fun getOrder(id: Long): Order = service.find(id)
}
"""


def test_kotlin_interface_mappings_and_controller_keys():
    path = "src/main/kotlin/shop/api/OrdersApi.kt"
    result = extract_kotlin_file(KOTLIN_API, path, "r")
    assert _endpoints(result) == {"GET /api/v1/orders/{id}"}
    implements = _implements(result)
    assert ("GET /api/v1/orders/{id}", "getOrder", path, "OrdersApi") in implements
    assert ("GET /api/v1/orders/{id}", "getOrder", IMPLEMENTERS, None) in implements
    controller = extract_kotlin_file(KOTLIN_CONTROLLER, "src/main/kotlin/shop/web/OrdersController.kt", "r")
    assert _mkeys(controller)[("getOrder", "OrdersController")] == ["spring:shop.api.OrdersApi"]
