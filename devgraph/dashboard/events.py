"""In-process pub/sub for pushing live graph-change events to SSE clients.

One `asyncio.Queue` per connected browser tab. `publish()` is the only
method called from outside the dashboard's own event loop -- the tray's
health-check thread and the watcher's debounce-timer threads call it
directly (see `agent/tray.py`'s `_on_changes`/`_check_registry_changes`),
never the dashboard loop thread. `asyncio.Queue` is not thread-safe, so
`publish()` never touches a queue directly; it schedules the actual
`put_nowait` onto the dashboard loop via `call_soon_threadsafe` (a plain
callback, not a coroutine -- `run_coroutine_threadsafe` would work too but
is unnecessary machinery for a call that never needs to await anything).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Small and bounded: a client is expected to just refetch on the next event
# rather than replay a long backlog, so there is no reason to let a stalled
# browser tab's queue grow unbounded and no reason to keep more than a
# handful of not-yet-delivered events around.
_QUEUE_MAXSIZE = 32


class EventBroadcaster:
    """Holds subscriber queues; safe to publish into from any thread."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Register the dashboard's event loop. Called once, from the
        dashboard thread, right after it creates its loop -- before that,
        `publish()` is a no-op (nothing is subscribed yet either)."""
        with self._lock:
            self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        """Register a new client queue. Must be called from the dashboard
        loop thread (i.e. from inside an `async def` request handler)."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        with self._lock:
            self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._queues.discard(queue)

    def publish(self, event: dict[str, Any]) -> None:
        """Fan an event out to every connected client. Safe to call from
        any thread; a no-op before the dashboard loop has started (e.g.
        dashboard_enabled=False, or the dashboard hasn't finished booting
        yet) since there is nothing to deliver to."""
        with self._lock:
            loop = self._loop
            queues = list(self._queues)
        if loop is None or not queues:
            return
        for queue in queues:
            loop.call_soon_threadsafe(_put_dropping_oldest, queue, event)


def _put_dropping_oldest(queue: asyncio.Queue, event: dict[str, Any]) -> None:
    """Runs on the loop thread only (scheduled via call_soon_threadsafe).

    A full queue means a slow/stalled client -- drop its oldest queued
    event rather than blocking the publisher or growing unbounded.
    """
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        logger.debug("dropped event for a still-full subscriber queue")
