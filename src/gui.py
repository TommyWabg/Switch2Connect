import sys
import tempfile, os
with open(os.path.join(tempfile.gettempdir(), "argv_test.log"), "w") as f:
    f.write(str(sys.argv) + "\n")
import queue
import time
import webbrowser
import threading
import tkinter as tk
from tkinter import filedialog, ttk
import tkinter.font as tkFont
import yaml
import logging
import asyncio
import os
import re
import ctypes
from controller import Controller, INPUT_REPORT_UUID, COMMAND_RESPONSE_UUID, NSO_GAMECUBE_CONTROLLER_PID
from discoverer import start_discoverer, set_shutting_down, set_suspending, emergency_cleanup
from config import get_resource, CONFIG, BACK_BUTTON_OPTIONS, JOYSTICK_OPTIONS, SWITCH_BUTTONS, get_driver_path, GYRO_LOCK_TOKEN, GYRO_LOCK_LABEL, MODE_SHIFT_TOKEN, MODE_SHIFT_LABEL, _YamlLoader, _YamlDumper
from cemuhook_udp import cemuhook_server
from virtual_controller import VirtualController
from discoverer import split_controller, merge_controllers, VIRTUAL_CONTROLLERS
from utils import set_startup, disable_power_throttling
import pystray
from pystray import MenuItem as item
from PIL import Image, ImageTk
import win32gui
import win32con
from ctypes import wintypes

APP_VERSION = "0.11.2"

class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]

# Explicitly set types for Win32 API to ensure compatibility
ctypes.windll.shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
ctypes.windll.shell32.ShellExecuteExW.restype = wintypes.BOOL

ctypes.windll.kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
ctypes.windll.kernel32.WaitForSingleObject.restype = wintypes.DWORD

ctypes.windll.kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
ctypes.windll.kernel32.GetExitCodeProcess.restype = wintypes.BOOL

ctypes.windll.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
ctypes.windll.kernel32.CloseHandle.restype = wintypes.BOOL

SEE_MASK_NOCLOSEPROCESS = 0x00000040
WAIT_TIMEOUT = 0x00000102
WAIT_OBJECT_0 = 0x00000000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

ctypes.windll.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
ctypes.windll.kernel32.OpenProcess.restype = wintypes.HANDLE

ctypes.windll.kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
ctypes.windll.kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

ctypes.windll.user32.GetForegroundWindow.argtypes = []
ctypes.windll.user32.GetForegroundWindow.restype = wintypes.HWND

def normalize_app_path(path):
    if not path:
        return ""
    try:
        return os.path.normcase(os.path.abspath(os.path.normpath(path)))
    except Exception:
        return os.path.normcase(os.path.normpath(path))

def get_exe_display_name(path):
    if not path:
        return "Choose App"
    try:
        import win32api
        info = win32api.GetFileVersionInfo(path, "\\")
        lang, codepage = win32api.GetFileVersionInfo(path, "\\VarFileInfo\\Translation")[0]
        for key in ("FileDescription", "ProductName"):
            value = win32api.GetFileVersionInfo(path, f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\{key}")
            if value:
                return str(value)
    except Exception:
        pass
    return os.path.splitext(os.path.basename(path))[0] or "Choose App"

def check_driver_registry():
    import winreg
    for sam in (winreg.KEY_READ, winreg.KEY_READ | winreg.KEY_WOW64_64KEY):
        try:
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\WUDF\Services\WinUHidDriver",
                0,
                sam
            )
            winreg.CloseKey(key)
            return True
        except FileNotFoundError:
            pass
        except Exception:
            pass
    return False

def check_driver_pnputil():
    import subprocess
    try:
        result = subprocess.run(
            ["pnputil", "/enum-devices", "/deviceid", "Root\\WinUHid"],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        )
        if result.returncode == 0 and "root\\winuhid" in result.stdout.lower():
            return True
    except Exception as e:
        logger.error(f"Error checking driver installation via pnputil: {e}")
    return False

def is_driver_installed():
    return check_driver_registry() or check_driver_pnputil()

def check_vigembus_registry():
    import winreg
    for sam in (winreg.KEY_READ, winreg.KEY_READ | winreg.KEY_WOW64_64KEY):
        try:
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Services\ViGEmBus",
                0,
                sam
            )
            winreg.CloseKey(key)
            return True
        except FileNotFoundError:
            pass
        except Exception:
            pass
            
    # Also check uninstall keys
    paths = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
    ]
    for path in paths:
        for sam in (winreg.KEY_READ, winreg.KEY_READ | winreg.KEY_WOW64_64KEY):
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, sam)
                info = winreg.QueryInfoKey(key)
                for i in range(info[0]):
                    try:
                        subkey_name = winreg.EnumKey(key, i)
                        if subkey_name == "{966606F3-2745-49E9-BF15-5C3EAA4E9077}":
                            winreg.CloseKey(key)
                            return True
                        subkey = winreg.OpenKey(key, subkey_name)
                        try:
                            val, _ = winreg.QueryValueEx(subkey, "DisplayName")
                            if "vigem" in str(val).lower() or "virtual gamepad emulation" in str(val).lower():
                                winreg.CloseKey(subkey)
                                winreg.CloseKey(key)
                                return True
                        except:
                            pass
                        winreg.CloseKey(subkey)
                    except:
                        pass
                winreg.CloseKey(key)
            except:
                pass
    return False

def check_vigembus_pnputil():
    import subprocess
    try:
        result = subprocess.run(
            ["pnputil", "/enum-devices", "/deviceid", "Root\\ViGEmBus"],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        )
        if result.returncode == 0 and "root\\vigembus" in result.stdout.lower():
            return True
            
        result = subprocess.run(
            ["pnputil", "/enum-devices", "/deviceid", "Nefarius\\ViGEmBus\\Gen1"],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        )
        if result.returncode == 0 and "vigembus" in result.stdout.lower():
            return True
    except Exception as e:
        logger.error(f"Error checking ViGEmBus installation via pnputil enum-devices: {e}")

    try:
        result = subprocess.run(
            ["pnputil", "/enum-drivers"],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        )
        if result.returncode == 0:
            if "vigembus" in result.stdout.lower() or "nefarius" in result.stdout.lower():
                return True
    except Exception as e:
        logger.error(f"Error checking ViGEmBus installation via pnputil enum-drivers: {e}")
        
    return False

def is_vigembus_installed():
    return check_vigembus_registry() or check_vigembus_pnputil()

logger = logging.getLogger(__name__)

try:
    # Break out of Windows terminal DPI virtualization cache to get TRUE physical resolution
    ctypes.windll.shcore.SetProcessDpiAwareness(2) # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

try:
    import tkinter as tk
    temp_root = tk.Tk()
    temp_root.withdraw()
    screen_height = temp_root.winfo_screenheight()
    temp_root.destroy()
except Exception:
    screen_height = 1440

# Baseline is 1440p physical height.
resolution_ratio = 1.0
window_resolution_ratio = 1.0
scaling_factor = 1.0
controller_frame_size = 200
battery_height = 40
player_row_height = 40
player_led_width = 60
player_led_height = 8

def _scaled_px(base_value, minimum=1, scale=None):
    if scale is None:
        scale = scaling_factor
    return max(minimum, int(base_value * scale))

def _get_window_non_client_height():
    caption_height = ctypes.windll.user32.GetSystemMetrics(4)   # SM_CYCAPTION
    frame_height = ctypes.windll.user32.GetSystemMetrics(33)    # SM_CYFRAME
    padded_border = ctypes.windll.user32.GetSystemMetrics(92)   # SM_CXPADDEDBORDER
    return caption_height + (2 * frame_height) + (2 * padded_border)

def _get_effective_client_height(fallback_height):
    effective_height = fallback_height
    try:
        work_area = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work_area), 0):
            work_height = work_area.bottom - work_area.top
            effective_height = min(effective_height, max(1, work_height - _get_window_non_client_height()))
    except Exception:
        pass
    return effective_height

def refresh_ui_scaling(current_screen_height=None):
    global screen_height, resolution_ratio, window_resolution_ratio, scaling_factor
    global controller_frame_size, battery_height, player_row_height
    global player_led_width, player_led_height

    if current_screen_height:
        screen_height = current_screen_height

    window_resolution_ratio = (screen_height / 1440.0) * getattr(CONFIG, 'ui_scale', 1.0)

    ui_scale = getattr(CONFIG, 'ui_scale', 1.0)
    if screen_height >= 1440:
        resolution_ratio = (screen_height / 1440.0) * ui_scale
    else:
        effective_height = _get_effective_client_height(screen_height)
        try:
            baseline_height = max(1, 1440 - _get_window_non_client_height())
        except Exception:
            baseline_height = 1440
        resolution_ratio = (effective_height / baseline_height) * ui_scale
    scaling_factor = 1.2 * resolution_ratio
    controller_frame_size = _scaled_px(200)
    battery_height = _scaled_px(40)
    player_row_height = _scaled_px(40)
    player_led_width = _scaled_px(60)
    player_led_height = _scaled_px(8)

refresh_ui_scaling()

def scale_font(font_tuple):
    if not font_tuple:
        return font_tuple
    if isinstance(font_tuple, tuple) and len(font_tuple) >= 2:
        family, size = font_tuple[0], font_tuple[1]
        weight = font_tuple[2] if len(font_tuple) > 2 else ""
        # Convert Tkinter points to physical pixels (1 point = 96/72 pixels)
        base_pixel_size = size * (96.0 / 72.0)
        scaled_pixel_size = max(8, int(base_pixel_size * scaling_factor))
        
        # Negative size tells Tkinter to use exact physical pixels, preventing DPI double-scaling
        return (family, -scaled_pixel_size, weight)
    return font_tuple


# Keyboard modifier tokens that keep their bare name on screen (no "KB" prefix), so a
# combo reads e.g. "CONTROL+KBC" rather than "KBCONTROL+KBC".
_INPUT_MODIFIER_TOKENS = {
    "VK_CONTROL", "VK_CONTROL_L", "VK_CONTROL_R", "VK_LCONTROL", "VK_RCONTROL",
    "VK_SHIFT", "VK_SHIFT_L", "VK_SHIFT_R", "VK_LSHIFT", "VK_RSHIFT",
    "VK_MENU", "VK_ALT", "VK_ALT_L", "VK_ALT_R", "VK_LMENU", "VK_RMENU",
    "VK_WIN", "VK_LWIN", "VK_RWIN", "VK_WIN_L", "VK_WIN_R",
}


def format_input_display(text):
    """Human-readable form of a recorded Custom input token string. Mouse buttons are
    shown as M1/M2/M3 (left/middle/right), keyboard keys as KB<key> (e.g. KB1, KBA),
    keyboard modifiers keep their bare name (CONTROL, SHIFT, ...), and controller buttons
    keep their bare name. The stored config value still uses the raw MB_/VK_/BTN_ tokens;
    this only affects what the recorder entry displays."""
    parts = []
    for token in text.split("+"):
        if token in _INPUT_MODIFIER_TOKENS:
            parts.append(token[3:])          # strip "VK_", keep modifier name as-is
        elif token.startswith("VK_"):
            parts.append("KB" + token[3:])
        elif token.startswith("MB_"):
            parts.append("M" + token[3:])
        elif token.startswith("BTN_"):
            parts.append(token[4:])
        else:
            parts.append(token)
    return "+".join(parts)


class Tooltip:
    """Lightweight hover tooltip. Shows the widget's full text in a small borderless
    window just below it, so content that is visually clipped (e.g. a long Custom
    recording in a fixed-width entry) can still be read in full. The text is resolved
    lazily through text_getter on each hover so it always reflects the current value."""

    def __init__(self, widget, text_getter, delay_ms=350):
        self.widget = widget
        self.text_getter = text_getter
        self.delay_ms = delay_ms
        self.tip = None
        self.after_id = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, event=None):
        self._cancel()
        try:
            self.after_id = self.widget.after(self.delay_ms, self._show)
        except Exception:
            self.after_id = None

    def _cancel(self):
        if self.after_id is not None:
            try:
                self.widget.after_cancel(self.after_id)
            except Exception:
                pass
            self.after_id = None

    def _show(self):
        self.after_id = None
        if self.tip is not None or not self.widget.winfo_exists():
            return
        try:
            text = self.text_getter()
        except Exception:
            text = ""
        if not text:
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        try:
            tip.attributes("-topmost", True)
        except Exception:
            pass
        tk.Label(
            tip, text=text, bg="#1E1E1E", fg="white",
            font=scale_font(("Arial", 10, "bold")), justify=tk.LEFT,
            bd=1, relief=tk.SOLID, padx=int(6 * scaling_factor), pady=int(3 * scaling_factor),
        ).pack()
        tip.update_idletasks()

        # Same boundary-aware placement as the Back Button Options popup
        # (_place_popup_within_root_bounds): prefer below-left of the widget, but flip to
        # the right edge / above when there isn't room inside the toplevel's bounds.
        root = self.widget.winfo_toplevel()
        anchor_x = self.widget.winfo_rootx()
        anchor_right = anchor_x + self.widget.winfo_width()
        anchor_top = self.widget.winfo_rooty()
        anchor_bottom = anchor_top + self.widget.winfo_height()
        root_right = root.winfo_rootx() + root.winfo_width()
        root_bottom = root.winfo_rooty() + root.winfo_height()
        tip_w = tip.winfo_reqwidth()
        tip_h = tip.winfo_reqheight()
        x_offset = int(3 * scaling_factor)
        y_offset = int(2 * scaling_factor)

        enough_right = anchor_x - x_offset + tip_w <= root_right
        enough_bottom = anchor_bottom + y_offset + tip_h <= root_bottom

        x = (anchor_x - x_offset) if enough_right else (anchor_right + x_offset - tip_w)
        y = (anchor_bottom + y_offset) if enough_bottom else (anchor_top - y_offset - tip_h)

        tip.wm_geometry(f"+{int(x)}+{int(y)}")
        self.tip = tip

    def _hide(self, event=None):
        self._cancel()
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


class RecordingEntry(tk.Text):
    """Single-line, read-only display of a recorded Custom input. It is a drop-in for the
    tk.Entry it replaces (entry-style get/insert/delete, and config(state=...) is accepted
    and ignored since it is always read-only), but renders the leading M/KB prefix of each
    token two font sizes smaller than the rest via text tags.

    The default "Text" bindtag is removed so the widget can't be typed into and never
    consumes keystrokes; while it holds focus during recording, key events still bubble up
    to the root recorder binding exactly as the old readonly Entry allowed."""

    def __init__(self, parent, normal_font, prefix_font, width, bg, fg):
        super().__init__(parent, height=1, width=width, font=normal_font, bg=bg, fg=fg,
                         bd=0, highlightthickness=0, wrap="none", cursor="arrow",
                         insertwidth=0, padx=0, pady=0, takefocus=1, exportselection=0)
        self.is_custom_recording_entry = True
        self.tag_configure("normal", font=normal_font, justify="center")
        self.tag_configure("prefix", font=prefix_font, justify="center")
        self.bindtags(tuple(t for t in self.bindtags() if t != "Text"))
        # tk.Text top-aligns its single line; when fill=Y stretches it to the row height,
        # split the leftover space into equal top/bottom padding so the text is centered.
        self._line_font = tkFont.Font(font=normal_font)
        self._applied_pady = -1
        self.bind("<Configure>", self._recenter, add="+")

    def _recenter(self, event=None):
        try:
            pad = max(0, (self.winfo_height() - self._line_font.metrics("linespace")) // 2)
            if pad != self._applied_pady:
                self._applied_pady = pad
                super().configure(pady=pad)
        except Exception:
            pass

    @staticmethod
    def _split_prefix(segment):
        # Leading prefix to shrink: "M" before mouse-button digits, "KB" before a key name.
        if len(segment) > 1 and segment[0] == "M" and segment[1:].isdigit():
            return "M", segment[1:]
        if len(segment) > 2 and segment.startswith("KB"):
            return "KB", segment[2:]
        return "", segment

    def get(self, *args):
        if args:
            return super().get(*args)
        return super().get("1.0", "end-1c")

    def delete(self, *args):
        super().delete("1.0", "end")

    def insert(self, index, text="", *args):
        for i, seg in enumerate(str(text).split("+")):
            if i:
                super().insert("end", "+", ("normal",))
            prefix, rest = self._split_prefix(seg)
            if prefix:
                super().insert("end", prefix, ("prefix",))
            if rest:
                super().insert("end", rest, ("normal",))

    def config(self, cnf=None, **kwargs):
        if cnf:
            kwargs.update(cnf)
        kwargs.pop("state", None)
        if kwargs:
            super().configure(**kwargs)

    configure = config


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

# Current Color Scheme (Space Gray / Cyan Accent)
background_color = "#2D2D2D"
tab_black = "#1E1E1E"
block_color = "#3C3C3C"
player_number_bg_color = "#2D2D2D"
highlight_color = "#00C3E3"
text_color = "#FFFFFF"
button_gray = "#4B4B4B"

CONTROLLER_UPDATED_EVENT = '<<ControllersUpdated>>'
pending_merge_vc_index = None

class FocusOutline:
    def __init__(self, root):
        self.root = root
        self.lines = [tk.Frame(root, bg="white") for _ in range(4)]
        self.active = False
        self.target_widget = None

    def update(self, widget):
        if not widget or not widget.winfo_exists():
            self.hide()
            return
            
        self.target_widget = widget
        try:
            w = widget.winfo_width()
            h = widget.winfo_height()
            
            pad = 2
            t = 2 # thickness
            
            is_toggle_switch = False
            is_standard_btn = False
            is_dropdown_or_slider = False
            
            try:
                if isinstance(widget, (ttk.Combobox, tk.Scale)):
                    is_dropdown_or_slider = True
                elif isinstance(widget, tk.Button):
                    if hasattr(widget.master, 'master') and hasattr(widget.master.master, 'buttons'):
                        is_toggle_switch = True
                    else:
                        is_standard_btn = True
            except:
                pass
            
            if is_dropdown_or_slider:
                shift = 0
            elif is_toggle_switch:
                shift = 1
            elif is_standard_btn:
                shift = 2
            else:
                shift = 0
            
            start_x = -pad - t - shift
            start_y = -pad - t - shift
            
            right_x = w + pad - shift
            bottom_y = h + pad - shift
            
            self.lines[0].place(in_=widget, x=start_x, y=start_y, width=w+2*pad+2*t, height=t)
            self.lines[1].place(in_=widget, x=start_x, y=bottom_y, width=w+2*pad+2*t, height=t)
            self.lines[2].place(in_=widget, x=start_x, y=start_y, width=t, height=h+2*pad+2*t)
            self.lines[3].place(in_=widget, x=right_x, y=start_y, width=t, height=h+2*pad+2*t)
            
            for line in self.lines:
                line.lift()
            self.active = True
        except Exception:
            self.hide()
            
    def hide(self):
        if self.active:
            for line in self.lines:
                line.place_forget()
            self.active = False
            self.target_widget = None

    def refresh(self):
        if self.target_widget:
            self.update(self.target_widget)


class BackButtonSelector(tk.Button):
    """Drop-in replacement for the Back Button Option combobox. It looks like the old
    readonly combobox (flat gray button) but opens a categorized floating popup instead
    of a native dropdown. It exposes the small slice of the ttk.Combobox API the mapping
    code relies on: get()/set() plus a <<ComboboxSelected>> event fired when the user
    picks an option, so on_combo_selected and the refresh paths keep working unchanged."""

    def __init__(self, parent, gui, font=None):
        self._gui = gui
        self._value = "Default"
        self._font = font or scale_font(("Arial", 11, "bold"))
        # Width auto-fits each label so it is never clipped, but never shrinks below the
        # width of the "Default" label. tk.Button width is in character units, so both the
        # minimum and the per-label widths are derived from the font's character width.
        self._fnt = tkFont.Font(font=self._font)
        self._char_px = self._fnt.measure("0") or 1
        self._min_chars = max(1, self._fit_chars("Default"))
        super().__init__(
            parent,
            text="Default",
            width=self._min_chars,
            font=self._font,
            bg=button_gray,
            fg="white",
            relief=tk.FLAT,
            bd=0,
            activebackground=button_gray,
            activeforeground="white",
            command=self._open_popup,
        )

    def _fit_chars(self, label):
        # Smallest character-unit width whose button is at least as wide as the label.
        return -(-self._fnt.measure(label) // self._char_px)

    def _open_popup(self):
        self._gui.open_back_button_popup(self)

    def get(self):
        return self._value

    def set(self, value):
        from config import back_button_label
        label = back_button_label(value)
        self._value = value
        self.config(text=label, width=max(self._min_chars, self._fit_chars(label)))

    def select_value(self, value):
        # Picking an option in the popup updates the value and fires the same event the
        # old combobox fired, so on_combo_selected runs the existing selection logic.
        self.set(value)
        self.event_generate("<<ComboboxSelected>>")



class ToggleSwitch(tk.Frame):
    def __init__(self, parent, labels, values, initial_value, command, bg_color, widths=None):
        super().__init__(parent, bg=bg_color)
        self.labels = labels  
        self.values = values  
        self.command = command
        self.bg_color = bg_color
        self.buttons = []

        for i, label in enumerate(labels):
            # Create a wrapper frame to simulate the border/outline
            frame = tk.Frame(self, bg=bg_color)
            frame.pack(side=tk.LEFT, padx=int(2 * scaling_factor))
            
            w = widths[i] if widths else 8
            btn = tk.Button(frame, text=label, width=w, font=scale_font(("Arial", 11, "bold")),
                            bd=0, relief=tk.FLAT, highlightthickness=0,
                            command=lambda idx=i: self._on_click(idx))
            btn.pack(padx=0, pady=0) # Base state: no padding
            self.buttons.append((btn, frame))

        try:
            self.current_index = values.index(initial_value)
        except ValueError:
            self.current_index = 0
        self._update_ui()

    def _on_click(self, index):
        if self.current_index != index:
            self.current_index = index
            self._update_ui()
            self.command(self.values[index])

    def _update_ui(self):
        for i, (btn, frame) in enumerate(self.buttons):
            if i == self.current_index:
                # Active: Show Cyan Frame Border
                frame.config(bg=highlight_color)
            else:
                # Inactive: Border matches button color
                frame.config(bg=button_gray)
            btn.config(bg=button_gray, fg="#FFFFFF", padx=0, pady=0)
            btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor)) # Consistent size

    def set_value(self, value):
        try:
            self.current_index = self.values.index(value)
            self._update_ui()
        except ValueError:
            pass

    def update_options(self, labels, values, current_value, widths=None):
        # Destroy all old buttons and frames
        for btn, frame in self.buttons:
            try:
                btn.destroy()
            except:
                pass
            try:
                frame.destroy()
            except:
                pass
        self.buttons.clear()
        
        self.labels = labels
        self.values = values
        
        for i, label in enumerate(labels):
            # Create a wrapper frame to simulate the border/outline
            frame = tk.Frame(self, bg=self.bg_color)
            frame.pack(side=tk.LEFT, padx=int(2 * scaling_factor))
            
            w = widths[i] if widths else 8
            btn = tk.Button(frame, text=label, width=w, font=scale_font(("Arial", 11, "bold")),
                            bd=0, relief=tk.FLAT, highlightthickness=0,
                            command=lambda idx=i: self._on_click(idx))
            btn.pack(padx=0, pady=0) # Base state: no padding
            self.buttons.append((btn, frame))
            
        try:
            self.current_index = values.index(current_value)
        except ValueError:
            self.current_index = 0
        self._update_ui()

class PlayerInfoBlock:
    def __init__(self, parent, window):
        self.parent = parent
        self.window = window
        self.controller_label = None
        self.player_led_label = None
        self.current_vc = None
        self.mag_btn_single = None
        self.mag_frame_single = None
        self.mag_btn_l = None
        self.mag_frame_l = None
        self.mag_btn_r = None
        self.mag_frame_r = None

        self.load_pictures()
        self.init_interface()

    def get_left_controller(self):
        if self.current_vc is None: return None
        for c in self.current_vc.controllers:
            if c.is_joycon_left():
                return c
        return None

    def get_right_controller(self):
        if self.current_vc is None: return None
        for c in self.current_vc.controllers:
            if c.is_joycon_right():
                return c
        return None

    def get_single_controller(self):
        if self.current_vc is None or not self.current_vc.controllers: return None
        return self.current_vc.controllers[0]

    def _on_mag_clicked(self, controller, btn, frame):
        if controller is None: return
        if not getattr(controller, 'is_mag_calibrating', False):
            controller.start_mag_calibration()
            btn.config(text="Stop Cal", fg="white")
            frame.config(bg="#FF8C00")
            btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))
        else:
            controller.stop_mag_calibration()
            btn.config(text="Mag Cal", fg="white")
            frame.config(bg=button_gray)
            btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))

    def _on_split_clicked(self):
        if self.current_vc is not None:
            vc_index = self.current_vc.player_number - 1
            split_controller(vc_index)

    def _on_merge_clicked(self):
        global pending_merge_vc_index
        if self.current_vc is not None:
            vc_index = self.current_vc.player_number - 1
            if pending_merge_vc_index is None:
                pending_merge_vc_index = vc_index
            elif pending_merge_vc_index == vc_index:
                pending_merge_vc_index = None
            else:
                v1 = VIRTUAL_CONTROLLERS[pending_merge_vc_index]
                v2 = self.current_vc
                is_opposite = (v1.is_single_joycon_left() and v2.is_single_joycon_right()) or \
                              (v1.is_single_joycon_right() and v2.is_single_joycon_left())

                if is_opposite:
                    merge_controllers(pending_merge_vc_index, vc_index)
                    pending_merge_vc_index = None
                else:
                    pending_merge_vc_index = vc_index

            self.window.update(list(VIRTUAL_CONTROLLERS))

    def _on_vibrate_clicked(self):
        from controller import VibrationData
        if self.current_vc is not None and getattr(self.current_vc, 'loop', None):
            vib = VibrationData(lf_amp=800, hf_amp=800)
            off = VibrationData(lf_amp=0, hf_amp=0)
            for controller in self.current_vc.controllers:
                asyncio.run_coroutine_threadsafe(controller.set_vibration(vib, ignore_freq_scaling=True), self.current_vc.loop)
                self.parent.after(100, lambda c=controller, loop=self.current_vc.loop, o=off: 
                    asyncio.run_coroutine_threadsafe(c.set_vibration(o, ignore_freq_scaling=True), loop))
                self.parent.after(200, lambda c=controller, loop=self.current_vc.loop, v=vib: 
                    asyncio.run_coroutine_threadsafe(c.set_vibration(v, ignore_freq_scaling=True), loop))
                self.parent.after(300, lambda c=controller, loop=self.current_vc.loop, o=off: 
                    asyncio.run_coroutine_threadsafe(c.set_vibration(o, ignore_freq_scaling=True), loop))
            
            # Brief UI feedback (consistent size)
            if getattr(self, 'vibrate_frame', None):
                self.vibrate_frame.config(bg=highlight_color)
                self.vibrate_btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))
                self.parent.after(400, lambda: (self.vibrate_frame.config(bg=button_gray), self.vibrate_btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))))

    def _on_hold_mode_toggled(self, val):
        if self.current_vc is not None:
            self.current_vc.hold_mode = val
            self._update_controller_image()
            
            # Save hold mode mapped by MAC address for single joycons
            if self.current_vc.is_single() and len(self.current_vc.controllers) > 0:
                c = self.current_vc.controllers[0]
                if c.is_joycon():
                    addr = c.device.address
                    CONFIG.joycon_hold_mode[addr] = val
                    CONFIG.save_config()

    def _on_gyro_side_toggled(self, val):
        if self.current_vc is not None:
            djg_enabled = getattr(CONFIG, "djg_enabled", False)
            djg_mode = getattr(CONFIG, "djg_mode", "Single Side Toggle")
            
            if djg_enabled and djg_mode != "Switch Gyro Side":
                if val == "Left":
                    self.current_vc.djg_left_active = not getattr(self.current_vc, 'djg_left_active', True)
                elif val == "Right":
                    self.current_vc.djg_right_active = not getattr(self.current_vc, 'djg_right_active', True)
            else:
                self.current_vc.active_gyro_side = val
                if djg_mode == "Switch Gyro Side":
                    CONFIG.djg_dominant_side = val
                    CONFIG.save_config()
                
                if not self.current_vc.is_single() and len(self.current_vc.controllers) == 2:
                    left_mac = None
                    right_mac = None
                    for c in self.current_vc.controllers:
                        if c.is_joycon_left():
                            left_mac = c.device.address
                        elif c.is_joycon_right():
                            right_mac = c.device.address
                    if left_mac and right_mac:
                        key = f"{left_mac}+{right_mac}"
                        CONFIG.merged_gyro_side[key] = val
                        CONFIG.save_config()
            self.window.force_refresh_player_slots()

    def _update_controller_image(self):
        if self.current_vc is None: return
        if not self.current_vc.is_single():
            image = self.joycon2leftandright
        elif self.current_vc.is_single_joycon_right():
            image = self.joycon2right_sideway if self.current_vc.hold_mode == "Horizontal" else self.joycon2right_vertical
        elif self.current_vc.is_single_joycon_left():
            image = self.joycon2left_sideway if self.current_vc.hold_mode == "Horizontal" else self.joycon2left_vertical
        elif len(self.current_vc.controllers) > 0 and getattr(self.current_vc.controllers[0].controller_info, 'product_id', 0) == NSO_GAMECUBE_CONTROLLER_PID:
            image = self.gamecubecontroller
        else:
            image = self.procontroller2
        if image:
            self.controller_label.configure(image=image)

    def init_interface(self):
        self.main_frame = tk.Frame(self.parent, width=controller_frame_size, height=controller_frame_size + int(8 * scaling_factor) + battery_height, bg=player_number_bg_color)
        self.main_frame.pack_propagate(False)
        self.controllers_frame = tk.Frame(self.main_frame, width=controller_frame_size, height=controller_frame_size - battery_height, bg=block_color)
        self.controllers_frame.pack()
        self.controllers_frame.pack_propagate(False)
        self.battery_frame = tk.Frame(self.main_frame, width=controller_frame_size, height=battery_height, bg=block_color)
        self.battery_frame.pack()
        self.battery_frame.pack_propagate(False)
        self.player_row = None
        self.controller_label = None
        self.player_led_label = None

    async def _disconnect_merged_sequential(self, vc):
        async with vc._disconnect_lock:
            if not getattr(vc, 'running', False) and vc.vg_controller is None and not vc.controllers:
                return
                
            vc.running = False
            import time
            import gc
            current_time = time.strftime("%H:%M:%S")
            logger.info(f"[{current_time}] Player {vc.player_number} (Merged): Starting safe sequential disconnect sequence...")
            
            # Wait for the update thread to finish before proceeding with handle cleanup
            if hasattr(vc, 'update_thread') and vc.update_thread.is_alive():
                logger.info(f"Player {vc.player_number}: Waiting for update thread to exit...")
                vc.update_thread.join(timeout=0.5)
                if vc.update_thread.is_alive():
                    logger.warning(f"Player {vc.player_number}: Update thread did not exit in time!")
            
            if not vc.controllers and vc.vg_controller is None:
                return

            logger.info(f"Player {vc.player_number}: Cleaning up virtual device and physical connections sequentially...")
            
            with vc.state_lock:
                if hasattr(vc, 'vg_controller') and vc.vg_controller is not None:
                    logger.info(f"Player {vc.player_number}: Unregistering notifications and clearing vg_controller")
                    try:
                        vc.vg_controller.unregister_notification()
                    except Exception as e:
                        logger.debug(f"Unregister notification failed: {e}")
                    if hasattr(vc.vg_controller, 'cmp_func'):
                        vc.vg_controller.cmp_func = None
                    if hasattr(vc.vg_controller, 'close'):
                        try:
                            vc.vg_controller.close()
                        except Exception:
                            pass
                    vc.vg_controller = None
            
            gc.collect()
            
            # Disconnect each physical controller sequentially with a delay to prevent Windows BLE driver bottlenecks
            for c in list(vc.controllers):
                c.interp_running = False
                if hasattr(c, 'interp_thread') and c.interp_thread.is_alive():
                    logger.info(f"Controller {c.device.address}: Joining interpolation thread (non-blocking)...")
                    try:
                        await asyncio.to_thread(c.interp_thread.join, 0.5)
                    except Exception as e:
                        logger.warning(f"Failed to join interpolation thread: {e}")
                        
                if hasattr(c, 'client') and c.client and c.client.is_connected:
                    logger.info(f"Safe Disconnect: Disconnecting {c.device.address}...")
                    try:
                        await c.client.stop_notify(INPUT_REPORT_UUID)
                    except Exception:
                        pass
                    try:
                        await c.client.stop_notify(COMMAND_RESPONSE_UUID)
                    except Exception:
                        pass
                        
                    try:
                        await asyncio.wait_for(c.client.disconnect(), timeout=2.5)
                    except Exception as e:
                        logger.debug(f"Bluetooth disconnect error (ignored): {e}")
                        
                # Call the disconnect callback while c.client is still not None to completely avoid AttributeError
                if vc.on_disconnected_callback:
                    try:
                        await vc.on_disconnected_callback(c)
                    except Exception as e:
                        logger.error(f"Error in on_disconnected_callback: {e}")
                        
                c.client = None
                await asyncio.sleep(0.3)
                
            vc.controllers.clear()
            logger.info(f"Player {vc.player_number} (Merged): Safe sequential disconnect complete.")

    def _on_close_clicked(self):
        if self.current_vc is not None:
            if hasattr(self, 'close_btn') and self.close_btn:
                self.close_btn.config(state=tk.DISABLED)
            
            if not self.current_vc.is_single():
                # Merge mode close button: run the highly safe sequential disconnect
                if self.current_vc.loop and self.current_vc.loop.is_running():
                    asyncio.run_coroutine_threadsafe(self._disconnect_merged_sequential(self.current_vc), self.current_vc.loop)
                else:
                    logger.error("Event loop not found or not running for merged controller.")
            else:
                # Single mode: standard trigger disconnect
                self.current_vc.trigger_disconnect()

    def load_pictures(self):
        sf = scaling_factor
        
        def load_img(path, w=None, h=None):
            try:
                img = Image.open(get_resource(path))
                if w is None or h is None:
                    orig_w, orig_h = img.size
                    w = _scaled_px(orig_w, scale=sf)
                    h = _scaled_px(orig_h, scale=sf)
                else:
                    w = max(1, int(w))
                    h = max(1, int(h))
                img = img.resize((w, h), Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.ANTIALIAS)
                return ImageTk.PhotoImage(img)
            except Exception as e:
                logger.error(f"Error scaling image {path}: {e}")
                return tk.PhotoImage(file=get_resource(path))
        
        self.joycon2leftandright = load_img("images/joycon2leftandright.png")
        self.joycon2right_sideway = load_img("images/joycon2right_sideway.png")
        self.joycon2left_sideway = load_img("images/joycon2left_sideway.png")
        try:
            self.joycon2right_vertical = load_img("images/joycon2right.png")
            self.joycon2left_vertical = load_img("images/joycon2left.png")
        except Exception:
            self.joycon2right_vertical = self.joycon2right_sideway
            self.joycon2left_vertical = self.joycon2left_sideway
        self.procontroller2 = load_img("images/procontroller2.png")
        self.gamecubecontroller = load_img("images/nsogamecubecontroller.png")
        
        bat_w, bat_h = _scaled_px(28, scale=sf), _scaled_px(14, scale=sf)
        self.battery_h = load_img("images/battery_h.png", bat_w, bat_h)
        self.battery_m = load_img("images/battery_m.png", bat_w, bat_h)
        self.battery_l = load_img("images/battery_l.png", bat_w, bat_h)
        
        self.player_leds = {
            nb: load_img(f"images/player{nb}.png", player_led_width, player_led_height)
            for nb in range(1,5)
        }

    def clearControllerInfo(self):
        for attr in ['controller_label', 'player_led_label', 'close_btn', 'split_btn', 'split_frame', 'merge_btn', 'merge_frame', 'mode_switch', 'gyro_btn_l', 'gyro_btn_r', 'gyro_frame_l', 'gyro_frame_r', 'vibrate_btn', 'vibrate_frame', 'player_row', 'battery_label', 'battery_label2', 'mag_btn_single', 'mag_frame_single', 'mag_btn_l', 'mag_frame_l', 'mag_btn_r', 'mag_frame_r']:
            widget = getattr(self, attr, None)
            if widget is not None:
                if attr in ['controller_label', 'player_row']: widget.pack_forget()
                else: widget.place_forget()

    def get_image_for_battery_level(self, controller: Controller):
        if controller.battery_voltage is None: return self.battery_l
        if controller.battery_voltage > 3.25: return self.battery_h
        if controller.battery_voltage > 3.125: return self.battery_m
        return self.battery_l

    def displayControllersInfo(self, virtualController : VirtualController):
        self.current_vc = virtualController
        if not self.controller_label:
            self.controller_label = tk.Label(self.controllers_frame, bg=block_color)
        self.controller_label.pack(fill="none", expand=True)
        self._update_controller_image()

        if not getattr(self, 'close_btn', None):
            self.close_btn = tk.Button(self.controllers_frame, text="X", bg=block_color, fg="#FFFFFF", bd=0, 
                                       relief=tk.FLAT, highlightthickness=0,
                                       font=scale_font(("Arial", 14, "bold")), activebackground="#ff4444", activeforeground="white", 
                                       command=self._on_close_clicked)
        self.close_btn.place(x=controller_frame_size - int(30 * scaling_factor), y=int(5 * scaling_factor), width=int(25 * scaling_factor), height=int(25 * scaling_factor))
        if self.close_btn.cget("state") == tk.DISABLED: self.close_btn.config(state=tk.NORMAL)

        if virtualController.is_single():
            if not getattr(self, 'battery_label', None): self.battery_label = tk.Label(self.battery_frame, bg=block_color)
            self.battery_label.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
            if virtualController.controllers: self.battery_label.config(image=self.get_image_for_battery_level(virtualController.controllers[0]))
            if getattr(self, 'battery_label2', None): self.battery_label2.place_forget()

            if getattr(self, 'mag_frame_l', None): self.mag_frame_l.place_forget()
            if getattr(self, 'mag_frame_r', None): self.mag_frame_r.place_forget()

            c = self.get_single_controller()
            if c is not None and getattr(c.controller_info, 'product_id', 0) != NSO_GAMECUBE_CONTROLLER_PID:
                if not getattr(self, 'mag_btn_single', None):
                    self.mag_frame_single = tk.Frame(self.controllers_frame, bg=button_gray)
                    self.mag_btn_single = tk.Button(self.mag_frame_single, text="Mag Cal", font=scale_font(("Arial", 8, "bold")), bd=0, relief=tk.FLAT, highlightthickness=0,
                                                    command=lambda: self._on_mag_clicked(self.get_single_controller(), self.mag_btn_single, self.mag_frame_single))
                    self.mag_btn_single.pack()
                
                if getattr(c, 'is_mag_calibrating', False):
                    self.mag_btn_single.config(text="Stop Cal", bg=button_gray, fg="white")
                    self.mag_frame_single.config(bg="#FF8C00")
                else:
                    self.mag_btn_single.config(text="Mag Cal", bg=button_gray, fg="white")
                    self.mag_frame_single.config(bg=button_gray)
                self.mag_btn_single.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))
                self.mag_frame_single.place(x=int(150 * scaling_factor), y=int(125 * scaling_factor))
            else:
                if getattr(self, 'mag_frame_single', None): self.mag_frame_single.place_forget()
        else:
            if not getattr(self, 'battery_label', None): self.battery_label = tk.Label(self.battery_frame, bg=block_color)
            if not getattr(self, 'battery_label2', None): self.battery_label2 = tk.Label(self.battery_frame, bg=block_color)
            self.battery_label.place(relx=0.4, rely=0.5, anchor=tk.CENTER)
            if len(virtualController.controllers) > 0: self.battery_label.config(image=self.get_image_for_battery_level(virtualController.controllers[0]))
            self.battery_label2.place(relx=0.6, rely=0.5, anchor=tk.CENTER)
            if len(virtualController.controllers) > 1: self.battery_label2.config(image=self.get_image_for_battery_level(virtualController.controllers[1]))

            if getattr(self, 'mag_frame_single', None): self.mag_frame_single.place_forget()

            lc = self.get_left_controller()
            if lc is not None:
                if not getattr(self, 'mag_btn_l', None):
                    self.mag_frame_l = tk.Frame(self.controllers_frame, bg=button_gray)
                    self.mag_btn_l = tk.Button(self.mag_frame_l, text="Mag Cal", font=scale_font(("Arial", 8, "bold")), bd=0, relief=tk.FLAT, highlightthickness=0,
                                                command=lambda: self._on_mag_clicked(self.get_left_controller(), self.mag_btn_l, self.mag_frame_l))
                    self.mag_btn_l.pack()
                
                if getattr(lc, 'is_mag_calibrating', False):
                    self.mag_btn_l.config(text="Stop Cal", bg=button_gray, fg="white")
                    self.mag_frame_l.config(bg="#FF8C00")
                else:
                    self.mag_btn_l.config(text="Mag Cal", bg=button_gray, fg="white")
                    self.mag_frame_l.config(bg=button_gray)
                self.mag_btn_l.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))
                self.mag_frame_l.place(x=int(5 * scaling_factor), y=int(125 * scaling_factor))
            else:
                if getattr(self, 'mag_frame_l', None): self.mag_frame_l.place_forget()

            rc = self.get_right_controller()
            if rc is not None:
                if not getattr(self, 'mag_btn_r', None):
                    self.mag_frame_r = tk.Frame(self.controllers_frame, bg=button_gray)
                    self.mag_btn_r = tk.Button(self.mag_frame_r, text="Mag Cal", font=scale_font(("Arial", 8, "bold")), bd=0, relief=tk.FLAT, highlightthickness=0,
                                                command=lambda: self._on_mag_clicked(self.get_right_controller(), self.mag_btn_r, self.mag_frame_r))
                    self.mag_btn_r.pack()
                
                if getattr(rc, 'is_mag_calibrating', False):
                    self.mag_btn_r.config(text="Stop Cal", bg=button_gray, fg="white")
                    self.mag_frame_r.config(bg="#FF8C00")
                else:
                    self.mag_btn_r.config(text="Mag Cal", bg=button_gray, fg="white")
                    self.mag_frame_r.config(bg=button_gray)
                self.mag_btn_r.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))
                self.mag_frame_r.place(x=int(150 * scaling_factor), y=int(125 * scaling_factor))
            else:
                if getattr(self, 'mag_frame_r', None): self.mag_frame_r.place_forget()

        global pending_merge_vc_index
        if not virtualController.is_single():
            if not getattr(self, 'split_btn', None):
                self.split_frame = tk.Frame(self.controllers_frame, bg=button_gray)
                self.split_btn = tk.Button(self.split_frame, text="Split", bg=button_gray, fg="white", bd=0,
                                           relief=tk.FLAT, highlightthickness=0,
                                           font=scale_font(("Arial", 10, "bold")), command=self._on_split_clicked)
                self.split_btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))
            self.split_frame.place(x=int(5 * scaling_factor), y=int(5 * scaling_factor))
            if getattr(self, 'merge_btn', None): self.merge_frame.place_forget()
            if getattr(self, 'mode_switch', None): self.mode_switch.place_forget()

            if virtualController.mode != "Switch1":
                if not getattr(self, 'gyro_btn_l', None):
                    self.gyro_frame_l = tk.Frame(self.battery_frame, bg=block_color)
                    self.gyro_frame_r = tk.Frame(self.battery_frame, bg=block_color)
                    self.gyro_btn_l = tk.Button(self.gyro_frame_l, text="L Gyro", font=scale_font(("Arial", 8, "bold")), bd=0, relief=tk.FLAT, command=lambda: self._on_gyro_side_toggled("Left"))
                    self.gyro_btn_r = tk.Button(self.gyro_frame_r, text="R Gyro", font=scale_font(("Arial", 8, "bold")), bd=0, relief=tk.FLAT, command=lambda: self._on_gyro_side_toggled("Right"))
                    self.gyro_btn_l.pack(); self.gyro_btn_r.pack()
    
                self.gyro_frame_l.place(relx=0.04, rely=0.5, anchor=tk.W)
                self.gyro_frame_r.place(relx=0.96, rely=0.5, anchor=tk.E)
                if getattr(CONFIG, "djg_enabled", False) and getattr(CONFIG, "djg_mode", "Single Side Toggle") != "Switch Gyro Side":
                    self.gyro_frame_l.config(bg=highlight_color if getattr(virtualController, 'djg_left_active', True) else button_gray)
                    self.gyro_frame_r.config(bg=highlight_color if getattr(virtualController, 'djg_right_active', True) else button_gray)
                elif virtualController.active_gyro_side == "Left":
                    self.gyro_frame_l.config(bg=highlight_color)
                    self.gyro_frame_r.config(bg=button_gray)
                else:
                    self.gyro_frame_l.config(bg=button_gray)
                    self.gyro_frame_r.config(bg=highlight_color)
                self.gyro_btn_l.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))
                self.gyro_btn_r.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))
                for b in [self.gyro_btn_l, self.gyro_btn_r]: b.config(bg=button_gray, fg="#FFFFFF")
            else:
                if getattr(self, 'gyro_btn_l', None):
                    self.gyro_frame_l.place_forget()
                    self.gyro_frame_r.place_forget()
        else:
            if getattr(self, 'split_frame', None): self.split_frame.place_forget()
            if getattr(self, 'gyro_btn_l', None):
                self.gyro_frame_l.place_forget()
                self.gyro_frame_r.place_forget()

            vc_index = virtualController.player_number - 1
            is_left = virtualController.is_single_joycon_left()
            is_right = virtualController.is_single_joycon_right()

            if is_left or is_right:
                has_opposite = any(vc for vc in VIRTUAL_CONTROLLERS if vc is not None and vc != self.current_vc and 
                                   ((is_left and vc.is_single_joycon_right()) or (is_right and vc.is_single_joycon_left())))

                if has_opposite or pending_merge_vc_index == vc_index:
                    if not getattr(self, 'merge_btn', None):
                        self.merge_frame = tk.Frame(self.controllers_frame, bg=block_color)
                        self.merge_btn = tk.Button(self.merge_frame, fg="white", bd=0, relief=tk.FLAT, font=scale_font(("Arial", 10, "bold")), command=self._on_merge_clicked)
                        self.merge_btn.pack()
                    self.merge_frame.place(x=int(5 * scaling_factor), y=int(5 * scaling_factor))

                    m_text = "Merge"; m_color = "white"; m_border = block_color; m_pad = 0
                    if pending_merge_vc_index == vc_index:
                        m_text = "Selecting"; m_color = "#FFFFFF"; m_border = highlight_color; m_pad = 2
                    elif pending_merge_vc_index is not None:
                        p_vc = VIRTUAL_CONTROLLERS[pending_merge_vc_index]
                        if p_vc and ((is_left and p_vc.is_single_joycon_right()) or (is_right and p_vc.is_single_joycon_left())):
                            m_text = "Merge"; m_color = "#FFFFFF"; m_border = "#FF8C00"; m_pad = 2

                    self.merge_btn.config(text=m_text, bg=button_gray, fg=m_color)
                    self.merge_frame.config(bg=m_border)
                    self.merge_btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor)) # Consistent size
                elif getattr(self, 'merge_btn', None): self.merge_frame.place_forget()

                if virtualController.mode != "Switch1":
                    if not getattr(self, 'mode_switch', None):
                        self.mode_switch = ToggleSwitch(self.battery_frame, ["V", "H"], ["Vertical", "Horizontal"], virtualController.hold_mode, self._on_hold_mode_toggled, block_color)
                        for btn_data in self.mode_switch.buttons:
                            btn_data[0].config(font=scale_font(("Arial", 9, "bold")), width=2, padx=0, pady=0)
                    self.mode_switch.place(relx=0.98, rely=0.5, anchor=tk.E)
                    self.mode_switch.set_value(virtualController.hold_mode)
                else:
                    if getattr(self, 'mode_switch', None): self.mode_switch.place_forget()
            else:
                if getattr(self, 'merge_btn', None): self.merge_frame.place_forget()
                if getattr(self, 'mode_switch', None): self.mode_switch.place_forget()

        if not getattr(self, 'player_row', None):
            self.player_row = tk.Frame(self.main_frame, bg=player_number_bg_color, width=controller_frame_size, height=player_row_height)
            self.player_row.pack_propagate(False)
            self.player_led_label = tk.Label(self.player_row, bg=player_number_bg_color)
            self.vibrate_frame = tk.Frame(self.player_row, bg=button_gray)
            self.vibrate_btn = tk.Button(self.vibrate_frame, text="Ping", bg=button_gray, fg="white", bd=0, relief=tk.FLAT, font=scale_font(("Arial", 9, "bold")), width=5, command=self._on_vibrate_clicked)
            self.vibrate_btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))
        self.player_row.pack(pady=int(10 * scaling_factor))
        self.player_led_label.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        self.vibrate_frame.place(relx=0.96, rely=0.5, anchor=tk.E)
        self.player_led_label.config(image=self.player_leds[virtualController.player_number])

class CalibrationOverlay:
    def __init__(self, root):
        self.root = root
        self.window = None
        self.lbl_title = None
        self.lbl_msg = None
        self.close_timer = None
        self.profile_window = None
        self.profile_close_timer = None

    def update(self, title, message):
        # We must run this on the main thread. If we are called from a background thread,
        # we schedule it via self.root.after
        if threading.current_thread() != threading.main_thread():
            self.root.after(0, self.update, title, message)
            return
            
        if self.window is None or not self.window.winfo_exists():
            self._create_window()
            
        # Highlight colors depending on status
        if "started" in message.lower() or "progress" in message.lower() or "stationary" in message.lower():
            color = "#ff9f0a" # Orange
        elif "complete" in message.lower() or "success" in message.lower():
            color = "#30d158" # Green
        elif "cancelled" in message.lower():
            color = "#ff453a" # Red
        else:
            color = "#0a84ff" # Blue
            
        self.lbl_title.config(text=title, fg=color)
        self.lbl_msg.config(text=message)
        
        # Cancel any pending auto-close timer
        if self.close_timer:
            self.root.after_cancel(self.close_timer)
            self.close_timer = None
            
        # Auto close after 3 seconds for final completion / cancellation
        # We do not auto-close on Gyro completion because it has instructions waiting for Mag start
        is_final_complete = "magnetometer calibration complete" in message.lower()
        is_cancelled = "cancelled" in message.lower()
        is_profile = "profile" in title.lower()
        if is_final_complete or is_cancelled or is_profile:
            self.close_timer = self.root.after(3000, self.close)

    def _create_window(self):
        self.window = tk.Toplevel(self.root)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", 0.95)
        self.window.configure(bg="#1c1c1e")
        
        screen_width = self.window.winfo_screenwidth()
        screen_height = self.window.winfo_screenheight()
        w, h = int(500 * scaling_factor), int(110 * scaling_factor)
        x = screen_width - w - int(30 * scaling_factor)
        y = screen_height - h - int(70 * scaling_factor) # Bottom-right, staying above the taskbar
        self.window.geometry(f"{w}x{h}+{x}+{y}")
        
        frame = tk.Frame(self.window, bg="#1c1c1e", highlightbackground="#3a3a3c", highlightthickness=2, bd=0)
        frame.pack(fill="both", expand=True)
        
        self.lbl_title = tk.Label(frame, text="Switch 2 Controller", fg="#0a84ff", bg="#1c1c1e", font=scale_font(("Segoe UI", 11, "bold")))
        self.lbl_title.pack(anchor="w", padx=int(20 * scaling_factor), pady=(int(12 * scaling_factor), int(2 * scaling_factor)))
        
        self.lbl_msg = tk.Label(frame, text="", fg="#ffffff", bg="#1c1c1e", font=scale_font(("Segoe UI", 11)), justify="left", wraplength=int(460 * scaling_factor))
        self.lbl_msg.pack(anchor="w", padx=int(20 * scaling_factor), pady=(0, int(12 * scaling_factor)))

    def close(self):
        if threading.current_thread() != threading.main_thread():
            self.root.after(0, self.close)
            return
            
        if self.window and self.window.winfo_exists():
            self.window.destroy()
        self.window = None

    def show_profile_selection(self, prev_name, selected_name, next_name, manual, layout_label, auto_close_ms=None, name_px=0):
        if threading.current_thread() != threading.main_thread():
            self.root.after(0, self.show_profile_selection, prev_name, selected_name, next_name, manual, layout_label, auto_close_ms, name_px)
            return

        CYAN = "#00e5ff"
        GREEN = "#30d158"
        RED = "#ff453a"
        WHITE = "#ffffff"
        BG = "#1c1c1e"

        # Rebuild the window/content only when it doesn't exist or the mode/layout
        # changed. During cycling we only update the three profile-name labels so the
        # window doesn't flicker (title, background and instructions stay put).
        rebuild = (self.profile_window is None or not self.profile_window.winfo_exists()
                   or not hasattr(self, "_profile_sel_lbl")
                   or getattr(self, "_profile_manual", None) != manual
                   or getattr(self, "_profile_layout", None) != layout_label)

        if rebuild:
            if self.profile_window is not None and self.profile_window.winfo_exists():
                self.profile_window.destroy()
            self.profile_window = tk.Toplevel(self.root)
            self.profile_window.overrideredirect(True)
            self.profile_window.attributes("-topmost", True)
            self.profile_window.attributes("-alpha", 0.95)
            self.profile_window.configure(bg=BG)
            self._profile_manual = manual
            self._profile_layout = layout_label

            pad = int(14 * scaling_factor)
            frame = tk.Frame(self.profile_window, bg=BG, highlightbackground="#3a3a3c", highlightthickness=2, bd=0)
            frame.pack(fill="both", expand=True)

            tk.Label(frame, text="Change Profile To", fg=WHITE, bg=BG, font=scale_font(("Segoe UI", 11, "bold"))).pack(padx=pad, pady=(int(10 * scaling_factor), int(6 * scaling_factor)))

            self._profile_prev_lbl = tk.Label(frame, text=" ", fg=WHITE, bg=BG, font=scale_font(("Segoe UI", 11)))
            self._profile_prev_lbl.pack(padx=pad)

            sel_wrap = tk.Frame(frame, bg=BG, highlightbackground=CYAN, highlightcolor=CYAN, highlightthickness=2, bd=0)
            sel_wrap.pack(pady=int(2 * scaling_factor))
            self._profile_sel_lbl = tk.Label(sel_wrap, text=" ", fg=WHITE, bg=BG, font=scale_font(("Segoe UI", 11, "bold")))
            self._profile_sel_lbl.pack(padx=int(6 * scaling_factor), pady=int(1 * scaling_factor))

            self._profile_next_lbl = tk.Label(frame, text=" ", fg=WHITE, bg=BG, font=scale_font(("Segoe UI", 11)))
            self._profile_next_lbl.pack(padx=pad)

            if manual:
                tk.Label(frame, text=f"Press {layout_label} Layout", fg=WHITE, bg=BG, font=scale_font(("Segoe UI", 10))).pack(pady=(int(8 * scaling_factor), 0))
                row = tk.Frame(frame, bg=BG)
                row.pack(pady=(0, int(10 * scaling_factor)))
                tk.Label(row, text="A button to SELECT", fg=GREEN, bg=BG, font=scale_font(("Segoe UI", 10, "bold"))).pack(side=tk.LEFT)
                tk.Label(row, text=" or ", fg=WHITE, bg=BG, font=scale_font(("Segoe UI", 10))).pack(side=tk.LEFT)
                tk.Label(row, text="B button to CANCEL", fg=RED, bg=BG, font=scale_font(("Segoe UI", 10, "bold"))).pack(side=tk.LEFT)
            else:
                tk.Frame(frame, bg=BG, height=int(8 * scaling_factor)).pack()

            self._profile_prev_lbl.config(text=prev_name or " ")
            self._profile_sel_lbl.config(text=selected_name or " ")
            self._profile_next_lbl.config(text=next_name or " ")

            # Width: 2/3 of the old notification width as a lower bound, widened to fit
            # the longest profile name in the change list (name_px) so cycling never
            # clips or resizes; height fits the content.
            target_w = int(500 * scaling_factor * 2 / 3)
            self.profile_window.update_idletasks()
            w = max(target_w, self.profile_window.winfo_reqwidth(), int(name_px) + int(40 * scaling_factor))
            h = self.profile_window.winfo_reqheight()
            sw = self.profile_window.winfo_screenwidth()
            sh = self.profile_window.winfo_screenheight()
            x = sw - w - int(30 * scaling_factor)
            y = sh - h - int(70 * scaling_factor)
            self.profile_window.geometry(f"{w}x{h}+{x}+{y}")
        else:
            self._profile_prev_lbl.config(text=prev_name or " ")
            self._profile_sel_lbl.config(text=selected_name or " ")
            self._profile_next_lbl.config(text=next_name or " ")

        self.profile_window.lift()

        if self.profile_close_timer:
            self.root.after_cancel(self.profile_close_timer)
            self.profile_close_timer = None
        if auto_close_ms:
            self.profile_close_timer = self.root.after(auto_close_ms, self.close_profile_selection)

    def close_profile_selection(self):
        if threading.current_thread() != threading.main_thread():
            self.root.after(0, self.close_profile_selection)
            return
        if self.profile_close_timer:
            try:
                self.root.after_cancel(self.profile_close_timer)
            except Exception:
                pass
            self.profile_close_timer = None
        if self.profile_window and self.profile_window.winfo_exists():
            self.profile_window.destroy()
        self.profile_window = None

class GCTriggerCalibrationWizard:
    def __init__(self, root, gc_controller):
        self.root = root
        self.gc_controller = gc_controller
        self.window = tk.Toplevel(root)
        self.window.title("GameCube Trigger Calibration")
        w, h = int(450 * scaling_factor), int(180 * scaling_factor)
        self.window.geometry(f"{w}x{h}")
        self.window.attributes("-topmost", True)
        self.window.configure(bg=background_color)
        
        self.step = 0
        self.min_l = 36
        self.bump_l = 190
        self.max_l = 240
        self.min_r = 36
        self.bump_r = 190
        self.max_r = 240

        self.title_label = tk.Label(self.window, text="Step 1: Base State", font=scale_font(("Arial", 14, "bold")), bg=background_color, fg=highlight_color)
        self.title_label.pack(pady=(int(10 * scaling_factor), 0))

        self.desc_label = tk.Label(self.window, text="Release both triggers completely and wait a moment.\nThen click Next.", font=scale_font(("Arial", 11)), bg=background_color, fg="white", wraplength=int(400 * scaling_factor))
        self.desc_label.pack(pady=int(10 * scaling_factor))

        self.val_label = tk.Label(self.window, text="L: 0 | R: 0", font=scale_font(("Arial", 10)), bg=background_color, fg="#888888")
        self.val_label.pack(pady=(0, int(10 * scaling_factor)))

        self.btn_frame = tk.Frame(self.window, bg=background_color)
        self.btn_frame.pack()

        self.cancel_btn = tk.Button(self.btn_frame, text="Cancel", font=scale_font(("Arial", 10)), bg=button_gray, fg="white", bd=0, command=self.close)
        self.cancel_btn.pack(side=tk.LEFT, padx=int(10 * scaling_factor))

        self.next_btn = tk.Button(self.btn_frame, text="Next", font=scale_font(("Arial", 10, "bold")), bg=highlight_color, fg="black", bd=0, command=self.on_next)
        self.next_btn.pack(side=tk.LEFT, padx=int(10 * scaling_factor))

        self.update_loop()

    def update_loop(self):
        if not self.window.winfo_exists():
            return
        if hasattr(self.gc_controller, 'last_input_data') and self.gc_controller.last_input_data:
            l = self.gc_controller.last_input_data.left_trigger_raw
            r = self.gc_controller.last_input_data.right_trigger_raw
            self.val_label.config(text=f"L: {l} | R: {r}")
            
            if self.step == 0:
                self.min_l = l
                self.min_r = r
            elif self.step == 1:
                if l > self.bump_l: self.bump_l = l
            elif self.step == 2:
                if l > self.max_l: self.max_l = l
            elif self.step == 3:
                if r > self.bump_r: self.bump_r = r
            elif self.step == 4:
                if r > self.max_r: self.max_r = r

        self.root.after(50, self.update_loop)

    def on_next(self):
        if self.step == 0:
            self.step = 1
            self.title_label.config(text="Step 2: Left Trigger (Bump)")
            self.desc_label.config(text="Press the LEFT trigger down just until you feel the click (bump).\nHold it there and click Next.")
            self.bump_l = 0
        elif self.step == 1:
            self.step = 2
            self.title_label.config(text="Step 3: Left Trigger (Max)")
            self.desc_label.config(text="Fully press the LEFT trigger all the way down past the click.\nWhile holding it down, click Next.")
            self.max_l = 0
        elif self.step == 2:
            self.step = 3
            self.title_label.config(text="Step 4: Right Trigger (Bump)")
            self.desc_label.config(text="Press the RIGHT trigger down just until you feel the click (bump).\nHold it there and click Next.")
            self.bump_r = 0
        elif self.step == 3:
            self.step = 4
            self.title_label.config(text="Step 5: Right Trigger (Max)")
            self.desc_label.config(text="Fully press the RIGHT trigger all the way down past the click.\nWhile holding it down, click Finish.")
            self.max_r = 0
            self.next_btn.config(text="Finish")
        elif self.step == 4:
            CONFIG.gc_trigger_calibration_data[self.gc_controller.device.address] = [self.min_l, self.bump_l, self.max_l, self.min_r, self.bump_r, self.max_r]
            CONFIG.save_config()
            logger.info(f"Saved GC Trigger Calibration for {self.gc_controller.device.address}: {CONFIG.gc_trigger_calibration_data[self.gc_controller.device.address]}")
            from tkinter import messagebox
            messagebox.showinfo("Success", "GameCube Trigger Calibration saved successfully!")
            self.close()

    def close(self):
        if self.window and self.window.winfo_exists():
            self.window.destroy()

class ControllerWindow:
    def __init__(self):
        self.root = None
        self.main_frame = None
        self.settings_frame = None
        self.no_controllers = True
        self.message_queue = queue.Queue()
        self.quit_event = threading.Event()
        self.discoverer_callback = None
        self.power_listener = PowerListener(self.handle_power_event)
        self.last_width = CONFIG.window_width
        self.last_height = CONFIG.window_height
        self.last_x = CONFIG.window_x
        self.last_y = CONFIG.window_y
        self.last_foreground_app_path = None
        self.app_profile_poll_suspended = False
        self.app_profile_switching = False
        self.esp32s3_bridge_status = None
        self.esp32s3_detected = False
        self._esp32s3_refresh_running = False
        self._esp32s3_auto_firmware_running = False
        self._esp32s3_auto_firmware_attempted = set()
        self._esp32s3_current_seen = False
        # Mirrors esp32s3_detected as seen by the periodic status timer. Must be kept
        # in sync whenever detection state is set elsewhere (startup / post-flash
        # resume), otherwise the timer misreads the first poll as a fresh plug-in
        # event and needlessly restarts the discoverer, dropping a live controller.
        self._esp32s3_was_detected = False
        # True while a firmware flash + replug window is in progress. While set, the
        # periodic status timer must NOT open the COM port, otherwise it collides
        # with esptool during the flash and holds the port open during the replug,
        # forcing the user to restart the app to clear the occupancy.
        self._esp32s3_firmware_busy = False
        
        import utils
        utils.change_profile_callback = self.on_cycle_profile
        utils.switch_profile_callback = self.on_profile_combo_switch
        utils.profile_nav_callback = self.on_profile_nav
        utils.profile_confirm_callback = self.on_profile_confirm
        utils.profile_cancel_callback = self.on_profile_cancel
        utils.force_ui_update_callback = self.force_refresh_player_slots

    def center_window_on_root(self, window, width, height):
        self.root.update_idletasks()
        rx = self.root.winfo_x()
        ry = self.root.winfo_y()
        rw = self.root.winfo_width()
        rh = self.root.winfo_height()
        x = rx + (rw - width) // 2
        y = ry + (rh - height) // 2
        window.geometry(f"{width}x{height}+{x}+{y}")

    def get_root_hwnd(self):
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            return hwnd or self.root.winfo_id()
        except Exception:
            return None

    def show_centered_dialog(self, title, message, buttons=("OK",), default=None):
        if threading.current_thread() != threading.main_thread():
            done = threading.Event()
            result = {"value": default or buttons[-1]}

            def run_on_ui_thread():
                try:
                    result["value"] = self.show_centered_dialog(title, message, buttons, default)
                finally:
                    done.set()

            try:
                self.root.after(0, run_on_ui_thread)
                done.wait()
            except RuntimeError:
                pass
            return result["value"]

        dialog_w = int(500 * scaling_factor)
        extra_lines = message.count("\n") + max(0, len(message) // 70)
        dialog_h = max(int(150 * scaling_factor), min(int(280 * scaling_factor), int((135 + extra_lines * 18) * scaling_factor)))
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.resizable(False, False)
        dialog.config(bg="#1E1E1E")
        dialog.transient(self.root)
        dialog.grab_set()
        self.center_window_on_root(dialog, dialog_w, dialog_h)

        result = {"value": default or buttons[-1]}

        tk.Label(
            dialog,
            text=message,
            fg="white",
            bg="#1E1E1E",
            font=scale_font(("Arial", 11, "bold")),
            justify=tk.CENTER,
            wraplength=int(440 * scaling_factor),
        ).pack(padx=int(24 * scaling_factor), pady=(int(24 * scaling_factor), int(12 * scaling_factor)), fill=tk.BOTH, expand=True)

        button_frame = tk.Frame(dialog, bg="#1E1E1E")
        button_frame.pack(pady=(0, int(18 * scaling_factor)))

        def close(value):
            result["value"] = value
            dialog.grab_release()
            dialog.destroy()

        for button_text in buttons:
            frame = tk.Frame(button_frame, bg=button_gray)
            frame.pack(side=tk.LEFT, padx=int(6 * scaling_factor))
            btn = tk.Button(
                frame,
                text=button_text,
                bg=button_gray,
                fg=text_color,
                bd=0,
                relief=tk.FLAT,
                font=scale_font(("Arial", 10, "bold")),
                width=8,
                command=lambda value=button_text: close(value),
            )
            btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))
            if button_text == (default or buttons[-1]):
                btn.focus_set()

        dialog.protocol("WM_DELETE_WINDOW", lambda: close(default or buttons[-1]))
        self.root.wait_window(dialog)
        return result["value"]

    def ask_centered_yes_no(self, title, message):
        return self.show_centered_dialog(title, message, ("Yes", "No"), "No") == "Yes"

    def show_centered_message(self, title, message):
        self.show_centered_dialog(title, message, ("OK",), "OK")

    def refresh_esp32s3_status(self):
        try:
            try:
                from usb_serial_bridge import detect_bridge
                status = detect_bridge()
                self.esp32s3_bridge_status = status
            except Exception:
                self.esp32s3_bridge_status = None
            self.esp32s3_detected = bool(self.esp32s3_bridge_status and self.esp32s3_bridge_status.board_present)
        except Exception as e:
            logger.debug(f"ESP32-S3 status refresh failed: {e}")
            self.esp32s3_bridge_status = None
            self.esp32s3_detected = False
        self.update_driver_buttons_visibility()
        return self.esp32s3_bridge_status

    def refresh_esp32s3_status_async(self):
        if getattr(self, '_esp32s3_refresh_running', False) or getattr(self, 'is_quitting', False):
            return
        # Never probe the COM port while a firmware flash / replug is in progress —
        # doing so collides with esptool and re-occupies the port during replug.
        if getattr(self, '_esp32s3_firmware_busy', False):
            return
        self._esp32s3_refresh_running = True

        def worker():
            status = None
            detected = False
            try:
                from usb_serial_bridge import detect_bridge
                status = detect_bridge()
                detected = bool(status and status.board_present)
            except Exception as e:
                logger.debug(f"ESP32-S3 async status refresh failed: {e}")

            def apply_status():
                self._esp32s3_refresh_running = False
                if getattr(self, 'is_quitting', False):
                    return
                was_current = self._esp32s3_current_seen
                was_detected = getattr(self, '_esp32s3_was_detected', False)

                # If the bridge was recently ready and the new probe returns
                # "no firmware" (board still physically present), this is almost
                # certainly a transient PermissionError because the discoverer's
                # shared_client is holding the COM port open. Discarding the
                # result keeps Boot-mode detection confined to the firmware-flash
                # UI and prevents it from disrupting any connection logic.
                # Exception: OTG-only boards have no CDC serial port for the
                # discoverer to hold open, so a firmware_installed=False probe
                # on OTG is a genuine boot+reset event and must not be discarded.
                if (was_current
                        and status
                        and getattr(status, 'board_present', False)
                        and not getattr(status, 'firmware_installed', False)
                        and not getattr(status, 'otg_only', False)):
                    return  # transient probe failure — keep previous state

                self.esp32s3_bridge_status = status
                self.esp32s3_detected = detected
                self._esp32s3_was_detected = detected
                self._esp32s3_current_seen = bool(status and getattr(status, "bridge_ready", False))
                self.update_driver_buttons_visibility()
                self.maybe_auto_update_esp32s3_firmware(status)
                discoverer_running = bool(
                    getattr(self, 'discoverer_thread', None)
                    and self.discoverer_thread
                    and self.discoverer_thread.is_alive()
                )
                # Never tear down a live bridge session: if a controller is already
                # connected, a restart would disconnect it. A transient status-probe
                # timeout can briefly drop bridge_ready and make it look like the
                # bridge "just became ready" again on the next poll — restarting then
                # would kick the user's controller mid-use.
                has_live_controllers = any(
                    vc is not None for vc in getattr(self, 'current_controllers', []) or []
                )

                # Restart discoverer if bridge became ready OR if board was just plugged
                # in — but only when no controller is currently connected.
                if (((self._esp32s3_current_seen and not was_current) or (detected and not was_detected))
                        and discoverer_running and not has_live_controllers):
                    logger.info("ESP32-S3 state changed. Restarting discoverer...")
                    # Pause this 5 s status timer while the discoverer (re)opens the
                    # bridge COM port. Otherwise a transient detect_bridge probe from a
                    # later tick races the discoverer's persistent open on the freshly
                    # hot-plugged port and one side gets "port occupied" — which is why
                    # plugging the ESP32-S3 in AFTER launch failed but before launch
                    # worked. Resume probing once the open window has passed.
                    self._esp32s3_firmware_busy = True

                    def _hotplug_restart():
                        try:
                            self.start_discoverer_thread()
                        finally:
                            self.root.after(4000, lambda: setattr(self, '_esp32s3_firmware_busy', False))

                    self.root.after(100, _hotplug_restart)

            try:
                self.root.after(0, apply_status)
            except RuntimeError:
                self._esp32s3_refresh_running = False

        threading.Thread(target=worker, daemon=True).start()

    def maybe_auto_update_esp32s3_firmware(self, status, on_complete=None):
        # Never auto-flash via OTG: esptool requires manual BOOT button hold on native USB
        if getattr(status, "otg_only", False):
            return False
        if (
            not status
            or not getattr(status, "board_present", False)
            or not getattr(status, "firmware_update_required", False)
            or not getattr(status, "status_text", "")
            or not getattr(status, "firmware_version", "")
            or getattr(self, "_esp32s3_auto_firmware_running", False)
            or getattr(self, "is_quitting", False)
        ):
            return False

        serial_port = getattr(status, "serial_port", None)
        if not serial_port:
            logger.warning("ESP32-S3 firmware update is required, but CH343 flashing port was not detected.")
            return False

        attempt_key = (
            serial_port.port,
            getattr(status, "firmware_version", ""),
            getattr(status, "firmware_mode", ""),
            getattr(status, "expected_version", ""),
        )
        if attempt_key in self._esp32s3_auto_firmware_attempted:
            return False
        self._esp32s3_auto_firmware_attempted.add(attempt_key)
        self._esp32s3_auto_firmware_running = True

        discoverer_was_running = bool(
            getattr(self, 'discoverer_thread', None)
            and self.discoverer_thread
            and self.discoverer_thread.is_alive()
        )
        if discoverer_was_running:
            self.stop_discoverer_thread()

        def completed(ok):
            self._esp32s3_auto_firmware_running = False

            def resume():
                # Reopen the port only after the device re-enumerates post-replug.
                self._esp32s3_firmware_busy = False
                self.start_discoverer_thread()

            if on_complete:
                self._esp32s3_firmware_busy = False
                self.refresh_esp32s3_status_async()
                on_complete(ok)
            elif ok or discoverer_was_running:
                if ok:
                    # Keep the COM port free while the user replugs; resume once current.
                    self.root.after(1000, lambda: self.wait_for_current_esp32s3_then(resume))
                else:
                    self._esp32s3_firmware_busy = False
                    self.refresh_esp32s3_status_async()
                    self.root.after(0, self.start_discoverer_thread)
            else:
                self._esp32s3_firmware_busy = False
                self.refresh_esp32s3_status_async()

        logger.info(
            "ESP32-S3 firmware update required: current version=%s mode=%s expected=%s",
            getattr(status, "firmware_version", ""),
            getattr(status, "firmware_mode", ""),
            getattr(status, "expected_version", ""),
        )
        self.run_esp32s3_firmware_task("install", auto=True, status=status, on_complete=completed)
        return True

    def wait_for_current_esp32s3_then(self, callback, attempts=24):
        if getattr(self, "is_quitting", False):
            return

        def worker(remaining):
            status = None
            try:
                from usb_serial_bridge import detect_bridge
                status = detect_bridge()
            except Exception as e:
                logger.debug(f"Waiting for ESP32-S3 firmware HID failed: {e}")

            def apply_status():
                if getattr(self, "is_quitting", False):
                    return
                self.esp32s3_bridge_status = status
                board_present = bool(status and getattr(status, "board_present", False))
                self.esp32s3_detected = board_present
                self._esp32s3_was_detected = board_present
                self._esp32s3_current_seen = bool(status and getattr(status, "bridge_ready", False))
                self.update_driver_buttons_visibility()
                if self._esp32s3_current_seen or remaining <= 0:
                    callback()
                elif not board_present:
                    # ESP32 fully disconnected — clear state and fall back to System BLE immediately
                    self.esp32s3_bridge_status = None
                    self.esp32s3_detected = False
                    self._esp32s3_was_detected = False
                    self.update_driver_buttons_visibility()
                    callback()
                else:
                    self.root.after(500, lambda: self.wait_for_current_esp32s3_then(callback, remaining - 1))

            try:
                self.root.after(0, apply_status)
            except RuntimeError:
                pass

        threading.Thread(target=worker, args=(attempts,), daemon=True).start()

    def run_esp32s3_firmware_task(self, action, auto=False, status=None, on_complete=None):
        from tkinter import messagebox
        try:
            from usb_serial_bridge import ESP32S3_LABEL, flash_firmware
        except Exception:
            ESP32S3_LABEL = "ESP32-S3 CDC"
            flash_firmware = None
        
        # COM Port Release Protection Mechanism
        # Mark firmware busy BEFORE stopping discovery so the 5 s status timer can't
        # sneak in a COM-port probe between stop and flash (which would block esptool
        # or re-occupy the port across the replug).
        self._esp32s3_firmware_busy = True
        discoverer_was_running = False
        if hasattr(self, 'discoverer_thread') and self.discoverer_thread and self.discoverer_thread.is_alive():
            discoverer_was_running = True
            self.stop_discoverer_thread()

        # Run emergency cleanup to close all virtual controller handles
        from discoverer import emergency_cleanup
        emergency_cleanup()

        # Explicitly release every open serial client BEFORE flashing so esptool gets
        # exclusive access to the COM port. The discoverer's own shutdown should have
        # closed the shared client, but a lingering handle here is exactly what leaves
        # the port "occupied" after install and forces an app restart.
        try:
            from usb_serial_bridge import close_all_clients
            close_all_clients()
        except Exception:
            pass

        status = status or self.refresh_esp32s3_status()
        if not status or not status.serial_port:
            self._esp32s3_firmware_busy = False
            if discoverer_was_running:
                self.start_discoverer_thread()
            messagebox.showerror(
                ESP32S3_LABEL,
                "Could not find the ESP32-S3 N16R8 CH343/COM flashing port.\nConnect the flashing Type-C/CH343P port and try again."
            )
            return

        title_map = {
            "install": "Installing ESP32-S3 N16R8 Firmware",
            "repair": "Repairing ESP32-S3 N16R8 Firmware",
            "delete": "Deleting ESP32-S3 N16R8 Firmware",
        }
        verb_map = {
            "install": "installing",
            "repair": "repairing",
            "delete": "deleting",
        }

        progress_win = tk.Toplevel(self.root)
        progress_win.title(title_map.get(action, "ESP32-S3 N16R8 Firmware"))
        progress_win.geometry(f"{int(460 * scaling_factor)}x{int(150 * scaling_factor)}+180+180")
        progress_win.resizable(False, False)
        progress_win.config(bg="#1E1E1E")
        progress_win.transient(self.root)
        progress_win.grab_set()

        label = tk.Label(
            progress_win,
            text=f"{ESP32S3_LABEL}: {verb_map.get(action, 'working')} firmware on {status.serial_port.port}...",
            fg="white", bg="#1E1E1E",
            font=scale_font(("Arial", 11, "bold")),
            wraplength=int(420 * scaling_factor),
            justify=tk.CENTER
        )
        label.pack(pady=(int(18 * scaling_factor), int(10 * scaling_factor)), padx=int(16 * scaling_factor))

        progress_var = tk.DoubleVar(value=0)
        progress_bar = ttk.Progressbar(
            progress_win,
            orient=tk.HORIZONTAL,
            mode="determinate",
            maximum=100,
            variable=progress_var,
            length=int(380 * scaling_factor)
        )
        progress_bar.pack(padx=int(24 * scaling_factor), fill=tk.X)

        percent_label = tk.Label(
            progress_win,
            text="0%",
            fg="white", bg="#1E1E1E",
            font=scale_font(("Arial", 11, "bold"))
        )
        percent_label.pack(pady=(int(8 * scaling_factor), 0))
        progress_win.protocol("WM_DELETE_WINDOW", lambda: None)

        done = {"ok": False, "error": None}
        progress_queue = queue.Queue()

        def progress(payload):
            progress_queue.put(("progress", payload))

        def worker():
            try:
                flash_firmware(status.serial_port.port, mode=action, progress=progress)
                done["ok"] = True
            except Exception as e:
                done["error"] = e
                logger.exception("ESP32-S3 firmware task failed")
            finally:
                progress_queue.put(("done", None))

        def finish():
            if progress_win.winfo_exists():
                progress_win.grab_release()
                progress_win.destroy()

            def resume_discovery():
                # Clear the busy flag only once we're ready to reopen the port, so the
                # 5 s status timer stays quiet through the whole flash + replug window.
                self._esp32s3_firmware_busy = False
                if discoverer_was_running:
                    self.start_discoverer_thread()

            if done["ok"]:
                # Release the COM port so replug is clean and Windows doesn't report a stale handle.
                try:
                    from usb_serial_bridge import close_all_clients
                    close_all_clients()
                except Exception:
                    pass

                if action == "delete":
                    # Uninstall succeeded — firmware is gone, no replug needed.
                    messagebox.showinfo(
                        ESP32S3_LABEL,
                        "ESP32-S3 N16R8 firmware uninstalled successfully.",
                    )
                elif not auto:
                    messagebox.showinfo(
                        ESP32S3_LABEL,
                        "ESP32-S3 N16R8 firmware installed successfully.\n\n"
                        "Please replug the ESP32-S3 USB cable (unplug then reinsert) "
                        "to complete initialization and avoid port conflicts.",
                    )
                else:
                    # Auto-update path: show a brief replug reminder in a non-blocking way.
                    messagebox.showinfo(
                        ESP32S3_LABEL,
                        "Firmware auto-updated. Please replug the ESP32-S3 USB cable.",
                    )

                if on_complete:
                    # Auto-update path manages its own busy-clear + delayed restart.
                    on_complete(done["ok"])
                elif action == "delete":
                    # Firmware removed: nothing to wait for, resume discovery now.
                    self.refresh_esp32s3_status()
                    resume_discovery()
                else:
                    # Manual install/repair: the user must replug. Keep the COM port
                    # free and wait until the device re-enumerates with current
                    # firmware before reopening it, then resume discovery. This is what
                    # stops the "port occupied" state that previously needed an app restart.
                    self.root.after(1500, lambda: self.wait_for_current_esp32s3_then(resume_discovery))
            else:
                self._esp32s3_firmware_busy = False
                error_str = str(done["error"])
                if "Could not put ESP32-S3" in error_str and "into flashing mode" in error_str:
                    messagebox.showerror(ESP32S3_LABEL, (
                        "Could not enter flashing mode.\n\n"
                        "To enter Boot mode manually:\n"
                        "  1. Hold the BOOT button\n"
                        "  2. Tap RESET once, then release BOOT\n"
                        "  3. Click Repair to retry"
                    ))
                else:
                    messagebox.showerror(ESP32S3_LABEL, f"ESP32-S3 N16R8 firmware operation failed:\n{done['error']}")
                self.refresh_esp32s3_status()
                if on_complete:
                    on_complete(done["ok"])
                elif discoverer_was_running:
                    self.start_discoverer_thread()

        def apply_progress(payload):
            current = float(progress_var.get())
            percent = None
            message = None
            if isinstance(payload, dict):
                if "percent" in payload:
                    percent = float(payload["percent"])
                elif "write_percent" in payload:
                    percent = 25.0 + (max(0.0, min(100.0, float(payload["write_percent"]))) * 0.70)
                message = payload.get("message")
            else:
                text = str(payload)
                match = re.search(r"\((\d{1,3})\s*%\)", text)
                if match:
                    percent = 25.0 + (max(0.0, min(100.0, float(match.group(1)))) * 0.70)

            if percent is None:
                percent = min(95.0, current + 1.0)
            percent = max(current, min(100.0, percent))
            progress_var.set(percent)
            percent_label.config(text=f"{int(percent)}%")
            if message and progress_win.winfo_exists():
                label.config(text=message)

        def poll_progress_queue():
            should_finish = False
            while True:
                try:
                    kind, payload = progress_queue.get_nowait()
                except queue.Empty:
                    break
                if kind == "progress":
                    if progress_win.winfo_exists():
                        apply_progress(payload)
                elif kind == "done":
                    should_finish = True
            if should_finish:
                progress_var.set(100)
                percent_label.config(text="100%")
                finish()
            elif progress_win.winfo_exists():
                progress_win.after(50, poll_progress_queue)

        threading.Thread(target=worker, daemon=True).start()
        progress_win.after(50, poll_progress_queue)
        self.root.wait_window(progress_win)

    def on_esp32s3_btn_clicked(self):
        from tkinter import messagebox
        try:
            from usb_serial_bridge import ESP32S3_LABEL
        except Exception:
            ESP32S3_LABEL = "ESP32-S3 CDC"

        status = self.refresh_esp32s3_status()
        otg_only = bool(status and getattr(status, "otg_only", False))
        # OTG in Boot mode: firmware_installed=False means ROM bootloader is running → can flash directly
        otg_boot_mode = otg_only and not bool(status and getattr(status, "firmware_installed", False))

        if status and getattr(status, "bridge_ready", False):
            firmware_text = f"Installed ({getattr(status, 'firmware_version', '')})"
        elif status and getattr(status, "firmware_current", False):
            firmware_text = f"Installed ({getattr(status, 'firmware_version', '')}, waiting for USB transport)"
        elif status and getattr(status, "firmware_update_required", False):
            current = getattr(status, "firmware_version", "") or "unknown"
            expected = getattr(status, "expected_version", "") or "bundled"
            firmware_text = f"Update required ({current} -> {expected})"
        elif status and getattr(status, "board_present", False):
            firmware_text = "Detected, waiting for status"
        else:
            firmware_text = "Not installed"
        port_text = status.serial_port.port if status and status.serial_port else "CH343/COM not detected"

        dialog_w = int(480 * scaling_factor)
        dialog_h = int(290 * scaling_factor) if otg_only else int(250 * scaling_factor)
        dialog = tk.Toplevel(self.root)
        dialog.title(ESP32S3_LABEL)
        dialog.resizable(False, False)
        dialog.config(bg="#1E1E1E")
        dialog.transient(self.root)
        dialog.grab_set()
        # Center on main window
        self.root.update_idletasks()
        rx = self.root.winfo_x()
        ry = self.root.winfo_y()
        rw = self.root.winfo_width()
        rh = self.root.winfo_height()
        dx = rx + (rw - dialog_w) // 2
        dy = ry + (rh - dialog_h) // 2
        dialog.geometry(f"{dialog_w}x{dialog_h}+{dx}+{dy}")

        info_label = tk.Label(
            dialog,
            text=f"{ESP32S3_LABEL}\nFirmware: {firmware_text}\nFlashing port: {port_text}",
            fg="white", bg="#1E1E1E",
            font=scale_font(("Arial", 11, "bold")),
            justify=tk.LEFT,
        )
        info_label.pack(pady=(int(16 * scaling_factor), int(4 * scaling_factor)), padx=int(16 * scaling_factor), anchor=tk.W)

        if otg_only and not otg_boot_mode:
            # OTG with firmware running — user must manually enter Boot mode before clicking Install
            tk.Label(
                dialog,
                text="OTG port detected (firmware running). Please enter Boot mode first:\n"
                     "Hold BOOT, tap RESET once, release BOOT — then click Install.",
                fg="#FF8800", bg="#1E1E1E",
                font=scale_font(("Arial", 9, "bold")),
                justify=tk.LEFT,
                wraplength=int(440 * scaling_factor),
            ).pack(padx=int(16 * scaling_factor), anchor=tk.W)
        elif otg_boot_mode:
            # OTG in ROM bootloader — ready to flash
            tk.Label(
                dialog,
                text="OTG Boot mode detected — ready to install firmware.",
                fg="#55CC55", bg="#1E1E1E",
                font=scale_font(("Arial", 9, "bold")),
                justify=tk.LEFT,
            ).pack(padx=int(16 * scaling_factor), anchor=tk.W)

        # Progress bar (hidden until operation starts)
        progress_var = tk.DoubleVar(value=0)
        progress_bar = ttk.Progressbar(
            dialog, orient=tk.HORIZONTAL, mode="determinate", maximum=100,
            variable=progress_var, length=int(380 * scaling_factor),
        )
        percent_label = tk.Label(dialog, text="0%", fg="white", bg="#1E1E1E",
                                 font=scale_font(("Arial", 11, "bold")))

        # Result label (hidden until done)
        result_label = tk.Label(
            dialog, text="", fg="lightgreen", bg="#1E1E1E",
            font=scale_font(("Arial", 10, "bold")),
            wraplength=int(440 * scaling_factor), justify=tk.CENTER,
        )

        # Phase 1: action selection buttons
        sel_frame = tk.Frame(dialog, bg="#1E1E1E")
        sel_frame.pack(pady=int(8 * scaling_factor))

        def close_dialog():
            dialog.grab_release()
            dialog.destroy()

        def choose(action):
            if action == "delete":
                if not messagebox.askyesno(ESP32S3_LABEL, "Erase ESP32-S3 N16R8 firmware?", parent=dialog):
                    return

            # OTG port with firmware running — cannot flash until Boot mode is entered.
            # Show guidance in large red text; do NOT attempt to run esptool.
            if otg_only and not otg_boot_mode and action in ("install", "repair"):
                sel_frame.pack_forget()
                tk.Label(
                    dialog,
                    text=(
                        "ESP32 is not in Boot mode — firmware cannot be installed.\n\n"
                        "To enter Boot mode:\n"
                        "  1. Hold the BOOT button\n"
                        "  2. Tap RESET once, then release BOOT\n"
                        "  3. Wait for Status to show \"Boot\", then click Install"
                    ),
                    fg="#FF3333", bg="#1E1E1E",
                    font=scale_font(("Arial", 12, "bold")),
                    justify=tk.LEFT,
                    wraplength=int(440 * scaling_factor),
                ).pack(pady=int(10 * scaling_factor), padx=int(16 * scaling_factor), anchor=tk.W)
                close_btn_frame = tk.Frame(dialog, bg=button_gray)
                close_btn_frame.pack(pady=int(6 * scaling_factor))
                tk.Button(
                    close_btn_frame, text="Close", bg=button_gray, fg=text_color,
                    bd=0, relief=tk.FLAT, font=scale_font(("Arial", 10, "bold")), width=8,
                    command=close_dialog,
                ).pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))
                dialog.protocol("WM_DELETE_WINDOW", close_dialog)
                return

            # Transition: hide buttons, show progress
            sel_frame.pack_forget()
            progress_bar.pack(padx=int(24 * scaling_factor), fill=tk.X)
            percent_label.pack(pady=(int(8 * scaling_factor), 0))
            dialog.protocol("WM_DELETE_WINDOW", lambda: None)

            def on_flash_done(ok, message):
                progress_bar.pack_forget()
                percent_label.pack_forget()
                is_boot_guidance = not ok and message.startswith("Could not enter flashing mode")
                result_label.config(
                    text=message,
                    fg="lightgreen" if ok else "#FF3333",
                    font=scale_font(("Arial", 12, "bold")) if is_boot_guidance else scale_font(("Arial", 10, "bold")),
                )
                result_label.pack(pady=int(8 * scaling_factor), padx=int(16 * scaling_factor))
                close_btn_frame = tk.Frame(dialog, bg=button_gray)
                close_btn_frame.pack(pady=int(6 * scaling_factor))
                tk.Button(
                    close_btn_frame, text="Close", bg=button_gray, fg=text_color,
                    bd=0, relief=tk.FLAT, font=scale_font(("Arial", 10, "bold")), width=8,
                    command=close_dialog,
                ).pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))
                dialog.protocol("WM_DELETE_WINDOW", close_dialog)

            self._run_flash_in_dialog(action, status, dialog, info_label, progress_var, percent_label, on_flash_done)

        for text, action in (("Install", "install"), ("Repair", "repair"), ("Delete", "delete")):
            frame = tk.Frame(sel_frame, bg=button_gray)
            frame.pack(side=tk.LEFT, padx=int(6 * scaling_factor))
            tk.Button(
                frame, text=text, bg=button_gray, fg=text_color, bd=0, relief=tk.FLAT,
                font=scale_font(("Arial", 10, "bold")), width=8,
                command=lambda a=action: choose(a),
            ).pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))

        cancel_frame = tk.Frame(sel_frame, bg=button_gray)
        cancel_frame.pack(side=tk.LEFT, padx=int(6 * scaling_factor))
        tk.Button(
            cancel_frame, text="Cancel", bg=button_gray, fg=text_color, bd=0, relief=tk.FLAT,
            font=scale_font(("Arial", 10, "bold")), width=8, command=close_dialog,
        ).pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))

    def _show_boot_mode_prompt(self, label="ESP32-S3 CDC"):
        dialog_w = int(480 * scaling_factor)
        dialog_h = int(220 * scaling_factor)
        dialog = tk.Toplevel(self.root)
        dialog.title(label)
        dialog.resizable(False, False)
        dialog.config(bg="#1E1E1E")
        dialog.transient(self.root)
        dialog.grab_set()
        self.root.update_idletasks()
        rx = self.root.winfo_x()
        ry = self.root.winfo_y()
        rw = self.root.winfo_width()
        rh = self.root.winfo_height()
        dx = rx + (rw - dialog_w) // 2
        dy = ry + (rh - dialog_h) // 2
        dialog.geometry(f"{dialog_w}x{dialog_h}+{dx}+{dy}")

        tk.Label(
            dialog,
            text=(
                "ESP32-S3 is connected via OTG / native USB.\n\n"
                "Firmware cannot be flashed automatically on this port.\n\n"
                "To install firmware, please:\n"
                "  1. Hold the BOOT button on the ESP32-S3 board\n"
                "  2. Tap the RESET button once, then release BOOT\n"
                "  3. Connect the CH343P / UART Type-C port and retry."
            ),
            fg="white", bg="#1E1E1E",
            font=scale_font(("Arial", 10, "bold")),
            justify=tk.LEFT,
            wraplength=int(440 * scaling_factor),
        ).pack(pady=int(16 * scaling_factor), padx=int(16 * scaling_factor), anchor=tk.W)

        close_frame = tk.Frame(dialog, bg=button_gray)
        close_frame.pack(pady=int(6 * scaling_factor))
        tk.Button(
            close_frame, text="OK", bg=button_gray, fg=text_color, bd=0, relief=tk.FLAT,
            font=scale_font(("Arial", 10, "bold")), width=8,
            command=lambda: (dialog.grab_release(), dialog.destroy()),
        ).pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))

    def _run_flash_in_dialog(self, action, status, dialog, info_label, progress_var, percent_label, on_done):
        try:
            from usb_serial_bridge import ESP32S3_LABEL, flash_firmware
        except Exception:
            ESP32S3_LABEL = "ESP32-S3 CDC"
            flash_firmware = None

        self._esp32s3_firmware_busy = True
        discoverer_was_running = False
        if hasattr(self, 'discoverer_thread') and self.discoverer_thread and self.discoverer_thread.is_alive():
            discoverer_was_running = True
            self.stop_discoverer_thread()

        from discoverer import emergency_cleanup
        emergency_cleanup()

        try:
            from usb_serial_bridge import close_all_clients
            close_all_clients()
        except Exception:
            pass

        if not status or not status.serial_port:
            self._esp32s3_firmware_busy = False
            if discoverer_was_running:
                self.start_discoverer_thread()
            on_done(False, "Could not find the ESP32-S3 N16R8 CH343/COM flashing port.\nConnect the flashing port and try again.")
            return

        verb_map = {"install": "Installing", "repair": "Repairing", "delete": "Deleting"}
        if dialog.winfo_exists():
            info_label.config(
                text=f"{ESP32S3_LABEL}: {verb_map.get(action, 'Working')} firmware on {status.serial_port.port}..."
            )

        done = {"ok": False, "error": None}
        progress_queue_obj = queue.Queue()

        def progress(payload):
            progress_queue_obj.put(("progress", payload))

        def worker():
            try:
                flash_firmware(status.serial_port.port, mode=action, progress=progress)
                done["ok"] = True
            except Exception as e:
                done["error"] = e
                logger.exception("ESP32-S3 firmware task failed")
            finally:
                progress_queue_obj.put(("done", None))

        def apply_progress(payload):
            current = float(progress_var.get())
            percent = None
            if isinstance(payload, dict):
                if "percent" in payload:
                    percent = float(payload["percent"])
                elif "write_percent" in payload:
                    percent = 25.0 + (max(0.0, min(100.0, float(payload["write_percent"]))) * 0.70)
            else:
                text = str(payload)
                m = re.search(r"\((\d{1,3})\s*%\)", text)
                if m:
                    percent = 25.0 + (max(0.0, min(100.0, float(m.group(1)))) * 0.70)
            if percent is None:
                percent = min(95.0, current + 1.0)
            percent = max(current, min(100.0, percent))
            progress_var.set(percent)
            if dialog.winfo_exists():
                percent_label.config(text=f"{int(percent)}%")

        def finish():
            def resume_discovery():
                self._esp32s3_firmware_busy = False
                if discoverer_was_running:
                    self.start_discoverer_thread()

            if done["ok"]:
                try:
                    from usb_serial_bridge import close_all_clients
                    close_all_clients()
                except Exception:
                    pass
                progress_var.set(100)
                if dialog.winfo_exists():
                    percent_label.config(text="100%")
                if action == "delete":
                    on_done(True, "ESP32-S3 N16R8 firmware uninstalled successfully.")
                    self.refresh_esp32s3_status()
                    resume_discovery()
                else:
                    on_done(
                        True,
                        "ESP32-S3 N16R8 firmware installed successfully.\n\n"
                        "Please replug the ESP32-S3 USB cable (unplug then reinsert) "
                        "to complete initialization and avoid port conflicts.",
                    )
                    self.root.after(1500, lambda: self.wait_for_current_esp32s3_then(resume_discovery))
            else:
                self._esp32s3_firmware_busy = False
                error_str = str(done["error"])
                if "Could not put ESP32-S3" in error_str and "into flashing mode" in error_str:
                    on_done(False, (
                        "Could not enter flashing mode.\n\n"
                        "To enter Boot mode manually:\n"
                        "  1. Hold the BOOT button\n"
                        "  2. Tap RESET once, then release BOOT\n"
                        "  3. Click Repair to retry"
                    ))
                else:
                    on_done(False, f"ESP32-S3 N16R8 firmware operation failed:\n{done['error']}")
                self.refresh_esp32s3_status()
                if discoverer_was_running:
                    self.start_discoverer_thread()

        def poll():
            should_finish = False
            while True:
                try:
                    kind, payload = progress_queue_obj.get_nowait()
                except queue.Empty:
                    break
                if kind == "progress":
                    if dialog.winfo_exists():
                        apply_progress(payload)
                elif kind == "done":
                    should_finish = True
            if should_finish:
                progress_var.set(100)
                if dialog.winfo_exists():
                    percent_label.config(text="100%")
                finish()
            elif dialog.winfo_exists():
                dialog.after(50, poll)

        threading.Thread(target=worker, daemon=True).start()
        dialog.after(50, poll)

    def check_vigembus_installation(self, save=True):
        installed = is_vigembus_installed()
        if not installed:
            CONFIG.vigembus_installed = False
            if save:
                CONFIG.save_config()
            
            import webbrowser
            
            answer = self.ask_centered_yes_no(
                "Install ViGEmBus Driver",
                "ViGEmBus driver is not installed on your system.\n\nDo you want to open the download page to install it?\n(https://github.com/nefarius/ViGEmBus/releases)"
            )
            
            if answer:
                webbrowser.open("https://github.com/nefarius/ViGEmBus/releases")
            return False

        # If installed, test if the driver is actually functioning (accessible to Python via VBus connection)
        try:
            from virtual_controller import get_vigem
            test_vigem = get_vigem()
            # Test instantiating the bus (will throw Exception if service is not started/active)
            bus = test_vigem.win.virtual_gamepad.VBus()
            del bus
            CONFIG.vigembus_installed = True
            if save:
                CONFIG.save_config()
            return True
        except Exception as e:
            self.show_centered_message(
                "ViGEmBus Connection Error",
                f"ViGEmBus driver was detected in the system registry, but it failed to initialize ({e}).\n\n"
                "Please restart your computer to apply the installation, or reinstall the driver if the issue persists."
            )
            CONFIG.driver_type = "WinUHid"
            CONFIG.simulation_mode = CONFIG.winuhid_sim_mode
            CONFIG.vigembus_installed = False
            if save:
                CONFIG.save_config()
            if hasattr(self, 'driver_switch'):
                self.driver_switch.set_value("WinUHid")
            self.update_driver_button()
            return False

    def check_driver_installation(self, save=True):
        # If driver type is USBIP, check USBIP driver instead
        if getattr(CONFIG, "driver_type", "") == "USBIP":
            usbip_exe = "C:\\Program Files\\USBip\\usbip.exe"
            if not os.path.exists(usbip_exe):
                answer = self.ask_centered_yes_no(
                    "Install USBIP Driver",
                    "Switch emulation is selected, but the USBIP driver is not installed.\n\n"
                    "Do you want to install it now?\n(Requires administrator privileges and will temporarily reset USB connections.)"
                )
                if answer:
                    self.run_usbip_install(show_success_msg=False)
            return

        driver_type = getattr(CONFIG, "driver_type", "WinUHid")
        if driver_type == "ViGEmBus":
            self.check_vigembus_installation(save=save)
            return

        # 憒?yaml鋆⊥?撌脣?鋆?蝝????app???炎?交?行?摰?嚗璇辣??app
        if is_driver_installed():
            # 憒?瑼Ｘ蝯??臬歇摰?嚗???yaml鋆?
            CONFIG.driver_installed = True
            if save:
                CONFIG.save_config()
            return

        if getattr(CONFIG, 'driver_installed', False):
            CONFIG.driver_installed = False
            if save:
                CONFIG.save_config()
            self.update_driver_button()
            
        answer = self.ask_centered_yes_no(
            "Install Virtual Controller Driver",
            "WinUHid driver is not installed on your system.\n\nDo you want to install it now?\n(Requires administrator privileges.)"
        )
        
        if answer:
            self.run_driver_install(show_success_msg=False)

    def run_driver_install(self, show_success_msg=True):
        import sys
        import os
        from tkinter import messagebox
        
        # Stop discoverer before installation
        discoverer_was_running = False
        if hasattr(self, 'discoverer_thread') and self.discoverer_thread and self.discoverer_thread.is_alive():
            discoverer_was_running = True
            self.stop_discoverer_thread()
            
        # Run emergency cleanup to close all virtual controller handles immediately
        from discoverer import emergency_cleanup
        emergency_cleanup()

        install_ps1 = get_driver_path("install_driver.ps1")
        if os.path.exists(install_ps1):
            try:
                progress_win = tk.Toplevel(self.root)
                progress_win.title("Driver Installation")
                progress_w = int(450 * scaling_factor)
                progress_h = int(130 * scaling_factor)
                progress_win.resizable(False, False)
                progress_win.config(bg="#1E1E1E")
                progress_win.transient(self.root)
                progress_win.grab_set()
                self.center_window_on_root(progress_win, progress_w, progress_h)
                
                label = tk.Label(
                    progress_win,
                    text="Installing WinUHid Driver...\nPlease authorize the UAC prompt if asked.",
                    fg="white", bg="#1E1E1E",
                    font=scale_font(("Arial", 11, "bold"))
                )
                label.pack(pady=int(40 * scaling_factor))
                
                # Bypassing CMD and launching powershell directly via ShellExecuteExW (runas verb)
                info = SHELLEXECUTEINFOW()
                info.cbSize = ctypes.sizeof(info)
                info.fMask = SEE_MASK_NOCLOSEPROCESS
                info.hwnd = self.get_root_hwnd()
                info.lpVerb = "runas"
                info.lpFile = "powershell.exe"
                info.lpParameters = f'-NoProfile -ExecutionPolicy Bypass -File "{install_ps1}"'
                info.lpDirectory = None
                info.nShow = 1  # SW_SHOWNORMAL
                
                launched = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info))
                if not launched:
                    # User cancelled the UAC prompt or it failed
                    progress_win.grab_release()
                    progress_win.destroy()
                    self.show_centered_message("Error", "Driver installation was cancelled or failed to start (UAC prompt declined).")
                    if discoverer_was_running:
                        self.start_discoverer_thread()
                    return

                hProcess = info.hProcess
                proc_exit_code = [0]

                def check_process():
                    if hProcess:
                        res = ctypes.windll.kernel32.WaitForSingleObject(hProcess, 0)
                        if res == WAIT_TIMEOUT:
                            progress_win.after(200, check_process)
                        else:
                            exit_code = wintypes.DWORD()
                            ctypes.windll.kernel32.GetExitCodeProcess(hProcess, ctypes.byref(exit_code))
                            ctypes.windll.kernel32.CloseHandle(hProcess)
                            proc_exit_code[0] = exit_code.value
                            progress_win.grab_release()
                            progress_win.destroy()
                    else:
                        progress_win.grab_release()
                        progress_win.destroy()
                            
                progress_win.after(200, check_process)
                self.root.wait_window(progress_win)
                
                logger.info(f"Driver installer process exited with code: {proc_exit_code[0]}")
                
                driver_installed_ok = is_driver_installed()
                if driver_installed_ok:
                    CONFIG.driver_installed = True
                    CONFIG.save_config()
                    if show_success_msg:
                        self.show_centered_message("Success", "WinUHid driver installed successfully.")
                else:
                    self.show_centered_message(
                        "Error",
                        "Driver installation was not completed or failed.\nSome emulator functions may not work."
                    )
                self.update_driver_button()
            except Exception as e:
                self.show_centered_message("Error", f"Failed to start the installer: {e}")
        else:
            self.show_centered_message("Error", "Could not find install_driver.ps1. Please verify the integrity of the application files.")

        if discoverer_was_running:
            self.start_discoverer_thread()

    def run_driver_uninstall(self):
        import sys
        import os
        from tkinter import messagebox
        
        # Stop discoverer before uninstallation
        discoverer_was_running = False
        if hasattr(self, 'discoverer_thread') and self.discoverer_thread and self.discoverer_thread.is_alive():
            discoverer_was_running = True
            self.stop_discoverer_thread()
            
        # Run emergency cleanup to close all virtual controller handles immediately
        from discoverer import emergency_cleanup
        emergency_cleanup()

        uninstall_ps1 = get_driver_path("uninstall_driver.ps1")
        if os.path.exists(uninstall_ps1):
            try:
                progress_win = tk.Toplevel(self.root)
                progress_win.title("Driver Uninstallation")
                progress_w = int(450 * scaling_factor)
                progress_h = int(130 * scaling_factor)
                progress_win.resizable(False, False)
                progress_win.config(bg="#1E1E1E")
                progress_win.transient(self.root)
                progress_win.grab_set()
                self.center_window_on_root(progress_win, progress_w, progress_h)
                
                label = tk.Label(
                    progress_win,
                    text="Uninstalling WinUHid Driver...\nPlease authorize the UAC prompt if asked.",
                    fg="white", bg="#1E1E1E",
                    font=scale_font(("Arial", 11, "bold"))
                )
                label.pack(pady=int(40 * scaling_factor))
                
                # Bypassing CMD and launching powershell directly via ShellExecuteExW (runas verb)
                info = SHELLEXECUTEINFOW()
                info.cbSize = ctypes.sizeof(info)
                info.fMask = SEE_MASK_NOCLOSEPROCESS
                info.hwnd = self.get_root_hwnd()
                info.lpVerb = "runas"
                info.lpFile = "powershell.exe"
                info.lpParameters = f'-NoProfile -ExecutionPolicy Bypass -File "{uninstall_ps1}"'
                info.lpDirectory = None
                info.nShow = 1  # SW_SHOWNORMAL
                
                launched = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info))
                if not launched:
                    # User cancelled the UAC prompt or it failed
                    progress_win.grab_release()
                    progress_win.destroy()
                    self.show_centered_message("Error", "Driver uninstallation was cancelled or failed to start (UAC prompt declined).")
                    if discoverer_was_running:
                        self.start_discoverer_thread()
                    return

                hProcess = info.hProcess
                proc_exit_code = [0]

                def check_process():
                    if hProcess:
                        res = ctypes.windll.kernel32.WaitForSingleObject(hProcess, 0)
                        if res == WAIT_TIMEOUT:
                            progress_win.after(200, check_process)
                        else:
                            exit_code = wintypes.DWORD()
                            ctypes.windll.kernel32.GetExitCodeProcess(hProcess, ctypes.byref(exit_code))
                            ctypes.windll.kernel32.CloseHandle(hProcess)
                            proc_exit_code[0] = exit_code.value
                            progress_win.grab_release()
                            progress_win.destroy()
                    else:
                        progress_win.grab_release()
                        progress_win.destroy()
                            
                progress_win.after(200, check_process)
                self.root.wait_window(progress_win)
                
                logger.info(f"Driver uninstaller process exited with code: {proc_exit_code[0]}")
                
                # Now that progress_win is destroyed, check if it was removed
                driver_removed_ok = not is_driver_installed()
                if driver_removed_ok:
                    CONFIG.driver_installed = False
                    CONFIG.save_config()
                    self.show_centered_message("Success", "WinUHid driver uninstalled successfully.")
                else:
                    self.show_centered_message(
                        "Error",
                        "Driver uninstallation failed or was cancelled."
                    )
                self.update_driver_button()
            except Exception as e:
                self.show_centered_message("Error", f"Failed to start the uninstaller: {e}")
        else:
            self.show_centered_message("Error", "Could not find uninstall_driver.ps1. Please verify the integrity of the application files.")

        if discoverer_was_running:
            self.start_discoverer_thread()

    def stop_discoverer_thread(self):
        if hasattr(self, 'discoverer_thread') and self.discoverer_thread and self.discoverer_thread.is_alive():
            logger.info("Stopping discoverer thread...")
            self.quit_event.set()
            self.discoverer_thread.join(timeout=5.0)
            self.discoverer_thread = None

    def start_discoverer_thread(self):
        self.stop_discoverer_thread()
        self.quit_event.clear()
        
        def run():
            from discoverer import start_discoverer
            start_discoverer(self.discoverer_callback, self.quit_event)
            
        logger.info("Starting discoverer thread...")
        self.discoverer_thread = threading.Thread(target=run, daemon=True)
        self.discoverer_thread.start()

    def run_vigembus_uninstall(self):
        import sys
        import os
        from tkinter import messagebox
        
        # Stop discoverer before uninstallation
        discoverer_was_running = False
        if hasattr(self, 'discoverer_thread') and self.discoverer_thread and self.discoverer_thread.is_alive():
            discoverer_was_running = True
            self.stop_discoverer_thread()
            
        # Run emergency cleanup to close all virtual controller handles immediately
        from discoverer import emergency_cleanup
        emergency_cleanup()

        uninstall_ps1 = get_driver_path("uninstall_vigembus.ps1")
        if os.path.exists(uninstall_ps1):
            try:
                progress_win = tk.Toplevel(self.root)
                progress_win.title("ViGEmBus Uninstallation")
                progress_w = int(450 * scaling_factor)
                progress_h = int(130 * scaling_factor)
                progress_win.resizable(False, False)
                progress_win.config(bg="#1E1E1E")
                progress_win.transient(self.root)
                progress_win.grab_set()
                self.center_window_on_root(progress_win, progress_w, progress_h)
                
                label = tk.Label(
                    progress_win,
                    text="Uninstalling ViGEmBus Driver...\nPlease authorize the UAC prompt if asked.",
                    fg="white", bg="#1E1E1E",
                    font=scale_font(("Arial", 11, "bold"))
                )
                label.pack(pady=int(40 * scaling_factor))
                
                # Bypassing CMD and launching powershell directly via ShellExecuteExW (runas verb)
                info = SHELLEXECUTEINFOW()
                info.cbSize = ctypes.sizeof(info)
                info.fMask = SEE_MASK_NOCLOSEPROCESS
                info.hwnd = self.get_root_hwnd()
                info.lpVerb = "runas"
                info.lpFile = "powershell.exe"
                info.lpParameters = f'-NoProfile -ExecutionPolicy Bypass -File "{uninstall_ps1}"'
                info.lpDirectory = None
                info.nShow = 1  # SW_SHOWNORMAL
                
                launched = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info))
                if not launched:
                    # User cancelled the UAC prompt or it failed
                    progress_win.grab_release()
                    progress_win.destroy()
                    self.show_centered_message("Error", "ViGEmBus uninstallation was cancelled or failed to start (UAC prompt declined).")
                    if discoverer_was_running:
                        self.start_discoverer_thread()
                    return

                hProcess = info.hProcess
                proc_exit_code = [0]

                def check_process():
                    if hProcess:
                        res = ctypes.windll.kernel32.WaitForSingleObject(hProcess, 0)
                        if res == WAIT_TIMEOUT:
                            progress_win.after(200, check_process)
                        else:
                            exit_code = wintypes.DWORD()
                            ctypes.windll.kernel32.GetExitCodeProcess(hProcess, ctypes.byref(exit_code))
                            ctypes.windll.kernel32.CloseHandle(hProcess)
                            proc_exit_code[0] = exit_code.value
                            progress_win.grab_release()
                            progress_win.destroy()
                    else:
                        progress_win.grab_release()
                        progress_win.destroy()
                            
                progress_win.after(200, check_process)
                self.root.wait_window(progress_win)
                
                logger.info(f"ViGEmBus uninstaller process exited with code: {proc_exit_code[0]}")
                
                driver_removed_ok = (proc_exit_code[0] == 0)
                if driver_removed_ok:
                    CONFIG.vigembus_installed = False
                    CONFIG.save_config()
                    self.show_centered_message("Success", "ViGEmBus driver uninstalled successfully. A system reboot is highly recommended.")
                else:
                    self.show_centered_message(
                        "Error",
                        "ViGEmBus uninstallation failed or was cancelled."
                    )
                self.update_driver_button()
            except Exception as e:
                self.show_centered_message("Error", f"Failed to start the uninstaller: {e}")
        else:
            self.show_centered_message("Error", "Could not find uninstall_vigembus.ps1. Please verify the integrity of the application files.")

        if discoverer_was_running:
            self.start_discoverer_thread()

    def on_driver_btn_clicked(self):
        driver_type = getattr(CONFIG, "driver_type", "WinUHid")
        if driver_type == "ViGEmBus":
            if getattr(CONFIG, 'vigembus_installed', False):
                if self.ask_centered_yes_no("Uninstall Driver", "Are you sure you want to uninstall the ViGEmBus driver?\n(Requires administrator privileges.)"):
                    self.run_vigembus_uninstall()
            else:
                import webbrowser
                webbrowser.open("https://github.com/nefarius/ViGEmBus/releases")
        else:
            if getattr(CONFIG, 'driver_installed', False):
                if self.ask_centered_yes_no("Uninstall Driver", "Are you sure you want to uninstall the WinUHid driver?\n(Requires administrator privileges.)"):
                    self.run_driver_uninstall()
            else:
                self.run_driver_install()

    def update_driver_buttons_visibility(self):
        if not hasattr(self, 'top_btn_frame'):
            return
        scaling_factor = getattr(self, 'scaling_factor', 1.0)
        
        # First, unpack all frames from top_btn_frame to preserve order
        if hasattr(self, 'driver_frame'): self.driver_frame.pack_forget()
        if hasattr(self, 'esp32s3_frame'): self.esp32s3_frame.pack_forget()
        if hasattr(self, 'usbip_frame'): self.usbip_frame.pack_forget()
        if hasattr(self, 'startup_frame'): self.startup_frame.pack_forget()
        if hasattr(self, 'min_frame'): self.min_frame.pack_forget()
        if hasattr(self, 'hide_frame'): self.hide_frame.pack_forget()
        
        driver_type = getattr(CONFIG, "driver_type", "WinUHid")
        
        # Pack the active driver button
        if driver_type == "USBIP":
            if hasattr(self, 'usbip_frame'):
                self.usbip_frame.pack(side=tk.LEFT, padx=int(5 * scaling_factor))
        else:
            if hasattr(self, 'driver_frame'):
                self.driver_frame.pack(side=tk.LEFT, padx=int(5 * scaling_factor))

        if getattr(self, 'esp32s3_detected', False) and hasattr(self, 'esp32s3_frame'):
            esp_status = getattr(self, 'esp32s3_bridge_status', None)
            update_needed = bool(esp_status and getattr(esp_status, 'firmware_update_required', False))
            not_installed = bool(esp_status and not getattr(esp_status, 'firmware_installed', True))
            otg_only = bool(esp_status and getattr(esp_status, 'otg_only', False))
            # Orange "Install ESP32-S3 Driver" when OTG connected with wrong/missing firmware
            if otg_only and (update_needed or not_installed):
                btn_color = "#CC5500"
                btn_text = "Install ESP32-S3 Driver"
            elif update_needed:
                btn_color = "#CC5500"
                btn_text = "ESP32-S3: Update Available"
            else:
                btn_color = button_gray
                btn_text = "ESP32-S3 N16R8 Driver"
            if hasattr(self, 'esp32s3_frame'):
                self.esp32s3_frame.config(bg=btn_color)
            if hasattr(self, 'esp32s3_btn'):
                self.esp32s3_btn.config(text=btn_text, bg=btn_color)
            self.esp32s3_frame.pack(side=tk.LEFT, padx=int(5 * scaling_factor))

        # Pack the rest of the buttons
        if hasattr(self, 'startup_frame'): self.startup_frame.pack(side=tk.LEFT, padx=int(5 * scaling_factor))
        if hasattr(self, 'min_frame'): self.min_frame.pack(side=tk.LEFT, padx=int(5 * scaling_factor))
        if hasattr(self, 'hide_frame'): self.hide_frame.pack(side=tk.LEFT, padx=int(5 * scaling_factor))

        self.update_header_status()

    def update_header_status(self):
        if not hasattr(self, 'header_label'):
            return
        status = getattr(self, 'esp32s3_bridge_status', None)
        esp32_detected = getattr(self, 'esp32s3_detected', False)

        conn_method = "ESP32-S3" if esp32_detected else "System BLE"

        if status and esp32_detected:
            otg_only = getattr(status, 'otg_only', False)
            fw_installed = getattr(status, 'firmware_installed', False)
            was_ready = getattr(self, '_esp32_header_was_ready', False)
            if getattr(status, 'bridge_ready', False):
                try:
                    import usb_serial_bridge as _usb_sb
                    scan_active = _usb_sb.BRIDGE_SCAN_ACTIVE
                except Exception:
                    scan_active = True
                if scan_active:
                    conn_status = "Ready"
                    status_color = "#55CC55"
                else:
                    conn_status = "Initializing"
                    status_color = "#888888"
            elif otg_only and not fw_installed:
                if was_ready:
                    # Firmware was running but just stopped responding → brief disconnect transition
                    conn_status = "Disconnect"
                    status_color = "#888888"
                else:
                    # OTG in ROM bootloader (no version reported) — ready for manual flash
                    conn_status = "Boot"
                    status_color = "#FF8800"
            elif getattr(status, 'firmware_update_required', False):
                conn_status = "Error"
                status_color = "#FF4444"
            elif getattr(status, 'board_present', False):
                conn_status = "Initializing"
                status_color = "#888888"
            else:
                conn_status = "Initializing"
                status_color = "#888888"
        else:
            try:
                from discoverer import is_system_bluetooth_available
                bt_ok = is_system_bluetooth_available()
            except Exception:
                bt_ok = True
            if bt_ok:
                conn_status = "Ready"
                status_color = "#55CC55"
            else:
                conn_status = "Disconnect"
                status_color = "#888888"

        self.header_label.config(
            text=f"Connecting Via: {conn_method}  |  Status: {conn_status}",
            fg=status_color,
        )
        # Track whether we were in bridge_ready for next poll's disconnect detection
        self._esp32_header_was_ready = (conn_status == "Ready" and esp32_detected)
        self._esp32_last_rendered_status = conn_status

        # When transitioning to Disconnect, start fast-polling so Boot mode is detected
        # within ~500ms instead of waiting up to 5s for the regular timer.
        if conn_status == "Disconnect" and not getattr(self, '_esp32_fast_poll_active', False):
            self._esp32_fast_poll_active = True
            self._esp32_fast_poll_count = 0
            self.root.after(500, self._esp32_fast_poll)

    def _esp32_fast_poll(self):
        """Poll at 500ms intervals after a Disconnect event until status stabilises."""
        if getattr(self, 'is_quitting', False):
            self._esp32_fast_poll_active = False
            return

        self.refresh_esp32s3_status_async()

        count = getattr(self, '_esp32_fast_poll_count', 0) + 1
        self._esp32_fast_poll_count = count

        # Stop if the last rendered status is no longer "Disconnect", or after 30 polls (15s).
        last_status = getattr(self, '_esp32_last_rendered_status', 'Disconnect')
        if last_status != 'Disconnect' or count >= 30:
            self._esp32_fast_poll_active = False
            self._esp32_fast_poll_count = 0
        else:
            self.root.after(500, self._esp32_fast_poll)

    def update_driver_button(self):
        if not hasattr(self, 'driver_btn') or not self.driver_btn:
            return
        driver_type = getattr(CONFIG, "driver_type", "WinUHid")
        if driver_type == "ViGEmBus":
            installed = getattr(CONFIG, 'vigembus_installed', False)
            text = "Uninstall ViGEmBus Driver" if installed else "Download ViGEmBus Driver"
        else:
            installed = getattr(CONFIG, 'driver_installed', False)
            text = "Uninstall WinUHid Driver" if installed else "Install WinUHid Driver"
        self.driver_btn.config(text=text)
        self.update_driver_buttons_visibility()

    def update_usbip_button(self):
        if not hasattr(self, 'usbip_btn') or not self.usbip_btn:
            return
        usbip_exe = "C:\\Program Files\\USBip\\usbip.exe"
        text = "Uninstall USBIP Driver" if os.path.exists(usbip_exe) else "Install USBIP Driver"
        self.usbip_btn.config(text=text)
        self.update_driver_buttons_visibility()

    def on_usbip_btn_clicked(self):
        usbip_exe = "C:\\Program Files\\USBip\\usbip.exe"
        if os.path.exists(usbip_exe):
            if self.ask_centered_yes_no("Uninstall USBIP Driver", "Are you sure you want to uninstall the USBIP driver?\n(Requires administrator privileges.)"):
                self.run_usbip_uninstall()
        else:
            if self.ask_centered_yes_no(
                "Install USBIP Driver",
                "Are you sure you want to install the USBIP driver?\n\n"
                "WARNING: During the installation of USBIP-win2, Windows USB hubs will restart briefly, which will temporarily disconnect other USB peripherals (mice, keyboards, etc.).\n\n"
                "Do you want to proceed?\n(Requires administrator privileges.)"
            ):
                self.run_usbip_install()

    def run_usbip_install(self, show_success_msg=True):
        import sys
        import os
        from tkinter import messagebox
        
        # Stop discoverer before installation
        discoverer_was_running = False
        if hasattr(self, 'discoverer_thread') and self.discoverer_thread and self.discoverer_thread.is_alive():
            discoverer_was_running = True
            self.stop_discoverer_thread()
            
        # Run emergency cleanup to close all virtual controller handles immediately
        from discoverer import emergency_cleanup
        emergency_cleanup()

        install_ps1 = get_driver_path("install_usbip.ps1")
        if os.path.exists(install_ps1):
            try:
                progress_win = tk.Toplevel(self.root)
                progress_win.title("USBIP Driver Installation")
                progress_w = int(450 * scaling_factor)
                progress_h = int(130 * scaling_factor)
                progress_win.resizable(False, False)
                progress_win.config(bg="#1E1E1E")
                progress_win.transient(self.root)
                progress_win.grab_set()
                self.center_window_on_root(progress_win, progress_w, progress_h)
                
                label = tk.Label(
                    progress_win,
                    text="Installing USBIP-win2 Driver...\nPlease authorize the UAC prompt if asked.",
                    fg="white", bg="#1E1E1E",
                    font=scale_font(("Arial", 11, "bold"))
                )
                label.pack(pady=int(40 * scaling_factor))
                
                # Bypassing CMD and launching powershell directly via ShellExecuteExW (runas verb)
                info = SHELLEXECUTEINFOW()
                info.cbSize = ctypes.sizeof(info)
                info.fMask = SEE_MASK_NOCLOSEPROCESS
                info.hwnd = self.get_root_hwnd()
                info.lpVerb = "runas"
                info.lpFile = "powershell.exe"
                info.lpParameters = f'-NoProfile -ExecutionPolicy Bypass -File "{install_ps1}"'
                info.lpDirectory = None
                info.nShow = 1  # SW_SHOWNORMAL
                
                launched = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info))
                if not launched:
                    # User cancelled the UAC prompt or it failed
                    progress_win.grab_release()
                    progress_win.destroy()
                    self.show_centered_message("Error", "USBIP driver installation was cancelled or failed to start (UAC prompt declined).")
                    if discoverer_was_running:
                        self.start_discoverer_thread()
                    return
                
                hProcess = info.hProcess
                proc_exit_code = [0]

                def check_process():
                    if hProcess:
                        res = ctypes.windll.kernel32.WaitForSingleObject(hProcess, 0)
                        if res == WAIT_TIMEOUT:
                            progress_win.after(200, check_process)
                        else:
                            exit_code = wintypes.DWORD()
                            ctypes.windll.kernel32.GetExitCodeProcess(hProcess, ctypes.byref(exit_code))
                            ctypes.windll.kernel32.CloseHandle(hProcess)
                            proc_exit_code[0] = exit_code.value
                            progress_win.grab_release()
                            progress_win.destroy()
                    else:
                        progress_win.grab_release()
                        progress_win.destroy()
                            
                progress_win.after(200, check_process)
                self.root.wait_window(progress_win)
                
                logger.info(f"USBIP driver installer process exited with code: {proc_exit_code[0]}")
                
                usbip_exe = "C:\\Program Files\\USBip\\usbip.exe"
                usbip_installed_ok = os.path.exists(usbip_exe)
                if usbip_installed_ok:
                    if show_success_msg:
                        self.show_centered_message("Success", "USBIP-win2 driver installed successfully.")
                else:
                    self.show_centered_message(
                        "Error",
                        "USBIP driver installation was not completed or failed."
                    )
                self.update_usbip_button()
            except Exception as e:
                self.show_centered_message("Error", f"Failed to start the USBIP installer: {e}")
        else:
            self.show_centered_message("Error", "Could not find install_usbip.ps1. Please verify the integrity of the application files.")

        if discoverer_was_running:
            self.start_discoverer_thread()

    def run_usbip_uninstall(self):
        import sys
        import os
        from tkinter import messagebox
        
        # Stop discoverer before uninstallation
        discoverer_was_running = False
        if hasattr(self, 'discoverer_thread') and self.discoverer_thread and self.discoverer_thread.is_alive():
            discoverer_was_running = True
            self.stop_discoverer_thread()
            
        # Run emergency cleanup to close all virtual controller handles immediately
        from discoverer import emergency_cleanup
        emergency_cleanup()

        uninstaller_exe = "C:\\Program Files\\USBip\\unins000.exe"
        if os.path.exists(uninstaller_exe):
            try:
                progress_win = tk.Toplevel(self.root)
                progress_win.title("USBIP Driver Uninstallation")
                progress_w = int(450 * scaling_factor)
                progress_h = int(130 * scaling_factor)
                progress_win.resizable(False, False)
                progress_win.config(bg="#1E1E1E")
                progress_win.transient(self.root)
                progress_win.grab_set()
                self.center_window_on_root(progress_win, progress_w, progress_h)
                
                label = tk.Label(
                    progress_win,
                    text="Running USBIP-win2 Uninstaller...\nPlease follow the uninstall wizard on the screen.",
                    fg="white", bg="#1E1E1E",
                    font=scale_font(("Arial", 11, "bold"))
                )
                label.pack(pady=int(40 * scaling_factor))
                
                # Bypassing CMD and launching the uninstaller directly via ShellExecuteExW (runas verb)
                info = SHELLEXECUTEINFOW()
                info.cbSize = ctypes.sizeof(info)
                info.fMask = SEE_MASK_NOCLOSEPROCESS
                info.hwnd = self.get_root_hwnd()
                info.lpVerb = "runas"
                info.lpFile = uninstaller_exe
                info.lpParameters = ""
                info.lpDirectory = "C:\\Program Files\\USBip"
                info.nShow = 1  # SW_SHOWNORMAL
                
                launched = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info))
                if not launched:
                    # User cancelled the UAC prompt or it failed
                    progress_win.grab_release()
                    progress_win.destroy()
                    self.show_centered_message("Error", "USBIP driver uninstallation was cancelled or failed to start (UAC prompt declined).")
                    if discoverer_was_running:
                        self.start_discoverer_thread()
                    return

                hProcess = info.hProcess
                proc_exit_code = [0]

                def check_process():
                    if hProcess:
                        res = ctypes.windll.kernel32.WaitForSingleObject(hProcess, 0)
                        if res == WAIT_TIMEOUT:
                            progress_win.after(200, check_process)
                        else:
                            exit_code = wintypes.DWORD()
                            ctypes.windll.kernel32.GetExitCodeProcess(hProcess, ctypes.byref(exit_code))
                            ctypes.windll.kernel32.CloseHandle(hProcess)
                            proc_exit_code[0] = exit_code.value
                            progress_win.grab_release()
                            progress_win.destroy()
                    else:
                        progress_win.grab_release()
                        progress_win.destroy()
                            
                progress_win.after(200, check_process)
                self.root.wait_window(progress_win)
                
                logger.info(f"USBIP driver uninstaller process exited with code: {proc_exit_code[0]}")
                
                usbip_exe = "C:\\Program Files\\USBip\\usbip.exe"
                usbip_removed_ok = not os.path.exists(usbip_exe)
                if usbip_removed_ok:
                    self.show_centered_message("Success", "USBIP driver uninstalled successfully.")
                else:
                    self.show_centered_message("Information", "USBIP driver uninstaller closed.")
                self.update_usbip_button()
            except Exception as e:
                self.show_centered_message("Error", f"Failed to start the USBIP uninstaller: {e}")
        else:
            self.show_centered_message("Error", f"Could not find uninstaller at {uninstaller_exe}.")

        if discoverer_was_running:
            self.start_discoverer_thread()


    def init_interface(self):
        # 1. Enable Windows High DPI Mode
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except:
                pass

        try: ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Switch 2 Controllers')
        except: pass
        self.root = tk.Tk()
        self.root.tk.call('tk', 'scaling', 1.3333333333333333)
        self.root.withdraw() # Hide while building the UI, then show from start().
        
        # 2. Re-apply global scaling factors using the actual Tk screen height.
        try:
            refresh_ui_scaling(self.root.winfo_screenheight())
        except Exception:
            refresh_ui_scaling()

        self.calibration_overlay = CalibrationOverlay(self.root)
        import utils
        utils.show_notification_callback = self.calibration_overlay.update

        def safe_ui_update():
            if getattr(self, 'discoverer_callback', None):
                self.discoverer_callback(list(VIRTUAL_CONTROLLERS))
        utils.force_ui_update_callback = safe_ui_update
        try:
            photo = tk.PhotoImage(file=get_resource('images/icon.png'))
            self.root.wm_iconphoto(False, photo)
        except: pass
        import sys
        exe_name = os.path.basename(sys.argv[0])
        if exe_name.lower().endswith('.exe'):
            self.root.title(os.path.splitext(exe_name)[0])
        else:
            self.root.title(f"Switch2 Controllers v{APP_VERSION}")
        
        # 3. Handle window geometry & minsize (remembering position)
        default_w = int(1270 * window_resolution_ratio)
        default_h = int(1250 * window_resolution_ratio)
        x = CONFIG.window_x if CONFIG.window_x is not None else 50
        y = CONFIG.window_y if CONFIG.window_y is not None else 50
        self.root.geometry(f"{default_w}x{default_h}+{x}+{y}")
        self.root.minsize(default_w, default_h)
        self.root.config(bg=background_color, padx=int(10 * scaling_factor), pady=int(10 * scaling_factor))
        self.root.bind("<Configure>", self.on_configure)
        
        # Set title bar color to match background
        try:
            self.root.update()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            color = background_color.lstrip('#')
            r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
            color_int = (b << 16) | (g << 8) | r # BGR format
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(ctypes.c_int(color_int)), 4) # Caption color
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(ctypes.c_int(0xFFFFFF)), 4)  # Title text color (White)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)         # Immersive dark mode
            
            # Get expected outer dimensions corresponding to client size
            rect = win32gui.GetWindowRect(hwnd)
            self.expected_outer_w = rect[2] - rect[0]
            self.expected_outer_h = rect[3] - rect[1]

            class POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            class MINMAXINFO(ctypes.Structure):
                _fields_ = [
                    ("ptReserved", POINT),
                    ("ptMaxSize", POINT),
                    ("ptMaxPosition", POINT),
                    ("ptMinTrackSize", POINT),
                    ("ptMaxTrackSize", POINT),
                ]

            class WINDOWPOS(ctypes.Structure):
                _fields_ = [
                    ("hwnd", ctypes.c_void_p),
                    ("hwndInsertAfter", ctypes.c_void_p),
                    ("x", ctypes.c_int),
                    ("y", ctypes.c_int),
                    ("cx", ctypes.c_int),
                    ("cy", ctypes.c_int),
                    ("flags", ctypes.c_uint),
                ]

            self._user_resizing = False

            # Subclass to ignore WM_DPICHANGED (0x02E0) and prevent auto-resizing
            def wndproc(hwnd_val, msg, wparam, lparam):
                if msg == 0x0231: # WM_ENTERSIZEMOVE
                    self._user_resizing = True
                elif msg == 0x0232: # WM_EXITSIZEMOVE
                    self._user_resizing = False
                elif msg == 0x0024: # WM_GETMINMAXINFO
                    res = win32gui.CallWindowProc(self.old_wndproc, hwnd_val, msg, wparam, lparam)
                    if getattr(self, 'expected_outer_w', None) and getattr(self, 'expected_outer_h', None):
                        mmi = MINMAXINFO.from_address(lparam)
                        mmi.ptMinTrackSize.x = self.expected_outer_w
                        mmi.ptMinTrackSize.y = self.expected_outer_h
                    return res
                elif msg == 0x0046: # WM_WINDOWPOSCHANGING
                    wp = WINDOWPOS.from_address(lparam)
                    if not (wp.flags & 0x0001): # Not SWP_NOSIZE
                        if not getattr(self, '_user_resizing', False):
                            if getattr(self, 'expected_outer_w', None) and getattr(self, 'expected_outer_h', None):
                                wp.cx = self.expected_outer_w
                                wp.cy = self.expected_outer_h
                elif msg == 0x02E0: # WM_DPICHANGED
                    return 0
                return win32gui.CallWindowProc(self.old_wndproc, hwnd_val, msg, wparam, lparam)
            self._wndproc_ref = wndproc
            self.old_wndproc = win32gui.SetWindowLong(hwnd, win32con.GWL_WNDPROC, wndproc)
            
            # Force the geometry, minsize, and scaling factor back to defaults to overwrite any initial scaling applied during update()
            self.root.tk.call('tk', 'scaling', 1.3333333333333333)
            self.root.geometry(f"{default_w}x{default_h}+{x}+{y}")
            self.root.minsize(default_w, default_h)
            self.root.update()
        except Exception as e:
            logger.debug(f"Failed to set title bar color or subclass window: {e}")

        # Dropdown (Combobox) Styling
        style = ttk.Style()
        style.theme_use('clam')
        style.configure("TCombobox", 
                        fieldbackground=button_gray, 
                        background=button_gray, 
                        foreground="white", 
                        arrowcolor="white",
                        borderwidth=0,
                        relief="flat",
                        bordercolor=button_gray,
                        darkcolor=button_gray,
                        lightcolor=button_gray,
                        font=scale_font(("Arial", 11, "bold")))
        style.map("TCombobox", 
                  fieldbackground=[('readonly', button_gray)],
                  background=[('readonly', button_gray), ('active', button_gray), ('pressed', button_gray)],
                  foreground=[('readonly', 'white')],
                  bordercolor=[('readonly', button_gray)],
                  lightcolor=[('readonly', button_gray)],
                  darkcolor=[('readonly', button_gray)])
        
        self.root.option_add("*TCombobox*Listbox.background", button_gray)
        self.root.option_add("*TCombobox*Listbox.foreground", "white")
        self.root.option_add("*TCombobox*Listbox.selectBackground", highlight_color)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "white")
        self.root.option_add("*TCombobox*Listbox.font", scale_font(("Arial", 11, "bold")))
        self.root.option_add("*TCombobox*Listbox.borderwidth", 0)
        self.root.option_add("*TCombobox*Listbox.highlightthickness", 0)
        self.root.option_add("*TCombobox*Listbox.relief", "flat")

        # Modern Scrollbar Styling for Dropdowns
        style.configure("Vertical.TScrollbar", 
                        gripcount=0,
                        background=button_gray,
                        troughcolor=background_color,
                        borderwidth=0,
                        arrowsize=0,
                        relief="flat")
        style.map("Vertical.TScrollbar",
                  background=[('pressed', highlight_color), ('active', highlight_color)],
                  troughcolor=[('pressed', background_color), ('active', background_color)])

        self.font = tkFont.Font(family="Arial", size=int(15 * scaling_factor), weight="bold")
        
        try:
            hint_img = Image.open(get_resource("images/pairing_hint.png"))
            hw, hh = hint_img.size
            hint_img = hint_img.resize((int(hw * scaling_factor), int(hh * scaling_factor)), Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.ANTIALIAS)
            self.pairing_hint_image = ImageTk.PhotoImage(hint_img)
        except Exception as e:
            logger.error(f"Failed to load/scale pairing hint image: {e}")
            self.pairing_hint_image = tk.PhotoImage(file=get_resource("images/pairing_hint.png"))

        # Header bar — connection method + status (packed at TOP before content panels)
        self.header_frame = tk.Frame(self.root, bg=background_color, height=int(24 * scaling_factor))
        self.header_frame.pack(side=tk.TOP, fill=tk.X, pady=(0, int(2 * scaling_factor)))
        self.header_frame.pack_propagate(False)
        self.header_label = tk.Label(
            self.header_frame,
            text="Connecting Via: System BLE  |  Status: Ready",
            fg="#888888",
            bg=background_color,
            font=scale_font(("Arial", 9, "bold")),
            anchor=tk.E,
        )
        self.header_label.pack(side=tk.RIGHT, padx=int(12 * scaling_factor), fill=tk.Y)

        self.main_frame = tk.Frame(self.root, bg=background_color)
        self.main_frame.pack(side=tk.TOP, pady=(10, 5), fill=tk.Y)
        self.players_info = None

        self.init_settings_panel()
        self.init_compensation_panel(parent=self.tab_content_frame)
        self.init_djg_panel(parent=self.tab_content_frame)
        self.init_gyro_settings_panel(parent=self.tab_content_frame)
        self.show_settings_tab("controller_mapping")
        self.init_auto_disconnect_panel()

        # New centralized button row above Gyro Settings
        self.top_btn_frame = tk.Frame(self.root, bg=background_color)
        self.top_btn_frame.pack(side=tk.BOTTOM, pady=(0, int(5 * scaling_factor)))

        # Driver Install/Uninstall Button
        self.driver_frame = tk.Frame(self.top_btn_frame, bg=button_gray)
        self.driver_btn = tk.Button(self.driver_frame, text="", bg=button_gray, fg=text_color, bd=0, relief=tk.FLAT, font=scale_font(("Arial", 10, "bold")), command=self.on_driver_btn_clicked)
        self.driver_btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))

        # USBIP Driver Button
        self.usbip_frame = tk.Frame(self.top_btn_frame, bg=button_gray)
        self.usbip_btn = tk.Button(self.usbip_frame, text="", bg=button_gray, fg=text_color, bd=0, relief=tk.FLAT, font=scale_font(("Arial", 10, "bold")), command=self.on_usbip_btn_clicked)
        self.usbip_btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))

        # ESP32-S3 N16R8 Firmware Button
        self.esp32s3_frame = tk.Frame(self.top_btn_frame, bg=button_gray)
        self.esp32s3_btn = tk.Button(
            self.esp32s3_frame,
            text="ESP32-S3 N16R8 Driver",
            bg=button_gray,
            fg=text_color,
            bd=0,
            relief=tk.FLAT,
            font=scale_font(("Arial", 10, "bold")),
            command=self.on_esp32s3_btn_clicked
        )
        self.esp32s3_btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))

        # Startup Button
        self.startup_frame = tk.Frame(self.top_btn_frame, bg=highlight_color if CONFIG.open_when_startup else button_gray)
        self.startup_frame.pack(side=tk.LEFT, padx=int(5 * scaling_factor))
        startup_text = f"Run At Startup: {'ON' if CONFIG.open_when_startup else 'OFF'}"
        self.startup_btn = tk.Button(self.startup_frame, text=startup_text, bg=button_gray, fg=text_color, bd=0, relief=tk.FLAT, font=scale_font(("Arial", 10, "bold")), command=lambda: self.update_startup_setting(not CONFIG.open_when_startup))
        self.startup_btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))

        # Minimized Button
        self.min_frame = tk.Frame(self.top_btn_frame, bg=highlight_color if CONFIG.start_minimized else button_gray)
        self.min_frame.pack(side=tk.LEFT, padx=int(5 * scaling_factor))
        minimized_text = f"Start Minimized: {'ON' if CONFIG.start_minimized else 'OFF'}"
        self.minimized_btn = tk.Button(self.min_frame, text=minimized_text, bg=button_gray, fg=text_color, bd=0, relief=tk.FLAT, font=scale_font(("Arial", 10, "bold")), command=lambda: self.update_minimized_setting(not CONFIG.start_minimized))
        self.minimized_btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))

        # Hide Button
        self.hide_frame = tk.Frame(self.top_btn_frame, bg=button_gray)
        self.hide_frame.pack(side=tk.LEFT, padx=int(5 * scaling_factor))
        self.hide_btn = tk.Button(self.hide_frame, text="Hide to System Tray", bg=button_gray, fg=text_color, bd=0, relief=tk.FLAT, font=scale_font(("Arial", 10, "bold")), command=self.hide_to_tray)
        self.hide_btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))


        self.pack_controls_under_player()

        self.update_driver_button()
        self.update_usbip_button()
        self.update_driver_buttons_visibility()

        self.update([None])

        def get_focusable_widgets(parent, lst=None):
            if lst is None:
                lst = []
            if parent.winfo_ismapped():
                if isinstance(parent, (tk.Button, ttk.Combobox, tk.Scale, tk.Entry, tk.Checkbutton, tk.Radiobutton)) or getattr(parent, 'is_custom_recording_entry', False):
                    try:
                        if parent.cget('state') != 'disabled' and parent.cget('state') != tk.DISABLED:
                            lst.append(parent)
                    except:
                        lst.append(parent)
                for child in parent.winfo_children():
                    get_focusable_widgets(child, lst)
            return lst
            
        def spatial_navigate(current_widget, direction):
            widgets = get_focusable_widgets(self.root)
            if not widgets: return
            
            if not current_widget or current_widget not in widgets:
                widgets[0].focus_set()
                return

            cx = current_widget.winfo_rootx() + current_widget.winfo_width() / 2
            cy = current_widget.winfo_rooty() + current_widget.winfo_height() / 2

            candidates = []
            for w in widgets:
                if w == current_widget: continue
                wx = w.winfo_rootx() + w.winfo_width() / 2
                wy = w.winfo_rooty() + w.winfo_height() / 2
                dx = wx - cx
                dy = wy - cy
                
                # Filter candidates by strictly checking direction
                if direction == "UP" and dy >= -5: continue
                if direction == "DOWN" and dy <= 5: continue
                if direction == "LEFT" and dx >= -5: continue
                if direction == "RIGHT" and dx <= 5: continue
                
                candidates.append((w, dx, dy))
                
            if not candidates: return
            
            best_widget = None
            
            if direction in ("UP", "DOWN"):
                # Sort by vertical distance first to find the closest row
                candidates.sort(key=lambda item: abs(item[2]))
                min_dy = abs(candidates[0][2])
                # Filter candidates that belong to this closest row (within 15px)
                row_candidates = [c for c in candidates if abs(abs(c[2]) - min_dy) < 15]
                # Within this row, pick the one with smallest horizontal distance
                row_candidates.sort(key=lambda item: abs(item[1]))
                best_widget = row_candidates[0][0]
                
            else: # LEFT, RIGHT
                # For Left/Right, prefer staying on the same row.
                candidates.sort(key=lambda item: abs(item[2]))
                same_row_candidates = [c for c in candidates if abs(c[2]) < 15]
                
                if same_row_candidates:
                    same_row_candidates.sort(key=lambda item: abs(item[1]))
                    best_widget = same_row_candidates[0][0]
                else:
                    # If nothing on the same row, find the next closest column overall
                    candidates.sort(key=lambda item: abs(item[1]))
                    min_dx = abs(candidates[0][1])
                    col_candidates = [c for c in candidates if abs(abs(c[1]) - min_dx) < 15]
                    col_candidates.sort(key=lambda item: abs(item[2]))
                    best_widget = col_candidates[0][0]

            if best_widget:
                if isinstance(best_widget, tk.Button):
                    # For standard buttons, remove native focus to hide the dashed outline
                    # but manually trigger the outline so it remains visually targeted.
                    self.root.focus_set()
                    try:
                        self.focus_outline.update(best_widget)
                    except: pass
                else:
                    best_widget.focus_set()

        self.focus_outline = FocusOutline(self.root)

        def on_mouse_click(e):
            if getattr(self, 'ui_navigation_active', False):
                self.ui_navigation_active = False
                if hasattr(self, 'focus_outline'):
                    self.focus_outline.hide()
                    
        self.root.bind_all("<Button-1>", on_mouse_click, add='+')

        def on_global_focus_in(e):
            if getattr(self, 'ui_navigation_active', False):
                if isinstance(e.widget, (tk.Button, ttk.Combobox, tk.Scale, tk.Entry, tk.Checkbutton, tk.Radiobutton)) or getattr(e.widget, 'is_custom_recording_entry', False):
                    self.focus_outline.update(e.widget)

        self.root.bind_all("<FocusIn>", on_global_focus_in)
        
        def poll_ui_navigation():
            if not getattr(self, 'root', None) or not self.root.winfo_exists():
                return
            
            self.root.after(50, poll_ui_navigation)
            
            if getattr(self, 'recording_controllers', False):
                return

            def safe_focus_get():
                try:
                    return self.root.focus_get()
                except KeyError:
                    return None

            # Check if OS active window is our app
            try:
                import win32process
                import os
                import ctypes
                hwnd = ctypes.windll.user32.GetForegroundWindow()
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid != os.getpid():
                    return
            except:
                pass

                
            if self.root.state() != 'normal' and self.root.state() != 'zoomed':
                return
                
            from config import SWITCH_BUTTONS
            import time
            current_time = time.time()
            
            if not hasattr(self, 'nav_last_move_time'): self.nav_last_move_time = 0
            if not hasattr(self, 'nav_last_click_time'): self.nav_last_click_time = 0
            if not hasattr(self, 'nav_last_cancel_time'): self.nav_last_cancel_time = 0
            if not hasattr(self, 'ui_navigation_active'): self.ui_navigation_active = False
            if not hasattr(self, 'debug_print_count'): self.debug_print_count = 0
                
            if not hasattr(self, 'nav_last_right_time'): self.nav_last_right_time = 0
                
            nav_dir = None
            right_nav_dir = None
            click_pressed = False
            cancel_pressed = False
            
            up_mask = SWITCH_BUTTONS.get("UP", 0x00020000)
            down_mask = SWITCH_BUTTONS.get("DOWN", 0x00010000)
            left_mask = SWITCH_BUTTONS.get("LEFT", 0x00080000)
            right_mask = SWITCH_BUTTONS.get("RIGHT", 0x00040000)
            a_mask = SWITCH_BUTTONS.get("A", 0x00000008) # Physical A button (Right)
            b_mask = SWITCH_BUTTONS.get("B", 0x00000004) # Physical B button (Down)
            
            from config import CONFIG
            if getattr(CONFIG, 'abxy_mode', 'Switch') == 'Xbox':
                click_mask = b_mask
                cancel_mask = a_mask
            else:
                click_mask = a_mask
                cancel_mask = b_mask
            
            vcs = getattr(self, 'current_controllers', [])
            
            for vc in vcs:
                if vc is None: continue
                for c in vc.controllers:
                    last_data = getattr(c, 'last_input_data', None)
                    if last_data:
                        if not getattr(c, 'is_joycon_right', lambda: False)():
                            lx, ly = last_data.left_stick
                            if lx > 0.5: nav_dir = "RIGHT"
                            elif lx < -0.5: nav_dir = "LEFT"
                            elif ly > 0.5: nav_dir = "UP"
                            elif ly < -0.5: nav_dir = "DOWN"
                        
                        if not getattr(c, 'is_joycon_left', lambda: False)():
                            rx, ry = last_data.right_stick
                            if rx > 0.5: right_nav_dir = "RIGHT"
                            elif rx < -0.5: right_nav_dir = "LEFT"
                            elif ry > 0.5: right_nav_dir = "UP"
                            elif ry < -0.5: right_nav_dir = "DOWN"
                    
                    buttons = getattr(c, 'raw_buttons', 0)
                    if buttons & up_mask: nav_dir = "UP"
                    if buttons & down_mask: nav_dir = "DOWN"
                    if buttons & left_mask: nav_dir = "LEFT"
                    if buttons & right_mask: nav_dir = "RIGHT"
                    if buttons & click_mask: click_pressed = True
                    if buttons & cancel_mask: cancel_pressed = True
                        
            if nav_dir or click_pressed or cancel_pressed or right_nav_dir:
                
                # Definitively check if we are currently inside an open combobox popdown via Tcl
                popdown_is_open = False
                popdown = None
                listbox = None
                cb = None
                
                if hasattr(self, 'focus_outline') and self.focus_outline.target_widget:
                    cb = self.focus_outline.target_widget
                    if isinstance(cb, ttk.Combobox):
                        try:
                            popdown = self.root.tk.call('ttk::combobox::PopdownWindow', cb)
                            if self.root.tk.call('winfo', 'exists', popdown) and self.root.tk.call('winfo', 'ismapped', popdown):
                                popdown_is_open = True
                                listbox = f"{popdown}.f.l"
                        except Exception:
                            pass

                if popdown_is_open and listbox:
                    try:
                        # Both Right Stick and Left Stick (D-Pad) can navigate the list
                        if right_nav_dir in ("UP", "DOWN") or nav_dir in ("UP", "DOWN"):
                            if current_time - self.nav_last_right_time > 0.2:
                                self.nav_last_right_time = current_time
                                self.nav_last_move_time = current_time
                                try:
                                    size = int(self.root.tk.call(listbox, 'size'))
                                    if size > 0:
                                        selected = self.root.tk.call(listbox, 'curselection')
                                        if not selected:
                                            curr = 0
                                        else:
                                            curr = int(selected[0]) if isinstance(selected, (tuple, list)) else int(selected)
                                            
                                        if right_nav_dir == "UP" or nav_dir == "UP":
                                            curr -= 1
                                        else:
                                            curr += 1
                                            
                                        if curr < 0: curr = 0
                                        if curr >= size: curr = size - 1
                                        
                                        self.root.tk.call(listbox, 'selection', 'clear', 0, 'end')
                                        self.root.tk.call(listbox, 'selection', 'set', curr)
                                        self.root.tk.call(listbox, 'activate', curr)
                                        self.root.tk.call(listbox, 'see', curr)
                                except Exception as listbox_e:
                                    import logging
                                    logging.getLogger(__name__).error(f"Listbox nav error: {listbox_e}")
                        elif click_pressed and current_time - self.nav_last_click_time > 0.3:
                            self.nav_last_click_time = current_time
                            self.nav_last_cancel_time = current_time # Sync to prevent A/B swap bounce
                            self.root.tk.call('event', 'generate', listbox, '<Return>')
                        elif cancel_pressed and current_time - self.nav_last_cancel_time > 0.3:
                            self.nav_last_cancel_time = current_time
                            self.nav_last_click_time = current_time # Sync to prevent A/B swap bounce
                            self.root.tk.call('event', 'generate', listbox, '<Escape>')
                            
                        # Prevent spatial navigation from taking place while menu is open
                        nav_dir = None
                        click_pressed = False
                        cancel_pressed = False
                        right_nav_dir = None
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).error(f"Dropdown error: {e}")

            if nav_dir or click_pressed or cancel_pressed:
                import logging
                logging.getLogger(__name__).info(f"Input detected: dir={nav_dir}, click={click_pressed}, cancel={cancel_pressed}")

            if cancel_pressed and self.ui_navigation_active and current_time - self.nav_last_cancel_time > 0.3:
                self.nav_last_cancel_time = current_time
                self.nav_last_click_time = current_time # Sync
                self.ui_navigation_active = False
                if hasattr(self, 'focus_outline'):
                    self.focus_outline.hide()
                self.root.focus_set()

            if nav_dir and current_time - self.nav_last_move_time > 0.2:
                self.nav_last_move_time = current_time
                self.ui_navigation_active = True
                focused = safe_focus_get()
                if (not focused or focused == self.root) and hasattr(self, 'focus_outline') and self.focus_outline.target_widget:
                    focused = self.focus_outline.target_widget
                
                if click_pressed and (isinstance(focused, tk.Scale) or getattr(focused, 'is_time_entry', False)):
                    if getattr(focused, 'is_time_entry', False):
                        try:
                            val = int(focused.get() or 0)
                            if nav_dir in ("UP", "RIGHT"):
                                val += 1
                            else:
                                val -= 1
                            if val < 0: val = 0
                            focused.delete(0, tk.END)
                            focused.insert(0, str(val))
                            if hasattr(self, 'on_auto_disconnect_time_changed'):
                                self.on_auto_disconnect_time_changed()
                        except: pass
                    else:
                        try:
                            val = float(focused.get())
                            res = float(focused.cget('resolution')) or 1.0
                            if nav_dir in ("UP", "RIGHT"):
                                val += res
                            else:
                                val -= res
                            focused.set(val)
                        except: pass
                else:
                    spatial_navigate(focused, nav_dir)
                
            if right_nav_dir and self.ui_navigation_active and current_time - self.nav_last_right_time > 0.2:
                self.nav_last_right_time = current_time
                focused = safe_focus_get()
                if (not focused or focused == self.root) and hasattr(self, 'focus_outline') and self.focus_outline.target_widget:
                    focused = self.focus_outline.target_widget
                
                if focused:
                    if getattr(focused, 'is_time_entry', False):
                        try:
                            val = int(focused.get() or 0)
                            if right_nav_dir in ("UP", "RIGHT"):
                                val += 1
                            else:
                                val -= 1
                            if val < 0: val = 0
                            focused.delete(0, tk.END)
                            focused.insert(0, str(val))
                            if hasattr(self, 'on_auto_disconnect_time_changed'):
                                self.on_auto_disconnect_time_changed()
                        except: pass
                    elif isinstance(focused, tk.Scale):
                        try:
                            val = float(focused.get())
                            res = float(focused.cget('resolution')) or 1.0
                            if right_nav_dir in ("UP", "RIGHT"):
                                val += res
                            else:
                                val -= res
                            focused.set(val)
                        except: pass
                    elif isinstance(focused, ttk.Combobox):
                        try:
                            vals = focused['values']
                            if vals:
                                try:
                                    idx = vals.index(focused.get())
                                except ValueError:
                                    idx = 0
                                if right_nav_dir in ("UP", "LEFT"):
                                    idx = (idx - 1) % len(vals)
                                else:
                                    idx = (idx + 1) % len(vals)
                                focused.set(vals[idx])
                                focused.event_generate("<<ComboboxSelected>>")
                        except: pass
                
            if click_pressed and self.ui_navigation_active and current_time - self.nav_last_click_time > 0.3:
                self.nav_last_click_time = current_time
                self.nav_last_cancel_time = current_time # Sync
                focused = safe_focus_get()
                if (not focused or focused == self.root) and hasattr(self, 'focus_outline') and self.focus_outline.target_widget:
                    focused = self.focus_outline.target_widget
                    
                if focused:
                    if isinstance(focused, ttk.Combobox):
                        focused.focus_set() # Regain native focus before trying to open popdown
                        focused.event_generate('<Down>')
                    elif getattr(focused, 'is_custom_recording_entry', False):
                        if callable(getattr(focused, 'restart_custom_recording_fn', None)):
                            focused.restart_custom_recording_fn()
                    elif hasattr(focused, 'invoke') and callable(getattr(focused, 'invoke')):
                        try:
                            focused.invoke()
                        except:
                            pass
                    else:
                        try:
                            focused.event_generate('<space>')
                            focused.event_generate('<Return>')
                        except:
                            pass
                            
        poll_ui_navigation()


    def pack_controls_under_player(self):
        for frame in (getattr(self, "top_btn_frame", None), getattr(self, "auto_disconnect_frame", None), getattr(self, "settings_frame", None)):
            if frame is not None:
                frame.pack_forget()

        if getattr(self, "top_btn_frame", None) is not None:
            self.top_btn_frame.pack(side=tk.TOP, pady=(0, int(5 * scaling_factor)))
        if getattr(self, "auto_disconnect_frame", None) is not None:
            self.auto_disconnect_frame.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        if getattr(self, "settings_frame", None) is not None:
            self.settings_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(int(5 * scaling_factor), 0))


    def on_configure(self, event):
        if event.widget == self.root:
            try:
                if self.root.state() == 'normal':
                    w = self.root.winfo_width()
                    h = self.root.winfo_height()
                    rx = self.root.winfo_x()
                    ry = self.root.winfo_y()
                    if w > 100 and h > 100:
                        self.last_width = w
                        self.last_height = h
                        self.last_x = rx
                        self.last_y = ry
            except Exception:
                pass

    def init_compensation_panel(self, parent=None):
        parent = parent or self.root
        panel_bg = parent.cget("bg") if parent is not self.root else background_color
        self.comp_frame = tk.LabelFrame(parent, text=" Gyro Pass-Through ", bg=panel_bg, fg=text_color, font=scale_font(("Arial", 11, "bold")), padx=int(10 * scaling_factor), pady=int(10 * scaling_factor))
        if parent is self.root:
            self.comp_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=int(5 * scaling_factor))
        
        tk.Label(self.comp_frame, text="9-axis Assist:", bg=panel_bg, fg=text_color, font=scale_font(("Arial", 11, "bold"))).grid(row=0, column=0, padx=int(5 * scaling_factor), sticky="e")
        self.stabilized_gyro_switch = ToggleSwitch(self.comp_frame, labels=["ON", "OFF"], values=[True, False], initial_value=getattr(CONFIG, "stabilized_gyro", False), command=self.update_stabilized_gyro_setting, bg_color=panel_bg)
        self.stabilized_gyro_switch.grid(row=0, column=1, columnspan=2, padx=int(5 * scaling_factor), sticky="w")
        tk.Label(self.comp_frame, text="Horizon Lock:", bg=panel_bg, fg=text_color, font=scale_font(("Arial", 11, "bold"))).grid(row=0, column=3, padx=(int(20 * scaling_factor), int(5 * scaling_factor)), sticky="e")
        self.steam_roll_comp_switch = ToggleSwitch(self.comp_frame, labels=["ON", "OFF"], values=[True, False], initial_value=getattr(CONFIG, "steam_roll_compensation", False), command=self.update_steam_roll_comp_setting, bg_color=panel_bg)
        self.steam_roll_comp_switch.grid(row=0, column=4, columnspan=2, padx=int(5 * scaling_factor), sticky="w")

        tk.Label(self.comp_frame, text="Deadzone:", bg=panel_bg, fg=text_color, font=scale_font(("Arial", 11, "bold"))).grid(row=0, column=6, padx=(int(20 * scaling_factor), int(5 * scaling_factor)), sticky="e")
        self.deadzone_scale = tk.Scale(
            self.comp_frame,
            from_=0.0,
            to=5.0,
            resolution=0.5,
            orient=tk.HORIZONTAL,
            length=int(120 * scaling_factor),
            bg=panel_bg,
            fg=text_color,
            troughcolor=button_gray,
            activebackground=highlight_color,
            highlightthickness=0,
            bd=0,
            sliderrelief=tk.FLAT,
            sliderlength=int(15 * scaling_factor),
            width=int(15 * scaling_factor),
            font=scale_font(("Arial", 11, "bold")),
            command=self.update_virtual_gyro_soft_deadzone_setting
        )
        self.deadzone_scale.set(getattr(CONFIG, "virtual_gyro_soft_deadzone", 2.0))
        self.deadzone_scale.grid(row=0, column=7, columnspan=2, padx=int(5 * scaling_factor), sticky="w")

        tk.Label(self.comp_frame, text="Mode:", bg=panel_bg, fg=text_color, font=scale_font(("Arial", 11, "bold"))).grid(row=1, column=0, padx=int(5 * scaling_factor), pady=(int(5 * scaling_factor), 0), sticky="e")
        self.passthrough_mode_switch = ToggleSwitch(self.comp_frame, labels=["Default", "Cemuhook"], values=["Default", "Cemuhook"], 
initial_value=getattr(CONFIG, "gyro_passthrough_mode", "Default"), command=self.update_passthrough_mode, 
bg_color=panel_bg, widths=[8, 10])
        self.passthrough_mode_switch.grid(row=1, column=1, columnspan=2, padx=int(5 * scaling_factor), pady=(int(5 * scaling_factor), 0), sticky="w")

        self.sens_label = tk.Label(self.comp_frame, text="Sensitivity:", bg=panel_bg, fg=text_color, font=scale_font(("Arial", 11, "bold")))
        self.cemuhook_sens_scale = tk.Scale(
            self.comp_frame,
            from_=1,
            to=5,
            resolution=1,
            orient=tk.HORIZONTAL,
            length=int(120 * scaling_factor),
            bg=panel_bg,
            fg=text_color,
            troughcolor=button_gray,
            activebackground=highlight_color,
            highlightthickness=0,
            bd=0,
            sliderrelief=tk.FLAT,
            sliderlength=int(15 * scaling_factor),
            width=int(15 * scaling_factor),
            font=scale_font(("Arial", 11, "bold")),
            command=self.update_cemuhook_sensitivity
        )
        self.cemuhook_sens_scale.set(getattr(CONFIG, "cemuhook_sensitivity", 1))
        
        self.update_sens_visibility(getattr(CONFIG, "gyro_passthrough_mode", "Default"))

        if getattr(CONFIG, "gyro_passthrough_mode", "Default") == "Cemuhook":
            cemuhook_server.start()

    def update_sens_visibility(self, mode):
        if mode == "Cemuhook":
            self.sens_label.grid(row=1, column=3, padx=(int(20 * scaling_factor), int(5 * scaling_factor)), pady=(int(5 * scaling_factor), 0), sticky="e")
            self.cemuhook_sens_scale.grid(row=1, column=4, columnspan=2, padx=int(5 * scaling_factor), pady=(int(5 * scaling_factor), 0), sticky="w")
        else:
            self.sens_label.grid_forget()
            self.cemuhook_sens_scale.grid_forget()

    def update_passthrough_mode(self, mode):
        CONFIG.gyro_passthrough_mode = mode
        CONFIG.save_config()
        if mode == "Cemuhook":
            cemuhook_server.start()
        else:
            cemuhook_server.stop()
        self.update_sens_visibility(mode)
        logger.info(f"Gyro Passthrough Mode updated to {mode}")

    def update_cemuhook_sensitivity(self, val):
        val = int(float(val))
        CONFIG.cemuhook_sensitivity = val
        CONFIG.save_config()
        logger.info(f"Cemuhook Sensitivity updated to {val}")

    def init_djg_panel(self, parent=None):
        parent = parent or self.root
        panel_bg = parent.cget("bg") if parent is not self.root else background_color
        self.djg_frame = tk.LabelFrame(parent, text=" Dual Joy-con Gyro (DJG) ", bg=panel_bg, fg=text_color, font=scale_font(("Arial", 11, "bold")), padx=int(10 * scaling_factor), pady=int(10 * scaling_factor))
        if parent is self.root:
            self.djg_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=int(5 * scaling_factor))
        
        tk.Label(self.djg_frame, text="DJG:", bg=panel_bg, fg=text_color, font=scale_font(("Arial", 11, "bold"))).grid(row=0, column=0, padx=int(5 * scaling_factor), sticky="e")
        self.djg_enabled_switch = ToggleSwitch(self.djg_frame, labels=["ON", "OFF"], values=[True, False], initial_value=getattr(CONFIG, "djg_enabled", False), command=self.update_djg_enabled_setting, bg_color=panel_bg)
        self.djg_enabled_switch.grid(row=0, column=1, columnspan=2, padx=int(5 * scaling_factor), sticky="w")
        
        tk.Label(self.djg_frame, text="Dominant Side:", bg=panel_bg, fg=text_color, font=scale_font(("Arial", 11, "bold"))).grid(row=0, column=3, padx=(int(20 * scaling_factor), int(5 * scaling_factor)), sticky="e")
        self.djg_dominant_switch = ToggleSwitch(self.djg_frame, labels=["Left", "Right"], values=["Left", "Right"], initial_value=getattr(CONFIG, "djg_dominant_side", "Left"), command=self.update_djg_dominant_setting, bg_color=panel_bg)
        self.djg_dominant_switch.grid(row=0, column=4, columnspan=2, padx=int(5 * scaling_factor), sticky="w")
        
        tk.Label(self.djg_frame, text="Mode:", bg=panel_bg, fg=text_color, font=scale_font(("Arial", 11, "bold"))).grid(row=0, column=6, padx=(int(20 * scaling_factor), int(5 * scaling_factor)), sticky="e")
        
        self.djg_mode_var = tk.StringVar(value=getattr(CONFIG, "djg_mode", "Single Side Toggle"))
        djg_modes = ["Single Side Toggle", "Switch Dominant Side", "Switch Gyro Side"]
        
        # Calculate max width for dropdown
        max_mode_len = max(len(m) for m in djg_modes)
        
        self.djg_mode_combo = ttk.Combobox(self.djg_frame, textvariable=self.djg_mode_var, values=djg_modes, state="readonly", font=scale_font(("Arial", 11, "bold")), width=max_mode_len, justify="center")
        self.djg_mode_combo.grid(row=0, column=7, padx=int(5 * scaling_factor), sticky="w")
        self.djg_mode_combo.bind("<<ComboboxSelected>>", lambda e: self.update_djg_mode_setting(self.djg_mode_var.get()))

        tk.Label(self.djg_frame, text="Activation:", bg=panel_bg, fg=text_color, font=scale_font(("Arial", 11, "bold"))).grid(row=0, column=8, padx=(int(20 * scaling_factor), int(5 * scaling_factor)), sticky="e")
        self.djg_activation_switch = ToggleSwitch(self.djg_frame, labels=["Hold", "Toggle"], values=["Hold", "Toggle"], initial_value=getattr(CONFIG, "djg_activation", "Toggle"), command=self.update_djg_activation_setting, bg_color=panel_bg)
        self.djg_activation_switch.grid(row=0, column=9, columnspan=2, padx=int(5 * scaling_factor), sticky="w")

        self._update_djg_panel_visibility()

    def _update_djg_panel_visibility(self):
        if not hasattr(self, 'djg_frame'):
            return
        if hasattr(self, "settings_active_tab"):
            self.show_settings_tab(self.settings_active_tab)
        elif getattr(CONFIG, 'simulation_mode', '') == "Switch1":
            self.djg_frame.pack_forget()
        elif not self.djg_frame.winfo_ismapped():
            self.djg_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=int(5 * scaling_factor))
        try:
            self.root.update_idletasks()
        except Exception:
            pass

    def update_djg_activation_setting(self, val):
        CONFIG.djg_activation = val
        CONFIG.save_config()
        logger.info(f"DJG Activation: {val}")

    def update_djg_mode_setting(self, val):
        CONFIG.djg_mode = val
        CONFIG.save_config()
        logger.info(f"DJG Mode: {val}")
        self.force_refresh_player_slots()


    def update_djg_enabled_setting(self, val):
        CONFIG.djg_enabled = val
        CONFIG.save_config()
        logger.info(f"DJG Enabled: {val}")
        if not val:
            for vc in VIRTUAL_CONTROLLERS:
                if vc:
                    vc.active_gyro_side = getattr(CONFIG, "djg_dominant_side", "Left")
        self.force_refresh_player_slots()

    def update_djg_dominant_setting(self, val):
        CONFIG.djg_dominant_side = val
        CONFIG.save_config()
        logger.info(f"DJG Dominant Side: {val}")
        if not getattr(CONFIG, "djg_enabled", False):
            for vc in VIRTUAL_CONTROLLERS:
                if vc:
                    vc.active_gyro_side = val
        else:
            mode = getattr(CONFIG, "djg_mode", "Single Side Toggle")
            if mode == "Switch Dominant Side":
                for vc in VIRTUAL_CONTROLLERS:
                    if vc:
                        vc.djg_left_active = True
                        vc.djg_right_active = True
            elif mode == "Switch Gyro Side":
                for vc in VIRTUAL_CONTROLLERS:
                    if vc:
                        vc.active_gyro_side = val
        self.force_refresh_player_slots()


    def init_gyro_settings_panel(self, parent=None):
        parent = parent or self.root
        panel_bg = parent.cget("bg") if parent is not self.root else background_color
        self.gyro_frame = tk.LabelFrame(parent, text=" In-app Gyro Mode ", bg=panel_bg, fg=text_color, font=scale_font(("Arial", 11, "bold")), padx=int(10 * scaling_factor), pady=int(10 * scaling_factor))
        if parent is self.root:
            self.gyro_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=int(5 * scaling_factor))

        # ---- Shared calibration controls ----
        gyro_calib_row = tk.Frame(self.gyro_frame, bg=panel_bg)
        self.calib_frame = tk.Frame(gyro_calib_row, bg=panel_bg)
        self.calib_frame.pack(side=tk.LEFT)
        self.calib_button_frame = tk.Frame(self.calib_frame, bg=button_gray)
        self.calib_button_frame.pack(side=tk.LEFT)
        self.calibrate_btn = tk.Button(self.calib_button_frame, text="Calibrate Gyro", command=self.on_calibrate_clicked, bg=button_gray, fg=text_color, bd=0, relief=tk.FLAT, font=scale_font(("Arial", 11, "bold")))
        self.calibrate_btn.pack(side=tk.LEFT, padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))

        self.calib_hint_label = tk.Label(self.calib_frame, text="Keep controller stationary\nbefore calibrating.", bg=panel_bg, fg=text_color, font=scale_font(("Arial", 11, "bold")), justify=tk.LEFT)
        self.calib_hint_label.pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(2 * scaling_factor)), pady=int(2 * scaling_factor))

        mag_hint_frame = tk.Frame(self.gyro_frame, bg=panel_bg)

        l1 = tk.Frame(mag_hint_frame, bg=panel_bg)
        l1.pack(side=tk.TOP, anchor="w")
        tk.Label(l1, text="Calibrate Mag (Mag Cal): Move controller in a", bg=panel_bg, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT)

        l2 = tk.Frame(mag_hint_frame, bg=panel_bg)
        l2.pack(side=tk.TOP, anchor="w")

        lnk = tk.Label(l2, text="'figure 8'", bg=panel_bg, fg=highlight_color, font=scale_font(("Arial", 11, "bold", "underline")), cursor="hand2")
        lnk.pack(side=tk.LEFT)
        lnk.bind("<Button-1>", lambda e: (logger.info(f"Opening YouTube link via webbrowser..."), webbrowser.open("https://youtu.be/J_cZnPcW-Yw?si=ID2vdzURiOph8x77&t=6")))

        tk.Label(l2, text=" pattern during calibration.", bg=panel_bg, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT)

        # ---- Row 1: Gyro Control + Sensitivity + Calibrate Gyro ----
        tk.Label(self.gyro_frame, text="Gyro Control:", bg=panel_bg, fg=text_color, font=scale_font(("Arial", 11, "bold"))).grid(row=1, column=0, padx=int(5 * scaling_factor), pady=(int(10 * scaling_factor), 0), sticky="e")
        initial_gyro_control = "Steering" if getattr(CONFIG, "gyro_mode", "World") == "Roll" else getattr(CONFIG, "gyro_control_mode", "Mouse")
        self.gyro_control_switch = ToggleSwitch(self.gyro_frame, labels=["Mouse", "R Joystick", "Steering"], values=["Mouse", "R Joystick", "Steering"], initial_value=initial_gyro_control, command=self.update_gyro_control_mode, bg_color=panel_bg)
        self.gyro_control_switch.grid(row=1, column=1, padx=int(5 * scaling_factor), pady=(int(10 * scaling_factor), 0), sticky="w")
        tk.Label(self.gyro_frame, text="Sensitivity:", bg=panel_bg, fg=text_color, font=scale_font(("Arial", 11, "bold"))).grid(row=1, column=2, padx=(int(20 * scaling_factor), int(5 * scaling_factor)), pady=(int(10 * scaling_factor), 0), sticky="e")
        self.sens_scale = tk.Scale(self.gyro_frame, from_=1, to=10, resolution=0.2, orient=tk.HORIZONTAL, length=int(120 * scaling_factor), bg=panel_bg, fg=text_color, troughcolor=button_gray, activebackground=highlight_color, highlightthickness=0, bd=0, sliderrelief=tk.FLAT, sliderlength=int(15 * scaling_factor), width=int(15 * scaling_factor), font=scale_font(("Arial", 11, "bold")), command=self.on_gyro_setting_changed)
        self.sens_scale.set(self._current_gyro_control_sensitivity())
        self.sens_scale.grid(row=1, column=3, padx=int(5 * scaling_factor), pady=(int(10 * scaling_factor), 0), sticky="w")
        gyro_calib_row.grid(row=1, column=4, columnspan=3, padx=(int(20 * scaling_factor), int(5 * scaling_factor)), pady=(int(10 * scaling_factor), 0), sticky="w")

        # ---- Row 2: Mode + Stick Assist + Mag Cal hint ----
        tk.Label(self.gyro_frame, text="Mode:", bg=panel_bg, fg=text_color, font=scale_font(("Arial", 11, "bold"))).grid(row=2, column=0, padx=int(5 * scaling_factor), pady=(int(10 * scaling_factor), 0), sticky="e")
        self.gyro_mode_switch = ToggleSwitch(self.gyro_frame, labels=["9-Axis", "6-Axis"], values=["World", "Yaw"], initial_value=(CONFIG.gyro_mode if CONFIG.gyro_mode in ("World", "Yaw") else "World"), command=self.update_mode_setting, bg_color=panel_bg)
        self.gyro_mode_switch.grid(row=2, column=1, padx=int(5 * scaling_factor), pady=(int(10 * scaling_factor), 0), sticky="w")
        self.stick_assist_label = tk.Label(self.gyro_frame, text="Stick Assist:", bg=panel_bg, fg=text_color, font=scale_font(("Arial", 11, "bold")))
        self.stick_assist_label.grid(row=2, column=2, padx=(int(20 * scaling_factor), int(5 * scaling_factor)), pady=(int(10 * scaling_factor), 0), sticky="e")
        self.stick_scale = tk.Scale(self.gyro_frame, from_=0, to=10, resolution=0.2, orient=tk.HORIZONTAL, length=int(120 * scaling_factor), bg=panel_bg, fg=text_color, troughcolor=button_gray, activebackground=highlight_color, highlightthickness=0, bd=0, sliderrelief=tk.FLAT, sliderlength=int(15 * scaling_factor), width=int(15 * scaling_factor), font=scale_font(("Arial", 11, "bold")), command=self.on_gyro_setting_changed)
        self.stick_scale.set(getattr(CONFIG, "stick_mouse_sensitivity", 5.0))
        self.stick_scale.grid(row=2, column=3, padx=int(5 * scaling_factor), pady=(int(10 * scaling_factor), 0), sticky="w")
        mag_hint_frame.grid(row=2, column=4, columnspan=3, padx=(int(20 * scaling_factor), int(5 * scaling_factor)), pady=(int(10 * scaling_factor), 0), sticky="w")

        # ---- Row 3: Activation + Mode Shift ----
        tk.Label(self.gyro_frame, text="Activation:", bg=panel_bg, fg=text_color, font=scale_font(("Arial", 11, "bold"))).grid(row=3, column=0, padx=int(5 * scaling_factor), pady=(int(10 * scaling_factor), 0), sticky="e")
        self.gyro_act_switch = ToggleSwitch(self.gyro_frame, labels=["Toggle", "Hold"], values=["Toggle", "Hold"], initial_value=CONFIG.gyro_activation_mode, command=self.update_act_setting, bg_color=panel_bg)
        self.gyro_act_switch.grid(row=3, column=1, padx=int(5 * scaling_factor), pady=(int(10 * scaling_factor), 0), sticky="w")

        tk.Label(self.gyro_frame, text="Mode Shift:", bg=panel_bg, fg=text_color, font=scale_font(("Arial", 11, "bold"))).grid(row=3, column=2, padx=(int(20 * scaling_factor), int(5 * scaling_factor)), pady=(int(10 * scaling_factor), 0), sticky="e")
        self.mode_shift_switch = ToggleSwitch(self.gyro_frame, labels=["On", "Off"], values=[True, False], initial_value=CONFIG.mode_shift_enabled, command=self.update_mode_shift_setting, bg_color=panel_bg)
        self.mode_shift_switch.grid(row=3, column=3, padx=int(5 * scaling_factor), pady=(int(10 * scaling_factor), 0), sticky="w")

        self._update_gyro_control_visibility(initial_gyro_control)


    def init_auto_disconnect_panel(self):
        self.auto_disconnect_frame = tk.LabelFrame(self.root, text=" Auto Disconnect ", bg=background_color, fg=text_color, font=scale_font(("Arial", 11, "bold")), padx=int(10 * scaling_factor), pady=int(10 * scaling_factor))
        self.auto_disconnect_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=int(5 * scaling_factor))
        
        tk.Label(self.auto_disconnect_frame, text="Auto Disconnect:", bg=background_color, fg=text_color, font=scale_font(("Arial", 11, "bold"))).grid(row=0, column=0, padx=int(5 * scaling_factor), sticky="e")
        self.auto_disconnect_switch = ToggleSwitch(self.auto_disconnect_frame, labels=["OFF", "Inactive", "Absolute"], values=["OFF", "Inactive", "Absolute"], initial_value=getattr(CONFIG, "auto_disconnect_mode", "OFF"), command=self.update_auto_disconnect_mode, bg_color=background_color)
        self.auto_disconnect_switch.grid(row=0, column=1, padx=int(5 * scaling_factor), sticky="w")
        
        tk.Label(self.auto_disconnect_frame, text="Disconnect after:", bg=background_color, fg=text_color, font=scale_font(("Arial", 11, "bold"))).grid(row=0, column=2, padx=(int(20 * scaling_factor), int(5 * scaling_factor)), sticky="e")
        
        # Validation to only allow digits in time entries
        def validate_numeric(char):
            return char.isdigit() or char == ""
        vcmd = (self.root.register(validate_numeric), '%S')
        
        # Day Entry
        self.day_entry = tk.Entry(self.auto_disconnect_frame, width=4, bg=button_gray, fg=text_color, insertbackground=text_color, bd=0, relief=tk.FLAT, font=scale_font(("Arial", 11, "bold")), justify=tk.CENTER, validate="key", validatecommand=vcmd)
        self.day_entry.insert(0, str(getattr(CONFIG, "auto_disconnect_days", 0)))
        self.day_entry.grid(row=0, column=3, padx=int(2 * scaling_factor))
        self.day_entry.is_time_entry = True
        tk.Label(self.auto_disconnect_frame, text="Day", bg=background_color, fg=text_color, font=scale_font(("Arial", 11, "bold"))).grid(row=0, column=4, padx=(0, int(10 * scaling_factor)), sticky="w")
        
        # Hour Entry
        self.hour_entry = tk.Entry(self.auto_disconnect_frame, width=4, bg=button_gray, fg=text_color, insertbackground=text_color, bd=0, relief=tk.FLAT, font=scale_font(("Arial", 11, "bold")), justify=tk.CENTER, validate="key", validatecommand=vcmd)
        self.hour_entry.insert(0, str(getattr(CONFIG, "auto_disconnect_hours", 0)))
        self.hour_entry.grid(row=0, column=5, padx=int(2 * scaling_factor))
        self.hour_entry.is_time_entry = True
        tk.Label(self.auto_disconnect_frame, text="Hour", bg=background_color, fg=text_color, font=scale_font(("Arial", 11, "bold"))).grid(row=0, column=6, padx=(0, int(10 * scaling_factor)), sticky="w")
        
        # Minute Entry
        self.minute_entry = tk.Entry(self.auto_disconnect_frame, width=4, bg=button_gray, fg=text_color, insertbackground=text_color, bd=0, relief=tk.FLAT, font=scale_font(("Arial", 11, "bold")), justify=tk.CENTER, validate="key", validatecommand=vcmd)
        self.minute_entry.insert(0, str(getattr(CONFIG, "auto_disconnect_minutes", 0)))
        self.minute_entry.grid(row=0, column=7, padx=int(2 * scaling_factor))
        self.minute_entry.is_time_entry = True
        tk.Label(self.auto_disconnect_frame, text="Minute", bg=background_color, fg=text_color, font=scale_font(("Arial", 11, "bold"))).grid(row=0, column=8, padx=(0, int(10 * scaling_factor)), sticky="w")
        
        # Bind events
        self.day_entry.bind("<KeyRelease>", self.on_auto_disconnect_time_changed)
        self.hour_entry.bind("<KeyRelease>", self.on_auto_disconnect_time_changed)
        self.minute_entry.bind("<KeyRelease>", self.on_auto_disconnect_time_changed)

    def update_auto_disconnect_mode(self, val):
        CONFIG.auto_disconnect_mode = val
        CONFIG.save_config()

    def on_auto_disconnect_time_changed(self, event=None):
        try:
            days_str = self.day_entry.get()
            hours_str = self.hour_entry.get()
            minutes_str = self.minute_entry.get()
            
            days = int(days_str) if days_str else 0
            hours = int(hours_str) if hours_str else 0
            minutes = int(minutes_str) if minutes_str else 0
            
            CONFIG.auto_disconnect_days = days
            CONFIG.auto_disconnect_hours = hours
            CONFIG.auto_disconnect_minutes = minutes
            CONFIG.save_config()
        except Exception as e:
            logger.error(f"Failed to save auto disconnect settings: {e}")

    def on_rumble_delay_changed(self, event=None):
        val_str = getattr(self, "rumble_delay_entry", tk.Entry(self.root)).get().strip()
        if not val_str:
            val = 0
        else:
            try:
                val = int(val_str)
            except ValueError:
                val = 0
        CONFIG.rumble_delay_ms = val
        CONFIG.save_config()

    def update_mode_setting(self, val):
        CONFIG.gyro_mode = val
        self.on_gyro_setting_changed()

    def update_stabilized_gyro_setting(self, val):
        CONFIG.stabilized_gyro = val
        CONFIG.save_config()
        logger.info(f"9-Axis Stabilization (for 6-Axis): {val}")

    def update_steam_roll_comp_setting(self, val):
        CONFIG.steam_roll_compensation = val
        CONFIG.save_config()
        logger.info(f"Roll Compensation: {val}")

    def update_virtual_gyro_soft_deadzone_setting(self, val):
        val = float(val)
        CONFIG.virtual_gyro_soft_deadzone = val
        CONFIG.save_config()
        logger.info(f"Third-Party Gyro Deadzone: {val}")

    def update_mouse_setting(self, val):
        CONFIG.mouse_config.enabled = val
        try:
            with open(CONFIG.config_file_path, 'r', encoding='utf-8') as f: data = yaml.load(f, Loader=_YamlLoader) or {}
            if 'mouse' not in data: data['mouse'] = {}
            data['mouse']['enabled'] = val
            with open(CONFIG.config_file_path, 'w', encoding='utf-8') as f: yaml.dump(data, f, Dumper=_YamlDumper, default_flow_style=False)
        except Exception as e: logger.error(f"Failed to save mouse settings: {e}")

    def update_act_setting(self, val):
        CONFIG.gyro_activation_mode = val
        self.on_gyro_setting_changed()

    def update_mode_shift_setting(self, val):
        # Stored per (profile, Gyro Control mode). Controls only whether In-app Gyro
        # auto-applies the Mode Shift Mapping; the mapping tab stays visible regardless.
        CONFIG.mode_shift_enabled = bool(val)
        # Turning Mode Shift On re-engages the In-app Gyro activation-button sync between
        # Controller Mapping and the Mode Shift Mapping store (Off leaves them independent).
        if val:
            CONFIG.sync_active_in_app_gyro_activation()
        CONFIG.save_config()
        self._refresh_mapping_comboboxes()

    def _current_gyro_control_sensitivity(self):
        if getattr(CONFIG, "gyro_control_mode", "Mouse") == "R Joystick":
            return getattr(CONFIG, "r_joystick_gyro_sensitivity", 5.0)
        return getattr(CONFIG, "gyro_sensitivity", 0.3)

    def _save_current_gyro_control_sensitivity(self):
        if not hasattr(self, 'sens_scale'):
            return
        if getattr(CONFIG, "gyro_control_mode", "Mouse") == "R Joystick":
            CONFIG.r_joystick_gyro_sensitivity = float(self.sens_scale.get())
        else:
            CONFIG.gyro_sensitivity = float(self.sens_scale.get())

    def update_gyro_control_mode(self, val):
        self._save_current_gyro_control_sensitivity()
        CONFIG.gyro_control_mode = val
        if val == "Steering":
            CONFIG.gyro_mode = "Roll"
        elif getattr(CONFIG, "gyro_mode", "World") == "Roll":
            CONFIG.gyro_mode = self.gyro_mode_switch.values[self.gyro_mode_switch.current_index] if hasattr(self, "gyro_mode_switch") else "World"
        if hasattr(self, 'sens_scale'):
            self._updating_gyro_control_sensitivity = True
            self.sens_scale.set(self._current_gyro_control_sensitivity())
            self._updating_gyro_control_sensitivity = False
        # Mode Shift is stored per (profile, Gyro Control mode): reload its state for the
        # newly selected mode so the toggle and the mapping tab reflect that mode.
        if hasattr(self, 'mode_shift_switch'):
            self.mode_shift_switch.set_value(CONFIG.mode_shift_enabled)
        self._update_gyro_control_visibility(val)
        # The active In-app Gyro store switches with the mode; re-sync its In-app Gyro
        # activation buttons with Controller Mapping, then refresh the mapping tab so it
        # shows the mappings (and synced In-app Gyro buttons) for the selected mode.
        CONFIG.sync_active_in_app_gyro_activation()
        CONFIG.save_config()
        self._refresh_mapping_comboboxes()

    def _update_gyro_control_visibility(self, val):
        self._current_gyro_control_ui_value = val
        # Stick Assist only applies to gyro Mouse control; hide it for R Joystick/Steering.
        if not hasattr(self, 'stick_scale') or not hasattr(self, 'stick_assist_label'):
            return
        if val in ("R Joystick", "Steering"):
            self.stick_assist_label.grid_remove()
            self.stick_scale.grid_remove()
        else:
            self.stick_assist_label.grid()
            self.stick_scale.grid()
        self._update_in_app_gyro_mapping_tab_visibility()

    def _update_in_app_gyro_mapping_tab_visibility(self):
        # The Mode Shift Mapping tab is always visible. The Mode Shift On/Off toggle only
        # controls whether the mapping is applied at runtime (auto-applied on In-app Gyro
        # when On; otherwise applied only via the Mode Shift back button) -- it no longer
        # shows/hides this tab.
        widgets = getattr(self, "settings_tab_buttons", {}).get("in_app_gyro_mode_mapping")
        if not widgets:
            return
        btn, frame = widgets
        if not frame.winfo_ismapped():
            before_widgets = getattr(self, "settings_tab_buttons", {}).get("gyro_passthrough")
            pack_kwargs = {"side": tk.LEFT, "padx": (int(2 * scaling_factor), int(2 * scaling_factor))}
            if before_widgets:
                pack_kwargs["before"] = before_widgets[1]
            frame.pack(**pack_kwargs)

    def update_mouse_sensitivity(self, val):
        new_sens = float(val)
        CONFIG.mouse_config.sensitivity = new_sens
        try:
            with open(CONFIG.config_file_path, 'r', encoding='utf-8') as f: data = yaml.load(f, Loader=_YamlLoader) or {}
            if 'mouse' not in data: data['mouse'] = {}
            data['mouse']['sensitivity'] = new_sens
            with open(CONFIG.config_file_path, 'w', encoding='utf-8') as f: yaml.dump(data, f, Dumper=_YamlDumper, default_flow_style=False)
        except Exception as e: logger.error(f"Failed to save mouse sensitivity: {e}")

    def update_ir_activate_threshold(self, val):
        new_val = int(float(val))
        CONFIG.mouse_config.ir_activate_threshold = new_val
        try:
            with open(CONFIG.config_file_path, 'r', encoding='utf-8') as f: data = yaml.load(f, Loader=_YamlLoader) or {}
            if 'mouse' not in data: data['mouse'] = {}
            data['mouse']['ir_activate_threshold'] = new_val
            with open(CONFIG.config_file_path, 'w', encoding='utf-8') as f: yaml.dump(data, f, Dumper=_YamlDumper, default_flow_style=False)
        except Exception as e: logger.error(f"Failed to save IR activate threshold: {e}")

    def on_gyro_setting_changed(self, *args):
        if not hasattr(self, 'sens_scale') or not hasattr(self, 'stick_scale'):
            return
        if not getattr(self, '_updating_gyro_control_sensitivity', False):
            if getattr(CONFIG, "gyro_control_mode", "Mouse") == "R Joystick":
                CONFIG.r_joystick_gyro_sensitivity = float(self.sens_scale.get())
            else:
                CONFIG.gyro_sensitivity = float(self.sens_scale.get())
        CONFIG.stick_mouse_sensitivity = float(self.stick_scale.get())
        CONFIG.set_joystick_setting_scoped("l_joystick", "mouse_sensitivity", CONFIG.stick_mouse_sensitivity, "in_app_gyro_mode_mappings")
        CONFIG.set_joystick_setting_scoped("r_joystick", "mouse_sensitivity", CONFIG.stick_mouse_sensitivity, "in_app_gyro_mode_mappings")
        CONFIG.save_config()

    def on_calibrate_clicked(self):
        if not hasattr(self, 'current_controllers') or self.no_controllers: return
        
        self.calibrate_btn.config(state=tk.DISABLED, text="Starting in 3..", fg="#ffffff", disabledforeground="#ffffff")
        self.calib_button_frame.config(bg=highlight_color)
        self.calibrate_btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))
        
        self.root.after(1000, lambda: self.calibrate_btn.config(text="Starting in 2..", fg="#ffffff", disabledforeground="#ffffff"))
        self.root.after(2000, lambda: self.calibrate_btn.config(text="Starting in 1..", fg="#ffffff", disabledforeground="#ffffff"))
        
        def start_actual_calibration():
            for vc in self.current_controllers:
                if vc is not None: vc.start_calibration()
                
            self.calibrate_btn.config(text="Calibrating 5..", fg="#ffffff", disabledforeground="#ffffff")
            self.calib_button_frame.config(bg=highlight_color)
            self.calibrate_btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))
            
            self.root.after(1000, lambda: self.calibrate_btn.config(text="Calibrating 4..", fg="#ffffff", disabledforeground="#ffffff"))
            self.root.after(2000, lambda: self.calibrate_btn.config(text="Calibrating 3..", fg="#ffffff", disabledforeground="#ffffff"))
            self.root.after(3000, lambda: self.calibrate_btn.config(text="Calibrating 2..", fg="#ffffff", disabledforeground="#ffffff"))
            self.root.after(4000, lambda: self.calibrate_btn.config(text="Calibrating 1..", fg="#ffffff", disabledforeground="#ffffff"))
            
            self.root.after(5000, lambda: (
                self.calibrate_btn.config(state=tk.NORMAL, text="Calibration Done"), 
                self.calib_button_frame.config(bg=button_gray), 
                self.calibrate_btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))
            ))
            
        self.root.after(3000, start_actual_calibration)



    def _mapping_scope_suffix(self, mapping_scope=None):
        return "_in_app_gyro_mode" if mapping_scope == "in_app_gyro_mode_mappings" else ""

    def _mapping_attr(self, key, suffix):
        return f"{key}{suffix}"

    def start_custom_recording(self, key, entry, combo, custom_frame, mode_var, mapping_scope=None):
        entry.config(state="normal")
        entry.delete(0, tk.END)
        entry.insert(0, "Recording...")
        entry.config(state="readonly")
        entry.focus_set()
        
        pressed_keys = set()
        recorded_seq = []
        self.recording_controllers = True
        self.recorded_controller_buttons = set()
        self.waiting_for_controller_release = True

        def end_recording():
            self.recording_controllers = False
            self.root.unbind("<KeyPress>")
            self.root.unbind("<KeyRelease>")
            self.root.unbind("<ButtonPress>")
            self.root.unbind("<ButtonRelease>")
            self.root.unbind("<MouseWheel>")
            self.root.unbind("<FocusOut>")
            raw_seq = recorded_seq
            
            normalized_seq = []
            for k in raw_seq:
                if k in ("VK_CONTROL", "VK_CONTROL_L", "VK_CONTROL_R", "VK_LCONTROL", "VK_RCONTROL"):
                    nk = "VK_CONTROL"
                elif k in ("VK_SHIFT", "VK_SHIFT_L", "VK_SHIFT_R", "VK_LSHIFT", "VK_RSHIFT"):
                    nk = "VK_SHIFT"
                elif k in ("VK_MENU", "VK_ALT", "VK_ALT_L", "VK_ALT_R", "VK_LMENU", "VK_RMENU"):
                    nk = "VK_MENU"
                elif k in ("VK_WIN", "VK_LWIN", "VK_RWIN", "VK_WIN_L", "VK_WIN_R"):
                    nk = "VK_LWIN"
                else:
                    nk = k
                if nk not in normalized_seq:
                    normalized_seq.append(nk)
            
            final_seq = normalized_seq

            def sync_joystick_direction(value):
                base_key, sep, direction = key.rpartition("_")
                if sep and base_key in ("l_joystick", "r_joystick") and direction in ("up", "down", "left", "right"):
                    current = CONFIG.get_joystick_custom_scoped(base_key, mapping_scope)
                    current[direction] = value
                    CONFIG.set_joystick_custom_scoped(base_key, current, mapping_scope)
            
            if not final_seq:
                custom_frame.pack_forget()
                combo.pack(side=tk.LEFT)
                combo.set("Default")
                CONFIG.set_mapping_setting_scoped(key, "Default", mapping_scope)
                sync_joystick_direction("Default")
            else:
                mode = mode_var.get()
                val = f"Custom[{mode}]:" + "+".join(final_seq)
                CONFIG.set_mapping_setting_scoped(key, val, mapping_scope)
                sync_joystick_direction(val)
                entry.config(state="normal")
                entry.delete(0, tk.END)
                display_val = format_input_display("+".join(final_seq))
                entry.insert(0, display_val)
                entry.config(state="readonly")
            self.on_setting_changed()
            if getattr(self, "joystick_custom_popup", None) is not None:
                self.root.after(100, self.bind_joystick_custom_popup_outside_click)

        def check_release():
            if not pressed_keys and not getattr(self, 'controller_buttons_pressed', False):
                if not recorded_seq and not self.recorded_controller_buttons:
                    return
                end_recording()

        def on_key_press(e):
            vk = e.keysym.upper()
            pressed_keys.add(f"VK_{vk}")
            if f"VK_{vk}" not in recorded_seq:
                recorded_seq.append(f"VK_{vk}")
            return "break"

        def on_key_release(e):
            vk = e.keysym.upper()
            if f"VK_{vk}" in pressed_keys:
                pressed_keys.remove(f"VK_{vk}")
            check_release()
            return "break"

        def on_mouse_press(e):
            btn = f"MB_{e.num}"
            pressed_keys.add(btn)
            if btn not in recorded_seq:
                recorded_seq.append(btn)
            return "break"

        def on_mouse_release(e):
            btn = f"MB_{e.num}"
            if btn in pressed_keys:
                pressed_keys.remove(btn)
            check_release()
            return "break"

        def on_mouse_wheel(e):
            dir_str = "UP" if e.delta > 0 else "DOWN"
            if f"MW_{dir_str}" not in recorded_seq:
                recorded_seq.append(f"MW_{dir_str}")
            self.root.after(100, check_release)
            return "break"

        self.root.bind("<KeyPress>", on_key_press)
        self.root.bind("<KeyRelease>", on_key_release)
        self.root.bind("<ButtonPress>", on_mouse_press)
        self.root.bind("<ButtonRelease>", on_mouse_release)
        self.root.bind("<MouseWheel>", on_mouse_wheel)
        
        def on_focus_out(e):
            if e.widget == self.root and getattr(self, 'recording_controllers', False):
                try:
                    if self.root.focus_get():
                        return
                except: pass
                import ctypes
                import win32con
                vk_map = {}
                for name in dir(win32con):
                    if name.startswith("VK_"):
                        val = getattr(win32con, name)
                        if val not in vk_map:
                            vk_map[val] = name[3:]
                for vk in range(8, 255):
                    if ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000:
                        if vk in vk_map:
                            if f"VK_{vk_map[vk]}" not in recorded_seq:
                                recorded_seq.append(f"VK_{vk_map[vk]}")
                        elif (65 <= vk <= 90) or (48 <= vk <= 57):
                            if f"VK_{chr(vk)}" not in recorded_seq:
                                recorded_seq.append(f"VK_{chr(vk)}")
                end_recording()
        self.root.bind("<FocusOut>", on_focus_out)
        

        def poll_controller():
            if not getattr(self, 'recording_controllers', False):
                return
            from config import SWITCH_BUTTONS
            any_pressed = False
            reverse_map = {v: k for k, v in SWITCH_BUTTONS.items() if k not in ["Capture", "PS_C_Click"]}
            
            for vc in getattr(self, 'current_controllers', []):
                if vc is None: continue
                for c in vc.controllers:
                    raw = getattr(c, 'raw_buttons', 0)
                    if raw:
                        any_pressed = True
                        if not getattr(self, 'waiting_for_controller_release', False):
                            for bit, btn_name in reverse_map.items():
                                if raw & bit:
                                    self.recorded_controller_buttons.add(f"BTN_{btn_name}")
                                    if f"BTN_{btn_name}" not in recorded_seq:
                                        recorded_seq.append(f"BTN_{btn_name}")
            
            if getattr(self, 'waiting_for_controller_release', False):
                if not any_pressed:
                    self.waiting_for_controller_release = False
            else:
                self.controller_buttons_pressed = any_pressed
                if not any_pressed and self.recorded_controller_buttons and not pressed_keys:
                    end_recording()
                    return
            self.root.after(50, poll_controller)
            
        poll_controller()

    def create_mapping_widget(self, parent, key, label_text, mapping_scope=None):
        suffix = self._mapping_scope_suffix(mapping_scope)
        attr_key = self._mapping_attr(key, suffix)
        parent_bg = parent.cget("bg") if hasattr(parent, "cget") else background_color
        if label_text:
            tk.Label(parent, text=label_text, bg=parent_bg, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT, padx=(int(5 * scaling_factor), int(2 * scaling_factor)))
        container = tk.Frame(parent, bg=parent_bg)
        container.pack(side=tk.LEFT, padx=int(2 * scaling_factor))

        def sync_joystick_direction(value):
            base_key, sep, direction = key.rpartition("_")
            if sep and base_key in ("l_joystick", "r_joystick") and direction in ("up", "down", "left", "right"):
                current = CONFIG.get_joystick_custom_scoped(base_key, mapping_scope)
                current[direction] = value
                CONFIG.set_joystick_custom_scoped(base_key, current, mapping_scope)
        
        combo = BackButtonSelector(container, self, font=scale_font(("Arial", 11, "bold")))

        custom_frame = tk.Frame(container, bg=parent_bg)
        
        mode_var = tk.StringVar(value="Hold")
        def toggle_mode():
            new_mode = "Tap" if mode_var.get() == "Hold" else "Hold"
            mode_var.set(new_mode)
            mode_btn.config(text=new_mode)
            current_val = CONFIG.get_mapping_setting_scoped(key, "Default", mapping_scope)
            if current_val.startswith("Custom"):
                if current_val.startswith("Custom[Tap]:") or current_val.startswith("Custom[Hold]:"):
                    new_val = f"Custom[{new_mode}]:{current_val.split(':', 1)[1]}"
                else:
                    new_val = f"Custom[{new_mode}]:{current_val[7:]}"
                CONFIG.set_mapping_setting_scoped(key, new_val, mapping_scope)
                sync_joystick_direction(new_val)
                self.on_setting_changed()

        mode_btn = tk.Button(custom_frame, text="Hold", bg=button_gray, fg="white", font=scale_font(("Arial", 9, "bold")), bd=0, relief=tk.FLAT, command=toggle_mode, width=4)
        mode_btn.pack(side=tk.LEFT, padx=(0, int(2 * scaling_factor)), fill=tk.Y)
        
        entry = RecordingEntry(custom_frame, normal_font=scale_font(("Arial", 11, "bold")), prefix_font=scale_font(("Arial", 8, "bold")), width=11, bg=button_gray, fg="white")
        entry.pack(side=tk.LEFT, fill=tk.Y)
        # Hovering the (fixed-width, often clipped) recording shows its full content.
        Tooltip(entry, entry.get)
        entry.restart_custom_recording_fn = lambda: self.start_custom_recording(key, entry, combo, custom_frame, mode_var, mapping_scope)
        # Gyro Lock / Mode Shift are fixed tokens, not recorded inputs, so don't re-record on click.
        entry.bind("<Button-1>", lambda e: None if combo.get() in (GYRO_LOCK_LABEL, MODE_SHIFT_LABEL) else entry.restart_custom_recording_fn())
        
        def on_close():
            custom_frame.pack_forget()
            combo.pack(side=tk.LEFT)
            combo.set("Default")
            CONFIG.set_mapping_setting_scoped(key, "Default", mapping_scope)
            sync_joystick_direction("Default")
            self.on_setting_changed()
            if hasattr(self, 'focus_outline') and getattr(self.focus_outline, 'target_widget', None) == close_btn:
                try:
                    self.focus_outline.update(combo)
                except: pass

        close_btn = tk.Button(custom_frame, text="X", bg="#ff4444", fg="white", font=scale_font(("Arial", 10, "bold")), bd=0, relief=tk.FLAT, command=on_close)
        close_btn.pack(side=tk.LEFT, padx=(int(2 * scaling_factor), 0), fill=tk.Y)

        # "Change Profile" shows a button (opens an Auto/Manual popup) + X, like a
        # Joystick Custom mapping, instead of a plain combo selection.
        cp_frame = tk.Frame(container, bg=parent_bg)
        cp_btn = tk.Button(cp_frame, text="Change Profile", bg=button_gray, fg="white", font=scale_font(("Arial", 10, "bold")), bd=0, relief=tk.FLAT)
        cp_btn.pack(side=tk.LEFT, fill=tk.Y)
        cp_btn.config(command=lambda: self.open_change_profile_popup(cp_btn))

        def cp_close():
            cp_frame.pack_forget()
            combo.pack(side=tk.LEFT)
            combo.set("Default")
            CONFIG.set_mapping_setting_scoped(key, "Default", mapping_scope)
            sync_joystick_direction("Default")
            self.on_setting_changed()

        cp_close_btn = tk.Button(cp_frame, text="X", bg="#ff4444", fg="white", font=scale_font(("Arial", 10, "bold")), bd=0, relief=tk.FLAT, command=cp_close)
        cp_close_btn.pack(side=tk.LEFT, padx=(int(2 * scaling_factor), 0), fill=tk.Y)

        def show_change_profile(event=None):
            CONFIG.set_mapping_setting_scoped(key, "Change Profile", mapping_scope)
            sync_joystick_direction("Change Profile")
            combo.pack_forget()
            cp_frame.pack(side=tk.LEFT)
            self.on_setting_changed(event)

        current_val = CONFIG.get_mapping_setting_scoped(key, "Default", mapping_scope)
        if current_val.startswith("Custom"):
            entry.config(state="normal")
            entry.delete(0, tk.END)

            if current_val.startswith("Custom[Tap]:"):
                mode_var.set("Tap")
                mode_btn.config(text="Tap")
                display_val = current_val[12:]
            elif current_val.startswith("Custom[Hold]:"):
                mode_var.set("Hold")
                mode_btn.config(text="Hold")
                display_val = current_val[13:]
            else:
                mode_var.set("Hold")
                mode_btn.config(text="Hold")
                display_val = current_val[7:]

            if display_val == GYRO_LOCK_TOKEN:
                entry.insert(0, GYRO_LOCK_LABEL)
                combo.set(GYRO_LOCK_LABEL)
            elif display_val == MODE_SHIFT_TOKEN:
                entry.insert(0, MODE_SHIFT_LABEL)
                combo.set(MODE_SHIFT_LABEL)
            else:
                display_val = format_input_display(display_val)
                entry.insert(0, display_val)
                combo.set("Custom")
            entry.config(state="readonly")
            custom_frame.pack(side=tk.LEFT)
        elif current_val == "Change Profile":
            combo.set("Change Profile")
            cp_frame.pack(side=tk.LEFT)
        else:
            combo.set(current_val)
            combo.pack(side=tk.LEFT)

        def show_token_mapping(token, label, event=None):
            mode = mode_var.get() if mode_var.get() in ("Hold", "Tap") else "Hold"
            mode_var.set(mode)
            mode_btn.config(text=mode)
            CONFIG.set_mapping_setting_scoped(key, f"Custom[{mode}]:{token}", mapping_scope)
            sync_joystick_direction(f"Custom[{mode}]:{token}")
            entry.config(state="normal")
            entry.delete(0, tk.END)
            entry.insert(0, label)
            entry.config(state="readonly")
            combo.pack_forget()
            custom_frame.pack(side=tk.LEFT)
            self.on_setting_changed(event)

        def on_combo_selected(event):
            if combo.get() == "Custom":
                combo.pack_forget()
                custom_frame.pack(side=tk.LEFT)
                self.start_custom_recording(key, entry, combo, custom_frame, mode_var, mapping_scope)
            elif combo.get() == GYRO_LOCK_LABEL:
                show_token_mapping(GYRO_LOCK_TOKEN, GYRO_LOCK_LABEL, event)
            elif combo.get() == MODE_SHIFT_LABEL:
                show_token_mapping(MODE_SHIFT_TOKEN, MODE_SHIFT_LABEL, event)
            elif combo.get() == "Change Profile":
                show_change_profile(event)
            else:
                self.on_setting_changed(event)

        combo.bind("<<ComboboxSelected>>", on_combo_selected)
        setattr(self, f"{attr_key}_combo", combo)
        setattr(self, f"{attr_key}_custom_frame", custom_frame)
        setattr(self, f"{attr_key}_entry", entry)
        setattr(self, f"{attr_key}_mode_btn", mode_btn)
        setattr(self, f"{attr_key}_mode_var", mode_var)
        setattr(self, f"{attr_key}_cp_frame", cp_frame)

    def create_joystick_mapping_widget(self, parent, key, label_text, mapping_scope=None):
        suffix = self._mapping_scope_suffix(mapping_scope)
        attr_key = self._mapping_attr(key, suffix)
        parent_bg = parent.cget("bg") if hasattr(parent, "cget") else background_color
        tk.Label(parent, text=label_text, bg=parent_bg, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT, padx=(int(5 * scaling_factor), int(2 * scaling_factor)))
        container = tk.Frame(parent, bg=parent_bg)
        container.pack(side=tk.LEFT, padx=int(2 * scaling_factor))

        combo = ttk.Combobox(container, values=JOYSTICK_OPTIONS, font=scale_font(("Arial", 11, "bold")), state="readonly", width=11, justify="center")
        custom_frame = tk.Frame(container, bg=parent_bg)
        scroll_activation_var = tk.StringVar(value=CONFIG.get_joystick_setting_scoped(key, "scroll_activation", "Hold", mapping_scope))

        def toggle_scroll_activation():
            new_mode = "Tap" if scroll_activation_var.get() == "Hold" else "Hold"
            scroll_activation_var.set(new_mode)
            scroll_mode_btn.config(text=new_mode)
            CONFIG.set_joystick_setting_scoped(key, "scroll_activation", new_mode, mapping_scope)
            CONFIG.save_config()

        scroll_mode_btn = tk.Button(custom_frame, text=scroll_activation_var.get(), bg=button_gray, fg="white", font=scale_font(("Arial", 9, "bold")), bd=0, relief=tk.FLAT, command=toggle_scroll_activation, width=4)
        custom_btn = tk.Button(custom_frame, text="Custom", bg=button_gray, fg="white", font=scale_font(("Arial", 10, "bold")), bd=0, relief=tk.FLAT)
        custom_btn.pack(side=tk.LEFT, fill=tk.Y)

        def open_current_popup():
            mode = CONFIG.get_mapping_setting_scoped(key, "Default", mapping_scope)
            if mode == "Mouse":
                self.root.after(50, lambda: self.open_joystick_mouse_popup(key, custom_btn, mapping_scope))
            elif mode == "Scroll Wheel":
                self.root.after(50, lambda: self.open_joystick_scroll_popup(key, custom_btn, mapping_scope))
            else:
                self.root.after(50, lambda: self.open_joystick_custom_popup(key, custom_btn, mapping_scope))

        custom_btn.config(command=open_current_popup)

        def close_custom():
            scroll_mode_btn.pack_forget()
            custom_frame.pack_forget()
            combo.pack(side=tk.LEFT)
            combo.set("Default")
            CONFIG.set_mapping_setting_scoped(key, "Default", mapping_scope)
            self.on_setting_changed()

        close_btn = tk.Button(custom_frame, text="X", bg="#ff4444", fg="white", font=scale_font(("Arial", 10, "bold")), bd=0, relief=tk.FLAT, command=close_custom)
        close_btn.pack(side=tk.LEFT, padx=(int(2 * scaling_factor), 0), fill=tk.Y)

        def show_current():
            current_val = CONFIG.get_mapping_setting_scoped(key, "Default", mapping_scope)
            if current_val in ("Custom", "Mouse", "Scroll Wheel"):
                combo.pack_forget()
                custom_frame.pack(side=tk.LEFT)
                if current_val == "Scroll Wheel":
                    scroll_activation_var.set(CONFIG.get_joystick_setting_scoped(key, "scroll_activation", "Hold", mapping_scope))
                    scroll_mode_btn.config(text=scroll_activation_var.get())
                    scroll_mode_btn.pack(side=tk.LEFT, padx=(0, int(2 * scaling_factor)), fill=tk.Y, before=custom_btn)
                else:
                    scroll_mode_btn.pack_forget()
                custom_btn.config(text=current_val)
                combo.set(current_val)
            else:
                scroll_mode_btn.pack_forget()
                custom_frame.pack_forget()
                combo.pack(side=tk.LEFT)
                combo.set(current_val if current_val in JOYSTICK_OPTIONS else "Default")

        def on_combo_selected(event):
            selected = combo.get()
            CONFIG.set_mapping_setting_scoped(key, selected, mapping_scope)
            CONFIG.save_config()
            if selected in ("Custom", "Mouse", "Scroll Wheel"):
                combo.pack_forget()
                custom_frame.pack(side=tk.LEFT)
                if selected == "Scroll Wheel":
                    scroll_activation_var.set(CONFIG.get_joystick_setting_scoped(key, "scroll_activation", "Hold", mapping_scope))
                    scroll_mode_btn.config(text=scroll_activation_var.get())
                    scroll_mode_btn.pack(side=tk.LEFT, padx=(0, int(2 * scaling_factor)), fill=tk.Y, before=custom_btn)
                else:
                    scroll_mode_btn.pack_forget()
                custom_btn.config(text=selected)
                self.root.update_idletasks()
                open_current_popup()
            else:
                self.on_setting_changed(event)

        combo.bind("<<ComboboxSelected>>", on_combo_selected)
        show_current()
        setattr(self, f"{attr_key}_combo", combo)
        setattr(self, f"{attr_key}_custom_frame", custom_frame)
        setattr(self, f"{attr_key}_custom_btn", custom_btn)
        setattr(self, f"{attr_key}_scroll_mode_btn", scroll_mode_btn)
        setattr(self, f"{attr_key}_scroll_activation_var", scroll_activation_var)

    def _event_in_widget(self, widget, event):
        # True if a <ButtonPress> landed inside the given widget (used so an outside-click
        # handler can ignore clicks on the button that owns the popup, letting that button's
        # own command toggle the popup closed instead of close-then-reopen flashing).
        if widget is None or not widget.winfo_exists():
            return False
        wx, wy = widget.winfo_rootx(), widget.winfo_rooty()
        return wx <= event.x_root <= wx + widget.winfo_width() and wy <= event.y_root <= wy + widget.winfo_height()

    def _toggle_joystick_popup(self, anchor_widget):
        # If the joystick popup is already open for this same anchor, close it and report
        # that the caller should abort (so re-clicking the anchor just closes the popup).
        existing = getattr(self, "joystick_custom_popup", None)
        if existing is not None and existing.winfo_exists() and getattr(self, "joystick_custom_popup_anchor", None) is anchor_widget:
            self.close_joystick_custom_popup()
            return True
        return False

    def close_joystick_custom_popup(self):
        popup = getattr(self, "joystick_custom_popup", None)
        if popup is not None and popup.winfo_exists():
            popup.destroy()
        self.joystick_custom_popup = None
        self.joystick_custom_popup_anchor = None
        bind_id = getattr(self, "joystick_custom_popup_bind_id", None)
        if bind_id:
            try:
                self.root.unbind("<ButtonPress>", bind_id)
            except:
                pass
            self.joystick_custom_popup_bind_id = None

    def bind_joystick_custom_popup_outside_click(self):
        popup = getattr(self, "joystick_custom_popup", None)
        if popup is None or not popup.winfo_exists():
            return
        bind_id = getattr(self, "joystick_custom_popup_bind_id", None)
        if bind_id:
            try:
                self.root.unbind("<ButtonPress>", bind_id)
            except:
                pass
            self.joystick_custom_popup_bind_id = None

        def close_if_outside(event):
            current_popup = getattr(self, "joystick_custom_popup", None)
            if current_popup is None or not current_popup.winfo_exists():
                self.close_joystick_custom_popup()
                return
            if self._event_in_widget(current_popup, event):
                return
            # Leave clicks on the owning anchor to its command (toggles the popup closed).
            if self._event_in_widget(getattr(self, "joystick_custom_popup_anchor", None), event):
                return
            self.close_joystick_custom_popup()

        self.joystick_custom_popup_bind_id = self.root.bind("<ButtonPress>", close_if_outside, add="+")

    def open_joystick_custom_popup(self, key, anchor_widget, mapping_scope=None):
        if self._toggle_joystick_popup(anchor_widget):
            return
        self.close_joystick_custom_popup()

        spacing = int(10 * scaling_factor)
        cell_padx = int(2 * scaling_factor)  # container.pack padx inside create_mapping_widget
        popup = tk.Frame(self.root, bg=background_color, bd=1, relief=tk.SOLID, padx=spacing - cell_padx, pady=spacing)
        self.joystick_custom_popup = popup
        self.joystick_custom_popup_anchor = anchor_widget

        self.root.update_idletasks()
        popup.place(in_=anchor_widget, relx=0, rely=1, x=-int(3 * scaling_factor), y=int(2 * scaling_factor), anchor=tk.NW)
        popup.lift()
        inner = tk.Frame(popup, bg=background_color)
        inner.pack(side=tk.TOP)

        values = CONFIG.get_joystick_custom_scoped(key, mapping_scope)
        # grid layout: col 0=left labels, col 1=left combos, col 2=right labels, col 3=right combos
        layout = [
            ("up",    "Up:",    0, 0),
            ("down",  "Down:",  0, 2),
            ("left",  "Left:",  1, 0),
            ("right", "Right:", 1, 2),
        ]
        row_widgets = {}

        def save_direction(direction, value):
            current = CONFIG.get_joystick_custom_scoped(key, mapping_scope)
            current[direction] = value
            CONFIG.set_joystick_custom_scoped(key, current, mapping_scope)
            CONFIG.save_config()

        row_pady = spacing
        col_gap  = int(8 * scaling_factor)

        for direction, label_text, grow, lcol in layout:
            pady = (0, row_pady) if grow == 0 else 0
            lpadx = (col_gap, cell_padx) if lcol == 2 else (0, cell_padx)
            tk.Label(inner, text=label_text, bg=background_color, fg=text_color,
                     font=scale_font(("Arial", 11, "bold")), anchor=tk.E).grid(
                         row=grow, column=lcol, sticky=tk.E, padx=lpadx, pady=pady)
            cell = tk.Frame(inner, bg=background_color)
            cell.grid(row=grow, column=lcol + 1, sticky=tk.W, pady=pady)
            self.create_mapping_widget(cell, f"{key}_{direction}", "", mapping_scope)
            combo = getattr(self, f"{self._mapping_attr(f'{key}_{direction}', self._mapping_scope_suffix(mapping_scope))}_combo")
            combo.set(values.get(direction, "Default"))
            row_widgets[direction] = combo

        for direction, combo in row_widgets.items():
            combo.bind("<<ComboboxSelected>>", lambda e, d=direction, c=combo: save_direction(d, c.get()), add="+")

        def enable_outside_click_close():
            if popup.winfo_exists():
                self.bind_joystick_custom_popup_outside_click()

        popup.update_idletasks()
        self.root.after(100, enable_outside_click_close)

    def _create_joystick_option_popup(self, anchor_widget, defer_place=False):
        self.close_joystick_custom_popup()
        spacing = int(10 * scaling_factor)
        popup = tk.Frame(self.root, bg=background_color, bd=1, relief=tk.SOLID, padx=spacing, pady=spacing)
        self.joystick_custom_popup = popup
        self.joystick_custom_popup_anchor = anchor_widget
        if not defer_place:
            self.root.update_idletasks()
            popup.place(in_=anchor_widget, relx=0, rely=1, x=-int(3 * scaling_factor), y=int(2 * scaling_factor), anchor=tk.NW)
            popup.lift()
        return popup

    def _place_popup_within_root_bounds(self, popup, anchor_widget):
        self.root.update_idletasks()
        popup.update_idletasks()

        anchor_x = anchor_widget.winfo_rootx()
        anchor_bottom = anchor_widget.winfo_rooty() + anchor_widget.winfo_height()
        root_right = self.root.winfo_rootx() + self.root.winfo_width()
        root_bottom = self.root.winfo_rooty() + self.root.winfo_height()

        popup_w = popup.winfo_reqwidth()
        popup_h = popup.winfo_reqheight()
        x_offset = int(3 * scaling_factor)
        y_offset = int(2 * scaling_factor)

        enough_right = anchor_x - x_offset + popup_w <= root_right
        enough_bottom = anchor_bottom + y_offset + popup_h <= root_bottom

        if enough_right and enough_bottom:
            popup.place(in_=anchor_widget, relx=0, rely=1, x=-x_offset, y=y_offset, anchor=tk.NW)
        elif not enough_right and enough_bottom:
            popup.place(in_=anchor_widget, relx=1, rely=1, x=x_offset, y=y_offset, anchor=tk.NE)
        elif enough_right and not enough_bottom:
            popup.place(in_=anchor_widget, relx=0, rely=0, x=-x_offset, y=-y_offset, anchor=tk.SW)
        else:
            popup.place(in_=anchor_widget, relx=1, rely=0, x=x_offset, y=-y_offset, anchor=tk.SE)
        popup.lift()

    def open_change_profile_popup(self, anchor_widget):
        if self._toggle_joystick_popup(anchor_widget):
            return
        popup = self._create_joystick_option_popup(anchor_widget, defer_place=True)
        row = tk.Frame(popup, bg=background_color)
        row.pack(side=tk.TOP, fill=tk.X)
        tk.Label(row, text="Select & Change Profile:", bg=background_color, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT, padx=(0, int(5 * scaling_factor)))

        def set_change_profile_mode(val):
            CONFIG.change_profile_mode = val
            CONFIG.save_config()

        switch = ToggleSwitch(row, ["Auto", "Manual"], ["Auto", "Manual"], getattr(CONFIG, "change_profile_mode", "Manual"), set_change_profile_mode, background_color)
        switch.pack(side=tk.LEFT)
        popup.update_idletasks()
        self._place_popup_within_root_bounds(popup, anchor_widget)
        self.root.after(100, self.bind_joystick_custom_popup_outside_click)

    def close_back_button_popup(self):
        popup = getattr(self, "back_button_popup", None)
        if popup is not None and popup.winfo_exists():
            try:
                popup.place_forget()
                self.root.update_idletasks()
            except Exception:
                pass
            popup.destroy()
        self.back_button_popup = None
        self.back_button_popup_anchor = None
        bind_id = getattr(self, "back_button_popup_bind_id", None)
        if bind_id:
            try:
                self.root.unbind("<ButtonPress>", bind_id)
            except:
                pass
            self.back_button_popup_bind_id = None

    def bind_back_button_popup_outside_click(self):
        popup = getattr(self, "back_button_popup", None)
        if popup is None or not popup.winfo_exists():
            return
        bind_id = getattr(self, "back_button_popup_bind_id", None)
        if bind_id:
            try:
                self.root.unbind("<ButtonPress>", bind_id)
            except:
                pass
            self.back_button_popup_bind_id = None

        def close_if_outside(event):
            current_popup = getattr(self, "back_button_popup", None)
            if current_popup is None or not current_popup.winfo_exists():
                self.close_back_button_popup()
                return
            px, py = current_popup.winfo_rootx(), current_popup.winfo_rooty()
            pw, ph = current_popup.winfo_width(), current_popup.winfo_height()
            if px <= event.x_root <= px + pw and py <= event.y_root <= py + ph:
                return
            # A click on the owning selector is left for its command to toggle the popup
            # closed, so it isn't closed here and immediately reopened (which would flash).
            anchor = getattr(self, "back_button_popup_anchor", None)
            if anchor is not None and anchor.winfo_exists():
                ax, ay = anchor.winfo_rootx(), anchor.winfo_rooty()
                aw, ah = anchor.winfo_width(), anchor.winfo_height()
                if ax <= event.x_root <= ax + aw and ay <= event.y_root <= ay + ah:
                    return
            self.close_back_button_popup()

        self.back_button_popup_bind_id = self.root.bind("<ButtonPress>", close_if_outside, add="+")

    def open_back_button_popup(self, selector):
        # Floating, categorized replacement for the Back Button Option dropdown. Opened
        # by a BackButtonSelector; positioned like the Change Profile popup.
        from config import BACK_BUTTON_CATEGORIES, back_button_label

        # Clicking the selector whose popup is already open toggles it closed (the outside-
        # click handler leaves the selector alone so this command does the closing).
        existing = getattr(self, "back_button_popup", None)
        if existing is not None and existing.winfo_exists() and getattr(self, "back_button_popup_anchor", None) is selector:
            self.close_back_button_popup()
            return
        self.close_back_button_popup()

        spacing = int(10 * scaling_factor)
        column_gap = int(8 * scaling_factor)
        btn_gap = int(5 * scaling_factor)

        popup = tk.Frame(self.root, bg=background_color, bd=1, relief=tk.SOLID, padx=column_gap, pady=spacing)
        self.back_button_popup = popup
        self.back_button_popup_anchor = selector

        header_font = scale_font(("Arial", 9, "bold"))
        btn_font = scale_font(("Arial", 9, "bold"))
        measure = tkFont.Font(font=btn_font)

        # All option buttons share one size, wide enough for the longest label.
        max_label_w = 0
        for _title, rows in BACK_BUTTON_CATEGORIES:
            for row in rows:
                for token in row:
                    max_label_w = max(max_label_w, measure.measure(back_button_label(token)))
        btn_w = max_label_w + int(16 * scaling_factor)
        btn_h = measure.metrics("linespace") + int(10 * scaling_factor)

        current_value = selector.get()

        def choose(token):
            selector.select_value(token)
            self.close_back_button_popup()

        # Category blocks: each defined row becomes a vertical column of buttons. General
        # sits top-left with Switch Input directly beneath it; the remaining small
        # categories share the top row to its right. Inter-category spacing matches the
        # gap between buttons: every cell carries a trailing btn_gap on its right/bottom,
        # so packing the blocks flush leaves exactly one btn_gap between categories.
        cats = dict(BACK_BUTTON_CATEGORIES)
        body = tk.Frame(popup, bg=background_color)
        body.pack(side=tk.TOP, anchor=tk.N)

        def render_category(parent, title):
            cat_frame = tk.Frame(parent, bg=background_color)
            cat_frame.pack(side=tk.LEFT, anchor=tk.N)
            tk.Label(cat_frame, text=title, bg=background_color, fg=text_color,
                     font=header_font, anchor=tk.W).pack(side=tk.TOP, anchor=tk.W, pady=(0, btn_gap))
            block = tk.Frame(cat_frame, bg=background_color)
            block.pack(side=tk.TOP, anchor=tk.W)
            for c_idx, col in enumerate(cats[title]):
                for r_idx, token in enumerate(col):
                    is_sel = (token == current_value)
                    cell = tk.Frame(block, bg=highlight_color if is_sel else background_color,
                                    width=btn_w, height=btn_h)
                    cell.grid(row=r_idx, column=c_idx, padx=(0, btn_gap), pady=(0, btn_gap), sticky="nsew")
                    cell.grid_propagate(False)
                    bd = int(2 * scaling_factor) if is_sel else 0
                    btn = tk.Button(cell, text=back_button_label(token), font=btn_font,
                                    bg=button_gray, fg="white", relief=tk.FLAT, bd=0,
                                    highlightthickness=0, takefocus=0,
                                    activebackground=highlight_color, activeforeground="white",
                                    command=lambda t=token: choose(t))
                    btn.place(x=bd, y=bd, width=btn_w - 2 * bd, height=btn_h - 2 * bd)

        top_row = tk.Frame(body, bg=background_color)
        top_row.pack(side=tk.TOP, anchor=tk.W)
        for title in ("General", "In-app Gyro", "PS Input", "Windows"):
            render_category(top_row, title)

        bottom_row = tk.Frame(body, bg=background_color)
        bottom_row.pack(side=tk.TOP, anchor=tk.W)
        render_category(bottom_row, "Switch Input")

        # Pre-realize and paint the popup off-screen so every button is already drawn with
        # its gray background before the popup appears at the anchor. Mapping the buttons
        # directly at their final spot is what makes them flash their default (white)
        # background for one frame; painting off-screen first avoids that.
        popup.place(in_=self.root, x=-10000, y=-10000)
        popup.update_idletasks()
        self._place_popup_within_root_bounds(popup, selector)
        self.root.after(100, self.bind_back_button_popup_outside_click)

    def open_joystick_mouse_popup(self, key, anchor_widget, mapping_scope=None):
        if self._toggle_joystick_popup(anchor_widget):
            return
        popup = self._create_joystick_option_popup(anchor_widget)
        row = tk.Frame(popup, bg=background_color)
        row.pack(side=tk.TOP, fill=tk.X)
        tk.Label(row, text="Sensitivity:", bg=background_color, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT, padx=(0, int(5 * scaling_factor)))
        def update_mouse_sensitivity(val):
            CONFIG.set_joystick_setting_scoped(key, "mouse_sensitivity", float(val), mapping_scope)
            if mapping_scope == "in_app_gyro_mode_mappings" and hasattr(self, "stick_scale"):
                self.stick_scale.set(float(val))
            CONFIG.save_config()
        scale = tk.Scale(
            row,
            from_=0,
            to=10,
            resolution=0.2,
            orient=tk.HORIZONTAL,
            length=int(120 * scaling_factor),
            bg=background_color,
            fg=text_color,
            troughcolor=button_gray,
            activebackground=highlight_color,
            highlightthickness=0,
            bd=0,
            sliderrelief=tk.FLAT,
            sliderlength=int(15 * scaling_factor),
            width=int(15 * scaling_factor),
            font=scale_font(("Arial", 11, "bold")),
            command=update_mouse_sensitivity
        )
        scale.set(float(CONFIG.get_joystick_setting_scoped(key, "mouse_sensitivity", 5.0, mapping_scope)))
        scale.pack(side=tk.LEFT)
        popup.update_idletasks()
        self.root.after(100, self.bind_joystick_custom_popup_outside_click)

    def open_joystick_scroll_popup(self, key, anchor_widget, mapping_scope=None):
        if self._toggle_joystick_popup(anchor_widget):
            return
        popup = self._create_joystick_option_popup(anchor_widget)
        row = tk.Frame(popup, bg=background_color)
        row.pack(side=tk.TOP, fill=tk.X)
        tk.Label(row, text="Scroll Wheel Mode:", bg=background_color, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT, padx=(0, int(5 * scaling_factor)))

        mode = CONFIG.get_joystick_setting_scoped(key, "scroll_mode", "Up/Down", mapping_scope)
        switch = ToggleSwitch(
            row,
            ["Up/Down", "Up/Down/Left/Right"],
            ["Up/Down", "Up/Down/Left/Right"],
            mode,
            lambda val: (CONFIG.set_joystick_setting_scoped(key, "scroll_mode", val, mapping_scope), CONFIG.save_config()),
            background_color,
            widths=[10, 20]
        )
        switch.pack(side=tk.LEFT)
        popup.update_idletasks()
        self.root.after(100, self.bind_joystick_custom_popup_outside_click)

    def _format_profile_combo_input(self, value):
        if not value:
            return "None"
        display = value
        if display.startswith("Custom[Tap]:"):
            display = display[12:]
        elif display.startswith("Custom[Hold]:"):
            display = display[13:]
        elif display.startswith("Custom:"):
            display = display[7:]
        return format_input_display(display)

    def _set_profile_button_text(self):
        if hasattr(self, "profile_button"):
            self.profile_button.config(text=getattr(CONFIG, "active_profile", "Default"))

    def on_popup_profile_selected(self, profile_name, name_frame, name_btn, frame_w, frame_h):
        # Selecting a profile in the popup: 1) move the highlight border onto the new
        # profile, 2) close the popup cleanly, 3) then run the actual profile switch.
        if not profile_name or profile_name == getattr(CONFIG, "active_profile", ""):
            self.close_profile_popup()
            return
        border = 2
        try:
            name_frame.config(bg=highlight_color)
            name_btn.place_configure(
                x=border, y=border,
                width=max(1, frame_w - border * 2),
                height=max(1, frame_h - border * 2),
            )
            self.root.update_idletasks()
        except Exception:
            pass

        def _close_popup():
            # Close exactly like the outside-click path: just close and return to the
            # event loop so the OS paints the clean close with nothing competing.
            self.close_profile_popup()
            # Run the (heavier) switch only after the close has had a full cycle to
            # paint; running it in the same idle tick made the close tear down in
            # stripes as the switch's repaints interleaved.
            self.root.after(50, lambda: self.switch_to_profile(profile_name))

        # Brief delay so the new highlight is actually visible before the popup closes.
        self.root.after(50, _close_popup)

    def close_profile_popup(self):
        popup = getattr(self, "profile_popup", None)
        if popup is not None and popup.winfo_exists():
            # Unmap the whole popup in one step (and repaint the revealed area once)
            # before destroying it, so it disappears cleanly instead of tearing down
            # its child widgets piecewise (which looked like a striped/segmented close).
            try:
                popup.place_forget()
                self.root.update_idletasks()
            except Exception:
                pass
            popup.destroy()
        self.profile_popup = None
        self.profile_popup_anchor = None
        bind_id = getattr(self, "profile_popup_bind_id", None)
        if bind_id:
            try:
                self.root.unbind("<ButtonPress>", bind_id)
            except:
                pass
            self.profile_popup_bind_id = None

    def bind_profile_popup_outside_click(self):
        popup = getattr(self, "profile_popup", None)
        if popup is None or not popup.winfo_exists():
            return

        bind_id = getattr(self, "profile_popup_bind_id", None)
        if bind_id:
            try:
                self.root.unbind("<ButtonPress>", bind_id)
            except:
                pass
            self.profile_popup_bind_id = None

        def close_if_outside(event):
            current_popup = getattr(self, "profile_popup", None)
            if current_popup is None or not current_popup.winfo_exists():
                self.close_profile_popup()
                return
            if self._event_in_widget(current_popup, event):
                return
            # Leave clicks on the Profile button to its command (toggles the popup closed).
            if self._event_in_widget(getattr(self, "profile_popup_anchor", None), event):
                return
            self.close_profile_popup()

        self.profile_popup_bind_id = self.root.bind("<ButtonPress>", close_if_outside, add="+")

    def _record_profile_combo_input(self, button, clear_button, save_callback, unique_profile=None):
        import utils
        button.config(text="Recording...")
        button.focus_set()
        if clear_button:
            if not clear_button.winfo_ismapped():
                clear_button.pack(side=tk.LEFT, padx=(int(2 * scaling_factor), 0), fill=tk.Y)

        pressed_keys = set()
        recorded_seq = []
        self.recording_controllers = True
        self.recorded_controller_buttons = set()
        self.controller_buttons_pressed = False
        self.waiting_for_controller_release = True

        def cleanup_recording_binds():
            self.recording_controllers = False
            if getattr(utils, "profile_combo_record_callback", None) is handle_controller_profile_combo:
                utils.profile_combo_record_callback = None
            self.root.unbind("<KeyPress>")
            self.root.unbind("<KeyRelease>")
            self.root.unbind("<ButtonPress>")
            self.root.unbind("<ButtonRelease>")
            self.root.unbind("<MouseWheel>")
            self.root.unbind("<FocusOut>")

        def restore_clear_button(value_exists):
            if clear_button:
                default_command = getattr(clear_button, "profile_combo_clear_command", None)
                if default_command:
                    clear_button.config(command=default_command)
                if value_exists:
                    if not clear_button.winfo_ismapped():
                        clear_button.pack(side=tk.LEFT, padx=(int(2 * scaling_factor), 0), fill=tk.Y)
                else:
                    clear_button.pack_forget()

        def cancel_recording():
            if not getattr(self, 'recording_controllers', False):
                return
            cleanup_recording_binds()
            save_callback("")
            button.config(text="None")
            restore_clear_button(False)
            CONFIG.save_config()
            self.root.after(100, self.bind_profile_popup_outside_click)

        if clear_button:
            clear_button.config(command=cancel_recording)

        def set_recorded_value(seq):
            final_seq = []
            for k in seq:
                if k in ("VK_CONTROL", "VK_CONTROL_L", "VK_CONTROL_R", "VK_LCONTROL", "VK_RCONTROL"):
                    nk = "VK_CONTROL"
                elif k in ("VK_SHIFT", "VK_SHIFT_L", "VK_SHIFT_R", "VK_LSHIFT", "VK_RSHIFT"):
                    nk = "VK_SHIFT"
                elif k in ("VK_MENU", "VK_ALT", "VK_ALT_L", "VK_ALT_R", "VK_LMENU", "VK_RMENU"):
                    nk = "VK_MENU"
                elif k in ("VK_WIN", "VK_LWIN", "VK_RWIN", "VK_WIN_L", "VK_WIN_R"):
                    nk = "VK_LWIN"
                else:
                    nk = k
                if nk not in final_seq:
                    final_seq.append(nk)
            value = "+".join(final_seq)
            if unique_profile and value:
                for profile_name, profile_data in CONFIG.profiles.items():
                    if profile_name != unique_profile and profile_data.get("profile_switching_combo", "") == value:
                        self.root.after(100, lambda: self._record_profile_combo_input(button, clear_button, save_callback, unique_profile))
                        return
            save_callback(value)
            button.config(text=self._format_profile_combo_input(value))
            if clear_button:
                if value:
                    restore_clear_button(True)
                else:
                    restore_clear_button(False)

        def end_recording():
            cleanup_recording_binds()
            if not recorded_seq:
                save_callback("")
                button.config(text="None")
                restore_clear_button(False)
            else:
                set_recorded_value(recorded_seq)
            CONFIG.save_config()
            self.root.after(100, self.bind_profile_popup_outside_click)

        def check_release():
            if not pressed_keys and not getattr(self, 'controller_buttons_pressed', False):
                if not recorded_seq and not self.recorded_controller_buttons:
                    return
                end_recording()

        def handle_controller_profile_combo(states):
            if not getattr(self, 'recording_controllers', False):
                return
            any_pressed = any(bool(v) for v in states.values())
            if getattr(self, 'waiting_for_controller_release', False):
                if not any_pressed:
                    self.waiting_for_controller_release = False
                return
            if any_pressed:
                for btn_name, pressed in states.items():
                    if pressed:
                        token = f"BTN_{btn_name}"
                        self.recorded_controller_buttons.add(token)
                        if token not in recorded_seq:
                            recorded_seq.append(token)
            self.controller_buttons_pressed = any_pressed
            if not any_pressed and self.recorded_controller_buttons and not pressed_keys:
                end_recording()

        def on_key_press(e):
            if self.recorded_controller_buttons:
                return "break"
            vk = e.keysym.upper()
            pressed_keys.add(f"VK_{vk}")
            if f"VK_{vk}" not in recorded_seq:
                recorded_seq.append(f"VK_{vk}")
            return "break"

        def on_key_release(e):
            vk = e.keysym.upper()
            pressed_keys.discard(f"VK_{vk}")
            check_release()
            return "break"

        def on_mouse_press(e):
            # Clicking the [X] button during recording cancels and reverts to None
            # instead of recording the click as a mouse-button input.
            if clear_button is not None and e.widget is clear_button:
                cancel_recording()
                return "break"
            if self.recorded_controller_buttons:
                return "break"
            btn = f"MB_{e.num}"
            pressed_keys.add(btn)
            if btn not in recorded_seq:
                recorded_seq.append(btn)
            return "break"

        def on_mouse_release(e):
            btn = f"MB_{e.num}"
            pressed_keys.discard(btn)
            check_release()
            return "break"

        def on_mouse_wheel(e):
            if self.recorded_controller_buttons:
                return "break"
            dir_str = "UP" if e.delta > 0 else "DOWN"
            if f"MW_{dir_str}" not in recorded_seq:
                recorded_seq.append(f"MW_{dir_str}")
            self.root.after(100, check_release)
            return "break"

        self.root.bind("<KeyPress>", on_key_press)
        self.root.bind("<KeyRelease>", on_key_release)
        self.root.bind("<ButtonPress>", on_mouse_press)
        self.root.bind("<ButtonRelease>", on_mouse_release)
        self.root.bind("<MouseWheel>", on_mouse_wheel)
        utils.profile_combo_record_callback = handle_controller_profile_combo

        def on_focus_out(e):
            if e.widget == self.root and getattr(self, 'recording_controllers', False):
                try:
                    if self.root.focus_get():
                        return
                except:
                    pass
                if not self.recorded_controller_buttons:
                    for vk in range(8, 255):
                        try:
                            if ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000:
                                if 65 <= vk <= 90 or 48 <= vk <= 57:
                                    token = f"VK_{chr(vk)}"
                                    if token not in recorded_seq:
                                        recorded_seq.append(token)
                        except:
                            pass
                end_recording()
        self.root.bind("<FocusOut>", on_focus_out)

        def poll_controller():
            if not getattr(self, 'recording_controllers', False):
                return
            any_pressed = False
            reverse_map = {v: k for k, v in SWITCH_BUTTONS.items() if k not in ["Capture", "PS_C_Click"]}
            for vc in getattr(self, 'current_controllers', []):
                if vc is None:
                    continue
                for c in vc.controllers:
                    raw = getattr(c, 'raw_buttons', 0)
                    if raw:
                        any_pressed = True
                        if not getattr(self, 'waiting_for_controller_release', False) and not self.recorded_controller_buttons:
                            for bit, btn_name in reverse_map.items():
                                if raw & bit:
                                    token = f"BTN_{btn_name}"
                                    self.recorded_controller_buttons.add(token)
                                    if token not in recorded_seq:
                                        recorded_seq.append(token)
            if getattr(self, 'waiting_for_controller_release', False):
                if not any_pressed:
                    self.waiting_for_controller_release = False
            else:
                self.controller_buttons_pressed = any_pressed
                if not any_pressed and self.recorded_controller_buttons and not pressed_keys:
                    end_recording()
                    return
            self.root.after(50, poll_controller)

        poll_controller()

    def _create_profile_combo_input_widget(self, parent, value_getter, value_setter, unique_profile=None, fill_width=False):
        frame = tk.Frame(parent, bg=background_color)
        btn = tk.Button(
            frame,
            text=self._format_profile_combo_input(value_getter()),
            font=scale_font(("Arial", 10, "bold")),
            bg=button_gray,
            fg="white",
            relief=tk.FLAT,
            bd=0,
            width=12 if not fill_width else 1,
            anchor=tk.CENTER
        )
        btn.pack(side=tk.LEFT, fill=tk.BOTH if fill_width else tk.Y, expand=fill_width)

        def save_value(value):
            value_setter(value)
            btn.config(text=self._format_profile_combo_input(value))

        def clear_value():
            save_value("")
            clear_btn.pack_forget()
            CONFIG.save_config()

        clear_btn = tk.Button(frame, text="X", bg="#ff4444", fg="white", font=scale_font(("Arial", 10, "bold")), bd=0, relief=tk.FLAT, command=clear_value)
        clear_btn.profile_combo_clear_command = clear_value
        if value_getter():
            clear_btn.pack(side=tk.LEFT, padx=(int(2 * scaling_factor), 0), fill=tk.Y)

        btn.config(command=lambda: self._record_profile_combo_input(btn, clear_btn, save_value, unique_profile))
        return frame

    def open_profile_popup(self):
        # Re-clicking the Profile button while its popup is open just closes it.
        existing = getattr(self, "profile_popup", None)
        if existing is not None and existing.winfo_exists():
            self.close_profile_popup()
            return
        self.close_profile_popup()
        self.profile_popup_anchor = getattr(self, "profile_button", None)
        spacing = int(10 * scaling_factor)
        column_gap = int(8 * scaling_factor)
        # Match the popup row/button height to the Add/Rename buttons.
        ref_btn = getattr(self, "add_profile_btn", None)
        try:
            row_height = ref_btn.winfo_reqheight() if ref_btn is not None else 0
        except Exception:
            row_height = 0
        if row_height < int(10 * scaling_factor):
            row_height = int(30 * scaling_factor)
        # Left/right gap between the text and the window border equals the gap between buttons.
        popup = tk.Frame(self.root, bg=background_color, bd=1, relief=tk.SOLID, padx=column_gap, pady=spacing)
        self.profile_popup = popup
        self.root.update_idletasks()
        popup.place(in_=self.profile_button, relx=0, rely=1, x=-2, y=2, anchor=tk.NW)
        popup.lift()

        header = tk.Frame(popup, bg=background_color)
        header.pack(side=tk.TOP, fill=tk.X)
        profile_popup_header_font = scale_font(("Arial", 10, "bold"))
        profile_col_width = int(148 * scaling_factor)
        # Add a small margin so the header Label (which needs a few px of internal
        # padding beyond the raw glyph width) isn't clipped.
        combo_col_width = tkFont.Font(font=profile_popup_header_font).measure("Profile Switching Combo") + int(8 * scaling_factor)
        change_col_width = tkFont.Font(font=profile_popup_header_font).measure("Change Profile List") + int(8 * scaling_factor)
        header_row_height = int(22 * scaling_factor)

        def configure_profile_grid(parent):
            parent.grid_columnconfigure(0, minsize=profile_col_width)
            parent.grid_columnconfigure(1, minsize=combo_col_width)
            parent.grid_columnconfigure(2, minsize=change_col_width)

        configure_profile_grid(header)
        # Give the header the same fixed-width column cells as the rows so the columns
        # line up exactly (header and rows live in different containers, so relying on
        # label width vs minsize would let columns drift and misalign the combo button
        # and the Change Profile List checkbox under their titles).
        name_hdr = tk.Frame(header, bg=background_color, width=profile_col_width, height=header_row_height)
        name_hdr.grid(row=0, column=0, sticky=tk.W, padx=(0, column_gap))
        name_hdr.grid_propagate(False)
        name_hdr.pack_propagate(False)
        tk.Label(name_hdr, text="Profile Name", bg=background_color, fg=text_color, font=profile_popup_header_font, anchor=tk.W).pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        combo_hdr = tk.Frame(header, bg=background_color, width=combo_col_width, height=header_row_height)
        combo_hdr.grid(row=0, column=1, sticky=tk.W, padx=(0, column_gap))
        combo_hdr.grid_propagate(False)
        combo_hdr.pack_propagate(False)
        tk.Label(combo_hdr, text="Profile Switching Combo", bg=background_color, fg=text_color, font=profile_popup_header_font, anchor=tk.W).pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tk.Label(header, text="Change Profile List", bg=background_color, fg=text_color, font=profile_popup_header_font, anchor=tk.W).grid(row=0, column=2, sticky=tk.W)

        # Match the row spacing to the L/R Joystick Custom popup (10px gap between rows).
        row_pady = int(5 * scaling_factor)
        max_rows = 10
        canvas_height = (row_height + row_pady * 2) * max_rows
        container = tk.Frame(popup, bg=background_color)
        container.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(container, bg=background_color, highlightthickness=0, height=canvas_height)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        scrollable = tk.Frame(canvas, bg=background_color)
        canvas_window = canvas.create_window((0, 0), window=scrollable, anchor="nw")

        def update_scroll(event=None):
            bbox = canvas.bbox("all")
            if not bbox:
                return
            canvas.configure(scrollregion=bbox)
            canvas_width = canvas.winfo_width()
            canvas.itemconfig(canvas_window, width=canvas_width)
            if scrollable.winfo_reqheight() > canvas_height:
                if not scrollbar.winfo_ismapped():
                    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
            else:
                if scrollbar.winfo_ismapped():
                    scrollbar.pack_forget()

        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollable.bind("<Configure>", update_scroll)
        canvas.bind("<Configure>", update_scroll)

        def on_mousewheel(event):
            bbox = canvas.bbox("all")
            if not bbox:
                return "break"
            if (bbox[3] - bbox[1]) <= canvas.winfo_height():
                return "break"
            direction = -1 if event.delta > 0 else 1
            canvas.yview_scroll(direction, "units")
            return "break"

        def bind_profile_mousewheel(widget):
            widget.bind("<MouseWheel>", on_mousewheel)
            for child in widget.winfo_children():
                bind_profile_mousewheel(child)

        def refresh_popup_rows():
            for child in scrollable.winfo_children():
                child.destroy()
            for row_idx, profile_name in enumerate(self.get_sorted_profiles()):
                profile_data = CONFIG.profiles.get(profile_name, {})
                row = tk.Frame(scrollable, bg=background_color, height=row_height)
                row.pack(side=tk.TOP, fill=tk.X, pady=row_pady)
                row.pack_propagate(False)
                row.grid_propagate(False)
                configure_profile_grid(row)

                is_active_profile = profile_name == CONFIG.active_profile
                name_frame = tk.Frame(row, bg=highlight_color if is_active_profile else background_color, width=profile_col_width, height=row_height)
                name_frame.grid(row=0, column=0, sticky=tk.EW, padx=(0, column_gap))
                name_frame.grid_propagate(False)
                name_btn = tk.Button(name_frame, text=profile_name, font=scale_font(("Arial", 10, "bold")), bg=button_gray, fg="white", relief=tk.FLAT, bd=0, anchor=tk.W)
                active_border = 2 if is_active_profile else 0
                name_btn.place(
                    x=active_border,
                    y=active_border,
                    width=max(1, profile_col_width - active_border * 2),
                    height=max(1, row_height - active_border * 2)
                )
                name_btn.config(command=lambda p=profile_name, nf=name_frame, nb=name_btn, w=profile_col_width, h=row_height: self.on_popup_profile_selected(p, nf, nb, w, h))
                # Hovering shows the full profile name when the fixed-width button clips it.
                Tooltip(name_btn, lambda nb=name_btn: nb.cget("text"))

                combo_cell = tk.Frame(row, bg=background_color, width=combo_col_width, height=row_height)
                combo_cell.grid(row=0, column=1, sticky=tk.EW, padx=(0, column_gap))
                combo_cell.grid_propagate(False)
                combo_widget = self._create_profile_combo_input_widget(
                    combo_cell,
                    lambda p=profile_name: CONFIG.profiles.get(p, {}).get("profile_switching_combo", ""),
                    lambda value, p=profile_name: self.set_profile_switching_combo(p, value),
                    unique_profile=profile_name,
                    fill_width=True
                )
                combo_widget.place(x=0, y=0, width=combo_col_width, height=row_height)

                checked = bool(profile_data.get("change_profile_list", False))
                check_cell = tk.Frame(row, bg=background_color, width=row_height, height=row_height)
                check_cell.grid(row=0, column=2, sticky=tk.W)
                check_cell.grid_propagate(False)
                chk_btn = tk.Button(check_cell, text="V" if checked else "", font=scale_font(("Arial", 11, "bold")), bg=button_gray, fg="white", relief=tk.FLAT, bd=0, command=lambda p=profile_name, cur=checked: self.on_profile_change_list_toggled(p, not cur, refresh_popup_rows))
                chk_btn.place(x=0, y=0, width=row_height, height=row_height)
                bind_profile_mousewheel(row)
            update_scroll()
            bind_profile_mousewheel(popup)

        self.refresh_profile_popup_rows = refresh_popup_rows
        refresh_popup_rows()
        popup.update_idletasks()
        self.root.after(100, self.bind_profile_popup_outside_click)

    def on_profile_change_list_toggled(self, profile_name, enabled, refresh_callback=None):
        if profile_name in CONFIG.profiles:
            CONFIG.profiles[profile_name]["change_profile_list"] = bool(enabled)
            CONFIG.save_config()
            if refresh_callback:
                refresh_callback()

    def set_profile_switching_combo(self, profile_name, value):
        if profile_name in CONFIG.profiles:
            CONFIG.profiles[profile_name]["profile_switching_combo"] = value
            CONFIG.save_config()

    def init_settings_panel(self):
        self.settings_frame = tk.Frame(self.root, bg=background_color)
        self.settings_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(int(5 * scaling_factor), 0))

        def left_row(pady=None):
            row = tk.Frame(self.settings_frame, bg=background_color)
            if pady is None:
                pady = int(5 * scaling_factor)
            row.pack(side=tk.TOP, fill=tk.X, pady=pady)
            inner = tk.Frame(row, bg=background_color)
            inner.pack(side=tk.LEFT, anchor=tk.W)
            return inner

        def left_tab_row():
            row = tk.Frame(self.settings_frame, bg=background_color)
            row.pack(side=tk.TOP, fill=tk.X, pady=(int(18 * scaling_factor), 0))
            inner = tk.Frame(row, bg=background_color)
            inner.pack(side=tk.LEFT, anchor=tk.W)
            return inner

        row_profile = left_row(pady=(int(18 * scaling_factor), int(5 * scaling_factor)))
        tk.Label(row_profile, text="Profile:", bg=background_color, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(5 * scaling_factor)))
        
        self.profile_button = tk.Button(
            row_profile,
            text=CONFIG.active_profile,
            font=scale_font(("Arial", 11, "bold")),
            bg=button_gray,
            fg="white",
            relief=tk.FLAT,
            bd=0,
            width=18,
            command=self.open_profile_popup
        )
        self.profile_button.pack(side=tk.LEFT, padx=int(5 * scaling_factor))
        # Hovering shows the full active-profile name when the fixed-width button clips it.
        Tooltip(self.profile_button, lambda b=self.profile_button: b.cget("text"))

        self.add_profile_btn = tk.Button(row_profile, text="Add", font=scale_font(("Arial", 11, "bold")), bg=button_gray, fg="white", relief=tk.FLAT, bd=0, command=self.on_add_profile)
        self.add_profile_btn.pack(side=tk.LEFT, padx=int(2 * scaling_factor))

        self.rename_profile_btn = tk.Button(row_profile, text="Rename", font=scale_font(("Arial", 11, "bold")), bg=button_gray, fg="white", relief=tk.FLAT, bd=0, command=self.on_rename_profile)
        self.rename_profile_btn.pack(side=tk.LEFT, padx=int(2 * scaling_factor))
        
        self.reset_profile_btn = tk.Button(row_profile, text="Reset", font=scale_font(("Arial", 11, "bold")), bg=button_gray, fg="white", relief=tk.FLAT, bd=0, command=self.on_reset_profile)
        self.reset_profile_btn.pack(side=tk.LEFT, padx=int(2 * scaling_factor))

        self.del_profile_btn = tk.Button(row_profile, text="Delete", font=scale_font(("Arial", 11, "bold")), bg=button_gray, fg="white", relief=tk.FLAT, bd=0, command=self.on_delete_profile)
        self.del_profile_btn.pack(side=tk.LEFT, padx=int(2 * scaling_factor))
        self.assigned_apps_frame = tk.Frame(row_profile, bg=background_color)
        self.assigned_apps_frame.pack(side=tk.LEFT, padx=int(2 * scaling_factor))
        self.refresh_assigned_apps_ui()

        self.profile_switch_trigger_frame = left_row(pady=int(5 * scaling_factor))
        self.refresh_profile_switching_combo_trigger_ui()

        row_global = left_row(pady=(int(18 * scaling_factor), int(5 * scaling_factor)))
        
        # Driver Switch
        tk.Label(row_global, text="Driver:", bg=background_color, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(2 * scaling_factor)))
        self.driver_switch = ToggleSwitch(row_global, ["WinUHid", "ViGEmBus", "USBIP"], ["WinUHid", "ViGEmBus", "USBIP"], getattr(CONFIG, "driver_type", "WinUHid"), self.update_driver_type_setting, background_color)
        self.driver_switch.pack(side=tk.LEFT, padx=int(5 * scaling_factor))
        
        # Emu Mode
        tk.Label(row_global, text="Emu Mode:", bg=background_color, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT, padx=(int(20 * scaling_factor), int(2 * scaling_factor)))
        
        initial_driver = getattr(CONFIG, "driver_type", "WinUHid")
        if initial_driver == "ViGEmBus":
            sim_options = ["Xbox360", "PS4"]
        elif initial_driver == "USBIP":
            sim_options = ["Switch1", "Switch2", "PS5"]
        else:
            sim_options = ["Xbox One", "PS4", "PS5"]
            
        self.sim_mode_switch = ToggleSwitch(row_global, sim_options, sim_options, getattr(CONFIG, "simulation_mode", "PS5"), self.update_sim_mode_setting, background_color)
        self.sim_mode_switch.pack(side=tk.LEFT, padx=int(5 * scaling_factor))
        
        # Layout
        tk.Label(row_global, text="Layout:", bg=background_color, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT, padx=(int(20 * scaling_factor), int(2 * scaling_factor)))
        self.layout_switch = ToggleSwitch(row_global, ["Xbox", "Switch"], ["Xbox", "Switch"], CONFIG.abxy_mode, self.update_layout_setting, background_color)
        self.layout_switch.pack(side=tk.LEFT, padx=int(5 * scaling_factor))

        row_vibration = left_row()
        tk.Label(row_vibration, text="Rumble Mode:", bg=background_color, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(2 * scaling_factor)))
        self.rumble_mode_switch = ToggleSwitch(row_vibration, ["Xbox", "Switch"], ["Xbox", "Switch"], getattr(CONFIG, "rumble_mode", "Xbox"), self.update_rumble_mode_setting, background_color)
        self.rumble_mode_switch.pack(side=tk.LEFT, padx=int(5 * scaling_factor))
        self.update_dynamic_rumble_mode_options()

        tk.Label(row_vibration, text="Strength:", bg=background_color, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT, padx=(int(20 * scaling_factor), int(2 * scaling_factor)))
        self.vibration_strength_scale = tk.Scale(row_vibration, from_=0, to=10, resolution=1, orient=tk.HORIZONTAL, length=int(120 * scaling_factor), bg=background_color, fg=text_color, troughcolor=button_gray, activebackground=highlight_color, highlightthickness=0, bd=0, sliderrelief=tk.FLAT, sliderlength=int(15 * scaling_factor), width=int(15 * scaling_factor), font=scale_font(("Arial", 11, "bold")), command=self.update_vibration_strength)
        self.vibration_strength_scale.set(getattr(CONFIG, "vibration_strength", 5))
        self.vibration_strength_scale.pack(side=tk.LEFT)

        self.vibration_frequency_label = tk.Label(row_vibration, text="Frequency:", bg=background_color, fg=text_color, font=scale_font(("Arial", 11, "bold")))
        self.vibration_frequency_scale = tk.Scale(row_vibration, from_=1, to=10, resolution=1, orient=tk.HORIZONTAL, length=int(120 * scaling_factor), bg=background_color, fg=text_color, troughcolor=button_gray, activebackground=highlight_color, highlightthickness=0, bd=0, sliderrelief=tk.FLAT, sliderlength=int(15 * scaling_factor), width=int(15 * scaling_factor), font=scale_font(("Arial", 11, "bold")), command=self.update_vibration_frequency)
        self.vibration_frequency_scale.set(getattr(CONFIG, "vibration_frequency", 10))

        self.delay_label = tk.Label(row_vibration, text="Delay:", bg=background_color, fg=text_color, font=scale_font(("Arial", 11, "bold")))
        self.delay_label.pack(side=tk.LEFT, padx=(int(20 * scaling_factor), int(2 * scaling_factor)))
        
        def validate_numeric(char):
            return char.isdigit() or char == ""
        vcmd = (self.root.register(validate_numeric), '%S')
        
        self.rumble_delay_entry = tk.Entry(row_vibration, width=4, bg=button_gray, fg=text_color, insertbackground=text_color, bd=0, relief=tk.FLAT, font=scale_font(("Arial", 11, "bold")), justify=tk.CENTER, validate="key", validatecommand=vcmd)
        self.rumble_delay_entry.insert(0, str(getattr(CONFIG, "rumble_delay_ms", 0)))
        self.rumble_delay_entry.pack(side=tk.LEFT, padx=int(2 * scaling_factor))
        self.delay_ms_label = tk.Label(row_vibration, text="ms", bg=background_color, fg=text_color, font=scale_font(("Arial", 11, "bold")))
        self.delay_ms_label.pack(side=tk.LEFT, padx=(0, int(10 * scaling_factor)))
        
        self.rumble_delay_entry.bind("<KeyRelease>", self.on_rumble_delay_changed)
        self.update_rumble_mode_ui(getattr(CONFIG, "rumble_mode", "Xbox"))

        row_tabs = left_tab_row()
        self.settings_tab_buttons = {}
        self.settings_tab_specs = [
            ("controller_mapping", "Controller Mapping"),
            ("in_app_gyro_mode_mapping", "Mode Shift Mapping"),
            ("gyro_passthrough", "Gyro Settings"),
        ]
        for idx, (tab_id, label) in enumerate(self.settings_tab_specs):
            frame = tk.Frame(row_tabs, bg=button_gray)
            frame.pack(side=tk.LEFT, padx=(0 if idx == 0 else int(2 * scaling_factor), int(2 * scaling_factor)))
            btn = tk.Button(
                frame,
                text=label,
                width=max(8, len(label) + 1),
                font=scale_font(("Arial", 11, "bold")),
                bg=button_gray,
                fg=text_color,
                relief=tk.FLAT,
                bd=0,
                highlightthickness=0,
                command=lambda t=tab_id: self.show_settings_tab(t)
            )
            btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))
            self.settings_tab_buttons[tab_id] = (btn, frame)

        self.tab_content_frame = tk.Frame(self.settings_frame, bg=tab_black, padx=int(8 * scaling_factor), pady=int(8 * scaling_factor))
        self.tab_content_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.controller_mapping_frame = tk.Frame(self.tab_content_frame, bg=tab_black)
        self.in_app_gyro_mode_mapping_frame = tk.Frame(self.tab_content_frame, bg=tab_black)

        row_mouse = tk.Frame(self.controller_mapping_frame, bg=tab_black); row_mouse.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        tk.Label(row_mouse, text="Joy-con Mouse:", bg=tab_black, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(2 * scaling_factor)))
        self.mouse_switch = ToggleSwitch(row_mouse, ["ON", "OFF"], [True, False], CONFIG.mouse_config.enabled, self.update_mouse_setting, tab_black)
        self.mouse_switch.pack(side=tk.LEFT, padx=int(5 * scaling_factor))
        tk.Label(row_mouse, text="Sensitivity:", bg=tab_black, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(2 * scaling_factor)))
        self.mouse_sens_scale = tk.Scale(row_mouse, from_=1, to=10, resolution=0.2, orient=tk.HORIZONTAL, length=int(120 * scaling_factor), bg=tab_black, fg=text_color, troughcolor=button_gray, activebackground=highlight_color, highlightthickness=0, bd=0, sliderrelief=tk.FLAT, sliderlength=int(15 * scaling_factor), width=int(15 * scaling_factor), font=scale_font(("Arial", 11, "bold")), command=self.update_mouse_sensitivity)
        self.mouse_sens_scale.set(CONFIG.mouse_config.sensitivity); self.mouse_sens_scale.pack(side=tk.LEFT)
        tk.Label(row_mouse, text="Activate Threshold:", bg=tab_black, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(2 * scaling_factor)))
        self.ir_activate_scale = tk.Scale(row_mouse, from_=1, to=3, resolution=1, orient=tk.HORIZONTAL, length=int(80 * scaling_factor), bg=tab_black, fg=text_color, troughcolor=button_gray, activebackground=highlight_color, highlightthickness=0, bd=0, sliderrelief=tk.FLAT, sliderlength=int(15 * scaling_factor), width=int(15 * scaling_factor), font=scale_font(("Arial", 11, "bold")), command=self.update_ir_activate_threshold)
        self.ir_activate_scale.set(CONFIG.mouse_config.ir_activate_threshold); self.ir_activate_scale.pack(side=tk.LEFT)

        shared_frame = tk.LabelFrame(self.controller_mapping_frame, text=" Shared Buttons & Joysticks ", bg=tab_black, fg=text_color, font=scale_font(("Arial", 11, "bold")), bd=1, relief=tk.GROOVE, padx=int(5 * scaling_factor), pady=int(5 * scaling_factor))
        shared_frame.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor), padx=(int(5 * scaling_factor), 0))

        def shared_mapping_row():
            row = tk.Frame(shared_frame, bg=tab_black)
            row.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
            return row

        row_shared_1 = shared_mapping_row()
        for key, label in [("zl", "ZL:"), ("l", "L:"), ("zr", "ZR:"), ("r", "R:")]:
            self.create_mapping_widget(row_shared_1, key, label)

        row_shared_2 = shared_mapping_row()
        for key, label in [("minus", "Minus:"), ("plus", "Plus:"), ("capt", "Capture:"), ("home", "Home:"), ("c", "Chat:")]:
            self.create_mapping_widget(row_shared_2, key, label)

        row_shared_3 = shared_mapping_row()
        self.create_joystick_mapping_widget(row_shared_3, "l_joystick", "L Joystick:")
        self.create_mapping_widget(row_shared_3, "l_stk", "L Joystick Click:")
        self.create_joystick_mapping_widget(row_shared_3, "r_joystick", "R Joystick:")
        self.create_mapping_widget(row_shared_3, "r_stk", "R Joystick Click:")

        row_shared_4 = shared_mapping_row()
        for key, label in [("a", "A:"), ("b", "B:"), ("x", "X:"), ("y", "Y:")]:
            self.create_mapping_widget(row_shared_4, key, label)

        row_shared_5 = shared_mapping_row()
        for key, label in [("up", "Up:"), ("down", "Down:"), ("left", "Left:"), ("right", "Right:")]:
            self.create_mapping_widget(row_shared_5, key, label)

        row_pro = tk.Frame(self.controller_mapping_frame, bg=tab_black); row_pro.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        tk.Label(row_pro, text="Pro Controller Back Buttons:", bg=tab_black, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(5 * scaling_factor)))
        for key, label in [("gl", "GL:"), ("gr", "GR:")]:
            self.create_mapping_widget(row_pro, key, label)

        row_jc = tk.Frame(self.controller_mapping_frame, bg=tab_black); row_jc.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        tk.Label(row_jc, text="Joy-con Rail Buttons:", bg=tab_black, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(5 * scaling_factor)))
        for key, label in [("sll", "Left SL:"), ("srl", "Left SR:"), ("slr", "Right SL:"), ("srr", "Right SR:")]:
            self.create_mapping_widget(row_jc, key, label)

        row_gc = tk.Frame(self.controller_mapping_frame, bg=tab_black); row_gc.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        tk.Label(row_gc, text="GameCube Controller:", bg=tab_black, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(5 * scaling_factor)))
        
        self.gc_trigger_calib_btn = tk.Button(row_gc, text="Trigger Calibration", font=scale_font(("Arial", 11, "bold")), bg=button_gray, fg="white", relief=tk.FLAT, bd=0, command=self.on_gc_trigger_calib_clicked)
        self.gc_trigger_calib_btn.pack(side=tk.LEFT, padx=(int(5 * scaling_factor), int(10 * scaling_factor)))

        tk.Label(row_gc, text="Analog Trigger 100%:", bg=tab_black, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT, padx=(int(5 * scaling_factor), int(2 * scaling_factor)))
        
        self.gc_trigger_labels = ["Hair Trigger", "Before Click", "Fully Clicked"]
        self.gc_trigger_values = ["Hair Trigger", "100% at Bump", "100% at Max"]
        
        self.gc_trigger_combo = ttk.Combobox(row_gc, values=self.gc_trigger_labels, font=scale_font(("Arial", 11, "bold")), state="readonly", width=12, justify="center")
        
        current_val = getattr(CONFIG, "gc_trigger_mode", "100% at Bump")
        try:
            idx = self.gc_trigger_values.index(current_val)
            self.gc_trigger_combo.set(self.gc_trigger_labels[idx])
        except ValueError:
            self.gc_trigger_combo.set(self.gc_trigger_labels[1])
            
        self.gc_click_map_frame = tk.Frame(row_gc, bg=tab_black)
        self.create_mapping_widget(self.gc_click_map_frame, "gc_l_click", "L Click:")
        self.create_mapping_widget(self.gc_click_map_frame, "gc_r_click", "R Click:")

        def on_gc_trigger_combo_selected(event):
            selected_label = self.gc_trigger_combo.get()
            try:
                idx = self.gc_trigger_labels.index(selected_label)
                val = self.gc_trigger_values[idx]
                self.update_gc_trigger_mode_setting(val)
                if val == "100% at Max":
                    self.gc_click_map_frame.pack_forget()
                else:
                    self.gc_click_map_frame.pack(side=tk.LEFT, padx=(int(5 * scaling_factor), 0))
            except ValueError:
                pass
                
        self.gc_trigger_combo.bind("<<ComboboxSelected>>", on_gc_trigger_combo_selected)
        self.gc_trigger_combo.pack(side=tk.LEFT, padx=int(2 * scaling_factor))

        if current_val != "100% at Max":
            self.gc_click_map_frame.pack(side=tk.LEFT, padx=(int(5 * scaling_factor), 0))

        gyro_mapping_scope = "in_app_gyro_mode_mappings"
        gyro_actions_row = tk.Frame(self.in_app_gyro_mode_mapping_frame, bg=tab_black)
        gyro_actions_row.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        tk.Button(gyro_actions_row, text="Copy From Controller Mapping", font=scale_font(("Arial", 11, "bold")), bg=button_gray, fg="white", relief=tk.FLAT, bd=0, command=self.on_use_default_controller_mapping_for_gyro_mode).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(2 * scaling_factor)))
        tk.Button(gyro_actions_row, text="Reset", font=scale_font(("Arial", 11, "bold")), bg=button_gray, fg="white", relief=tk.FLAT, bd=0, command=self.on_reset_in_app_gyro_mode_mapping).pack(side=tk.LEFT, padx=int(2 * scaling_factor))

        gyro_shared_frame = tk.LabelFrame(self.in_app_gyro_mode_mapping_frame, text=" Shared Buttons & Joysticks ", bg=tab_black, fg=text_color, font=scale_font(("Arial", 11, "bold")), bd=1, relief=tk.GROOVE, padx=int(5 * scaling_factor), pady=int(5 * scaling_factor))
        gyro_shared_frame.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor), padx=(int(5 * scaling_factor), 0))

        def gyro_shared_mapping_row():
            row = tk.Frame(gyro_shared_frame, bg=tab_black)
            row.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
            return row

        gyro_row_shared_1 = gyro_shared_mapping_row()
        for key, label in [("zl", "ZL:"), ("l", "L:"), ("zr", "ZR:"), ("r", "R:")]:
            self.create_mapping_widget(gyro_row_shared_1, key, label, gyro_mapping_scope)

        gyro_row_shared_2 = gyro_shared_mapping_row()
        for key, label in [("minus", "Minus:"), ("plus", "Plus:"), ("capt", "Capture:"), ("home", "Home:"), ("c", "Chat:")]:
            self.create_mapping_widget(gyro_row_shared_2, key, label, gyro_mapping_scope)

        gyro_row_shared_3 = gyro_shared_mapping_row()
        self.create_joystick_mapping_widget(gyro_row_shared_3, "l_joystick", "L Joystick:", gyro_mapping_scope)
        self.create_mapping_widget(gyro_row_shared_3, "l_stk", "L Joystick Click:", gyro_mapping_scope)
        self.create_joystick_mapping_widget(gyro_row_shared_3, "r_joystick", "R Joystick:", gyro_mapping_scope)
        self.create_mapping_widget(gyro_row_shared_3, "r_stk", "R Joystick Click:", gyro_mapping_scope)

        gyro_row_shared_4 = gyro_shared_mapping_row()
        for key, label in [("a", "A:"), ("b", "B:"), ("x", "X:"), ("y", "Y:")]:
            self.create_mapping_widget(gyro_row_shared_4, key, label, gyro_mapping_scope)

        gyro_row_shared_5 = gyro_shared_mapping_row()
        for key, label in [("up", "Up:"), ("down", "Down:"), ("left", "Left:"), ("right", "Right:")]:
            self.create_mapping_widget(gyro_row_shared_5, key, label, gyro_mapping_scope)

        gyro_row_pro = tk.Frame(self.in_app_gyro_mode_mapping_frame, bg=tab_black); gyro_row_pro.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        tk.Label(gyro_row_pro, text="Pro Controller Back Buttons:", bg=tab_black, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(5 * scaling_factor)))
        for key, label in [("gl", "GL:"), ("gr", "GR:")]:
            self.create_mapping_widget(gyro_row_pro, key, label, gyro_mapping_scope)

        gyro_row_jc = tk.Frame(self.in_app_gyro_mode_mapping_frame, bg=tab_black); gyro_row_jc.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        tk.Label(gyro_row_jc, text="Joy-con Rail Buttons:", bg=tab_black, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(5 * scaling_factor)))
        for key, label in [("sll", "Left SL:"), ("srl", "Left SR:"), ("slr", "Right SL:"), ("srr", "Right SR:")]:
            self.create_mapping_widget(gyro_row_jc, key, label, gyro_mapping_scope)

        gyro_row_gc = tk.Frame(self.in_app_gyro_mode_mapping_frame, bg=tab_black); gyro_row_gc.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        tk.Label(gyro_row_gc, text="GameCube Controller:", bg=tab_black, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(5 * scaling_factor)))

        tk.Label(gyro_row_gc, text="Analog Trigger 100%:", bg=tab_black, fg=text_color, font=scale_font(("Arial", 11, "bold"))).pack(side=tk.LEFT, padx=(int(5 * scaling_factor), int(2 * scaling_factor)))
        self.gyro_gc_trigger_combo = ttk.Combobox(gyro_row_gc, values=self.gc_trigger_labels, font=scale_font(("Arial", 11, "bold")), state="readonly", width=12, justify="center")
        gyro_current_val = CONFIG.get_scoped_category_setting("gc_trigger_mode", "Hair Trigger", gyro_mapping_scope)
        try:
            idx = self.gc_trigger_values.index(gyro_current_val)
            self.gyro_gc_trigger_combo.set(self.gc_trigger_labels[idx])
        except ValueError:
            self.gyro_gc_trigger_combo.set(self.gc_trigger_labels[0])

        self.gyro_gc_click_map_frame = tk.Frame(gyro_row_gc, bg=tab_black)
        self.create_mapping_widget(self.gyro_gc_click_map_frame, "gc_l_click", "L Click:", gyro_mapping_scope)
        self.create_mapping_widget(self.gyro_gc_click_map_frame, "gc_r_click", "R Click:", gyro_mapping_scope)

        def on_gyro_gc_trigger_combo_selected(event):
            selected_label = self.gyro_gc_trigger_combo.get()
            try:
                idx = self.gc_trigger_labels.index(selected_label)
                val = self.gc_trigger_values[idx]
                CONFIG.set_scoped_category_setting("gc_trigger_mode", val, gyro_mapping_scope)
                CONFIG.save_config()
                if val == "100% at Max":
                    self.gyro_gc_click_map_frame.pack_forget()
                else:
                    self.gyro_gc_click_map_frame.pack(side=tk.LEFT, padx=(int(5 * scaling_factor), 0))
            except ValueError:
                pass

        self.gyro_gc_trigger_combo.bind("<<ComboboxSelected>>", on_gyro_gc_trigger_combo_selected)
        self.gyro_gc_trigger_combo.pack(side=tk.LEFT, padx=int(2 * scaling_factor))

        if gyro_current_val != "100% at Max":
            self.gyro_gc_click_map_frame.pack(side=tk.LEFT, padx=(int(5 * scaling_factor), 0))

    def on_use_default_controller_mapping_for_gyro_mode(self):
        CONFIG.copy_controller_mapping_to_in_app_gyro_mode_mapping()
        CONFIG.save_config()
        self._refresh_mapping_comboboxes()

    def on_reset_in_app_gyro_mode_mapping(self):
        CONFIG.reset_in_app_gyro_mode_mapping()
        CONFIG.save_config()
        self._refresh_mapping_comboboxes()

    def show_settings_tab(self, tab_id):
        self.settings_active_tab = tab_id
        for key, widgets in getattr(self, "settings_tab_buttons", {}).items():
            btn, frame = widgets
            is_active = key == tab_id
            frame.config(bg=tab_black if is_active else button_gray)
            btn.config(bg=tab_black if is_active else button_gray, fg=text_color)

        for frame_name in ("controller_mapping_frame", "in_app_gyro_mode_mapping_frame", "djg_frame", "gyro_frame", "comp_frame"):
            frame = getattr(self, frame_name, None)
            if frame is not None:
                frame.pack_forget()

        if tab_id == "controller_mapping":
            self.controller_mapping_frame.pack(side=tk.TOP, fill=tk.X)
        elif tab_id == "in_app_gyro_mode_mapping":
            self.in_app_gyro_mode_mapping_frame.pack(side=tk.TOP, fill=tk.X)
        elif tab_id == "gyro_passthrough":
            # Top-to-bottom: In-app Gyro Mode, Dual Joy-con Gyro (DJG), Gyro Pass-Through
            self.gyro_frame.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
            if getattr(CONFIG, 'simulation_mode', '') != "Switch1":
                self.djg_frame.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
            self.comp_frame.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        # Refresh the now-active mapping tab so In-app Gyro sync from the other
        # scope (synced at config level) is reflected in the comboboxes.
        if tab_id in ("controller_mapping", "in_app_gyro_mode_mapping"):
            self._refresh_mapping_comboboxes()
        try:
            self.root.update_idletasks()
        except Exception:
            pass

    def _prerender_settings_tabs(self):
        # Realize and paint every settings tab once (call while the window is invisible)
        # so the first real switch to each tab doesn't flash its default white background:
        # the flash only happens the first time a frame's widgets are mapped.
        active = getattr(self, "settings_active_tab", "controller_mapping")
        for tab in list(getattr(self, "settings_tab_buttons", {}).keys()):
            if tab == active:
                continue
            try:
                self.show_settings_tab(tab)
                self.root.update_idletasks()
            except Exception:
                pass
        try:
            self.show_settings_tab(active)
            self.root.update_idletasks()
        except Exception:
            pass

    def on_gc_trigger_calib_clicked(self):
        gc_controller = None
        for vc in VIRTUAL_CONTROLLERS:
            if vc and len(vc.controllers) > 0:
                for c in vc.controllers:
                    if getattr(c.controller_info, 'product_id', 0) == NSO_GAMECUBE_CONTROLLER_PID:
                        gc_controller = c
                        break
                if gc_controller:
                    break
        if not gc_controller:
            from tkinter import messagebox
            messagebox.showinfo("Not Found", "No NSO GameCube Controller is currently connected.")
            return

        GCTriggerCalibrationWizard(self.root, gc_controller)

    def update_driver_type_setting(self, val):
        # 1. 霈??(Removed load_config to prevent async save race condition)

        old_driver = getattr(CONFIG, "driver_type", "WinUHid")
        old_sim_mode = getattr(CONFIG, "simulation_mode", "PS5")
        
        if hasattr(CONFIG, 'active_profile') and CONFIG.active_profile in CONFIG.profiles:
            CONFIG.profiles[CONFIG.active_profile]["driver_type"] = val
            
        if old_driver == val:
            CONFIG.save_config()
            return
            
        # Check driver installation BEFORE updating CONFIG or recreating controllers!
        if val == "ViGEmBus":
            if not self.check_vigembus_installation(save=False):
                # Revert to old driver
                self.driver_switch.set_value(old_driver)
                return
        elif val == "USBIP":
            usbip_exe = "C:\\Program Files\\USBip\\usbip.exe"
            if not os.path.exists(usbip_exe):
                answer = self.ask_centered_yes_no(
                    "Install USBIP Driver",
                    "The USBIP driver is required but is not installed.\n\n"
                    "Do you want to install it now?\n(Requires administrator privileges and will temporarily reset USB connections.)"
                )
                if answer:
                    self.run_usbip_install(show_success_msg=True)
                    if not os.path.exists(usbip_exe):
                        self.driver_switch.set_value(old_driver)
                        return
                else:
                    self.driver_switch.set_value(old_driver)
                    return
        else:
            if not is_driver_installed():
                if getattr(CONFIG, 'driver_installed', False):
                    CONFIG.driver_installed = False
                    CONFIG.save_config()
                    self.update_driver_button()
                answer = self.ask_centered_yes_no(
                    "Install Virtual Controller Driver",
                    "WinUHid driver is not installed on your system.\n\nDo you want to install it now?\n(Requires administrator privileges.)"
                )
                if answer:
                    self.run_driver_install(show_success_msg=False)
                    if not is_driver_installed():
                        self.driver_switch.set_value(old_driver)
                        return
                else:
                    self.driver_switch.set_value(old_driver)
                    return

        # If we got here, checking was successful! Apply the mode switch in memory:
        CONFIG.driver_type = val
        self.update_driver_button()
        
        # Load the remembered simulation mode for the target driver
        if val == "ViGEmBus":
            CONFIG.simulation_mode = CONFIG.vigembus_sim_mode
        elif val == "USBIP":
            CONFIG.simulation_mode = CONFIG.usbip_sim_mode
        else:
            CONFIG.simulation_mode = CONFIG.winuhid_sim_mode
            
        # Update sim mode switch options and set value
        if val == "ViGEmBus":
            self.sim_mode_switch.update_options(["Xbox360", "PS4"], ["Xbox360", "PS4"], CONFIG.simulation_mode)
        elif val == "USBIP":
            self.sim_mode_switch.update_options(["Switch1", "Switch2", "PS5"], ["Switch1", "Switch2", "PS5"], CONFIG.simulation_mode)
        else:
            self.sim_mode_switch.update_options(["Xbox One", "PS4", "PS5"], ["Xbox One", "PS4", "PS5"], CONFIG.simulation_mode)
            
        self.update_dynamic_rumble_mode_options()
            
        # Apply the driver change to all running virtual controllers immediately
        success = True
        if hasattr(self, 'current_controllers'):
            try:
                # Pass 1: Cleanly close all running virtual controllers
                for vc in self.current_controllers:
                    if vc is not None:
                        with vc.state_lock:
                            if hasattr(vc, 'vg_controller') and vc.vg_controller is not None:
                                vc.cleanup_vg_controller()
                
                # Wait for PnP subsystem to settle
                import gc
                gc.collect()
                import time
                end_t = time.time() + 0.5
                while time.time() < end_t:
                    self.root.update()
                    time.sleep(0.01)
                
                # Pass 2: Recreate them under the new driver/mode sequentially
                for i, vc in enumerate(self.current_controllers):
                    if vc is not None:
                        if i > 0:
                            end_t2 = time.time() + 0.2
                            while time.time() < end_t2:
                                self.root.update()
                                time.sleep(0.01)
                        with vc.state_lock:
                            vc.mode = CONFIG.simulation_mode
                            if vc.mode == "Switch1":
                                vc.hold_mode = "Vertical"
                            elif vc.is_single() and len(vc.controllers) > 0:
                                addr = vc.controllers[0].device.address
                                if addr in CONFIG.joycon_hold_mode:
                                    vc.hold_mode = CONFIG.joycon_hold_mode[addr]
                                else:
                                    vc.hold_mode = "Vertical"
                            vc._setup_vg_controller()
                        if vc.loop and vc.loop.is_running():
                            asyncio.run_coroutine_threadsafe(vc.update_leds(), vc.loop)
            except Exception as e:
                logger.error(f"Failed to recreate controllers during driver mode switch: {e}")
                success = False

        if not success:
            # Revert CONFIG memory values by reloading from disk
            CONFIG.load_config()
            # Revert the GUI switches
            self.driver_switch.set_value(old_driver)
            self.update_driver_button()
            
            if old_driver == "ViGEmBus":
                self.sim_mode_switch.update_options(["Xbox360", "PS4"], ["Xbox360", "PS4"], old_sim_mode)
            elif old_driver == "USBIP":
                self.sim_mode_switch.update_options(["Switch1", "Switch2", "PS5"], ["Switch1", "Switch2", "PS5"], old_sim_mode)
            else:
                self.sim_mode_switch.update_options(["Xbox One", "PS4", "PS5"], ["Xbox One", "PS4", "PS5"], old_sim_mode)
                
            # Recreate controllers under old config
            if hasattr(self, 'current_controllers'):
                try:
                    for vc in self.current_controllers:
                        if vc is not None:
                            with vc.state_lock:
                                vc.mode = old_sim_mode
                                if vc.mode == "Switch1":
                                    vc.hold_mode = "Vertical"
                                elif vc.is_single() and len(vc.controllers) > 0:
                                    addr = vc.controllers[0].device.address
                                    if addr in CONFIG.joycon_hold_mode:
                                        vc.hold_mode = CONFIG.joycon_hold_mode[addr]
                                    else:
                                        vc.hold_mode = "Vertical"
                                vc._setup_vg_controller()
                            if vc.loop and vc.loop.is_running():
                                asyncio.run_coroutine_threadsafe(vc.update_leds(), vc.loop)
                except Exception as re_err:
                    logger.error(f"Failed to restore controllers to old driver: {re_err}")
        else:
            # 摮?
            CONFIG.save_config()
            
        self._refresh_mapping_comboboxes()
        self.force_refresh_player_slots()
 
    def force_refresh_player_slots(self):
        # While a batch UI update is in progress (e.g. a profile switch), skip the
        # rebuild so the player area isn't destroyed/recreated and repainted multiple
        # times (which causes ghosting). The caller does one rebuild when the batch ends.
        if getattr(self, '_suppress_player_slot_refresh', False):
            self._player_slot_refresh_pending = True
            return
        self._update_djg_panel_visibility()
        if hasattr(self, 'djg_dominant_switch'):
            self.djg_dominant_switch.set_value(getattr(CONFIG, "djg_dominant_side", "Left"))
        if hasattr(self, 'current_controllers'):
            if getattr(self, 'players_info', None) is not None:
                for p in self.players_info:
                    if hasattr(p, 'main_frame') and p.main_frame:
                        p.main_frame.destroy()
                self.players_info = None
            self.update(self.current_controllers)
            try:
                self.root.update_idletasks()
            except:
                pass
    def _refresh_mapping_comboboxes(self):
        mapping_keys = [
            "home", "capt", "c", "plus", "minus",
            "a", "b", "x", "y",
            "up", "down", "left", "right",
            "zl", "l", "zr", "r",
            "l_stk", "r_stk",
            "gl", "gr", "sll", "srl", "slr", "srr",
            "gc_l_click", "gc_r_click"
        ]
        active_scope = "in_app_gyro_mode_mappings" if getattr(self, "settings_active_tab", None) == "in_app_gyro_mode_mapping" else None
        for mapping_scope in (active_scope,):
            suffix = self._mapping_scope_suffix(mapping_scope)
            for key in mapping_keys:
                attr_key = self._mapping_attr(key, suffix)
                combo = getattr(self, f"{attr_key}_combo", None)
                custom_frame = getattr(self, f"{attr_key}_custom_frame", None)
                entry = getattr(self, f"{attr_key}_entry", None)
                mode_btn = getattr(self, f"{attr_key}_mode_btn", None)
                mode_var = getattr(self, f"{attr_key}_mode_var", None)
                cp_frame = getattr(self, f"{attr_key}_cp_frame", None)

                if not combo:
                    continue
                current_val = CONFIG.get_mapping_setting_scoped(key, "Default", mapping_scope)
                if current_val == "Gyro":
                    current_val = "In-app Gyro"
                if cp_frame:
                    cp_frame.pack_forget()
                if current_val == "Change Profile" and cp_frame:
                    combo.set("Change Profile")
                    combo.pack_forget()
                    if custom_frame:
                        custom_frame.pack_forget()
                    cp_frame.pack(side=tk.LEFT)
                elif current_val.startswith("Custom"):
                    combo.pack_forget()
                    if custom_frame and entry and mode_btn and mode_var:
                        entry.config(state="normal")
                        entry.delete(0, tk.END)

                        if current_val.startswith("Custom[Tap]:"):
                            mode_var.set("Tap")
                            mode_btn.config(text="Tap")
                            display_val = current_val[12:]
                        elif current_val.startswith("Custom[Hold]:"):
                            mode_var.set("Hold")
                            mode_btn.config(text="Hold")
                            display_val = current_val[13:]
                        else:
                            mode_var.set("Hold")
                            mode_btn.config(text="Hold")
                            display_val = current_val[7:]

                        if display_val == GYRO_LOCK_TOKEN:
                            combo.set(GYRO_LOCK_LABEL)
                            entry.insert(0, GYRO_LOCK_LABEL)
                        elif display_val == MODE_SHIFT_TOKEN:
                            combo.set(MODE_SHIFT_LABEL)
                            entry.insert(0, MODE_SHIFT_LABEL)
                        else:
                            combo.set("Custom")
                            display_val = format_input_display(display_val)
                            entry.insert(0, display_val)
                        entry.config(state="readonly")
                        custom_frame.pack(side=tk.LEFT)
                    else:
                        combo.set("Custom")
                else:
                    combo.set(current_val)
                    if custom_frame:
                        custom_frame.pack_forget()
                    combo.pack(side=tk.LEFT)

        for mapping_scope in (None, "in_app_gyro_mode_mappings"):
            suffix = self._mapping_scope_suffix(mapping_scope)
            for key in ["l_joystick", "r_joystick"]:
                attr_key = self._mapping_attr(key, suffix)
                combo = getattr(self, f"{attr_key}_combo", None)
                custom_frame = getattr(self, f"{attr_key}_custom_frame", None)
                custom_btn = getattr(self, f"{attr_key}_custom_btn", None)
                scroll_mode_btn = getattr(self, f"{attr_key}_scroll_mode_btn", None)
                scroll_activation_var = getattr(self, f"{attr_key}_scroll_activation_var", None)
                if not combo:
                    continue
                current_val = CONFIG.get_mapping_setting_scoped(key, "Default", mapping_scope)
                if current_val in ("Custom", "Mouse", "Scroll Wheel"):
                    combo.set(current_val)
                    combo.pack_forget()
                    if custom_btn:
                        custom_btn.config(text=current_val)
                    if scroll_mode_btn:
                        if current_val == "Scroll Wheel":
                            if scroll_activation_var:
                                scroll_activation_var.set(CONFIG.get_joystick_setting_scoped(key, "scroll_activation", "Hold", mapping_scope))
                            scroll_mode_btn.config(text=CONFIG.get_joystick_setting_scoped(key, "scroll_activation", "Hold", mapping_scope))
                            scroll_mode_btn.pack(side=tk.LEFT, padx=(0, int(2 * scaling_factor)), fill=tk.Y, before=custom_btn)
                        else:
                            scroll_mode_btn.pack_forget()
                    if custom_frame:
                        custom_frame.pack(side=tk.LEFT)
                else:
                    combo.set(current_val if current_val in JOYSTICK_OPTIONS else "Default")
                    if scroll_mode_btn:
                        scroll_mode_btn.pack_forget()
                    if custom_frame:
                        custom_frame.pack_forget()
                    combo.pack(side=tk.LEFT)
                    
        if hasattr(self, 'gc_trigger_combo'):
            try:
                idx = self.gc_trigger_values.index(CONFIG.gc_trigger_mode)
                self.gc_trigger_combo.set(self.gc_trigger_labels[idx])
            except ValueError:
                pass
            if hasattr(self, 'gc_click_map_frame'):
                if CONFIG.gc_trigger_mode == "100% at Max":
                    self.gc_click_map_frame.pack_forget()
                else:
                    self.gc_click_map_frame.pack(side=tk.LEFT, padx=(int(scaling_factor * 5), 0))
        if hasattr(self, 'gyro_gc_trigger_combo'):
            gyro_gc_mode = CONFIG.get_scoped_category_setting("gc_trigger_mode", "Hair Trigger", "in_app_gyro_mode_mappings")
            try:
                idx = self.gc_trigger_values.index(gyro_gc_mode)
                self.gyro_gc_trigger_combo.set(self.gc_trigger_labels[idx])
            except ValueError:
                pass
            if hasattr(self, 'gyro_gc_click_map_frame'):
                if gyro_gc_mode == "100% at Max":
                    self.gyro_gc_click_map_frame.pack_forget()
                else:
                    self.gyro_gc_click_map_frame.pack(side=tk.LEFT, padx=(int(scaling_factor * 5), 0))
        if hasattr(self, 'layout_switch'):
            self.layout_switch.set_value(CONFIG.abxy_mode)
        if hasattr(self, 'rumble_mode_switch'):
            self.rumble_mode_switch.set_value(CONFIG.rumble_mode)
            self.update_rumble_mode_ui(CONFIG.rumble_mode)
        if hasattr(self, 'vibration_strength_scale'):
            self.vibration_strength_scale.set(CONFIG.vibration_strength)
        if hasattr(self, 'vibration_frequency_scale'):
            self.vibration_frequency_scale.set(CONFIG.vibration_frequency)

    def update_gc_trigger_mode_setting(self, val):
        CONFIG.gc_trigger_mode = val
        CONFIG.save_config()
        # No need to restart discovery, controllers can read the setting dynamically or on reconnect

    def update_sim_mode_setting(self, val):
        # 1. 霈??(Removed load_config to prevent async save race condition)
        
        old_mode = getattr(CONFIG, "simulation_mode", "PS5")
        
        if hasattr(CONFIG, 'active_profile') and CONFIG.active_profile in CONFIG.profiles:
            CONFIG.profiles[CONFIG.active_profile]["simulation_mode"] = val
        
        if old_mode == val:
            CONFIG.save_config()
            return
            
        CONFIG.simulation_mode = val
        driver_type = getattr(CONFIG, "driver_type", "WinUHid")
        if driver_type == "ViGEmBus":
            CONFIG.vigembus_sim_mode = val
        elif driver_type == "USBIP":
            CONFIG.usbip_sim_mode = val
        else:
            CONFIG.winuhid_sim_mode = val
            
        success = True
        reverted_vcs = []
        if hasattr(self, 'current_controllers'):
            try:
                for vc in self.current_controllers:
                    if vc is not None:
                        vc.set_mode(val)
                        reverted_vcs.append(vc)
            except Exception as e:
                logger.error(f"Failed to switch emulation mode: {e}")
                success = False
                
        if not success:
            # Revert CONFIG memory values by reloading from disk
            CONFIG.load_config()
            # Revert set_mode on already switched controllers
            for vc in reverted_vcs:
                if vc is not None:
                    try:
                        vc.set_mode(old_mode)
                    except Exception:
                        pass
            # Revert the UI switch
            self.sim_mode_switch.set_value(old_mode)
        else:
            # 摮?
            CONFIG.save_config()
            self.update_dynamic_rumble_mode_options()
            
        self._refresh_mapping_comboboxes()
        self.force_refresh_player_slots()

    def _revert_from_switch2_pro(self):
        default_mode = "PS4" if getattr(CONFIG, "driver_type", "WinUHid") == "ViGEmBus" else "PS5"
        CONFIG.simulation_mode = default_mode
        if getattr(CONFIG, "driver_type", "WinUHid") == "ViGEmBus":
            CONFIG.vigembus_sim_mode = default_mode
        else:
            CONFIG.winuhid_sim_mode = default_mode
        self.sim_mode_switch.set_value(default_mode)
        CONFIG.save_config()
        self._refresh_mapping_comboboxes()
        self.force_refresh_player_slots()

    def update_layout_setting(self, val):
        CONFIG.abxy_mode = val
        self.on_setting_changed()

    def update_vibration_strength(self, val):
        try:
            CONFIG.vibration_strength = int(float(val))
            CONFIG.save_config()
        except Exception as e:
            logger.error(f"Failed to save vibration strength setting: {e}")

    def update_vibration_frequency(self, val):
        try:
            CONFIG.vibration_frequency = int(float(val))
            CONFIG.save_config()
        except Exception as e:
            logger.error(f"Failed to save vibration frequency setting: {e}")

    def update_rumble_mode_setting(self, val):
        CONFIG.rumble_mode = val
        CONFIG.save_config()
        self.update_rumble_mode_ui(val)
        self.vibration_strength_scale.set(CONFIG.vibration_strength)
        self.vibration_frequency_scale.set(CONFIG.vibration_frequency)

    def update_dynamic_rumble_mode_options(self):
        if not hasattr(self, 'rumble_mode_switch'):
            return
            
        driver_type = getattr(CONFIG, "driver_type", "WinUHid")
        sim_mode = getattr(CONFIG, "simulation_mode", "PS5")
        
        current_rumble = getattr(CONFIG, "rumble_mode", "Xbox")
        if current_rumble == "Switch":
            current_rumble = "PS5" # Automatically migrate name in memory
            CONFIG.rumble_mode = "PS5"
            CONFIG.save_config()
            
        if driver_type == "USBIP" and sim_mode == "PS5":
            self.rumble_mode_switch.update_options(["Xbox", "PS5 / HD Rumble"], ["Xbox", "PS5"], current_rumble, widths=[8, 16])
        else:
            if current_rumble == "PS5":
                current_rumble = "Switch"
                CONFIG.rumble_mode = "Switch"
                CONFIG.save_config()
            self.rumble_mode_switch.update_options(["Xbox", "Switch"], ["Xbox", "Switch"], current_rumble)

    def update_rumble_mode_ui(self, mode):
        if mode in ["Switch", "PS5"]:
            self.vibration_frequency_label.pack_forget()
            self.vibration_frequency_scale.pack_forget()
        else:
            self.vibration_frequency_label.pack(side=tk.LEFT, before=self.delay_label, padx=(int(20 * scaling_factor), int(2 * scaling_factor)))
            self.vibration_frequency_scale.pack(side=tk.LEFT, before=self.delay_label)

    def update_startup_setting(self, val):
        CONFIG.open_when_startup = val
        set_startup(val)
        CONFIG.save_config()
        if hasattr(self, 'startup_btn'):
            self.startup_btn.config(text=f"Run At Startup: {'ON' if val else 'OFF'}")
        if hasattr(self, 'startup_frame'):
            self.startup_frame.config(bg=highlight_color if val else button_gray)

    def update_minimized_setting(self, val):
        CONFIG.start_minimized = val
        CONFIG.save_config()
        if hasattr(self, 'minimized_btn'):
            self.minimized_btn.config(text=f"Start Minimized: {'ON' if val else 'OFF'}")
        if hasattr(self, 'min_frame'):
            self.min_frame.config(bg=highlight_color if val else button_gray)


    def custom_askstring(self, title, prompt, initialvalue=""):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.configure(bg=background_color)
        dialog.transient(self.root)
        dialog.grab_set()
        
        w = int(350 * scaling_factor)
        h = int(150 * scaling_factor)
        x = self.root.winfo_x() + (self.root.winfo_width() // 2) - (w // 2)
        y = self.root.winfo_y() + (self.root.winfo_height() // 2) - (h // 2)
        dialog.geometry(f"{w}x{h}+{x}+{y}")
        
        tk.Label(dialog, text=prompt, font=scale_font(("Arial", 11, "bold")), bg=background_color, fg=text_color).pack(pady=(int(15*scaling_factor), int(5*scaling_factor)))
        
        entry = tk.Entry(dialog, font=scale_font(("Arial", 11, "bold")), bg=button_gray, fg="white", insertbackground="white", justify="center")
        entry.pack(padx=int(20*scaling_factor), fill=tk.X)
        if initialvalue:
            entry.insert(0, initialvalue)
            entry.select_range(0, tk.END)
        
        result = [None]
        def on_ok(event=None):
            result[0] = entry.get()
            dialog.destroy()
        def on_cancel(event=None):
            dialog.destroy()
            
        entry.bind("<Return>", on_ok)
        entry.bind("<Escape>", on_cancel)
        
        btn_frame = tk.Frame(dialog, bg=background_color)
        btn_frame.pack(pady=int(15*scaling_factor))
        tk.Button(btn_frame, text="OK", font=scale_font(("Arial", 11, "bold")), bg=button_gray, fg="white", width=8, relief=tk.FLAT, bd=0, command=on_ok).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Cancel", font=scale_font(("Arial", 11, "bold")), bg=button_gray, fg="white", width=8, relief=tk.FLAT, bd=0, command=on_cancel).pack(side=tk.LEFT, padx=5)
        
        entry.focus_set()
        self.root.wait_window(dialog)
        return result[0]

    def custom_messagebox(self, title, message, type="info"):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.configure(bg=background_color)
        dialog.transient(self.root)
        dialog.grab_set()
        
        w = int(350 * scaling_factor)
        h = int(150 * scaling_factor)
        x = self.root.winfo_x() + (self.root.winfo_width() // 2) - (w // 2)
        y = self.root.winfo_y() + (self.root.winfo_height() // 2) - (h // 2)
        dialog.geometry(f"{w}x{h}+{x}+{y}")
        
        tk.Label(dialog, text=message, font=scale_font(("Arial", 11, "bold")), bg=background_color, fg=text_color, wraplength=int(310*scaling_factor), justify="center").pack(pady=(int(20*scaling_factor), int(10*scaling_factor)), expand=True)
        
        result = [None]
        btn_frame = tk.Frame(dialog, bg=background_color)
        btn_frame.pack(pady=(0, int(15*scaling_factor)))
        
        def set_res(res):
            result[0] = res
            dialog.destroy()
            
        if type == "yesno":
            tk.Button(btn_frame, text="Yes", font=scale_font(("Arial", 11, "bold")), bg=button_gray, fg="white", width=8, relief=tk.FLAT, bd=0, command=lambda: set_res(True)).pack(side=tk.LEFT, padx=5)
            tk.Button(btn_frame, text="No", font=scale_font(("Arial", 11, "bold")), bg=button_gray, fg="white", width=8, relief=tk.FLAT, bd=0, command=lambda: set_res(False)).pack(side=tk.LEFT, padx=5)
        else:
            tk.Button(btn_frame, text="OK", font=scale_font(("Arial", 11, "bold")), bg=button_gray, fg="white", width=8, relief=tk.FLAT, bd=0, command=lambda: set_res(True)).pack()
            
        dialog.bind("<Return>", lambda e: set_res(True))
        if type == "yesno":
            dialog.bind("<Escape>", lambda e: set_res(False))
        else:
            dialog.bind("<Escape>", lambda e: set_res(True))
            
        self.root.wait_window(dialog)
        return result[0]

    def get_profile_assigned_apps(self, profile_name):
        profile_data = CONFIG.profiles.get(profile_name, {})
        assigned_apps = profile_data.get("assigned_apps")
        if isinstance(assigned_apps, list):
            apps = []
            for app in assigned_apps:
                if isinstance(app, str):
                    app = {"path": app, "name": get_exe_display_name(app)}
                if isinstance(app, dict) and app.get("path"):
                    apps.append({
                        "path": app.get("path", ""),
                        "name": app.get("name") or get_exe_display_name(app.get("path")),
                    })
            return apps

        assigned_app = profile_data.get("assigned_app", {})
        if isinstance(assigned_app, str) and assigned_app:
            return [{"path": assigned_app, "name": get_exe_display_name(assigned_app)}]
        if isinstance(assigned_app, dict) and assigned_app.get("path"):
            return [{
                "path": assigned_app.get("path", ""),
                "name": assigned_app.get("name") or get_exe_display_name(assigned_app.get("path")),
            }]
        return []

    def set_profile_assigned_apps(self, profile_name, apps):
        if profile_name not in CONFIG.profiles:
            return
        normalized_seen = set()
        normalized_apps = []
        for app in apps:
            app_path = app.get("path") if isinstance(app, dict) else str(app)
            normalized_path = normalize_app_path(app_path)
            if not normalized_path or normalized_path in normalized_seen:
                continue
            normalized_seen.add(normalized_path)
            normalized_apps.append({
                "path": os.path.normpath(app_path),
                "name": (app.get("name") if isinstance(app, dict) else None) or get_exe_display_name(app_path),
            })
        CONFIG.profiles[profile_name]["assigned_apps"] = normalized_apps
        CONFIG.profiles[profile_name].pop("assigned_app", None)

    def clear_app_from_other_profiles(self, app_path, current_profile):
        normalized_path = normalize_app_path(app_path)
        if not normalized_path:
            return
        for profile_name in list(CONFIG.profiles.keys()):
            if profile_name == current_profile:
                continue
            apps = self.get_profile_assigned_apps(profile_name)
            filtered_apps = [
                app for app in apps
                if normalize_app_path(app.get("path")) != normalized_path
            ]
            if len(filtered_apps) != len(apps):
                self.set_profile_assigned_apps(profile_name, filtered_apps)

    def refresh_assigned_apps_ui(self):
        if not hasattr(self, "assigned_apps_frame"):
            return
        for child in self.assigned_apps_frame.winfo_children():
            child.destroy()

        btn = tk.Button(
            self.assigned_apps_frame,
            text="Assign Current Profile To Apps",
            font=scale_font(("Arial", 11, "bold")),
            bg=button_gray,
            fg="white",
            relief=tk.FLAT,
            bd=0,
            command=self.open_assigned_apps_popup
        )
        btn.pack(side=tk.LEFT, padx=(int(20 * scaling_factor), int(10 * scaling_factor)))

    def refresh_profile_switching_combo_trigger_ui(self):
        if not hasattr(self, "profile_switch_trigger_frame"):
            return
        for child in self.profile_switch_trigger_frame.winfo_children():
            child.destroy()

        tk.Label(
            self.profile_switch_trigger_frame,
            text="Profile Switching Combo Trigger:",
            bg=background_color,
            fg=text_color,
            font=scale_font(("Arial", 11, "bold"))
        ).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(2 * scaling_factor)))

        trigger_widget = self._create_profile_combo_input_widget(
            self.profile_switch_trigger_frame,
            lambda: getattr(CONFIG, "profile_switching_combo_trigger", ""),
            self.set_profile_switching_combo_trigger
        )
        trigger_widget.pack(side=tk.LEFT, padx=int(2 * scaling_factor))

    def set_profile_switching_combo_trigger(self, value):
        CONFIG.profile_switching_combo_trigger = value
        CONFIG.save_config()

    def open_assigned_apps_popup(self):
        popup = tk.Toplevel(self.root)
        popup.title("Assigned Apps")
        popup.configure(bg=background_color)
        
        w, h = int(400 * scaling_factor), int(300 * scaling_factor)
        root_x, root_y = self.root.winfo_rootx(), self.root.winfo_rooty()
        root_w, root_h = self.root.winfo_width(), self.root.winfo_height()
        pos_x = root_x + (root_w - w) // 2
        pos_y = root_y + (root_h - h) // 2
        popup.geometry(f"{w}x{h}+{pos_x}+{pos_y}")
        popup.transient(self.root)
        popup.grab_set()

        btn_frame = tk.Frame(popup, bg=background_color)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=int(10 * scaling_factor))

        container = tk.Frame(popup, bg=background_color)
        container.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=int(10 * scaling_factor), pady=(int(10 * scaling_factor), 0))

        canvas = tk.Canvas(container, bg=background_color, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg=background_color)

        canvas_window = canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")

        def update_scroll_and_center(event=None):
            bbox = canvas.bbox("all")
            if not bbox: return
            canvas.configure(scrollregion=bbox)
            
            content_height = scrollable_frame.winfo_reqheight()
            canvas_height = canvas.winfo_height()
            canvas_width = canvas.winfo_width()
            
            if canvas_height <= 1:
                return

            if content_height > canvas_height:
                if not scrollbar.winfo_ismapped():
                    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
                canvas.coords(canvas_window, 0, 0)
            else:
                if scrollbar.winfo_ismapped():
                    scrollbar.pack_forget()
                y_offset = (canvas_height - content_height) // 2
                canvas.coords(canvas_window, 0, y_offset)

            canvas.itemconfig(canvas_window, width=canvas_width)

        scrollable_frame.bind("<Configure>", update_scroll_and_center)
        canvas.bind("<Configure>", update_scroll_and_center)

        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        def _on_mousewheel(event):
            if scrollbar.winfo_ismapped():
                canvas.yview_scroll(int(-1*(event.delta/120)), "units")

        def bind_mousewheel(widget):
            widget.bind("<MouseWheel>", _on_mousewheel)
            for child in widget.winfo_children():
                bind_mousewheel(child)

        def populate_list():
            for child in scrollable_frame.winfo_children():
                child.destroy()
            apps = self.get_profile_assigned_apps(getattr(CONFIG, "active_profile", ""))
            for index, app in enumerate(apps):
                app_path = app.get("path", "")
                app_name = os.path.basename(app_path)
                try:
                    import win32api
                    lang, codepage = win32api.GetFileVersionInfo(app_path, '\\VarFileInfo\\Translation')[0]
                    str_info = u'\\StringFileInfo\\%04X%04X\\FileDescription' % (lang, codepage)
                    desc = win32api.GetFileVersionInfo(app_path, str_info)
                    if desc and len(desc) < len(app_name):
                        app_name = desc
                except Exception:
                    pass

                if app_name.lower().endswith(".exe"):
                    app_name = app_name[:-4]

                row = tk.Frame(scrollable_frame, bg=background_color)
                row.pack(fill=tk.X, pady=int(2 * scaling_factor))
                
                del_btn = tk.Button(row, text="X", bg="#cc0000", fg="white", font=scale_font(("Arial", 9, "bold")), relief=tk.FLAT, bd=0, command=lambda i=index: [self.on_remove_assigned_app(i), populate_list()])
                del_btn.pack(side=tk.RIGHT, fill=tk.Y, padx=0)
                
                lbl = tk.Label(row, text=app_name, bg=button_gray, fg="white", font=scale_font(("Arial", 11, "bold")), justify="center", anchor="center", padx=int(5*scaling_factor), pady=int(4*scaling_factor))
                lbl.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, int(2*scaling_factor)))

            bind_mousewheel(popup)

        populate_list()

        def add_and_refresh():
            popup.grab_release()
            self.on_add_assigned_app()
            populate_list()
            popup.grab_set()

        add_btn = tk.Button(btn_frame, text="Add", font=scale_font(("Arial", 11, "bold")), bg=button_gray, fg="white", relief=tk.FLAT, bd=0, command=add_and_refresh)
        add_btn.pack(side=tk.LEFT, padx=int(10 * scaling_factor), expand=True, fill=tk.X)

        close_btn = tk.Button(btn_frame, text="Close", font=scale_font(("Arial", 11, "bold")), bg=button_gray, fg="white", relief=tk.FLAT, bd=0, command=popup.destroy)
        close_btn.pack(side=tk.RIGHT, padx=int(10 * scaling_factor), expand=True, fill=tk.X)

    def choose_app_path(self):
        initial_dir = os.environ.get("ProgramFiles") or os.path.expanduser("~")
        self.app_profile_poll_suspended = True
        try:
            return filedialog.askopenfilename(
                parent=self.root,
                title="Choose App",
                initialdir=initial_dir,
                filetypes=[("Applications", "*.exe"), ("All files", "*.*")],
            )
        finally:
            self.app_profile_poll_suspended = False

    def on_choose_assigned_app(self, index=0):
        if not getattr(CONFIG, "active_profile", None) or CONFIG.active_profile not in CONFIG.profiles:
            return

        app_path = self.choose_app_path()
        if not app_path:
            return

        app_name = get_exe_display_name(app_path)
        current_profile = CONFIG.active_profile
        apps = self.get_profile_assigned_apps(current_profile)
        new_app = {
            "path": os.path.normpath(app_path),
            "name": app_name,
        }

        if index < len(apps):
            apps[index] = new_app
        else:
            apps.append(new_app)

        self.clear_app_from_other_profiles(app_path, current_profile)
        self.set_profile_assigned_apps(current_profile, apps)
        CONFIG.save_config()
        self.refresh_assigned_apps_ui()

    def on_add_assigned_app(self):
        current_profile = getattr(CONFIG, "active_profile", "")
        self.on_choose_assigned_app(len(self.get_profile_assigned_apps(current_profile)))

    def on_remove_assigned_app(self, index):
        current_profile = getattr(CONFIG, "active_profile", "")
        if not current_profile or current_profile not in CONFIG.profiles:
            return
        apps = self.get_profile_assigned_apps(current_profile)
        if 0 <= index < len(apps):
            apps.pop(index)
        self.set_profile_assigned_apps(current_profile, apps)
        CONFIG.save_config()
        self.refresh_assigned_apps_ui()

    def get_foreground_app_path(self):
        try:
            import win32process
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                return ""

            _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
            if not pid:
                return ""

            process_handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not process_handle:
                return ""

            try:
                size = wintypes.DWORD(32768)
                buffer = ctypes.create_unicode_buffer(size.value)
                if ctypes.windll.kernel32.QueryFullProcessImageNameW(process_handle, 0, buffer, ctypes.byref(size)):
                    return normalize_app_path(buffer.value)
            finally:
                ctypes.windll.kernel32.CloseHandle(process_handle)
        except Exception as e:
            logger.debug(f"Failed to read foreground app path: {e}")
        return ""

    def get_profile_for_app_path(self, app_path):
        normalized_path = normalize_app_path(app_path)
        if not normalized_path:
            return None

        for profile_name in self.get_sorted_profiles():
            for assigned_app in self.get_profile_assigned_apps(profile_name):
                if normalize_app_path(assigned_app.get("path")) == normalized_path:
                    return profile_name
        return None

    def save_active_profile_runtime_settings(self):
        if hasattr(CONFIG, 'active_profile') and CONFIG.active_profile in CONFIG.profiles:
            CONFIG.profiles[CONFIG.active_profile]["driver_type"] = getattr(CONFIG, "driver_type", "WinUHid")
            CONFIG.profiles[CONFIG.active_profile]["simulation_mode"] = getattr(CONFIG, "simulation_mode", "PS5")

    def switch_to_profile(self, profile_name):
        if not profile_name or profile_name == getattr(CONFIG, "active_profile", ""):
            return False
        if profile_name not in CONFIG.profiles:
            return False

        if hasattr(self, 'profile_apply_timer') and self.profile_apply_timer:
            self.root.after_cancel(self.profile_apply_timer)
            self.profile_apply_timer = None
        self.pending_profile = None
        self.save_active_profile_runtime_settings()

        if CONFIG.switch_profile(profile_name):
            self._set_profile_button_text()
            self.app_profile_switching = True
            try:
                self.apply_profile_switch()
            finally:
                self.app_profile_switching = False
            # Close the popup last, after the UI has been updated, and without forcing
            # an intermediate repaint of it. Refreshing/painting the popup right before
            # destroying it (and closing before the main UI updated) caused the brief
            # ghosting during the switch.
            if getattr(self, "profile_popup", None) is not None and self.profile_popup.winfo_exists():
                self.close_profile_popup()
            return True
        return False

    def poll_assigned_app_focus(self):
        if not getattr(self, "root", None) or getattr(self, "is_quitting", False):
            return

        try:
            if not self.app_profile_poll_suspended and not self.app_profile_switching:
                foreground_app_path = self.get_foreground_app_path()
                target_profile = self.get_profile_for_app_path(foreground_app_path)
                if target_profile and target_profile != getattr(CONFIG, "active_profile", ""):
                    self.switch_to_profile(target_profile)
                self.last_foreground_app_path = foreground_app_path
        except Exception as e:
            logger.debug(f"Assigned app focus poll failed: {e}")
        finally:
            try:
                self.root.after(1000, self.poll_assigned_app_focus)
            except Exception:
                pass

    def get_sorted_profiles(self):
        import re
        def sort_key(s):
            tokens = re.findall(r'[a-zA-Z]+|\d+|[^a-zA-Z\d]+', s)
            key = []
            for t in tokens:
                if t.isalpha():
                    key.append((0, t.lower()))
                elif t.isdigit():
                    key.append((1, int(t)))
                else:
                    key.append((2, t))
            return key
        return sorted(
            list(CONFIG.profiles.keys()),
            key=lambda name: (0 if CONFIG.profiles.get(name, {}).get("change_profile_list", False) else 1, sort_key(name))
        )

    def _change_list_profiles(self):
        return [
            name for name in self.get_sorted_profiles()
            if CONFIG.profiles.get(name, {}).get("change_profile_list", False)
        ]

    def _show_profile_selection_notification(self, manual):
        lst = self._change_list_profiles()
        if not lst:
            return
        sel = self.pending_profile if self.pending_profile in lst else lst[0]
        idx = lst.index(sel)
        prev_name = lst[(idx - 1) % len(lst)]
        next_name = lst[(idx + 1) % len(lst)]
        layout = getattr(CONFIG, "abxy_mode", "Xbox")
        auto_close = None if manual else 3000
        # Widen the window to fit the longest profile name in the change list.
        try:
            sel_font = tkFont.Font(font=scale_font(("Segoe UI", 11, "bold")))
            name_px = max((sel_font.measure(n) for n in lst), default=0)
        except Exception:
            name_px = 0
        self.calibration_overlay.show_profile_selection(prev_name, sel, next_name, manual, layout, auto_close, name_px)

    def on_cycle_profile(self):
        if not hasattr(CONFIG, 'active_profile') or not CONFIG.profiles:
            return

        # Execute on main thread to avoid Tkinter threading errors
        if threading.current_thread() != threading.main_thread():
            self.root.after(0, self.on_cycle_profile)
            return

        sorted_profiles = self._change_list_profiles()
        if not sorted_profiles:
            return

        # Initialize pending profile if not set
        if not hasattr(self, 'pending_profile') or not self.pending_profile:
            self.pending_profile = CONFIG.active_profile

        try:
            curr_idx = sorted_profiles.index(self.pending_profile)
            next_idx = (curr_idx + 1) % len(sorted_profiles)
        except ValueError:
            next_idx = 0

        self.pending_profile = sorted_profiles[next_idx]

        import utils
        manual = getattr(CONFIG, "change_profile_mode", "Manual") == "Manual"

        # Cancel any pending auto-apply timer
        if hasattr(self, 'profile_apply_timer') and self.profile_apply_timer:
            self.root.after_cancel(self.profile_apply_timer)
            self.profile_apply_timer = None

        if manual:
            # Enter selection mode: pause virtual output, wait for A (confirm) / B (cancel).
            utils.profile_selection_active = True
            self._show_profile_selection_notification(True)
        else:
            # Auto: show the selection and auto-apply after a second of inactivity.
            utils.profile_selection_active = False
            self._show_profile_selection_notification(False)
            self.profile_apply_timer = self.root.after(1000, self.apply_pending_profile)

    def on_profile_nav(self, direction):
        if threading.current_thread() != threading.main_thread():
            self.root.after(0, self.on_profile_nav, direction)
            return
        import utils
        if not utils.profile_selection_active:
            return
        # Debounce so a single flick / Dpad tap doesn't advance multiple steps even
        # when reported by both controllers of a merged pair.
        now = time.perf_counter()
        if now - getattr(self, "_last_profile_nav_time", 0.0) < 0.18:
            return
        self._last_profile_nav_time = now
        lst = self._change_list_profiles()
        if not lst:
            return
        sel = self.pending_profile if self.pending_profile in lst else lst[0]
        idx = lst.index(sel)
        self.pending_profile = lst[(idx + direction) % len(lst)]
        self._show_profile_selection_notification(True)

    def on_profile_confirm(self):
        if threading.current_thread() != threading.main_thread():
            self.root.after(0, self.on_profile_confirm)
            return
        import utils
        if not utils.profile_selection_active:
            return
        utils.profile_selection_active = False
        self.calibration_overlay.close_profile_selection()
        target = getattr(self, "pending_profile", None)
        self.pending_profile = None
        if target and target != getattr(CONFIG, "active_profile", ""):
            self.switch_to_profile(target)

    def on_profile_cancel(self):
        if threading.current_thread() != threading.main_thread():
            self.root.after(0, self.on_profile_cancel)
            return
        import utils
        utils.profile_selection_active = False
        self.calibration_overlay.close_profile_selection()
        self.pending_profile = None

    def on_profile_combo_switch(self, profile_name):
        if threading.current_thread() != threading.main_thread():
            self.root.after(0, lambda p=profile_name: self.on_profile_combo_switch(p))
            return
        if profile_name not in CONFIG.profiles:
            return
        if profile_name == getattr(CONFIG, "active_profile", ""):
            return
        import utils
        utils.show_notification("Profile Switched", f"Current Profile: {profile_name}")
        self.switch_to_profile(profile_name)
        
    def apply_pending_profile(self):
        self.profile_apply_timer = None
        if not hasattr(self, 'pending_profile') or not self.pending_profile:
            return
            
        if self.pending_profile == getattr(CONFIG, 'active_profile', ""):
            return # No change
            
        self.switch_to_profile(self.pending_profile)
            
        self.pending_profile = None

    def apply_profile_switch(self):
        new_profile_name = getattr(CONFIG, 'active_profile', "")
        if not new_profile_name or new_profile_name not in CONFIG.profiles:
            return
            
        new_driver = CONFIG.profiles[new_profile_name].get("driver_type")
        if not new_driver:
            new_driver = getattr(CONFIG, "driver_type", "WinUHid")
            CONFIG.profiles[new_profile_name]["driver_type"] = new_driver
            
        new_emu = CONFIG.profiles[new_profile_name].get("simulation_mode")
        if not new_emu:
            new_emu = getattr(CONFIG, "simulation_mode", "PS5")
            CONFIG.profiles[new_profile_name]["simulation_mode"] = new_emu
            
        CONFIG.save_config()

        driver_changed = getattr(CONFIG, "driver_type", "") != new_driver
        emu_changed = getattr(CONFIG, "simulation_mode", "") != new_emu
        
        # Pre-set the target driver's default to avoid double recreation in update_driver_type_setting
        if new_driver == "ViGEmBus":
            CONFIG.vigembus_sim_mode = new_emu
        elif new_driver == "USBIP":
            CONFIG.usbip_sim_mode = new_emu
        else:
            CONFIG.winuhid_sim_mode = new_emu

        # 2. ???單?rofile?river
        if driver_changed:
            if getattr(self, 'driver_switch', None):
                self.driver_switch.set_value(new_driver)
            self.update_driver_type_setting(new_driver)
            
        # 3. ???單?rofile?mu Mode (If driver changed, it was already applied, but we ensure UI is updated)
        if not driver_changed and emu_changed:
            if getattr(self, 'sim_mode_switch', None):
                self.sim_mode_switch.set_value(new_emu)
            self.update_sim_mode_setting(new_emu)
        elif getattr(self, 'sim_mode_switch', None):
            self.sim_mode_switch.set_value(new_emu)

        # 4. ???單?rofile?ustom buttons?隞身摰?
        self.refresh_ui_for_profile()

    def on_profile_selected(self, event):
        return
            
        # 1. 蝝?銝身摰river?mu Mode?喳??祉?profile
    def on_add_profile(self):
        i = 1
        while f"Profile {i}" in CONFIG.profiles:
            i += 1
        new_name = f"Profile {i}"
        
        # 1. 蝝?銝身摰river?mu Mode?喳??祉?profile
        if hasattr(CONFIG, 'active_profile') and CONFIG.active_profile in CONFIG.profiles:
            CONFIG.profiles[CONFIG.active_profile]["driver_type"] = getattr(CONFIG, "driver_type", "WinUHid")
            CONFIG.profiles[CONFIG.active_profile]["simulation_mode"] = getattr(CONFIG, "simulation_mode", "PS5")
            
        if CONFIG.add_profile(new_name):
            self.set_profile_assigned_apps(CONFIG.active_profile, [])
            CONFIG.profiles[CONFIG.active_profile]["change_profile_list"] = True
            CONFIG.profiles[CONFIG.active_profile]["profile_switching_combo"] = ""
            CONFIG.save_config()
            self._set_profile_button_text()
            self.apply_profile_switch()
            self.refresh_assigned_apps_ui()

    def on_rename_profile(self):
        current_name = CONFIG.active_profile
        new_name = self.custom_askstring("Rename Profile", f"Rename '{current_name}' to:", initialvalue=current_name)
        if new_name and new_name != current_name:
            if new_name in CONFIG.profiles:
                self.custom_messagebox("Error", f"Profile '{new_name}' already exists.", type="error")
            else:
                if CONFIG.rename_profile(new_name):
                    self._set_profile_button_text()
                    self.refresh_assigned_apps_ui()

    def on_reset_profile(self):
        current_name = CONFIG.active_profile
        if self.custom_messagebox("Reset Profile", f"Are you sure you want to reset profile '{current_name}'?", type="yesno"):
            keep_change_profile_list = bool(CONFIG.profiles.get(current_name, {}).get("change_profile_list", False))
            if CONFIG.reset_profile_to_default(current_name):
                if current_name in CONFIG.profiles:
                    CONFIG.profiles[current_name]["change_profile_list"] = keep_change_profile_list
                    CONFIG.save_config()
                # We can just apply the profile switch to reload everything from CONFIG.profiles
                self.apply_profile_switch()
                self.refresh_assigned_apps_ui()
                refresh_popup_rows = getattr(self, "refresh_profile_popup_rows", None)
                if callable(refresh_popup_rows) and getattr(self, "profile_popup", None) is not None and self.profile_popup.winfo_exists():
                    refresh_popup_rows()
                
    def on_delete_profile(self):
        if len(CONFIG.profiles) <= 1:
            self.custom_messagebox("Delete Profile", "Cannot delete the last profile.", type="warning")
            return
            
        current_name = CONFIG.active_profile
        if self.custom_messagebox("Delete Profile", f"Are you sure you want to delete profile '{current_name}'?", type="yesno"):
            if CONFIG.delete_profile():
                self._set_profile_button_text()
                # Since the old profile is deleted, we just apply the new profile directly
                self.apply_profile_switch()
                self.refresh_assigned_apps_ui()

    def refresh_ui_for_profile(self):
        self._set_profile_button_text()
        self.layout_switch.set_value(CONFIG.abxy_mode)
        self.rumble_mode_switch.set_value(getattr(CONFIG, "rumble_mode", "Xbox"))
        self.update_rumble_mode_ui(getattr(CONFIG, "rumble_mode", "Xbox"))
        self.vibration_strength_scale.set(CONFIG.vibration_strength)
        self.vibration_frequency_scale.set(CONFIG.vibration_frequency)
        if hasattr(self, "rumble_delay_entry"):
            self.rumble_delay_entry.delete(0, tk.END)
            self.rumble_delay_entry.insert(0, str(getattr(CONFIG, "rumble_delay_ms", 0)))
        self._refresh_mapping_comboboxes()
        if hasattr(self, 'gc_trigger_combo'):
            current_val = getattr(CONFIG, "gc_trigger_mode", "100% at Bump")
            try:
                idx = self.gc_trigger_values.index(current_val)
                self.gc_trigger_combo.set(self.gc_trigger_labels[idx])
            except ValueError:
                self.gc_trigger_combo.set(self.gc_trigger_labels[1])
                
            if hasattr(self, 'gc_click_map_frame'):
                if current_val == "100% at Max":
                    self.gc_click_map_frame.pack_forget()
                else:
                    self.gc_click_map_frame.pack(side=tk.LEFT, padx=(int(5 * scaling_factor), 0))

        self.refresh_assigned_apps_ui()

        # Update Built-in Gyro Mouse
        if hasattr(self, 'gyro_mode_switch'):
            mode_value = getattr(CONFIG, "gyro_mode", "World")
            self.gyro_mode_switch.set_value(mode_value if mode_value in ("World", "Yaw") else "World")
        if hasattr(self, 'gyro_act_switch'):
            self.gyro_act_switch.set_value(getattr(CONFIG, "gyro_activation_mode", "Toggle"))
        if hasattr(self, 'mode_shift_switch'):
            self.mode_shift_switch.set_value(CONFIG.mode_shift_enabled)
        if hasattr(self, 'gyro_control_switch'):
            gyro_control_mode = "Steering" if getattr(CONFIG, "gyro_mode", "World") == "Roll" else getattr(CONFIG, "gyro_control_mode", "Mouse")
            self.gyro_control_switch.set_value(gyro_control_mode)
            self._update_gyro_control_visibility(gyro_control_mode)
        if hasattr(self, 'sens_scale'):
            self._updating_gyro_control_sensitivity = True
            self.sens_scale.set(self._current_gyro_control_sensitivity())
            self._updating_gyro_control_sensitivity = False
        if hasattr(self, 'stick_scale'):
            self.stick_scale.set(getattr(CONFIG, "stick_mouse_sensitivity", 20.0))
                
        # Update Gyro Passthrough Mode
        if hasattr(self, 'passthrough_mode_switch'):
            current_passthrough = getattr(CONFIG, "gyro_passthrough_mode", "Default")
            self.passthrough_mode_switch.set_value(current_passthrough)
            try:
                idx = self.passthrough_mode_switch.values.index(current_passthrough)
                self.update_passthrough_mode(current_passthrough)
            except ValueError:
                pass
                
        # Update Horizon Lock
        if hasattr(self, 'stabilized_gyro_switch'):
            self.stabilized_gyro_switch.set_value(getattr(CONFIG, "stabilized_gyro", False))
                
        if hasattr(self, 'steam_roll_comp_switch'):
            self.steam_roll_comp_switch.set_value(getattr(CONFIG, "steam_roll_compensation", False))
                
        if hasattr(self, 'deadzone_scale'):
            self.deadzone_scale.set(getattr(CONFIG, "virtual_gyro_soft_deadzone", 2.0))
                
        # Update Cemuhook Sensitivity
        if hasattr(self, 'cemuhook_sens_scale'):
            self.cemuhook_sens_scale.set(getattr(CONFIG, "cemuhook_sensitivity", 1))
                
        # Update DJG Settings as the last step. The DJG handlers each rebuild the
        # player area, so suppress those rebuilds and do a single one at the end to
        # avoid the player slots flashing/ghosting several times during the switch.
        self._suppress_player_slot_refresh = True
        self._player_slot_refresh_pending = False
        try:
            if hasattr(self, 'djg_enabled_switch'):
                djg_enabled = getattr(CONFIG, "djg_enabled", False)
                self.djg_enabled_switch.set_value(djg_enabled)
                self.update_djg_enabled_setting(djg_enabled)

            if hasattr(self, 'djg_dominant_switch'):
                djg_dominant = getattr(CONFIG, "djg_dominant_side", "Left")
                self.djg_dominant_switch.set_value(djg_dominant)
                self.update_djg_dominant_setting(djg_dominant)

            if hasattr(self, 'djg_mode_combo'):
                djg_mode = getattr(CONFIG, "djg_mode", "Single Side Toggle")
                self.djg_mode_var.set(djg_mode)
                self.update_djg_mode_setting(djg_mode)

            if hasattr(self, 'djg_activation_switch'):
                djg_activation = getattr(CONFIG, "djg_activation", "Toggle")
                self.djg_activation_switch.set_value(djg_activation)
                self.update_djg_activation_setting(djg_activation)
        finally:
            self._suppress_player_slot_refresh = False

        if getattr(self, '_player_slot_refresh_pending', False):
            self._player_slot_refresh_pending = False
            self.force_refresh_player_slots()

    def on_setting_changed(self, event=None):
        def get_mapping(key, mapping_scope=None):
            suffix = self._mapping_scope_suffix(mapping_scope)
            attr_key = self._mapping_attr(key, suffix)
            combo = getattr(self, f"{attr_key}_combo", None)
            if combo is None: return "Default"
            val = combo.get()
            if val in ("Custom", GYRO_LOCK_LABEL, MODE_SHIFT_LABEL):
                curr = CONFIG.get_mapping_setting_scoped(key, "Default", mapping_scope)
                if curr.startswith("Custom"):
                    return curr
            return val

        # Only write back the scope the user is actually editing. The other
        # scope's config is already kept in sync at the config level (In-app
        # Gyro cross-mapping) and its hidden combos hold stale values, so
        # writing them back here would clobber the just-applied sync.
        active_scope = "in_app_gyro_mode_mappings" if getattr(self, "settings_active_tab", None) == "in_app_gyro_mode_mapping" else None
        for mapping_scope in (active_scope,):
            suffix = self._mapping_scope_suffix(mapping_scope)
            for key in [
                "home", "capt", "c", "plus", "minus",
                "a", "b", "x", "y",
                "up", "down", "left", "right",
                "zl", "l", "zr", "r",
                "l_stk", "r_stk",
                "gl", "gr", "sll", "srl", "slr", "srr",
                "gc_l_click", "gc_r_click"
            ]:
                attr_key = self._mapping_attr(key, suffix)
                if getattr(self, f"{attr_key}_combo", None) is not None:
                    CONFIG.set_mapping_setting_scoped(key, get_mapping(key, mapping_scope), mapping_scope)
            for key in ["l_joystick", "r_joystick"]:
                attr_key = self._mapping_attr(key, suffix)
                combo = getattr(self, f"{attr_key}_combo", None)
                if combo is not None:
                    CONFIG.set_mapping_setting_scoped(key, combo.get(), mapping_scope)
        if hasattr(self, 'gc_trigger_combo'):
            pass # Value is already saved by the Combobox command
        CONFIG.save_config()
        self._refresh_mapping_comboboxes()
        self.root.focus_set()

    def update(self, controllers_info):
        if self.main_frame is None:
            self.main_frame = tk.Frame(self.root, bg=background_color); self.main_frame.pack(pady=(10, 5), fill=tk.Y)
            self.players_info = None
        self.current_controllers = controllers_info
        
        if hasattr(self, 'djg_dominant_switch'):
            self.djg_dominant_switch.set_value(getattr(CONFIG, "djg_dominant_side", "Left"))
        
        # Check if the driver type has been changed/fallback under the hood
        active_driver = getattr(CONFIG, "driver_type", "WinUHid")
        if hasattr(self, 'driver_switch') and self.driver_switch.values[self.driver_switch.current_index] != active_driver:
            if active_driver == "ViGEmBus":
                CONFIG.simulation_mode = CONFIG.vigembus_sim_mode
            elif active_driver == "USBIP":
                CONFIG.simulation_mode = CONFIG.usbip_sim_mode
            else:
                CONFIG.simulation_mode = CONFIG.winuhid_sim_mode
            CONFIG.save_config()
            
            self.driver_switch.set_value(active_driver)
            self.update_driver_button()
            if active_driver == "ViGEmBus":
                self.sim_mode_switch.update_options(["Xbox360", "PS4"], ["Xbox360", "PS4"], CONFIG.simulation_mode)
            elif active_driver == "USBIP":
                self.sim_mode_switch.update_options(["Switch1", "Switch2", "PS5"], ["Switch1", "Switch2", "PS5"], CONFIG.simulation_mode)
            else:
                self.sim_mode_switch.update_options(["Xbox One", "PS4", "PS5"], ["Xbox One", "PS4", "PS5"], CONFIG.simulation_mode)
        # A slot is only "connected" if the VirtualController exists AND has physical controllers
        any_connected = any(c is not None and len(getattr(c, 'controllers', [])) > 0 for c in controllers_info)
        self.no_controllers = not any_connected
        if any_connected:
            if self.players_info is None:
                for w in self.main_frame.winfo_children(): w.destroy()
                
                self.row1 = tk.Frame(self.main_frame, bg=background_color)
                self.row1.pack(pady=5, fill=tk.X)
                
                self.players_info = []
                for i in range(4):
                    parent_row = self.row1
                    p = PlayerInfoBlock(parent_row, self)
                    p.main_frame.pack(padx=10, pady=10, side=tk.LEFT)
                    self.players_info.append(p)
            for i, player_info in enumerate(self.players_info):
                vc = controllers_info[i] if i < len(controllers_info) else None
                if vc is not None and len(vc.controllers) > 0: 
                    player_info.displayControllersInfo(vc)
                else: 
                    player_info.clearControllerInfo()
        else:
            if self.players_info is not None:
                for p in self.players_info: p.main_frame.destroy()
                self.players_info = None
            if not any(isinstance(w, tk.Label) and w.cget("text").startswith("Press button") for w in self.main_frame.winfo_children()):
                for w in self.main_frame.winfo_children(): w.destroy()
                tk.Label(self.main_frame, text="Press button of a paired controller, or hold sync button to pair", font=self.font, bg=background_color, fg=text_color).pack()
                tk.Label(self.main_frame, image=self.pairing_hint_image, bg=background_color).pack(pady=10)

    def hide_to_tray(self):
        self.root.withdraw()
        if not hasattr(self, 'tray_icon') or self.tray_icon is None:
            self.setup_tray()
        else:
            try: self.tray_icon.run_detached()
            except: pass

    def show_window(self, icon=None, item=None):
        if hasattr(self, 'tray_icon') and self.tray_icon:
            self.tray_icon.stop()
            self.tray_icon = None
        self.root.after(0, self.root.deiconify)

    def setup_tray(self):
        try:
            img = Image.open(get_resource('images/icon.png'))
        except:
            img = Image.new('RGB', (64, 64), color=(0, 195, 227)) # Cyan fallback
        
        menu = (item('Show', self.show_window, default=True), item('Exit', lambda: self.root.after(0, self.on_quit)))
        self.tray_icon = pystray.Icon("Switch2Controllers", img, "Switch2 Controllers", menu, action=self.show_window)
        self.tray_icon.run_detached()

    def on_quit(self):
        if getattr(self, 'is_cleaning_up', False): return
        
        # Restore window procedure
        if hasattr(self, 'old_wndproc') and self.old_wndproc:
            try:
                hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
                try:
                    SetWindowLong = ctypes.windll.user32.SetWindowLongPtrW
                except AttributeError:
                    SetWindowLong = ctypes.windll.user32.SetWindowLongW
                SetWindowLong.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
                SetWindowLong.restype = ctypes.c_void_p
                SetWindowLong(hwnd, win32con.GWL_WNDPROC, self.old_wndproc)
            except Exception as e:
                logger.debug(f"Failed to restore old window proc: {e}")

        # Fallback query current root window geometry directly before saving
        try:
            if self.root and self.root.state() == 'normal':
                w = self.root.winfo_width()
                h = self.root.winfo_height()
                rx = self.root.winfo_x()
                ry = self.root.winfo_y()
                if w > 100 and h > 100:
                    self.last_width = w
                    self.last_height = h
                    self.last_x = rx
                    self.last_y = ry
        except Exception:
            pass
            
        # Save last window size if we tracked a normal state size
        if getattr(self, 'last_width', None) is not None and getattr(self, 'last_height', None) is not None:
            CONFIG.window_width = self.last_width
            CONFIG.window_height = self.last_height
            CONFIG.window_x = getattr(self, 'last_x', None)
            CONFIG.window_y = getattr(self, 'last_y', None)
            CONFIG.save_config()
            
        self.is_cleaning_up = True; self.is_quitting = True; set_shutting_down(True); self.root.withdraw()
        if hasattr(self, 'tray_icon') and self.tray_icon:
            try: self.tray_icon.stop()
            except: pass
        def cleanup():
            try:
                vcs = [vc for vc in getattr(self, 'current_controllers', []) if vc and getattr(vc, 'loop', None) and vc.loop.is_running()]
                if vcs:
                    async def disconnect():
                        for vc in vcs:
                            if hasattr(vc, 'vg_controller') and vc.vg_controller:
                                try: vc.vg_controller.unregister_notification()
                                except: pass
                            for c in vc.controllers[:]:
                                if c.client and c.client.is_connected: 
                                    await c.disconnect()
                                    await asyncio.sleep(0.3)
                        await asyncio.sleep(3.5)
                    
                    fut = asyncio.run_coroutine_threadsafe(disconnect(), vcs[0].loop)
                    try:
                        # Increased timeout protection to 20 seconds to ensure clean sequential shutdown for 3+ controllers
                        fut.result(timeout=20.0)
                    except:
                        pass
                # Issue 5: even if there were no active virtual controllers (e.g. a
                # controller was mid-connect), make sure the ESP32-S3 stops scanning,
                # disables auto-connect and drops any remaining BLE links before exit,
                # so nothing stays connected and the bridge idles until the next launch.
                try:
                    from usb_serial_bridge import shutdown_all_bridges
                    shutdown_all_bridges()
                except Exception:
                    pass
            except: pass
            finally: self.root.after(0, lambda: (self.root.destroy(), os._exit(0)))
        threading.Thread(target=cleanup, daemon=True).start()

    def handle_power_event(self, wparam):
        current_time = time.strftime("%H:%M:%S")
        if wparam == win32con.PBT_APMSUSPEND:
            logger.info(f"[{current_time}] System Suspend detected (PBT_APMSUSPEND). Starting cleanup...")
            set_suspending(True)
            
            if hasattr(self, 'current_controllers'):
                # Iterate and close each controller synchronously
                for vc in self.current_controllers:
                    if vc is not None:
                        # 1. Stop the 1000Hz loop thread and reset inputs
                        vc.running = False
                        vc.reset_inputs()
                        
                        # 2. ALSO stop physical controller threads to prevent background work
                        for c in vc.controllers:
                            c.interp_running = False
                            c.suspended = True 
                            c._is_suspending = True 
                        
                        # 3. IMMEDIATELY and SYNCHRONOUSLY destroy the virtual device handle
                        vc.force_close()
            
            # CRITICAL: Reset the ViGEm bus singleton to release the driver handle entirely
            from virtual_controller import reset_vigem_bus
            reset_vigem_bus()
            
            # Final pause to let any OS-level driver cleanup settle
            time.sleep(1.0)
            
            self.quit_event.set()
            self._is_restarting_discovery = False
            logger.info(f"[{current_time}] Suspend preparation complete. quit_event set.")
        
        elif wparam in [win32con.PBT_APMRESUMESUSPEND, 0x0012]: # PBT_APMRESUMESUSPEND or PBT_APMRESUMEAUTOMATIC
            event_name = "PBT_APMRESUMESUSPEND" if wparam == win32con.PBT_APMRESUMESUSPEND else "PBT_APMRESUMEAUTOMATIC"
            logger.info(f"[{current_time}] System Resume detected ({event_name}).")
            
            # Reset suspension state immediately
            set_suspending(False)
            self.quit_event.clear()
            
            # CRITICAL: Force immediate cleanup of any potentially stale handles that survived
            # This also re-initializes the ViGEm bus singleton via its internal call.
            emergency_cleanup()
            
            # Force UI to clear old/stale controller displays immediately
            self.root.after(0, lambda: self.update([]))
            
            logger.info(f"[{current_time}] quit_event cleared. UI cleared. Preparing to restart discovery...")
            
            if getattr(self, '_is_restarting_discovery', False):
                logger.info("Restart already in progress. Skipping...")
                return
            self._is_restarting_discovery = True

            def restart():
                try:
                    # Longer delay to ensure Bluetooth radio and driver handles are stable
                    # 7 seconds is safer for some slower BT adapters on wake
                    time.sleep(7.0)
                    
                    if not getattr(self, '_is_restarting_discovery', False): return
                    
                    # Double-check we didn't suspend again during the sleep
                    from discoverer import _IS_SUSPENDING
                    if _IS_SUSPENDING:
                        logger.info("System is suspending again. Aborting restart.")
                        self._is_restarting_discovery = False
                        return
                        
                    logger.info("Restarting discovery loop...")
                    self.start_discoverer_thread()
                except Exception as e:
                    logger.error(f"Restart failed: {e}")
                finally:
                    self._is_restarting_discovery = False

            threading.Thread(target=restart, daemon=True).start()

    def start_battery_refresh_timer(self):
        if not getattr(self, 'is_quitting', False):
            if hasattr(self, 'current_controllers') and self.current_controllers:
                try:
                    self.update(self.current_controllers)
                except Exception as e:
                    logger.debug(f"Failed to refresh battery indicators: {e}")
            self.root.after(300000, self.start_battery_refresh_timer) # 5 minutes

    def start_esp32s3_refresh_timer(self):
        if not getattr(self, 'is_quitting', False):
            self.refresh_esp32s3_status_async()
            self.root.after(5000, self.start_esp32s3_refresh_timer)

    def start_detection_and_discovery(self):
        if getattr(self, '_startup_detection_done', False) or getattr(self, 'is_quitting', False):
            return
        self._startup_detection_done = True

        def start_driver_check_and_discovery():
            try:
                self.check_driver_installation()
            except Exception as e:
                logger.debug(f"Startup driver check failed: {e}")
            self.start_discoverer_thread()

        def worker():
            status = None
            detected = False
            try:
                from usb_serial_bridge import detect_bridge
                status = detect_bridge()
                detected = bool(status and status.board_present)
            except Exception as e:
                logger.debug(f"Startup ESP32-S3 detection failed: {e}")

            def apply_status():
                if getattr(self, 'is_quitting', False):
                    return
                self.esp32s3_bridge_status = status
                self.esp32s3_detected = detected
                self._esp32s3_was_detected = detected
                self._esp32s3_current_seen = bool(status and getattr(status, "bridge_ready", False))
                self.update_driver_buttons_visibility()

                def after_auto_update(ok):
                    if ok:
                        self.root.after(1000, lambda: self.wait_for_current_esp32s3_then(start_driver_check_and_discovery))
                    else:
                        self.root.after(0, start_driver_check_and_discovery)

                if self.maybe_auto_update_esp32s3_firmware(status, on_complete=after_auto_update):
                    return
                start_driver_check_and_discovery()

            try:
                self.root.after(0, apply_status)
            except RuntimeError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def start(self):
        self.is_quitting = False
        def callback(vcs):
            if not getattr(self, 'is_quitting', False):
                try:
                    self.message_queue.put(vcs)
                    self.root.event_generate(CONTROLLER_UPDATED_EVENT)
                except Exception as e:
                    logger.debug(f"Ignored Tkinter event generation error: {e}")
        self.discoverer_callback = callback
        self.root.bind(CONTROLLER_UPDATED_EVENT, lambda e: self.update(self.message_queue.get()))
        
        self.power_listener.start()
        
        if CONFIG.start_minimized:
            self.hide_to_tray()
        else:
            # Reveal the window only once its content is painted: deiconify while fully
            # transparent, pre-render every tab (so neither the first show nor the first
            # tab switch flashes a white background), then fade it in opaque.
            try:
                self.root.attributes("-alpha", 0.0)
            except Exception:
                pass
            self.root.deiconify()
            self._prerender_settings_tabs()
            self.root.update_idletasks()
            try:
                self.root.attributes("-alpha", 1.0)
            except Exception:
                pass

        self.root.after(100, self.start_detection_and_discovery)
            
        # Start battery refresh timer (5 minutes)
        self.root.after(300000, self.start_battery_refresh_timer)
        self.root.after(5000, self.start_esp32s3_refresh_timer)
        self.root.after(1000, self.poll_assigned_app_focus)
            
        self.root.protocol("WM_DELETE_WINDOW", self.on_quit); self.root.mainloop()

if __name__ == "__main__":
    # Scheduled-task entry: when invoked elevated with this flag, just disable the
    # DualSense audio endpoint and exit (do NOT launch the GUI).  Used so a
    # non-elevated session can trigger the elevated disable silently via Task
    # Scheduler instead of a UAC prompt on every controller connect.
    import sys as _sys
    if "--disable-dualsense-audio-endpoint" in _sys.argv:
        try:
            from dualsense_audio_endpoint import _apply
            _apply("Disable")
        except Exception as e:
            import traceback, os, tempfile
            with open(os.path.join(tempfile.gettempdir(), "audio_disable_error.log"), "w") as f:
                f.write(traceback.format_exc())
        _sys.exit(0)

    disable_power_throttling()
    win = ControllerWindow()
    win.init_interface(); win.start()

