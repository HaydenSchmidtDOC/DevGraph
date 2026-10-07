"""HeadlessAgent wires RepoSync and the watcher to each other, like the tray."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch


def test_headless_agent_wires_repo_sync_and_the_watcher():
    with patch("devgraph.agent.headless.get_settings", return_value=MagicMock()), \
         patch("devgraph.agent.headless.RepoRegistry"), \
         patch("devgraph.agent.headless.GraphEngine"), \
         patch("devgraph.agent.headless.WatcherManager") as watcher_cls:
        from devgraph.agent.headless import HeadlessAgent

        agent = HeadlessAgent()
        kwargs = watcher_cls.call_args.kwargs
        assert kwargs["on_changes"] == agent._sync.on_changes
        assert kwargs["on_catch_up"] == agent._sync.on_catch_up
        since = datetime(2026, 10, 7, tzinfo=timezone.utc)
        agent._sync._request_catch_up("r", since, 30.0)
        agent._watcher.request_catch_up.assert_called_once_with("r", since, 30.0)
        agent.stop()
        assert agent._sync.stopping is True


def _agent():
    with patch("devgraph.agent.headless.get_settings", return_value=MagicMock()), \
         patch("devgraph.agent.headless.RepoRegistry"), \
         patch("devgraph.agent.headless.GraphEngine"), \
         patch("devgraph.agent.headless.WatcherManager"):
        from devgraph.agent.headless import HeadlessAgent

        return HeadlessAgent()


def _sync_logs(caplog, result, initial=None):
    agent = _agent()

    def sync(engine, registry, repo_id, on_initial=None):
        if initial is not None:
            on_initial(initial)
        return result

    with patch("devgraph.agent.headless.sync_git_history", side_effect=sync), \
         caplog.at_level("DEBUG", logger="devgraph.agent.headless"):
        agent._on_git_state_changed("r")
    return [(rec.levelname, rec.getMessage()) for rec in caplog.records if "git history" in rec.getMessage()]


def test_a_sync_with_head_unmoved_logs_only_at_debug(caplog):
    logs = _sync_logs(caplog, {"mode": "noop", "commits_indexed": 0, "commits_deleted": 0})
    assert logs and all(level == "DEBUG" for level, _message in logs)


def test_a_first_sync_says_live_updates_wait_for_it(caplog):
    logs = _sync_logs(caplog, {"mode": "initial", "commits_indexed": 412, "commits_deleted": 0}, initial=412)
    assert (
        "INFO",
        "Reading the git history of r for the first time (412 commits); live updates resume when it finishes",
    ) in logs
