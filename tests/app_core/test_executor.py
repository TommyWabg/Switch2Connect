from concurrent.futures import Future
import threading
import time

import pytest

from app_core.executor import CoreExecutor, on_core


@pytest.fixture
def executor():
    value = CoreExecutor()
    yield value
    value.shutdown(timeout=3)
    assert not value._thread.is_alive()


def test_fifo_single_daemon_thread_and_future(executor):
    gate = threading.Event()
    started = threading.Event()
    seen = []

    def block():
        started.set()
        assert gate.wait(3)

    blocked = executor.submit(block)
    assert started.wait(3)
    futures = [executor.submit(lambda i=i: seen.append(i)) for i in range(30)]
    try:
        assert not any(f.done() for f in futures)
    finally:
        gate.set()
    blocked.result(3)
    for future in futures:
        future.result(3)
    assert seen == list(range(30))
    assert executor._thread.name == "app-core"
    assert executor._thread.daemon
    assert not executor.is_core_thread()
    assert executor.submit(executor.is_core_thread).result(3)
    assert executor.submit(lambda a, b: a + b, 2, b=3).result(3) == 5


def test_later_timing_and_waking_an_earlier_timer(executor):
    done = threading.Event()
    observed = []
    far = executor.call_later(30, lambda: observed.append("far"))
    start = time.monotonic()

    def callback(value, *, suffix):
        observed.append((value + suffix, time.monotonic() - start))
        done.set()

    executor.call_later(0.06, callback, "a", suffix="b")
    assert done.wait(3)
    far.cancel()
    assert observed[0][0] == "ab"
    assert 0.045 <= observed[0][1] < 3


def test_every_completion_relative_and_survives_exception(executor, caplog):
    done = threading.Event()
    starts, finishes = [], []

    def callback():
        starts.append(time.monotonic())
        time.sleep(0.025)
        finishes.append(time.monotonic())
        if len(starts) == 1:
            raise ValueError("first periodic run")
        if len(starts) == 3:
            done.set()

    timer = executor.call_every(0.03, callback, initial_delay=0)
    assert done.wait(3)
    timer.cancel()
    executor.submit(lambda: None).result(3)
    assert len(starts) == 3
    assert all(starts[i + 1] - finishes[i] >= 0.025 for i in range(2))
    assert "first periodic run" in caplog.text


def test_cancel_timer_and_pending_future(executor):
    gate, started = threading.Event(), threading.Event()
    seen = []

    def block():
        started.set()
        assert gate.wait(3)

    executor.submit(block)
    assert started.wait(3)
    try:
        timer = executor.call_later(0, seen.append, "later")
        periodic = executor.call_every(0.01, lambda: seen.append("repeat"), initial_delay=0)
        future = executor.submit(seen.append, "future")
        timer.cancel()
        timer.cancel()
        periodic.cancel()
        assert future.cancel()
    finally:
        gate.set()
    executor.submit(lambda: None).result(3)
    assert seen == []


def test_every_can_cancel_itself(executor):
    done = threading.Event()
    seen = []
    # Create the handle on core so even an immediate timer cannot race its binding.
    def install():
        def callback():
            seen.append(1)
            timer.cancel()
            done.set()
        timer = executor.call_every(0.01, callback, initial_delay=0)
    executor.submit(install).result(3)
    assert done.wait(3)
    settled = threading.Event()
    executor.call_later(0.04, settled.set)
    assert settled.wait(3)
    assert seen == [1]


def test_submit_exception_is_returned_and_thread_survives(executor, caplog):
    def fail():
        raise ValueError("submit failed")
    with pytest.raises(ValueError, match="submit failed"):
        executor.submit(fail).result(3)
    assert executor.submit(lambda: 7).result(3) == 7
    assert "submit failed" in caplog.text


def test_on_core_inline_and_submitted(executor):
    class Service:
        _executor = executor

        @on_core
        def method(self, value, *, extra=0):
            assert self._executor.is_core_thread()
            return value + extra

    service = Service()
    future = service.method(4, extra=2)
    assert isinstance(future, Future)
    assert future.result(3) == 6
    assert executor.submit(lambda: service.method(4, extra=3)).result(3) == 7


def test_shutdown_cancels_pending_work_and_timeout(executor):
    gate, started = threading.Event(), threading.Event()
    seen = []

    def block():
        started.set()
        assert gate.wait(3)

    running = executor.submit(block)
    assert started.wait(3)
    future = executor.submit(seen.append, 1)
    timer = executor.call_later(30, seen.append, 2)
    executor.call_every(0.01, lambda: seen.append(3))
    try:
        start = time.monotonic()
        executor.shutdown(timeout=0.02)
        assert time.monotonic() - start < 1
        assert executor._thread.is_alive()
        assert future.cancelled()
        assert timer.cancelled
        for submit in (lambda: executor.submit(lambda: None),
                       lambda: executor.call_later(0, lambda: None),
                       lambda: executor.call_every(1, lambda: None)):
            with pytest.raises(RuntimeError):
                submit()
    finally:
        gate.set()
    running.result(3)
    executor.shutdown()
    executor.shutdown()
    assert seen == []


def test_shutdown_from_core_thread(executor):
    executor.submit(executor.shutdown).result(3)
    executor.shutdown()
    assert not executor._thread.is_alive()


@pytest.mark.parametrize("delay", [-1, float("inf"), float("nan")])
def test_invalid_delay(executor, delay):
    with pytest.raises(ValueError):
        executor.call_later(delay, lambda: None)
    with pytest.raises(ValueError):
        executor.call_every(delay, lambda: None)


def test_zero_repeat_interval_rejected(executor):
    with pytest.raises(ValueError):
        executor.call_every(0, lambda: None)
