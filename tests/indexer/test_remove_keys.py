"""remove_paths' gone keys, presence check and expansion (watcher spec W2), without Neo4j."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from devgraph.indexer import dispatch
from devgraph.indexer.dispatch import _gone_key, _present, remove_paths


class _GraphFilesEngine:
    """Answers the graph-files query from a fixed list and records every delete."""

    def __init__(self, files):
        self.files = set(files)
        self.language: list[str] = []
        self.extracted: list[tuple[str, list[str]]] = []
        self.graph_queries = 0

    def list_indexed_files(self, repo_id):
        self.graph_queries += 1
        return set(self.files)

    def list_extracted_paths(self, repo_id, extractor, labels):
        return set()

    def list_claim_sources(self, repo_id):
        return set()

    def read_applied_schema(self, repo_id):
        return None

    def delete_nodes_by_source_file(self, repo_id, file_name):
        self.language.append(file_name)

    def delete_extracted_nodes(self, repo_id, extractor, paths):
        self.extracted.append((extractor, list(paths)))


@pytest.fixture
def providers(monkeypatch):
    """Both provider passes declared; records what each one is given."""
    seen: dict[str, set[str]] = {}
    fs_spec = SimpleNamespace(file_label="File", folder_label="Folder")
    monkeypatch.setattr(dispatch, "_provider_specs", lambda root: (True, fs_spec, SimpleNamespace(types=[])))
    monkeypatch.setattr(dispatch, "schema_pending", lambda engine, repo_id, root: False)
    monkeypatch.setattr(
        dispatch.filesystem, "sync_absent",
        lambda engine, repo_id, root, spec, paths, **kw: seen.__setitem__("filesystem", set(paths)),
    )
    monkeypatch.setattr(
        dispatch, "_take_over_keys",
        lambda engine, repo_id, root, spec, gone: seen.__setitem__("takeover", set(gone)),
    )
    return seen


@pytest.fixture
def case_insensitive(monkeypatch, tmp_path):
    """The disk answers is_file/is_dir/exists case-insensitively below tmp_path, as on Windows;
    os.listdir still reports each name as it was created."""
    root = str(tmp_path)

    def folded(path):
        text = str(path)
        if not text.startswith(root):
            return Path(text)
        rest = text[len(root):]
        return Path(root + rest.lower())

    for name in ("is_file", "is_dir", "exists"):
        original = getattr(Path, name)
        monkeypatch.setattr(Path, name, lambda self, *a, _o=original, **k: _o(folded(self), *a, **k))


def test_gone_key_is_lexical_for_the_missing_leaf(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "foo.py").write_text("x = 1\n")
    assert _gone_key(tmp_path, tmp_path / "pkg" / "Foo.py") == "pkg/Foo.py"


def test_gone_key_resolves_the_parent(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    assert _gone_key(tmp_path, tmp_path / "link" / "gone.py") == "real/gone.py"


def test_present_matches_the_leaf_case_exactly(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "foo.py").write_text("x = 1\n")
    assert _present(tmp_path, "pkg/foo.py")
    assert not _present(tmp_path, "pkg/Foo.py")


def test_present_matches_every_folder_case_exactly(tmp_path, case_insensitive):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n")
    assert (tmp_path / "Pkg" / "a.py").is_file()  # the case-insensitive disk opens it
    assert not _present(tmp_path, "Pkg/a.py")
    assert _present(tmp_path, "pkg/a.py")


def test_folder_case_rename_removes_only_the_old_spelling(tmp_path, case_insensitive, providers):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n")
    engine = _GraphFilesEngine({"Pkg/a.py", "pkg/a.py"})
    remove_paths(engine, "_unit", tmp_path, {tmp_path / "Pkg"})
    assert "Pkg/a.py" in engine.language
    assert "pkg/a.py" not in engine.language
    assert providers["filesystem"] == {"Pkg", "Pkg/a.py"}


def test_case_only_rename_removes_exactly_the_old_file(tmp_path, providers):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "foo.py").write_text("x = 1\n")
    engine = _GraphFilesEngine({"pkg/Foo.py", "pkg/foo.py"})
    remove_paths(engine, "_unit", tmp_path, {tmp_path / "pkg" / "Foo.py"})
    assert engine.language == ["pkg/Foo.py"]
    assert providers["filesystem"] == {"pkg/Foo.py"}
    assert providers["takeover"] == {"pkg/Foo.py"}
    assert engine.extracted == [("docs", ["pkg/Foo.py"])]


def test_removing_the_root_removes_nothing(tmp_path, providers):
    (tmp_path / "a.py").write_text("x = 1\n")
    engine = _GraphFilesEngine({"a.py", "gone.py"})
    assert remove_paths(engine, "_unit", tmp_path, {tmp_path}) == 0
    assert engine.language == []
    assert providers == {}


def test_a_folder_expands_to_its_files_not_a_prefix_sibling(tmp_path, providers):
    engine = _GraphFilesEngine({"pkg/x.py", "pkg2/y.py"})
    remove_paths(engine, "_unit", tmp_path, {tmp_path / "pkg"})
    assert "pkg/x.py" in engine.language
    assert "pkg2/y.py" not in engine.language
    assert providers["filesystem"] == {"pkg", "pkg/x.py"}


def test_a_recreated_file_below_a_gone_folder_is_kept(tmp_path, providers):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")
    engine = _GraphFilesEngine({"pkg/mod.py", "pkg/sub/util.py"})
    remove_paths(engine, "_unit", tmp_path, {tmp_path / "pkg"})
    assert engine.language == ["pkg/sub/util.py"]
    # pkg still holds an indexable file, so only the exact paths go to the providers.
    assert providers["filesystem"] == {"pkg/sub/util.py"}


def test_one_schema_resolve_per_call(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(dispatch, "schema_pending", lambda engine, repo_id, root: calls.append("pending") or False)
    monkeypatch.setattr(dispatch, "_provider_specs", lambda root: calls.append("specs") or (True, None, None))
    remove_paths(_GraphFilesEngine({"pkg/x.py"}), "_unit", tmp_path, {tmp_path / "pkg"})
    assert calls == ["pending", "specs"]
