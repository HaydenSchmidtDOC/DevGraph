"""Both agents announce refreshed insights on their dashboard event stream."""

from devgraph.agent.headless import HeadlessAgent
from devgraph.agent.tray import TrayApp


class Events:
    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)


def test_headless_agent_publishes_insights_refreshed():
    agent = HeadlessAgent.__new__(HeadlessAgent)
    agent._events = Events()
    agent._on_insights_refreshed("demo")
    assert agent._events.published == [{"type": "insights_refreshed", "repo_id": "demo"}]


def test_tray_app_publishes_insights_refreshed():
    app = TrayApp.__new__(TrayApp)
    app._events = Events()
    app._on_insights_refreshed("demo")
    assert app._events.published == [{"type": "insights_refreshed", "repo_id": "demo"}]
