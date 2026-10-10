"""Files a scan skips: too large, binary, minified or generated, or ignored by a
.gitignore -- the same way for a full scan, a live batch and a catch-up.

The unit tests stub the graph; the live ones run against Neo4j.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.indexer import dispatch, walk
from devgraph.indexer.dispatch import catch_up, full_scan, index_paths, remove_paths
from devgraph.indexer.walk import BINARY, GENERATED, TOO_LARGE, content_skip_reason

LIMIT = 4096


# --- the content filters -----------------------------------------------------


def test_a_file_over_the_size_limit_is_too_large(tmp_path):
    path = tmp_path / "big.py"
    path.write_text("x = 1\n" * 1000)  # 6000 bytes
    assert content_skip_reason(path, LIMIT) == TOO_LARGE
    assert content_skip_reason(path, 10_000) is None


def test_a_nul_byte_near_the_start_is_binary(tmp_path):
    path = tmp_path / "blob.py"
    path.write_bytes(b"x = 1\n\x00\x01\x02")
    assert content_skip_reason(path, LIMIT) == BINARY


def test_a_nul_byte_past_the_first_8_kib_is_not_sniffed(tmp_path):
    path = tmp_path / "late.py"
    path.write_bytes(b"x = 1\n" * 2000 + b"\x00")
    assert content_skip_reason(path, 1_000_000) is None


def test_a_minified_file_with_one_huge_line_is_generated(tmp_path):
    path = tmp_path / "app.min.js"
    path.write_text("var a=1;" * 2000)  # one 16 KB line
    assert content_skip_reason(path, 1_000_000) == GENERATED


def test_a_file_of_long_lines_on_average_is_generated(tmp_path):
    path = tmp_path / "table.py"
    path.write_text(("x = '" + "y" * 400 + "'\n") * 40)
    assert content_skip_reason(path, 1_000_000) == GENERATED


def test_ordinary_source_and_a_short_one_liner_are_kept(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n" * 200)
    (tmp_path / "tiny.min.js").write_text("var a=1;" * 100)  # 800 bytes on one line
    assert content_skip_reason(tmp_path / "a.py", 1_000_000) is None
    assert content_skip_reason(tmp_path / "tiny.min.js", 1_000_000) is None


def test_prose_is_not_judged_by_its_line_length(tmp_path):
    path = tmp_path / "README.md"
    path.write_text(("word " * 100 + "\n\n") * 40)  # long paragraphs, one per line
    assert content_skip_reason(path, 1_000_000) is None


def test_the_size_limit_is_a_setting_with_a_one_mib_default(monkeypatch):
    from devgraph.config.settings import Settings

    assert Settings().max_file_bytes == 1024 * 1024
    monkeypatch.setenv("DEVGRAPH_MAX_FILE_BYTES", "2048")
    assert Settings().max_file_bytes == 2048


# --- index_paths, with the graph stubbed -------------------------------------


class _Engine:
    def __init__(self):
        self.deleted: list[str] = []

    def find_importing_modules(self, repo_id, module_name):
        return []

    def list_file_nodes(self, repo_id, files):
        return set()

    def read_applied_schema(self, repo_id):
        return None

    def delete_nodes_by_source_file(self, repo_id, rel):
        self.deleted.append(rel)

    def update_skipped_files(self, repo_id, add=None, drop=(), replace=False):
        self.marks = add or {}


@pytest.fixture
def small_limit(monkeypatch):
    monkeypatch.setattr(dispatch, "_max_file_bytes", lambda: LIMIT)


def test_index_paths_skips_and_reports_filtered_files_and_clears_their_nodes(tmp_path, monkeypatch, small_limit):
    (tmp_path / ".gitignore").write_text("out/\n")
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "big.py").write_text("x = 1\n" * 1000)
    (tmp_path / "blob.js").write_bytes(b"\x00\x01")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "gen.py").write_text("x = 1\n")
    seen = []
    monkeypatch.setattr(dispatch, "_index_single_path", lambda e, r, root, resolved, rel, *a: seen.append(rel) or 1)
    engine = _Engine()
    skipped: dict[str, str] = {}

    count = index_paths(
        engine, "_unit_guards", tmp_path, {tmp_path / n for n in ("a.py", "big.py", "blob.js", "out/gen.py")},
        skipped=skipped,
    )

    assert seen == ["a.py"] and count == 1
    assert skipped == {"big.py": TOO_LARGE, "blob.js": BINARY, "out/gen.py": walk.GITIGNORED}
    assert sorted(engine.deleted) == ["big.py", "blob.js", "out/gen.py"]
    assert {rel: mark[:2] for rel, mark in engine.marks.items()} == {"big.py": [TOO_LARGE, LIMIT], "blob.js": [BINARY, LIMIT]}


def test_a_file_no_extractor_reads_is_not_judged_by_its_content(tmp_path, monkeypatch, small_limit):
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\x00" * 2000)
    monkeypatch.setattr(dispatch, "_index_single_path", lambda *a: 0)
    skipped: dict[str, str] = {}
    index_paths(_Engine(), "_unit_guards", tmp_path, {tmp_path / "logo.png"}, skipped=skipped)
    assert skipped == {}


class _CatchUpEngine:
    def __init__(self, extracted=(), skips=None):
        self.extracted = set(extracted)
        self.skips = skips or {}

    def list_indexed_files(self, repo_id):
        return set(self.extracted)

    def list_claim_sources(self, repo_id):
        return set()

    def read_skipped_files(self, repo_id):
        return dict(self.skips)

    def update_skipped_files(self, repo_id, add=None, drop=(), replace=False):
        self.dropped = set(drop)


@pytest.fixture
def caught_up(monkeypatch, small_limit):
    """catch_up with the graph stubbed; returns the files offered to index_paths."""
    offered: list = []
    monkeypatch.setattr(dispatch, "prune_stale_files", lambda *a, **k: 0)
    monkeypatch.setattr(dispatch, "schema_pending", lambda *a, **k: False)
    monkeypatch.setattr(dispatch, "index_outdated", lambda *a, **k: False)
    monkeypatch.setattr(dispatch, "_docs_note_files", lambda *a, **k: set())
    monkeypatch.setattr(dispatch, "index_paths", lambda e, r, root, paths, **k: offered.append(paths) or len(paths))
    monkeypatch.setattr(dispatch, "sync_resolver_config", lambda *a, **k: 0)
    return offered


def _stamp(path):
    return dispatch._change_stamp_ns(path.stat())


def test_catch_up_does_not_re_judge_a_file_skipped_under_the_same_limit(tmp_path, monkeypatch, caught_up):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "big.py").write_text("x = 1\n" * 1000)
    engine = _CatchUpEngine(skips={"big.py": [TOO_LARGE, LIMIT, _stamp(tmp_path / "big.py")]})
    monkeypatch.setattr(dispatch, "_graph_files", lambda *a, **k: set())
    monkeypatch.setattr(dispatch, "content_skip_reason", lambda *a: pytest.fail("re-read a skipped file"))

    result = catch_up(engine, "_unit_guards", tmp_path, datetime.now(UTC))

    assert caught_up == [{tmp_path / "a.py"}]
    assert result.unknown == 1


@pytest.mark.parametrize("change", ["limit", "stamp", "unmarked"])
def test_catch_up_offers_a_skipped_file_again_when_its_judgement_may_change(tmp_path, monkeypatch, caught_up, change):
    (tmp_path / "big.py").write_text("x = 1\n" * 1000)
    mark = [TOO_LARGE, LIMIT * 2 if change == "limit" else LIMIT, _stamp(tmp_path / "big.py") - (change == "stamp")]
    engine = _CatchUpEngine(skips={} if change == "unmarked" else {"big.py": mark})
    monkeypatch.setattr(dispatch, "_graph_files", lambda *a, **k: set())
    catch_up(engine, "_unit_guards", tmp_path, datetime.now(UTC))
    assert caught_up == [{tmp_path / "big.py"}]


def test_a_provider_file_node_does_not_make_a_code_file_known(tmp_path, monkeypatch, caught_up):
    """A skipped file keeps its filesystem File node; after the limit is
    raised it must still be offered, so only extraction nodes count."""
    (tmp_path / "big.py").write_text("x = 1\n" * 1000)
    engine = _CatchUpEngine(skips={"big.py": [TOO_LARGE, LIMIT // 2, 0]})
    monkeypatch.setattr(dispatch, "_graph_files", lambda *a, **k: {"big.py"})  # its File node
    monkeypatch.setattr(dispatch, "_change_stamp_ns", lambda st: 0)  # older than `since`
    catch_up(engine, "_unit_guards", tmp_path, datetime.now(UTC))
    assert caught_up == [{tmp_path / "big.py"}]


# --- live: incremental equals fresh ------------------------------------------


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.init_schema()
    ids = [f"_smoketest_guards_{uuid.uuid4().hex[:8]}", f"_smoketest_guards_fresh_{uuid.uuid4().hex[:8]}"]
    yield test_engine, ids
    for repo_id in ids:
        test_engine.delete_repository(repo_id)
    test_engine.close()


def _graph(engine, repo_id):
    nodes = engine.run_cypher(
        "MATCH (n {repo_id: $r}) WHERE NOT n:Repository "
        "RETURN labels(n)[0] AS label, n.name AS name, coalesce(n.file, n.source_file, n.path, '') AS file",
        {"r": repo_id},
    )
    return sorted((n["label"], n["name"] or "", n["file"]) for n in nodes)


def _files(engine, repo_id):
    return {file for _label, _name, file in _graph(engine, repo_id) if file}


def _matches_fresh(engine, live, fresh, root):
    engine.delete_repository(fresh)
    engine.upsert_repository(fresh, fresh, str(root))
    full_scan(engine, fresh, root)
    assert _graph(engine, live) == _graph(engine, fresh)


def test_a_file_crossing_the_size_limit_leaves_and_rejoins_the_graph(engine, tmp_path, small_limit):
    engine, (live, fresh) = engine
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("def alpha():\n    return 1\n")
    (root / "b.py").write_text("def beta():\n    return 2\n")
    engine.upsert_repository(live, live, str(root))
    full_scan(engine, live, root)
    assert "b.py" in _files(engine, live)

    (root / "b.py").write_text("def beta():\n    return 2\n" + "# padding\n" * 600)  # over 4 KiB
    skipped: dict[str, str] = {}
    index_paths(engine, live, root, {root / "b.py"}, skipped=skipped)  # what the watcher sends
    assert skipped == {"b.py": TOO_LARGE}
    assert "b.py" not in _files(engine, live)
    _matches_fresh(engine, live, fresh, root)

    (root / "b.py").write_text("def beta():\n    return 3\n")
    index_paths(engine, live, root, {root / "b.py"})
    assert "b.py" in _files(engine, live)
    _matches_fresh(engine, live, fresh, root)


def test_a_gitignore_edit_is_caught_up_to_a_fresh_scan(engine, tmp_path):
    engine, (live, fresh) = engine
    root = tmp_path / "repo"
    (root / "out").mkdir(parents=True)
    (root / "a.py").write_text("def alpha():\n    return 1\n")
    (root / "out" / "gen.py").write_text("def generated():\n    return 2\n")
    (root / "keep.log.py").write_text("def kept():\n    return 3\n")
    engine.upsert_repository(live, live, str(root))
    full_scan(engine, live, root)
    assert {"a.py", "out/gen.py", "keep.log.py"} <= _files(engine, live)

    # Ignore out/ and *.log.py: the live batch carries the .gitignore, and a
    # catch-up from the batch's start follows it, as RepoSync does.
    started = datetime.now(UTC)
    (root / ".gitignore").write_text("out/\n*.log.py\n")
    index_paths(engine, live, root, {root / ".gitignore"})
    catch_up(engine, live, root, started)
    assert "out/gen.py" not in _files(engine, live) and "keep.log.py" not in _files(engine, live)
    _matches_fresh(engine, live, fresh, root)

    # Re-include keep.log.py with a negation, then drop the .gitignore altogether.
    started = datetime.now(UTC)
    (root / ".gitignore").write_text("out/\n*.log.py\n!keep.log.py\n")
    index_paths(engine, live, root, {root / ".gitignore"})
    catch_up(engine, live, root, started)
    assert "keep.log.py" in _files(engine, live) and "out/gen.py" not in _files(engine, live)
    _matches_fresh(engine, live, fresh, root)

    started = datetime.now(UTC)
    (root / ".gitignore").unlink()
    remove_paths(engine, live, root, {root / ".gitignore"})
    catch_up(engine, live, root, started)
    assert "out/gen.py" in _files(engine, live)
    _matches_fresh(engine, live, fresh, root)


def test_full_scan_reports_what_it_skipped(engine, tmp_path, small_limit):
    engine, (live, _fresh) = engine
    root = tmp_path / "repo"
    (root / "static").mkdir(parents=True)
    (root / "a.py").write_text("def alpha():\n    return 1\n")
    (root / "static" / "app.min.js").write_text("var a=1;" * 2000)
    (root / "data.py").write_bytes(b"\x00" * 10)
    engine.upsert_repository(live, live, str(root))
    skipped: dict[str, str] = {}
    full_scan(engine, live, root, skipped=skipped)
    assert skipped == {"static/app.min.js": TOO_LARGE, "data.py": BINARY}
    assert _files(engine, live) == {"a.py"}



@pytest.mark.parametrize(
    ("gitignore", "gone"),
    [("pkg/.gitignore", {"pkg/b.py", "pkg/.gitignore"}), (".gitignore", {".hidden.py", ".gitignore"})],
)
def test_a_gitignore_that_ignores_itself_reaches_the_catch_up(engine, tmp_path, gitignore, gone):
    """A nested `*` or a root `.*` ignores the .gitignore itself: the watcher
    still queues it, and RepoSync's catch-up leaves a fresh scan's graph."""
    import threading
    from unittest.mock import MagicMock

    from watchdog.events import FileCreatedEvent

    from devgraph.agent.sync import RepoSync
    from devgraph.registry.store import RepoRecord
    from devgraph.watcher.manager import _RepoEventHandler

    engine, (live, fresh) = engine
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "a.py").write_text("def alpha():\n    return 1\n")
    (root / "pkg" / "b.py").write_text("def beta():\n    return 2\n")
    (root / ".hidden.py").write_text("def hidden():\n    return 3\n")
    engine.upsert_repository(live, live, str(root))
    full_scan(engine, live, root)
    assert {"pkg/b.py", ".hidden.py"} <= _files(engine, live)

    registry = MagicMock()
    registry.get.return_value = RepoRecord(live, root, True, True, None, docs_path=None)
    repo_sync = RepoSync(engine, registry, lambda event: None, lambda *a: None)
    handler = _RepoEventHandler(live, root, 500, repo_sync.on_changes, timer_factory=lambda *a: MagicMock(),
                                batch_lock=threading.Lock())
    (root / gitignore).write_text("*\n" if gitignore.startswith("pkg/") else ".*\n")
    handler.dispatch(FileCreatedEvent(str(root / gitignore)))
    handler.flush()

    assert not (gone & _files(engine, live))
    _matches_fresh(engine, live, fresh, root)


def test_a_root_gitignore_ignoring_everything_says_so_instead_of_unmounted(engine, tmp_path):
    from devgraph.indexer.dispatch import prune_stale_files
    from devgraph.indexer.walk import RepoRootEmpty

    engine, (live, _fresh) = engine
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("def alpha():\n    return 1\n")
    engine.upsert_repository(live, live, str(root))
    full_scan(engine, live, root)

    (root / ".gitignore").write_text("*\n")
    with pytest.raises(RepoRootEmpty) as raised:
        prune_stale_files(engine, live, root)
    message = str(raised.value)
    assert "every file is ignored by .gitignore" in message
    assert f"devgraph rescan {live} --force" in message and "unmounted" not in message
    assert "a.py" in _files(engine, live)  # nothing was changed


def test_an_empty_root_still_reads_as_unmounted(engine, tmp_path):
    from devgraph.indexer.dispatch import prune_stale_files
    from devgraph.indexer.walk import RepoRootEmpty

    engine, (live, _fresh) = engine
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n")
    engine.upsert_repository(live, live, str(root))
    full_scan(engine, live, root)
    (root / "a.py").unlink()
    with pytest.raises(RepoRootEmpty, match="unmounted"):
        prune_stale_files(engine, live, root)


def test_raising_the_limit_lets_catch_up_index_a_file_with_a_provider_node(engine, tmp_path, monkeypatch):
    import textwrap

    from devgraph.graph.engine import provision_repository_schema

    engine, (live, fresh) = engine
    label = f"ZzGuardFile{uuid.uuid4().hex[:8]}"
    root = tmp_path / "repo"
    root.mkdir()
    (root / "devgraph.schema.yaml").write_text(textwrap.dedent(f"""
        version: 1
        node_types:
          - label: {label}
            key: [path]
            metadata: [{{name: path}}]
            source: {{provider: filesystem, kind: file}}
    """))
    (root / "a.py").write_text("def alpha():\n    return 1\n")
    (root / "big.py").write_text("def big():\n    return 2\n" + "# padding\n" * 600)
    try:
        monkeypatch.setattr(dispatch, "_max_file_bytes", lambda: LIMIT)
        provision_repository_schema(engine, root)
        engine.upsert_repository(live, live, str(root))
        full_scan(engine, live, root)
        assert ("Function", "big", "big.py") not in _graph(engine, live)
        assert (label, "big.py", "big.py") in _graph(engine, live)
        assert engine.read_skipped_files(live)["big.py"][:2] == [TOO_LARGE, LIMIT]

        later = datetime.now(UTC) + timedelta(minutes=1)  # nothing is due by its stamps
        assert catch_up(engine, live, root, later).offered == 0  # judged under this limit, unchanged

        monkeypatch.setattr(dispatch, "_max_file_bytes", lambda: 1024 * 1024)
        result = catch_up(engine, live, root, later)
        assert (result.offered, result.unknown) == (1, 1)
        assert ("Function", "big", "big.py") in _graph(engine, live)
        _matches_fresh(engine, live, fresh, root)
    finally:
        engine.run_cypher(f"DROP CONSTRAINT {label.lower()}_repo_key IF EXISTS")
        engine.run_cypher(f"DROP INDEX {label.lower()}_repo_name IF EXISTS")


def test_same_package_referrer_scan_skips_ignored_and_oversized_siblings(tmp_path, monkeypatch, small_limit):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (tmp_path / ".gitignore").write_text("Ignored.java\n")
    (pkg / "Base.java").write_text("class Base {}\n")
    (pkg / "Sub.java").write_text("class Sub extends Base {}\n")
    (pkg / "Ignored.java").write_text("class Ignored extends Base {}\n")
    (pkg / "Big.java").write_text("class Big extends Base {}\n" + "// pad\n" * 1000)
    read = []
    real = dispatch._read_text
    monkeypatch.setattr(dispatch, "_read_text", lambda path: read.append(path.name) or real(path))
    nodes = [{"label": "Class", "name": "Base"}]
    found = dispatch._same_package_subtype_referrers(tmp_path, {("Class", "Base")}, {"pkg/Base.java"},
                                                    {"pkg/Base.java": (nodes, [])})
    assert found == {"pkg/Sub.java"}
    assert read == ["Sub.java"]


@pytest.mark.skipif(__import__("os").name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlink_is_judged_by_its_own_path_not_its_target(tmp_path, monkeypatch, small_limit):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "real.py").write_text("x = 1\n")
    (tmp_path / "link.py").symlink_to(tmp_path / "out" / "real.py")
    (tmp_path / ".gitignore").write_text("out/\n")
    seen = []
    monkeypatch.setattr(dispatch, "_index_single_path", lambda e, r, root, resolved, rel, *a: seen.append(rel) or 1)
    engine = _Engine()
    skipped: dict[str, str] = {}
    index_paths(engine, "_unit_guards", tmp_path, {tmp_path / "link.py"}, skipped=skipped)
    assert seen == ["out/real.py"] and skipped == {}  # git and the walk see link.py, which is not ignored
    assert dispatch._is_provider_file(tmp_path, tmp_path / "link.py")
    assert "link.py" in {p.name for p in walk.indexable_paths(tmp_path)}


@pytest.mark.skipif(__import__("os").name == "nt", reason="symlinks need privileges on Windows")
def test_an_ignored_symlink_is_skipped_without_touching_its_target(tmp_path, monkeypatch, small_limit):
    (tmp_path / "real.py").write_text("x = 1\n")
    (tmp_path / "link.py").symlink_to(tmp_path / "real.py")
    (tmp_path / ".gitignore").write_text("link.py\n")
    seen = []
    monkeypatch.setattr(dispatch, "_index_single_path", lambda e, r, root, resolved, rel, *a: seen.append(rel) or 1)
    engine = _Engine()
    skipped: dict[str, str] = {}
    index_paths(engine, "_unit_guards", tmp_path, {tmp_path / "link.py"}, skipped=skipped)
    assert seen == [] and skipped == {"link.py": walk.GITIGNORED}
    assert engine.deleted == []  # real.py's nodes stay
    assert not dispatch._is_provider_file(tmp_path, tmp_path / "link.py")
    assert dispatch._is_provider_file(tmp_path, tmp_path / "real.py")


# --- re-review fixes ----------------------------------------------------------


@pytest.mark.skipif(__import__("os").name == "nt", reason="symlinks need privileges on Windows")
def test_saving_an_import_keeps_a_file_indexed_through_a_symlink_into_an_ignored_folder(engine, tmp_path):
    """link.py -> out/real.py with out/ ignored: the scan keys the target as
    out/real.py. Saving a module it imports pulls out/real.py into the batch
    as a reverse dependent, which must not be judged by .gitignore again."""
    engine, (live, fresh) = engine
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "out").mkdir()
    (root / ".gitignore").write_text("out/\n")
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "mod.py").write_text("def thing():\n    return 1\n")
    (root / "out" / "real.py").write_text("from pkg.mod import thing\n\ndef real():\n    return thing()\n")
    (root / "link.py").symlink_to(root / "out" / "real.py")
    engine.upsert_repository(live, live, str(root))
    full_scan(engine, live, root)
    assert "out/real.py" in _files(engine, live)

    (root / "pkg" / "mod.py").write_text("def thing():\n    return 2\n")
    index_paths(engine, live, root, {root / "pkg" / "mod.py"})

    assert "out/real.py" in _files(engine, live)
    _matches_fresh(engine, live, fresh, root)


@pytest.mark.skipif(__import__("os").name == "nt", reason="symlinks need privileges on Windows")
def test_a_link_and_its_ignored_target_in_one_batch_keep_the_link_spelling(tmp_path, monkeypatch, small_limit):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "real.py").write_text("x = 1\n")
    (tmp_path / "link.py").symlink_to(tmp_path / "out" / "real.py")
    (tmp_path / ".gitignore").write_text("out/\n")
    seen = []
    monkeypatch.setattr(dispatch, "_index_single_path", lambda e, r, root, resolved, rel, *a: seen.append(rel) or 1)
    engine = _Engine()
    index_paths(engine, "_unit_guards", tmp_path, {tmp_path / "link.py", tmp_path / "out" / "real.py"})
    assert seen == ["out/real.py"] and engine.deleted == []


def test_reverse_dependents_and_referrers_are_not_judged_by_gitignore(tmp_path, monkeypatch, small_limit):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "dep.py").write_text("x = 1\n")
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / ".gitignore").write_text("out/\n")
    monkeypatch.setattr(
        dispatch, "_expand_with_reverse_dependents", lambda e, r, root, paths: set(paths) | {tmp_path / "out" / "dep.py"}
    )
    seen = []
    monkeypatch.setattr(dispatch, "_index_single_path", lambda e, r, root, resolved, rel, *a: seen.append(rel) or 1)
    engine = _Engine()
    index_paths(engine, "_unit_guards", tmp_path, {tmp_path / "a.py"})
    assert seen == ["a.py", "out/dep.py"] and engine.deleted == []


@pytest.fixture
def provider_repo(engine, tmp_path):
    """A repository declaring a filesystem `File` type, so every file has a provider node."""
    import textwrap

    engine_, ids = engine
    label = f"ZzGuardFile{uuid.uuid4().hex[:8]}"
    root = tmp_path / "prov"
    root.mkdir()
    (root / "devgraph.schema.yaml").write_text(textwrap.dedent(f"""
        version: 1
        node_types:
          - label: {label}
            key: [path]
            metadata: [{{name: path}}]
            source: {{provider: filesystem, kind: file}}
    """))
    yield engine_, ids, root
    engine_.run_cypher(f"DROP CONSTRAINT {label.lower()}_repo_key IF EXISTS")
    engine_.run_cypher(f"DROP INDEX {label.lower()}_repo_name IF EXISTS")


def _scan(engine, repo_id, root):
    from devgraph.graph.engine import provision_repository_schema

    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root)


LATER = timedelta(minutes=1)  # a `since` after every write: nothing is due by its stamps


def test_an_idle_catch_up_offers_nothing_even_for_files_without_extraction_nodes(provider_repo, monkeypatch):
    engine, (live, _fresh), root = provider_repo
    (root / "a.py").write_text("def alpha():\n    return 1\n")
    (root / "compose.yaml").write_text("volumes:\n  data: {}\n")  # no services
    (root / "Dockerfile").write_text("# no FROM\n")
    (root / "big.py").write_text("x = 1\n" * 1000)
    monkeypatch.setattr(dispatch, "_max_file_bytes", lambda: LIMIT)
    _scan(engine, live, root)
    assert catch_up(engine, live, root, datetime.now(UTC) + LATER).offered == 0


def test_lowering_the_limit_lets_catch_up_drop_a_now_too_large_file(provider_repo, monkeypatch):
    engine, (live, fresh), root = provider_repo
    (root / "a.py").write_text("def alpha():\n    return 1\n")
    (root / "big.py").write_text("def big():\n    return 2\n" + "# padding\n" * 600)
    _scan(engine, live, root)
    assert ("Function", "big", "big.py") in _graph(engine, live)

    monkeypatch.setattr(dispatch, "_max_file_bytes", lambda: LIMIT)
    result = catch_up(engine, live, root, datetime.now(UTC) + LATER)
    assert result.offered == 1
    assert ("Function", "big", "big.py") not in _graph(engine, live)
    assert engine.read_skipped_files(live)["big.py"][:2] == [TOO_LARGE, LIMIT]
    assert catch_up(engine, live, root, datetime.now(UTC) + LATER).offered == 0
    _matches_fresh(engine, live, fresh, root)


def test_skip_marks_follow_deletes_renames_and_files_that_shrink(engine, tmp_path, small_limit):
    engine, (live, _fresh) = engine
    root = tmp_path / "repo"
    root.mkdir()
    padding = "# padding\n" * 600
    for name in ("gone", "moved", "kept", "shrinks", "offline"):
        (root / f"{name}.py").write_text(f"def {name}():\n    return 1\n{padding}")
    engine.upsert_repository(live, live, str(root))
    full_scan(engine, live, root)
    assert set(engine.read_skipped_files(live)) == {"gone.py", "moved.py", "kept.py", "shrinks.py", "offline.py"}

    # Live batches, as the watcher sends them.
    (root / "gone.py").unlink()
    remove_paths(engine, live, root, {root / "gone.py"})
    (root / "moved.py").rename(root / "renamed.py")
    remove_paths(engine, live, root, {root / "moved.py"})
    index_paths(engine, live, root, {root / "renamed.py"})
    (root / "shrinks.py").write_text("def shrinks():\n    return 1\n")
    index_paths(engine, live, root, {root / "shrinks.py"})
    assert set(engine.read_skipped_files(live)) == {"renamed.py", "kept.py", "offline.py"}

    # A delete made while nothing was watching.
    (root / "offline.py").unlink()
    catch_up(engine, live, root, datetime.now(UTC) + LATER)
    assert set(engine.read_skipped_files(live)) == {"renamed.py", "kept.py"}


def test_skip_marks_are_hidden_from_the_node_inspectors():
    from devgraph.graph.schema import INTERNAL_NODE_PROPERTIES

    assert "skipped_files" in INTERNAL_NODE_PROPERTIES
