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
