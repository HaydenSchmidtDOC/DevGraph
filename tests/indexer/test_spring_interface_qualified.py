"""Spring interface routes keyed by the interface's qualified name
(devgraph/indexer/apis/routes.py `spring_type_names`): two `OrdersApi`
interfaces of different packages don't cross-link, the controller's
interface resolved through its qualified text, an explicit import, its own
package or a wildcard import, in Java and Kotlin. Fixture code is
fictional."""

from devgraph.indexer.apis.routes import spring_type_names
from devgraph.indexer.java.extractor import extract_java_file
from devgraph.indexer.kotlin.extractor import extract_kotlin_file


def _keys(result) -> dict[str, list[str]]:
    return {
        n.name: n.properties.get("mkeys")
        for n in result.nodes if n.label == "Function" and n.properties.get("file")
    }


def _pins(result) -> set[str]:
    return {r.to_file for r in result.relationships if r.rel_type == "IMPLEMENTS" and "@" in (r.to_file or "")}


def test_spring_type_names():
    imported = {"OrdersApi": "com.shop.v1.api.OrdersApi", "Outer": "com.shop.Outer"}
    assert spring_type_names("com.x.AuditApi", ["app"], imported, []) == ["com.x.AuditApi"]
    assert spring_type_names("OrdersApi", ["app"], imported, [["lib"]]) == ["com.shop.v1.api.OrdersApi"]
    assert spring_type_names("Outer.Api", ["app"], imported, []) == ["com.shop.Outer.Api"]
    assert spring_type_names("UsersApi", ["app"], imported, [["lib"], ["x", "y"]]) == [
        "app.UsersApi", "lib.UsersApi", "x.y.UsersApi",
    ]
    assert spring_type_names("UsersApi", [], {}, []) == ["UsersApi"]


def test_interfaces_of_two_packages_have_their_own_keys():
    api = """\
package com.shop.{v}.api;

public interface OrdersApi {{
    @GetMapping("/{v}/orders/{{id}}")
    Order getOrder(Long id);
}}
"""
    v1 = extract_java_file(api.format(v="v1"), "src/main/java/com/shop/v1/api/OrdersApi.java", "r")
    v2 = extract_java_file(api.format(v="v2"), "src/main/java/com/shop/v2/api/OrdersApi.java", "r")
    assert _pins(v1) == {"/.?.java,.kt^t@spring:com.shop.v1.api.OrdersApi"}
    assert _pins(v2) == {"/.?.java,.kt^t@spring:com.shop.v2.api.OrdersApi"}


def test_java_controller_keys_by_import_package_and_wildcard():
    source = """\
package com.shop.web;

import com.shop.v1.api.OrdersApi;
import com.shop.common.*;

@RestController
public class OrdersController implements OrdersApi, UsersApi, com.shop.audit.AuditApi {
    public Order getOrder(Long id) { return null; }
}
"""
    keys = _keys(extract_java_file(source, "src/main/java/com/shop/web/OrdersController.java", "r"))
    assert keys["getOrder"] == [
        "spring:com.shop.audit.AuditApi", "spring:com.shop.common.UsersApi", "spring:com.shop.v1.api.OrdersApi",
        "spring:com.shop.web.UsersApi",
    ]


def test_kotlin_controller_and_interface_keys():
    controller = """\
package com.shop.web

import com.shop.v2.api.OrdersApi as Orders

@RestController
class OrdersController : Orders {
    override fun getOrder(id: Long): Order = TODO()
}
"""
    keys = _keys(extract_kotlin_file(controller, "src/main/kotlin/com/shop/web/OrdersController.kt", "r"))
    assert keys["getOrder"] == ["spring:com.shop.v2.api.OrdersApi"]
    api = """\
package com.shop.v2.api

interface OrdersApi {
    @GetMapping("/orders/{id}")
    fun getOrder(id: Long): Order
}
"""
    result = extract_kotlin_file(api, "src/main/kotlin/com/shop/v2/api/OrdersApi.kt", "r")
    assert _pins(result) == {"/.?.java,.kt^t@spring:com.shop.v2.api.OrdersApi"}
