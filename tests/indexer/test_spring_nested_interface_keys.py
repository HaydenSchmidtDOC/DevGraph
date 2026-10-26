"""A Spring controller implementing an interface nested in another class
(devgraph/indexer/java/extractor.py and kotlin/extractor.py
`_spring_routes`): keyed by the nested interface's qualified name through
a Kotlin import of the nested class or a class wildcard import
(`import a.api.Outer.*`), matching the interface's own key. Fixture code is
fictional."""

from devgraph.indexer.java.extractor import extract_java_file
from devgraph.indexer.kotlin.extractor import extract_kotlin_file


def _keys(result) -> dict[str, list[str] | None]:
    return {
        n.name: n.properties.get("mkeys")
        for n in result.nodes if n.label == "Function" and n.properties.get("file")
    }


def test_nested_interface_route_key():
    source = """\
package shop.api;

public class Outer {
    public interface Api {
        @GetMapping("/orders")
        String list();
    }
}
"""
    result = extract_java_file(source, "src/main/java/shop/api/Outer.java", "r")
    assert {r.to_file for r in result.relationships if r.rel_type == "IMPLEMENTS" and "@" in (r.to_file or "")} == {
        "/.?.java,.kt^t@spring:shop.api.Outer.Api",
    }


def test_java_class_wildcard_import():
    source = """\
package shop.web;

import shop.api.Outer.*;

@RestController
public class OrdersController implements Api {
    public String list() { return ""; }
}
"""
    keys = _keys(extract_java_file(source, "src/main/java/shop/web/OrdersController.java", "r"))
    assert "spring:shop.api.Outer.Api" in keys["list"]


def test_kotlin_nested_import_and_class_wildcard():
    named = """\
package shop.web

import shop.api.Outer.Api

@RestController
class OrdersController : Api {
    override fun list(): String = ""
}
"""
    keys = _keys(extract_kotlin_file(named, "src/main/kotlin/shop/web/OrdersController.kt", "r"))
    assert keys["list"] == ["spring:shop.api.Outer.Api"]
    aliased = named.replace("import shop.api.Outer.Api", "import shop.api.Outer.Api as OrdersApi").replace(
        ": Api", ": OrdersApi"
    )
    keys = _keys(extract_kotlin_file(aliased, "src/main/kotlin/shop/web/OrdersController.kt", "r"))
    assert keys["list"] == ["spring:shop.api.Outer.Api"]
    wildcard = named.replace("import shop.api.Outer.Api", "import shop.api.Outer.*")
    keys = _keys(extract_kotlin_file(wildcard, "src/main/kotlin/shop/web/OrdersController.kt", "r"))
    assert "spring:shop.api.Outer.Api" in keys["list"]
