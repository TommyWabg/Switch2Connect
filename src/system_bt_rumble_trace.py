"""Low-overhead diagnostics for Joy-Con rumble/input stalls.

The tracer is deliberately opt-in and bounded.  When enabled, it covers every
virtual-controller driver and emulation mode used with the native System
Bluetooth connection route.  Events are written by a background thread so
controller and virtual-device callbacks do not perform file I/O.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import queue
import threading
import time
from pathlib import Path


DEFAULT_TRACE_PATH = "logs/system_bt_rumble_trace.txt"
NO_RUMBLE_TRACE_PATH = "logs/system_bt_rumble_trace_no_rumble.txt"
DEFAULT_QUEUE_SIZE = 65536


def _config():
    try:
        from config import CONFIG  # imported lazily to avoid config/module cycles
        return CONFIG
    except Exception:
        return None


def _eligible(controller=None, virtual_controller=None) -> bool:
    cfg = _config()
    if cfg is None or not bool(getattr(cfg, "system_bt_rumble_trace_enabled", True)):
        return False
    vc = virtual_controller or getattr(controller, "virtual_controller", None)

    def is_system_bluetooth(subject) -> bool:
        return bool(
            subject is not None
            and not getattr(subject, "is_esp32s3_bridge", False)
            and not getattr(subject, "is_wired_usb", False)
        )

    if controller is not None:
        return is_system_bluetooth(controller)
    if vc is None:
        return False
    return any(is_system_bluetooth(item) for item in (getattr(vc, "controllers", ()) or ()))


def _test_context(controller=None, virtual_controller=None) -> dict:
    """Return non-identifying mode details for the current trace event."""
    cfg = _config()
    vc = virtual_controller or getattr(controller, "virtual_controller", None)
    subject = controller
    if subject is None and vc is not None:
        subject = next(iter(getattr(vc, "controllers", ()) or ()), None)

    if subject is None:
        connection_mode = "Unknown"
    elif getattr(subject, "is_esp32s3_bridge", False):
        connection_mode = "ESP32-S3"
    elif getattr(subject, "is_wired_usb", False):
        connection_mode = "Wired USB"
    else:
        connection_mode = "System Bluetooth"

    driver_type = getattr(vc, "driver_type", None)
    if not driver_type and cfg is not None:
        driver_type = getattr(cfg, "driver_type", None)
    emulation_mode = getattr(vc, "mode", None)
    if not emulation_mode and cfg is not None:
        emulation_mode = getattr(cfg, "simulation_mode", None)
    dry_run = bool(getattr(cfg, "system_bt_rumble_trace_dry_run", False)) if cfg else False
    return {
        "connection_mode": str(connection_mode),
        "driver_type": str(driver_type or "Unknown"),
        "emulation_mode": str(emulation_mode or "Unknown"),
        "rumble_mode": "Off" if dry_run else "On",
    }


class _TraceWriter:
    def __init__(self, path: str, maxsize: int = DEFAULT_QUEUE_SIZE):
        self.path = str(path)
        self.events: queue.Queue = queue.Queue(maxsize=max(1024, int(maxsize)))
        self.stop_event = threading.Event()
        self.dropped = 0
        self._drop_lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True, name="SystemBTRumbleTrace")
        self.thread.start()

    def emit(self, event: dict) -> None:
        try:
            self.events.put_nowait(event)
        except queue.Full:
            with self._drop_lock:
                self.dropped += 1

    def close(self) -> None:
        self.stop_event.set()
        try:
            self.thread.join(timeout=1.0)
        except Exception:
            pass

    def _run(self) -> None:
        handle = None
        try:
            while not self.stop_event.is_set() or not self.events.empty():
                try:
                    event = self.events.get(timeout=0.25)
                except queue.Empty:
                    continue
                if handle is None:
                    path = Path(self.path)
                    if not path.is_absolute():
                        path = Path.cwd() / path
                    path.parent.mkdir(parents=True, exist_ok=True)
                    handle = path.open("a", encoding="utf-8", buffering=1)
                handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        except Exception:
            # Diagnostics must never affect controller operation.  The event
            # producer remains non-blocking if the destination is unavailable.
            pass
        finally:
            if handle is not None:
                try:
                    handle.flush()
                    handle.close()
                except Exception:
                    pass


_writer = None
_writer_key = None
_writer_lock = threading.Lock()
_run_id = f"{os.getpid()}-{time.time_ns()}"
_sequence = 0
_marker_latched = False
_marker_lock = threading.Lock()


def resolve_trace_path(path=None, cfg=None) -> Path:
    """Resolve a trace path beside the active config, independent of process CWD."""
    cfg = cfg or _config()
    dry_run = bool(getattr(cfg, "system_bt_rumble_trace_dry_run", False)) if cfg else False
    if path is None:
        path = getattr(cfg, "system_bt_rumble_trace_path", DEFAULT_TRACE_PATH) if cfg else DEFAULT_TRACE_PATH
    path = str(path or DEFAULT_TRACE_PATH)
    if path in (DEFAULT_TRACE_PATH, NO_RUMBLE_TRACE_PATH):
        path = NO_RUMBLE_TRACE_PATH if dry_run else DEFAULT_TRACE_PATH

    resolved = Path(os.path.expandvars(os.path.expanduser(path)))
    if not resolved.is_absolute():
        config_path = getattr(cfg, "config_file_path", None) if cfg else None
        base_dir = Path(config_path).resolve().parent if config_path else Path.cwd()
        resolved = base_dir / resolved
    return resolved.resolve()


def get_trace_log_directory() -> str:
    """Return the directory used by the current trace mode."""
    return str(resolve_trace_path().parent)


def _get_writer():
    global _writer, _writer_key
    path = str(resolve_trace_path())
    key = path
    with _writer_lock:
        if _writer is None or _writer_key != key:
            if _writer is not None:
                _writer.close()
            _writer = _TraceWriter(path)
            _writer_key = key
        return _writer


def trace_event(event: str, *, controller=None, virtual_controller=None, **fields) -> bool:
    """Publish one diagnostic event; return False when the path is out of scope."""
    if not _eligible(controller, virtual_controller):
        return False
    global _sequence
    with _writer_lock:
        _sequence += 1
        seq = _sequence
    record = {
        "run_id": _run_id,
        "seq": seq,
        "ts_ns": time.perf_counter_ns(),
        "event": str(event),
    }
    vc = virtual_controller or getattr(controller, "virtual_controller", None)
    subject = controller
    if subject is None and vc is not None:
        subject = next(iter(getattr(vc, "controllers", ()) or ()), None)
    address = getattr(getattr(subject, "device", None), "address", None)
    if address:
        record["subject"] = hashlib.sha256(str(address).encode("utf-8")).hexdigest()[:12]
    record.update(_test_context(controller=controller, virtual_controller=vc))
    record.update(fields)
    _get_writer().emit(record)
    return True


def dry_run_enabled(controller=None, virtual_controller=None) -> bool:
    cfg = _config()
    return bool(getattr(cfg, "system_bt_rumble_trace_dry_run", False)) and _eligible(controller, virtual_controller)


def interval_override_ms(controller=None, virtual_controller=None):
    if not _eligible(controller, virtual_controller):
        return None
    value = getattr(_config(), "system_bt_rumble_interval_ms", None)
    if value in (None, "", 0, "0"):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def maybe_trace_lock_marker(controller=None, virtual_controller=None) -> bool:
    """Emit one marker for Ctrl+Shift+F12 while diagnostics are enabled."""
    global _marker_latched
    if not _eligible(controller, virtual_controller):
        return False
    try:
        import win32api
        pressed = all(
            bool(win32api.GetAsyncKeyState(vk) & 0x8000)
            for vk in (0x11, 0x10, 0x7B)  # Ctrl + Shift + F12
        )
    except Exception:
        pressed = False
    with _marker_lock:
        emit = pressed and not _marker_latched
        _marker_latched = pressed
    if emit:
        trace_event("USER_LOCK_MARKER", controller=controller, virtual_controller=virtual_controller)
    return emit


@atexit.register
def _close_writer():
    global _writer
    with _writer_lock:
        writer = _writer
        _writer = None
    if writer is not None:
        writer.close()
