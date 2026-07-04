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


def is_available() -> bool:
    """True if the HidHide driver is installed and its control device can be opened."""
    handle = _open_control()
    if handle is None:
        return False
    _k32.CloseHandle(handle)
    return True


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
    """Add this process's image to the HidHide application whitelist so it keeps access
    to hidden devices. Preserves any existing whitelist entries."""
    handle = _open_control()
    if handle is None:
        return False
    try:
        dos = _self_image_path()
        if not dos:
            return False
        current = _ioctl_get_multisz(handle, IOCTL_GET_WHITELIST)
        if any(e.lower() == dos.lower() for e in current):
            return True
        current.append(dos)
        return _ioctl_set_multisz(handle, IOCTL_SET_WHITELIST, current)
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
