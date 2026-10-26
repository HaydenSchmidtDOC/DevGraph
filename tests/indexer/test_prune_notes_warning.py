"""A scan that removes docs notes outside the docs path says so at WARNING,
naming the count and `--docs-path` (audit 10 item 2 review)."""

import logging

from devgraph.indexer import dispatch


class _Engine:
    def __init__(self, misplaced):
        self.misplaced = misplaced

    def list_indexed_files(self, repo_id):
        return {"a.py"}

    def read_applied_schema(self, repo_id):
        return None

    def list_claim_sources(self, repo_id):
        return set()

    def delete_bare_modules(self, repo_id):
        return 0

    def delete_bare_class_stubs(self, repo_id):
        return 0

    def delete_docs_notes_outside(self, repo_id, folder):
        return self.misplaced

    def delete_documents(self, repo_id):
        return 0


def _prune(tmp_path, caplog, docs_path, misplaced):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "a.py").write_text("x = 1\n")
    with caplog.at_level(logging.INFO, logger="devgraph.indexer.dispatch"):
        dispatch.prune_stale_files(_Engine(misplaced), "r", root, docs_path=docs_path)
    return [r for r in caplog.records if "docs notes" in r.getMessage()]


def test_removed_notes_are_a_warning_naming_the_count_and_docs_path(tmp_path, caplog):
    [record] = _prune(tmp_path, caplog, None, 2)
    assert record.levelno == logging.WARNING
    assert "removed 2 docs notes" in record.getMessage() and "--docs-path" in record.getMessage()


def test_notes_outside_a_set_docs_path_are_a_warning(tmp_path, caplog):
    [record] = _prune(tmp_path, caplog, "docs", 1)
    assert record.levelno == logging.WARNING and "(docs)" in record.getMessage()


def test_nothing_removed_says_nothing(tmp_path, caplog):
    assert _prune(tmp_path, caplog, "docs", 0) == []
