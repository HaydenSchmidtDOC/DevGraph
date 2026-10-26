"""Spring property placeholders in mappings (devgraph/indexer/apis/routes.py
`spring_path`): `${name:default}` is its default, and `${name}` a prefix
the code doesn't show, the route's Endpoint then its file's own
(`unmounted_endpoint`), for controllers and interfaces, at type and
method level, in Java and Kotlin. Fixture code is fictional."""

from devgraph.indexer.apis.routes import spring_path
from devgraph.indexer.java.extractor import extract_java_file
from devgraph.indexer.kotlin.extractor import extract_kotlin_file


def _endpoints(result) -> set[str]:
    return {n.name for n in result.nodes if n.label == "Endpoint"}


def test_spring_path():
    assert spring_path("${api.base:/v2}/pets") == ("/v2/pets", True)
    assert spring_path("${api.base:}/pets") == ("/pets", True)
    assert spring_path("${api.base}/pets") == ("/pets", False)
    assert spring_path("/pets/${api.id}") is None
    assert spring_path("/api/${api.base}", prefix=True) == ("", False)
    assert spring_path("/plain") == ("/plain", True)


CONTROLLER = """\
package shop.web;

@RestController
@RequestMapping("{prefix}")
public class PetsController {{
    @GetMapping("/pets")
    public String list() {{ return ""; }}

    @GetMapping("${{pets.find:/find}}")
    public String find() {{ return ""; }}

    @GetMapping("${{pets.extra}}/extra")
    public String extra() {{ return ""; }}

    @GetMapping("/pets/${{pets.id}}")
    public String dynamic() {{ return ""; }}
}}
"""
PATH = "src/main/java/shop/web/PetsController.java"


def test_java_controller_placeholders():
    defaulted = extract_java_file(CONTROLLER.format(prefix="${openapi.base-path:/v2}"), PATH, "r")
    assert _endpoints(defaulted) == {"GET /v2/pets", "GET /v2/find", f"GET ?/extra ({PATH})"}
    empty = extract_java_file(CONTROLLER.format(prefix="${openapi.base-path:}"), PATH, "r")
    assert _endpoints(empty) == {"GET /pets", "GET /find", f"GET ?/extra ({PATH})"}
    unknown = extract_java_file(CONTROLLER.format(prefix="${openapi.base-path}"), PATH, "r")
    assert _endpoints(unknown) == {f"GET ?/pets ({PATH})", f"GET ?/find ({PATH})", f"GET ?/extra ({PATH})"}


def test_java_interface_placeholders():
    source = """\
package shop.api;

@RequestMapping("${openapi.petstore.base-path:/v2}")
public interface PetApi {
    @GetMapping("/pet/{petId}")
    Pet getPetById(Long petId);
}
"""
    assert _endpoints(extract_java_file(source, "src/main/java/shop/api/PetApi.java", "r")) == {
        "GET /v2/pet/{petId}",
    }


def test_kotlin_escaped_placeholders():
    path = "src/main/kotlin/shop/web/PetsController.kt"
    source = """\
package shop.web

@RestController
@RequestMapping("\\${openapi.base-path:/v2}")
class PetsController {
    @GetMapping("/pets")
    fun list(): String = ""

    @GetMapping("\\${pets.extra}/extra")
    fun extra(): String = ""
}
"""
    assert _endpoints(extract_kotlin_file(source, path, "r")) == {"GET /v2/pets", f"GET ?/extra ({path})"}
