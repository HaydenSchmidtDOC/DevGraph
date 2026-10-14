"""The dashboard runs offline: every script it loads ships in the package."""

import re
import tomllib
from pathlib import Path

from devgraph.dashboard import app as dashboard_app

STATIC = Path(dashboard_app.__file__).resolve().parent / "static"
ROOT = Path(__file__).resolve().parents[2]


def test_index_html_loads_no_script_from_another_host():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    sources = re.findall(r"<script\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)", html, flags=re.IGNORECASE)
    assert sources, "expected the vendored Cytoscape script tag"
    for src in sources:
        assert "//" not in src and not src.lower().startswith(("http:", "https:", "data:")), src
    assert "unpkg.com" not in html and "cdn.jsdelivr" not in html and "cdnjs" not in html


def test_cytoscape_is_vendored_at_its_pinned_version():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '<script src="/static/vendor/cytoscape-3.30.2.min.js"></script>' in html
    vendored = STATIC / "vendor" / "cytoscape-3.30.2.min.js"
    assert vendored.stat().st_size > 100_000
    assert '"3.30.2"' in vendored.read_text(encoding="utf-8")


def test_the_vendored_files_are_package_data():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = pyproject["tool"]["setuptools"]["package-data"]["devgraph.dashboard"]
    shipped = {p for pattern in patterns for p in (STATIC.parent).glob(pattern)}
    assert STATIC / "index.html" in shipped
    assert STATIC / "vendor" / "cytoscape-3.30.2.min.js" in shipped


def test_the_vendored_license_is_noted():
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "Cytoscape.js 3.30.2" in notices and "The Cytoscape Consortium" in notices
    assert "devgraph/dashboard/static/vendor/cytoscape-3.30.2.min.js" in notices


VENDORED_SHA256 = "83e8c54a6bec655bfd81df07df605649c268af69aeca67a5ea2da54ea42dac81"


def test_the_vendored_file_is_the_pinned_build():
    import hashlib

    data = (STATIC / "vendor" / "cytoscape-3.30.2.min.js").read_bytes()
    assert hashlib.sha256(data).hexdigest() == VENDORED_SHA256
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert f"sha256 `{VENDORED_SHA256}`" in notices
    assert "npm tarball integrity" in notices
