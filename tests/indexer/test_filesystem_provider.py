"""Filesystem provider building blocks, with a recording engine."""

import textwrap

from devgraph.config.project_schema import resolve_effective_schema
from devgraph.indexer.providers import filesystem
from devgraph.indexer.providers.filesystem import FilesystemSpec, ancestors, build_graph, filesystem_spec

SPEC = FilesystemSpec(file_label="File", folder_label="Folder", relationship="IS_CHILD_OF",
                      child_labels=frozenset({"File", "Folder"}))
WORKTREE = """
    version: 1
    node_types:
      - label: File
        key: [path]
        metadata: [{name: path}]
        source: {provider: filesystem, kind: file}
      - label: Folder
        key: [path]
        metadata: [{name: path}]
        source: {provider: filesystem, kind: folder}
    relationships:
      - type: IS_CHILD_OF
        provider: filesystem
        from: [File, Folder]
        to: Folder
"""


class Recorder:
    def __init__(self):
        self.nodes, self.rels, self.deleted, self.kept = [], [], [], None

    def upsert_nodes(self, nodes):
        self.nodes.extend(nodes)

    def upsert_relationships(self, rels):
        self.rels.extend(rels)

    def delete_extracted_nodes(self, repo_id, extractor, paths):
        self.deleted.append((repo_id, extractor, sorted(paths)))

    def prune_extracted_nodes(self, repo_id, extractor, keep):
        self.kept = (repo_id, extractor, sorted(keep))
        return 0


def test_ancestors_run_from_nearest_to_root():
    assert ancestors("a/b/c.py") == ["a/b", "a", "."]
    assert ancestors("top.py") == ["."]


def test_spec_comes_from_the_effective_schema(tmp_path):
    (tmp_path / "devgraph.schema.yaml").write_text(textwrap.dedent(WORKTREE))
    assert filesystem_spec(resolve_effective_schema(tmp_path)) == SPEC


def test_no_filesystem_types_means_no_spec(tmp_path):
    assert filesystem_spec(resolve_effective_schema(tmp_path)) is None


def test_build_graph_emits_files_folders_and_parent_edges():
    nodes, rels = build_graph(SPEC, "demo", {"a/b/c.py", "top.py"})
    assert sorted((n["label"], n["name"]) for n in nodes) == [
        ("File", "a/b/c.py"), ("File", "top.py"), ("Folder", "."), ("Folder", "a"), ("Folder", "a/b"),
    ]
    assert all(n["properties"] == {"path": n["name"], "extractor": "filesystem"} for n in nodes)
    assert all(n["repo_id"] == "demo" for n in nodes)
    assert sorted((r["from_label"], r["from_name"], r["to_name"]) for r in rels) == [
        ("File", "a/b/c.py", "a/b"), ("File", "top.py", "."), ("Folder", "a", "."), ("Folder", "a/b", "a"),
    ]
    assert {r["rel_type"] for r in rels} == {"IS_CHILD_OF"} and {r["to_label"] for r in rels} == {"Folder"}


def test_build_graph_respects_which_kinds_are_declared():
    files_only = FilesystemSpec("File", None, None, frozenset())
    nodes, rels = build_graph(files_only, "demo", {"a/b.py"})
    assert [(n["label"], n["name"]) for n in nodes] == [("File", "a/b.py")] and rels == []
    folders_link_only = FilesystemSpec("File", "Folder", "IN", frozenset({"Folder"}))
    _, rels = build_graph(folders_link_only, "demo", {"a/b.py"})
    assert [(r["from_label"], r["from_name"]) for r in rels] == [("Folder", "a")]


def test_sync_absent_deletes_paths_then_folders_left_empty_on_disk(tmp_path):
    (tmp_path / "keep").mkdir()
    (tmp_path / "keep" / "x.py").write_text("")
    engine = Recorder()
    filesystem.sync_absent(engine, "demo", tmp_path, SPEC, {"gone/sub/y.py", "keep/z.py"},
                           is_indexable=lambda p: p.is_file(), is_ignored_dir=lambda name: False)
    assert engine.deleted[0] == ("demo", "filesystem", ["gone/sub/y.py", "keep/z.py"])
    # `gone` and `gone/sub` no longer hold any file; `keep` and the root still do.
    assert engine.deleted[1] == ("demo", "filesystem", ["gone", "gone/sub"])


def test_sync_absent_without_a_folder_type_only_deletes_paths(tmp_path):
    engine = Recorder()
    filesystem.sync_absent(engine, "demo", tmp_path, FilesystemSpec("File", None, None, frozenset()),
                           {"a/b.py"}, is_indexable=lambda p: p.is_file(), is_ignored_dir=lambda name: False)
    assert engine.deleted == [("demo", "filesystem", ["a/b.py"])]


def test_holding_check_never_walks_an_ignored_subtree(tmp_path):
    (tmp_path / "pkg" / "node_modules" / "dep").mkdir(parents=True)
    (tmp_path / "pkg" / "node_modules" / "dep" / "x.js").write_text("")
    seen = []

    def is_indexable(path):
        seen.append(path)
        return path.is_file()

    held = filesystem._holds_indexable_file(
        tmp_path / "pkg", is_indexable, lambda name: name == "node_modules"
    )

    assert held is False
    assert not [p for p in seen if "node_modules" in p.parts]


def test_holding_check_exits_early_on_the_first_indexable_file(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("")
    (tmp_path / "pkg" / "b.py").write_text("")
    seen = []

    def is_indexable(path):
        seen.append(path)
        return True

    assert filesystem._holds_indexable_file(tmp_path / "pkg", is_indexable, lambda name: False)
    assert len(seen) == 1


def test_reconcile_keeps_exactly_the_desired_nodes():
    engine = Recorder()
    filesystem.reconcile(engine, "demo", SPEC, {"a/b.py"})
    assert engine.kept == ("demo", "filesystem", ["File:a/b.py", "Folder:.", "Folder:a"])


def test_reconcile_without_a_spec_prunes_everything():
    engine = Recorder()
    filesystem.reconcile(engine, "demo", None, {"a/b.py"})
    assert engine.kept == ("demo", "filesystem", [])
