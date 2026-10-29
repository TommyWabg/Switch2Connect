"""Blocking core-to-UI request contracts and headless implementations."""
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
import logging
import threading
from typing import Literal, Protocol, runtime_checkable

logger = logging.getLogger(__name__)
MessageKind = Literal["info", "warning", "error"]


@runtime_checkable
class ProgressHandle(Protocol):
    def update(self, message: str | None = None, fraction: float | None = None) -> None: ...
    def close(self) -> None: ...
    @property
    def cancelled(self) -> bool: ...


@runtime_checkable
class UiPort(Protocol):
    def ask_yes_no(self, title: str, message: str, *, yes: str = "Yes", no: str = "No", danger: bool = False) -> bool: ...
    def ask_yes_no_opt_out(self, title: str, message: str, *, opt_out_label: str, yes: str = "Yes", no: str = "No") -> tuple[bool, bool]: ...
    def native_window_handle(self) -> int | None: ...
    def ask_choice(self, title: str, message: str, choices: Sequence[str], *, default: str | None = None) -> str | None: ...
    def show_message(self, title: str, message: str, *, kind: MessageKind = "info") -> None: ...
    def ask_string(self, title: str, prompt: str, *, initial: str = "") -> str | None: ...
    def ask_open_file(self, title: str, filters: Sequence[tuple[str, str]]) -> str | None: ...
    def ask_save_file(self, title: str, filters: Sequence[tuple[str, str]], *, default_name: str = "") -> str | None: ...
    def progress(self, title: str, message: str = "", *, cancellable: bool = False) -> ProgressHandle: ...


@dataclass(frozen=True, slots=True)
class UiCall:
    """One request; arguments are captured for assertions in headless tests."""
    name: str
    args: tuple
    kwargs: dict


class _Progress:
    def __init__(self, record, cancellable):
        self._record = record
        self._lock = threading.Lock()
        self._cancellable = cancellable
        self._cancelled = False
        self.closed = False
        self.message = None
        self.fraction = None

    def update(self, message=None, fraction=None):
        self._record("progress.update", (), {"message": message, "fraction": fraction})
        with self._lock:
            if message is not None:
                self.message = message
            if fraction is not None:
                self.fraction = fraction

    def close(self):
        self._record("progress.close", (), {})
        with self._lock:
            self.closed = True

    @property
    def cancelled(self):
        self._record("progress.cancelled", (), {})
        with self._lock:
            return self._cancelled

    def cancel(self):
        """Simulate the user's cancel action in a scripted test."""
        self._record("progress.cancel", (), {})
        with self._lock:
            if self._cancellable and not self.closed:
                self._cancelled = True


class NullUiPort:
    """Log all requests; questions decline/cancel and progress is inert."""

    def _record(self, name, args, kwargs):
        logger.info("UiPort.%s args=%r kwargs=%r", name, args, kwargs)

    def _answer(self, name, args, kwargs, fallback=None):
        self._record(name, args, kwargs)
        return fallback

    def ask_yes_no(self, title, message, *, yes="Yes", no="No", danger=False):
        return self._answer("ask_yes_no", (title, message), {"yes": yes, "no": no, "danger": danger}, False)

    def ask_yes_no_opt_out(self, title, message, *, opt_out_label, yes="Yes", no="No"):
        return self._answer("ask_yes_no_opt_out", (title, message),
                            {"opt_out_label": opt_out_label, "yes": yes, "no": no}, (False, False))

    def native_window_handle(self):
        self._record("native_window_handle", (), {})
        return None

    def ask_choice(self, title, message, choices, *, default=None):
        return self._answer("ask_choice", (title, message, tuple(choices)), {"default": default})

    def show_message(self, title, message, *, kind="info"):
        self._record("show_message", (title, message), {"kind": kind})

    def ask_string(self, title, prompt, *, initial=""):
        return self._answer("ask_string", (title, prompt), {"initial": initial})

    def ask_open_file(self, title, filters):
        return self._answer("ask_open_file", (title, tuple(filters)), {})

    def ask_save_file(self, title, filters, *, default_name=""):
        return self._answer("ask_save_file", (title, tuple(filters)), {"default_name": default_name})

    def progress(self, title, message="", *, cancellable=False):
        self._record("progress", (title, message), {"cancellable": cancellable})
        handle = _Progress(self._record, cancellable)
        handle.message = message
        return handle


class ScriptedUiPort(NullUiPort):
    """Consume one queued answer per question and record every call.

    Messages and progress never consume answers. An exhausted script raises
    AssertionError so tests cannot silently accept an unexpected question.
    Calls/answers are serialized; concurrent question ordering follows arrival.
    """

    def __init__(self, answers=()):
        self.answers = deque(answers)
        self.calls: list[UiCall] = []
        self._lock = threading.RLock()

    def _record(self, name, args, kwargs):
        with self._lock:
            self.calls.append(UiCall(name, args, dict(kwargs)))
        super()._record(name, args, kwargs)

    def _answer(self, name, args, kwargs, fallback=None):
        with self._lock:
            self._record(name, args, kwargs)
            if not self.answers:
                raise AssertionError(f"No scripted answer for {name}")
            return self.answers.popleft()
