"""SchemaRescanScheduler logs one line, not a traceback, for a repository whose
folder is missing or looks unmounted."""

import logging

from devgraph.agent import schema_rescan
from devgraph.agent.schema_rescan import SchemaRescanScheduler
from devgraph.indexer.walk import RepoRootUnavailable
from tests.agent.test_schema_rescan import Clock, Registry, Repo, setup


def test_a_missing_repository_folder_is_one_line_not_a_traceback(monkeypatch, tmp_path, caplog):
    setup(monkeypatch, tmp_path)
    gone = tmp_path / "gone"

    def refuse(engine, repo_id, root, docs_path=None, mentions_enabled=False):
        raise RepoRootUnavailable(gone, "not found")

    monkeypatch.setattr(schema_rescan, "full_scan", refuse)
    clock = Clock()
    sched = SchemaRescanScheduler(None, Registry([Repo("r", gone)]), clock=clock)
    with caplog.at_level(logging.DEBUG, logger="devgraph.agent.schema_rescan"):
        sched.run_once()
        clock.now += 1000
        sched.run_once()
        clock.now += 1000
        sched.run_once()
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "repository folder not found" in warnings[0].getMessage()
    assert all(r.exc_info is None for r in caplog.records)
