import asyncio

import pytest

from smartwatt_server.bus import Bus


@pytest.mark.asyncio
async def test_subscriber_receives_a_publish():
    bus = Bus()
    bus.attach_loop(asyncio.get_running_loop())
    sub = await bus.subscribe()
    bus.publish_threadsafe("telemetry", {"p": 1.0})
    await asyncio.sleep(0)
    message = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert message == {"kind": "telemetry", "payload": {"p": 1.0}}


@pytest.mark.asyncio
async def test_every_subscriber_receives_the_same_message():
    bus = Bus()
    bus.attach_loop(asyncio.get_running_loop())
    a, b = await bus.subscribe(), await bus.subscribe()
    bus.publish_threadsafe("event", {"edge": "on"})
    await asyncio.sleep(0)
    assert (await a.get())["payload"] == (await b.get())["payload"]


@pytest.mark.asyncio
async def test_slow_client_is_marked_dropped_not_backpressuring():
    """Ingest is never back-pressured by a browser.

    Half of the spec's "bounded queue, then disconnect": this is the
    queue half. test_api's test_slow_websocket_client_is_disconnected
    covers the other, that a subscriber in this state gets closed.
    """
    bus = Bus(max_queue=3)
    bus.attach_loop(asyncio.get_running_loop())
    sub = await bus.subscribe()
    for n in range(20):
        bus.publish_threadsafe("telemetry", {"seq": n})
    await asyncio.sleep(0)
    assert sub.dropped is True


@pytest.mark.asyncio
async def test_publishing_with_no_subscribers_is_harmless():
    bus = Bus()
    bus.attach_loop(asyncio.get_running_loop())
    bus.publish_threadsafe("telemetry", {"p": 1.0})
    await asyncio.sleep(0)
    late = await bus.subscribe()
    assert late.queue.empty()


@pytest.mark.asyncio
async def test_unsubscribe_removes_the_client():
    bus = Bus()
    bus.attach_loop(asyncio.get_running_loop())
    sub = await bus.subscribe()
    assert bus.client_count == 1
    bus.unsubscribe(sub)
    assert bus.client_count == 0


@pytest.mark.asyncio
async def test_publish_before_loop_attached_is_dropped_silently():
    bus = Bus()
    sub = await bus.subscribe()
    bus.publish_threadsafe("telemetry", {"p": 1.0})
    assert sub.queue.empty()


@pytest.mark.asyncio
async def test_publish_from_another_thread():
    import threading

    bus = Bus()
    bus.attach_loop(asyncio.get_running_loop())
    sub = await bus.subscribe()
    threading.Thread(
        target=lambda: bus.publish_threadsafe("telemetry", {"from": "thread"})
    ).start()
    message = await asyncio.wait_for(sub.get(), timeout=2.0)
    assert message["payload"] == {"from": "thread"}
