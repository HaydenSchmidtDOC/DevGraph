"""One file that can't be indexed costs a one-line warning, not a traceback."""

import logging

import pytest

from devgraph.graph.engine import EngineClosed
from devgraph.indexer import dispatch
from devgraph.indexer.dispatch import index_paths
from tests.helpers import stub


class _QuietEngine:
    def route_mount_state(self, repo_id, names=None):
        return {}

    def list_file_nodes(self, repo_id, files):
        return set()

    def read_applied_schema(self, repo_id):
        return None

    def update_skipped_files(self, repo_id, add=None, drop=(), replace=False):
        pass


def test_a_failing_file_logs_one_line_and_the_traceback_only_at_debug(tmp_path, monkeypatch, caplog):
    (tmp_path / "locked.py").write_text("x = 1\n")

    def fail(*args):
        raise PermissionError(13, "Permission denied")

    stub(monkeypatch, dispatch, "_index_single_path", fail)
    with caplog.at_level(logging.DEBUG, logger="devgraph.indexer.dispatch"):
        index_paths(_QuietEngine(), "_unit_failures", tmp_path, {tmp_path / "locked.py"})

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].exc_info is None
    assert "locked.py" in warnings[0].getMessage() and "Permission denied" in warnings[0].getMessage()
    debug = [r for r in caplog.records if r.levelno == logging.DEBUG and r.exc_info]
    assert len(debug) == 1


def test_engine_closed_is_still_raised(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n")

    def closed(*args):
        raise EngineClosed()

    stub(monkeypatch, dispatch, "_index_single_path", closed)
    with pytest.raises(EngineClosed):
        index_paths(_QuietEngine(), "_unit_failures", tmp_path, {tmp_path / "a.py"})
