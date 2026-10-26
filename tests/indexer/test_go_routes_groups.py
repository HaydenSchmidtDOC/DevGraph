"""Go group routes with an empty or relative path, and chi `Mount` of a
same-file router (devgraph/indexer/go/extractor.py `_go_routes`), on
fictional fixture code."""

from devgraph.indexer.go.extractor import extract_go_file


def _endpoints(result) -> set[str]:
    return {n.name for n in result.nodes if n.label == "Endpoint"}


def _implements(result) -> set[tuple]:
    return {(r.from_name, r.to_name, r.to_file) for r in result.relationships if r.rel_type == "IMPLEMENTS"}


GIN = """\
package web

import "github.com/gin-gonic/gin"

func Routes() {
\tr := gin.Default()
\tusers := r.Group("/users")
\tusers.GET("", list)
\tusers.POST("", create)
\tusers.GET("/:id", show)
\tusers.GET("me", show)
}

func list(c *gin.Context)   {}
func create(c *gin.Context) {}
func show(c *gin.Context)   {}
"""


def test_gin_group_empty_and_relative_paths():
    path = "web/routes.go"
    result = extract_go_file(GIN, path, "r")
    assert _endpoints(result) == {"GET /users", "POST /users", "GET /users/:id", "GET /users/me"}
    assert ("GET /users", "list", path) in _implements(result)


def test_echo_group_empty_path():
    source = """\
package web

import "github.com/labstack/echo/v4"

func Routes() {
\te := echo.New()
\tg := e.Group("/admin")
\tg.GET("", adminHome)
}

func adminHome(c echo.Context) error { return nil }
"""
    assert _endpoints(extract_go_file(source, "web/admin.go", "r")) == {"GET /admin"}


def test_empty_path_on_a_group_parameter_is_the_files_own():
    source = """\
package web

import "github.com/gin-gonic/gin"

func Mount(rg *gin.RouterGroup) {
\trg.GET("", list)
}

func list(c *gin.Context) {}
"""
    assert _endpoints(extract_go_file(source, "web/mount.go", "r")) == {"GET ?/ (web/mount.go)"}


def test_empty_path_on_a_non_router_is_no_route():
    source = """\
package web

import "net/http"

func load() {}

func Sync(cache *Cache, store Store) {
\tcache.Get("", load)
\tstore.Delete("key", load)
}
"""
    assert _endpoints(extract_go_file(source, "web/sync.go", "r")) == set()


CHI = """\
package web

import (
\t"net/http"

\t"github.com/go-chi/chi/v5"
)

func Routes() http.Handler {
\tr := chi.NewRouter()
\tr.Get("/health", health)
\tr.Mount("/admin", adminRouter())
\tusers := usersRouter()
\tr.Mount("/users", users)
\tapi := chi.NewRouter()
\tapi.Get("/ping", health)
\tr.Mount("/api", api)
\tr.Route("/v1", func(r chi.Router) {
\t\tr.Mount("/orders", ordersRouter())
\t})
\treturn r
}

func adminRouter() http.Handler {
\tr := chi.NewRouter()
\tr.Get("/stats", stats)
\tr.Route("/jobs", func(r chi.Router) {
\t\tr.Post("/", stats)
\t})
\treturn r
}

func usersRouter() chi.Router {
\tr := chi.NewRouter()
\tr.Get("/{id}", stats)
\treturn r
}

func ordersRouter() chi.Router {
\tr := chi.NewRouter()
\tr.Get("/", stats)
\treturn r
}

func health(w http.ResponseWriter, r *http.Request) {}
func stats(w http.ResponseWriter, r *http.Request)  {}
"""


def test_chi_mount_of_a_same_file_router():
    path = "web/chi.go"
    result = extract_go_file(CHI, path, "r")
    assert _endpoints(result) == {
        "GET /health",
        "GET /admin/stats",
        "POST /admin/jobs",
        "GET /users/{id}",
        "GET /api/ping",
        "GET /v1/orders",
    }
    assert ("GET /admin/stats", "stats", path) in _implements(result)


def test_chi_mount_of_an_unknown_router_adds_no_prefix():
    source = """\
package web

import (
\t"net/http"

\t"github.com/go-chi/chi/v5"
\t"example.com/shop/admin"
)

func Routes() http.Handler {
\tr := chi.NewRouter()
\tr.Mount("/admin", admin.Router())
\tsub := chi.NewRouter()
\tsub.Get("/a", h)
\treturn r
}

func h(w http.ResponseWriter, r *http.Request) {}
"""
    assert _endpoints(extract_go_file(source, "web/chi.go", "r")) == {"GET /a"}


def test_chi_router_mounted_at_two_prefixes_keeps_its_own_paths():
    """One constructor mounted at two prefixes: no single prefix is its
    routes', so they keep the paths the file shows."""
    source = """\
package web

import (
\t"net/http"

\t"github.com/go-chi/chi/v5"
)

func Routes() http.Handler {
\tr := chi.NewRouter()
\tr.Mount("/a", sub())
\tr.Mount("/b", sub())
\treturn r
}

func sub() http.Handler {
\tr := chi.NewRouter()
\tr.Get("/x", h)
\treturn r
}

func h(w http.ResponseWriter, r *http.Request) {}
"""
    assert _endpoints(extract_go_file(source, "web/chi.go", "r")) == {"GET /x"}
