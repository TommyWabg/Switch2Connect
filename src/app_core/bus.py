"""Synchronous, thread-safe publication with snapshot subscription semantics."""
import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class EventBus:
    """Callbacks run in registration order on the publisher thread.

    Each publish snapshots subscriptions: changes made during a callback affect
    subsequent publications (including nested publications), not that snapshot.
    Concurrent publishers may invoke the same callback concurrently.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._topics: dict[str, dict[object, Callable[[Any], None]]] = {}

    def subscribe(self, topic: str, callback: Callable[[Any], None]) -> Callable[[], None]:
        token = object()
        with self._lock:
            self._topics.setdefault(topic, {})[token] = callback

        def unsubscribe():
            with self._lock:
                subscribers = self._topics.get(topic)
                if subscribers is not None:
                    subscribers.pop(token, None)
                    if not subscribers:
                        del self._topics[topic]

        return unsubscribe

    def publish(self, topic: str, payload: Any = None) -> None:
        with self._lock:
            callbacks = tuple(self._topics.get(topic, {}).values())
        for callback in callbacks:
            try:
                callback(payload)
            except Exception:
                logger.exception("Event callback failed for topic %s", topic)
