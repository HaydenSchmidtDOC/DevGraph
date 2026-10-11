"""The agent writes its first heartbeat before its first Neo4j check.

`devgraph tray start` waits for that heartbeat; with Neo4j slow to answer
(driver retries), it used to report a timeout while the agent was up.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.parametrize("module, cls", [("devgraph.agent.headless", "HeadlessAgent"), ("devgraph.agent.tray", "TrayApp")])
def test_the_first_heartbeat_precedes_the_first_neo4j_check(tmp_path, module, cls):
    settings = SimpleNamespace(
        registry_db_path=tmp_path / "registry.sqlite3", neo4j_uri="bolt://x", neo4j_user="u", neo4j_password="p",
        health_check_interval_s=30, dashboard_enabled=False,
    )
    with patch(f"{module}.get_settings", return_value=settings), \
         patch(f"{module}.RepoRegistry"), patch(f"{module}.GraphEngine"), patch(f"{module}.WatcherManager"):
        import importlib

        agent = getattr(importlib.import_module(module), cls)()
    agent._watcher.get_repo_issues = MagicMock(return_value=[])
    heartbeat = tmp_path / "tray_heartbeat.txt"
    seen = {}

    def check():
        seen["beat"] = heartbeat.exists()
        agent._stop_event.set()
        raise OSError("Neo4j is still starting")

    agent._engine.verify_connectivity = check
    agent._health_check_loop()
    assert seen == {"beat": True}
