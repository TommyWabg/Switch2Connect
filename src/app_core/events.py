"""Event topics and immutable UI request payloads."""
from dataclasses import dataclass
from typing import Literal

CONTROLLERS_CHANGED = "controllers_changed"
STATUS_CHANGED = "status_changed"
WIRED_STATE_CHANGED = "wired_state_changed"
DRIVER_STATE_CHANGED = "driver_state_changed"
ESP32_STATE_CHANGED = "esp32_state_changed"
PROFILES_CHANGED = "profiles_changed"
SETTINGS_CHANGED = "settings_changed"
PROFILE_OVERLAY = "profile_overlay"
CALIBRATION_REQUEST = "calibration_request"
TASK_STATE = "task_state"
TOAST = "toast"
QUIT_REQUESTED = "quit_requested"
SHUTDOWN_COMPLETE = "shutdown_complete"


@dataclass(frozen=True, slots=True)
class SettingsChanged:
    keys: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class ProfileOverlayEvent:
    action: Literal["show", "update", "confirm", "cancel", "notification", "request"]
    previous: str | None = None
    current: str | None = None
    next: str | None = None
    layout: str | None = None
    title: str = ""
    message: str = ""
    kind: str = "info"
    auto_close_seconds: float | None = None
    request: str | None = None
    direction: int | None = None
    manual: bool = False
    auto_close_ms: int | None = None


@dataclass(frozen=True, slots=True)
class TaskState:
    task_id: str
    title: str
    active: bool
    message: str = ""
    progress: float | None = None


@dataclass(frozen=True, slots=True)
class Toast:
    message: str
    kind: Literal["info", "warning", "error", "success"] = "info"
    title: str = ""
    auto_close_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class CalibrationRequest:
    slot_index: int
    action: Literal["start", "cancel"] = "start"

PROFILE_OUTPUT_CHANGED = "profile_output_changed"

ESP32_FIRMWARE_SESSION = "esp32_firmware_session"

ESP32_PORTS_CHANGED = "esp32_ports_changed"

CALIBRATION_SESSION = "calibration_session"
