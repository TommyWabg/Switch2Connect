"""Small immutable display snapshots.

    None denotes an unavailable reading, never an inferred low/absent state.
    Collections use tuples so snapshots can safely cross thread boundaries.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Literal

Transport = Literal["ble", "esp32", "wired", "usbip"]


@dataclass(frozen=True, slots=True)
class ControllerInfo:
    kind: str
    """Physical model/side, e.g. pro, gamecube, joycon_left or joycon_right."""
    display_name: str
    """Human-readable model name shown on the player card."""
    address: str
    """Physical MAC or transport address, used to identify this controller."""
    transport: Transport
    """Physical connection route, independent of the virtual output driver."""
    product_id: int | None = None
    """Physical USB/Bluetooth product identifier, if available."""
    battery_level: Literal["high", "medium", "low"] | None = None
    """Classic battery icon tier; unknown readings remain None."""
    is_mag_calibrating: bool = False
    """Whether this physical controller's card should show Stop Cal."""
    mag_calibration_available: bool = True
    """Visibility of the single or corresponding merged-side Mag Cal action."""


@dataclass(frozen=True, slots=True)
class SlotInfo:
    index: int
    """Zero-based slot index, including extra discoverer slots."""
    player_number: int
    """Player number displayed on the card."""
    leds: tuple[bool, ...] = ()
    """Player LED pattern in physical LED order."""
    controllers: tuple[ControllerInfo, ...] = ()
    """Physical controllers assigned to the slot, one entry per controller."""
    is_merged_pair: bool = False
    """Whether Split and the dual-controller card are applicable."""
    is_empty: bool = True
    """Whether this slot should render the empty state."""
    hold_mode: str | None = None
    """Single Joy-Con orientation (Vertical/Horizontal), when applicable."""
    active_gyro_side: str | None = None
    """Selected left/right gyro source on a merged pair."""
    djg_left_active: bool = False
    """Whether the merged pair's L Gyro toggle is active."""
    djg_right_active: bool = False
    """Whether the merged pair's R Gyro toggle is active."""
    slot_token: tuple[int, int] | None = None
    """Opaque action identity: slot index and VC identity, never a VC reference."""
    kind: str | None = None
    split_available: bool = False
    merge_available: bool = False
    pending_merge_index: int | None = None
    merge_target: bool = False
    hold_mode_available: bool = False
    gyro_side_available: bool = False
    ping_available: bool = False


@dataclass(frozen=True, slots=True)
class StatusInfo:
    connection_route: str = ""
    """Header route label, e.g. ESP32-S3, System BLE or USB."""
    text: str = ""
    """Classic connection status text, e.g. Ready or Pending USB Connection."""
    level: Literal["ok", "warn", "error"] = "warn"
    """Semantic severity for the header color."""

    color: str = "#888888"


@dataclass(frozen=True, slots=True)
class DriverState:
    driver_type: str = ""
    """Active output driver (WinUHid, ViGEmBus or USBIP)."""
    simulation_mode: str = ""
    """Active Emu Mode label."""
    abxy_mode: str = "Xbox"
    """Selected Xbox/Switch button layout."""
    driver_options: tuple[str, ...] = ()
    """Driver choices available on this machine/package."""
    mode_options: tuple[tuple[str, tuple[str, ...]], ...] = ()
    """Available Emu Mode labels grouped by driver name."""
    install_states: tuple[tuple[str, str], ...] = ()
    """Named driver health states (healthy/partial/absent/unknown)."""
    wired_auto_scan_enabled: bool = False
    """Whether automatic wired-controller adoption is enabled."""
    hidhide_hide_enabled: bool = False
    """Whether hiding physical wired controllers is requested."""
    hidhide_service_state: bool | None = None
    """Tri-state HidHide service registration health."""
    usbip_restart_pending: bool = False
    """Whether USBIP removal requires a restart before selecting it."""
    busy: bool = False
    """Whether a driver operation currently owns the controls."""

    packaged_build: bool = False
    packaged_winuhid_available: bool = False
    pending_check: bool = True
    install_status: tuple[InstallStatus, ...] = ()
    action_label: str = "Install WinUHid Driver"
    action_kind: str = "install"
    needs_attention: bool = False
    keyboard_backend: str = "Win32 API"
    mouse_backend: str = "Win32 API"
    keyboard_available: bool = False
    mouse_available: bool = False
    wired_settings_label: str = "Wired Controller"
    hidhide_installed: bool = False
    hidhide_active: bool = False


@dataclass(frozen=True, slots=True)
class Esp32State:
    detected: bool = False
    """Whether an ESP32-S3 bridge is physically detected."""
    status: str = ""
    """Bridge status/diagnostic text for the firmware manager."""
    firmware_version: str | None = None
    """Detected firmware version, if readable."""
    port: str | None = None
    """Selected/detected serial port, if present."""
    transport: str | None = None
    """Bridge interface (CDC/OTG) controlling available firmware actions."""
    busy: bool = False
    """Whether flashing or another bridge operation owns the device."""

    firmware_current: bool = False
    firmware_installed: bool = False
    expected_version: str = ""
    firmware_mode: str = ""
    device_id: str = ""
    serial_number: str = ""
    location: str = ""
    otg_boot: bool = False
    unverified_candidate: bool = False
    firmware_text: str = "Not installed"


@dataclass(frozen=True, slots=True)
class ProfilesState:
    names: tuple[str, ...] = ()
    """Sorted profile names displayed in the selector."""
    active: str = ""
    """Currently active profile name."""
    assigned_apps: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()
    """Profile names with immutable (path, display name) app assignments."""
    change_list: tuple[str, ...] = ()
    """Profiles included in Change Profile cycling/selection."""
    switching_combos: tuple[tuple[str, str], ...] = ()
    """Per-profile recorded switching token strings."""
    combo_trigger: str = ""
    """Global recorded profile selection trigger token string."""
    change_profile_mode: str = "Auto"
    """Auto cycling or Manual selection behavior."""


@dataclass(frozen=True, slots=True)
class WiredState:
    detected: bool = False
    product_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ActionResult:
    ok: bool
    reason: str | None = None

    def __bool__(self):
        return self.ok


@dataclass(frozen=True, slots=True)
class InstallStatus:
    driver: str
    state: str = "unknown"
    description: str = ""


@dataclass(frozen=True, slots=True)
class PortChoice:
    port: str
    label: str
    name: str = ""
    manufacturer: str = ""
    device_id: str = ""
    serial_number: str = ""
    location: str = ""
    is_otg: bool = False
    confidence: int = 0
    verified: bool = False


@dataclass(frozen=True, slots=True)
class PortMode:
    mode: str = "auto"
    port: str = ""
    device_id: str = ""
    serial_number: str = ""
    location: str = ""


@dataclass(frozen=True, slots=True)
class FirmwareSession:
    phase: Literal["idle", "flashing", "awaiting_replug", "done", "error"] = "idle"
    percent: float = 0
    message: str = ""
    can_close: bool = True
    action: str = ""


# Compatibility name for ESP32 operation results.
OperationResult = ActionResult


@dataclass(frozen=True, slots=True)
class ProfileResult:
    ok: bool
    reason: str | None = None
    restart_recording: bool = False
    names: tuple[str, ...] = ()
    path: str | None = None

    def __bool__(self):
        return self.ok


@dataclass(frozen=True, slots=True)
class ImportPreview:
    path: str
    names: tuple[str, ...] = ()
    has_controller_data: bool = False
    ok: bool = True
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ExportPreview:
    names: tuple[str, ...] = ()
    active: str = ""
    has_controller_data: bool = False


@dataclass(frozen=True, slots=True)
class StickProgress:
    key: str
    side: str
    label: str
    rotate_elapsed: float
    release_elapsed: float
    rotate_done: bool
    release_done: bool
    phase: str
    raw: tuple[int, int]


@dataclass(frozen=True, slots=True)
class CalibrationSession:
    session_id: str
    kind: str
    slot_token: tuple
    phase: str
    title: str
    message: str
    active: bool = True
    completed: bool = False
    sticks: tuple[StickProgress, ...] = ()
    step: int | None = None
    trigger_values: tuple[int, int] | None = None
    next_label: str = "Next"
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CalibrationState:
    sessions: tuple[CalibrationSession, ...] = ()


def freeze(value):
    if isinstance(value, dict):
        return tuple((k, freeze(v)) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return tuple(freeze(v) for v in value)
    return value


@dataclass(frozen=True, slots=True)
class Option:
    value: Any
    label: str


@dataclass(frozen=True, slots=True)
class OptionCategory:
    title: str
    rows: tuple[tuple[Option, ...], ...]


@dataclass(frozen=True, slots=True)
class Control:
    key: str
    label: str
    value: Any
    options: tuple = ()
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    visible: bool = True


@dataclass(frozen=True, slots=True)
class MappingRow:
    key: str
    label: str
    group: str
    stored_value: Any
    display_text: str
    mode: str | None
    is_default: bool
    options: tuple[OptionCategory, ...]
    visible: bool = True
    selected_option: Any = None
    configurable: bool = False
    mode_values: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class MappingView:
    scope: str | None
    rows: tuple[MappingRow, ...]


@dataclass(frozen=True, slots=True)
class JoystickSettingsView:
    key: str
    scope: str | None
    mouse_sensitivity: float
    scroll_mode: str
    scroll_activation: str
    controls: tuple[Control, ...]
    mapping_rows: tuple[MappingRow, ...] = ()


@dataclass(frozen=True, slots=True)
class DeadzoneRow:
    family: str
    left: int
    right: int
    linked: bool


@dataclass(frozen=True, slots=True)
class DeadzoneView:
    rows: tuple[DeadzoneRow, ...]
    minimum: int = 0
    maximum: int = 100
    step: int = 1


@dataclass(frozen=True, slots=True)
class GcTriggerView:
    scope: str | None
    mode: str
    options: tuple[Option, ...]
    click_mapping_visible: bool = True


@dataclass(frozen=True, slots=True)
class IrSensorView:
    side: str
    scope: str | None
    function: str
    display_text: str
    mode: str | None
    activate_threshold: int
    mouse: tuple
    in_app_gyro: tuple
    options: tuple[OptionCategory, ...]
    controls: tuple[Control, ...]

    profile_name: str = ""
    category: str = ""
    selected_option: Any = None
    is_gyro: bool = False
    gyro_view: InAppGyroView | None = None

@dataclass(frozen=True, slots=True)
class GyroView:
    controls: tuple[Control, ...]


@dataclass(frozen=True, slots=True)
class InAppGyroView:
    key: str
    controls: tuple[Control, ...]
    simultaneous_options: tuple[OptionCategory, ...]
    simultaneous_mapping: MappingRow | None = None


@dataclass(frozen=True, slots=True)
class RumbleView:
    mode: str
    options: tuple[Option, ...]
    frequency_visible: bool
    strength: float
    frequency: float
    delay_ms: int
    audio_haptics_available: bool
    audio_haptics_enabled: bool
    adaptive_triggers_enabled: bool
    impulse_trigger_available: bool
    impulse_trigger_enabled: bool
    impulse_trigger_dynamic_frequency: bool
    impulse_trigger_frequency: int
    impulse_trigger_strength: int
    controls: tuple[Control, ...]


@dataclass(frozen=True, slots=True)
class AutoDisconnectView:
    mode: str
    days: int
    hours: int
    minutes: int
    options: tuple[str, ...] = ("OFF", "Inactive", "Absolute")


@dataclass(frozen=True, slots=True)
class AppSettingsView:
    run_at_startup: bool
    start_minimized: bool
    power_saving_mode: str
    ui_mode: str
    change_profile_mode: str = "Manual"


@dataclass(frozen=True, slots=True)
class SettingsState:
    mappings: tuple[MappingView, ...]
    joysticks: tuple[JoystickSettingsView, ...]
    deadzones: DeadzoneView
    gc_triggers: tuple[GcTriggerView, ...]
    ir_sensors: tuple[IrSensorView, ...]
    gyro: GyroView
    rumble: RumbleView
    auto_disconnect: AutoDisconnectView
    app: AppSettingsView
    in_app_gyro: tuple[InAppGyroView, ...] = ()


@dataclass(frozen=True, slots=True)
class SettingResult:
    ok: bool
    reason: str | None = None
    ui_priority_policy: str | None = None
    restart_required: bool = False
