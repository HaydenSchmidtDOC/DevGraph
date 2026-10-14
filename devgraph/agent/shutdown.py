"""The agent's ordered, bounded shutdown, shared by the tray and headless agents."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: Total time shutdown waits for in-flight work before giving up on it.
SHUTDOWN_WAIT_S = 5.0


def shutdown(
    engine: Any,
    watcher: Any,
    schema_rescans: Any,
    insights: Any,
    *,
    dashboard_server: Any = None,
    dashboard_thread: threading.Thread | None = None,
    events: Any = None,
    wait_s: float = SHUTDOWN_WAIT_S,
) -> None:
    """Stop every graph engine user, then close the engine, in `wait_s` total.

    The dashboard is told to exit and its event streams are ended; the
    schedulers are stopped before the watcher, so a pass waiting on a watcher
    batch's lock finds itself stopped when it gets it. Each stop waits for its
    running work against one shared deadline, and the engine close gets what
    remains: it refuses new sessions and waits for open ones, abandoning
    (with one warning) a query still running then rather than closing the
    driver under it. A step that fails is logged and the rest still run.
    """
    deadline = time.monotonic() + wait_s

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    def step(what: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except Exception:
            logger.warning("shutdown: %s failed", what, exc_info=True)

    if dashboard_server is not None:
        dashboard_server.should_exit = True
    if events is not None:
        step("ending the dashboard event streams", events.close)
    step("stopping the schema rescans", lambda: schema_rescans.stop(timeout=remaining()))
    step("stopping the insights scheduler", lambda: insights.stop(timeout=remaining()))
    step("stopping the watcher", lambda: watcher.stop(timeout=remaining()))
    if dashboard_thread is not None:
        dashboard_thread.join(timeout=remaining())
    engine.close(timeout=remaining())
