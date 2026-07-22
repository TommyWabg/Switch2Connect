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

import ctypes
import os
import sys
import logging
import time
_PERF_DIAGNOSTICS = os.environ.get('SWITCH2_PERF_DIAGNOSTICS', '0') == '1'

logger = logging.getLogger(__name__)

# Built-in diagnostics remain rate-limited below. Never emit one log per native
# WinUHid callback: doing so would hold the GIL and stall other controllers.

# Load the DLLs
try:
    from config import get_driver_path, get_app_root
    winuhid_path = get_driver_path("WinUHid.dll")
    winuhid_devs_path = get_driver_path("WinUHidDevs.dll")
    
    if not os.path.exists(winuhid_path):
        # Try WinUHid-main build directory
        winuhid_path = os.path.join(get_app_root(), "WinUHid-main", "build", "Release", "x64", "WinUHid.dll")
        winuhid_devs_path = os.path.join(get_app_root(), "WinUHid-main", "build", "Release", "x64", "WinUHidDevs.dll")
        if not os.path.exists(winuhid_path):
            winuhid_path = os.path.join(get_app_root(), "external", "WinUHid-main", "build", "Release", "x64", "WinUHid.dll")
            winuhid_devs_path = os.path.join(get_app_root(), "external", "WinUHid-main", "build", "Release", "x64", "WinUHidDevs.dll")

    # Load WinUHid.dll first
    _winuhid = ctypes.CDLL(winuhid_path)
    # Then load WinUHidDevs.dll
    _winuhid_devs = ctypes.CDLL(winuhid_devs_path)
    logger.info("Successfully loaded WinUHid DLLs")
except Exception as e:
    logger.error(f"Failed to load WinUHid DLLs: {e}")
    _winuhid = None
    _winuhid_devs = None
# Custom device structures for WinUHid.dll
class GUID(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("Data1", ctypes.c_uint),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8)
    ]

class WINUHID_DEVICE_CONFIG(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("SupportedEvents", ctypes.c_int),
        ("VendorID", ctypes.c_ushort),
        ("ProductID", ctypes.c_ushort),
        ("VersionNumber", ctypes.c_ushort),
        ("ReportDescriptorLength", ctypes.c_ushort),
        ("ReportDescriptor", ctypes.c_void_p),
        ("ContainerId", GUID),
        ("InstanceID", ctypes.c_wchar_p),
        ("HardwareIDs", ctypes.c_wchar_p),
        ("ReadReportPeriodUs", ctypes.c_uint)
    ]

class WINUHID_EVENT_WRITE(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("DataLength", ctypes.c_ulong),
        ("Data", ctypes.c_ubyte * 1024)
    ]

class WINUHID_EVENT_READ(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("DataLength", ctypes.c_ulong)
    ]

class WINUHID_EVENT_UNION(ctypes.Union):
    _pack_ = 1
    _fields_ = [
        ("Write", WINUHID_EVENT_WRITE),
        ("Read", WINUHID_EVENT_READ)
    ]

class WINUHID_EVENT(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("Type", ctypes.c_int),
        ("RequestId", ctypes.c_ulong),
        ("ReportId", ctypes.c_ubyte),
        ("u", WINUHID_EVENT_UNION)
    ]

WINUHID_EVENT_NONE = 0x0
WINUHID_EVENT_GET_FEATURE = 0x1
WINUHID_EVENT_SET_FEATURE = 0x2
WINUHID_EVENT_WRITE_REPORT = 0x4
WINUHID_EVENT_READ_REPORT = 0x8

WINUHID_EVENT_CALLBACK = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(WINUHID_EVENT))

# Structs for PS4
class WINUHID_PS4_TOUCH_POINT(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("ContactSeq", ctypes.c_ubyte),
        ("XLowPart", ctypes.c_ubyte),
        ("XHighPart", ctypes.c_ubyte, 4),
        ("YLowPart", ctypes.c_ubyte, 4),
        ("YHighPart", ctypes.c_ubyte)
    ]

class WINUHID_PS4_TOUCH_REPORT(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("Timestamp", ctypes.c_ubyte),
        ("TouchPoints", WINUHID_PS4_TOUCH_POINT * 2)
    ]

# Structs for PS4
class WINUHID_PS4_INPUT_REPORT(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("ReportId", ctypes.c_ubyte),
        ("LeftStickX", ctypes.c_ubyte),
        ("LeftStickY", ctypes.c_ubyte),
        ("RightStickX", ctypes.c_ubyte),
        ("RightStickY", ctypes.c_ubyte),
        
        # Bitfields (1 byte total)
        ("Hat", ctypes.c_ubyte, 4),
        ("ButtonSquare", ctypes.c_ubyte, 1),
        ("ButtonCross", ctypes.c_ubyte, 1),
        ("ButtonCircle", ctypes.c_ubyte, 1),
        ("ButtonTriangle", ctypes.c_ubyte, 1),
        
        # Bitfields (1 byte total)
        ("ButtonL1", ctypes.c_ubyte, 1),
        ("ButtonR1", ctypes.c_ubyte, 1),
        ("ButtonL2", ctypes.c_ubyte, 1),
        ("ButtonR2", ctypes.c_ubyte, 1),
        ("ButtonShare", ctypes.c_ubyte, 1),
        ("ButtonOptions", ctypes.c_ubyte, 1),
        ("ButtonL3", ctypes.c_ubyte, 1),
        ("ButtonR3", ctypes.c_ubyte, 1),
        
        # Bitfields (1 byte total)
        ("ButtonHome", ctypes.c_ubyte, 1),
        ("ButtonTouchpad", ctypes.c_ubyte, 1),
        ("Reserved", ctypes.c_ubyte, 6),
        
        ("LeftTrigger", ctypes.c_ubyte),
        ("RightTrigger", ctypes.c_ubyte),
        ("Timestamp", ctypes.c_ushort),
        ("BatteryLevel", ctypes.c_ubyte),
        
        ("GyroX", ctypes.c_short),
        ("GyroY", ctypes.c_short),
        ("GyroZ", ctypes.c_short),
        ("AccelX", ctypes.c_short),
        ("AccelY", ctypes.c_short),
        ("AccelZ", ctypes.c_short),
        
        ("Reserved2", ctypes.c_ubyte * 5),
        ("BatteryLevelSpecial", ctypes.c_ubyte),
        ("Status", ctypes.c_ubyte * 2),
        
        ("TouchReportCount", ctypes.c_ubyte),
        ("TouchReports", WINUHID_PS4_TOUCH_REPORT * 3),
        ("Reserved3", ctypes.c_ubyte * 3)
    ]

class WINUHID_PS5_TOUCH_POINT(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("ContactSeq", ctypes.c_ubyte),
        ("XLowPart", ctypes.c_ubyte),
        ("XHighPart", ctypes.c_ubyte, 4),
        ("YLowPart", ctypes.c_ubyte, 4),
        ("YHighPart", ctypes.c_ubyte)
    ]

class WINUHID_PS5_TOUCH_REPORT(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("TouchPoints", WINUHID_PS5_TOUCH_POINT * 2),
        ("Timestamp", ctypes.c_ubyte)
    ]

# Structs for PS5
class WINUHID_PS5_INPUT_REPORT(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("ReportId", ctypes.c_ubyte),
        ("LeftStickX", ctypes.c_ubyte),
        ("LeftStickY", ctypes.c_ubyte),
        ("RightStickX", ctypes.c_ubyte),
        ("RightStickY", ctypes.c_ubyte),
        ("LeftTrigger", ctypes.c_ubyte),
        ("RightTrigger", ctypes.c_ubyte),
        ("SequenceNumber", ctypes.c_ubyte),
        
        # Bitfields (1 byte total)
        ("Hat", ctypes.c_ubyte, 4),
        ("ButtonSquare", ctypes.c_ubyte, 1),
        ("ButtonCross", ctypes.c_ubyte, 1),
        ("ButtonCircle", ctypes.c_ubyte, 1),
        ("ButtonTriangle", ctypes.c_ubyte, 1),
        
        # Bitfields (1 byte total)
        ("ButtonL1", ctypes.c_ubyte, 1),
        ("ButtonR1", ctypes.c_ubyte, 1),
        ("ButtonL2", ctypes.c_ubyte, 1),
        ("ButtonR2", ctypes.c_ubyte, 1),
        ("ButtonShare", ctypes.c_ubyte, 1),
        ("ButtonOptions", ctypes.c_ubyte, 1),
        ("ButtonL3", ctypes.c_ubyte, 1),
        ("ButtonR3", ctypes.c_ubyte, 1),
        
        # Bitfields (1 byte total)
        ("ButtonHome", ctypes.c_ubyte, 1),
        ("ButtonTouchpad", ctypes.c_ubyte, 1),
        ("ButtonMute", ctypes.c_ubyte, 1),
        ("Reserved", ctypes.c_ubyte, 1),
        ("ButtonLeftFunction", ctypes.c_ubyte, 1),
        ("ButtonRightFunction", ctypes.c_ubyte, 1),
        ("ButtonLeftPaddle", ctypes.c_ubyte, 1),
        ("ButtonRightPaddle", ctypes.c_ubyte, 1),
        
        ("Reserved2", ctypes.c_ubyte * 5),
        
        ("GyroX", ctypes.c_short),
        ("GyroY", ctypes.c_short),
        ("GyroZ", ctypes.c_short),
        ("AccelX", ctypes.c_short),
        ("AccelY", ctypes.c_short),
        ("AccelZ", ctypes.c_short),
        ("SensorTimestamp", ctypes.c_uint),
        ("Temperature", ctypes.c_ubyte),
        
        ("TouchReport", WINUHID_PS5_TOUCH_REPORT),
        
        # Bitfields (1 byte)
        ("TriggerRightStopLocation", ctypes.c_ubyte, 4),
        ("TriggerRightStatus", ctypes.c_ubyte, 4),
        # Bitfields (1 byte)
        ("TriggerLeftStopLocation", ctypes.c_ubyte, 4),
        ("TriggerLeftStatus", ctypes.c_ubyte, 4),
        
        ("HostTimestamp", ctypes.c_uint),
        # Bitfields (1 byte)
        ("TriggerRightEffect", ctypes.c_ubyte, 4),
        ("TriggerLeftEffect", ctypes.c_ubyte, 4),
        ("DeviceTimestamp", ctypes.c_uint),
        
        # Bitfields (1 byte)
        ("BatteryPercent", ctypes.c_ubyte, 4),
        ("BatteryState", ctypes.c_ubyte, 4),
        
        ("Reserved3", ctypes.c_ubyte * 10)
    ]

# Structs for Xbox One
class WINUHID_XONE_INPUT_REPORT(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("LeftStickX", ctypes.c_ushort),
        ("LeftStickY", ctypes.c_ushort),
        ("RightStickX", ctypes.c_ushort),
        ("RightStickY", ctypes.c_ushort),
        
        # Bitfields
        ("LeftTrigger", ctypes.c_ushort, 10),
        ("RightTrigger", ctypes.c_ushort, 10),
        
        ("ButtonA", ctypes.c_ubyte, 1),
        ("ButtonB", ctypes.c_ubyte, 1),
        ("ButtonX", ctypes.c_ubyte, 1),
        ("ButtonY", ctypes.c_ubyte, 1),
        ("ButtonLB", ctypes.c_ubyte, 1),
        ("ButtonRB", ctypes.c_ubyte, 1),
        ("ButtonBack", ctypes.c_ubyte, 1),
        ("ButtonMenu", ctypes.c_ubyte, 1),
        
        ("ButtonLS", ctypes.c_ubyte, 1),
        ("ButtonRS", ctypes.c_ubyte, 1),
        ("Reserved3", ctypes.c_ubyte, 6),
        
        ("Hat", ctypes.c_ubyte, 4),
        ("Reserved4", ctypes.c_ubyte, 4),
        
        ("ButtonHome", ctypes.c_ubyte, 1),
        ("Reserved5", ctypes.c_ubyte, 7),
        
        ("BatteryLevel", ctypes.c_ubyte)
    ]

# Structs for Device Info
class WINUHID_PRESET_DEVICE_INFO(ctypes.Structure):
    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8)
        ]
        
    _fields_ = [
        ("VendorID", ctypes.c_ushort),
        ("ProductID", ctypes.c_ushort),
        ("VersionNumber", ctypes.c_ushort),
        ("ContainerId", GUID),
        ("InstanceID", ctypes.c_wchar_p),
        ("HardwareIDs", ctypes.c_wchar_p)
    ]

# Callback types
PS4_RUMBLE_CALLBACK = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_ubyte)
PS4_LED_CALLBACK = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ubyte)

PS5_RUMBLE_CALLBACK = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_ubyte)
PS5_LIGHTBAR_LED_CALLBACK = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ubyte)
PS5_PLAYER_LED_CALLBACK = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ubyte)
PS5_MIC_LED_CALLBACK = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ubyte)
PS5_TRIGGER_EFFECT_CALLBACK = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)

XONE_RUMBLE_CALLBACK = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ubyte)

class WINUHID_PS5_GAMEPAD_INFO(ctypes.Structure):
    _fields_ = [
        ("BasicInfo", ctypes.POINTER(WINUHID_PRESET_DEVICE_INFO)),
        ("MacAddress", ctypes.c_ubyte * 6),
        ("FirmwareInfo", ctypes.c_void_p),
        ("FirmwareInfoLength", ctypes.c_ubyte)
    ]

# Function prototypes configuration helper
def setup_prototypes():
    if _winuhid is not None:
        _winuhid.WinUHidCreateDevice.argtypes = [ctypes.POINTER(WINUHID_DEVICE_CONFIG)]
        _winuhid.WinUHidCreateDevice.restype = ctypes.c_void_p

        _winuhid.WinUHidSubmitInputReport.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        _winuhid.WinUHidSubmitInputReport.restype = ctypes.c_bool

        _winuhid.WinUHidStartDevice.argtypes = [ctypes.c_void_p, WINUHID_EVENT_CALLBACK, ctypes.c_void_p]
        _winuhid.WinUHidStartDevice.restype = ctypes.c_bool

        _winuhid.WinUHidCompleteWriteEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(WINUHID_EVENT), ctypes.c_bool]
        _winuhid.WinUHidCompleteWriteEvent.restype = None

        _winuhid.WinUHidCompleteReadEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(WINUHID_EVENT), ctypes.c_void_p, ctypes.c_ulong]
        _winuhid.WinUHidCompleteReadEvent.restype = None

        _winuhid.WinUHidStopDevice.argtypes = [ctypes.c_void_p]
        _winuhid.WinUHidStopDevice.restype = None

        _winuhid.WinUHidDestroyDevice.argtypes = [ctypes.c_void_p]
        _winuhid.WinUHidDestroyDevice.restype = None

    if _winuhid_devs is None:
        return
        
    # PS4
    _winuhid_devs.WinUHidPS4Create.argtypes = [
        ctypes.POINTER(WINUHID_PRESET_DEVICE_INFO),
        PS4_RUMBLE_CALLBACK,
        PS4_LED_CALLBACK,
        ctypes.c_void_p
    ]
    _winuhid_devs.WinUHidPS4Create.restype = ctypes.c_void_p
    
    _winuhid_devs.WinUHidPS4InitializeInputReport.argtypes = [ctypes.POINTER(WINUHID_PS4_INPUT_REPORT)]
    _winuhid_devs.WinUHidPS4InitializeInputReport.restype = None
    
    _winuhid_devs.WinUHidPS4SetHatState.argtypes = [ctypes.POINTER(WINUHID_PS4_INPUT_REPORT), ctypes.c_int, ctypes.c_int]
    _winuhid_devs.WinUHidPS4SetHatState.restype = None
    
    _winuhid_devs.WinUHidPS4SetBatteryState.argtypes = [ctypes.POINTER(WINUHID_PS4_INPUT_REPORT), ctypes.c_bool, ctypes.c_ubyte]
    _winuhid_devs.WinUHidPS4SetBatteryState.restype = None
    
    _winuhid_devs.WinUHidPS4SetTouchState.argtypes = [ctypes.POINTER(WINUHID_PS4_INPUT_REPORT), ctypes.c_ubyte, ctypes.c_bool, ctypes.c_ushort, ctypes.c_ushort]
    _winuhid_devs.WinUHidPS4SetTouchState.restype = None
    
    _winuhid_devs.WinUHidPS4SetAccelState.argtypes = [ctypes.POINTER(WINUHID_PS4_INPUT_REPORT), ctypes.c_float, ctypes.c_float, ctypes.c_float]
    _winuhid_devs.WinUHidPS4SetAccelState.restype = None
    
    _winuhid_devs.WinUHidPS4SetGyroState.argtypes = [ctypes.POINTER(WINUHID_PS4_INPUT_REPORT), ctypes.c_float, ctypes.c_float, ctypes.c_float]
    _winuhid_devs.WinUHidPS4SetGyroState.restype = None
    
    _winuhid_devs.WinUHidPS4ReportInput.argtypes = [ctypes.c_void_p, ctypes.POINTER(WINUHID_PS4_INPUT_REPORT)]
    _winuhid_devs.WinUHidPS4ReportInput.restype = ctypes.c_bool
    
    _winuhid_devs.WinUHidPS4Destroy.argtypes = [ctypes.c_void_p]
    _winuhid_devs.WinUHidPS4Destroy.restype = None
    
    # PS5
    _winuhid_devs.WinUHidPS5Create.argtypes = [
        ctypes.POINTER(WINUHID_PS5_GAMEPAD_INFO),
        PS5_RUMBLE_CALLBACK,
        PS5_LIGHTBAR_LED_CALLBACK,
        PS5_PLAYER_LED_CALLBACK,
        PS5_TRIGGER_EFFECT_CALLBACK,
        PS5_MIC_LED_CALLBACK,
        ctypes.c_void_p
    ]
    _winuhid_devs.WinUHidPS5Create.restype = ctypes.c_void_p
    
    _winuhid_devs.WinUHidPS5InitializeInputReport.argtypes = [ctypes.POINTER(WINUHID_PS5_INPUT_REPORT)]
    _winuhid_devs.WinUHidPS5InitializeInputReport.restype = None
    
    _winuhid_devs.WinUHidPS5SetHatState.argtypes = [ctypes.POINTER(WINUHID_PS5_INPUT_REPORT), ctypes.c_int, ctypes.c_int]
    _winuhid_devs.WinUHidPS5SetHatState.restype = None
    
    _winuhid_devs.WinUHidPS5SetBatteryState.argtypes = [ctypes.POINTER(WINUHID_PS5_INPUT_REPORT), ctypes.c_bool, ctypes.c_ubyte]
    _winuhid_devs.WinUHidPS5SetBatteryState.restype = None
    
    _winuhid_devs.WinUHidPS5SetTouchState.argtypes = [ctypes.POINTER(WINUHID_PS5_INPUT_REPORT), ctypes.c_ubyte, ctypes.c_bool, ctypes.c_ushort, ctypes.c_ushort]
    _winuhid_devs.WinUHidPS5SetTouchState.restype = None
    
    _winuhid_devs.WinUHidPS5SetAccelState.argtypes = [ctypes.POINTER(WINUHID_PS5_INPUT_REPORT), ctypes.c_float, ctypes.c_float, ctypes.c_float]
    _winuhid_devs.WinUHidPS5SetAccelState.restype = None
    
    _winuhid_devs.WinUHidPS5SetGyroState.argtypes = [ctypes.POINTER(WINUHID_PS5_INPUT_REPORT), ctypes.c_float, ctypes.c_float, ctypes.c_float]
    _winuhid_devs.WinUHidPS5SetGyroState.restype = None
    
    _winuhid_devs.WinUHidPS5ReportInput.argtypes = [ctypes.c_void_p, ctypes.POINTER(WINUHID_PS5_INPUT_REPORT)]
    _winuhid_devs.WinUHidPS5ReportInput.restype = ctypes.c_bool
    
    _winuhid_devs.WinUHidPS5Destroy.argtypes = [ctypes.c_void_p]
    _winuhid_devs.WinUHidPS5Destroy.restype = None
    
    # Xbox One
    _winuhid_devs.WinUHidXOneCreate.argtypes = [
        ctypes.POINTER(WINUHID_PRESET_DEVICE_INFO),
        XONE_RUMBLE_CALLBACK,
        ctypes.c_void_p
    ]
    _winuhid_devs.WinUHidXOneCreate.restype = ctypes.c_void_p
    
    _winuhid_devs.WinUHidXOneInitializeInputReport.argtypes = [ctypes.POINTER(WINUHID_XONE_INPUT_REPORT)]
    _winuhid_devs.WinUHidXOneInitializeInputReport.restype = None
    
    _winuhid_devs.WinUHidXOneSetHatState.argtypes = [ctypes.POINTER(WINUHID_XONE_INPUT_REPORT), ctypes.c_int, ctypes.c_int]
    _winuhid_devs.WinUHidXOneSetHatState.restype = None
    
    _winuhid_devs.WinUHidXOneReportInput.argtypes = [ctypes.c_void_p, ctypes.POINTER(WINUHID_XONE_INPUT_REPORT)]
    _winuhid_devs.WinUHidXOneReportInput.restype = ctypes.c_bool
    
    _winuhid_devs.WinUHidXOneDestroy.argtypes = [ctypes.c_void_p]
    _winuhid_devs.WinUHidXOneDestroy.restype = None

setup_prototypes()


class VDS4Gamepad:
    def __init__(self):
        self.notification_callback = None
        self.report = WINUHID_PS4_INPUT_REPORT()
        if _winuhid_devs is not None:
            _winuhid_devs.WinUHidPS4InitializeInputReport(ctypes.byref(self.report))
            # Define C-callbacks to prevent garbage collection
            self._c_rumble_cb = PS4_RUMBLE_CALLBACK(self._rumble_handler)
            self._c_led_cb = PS4_LED_CALLBACK(self._led_handler)
            
            # BasicInfo NULL, MacAddress NULL
            self.device = _winuhid_devs.WinUHidPS4Create(None, self._c_rumble_cb, self._c_led_cb, None)
            if not self.device:
                logger.error("Failed to create WinUHid PS4 Gamepad device")
        else:
            self.device = None
            logger.error("WinUHidDevs DLL not loaded")

    def _rumble_handler(self, context, left_motor, right_motor):
        if self.notification_callback:
            # Match the signature expected by virtual_controller.py
            # client, target, large_motor, small_motor, led_number, user_data
            # ViGEm passed 0-255. WinUHid passes UCHAR (0-255)
            self.notification_callback(None, None, left_motor, right_motor, 0, None)

    def _led_handler(self, context, r, g, b):
        pass

    def register_notification(self, callback_function):
        self.notification_callback = callback_function

    def unregister_notification(self):
        self.notification_callback = None

    def update(self):
        if self.device and _winuhid_devs:
            return bool(_winuhid_devs.WinUHidPS4ReportInput(self.device, ctypes.byref(self.report)))
        return False

    def close(self):
        if hasattr(self, 'device') and self.device and _winuhid_devs:
            _winuhid_devs.WinUHidPS4Destroy(self.device)
            self.device = None
        self._c_rumble_cb = None
        self._c_led_cb = None
        self.notification_callback = None

    def __del__(self):
        self.close()


class VDS5Gamepad:
    def __init__(self):
        self.notification_callback = None
        self.report = WINUHID_PS5_INPUT_REPORT()
        if _winuhid_devs is not None:
            _winuhid_devs.WinUHidPS5InitializeInputReport(ctypes.byref(self.report))
            self._c_rumble_cb = PS5_RUMBLE_CALLBACK(self._rumble_handler)
            self._c_led_cb = PS5_LIGHTBAR_LED_CALLBACK(self._led_handler)
            self._c_player_cb = PS5_PLAYER_LED_CALLBACK(self._player_led_handler)
            self._c_mic_cb = PS5_MIC_LED_CALLBACK(self._mic_led_handler)
            self._c_trigger_cb = PS5_TRIGGER_EFFECT_CALLBACK(self._trigger_handler)
            
            info = WINUHID_PS5_GAMEPAD_INFO()
            info.BasicInfo = None
            ctypes.memset(info.MacAddress, 0, 6)
            info.FirmwareInfo = None
            info.FirmwareInfoLength = 0
            
            self.device = _winuhid_devs.WinUHidPS5Create(
                ctypes.byref(info),
                self._c_rumble_cb,
                self._c_led_cb,
                self._c_player_cb,
                self._c_trigger_cb,
                self._c_mic_cb,
                None
            )
            if not self.device:
                logger.error("Failed to create WinUHid PS5 Gamepad device")
        else:
            self.device = None
            logger.error("WinUHidDevs DLL not loaded")

    def _rumble_handler(self, context, left_motor, right_motor):
        if self.notification_callback:
            self.notification_callback(None, None, left_motor, right_motor, 0, None)

    def _led_handler(self, context, r, g, b):
        pass

    def _player_led_handler(self, context, val):
        pass

    def _mic_led_handler(self, context, val):
        pass

    def _trigger_handler(self, context, left_eff, right_eff):
        pass

    def register_notification(self, callback_function):
        self.notification_callback = callback_function

    def unregister_notification(self):
        self.notification_callback = None

    def update(self):
        if self.device and _winuhid_devs:
            return bool(_winuhid_devs.WinUHidPS5ReportInput(self.device, ctypes.byref(self.report)))
        return False

    def close(self):
        if hasattr(self, 'device') and self.device and _winuhid_devs:
            _winuhid_devs.WinUHidPS5Destroy(self.device)
            self.device = None
        self._c_rumble_cb = None
        self._c_led_cb = None
        self._c_player_cb = None
        self._c_mic_cb = None
        self._c_trigger_cb = None
        self.notification_callback = None

    def __del__(self):
        self.close()


class VX360Gamepad:
    """Wraps WinUHid Xbox One controller to behave like VX360Gamepad from vgamepad."""
    def __init__(self):
        self.notification_callback = None
        self.force_feedback_notification_callback = None
        self.report = WINUHID_XONE_INPUT_REPORT()
        # Phase 1 diagnostic state.  Keep this in the Python wrapper so the
        # existing two-motor notification contract remains byte-for-byte
        # unchanged while we capture the Xbox One impulse-trigger values.
        self._impulse_log_previous = (0, 0, 0, 0)
        self._impulse_log_started_at = None
        self._impulse_log_last_at = None
        self._impulse_log_samples = 0
        self._impulse_log_sequence = 0
        self._impulse_log_peak_lt = 0
        self._impulse_log_peak_rt = 0
        self._impulse_log_peak_main_l = 0
        self._impulse_log_peak_main_r = 0
        if _winuhid_devs is not None:
            _winuhid_devs.WinUHidXOneInitializeInputReport(ctypes.byref(self.report))
            self._c_rumble_cb = XONE_RUMBLE_CALLBACK(self._rumble_handler)
            self.device = _winuhid_devs.WinUHidXOneCreate(None, self._c_rumble_cb, None)
            if not self.device:
                logger.error("Failed to create WinUHid Xbox One Gamepad device")
        else:
            self.device = None
            logger.error("WinUHidDevs DLL not loaded")

    def _rumble_handler(self, context, left_motor, right_motor, left_trigger, right_trigger):
        # Native WinUHid callback -- runs on the driver's callback thread and holds the
        # GIL.  Keep it to functional work only; the impulse calibration logging lives in
        # the rate-limited _log_impulse_diagnostics(); logging every rumble update here
        # would stall input for every connected controller.
        # WinUHid XOne supplies all four motors as percentages (0-100).
        impulse_l = int(left_trigger)
        impulse_r = int(right_trigger)

        if _PERF_DIAGNOSTICS:
            self._log_impulse_diagnostics(int(left_motor), int(right_motor), impulse_l, impulse_r)

        # Xbox-capable callers can consume all four motors atomically.  Keep
        # the legacy two-motor callback as a fallback for existing users.
        if self.force_feedback_notification_callback:
            # WinUHid provides motor values as percentages (0-100); vgamepad expects 0-255.
            self.force_feedback_notification_callback(
                int(left_motor * 2.55), int(right_motor * 2.55), impulse_l, impulse_r)
        elif self.notification_callback:
            self.notification_callback(
                None, None, int(left_motor * 2.55), int(right_motor * 2.55), 0, None)

    def _log_impulse_diagnostics(self, main_l, main_r, impulse_l, impulse_r):
        """Rate-limited Xbox impulse-trigger calibration logging.

        Records the raw four-motor values so the gpadtester Low/High calibration can be
        based on the actual Xbox impulse-trigger magnitudes.
        """
        current = (main_l, main_r, impulse_l, impulse_r)
        impulse_active = impulse_l > 0 or impulse_r > 0
        was_impulse_active = (
            self._impulse_log_previous[2] > 0
            or self._impulse_log_previous[3] > 0
        )
        now = time.perf_counter()

        if impulse_active:
            if not was_impulse_active:
                self._impulse_log_started_at = now
                self._impulse_log_last_at = None
                self._impulse_log_samples = 0
                self._impulse_log_peak_lt = 0
                self._impulse_log_peak_rt = 0
                self._impulse_log_peak_main_l = 0
                self._impulse_log_peak_main_r = 0

            self._impulse_log_samples += 1
            self._impulse_log_peak_lt = max(self._impulse_log_peak_lt, impulse_l)
            self._impulse_log_peak_rt = max(self._impulse_log_peak_rt, impulse_r)
            self._impulse_log_peak_main_l = max(self._impulse_log_peak_main_l, main_l)
            self._impulse_log_peak_main_r = max(self._impulse_log_peak_main_r, main_r)

        # Emit only edge/change records. This keeps the native rumble callback
        # lightweight while retaining every value needed to identify Low/High.
        if impulse_active or was_impulse_active:
            if not was_impulse_active:
                event = "START"
            elif not impulse_active:
                event = "STOP"
            elif current != self._impulse_log_previous:
                event = "UPDATE"
            else:
                event = None

            if event is not None:
                self._impulse_log_sequence += 1
                dt_ms = 0.0 if self._impulse_log_last_at is None else (now - self._impulse_log_last_at) * 1000.0
                logger.info(
                    "XONE-IMPULSE seq=%d dt=%.1fms main[L=%d R=%d] impulse[LT=%d RT=%d] event=%s",
                    self._impulse_log_sequence, dt_ms, main_l, main_r,
                    impulse_l, impulse_r, event,
                )
                self._impulse_log_last_at = now

            if was_impulse_active and not impulse_active:
                duration_ms = 0.0 if self._impulse_log_started_at is None else (now - self._impulse_log_started_at) * 1000.0
                logger.info(
                    "XONE-IMPULSE-SUMMARY duration=%.1fms samples=%d peak[LT=%d RT=%d] main_peak[L=%d R=%d]",
                    duration_ms, self._impulse_log_samples,
                    self._impulse_log_peak_lt, self._impulse_log_peak_rt,
                    self._impulse_log_peak_main_l, self._impulse_log_peak_main_r,
                )
                self._impulse_log_started_at = None

        self._impulse_log_previous = current

    def register_notification(self, callback_function):
        self.notification_callback = callback_function

    def unregister_notification(self):
        self.notification_callback = None

    def register_force_feedback_notification(self, callback_function):
        """Registers an Xbox One-only, atomic four-motor callback.

        Main motors retain the legacy 0-255 representation; impulse motors
        intentionally retain the native WinUHid 0-100 percentage values.
        """
        self.force_feedback_notification_callback = callback_function

    def unregister_force_feedback_notification(self):
        self.force_feedback_notification_callback = None

    def left_trigger(self, val):
        # val is 0-255. WinUHid XOne expects 10-bit LeftTrigger (0-1023).
        self.report.LeftTrigger = int(val * 1023 / 255)

    def right_trigger(self, val):
        # val is 0-255. WinUHid XOne expects 10-bit RightTrigger (0-1023).
        self.report.RightTrigger = int(val * 1023 / 255)

    @staticmethod
    def _axis_float_to_ushort(val):
        val = max(-1.0, min(1.0, float(val)))
        return int((val + 1.0) * 32767.5)

    def left_joystick_float(self, x, y):
        # x, y are floats (-1.0 to 1.0)
        # WinUHid XOne expects USHORT (0 to 65535, 32768 is center)
        self.report.LeftStickX = self._axis_float_to_ushort(x)
        self.report.LeftStickY = self._axis_float_to_ushort(y)

    def right_joystick_float(self, x, y):
        # x, y are floats (-1.0 to 1.0)
        # WinUHid XOne expects USHORT (0 to 65535, 32768 is center)
        self.report.RightStickX = self._axis_float_to_ushort(x)
        self.report.RightStickY = self._axis_float_to_ushort(y)

    def set_buttons(self, buttons_mask):
        # Map XInput buttons flags to WINUHID_XONE_INPUT_REPORT bitfields
        self.report.ButtonA = 1 if (buttons_mask & 0x1000) else 0
        self.report.ButtonB = 1 if (buttons_mask & 0x2000) else 0
        self.report.ButtonX = 1 if (buttons_mask & 0x4000) else 0
        self.report.ButtonY = 1 if (buttons_mask & 0x8000) else 0
        self.report.ButtonLB = 1 if (buttons_mask & 0x0100) else 0
        self.report.ButtonRB = 1 if (buttons_mask & 0x0200) else 0
        self.report.ButtonBack = 1 if (buttons_mask & 0x0020) else 0
        self.report.ButtonMenu = 1 if (buttons_mask & 0x0010) else 0
        self.report.ButtonLS = 1 if (buttons_mask & 0x0040) else 0
        self.report.ButtonRS = 1 if (buttons_mask & 0x0080) else 0
        self.report.ButtonHome = 1 if (buttons_mask & 0x0400) else 0
        
        # D-pad mapping
        up = bool(buttons_mask & 0x0001)
        down = bool(buttons_mask & 0x0002)
        left = bool(buttons_mask & 0x0004)
        right = bool(buttons_mask & 0x0008)
        
        hat_x = -1 if left else (1 if right else 0)
        hat_y = -1 if up else (1 if down else 0)
        
        if _winuhid_devs:
            _winuhid_devs.WinUHidXOneSetHatState(ctypes.byref(self.report), hat_x, hat_y)

    def update(self):
        if self.device and _winuhid_devs:
            return bool(_winuhid_devs.WinUHidXOneReportInput(self.device, ctypes.byref(self.report)))
        return False

    def close(self):
        if hasattr(self, 'device') and self.device and _winuhid_devs:
            _winuhid_devs.WinUHidXOneDestroy(self.device)
            self.device = None
        self._c_rumble_cb = None
        self.notification_callback = None
        self.force_feedback_notification_callback = None

    def __del__(self):
        self.close()



