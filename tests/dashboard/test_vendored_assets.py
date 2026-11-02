"""The dashboard runs offline: every script it loads ships in the package."""

import hashlib
import re
import tomllib
from pathlib import Path

from devgraph.dashboard import app as dashboard_app

STATIC = Path(dashboard_app.__file__).resolve().parent / "static"
ROOT = Path(__file__).resolve().parents[2]

# file name -> (sha256 of the file, notices heading)
VENDORED = {
    "sigma-3.0.3.min.js": (
        "58e30383ab428f832068d9d16a5215c65ba12430d438ed091c5703f398de9e16",
        "Sigma.js 3.0.3",
    ),
    "graphology-0.25.4.umd.min.js": (
        "641ea047e2f414dead999769d62567ce3c6f1ddc334f1e728bd5edb19d337977",
        "graphology 0.25.4",
    ),
}


def test_index_html_loads_no_script_from_another_host():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    sources = re.findall(r"<script\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)", html, flags=re.IGNORECASE)
    assert sources, "expected the vendored script tags"
    for src in sources:
        assert "//" not in src and not src.lower().startswith(("http:", "https:", "data:")), src
    assert "unpkg.com" not in html and "cdn.jsdelivr" not in html and "cdnjs" not in html


def test_graphology_then_sigma_load_before_the_graph_view():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    tags = [f'<script src="/static/vendor/{name}"></script>' for name in sorted(VENDORED)]
    tags.append('<script src="/static/graph-view.js"></script>')
    positions = [html.index(t) for t in tags]
    assert positions == sorted(positions)
    assert (STATIC / "graph-view.js").stat().st_size > 1_000


def test_the_vendored_files_are_package_data():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = pyproject["tool"]["setuptools"]["package-data"]["devgraph.dashboard"]
    shipped = {p for pattern in patterns for p in (STATIC.parent).glob(pattern)}
    assert STATIC / "index.html" in shipped
    assert STATIC / "graph-view.js" in shipped
    for name in VENDORED:
        assert STATIC / "vendor" / name in shipped


def test_the_vendored_files_are_the_pinned_builds_and_noted():
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "npm tarball integrity" in notices
    for name, (sha, heading) in VENDORED.items():
        assert hashlib.sha256((STATIC / "vendor" / name).read_bytes()).hexdigest() == sha
        assert f"## {heading}" in notices
        assert f"devgraph/dashboard/static/vendor/{name}" in notices
        assert f"sha256 `{sha}`" in notices
    assert "Alexis Jacomy" in notices and "Guillaume Plique" in notices
