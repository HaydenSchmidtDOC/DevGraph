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
