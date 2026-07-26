# Switch2Connect - A Python and ESP32-S3 bridge utility for Switch 2 controller inputs.
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

r"""Minimal Python wrapper for the HidHide kernel driver control interface.

HidHide (github.com/nefarius/HidHide) hides selected HID devices from every process
*except* those on its application whitelist.  We use it so a physically-connected
controller's own HID device disappears from games while our whitelisted app can still
read it, leaving only the software-created virtual controller visible.

This talks to the driver's control device (``\\.\HidHide``) directly via
``DeviceIoControl`` — the same IOCTL contract used by HidHideCLI and the
Nefarius.Drivers.HidHide C# library (see
HIDHide/Nefarius.Drivers.HidHide-3.0.0/src/HidHideControlService.cs and
HIDHide/HidHide-master/Shared/HidHideIoctlContract.h).  No external binary is required.

All functions are defensive: if the driver isn't installed everything degrades to a
logged no-op so the wired controller still works (just not hidden).
"""

import ctypes
import logging
import sys
from ctypes import wintypes

logger = logging.getLogger(__name__)

_CONTROL_DEVICE = r"\\.\HidHide"

# CTL_CODE(DeviceType, Function, Method, Access) — matches HidHideControlService.CTL_CODE.
_IO_DEVICE_TYPE = 32769
_METHOD_BUFFERED = 0
_FILE_READ_DATA = 0x0001


def _ctl_code(function: int) -> int:
    return (_IO_DEVICE_TYPE << 16) | (_FILE_READ_DATA << 14) | (function << 2) | _METHOD_BUFFERED


IOCTL_GET_WHITELIST = _ctl_code(2048)
IOCTL_SET_WHITELIST = _ctl_code(2049)
IOCTL_GET_BLACKLIST = _ctl_code(2050)
IOCTL_SET_BLACKLIST = _ctl_code(2051)
IOCTL_GET_ACTIVE = _ctl_code(2052)
IOCTL_SET_ACTIVE = _ctl_code(2053)
IOCTL_GET_INVERSE = _ctl_code(2054)
IOCTL_SET_INVERSE = _ctl_code(2055)

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

if sys.platform == "win32":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _k32.DeviceIoControl.restype = wintypes.BOOL
    _k32.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.QueryDosDeviceW.restype = wintypes.DWORD
    _k32.QueryDosDeviceW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
else:  # pragma: no cover - Windows-only feature
    _k32 = None


def _open_control():
    if _k32 is None:
        return None
    handle = _k32.CreateFileW(
        _CONTROL_DEVICE,
        _GENERIC_READ | _GENERIC_WRITE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None, _OPEN_EXISTING, 0, None,
    )
    if not handle or handle == _INVALID_HANDLE_VALUE:
        return None
    return handle


def service_state():
    """Registration state of the HidHide kernel service.

    True  - registered and not pending deletion.
    False - definitely not registered (or deletion is pending).
    None  - could not be determined, e.g. the key exists but is not readable.

    The None case matters: PermissionError is an OSError, so folding it into
    False would report a perfectly good installation as missing and let the GUI
    persist that wrong answer into config.yaml.
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Services\HidHide",
            0,
            winreg.KEY_READ,
        )
    except FileNotFoundError:
        return False
    except OSError:
        return None
    try:
        try:
            delete_pending, _ = winreg.QueryValueEx(key, "DeleteFlag")
        except FileNotFoundError:
            delete_pending = 0
        except OSError:
            return None
    finally:
        winreg.CloseKey(key)
    return not bool(delete_pending)


def is_available() -> bool:
    """True when the HidHide kernel service is registered.

    The .sys file can remain on disk until reboot after a successful uninstall,
    and using only that file as the installation signal makes the GUI continue
    to display HidHide as installed.  The service registration is the component
    Windows uses to load the driver and is removed as part of uninstallation.

    An undeterminable state reads as False here to preserve the existing
    contract; callers that must distinguish it use service_state() directly.
    """
    return service_state() is True


def _ioctl_get_multisz(handle, code) -> list[str]:
    # First call: query required output size.
    required = wintypes.DWORD(0)
    _k32.DeviceIoControl(handle, code, None, 0, None, 0, ctypes.byref(required), None)
    size = required.value
    if size <= 0:
        return []
    buf = ctypes.create_string_buffer(size)
    returned = wintypes.DWORD(0)
    ok = _k32.DeviceIoControl(handle, code, None, 0, buf, size, ctypes.byref(returned), None)
    if not ok:
        return []
    raw = buf.raw[: returned.value]
    text = raw.decode("utf-16-le", errors="ignore")
    return [s for s in text.split("\x00") if s]


def _ioctl_set_multisz(handle, code, entries: list[str]) -> bool:
    payload = ("".join(e + "\x00" for e in entries) + "\x00").encode("utf-16-le")
    buf = ctypes.create_string_buffer(payload, len(payload))
    returned = wintypes.DWORD(0)
    ok = _k32.DeviceIoControl(handle, code, buf, len(payload), None, 0, ctypes.byref(returned), None)
    return bool(ok)


def _set_active(handle, active: bool) -> bool:
    val = ctypes.c_byte(1 if active else 0)
    returned = wintypes.DWORD(0)
    ok = _k32.DeviceIoControl(
        handle, IOCTL_SET_ACTIVE, ctypes.byref(val), 1, None, 0, ctypes.byref(returned), None
    )
    return bool(ok)


def is_active() -> bool:
    """True when HidHide filtering is currently active."""
    handle = _open_control()
    if handle is None:
        return False
    try:
        val = ctypes.c_byte(0)
        returned = wintypes.DWORD(0)
        ok = _k32.DeviceIoControl(
            handle, IOCTL_GET_ACTIVE, None, 0, ctypes.byref(val), 1, ctypes.byref(returned), None
        )
        return bool(ok and val.value)
    finally:
        _k32.CloseHandle(handle)


def set_active(active: bool) -> bool:
    """Enable or disable HidHide filtering without changing whitelist/blacklist entries."""
    handle = _open_control()
    if handle is None:
        return False
    try:
        return _set_active(handle, active)
    finally:
        _k32.CloseHandle(handle)


def _is_inverse(handle) -> bool:
    val = ctypes.c_byte(0)
    returned = wintypes.DWORD(0)
    ok = _k32.DeviceIoControl(
        handle, IOCTL_GET_INVERSE, None, 0, ctypes.byref(val), 1, ctypes.byref(returned), None
    )
    return bool(ok and val.value)


def is_inverse() -> bool:
    """True when HidHide Inverse Cloak (whitelist) is currently active."""
    handle = _open_control()
    if handle is None:
        return False
    try:
        return _is_inverse(handle)
    finally:
        _k32.CloseHandle(handle)


def _set_inverse(handle, active: bool) -> bool:
    val = ctypes.c_byte(1 if active else 0)
    returned = wintypes.DWORD(0)
    ok = _k32.DeviceIoControl(
        handle, IOCTL_SET_INVERSE, ctypes.byref(val), 1, None, 0, ctypes.byref(returned), None
    )
    return bool(ok)


def set_inverse(active: bool) -> bool:
    """Enable or disable HidHide Inverse Cloak (whitelist mode)."""
    handle = _open_control()
    if handle is None:
        return False
    try:
        return _set_inverse(handle, active)
    finally:
        _k32.CloseHandle(handle)


def _path_to_dos_device_path(path: str) -> str | None:
    """Convert e.g. ``C:\\App\\x.exe`` to ``\\Device\\HarddiskVolumeN\\App\\x.exe``.

    The whitelist stores application paths in DOS-device form (as HidHideCLI does)."""
    if not path or len(path) < 2 or path[1] != ":":
        return None
    drive = path[0:2]
    remainder = path[2:]
    target = ctypes.create_unicode_buffer(1024)
    if _k32.QueryDosDeviceW(drive, target, 1024) == 0:
        return None
    return target.value + remainder


def hid_path_to_instance_id(hid_path) -> str | None:
    r"""Derive a device instance ID from a hidapi enumeration ``path``.

    ``\\?\HID#VID_057E&PID_2069&MI_00#8&abc&0&0000#{guid}``  ->
    ``HID\\VID_057E&PID_2069&MI_00\\8&abc&0&0000``
    """
    if hid_path is None:
        return None
    if isinstance(hid_path, bytes):
        hid_path = hid_path.decode("utf-8", errors="ignore")
    s = hid_path
    for prefix in ("\\\\?\\", "\\\\.\\"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    # Drop the trailing interface-class GUID component ("#{...}").
    if "#{" in s:
        s = s[: s.index("#{")]
    return s.replace("#", "\\")


def _self_image_path() -> str | None:
    return _path_to_dos_device_path(sys.executable)


def whitelist_self() -> bool:
    """Configure the application list so this process can access hidden devices.

    Normal Cloak adds the process; Reverse/Inverse Cloak removes it because the
    application-list meaning is inverted. Other entries are preserved.
    """
    return prepare_self_visibility()


def prepare_self_visibility() -> bool:
    """Ensure this process can see HidHide-cloaked devices in either cloak mode.

    In normal cloak mode the application list is an allow-list, so this process
    must be present. In Reverse/Inverse Cloak mode the list meaning is inverted,
    so a path previously added in normal mode must be removed. Other application
    entries and the user's selected cloak mode are preserved.
    """
    handle = _open_control()
    if handle is None:
        return False
    try:
        dos = _self_image_path()
        if not dos:
            return False

        current = _ioctl_get_multisz(handle, IOCTL_GET_WHITELIST)
        is_inverse_mode = _is_inverse(handle)
        if is_inverse_mode:
            desired = [entry for entry in current if entry.lower() != dos.lower()]
            if desired != current and not _ioctl_set_multisz(handle, IOCTL_SET_WHITELIST, desired):
                return False
            verified = _ioctl_get_multisz(handle, IOCTL_GET_WHITELIST)
            return not any(entry.lower() == dos.lower() for entry in verified)

        if not any(entry.lower() == dos.lower() for entry in current):
            current.append(dos)
            if not _ioctl_set_multisz(handle, IOCTL_SET_WHITELIST, current):
                return False
        verified = _ioctl_get_multisz(handle, IOCTL_GET_WHITELIST)
        return any(entry.lower() == dos.lower() for entry in verified)
    finally:
        _k32.CloseHandle(handle)

def hide_device(instance_id: str) -> bool:
    """Hide the given device instance: whitelist self, add the instance to the blacklist,
    and activate hiding. No-op-safe if HidHide is unavailable."""
    if not instance_id:
        return False
    if not whitelist_self():
        logger.info("HidHide: could not whitelist self (driver missing?); device left visible")
        return False
    handle = _open_control()
    if handle is None:
        return False
    try:
        current = _ioctl_get_multisz(handle, IOCTL_GET_BLACKLIST)
        if not any(e.lower() == instance_id.lower() for e in current):
            current.append(instance_id)
            if not _ioctl_set_multisz(handle, IOCTL_SET_BLACKLIST, current):
                logger.info("HidHide: failed to set blacklist for %s", instance_id)
                return False
        ok = _set_active(handle, True)
        if ok:
            logger.info("HidHide: hiding %s", instance_id)
        return ok
    finally:
        _k32.CloseHandle(handle)


def unhide_device(instance_id: str) -> bool:
    """Remove the given device instance from the blacklist. If nothing remains hidden,
    deactivate HidHide so no unrelated devices stay affected by us."""
    if not instance_id:
        return False
    handle = _open_control()
    if handle is None:
        return False
    try:
        current = _ioctl_get_multisz(handle, IOCTL_GET_BLACKLIST)
        remaining = [e for e in current if e.lower() != instance_id.lower()]
        if len(remaining) == len(current):
            return True  # wasn't hidden
        ok = _ioctl_set_multisz(handle, IOCTL_SET_BLACKLIST, remaining)
        if not remaining:
            _set_active(handle, False)
        logger.info("HidHide: unhiding %s", instance_id)
        return ok
    finally:
        _k32.CloseHandle(handle)
