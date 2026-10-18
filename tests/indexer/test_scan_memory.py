"""A batch's extractions are held compactly between index_paths' passes, pass 2
writes no batch file's nodes again, and full scans run one at a time. Stub
engines, no Neo4j: test_graph_accuracy_fuzz.py checks the graph itself."""

import logging
import shutil
import threading
import time
import tracemalloc
from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from devgraph.indexer import dispatch
from devgraph.indexer.dispatch import _Extractions, full_scan, index_paths
from tests.helpers import stub

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "callgraph"

PYTHON = {
    "app/models.py": (
        "class Base:\n    def save(self):\n        return 1\n\n"
        "class User(Base):\n    def save(self):\n        return super().save()\n"
    ),
    "app/service.py": (
        "from app.models import User\nimport sqlite3\n\n"
        "def run():\n    conn = sqlite3.connect('shop.db')\n    return User().save()\n"
    ),
}


class RecordingEngine:
    """Answers every read empty and records every call."""

    _ANSWERS = {
        "route_mount_state": {}, "list_file_nodes": set(), "list_shared_claims": {}, "file_heir_open": {},
        "run_cypher": [], "read_resolver_config": {}, "find_handler_stub_sources": set(), "find_name_refs": [],
        "find_mentioning_documents": set(),
    }

    def __init__(self):
        self.calls = defaultdict(list)

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls[name].append(args)
            return self._ANSWERS.get(name)

        return call


@pytest.fixture
def repo(tmp_path):
    """Every call-graph fixture, plus a few Python files, as one repository."""
    root = tmp_path / "repo"
    shutil.copytree(FIXTURES, root)
    (root / ".gitignore").unlink()  # it ignores every fixture, for DevGraph's own scan
    for rel, text in PYTHON.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


@pytest.fixture
def added(monkeypatch):
    """Every (lang, rel_path, nodes, rels, content) `_Extractions.add` is given, in order."""
    seen = []
    real = _Extractions.add

    def add(self, lang, rel_path, nodes, rels, content=None):
        seen.append((lang, rel_path, nodes, rels, content))
        return real(self, lang, rel_path, nodes, rels, content)

    stub(monkeypatch, _Extractions, "add", add)
    return seen


def _scan(engine, root, paths=None):
    """index_paths as full_scan calls it."""
    paths = paths if paths is not None else {p for p in root.rglob("*") if p.is_file()}
    return index_paths(engine, "_unit_scan", root, paths, sync_provider=False, relink_outside=False,
                       skipped={}, resolver={})


# --- the store ---------------------------------------------------------------------


def _node(label, name, **properties):
    return {"label": label, "repo_id": "r", "name": name, "properties": properties}


def test_the_store_returns_each_extraction_exactly_as_given():
    nodes = [_node("Module", "a.py", source_file="a.py", heir_open=["B", "A"]),
             _node("Function", "f", file="a.py", owner="", mkeys=("k2", "k1"), lines=(1, 2))]
    rels = [{"from_label": "Module", "from_name": "a.py", "rel_type": "CONTAINS", "to_label": "Function",
             "to_name": "f", "repo_id": "r", "properties": {"line": 3}, "origin": "a.py"}]
    with _Extractions() as store:
        store.add("py", "a.py", nodes, rels, "def f(): ünïcode\n")
        store.add("go", "b.go", [_node("Module", "b.go")], [])
        (lang, rel, entry), second = list(store.entries())
        assert (lang, rel) == ("py", "a.py") and second[:2] == ("go", "b.go")
        assert store.load(entry) == (nodes, rels, "def f(): ünïcode\n")
        assert store.load(entry)[0][1]["properties"]["lines"] == (1, 2)  # a tuple stays a tuple
        assert entry.nodes == (("Module", "a.py", None, None, None, None, None),
                               ("Function", "f", "a.py", "", ("k2", "k1"), None, None))
        assert entry.heir_open == (("a.py", ("A", "B")),)
        assert store.load(second[2]) == ([_node("Module", "b.go")], [], None)


def test_a_file_added_again_keeps_its_place_with_its_latest_extraction():
    with _Extractions() as store:
        store.add("js", "a.ts", [_node("Module", "a.ts", v=1)], [])
        store.add("js", "b.ts", [_node("Module", "b.ts")], [])
        store.add("js", "a.ts", [_node("Module", "a.ts", v=2)], [])
        entries = list(store.entries())
        assert [rel for _lang, rel, _e in entries] == ["a.ts", "b.ts"]
        assert store.load(entries[0][2])[0][0]["properties"]["v"] == 2


def test_a_discarded_file_is_left_out_of_pass_two():
    with _Extractions() as store:
        store.add("js", "a.ts", [_node("Module", "a.ts")], [])
        store.discard("a.ts")
        store.discard("never.ts")
        assert list(store.entries()) == []


def test_only_files_added_after_the_match_keys_are_late():
    with _Extractions() as store:
        store.add("py", "a.py", [], [])
        store.late = True
        store.add("py", "b.py", [], [])
        assert [entry.late for _lang, _rel, entry in store.entries()] == [False, True]


def test_the_spill_file_is_closed_with_the_batch():
    store = _Extractions()
    with store:
        store.add("py", "a.py", [], [])
        spill = store._spill
    assert spill.closed


# --- the steps between the passes read the same as from the full extractions -------


def _reference(extracted):
    """_batch_nodes' code part, _match_keys, _projects, _heir_open and the route and
    client-store maps as index_paths computed them from the full node dicts."""
    batch = {
        (n["label"], n["name"], rel if n["label"] == "Module" else n["properties"]["file"],
         n["properties"].get("owner") or "")
        for _lang, rel, nodes, _rels, _c in extracted for n in nodes
        if n["label"] == "Module" or n["properties"].get("file")
    } | {
        (n["label"], n["name"], "", "")
        for _lang, rel, nodes, _rels, _c in extracted for n in nodes
        if not n["properties"].get("file") and n["properties"].get("source") == rel
    }

    def key(n):
        return n["label"], n["name"], n["properties"]["file"], n["properties"].get("owner") or ""

    filed = [n for _lang, _rel, nodes, _rels, _c in extracted for n in nodes if n["properties"].get("file")]
    match_keys = {key(n): None if n["properties"].get("mkeys") is None
                  else tuple(sorted(set(n["properties"]["mkeys"]))) for n in filed}
    projects = {key(n): n["properties"].get("proj") for n in filed}
    heir_open = {n["name"]: tuple(sorted(n["properties"]["heir_open"]))
                 for _lang, _rel, nodes, _rels, _c in extracted for n in nodes
                 if n["label"] == "Module" and "heir_open" in n["properties"]}
    routes = {rel: [n["name"] for n in nodes if n["label"] == "Endpoint"]
              for lang, rel, nodes, _rels, _c in extracted if lang != "py"}
    return batch, match_keys, projects, heir_open, {rel: names for rel, names in routes.items() if names}


def test_the_compact_index_reads_as_the_full_extractions_did(repo, added):
    store = _Extractions()
    config = dispatch.ResolverConfig(repo)
    mounts = dispatch._Mounts(MagicMock(), "r")
    with store:
        for path in sorted(p for p in repo.rglob("*") if p.is_file()):
            rel = path.relative_to(repo).as_posix()
            dispatch._index_single_path(MagicMock(), "r", repo, path.resolve(), rel, None, False, config,
                                        store, [], [], set(), mounts)
        assert {lang for lang, *_ in added} == set(_Extractions.LANGUAGES)  # every language is exercised
        # Last extraction of each file, as index_paths' dicts kept it.
        latest = {rel: (lang, rel, nodes, rels, content) for lang, rel, nodes, rels, content in added}
        batch, match_keys, projects, heir_open, routes = _reference(list(latest.values()))
        assert dispatch._batch_nodes("r", repo, store, [], [], set(), set()) == batch
        assert dispatch._match_keys(store) == match_keys
        assert dispatch._projects(store) == projects
        assert dispatch._heir_open(store) == heir_open
        assert any(keys for keys in match_keys.values()) and heir_open  # the fixtures hold both
        assert {rel: [n[1] for n in entry.nodes if n[0] == "Endpoint"]
                for lang, rel, entry in store.entries() if lang != "py"
                if any(n[0] == "Endpoint" for n in entry.nodes)} == routes
        for lang, rel, entry in store.entries():
            assert store.load(entry) == latest[rel][2:]


def test_no_extraction_writes_a_node_another_file_owns(repo, added):
    """Why pass 2 need not write a batch file's nodes again: no other file's
    replace can delete them, since every node a file writes is its own
    (its Module, or a symbol whose `file` is the file) or file-less."""
    _scan(RecordingEngine(), repo)
    assert added
    for _lang, rel, nodes, _rels, _content in added:
        for node in nodes:
            props = node["properties"]
            if node["label"] == "Module":
                assert node["name"] == rel
            else:
                assert props.get("file") in (None, "", rel), (rel, node)
                assert props.get("source_file") in (None, rel), (rel, node)


# --- pass 2 --------------------------------------------------------------------------


def test_pass_two_writes_each_files_edges_but_not_its_nodes(repo, added):
    engine = RecordingEngine()
    _scan(engine, repo)
    code_files = {rel for _lang, rel, *_ in added}
    assert len(engine.calls["replace_file_nodes"]) == len(added)
    assert engine.calls["upsert_nodes"] == []
    pass_two = [args[0] for args in engine.calls["upsert_relationships"]][:len(code_files)]
    latest = {rel: rels for _lang, rel, _nodes, rels, _c in added}
    order = {lang: i for i, lang in enumerate(_Extractions.LANGUAGES)}
    first = {}
    for lang, rel, *_ in added:
        first.setdefault(rel, (order[lang], len(first)))
    assert pass_two == [latest[rel] for rel in sorted(first, key=first.get)]


def test_a_referrer_has_its_nodes_written_again(repo, added, monkeypatch):
    """A referrer is indexed after the batch's match keys are written, so pass 2
    writes its nodes as it always did."""
    late = "app/models.py"
    stub(monkeypatch, dispatch, "_find_referrers", lambda *a: {late})
    engine = RecordingEngine()
    index_paths(engine, "_unit_scan", repo, {repo / "app" / "service.py"}, sync_provider=False, resolver={})
    referrer_nodes = [nodes for _lang, rel, nodes, _rels, _c in added if rel == late]
    assert [args[0] for args in engine.calls["upsert_nodes"]] == referrer_nodes


def test_a_file_left_out_after_its_extraction_writes_nothing_in_pass_two(tmp_path, monkeypatch):
    """A router file the mounts pass finds too large now is deleted, and pass 2
    must not write its first extraction's edges back."""
    (tmp_path / "a.py").write_text("def a():\n    return 1\n")
    real = dispatch._skip_reason
    calls = []

    def skip_second_time(*args):
        calls.append(args)
        return "too large" if len(calls) > 1 else real(*args)

    stub(monkeypatch, dispatch, "_skip_reason", skip_second_time)
    stub(monkeypatch, dispatch, "_stale_mounts", lambda state, table: {"a.py"})
    engine = RecordingEngine()
    engine._ANSWERS = {**engine._ANSWERS, "route_mount_state": {"a.py": (None, None, None)}}  # mounts touched
    _scan(engine, tmp_path)
    assert engine.calls["delete_nodes_by_source_file"] == [("_unit_scan", "a.py")]
    assert engine.calls["upsert_relationships"] == []


def test_progress_is_logged_for_a_long_batch(tmp_path, monkeypatch, caplog):
    for i in range(5):
        (tmp_path / f"m{i}.py").write_text("x = 1\n")
    monkeypatch.setattr(dispatch, "PROGRESS_EVERY", 2)
    stub(monkeypatch, dispatch, "_index_single_path", lambda *a: 1)
    with caplog.at_level(logging.INFO, logger="devgraph.indexer.dispatch"):
        _scan(RecordingEngine(), tmp_path)
    assert [r.getMessage() for r in caplog.records if "indexed" in r.getMessage()] == [
        "_unit_scan: indexed 2 / 5 files", "_unit_scan: indexed 4 / 5 files",
    ]


# --- memory ---------------------------------------------------------------------------


def _small_repo(root, count):
    for i in range(count):
        folder = root / f"pkg{i // 100}"
        folder.mkdir(parents=True, exist_ok=True)
        other = (i + 1) % count
        (folder / f"m{i}.py").write_text(
            f"from pkg{other // 100}.m{other} import f{other}\n\n"
            + "".join(f"def f{i}_{k}(x):\n    return f{other}(x) + f{i}_{(k + 1) % 6}(x)\n\n" for k in range(6))
            + f"class C{i}:\n    def run(self):\n        return f{i}_0(1)\n"
        )


def test_a_large_batch_holds_its_extractions_compactly(tmp_path):
    """1,000 small files under a fixed bound: holding their node and edge dicts
    to the end, the batch peaked at about 50 MB here; held compactly, at 10."""
    _small_repo(tmp_path, 1_000)
    tracemalloc.start()
    try:
        _scan(RecordingEngineWithoutLog(), tmp_path)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 25 * 2**20, f"peak {peak / 2**20:.1f} MB"


class RecordingEngineWithoutLog(RecordingEngine):
    """Keeps no arguments, so the memory test measures the batch, not the stub."""

    def __getattr__(self, name):
        return lambda *args, **kwargs: self._ANSWERS.get(name)


# --- one full scan at a time ----------------------------------------------------------


def test_full_scans_never_run_together(tmp_path, monkeypatch, caplog):
    active, peak, done = [0], [0], []
    lock = threading.Lock()

    def scan(engine, repo_id, repo_root, paths, **kwargs):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.2)
        with lock:
            active[0] -= 1
        done.append(repo_id)
        return 0

    stub(monkeypatch, dispatch, "index_paths", scan)
    stub(monkeypatch, dispatch, "prune_stale_files", lambda *a, **k: 0)
    stub(monkeypatch, dispatch, "_apply_project_schema", lambda *a: (False, None))
    roots = []
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "x.py").write_text("x = 1\n")
        roots.append((name, tmp_path / name))
    with caplog.at_level(logging.INFO, logger="devgraph.indexer.dispatch"):
        threads = [threading.Thread(target=full_scan, args=(RecordingEngine(), name, root)) for name, root in roots]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
    assert sorted(done) == ["a", "b", "c"]
    assert peak[0] == 1
    assert sum("waiting for another repository's full scan" in r.getMessage() for r in caplog.records) == 2


def test_a_failed_full_scan_lets_the_next_one_run(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("boom")

    stub(monkeypatch, dispatch, "prune_stale_files", fail)
    (tmp_path / "x.py").write_text("x = 1\n")
    with pytest.raises(RuntimeError):
        full_scan(RecordingEngine(), "a", tmp_path)
    assert dispatch._FULL_SCAN_LOCK.acquire(blocking=False)
    dispatch._FULL_SCAN_LOCK.release()
