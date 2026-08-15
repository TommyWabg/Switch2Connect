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

"""Wired USB support for the Switch 2 Pro Controller 2 and the NSO GameCube Controller.

A physically-connected pad (USB, VID 0x057E / PID 0x2069 Pro Controller 2 or
PID 0x2073 NSO GameCube Controller) is adapted
into the existing ``Controller`` pipeline the same way the ESP32-S3 serial bridge is
(see ``usb_serial_bridge.ESP32S3Controller``): we subclass ``Controller`` and provide
a small mock "client" that speaks hidapi instead of BLE/GATT.  Because the mock client
exposes the same ``services`` / ``start_notify`` / ``write_gatt_char`` surface the base
class already uses, the whole input pipeline — button/stick parsing, gyro fusion, mouse
emulation, calibration, rumble — is reused unchanged.

Only two things genuinely differ from BLE:

* The SW2 command header carries a *transport* byte (``commands.md`` header offset 2):
  ``0x00`` for USB vs ``0x01`` for Bluetooth.  ``write_command`` is overridden for that.
* Input/commands travel as USB HID reports (report IDs from ``hid_reports.md``) instead
  of GATT notifications, handled by the ``_UsbHidClient`` shim.

Windows note: the vendor command interface (MI_01) is reached through Windows' own
inbox WinUSB driver, which the controller's Microsoft OS descriptor normally makes
Windows bind automatically -- no Zadig, no third-party driver.  Three routes are
tried in order: native WinUSB, pyusb/libusb, then HID output reports on interface 0.

If all three fail the pad never leaves its power-on input mode.  That degrades
rather than dies: the GameCube path re-encodes the power-on report (0x05) into the
same layout its native report (0x0A) produces, so buttons, sticks and both analog
triggers keep working; only motion data is lost.  ``input_transport`` says which
mode is actually live, and it is set from the report id that arrived -- never
inferred from a command write having succeeded.

GameCube rumble follows the same transport priority as startup: a verified vendor
interface is tried first, then a failed or unavailable interface 1 falls back to HID
output report 0x03 on interface 0.  The HID encoding is confirmed from a wire-level
capture of the official console. Pro Controller 2 rumble remains on HID report 0x02.

GameCube notes: the NSO GameCube Controller speaks the very same command framing and
differs only in (a) its input report id (0x0A instead of 0x09), (b) its output report
id (0x03 instead of 0x02), and (c) rumble, which is a plain on/off command rather than
HD rumble frames.  Report 0x0A's payload is byte-for-byte the layout the Bluetooth
GameCube path already parses (``ControllerInputData``'s GameCube branch), so wiring it
up is a transport concern only -- no new parsing.
"""

import logging
import threading
import time
import asyncio
import os
import json
from collections import deque
import timer_resolution
import power_saving

from config import CONFIG
from controller import (
    Controller,
    ControllerInfo,
    StickCalibrationData,
    normalize_calibration_key,
    get_calibration_entry,
    ensure_wired_controller_calibration_alias,
    INPUT_REPORT_UUID,
    COMMAND_RESPONSE_UUID,
    VIBRATION_WRITE_PRO_CONTROLLER_UUID,
    COMMAND_WRITE_UUID,
    NINTENDO_VENDOR_ID,
    PRO_CONTROLLER2_PID,
    NSO_GAMECUBE_CONTROLLER_PID,
    make_fixed_stick_calibration,
)
from usb_serial_bridge import MockService, MockCharacteristic, DEFAULT_STICK_CALIBRATION

logger = logging.getLogger(__name__)

_usb_rumble_trace_dump_lock = threading.Lock()
_USB_RUMBLE_TRACE_FIELDS = (
    "time", "event", "value0", "value1", "value2", "value3")

# SW2 GATT service UUID (used only so initialize()'s SW2-detection branch runs).
SW2_SERVICE_UUID = "ab7de9be-89fe-49ad-828f-118f09df7fd0"

# USB HID report IDs (from switch2_controller_research hid_reports.md report map).
REPORT_ID_COMMON = 0x05          # Input report common to all controllers (default/pre-init)
REPORT_ID_PRO2 = 0x09            # Pro Controller 2 specific input report
REPORT_ID_GAMECUBE = 0x0A        # NSO GameCube specific input report
INPUT_REPORT_IDS = (REPORT_ID_COMMON, REPORT_ID_PRO2, REPORT_ID_GAMECUBE)
OUTPUT_REPORT_ID_PRO2 = 0x02     # Pro Controller 2 output report (commands + rumble)
OUTPUT_REPORT_ID_GAMECUBE = 0x03 # NSO GameCube output report (commands + rumble)
PRO2_OUTPUT_REPORT_BODY_SIZE = 0x2A
# Every SW2 input report is 63 payload bytes + 1 report-id byte, sized to one
# full-speed interrupt packet. A short read is therefore a malformed read, never a
# smaller-but-valid report -- and it must not be zero-padded into a report, because
# zeroed stick bytes decode as a stick held hard bottom-left, not centered.
USB_INPUT_REPORT_SIZE = 64
USB_COMMAND_ENDPOINT_OUT = 0x02
USB_COMMAND_INTERFACE = 1

# Wired pads this module can adopt. Order matters only for logging.
WIRED_USB_PIDS = (PRO_CONTROLLER2_PID, NSO_GAMECUBE_CONTROLLER_PID)

# Command 0x03 / subcommand 0x0D — "initialise USB": activates full input reporting.
# Body mirrors the reference nso-gc-bridge DEFAULT_REPORT_DATA (minus the report-id byte,
# which the shim prepends).
USB_INIT_COMMAND = bytes([0x03, 0x91, 0x00, 0x0D, 0x00, 0x08,
                          0x00, 0x00, 0x01, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])
USB_SET_LED_COMMAND = bytes([0x09, 0x91, 0x00, 0x07, 0x00, 0x08,
                             0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00,
                             0x00, 0x00])
USB_SET_FEATURE_MASK_COMMAND = bytes([0x0C, 0x91, 0x00, 0x02, 0x00, 0x04,
                                      0x00, 0x00, 0x27, 0x00, 0x00, 0x00])
USB_ENABLE_FEATURES_COMMAND = bytes([0x0C, 0x91, 0x00, 0x04, 0x00, 0x04,
                                     0x00, 0x00, 0x27, 0x00, 0x00, 0x00])
USB_SELECT_COMMON_REPORT_COMMAND = bytes([0x03, 0x91, 0x00, 0x0A, 0x00, 0x04,
                                          0x00, 0x00, REPORT_ID_COMMON, 0x00, 0x00, 0x00])
# GameCube keeps its native report 0x0A rather than switching to the common 0x05: only
# 0x0A carries the two analog triggers at the offsets the GameCube parsing branch reads,
# and its payload is identical to the Bluetooth report the app already decodes.
USB_SELECT_GC_REPORT_COMMAND = bytes([0x03, 0x91, 0x00, 0x0A, 0x00, 0x04,
                                      0x00, 0x00, REPORT_ID_GAMECUBE, 0x00, 0x00, 0x00])


def _output_report_id(product_id: int) -> int:
    """HID output report id for a wired pad (hid_reports.md report map)."""
    return (OUTPUT_REPORT_ID_GAMECUBE if product_id == NSO_GAMECUBE_CONTROLLER_PID
            else OUTPUT_REPORT_ID_PRO2)


def _startup_commands(product_id: int) -> tuple:
    """Startup command sequence for a wired pad.

    Identical for both pads except the final report selection: the Pro Controller 2
    switches to the common report 0x05 (its own 0x09 has an undocumented motion block),
    while GameCube stays on 0x0A (documented-equivalent to its Bluetooth report, and the
    only one carrying the analog triggers).

    The 0x27 feature mask is what the official GameCube init uses (buttons + sticks +
    IMU + rumble); it is also what the Pro Controller 2 path here has always sent.
    """
    select = (USB_SELECT_GC_REPORT_COMMAND
              if product_id == NSO_GAMECUBE_CONTROLLER_PID
              else USB_SELECT_COMMON_REPORT_COMMAND)
    return (
        USB_INIT_COMMAND,
        USB_SET_LED_COMMAND,
        USB_SET_FEATURE_MASK_COMMAND,
        USB_ENABLE_FEATURES_COMMAND,
        select,
    )


def _device_label(product_id: int) -> str:
    return ("NSO GameCube Controller" if product_id == NSO_GAMECUBE_CONTROLLER_PID
            else "Pro Controller 2")


_hid_import_warned = False
_pyusb_import_warned = False
_native_winusb_warned = False
# Per-PID, so the Pro Controller 2's "no WinUSB binding" notice doesn't silence the
# GameCube's (and vice versa). Informational only -- pyusb and the HID output-report
# fallback are tried next.
_winusb_binding_warned: set[int] = set()
# True once a client's read loop is streaming input. While set, SET_CONFIGURATION on the
# composite device is off limits: it resets every endpoint, including the interface 0 pipe
# we are in the middle of reading. Only that one call is gated -- writing commands to the
# interface-1 bulk endpoint stays available, because _delayed_reinit() needs it to re-apply
# the startup commands after connect. See _ensure_configuration().
_usb_input_streaming = threading.Event()
_usb_streaming_lock = threading.Lock()
_usb_streaming_refs = 0


def _import_hid():
    """Import the hidapi ('hid') module once, warning loudly if it's missing so a
    silent failure doesn't look like 'controller not detected'."""
    global _hid_import_warned
    try:
        import hid
        return hid
    except Exception as e:
        if not _hid_import_warned:
            _hid_import_warned = True
            logger.warning(
                "Wired USB support disabled: could not import the 'hid' module "
                "(install it with: pip install hidapi). Error: %s", e)
        return None


def _import_pyusb():
    """Import pyusb and resolve a libusb-1.0 backend.

    On Windows pyusb needs an explicit libusb backend; the bundled ``libusb-package``
    provides the DLL and works with WinUSB-bound devices (it reaches the interface via
    the always-present composite USB device, so no custom DeviceInterfaceGUID is needed).
    Returns ``(usb.core, usb.util, backend)`` — backend may be None to let pyusb search.
    """
    global _pyusb_import_warned
    try:
        import usb.core
        import usb.util
    except Exception as e:
        if not _pyusb_import_warned:
            _pyusb_import_warned = True
            logger.warning(
                "Wired USB startup command path unavailable: could not import pyusb "
                "(install it with: pip install pyusb). Error: %s", e)
        return None, None, None
    backend = None
    try:
        import libusb_package
        backend = libusb_package.get_libusb1_backend()
    except Exception:
        try:
            import usb.backend.libusb1
            backend = usb.backend.libusb1.get_backend()
        except Exception:
            backend = None
    return usb.core, usb.util, backend


def _guid_from_string(value: str):
    import ctypes
    import uuid

    guid_value = uuid.UUID(value.strip("{}"))

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    return GUID(
        guid_value.time_low,
        guid_value.time_mid,
        guid_value.time_hi_version,
        (ctypes.c_ubyte * 8)(*guid_value.bytes[8:]),
    )


def _pro2_winusb_interface_guids(product_id: int = PRO_CONTROLLER2_PID) -> list[str]:
    global _winusb_binding_warned
    try:
        import winreg
    except Exception:
        return []

    guids: list[str] = []
    services_seen: list[str] = []
    base_path = (r"SYSTEM\CurrentControlSet\Enum\USB"
                 "\\" + f"VID_{NINTENDO_VENDOR_ID:04X}&PID_{product_id:04X}&MI_01")
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base_path) as base_key:
            index = 0
            while True:
                try:
                    instance = winreg.EnumKey(base_key, index)
                    index += 1
                except OSError:
                    break
                if "SWITCH2EMU" in instance.upper():
                    continue
                try:
                    with winreg.OpenKey(base_key, instance) as instance_key:
                        service = str(winreg.QueryValueEx(instance_key, "Service")[0]).upper()
                    services_seen.append(service or "(none)")
                    if service != "WINUSB":
                        continue
                    with winreg.OpenKey(base_key, instance + r"\Device Parameters") as params_key:
                        value = winreg.QueryValueEx(params_key, "DeviceInterfaceGUIDs")[0]
                except OSError:
                    continue
                if isinstance(value, str):
                    candidates = [value]
                else:
                    candidates = list(value)
                for candidate in candidates:
                    candidate = str(candidate).strip()
                    if candidate and candidate not in guids:
                        guids.append(candidate)
    except OSError:
        pass

    if not guids and product_id not in _winusb_binding_warned:
        _winusb_binding_warned.add(product_id)
        # Informational only. The native WinUSB route is one of three ways to reach the
        # vendor command endpoint; pyusb and the HID output-report fallback are tried next,
        # and initialize() reports which one actually won. Drawing a "less reliable"
        # conclusion here was wrong -- it fired even when pyusb went on to work fine.
        if any(s == "WINUSB" for s in services_seen):
            reason = ("bound to WinUSB but no DeviceInterfaceGUIDs registered, so it has "
                      "no device interface to open")
        elif services_seen:
            reason = f"not bound to WinUSB (services found: {services_seen})"
        else:
            reason = "not present in the registry"
        logger.info(
            "Wired USB: native WinUSB route unavailable for %s interface 1 (MI_01) -- %s. "
            "Falling back to pyusb, then to HID output reports.",
            _device_label(product_id), reason)
    return guids


def _winusb_device_paths(interface_guid: str, product_id: int = PRO_CONTROLLER2_PID) -> list[str]:
    import ctypes
    from ctypes import wintypes

    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    guid = _guid_from_string(interface_guid)

    class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("InterfaceClassGuid", type(guid)),
            ("Flags", wintypes.DWORD),
            ("Reserved", ctypes.c_void_p),
        ]

    class SP_DEVICE_INTERFACE_DETAIL_DATA_W(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("DevicePath", wintypes.WCHAR * 1024),
        ]

    DIGCF_PRESENT = 0x00000002
    DIGCF_DEVICEINTERFACE = 0x00000010
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(type(guid)), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD
    ]
    setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(type(guid)), wintypes.DWORD,
        ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)
    ]
    setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
        ctypes.POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA_W), wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p
    ]
    setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]

    info_set = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    )
    if info_set == INVALID_HANDLE_VALUE:
        return []

    paths: list[str] = []
    try:
        index = 0
        while True:
            iface_data = SP_DEVICE_INTERFACE_DATA()
            iface_data.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(info_set, None, ctypes.byref(guid), index, ctypes.byref(iface_data)):
                break
            index += 1
            detail = SP_DEVICE_INTERFACE_DETAIL_DATA_W()
            detail.cbSize = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            required = wintypes.DWORD()
            if setupapi.SetupDiGetDeviceInterfaceDetailW(
                info_set, ctypes.byref(iface_data), ctypes.byref(detail), ctypes.sizeof(detail),
                ctypes.byref(required), None
            ):
                path = detail.DevicePath
                needle = f"vid_{NINTENDO_VENDOR_ID:04x}&pid_{product_id:04x}&mi_01"
                if needle in path.lower() and "switch2emu" not in path.lower():
                    paths.append(path)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(info_set)
    return paths


def _write_commands_native_winusb(commands, product_id: int = PRO_CONTROLLER2_PID,
                                  transport=None) -> bool:
    global _native_winusb_warned
    if os.name != "nt":
        return False

    import ctypes
    from ctypes import wintypes

    if transport is not None and transport.winusb_paths:
        # Bound to one physical pad: never touch any other device's interface.
        paths = list(transport.winusb_paths)
    else:
        paths = []
        for guid in _pro2_winusb_interface_guids(product_id):
            paths.extend(_winusb_device_paths(guid, product_id))
    if not paths:
        return False

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    winusb = ctypes.WinDLL("winusb", use_last_error=True)

    class USB_INTERFACE_DESCRIPTOR(ctypes.Structure):
        _fields_ = [
            ("bLength", ctypes.c_ubyte),
            ("bDescriptorType", ctypes.c_ubyte),
            ("bInterfaceNumber", ctypes.c_ubyte),
            ("bAlternateSetting", ctypes.c_ubyte),
            ("bNumEndpoints", ctypes.c_ubyte),
            ("bInterfaceClass", ctypes.c_ubyte),
            ("bInterfaceSubClass", ctypes.c_ubyte),
            ("bInterfaceProtocol", ctypes.c_ubyte),
            ("iInterface", ctypes.c_ubyte),
        ]

    class WINUSB_PIPE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PipeType", ctypes.c_int),
            ("PipeId", ctypes.c_ubyte),
            ("MaximumPacketSize", ctypes.c_ushort),
            ("Interval", ctypes.c_ubyte),
        ]

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    FILE_FLAG_OVERLAPPED = 0x40000000
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    USB_ENDPOINT_DIRECTION_MASK = 0x80
    USBD_PIPE_TYPE_BULK = 2

    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    winusb.WinUsb_Initialize.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE)]
    winusb.WinUsb_Initialize.restype = wintypes.BOOL
    winusb.WinUsb_QueryInterfaceSettings.argtypes = [
        wintypes.HANDLE, ctypes.c_ubyte, ctypes.POINTER(USB_INTERFACE_DESCRIPTOR)
    ]
    winusb.WinUsb_QueryInterfaceSettings.restype = wintypes.BOOL
    winusb.WinUsb_QueryPipe.argtypes = [
        wintypes.HANDLE, ctypes.c_ubyte, ctypes.c_ubyte, ctypes.POINTER(WINUSB_PIPE_INFORMATION)
    ]
    winusb.WinUsb_QueryPipe.restype = wintypes.BOOL
    winusb.WinUsb_WritePipe.argtypes = [
        wintypes.HANDLE, ctypes.c_ubyte, ctypes.POINTER(ctypes.c_ubyte), wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG), ctypes.c_void_p
    ]
    winusb.WinUsb_WritePipe.restype = wintypes.BOOL
    winusb.WinUsb_Free.argtypes = [wintypes.HANDLE]

    for path in paths:
        file_handle = kernel32.CreateFileW(
            path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
            None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OVERLAPPED, None
        )
        if file_handle == INVALID_HANDLE_VALUE:
            logger.debug("Native WinUSB CreateFileW failed for %s: %s", path, ctypes.get_last_error())
            continue
        usb_handle = wintypes.HANDLE()
        try:
            if not winusb.WinUsb_Initialize(file_handle, ctypes.byref(usb_handle)):
                continue
            descriptor = USB_INTERFACE_DESCRIPTOR()
            if not winusb.WinUsb_QueryInterfaceSettings(usb_handle, 0, ctypes.byref(descriptor)):
                continue
            endpoint_out = None
            for idx in range(descriptor.bNumEndpoints):
                pipe = WINUSB_PIPE_INFORMATION()
                if not winusb.WinUsb_QueryPipe(usb_handle, 0, idx, ctypes.byref(pipe)):
                    continue
                if pipe.PipeType == USBD_PIPE_TYPE_BULK and (pipe.PipeId & USB_ENDPOINT_DIRECTION_MASK) == 0:
                    endpoint_out = pipe.PipeId
                    break
            if endpoint_out is None:
                endpoint_out = USB_COMMAND_ENDPOINT_OUT
            for command in commands:
                buffer = (ctypes.c_ubyte * len(command)).from_buffer_copy(command)
                written = wintypes.ULONG()
                if not winusb.WinUsb_WritePipe(
                    usb_handle, endpoint_out, buffer, len(command), ctypes.byref(written), None
                ):
                    raise OSError(ctypes.get_last_error(), "WinUsb_WritePipe failed")
                time.sleep(0.02)
            logger.info("Wired USB %s commands sent via native WinUSB",
                        _device_label(product_id))
            return True
        except Exception as e:
            if not _native_winusb_warned:
                _native_winusb_warned = True
                logger.warning("Native WinUSB startup command path failed: %s", e)
        finally:
            if usb_handle:
                try:
                    winusb.WinUsb_Free(usb_handle)
                except Exception:
                    pass
            kernel32.CloseHandle(file_handle)
    return False


def _write_startup_reports_native_winusb(product_id: int = PRO_CONTROLLER2_PID,
                                         transport=None) -> bool:
    return _write_commands_native_winusb(
        _startup_commands(product_id), product_id, transport)


class UsbCommandTransport:
    """The vendor command interface (MI_01) of ONE physical pad.

    Without this, every command route re-searched by VID/PID and took the first
    match: with two same-PID pads connected, each controller instance would
    initialise -- and rumble -- whichever one Windows happened to enumerate first,
    leaving the second stuck on its power-on report with rumble crosstalk.

    ``winusb_paths`` empty means resolution failed; callers then fall back to the
    old global behaviour, which is still correct for the single-pad case.
    """

    __slots__ = ("product_id", "hid_path", "winusb_paths", "mi01_instance_id",
                 "hid_instance_id")

    def __init__(self, product_id, hid_path=None, winusb_paths=None,
                 mi01_instance_id=None, hid_instance_id=None):
        self.product_id = int(product_id)
        self.hid_path = hid_path
        self.winusb_paths = list(winusb_paths or [])
        self.mi01_instance_id = mi01_instance_id
        self.hid_instance_id = hid_instance_id

    @property
    def is_bound(self) -> bool:
        """True when this transport is known to address one specific pad."""
        return bool(self.winusb_paths)

    def describe(self) -> str:
        if self.is_bound:
            return f"bound(mi01={self.mi01_instance_id})"
        return f"unbound(hid={self.hid_instance_id or '?'})"


def winusb_binding_state(product_id: int) -> dict:
    """What driver, if any, Windows bound to this pad's vendor interface (MI_01).

    Returns a dict with:
      ``state``      -- "bound" | "unbound" | "absent" | "unknown"
      ``service``    -- the bound service name, e.g. "WINUSB"
      ``ms_comp``    -- True when the device reports the MS_COMP_WINUSB compatible id
      ``has_guids``  -- whether DeviceInterfaceGUIDs is registered

    ``has_guids`` is reported for diagnostics only and must NOT be used to decide
    whether WinUSB works: Windows' inbox winusb.inf registers no interface GUIDs, so
    a perfectly bound MS_COMP_WINUSB device has none. libusb reaches the interface
    anyway, through the composite device.

    ``ms_comp`` is the useful signal when nothing is bound: a device that advertises
    MS_COMP_WINUSB will be bound automatically once Windows finishes installing, so
    "unbound + ms_comp" means "wait or replug", while "unbound + no ms_comp" means
    the binding will never happen on its own.
    """
    result = {"state": "unknown", "service": None, "ms_comp": False,
              "has_guids": False, "instances": 0}
    if os.name != "nt":
        return result
    try:
        import winreg
    except Exception:
        return result

    base = (r"SYSTEM\CurrentControlSet\Enum\USB"
            "\\" + f"VID_{NINTENDO_VENDOR_ID:04X}&PID_{product_id:04X}&MI_01")
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as key:
            index = 0
            while True:
                try:
                    instance = winreg.EnumKey(key, index)
                    index += 1
                except OSError:
                    break
                if "SWITCH2EMU" in instance.upper():
                    continue
                result["instances"] += 1
                try:
                    with winreg.OpenKey(key, instance) as inst_key:
                        try:
                            service = str(winreg.QueryValueEx(inst_key, "Service")[0])
                        except OSError:
                            service = ""
                        try:
                            compat = winreg.QueryValueEx(inst_key, "CompatibleIDs")[0]
                        except OSError:
                            compat = []
                except OSError:
                    continue
                if service:
                    result["service"] = service
                if any("MS_COMP_WINUSB" in str(c).upper() for c in (compat or ())):
                    result["ms_comp"] = True
                try:
                    with winreg.OpenKey(key, instance + r"\Device Parameters") as params:
                        winreg.QueryValueEx(params, "DeviceInterfaceGUIDs")
                    result["has_guids"] = True
                except OSError:
                    pass
    except FileNotFoundError:
        result["state"] = "absent"
        return result
    except OSError:
        return result

    if not result["instances"]:
        result["state"] = "absent"
    elif (result["service"] or "").upper() == "WINUSB":
        result["state"] = "bound"
    else:
        result["state"] = "unbound"
    return result


def _instance_id_to_path_fragment(instance_id: str) -> str:
    r"""``USB\VID_057E&PID_2073&MI_01\7&abc&0&0001`` -> ``usb#vid_057e&…#7&abc&0&0001``.

    The inverse of ``hidhide.hid_path_to_instance_id``: a device interface path
    embeds the instance id with ``#`` separators, so this fragment identifies one
    specific interface inside a WinUSB device path.
    """
    return str(instance_id).replace("\\", "#").lower()


def resolve_command_transport(hid_path, product_id: int) -> UsbCommandTransport:
    """Bind the MI_01 vendor interface belonging to the pad behind ``hid_path``.

    The HID path and the WinUSB path cannot be correlated by string surgery: they
    carry different instance components (``8&…`` assigned by the HID stack vs
    ``7&…`` by the USB hub). The composite device that owns both is the only thing
    they share, so we walk the PnP tree to it:

        HID\\…&MI_00\\…            (from the hidapi path)
          -> USB\\…&MI_00\\…       parent
          -> USB\\VID&PID\\…       parent  (the composite device)
          -> children              pick the one whose id contains &MI_01

    Any failure returns an unbound transport rather than raising; the caller keeps
    the previous global behaviour, which is still right when only one pad is
    connected.
    """
    transport = UsbCommandTransport(product_id, hid_path=hid_path)
    if os.name != "nt" or hid_path is None:
        return transport

    try:
        import hidhide
        import pnp_cfgmgr
    except Exception:
        logger.debug("Wired USB: transport resolution unavailable (imports)", exc_info=True)
        return transport

    try:
        hid_instance = hidhide.hid_path_to_instance_id(hid_path)
        transport.hid_instance_id = hid_instance
        if not hid_instance:
            return transport

        usb_interface = pnp_cfgmgr.parent_instance_id(hid_instance)
        composite = pnp_cfgmgr.parent_instance_id(usb_interface) if usb_interface else None
        if not composite:
            logger.debug("Wired USB: no composite parent for %s", hid_instance)
            return transport

        children = pnp_cfgmgr.child_instance_ids(composite) or []
        mi01 = next((c for c in children if "&mi_01" in c.lower()), None)
        if not mi01:
            logger.debug("Wired USB: composite %s exposes no MI_01 (children=%s)",
                         composite, children)
            return transport
        transport.mi01_instance_id = mi01

        fragment = _instance_id_to_path_fragment(mi01)
        for guid in _pro2_winusb_interface_guids(product_id):
            for path in _winusb_device_paths(guid, product_id):
                if fragment in path.lower():
                    transport.winusb_paths.append(path)
        if not transport.winusb_paths:
            logger.debug("Wired USB: no WinUSB interface path matched %s", mi01)
    except Exception:
        logger.debug("Wired USB: transport resolution failed", exc_info=True)

    return transport


def _find_usb_devices(usb_core, backend, product_id, transport=None):
    """Every libusb device matching the pad's VID/PID.

    libusb gives us no instance id, so unlike the WinUSB route this cannot be
    narrowed to one pad. Callers decide what that means: initialisation is
    idempotent and goes to all of them (which is what the reference pairing app
    does, and is strictly better than only ever hitting the first), while rumble
    refuses to guess.
    """
    del transport
    try:
        found = usb_core.find(find_all=True, idVendor=NINTENDO_VENDOR_ID,
                              idProduct=product_id, backend=backend)
        return list(found or [])
    except Exception:
        logger.debug("Wired USB: libusb enumeration failed", exc_info=True)
        return []


def _ensure_configuration(dev) -> None:
    """Set the USB configuration only when the device does not already have one.

    Calling set_configuration() unconditionally is destructive here: interface 0 of this
    composite device is bound to HIDClass and is the interface we stream input from, and a
    SET_CONFIGURATION resets every endpoint on the device. Doing that repeatedly (the
    rumble fallback used to, every 0.5 s) tears down the input stream over and over.

    This is the only libusb operation that is unsafe mid-stream, so it is the only one
    gated on _usb_input_streaming. Writing to the interface-1 bulk endpoint is fine and
    must stay available -- _delayed_reinit() depends on it to re-apply the startup
    commands a second after connect.
    """
    try:
        if dev.get_active_configuration() is not None:
            return
    except Exception:
        pass
    if _usb_input_streaming.is_set():
        logger.debug("Wired USB: skipping set_configuration while input is streaming")
        return
    try:
        dev.set_configuration()
    except Exception:
        logger.debug("Wired USB set_configuration failed", exc_info=True)


def _write_commands_pyusb(dev, usb_util, commands, label) -> bool:
    """Write a command sequence to one libusb device's vendor bulk endpoint."""
    claimed = False
    try:
        _ensure_configuration(dev)
        try:
            usb_util.claim_interface(dev, USB_COMMAND_INTERFACE)
            claimed = True
        except Exception:
            logger.debug("Wired USB: claim interface %d failed; trying writes anyway",
                         USB_COMMAND_INTERFACE, exc_info=True)

        endpoint_out = USB_COMMAND_ENDPOINT_OUT
        try:
            cfg = dev.get_active_configuration()
            interface = cfg[(USB_COMMAND_INTERFACE, 0)]
            for endpoint in interface:
                address = int(endpoint.bEndpointAddress)
                attributes = int(endpoint.bmAttributes)
                is_out = (address & 0x80) == 0
                is_bulk = (attributes & 0x03) == 0x02
                if is_out and is_bulk:
                    endpoint_out = address
                    break
        except Exception:
            logger.debug("Wired USB: endpoint scan failed; using 0x%02x",
                         endpoint_out, exc_info=True)

        for command in commands:
            dev.write(endpoint_out, bytes(command), 1000)
            time.sleep(0.02)
        logger.debug("Wired USB %s commands sent on endpoint 0x%02x", label, endpoint_out)
        return True
    except Exception as e:
        logger.debug("Wired USB %s command write failed: %s", label, e)
        return False
    finally:
        if claimed:
            try:
                usb_util.release_interface(dev, USB_COMMAND_INTERFACE)
            except Exception:
                pass
        try:
            usb_util.dispose_resources(dev)
        except Exception:
            pass


def send_pro_controller2_usb_command(command: bytes,
                                     product_id: int = PRO_CONTROLLER2_PID,
                                     transport=None,
                                     require_unique: bool = False) -> bool:
    """Send one command to the pad's vendor interface.

    ``require_unique`` refuses the libusb route when more than one same-PID pad is
    present and the transport is not bound to a specific one. Rumble sets it: making
    the wrong controller buzz is worse than not buzzing at all. Initialisation
    leaves it False, because those commands are idempotent and every connected pad
    needs them anyway.
    """
    if _write_commands_native_winusb((bytes(command),), product_id, transport):
        return True

    usb_core, usb_util, backend = _import_pyusb()
    if usb_core is None or usb_util is None:
        return False

    devices = _find_usb_devices(usb_core, backend, product_id, transport)
    if not devices:
        return False
    if require_unique and len(devices) > 1:
        logger.warning(
            "Wired USB: %d %s devices present and none is bound to this controller; "
            "refusing to send a per-device command that could reach the wrong pad.",
            len(devices), _device_label(product_id))
        return False

    label = _device_label(product_id)
    return any(_write_commands_pyusb(dev, usb_util, (command,), label) for dev in devices)


def initialize_pro_controller2_usb_reports(product_id: int = PRO_CONTROLLER2_PID,
                                           transport=None) -> str:
    """Send the startup commands required before a wired pad streams USB input.

    Returns the transport that delivered them -- "native_winusb", "pyusb", or "" when none
    did. Truthiness matches the old bool return, and the name lets the caller re-apply the
    commands later over the same route instead of blindly retrying one that cannot work on
    this machine.
    """
    label = _device_label(product_id)
    if _write_startup_reports_native_winusb(product_id, transport):
        return "native_winusb"

    usb_core, usb_util, backend = _import_pyusb()
    if usb_core is None or usb_util is None:
        return ""

    devices = _find_usb_devices(usb_core, backend, product_id, transport)
    if not devices:
        logger.debug("Wired USB init: %s USB device not found", label)
        return ""

    # Initialisation goes to every matching device: the commands are idempotent, and
    # sending to only the first one is what left a second same-PID pad stuck on its
    # power-on report.
    commands = _startup_commands(product_id)
    sent = sum(1 for dev in devices
               if _write_commands_pyusb(dev, usb_util, commands, label))
    if not sent:
        logger.warning("Wired USB %s startup commands failed on all %d device(s)",
                       label, len(devices))
        return ""
    logger.info("Wired USB %s startup commands sent to %d of %d device(s) via libusb",
                label, sent, len(devices))
    return "pyusb"


def _limit_frame_amp_sum(frame: bytes, limit: int = 511) -> bytes:
    """Enforce lf_amp + hf_amp <= limit on one 5-byte HD-rumble frame, scaling BOTH
    amplitudes down proportionally (weighted) so their low/high ratio is preserved.
    Frequency and tone bits are untouched. Frame layout:
    Byte 0: hf_amp[0:7]
    Byte 1: hf_amp[8] (bit 0), hf_freq[0:6] (bits 1-7)
    Byte 2: lf_amp[0:7]
    Byte 3: lf_amp[8] (bit 0), lf_freq[0:6] (bits 1-7)
    Byte 4: padding"""
    if len(frame) != 5:
        return frame
    
    hf_amp = frame[0] + ((frame[1] & 0x01) << 8)
    lf_amp = frame[2] + ((frame[3] & 0x01) << 8)
    
    total = lf_amp + hf_amp
    if total <= limit:
        return frame
        
    nhf = hf_amp * limit // total
    nlf = lf_amp * limit // total
    
    out = bytearray(frame)
    out[0] = nhf & 0xFF
    out[1] = (out[1] & 0xFE) | ((nhf >> 8) & 0x01)
    out[2] = nlf & 0xFF
    out[3] = (out[3] & 0xFE) | ((nlf >> 8) & 0x01)
    
    return bytes(out)


def _limit_combined_amp_sum(frame_l: bytes, frame_r: bytes, limit: int = 300) -> tuple[bytes, bytes]:
    """Enforce total amplitude (left + right) <= limit across two frames to prevent USB power surges."""
    if len(frame_l) != 5 or len(frame_r) != 5:
        return frame_l, frame_r
        
    hf_amp_l = frame_l[0] + ((frame_l[1] & 0x01) << 8)
    lf_amp_l = frame_l[2] + ((frame_l[3] & 0x01) << 8)
    hf_amp_r = frame_r[0] + ((frame_r[1] & 0x01) << 8)
    lf_amp_r = frame_r[2] + ((frame_r[3] & 0x01) << 8)
    
    total = lf_amp_l + hf_amp_l + lf_amp_r + hf_amp_r
    if total <= limit:
        return frame_l, frame_r
        
    nhf_l = hf_amp_l * limit // total
    nlf_l = lf_amp_l * limit // total
    nhf_r = hf_amp_r * limit // total
    nlf_r = lf_amp_r * limit // total
    
    out_l = bytearray(frame_l)
    out_l[0] = nhf_l & 0xFF
    out_l[1] = (out_l[1] & 0xFE) | ((nhf_l >> 8) & 0x01)
    out_l[2] = nlf_l & 0xFF
    out_l[3] = (out_l[3] & 0xFE) | ((nlf_l >> 8) & 0x01)
    
    out_r = bytearray(frame_r)
    out_r[0] = nhf_r & 0xFF
    out_r[1] = (out_r[1] & 0xFE) | ((nhf_r >> 8) & 0x01)
    out_r[2] = nlf_r & 0xFF
    out_r[3] = (out_r[3] & 0xFE) | ((nlf_r >> 8) & 0x01)
    
    return bytes(out_l), bytes(out_r)


def _pro2_usb_output_body(data: bytes, is_audio_active: bool = False) -> bytes:
    """Return a Pro Controller 2 USB output-report body in hid_reports.md order.

    ``set_vibration`` emits ``0x00 + LEFT(16) + RIGHT(16)`` (controller.py:1908-1914),
    which already matches Output Report 0x02 (hid_reports.md: 0x1=Left LRA, 0x11=Right
    LRA).  So we only strip the leading Bluetooth report-id byte (0x00) and keep the
    left-then-right order intact.  (Earlier code swapped the two 16-byte blocks, which
    mirrored stereo audio haptics onto the wrong actuator.)

    WIRED-ONLY rule: each frame's combined amplitude (traditional rumble + audio haptic
    + adaptive trigger, already merged into these frames by ``set_vibration``) must not
    exceed 511; over-limit frames are scaled down proportionally. This builder is used
    ONLY for the wired Pro Controller 2, so Bluetooth output strength is never touched."""
    payload = bytes(data)
    if len(payload) >= 33 and payload[0] == 0x00:
        payload = payload[1:]
    if len(payload) >= 32:
        buf = bytearray(payload)
        # Three 5-byte frames per 16-byte block: L @ 1/6/11, R @ 17/22/27.
        for slot in range(3):
            off_l = 1 + slot * 5
            off_r = 17 + slot * 5
            
            # Step 1: hardware limit 511 per motor
            frame_l = _limit_frame_amp_sum(bytes(buf[off_l:off_l + 5]), limit=511)
            frame_r = _limit_frame_amp_sum(bytes(buf[off_r:off_r + 5]), limit=511)
            
            # Step 2: global combined limit 800 to prevent USB power surges (unlocked for Type-C)
            if is_audio_active:
                frame_l, frame_r = _limit_combined_amp_sum(frame_l, frame_r, limit=800)
            
            buf[off_l:off_l + 5] = frame_l
            buf[off_r:off_r + 5] = frame_r
            
        payload = bytes(buf)
    return payload.ljust(PRO2_OUTPUT_REPORT_BODY_SIZE, b"\x00")


def _pro2_usb_vibration_command(data: bytes) -> bytes:
    payload = bytes(data)
    if len(payload) >= 33 and payload[0] == 0x00:
        left = payload[1:17]
    else:
        left = bytes(16)
    # 0x0A (Command), 0x91, 0x00 (USB transport), 0x08 (Send vibration data), 0x00, 0x14 (Length)
    # Payload: 0x01 + 16 bytes of Left HD Rumble + 3 bytes padding
    return bytes([0x0A, 0x91, 0x00, 0x08, 0x00, 0x14, 0x00, 0x00, 0x01]) + left + bytes([0x00, 0x00, 0x00])


def _pro2_vibration_sample_command(data: bytes) -> bytes:
    # Basic dummy/keepalive sample command if physical controller needs it
    return bytes([0x0A, 0x91, 0x00, 0x02, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])


def _gc_usb_rumble_command(data: bytes) -> bytes | None:
    """Convert the Bluetooth GameCube rumble command into its USB form.

    ``Controller.set_vibration``'s GameCube branch (controller.py) already emits
    ``0A 91 01 02 00 04 00 00 <on> 00 00 00`` -- command 0x0A / subcommand 0x02, the
    on/off vibration command. The only USB difference is the transport byte at header
    offset 2 (commands.md): 0x01 Bluetooth -> 0x00 USB.

    Returns None when ``data`` is not that command, so a stray payload can't be sent.
    """
    payload = bytes(data)
    if len(payload) < 12 or payload[0] != 0x0A or payload[1] != 0x91 or payload[3] != 0x02:
        return None
    out = bytearray(payload)
    out[2] = 0x00
    return bytes(out)


def _gc_rumble_payload_is_active(data: bytes) -> bool:
    """True when a GameCube on/off vibration command turns the motor on."""
    payload = bytes(data)
    return len(payload) > 8 and payload[8] != 0x00


# The four "GameCube Rumble Data" bytes of output report 0x03, taken verbatim from the
# official console capture (reference/switch2_controller_research-master/captures/usb/
# rumble-procon-gccon.pcapng.gz, a wire-level LINKTYPE_USB_2_0 recording).
#
# Every rumble report the console sent to the GameCube pad went to the HID interrupt
# OUT endpoint 0x01 as report 0x03 -- never to the vendor bulk endpoint. hid_reports.md
# documents the field only as "packed rumble data"; these are the observed values:
#
#     03 50 02 00 00     03 61 00 01 00     03 5b 00 00 00   <- last one, motor stops
#     03 55 02 00 00     03 63 00 01 00
#     03 5a 02 00 00     03 66 00 01 00
#                        03 68 00 01 00
#
# Byte 0's low nibble increments strictly across the capture (0,1,3,5,6,8,a,b), i.e. a
# packet counter -- the same construction the Pro Controller 2 uses for its own report
# (0x50 + packet id), which is independently confirmed in the same capture. The high
# nibble and bytes 1-2 carry the rumble state: two non-zero states were exercised, and
# all-zero is the stop the console sent at the end of the test.
GC_RUMBLE_STATE_OFF = (0x50, 0x00, 0x00, 0x00)
GC_RUMBLE_STATE_ON = (0x60, 0x00, 0x01, 0x00)
# The other non-zero state the console used. Kept documented so it can be swapped in
# with a one-line change if hardware testing shows it is the stronger/correct one.
GC_RUMBLE_STATE_ALT = (0x50, 0x02, 0x00, 0x00)


def _gc_native_rumble_body(active: bool, counter: int) -> bytes:
    """Body of GameCube output report 0x03: 4 rumble bytes, then padding."""
    base, b1, b2, b3 = GC_RUMBLE_STATE_ON if active else GC_RUMBLE_STATE_OFF
    body = bytes([base | (counter & 0x0F), b1, b2, b3])
    return body.ljust(PRO2_OUTPUT_REPORT_BODY_SIZE, b"\x00")


def _pro2_rumble_payload_is_active(data: bytes) -> bool:
    payload = bytes(data)
    if len(payload) < 17 or payload[0] != 0x00:
        return any(payload)

    def segment_active(segment: bytes) -> bool:
        if len(segment) < 16:
            return False
        for start in (1, 6, 11):
            frame = int.from_bytes(segment[start:start + 5], "little")
            if ((frame >> 10) & 0x3FF) or ((frame >> 30) & 0x3FF):
                return True
        return False

    return segment_active(payload[1:17]) or segment_active(payload[17:33])


def _pro2_zero_rumble_payload(data: bytes) -> bytes:
    """Build a neutral Pro 2 frame from an accepted 0x02 rumble body."""
    payload = bytearray(data)
    if len(payload) < 33 or payload[0] != 0x00:
        return bytes(len(payload))
    for segment_start in (1, 17):
        packet_id = ((payload[segment_start] & 0x0F) + 1) & 0x0F
        payload[segment_start] = 0x50 | packet_id
        payload[segment_start + 1:segment_start + 16] = b"\x00" * 15
    return bytes(payload)


_gc_common_report_warned = False


def _gc_translate_common_report(data) -> bytes | None:
    """Translate the NSO common report 0x05 into the GameCube body layout.

    When none of the command routes reach the pad, it never leaves its power-on
    state and keeps streaming the common report 0x05 instead of the GameCube
    report 0x0A.  That report carries the same physical inputs -- including both
    analog triggers -- just in the shared NSO encoding at different offsets.  So
    rather than dropping it (no input at all), re-encode it into the GameCube body
    layout and hand it to the same GameCube parser.  Trigger calibration, the ABXY
    layout option and the GC_L/R_CLICK mapping then all work unchanged.

    ``data`` is the raw report *including* its leading report-id byte.

    Ported from the reference pairing app's ``_translate_report_0x05``, with every
    destination index shifted down by one: that app's parser reads buttons at
    [3][4][5] (report-id byte still attached) while ours reads them at [2][3][4]
    (report-id byte stripped).

    Source encoding (raw offsets), cross-checked against the research docs'
    report-0x05 button table:
        [5]     NSO buttons 0: Y=01 X=02 B=04 A=08 SR=10 SL=20 R=40 ZR=80
        [6]     NSO buttons 1: Minus=01 Plus=02 RStick=04 LStick=08
                               Home=10 Capture=20 C=40
        [7]     NSO buttons 2: Down=01 Up=02 Right=04 Left=08
                               SR=10 SL=20 L=40 ZL=80
        [11:17] sticks, packed 12-bit (same packing as report 0x0A)
        [61]    left analog trigger      [62] right analog trigger

    Motion is left zeroed: the IMU feature bit is only set by the feature-enable
    command, which by definition did not get through if we are on this path.
    """
    if len(data) < USB_INPUT_REPORT_SIZE:
        return None

    out = bytearray(64)

    b0 = data[5]
    b1 = data[6]
    b2 = data[7]

    # GameCube buttons byte 0: B=01 A=02 Y=04 X=08 R(click)=10 Z=20 Start=40
    v = 0
    if b0 & 0x04: v |= 0x01   # B
    if b0 & 0x08: v |= 0x02   # A
    if b0 & 0x01: v |= 0x04   # Y
    if b0 & 0x02: v |= 0x08   # X
    if b0 & 0x40: v |= 0x10   # R  -> GameCube R digital click
    if b0 & 0x80: v |= 0x20   # ZR -> GameCube Z
    if b1 & 0x02: v |= 0x40   # Plus -> Start
    out[2] = v

    # GameCube buttons byte 1: Down=01 Right=02 Left=04 Up=08 L(click)=10 ZL=20
    # The D-pad is a bit permutation, not a copy: NSO orders Down/Up/Right/Left
    # while GameCube orders Down/Right/Left/Up.
    v = 0
    if b2 & 0x01: v |= 0x01   # Down
    if b2 & 0x04: v |= 0x02   # Right
    if b2 & 0x08: v |= 0x04   # Left
    if b2 & 0x02: v |= 0x08   # Up
    if b2 & 0x40: v |= 0x10   # L  -> GameCube L digital click
    if b2 & 0x80: v |= 0x20   # ZL
    out[3] = v

    # GameCube buttons byte 2: Home=01 Capture=02 C=10
    v = 0
    if b1 & 0x10: v |= 0x01   # Home
    if b1 & 0x20: v |= 0x02   # Capture
    if b1 & 0x40: v |= 0x10   # C
    out[4] = v

    # Sticks: identical 12-bit packing in both reports, so a straight byte copy.
    out[5:11] = bytes(data[11:17])

    # Analog triggers, raw. Their resting value differs slightly from report 0x0A
    # (~0x1e/0x24 here vs ~0x24 there), which the existing trigger calibration
    # absorbs.
    out[12] = data[61]
    out[13] = data[62]

    return bytes(out)


def _translate_pro2_usb_report(data) -> bytes | None:
    """v1.7 Pro Controller 2 input translator with no product dispatch."""
    if not data:
        return None
    report_id = data[0]

    if report_id == REPORT_ID_COMMON:
        body = bytes(data[1:])
        if len(body) < 60:
            body = body.ljust(64, b"\x00")
        return body

    if report_id == REPORT_ID_PRO2:
        if len(data) < 12:
            return None
        buf = bytearray(64)
        buf[0] = data[1]
        buttons = _pro2_buttons_to_u32(data[3], data[4], data[5])
        buf[4] = buttons & 0xFF
        buf[5] = (buttons >> 8) & 0xFF
        buf[6] = (buttons >> 16) & 0xFF
        buf[7] = (buttons >> 24) & 0xFF
        buf[10:13] = bytes(data[6:9])
        buf[13:16] = bytes(data[9:12])
        power = data[2]
        level = min((power >> 2) & 0x0F, 9)
        volt_mv = 3100 + level * 110
        buf[31] = volt_mv & 0xFF
        buf[32] = (volt_mv >> 8) & 0xFF
        return bytes(buf)

    return None


def translate_usb_report(data, product_id: int = PRO_CONTROLLER2_PID) -> bytes | None:
    """Translate a raw USB HID input report into the internal buffer layout that
    ``ControllerInputData`` expects for this ``product_id``.

    For everything but GameCube that is the SW2 "report 0x05" layout: counter[0:4],
    buttons u32[4:8], left stick[10:13], right stick[13:16], battery[31:33],
    accel[48:54], gyro[54:60].

    For GameCube it is the GameCube branch's own layout, which report 0x0A already is.

    ``data`` is the report *including* its leading report-id byte (list[int] or bytes).
    Returns ``None`` for reports we don't consume.
    """
    if not data:
        return None
    report_id = data[0]

    if product_id == NSO_GAMECUBE_CONTROLLER_PID:
        if report_id == REPORT_ID_GAMECUBE:
            # Report 0x0A's payload is byte-for-byte what the GameCube branch of
            # ControllerInputData parses off Bluetooth (counter[0], buttons[2:5],
            # sticks[5:11], analog triggers[12:14], motion[34:46]). Dropping the
            # report-id byte is the whole translation.
            if len(data) < USB_INPUT_REPORT_SIZE:
                return None
            return bytes(data[1:])

        if report_id == REPORT_ID_COMMON:
            # The pad stays on the common report when the 0x03/0x0D init did not
            # reach it. Re-encode rather than drop, so basic input (including both
            # analog triggers) keeps working without a usable command endpoint.
            global _gc_common_report_warned
            if not _gc_common_report_warned:
                _gc_common_report_warned = True
                logger.warning(
                    "Wired USB NSO GameCube Controller is streaming the common report "
                    "0x05: the USB init commands did not reach the pad. Falling back to "
                    "translated input -- buttons, sticks, analog triggers and rumble "
                    "work; motion data does not.")
            return _gc_translate_common_report(data)
        return None

    return _translate_pro2_usb_report(data)


def _pro2_buttons_to_u32(b0: int, b1: int, b2: int) -> int:
    """Remap the Pro Controller 2 report-0x09 button bytes (hid_reports.md
    "Button Format 3") into the SW2 uint32 bitmask the app uses (report-0x05 layout)."""
    v = 0
    # Byte 0: RStick, Plus, ZR, R, X, Y, A, B
    if b0 & 0x01: v |= 0x00000004  # B
    if b0 & 0x02: v |= 0x00000008  # A
    if b0 & 0x04: v |= 0x00000001  # Y
    if b0 & 0x08: v |= 0x00000002  # X
    if b0 & 0x10: v |= 0x00000040  # R
    if b0 & 0x20: v |= 0x00000080  # ZR
    if b0 & 0x40: v |= 0x00000200  # Plus
    if b0 & 0x80: v |= 0x00000400  # Right Stick click
    # Byte 1: LStick, Minus, ZL, L, Up, Left, Right, Down
    if b1 & 0x01: v |= 0x00010000  # Down
    if b1 & 0x02: v |= 0x00040000  # Right
    if b1 & 0x04: v |= 0x00080000  # Left
    if b1 & 0x08: v |= 0x00020000  # Up
    if b1 & 0x10: v |= 0x00400000  # L
    if b1 & 0x20: v |= 0x00800000  # ZL
    if b1 & 0x40: v |= 0x00000100  # Minus
    if b1 & 0x80: v |= 0x00000800  # Left Stick click
    # Byte 2: -, -, -, C, GL, GR, Capture, Home
    if b2 & 0x01: v |= 0x00001000  # Home
    if b2 & 0x02: v |= 0x00002000  # Capture
    if b2 & 0x04: v |= 0x01000000  # GR
    if b2 & 0x08: v |= 0x02000000  # GL
    if b2 & 0x10: v |= 0x00004000  # C
    return v


class _UsbHidClient:
    """Minimal hidapi-backed stand-in for a Bleak client, matching the small surface
    the base ``Controller`` uses (``is_connected``, ``services``, ``start_notify``,
    ``stop_notify``, ``write_gatt_char``, ``disconnect``)."""

    def __init__(self, path, product_id: int = PRO_CONTROLLER2_PID, transport=None):
        self.path = path
        self.product_id = int(product_id)
        self.is_gamecube = self.product_id == NSO_GAMECUBE_CONTROLLER_PID
        # The vendor command interface of THIS pad. None until resolved by the
        # controller; an unbound transport still works, it just cannot disambiguate
        # between two same-PID pads.
        self.transport = transport
        # Set by the read loop to whatever report id actually arrived. Writing the
        # startup commands successfully does not prove the pad switched reports, so
        # this is the only honest source for "which input mode are we in".
        self.last_input_report_id = None
        # Which route last carried rumble to the wire: "bulk", "hid_output" or "none".
        # Kept separate from the command/input transports so one route working is
        # never mistaken for all of them working.
        self.rumble_transport = "none"
        # Once interface 1 rejects a GameCube rumble write, keep this connection on
        # interface 0. Retrying bulk for every frame would add latency and log spam.
        self._gc_bulk_rumble_failed = False
        # Set by USBHidController.initialize() to the route that actually delivered the
        # startup commands. That is the only evidence we have that the vendor endpoint
        # is reachable, and it is what decides where GameCube rumble goes.
        self.command_transport = "none"
        # Resolve product-specific hot paths once. Pro Controller 2 then executes
        # the same branch-free translator/activity/output route as v1.7; GameCube
        # keeps its protocol-specific implementation without taxing every Pro frame.
        if self.is_gamecube:
            self._translate_input_report = self._translate_gamecube_input_report
            self._rumble_payload_is_active = _gc_rumble_payload_is_active
            self._accept_rumble_write = self._accept_gamecube_rumble_write
            self._write_primary_rumble_frame = self._write_gamecube_primary_rumble_frame
            self._submit_rumble_write = self._submit_gamecube_rumble_write
            self._rumble_write_loop = self._gamecube_rumble_write_loop
        else:
            self._translate_input_report = _translate_pro2_usb_report
            self._rumble_payload_is_active = _pro2_rumble_payload_is_active
            self._accept_rumble_write = self._accept_pro2_rumble_write
            self._write_primary_rumble_frame = self._write_pro2_primary_rumble_frame
            self._submit_rumble_write = self._submit_pro2_rumble_write_v17
            self._rumble_write_loop = self._pro2_rumble_write_loop_v17
        self.dev = None
        self.is_connected = False
        if self.is_gamecube:
            # Expose ONLY the input-report characteristic. Controller's GameCube branch
            # subscribes its input callback to every notify characteristic in the SW2
            # service; with the default MockService that would also hook
            # COMMAND_RESPONSE_UUID, and a 64-byte command ack (63 bytes once the
            # report id is stripped) is long enough to pass that callback's length
            # filter and be misparsed as input.
            self.services = [MockService(SW2_SERVICE_UUID, [
                MockCharacteristic(INPUT_REPORT_UUID, ["notify"], handle=2),
            ])]
        else:
            self.services = [MockService(SW2_SERVICE_UUID)]
        self._notify = {}          # lowercased uuid -> callback
        self._read_thread = None
        self._read_stop = threading.Event()
        self._write_lock = threading.Lock()
        self.is_high_speed_usb = False
        self._input_deltas = deque(maxlen=50)
        self._input_delta_sum = 0.0
        self._last_input_time = 0.0
        self._next_input_stats_update = 0.0
        self._last_usb_rumble_active = None
        self._last_usb_rumble_refresh = 0.0
        self._last_usb_rumble_command = None
        self._last_usb_sample_command = None
        self._last_usb_sample_refresh = 0.0
        # Dedicated rumble writer thread: producers (the asyncio event loop) only
        # drop the latest payload into a single slot and return immediately, so a
        # blocking hidapi dev.write() can never stall the shared event loop.
        self._rumble_slot = None            # latest pending output-report payload (new overwrites old)
        self._rumble_slot_lock = threading.Lock()
        # GameCube rumble is a latched ON/OFF motor state rather than a stream of
        # HD-rumble samples.  Keep one opposite follow-up transition so an OFF
        # submitted before a pending ON reaches the wire cannot erase that ON.
        # Pro Controller 2 never reads these fields and retains its v1.7 latest-frame
        # producer unchanged.
        self._gc_rumble_desired_active = False
        self._gc_rumble_followup = None
        self._rumble_wake = threading.Event()
        self._rumble_stop = threading.Event()
        self._rumble_thread = None
        self._rumble_high_priority_enabled = os.environ.get(
            "SWITCH2_USB_RUMBLE_HIGH_PRIORITY", "1").lower() in (
                "1", "true", "yes", "on")
        self._rumble_thread_priority_applied = False
        # Wired Pro rumble frames contain only ~15 ms of samples. Keep replaying the
        # latest active frame across short producer/asyncio stalls, but bound the hold
        # so a lost stop command can never leave a motor running indefinitely.
        self._rumble_sustain_enabled = not self.is_gamecube
        try:
            sustain_ms = float(os.environ.get("SWITCH2_USB_RUMBLE_SUSTAIN_MS", "120"))
        except (TypeError, ValueError):
            sustain_ms = 120.0
        self._rumble_sustain_seconds = max(0.030, min(0.250, sustain_ms / 1000.0))
        self._rumble_sustain_frame = None
        self._rumble_sustain_deadline = 0.0
        self._rumble_sustain_repeat_count = 0
        self._rumble_sustain_timeout_count = 0
        self._rumble_pending_stop_submitted_at = 0.0
        # Pro-only persistent playout state. GameCube's bound submit/writer methods
        # never read or mutate these fields.
        try:
            pro_hold_ms = float(os.environ.get(
                "SWITCH2_USB_PRO_RUMBLE_HOLD_MS", "120"))
        except (TypeError, ValueError):
            pro_hold_ms = 120.0
        self._pro_hold_seconds = max(0.030, min(0.250, pro_hold_ms / 1000.0))
        self._pro_latest_active_frame = None
        self._pro_pending_active_frame = None
        self._pro_pending_stop_frame = None
        self._pro_active_hold_deadline = 0.0
        self._pro_submit_sequence = 0
        self._pro_stop_sequence = 0
        self._pro_replay_count = 0
        self._pro_real_stop_count = 0
        self._pro_timeout_stop_count = 0
        self._pro_stale_active_drop_count = 0
        self._pro_timer_create_error = 0
        self._pro_timer_set_error = 0
        self._rumble_trace_writer_late_count = 0
        self._rumble_trace_writer_late_max_ms = 0.0
        self._rumble_trace_producer_frame_count = 0
        self._rumble_trace_sustain_wire_count = 0
        self._rumble_trace_stop_latency_max_ms = 0.0
        trace_enabled = os.environ.get("SWITCH2_USB_RUMBLE_TRACE", "0").lower() in (
            "1", "true", "yes", "on")
        try:
            trace_size = max(128, min(
                16384, int(os.environ.get("SWITCH2_USB_RUMBLE_TRACE_SIZE", "4096"))))
        except (TypeError, ValueError):
            trace_size = 4096
        try:
            self._rumble_trace_gap_seconds = max(0.015, float(
                os.environ.get("SWITCH2_USB_RUMBLE_TRACE_GAP_MS", "25")) / 1000.0)
        except (TypeError, ValueError):
            self._rumble_trace_gap_seconds = 0.025
        self._rumble_trace = deque(maxlen=trace_size) if trace_enabled else None
        self._rumble_trace_path = os.environ.get(
            "SWITCH2_USB_RUMBLE_TRACE_PATH", "logs/usb_rumble_trace.jsonl")
        self._blackbox_enabled = os.environ.get(
            "SWITCH2_USB_RUMBLE_BLACKBOX", "0").lower() in (
                "1", "true", "yes", "on")
        self._rumble_trace_last_wire_start = 0.0
        self._rumble_trace_last_wire_active = False
        self._rumble_trace_gap_count = 0
        self._rumble_trace_first_gap_snapshot = None
        self._rumble_trace_submit_count = 0
        self._rumble_trace_overwrite_count = 0
        self._hid_rumble_ok = False         # a hid output write has succeeded at least once
        self._hid_rumble_fail_streak = 0
        self._last_bulk_fallback = 0.0
        self._connected_at = time.time()
        self._timer_res_raised = False
        self._last_rumble_write_warn = 0.0
        # Audio-haptic rate gate: the controller sets this True whenever the emulated
        # DualSense is receiving an audio-haptic PCM stream (any form, including all-zero
        # frames). The write loop then caps at 40 Hz (25 ms); pure traditional rumble
        # runs at 60 Hz (15 ms). Wired Audio Haptic halts the pad's OUT endpoint above
        # ~40 Hz, so this cap is required.
        self.is_audio_haptic_active = False
        self._last_was_audio_haptic = False
        # Lazily set by _write_rumble_frame on congestion; read by the loop only while
        # _congested_until is in the future, so a default keeps the first read safe.
        self._congest_interval = 0.025
        # Silence suppression: after a few inactive (zero-amplitude) frames, stop
        # re-sending silence so we only touch the interrupt OUT endpoint when the
        # motor actually needs data -- this keeps the controller's OUT queue from
        # slowly filling under sustained 66 Hz audio haptics.
        self._inactive_run = 0
        # Congestion backoff: a slow write (device NAK/backpressure) temporarily
        # widens the min inter-write interval so we stop over-driving a busy pad.
        self._congested_until = 0.0
        # OUT-pipe stall detection + self-heal (close/reopen the hid handle).
        self._stall_streak = 0
        self._last_recover = 0.0
        self._recover_attempts = 0
        self._io_pause = threading.Event()   # set while recovering; read loop stands down
        # _reopen() is called from both the read thread and the rumble writer. Without this
        # lock the two race on _recover_attempts and can burn the whole retry budget inside
        # a single backoff window.
        self._recover_lock = threading.Lock()
        self.on_disconnect_callback = None
        self._disconnect_notified = False

    # A single transient read failure used to kill the wired pad permanently. The
    # handle is reopened in place instead, with a backoff: the drop is caused by
    # the pad itself glitching (brown-out, endpoint stall, selective suspend), so
    # reopening immediately just fails again and adds bus load.
    _RECOVER_BACKOFF = (0.25, 1.0, 2.0)
    _MAX_RECOVER_ATTEMPTS = len(_RECOVER_BACKOFF)
    # A healthy wired pad reports continuously at its polling rate (that is what
    # makes the polling-rate detection below possible), so a multi-second gap is a
    # stall, not idleness. Three orders of magnitude of headroom over a ~1 ms pad.
    _STALL_TIMEOUT = 2.0
    # Consecutive failed output writes (~15 ms apart) before treating the OUT pipe
    # as stalled and reopening.
    _STALL_WRITE_STREAK = 20
    # Healthy input for this long after a recovery refunds the retry budget, so a
    # session that glitches once an hour keeps recovering instead of running out.
    _RECOVER_SETTLE = 10.0
    # How long after connect the Bulk/WinUSB rumble fallback stays eligible. It exists to
    # probe which transport this pad accepts, so it only needs the opening seconds.
    _BULK_FALLBACK_WINDOW = 3.0

    def _reopen(self):
        """Close and re-open the HID handle in place. True when input can resume.

        Runs on whichever worker noticed the fault. `_io_pause` stands the read and
        rumble loops down while the handle is swapped, and `_write_lock` is held so
        no writer is inside `dev.write()` when the handle goes away.
        """
        # Serialised so a concurrent fault on the other worker cannot consume a second
        # attempt while this one is still in its backoff sleep. The budget itself is
        # unchanged (_MAX_RECOVER_ATTEMPTS stays 3).
        if not self._recover_lock.acquire(blocking=False):
            return False
        try:
            return self._reopen_locked()
        finally:
            self._recover_lock.release()

    def _reopen_locked(self):
        if self._read_stop.is_set():
            return False
        if self._recover_attempts >= self._MAX_RECOVER_ATTEMPTS:
            return False
        delay = self._RECOVER_BACKOFF[self._recover_attempts]
        self._recover_attempts += 1
        self._last_recover = time.time()

        self._io_pause.set()
        try:
            with self._write_lock:
                if self.dev is not None:
                    try:
                        self.dev.close()
                    except Exception:
                        logger.debug("Wired USB HID close during recovery failed", exc_info=True)
                    self.dev = None
            time.sleep(delay)
            if self._read_stop.is_set():
                return False
            try:
                self.open()
            except Exception as exc:
                logger.warning(
                    "Wired USB HID reopen attempt %d/%d failed: %s",
                    self._recover_attempts, self._MAX_RECOVER_ATTEMPTS, exc)
                return False
            # Do not resume driving the motors with the pre-fault payload; a pad
            # that just glitched should not be hit with rumble the instant it
            # comes back.
            with self._rumble_slot_lock:
                self._rumble_slot = None
                self._rumble_sustain_frame = None
                self._rumble_sustain_deadline = 0.0
                self._pro_latest_active_frame = None
                self._pro_pending_active_frame = None
                self._pro_pending_stop_frame = None
                self._pro_active_hold_deadline = 0.0
                if self.is_gamecube:
                    # The physical motor state is unknown after reopening.
                    # Accept the next GameCube transition as a resync command.
                    self._gc_rumble_desired_active = None
                    self._gc_rumble_followup = None
            self._inactive_run = 0
            self._stall_streak = 0
            # Restart the staleness clock, otherwise the watchdog fires again
            # immediately on the gap the fault itself created.
            self._last_input_time = 0.0
            self._input_deltas.clear()
            self._input_delta_sum = 0.0
            logger.info(
                "Wired USB HID handle reopened (attempt %d/%d)",
                self._recover_attempts, self._MAX_RECOVER_ATTEMPTS)
            return True
        finally:
            self._io_pause.clear()

    def open(self):
        if self.dev is not None:
            return
        hid = _import_hid()
        if hid is None:
            raise RuntimeError("hid module unavailable")
        # Support both common packages: 'hidapi' (hid.device().open_path) and
        # 'hid' (hid.Device(path=...)).
        if hasattr(hid, "device"):
            dev = hid.device()
            dev.open_path(self.path)
            try:
                dev.set_nonblocking(0)
            except Exception:
                pass
        elif hasattr(hid, "Device"):
            dev = hid.Device(path=self.path)
        else:
            raise RuntimeError("unrecognized hid package API")
        self.dev = dev
        self.is_connected = True

    async def start_notify(self, uuid, callback):
        self.open()
        self._notify[str(uuid).lower()] = callback
        self._ensure_read_thread()

    async def stop_notify(self, uuid):
        self._notify.pop(str(uuid).lower(), None)

    async def write_gatt_char(self, uuid, data, response=False):
        # The base Controller pipeline writes rumble to the Bluetooth GATT output
        # characteristic. On wired USB that same body becomes HID output report 0x02:
        # [HID report-id 0x02] + [SW2 output body: 0x00 + L/R rumble + padding].
        # Keep non-rumble writes disabled; command/feature init reports are known to
        # disrupt the controller's default 0x05 input stream on Windows.
        #
        # This runs on the shared asyncio event loop. The actual hidapi dev.write()
        # is blocking and, under sustained ~66 Hz audio haptics, can stall on a USB
        # NAK/buffer-full; doing it inline would freeze the whole loop (all
        # controllers' rumble + input). So we only stash the latest payload in a
        # single slot and let a dedicated writer thread touch the wire.
        del response
        self._submit_rumble_write(uuid, data)

    def _record_rumble_submit(self, payload, overwritten):
        if self._rumble_trace is None:
            return
        self._rumble_trace_submit_count += 1
        if overwritten:
            self._rumble_trace_overwrite_count += 1
        self._trace_rumble_event(
            "submit", int(overwritten), len(payload),
            payload[1] if len(payload) > 1 else -1,
            self._rumble_trace_submit_count)

    def _submit_pro2_rumble_write_v17(self, uuid, data):
        """v1.7-style latest-state Pro producer (no replay or stop barrier)."""
        payload = self._accept_pro2_rumble_write(uuid, data)
        if payload is None:
            return
        with self._rumble_slot_lock:
            overwritten = self._rumble_slot is not None
            self._rumble_slot = payload
        self._record_rumble_submit(payload, overwritten)
        self._ensure_rumble_thread()
        self._rumble_wake.set()

    def _submit_gamecube_rumble_write(self, uuid, data):
        """Queue only GameCube ON/OFF transitions, preserving one opposite edge."""
        payload = self._accept_gamecube_rumble_write(uuid, data)
        if payload is None:
            return
        active = _gc_rumble_payload_is_active(payload)
        with self._rumble_slot_lock:
            # Like the working wireless GameCube path, repeated copies of the
            # same latched motor state carry no new information.  Dropping them
            # here also prevents the 7 ms producer from needlessly waking the
            # Pro-derived 15 ms USB writer.
            if active == self._gc_rumble_desired_active:
                return
            self._gc_rumble_desired_active = active

            if self._rumble_slot is None:
                self._rumble_slot = payload
            else:
                pending_active = _gc_rumble_payload_is_active(self._rumble_slot)
                if pending_active == active:
                    # The newest desired state has returned to the pending state;
                    # an intervening edge that never reached the wire is obsolete.
                    self._gc_rumble_followup = None
                else:
                    self._gc_rumble_followup = payload
        self._record_rumble_submit(payload, False)
        self._ensure_rumble_thread()
        self._rumble_wake.set()

    @staticmethod
    def _accept_pro2_rumble_write(uuid, data):
        if str(uuid).lower() != VIBRATION_WRITE_PRO_CONTROLLER_UUID.lower():
            return None
        return bytes(data)

    @staticmethod
    def _accept_gamecube_rumble_write(uuid, data):
        if str(uuid).lower() != COMMAND_WRITE_UUID.lower():
            return None
        return _gc_usb_rumble_command(bytes(data))

    def _ensure_rumble_thread(self):
        if self._rumble_thread and self._rumble_thread.is_alive():
            return
        self._rumble_stop.clear()
        self._rumble_thread = threading.Thread(target=self._rumble_write_loop, daemon=True)
        self._rumble_thread.start()

    def _set_timer_resolution(self, enable: bool) -> None:
        """Raise/restore Windows multimedia timer resolution to 1 ms so the writer
        thread's 15 ms pacing is honoured (default granularity is ~15.6 ms). No-op
        off Windows; balanced by a matching timeEndPeriod on stop."""
        if enable and not self._timer_res_raised:
            self._timer_res_raised = timer_resolution.acquire()
        elif not enable and self._timer_res_raised:
            timer_resolution.release()
            self._timer_res_raised = False

    def _set_rumble_thread_priority(self) -> None:
        """Raise only the current USB writer thread to ABOVE_NORMAL on Windows.

        This deliberately does not alter process priority and never uses
        TIME_CRITICAL. Thread priority dies with the writer thread, so no global
        restoration is required during disconnect.
        """
        enabled = self._rumble_high_priority_enabled
        if power_saving.is_full():
            enabled = False
        if not enabled or os.name != "nt":
            self._trace_rumble_event(
                "thread_priority", int(enabled), 0, 0, 0)
            return
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentThread.argtypes = []
            kernel32.GetCurrentThread.restype = wintypes.HANDLE
            kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
            kernel32.SetThreadPriority.restype = wintypes.BOOL
            thread_handle = kernel32.GetCurrentThread()
            # THREAD_PRIORITY_ABOVE_NORMAL = 1. Pseudo handles returned by
            # GetCurrentThread are valid for SetThreadPriority in this process.
            applied = bool(kernel32.SetThreadPriority(thread_handle, 1))
            self._rumble_thread_priority_applied = applied
            error_code = 0 if applied else int(kernel32.GetLastError())
            self._trace_rumble_event(
                "thread_priority", 1, int(applied), 1, error_code)
            if not applied:
                logger.warning(
                    "Could not raise USB rumble writer thread priority (error %d)",
                    error_code)
        except Exception:
            self._trace_rumble_event("thread_priority", 1, 0, 1, -1)
            logger.debug(
                "Could not raise USB rumble writer thread priority",
                exc_info=True)

    def _create_pro_waitable_timer(self):
        """Create one Pro-writer-owned Windows high-resolution timer handle."""
        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create = kernel32.CreateWaitableTimerExW
            create.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR,
                               wintypes.DWORD, wintypes.DWORD]
            create.restype = wintypes.HANDLE
            set_timer = kernel32.SetWaitableTimerEx
            set_timer.argtypes = [wintypes.HANDLE,
                                  ctypes.POINTER(ctypes.c_longlong),
                                  wintypes.LONG, wintypes.LPVOID,
                                  wintypes.LPVOID, wintypes.LPVOID,
                                  wintypes.ULONG]
            set_timer.restype = wintypes.BOOL
            wait = kernel32.WaitForSingleObject
            wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            wait.restype = wintypes.DWORD
            close = kernel32.CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close.restype = wintypes.BOOL

            # CREATE_WAITABLE_TIMER_HIGH_RESOLUTION = 0x2. Retry without the flag
            # on Windows builds that predate high-resolution waitable timers.
            handle = create(None, None, 0x2, 0x1F0003)
            high_resolution = 1
            if not handle:
                first_error = ctypes.get_last_error()
                handle = create(None, None, 0, 0x1F0003)
                high_resolution = 0
                if not handle:
                    self._pro_timer_create_error = ctypes.get_last_error() or first_error
                    self._trace_rumble_event(
                        "pro_timer_create", 0, 0,
                        self._pro_timer_create_error, 0)
                    return None
            self._pro_timer_api = (set_timer, wait, close, ctypes)
            self._trace_rumble_event(
                "pro_timer_create", 1, high_resolution, 0, 0)
            return handle
        except Exception:
            self._pro_timer_create_error = -1
            self._trace_rumble_event("pro_timer_create", 0, 0, -1, 0)
            logger.debug("Could not create Pro rumble waitable timer", exc_info=True)
            return None

    def _wait_for_pro_deadline(self, timer_handle, wait_seconds):
        """Wait until a relative Pro playout deadline; True when native timer worked."""
        wait_seconds = max(0.0, float(wait_seconds))
        if timer_handle is None:
            if wait_seconds > 0.002:
                time.sleep(wait_seconds - 0.002)
            deadline = time.perf_counter() + min(wait_seconds, 0.002)
            while time.perf_counter() < deadline:
                pass
            return False
        try:
            set_timer, wait, _close, ctypes = self._pro_timer_api
            due = ctypes.c_longlong(-max(1, int(wait_seconds * 10_000_000)))
            if not set_timer(timer_handle, ctypes.byref(due), 0,
                             None, None, None, 0):
                self._pro_timer_set_error = ctypes.get_last_error()
                self._trace_rumble_event(
                    "pro_timer_arm", wait_seconds * 1000.0,
                    0, self._pro_timer_set_error, 0)
                time.sleep(wait_seconds)
                return False
            self._trace_rumble_event(
                "pro_timer_arm", wait_seconds * 1000.0, 1, 0, 0)
            wait_result = int(wait(timer_handle,
                                   max(1, int(wait_seconds * 1000.0) + 20)))
            self._trace_rumble_event(
                "pro_timer_wake", wait_seconds * 1000.0,
                wait_result, 0, 0)
            return wait_result == 0
        except Exception:
            self._pro_timer_set_error = -1
            time.sleep(wait_seconds)
            return False

    def _close_pro_waitable_timer(self, timer_handle):
        if timer_handle is None:
            return
        try:
            self._pro_timer_api[2](timer_handle)
        except Exception:
            logger.debug("Could not close Pro rumble waitable timer", exc_info=True)

    def _trace_rumble_event(self, event, value0=0, value1=0, value2=0, value3=0):
        """Append one fixed-size record; never performs I/O on a rumble hot path."""
        trace = self._rumble_trace
        if trace is not None:
            trace.append((time.perf_counter(), event, value0, value1, value2, value3))

    def _dump_rumble_trace(self, reason):
        """Flush the bounded in-memory trace after worker shutdown or disconnect."""
        trace = self._rumble_trace
        if trace is None:
            return
        current = list(trace)
        first_gap = self._rumble_trace_first_gap_snapshot
        payload = {
            "type": "usb_rumble_trace",
            "reason": reason,
            "wall_time": time.time(),
            "product_id": self.product_id,
            "fields": _USB_RUMBLE_TRACE_FIELDS,
            "submit_count": self._rumble_trace_submit_count,
            "overwrite_count": self._rumble_trace_overwrite_count,
            "gap_count": self._rumble_trace_gap_count,
            "gap_threshold_ms": self._rumble_trace_gap_seconds * 1000.0,
            "sustain_ms": self._rumble_sustain_seconds * 1000.0,
            "sustain_repeat_count": self._rumble_sustain_repeat_count,
            "sustain_timeout_count": self._rumble_sustain_timeout_count,
            "writer_late_count": self._rumble_trace_writer_late_count,
            "writer_late_max_ms": self._rumble_trace_writer_late_max_ms,
            "producer_frame_count": self._rumble_trace_producer_frame_count,
            "sustain_wire_count": self._rumble_trace_sustain_wire_count,
            "stop_latency_max_ms": self._rumble_trace_stop_latency_max_ms,
            "pro_hold_ms": self._pro_hold_seconds * 1000.0,
            "pro_replay_count": self._pro_replay_count,
            "pro_real_stop_count": self._pro_real_stop_count,
            "pro_timeout_stop_count": self._pro_timeout_stop_count,
            "pro_stale_active_drop_count": self._pro_stale_active_drop_count,
            "pro_timer_create_error": self._pro_timer_create_error,
            "pro_timer_set_error": self._pro_timer_set_error,
            "first_gap_snapshot": first_gap,
            "events": current,
        }
        path = os.path.abspath(self._rumble_trace_path)
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            with _usb_rumble_trace_dump_lock:
                with open(path, "a", encoding="utf-8") as stream:
                    stream.write(line)
            logger.info(
                "USB rumble trace dumped: %s (%d events, %d gaps, %d overwrites)",
                path, len(current), self._rumble_trace_gap_count,
                self._rumble_trace_overwrite_count)
        except Exception:
            logger.exception("Failed to dump USB rumble trace to %s", path)

    def _pro2_rumble_write_loop_v17(self):
        """v1.7-style Pro writer: producer-driven latest frame, no replay."""
        SILENCE_KEEP = 3
        last_write = 0.0
        while not self._rumble_stop.is_set():
            self._rumble_wake.wait()
            if self._rumble_stop.is_set():
                break
            self._rumble_wake.clear()
            if self._io_pause.is_set():
                time.sleep(0.01)
                continue
            with self._rumble_slot_lock:
                data = self._rumble_slot
                self._rumble_slot = None
            if data is None:
                continue
            self._set_timer_resolution(True)

            interval = 0.015
            if time.perf_counter() < self._congested_until:
                interval = max(interval, self._congest_interval)
            target_time = last_write + interval
            now = time.perf_counter()
            if now < target_time:
                sleep_amount = target_time - now
                if power_saving.is_off():
                    if sleep_amount > 0.002:
                        time.sleep(sleep_amount - 0.002)
                    while time.perf_counter() < target_time:
                        pass
                elif self._rumble_stop.wait(sleep_amount):
                    break

            active = _pro2_rumble_payload_is_active(data)
            if active:
                self._inactive_run = 0
            else:
                self._inactive_run += 1
                if self._inactive_run > SILENCE_KEEP:
                    self._trace_rumble_event("silence_drop", self._inactive_run)
                    self._set_timer_resolution(False)
                    continue

            self._rumble_trace_producer_frame_count += 1
            self._trace_rumble_event(
                "pro_producer_frame", self._rumble_trace_producer_frame_count,
                int(active), 0, 0)
            last_write = time.perf_counter()
            self._write_rumble_frame(data)
            if not active:
                self._set_timer_resolution(False)
        self._set_timer_resolution(False)

    def _gamecube_rumble_write_loop(self):
        """GameCube transition writer using the Pro 2 writer's 15 ms pacing."""
        last_write = 0.0
        while not self._rumble_stop.is_set():
            self._rumble_wake.wait()
            if self._rumble_stop.is_set():
                break
            self._rumble_wake.clear()
            if self._io_pause.is_set():
                time.sleep(0.01)
                continue
            with self._rumble_slot_lock:
                data = self._rumble_slot
                self._rumble_slot = self._gc_rumble_followup
                self._gc_rumble_followup = None
                has_followup = self._rumble_slot is not None
            if data is None:
                continue
            self._set_timer_resolution(True)

            interval = 0.015
            if time.perf_counter() < self._congested_until:
                interval = max(interval, self._congest_interval)
            target_time = last_write + interval
            now = time.perf_counter()
            if now < target_time:
                sleep_amount = target_time - now
                if power_saving.is_off():
                    if sleep_amount > 0.002:
                        time.sleep(sleep_amount - 0.002)
                    while time.perf_counter() < target_time:
                        pass
                elif self._rumble_stop.wait(sleep_amount):
                    break
            active = _gc_rumble_payload_is_active(data)
            self._rumble_trace_producer_frame_count += 1
            self._trace_rumble_event(
                "gc_producer_frame", self._rumble_trace_producer_frame_count,
                int(active), 0, 0)
            last_write = time.perf_counter()
            self._write_rumble_frame(data)
            write_succeeded = (
                self.rumble_transport == "bulk"
                or (self.rumble_transport == "hid_output"
                    and self._hid_rumble_fail_streak == 0)
            )
            if not write_succeeded:
                # Do not let transition deduplication turn a failed write into
                # a permanently latched state.  The next identical GameCube
                # producer update may retry through the existing recovery path.
                with self._rumble_slot_lock:
                    if self._gc_rumble_desired_active == active:
                        self._gc_rumble_desired_active = None
            if has_followup:
                # A follow-up was already pending when this transition was
                # consumed.  Keep the writer event-driven, but do not make it wait
                # for another producer callback before delivering that edge.
                self._rumble_wake.set()
            else:
                self._set_timer_resolution(False)
        self._set_timer_resolution(False)

    def _experimental_deadline_rumble_write_loop(self):
        # Minimum spacing between wire writes. HD rumble frames carry 3x5 ms of
        # We dynamically adjust the interval:
        # - 15ms (66Hz) for traditional vibration (safe, proven)
        # - 25ms (40Hz) for audio haptics (prevents USB buffer overrun from dense payloads)
        # The interval is evaluated per frame based on the Controller state.
        # After this many consecutive inactive frames, stop re-sending silence.
        self._set_rumble_thread_priority()
        SILENCE_KEEP = 3
        last_write = 0.0
        while not self._rumble_stop.is_set():
            wait_started = time.perf_counter()
            with self._rumble_slot_lock:
                sustaining = self._rumble_sustain_frame is not None
            interval = 0.015
            if wait_started < self._congested_until:
                interval = max(interval, self._congest_interval)
            target_time = last_write + interval if last_write else 0.0
            # Wait against an absolute wire-start deadline. The old fixed 15 ms
            # wait began after dev.write() returned, adding every 2-7 ms USB write
            # to the cadence. Leave the last 2 ms to the existing precision spin.
            if sustaining and target_time:
                wait_timeout = max(0.0, target_time - wait_started - 0.002)
            else:
                wait_timeout = 0.5
            self._rumble_wake.wait(wait_timeout)
            woke_at = time.perf_counter()
            wake_signaled = int(self._rumble_wake.is_set())
            self._trace_rumble_event(
                "wake", (woke_at - wait_started) * 1000.0,
                wake_signaled, 0, 0)
            if sustaining and target_time:
                actual_wait_ms = (woke_at - wait_started) * 1000.0
                self._trace_rumble_event(
                    "deadline_wait", wait_timeout * 1000.0,
                    actual_wait_ms, wake_signaled,
                    max(0.0, target_time - woke_at) * 1000.0)
            if self._rumble_stop.is_set():
                break
            self._rumble_wake.clear()
            # Stand down while the handle is being swapped: write_output_report
            # calls open() outside _write_lock, so writing here during a reopen
            # would race with self.dev being None.
            if self._io_pause.is_set():
                time.sleep(0.01)
                continue
            with self._rumble_slot_lock:
                data = self._rumble_slot
                self._rumble_slot = None
                frame_source = "producer" if data is not None else None
                if data is None and self._rumble_sustain_frame is not None:
                    now = time.perf_counter()
                    if now < self._rumble_sustain_deadline:
                        data = self._rumble_sustain_frame
                        frame_source = "sustain"
                        self._rumble_sustain_repeat_count += 1
                        self._trace_rumble_event(
                            "sustain_repeat", self._rumble_sustain_repeat_count,
                            max(0.0, self._rumble_sustain_deadline - now) * 1000.0,
                            0, 0)
                    else:
                        data = _pro2_zero_rumble_payload(self._rumble_sustain_frame)
                        frame_source = "timeout"
                        self._rumble_sustain_frame = None
                        self._rumble_sustain_deadline = 0.0
                        self._rumble_sustain_timeout_count += 1
                        self._trace_rumble_event(
                            "sustain_timeout", self._rumble_sustain_timeout_count,
                            self._rumble_sustain_seconds * 1000.0, 0, 0)
            if data is None:
                self._trace_rumble_event("slot_empty")
                continue

            if time.perf_counter() < self._congested_until:
                interval = max(interval, self._congest_interval)
            
            now = time.perf_counter()
            target_time = last_write + interval
            if now < target_time:
                sleep_amount = target_time - now
                self._trace_rumble_event(
                    "pace", interval * 1000.0, sleep_amount * 1000.0,
                    max(0.0, self._congested_until - now) * 1000.0, 0)
                if power_saving.is_off():
                    if sleep_amount > 0.002:
                        # Sleep most of the way, leaving 2ms for spin-wait accuracy
                        time.sleep(sleep_amount - 0.002)
                    # Spin-wait the remaining time to guarantee strict interval
                    while time.perf_counter() < target_time:
                        pass
                elif self._rumble_stop.wait(sleep_amount):
                    break
            # A stop submitted while this frame was being paced must preempt a
            # locally-held sustain replay; otherwise one stale active frame can be
            # emitted just before the queued stop and extend stop latency by 15 ms.
            if self._rumble_payload_is_active(data):
                with self._rumble_slot_lock:
                    pending = self._rumble_slot
                    if (pending is not None and
                            not self._rumble_payload_is_active(pending)):
                        data = pending
                        frame_source = "producer"
                        self._rumble_slot = None
                        self._trace_rumble_event("priority_stop")
            # Silence suppression: keep the motor definitively stopped by sending a
            # few zero frames, then stop touching the wire until real motion returns.
            if self._rumble_payload_is_active(data):
                self._inactive_run = 0
            else:
                self._inactive_run += 1
                if self._inactive_run > SILENCE_KEEP:
                    self._trace_rumble_event("silence_drop", self._inactive_run)
                    continue

            active = self._rumble_payload_is_active(data)
            if frame_source == "sustain":
                self._rumble_trace_sustain_wire_count += 1
                self._trace_rumble_event(
                    "sustain_frame", self._rumble_trace_sustain_wire_count)
            elif frame_source == "timeout":
                self._trace_rumble_event("timeout_zero")
            else:
                self._rumble_trace_producer_frame_count += 1
                self._trace_rumble_event(
                    "producer_frame", self._rumble_trace_producer_frame_count,
                    int(active), 0, 0)
            if not active and self._rumble_pending_stop_submitted_at:
                stop_latency_ms = ((time.perf_counter() -
                                    self._rumble_pending_stop_submitted_at) * 1000.0)
                self._rumble_trace_stop_latency_max_ms = max(
                    self._rumble_trace_stop_latency_max_ms, stop_latency_ms)
                self._rumble_pending_stop_submitted_at = 0.0
                self._trace_rumble_event("stop_wire", stop_latency_ms)

            # Rebase after a late wake. We send only the latest frame once and make
            # the actual wire start the next cadence anchor; no catch-up burst.
            last_write = time.perf_counter()
            deadline_late_ms = ((last_write - target_time) * 1000.0
                                if target_time else 0.0)
            if sustaining and deadline_late_ms > 0.5:
                self._rumble_trace_writer_late_count += 1
                self._rumble_trace_writer_late_max_ms = max(
                    self._rumble_trace_writer_late_max_ms, deadline_late_ms)
                self._trace_rumble_event(
                    "writer_late", deadline_late_ms,
                    self._rumble_trace_writer_late_count,
                    wait_timeout * 1000.0, wake_signaled)
                self._trace_rumble_event(
                    "deadline_rebase", deadline_late_ms,
                    interval * 1000.0, 0, 0)
            self._write_rumble_frame(data)

    def bulk_rumble_available(self) -> bool:
        """True when the vendor command endpoint demonstrably works for this pad.

        The right signal is which route actually delivered the startup commands, not
        whether a WinUSB device-interface path could be resolved. Windows' inbox
        winusb.inf registers no ``DeviceInterfaceGUIDs`` (verified on a real
        MS_COMP_WINUSB-bound Pro Controller 2), so ``transport.is_bound`` is False on
        perfectly working machines -- gating rumble on it meant the GameCube bulk path
        never ran even once. libusb does not need those GUIDs; it reaches interface 1
        through the composite device.

        ``is_bound`` keeps its own meaning (this transport addresses one specific pad)
        and is still what stops rumble crossing between two same-PID controllers.
        """
        return self.command_transport in ("native_winusb", "pyusb")

    def _write_rumble_frame(self, data):
        # GameCube uses the command interface whenever startup proved it reachable.
        # If the bulk write fails, send the same motor state through HID interface 0
        # immediately so a transient or missing WinUSB binding does not lose rumble.
        if (self.is_gamecube and not self._gc_bulk_rumble_failed
                and self.bulk_rumble_available()):
            if self.write_rumble_command(data):
                return
            self._gc_bulk_rumble_failed = True
            self.rumble_transport = "none"
            logger.warning(
                "Wired USB GameCube interface-1 rumble failed; falling back to HID interface 0")

        t0 = time.perf_counter()
        previous_wire_start = self._rumble_trace_last_wire_start
        previous_wire_active = self._rumble_trace_last_wire_active
        wire_gap = t0 - previous_wire_start if previous_wire_start else 0.0
        self._rumble_trace_last_wire_start = t0
        written = None
        try:
            written = self._write_primary_rumble_frame(data)
        except Exception:
            logger.debug("Wired USB HID output rumble write failed", exc_info=True)
        elapsed = time.perf_counter() - t0
        if self._rumble_trace is not None:
            active = int(self._rumble_payload_is_active(data))
            self._rumble_trace_last_wire_active = bool(active)
            self._trace_rumble_event(
                "wire", wire_gap * 1000.0, elapsed * 1000.0,
                written if written is not None else -1, active)
            if (previous_wire_start and previous_wire_active and active and
                    wire_gap >= self._rumble_trace_gap_seconds):
                self._rumble_trace_gap_count += 1
                self._trace_rumble_event(
                    "gap", wire_gap * 1000.0, elapsed * 1000.0,
                    self._rumble_trace_gap_count, active)
                if self._rumble_trace_first_gap_snapshot is None:
                    self._rumble_trace_first_gap_snapshot = list(self._rumble_trace)
                    logger.warning(
                        "USB rumble wire gap %.1f ms captured; trace will flush to %s "
                        "when the controller disconnects",
                        wire_gap * 1000.0, self._rumble_trace_path)
        if elapsed > 1.0:
            now = time.time()
            if now - getattr(self, '_last_rumble_write_warn', 0.0) >= 1.0:
                self._last_rumble_write_warn = now
                logger.warning("Wired USB HID rumble write blocked for %.2fs", elapsed)
                if hasattr(self, '_blackbox_history') and not getattr(self, '_blackbox_frozen', False):
                    self._blackbox_frozen = True
                    logger.warning("USB RUMBLE BLACKBOX (write blocked): last 32 wire writes ->")
                    for _t, _wr, _ms, _rep in list(self._blackbox_history):
                        logger.warning("  t=%.3f wr=%s ms=%.1f report=%s", _t, _wr, _ms, _rep)

        # Congestion backoff: a write that takes >40 ms means the pad is NAKing/
        # backpressuring the interrupt OUT endpoint. Widen the next few intervals so
        # we stop over-driving it (the slot naturally drops the excess frames).
        if elapsed > 0.040:
            self._congest_interval = min(0.1, max(0.030, elapsed))
            self._congested_until = time.perf_counter() + 0.5
            self._trace_rumble_event(
                "congestion", elapsed * 1000.0,
                self._congest_interval * 1000.0, 500.0, 0)

        # hidapi's device.write() returns the byte count on success and -1 on failure.
        if written is not None and written > 0:
            # For GameCube this proves only that the OS queued the fallback report;
            # the USB protocol has no acknowledgement that the motor physically moved.
            self.rumble_transport = "hid_output"
            self._hid_rumble_ok = True
            self._hid_rumble_fail_streak = 0
            self._stall_streak = 0
            self._recover_attempts = 0
            return

        self._hid_rumble_fail_streak += 1
        self._stall_streak += 1

        # Pro Controller 2 keeps its bounded init-time Bulk/WinUSB fallback when HID
        # has never accepted a report. GameCube already tried interface 1 before HID
        # at the top of this method, so it must not retry bulk after the fallback fails.
        #
        # Also bounded in time. _hid_rumble_ok never flipping true used to mean this ran
        # every 0.5 s for the whole session; on a machine without the WinUSB binding that
        # is an endless stream of libusb round-trips against the composite device. It is a
        # transport probe, so a few seconds after connect is all it is good for.
        now = time.time()
        if (not self.is_gamecube and not self._hid_rumble_ok
                and now - self._connected_at <= self._BULK_FALLBACK_WINDOW
                and now - self._last_bulk_fallback >= 0.5):
            self._last_bulk_fallback = now
            self.write_rumble_command(data)
            return

        # A sustained run of rejected writes means the OUT pipe has stalled. Heal it
        # by reopening rather than letting the read side wait for a fault that may
        # never come.
        if self._hid_rumble_ok and self._stall_streak >= self._STALL_WRITE_STREAK:
            logger.warning(
                "Wired USB HID output stalled for %d consecutive writes; attempting recovery",
                self._stall_streak)
            self._stall_streak = 0
            self._reopen()

    def _write_pro2_primary_rumble_frame(self, data):
        """v1.7 Pro Controller 2 HID OUT path, with no GameCube dispatch."""
        return self._write_pro2_output_report(data)

    def _write_gamecube_primary_rumble_frame(self, data):
        self._gc_rumble_counter = (getattr(self, "_gc_rumble_counter", 0) + 1) & 0x0F
        body = _gc_native_rumble_body(
            self._rumble_payload_is_active(data), self._gc_rumble_counter)
        return self.write_output_report(
            body, OUTPUT_REPORT_ID_GAMECUBE, raw_body=True)

    def write_rumble_command(self, data):
        active = self._rumble_payload_is_active(data)
        now = time.time()
        if self.is_gamecube:
            # Already a complete USB vibration command; just push it over the vendor
            # bulk endpoint, deduplicated the same way as the Pro Controller 2 path.
            should_send = (data != self._last_usb_rumble_command or
                           (active and now - self._last_usb_rumble_refresh >= 0.1))
            if should_send:
                if send_pro_controller2_usb_command(bytes(data), self.product_id,
                                                     self.transport, require_unique=True):
                    self.rumble_transport = "bulk"
                    self._last_usb_rumble_active = active
                    self._last_usb_rumble_refresh = now
                    self._last_usb_rumble_command = bytes(data)
                    return True
                return False
            # A deduplicated command still means the interface-1 route is live;
            # its last accepted motor state remains in effect.
            return True
        command = _pro2_usb_vibration_command(data)
        if command == self._last_usb_rumble_command and (not active or now - self._last_usb_rumble_refresh < 0.1):
            raw_sent = False
        else:
            raw_sent = send_pro_controller2_usb_command(command)
        if raw_sent:
            self._last_usb_rumble_active = active
            self._last_usb_rumble_refresh = now
            self._last_usb_rumble_command = command

        sample_command = _pro2_vibration_sample_command(data)
        sample_changed = sample_command != self._last_usb_sample_command
        sample_refresh_due = active and now - self._last_usb_sample_refresh >= 0.45
        if sample_changed or sample_refresh_due:
            if send_pro_controller2_usb_command(sample_command):
                self._last_usb_sample_command = sample_command
                self._last_usb_sample_refresh = now
        return bool(raw_sent)

    def write_output_report(self, data, report_id=None, raw_body=False):
        # Never re-open here. This used to call open(), so a writer that outlived
        # disconnect()'s bounded join could resurrect the HID handle after teardown --
        # leaking it, and letting _ensure_rumble_thread() spin up a fresh writer against
        # a controller the discoverer had already dropped.
        if self.dev is None or self._read_stop.is_set():
            return 0
        if report_id is None:
            report_id = _output_report_id(self.product_id)
        if raw_body:
            # Already in the device's own output-report layout. _pro2_usb_output_body
            # would reinterpret it as HD rumble frames and "limit" amplitudes that
            # aren't there.
            payload = bytes(data).ljust(PRO2_OUTPUT_REPORT_BODY_SIZE, b"\x00")
        else:
            is_audio = getattr(self, 'is_audio_haptic_active', False)
            payload = _pro2_usb_output_body(data, is_audio_active=is_audio)
        report = bytes([report_id]) + payload
            
        with self._write_lock:
            try:
                t0 = time.perf_counter()
                written = self.dev.write(report)
            except TypeError:
                t0 = time.perf_counter()
                written = self.dev.write(list(report))
                
        # Opt-in diagnostic: report.hex() is deliberately kept off the hot path.
        if self._blackbox_enabled and not getattr(self, "_blackbox_frozen", False):
            if not hasattr(self, "_blackbox_history"):
                self._blackbox_history = []
                
            elapsed = time.perf_counter() - t0
            self._blackbox_history.append((time.time(), written, elapsed * 1000, report.hex()))
            if len(self._blackbox_history) > 32:
                self._blackbox_history.pop(0)
            
        return written

    def _write_pro2_output_report(self, data):
        """Exact v1.7 Pro Controller 2 output-report construction fast path."""
        if self.dev is None or self._read_stop.is_set():
            return 0
        is_audio = getattr(self, 'is_audio_haptic_active', False)
        payload = _pro2_usb_output_body(data, is_audio_active=is_audio)
        report = bytes([OUTPUT_REPORT_ID_PRO2]) + payload

        with self._write_lock:
            try:
                t0 = time.perf_counter()
                written = self.dev.write(report)
            except TypeError:
                t0 = time.perf_counter()
                written = self.dev.write(list(report))

        if self._blackbox_enabled and not getattr(self, "_blackbox_frozen", False):
            if not hasattr(self, "_blackbox_history"):
                self._blackbox_history = []
            elapsed = time.perf_counter() - t0
            self._blackbox_history.append(
                (time.time(), written, elapsed * 1000, report.hex()))
            if len(self._blackbox_history) > 32:
                self._blackbox_history.pop(0)
        return written

    def write_command_report(self, command: bytes):
        # Same rule as write_output_report(): the handle is opened by open()/initialize(),
        # never lazily resurrected from a worker thread.
        if self.dev is None or self._read_stop.is_set():
            return 0
        report = (bytes([_output_report_id(self.product_id)])
                  + bytes(command).ljust(PRO2_OUTPUT_REPORT_BODY_SIZE, b"\x00"))
        with self._write_lock:
            try:
                return self.dev.write(report)
            except TypeError:
                return self.dev.write(list(report))

    def send_startup_reports_hid(self) -> bool:
        """Fallback for systems where interface 1 is not reachable through pyusb."""
        try:
            for command in _startup_commands(self.product_id):
                # Report the real outcome: this result now selects _init_transport, and
                # claiming success for writes the handle rejected would make _delayed_reinit
                # keep re-sending over a route that does not work.
                if self.write_command_report(command) <= 0:
                    logger.warning(
                        "Wired USB HID startup fallback: command 0x%02x rejected", command[0])
                    return False
                time.sleep(0.02)
            logger.info("Wired USB %s startup commands sent via HID output report fallback",
                        _device_label(self.product_id))
            return True
        except Exception as e:
            logger.warning("Wired USB HID startup fallback failed: %s", e)
            return False

    def _ensure_read_thread(self):
        if self._read_thread and self._read_thread.is_alive():
            return
        self._read_stop.clear()
        self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._read_thread.start()

    def _read_loop(self):
        # Marks the composite device as off limits to the libusb command path for as long
        # as we are reading interface 0. Reference-counted so a second wired pad does not
        # clear the flag when the first one's loop exits.
        with _usb_streaming_lock:
            global _usb_streaming_refs
            _usb_streaming_refs += 1
            _usb_input_streaming.set()
        try:
            self._read_loop_inner()
        finally:
            with _usb_streaming_lock:
                _usb_streaming_refs -= 1
                if _usb_streaming_refs <= 0:
                    _usb_streaming_refs = 0
                    _usb_input_streaming.clear()

    def _translate_gamecube_input_report(self, data):
        """GameCube-only input state tracking and translation."""
        if data:
            self.last_input_report_id = data[0]
        return translate_usb_report(data, NSO_GAMECUBE_CONTROLLER_PID)

    def _read_loop_inner(self):
        while not self._read_stop.is_set():
            if self._io_pause.is_set():
                time.sleep(0.01)
                continue
            if self.dev is None:
                break
            try:
                # Positional args work for both 'hidapi' (max_length, timeout_ms)
                # and 'hid' (size, timeout).
                data = self.dev.read(64, 8)
            except Exception as e:
                if self._io_pause.is_set():
                    continue
                logger.warning("Wired USB HID read failed (%s); attempting recovery", e)
                if self._reopen():
                    continue
                if self._read_stop.is_set():
                    break  # normal shutdown raced the recovery; not a failure
                logger.warning(
                    "Wired USB HID recovery exhausted after %d attempts; disconnecting",
                    self._recover_attempts)
                self._notify_disconnect("read_error")
                break
            if not data:
                # A read timeout is indistinguishable from a silently stalled pad,
                # so a gap this long is treated as a fault. Without this the pad can
                # stop reporting forever while still looking connected: no exception
                # is raised, the thread stays alive and nothing ever notices.
                if (self._last_input_time > 0
                        and not self._io_pause.is_set()
                        and time.perf_counter() - self._last_input_time > self._STALL_TIMEOUT):
                    stalled_for = time.perf_counter() - self._last_input_time
                    logger.warning(
                        "Wired USB HID input stalled for %.1fs with no error; attempting recovery",
                        stalled_for)
                    if self._reopen():
                        continue
                    logger.warning(
                        "Wired USB HID recovery exhausted after %d attempts; disconnecting",
                        self._recover_attempts)
                    self._notify_disconnect("input_stalled")
                    break
                continue


            now = time.perf_counter()
            update_stats = now >= self._next_input_stats_update
            if self._last_input_time > 0 and update_stats:
                delta = now - self._last_input_time
                if delta < 0.05:
                    if len(self._input_deltas) == self._input_deltas.maxlen:
                        self._input_delta_sum -= self._input_deltas[0]
                    self._input_deltas.append(delta)
                    self._input_delta_sum += delta
                    if len(self._input_deltas) == self._input_deltas.maxlen:
                        avg_delta = self._input_delta_sum / self._input_deltas.maxlen
                        new_is_high_speed_usb = (avg_delta <= 0.0015)
                        if getattr(self, '_logged_speed', None) is None:
                            self._logged_speed = True
                            rate = 1.0 / avg_delta if avg_delta > 0 else 0
                            logger.info(f"Wired USB Polling Rate Detected: {rate:.1f} Hz (avg interval: {avg_delta*1000:.2f} ms). High Speed: {new_is_high_speed_usb}")
                        self.is_high_speed_usb = new_is_high_speed_usb
                        self._next_input_stats_update = (
                            now if power_saving.is_off() else now + 0.25)
            self._last_input_time = now

            # Restore the recovery budget once input has been flowing again for a
            # while. Without this the budget is only ever refunded by a successful
            # rumble write, so an idle pad would permanently exhaust it after a few
            # unrelated glitches spread over a long session.
            if (self._recover_attempts
                    and time.time() - self._last_recover >= self._RECOVER_SETTLE):
                logger.debug("Wired USB HID input healthy again; resetting recovery budget")
                self._recover_attempts = 0
                self._stall_streak = 0


            report_id = data[0]
            if report_id in INPUT_REPORT_IDS:
                translated = self._translate_input_report(data)
                if translated is None:
                    continue
                cb = self._notify.get(INPUT_REPORT_UUID.lower())
                if cb:
                    try:
                        payload = (bytearray(translated) if power_saving.is_off()
                                   else translated)
                        cb(None, payload)
                    except Exception as e:
                        # Throttle: this runs ~500x/s, so log at most once per second.
                        now = time.time()
                        if now - getattr(self, "_last_cb_err_log", 0) >= 1.0:
                            self._last_cb_err_log = now
                            logger.exception("USB HID input callback failed: %s", e)
            else:
                # Treat anything else as a command/ack response: strip the report-id so
                # the body starts at the command id (write_command checks [0]==cmd,[1]==0x01).
                cb = self._notify.get(COMMAND_RESPONSE_UUID.lower())
                if cb:
                    try:
                        cb(None, bytearray(bytes(data[1:])))
                    except Exception:
                        logger.exception("USB HID command-response callback failed")

    def _notify_disconnect(self, reason):
        if self._read_stop.is_set() or self._disconnect_notified:
            return
        self._disconnect_notified = True
        self.is_connected = False
        self._rumble_stop.set()
        self._rumble_wake.set()
        # Drop the handle here rather than waiting for disconnect(). The transport is
        # already dead, and holding an open handle to it across the rescan only gives the
        # driver stack another reason to keep the device in a half-torn-down state.
        with self._write_lock:
            if self.dev is not None:
                try:
                    self.dev.close()
                except Exception:
                    logger.debug("Wired USB HID close on disconnect notify failed", exc_info=True)
                self.dev = None
        callback = self.on_disconnect_callback
        if callback is None:
            return
        try:
            callback(reason)
        except Exception:
            logger.debug("USB HID disconnect callback failed", exc_info=True)

    async def disconnect(self):
        self._read_stop.set()
        self._disconnect_notified = True
        self._rumble_stop.set()
        self._rumble_wake.set()
        # Joins go to a worker thread so the discoverer's event loop stays responsive
        # during teardown (see USBHidController.disconnect).
        if self._rumble_thread and self._rumble_thread.is_alive():
            await asyncio.to_thread(self._rumble_thread.join, 0.5)
        self._rumble_thread = None
        self._trace_rumble_event("disconnect", self._rumble_trace_gap_count,
                                 self._rumble_trace_overwrite_count, 0, 0)
        self._dump_rumble_trace("disconnect")
        self._set_timer_resolution(False)
        if self._read_thread and self._read_thread.is_alive():
            await asyncio.to_thread(self._read_thread.join, 0.5)
        self._read_thread = None
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None
        self.is_connected = False


_enum_log_state = {"last_seen": None}


def enumerate_wired_controllers(reason: str = "unspecified", candidate_path=None,
                                allow_global_fallback: bool = False,
                                pids=WIRED_USB_PIDS) -> list:
    """Return hidapi enumeration entries for supported wired Switch 2 pads.

    Robust across hidapi builds: tries the filtered enumerate first, then falls back
    to enumerating everything and filtering by VID/PID. Prefers the Generic-Desktop
    Gamepad/Joystick collection when the pad exposes multiple HID interfaces.
    """
    hid = _import_hid()
    if hid is None:
        return []

    pids = tuple(pids)
    t0 = time.perf_counter()
    used_global_fallback = False
    entries = []
    for pid in pids:
        try:
            entries.extend(hid.enumerate(NINTENDO_VENDOR_ID, pid) or [])
        except Exception as e:
            logger.debug("hid.enumerate(vid,0x%04x) failed: %s", pid, e)

    if candidate_path is not None:
        candidate_text = candidate_path.decode("utf-8", errors="ignore") if isinstance(candidate_path, bytes) else str(candidate_path)
        candidate_key = candidate_text.lower()

        def _path_text(d):
            path = d.get("path") or ""
            return path.decode("utf-8", errors="ignore") if isinstance(path, bytes) else str(path)

        matched_entries = [d for d in entries if _path_text(d).lower() == candidate_key]
        if matched_entries:
            entries = matched_entries

    if not entries and allow_global_fallback:
        # Some hidapi builds ignore the VID/PID filter — enumerate all and filter.
        try:
            alldev = hid.enumerate() or []
            used_global_fallback = True
        except Exception as e:
            logger.debug("hid.enumerate() failed: %s", e)
            alldev = []
        entries = [d for d in alldev
                   if d.get("vendor_id") == NINTENDO_VENDOR_ID
                   and d.get("product_id") in pids]
        # One-time visibility: log any Nintendo devices present so a wrong-mode /
        # wrong-PID controller (e.g. safe mode 0x2072) is diagnosable from the log.
        nin = sorted({(d.get("product_id"), (d.get("product_string") or ""))
                      for d in alldev if d.get("vendor_id") == NINTENDO_VENDOR_ID})
        if nin != _enum_log_state["last_seen"]:
            _enum_log_state["last_seen"] = nin
            if nin:
                logger.info("Wired USB: Nintendo HID devices present: %s", nin)
            else:
                logger.info("Wired USB: no Nintendo (VID 0x057E) HID devices found.")

    # Exclude our OWN virtual USBIP Switch 2 controllers — they share VID 057E/PID 2069
    # but advertise a "SWITCH2EMU..." serial. Without this the watcher would adopt the
    # app's own virtual pads and spawn more in a feedback loop.
    entries = [d for d in entries
               if "SWITCH2EMU" not in (d.get("serial_number") or "").upper()]

    def _priority(d):
        # usage_page 0x01 (Generic Desktop), usage 0x04 (Joystick) / 0x05 (Gamepad)
        if d.get("usage_page", 0) == 0x01 and d.get("usage", 0) in (0x04, 0x05):
            return 0
        return 1

    result = sorted(entries, key=_priority)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    if elapsed_ms >= 100 or used_global_fallback:
        logger.info(
            "Wired USB scan: reason=%s duration=%.1fms found=%d global_fallback=%s",
            reason,
            elapsed_ms,
            len(result),
            used_global_fallback,
        )
    return result


def enumerate_pro_controller2(reason: str = "unspecified", candidate_path=None,
                              allow_global_fallback: bool = False) -> list:
    """Backwards-compatible alias for :func:`enumerate_wired_controllers`."""
    return enumerate_wired_controllers(reason, candidate_path, allow_global_fallback)


class USBHidController(Controller):
    """A wired USB Pro Controller 2 or NSO GameCube Controller, driven through hidapi."""

    # Keep command round-trips short: on Windows the command endpoint may be
    # undeliverable, and initialize() issues several best-effort commands whose
    # failure should fall back to input-only quickly rather than stalling.
    COMMAND_TIMEOUT: float = 1.0

    def __init__(self, hid_entry: dict):
        import hashlib
        from usb_serial_bridge import DummyBleDevice
        path = hid_entry.get("path")
        product_id = int(hid_entry.get("product_id") or PRO_CONTROLLER2_PID)
        if product_id not in WIRED_USB_PIDS:
            product_id = PRO_CONTROLLER2_PID
        is_gamecube = product_id == NSO_GAMECUBE_CONTROLLER_PID
        serial = (hid_entry.get("serial_number") or "").strip()
        # Prefer a real 12-hex hardware id when HID exposes one; otherwise derive a
        # stable 12-hex-char pseudo-MAC from the unique HID instance path.
        path_key = path.decode("utf-8", "ignore") if isinstance(path, bytes) else str(path)
        serial_key = normalize_calibration_key(serial)
        if len(serial_key) == 12 and serial_key != "000000000000":
            address = serial_key
        else:
            address = hashlib.md5(path_key.encode("utf-8")).hexdigest()[:12].upper()
        super().__init__(DummyBleDevice(
            address,
            "USB NSO GameCube Controller" if is_gamecube else "USB Pro Controller 2"))

        self.hid_entry = hid_entry
        self.hid_path = path
        self.is_wired_usb = True
        self.usb_product_id = product_id
        # Bind the vendor command interface of THIS pad before anything uses it, so
        # initialisation and rumble cannot land on a different same-PID controller.
        self.usb_transport = resolve_command_transport(path, product_id)
        self.client = _UsbHidClient(path, product_id, self.usb_transport)
        # Three independent states. Collapsing them into one "transport" string is
        # what let "the startup commands were written" be misread as "the pad is in
        # report 0x0A and rumble works".
        self.command_transport = "none"   # native_winusb | pyusb | hid_fallback | none
        self.input_transport = "none"     # "report_XX" for the id actually seen, or none
        # (rumble_transport lives on the client, which is what does the writing)

        # Synthesize controller_info up-front (mirrors ESP32S3Controller) so the pipeline
        # has a product id even if the USB info-read command can't be delivered.
        self.controller_info = ControllerInfo.__new__(ControllerInfo)
        self.controller_info.serial_number = serial or address
        self.controller_info.vendor_id = NINTENDO_VENDOR_ID
        self.controller_info.product_id = product_id
        self.controller_info.color1 = b"\x00\x00\x00"
        self.controller_info.color2 = b"\xff\xff\xff"
        self.controller_info.color3 = b"\x2d\x2d\x2d"
        self.controller_info.color4 = b"\xff\xff\xff"
        self.controller_info.mac_address = address

        # Default stick calibration: center 2048, range 1500 (matches the ESP32 path's
        # DEFAULT_STICK_CALIBRATION). A real Pro Controller 2 stick doesn't reach the
        # full 0-4095 raw range, so a narrower range gives correct full-scale output.
        # Upgraded from flash later if the command channel works.
        #
        # GameCube has no SPI stick calibration, so it uses the same fixed full-range
        # calibration the Bluetooth GameCube path forces on itself.
        if is_gamecube:
            self.stick_calibration = make_fixed_stick_calibration()
            self.second_stick_calibration = make_fixed_stick_calibration()
        else:
            self.stick_calibration = StickCalibrationData(DEFAULT_STICK_CALIBRATION)
            self.second_stick_calibration = StickCalibrationData(DEFAULT_STICK_CALIBRATION)
        self.left_stick_calibration = self.stick_calibration
        self.right_stick_calibration = self.second_stick_calibration
        self.side_buttons_pressed = False
        self.battery_voltage = 3.7
        self.full_parity = False   # set True once command-based init/calibration succeeds
        self._loop = None
        self._disconnect_notified = False
        self.client.on_disconnect_callback = self._on_usb_hid_disconnected

    # --- Output reports are restricted for the wired pad ---
    # Command/feature/LED output reports can stop the default 0x05 input stream on
    # Windows, so those remain disabled. Rumble is a standalone HID output report and
    # is allowed through _UsbHidClient.write_gatt_char().

    async def write_command(self, command_id: int, subcommand_id: int, command_data=b""):
        raise Exception("write_command disabled on wired USB (output reports break input stream)")

    async def set_leds(self, *args, **kwargs):
        return

    async def play_vibration_preset(self, *args, **kwargs):
        return

    async def enableFeatures(self, *args, **kwargs):
        return

    async def trigger_connection_haptics(self):
        # The discoverer fires the connection buzz immediately after initialize(), but on
        # a freshly-enumerated wired pad the rumble subsystem isn't ready until the
        # startup/feature commands have been re-applied by _delayed_reinit (~1s). Firing
        # too early means the first-connect buzz is silent (later in-game rumble is fine).
        # Wait briefly so the connection haptic reliably plays on the first connect too.
        try:
            await asyncio.sleep(1.2)
        except asyncio.CancelledError:
            return
        if not getattr(self, "interp_running", False):
            return
        await Controller.trigger_connection_haptics(self)

    def _on_usb_hid_disconnected(self, reason="read_error"):
        if self._disconnect_notified:
            return
        self._disconnect_notified = True
        self.interp_running = False
        self._interp_wake_event.set()
        logger.info("Wired USB %s hardware disconnect detected (%s, %s)",
                    _device_label(self.usb_product_id), reason, self.device.address)
        callback = self.disconnected_callback
        if callback is None:
            return
        loop = getattr(self, "_loop", None)
        if loop is None or not loop.is_running():
            logger.debug("Wired USB disconnect callback dropped: discoverer loop unavailable")
            return

        async def _run_disconnect_callback():
            await callback(self)

        asyncio.run_coroutine_threadsafe(_run_disconnect_callback(), loop)

    async def initialize(self):
        """Initialize USB reports, then open hidapi and read the input stream.
        Startup commands go through the vendor bulk endpoint; arbitrary command HID
        output reports stay disabled after connect."""
        self._loop = asyncio.get_running_loop()
        label = _device_label(self.usb_product_id)
        self.client.open()
        # Runs before the read thread starts, so the pyusb route is still allowed here.
        transport = await asyncio.to_thread(
            initialize_pro_controller2_usb_reports, self.usb_product_id, self.usb_transport)
        if not transport:
            if await asyncio.to_thread(self.client.send_startup_reports_hid):
                transport = "hid_fallback"
        self._init_transport = transport or "none"
        self.command_transport = self._init_transport
        # The rumble writer lives on the client, so it needs the same answer.
        self.client.command_transport = self._init_transport
        if self._init_transport == "none":
            # Every route has been tried and none of them worked. Input is not
            # necessarily lost: the pad keeps streaming its power-on report, which the
            # GameCube path translates (report 0x05 fallback).
            logger.warning(
                "Wired USB command transport = none (%s): the vendor command endpoint "
                "could not be reached by any route. The pad will stay in its power-on "
                "input mode; motion data will be unavailable. Rumble is unaffected -- "
                "it goes out on the HID interface.",
                self.device.address)
        else:
            logger.info("Wired USB command transport = %s (%s)",
                        self._init_transport, self.device.address)
        self._log_usb_diagnostics()

        ensure_wired_controller_calibration_alias(self)
        gyro_cal_data = get_calibration_entry(getattr(CONFIG, "calibration_data", {}) or {}, self)
        if gyro_cal_data is not None:
            self.gyro_bias = tuple(gyro_cal_data)
            logger.info("Loaded wired USB gyro calibration for %s", self.device.address)
        else:
            self.gyro_bias = tuple(getattr(CONFIG, "gyro_bias_r", [0.0, 0.0, 0.0]))

        mag_cal_data = get_calibration_entry(getattr(CONFIG, "mag_calibration_data", {}) or {}, self)
        if mag_cal_data is not None:
            self.mag_bias = tuple(mag_cal_data)
            logger.info("Loaded wired USB mag calibration for %s", self.device.address)
        self.apply_in_app_joystick_calibration()

        # Start input streaming (base handler → our read thread) + the interpolation
        # thread the rest of the pipeline relies on. enable_input_notify_callback() only
        # subscribes (no writes) for a non-GameCube controller.
        await self.enable_input_notify_callback()

        self.interp_running = True
        self.interp_thread = threading.Thread(target=self._interpolation_thread_loop, daemon=True)
        self.interp_thread.start()

        self.connected_at = time.time()
        self.last_input_time = time.time()
        logger.info(
            "Wired USB %s initialized (%s, input + rumble; commands disabled)",
            label, self.device.address,
        )

        # Fresh-enumeration timing fix: the very first startup sequence after a WinUSB
        # (re)bind can only partially apply — input streams, but the feature-enable that
        # populates battery/current and the settings behind rumble don't take, and the pad
        # only works fully after an app restart. Re-send the startup commands (idempotent,
        # via the interface-1 bulk endpoint so the interface-0 input stream is untouched)
        # a moment later, once the freshly-enumerated controller is fully booted.
        self._reinit_task = asyncio.create_task(self._delayed_reinit())

    @property
    def expected_input_report_id(self) -> int:
        """The report id this pad emits once the startup commands have taken."""
        return (REPORT_ID_GAMECUBE
                if self.usb_product_id == NSO_GAMECUBE_CONTROLLER_PID
                else REPORT_ID_COMMON)

    @property
    def is_input_fallback(self) -> bool:
        """True when the pad never switched to the report the startup commands ask for.

        For GameCube that means the translated power-on report: usable, but without
        motion. For the Pro Controller 2 it means its own report 0x09 instead of the
        common 0x05, which likewise costs motion.
        """
        return self.input_transport not in ("none", f"report_{self.expected_input_report_id:02x}")

    def _refresh_input_transport(self) -> str:
        """Name the input mode after the report id that actually arrived.

        This is the only trustworthy signal. A successful command write means the OS
        accepted the bytes, not that the pad changed mode, so init success must never
        be promoted straight to "the pad is in its native report".
        """
        observed = getattr(self.client, "last_input_report_id", None) if self.client else None
        self.input_transport = "none" if observed is None else f"report_{observed:02x}"
        return self.input_transport

    def _log_usb_diagnostics(self):
        """One structured line per connect.

        "Driver installed but no input", "only the first pad works" and "the write
        succeeded but nothing vibrated" are otherwise indistinguishable in a log.
        """
        transport = self.usb_transport
        binding = winusb_binding_state(self.usb_product_id)
        logger.info(
            "Wired USB diagnostics (%s): pad=%s hid_path=%s hid_instance=%s "
            "mi01_instance=%s winusb_paths=%d transport=%s",
            self.device.address,
            _device_label(self.usb_product_id),
            self.hid_path,
            getattr(transport, "hid_instance_id", None),
            getattr(transport, "mi01_instance_id", None),
            len(getattr(transport, "winusb_paths", []) or []),
            transport.describe() if transport is not None else "none",
        )
        logger.info(
            "Wired USB diagnostics (%s): mi01_state=%s mi01_service=%s "
            "ms_comp_winusb=%s iface_guids=%s | command=%s input=%s rumble=%s bulk_ok=%s",
            self.device.address,
            binding["state"], binding["service"], binding["ms_comp"],
            binding["has_guids"],
            self.command_transport,
            self.input_transport,
            getattr(self.client, "rumble_transport", "none") if self.client else "none",
            self.client.bulk_rumble_available() if self.client else False,
        )

    async def _delayed_reinit(self):
        # Re-apply over whichever route actually worked at init. This used to always call
        # initialize_pro_controller2_usb_reports(), which on a machine without the WinUSB
        # binding is two silent no-ops -- the pad never received its feature-enable and
        # only ever worked after an app restart.
        transport = getattr(self, "_init_transport", "none")
        if transport == "none":
            logger.warning(
                "Wired USB re-init skipped (%s): no working command transport",
                self.device.address)
            return
        try:
            for delay in (0.8, 1.8):
                await asyncio.sleep(delay)
                if not self.interp_running or self.client is None:
                    return
                # Deliberately unconditional. An earlier version skipped this once the
                # expected report id was arriving, on the theory that the commands had
                # demonstrably taken -- but the report id only proves the *report
                # selection* command landed (0x03/0x0A). It says nothing about the
                # feature-enable pair (0x0C/0x02 + 0x0C/0x04, mask 0x27) whose bit 5 is
                # rumble, and that is precisely what the first startup pass tends to
                # miss. Skipping here silently killed wired Pro Controller 2 rumble and
                # battery reporting, because the pad streams report 0x05 within
                # milliseconds and the re-send never happened.
                #
                # The observation below is kept for diagnostics only; it must not gate
                # the re-send. The commands are idempotent.
                self._refresh_input_transport()
                if transport == "hid_fallback":
                    ok = await asyncio.to_thread(self.client.send_startup_reports_hid)
                else:
                    ok = await asyncio.to_thread(
                        initialize_pro_controller2_usb_reports,
                        self.usb_product_id, self.usb_transport)
                if not ok:
                    # Do not claim success we did not verify: this re-init is what makes
                    # battery reporting and the rumble settings take effect, so a silent
                    # failure here used to look identical to a working connect.
                    logger.warning(
                        "Wired USB re-init via %s did not complete (%s); battery "
                        "reporting or motion may be unavailable",
                        transport, self.device.address)
                    return
            # Give the pad a moment to act on the last batch, then report what it is
            # actually doing rather than what we hoped it would do.
            await asyncio.sleep(0.5)
            self._refresh_input_transport()
            if self.is_input_fallback:
                logger.warning(
                    "Wired USB %s is still streaming %s after re-init (%s), not the "
                    "expected report 0x%02X; running on translated input -- buttons, "
                    "sticks, analog triggers and rumble work; motion data does not.",
                    _device_label(self.usb_product_id), self.input_transport,
                    self.device.address, self.expected_input_report_id)
            else:
                logger.info("Wired USB %s startup commands re-applied via %s (%s)",
                            _device_label(self.usb_product_id), transport,
                            self.device.address)
            self._log_usb_diagnostics()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Wired USB re-init failed", exc_info=True)

    async def disconnect(self):
        self._disconnect_notified = True
        self.interp_running = False
        self._interp_wake_event.set()
        task = getattr(self, "_reinit_task", None)
        if task is not None:
            task.cancel()
        # This class overrides Controller.disconnect() entirely, so the base class never
        # gets to stop the threads it starts in __init__. Do it here.
        self._stop_worker_threads()
        # Off the event loop: these are blocking joins, and the discoverer's wired watcher
        # runs on this same loop. Blocking it here left WIRED_RESCAN_EVENT unserviced, so
        # pressing manual scan during a teardown appeared to do nothing.
        if hasattr(self, "interp_thread") and self.interp_thread.is_alive():
            await asyncio.to_thread(self.interp_thread.join, 0.5)
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                logger.debug("USB HID disconnect error (ignored)", exc_info=True)
            self.client = None
        logger.info("Wired USB %s disconnected (%s)",
                    _device_label(self.usb_product_id), self.device.address)
