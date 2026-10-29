from concurrent.futures import ThreadPoolExecutor
import threading

from app_core.bus import EventBus


def test_order_payload_and_publisher_thread():
    bus = EventBus()
    seen = []
    for i in range(3):
        bus.subscribe("topic", lambda payload, i=i: seen.append((i, payload, threading.get_ident())))
    bus.publish("other")
    bus.publish("topic", "value")
    assert seen == [(i, "value", threading.get_ident()) for i in range(3)]


def test_subscription_changes_during_publish_use_snapshot():
    bus = EventBus()
    seen = []

    def first(payload):
        seen.append("first")
        unsubscribe()
        unsubscribe()
        bus.subscribe("t", lambda p: seen.append("new"))

    bus.subscribe("t", first)
    unsubscribe = bus.subscribe("t", lambda p: seen.append("second"))
    bus.publish("t")
    assert seen == ["first", "second"]
    bus.publish("t")
    assert seen == ["first", "second", "first", "new"]


def test_exception_isolation_and_reentrant_publish(caplog):
    bus = EventBus()
    seen = []

    def bad(payload):
        raise ValueError("broken subscriber")

    bus.subscribe("t", bad)
    bus.subscribe("t", lambda p: bus.publish("nested", p))
    bus.subscribe("nested", seen.append)
    bus.publish("t", 42)
    assert seen == [42]
    assert "broken subscriber" in caplog.text


def test_multi_thread_publish_and_subscription_churn():
    bus = EventBus()
    seen = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def receive(payload):
        with lock:
            seen.append((payload, threading.get_ident()))

    bus.subscribe("t", receive)

    def publish(worker):
        ident = threading.get_ident()
        barrier.wait(timeout=3)
        for i in range(100):
            stop = bus.subscribe("t", lambda p: None)
            bus.publish("t", (worker, i))
            stop()
        return worker, ident

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = dict(pool.map(publish, range(4)))
    assert len(seen) == 400
    assert len({payload for payload, _ in seen}) == 400
    assert all(ident == ids[payload[0]] for payload, ident in seen)
