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
# The default IR Mouse output path is win32api.mouse_event, which only moves the
# system cursor. Games that read the mouse through Raw Input (WM_INPUT) never see
# those events. "Raw Input" mode instead submits the IR-derived motion through a
# real virtual HID mouse created by WinUHid, which the whole input stack honours.
#
# One device per side, shared by reference count: the raw_input setting is stored
# per side (not per controller), so two left Joy-Cons connected at once are both
# served by the single "left" device rather than enumerating a duplicate.

import logging
import threading

logger = logging.getLogger(__name__)

# Nintendo's vendor id with the real Joy-Con 2 product ids (JOYCON2_LEFT_PID /
# JOYCON2_RIGHT_PID in controller.py), so the two virtual mice are distinguishable
# from each other and from every other mouse on the system. WinUHid exposes no
# product-string field, so Windows still labels them "HID-compliant mouse"; the
# identifiers plus the instance id are the identity.
_SIDE_IDS = {
    "left": (0x057E, 0x2067, "SWITCH2CONNECT_JOYCON_MOUSE_LEFT"),
    "right": (0x057E, 0x2066, "SWITCH2CONNECT_JOYCON_MOUSE_RIGHT"),
}

_lock = threading.Lock()
# side -> [VMouse, refcount]
_devices = {}
# Sides whose creation already failed once, so the failure is logged only once.
_failed_sides = set()


def _is_packaged_build():
    try:
        from utils import is_packaged
        return bool(is_packaged())
    except Exception:
        # If package detection itself fails, preserve the standalone behaviour.
        return False


def acquire(side):
    """Return the shared virtual mouse for `side`, creating it if needed.

    Returns None when the device could not be created (most commonly because the
    WinUHid driver is not installed). Callers must treat that as "fall back to the
    normal mouse_event path", never as a fatal error.

    Every successful call must be paired with exactly one release(side).
    """
    # Raw Input is a WinUHid-backed feature.  The MSIX package may use it only
    # when the user has separately installed a healthy WinUHid stack.  Keep this
    # live capability guard at the device boundary so a stale config or profile
    # cannot create a virtual HID device when the driver is absent.
    if _is_packaged_build():
        try:
            from config import packaged_winuhid_available
            if not packaged_winuhid_available():
                return None
        except Exception:
            return None
    if side not in _SIDE_IDS:
        return None
    with _lock:
        entry = _devices.get(side)
        if entry is not None:
            entry[1] += 1
            return entry[0]

        try:
            import winuhid_client
        except Exception as exc:
            if side not in _failed_sides:
                _failed_sides.add(side)
                logger.warning("Raw Input mouse unavailable (%s side): %s", side, exc)
            return None

        vendor_id, product_id, instance_id = _SIDE_IDS[side]
        try:
            mouse = winuhid_client.VMouse(vendor_id=vendor_id, product_id=product_id,
                                          instance_id=instance_id)
        except Exception as exc:
            if side not in _failed_sides:
                _failed_sides.add(side)
                logger.warning("Raw Input mouse creation raised (%s side): %s", side, exc)
            return None

        if mouse.device is None:
            if side not in _failed_sides:
                _failed_sides.add(side)
                logger.warning(
                    "Raw Input mouse could not be created for the %s side; "
                    "falling back to the standard IR Mouse output. "
                    "Is the WinUHid driver installed?", side)
            return None

        _failed_sides.discard(side)
        _devices[side] = [mouse, 1]
        logger.info("Created Raw Input virtual mouse for the %s Joy-Con", side)
        return mouse


def release(side):
    """Drop one reference to `side`'s device, destroying it at zero."""
    with _lock:
        entry = _devices.get(side)
        if entry is None:
            return
        entry[1] -= 1
        if entry[1] > 0:
            return
        mouse = entry[0]
        del _devices[side]
    try:
        mouse.close()
    except Exception:
        logger.exception("Failed to destroy the %s Raw Input virtual mouse", side)
    else:
        logger.info("Destroyed the %s Raw Input virtual mouse", side)


def shutdown():
    """Destroy every virtual mouse regardless of refcount (app teardown)."""
    with _lock:
        entries = list(_devices.items())
        _devices.clear()
    for side, (mouse, _count) in entries:
        try:
            mouse.close()
        except Exception:
            logger.exception("Failed to destroy the %s Raw Input virtual mouse", side)
