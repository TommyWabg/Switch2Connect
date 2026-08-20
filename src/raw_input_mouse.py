# Switch2Connect - Virtual HID mice for the Joy-Con IR Mouse "Raw Input" mode.
# Copyright (C) 2026 TommyWabg
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Contact Information:
# Electronic Mail: tommyw9318@gmail.com
#
# The default mouse output path is win32api.mouse_event. Games that read through
# Raw Input (WM_INPUT) may ignore those events. "Raw Input" mode instead routes
# every controller's gyro, joystick, IR, button and wheel output through its own
# real virtual HID mouse created by WinUHid.
#
# Every connected physical controller owns a distinct virtual mouse.  The caller
# supplies a connection-unique key; a stable hash turns it into a PnP-safe WinUHid
# instance id without exposing Bluetooth addresses or USB paths.

import hashlib
import logging
import threading

logger = logging.getLogger(__name__)

_VENDOR_ID = 0x057E
_PRODUCT_ID = 0xF100

_lock = threading.Lock()
# controller key -> [VMouse, refcount]
_devices = {}
# Controller keys whose creation already failed once, so failure is logged once.
_failed_devices = set()
# Compatibility alias used by older diagnostics/tests.
_failed_sides = _failed_devices


def _is_packaged_build():
    try:
        from utils import is_packaged
        return bool(is_packaged())
    except Exception:
        # If package detection itself fails, preserve the standalone behaviour.
        return False


def available(refresh=False):
    """Return whether the live WinUHid stack can back a virtual mouse."""
    if _is_packaged_build():
        try:
            from config import packaged_winuhid_available
            return bool(packaged_winuhid_available(refresh=refresh))
        except Exception:
            return False
    try:
        from driver_install_helper import is_winuhid_usable
        return bool(is_winuhid_usable(use_cache=not refresh))
    except Exception:
        try:
            from winuhid_client import driver_interface_version
            return driver_interface_version() > 0
        except Exception:
            return False


def requested_mode():
    """Resolve the active Profile's explicit or capability-derived mouse mode."""
    try:
        from config import CONFIG
        mode = getattr(CONFIG, "mouse_output_mode", None)
        if mode in ("Standard", "Raw Input"):
            return mode
    except Exception:
        pass
    return "Raw Input" if available() else "Standard"


def _instance_id(device_key):
    digest = hashlib.sha1(str(device_key).encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"SWITCH2CONNECT_MOUSE_{digest.upper()}"


def acquire(device_key):
    """Return this connected controller's virtual mouse, creating it if needed.

    Returns None when the device could not be created (most commonly because the
    WinUHid driver is not installed). Callers must treat that as "fall back to the
    normal mouse_event path", never as a fatal error.

    Every successful call must be paired with exactly one release(device_key).
    """
    # Raw Input is a WinUHid-backed feature.  The MSIX package may use it only
    # when the user has separately installed a healthy WinUHid stack.  Keep this
    # live capability guard at the device boundary so a stale config or profile
    # cannot create a virtual HID device when the driver is absent.
    if not available():
        return None
    if device_key is None or str(device_key) == "":
        return None
    device_key = str(device_key)
    with _lock:
        entry = _devices.get(device_key)
        if entry is not None:
            entry[1] += 1
            return entry[0]

        try:
            import winuhid_client
        except Exception as exc:
            if device_key not in _failed_devices:
                _failed_devices.add(device_key)
                logger.warning("Raw Input mouse unavailable (%s): %s", device_key, exc)
            return None

        instance_id = _instance_id(device_key)
        try:
            mouse = winuhid_client.VMouse(vendor_id=_VENDOR_ID, product_id=_PRODUCT_ID,
                                          instance_id=instance_id)
        except Exception as exc:
            if device_key not in _failed_devices:
                _failed_devices.add(device_key)
                logger.warning("Raw Input mouse creation raised (%s): %s", device_key, exc)
            return None

        if mouse.device is None:
            if device_key not in _failed_devices:
                _failed_devices.add(device_key)
                logger.warning(
                    "Raw Input mouse could not be created for controller %s; "
                    "falling back to Win32 API mouse output. "
                    "Is the WinUHid driver installed?", device_key)
            return None

        _failed_devices.discard(device_key)
        _devices[device_key] = [mouse, 1]
        logger.info("Created Raw Input virtual mouse for controller %s", device_key)
        return mouse


def release(device_key):
    """Drop one reference to a controller device, destroying it at zero."""
    device_key = str(device_key)
    with _lock:
        entry = _devices.get(device_key)
        if entry is None:
            return
        entry[1] -= 1
        if entry[1] > 0:
            return
        mouse = entry[0]
        del _devices[device_key]
    try:
        mouse.close()
    except Exception:
        logger.exception("Failed to destroy Raw Input virtual mouse %s", device_key)
    else:
        logger.info("Destroyed Raw Input virtual mouse %s", device_key)


def shutdown():
    """Destroy every virtual mouse regardless of refcount (app teardown)."""
    with _lock:
        entries = list(_devices.items())
        _devices.clear()
    for device_key, (mouse, _count) in entries:
        try:
            mouse.close()
        except Exception:
            logger.exception("Failed to destroy Raw Input virtual mouse %s", device_key)
