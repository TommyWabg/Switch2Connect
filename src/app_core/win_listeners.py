"""Hidden Win32 power and device notification windows (no UI toolkit)."""
import ctypes
from ctypes import wintypes
import logging
import threading
import time
import uuid
import win32con
import win32gui

logger = logging.getLogger(__name__)

class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_string(cls, value):
        parsed = uuid.UUID(str(value).strip("{}"))
        return cls.from_buffer_copy(parsed.bytes_le)


class DEV_BROADCAST_HDR(ctypes.Structure):
    _fields_ = [
        ("dbch_size", wintypes.DWORD),
        ("dbch_devicetype", wintypes.DWORD),
        ("dbch_reserved", wintypes.DWORD),
    ]


class DEV_BROADCAST_DEVICEINTERFACE_W(ctypes.Structure):
    _fields_ = [
        ("dbcc_size", wintypes.DWORD),
        ("dbcc_devicetype", wintypes.DWORD),
        ("dbcc_reserved", wintypes.DWORD),
        ("dbcc_classguid", GUID),
        ("dbcc_name", wintypes.WCHAR * 1),
    ]


class PowerListener:
    def __init__(self, callback):
        self.callback = callback
        self.hwnd = None

    def start(self):
        def _listen():
            wc = win32gui.WNDCLASS()
            wc.lpfnWndProc = self.wndproc
            wc.lpszClassName = "PowerListenerWindow"
            hInstance = win32gui.GetModuleHandle(None)
            wc.hInstance = hInstance
            try:
                class_atom = win32gui.RegisterClass(wc)
                self.hwnd = win32gui.CreateWindow(class_atom, "PowerListener", 0, 0, 0, 0, 0, 0, 0, hInstance, None)
                win32gui.PumpMessages()
            except Exception as e:
                logger.error(f"PowerListener failed: {e}")
            
        threading.Thread(target=_listen, daemon=True).start()

    def wndproc(self, hwnd, msg, wparam, lparam):
        if msg == win32con.WM_POWERBROADCAST:
            self.callback(wparam)
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)


class WiredDeviceChangeListener:
    """Watches for wired controller arrival/removal, and for Bluetooth radios.

    Both live on one hidden window: RegisterDeviceNotificationW can be called more than
    once for the same hwnd, and the two interfaces are told apart in _wndproc. The radio
    notification lets the wireless route sit idle until a radio actually appears instead
    of retrying on a timer.
    """

    WM_DEVICECHANGE = 0x0219
    DBT_DEVICEARRIVAL = 0x8000
    DBT_DEVICEREMOVECOMPLETE = 0x8004
    DBT_DEVTYP_DEVICEINTERFACE = 0x00000005
    DEVICE_NOTIFY_WINDOW_HANDLE = 0x00000000
    HID_INTERFACE_GUID = "{4D1E55B2-F16F-11CF-88CB-001111000030}"
    BLUETOOTH_RADIO_GUID = "{0850302A-B344-4FDA-9BE9-90576B8D46F0}"

    def __init__(self, event_queue):
        self.event_queue = event_queue
        self.hwnd = None
        self.thread = None
        self._stop_event = threading.Event()
        self._class_name = f"Switch2WiredDeviceChangeWindow_{id(self)}"
        self.notification_handle = None
        self.bluetooth_notification_handle = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self._stop_event.clear()
        self.thread = threading.Thread(target=self._listen, daemon=True)
        self.thread.start()

    def stop(self):
        self._stop_event.set()
        hwnd = self.hwnd
        if hwnd:
            try:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)
        self.thread = None
        self.hwnd = None

    def _listen(self):
        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = self._wndproc
        wc.lpszClassName = self._class_name
        hinstance = win32gui.GetModuleHandle(None)
        wc.hInstance = hinstance
        try:
            class_atom = win32gui.RegisterClass(wc)
            self.hwnd = win32gui.CreateWindow(class_atom, self._class_name, 0, 0, 0, 0, 0, 0, 0, hinstance, None)
            device_filter = DEV_BROADCAST_DEVICEINTERFACE_W()
            device_filter.dbcc_size = ctypes.sizeof(DEV_BROADCAST_DEVICEINTERFACE_W)
            device_filter.dbcc_devicetype = self.DBT_DEVTYP_DEVICEINTERFACE
            device_filter.dbcc_classguid = GUID.from_string(self.HID_INTERFACE_GUID)
            self.notification_handle = ctypes.windll.user32.RegisterDeviceNotificationW(
                self.hwnd,
                ctypes.byref(device_filter),
                self.DEVICE_NOTIFY_WINDOW_HANDLE,
            )
            if not self.notification_handle:
                raise ctypes.WinError(ctypes.get_last_error())
            logger.info("Wired HID device notification registered.")

            # Second registration on the same window for Bluetooth radios.
            try:
                radio_filter = DEV_BROADCAST_DEVICEINTERFACE_W()
                radio_filter.dbcc_size = ctypes.sizeof(DEV_BROADCAST_DEVICEINTERFACE_W)
                radio_filter.dbcc_devicetype = self.DBT_DEVTYP_DEVICEINTERFACE
                radio_filter.dbcc_classguid = GUID.from_string(self.BLUETOOTH_RADIO_GUID)
                self.bluetooth_notification_handle = ctypes.windll.user32.RegisterDeviceNotificationW(
                    self.hwnd,
                    ctypes.byref(radio_filter),
                    self.DEVICE_NOTIFY_WINDOW_HANDLE,
                )
                if self.bluetooth_notification_handle:
                    logger.info("Bluetooth radio device notification registered.")
                else:
                    logger.warning("Bluetooth radio notification could not be registered; "
                                   "the wireless route will fall back to its periodic check.")
            except Exception as e:
                logger.warning("Bluetooth radio notification setup failed: %s", e)

            win32gui.PumpMessages()
        except Exception as e:
            logger.error("Wired device change listener failed: %s", e)
        finally:
            for attr in ("notification_handle", "bluetooth_notification_handle"):
                handle = getattr(self, attr, None)
                if handle:
                    try:
                        ctypes.windll.user32.UnregisterDeviceNotification(handle)
                    except Exception:
                        pass
                    setattr(self, attr, None)
            self.hwnd = None
            try:
                win32gui.UnregisterClass(self._class_name, hinstance)
            except Exception:
                pass

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == self.WM_DEVICECHANGE:
            reason = None
            if int(wparam) == self.DBT_DEVICEARRIVAL:
                reason = "device_arrival"
            elif int(wparam) == self.DBT_DEVICEREMOVECOMPLETE:
                reason = "device_removal"
            path = None
            if reason and lparam:
                try:
                    header = ctypes.cast(
                        lparam, ctypes.POINTER(DEV_BROADCAST_HDR)).contents
                    if header.dbch_devicetype == self.DBT_DEVTYP_DEVICEINTERFACE:
                        path = ctypes.wstring_at(
                            lparam + DEV_BROADCAST_DEVICEINTERFACE_W.dbcc_name.offset)
                except Exception:
                    path = None
            target_path = (path or "").upper()
            if reason and any(f"VID_057E&PID_{pid:04X}" in target_path
                              for pid in wired_usb_device_pids()):
                try:
                    self.event_queue.put_nowait({
                        "kind": "wired",
                        "reason": reason,
                        "path": path,
                        "timestamp": time.time(),
                    })
                except Exception:
                    pass
            elif reason and self.BLUETOOTH_RADIO_GUID.strip("{}") in target_path:
                # A Bluetooth radio came or went. The wireless route is parked waiting for
                # exactly this instead of retrying an adapter that is not there.
                try:
                    self.event_queue.put_nowait({
                        "kind": "bluetooth_radio",
                        "reason": reason,
                        "path": path,
                        "timestamp": time.time(),
                    })
                except Exception:
                    pass
        elif msg == win32con.WM_CLOSE:
            try:
                win32gui.DestroyWindow(hwnd)
            except Exception:
                pass
            return 0
        elif msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)


def wired_usb_device_pids():
    try:
        from usb_hid_controller import WIRED_USB_PIDS
        return WIRED_USB_PIDS
    except Exception:
        return (0x2069, 0x2073)

ctypes.windll.user32.RegisterDeviceNotificationW.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
]
ctypes.windll.user32.RegisterDeviceNotificationW.restype = wintypes.HANDLE
ctypes.windll.user32.UnregisterDeviceNotification.argtypes = [wintypes.HANDLE]
ctypes.windll.user32.UnregisterDeviceNotification.restype = wintypes.BOOL
