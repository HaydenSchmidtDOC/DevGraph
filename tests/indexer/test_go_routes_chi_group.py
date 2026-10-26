"""chi `Group(func(r chi.Router) {...})` routes under the prefix of the
router it is on (devgraph/indexer/go/extractor.py `_go_routes`), on
fictional fixture code."""

from devgraph.indexer.go.extractor import extract_go_file

CHI = """\
package web

import (
\t"net/http"

\t"github.com/go-chi/chi/v5"
)

func h(w http.ResponseWriter, r *http.Request) {}
"""


def _endpoints(source: str) -> set[str]:
    return {n.name for n in extract_go_file(CHI + source, "web/r.go", "r").nodes if n.label == "Endpoint"}


def test_chi_group_inside_a_mounted_router():
    assert _endpoints("""
func Main() {
\tr := chi.NewRouter()
\tr.Mount("/admin", adminRouter())
}

func adminRouter() chi.Router {
\tr := chi.NewRouter()
\tr.Group(func(r chi.Router) {
\t\tr.Get("/g", h)
\t})
\treturn r
}
""") == {"GET /admin/g"}


def test_chi_group_inside_a_route_and_at_the_root():
    assert _endpoints("""
func Main() {
\tr := chi.NewRouter()
\tr.Route("/api", func(r chi.Router) {
\t\tr.Group(func(r chi.Router) {
\t\t\tr.Get("/g", h)
\t\t})
\t})
\tr.Group(func(g chi.Router) {
\t\tg.Post("/p", h)
\t})
}
""") == {"GET /api/g", "POST /p"}

