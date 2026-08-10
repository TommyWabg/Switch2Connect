"""Windows-version and API-contract gates for BLE preferred parameters."""

from __future__ import annotations

import importlib
import sys


PREFERRED_CONNECTION_PARAMETERS_MIN_BUILD = 22000


def _load_api_information():
    for module_name in (
        "winrt.windows.foundation.metadata",
        "winsdk.windows.foundation.metadata",
        "bleak_winrt.windows.foundation.metadata",
    ):
        try:
            module = importlib.import_module(module_name)
            api_information = getattr(module, "ApiInformation", None)
            if api_information is not None:
                return api_information
        except Exception:
            continue
    return None


def _call_api_information(api_information, snake_name, pascal_name, *args):
    method = getattr(api_information, snake_name, None)
    if method is None:
        method = getattr(api_information, pascal_name, None)
    if not callable(method):
        return None
    try:
        return bool(method(*args))
    except Exception:
        return False


def preferred_connection_parameters_supported(
        windows_build=None, api_information=None):
    """Return ``(supported, reason)`` without touching WinRT Bluetooth on Win10."""
    if sys.platform != "win32" and windows_build is None:
        return False, "non-Windows platform"
    if windows_build is None:
        try:
            windows_build = int(sys.getwindowsversion().build)
        except Exception:
            return False, "Windows build unavailable"
    windows_build = int(windows_build)
    if windows_build < PREFERRED_CONNECTION_PARAMETERS_MIN_BUILD:
        return False, (
            f"Windows build {windows_build} < "
            f"{PREFERRED_CONNECTION_PARAMETERS_MIN_BUILD}")

    if api_information is None:
        api_information = _load_api_information()
    if api_information is None:
        return True, f"Windows build {windows_build}; ApiInformation unavailable"

    type_present = _call_api_information(
        api_information, "is_type_present", "IsTypePresent",
        "Windows.Devices.Bluetooth.BluetoothLEPreferredConnectionParameters")
    method_present = _call_api_information(
        api_information, "is_method_present", "IsMethodPresent",
        "Windows.Devices.Bluetooth.BluetoothLEDevice",
        "RequestPreferredConnectionParameters")
    if type_present is not True or method_present is not True:
        return False, (
            f"WinRT API contract unavailable type={type_present} "
            f"method={method_present}")
    return True, f"Windows build {windows_build}; WinRT API contract present"
