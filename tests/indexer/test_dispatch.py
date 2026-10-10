"""Integration tests for devgraph.indexer.dispatch — the orchestration layer
that routes changed/deleted files to the right extractor. This is the piece
that was previously missing entirely: extractors existed but nothing called
them from add/rescan/watch.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from devgraph.graph.engine import GraphEngine
from devgraph.indexer import dispatch
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.init_schema()
    yield test_engine
    test_engine.close()


@pytest.fixture
def temp_repo():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


class TestIndexPaths:
    def test_indexes_python_file(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_python"
        py_file = temp_repo / "service.py"
        py_file.write_text("class MyService:\n    pass\n")

        try:
            count = index_paths(engine, repo_id, temp_repo, {py_file})
            assert count == 1

            result = engine.run_cypher(
                "MATCH (c:Class {repo_id: $repo_id, name: 'MyService'}) RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert result[0]["c"] == 1
        finally:
            engine.delete_repository(repo_id)

    def test_reindexing_prunes_symbols_removed_from_the_file(self, engine, temp_repo):
        """A function/class removed from a file (edited, not deleted) must not
        survive in the graph as a stale node after the file is reindexed.

        MERGE-based upserts only ever add/update matching nodes, never
        remove ones the current source no longer produces — index_paths must
        explicitly prune this file's previously-indexed nodes before
        re-extracting, or a removed symbol lingers forever until the whole
        repo is deleted and re-added.
        """
        repo_id = "_smoketest_dispatch_prune"
        py_file = temp_repo / "service.py"
        py_file.write_text("def keep():\n    pass\n\ndef remove_me():\n    pass\n")

        try:
            index_paths(engine, repo_id, temp_repo, {py_file})
            result = engine.run_cypher(
                "MATCH (f:Function {repo_id: $repo_id}) RETURN f.name as name",
                {"repo_id": repo_id},
            )
            names = {r["name"] for r in result}
            assert {"keep", "remove_me"} <= names

            # Edit the file to remove one function, then reindex it again —
            # simulating a rescan/live-reindex after that edit.
            py_file.write_text("def keep():\n    pass\n")
            index_paths(engine, repo_id, temp_repo, {py_file})

            result = engine.run_cypher(
                "MATCH (f:Function {repo_id: $repo_id}) RETURN f.name as name",
                {"repo_id": repo_id},
            )
            names = {r["name"] for r in result}
            assert names == {"keep"}, f"stale node(s) survived reindex: {names}"
        finally:
            engine.delete_repository(repo_id)

    def test_reindexing_a_file_also_refreshes_its_direct_importers(self, engine, temp_repo):
        """Renaming a function in file A must update file B's CALLS edge even
        though only A was passed to index_paths — B is a direct importer of
        A (via an IMPORTS edge already in the graph), so it should be
        transparently pulled into the same reindex batch.

        Without this, upsert_relationship's MATCH-MATCH semantics mean B's
        stale CALLS edge to the old function name just silently never
        re-resolves until B itself is edited or a full rescan runs.
        """
        repo_id = "_smoketest_dispatch_reverse_deps"
        a_file = temp_repo / "a.py"
        b_file = temp_repo / "b.py"
        a_file.write_text("def do_thing():\n    pass\n")
        b_file.write_text("from a import do_thing\n\ndef caller():\n    do_thing()\n")

        try:
            index_paths(engine, repo_id, temp_repo, {a_file, b_file})

            result = engine.run_cypher(
                "MATCH (:Function {repo_id: $repo_id, name: 'caller'})"
                "-[:CALLS]->(f:Function {repo_id: $repo_id}) RETURN f.name as name",
                {"repo_id": repo_id},
            )
            assert {r["name"] for r in result} == {"do_thing"}

            # Rename the called function in a.py, then reindex ONLY a.py —
            # simulating a live edit where the watcher only saw a.py change.
            a_file.write_text("def do_other_thing():\n    pass\n")
            index_paths(engine, repo_id, temp_repo, {a_file})

            result = engine.run_cypher(
                "MATCH (:Function {repo_id: $repo_id, name: 'caller'})"
                "-[:CALLS]->(f:Function {repo_id: $repo_id}) RETURN f.name as name",
                {"repo_id": repo_id},
            )
            names = {r["name"] for r in result}
            assert "do_thing" not in names, f"stale CALLS edge survived: {names}"
        finally:
            engine.delete_repository(repo_id)

    def test_indexes_datastore_usage_from_same_python_file(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_datastore"
        py_file = temp_repo / "db.py"
        py_file.write_text("import redis\ncache = redis.Redis()\n")

        try:
            index_paths(engine, repo_id, temp_repo, {py_file})
            result = engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) WHERE n.provider = 'Redis' OR n.name = 'Redis' "
                "RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert result[0]["c"] >= 1
        finally:
            engine.delete_repository(repo_id)

    def test_indexes_dockerfile(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_container"
        dockerfile = temp_repo / "Dockerfile"
        dockerfile.write_text("FROM python:3.13\n")

        try:
            count = index_paths(engine, repo_id, temp_repo, {dockerfile})
            assert count == 1
            result = engine.run_cypher(
                "MATCH (c:Container {repo_id: $repo_id}) RETURN COUNT(*) as c", {"repo_id": repo_id}
            )
            assert result[0]["c"] >= 1
        finally:
            engine.delete_repository(repo_id)

    def test_indexes_docs_note_under_docs_path(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_docs"
        docs_dir = temp_repo / "docs"
        docs_dir.mkdir()
        note = docs_dir / "req.md"
        note.write_text("---\ntype: requirement\nid: req-1\n---\n# A requirement\n")

        try:
            count = index_paths(engine, repo_id, temp_repo, {note}, docs_path="docs")
            assert count == 1
            result = engine.run_cypher(
                "MATCH (r:Requirement {repo_id: $repo_id, name: 'req-1'}) RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert result[0]["c"] == 1
        finally:
            engine.delete_repository(repo_id)

    def test_sibling_directory_sharing_the_repo_name_prefix_is_skipped(self, engine):
        repo_id = "_smoketest_dispatch_sibling_prefix"
        with tempfile.TemporaryDirectory() as parent:
            repo_root = Path(parent) / "proj"
            repo_root.mkdir()
            sibling = Path(parent) / "proj-private"
            sibling.mkdir()
            sibling_file = sibling / "secret.py"
            sibling_file.write_text("class ShouldNotAppear:\n    pass\n")

            try:
                count = index_paths(engine, repo_id, repo_root, {sibling_file})
                assert count == 0

                result = engine.run_cypher(
                    "MATCH (c:Class {repo_id: $repo_id, name: 'ShouldNotAppear'}) RETURN COUNT(*) as c",
                    {"repo_id": repo_id},
                )
                assert result[0]["c"] == 0
            finally:
                engine.delete_repository(repo_id)

    def test_markdown_outside_docs_path_is_skipped(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_docs_skip"
        note = temp_repo / "README.md"
        note.write_text("---\ntype: requirement\nid: req-skip\n---\n# Should not be indexed\n")

        try:
            count = index_paths(engine, repo_id, temp_repo, {note}, docs_path="docs")
            assert count == 0
        finally:
            engine.delete_repository(repo_id)

    def test_markdown_in_sibling_of_docs_path_is_skipped(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_docs_sibling_prefix"
        (temp_repo / "docs").mkdir()
        (temp_repo / "docs-private").mkdir()
        note = temp_repo / "docs-private" / "note.md"
        note.write_text("---\ntype: requirement\nid: req-sibling\n---\n# Should not be indexed\n")

        try:
            count = index_paths(engine, repo_id, temp_repo, {note}, docs_path="docs")
            assert count == 0
        finally:
            engine.delete_repository(repo_id)

    def test_path_outside_repo_root_is_skipped(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_outside"
        with tempfile.TemporaryDirectory() as other_dir:
            outside_file = Path(other_dir) / "evil.py"
            outside_file.write_text("class ShouldNotAppear:\n    pass\n")

            count = index_paths(engine, repo_id, temp_repo, {outside_file})
            assert count == 0

            result = engine.run_cypher(
                "MATCH (c:Class {repo_id: $repo_id, name: 'ShouldNotAppear'}) RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert result[0]["c"] == 0


class TestRemovePaths:
    def test_sibling_directory_sharing_the_repo_name_prefix_is_skipped(self, engine):
        repo_id = "_smoketest_dispatch_remove_sibling_prefix"
        with tempfile.TemporaryDirectory() as parent:
            repo_root = Path(parent) / "proj"
            repo_root.mkdir()
            (repo_root / "a.py").write_text("class KeepMe:\n    pass\n")
            sibling = Path(parent) / "proj-private"
            sibling.mkdir()

            try:
                index_paths(engine, repo_id, repo_root, {repo_root / "a.py"})
                cleaned = remove_paths(engine, repo_id, repo_root, {sibling / "a.py"})
                assert cleaned == 0

                result = engine.run_cypher(
                    "MATCH (c:Class {repo_id: $repo_id, name: 'KeepMe'}) RETURN COUNT(*) as c",
                    {"repo_id": repo_id},
                )
                assert result[0]["c"] == 1
            finally:
                engine.delete_repository(repo_id)

    def test_removes_nodes_for_deleted_python_file(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_remove"
        py_file = temp_repo / "gone.py"
        py_file.write_text("class Gone:\n    pass\n")

        try:
            index_paths(engine, repo_id, temp_repo, {py_file})
            result = engine.run_cypher(
                "MATCH (c:Class {repo_id: $repo_id, name: 'Gone'}) RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert result[0]["c"] == 1

            py_file.unlink()
            cleaned = remove_paths(engine, repo_id, temp_repo, {py_file})
            assert cleaned == 1

            result = engine.run_cypher(
                "MATCH (c:Class {repo_id: $repo_id, name: 'Gone'}) RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert result[0]["c"] == 0
        finally:
            engine.delete_repository(repo_id)

    def test_removes_nodes_for_deleted_kotlin_file(self, engine, temp_repo):
        """Deleting a .kt file must clean up its nodes.

        Regression for the Kotlin-support change: `.kt` was added to the
        indexing side (_index_single_path) but not to remove_paths, so a
        deleted Kotlin file left stale nodes in the graph forever.
        """
        repo_id = "_smoketest_dispatch_remove_kt"
        kt_file = temp_repo / "Gone.kt"
        kt_file.write_text("class Gone {\n    fun gone() {}\n}\n")

        try:
            index_paths(engine, repo_id, temp_repo, {kt_file})
            result = engine.run_cypher(
                "MATCH (c:Class {repo_id: $repo_id, name: 'Gone'}) RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert result[0]["c"] == 1

            kt_file.unlink()
            cleaned = remove_paths(engine, repo_id, temp_repo, {kt_file})
            assert cleaned == 1

            result = engine.run_cypher(
                "MATCH (c:Class {repo_id: $repo_id, name: 'Gone'}) RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert result[0]["c"] == 0
        finally:
            engine.delete_repository(repo_id)

    def test_removes_document_node_for_deleted_markdown_file_in_subdirectory(self, engine, temp_repo):
        """Deleting a .md file in a subdirectory must clean up its Document node.

        This tests the fix for the deletion bug where remove_paths() was using
        bare filename instead of repo-relative path. Without the fix, Document
        nodes keyed by repo-relative path (e.g., 'docs/guide.md') wouldn't be
        found when deleting by bare filename ('guide.md').
        """
        repo_id = "_smoketest_dispatch_remove_md_subdir"

        # Create a Python file with a known entity
        py_file = temp_repo / "service.py"
        py_file.write_text("def my_handler():\n    pass\n")

        # Create Markdown file in a subdirectory mentioning that entity
        docs_dir = temp_repo / "docs"
        docs_dir.mkdir()
        md_file = docs_dir / "guide.md"
        md_file.write_text("# Guide\n\nThe `my_handler()` function is important.\n")

        try:
            # Index both files
            index_paths(engine, repo_id, temp_repo, {py_file})
            index_paths(engine, repo_id, temp_repo, {md_file}, mentions_enabled=True)

            # Verify Document node was created with repo-relative path as key
            result = engine.run_cypher(
                "MATCH (d:Document {repo_id: $repo_id, name: 'docs/guide.md'}) RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert result[0]["c"] == 1, "Document node should exist with repo-relative path"

            # Delete the file and clean up its provenance
            md_file.unlink()
            cleaned = remove_paths(engine, repo_id, temp_repo, {md_file})
            assert cleaned == 1, "remove_paths should report 1 file cleaned"

            # Verify Document node was actually deleted
            result = engine.run_cypher(
                "MATCH (d:Document {repo_id: $repo_id, name: 'docs/guide.md'}) RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert result[0]["c"] == 0, "Document node should be deleted"
        finally:
            engine.delete_repository(repo_id)


    def test_a_deleted_directory_removes_its_language_nodes(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_remove_dir"
        fresh = f"{repo_id}_fresh"
        (temp_repo / "pkg" / "sub").mkdir(parents=True)
        (temp_repo / "pkg" / "mod.py").write_text("class Widget:\n    pass\n")
        (temp_repo / "pkg" / "sub" / "util.py").write_text("def helper():\n    return 2\n")
        (temp_repo / "keep.py").write_text("def kept():\n    pass\n")
        try:
            full_scan(engine, repo_id, temp_repo)
            shutil.rmtree(temp_repo / "pkg")
            remove_paths(engine, repo_id, temp_repo, {temp_repo / "pkg"})

            left = engine.run_cypher(
                "MATCH (n {repo_id: $r}) WHERE coalesce(n.source_file, n.file, n.name) STARTS WITH 'pkg/' "
                "RETURN n.name AS n",
                {"r": repo_id},
            )
            assert left == []
            full_scan(engine, fresh, temp_repo)
            assert _graph(engine, repo_id) == _graph(engine, fresh)
        finally:
            engine.delete_repository(repo_id)
            engine.delete_repository(fresh)

    def test_one_graph_files_query_per_call(self, engine, temp_repo, monkeypatch):
        repo_id = "_smoketest_dispatch_remove_one_query"
        (temp_repo / "pkg").mkdir()
        (temp_repo / "pkg" / "mod.py").write_text("class Widget:\n    pass\n")
        try:
            full_scan(engine, repo_id, temp_repo)
            (temp_repo / "pkg" / "mod.py").unlink()
            calls = []
            original = engine.list_indexed_files
            monkeypatch.setattr(engine, "list_indexed_files", lambda r: calls.append(r) or original(r))
            remove_paths(engine, repo_id, temp_repo, {temp_repo / "pkg" / "mod.py"})
            assert calls == [repo_id]
        finally:
            engine.delete_repository(repo_id)

    def test_a_deleted_service_folder_unclaims_its_dockerfile(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_remove_dockerfile_dir"
        fresh = f"{repo_id}_fresh"
        (temp_repo / "svc").mkdir()
        (temp_repo / "svc" / "Dockerfile").write_text("FROM python:3.12\n")
        (temp_repo / "Containerfile").write_text("FROM postgres:16\n")
        try:
            full_scan(engine, repo_id, temp_repo)
            assert _claimed_by(engine, repo_id, "svc/Dockerfile")
            shutil.rmtree(temp_repo / "svc")
            remove_paths(engine, repo_id, temp_repo, {temp_repo / "svc"})
            assert not _claimed_by(engine, repo_id, "svc/Dockerfile")
            full_scan(engine, fresh, temp_repo)
            assert _graph(engine, repo_id) == _graph(engine, fresh)
        finally:
            engine.delete_repository(repo_id)
            engine.delete_repository(fresh)

    def test_a_containerfile_deleted_while_away_is_pruned(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_prune_containerfile"
        fresh = f"{repo_id}_fresh"
        (temp_repo / "Containerfile").write_text("FROM python:3.12\n")
        (temp_repo / "app.py").write_text("def main():\n    pass\n")
        try:
            full_scan(engine, repo_id, temp_repo)
            assert _claimed_by(engine, repo_id, "Containerfile")
            (temp_repo / "Containerfile").unlink()
            assert dispatch.prune_stale_files(engine, repo_id, temp_repo) == 1
            assert not _claimed_by(engine, repo_id, "Containerfile")
            full_scan(engine, fresh, temp_repo)
            assert _graph(engine, repo_id) == _graph(engine, fresh)
        finally:
            engine.delete_repository(repo_id)
            engine.delete_repository(fresh)


def _claimed_by(engine, repo_id, source):
    return engine.run_cypher(
        "MATCH (n {repo_id: $r}) WHERE $s IN coalesce(n.sources, [n.source]) RETURN n.name AS n",
        {"r": repo_id, "s": source},
    )


def _graph(engine, repo_id):
    nodes = engine.run_cypher(
        "MATCH (n {repo_id: $r}) WHERE NOT n:Repository RETURN labels(n) AS labels, properties(n) AS p",
        {"r": repo_id},
    )
    rels = engine.run_cypher(
        "MATCH (a {repo_id: $r})-[x]->(b {repo_id: $r}) "
        "RETURN labels(a)[0] AS a, a.name AS an, type(x) AS t, labels(b)[0] AS b, b.name AS bn",
        {"r": repo_id},
    )
    return (
        sorted(
            repr((sorted(n["labels"]), sorted((k, repr(v)) for k, v in n["p"].items() if k != "repo_id")))
            for n in nodes
        ),
        sorted((r["a"], r["an"] or "", r["t"], r["b"], r["bn"] or "") for r in rels),
    )


class TestFullScan:
    def test_full_scan_indexes_multiple_files(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_fullscan"
        (temp_repo / "a.py").write_text("class A:\n    pass\n")
        (temp_repo / "b.py").write_text("class B:\n    pass\n")
        (temp_repo / ".git").mkdir()
        (temp_repo / ".git" / "ignored.py").write_text("class ShouldBeSkipped:\n    pass\n")

        try:
            count = full_scan(engine, repo_id, temp_repo)
            assert count == 2

            result = engine.run_cypher(
                "MATCH (c:Class {repo_id: $repo_id}) RETURN c.name as name ORDER BY c.name",
                {"repo_id": repo_id},
            )
            names = [r["name"] for r in result]
            assert names == ["A", "B"]
        finally:
            engine.delete_repository(repo_id)

    def test_full_scan_skips_unstatable_file_instead_of_crashing(self, engine, temp_repo, monkeypatch):
        """A file the OS refuses to stat (locked, broken symlink, Windows
        reparse point) must be skipped, not abort the whole scan — this is
        what WinError 1920 on a HuggingFace-cache symlink used to do."""
        repo_id = "_smoketest_dispatch_unstatable"
        (temp_repo / "a.py").write_text("class A:\n    pass\n")
        cursed = temp_repo / "cursed.bin"
        cursed.write_bytes(b"")

        real_is_file = Path.is_file

        def flaky_is_file(self, *args, **kwargs):
            if self.name == "cursed.bin":
                raise OSError("[WinError 1920] The file cannot be accessed by the system")
            return real_is_file(self, *args, **kwargs)

        monkeypatch.setattr(Path, "is_file", flaky_is_file)

        try:
            count = full_scan(engine, repo_id, temp_repo)
            assert count == 1
        finally:
            engine.delete_repository(repo_id)

    def test_full_scan_skips_symlink_pointing_outside_the_repo(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_symlink_out"
        (temp_repo / "a.py").write_text("class A:\n    pass\n")
        with tempfile.TemporaryDirectory() as other_dir:
            outside_file = Path(other_dir) / "outside.py"
            outside_file.write_text("class Outside:\n    pass\n")
            (temp_repo / "link.py").symlink_to(outside_file)

            try:
                count = full_scan(engine, repo_id, temp_repo)
                assert count == 1

                result = engine.run_cypher(
                    "MATCH (c:Class {repo_id: $repo_id}) RETURN c.name as name ORDER BY c.name",
                    {"repo_id": repo_id},
                )
                assert [r["name"] for r in result] == ["A"]
            finally:
                engine.delete_repository(repo_id)

    def test_full_scan_follows_symlink_within_the_repo(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_symlink_in"
        (temp_repo / "pkg").mkdir()
        (temp_repo / "pkg" / "a.py").write_text("class A:\n    pass\n")
        (temp_repo / "alias.py").symlink_to(temp_repo / "pkg" / "a.py")

        try:
            full_scan(engine, repo_id, temp_repo)

            result = engine.run_cypher(
                "MATCH (c:Class {repo_id: $repo_id}) RETURN c.name as name, c.file as file",
                {"repo_id": repo_id},
            )
            assert [(r["name"], r["file"]) for r in result] == [("A", "pkg/a.py")]
        finally:
            engine.delete_repository(repo_id)

    def test_full_scan_prunes_nodes_for_file_deleted_since_last_index(self, engine, temp_repo):
        """A file deleted while the watcher was down must have its nodes
        pruned by the next full_scan — a rescan reconciles the graph to
        disk, it doesn't just add/update what's current."""
        repo_id = "_smoketest_dispatch_prune"
        py_file = temp_repo / "gone.py"
        py_file.write_text("class Gone:\n    pass\n")
        keep_file = temp_repo / "keep.py"
        keep_file.write_text("class Keep:\n    pass\n")

        try:
            full_scan(engine, repo_id, temp_repo)
            result = engine.run_cypher(
                "MATCH (c:Class {repo_id: $repo_id}) RETURN c.name as name ORDER BY c.name",
                {"repo_id": repo_id},
            )
            assert [r["name"] for r in result] == ["Gone", "Keep"]

            # Delete the file behind 'Gone' — no watcher running, so nothing
            # cleans it up until the next full_scan.
            py_file.unlink()
            full_scan(engine, repo_id, temp_repo)

            result = engine.run_cypher(
                "MATCH (c:Class {repo_id: $repo_id}) RETURN c.name as name ORDER BY c.name",
                {"repo_id": repo_id},
            )
            assert [r["name"] for r in result] == ["Keep"], "stale node for deleted file should be pruned"
        finally:
            engine.delete_repository(repo_id)

    def test_full_scan_prunes_ignored_dir_files_indexed_before_ignore(self, engine, temp_repo):
        """Files under a now-ignored directory (e.g. .playwright-mcp) must be
        pruned by a rescan even though they were indexed before the ignore
        rule existed — the disk-vs-graph diff uses the same ignore filter as
        the index walk."""
        repo_id = "_smoketest_dispatch_prune_ignored"
        scratch = temp_repo / ".playwright-mcp"
        scratch.mkdir()
        scratch_file = scratch / "dump.txt"
        scratch_file.write_text("scratch")
        (temp_repo / "keep.py").write_text("class Keep:\n    pass\n")

        try:
            # Simulate a bare Module node keyed on the repo-relative path —
            # the shape the docs/mentions extractors write, and what a file
            # indexed before .playwright-mcp was ignored would look like.
            engine.upsert_node(
                "Module",
                repo_id,
                ".playwright-mcp/dump.txt",
                {"type": "module", "source_file": ".playwright-mcp/dump.txt"},
            )
            result = engine.run_cypher(
                "MATCH (m:Module {repo_id: $repo_id}) RETURN m.name as name",
                {"repo_id": repo_id},
            )
            assert any("dump.txt" in r["name"] for r in result), "scratch file should be in graph"

            # A rescan must prune it: it's not an indexable file anymore.
            full_scan(engine, repo_id, temp_repo)
            result = engine.run_cypher(
                "MATCH (m:Module {repo_id: $repo_id}) RETURN m.name as name",
                {"repo_id": repo_id},
            )
            assert not any("dump.txt" in r["name"] for r in result), "ignored-dir file should be pruned"
        finally:
            engine.delete_repository(repo_id)


class TestServiceCrossLinking:
    """Previously the container extractor (compose-derived Service nodes)
    and the datastore/API extractors (per-file Database/Endpoint nodes)
    never cross-referenced each other — explain_architecture's Service
    'uses'/'calls' output stayed empty even on a fully-scanned repo. Fixed
    via build_context-based directory containment matching.
    """

    def test_full_scan_links_service_to_datastore_it_uses(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_service_link"
        (temp_repo / "docker-compose.yml").write_text(
            "services:\n"
            "  api:\n"
            "    build: ./services/api\n"
        )
        api_dir = temp_repo / "services" / "api"
        api_dir.mkdir(parents=True)
        (api_dir / "db.py").write_text("import redis\ncache = redis.Redis()\n")

        try:
            full_scan(engine, repo_id, temp_repo)

            result = engine.run_cypher(
                "MATCH (s:Service {repo_id: $repo_id, name: 'api'})-[:USES]->(d) "
                "RETURN labels(d) as labels, d.name as name",
                {"repo_id": repo_id},
            )
            assert any(r["name"] == "Redis" for r in result)
        finally:
            engine.delete_repository(repo_id)

    def test_full_scan_links_endpoint_to_owning_service(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_endpoint_link"
        (temp_repo / "docker-compose.yml").write_text(
            "services:\n"
            "  web:\n"
            "    build: ./services/web\n"
        )
        web_dir = temp_repo / "services" / "web"
        web_dir.mkdir(parents=True)
        (web_dir / "app.py").write_text(
            "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/health')\ndef health():\n    pass\n"
        )

        try:
            full_scan(engine, repo_id, temp_repo)

            result = engine.run_cypher(
                "MATCH (e:Endpoint {repo_id: $repo_id})-[:CALLS]->(s:Service {name: 'web'}) "
                "RETURN e.name as name",
                {"repo_id": repo_id},
            )
            assert any(r["name"] == "GET /health" for r in result)
        finally:
            engine.delete_repository(repo_id)

    def test_file_outside_any_build_context_is_not_linked(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_no_link"
        (temp_repo / "docker-compose.yml").write_text(
            "services:\n"
            "  api:\n"
            "    build: ./services/api\n"
        )
        (temp_repo / "services" / "api").mkdir(parents=True)
        # File lives outside the api service's build context (e.g. shared code).
        (temp_repo / "shared_db.py").write_text("import redis\ncache = redis.Redis()\n")

        try:
            full_scan(engine, repo_id, temp_repo)

            result = engine.run_cypher(
                "MATCH (s:Service {repo_id: $repo_id})-[:USES]->(d:Cache) RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert result[0]["c"] == 0
        finally:
            engine.delete_repository(repo_id)


class TestMentionsIntegration:
    def test_mentions_enabled_creates_document_and_edge(self, engine, temp_repo):
        """With mentions_enabled=True, a .md file mentioning a known entity creates Document and MENTIONS edge."""
        repo_id = "_smoketest_dispatch_mentions_enabled"

        # First, create a Python file with a Function
        py_file = temp_repo / "service.py"
        py_file.write_text("def my_handler():\n    pass\n")

        # Then, create a Markdown file mentioning that function
        md_file = temp_repo / "README.md"
        md_file.write_text("# API Guide\n\nThe `my_handler()` function processes requests.\n")

        try:
            # Index the Python file first so the Function exists
            index_paths(engine, repo_id, temp_repo, {py_file})

            # Index the Markdown file with mentions_enabled=True
            index_paths(engine, repo_id, temp_repo, {md_file}, mentions_enabled=True)

            # Check that a Document node was created
            doc_result = engine.run_cypher(
                "MATCH (d:Document {repo_id: $repo_id, name: 'README.md'}) RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert doc_result[0]["c"] == 1

            # Check that a MENTIONS relationship exists
            mentions_result = engine.run_cypher(
                "MATCH (d:Document {repo_id: $repo_id, name: 'README.md'})"
                "-[:MENTIONS]->(f:Function {repo_id: $repo_id, name: 'my_handler'}) "
                "RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert mentions_result[0]["c"] == 1
        finally:
            engine.delete_repository(repo_id)

    def test_mentions_disabled_skips_markdown_outside_docs_path(self, engine, temp_repo):
        """With mentions_enabled=False (default), .md files outside docs_root are not scanned at all."""
        repo_id = "_smoketest_dispatch_mentions_disabled"

        # Create a Python file with a Function
        py_file = temp_repo / "service.py"
        py_file.write_text("def helper():\n    pass\n")

        # Create a Markdown file at repo root (outside docs_path)
        md_file = temp_repo / "README.md"
        md_file.write_text("# Readme\n\nThe `helper()` function is important.\n")

        try:
            # Index Python file
            index_paths(engine, repo_id, temp_repo, {py_file})

            # Index Markdown file with mentions_enabled=False (default)
            index_paths(engine, repo_id, temp_repo, {md_file}, mentions_enabled=False)

            # Verify no Document node was created
            doc_result = engine.run_cypher(
                "MATCH (d:Document {repo_id: $repo_id, name: 'README.md'}) RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert doc_result[0]["c"] == 0

            # Verify no MENTIONS relationship
            mentions_result = engine.run_cypher(
                "MATCH (d:Document)-[:MENTIONS]->(f:Function {repo_id: $repo_id, name: 'helper'}) "
                "RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert mentions_result[0]["c"] == 0
        finally:
            engine.delete_repository(repo_id)

    def test_mentions_integration_across_file_types(self, engine, temp_repo):
        """Indexing Python files first, then Markdown files with mentions enabled."""
        repo_id = "_smoketest_dispatch_mentions_integration"

        # Create a Python file with classes and functions
        py_file = temp_repo / "service.py"
        py_file.write_text("class ApiHandler:\n    pass\n\ndef validate_input():\n    pass\n")

        # Create a Markdown file that mentions those entities
        guide = temp_repo / "ARCHITECTURE.md"
        guide.write_text("# Architecture\n\nThe `ApiHandler` class and `validate_input()` function handle requests.\n")

        try:
            # Index Python file first so entities exist
            index_paths(engine, repo_id, temp_repo, {py_file})

            # Then index Markdown file with mentions enabled
            index_paths(engine, repo_id, temp_repo, {guide}, mentions_enabled=True)

            # Check that a Document node was created
            doc_result = engine.run_cypher(
                "MATCH (d:Document {repo_id: $repo_id, name: 'ARCHITECTURE.md'}) RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert doc_result[0]["c"] == 1

            # Check that mentions were indexed for both entities
            mentions_result = engine.run_cypher(
                "MATCH (d:Document {repo_id: $repo_id})-[:MENTIONS]->(n) "
                "RETURN COUNT(*) as c",
                {"repo_id": repo_id},
            )
            assert mentions_result[0]["c"] >= 2
        finally:
            engine.delete_repository(repo_id)


# A small repo whose graph depends on file order if indexing follows set
# order: an Endpoint IMPLEMENTS edge to a same-named handler in another file,
# a Java interface/implementation pair, a Markdown doc mentioning code
# symbols, a SUPERSEDES chain of design decisions, and a Datastore node two
# files (and two libraries in one file) both claim.
_ORDER_FIXTURE = {
    "api/routes.py": (
        "import psycopg2\nimport psycopg\n\n"
        "@app.get('/items')\ndef list_items():\n    return fetch_items()\n"
    ),
    "api/handlers.py": "def list_items():\n    pass\n\ndef fetch_items():\n    pass\n",
    "worker.py": "import psycopg2\nimport redis\n\nclass Worker:\n    def run(self):\n        fetch_items()\n",
    "store/Store.java": "package store;\n\npublic interface Store {\n    void save();\n}\n",
    # Kotlin implementation before its same-package interface.
    "repo/ASqlRepo.kt": "package repo\n\nclass ASqlRepo : Repo {\n    override fun save() {}\n}\n",
    "repo/Repo.kt": "package repo\n\ninterface Repo {\n    fun save()\n}\n",
    "store/SqlStore.java": (
        "package store;\n\npublic class SqlStore implements Store {\n    public void save() {}\n}\n"
    ),
    "NOTES.md": "# Notes\n\n`list_items()` is served by `SqlStore` and `Worker`.\n",
    "docs/adr-0001.md": "---\ntype: design_decision\nid: adr-0001\n---\n# Use Postgres\n",
    "docs/adr-0002.md": (
        "---\ntype: design_decision\nid: adr-0002\nsupersedes: adr-0001\n---\n# Use `SqlStore`\n"
    ),
    "docs/adr-0003.md": (
        "---\ntype: design_decision\nid: adr-0003\nsupersedes: adr-0002\n---\n# Keep `Worker`\n"
    ),
}

# Runs one full scan in a fresh interpreter (so PYTHONHASHSEED takes effect)
# and prints the resulting graph with the per-run repo_id stripped.
_SCAN_AND_EXPORT = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    from devgraph.graph.engine import GraphEngine
    from devgraph.indexer.dispatch import full_scan

    repo_root, repo_id = Path(sys.argv[1]), sys.argv[2]
    engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        full_scan(engine, repo_id, repo_root, docs_path="docs", mentions_enabled=True)

        def ident(labels, props):
            return [sorted(labels), props.get("name"), props.get("file"), props.get("source_file")]

        nodes = sorted(
            json.dumps([sorted(r["labels"]), {k: v for k, v in r["props"].items() if k != "repo_id"}], sort_keys=True)
            for r in engine.run_cypher(
                "MATCH (n {repo_id: $repo_id}) RETURN labels(n) AS labels, properties(n) AS props",
                {"repo_id": repo_id},
            )
        )
        rels = sorted(
            json.dumps(
                [ident(r["a_labels"], r["a"]), r["type"], r["props"], ident(r["b_labels"], r["b"])],
                sort_keys=True,
            )
            for r in engine.run_cypher(
                "MATCH (a {repo_id: $repo_id})-[rel]->(b {repo_id: $repo_id}) "
                "RETURN labels(a) AS a_labels, properties(a) AS a, type(rel) AS type, "
                "properties(rel) AS props, labels(b) AS b_labels, properties(b) AS b",
                {"repo_id": repo_id},
            )
        )
        print(json.dumps({"nodes": nodes, "rels": rels}))
    finally:
        engine.delete_repository(repo_id)
        engine.close()
    """
)


class _NoImportersEngine:
    def find_importing_modules(self, repo_id, module_name):
        return []

    def list_file_nodes(self, repo_id, files):
        return set()

    def read_applied_schema(self, repo_id):
        return None

    def update_skipped_files(self, repo_id, add=None, drop=(), replace=False):
        pass


class TestDeterministicIndexOrder:
    def test_index_paths_processes_files_in_sorted_path_order(self, temp_repo, monkeypatch):
        """Files are indexed in repo-relative path order, whatever order the
        caller's set happens to iterate in."""
        for rel in _ORDER_FIXTURE:
            (temp_repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (temp_repo / rel).write_text(_ORDER_FIXTURE[rel])

        seen: list[str] = []

        def record(engine, repo_id, repo_root, resolved, rel_path, *args):
            seen.append(rel_path)
            return 1

        monkeypatch.setattr(dispatch, "_index_single_path", record)
        index_paths(_NoImportersEngine(), "_unit_order", temp_repo, {temp_repo / rel for rel in _ORDER_FIXTURE})

        assert seen == sorted(_ORDER_FIXTURE)

    def test_full_scan_graph_is_identical_across_hash_seeds(self, engine, temp_repo):
        """Two full scans of the same tree under different PYTHONHASHSEEDs
        must produce the same nodes, properties and edges."""
        for rel in _ORDER_FIXTURE:
            (temp_repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (temp_repo / rel).write_text(_ORDER_FIXTURE[rel])

        exports = []
        for seed in ("1", "2", "3"):
            repo_id = f"_smoketest_dispatch_hashseed_{seed}"
            proc = subprocess.run(
                [sys.executable, "-c", _SCAN_AND_EXPORT, str(temp_repo), repo_id],
                env={**os.environ, "PYTHONHASHSEED": seed},
                capture_output=True,
                text=True,
                timeout=300,
            )
            engine.delete_repository(repo_id)
            assert proc.returncode == 0, proc.stderr
            exports.append(json.loads(proc.stdout.strip().splitlines()[-1]))

        assert exports[0]["rels"], "fixture should produce edges"
        for other in exports[1:]:
            assert other == exports[0]

    def test_index_paths_orders_mixed_relative_and_absolute_paths_by_repo_path(self, temp_repo, monkeypatch):
        """A relative path and an absolute path are ordered by where they sit
        in the repo, not by how the caller spelled them."""
        (temp_repo / "a.py").write_text("def a():\n    pass\n")
        (temp_repo / "b.py").write_text("def b():\n    pass\n")
        seen: list[str] = []

        def record(engine, repo_id, repo_root, resolved, rel_path, *args):
            seen.append(rel_path)
            return 1

        monkeypatch.setattr(dispatch, "_index_single_path", record)
        # Leave the directory before temp_repo removes it: Windows can't delete the working directory.
        with monkeypatch.context() as m:
            m.chdir(temp_repo)
            # As raw strings "/tmp/.../b.py" sorts before "a.py".
            index_paths(_NoImportersEngine(), "_unit_order", temp_repo, {Path("a.py"), (temp_repo / "b.py").resolve()})

        assert seen == ["a.py", "b.py"]


class _RowsEngine:
    def __init__(self, rows):
        self.rows = rows

    def run_cypher(self, query, params):
        return list(self.rows)


class TestOwningServiceTieBreak:
    def test_services_sharing_a_build_context_resolve_the_same_way_in_any_row_order(self):
        rows = [
            {"name": "web", "build_context": "app", "file": "compose.yml"},
            {"name": "api", "build_context": "app", "file": "compose.yml"},
        ]
        owners = {
            dispatch._match_owning_service(
                dispatch._load_services_with_build_context(_RowsEngine(order), "_unit_services"), "app/main.py"
            )
            for order in (rows, rows[::-1])
        }
        assert owners == {"api"}


# Every referrer here sorts BEFORE the file whose node its edge targets, so
# an edge only forms on a first scan if index_paths resolves cross-file edges
# after every node in the batch exists.
_REFERRER_FIRST_FIXTURE = {
    # Mentions: a doc mentioning a later file's class, a later design
    # decision, and a later doc.
    "a.md": "# Guide\n\n`Zebra` is decided in `b-old`; see `notes/z.md`.\n",
    # Docs: a decision superseding / decided by later notes, linking a later module.
    "docs/a-new.md": (
        "---\ntype: design_decision\nid: a-new\nsupersedes: b-old\n"
        "decided_by: c-arch\nlinks: [src/z.py]\n---\n# New\n"
    ),
    "docs/b-old.md": "---\ntype: design_decision\nid: b-old\n---\n# Old\n",
    "docs/c-arch.md": "---\ntype: architecture_note\nid: c-arch\n---\n# Arch\n",
    "notes/z.md": "# Z notes\n",
    # Django route (urls.py) implemented by a view in a later file.
    "api/urls.py": "urlpatterns = [path('items/', item_list)]\n",
    "api/views.py": "def item_list(request):\n    pass\n",
    # Java implementation before its interface.
    "store/ASqlStore.java": "package store;\n\npublic class ASqlStore implements Store {\n    public void save() {}\n}\n",
    "store/Store.java": "package store;\n\npublic interface Store {\n    void save();\n}\n",
    # Kotlin implementation before its same-package interface.
    "repo/ASqlRepo.kt": "package repo\n\nclass ASqlRepo : Repo {\n    override fun save() {}\n}\n",
    "repo/Repo.kt": "package repo\n\ninterface Repo {\n    fun save()\n}\n",
    "src/z.py": "class Zebra:\n    pass\n",
}

_CROSS_FILE_EDGES = {
    "mentions class": (
        "MATCH (:Document {repo_id: $repo_id, name: 'a.md'})-[:MENTIONS]->(:Class {name: 'Zebra'}) RETURN count(*) AS c"
    ),
    "mentions decision": (
        "MATCH (:Document {repo_id: $repo_id, name: 'a.md'})-[:MENTIONS]->(:DesignDecision {name: 'b-old'}) "
        "RETURN count(*) AS c"
    ),
    "mentions document": (
        "MATCH (:Document {repo_id: $repo_id, name: 'a.md'})-[:MENTIONS]->(:Document {name: 'notes/z.md'}) "
        "RETURN count(*) AS c"
    ),
    "supersedes": (
        "MATCH (:DesignDecision {repo_id: $repo_id, name: 'a-new'})-[:SUPERSEDES]->(:DesignDecision {name: 'b-old'}) "
        "RETURN count(*) AS c"
    ),
    "decided by": (
        "MATCH (:DesignDecision {repo_id: $repo_id, name: 'a-new'})-[:DECIDED_BY]->(:ArchitectureNote {name: 'c-arch'}) "
        "RETURN count(*) AS c"
    ),
    "documented by": (
        "MATCH (:Module {repo_id: $repo_id, name: 'src/z.py'})-[:DOCUMENTED_BY]->(:DesignDecision {name: 'a-new'}) "
        "RETURN count(*) AS c"
    ),
    "implements": (
        "MATCH (:Endpoint {repo_id: $repo_id, name: '* items/'})-[:IMPLEMENTS]->"
        "(:Function {name: 'item_list', file: 'api/views.py'}) RETURN count(*) AS c"
    ),
    "extends": (
        "MATCH (:Class {repo_id: $repo_id, name: 'ASqlStore'})-[:EXTENDS]->(:Class {name: 'Store'}) RETURN count(*) AS c"
    ),
    "kotlin extends": (
        "MATCH (:Class {repo_id: $repo_id, name: 'ASqlRepo'})-[:EXTENDS]->(:Class {name: 'Repo'}) RETURN count(*) AS c"
    ),
}

# Per lookup kind: the referrer files, the target files added in a later
# batch, and the edges that batch must create from the referrers.
_RELINK_KINDS = {
    "docs notes": (
        {"docs/a-new.md"},
        {"docs/b-old.md", "docs/c-arch.md", "src/z.py"},
        {"supersedes", "decided by", "documented by"},
    ),
    "api handler stubs": ({"api/urls.py"}, {"api/views.py"}, {"implements"}),
    "mentions": (
        {"a.md"},
        {"src/z.py", "docs/b-old.md", "notes/z.md"},
        {"mentions class", "mentions decision", "mentions document"},
    ),
    "java same-package extends": ({"store/ASqlStore.java"}, {"store/Store.java"}, {"extends"}),
    "kotlin same-package extends": ({"repo/ASqlRepo.kt"}, {"repo/Repo.kt"}, {"kotlin extends"}),
}

_REFERRERS = set().union(*(referrers for referrers, _, _ in _RELINK_KINDS.values()))


def _write_fixture(root: Path, fixture: dict[str, str]) -> None:
    for rel, content in fixture.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content)


def _record_indexed(monkeypatch) -> list[str]:
    """Record the repo-relative path of every file index_paths indexes."""
    seen: list[str] = []
    original = dispatch._index_single_path

    def record(engine, repo_id, repo_root, resolved, rel_path, *args):
        seen.append(rel_path)
        return original(engine, repo_id, repo_root, resolved, rel_path, *args)

    monkeypatch.setattr(dispatch, "_index_single_path", record)
    return seen


def _missing_edges(engine, repo_id: str) -> list[str]:
    return [name for name, query in _CROSS_FILE_EDGES.items() if engine.run_cypher(query, {"repo_id": repo_id})[0]["c"] == 0]


class TestCrossFileEdgesIndependentOfOrder:
    def test_full_scan_creates_edges_whose_referrer_sorts_before_its_target(self, engine, temp_repo):
        repo_id = "_smoketest_dispatch_referrer_first_full"
        _write_fixture(temp_repo, _REFERRER_FIRST_FIXTURE)
        try:
            full_scan(engine, repo_id, temp_repo, docs_path="docs", mentions_enabled=True)
            assert _missing_edges(engine, repo_id) == []
        finally:
            engine.delete_repository(repo_id)

    def test_adding_a_target_later_relinks_existing_referrers(self, engine, temp_repo):
        """An incremental batch that adds only target files re-indexes the
        referrers indexed earlier, so their edges form without the referrer
        changing or a full rescan."""
        repo_id = "_smoketest_dispatch_referrer_first_incremental"
        _write_fixture(temp_repo, _REFERRER_FIRST_FIXTURE)
        try:
            index_paths(engine, repo_id, temp_repo, {temp_repo / r for r in _REFERRERS}, docs_path="docs", mentions_enabled=True)
            assert set(_missing_edges(engine, repo_id)) == set(_CROSS_FILE_EDGES)

            targets = set(_REFERRER_FIRST_FIXTURE) - _REFERRERS
            index_paths(engine, repo_id, temp_repo, {temp_repo / t for t in targets}, docs_path="docs", mentions_enabled=True)
            assert _missing_edges(engine, repo_id) == []
        finally:
            engine.delete_repository(repo_id)

    @pytest.mark.parametrize("kind", sorted(_RELINK_KINDS))
    def test_each_lookup_kind_relinks_its_referrers(self, engine, temp_repo, kind):
        referrers, targets, edges = _RELINK_KINDS[kind]
        repo_id = "_smoketest_dispatch_relink_" + kind.replace(" ", "_")
        _write_fixture(temp_repo, {rel: _REFERRER_FIRST_FIXTURE[rel] for rel in referrers | targets})
        mentions_enabled = kind == "mentions"
        try:
            index_paths(engine, repo_id, temp_repo, {temp_repo / r for r in referrers}, docs_path="docs", mentions_enabled=mentions_enabled)
            index_paths(engine, repo_id, temp_repo, {temp_repo / t for t in targets}, docs_path="docs", mentions_enabled=mentions_enabled)
            assert edges.isdisjoint(_missing_edges(engine, repo_id))
        finally:
            engine.delete_repository(repo_id)

    def test_batches_that_add_no_targets_index_no_extra_files(self, engine, temp_repo, monkeypatch):
        """Cost guard: a full scan indexes each file once, and re-indexing
        unchanged target files (they add no new nodes) pulls in no referrers."""
        repo_id = "_smoketest_dispatch_relink_cost"
        _write_fixture(temp_repo, _REFERRER_FIRST_FIXTURE)
        seen = _record_indexed(monkeypatch)
        try:
            full_scan(engine, repo_id, temp_repo, docs_path="docs", mentions_enabled=True)
            assert sorted(seen) == sorted(_REFERRER_FIRST_FIXTURE)

            seen.clear()
            targets = set(_REFERRER_FIRST_FIXTURE) - _REFERRERS
            index_paths(engine, repo_id, temp_repo, {temp_repo / t for t in targets}, docs_path="docs", mentions_enabled=True)
            assert sorted(seen) == sorted(targets)
            assert _missing_edges(engine, repo_id) == []
        finally:
            engine.delete_repository(repo_id)

    def test_resaving_a_file_with_a_file_less_stub_indexes_no_extra_files(self, engine, temp_repo, monkeypatch):
        """Cost guard: a C++ out-of-class method definition emits a Class stub
        with no file provenance; re-saving the file unchanged must not count
        it as an added node and re-index the docs that mention it."""
        repo_id = "_smoketest_dispatch_relink_cost_cpp"
        _write_fixture(temp_repo, {
            "w/widget.cpp": '#include "widget.h"\nvoid Widget::draw() {}\n',
            "README.md": "# Readme\n\nUses `Widget`.\n",
        })
        seen = _record_indexed(monkeypatch)
        try:
            full_scan(engine, repo_id, temp_repo, mentions_enabled=True)
            seen.clear()
            index_paths(engine, repo_id, temp_repo, {temp_repo / "w/widget.cpp"}, mentions_enabled=True)
            assert seen == ["w/widget.cpp"]
        finally:
            engine.delete_repository(repo_id)

    @pytest.mark.parametrize(
        ("target", "before", "edge"),
        [
            ("src/z.py", "class Other:\n    pass\n", "mentions class"),
            ("api/views.py", "def other(request):\n    pass\n", "implements"),
            ("store/Store.java", "package store;\n\npublic interface Other {\n    void save();\n}\n", "extends"),
        ],
    )
    def test_an_existing_file_gaining_a_symbol_relinks_its_referrers(self, engine, temp_repo, target, before, edge):
        """The watcher's common case: the target file is already indexed and an
        edit adds (or renames to) the symbol a referrer names."""
        repo_id = "_smoketest_dispatch_relink_gain_" + Path(target).stem
        _write_fixture(temp_repo, _REFERRER_FIRST_FIXTURE)
        (temp_repo / target).write_text(before)
        try:
            full_scan(engine, repo_id, temp_repo, docs_path="docs", mentions_enabled=True)
            assert edge in _missing_edges(engine, repo_id)

            (temp_repo / target).write_text(_REFERRER_FIRST_FIXTURE[target])
            index_paths(engine, repo_id, temp_repo, {temp_repo / target}, docs_path="docs", mentions_enabled=True)
            assert edge not in _missing_edges(engine, repo_id)
        finally:
            engine.delete_repository(repo_id)


class TestMentionRelinkBound:
    """Adding a common name (`get`, `run`) must not re-index (or relink)
    every Markdown file that mentions it on a watcher save."""

    def _repo(self, root: Path) -> None:
        _write_fixture(root, {"a.py": "def alpha():\n    pass\n", "b.py": "def get():\n    pass\n"})
        for i in range(6):
            _write_fixture(root, {f"docs/d{i}.md": f"# Doc {i}\n\n```python\nget()\nput()\n```\n"})

    def _record_relinks(self, monkeypatch) -> list[str]:
        relinked: list[str] = []
        original = dispatch.index_mentions_file

        def record(engine, repo_id, path, repo_root, **kwargs):
            if kwargs.get("names") is not None:
                relinked.append(Path(path).name)
            return original(engine, repo_id, path, repo_root, **kwargs)

        monkeypatch.setattr(dispatch, "index_mentions_file", record)
        return relinked

    def _mentioning(self, engine, repo_id: str, name: str) -> int:
        return engine.run_cypher(
            "MATCH (:Document {repo_id: $repo_id})-[:MENTIONS]->(:Function {name: $name, file: 'a.py'}) RETURN count(*) AS c",
            {"repo_id": repo_id, "name": name},
        )[0]["c"]

    def test_a_name_that_already_exists_finds_referrers_in_the_graph(self, engine, temp_repo, monkeypatch):
        repo_id = "_smoketest_dispatch_relink_bound_existing"
        self._repo(temp_repo)
        monkeypatch.setattr(dispatch, "_MAX_MENTION_RELINKS", 3)
        text_scans: list[set[str]] = []
        original = dispatch.mentions_any
        monkeypatch.setattr(dispatch, "mentions_any", lambda content, names: text_scans.append(names) or original(content, names))
        try:
            full_scan(engine, repo_id, temp_repo, mentions_enabled=True)
            seen = _record_indexed(monkeypatch)
            relinked = self._record_relinks(monkeypatch)
            text_scans.clear()
            (temp_repo / "a.py").write_text("def alpha():\n    pass\n\ndef get():\n    pass\n")
            index_paths(engine, repo_id, temp_repo, {temp_repo / "a.py"}, mentions_enabled=True)

            assert text_scans == []
            assert seen == ["a.py"]
            assert len(relinked) == 3
            assert self._mentioning(engine, repo_id, "get") == 3
        finally:
            engine.delete_repository(repo_id)

    def test_a_name_new_to_the_graph_is_text_scanned_up_to_the_cap(self, engine, temp_repo, monkeypatch, caplog):
        repo_id = "_smoketest_dispatch_relink_bound_new"
        self._repo(temp_repo)
        monkeypatch.setattr(dispatch, "_MAX_MENTION_RELINKS", 3)
        try:
            full_scan(engine, repo_id, temp_repo, mentions_enabled=True)
            seen = _record_indexed(monkeypatch)
            relinked = self._record_relinks(monkeypatch)
            (temp_repo / "a.py").write_text("def alpha():\n    pass\n\ndef put():\n    pass\n")
            index_paths(engine, repo_id, temp_repo, {temp_repo / "a.py"}, mentions_enabled=True)

            assert seen == ["a.py"]
            assert len(relinked) == 3
            assert self._mentioning(engine, repo_id, "put") == 3
            assert "rescan" in caplog.text
        finally:
            engine.delete_repository(repo_id)


_KEYED_ADR_SCHEMA = textwrap.dedent("""
    version: 1
    node_types:
      - label: Adr
        key: [adr_id]
        metadata: [{name: path}, {name: adr_id}]
        source: {provider: docs, paths: ["decisions/*.md"], fields: {adr_id: id}}
    relationships:
      - {type: REPLACES, provider: docs, from: Adr, to: Adr, field: supersedes}
""")


class _RecordingEngine:
    """Records every engine call by name and arguments; returns None."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *args, **kwargs: self.calls.append((name, args))


def _keyed_docs(temp_repo):
    from devgraph.config.project_schema import parse_project_schema, resolve_declaration
    from devgraph.indexer.providers import docs

    files = {}
    for rel, body in {
        "decisions/adr-1.md": "id: ADR-1\nsupersedes: ADR-0",
        "decisions/adr-1 copy.md": "id: ADR-1\nsupersedes: ADR-9",
    }.items():
        path = temp_repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\n{body}\n---\n", encoding="utf-8")
        files[rel] = path
    spec = docs.docs_spec(resolve_declaration(parse_project_schema(_KEYED_ADR_SCHEMA, Path("devgraph.schema.yaml"))))
    return spec, files


class TestFieldKeyedDocsApply:
    """The apply/full-scan path gates field-keyed docs entries on owners from every file on disk."""

    def test_prune_docs_keeps_only_each_keys_owner(self, temp_repo):
        spec, files = _keyed_docs(temp_repo)
        engine = _RecordingEngine()
        nodes, selected, _owners = dispatch._prune_docs(engine, "demo", temp_repo, spec, files)
        assert [(n["name"], n["properties"]["path"]) for n in nodes] == [("ADR-1", "decisions/adr-1.md")]
        assert sorted(selected) == sorted(files)
        assert engine.calls[-1] == ("prune_extracted_nodes", ("demo", "docs", ["Adr:ADR-1"]))

    def test_prune_docs_writes_nothing_when_building_nodes_fails(self, temp_repo, monkeypatch):
        from devgraph.indexer.providers import docs

        spec, files = _keyed_docs(temp_repo)
        engine = _RecordingEngine()

        def boom(*args, **kwargs):
            raise ValueError("owners are required")

        monkeypatch.setattr(docs, "build_nodes", boom)
        with pytest.raises(ValueError):
            dispatch._prune_docs(engine, "demo", temp_repo, spec, files)
        assert engine.calls == []

    def test_full_scan_edge_pass_writes_edges_from_owners_only(self, temp_repo, monkeypatch):
        from devgraph.indexer.providers import docs

        spec, files = _keyed_docs(temp_repo)
        engine = _RecordingEngine()
        monkeypatch.setattr(dispatch, "schema_pending", lambda *args: False)
        selected = docs.read_selected(spec, files)
        owners = docs.keyed_owners(docs.keyed_claims(spec, selected))
        dispatch._sync_docs_edges(engine, "demo", temp_repo, (spec, selected, owners))
        ((name, (edges,)),) = engine.calls
        assert name == "upsert_relationships"
        assert [(e["from_path"], e["to_name"]) for e in edges] == [("decisions/adr-1.md", "ADR-0")]

    def test_a_batch_holding_a_loser_adds_only_its_keys_owner(self, temp_repo):
        spec, files = _keyed_docs(temp_repo)
        engine = _RecordingEngine()
        engine.extracted_entries = lambda repo_id, extractor, labels: {("Adr", "ADR-1", "decisions/adr-1.md")}
        engine.list_file_nodes = lambda repo_id, files: {("Adr", "ADR-1", "decisions/adr-1.md")}
        copy = "decisions/adr-1 copy.md"
        batch = dispatch._read_docs_batch(engine, "demo", temp_repo, spec, {copy: files[copy]}, set())
        assert sorted(batch.selected) == ["decisions/adr-1 copy.md", "decisions/adr-1.md"]
        assert [(n["name"], n["properties"]["path"]) for n in batch.nodes] == [("ADR-1", "decisions/adr-1.md")]
        assert batch.existing == {("Adr", "ADR-1")}

    def test_relink_writes_no_edge_from_a_loser_outside_the_batch(self, temp_repo):
        spec, _files = _keyed_docs(temp_repo)
        engine = _RecordingEngine()
        engine.extracted_entries = lambda repo_id, extractor, labels: {("Adr", "ADR-1", "decisions/adr-1.md")}
        # The copy, outside the batch, names ADR-9 but its ADR-1 entry sits at the original.
        dispatch._relink_docs(engine, "demo", temp_repo, spec, {("Adr", "ADR-9")}, {"decisions/adr-1.md"})
        assert engine.calls == [("upsert_relationships", ([],))]


class TestDeferredLabelWarning:
    def test_a_repository_spelling_the_label_differently_is_named_as_such(self, monkeypatch, caplog):
        from devgraph.config.project_schema import parse_project_schema, resolve_declaration

        effective = resolve_declaration(parse_project_schema(
            "version: 1\nnode_types:\n  - label: Adr\n    key: [adr_id]\n"
            "    metadata: [{name: path}, {name: adr_id}]\n",
            Path("devgraph.schema.yaml"),
        ))
        monkeypatch.setattr(dispatch, "recorded_declarations", lambda engine: {"adr": [
            ("demo", "Adr", ("adr_id",)), ("b", "Adr", ("path",)), ("c", "ADR", ("adr_id",)),
        ]})
        engine = _RecordingEngine()

        def boom(nodes):
            raise RuntimeError("constraint violated")

        engine.upsert_nodes = boom
        with caplog.at_level(logging.WARNING, logger="devgraph.indexer.dispatch"):
            dispatch._upsert_deferred_label(engine, "demo", effective, "Adr", [{"label": "Adr"}])
        assert (
            "demo: Adr entries were not written: b declares Adr keyed differently and c spells the type 'ADR', "
            "so its constraint keeps the old key and these entries break it; align the key or rename one label "
            "(constraint violated)"
        ) in caplog.text


class TestDocsPartialWrites:
    """A docs write failing after an entry moved says what it left behind."""

    def test_sync_logs_links_left_behind_when_clearing_fails_after_the_upsert(self, temp_repo, caplog):
        from devgraph.indexer.providers import docs

        spec, files = _keyed_docs(temp_repo)
        engine = _RecordingEngine()

        def boom(*args):
            raise RuntimeError("edge delete refused")

        engine.delete_extracted_edges = boom
        selected = docs.read_selected(spec, files)
        owners = docs.keyed_owners(docs.keyed_claims(spec, selected))
        nodes, _problems = docs.build_nodes(spec, "demo", selected, owners)
        with caplog.at_level(logging.WARNING, logger="devgraph.indexer.dispatch"):
            dispatch._sync_docs(engine, "demo", spec, dispatch._DocsBatch(selected, nodes, owners, None, set()))
        assert [name for name, _args in engine.calls] == ["upsert_nodes"]  # no edges written after the failure
        assert "wrote its nodes but could not clear their old links" in caplog.text

    def test_takeover_logs_links_left_behind_when_its_edge_rebuild_fails(self, temp_repo, caplog):
        spec, _files = _keyed_docs(temp_repo)
        engine = _RecordingEngine()
        engine.extracted_nodes_at = lambda *args: {("Adr", "ADR-1")}

        def boom(*args):
            raise RuntimeError("edge delete refused")

        engine.delete_extracted_edges = boom
        with caplog.at_level(logging.WARNING, logger="devgraph.indexer.dispatch"):
            dispatch._take_over_keys(engine, "demo", temp_repo, spec, ["decisions/gone.md"])
        assert [name for name, _args in engine.calls] == ["upsert_nodes"]
        assert "moved entries to the files that now own their ids but could not rebuild their links" in caplog.text


def test_prune_skips_walked_paths_outside_the_repository(tmp_path, monkeypatch):
    # On Windows a junction can lead the walk outside the repository; pruning must not crash on those paths.
    from devgraph.indexer import walk

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    outside = tmp_path / "outside.py"
    outside.write_text("y = 2\n")
    # As a junction to `outside` would be walked: linked, so keyed by its resolved target.
    monkeypatch.setattr(walk, "_walk", lambda root, unreadable=None: iter([(repo / "a.py", "a.py", False), (outside, "j/outside.py", True)]))
    removed = []
    monkeypatch.setattr(dispatch, "remove_paths", lambda engine, repo_id, root, paths: removed.append(paths) or len(paths))

    class FakeEngine:
        def list_indexed_files(self, repo_id):
            return {"a.py", "gone.py"}

        def read_applied_schema(self, repo_id):
            return None

        def list_claim_sources(self, repo_id):
            return set()

        def delete_bare_modules(self, repo_id):
            return 0

    assert dispatch.prune_stale_files(FakeEngine(), "r", repo) == 1
    assert removed == [{repo / "gone.py"}]


def test_name_refs_are_compact():
    """A Module's name_refs hold each by-name edge once, as a short string, in
    a fixed order: no keys, no repo id, far smaller than the edge dicts. A
    call resolved to several candidate files is one entry listing them."""
    import json
    import random

    from devgraph.indexer.common import name_ref_properties
    from devgraph.indexer.python.extractor import extract_python_file

    # A fixed module: 40 functions, each with a bare-name call, a call
    # resolved through an import, a same-file call and a builtin's method.
    source = "import os\nfrom pkg import util\n\n\nclass Child(Base):\n    pass\n\n" + "".join(
        f"\ndef f{i}():\n    obj.helper{i % 7}()\n    util.run{i % 3}()\n    f{(i + 1) % 40}()\n"
        f"    return os.path.join('a')\n\n"
        for i in range(40)
    )
    repo_id = "zz-compact-repo"
    rels = [r.to_dict() for r in extract_python_file(source, "pkg/fixture.py", repo_id).relationships]
    props = name_ref_properties(rels)
    refs = props["name_refs"]
    # One entry per bare-name call and one per resolved call (the same-file
    # calls are not by-name, and `join` on an untyped value links nothing),
    # plus the 12 import candidates and the EXTENDS.
    assert len(refs) == 93 and refs == sorted(set(refs))
    assert not any("{" in entry or repo_id in entry for entry in refs)
    # The stored size: each string's UTF-8 bytes (JSON would escape every separator as \u001f).
    stored = sum(len(entry.encode()) for values in props.values() for entry in values)
    assert stored < 20_000 and stored * 5 < len(json.dumps(rels))
    shuffled = list(rels)
    random.Random(7).shuffle(shuffled)
    assert name_ref_properties(shuffled) == props


def _rel(to_name, to_file=None, origin="app.py", **properties):
    return {
        "from_label": "Function", "from_name": "main", "from_file": "app.py", "rel_type": "CALLS",
        "to_label": "Function", "to_name": to_name, "repo_id": "r", "to_file": to_file, "origin": origin,
        "properties": properties or None,
    }


def test_name_refs_record_cross_file_pins_once_per_target_name():
    from devgraph.indexer.common import name_ref_properties, parse_name_ref

    refs = name_ref_properties([
        _rel("f", "pkg/__init__.py", confidence="resolved", caller_class="C"),
        _rel("f", "pkg/", confidence="package", caller_class="C"),
        _rel("f", "pkg.py", confidence="resolved", caller_class="C"),
        _rel("local", "app.py", confidence="resolved"),  # the writer's own file: nothing can add it
        _rel("stub", ""),  # the file-less node, from a pinned source
        _rel("g", confidence="name"),
    ])["name_refs"]
    parsed = sorted(parse_name_ref(entry) for entry in refs)
    assert parsed == [
        # Each pin's confidence is its kind's, not one value for the entry.
        (["CALLS", "Function", "main", "app.py", "Function", "f", "C"], ["pkg.py", "pkg/", "pkg/__init__.py"],
         "pin"),
        (["CALLS", "Function", "main", "app.py", "Function", "g", ""], None, "name"),
    ]


def test_name_refs_keep_a_fileless_pin_apart_from_unpinned():
    from devgraph.indexer.common import name_ref_properties, parse_name_ref

    unpinned_source = {**_rel("Display", ""), "from_label": "Class", "from_name": "Foo", "from_file": None}
    (entry,) = name_ref_properties([unpinned_source])["name_refs"]
    assert parse_name_ref(entry) == (["CALLS", "Class", "Foo", "", "Function", "Display", ""], [""], "")


def test_a_legacy_name_ref_entry_parses_as_unpinned():
    from devgraph.indexer.common import NAME_REF_SEP, parse_name_ref

    legacy = NAME_REF_SEP.join(["CALLS", "Function", "main", "a.py", "Function", "helper", "Svc"])
    assert parse_name_ref(legacy) == (["CALLS", "Function", "main", "a.py", "Function", "helper", "Svc"], None, "")


def test_a_directory_pin_is_tested_before_the_fileless_pin():
    from devgraph.graph.engine import _end_match, _group_rels_by_triple, _pin

    assert [_pin(f) for f in (None, "", "pkg/", "pkg/a.py")] == ["name", "fileless", "prefix", "file"]
    groups = _group_rels_by_triple([_rel("f", "pkg/a.py"), {**_rel("f", "pkg/"), "exclude": ["pkg/a.py"]}])
    assert {key[4] for key in groups} == {"file", "prefix"}
    (prefix_row,) = groups[("Function", "CALLS", "Function", "file", "prefix")]
    assert prefix_row["targets"] == [{
        "to_name": "f", "to_file": "pkg/", "pin": {"k": "prefix", "v": "pkg/", "ns": ""},
        "ex_files": ["pkg/a.py"], "ex_pins": [],
    }]
    match = _end_match("b", "Function", "to", "prefix", "t")
    assert "USING INDEX SEEK b:Function(repo_id, name)" in match
    assert "WHEN 'prefix' THEN b.file STARTS WITH t.pin.v" in match
    assert "NOT b.file IN t.ex_files" in match and "NOT any(x IN t.ex_pins WHERE" in match


def test_edges_out_of_one_source_share_a_row():
    from devgraph.graph.engine import _group_rels_by_triple

    rels = [_rel(name, f"lib/{name}.py", confidence="resolved") for name in ("f", "g")]
    rels += [_rel("h", "lib/h.py", confidence="package"), {**_rel("f", "lib/f.py"), "from_name": "other"}]
    (rows,) = _group_rels_by_triple(rels).values()
    assert [(row["from_name"], row["properties"], [t["to_name"] for t in row["targets"]]) for row in rows] == [
        ("main", {"confidence": "resolved"}, ["f", "g"]),
        ("main", {"confidence": "package"}, ["h"]),
        ("other", {}, ["f"]),
    ]


def test_a_source_end_pinned_to_a_package_directory_is_refused():
    import pytest as _pytest

    from devgraph.graph.engine import _group_rels_by_triple

    with _pytest.raises(ValueError, match="source end"):
        _group_rels_by_triple([{**_rel("f"), "from_file": "pkg/"}])


@pytest.mark.parametrize("module", ["devgraph.indexer.python.extractor", "devgraph.indexer.common"])
def test_an_indexer_module_imports_first_in_a_fresh_interpreter(module):
    import subprocess
    import sys

    done = subprocess.run([sys.executable, "-c", f"import {module}"], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("deleted", [False, True])
def test_a_save_while_the_indexes_build_fails_its_batch_instead_of_skipping_the_file(
    engine, temp_repo, monkeypatch, deleted
):
    """A hinted query fails outright while its index is POPULATING. If that
    outlasts the wait, the batch must fail (so it is retried and never
    stamped), not log and skip the file as a per-file failure would."""
    import neo4j

    import devgraph.graph.engine as engine_module

    repo_id = "_smoketest_dispatch_index_building" + ("_rm" if deleted else "")
    source = temp_repo / "a.py"
    source.write_text("def f():\n    return 1\n")
    try:
        full_scan(engine, repo_id, temp_repo)
        source.write_text("def g():\n    return 1\n")
        if deleted:
            source.unlink()
        monkeypatch.setattr(engine_module, "INDEX_WAIT_S", 0.0)
        for cls in (neo4j.ManagedTransaction, neo4j.Session):
            def run(self, query, *args, _original=cls.run, **kwargs):
                if "USING INDEX SEEK" in query:
                    raise neo4j.exceptions.ClientError("Failed to fulfil the hints of the query.")
                return _original(self, query, *args, **kwargs)

            monkeypatch.setattr(cls, "run", run)
        with pytest.raises(engine_module.IndexesNotReady):
            if deleted:
                remove_paths(engine, repo_id, temp_repo, {source})
            else:
                index_paths(engine, repo_id, temp_repo, {source})
    finally:
        monkeypatch.undo()
        engine.delete_repository(repo_id)
