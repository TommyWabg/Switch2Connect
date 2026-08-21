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

import win32api
import winreg
import sys
import os
import math
import threading
class USBIPAllocator:
    _lock = threading.Lock()
    _counter = 1

    @classmethod
    def allocate(cls):
        with cls._lock:
            host = f"127.0.0.{cls._counter}"
            bus_id = f"1-{cls._counter}"
            port = 3240 + (cls._counter - 1)
            
            cls._counter += 1
            if cls._counter > 254:
                cls._counter = 1
                
            return host, bus_id, port

def to_hex(buffer):
    return " ".join("{:02x}".format(x) for x in buffer)

def decodeu(data: bytes):
    return int.from_bytes(data, byteorder='little', signed=False)

def decodes(data: bytes):
    return int.from_bytes(data, byteorder='little', signed=True)

_CACHED_LOCAL_MAC_VALUE = None

# Bluetooth radio (HCI) device interface. Present exactly when Windows has a Bluetooth
# radio enumerated -- built in or on a dongle.
GUID_BTHPORT_DEVICE_INTERFACE = "{0850302A-B344-4FDA-9BE9-90576B8D46F0}"


def bluetooth_radio_present() -> bool:
    """True when Windows currently has a Bluetooth radio device.

    ``get_local_mac_value()`` below raises "No more data is available"
    (ERROR_NO_MORE_ITEMS) both when there is no radio at all and when the Bluetooth stack
    simply has not finished starting yet, which are very different situations: the first
    should never be retried, the second always should. Asking PnP directly tells the two
    apart, so a machine with no dongle stops retrying immediately while a machine whose
    radio is still warming up at boot keeps its retries.

    Returns True on any unexpected failure -- callers then fall back to their retry loop,
    which is the safe direction to be wrong in.
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        import uuid

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("InterfaceClassGuid", GUID),
                ("Flags", wintypes.DWORD),
                ("Reserved", ctypes.c_void_p),
            ]

        value = uuid.UUID(GUID_BTHPORT_DEVICE_INTERFACE.strip("{}"))
        guid = GUID(value.time_low, value.time_mid, value.time_hi_version,
                    (ctypes.c_ubyte * 8)(*value.bytes[8:]))

        DIGCF_PRESENT = 0x00000002
        DIGCF_DEVICEINTERFACE = 0x00000010
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
        setupapi.SetupDiGetClassDevsW.argtypes = [
            ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD
        ]
        setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
        setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.DWORD,
            ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)
        ]
        setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
        setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]

        info_set = setupapi.SetupDiGetClassDevsW(
            ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
        if info_set == INVALID_HANDLE_VALUE:
            return True
        try:
            iface = SP_DEVICE_INTERFACE_DATA()
            iface.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            return bool(setupapi.SetupDiEnumDeviceInterfaces(
                info_set, None, ctypes.byref(guid), 0, ctypes.byref(iface)))
        finally:
            setupapi.SetupDiDestroyDeviceInfoList(info_set)
    except Exception:
        return True


def convert_mac_string_to_value(mac: str):
    # Handle colons, dashes, and spaces robustly and convert to integer
    cleaned = mac.replace(":", "").replace("-", "").strip()
    return int(cleaned, 16)

def get_local_mac_value():
    global _CACHED_LOCAL_MAC_VALUE
    if _CACHED_LOCAL_MAC_VALUE is not None:
        return _CACHED_LOCAL_MAC_VALUE
    
    import bluetooth
    addr_info = bluetooth.read_local_bdaddr()
    if addr_info and len(addr_info) > 0:
        _CACHED_LOCAL_MAC_VALUE = convert_mac_string_to_value(addr_info[0])
        return _CACHED_LOCAL_MAC_VALUE
    raise RuntimeError("No local Bluetooth adapter found or Bluetooth is disabled.")

def get_stick_xy(data: bytes):
    """Convert 3 bytes containing stick x y values into these values"""
    value = decodeu(data)
    x = value & 0xFFF
    y = value >> 12

    return x, y

def signed_looping_difference_16bit(a, b):
    diff = (b - a) % 65536
    return diff - 65536 if diff > 32768 else diff

def apply_calibration_to_axis(raw_value, center, max_abs, min_abs):
    signed_value = raw_value - center
    if signed_value >= 0:
        return min(signed_value / max(max_abs, 1), 1.0)
    return -min(-signed_value / max(min_abs, 1), 1.0)

def apply_radial_deadzone(x, y, deadzone):
    magnitude = math.sqrt(x * x + y * y)
    if magnitude < deadzone:
        return 0.0, 0.0
    return x, y

def press_or_release_mouse_button(state: bool, prev_state: bool, button: int, mouse_x: int, mouse_y):
    if (state and not prev_state):
        win32api.mouse_event(button, mouse_x, mouse_y, 0, 0)
    if (not state and prev_state):
        win32api.mouse_event(button << 1, mouse_x, mouse_y, 0, 0)

def reverse_bits(n: int, no_of_bits: int):
    result = 0
    for i in range(no_of_bits):
        result <<= 1
        result |= n & 1
        n >>= 1
    return result

def vector_normalize(v):
    mag = math.sqrt(sum(x*x for x in v))
    if mag == 0: return (0, 0, 0)
    return tuple(x/mag for x in v)

def vector_cross(a, b):
    return (
        a[1]*b[2] - a[2]*b[1],
        a[2]*b[0] - a[0]*b[2],
        a[0]*b[1] - a[1]*b[0]
    )

def vector_dot(a, b):
    return sum(x*y for x, y in zip(a, b))

def quaternion_multiply(q, p):
    w1, x1, y1, z1 = q
    w2, x2, y2, z2 = p
    return (
        w1*w2 - x1*x2 - y1*y2 - z1*z2,
        w1*x2 + x1*w2 + y1*z2 - z1*y2,
        w1*y2 - x1*z2 + y1*w2 + z1*x2,
        w1*z2 + x1*y2 - y1*x2 + z1*w2
    )

def quaternion_normalize(q):
    mag = math.sqrt(sum(x*x for x in q))
    if mag == 0: return (1, 0, 0, 0)
    return tuple(x/mag for x in q)

def quaternion_rotate_vector(q, v):
    qv = (0, v[0], v[1], v[2])
    q_inv = (q[0], -q[1], -q[2], -q[3])
    res = quaternion_multiply(quaternion_multiply(q, qv), q_inv)
    return (res[1], res[2], res[3])

_IS_PACKAGED_CACHE = None


def _package_identity_result_has_identity(result):
    """Interpret the first GetCurrentPackageFullName probe conservatively."""
    # With a zero-length output buffer, ERROR_INSUFFICIENT_BUFFER is the only
    # success-shaped result proving that a package full name exists. Treating
    # access errors or other transient failures as package identity made normal
    # one-file executables enter the MSIX-only driver fallback path.
    return int(result) == 122

def is_packaged():
    """Return True when running from inside an MSIX package (has package identity).

    MSIX-packaged builds behave differently from the standalone GitHub .exe:
    driver installation is delegated to external download+install, and startup
    uses a StartupTask instead of the HKCU\\Run registry value (which is
    virtualized and ineffective inside the package).
    """
    global _IS_PACKAGED_CACHE
    if _IS_PACKAGED_CACHE is not None:
        return _IS_PACKAGED_CACHE
    packaged = False
    try:
        import ctypes
        from ctypes import wintypes
        length = ctypes.c_uint32(0)
        # ERROR_INSUFFICIENT_BUFFER (122) means we have identity;
        # APPMODEL_ERROR_NO_PACKAGE (15700), and every other error, do not prove
        # that this process has package identity.
        rc = ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(length), None)
        packaged = _package_identity_result_has_identity(rc)
    except Exception:
        packaged = False
    _IS_PACKAGED_CACHE = packaged
    return packaged

def _set_startup_task(enabled: bool):
    """Enable/disable startup for MSIX-packaged builds via the manifest StartupTask.

    The TaskId must match the desktop:StartupTask Id declared in AppxManifest.xml.
    """
    try:
        from winrt.windows.applicationmodel import StartupTask, StartupTaskState
        task = StartupTask.get_async("Switch2ConnectStartup").get()
        if enabled:
            if task.state in (StartupTaskState.DISABLED, StartupTaskState.DISABLED_BY_USER):
                task.request_enable_async().get()
        else:
            if task.state == StartupTaskState.ENABLED:
                task.disable()
        return True
    except Exception as e:
        print(f"Error setting MSIX StartupTask: {e}")
        return False

def set_startup(enabled: bool):
    if is_packaged():
        return _set_startup_task(enabled)

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    app_name = "Switch 2 Connect"
    legacy_app_name = "Switch2Controllers"

    if hasattr(sys, 'frozen'):
        # Executable path
        app_path = sys.executable
    else:
        # Python script path
        app_path = f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}"'

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, app_path)
            try:
                winreg.DeleteValue(key, legacy_app_name)
            except FileNotFoundError:
                pass
        else:
            for value_name in (app_name, legacy_app_name):
                try:
                    winreg.DeleteValue(key, value_name)
                except FileNotFoundError:
                    pass
        winreg.CloseKey(key)
        return True
    except Exception as e:
        print(f"Error setting startup: {e}")
        return False

show_notification_callback = None

def show_notification(title, message):
    global show_notification_callback
    if show_notification_callback is not None:
        show_notification_callback(title, message)
    else:
        # Fallback to console print
        print(f"[{title}] {message}", flush=True)

force_ui_update_callback = None

def force_ui_update():
    global force_ui_update_callback
    if force_ui_update_callback is not None:
        force_ui_update_callback()

joystick_calibration_callback = None

def trigger_joystick_calibration(virtual_controller):
    global joystick_calibration_callback
    if joystick_calibration_callback is not None:
        joystick_calibration_callback(virtual_controller)

joystick_calibration_cancel_callback = None

def cancel_joystick_calibration(virtual_controller):
    global joystick_calibration_cancel_callback
    if joystick_calibration_cancel_callback is not None:
        joystick_calibration_cancel_callback(virtual_controller)

def disable_power_throttling():
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
            _fields_ = [
                ("Version", wintypes.DWORD),
                ("ControlMask", wintypes.DWORD),
                ("StateMask", wintypes.DWORD),
            ]

        PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
        PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
        ProcessPowerThrottling = 4

        kernel32 = ctypes.windll.kernel32
        GetCurrentProcess = kernel32.GetCurrentProcess
        SetProcessInformation = getattr(kernel32, 'SetProcessInformation', None)
        if not SetProcessInformation:
            return False
            
        state = PROCESS_POWER_THROTTLING_STATE()
        state.Version = PROCESS_POWER_THROTTLING_CURRENT_VERSION
        state.ControlMask = PROCESS_POWER_THROTTLING_EXECUTION_SPEED
        state.StateMask = 0  # 0 means turn off throttling for EXECUTION_SPEED
        
        res = SetProcessInformation(
            GetCurrentProcess(),
            ProcessPowerThrottling,
            ctypes.byref(state),
            ctypes.sizeof(state)
        )
        return res != 0
    except Exception:
        return False
change_profile_callback = None
def trigger_change_profile():
    if change_profile_callback:
        change_profile_callback()

# Manual Change Profile selection mode. While active, controllers suppress their
# virtual output and route stick/Dpad/A/B to the navigation callbacks below.
profile_selection_active = False
profile_nav_callback = None      # fn(direction): -1 = back, +1 = forward
profile_confirm_callback = None
profile_cancel_callback = None

def profile_nav(direction):
    if profile_nav_callback:
        profile_nav_callback(direction)

def profile_confirm():
    if profile_confirm_callback:
        profile_confirm_callback()

def profile_cancel():
    if profile_cancel_callback:
        profile_cancel_callback()

switch_profile_callback = None
def trigger_switch_profile(profile_name):
    if switch_profile_callback:
        switch_profile_callback(profile_name)

profile_combo_record_callback = None
def record_profile_combo_controller_buttons(btn_states):
    if profile_combo_record_callback:
        profile_combo_record_callback(btn_states)
