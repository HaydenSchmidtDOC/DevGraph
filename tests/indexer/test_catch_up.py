"""dispatch.catch_up: which files are due after the agent was off (spec W5).

The unit tests stub the graph side (`prune_stale_files`, `_graph_files`,
`index_paths`); the live tests run against Neo4j.
"""

import os
import shutil
import sys
import textwrap
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer import dispatch
from devgraph.indexer.dispatch import CATCH_UP_MARGIN_NS, CatchUp, _change_stamp_ns, catch_up, full_scan
from tests.indexer.docs_live_helpers import assert_matches_fresh_apply

NOW = datetime.now(timezone.utc)


def _ns(when: datetime) -> int:
    return int(when.timestamp() * 1_000_000_000)


@pytest.fixture
def stubbed(monkeypatch):
    """catch_up with the graph stubbed: `known` is `_graph_files`; returns the
    files offered to `index_paths`, as repo-relative strings."""
    state = {"known": set(), "offered": [], "pruned": 0}

    monkeypatch.setattr(dispatch, "prune_stale_files", lambda *a, **k: state["pruned"])
    monkeypatch.setattr(dispatch, "schema_pending", lambda *a, **k: False)
    monkeypatch.setattr(dispatch, "_graph_files", lambda *a, **k: set(state["known"]))
    monkeypatch.setattr(dispatch, "_docs_note_files", lambda *a, **k: set(state["known"]))

    def index(engine, repo_id, root, paths, docs_path=None, mentions_enabled=False):
        state["offered"].append({p.relative_to(root).as_posix() for p in paths})
        return len(paths) * 10

    monkeypatch.setattr(dispatch, "index_paths", index)
    return state


def test_change_stamp_takes_the_later_of_mtime_and_the_change_time():
    if sys.platform == "win32":
        st = SimpleNamespace(st_mtime_ns=5, st_ctime_ns=99, st_birthtime_ns=7)
        assert _change_stamp_ns(st) == 7
    else:
        st = SimpleNamespace(st_mtime_ns=5, st_ctime_ns=7)
        assert _change_stamp_ns(st) == 7
    assert _change_stamp_ns(SimpleNamespace(st_mtime_ns=9, st_ctime_ns=7, st_birthtime_ns=7)) == 9


@pytest.mark.skipif(sys.platform == "win32", reason="ctime is the creation time on Windows")
def test_a_preserved_mtime_with_a_new_ctime_is_due(tmp_path, stubbed):
    f = tmp_path / "a.py"
    f.write_text("a = 1\n")
    hour_ago = time.time() - 3600
    os.utime(f, (hour_ago, hour_ago))  # what cp -p or tar does; ctime is now
    stubbed["known"] = {"a.py"}
    result = catch_up(None, "r", tmp_path, NOW - timedelta(minutes=1))
    assert stubbed["offered"] == [{"a.py"}]
    assert result == CatchUp(indexed=10, pruned=0, checked=1, offered=1, unknown=0)


def test_a_file_with_every_stamp_old_is_not_due(tmp_path, stubbed):
    (tmp_path / "a.py").write_text("a = 1\n")
    stubbed["known"] = {"a.py"}
    result = catch_up(None, "r", tmp_path, datetime.now(timezone.utc) + timedelta(hours=1))
    assert stubbed["offered"] == []
    assert result == CatchUp(indexed=0, pruned=0, checked=1, offered=0, unknown=0)


@pytest.mark.parametrize(("before_s", "due"), [(4, True), (6, False)])
def test_the_margin_is_five_seconds(tmp_path, stubbed, monkeypatch, before_s, due):
    assert CATCH_UP_MARGIN_NS == 5_000_000_000
    (tmp_path / "a.py").write_text("a = 1\n")
    stubbed["known"] = {"a.py"}
    since = NOW
    stamp = _ns(since) - before_s * 1_000_000_000
    monkeypatch.setattr(dispatch, "_change_stamp_ns", lambda st: stamp)
    catch_up(None, "r", tmp_path, since)
    assert stubbed["offered"] == ([{"a.py"}] if due else [])


def test_a_file_the_graph_does_not_know_is_due_whatever_its_stamps(tmp_path, stubbed):
    (tmp_path / "new.py").write_text("n = 1\n")
    (tmp_path / "old.py").write_text("o = 1\n")
    stubbed["known"] = {"old.py"}
    result = catch_up(None, "r", tmp_path, datetime.now(timezone.utc) + timedelta(hours=1))
    assert stubbed["offered"] == [{"new.py"}]
    assert (result.offered, result.unknown) == (1, 1)


FS_SCHEMA = """
    version: 1
    node_types:
      - label: File
        key: [path]
        metadata: [{name: path}]
        source: {provider: filesystem, kind: file}
"""


def test_a_provider_only_file_the_graph_knows_is_not_offered_again(tmp_path, stubbed):
    (tmp_path / "devgraph.schema.yaml").write_text(textwrap.dedent(FS_SCHEMA))
    (tmp_path / "logo.png").write_bytes(b"\x89PNG")
    stubbed["known"] = {"logo.png", "devgraph.schema.yaml"}
    result = catch_up(None, "r", tmp_path, datetime.now(timezone.utc) + timedelta(hours=1))
    assert stubbed["offered"] == []
    assert result.checked == 2


def test_files_nothing_would_index_are_never_offered(tmp_path, stubbed):
    for name in ("notes.txt", "data.json", "LICENSE", "empty.md"):
        (tmp_path / name).write_text("")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG")
    result = catch_up(None, "r", tmp_path, datetime.now(timezone.utc) - timedelta(hours=1))
    assert stubbed["offered"] == []
    assert result == CatchUp(indexed=0, pruned=0, checked=5, offered=0, unknown=0)


@pytest.mark.parametrize(
    "name", ["a.py", "a.ts", "a.cs", "a.cpp", "A.java", "a.rs", "a.kt", "a.go", "Dockerfile", "compose.yaml"]
)
def test_files_an_extractor_handles_are_offered(tmp_path, stubbed, name):
    (tmp_path / name).write_text("")
    catch_up(None, "r", tmp_path, NOW)
    assert stubbed["offered"] == [{name}]


def test_markdown_is_offered_under_the_docs_path_or_with_mentions(tmp_path, stubbed):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("# A\n")
    (tmp_path / "b.md").write_text("# B\n")
    catch_up(None, "r", tmp_path, NOW, docs_path="docs")
    catch_up(None, "r", tmp_path, NOW, mentions_enabled=True)
    assert stubbed["offered"] == [{"docs/a.md"}, {"docs/a.md", "b.md"}]


def test_declared_providers_make_their_files_offered(tmp_path, stubbed):
    (tmp_path / "devgraph.schema.yaml").write_text(textwrap.dedent(FS_SCHEMA))
    (tmp_path / "notes.txt").write_text("")
    catch_up(None, "r", tmp_path, NOW)
    assert stubbed["offered"] == [{"devgraph.schema.yaml", "notes.txt"}]

    (tmp_path / "devgraph.schema.yaml").write_text(textwrap.dedent("""
        version: 1
        node_types:
          - label: Adr
            key: [path]
            metadata: [{name: path}]
            source: {provider: docs, paths: ["decisions/*.md"]}
    """))
    (tmp_path / "decisions").mkdir()
    (tmp_path / "decisions" / "a.md").write_text("# A\n")
    (tmp_path / "other.md").write_text("# B\n")
    stubbed["offered"].clear()
    catch_up(None, "r", tmp_path, NOW)
    assert stubbed["offered"] == [{"decisions/a.md"}]


def test_a_pending_schema_offers_no_provider_files(tmp_path, stubbed, monkeypatch):
    (tmp_path / "devgraph.schema.yaml").write_text(textwrap.dedent(FS_SCHEMA))
    (tmp_path / "notes.txt").write_text("")
    monkeypatch.setattr(dispatch, "schema_pending", lambda *a, **k: True)
    catch_up(None, "r", tmp_path, NOW)
    assert stubbed["offered"] == []


def _routed_by_index_single_path(root, path, docs_root, mentions_enabled):
    """Whether `index_paths` writes anything for this file through a built-in
    extractor: `_index_single_path` on its resolved path, with a mock graph."""
    resolved = path.resolve()
    lists = {name: [] for name in (
        "py_files", "js_files", "cs_files", "cpp_files", "java_files", "rs_files", "kt_files", "docs_files", "mention_files",
    )}
    extractions = {name: {} for name in (
        "py_extractions", "js_extractions", "cs_extractions", "cpp_extractions", "java_extractions",
        "rs_extractions", "kt_extractions", "go_extractions",
    )}
    return dispatch._index_single_path(
        MagicMock(), "r", root, resolved, resolved.relative_to(root.resolve()).as_posix(),
        docs_root, mentions_enabled, None, batch_services=set(), **lists, **extractions,
    ) > 0


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("mentions_enabled", [False, True])
def test_would_index_routes_exactly_as_index_single_path(tmp_path, mentions_enabled):
    root = tmp_path / "mixed"
    (root / "docs" / "deep").mkdir(parents=True)
    (root / "src").mkdir()
    files = {
        "src/a.py": "x = 1\n", "src/B.PY": "x = 1\n", "src/c.ts": "", "src/d.tsx": "", "src/e.js": "",
        "src/f.cs": "", "src/g.cpp": "", "src/g.h": "", "src/G.java": "", "src/h.rs": "", "src/i.kt": "",
        "src/j.go": "package j\n", "src/notes.txt": "", "src/data.json": "{}",
        "Dockerfile": "FROM python\n", "src/dockerfile": "FROM python\n", "Containerfile": "FROM python\n",
        "compose.yaml": "services: {}\n", "Docker-Compose.YML": "services: {}\n", "podman-compose.yaml": "",
        "src/compose.yaml.bak": "", "README.md": "# R\n", "CHANGES.MARKDOWN": "# C\n",
        "docs/a.md": "---\ntype: requirement\nid: r-a\n---\n# A\n", "docs/deep/b.markdown": "# B\n",
        "docs/c.MD": "# C\n", "docs/d.txt": "",
    }
    for rel, text in files.items():
        (root / rel).write_text(text)
    links = {
        "src/link.py": "notes.txt",          # a code name onto a file no extractor reads
        "src/link.txt": "a.py",              # and the other way round
        "doc-link.md": "docs/a.md",          # Markdown outside docs/ onto a note inside it
        "docs/out-link.md": "../README.md",  # and the other way round
        "src/note.txt": "../docs/a.md",      # a non-Markdown name onto a note
        "Dockerfile.link": "Dockerfile",
        "src/Dockerfile": "../src/notes.txt",
    }
    for rel, target in links.items():
        (root / rel).symlink_to(target)
    docs_root = (root / "docs").resolve()
    no_specs = (False, None, None)
    with patch.object(dispatch, "get_settings", return_value=MagicMock(mentions_ambiguous_mode="all")):
        verdicts = {
            rel: (
                dispatch._would_index(path, rel, docs_root, mentions_enabled, no_specs),
                _routed_by_index_single_path(root, path, docs_root, mentions_enabled),
            )
            for path, rel in dispatch._keyed_indexable_paths(root)
            for rel in [path.relative_to(root).as_posix()]
        }
    assert set(verdicts) == set(files) | set(links)
    assert {rel: v for rel, v in verdicts.items() if v[0] != v[1]} == {}
    assert verdicts["src/link.py"] == (False, False) and verdicts["src/note.txt"] == (True, True)


def test_a_docs_note_known_only_through_its_mentions_document_is_offered(tmp_path, stubbed, monkeypatch):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text("---\ntype: requirement\nid: r\n---\n# R\n")
    (tmp_path / "docs" / "page.md").write_text("# Just a page\n")
    stubbed["known"] = {"docs/note.md", "docs/page.md"}
    monkeypatch.setattr(dispatch, "_docs_note_files", lambda *a, **k: set())
    result = catch_up(None, "r", tmp_path, NOW + timedelta(hours=1), docs_path="docs", mentions_enabled=True)
    assert stubbed["offered"] == [{"docs/note.md"}]
    assert (result.offered, result.unknown) == (1, 1)


def test_prune_count_is_reported(tmp_path, stubbed):
    stubbed["pruned"] = 3
    assert catch_up(None, "r", tmp_path, NOW) == CatchUp(indexed=0, pruned=3, checked=0, offered=0, unknown=0)


# --- live ----------------------------------------------------------------

_TOKEN = uuid.uuid4().hex[:8]
REPO = f"_smoketest_catch_up_{_TOKEN}"
FRESH = f"{REPO}_fresh"
FILE, FOLDER, ADR = (f"ZzFile{_TOKEN}", f"ZzFolder{_TOKEN}", f"ZzAdr{_TOKEN}")
SCHEMA = f"""
    version: 1
    node_types:
      - label: {FILE}
        key: [path]
        metadata: [{{name: path}}]
        source: {{provider: filesystem, kind: file}}
      - label: {FOLDER}
        key: [path]
        metadata: [{{name: path}}]
        source: {{provider: filesystem, kind: folder}}
      - label: {ADR}
        key: [adr_id]
        metadata: [{{name: path}}, {{name: adr_id}}]
        source: {{provider: docs, paths: ["decisions/**/*.md"], fields: {{adr_id: id}}}}
    relationships:
      - type: IS_CHILD_OF
        provider: filesystem
        from: [{FILE}, {FOLDER}]
        to: {FOLDER}
      - {{type: ZZ_SUPERSEDES, provider: docs, from: {ADR}, to: {ADR}, field: supersedes}}
"""


@pytest.fixture(scope="module", autouse=True)
def _drop_generated_constraints():
    yield
    cleanup = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        for label in (FILE, FOLDER, ADR):
            cleanup.run_cypher(f"DROP CONSTRAINT {label.lower()}_repo_key IF EXISTS")
            cleanup.run_cypher(f"DROP INDEX {label.lower()}_repo_name IF EXISTS")
    except Exception:
        pass  # Neo4j unavailable: the tests were skipped
    finally:
        cleanup.close()


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.delete_repository(REPO)
    yield test_engine
    test_engine.delete_repository(REPO)
    test_engine.delete_repository(FRESH)
    test_engine.close()


def _front(root, rel, front):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{textwrap.dedent(front).strip()}\n---\n# Notes\n")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "devgraph.schema.yaml").write_text(textwrap.dedent(SCHEMA))
    (root / "pkg" / "a.py").write_text("class Alpha:\n    pass\n")
    (root / "pkg" / "b.py").write_text("from pkg.a import Alpha\n\nclass Beta(Alpha):\n    pass\n")
    (root / "notes.md").write_text("# Notes\n")
    (root / "logo.png").write_bytes(b"\x89PNG\r\n")
    _front(root, "decisions/adr-1.md", "id: ADR-1")
    _front(root, "decisions/adr-2.md", "id: ADR-2\nsupersedes: ADR-1")
    return root


def snapshot(engine, repo_id):
    nodes = engine.run_cypher(
        "MATCH (n {repo_id: $r}) WHERE NOT n:Repository "
        "RETURN labels(n) AS labels, n.name AS name, coalesce(n.file, n.source_file, n.path, '') AS file",
        {"r": repo_id},
    )
    rels = engine.run_cypher(
        "MATCH (a {repo_id: $r})-[x]->(b {repo_id: $r}) "
        "RETURN labels(a)[0] AS a, a.name AS an, type(x) AS t, labels(b)[0] AS b, b.name AS bn",
        {"r": repo_id},
    )
    return (
        sorted((tuple(sorted(n["labels"])), n["name"] or "", n["file"]) for n in nodes),
        sorted((r["a"], r["an"] or "", r["t"], r["b"], r["bn"] or "") for r in rels),
    )


def scan(engine, root, repo_id=REPO):
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)


def test_edits_made_while_off_are_caught_up_to_a_fresh_scan(engine, repo):
    since = datetime.now(timezone.utc)
    scan(engine, repo)

    (repo / "pkg" / "a.py").write_text("class Alpha:\n    pass\n\nclass Gamma:\n    pass\n")
    (repo / "pkg" / "b.py").unlink()
    (repo / "notes.md").unlink()
    (repo / "logo.png").unlink()
    (repo / "pkg" / "c.py").write_text("def gamma():\n    return 3\n")
    (repo / "decisions" / "adr-1.md").rename(repo / "decisions" / "0001-start.md")

    result = catch_up(engine, REPO, repo, since)
    assert result.indexed > 0 and result.pruned > 0

    scan(engine, repo, FRESH)
    assert snapshot(engine, REPO) == snapshot(engine, FRESH)
    engine.delete_repository(FRESH)
    assert_matches_fresh_apply(engine, REPO, repo)
    incoming = engine.run_cypher(
        f"MATCH (a:{ADR} {{repo_id: $r}})-[:ZZ_SUPERSEDES]->(b:{ADR} {{repo_id: $r, name: 'ADR-1'}}) "
        "RETURN a.name AS a, b.path AS p",
        {"r": REPO},
    )
    assert [(row["a"], row["p"]) for row in incoming] == [("ADR-2", "decisions/0001-start.md")]


def test_a_second_catch_up_does_nothing(engine, repo):
    scan(engine, repo)
    shutil.rmtree(repo / "pkg")
    first = catch_up(engine, REPO, repo, datetime.now(timezone.utc) - timedelta(minutes=1))
    assert first.pruned > 0
    again = catch_up(engine, REPO, repo, datetime.now(timezone.utc) + timedelta(minutes=1))
    assert (again.indexed, again.pruned) == (0, 0)
    assert again.checked == first.checked


def test_bare_modules_left_by_old_recency_writes_are_pruned(engine, repo):
    """Before recency writes became MATCH-only, a commit touching a README, an
    image or a deleted file MERGEd a Module with no file key; nothing a scan
    makes looks like that."""
    scan(engine, repo)
    for name in ("README.md", "logo.png", "gone.py"):
        engine.run_cypher(
            "CREATE (:Module {repo_id: $r, name: $n, created_at: '2026-01-01T00:00:00Z'})", {"r": REPO, "n": name}
        )
    result = catch_up(engine, REPO, repo, datetime.now(timezone.utc) + timedelta(minutes=1))
    bare = engine.run_cypher(
        "MATCH (m:Module {repo_id: $r}) WHERE m.source_file IS NULL AND m.file IS NULL AND m.path IS NULL "
        "RETURN m.name AS n",
        {"r": REPO},
    )
    assert bare == []
    assert result.indexed == 0
    scan(engine, repo, FRESH)
    assert snapshot(engine, REPO) == snapshot(engine, FRESH)


# --- docs notes under docs_path (keyed by their repo-relative path) -------


@pytest.fixture
def notes_repo(tmp_path):
    """Two docs notes with the same filename in different folders, plus a
    module they link to."""
    root = tmp_path / "notes_repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("class Alpha:\n    pass\n")
    _front(root, "docs/arch.md", "type: architecture_note\nid: note-top\nlinks: [pkg/a.py]")
    _front(root, "docs/sub/arch.md", "type: requirement\nid: req-sub")
    return root


def _scan_docs(engine, root, repo_id=REPO):
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root, docs_path="docs")


def _note_files(engine, repo_id=REPO):
    rows = engine.run_cypher(
        "MATCH (n {repo_id: $r}) WHERE n:Requirement OR n:ArchitectureNote OR n:DesignDecision "
        "RETURN n.name AS name, n.source_file AS file",
        {"r": repo_id},
    )
    return sorted((row["name"], row["file"]) for row in rows)


def test_docs_notes_record_their_repo_relative_path(engine, notes_repo):
    _scan_docs(engine, notes_repo)
    assert _note_files(engine) == [("note-top", "docs/arch.md"), ("req-sub", "docs/sub/arch.md")]


def test_a_no_op_catch_up_with_a_docs_path_offers_nothing(engine, notes_repo):
    _scan_docs(engine, notes_repo)
    result = catch_up(engine, REPO, notes_repo, datetime.now(timezone.utc) + timedelta(minutes=1), docs_path="docs")
    assert (result.offered, result.unknown, result.pruned, result.indexed) == (0, 0, 0, 0)
    assert _note_files(engine) == [("note-top", "docs/arch.md"), ("req-sub", "docs/sub/arch.md")]


def test_deleting_a_docs_note_removes_only_that_note(engine, notes_repo):
    _scan_docs(engine, notes_repo)
    (notes_repo / "docs" / "arch.md").unlink()
    assert dispatch.remove_paths(engine, REPO, notes_repo, {notes_repo / "docs" / "arch.md"}) == 1
    assert _note_files(engine) == [("req-sub", "docs/sub/arch.md")]
    _scan_docs(engine, notes_repo, FRESH)
    assert snapshot(engine, REPO) == snapshot(engine, FRESH)


def _downgrade_note_keys(engine):
    """What an older scan wrote: a docs note's bare filename as its source_file."""
    engine.run_cypher(
        "MATCH (n {repo_id: $r}) WHERE n:Requirement OR n:ArchitectureNote OR n:DesignDecision "
        "SET n.source_file = split(n.source_file, '/')[-1]",
        {"r": REPO},
    )
    assert _note_files(engine) == [("note-top", "arch.md"), ("req-sub", "arch.md")]


@pytest.mark.parametrize("heal", ["catch_up", "full_scan"])
def test_notes_keyed_by_a_bare_filename_are_rekeyed_without_duplicates(engine, notes_repo, heal):
    _scan_docs(engine, notes_repo)
    _downgrade_note_keys(engine)
    if heal == "catch_up":
        catch_up(engine, REPO, notes_repo, datetime.now(timezone.utc) + timedelta(minutes=1), docs_path="docs")
    else:
        _scan_docs(engine, notes_repo)
    assert _note_files(engine) == [("note-top", "docs/arch.md"), ("req-sub", "docs/sub/arch.md")]
    _scan_docs(engine, notes_repo, FRESH)
    assert snapshot(engine, REPO) == snapshot(engine, FRESH)
    again = catch_up(engine, REPO, notes_repo, datetime.now(timezone.utc) + timedelta(minutes=1), docs_path="docs")
    assert (again.offered, again.pruned) == (0, 0)


# --- the bare-Module cleanup's assumptions -------------------------------

_MODULE_SOURCES = {
    "py": ("pkg/mod.py", "import os\n\ndef f():\n    return 1\n"),
    "ts": ("web/app.ts", "import { x } from './x';\nexport function f() { return 1; }\n"),
    "js": ("web/app.js", "const x = require('./x');\nfunction f() { return 1; }\n"),
    "cs": ("src/App.cs", "using System;\nnamespace A { class B { void F() {} } }\n"),
    "cpp": ("src/app.cpp", "#include \"x.h\"\nint f() { return 1; }\n"),
    "java": ("src/a/App.java", "package a;\nimport java.util.List;\nclass App { void f() {} }\n"),
    "rs": ("src/app.rs", "use std::io;\nfn f() -> i32 { 1 }\n"),
    "kt": ("src/App.kt", "package a\nimport b.C\nfun f() = 1\n"),
    "go": ("cmd/app/main.go", "package main\nimport \"fmt\"\nfunc f() { fmt.Println(1) }\n"),
}


def _extract(kind: str, rel: str, content: str):
    from devgraph.indexer.cpp.extractor import extract_cpp_file
    from devgraph.indexer.csharp.extractor import extract_csharp_file
    from devgraph.indexer.go.extractor import extract_go_file
    from devgraph.indexer.java.extractor import extract_java_file
    from devgraph.indexer.jsts.extractor import extract_js_file
    from devgraph.indexer.kotlin.extractor import extract_kotlin_file
    from devgraph.indexer.python.extractor import extract_python_file
    from devgraph.indexer.rust.extractor import extract_rust_file

    extractors = {
        "py": extract_python_file, "ts": extract_js_file, "js": extract_js_file, "cs": extract_csharp_file,
        "cpp": extract_cpp_file, "java": extract_java_file, "rs": extract_rust_file, "kt": extract_kotlin_file,
    }
    if kind == "go":
        return extract_go_file(content, rel, "r", "example.com/app")
    return extractors[kind](content, rel, "r")


@pytest.mark.parametrize("kind", sorted(_MODULE_SOURCES))
def test_every_module_an_extractor_writes_has_its_path_as_source_file(kind):
    """`delete_bare_modules` deletes Modules with no file key: no scan may write one."""
    rel, content = _MODULE_SOURCES[kind]
    modules = [n.to_dict() for n in _extract(kind, rel, content).nodes]
    modules = [n for n in modules if n["label"] == "Module"]
    assert modules
    assert [n["properties"].get("source_file") for n in modules] == [rel] * len(modules)


def test_bare_module_cleanup_stays_in_its_repository(engine):
    other = f"{REPO}_other"
    engine.delete_repository(other)
    try:
        for repo_id in (REPO, other):
            engine.run_cypher("CREATE (:Module {repo_id: $r, name: 'gone.py'})", {"r": repo_id})
        assert engine.delete_bare_modules(REPO) == 1
        left = engine.run_cypher("MATCH (m:Module {name: 'gone.py'}) WHERE m.repo_id IN $r RETURN m.repo_id AS r",
                                 {"r": [REPO, other]})
        assert [row["r"] for row in left] == [other]
    finally:
        engine.delete_repository(other)


def _scan_docs_mentions(engine, root, repo_id=REPO, mentions=True):
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root, docs_path="docs", mentions_enabled=mentions)


def _heal(engine, root, heal, mentions):
    if heal == "catch_up":
        catch_up(
            engine, REPO, root, datetime.now(timezone.utc) + timedelta(minutes=1),
            docs_path="docs", mentions_enabled=mentions,
        )
    else:
        _scan_docs_mentions(engine, root, mentions=mentions)


@pytest.mark.parametrize("heal", ["catch_up", "full_scan"])
def test_bare_note_keys_are_rekeyed_with_mentions_on(engine, notes_repo, heal):
    """The mentions Document already gives docs/arch.md a source_file, so the
    file looks known; the note must still come back under its new key."""
    _scan_docs_mentions(engine, notes_repo)
    _downgrade_note_keys(engine)
    _heal(engine, notes_repo, heal, mentions=True)
    assert _note_files(engine) == [("note-top", "docs/arch.md"), ("req-sub", "docs/sub/arch.md")]
    _scan_docs_mentions(engine, notes_repo, FRESH)
    assert snapshot(engine, REPO) == snapshot(engine, FRESH)
    again = catch_up(
        engine, REPO, notes_repo, datetime.now(timezone.utc) + timedelta(minutes=1),
        docs_path="docs", mentions_enabled=True,
    )
    assert (again.offered, again.pruned) == (0, 0)


@pytest.mark.parametrize("heal", ["catch_up", "full_scan"])
def test_a_bare_note_key_naming_a_real_root_file_is_rekeyed(engine, tmp_path, heal):
    """Old key `README.md` for docs/README.md while a root README.md exists:
    the stale key is on disk, so pruning alone never touches the note."""
    root = tmp_path / "collide"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("class Alpha:\n    pass\n")
    (root / "README.md").write_text("# Root\n")
    _front(root, "docs/README.md", "type: architecture_note\nid: overview\nlinks: [Alpha]")
    _scan_docs(engine, root)
    engine.run_cypher(
        "MATCH (n:ArchitectureNote {repo_id: $r, name: 'overview'}) SET n.source_file = 'README.md'", {"r": REPO}
    )
    # What an older scan left: a note since re-identified, under the bare key, with its edge.
    engine.run_cypher(
        "MATCH (m {repo_id: $r, name: 'Alpha'}) "
        "CREATE (m)-[:DOCUMENTED_BY]->(:ArchitectureNote {repo_id: $r, name: 'note-old', source_file: 'README.md'})",
        {"r": REPO},
    )
    assert _note_files(engine) == [("note-old", "README.md"), ("overview", "README.md")]
    _heal(engine, root, heal, mentions=False)
    assert _note_files(engine) == [("overview", "docs/README.md")]
    _scan_docs(engine, root, FRESH)
    assert snapshot(engine, REPO) == snapshot(engine, FRESH)
