"""Unit tests for EventBroadcaster, in isolation -- no live server needed."""

import asyncio
import threading

import pytest

from devgraph.dashboard.events import EventBroadcaster, _QUEUE_MAXSIZE


@pytest.mark.asyncio
async def test_publish_reaches_subscribed_queue():
    broadcaster = EventBroadcaster()
    broadcaster.bind_loop(asyncio.get_running_loop())
    queue = broadcaster.subscribe()

    broadcaster.publish({"type": "reindexed", "repo_id": "r1"})
    # publish() schedules via call_soon_threadsafe; yield once so it runs.
    await asyncio.sleep(0)

    event = await asyncio.wait_for(queue.get(), timeout=1)
    assert event == {"type": "reindexed", "repo_id": "r1"}


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery():
    broadcaster = EventBroadcaster()
    broadcaster.bind_loop(asyncio.get_running_loop())
    queue = broadcaster.subscribe()
    broadcaster.unsubscribe(queue)

    broadcaster.publish({"type": "registry_changed"})
    await asyncio.sleep(0)

    assert queue.empty()


@pytest.mark.asyncio
async def test_publish_before_loop_bound_is_a_noop():
    broadcaster = EventBroadcaster()
    # No bind_loop() call -- simulates publishing before the dashboard
    # thread has finished starting up, or dashboard_enabled=False.
    broadcaster.publish({"type": "reindexed"})  # must not raise


@pytest.mark.asyncio
async def test_slow_subscriber_queue_does_not_grow_unbounded():
    broadcaster = EventBroadcaster()
    broadcaster.bind_loop(asyncio.get_running_loop())
    queue = broadcaster.subscribe()  # never read from

    for i in range(_QUEUE_MAXSIZE + 20):
        broadcaster.publish({"type": "reindexed", "n": i})
    await asyncio.sleep(0)

    assert queue.qsize() <= _QUEUE_MAXSIZE


@pytest.mark.asyncio
async def test_publish_from_other_thread_is_delivered():
    """publish() is called from the health-check/watcher threads in
    production, never the dashboard loop thread itself -- exercise that."""
    broadcaster = EventBroadcaster()
    loop = asyncio.get_running_loop()
    broadcaster.bind_loop(loop)
    queue = broadcaster.subscribe()

    def publish_from_thread():
        broadcaster.publish({"type": "reindexed", "repo_id": "r1"})

    thread = threading.Thread(target=publish_from_thread)
    thread.start()
    thread.join()

    event = await asyncio.wait_for(queue.get(), timeout=1)
    assert event == {"type": "reindexed", "repo_id": "r1"}
