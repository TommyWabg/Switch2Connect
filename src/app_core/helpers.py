"""UI-independent classic helpers; moved bodies retain their original behavior."""
import ctypes
from ctypes import wintypes
import logging
import os
import time
from collections.abc import Mapping
from importlib import import_module
MOUSE_CLICK_BACK_BUTTON_TOKENS = {"m1 Left Click": "MB_1", "m2 Middle Click": "MB_2",
    "m3 Right Click": "MB_3", "m4 Backward": "MB_4", "m5 Forward": "MB_5"}
from driver_install_helper import get_winuhid_status, get_vigembus_status, invalidate_driver_status_cache

logger = logging.getLogger(__name__)

class _ControllerNames(Mapping):
    """Resolve names only when used, without loading the hardware stack on import."""
    def __getitem__(self, key):
        return import_module("controller").CONTROLER_NAMES[key]
    def __iter__(self):
        return iter(import_module("controller").CONTROLER_NAMES)
    def __len__(self):
        return len(import_module("controller").CONTROLER_NAMES)

CONTROLER_NAMES = _ControllerNames()

def _set_current_thread_priority(level):
    try:
        if os.name == "nt":
            kernel32 = ctypes.windll.kernel32
            kernel32.SetThreadPriority(kernel32.GetCurrentThread(), int(level))
    except Exception:
        pass


def _get_current_thread_priority():
    try:
        if os.name == "nt":
            kernel32 = ctypes.windll.kernel32
            priority = int(kernel32.GetThreadPriority(kernel32.GetCurrentThread()))
            return None if priority == 0x7FFFFFFF else priority
    except Exception:
        pass
    return None


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT),
    ]


def normalize_app_path(path):
    if not path:
        return ""
    try:
        return os.path.normcase(os.path.abspath(os.path.normpath(path)))
    except Exception:
        return os.path.normcase(os.path.normpath(path))


def get_exe_display_name(path):
    if not path:
        return "Choose App"
    try:
        import win32api
        win32api.GetFileVersionInfo(path, "\\")
        lang, codepage = win32api.GetFileVersionInfo(path, "\\VarFileInfo\\Translation")[0]
        for key in ("FileDescription", "ProductName"):
            value = win32api.GetFileVersionInfo(path, f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\{key}")
            if value:
                return str(value)
    except Exception:
        pass
    return os.path.splitext(os.path.basename(path))[0] or "Choose App"


def check_driver_registry():
    return bool(get_winuhid_status(use_cache=True).registry_exists)


def check_driver_pnputil():
    return bool(get_winuhid_status(use_cache=True).present_instances)


def is_driver_installed():
    return get_winuhid_status(use_cache=True).installed


def verify_winuhid_runtime(attempts=10, delay_seconds=0.5):
    """Create, submit one neutral report to, and destroy a temporary WinUHid pad."""
    import winuhid_client
    for attempt in range(max(1, attempts)):
        pad = None
        try:
            pad = winuhid_client.VX360Gamepad()
            if getattr(pad, "device", None) and pad.update() is not False:
                return True
        except Exception as exc:
            logger.debug("WinUHid runtime smoke test attempt %d failed: %s", attempt + 1, exc)
        finally:
            if pad is not None:
                try:
                    pad.close()
                except Exception:
                    pass
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    logger.error("WinUHid runtime smoke test failed after %d attempts", attempts)
    return False


def hidhide_service_state():
    """HidHide service registration: True / False / None (undeterminable).

    Never raises. None must not be persisted as "not installed" - a registry key
    that exists but cannot be read would otherwise write a wrong answer into
    config.yaml that survives restarts.
    """
    try:
        import hidhide
        return hidhide.service_state()
    except Exception as exc:
        logger.debug("HidHide state could not be read: %s", exc)
        return None


def removal_verified(status, runtime_probe):
    """True when a driver can be considered gone.

    Normally every layer must read absent. When the layers cannot be read at all
    (older pnputil), fall back to the runtime probe: if a client can no longer be
    created, the driver is effectively removed.
    """
    if status.absent:
        return True
    if status.unknown:
        return not runtime_probe(attempts=2)
    return False


def check_vigembus_registry():
    status = get_vigembus_status(use_cache=True)
    return bool(status.service_exists) or bool(status.msi_entries)


def check_vigembus_pnputil():
    status = get_vigembus_status(use_cache=True)
    return bool(status.bound_instances and status.driver_packages)


def is_vigembus_installed():
    return get_vigembus_status(use_cache=True).installed


def verify_vigembus_runtime(attempts=10, delay_seconds=0.5):
    for attempt in range(max(1, attempts)):
        bus = None
        try:
            from virtual_controller import get_vigem
            vigem = get_vigem()
            bus = vigem.win.virtual_gamepad.VBus()
            return True
        except Exception as exc:
            logger.debug("ViGEmBus runtime smoke test attempt %d failed: %s", attempt + 1, exc)
        finally:
            if bus is not None:
                try:
                    del bus
                except Exception:
                    pass
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    return False


def verify_vigembus_ready(attempts=12, delay_seconds=0.5):
    """Wait for both PnP/service health and an actual client connection.

    When the PnP layers cannot be determined (pnputil without /properties), the
    runtime connection alone decides - it is what the app actually depends on.
    """
    for attempt in range(max(1, attempts)):
        invalidate_driver_status_cache("vigembus")
        status = get_vigembus_status()
        if (status.installed or status.unknown) and verify_vigembus_runtime(attempts=1):
            return True
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    return False


def wired_controller_label(product_ids, sentence=False):
    """Name the wired pad(s) currently connected, for UI text.

    Every wired string used to be hardcoded to "Pro Controller 2", so plugging in a
    GameCube controller produced buttons and prompts naming the wrong device. Names
    come from CONTROLER_NAMES so wired text matches what the rest of the app calls
    the same pad.

    ``sentence`` returns a subject phrase to open a sentence with ("A wired NSO
    GameCube Controller"), rather than the bare button label.
    """
    ids = [pid for pid in dict.fromkeys(product_ids or ()) if pid in CONTROLER_NAMES]
    if len(ids) > 1:
        # Mixed set (e.g. a Pro Controller 2 and a GameCube pad): naming one of them
        # would be wrong, so stay generic rather than pick a winner.
        return "Wired controllers were" if sentence else "Wired Controllers"
    if not ids:
        return "A wired controller was" if sentence else "Wired Controller"
    name = CONTROLER_NAMES[ids[0]]
    if sentence:
        return f"{'An' if name[0] in 'AEIOU' else 'A'} wired {name} was"
    return f"Wired {name}"


_INPUT_MODIFIER_TOKENS = {
    "VK_CONTROL", "VK_CONTROL_L", "VK_CONTROL_R", "VK_LCONTROL", "VK_RCONTROL",
    "VK_SHIFT", "VK_SHIFT_L", "VK_SHIFT_R", "VK_LSHIFT", "VK_RSHIFT",
    "VK_MENU", "VK_ALT", "VK_ALT_L", "VK_ALT_R", "VK_LMENU", "VK_RMENU",
    "VK_WIN", "VK_LWIN", "VK_RWIN", "VK_WIN_L", "VK_WIN_R",
}


def format_input_display(text):
    """Human-readable form of a recorded Custom input token string. Mouse buttons are
    shown as M1/M2/M3 (left/right/middle), keyboard keys as KB<key> (e.g. KB1, KBA),
    keyboard modifiers keep their bare name (CONTROL, SHIFT, ...), and controller buttons
    keep their bare name. The stored config value still uses the raw MB_/VK_/BTN_ tokens;
    this only affects what the recorder entry displays."""
    parts = []
    for token in text.split("+"):
        if token in _INPUT_MODIFIER_TOKENS:
            parts.append(token[3:])          # strip "VK_", keep modifier name as-is
        elif token.startswith("VK_"):
            parts.append("KB" + token[3:])
        elif token.startswith("MB_"):
            parts.append({"MB_1": "M1", "MB_2": "M3", "MB_3": "M2"}.get(token, "M" + token[3:]))
        elif token.startswith("BTN_"):
            parts.append(token[4:])
        else:
            parts.append(token)
    return "+".join(parts)


MOUSE_CLICK_CUSTOM_TOKENS = {v: k for k, v in MOUSE_CLICK_BACK_BUTTON_TOKENS.items()}


def parse_mouse_click_mapping(value):
    if not isinstance(value, str):
        return None
    if value in MOUSE_CLICK_BACK_BUTTON_TOKENS:
        return value, "Hold"
    if value.startswith("Custom[Tap]:"):
        mode = "Tap"
        payload = value[12:]
    elif value.startswith("Custom[Hold]:"):
        mode = "Hold"
        payload = value[13:]
    elif value.startswith("Custom:"):
        mode = "Hold"
        payload = value[7:]
    else:
        return None
    if "+" in payload:
        return None
    option_token = MOUSE_CLICK_CUSTOM_TOKENS.get(payload)
    if option_token is None:
        return None
    return option_token, mode


MACHINE_LOCAL_CONFIG_KEYS = frozenset({
    "ui_mode",
    "driver_installed",
    "vigembus_installed",
    "hidhide_installed",
    "hidhide_install_prompt_suppressed",
    "window_width",
    "window_height",
    "window_x",
    "window_y",
    "ui_scale",
    "controller_fast_cache",
    "controller_fast_cache_entries",
    "winrt_cached_services",
    "power_saving_mode",
    "power_saving_auto_warning_suppressed",
    "power_saving_full_warning_suppressed",
    "esp32_serial_port_mode",
    "esp32_serial_port",
    "esp32_serial_device_id",
    "esp32_serial_number",
    "esp32_serial_location",
})


CONTROLLER_RELATED_CONFIG_KEYS = frozenset({
    "calibration_data",
    "joystick_calibration_data",
    "mag_calibration_data",
    "mag_v2_calibration_data",
    "gc_trigger_calibration_data",
    "controller_calibration_aliases",
    "cemuhook_mac_to_pad",
    "merged_gyro_side",
    "joycon_hold_mode",
    "controller_fast_cache_entries",
    "gyro_bias_l",
    "gyro_bias_r",
    "stick_r_bias",
})


CONTROLLER_RELATED_PRESENCE_KEYS = (
    "calibration_data",
    "joystick_calibration_data",
    "mag_calibration_data",
    "mag_v2_calibration_data",
    "gc_trigger_calibration_data",
    "controller_calibration_aliases",
    "cemuhook_mac_to_pad",
    "merged_gyro_side",
    "joycon_hold_mode",
    "controller_fast_cache_entries",
)


CONTROLLER_RELATED_NESTED_KEYS = ("joycon_hold_mode",)


def strip_controller_related_from_profile(profile):
    """Empty the MAC-keyed entries inside one profile-shaped mapping."""
    if not isinstance(profile, dict):
        return
    for key in CONTROLLER_RELATED_NESTED_KEYS:
        if isinstance(profile.get(key), dict):
            profile[key] = {}
        for value in profile.values():
            if isinstance(value, dict) and isinstance(value.get(key), dict):
                value[key] = {}


def strip_controller_related(config_data):
    """Remove every controller-bound entry from a whole config mapping.

    Covers the top-level MAC-keyed blobs, the per-profile category dicts and the
    legacy ``button_remaps`` block, which is profile-shaped and still carries
    ``joycon_hold_mode`` entries keyed by controller address.
    """
    if not isinstance(config_data, dict):
        return
    for key in CONTROLLER_RELATED_CONFIG_KEYS:
        config_data.pop(key, None)
    strip_controller_related_from_profile(config_data.get("button_remaps"))
    profiles = config_data.get("profiles")
    if isinstance(profiles, dict):
        for profile_data in profiles.values():
            strip_controller_related_from_profile(profile_data)


def _profile_has_controller_related_data(profile):
    if not isinstance(profile, dict):
        return False
    for key in CONTROLLER_RELATED_NESTED_KEYS:
        if profile.get(key):
            return True
        if any(isinstance(value, dict) and value.get(key) for value in profile.values()):
            return True
    return False


def has_controller_related_data(data):
    """True when ``data`` (a config mapping) carries controller-bound entries."""
    if not isinstance(data, dict):
        return False
    if any(data.get(key) for key in CONTROLLER_RELATED_PRESENCE_KEYS):
        return True
    if _profile_has_controller_related_data(data.get("button_remaps")):
        return True
    profiles = data.get("profiles")
    if isinstance(profiles, dict):
        return any(_profile_has_controller_related_data(p) for p in profiles.values())
    return False

