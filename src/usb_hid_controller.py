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

"""Wired USB support for the Switch 2 Pro Controller 2.

A physically-connected Pro Controller 2 (USB, VID 0x057E / PID 0x2069) is adapted
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

Windows note: sending command HID *output* reports to Nintendo pads is not always
possible (the command endpoint can live on a separate USB interface needing
WinUSB/libusb).  This module therefore degrades gracefully: input still works from
the controller's default report stream even when init/calibration commands can't be
delivered. Rumble uses the dedicated Pro Controller 2 output report and is sent
best-effort through hidapi.
"""

import logging
import threading
import time
import asyncio
import os

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
    NINTENDO_VENDOR_ID,
    PRO_CONTROLLER2_PID,
)
from usb_serial_bridge import MockService, DEFAULT_STICK_CALIBRATION

logger = logging.getLogger(__name__)

# SW2 GATT service UUID (used only so initialize()'s SW2-detection branch runs).
SW2_SERVICE_UUID = "ab7de9be-89fe-49ad-828f-118f09df7fd0"

# USB HID report IDs (from switch2_controller_research hid_reports.md report map).
REPORT_ID_COMMON = 0x05          # Input report common to all controllers (default/pre-init)
REPORT_ID_PRO2 = 0x09            # Pro Controller 2 specific input report
REPORT_ID_GAMECUBE = 0x0A        # NSO GameCube specific input report (not handled here)
INPUT_REPORT_IDS = (REPORT_ID_COMMON, REPORT_ID_PRO2, REPORT_ID_GAMECUBE)
OUTPUT_REPORT_ID_PRO2 = 0x02     # Pro Controller 2 output report (commands + rumble)
PRO2_OUTPUT_REPORT_BODY_SIZE = 0x2A
USB_COMMAND_ENDPOINT_OUT = 0x02
USB_COMMAND_INTERFACE = 1

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


_hid_import_warned = False
_pyusb_import_warned = False
_native_winusb_warned = False


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


def _pro2_winusb_interface_guids() -> list[str]:
    try:
        import winreg
    except Exception:
        return []

    guids: list[str] = []
    base_path = r"SYSTEM\CurrentControlSet\Enum\USB\VID_057E&PID_2069&MI_01"
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
    return guids


def _winusb_device_paths(interface_guid: str) -> list[str]:
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
                if "vid_057e&pid_2069&mi_01" in path.lower() and "switch2emu" not in path.lower():
                    paths.append(path)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(info_set)
    return paths


def _write_commands_native_winusb(commands) -> bool:
    global _native_winusb_warned
    if os.name != "nt":
        return False

    import ctypes
    from ctypes import wintypes

    paths: list[str] = []
    for guid in _pro2_winusb_interface_guids():
        paths.extend(_winusb_device_paths(guid))
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
            logger.info("Wired USB Pro Controller 2 commands sent via native WinUSB")
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


def _write_startup_reports_native_winusb() -> bool:
    return _write_commands_native_winusb((
        USB_INIT_COMMAND,
        USB_SET_LED_COMMAND,
        USB_SET_FEATURE_MASK_COMMAND,
        USB_ENABLE_FEATURES_COMMAND,
        USB_SELECT_COMMON_REPORT_COMMAND,
    ))


def send_pro_controller2_usb_command(command: bytes) -> bool:
    if _write_commands_native_winusb((bytes(command),)):
        return True

    usb_core, usb_util, backend = _import_pyusb()
    if usb_core is None or usb_util is None:
        return False
    dev = None
    claimed = False
    try:
        dev = usb_core.find(idVendor=NINTENDO_VENDOR_ID, idProduct=PRO_CONTROLLER2_PID, backend=backend)
        if dev is None:
            return False
        try:
            dev.set_configuration()
        except Exception:
            pass
        try:
            usb_util.claim_interface(dev, USB_COMMAND_INTERFACE)
            claimed = True
        except Exception:
            pass
        dev.write(USB_COMMAND_ENDPOINT_OUT, bytes(command), 1000)
        return True
    except Exception as e:
        logger.debug("Wired USB command write failed: %s", e)
        return False
    finally:
        if dev is not None:
            if claimed:
                try:
                    usb_util.release_interface(dev, USB_COMMAND_INTERFACE)
                except Exception:
                    pass
            try:
                usb_util.dispose_resources(dev)
            except Exception:
                pass


def initialize_pro_controller2_usb_reports() -> bool:
    """Send the startup commands required before a Pro Controller 2 streams USB input."""
    if _write_startup_reports_native_winusb():
        return True

    usb_core, usb_util, backend = _import_pyusb()
    if usb_core is None or usb_util is None:
        return False

    dev = None
    claimed = False
    try:
        dev = usb_core.find(idVendor=NINTENDO_VENDOR_ID, idProduct=PRO_CONTROLLER2_PID, backend=backend)
        if dev is None:
            logger.debug("Wired USB init: Pro Controller 2 USB device not found")
            return False

        try:
            dev.set_configuration()
        except Exception:
            pass

        try:
            usb_util.claim_interface(dev, USB_COMMAND_INTERFACE)
            claimed = True
        except Exception:
            logger.debug("Wired USB init: claim interface %d failed; trying writes anyway",
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
            logger.debug("Wired USB init: endpoint scan failed; using 0x%02x",
                         endpoint_out, exc_info=True)

        for command in (
            USB_INIT_COMMAND,
            USB_SET_LED_COMMAND,
            USB_SET_FEATURE_MASK_COMMAND,
            USB_ENABLE_FEATURES_COMMAND,
            USB_SELECT_COMMON_REPORT_COMMAND,
        ):
            dev.write(endpoint_out, command, 1000)
            time.sleep(0.02)

        logger.info("Wired USB Pro Controller 2 startup commands sent on endpoint 0x%02x",
                    endpoint_out)
        return True
    except Exception as e:
        logger.warning("Wired USB Pro Controller 2 startup commands failed: %s", e)
        return False
    finally:
        if dev is not None:
            if claimed:
                try:
                    usb_util.release_interface(dev, USB_COMMAND_INTERFACE)
                except Exception:
                    pass
            try:
                usb_util.dispose_resources(dev)
            except Exception:
                pass


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


def translate_usb_report(data) -> bytes | None:
    """Translate a raw USB HID input report into the internal buffer layout that
    ``ControllerInputData`` (non-GameCube branch) expects, i.e. the SW2 "report 0x05"
    layout: counter[0:4], buttons u32[4:8], left stick[10:13], right stick[13:16],
    battery[31:33], accel[48:54], gyro[54:60].

    ``data`` is the report *including* its leading report-id byte (list[int] or bytes).
    Returns ``None`` for reports we don't consume (e.g. GameCube 0x0A).
    """
    if not data:
        return None
    report_id = data[0]

    if report_id == REPORT_ID_COMMON:
        # Report 0x05 payload is already exactly the layout the parser wants — just
        # drop the report-id byte.
        body = bytes(data[1:])
        if len(body) < 60:
            body = body.ljust(64, b"\x00")
        return body

    if report_id == REPORT_ID_PRO2:
        # Report 0x09 (Pro Controller 2 default): data[0]=report id, then body
        # Counter[0]->data[1], Power[1]->data[2], Buttons[2:5]->data[3:6],
        # LStick[5:8]->data[6:9], RStick[8:11]->data[9:12]. Motion is an
        # undocumented packed format, so gyro/accel are left zeroed.
        if len(data) < 12:
            return None
        buf = bytearray(64)
        buf[0] = data[1]                      # counter (low byte)
        buttons = _pro2_buttons_to_u32(data[3], data[4], data[5])
        buf[4] = buttons & 0xFF
        buf[5] = (buttons >> 8) & 0xFF
        buf[6] = (buttons >> 16) & 0xFF
        buf[7] = (buttons >> 24) & 0xFF
        buf[10:13] = bytes(data[6:9])         # left stick (packed 12-bit)
        buf[13:16] = bytes(data[9:12])        # right stick (packed 12-bit)
        # Battery: report 0x09 has no voltage field, only a Power-Info byte
        # (bits [2:5] = level 0-9). Synthesize a Li-ion-ish voltage into the
        # [31:33] field ControllerInputData reads, so the battery icon is correct
        # even when the pad streams 0x09 instead of 0x05.
        power = data[2]
        level = min((power >> 2) & 0x0F, 9)
        volt_mv = 3100 + level * 110          # ~3.10V (empty) .. ~4.09V (full)
        buf[31] = volt_mv & 0xFF
        buf[32] = (volt_mv >> 8) & 0xFF
        return bytes(buf)

    # 0x0A (GameCube) or anything else: not a Pro Controller 2 input report.
    return None


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

    def __init__(self, path):
        self.path = path
        self.dev = None
        self.is_connected = False
        self.services = [MockService(SW2_SERVICE_UUID)]
        self._notify = {}          # lowercased uuid -> callback
        self._read_thread = None
        self._read_stop = threading.Event()
        self._write_lock = threading.Lock()
        self.is_high_speed_usb = False
        self._input_deltas = []
        self._last_input_time = 0.0
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
        self._rumble_wake = threading.Event()
        self._rumble_stop = threading.Event()
        self._rumble_thread = None
        self._hid_rumble_ok = False         # a hid output write has succeeded at least once
        self._hid_rumble_fail_streak = 0
        self._last_bulk_fallback = 0.0
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
        self.on_disconnect_callback = None
        self._disconnect_notified = False

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
        if str(uuid).lower() != VIBRATION_WRITE_PRO_CONTROLLER_UUID.lower():
            return
        with self._rumble_slot_lock:
            self._rumble_slot = bytes(data)
        self._ensure_rumble_thread()
        self._rumble_wake.set()

    def _ensure_rumble_thread(self):
        if self._rumble_thread and self._rumble_thread.is_alive():
            return
        self._rumble_stop.clear()
        self._set_timer_resolution(True)
        self._rumble_thread = threading.Thread(target=self._rumble_write_loop, daemon=True)
        self._rumble_thread.start()

    def _set_timer_resolution(self, enable: bool) -> None:
        """Raise/restore Windows multimedia timer resolution to 1 ms so the writer
        thread's 15 ms pacing is honoured (default granularity is ~15.6 ms). No-op
        off Windows; balanced by a matching timeEndPeriod on stop."""
        if os.name != "nt":
            return
        try:
            import ctypes
            if enable and not self._timer_res_raised:
                ctypes.windll.winmm.timeBeginPeriod(1)
                self._timer_res_raised = True
            elif not enable and self._timer_res_raised:
                ctypes.windll.winmm.timeEndPeriod(1)
                self._timer_res_raised = False
        except Exception as e:
            logger.debug("timeBeginPeriod/timeEndPeriod failed: %s", e)

    def _rumble_write_loop(self):
        # Minimum spacing between wire writes. HD rumble frames carry 3x5 ms of
        # We dynamically adjust the interval:
        # - 15ms (66Hz) for traditional vibration (safe, proven)
        # - 25ms (40Hz) for audio haptics (prevents USB buffer overrun from dense payloads)
        # The interval is evaluated per frame based on the Controller state.
        # After this many consecutive inactive frames, stop re-sending silence.
        SILENCE_KEEP = 3
        last_write = 0.0
        while not self._rumble_stop.is_set():
            self._rumble_wake.wait(0.5)
            if self._rumble_stop.is_set():
                break
            self._rumble_wake.clear()
            with self._rumble_slot_lock:
                data = self._rumble_slot
                self._rumble_slot = None
            if data is None:
                continue

            interval = 0.015

            if time.perf_counter() < self._congested_until:
                interval = max(interval, self._congest_interval)
            
            now = time.perf_counter()
            target_time = last_write + interval
            if now < target_time:
                sleep_amount = target_time - now
                if sleep_amount > 0.002:
                    # Sleep most of the way, leaving 2ms for spin-wait accuracy
                    time.sleep(sleep_amount - 0.002)
                # Spin-wait the remaining time to guarantee strict interval
                while time.perf_counter() < target_time:
                    pass
            # Silence suppression: keep the motor definitively stopped by sending a
            # few zero frames, then stop touching the wire until real motion returns.
            if _pro2_rumble_payload_is_active(data):
                self._inactive_run = 0
            else:
                self._inactive_run += 1
                if self._inactive_run > SILENCE_KEEP:
                    continue

            last_write = time.perf_counter()
            self._write_rumble_frame(data)

    def _write_rumble_frame(self, data):
        t0 = time.perf_counter()
        written = None
        try:
            written = self.write_output_report(data, OUTPUT_REPORT_ID_PRO2)
        except Exception:
            logger.debug("Wired USB HID output rumble write failed", exc_info=True)
        elapsed = time.perf_counter() - t0
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

        # hidapi's device.write() returns the byte count on success and -1 on failure.
        if written is not None and written > 0:
            self._hid_rumble_ok = True
            self._hid_rumble_fail_streak = 0
            self._stall_streak = 0
            self._recover_attempts = 0
            return

        self._hid_rumble_fail_streak += 1

        # Only fall back to the expensive Bulk/WinUSB path when the device has NEVER
        # accepted a hid report (init-time transport probe). Once hid writes have
        # succeeded, a failure is a stall to be healed by reopen -- not a reason to
        # hammer set_configuration() on interface 1 (which makes recovery impossible).
        now = time.time()
        if not self._hid_rumble_ok and now - self._last_bulk_fallback >= 0.5:
            self._last_bulk_fallback = now
            self.write_rumble_command(data)

    def write_rumble_command(self, data):
        active = _pro2_rumble_payload_is_active(data)
        now = time.time()
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

    def write_output_report(self, data, report_id=OUTPUT_REPORT_ID_PRO2):
        self.open()
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
                
        # BLACKBOX RECORD
        if not getattr(self, "_blackbox_frozen", False):
            if not hasattr(self, "_blackbox_history"):
                self._blackbox_history = []
                
            elapsed = time.perf_counter() - t0
            self._blackbox_history.append((time.time(), written, elapsed * 1000, report.hex()))
            if len(self._blackbox_history) > 32:
                self._blackbox_history.pop(0)
            
        return written

    def write_command_report(self, command: bytes):
        self.open()
        report = bytes([OUTPUT_REPORT_ID_PRO2]) + bytes(command).ljust(PRO2_OUTPUT_REPORT_BODY_SIZE, b"\x00")
        with self._write_lock:
            try:
                return self.dev.write(report)
            except TypeError:
                return self.dev.write(list(report))

    def send_startup_reports_hid(self) -> bool:
        """Fallback for systems where interface 1 is not reachable through pyusb."""
        try:
            for command in (
                USB_INIT_COMMAND,
                USB_SET_LED_COMMAND,
                USB_SET_FEATURE_MASK_COMMAND,
                USB_ENABLE_FEATURES_COMMAND,
                USB_SELECT_COMMON_REPORT_COMMAND,
            ):
                self.write_command_report(command)
                time.sleep(0.02)
            logger.info("Wired USB Pro Controller 2 startup commands sent via HID output report fallback")
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
                logger.info("USB HID read loop ended: %s", e)
                self._notify_disconnect("read_error")
                break
            if not data:
                continue
                
            now = time.perf_counter()
            if self._last_input_time > 0:
                delta = now - self._last_input_time
                if delta < 0.05:
                    self._input_deltas.append(delta)
                    if len(self._input_deltas) > 50:
                        self._input_deltas.pop(0)
                        avg_delta = sum(self._input_deltas) / 50.0
                        new_is_high_speed_usb = (avg_delta <= 0.0015)
                        if getattr(self, '_logged_speed', None) is None:
                            self._logged_speed = True
                            rate = 1.0 / avg_delta if avg_delta > 0 else 0
                            logger.info(f"Wired USB Polling Rate Detected: {rate:.1f} Hz (avg interval: {avg_delta*1000:.2f} ms). High Speed: {new_is_high_speed_usb}")
                        self.is_high_speed_usb = new_is_high_speed_usb
            self._last_input_time = now
            
            report_id = data[0]
            if report_id in INPUT_REPORT_IDS:
                translated = translate_usb_report(data)
                if translated is None:
                    continue
                cb = self._notify.get(INPUT_REPORT_UUID.lower())
                if cb:
                    try:
                        cb(None, bytearray(translated))
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
        if self._rumble_thread and self._rumble_thread.is_alive():
            self._rumble_thread.join(timeout=0.5)
        self._rumble_thread = None
        self._set_timer_resolution(False)
        if self._read_thread and self._read_thread.is_alive():
            self._read_thread.join(timeout=0.5)
        self._read_thread = None
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None
        self.is_connected = False


_enum_log_state = {"last_seen": None}


def enumerate_pro_controller2(reason: str = "unspecified", candidate_path=None, allow_global_fallback: bool = False) -> list:
    """Return hidapi enumeration entries for wired Pro Controller 2 devices.

    Robust across hidapi builds: tries the filtered enumerate first, then falls back
    to enumerating everything and filtering by VID/PID. Prefers the Generic-Desktop
    Gamepad/Joystick collection when the pad exposes multiple HID interfaces.
    """
    hid = _import_hid()
    if hid is None:
        return []

    t0 = time.perf_counter()
    used_global_fallback = False
    entries = []
    try:
        entries = hid.enumerate(NINTENDO_VENDOR_ID, PRO_CONTROLLER2_PID) or []
    except Exception as e:
        logger.debug("hid.enumerate(vid,pid) failed: %s", e)

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
                   and d.get("product_id") == PRO_CONTROLLER2_PID]
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


class USBHidController(Controller):
    """A wired USB Pro Controller 2, driven through hidapi."""

    # Keep command round-trips short: on Windows the command endpoint may be
    # undeliverable, and initialize() issues several best-effort commands whose
    # failure should fall back to input-only quickly rather than stalling.
    COMMAND_TIMEOUT: float = 1.0

    def __init__(self, hid_entry: dict):
        import hashlib
        from usb_serial_bridge import DummyBleDevice
        path = hid_entry.get("path")
        serial = (hid_entry.get("serial_number") or "").strip()
        # Prefer a real 12-hex hardware id when HID exposes one; otherwise derive a
        # stable 12-hex-char pseudo-MAC from the unique HID instance path.
        path_key = path.decode("utf-8", "ignore") if isinstance(path, bytes) else str(path)
        serial_key = normalize_calibration_key(serial)
        if len(serial_key) == 12 and serial_key != "000000000000":
            address = serial_key
        else:
            address = hashlib.md5(path_key.encode("utf-8")).hexdigest()[:12].upper()
        super().__init__(DummyBleDevice(address, "USB Pro Controller 2"))

        self.hid_entry = hid_entry
        self.hid_path = path
        self.is_wired_usb = True
        self.client = _UsbHidClient(path)

        # Synthesize controller_info up-front (mirrors ESP32S3Controller) so the pipeline
        # has a product id even if the USB info-read command can't be delivered.
        self.controller_info = ControllerInfo.__new__(ControllerInfo)
        self.controller_info.serial_number = serial or address
        self.controller_info.vendor_id = NINTENDO_VENDOR_ID
        self.controller_info.product_id = PRO_CONTROLLER2_PID
        self.controller_info.color1 = b"\x00\x00\x00"
        self.controller_info.color2 = b"\xff\xff\xff"
        self.controller_info.color3 = b"\x2d\x2d\x2d"
        self.controller_info.color4 = b"\xff\xff\xff"
        self.controller_info.mac_address = address

        # Default stick calibration: center 2048, range 1500 (matches the ESP32 path's
        # DEFAULT_STICK_CALIBRATION). A real Pro Controller 2 stick doesn't reach the
        # full 0-4095 raw range, so a narrower range gives correct full-scale output.
        # Upgraded from flash later if the command channel works.
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
        logger.info("Wired USB Pro Controller 2 hardware disconnect detected (%s, %s)", reason, self.device.address)
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
        self.client.open()
        winusb_ok = await asyncio.to_thread(initialize_pro_controller2_usb_reports)
        if not winusb_ok:
            await asyncio.to_thread(self.client.send_startup_reports_hid)

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
            "Wired USB Pro Controller 2 initialized (%s, input + rumble; commands disabled)",
            self.device.address,
        )

        # Fresh-enumeration timing fix: the very first startup sequence after a WinUSB
        # (re)bind can only partially apply — input streams, but the feature-enable that
        # populates battery/current and the settings behind rumble don't take, and the pad
        # only works fully after an app restart. Re-send the startup commands (idempotent,
        # via the interface-1 bulk endpoint so the interface-0 input stream is untouched)
        # a moment later, once the freshly-enumerated controller is fully booted.
        self._reinit_task = asyncio.create_task(self._delayed_reinit())

    async def _delayed_reinit(self):
        try:
            for delay in (0.8, 1.8):
                await asyncio.sleep(delay)
                if not self.interp_running:
                    return
                await asyncio.to_thread(initialize_pro_controller2_usb_reports)
            logger.info("Wired USB Pro Controller 2 startup commands re-applied (%s)", self.device.address)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Wired USB re-init failed", exc_info=True)

    async def disconnect(self):
        self._disconnect_notified = True
        self.interp_running = False
        task = getattr(self, "_reinit_task", None)
        if task is not None:
            task.cancel()
        if hasattr(self, "interp_thread") and self.interp_thread.is_alive():
            self.interp_thread.join(timeout=0.5)
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                logger.debug("USB HID disconnect error (ignored)", exc_info=True)
            self.client = None
        logger.info("Wired USB Pro Controller 2 disconnected (%s)", self.device.address)
