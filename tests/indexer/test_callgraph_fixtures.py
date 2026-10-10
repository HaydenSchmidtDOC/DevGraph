"""The call-graph ground-truth fixtures (tests/fixtures/callgraph) are well
formed: every expected.json parses, has the documented shape, and names files
that exist and functions that appear in them."""

import json
import re
from pathlib import Path

import pytest

from devgraph.indexer import gitignore
from devgraph.indexer.walk import is_ignored_path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "callgraph"
LANGUAGES = ["ts", "go", "java", "kotlin", "csharp", "rust", "cpp"]

_CALL_KEYS = {"caller", "caller_file", "callee", "callee_file"}
_CALL_OPTIONAL = {"ambiguous", "not_files", "note"}
_IMPORT_KEYS = {"from_file", "to_file"}
_IMPORT_OPTIONAL = {"kind", "ambiguous", "note"}
_NO_EDGE_KEYS = {"caller", "caller_file", "callee"}
_NO_EDGE_OPTIONAL = {"note"}


def _load(lang: str) -> dict:
    return json.loads((FIXTURES / lang / "expected.json").read_text())


def _mentions(root: Path, rel: str, name: str) -> bool:
    return re.search(rf"\b{re.escape(name)}\b", (root / rel).read_text()) is not None


def _check_keys(row: dict, required: set[str], optional: set[str]) -> None:
    assert required <= row.keys(), row
    assert row.keys() <= required | optional, row


@pytest.mark.parametrize("lang", LANGUAGES)
def test_expected_json_is_well_formed(lang: str) -> None:
    root = FIXTURES / lang
    data = _load(lang)
    assert data["language"] == lang
    assert set(data) <= {"language", "description", "calls", "imports", "no_edge", "symbols", "notes"}
    notes = data["notes"]
    source_files = [p for p in root.rglob("*") if p.is_file() and p.name != "expected.json"]
    assert 15 <= len(source_files) <= 30, len(source_files)

    def file_ok(rel: str) -> None:
        assert not rel.startswith("/") and (root / rel).is_file(), f"{lang}: missing {rel}"
        assert not is_ignored_path(Path(rel)), f"{lang}: {rel} is under a directory DevGraph never walks"

    calls = data["calls"]
    keys = [tuple(c[k] for k in ("caller", "caller_file", "callee", "callee_file")) for c in calls]
    assert len(keys) == len(set(keys)), f"{lang}: duplicate calls row"
    groups: dict[tuple, list[dict]] = {}
    for c in calls:
        _check_keys(c, _CALL_KEYS, _CALL_OPTIONAL)
        file_ok(c["caller_file"])
        file_ok(c["callee_file"])
        assert _mentions(root, c["caller_file"], c["caller"]), c
        assert _mentions(root, c["callee_file"], c["callee"]), c
        for f in c.get("not_files", []):
            file_ok(f)
            assert f != c["callee_file"], c
        assert "note" not in c or c["note"] in notes, c
        groups.setdefault((c["caller"], c["caller_file"], c["callee"]), []).append(c)
    for site, rows in groups.items():
        flags = {bool(r.get("ambiguous")) for r in rows}
        assert len(flags) == 1, f"{lang}: {site} mixes ambiguous and plain rows"
        if flags == {True}:
            assert len(rows) >= 2, f"{lang}: ambiguous {site} needs at least two targets"

    pairs = [(i["from_file"], i["to_file"]) for i in data["imports"]]
    assert len(pairs) == len(set(pairs)), f"{lang}: duplicate imports row"
    for i in data["imports"]:
        _check_keys(i, _IMPORT_KEYS, _IMPORT_OPTIONAL)
        file_ok(i["from_file"])
        file_ok(i["to_file"])
        assert i["from_file"] != i["to_file"], i
        assert "note" not in i or i["note"] in notes, i

    linked = set(groups)
    for n in data["no_edge"]:
        _check_keys(n, _NO_EDGE_KEYS, _NO_EDGE_OPTIONAL)
        file_ok(n["caller_file"])
        assert _mentions(root, n["caller_file"], n["caller"]), n
        assert _mentions(root, n["caller_file"], n["callee"]), n
        assert (n["caller"], n["caller_file"], n["callee"]) not in linked, f"{lang}: {n} is also a true call"
        assert "note" not in n or n["note"] in notes, n

    for s in data.get("symbols", []):
        assert set(s) == {"name", "file", "kind"}, s
        file_ok(s["file"])
        assert _mentions(root, s["file"], s["name"]), s


def test_fixture_directories_are_hidden_from_devgraphs_own_scan() -> None:
    """DevGraph honours .gitignore files, so the fixture projects never land
    in the graph of this repository itself."""
    repo_root = FIXTURES.parents[2]
    for lang in LANGUAGES:
        rel = (FIXTURES / lang).relative_to(repo_root).as_posix()
        assert gitignore.is_gitignored(repo_root, rel, is_dir=True), rel
