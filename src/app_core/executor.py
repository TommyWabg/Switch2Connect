"""Serialized core work and monotonic, completion-relative timers."""
from concurrent.futures import Future
from functools import wraps
import heapq
import itertools
import logging
import math
import threading
import time
from typing import Protocol

logger = logging.getLogger(__name__)


class Cancellable(Protocol):
    def cancel(self) -> None: ...


class _Timer:
    def __init__(self, condition):
        self._condition = condition
        self.cancelled = False

    def cancel(self):
        # Cancellation prevents queued/future runs; it cannot interrupt a run
        # which has already been claimed by the executor.
        with self._condition:
            self.cancelled = True
            self._condition.notify_all()


def _delay(seconds):
    seconds = float(seconds)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("delay must be finite and nonnegative")
    return seconds


class CoreExecutor:
    """One daemon thread; shutdown cancels pending jobs and waits for active work.

    Submissions after shutdown raise RuntimeError. A timeout bounds the join;
    running Python callbacks cannot be interrupted. Calling shutdown from the
    core thread never joins itself.
    """

    def __init__(self):
        self._condition = threading.Condition()
        self._jobs = []
        self._sequence = itertools.count()
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name="app-core", daemon=True)
        self._thread.start()

    def _enqueue(self, delay, fn, args, kwargs, future=None, timer=None, interval=None):
        with self._condition:
            if self._stopped:
                raise RuntimeError("CoreExecutor has shut down")
            heapq.heappush(self._jobs, (
                time.monotonic() + delay, next(self._sequence),
                fn, args, kwargs, future, timer, interval,
            ))
            self._condition.notify_all()

    def submit(self, fn, *a, **kw) -> Future:
        future = Future()
        self._enqueue(0, fn, a, kw, future=future)
        return future

    def call_later(self, seconds, fn, *a, **kw) -> Cancellable:
        timer = _Timer(self._condition)
        self._enqueue(_delay(seconds), fn, a, kw, timer=timer)
        return timer

    def call_every(self, seconds, fn, *, initial_delay=None) -> Cancellable:
        seconds = _delay(seconds)
        if seconds == 0:
            raise ValueError("repeat interval must be positive")
        delay = seconds if initial_delay is None else _delay(initial_delay)
        timer = _Timer(self._condition)
        self._enqueue(delay, fn, (), {}, timer=timer, interval=seconds)
        return timer

    def is_core_thread(self) -> bool:
        return threading.current_thread() is self._thread

    def shutdown(self, wait=True, timeout=None):
        with self._condition:
            self._stopped = True
            jobs, self._jobs = self._jobs, []
            for job in jobs:
                if job[6] is not None:
                    job[6].cancelled = True
            self._condition.notify_all()
        # Future callbacks are user code: never run them under the queue lock.
        for job in jobs:
            if job[5] is not None:
                job[5].cancel()
        if wait and not self.is_core_thread():
            self._thread.join(timeout)

    def _run(self):
        while True:
            with self._condition:
                while True:
                    if self._stopped:
                        return
                    if not self._jobs:
                        self._condition.wait()
                        continue
                    job = self._jobs[0]
                    if job[6] is not None and job[6].cancelled:
                        heapq.heappop(self._jobs)
                        continue
                    remaining = job[0] - time.monotonic()
                    if remaining > 0:
                        self._condition.wait(remaining)
                        continue
                    heapq.heappop(self._jobs)
                    break
            _, _, fn, args, kwargs, future, timer, interval = job
            if future is not None and not future.set_running_or_notify_cancel():
                continue
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                logger.exception("Core job failed")
                if future is not None:
                    future.set_exception(exc)
            else:
                if future is not None:
                    future.set_result(result)
            if interval is not None:
                with self._condition:
                    if not self._stopped and not timer.cancelled:
                        heapq.heappush(self._jobs, (
                            time.monotonic() + interval, next(self._sequence),
                            fn, args, kwargs, None, timer, interval,
                        ))
                        self._condition.notify_all()


def on_core(fn):
    """Run a service method inline on its core thread, otherwise return a Future."""
    @wraps(fn)
    def wrapper(self, *a, **kw):
        if self._executor.is_core_thread():
            return fn(self, *a, **kw)
        return self._executor.submit(fn, self, *a, **kw)
    return wrapper


def run_flow(executor, generator, transform=lambda value: value):
    """Run synchronous stages inline, suspend only on unfinished Futures.

    The returned Future is the completion of the entire flow, not its worker.
    No worker writes CONFIG, VC state or the cached snapshot.
    """
    completion = Future()

    def advance(value=None, error=None):
        try:
            while True:
                item = generator.throw(error) if error else generator.send(value)
                error = None
                if isinstance(item, Future):
                    if not item.done():
                        def resume(f):
                            try:
                                result, exc = f.result(), None
                            except BaseException as e:
                                result, exc = None, e
                            try:
                                job = executor.submit(advance, result, exc)
                                def submitted_done(j):
                                    if j.cancelled() and not completion.done():
                                        completion.cancel()
                                job.add_done_callback(submitted_done)
                            except RuntimeError as e:
                                if not completion.done():
                                    completion.set_exception(e)
                        item.add_done_callback(resume)
                        return
                    try:
                        value = item.result()
                    except BaseException as exc:
                        value, error = None, exc
                else:
                    value = item
        except StopIteration as stop:
            completion.set_result(transform(stop.value))
        except BaseException as exc:
            completion.set_exception(exc)
    advance()
    return completion.result() if completion.done() else completion
