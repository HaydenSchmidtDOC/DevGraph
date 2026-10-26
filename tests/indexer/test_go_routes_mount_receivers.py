"""chi `Mount` read only on a router (devgraph/indexer/go/extractor.py
`_go_routes`), on fictional fixture code."""

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


def test_a_mount_on_something_not_a_router_mounts_nothing():
    assert _endpoints("""
func Main(fs *vfs.FS) {
\tr := chi.NewRouter()
\tfs.Mount("/m", r)
\tr.Get("/x", h)
}
""") == {"GET /x"}

