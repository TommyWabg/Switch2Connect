import queue
import time
import webbrowser
import threading
import tkinter as tk
from tkinter import ttk
import tkinter.font as tkFont
import yaml
import logging
import asyncio
import os
import ctypes
from controller import Controller, INPUT_REPORT_UUID, COMMAND_RESPONSE_UUID, NSO_GAMECUBE_CONTROLLER_PID
from discoverer import start_discoverer, set_shutting_down, set_suspending, emergency_cleanup
from config import get_resource, CONFIG, BACK_BUTTON_OPTIONS, get_driver_path
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
resolution_ratio = (screen_height / 1440.0) * getattr(CONFIG, 'ui_scale', 1.0)

scaling_factor = 1.2 * resolution_ratio

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

controller_frame_size = int(200 * resolution_ratio)
battery_height = int(40 * resolution_ratio)

# Current Color Scheme (Space Gray / Cyan Accent)
background_color = "#2D2D2D"
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
            btn = tk.Button(frame, text=label, width=w, font=scale_font(("Arial", 12, "bold")),
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

    def update_options(self, labels, values, current_value):
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
            
            btn = tk.Button(frame, text=label, width=8, font=scale_font(("Arial", 12, "bold")),
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
                    w = int(orig_w * sf)
                    h = int(orig_h * sf)
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
        
        bat_w, bat_h = int(28 * sf), int(14 * sf)
        self.battery_h = load_img("images/battery_h.png", bat_w, bat_h)
        self.battery_m = load_img("images/battery_m.png", bat_w, bat_h)
        self.battery_l = load_img("images/battery_l.png", bat_w, bat_h)
        
        self.player_leds = {nb: load_img(f"images/player{nb}.png") for nb in range(1,5)}

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
            self.close_btn = tk.Button(self.controllers_frame, text="✖", bg=block_color, fg="#FFFFFF", bd=0, 
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
            self.player_row = tk.Frame(self.main_frame, bg=player_number_bg_color, width=controller_frame_size, height=int(40 * scaling_factor))
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
        
        self.lbl_title = tk.Label(frame, text="Switch 2 Controller", fg="#0a84ff", bg="#1c1c1e", font=scale_font(("Segoe UI", 12, "bold")))
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
        
        import utils
        utils.change_profile_callback = self.on_cycle_profile
        utils.force_ui_update_callback = self.force_refresh_player_slots

    def check_vigembus_installation(self, save=True):
        installed = is_vigembus_installed()
        if not installed:
            CONFIG.vigembus_installed = False
            if save:
                CONFIG.save_config()
            
            from tkinter import messagebox
            import webbrowser
            
            answer = messagebox.askyesno(
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
            from tkinter import messagebox
            messagebox.showwarning(
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
                from tkinter import messagebox
                answer = messagebox.askyesno(
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

        # 如果yaml裡有已安裝的紀錄：開啟app時不再檢查是否有安裝，無條件開啟app
        if getattr(CONFIG, 'driver_installed', False):
            return

        if is_driver_installed():
            # 如果檢查結果是已安裝，自動記錄到yaml裡
            CONFIG.driver_installed = True
            if save:
                CONFIG.save_config()
            return
            
        from tkinter import messagebox
        
        answer = messagebox.askyesno(
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
                progress_win.geometry(f"{int(450 * scaling_factor)}x{int(130 * scaling_factor)}+150+150")
                progress_win.resizable(False, False)
                progress_win.config(bg="#1E1E1E")
                progress_win.transient(self.root)
                progress_win.grab_set()
                
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
                info.hwnd = None
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
                    messagebox.showerror("Error", "Driver installation was cancelled or failed to start (UAC prompt declined).")
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
                        messagebox.showinfo("Success", "WinUHid driver installed successfully.")
                else:
                    messagebox.showerror(
                        "Error",
                        "Driver installation was not completed or failed.\nSome emulator functions may not work."
                    )
                self.update_driver_button()
            except Exception as e:
                messagebox.showerror("Error", f"Failed to start the installer: {e}")
        else:
            messagebox.showerror("Error", "Could not find install_driver.ps1. Please verify the integrity of the application files.")

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
                progress_win.geometry(f"{int(450 * scaling_factor)}x{int(130 * scaling_factor)}+150+150")
                progress_win.resizable(False, False)
                progress_win.config(bg="#1E1E1E")
                progress_win.transient(self.root)
                progress_win.grab_set()
                
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
                info.hwnd = None
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
                    messagebox.showerror("Error", "Driver uninstallation was cancelled or failed to start (UAC prompt declined).")
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
                    messagebox.showinfo("Success", "WinUHid driver uninstalled successfully.")
                else:
                    messagebox.showerror(
                        "Error",
                        "Driver uninstallation failed or was cancelled."
                    )
                self.update_driver_button()
            except Exception as e:
                messagebox.showerror("Error", f"Failed to start the uninstaller: {e}")
        else:
            messagebox.showerror("Error", "Could not find uninstall_driver.ps1. Please verify the integrity of the application files.")

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
                progress_win.geometry(f"{int(450 * scaling_factor)}x{int(130 * scaling_factor)}+150+150")
                progress_win.resizable(False, False)
                progress_win.config(bg="#1E1E1E")
                progress_win.transient(self.root)
                progress_win.grab_set()
                
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
                info.hwnd = None
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
                    messagebox.showerror("Error", "ViGEmBus uninstallation was cancelled or failed to start (UAC prompt declined).")
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
                    messagebox.showinfo("Success", "ViGEmBus driver uninstalled successfully. A system reboot is highly recommended.")
                else:
                    messagebox.showerror(
                        "Error",
                        "ViGEmBus uninstallation failed or was cancelled."
                    )
                self.update_driver_button()
            except Exception as e:
                messagebox.showerror("Error", f"Failed to start the uninstaller: {e}")
        else:
            messagebox.showerror("Error", "Could not find uninstall_vigembus.ps1. Please verify the integrity of the application files.")

        if discoverer_was_running:
            self.start_discoverer_thread()

    def on_driver_btn_clicked(self):
        driver_type = getattr(CONFIG, "driver_type", "WinUHid")
        if driver_type == "ViGEmBus":
            if getattr(CONFIG, 'vigembus_installed', False):
                from tkinter import messagebox
                if messagebox.askyesno("Uninstall Driver", "Are you sure you want to uninstall the ViGEmBus driver?\n(Requires administrator privileges.)"):
                    self.run_vigembus_uninstall()
            else:
                import webbrowser
                webbrowser.open("https://github.com/nefarius/ViGEmBus/releases")
        else:
            if getattr(CONFIG, 'driver_installed', False):
                from tkinter import messagebox
                if messagebox.askyesno("Uninstall Driver", "Are you sure you want to uninstall the WinUHid driver?\n(Requires administrator privileges.)"):
                    self.run_driver_uninstall()
            else:
                self.run_driver_install()

    def update_driver_buttons_visibility(self):
        if not hasattr(self, 'top_btn_frame'):
            return
        scaling_factor = getattr(self, 'scaling_factor', 1.0)
        
        # First, unpack all frames from top_btn_frame to preserve order
        if hasattr(self, 'driver_frame'): self.driver_frame.pack_forget()
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
                
        # Pack the rest of the buttons
        if hasattr(self, 'startup_frame'): self.startup_frame.pack(side=tk.LEFT, padx=int(5 * scaling_factor))
        if hasattr(self, 'min_frame'): self.min_frame.pack(side=tk.LEFT, padx=int(5 * scaling_factor))
        if hasattr(self, 'hide_frame'): self.hide_frame.pack(side=tk.LEFT, padx=int(5 * scaling_factor))

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
        from tkinter import messagebox
        if os.path.exists(usbip_exe):
            if messagebox.askyesno("Uninstall USBIP Driver", "Are you sure you want to uninstall the USBIP driver?\n(Requires administrator privileges.)"):
                self.run_usbip_uninstall()
        else:
            if messagebox.askyesno(
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
                progress_win.geometry(f"{int(450 * scaling_factor)}x{int(130 * scaling_factor)}+150+150")
                progress_win.resizable(False, False)
                progress_win.config(bg="#1E1E1E")
                progress_win.transient(self.root)
                progress_win.grab_set()
                
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
                info.hwnd = None
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
                    messagebox.showerror("Error", "USBIP driver installation was cancelled or failed to start (UAC prompt declined).")
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
                        messagebox.showinfo("Success", "USBIP-win2 driver installed successfully.")
                else:
                    messagebox.showerror(
                        "Error",
                        "USBIP driver installation was not completed or failed."
                    )
                self.update_usbip_button()
            except Exception as e:
                messagebox.showerror("Error", f"Failed to start the USBIP installer: {e}")
        else:
            messagebox.showerror("Error", "Could not find install_usbip.ps1. Please verify the integrity of the application files.")

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
                progress_win.geometry(f"{int(450 * scaling_factor)}x{int(130 * scaling_factor)}+150+150")
                progress_win.resizable(False, False)
                progress_win.config(bg="#1E1E1E")
                progress_win.transient(self.root)
                progress_win.grab_set()
                
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
                info.hwnd = None
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
                    messagebox.showerror("Error", "USBIP driver uninstallation was cancelled or failed to start (UAC prompt declined).")
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
                    messagebox.showinfo("Success", "USBIP driver uninstalled successfully.")
                else:
                    messagebox.showinfo("Information", "USBIP driver uninstaller closed.")
                self.update_usbip_button()
            except Exception as e:
                messagebox.showerror("Error", f"Failed to start the USBIP uninstaller: {e}")
        else:
            messagebox.showerror("Error", f"Could not find uninstaller at {uninstaller_exe}.")

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
        self.root.withdraw() # Hide immediately to prevent blank window during check_driver_installation()
        
        # 2. Re-apply global scaling factors to ensure they are up to date with config
        global scaling_factor, controller_frame_size, battery_height
        # resolution_ratio is calculated at the top of the file
        scaling_factor = 1.2 * resolution_ratio
        controller_frame_size = int(200 * scaling_factor)
        battery_height = int(40 * scaling_factor)


        self.check_driver_installation()
        
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
        self.root.title("Switch2 Controllers")
        
        # 3. Handle window geometry & minsize (remembering size)
        default_w = int(1460 * resolution_ratio)
        default_h = int(1320 * resolution_ratio)
        w = default_w
        h = default_h
        self.root.geometry(f"{w}x{h}+50+50")
        self.root.minsize(int(1240 * resolution_ratio), int(920 * resolution_ratio))
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
            
            # Get expected outer dimensions corresponding to 1330x990 client size
            rect = win32gui.GetWindowRect(hwnd)
            self.expected_outer_w = rect[2] - rect[0]
            self.expected_outer_h = rect[3] - rect[1]

            # Structures for window sizing message interception
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
            self.root.geometry(f"{default_w}x{default_h}+50+50")
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
                        font=scale_font(("Arial", 12, "bold")))
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
        self.root.option_add("*TCombobox*Listbox.font", scale_font(("Arial", 12, "bold")))
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

        self.init_settings_panel()
        self.init_compensation_panel()
        self.init_djg_panel()
        self.init_gyro_settings_panel()
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



        self.update_driver_button()
        self.update_usbip_button()

        self.update([None])

        def get_focusable_widgets(parent, lst=None):
            if lst is None:
                lst = []
            if parent.winfo_ismapped():
                if isinstance(parent, (tk.Button, ttk.Combobox, tk.Scale, tk.Entry, tk.Checkbutton, tk.Radiobutton)):
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
                if isinstance(e.widget, (tk.Button, ttk.Combobox, tk.Scale, tk.Entry, tk.Checkbutton, tk.Radiobutton)):
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
                    elif isinstance(focused, tk.Entry) and getattr(focused, 'is_custom_recording_entry', False):
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


    def on_configure(self, event):
        if event.widget == self.root:
            try:
                if self.root.state() == 'normal':
                    w = self.root.winfo_width()
                    h = self.root.winfo_height()
                    if w > 100 and h > 100:
                        self.last_width = w
                        self.last_height = h
            except Exception:
                pass

    def init_compensation_panel(self):
        self.comp_frame = tk.LabelFrame(self.root, text=" Gyro Passthrough For 3rd Party Apps ", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold")), padx=int(10 * scaling_factor), pady=int(10 * scaling_factor))
        self.comp_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=int(5 * scaling_factor))
        
        tk.Label(self.comp_frame, text="9-axis Assist:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).grid(row=0, column=0, padx=int(5 * scaling_factor), sticky="e")
        self.stabilized_gyro_switch = ToggleSwitch(self.comp_frame, labels=["ON", "OFF"], values=[True, False], initial_value=getattr(CONFIG, "stabilized_gyro", False), command=self.update_stabilized_gyro_setting, bg_color=background_color)
        self.stabilized_gyro_switch.grid(row=0, column=1, columnspan=2, padx=int(5 * scaling_factor), sticky="w")
        tk.Label(self.comp_frame, text="Horizon Lock:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).grid(row=0, column=3, padx=(int(20 * scaling_factor), int(5 * scaling_factor)), sticky="e")
        self.steam_roll_comp_switch = ToggleSwitch(self.comp_frame, labels=["ON", "OFF"], values=[True, False], initial_value=getattr(CONFIG, "steam_roll_compensation", False), command=self.update_steam_roll_comp_setting, bg_color=background_color)
        self.steam_roll_comp_switch.grid(row=0, column=4, columnspan=2, padx=int(5 * scaling_factor), sticky="w")

        tk.Label(self.comp_frame, text="Deadzone:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).grid(row=0, column=6, padx=(int(20 * scaling_factor), int(5 * scaling_factor)), sticky="e")
        self.deadzone_scale = tk.Scale(
            self.comp_frame,
            from_=0.0,
            to=5.0,
            resolution=0.5,
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
            font=scale_font(("Arial", 12, "bold")),
            command=self.update_virtual_gyro_soft_deadzone_setting
        )
        self.deadzone_scale.set(getattr(CONFIG, "virtual_gyro_soft_deadzone", 2.0))
        self.deadzone_scale.grid(row=0, column=7, columnspan=2, padx=int(5 * scaling_factor), sticky="w")

        tk.Label(self.comp_frame, text="Mode:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).grid(row=1, column=0, padx=int(5 * scaling_factor), pady=(int(5 * scaling_factor), 0), sticky="e")
        self.passthrough_mode_switch = ToggleSwitch(self.comp_frame, labels=["Default", "Cemuhook"], values=["Default", "Cemuhook"], 
initial_value=getattr(CONFIG, "gyro_passthrough_mode", "Default"), command=self.update_passthrough_mode, 
bg_color=background_color, widths=[8, 10])
        self.passthrough_mode_switch.grid(row=1, column=1, columnspan=2, padx=int(5 * scaling_factor), pady=(int(5 * scaling_factor), 0), sticky="w")

        self.sens_label = tk.Label(self.comp_frame, text="Sensitivity:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold")))
        self.cemuhook_sens_scale = tk.Scale(
            self.comp_frame,
            from_=1,
            to=5,
            resolution=1,
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
            font=scale_font(("Arial", 12, "bold")),
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

    def init_djg_panel(self):
        self.djg_frame = tk.LabelFrame(self.root, text=" Dual Joy-con Gyro (DJG) ", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold")), padx=int(10 * scaling_factor), pady=int(10 * scaling_factor))
        self.djg_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=int(5 * scaling_factor))
        
        tk.Label(self.djg_frame, text="DJG:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).grid(row=0, column=0, padx=int(5 * scaling_factor), sticky="e")
        self.djg_enabled_switch = ToggleSwitch(self.djg_frame, labels=["ON", "OFF"], values=[True, False], initial_value=getattr(CONFIG, "djg_enabled", False), command=self.update_djg_enabled_setting, bg_color=background_color)
        self.djg_enabled_switch.grid(row=0, column=1, columnspan=2, padx=int(5 * scaling_factor), sticky="w")
        
        tk.Label(self.djg_frame, text="Dominant Side:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).grid(row=0, column=3, padx=(int(20 * scaling_factor), int(5 * scaling_factor)), sticky="e")
        self.djg_dominant_switch = ToggleSwitch(self.djg_frame, labels=["Left", "Right"], values=["Left", "Right"], initial_value=getattr(CONFIG, "djg_dominant_side", "Left"), command=self.update_djg_dominant_setting, bg_color=background_color)
        self.djg_dominant_switch.grid(row=0, column=4, columnspan=2, padx=int(5 * scaling_factor), sticky="w")
        
        tk.Label(self.djg_frame, text="Mode:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).grid(row=0, column=6, padx=(int(20 * scaling_factor), int(5 * scaling_factor)), sticky="e")
        
        self.djg_mode_var = tk.StringVar(value=getattr(CONFIG, "djg_mode", "Single Side Toggle"))
        djg_modes = ["Single Side Toggle", "Switch Dominant Side", "Switch Gyro Side"]
        
        # Calculate max width for dropdown
        max_mode_len = max(len(m) for m in djg_modes)
        
        self.djg_mode_combo = ttk.Combobox(self.djg_frame, textvariable=self.djg_mode_var, values=djg_modes, state="readonly", font=scale_font(("Arial", 12, "bold")), width=max_mode_len, justify="center")
        self.djg_mode_combo.grid(row=0, column=7, padx=int(5 * scaling_factor), sticky="w")
        self.djg_mode_combo.bind("<<ComboboxSelected>>", lambda e: self.update_djg_mode_setting(self.djg_mode_var.get()))

        tk.Label(self.djg_frame, text="Activation:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).grid(row=0, column=8, padx=(int(20 * scaling_factor), int(5 * scaling_factor)), sticky="e")
        self.djg_activation_switch = ToggleSwitch(self.djg_frame, labels=["Hold", "Toggle"], values=["Hold", "Toggle"], initial_value=getattr(CONFIG, "djg_activation", "Toggle"), command=self.update_djg_activation_setting, bg_color=background_color)
        self.djg_activation_switch.grid(row=0, column=9, columnspan=2, padx=int(5 * scaling_factor), sticky="w")

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


    def init_gyro_settings_panel(self):
        self.gyro_frame = tk.LabelFrame(self.root, text=" Built-in Gyro Mouse ", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold")), padx=int(10 * scaling_factor), pady=int(10 * scaling_factor))
        self.gyro_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=int(5 * scaling_factor))
        tk.Label(self.gyro_frame, text="Mode:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).grid(row=0, column=0, padx=int(5 * scaling_factor), sticky="e")
        self.gyro_mode_switch = ToggleSwitch(self.gyro_frame, labels=["9-Axis", "6-Axis", "Steering"], values=["World", "Yaw", "Roll"], initial_value=CONFIG.gyro_mode, command=self.update_mode_setting, bg_color=background_color)
        self.gyro_mode_switch.grid(row=0, column=1, columnspan=2, padx=int(5 * scaling_factor), sticky="w")
        tk.Label(self.gyro_frame, text="Sensitivity:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).grid(row=0, column=3, padx=(int(20 * scaling_factor), int(5 * scaling_factor)), sticky="e")
        self.sens_scale = tk.Scale(self.gyro_frame, from_=1, to=10, resolution=0.2, orient=tk.HORIZONTAL, length=int(120 * scaling_factor), bg=background_color, fg=text_color, troughcolor=button_gray, activebackground=highlight_color, highlightthickness=0, bd=0, sliderrelief=tk.FLAT, sliderlength=int(15 * scaling_factor), width=int(15 * scaling_factor), font=scale_font(("Arial", 12, "bold")), command=self.on_gyro_setting_changed)
        self.sens_scale.set(CONFIG.gyro_sensitivity)
        self.sens_scale.grid(row=0, column=4)

        self.gyro_calib_group_frame = tk.Frame(self.gyro_frame, bg=background_color)
        self.gyro_calib_group_frame.grid(row=0, column=5, columnspan=2, padx=(int(20 * scaling_factor), int(5 * scaling_factor)), sticky="w")

        self.calib_frame = tk.Frame(self.gyro_calib_group_frame, bg=button_gray)
        self.calib_frame.pack(side=tk.LEFT)
        self.calibrate_btn = tk.Button(self.calib_frame, text="Calibrate Gyro", command=self.on_calibrate_clicked, bg=button_gray, fg=text_color, bd=0, relief=tk.FLAT, font=scale_font(("Arial", 12, "bold")))
        self.calibrate_btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))

        self.calib_hint_label = tk.Label(self.gyro_calib_group_frame, text="Keep controller stationary\nbefore calibrating.", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold")), justify=tk.LEFT)
        self.calib_hint_label.pack(side=tk.LEFT, padx=int(10 * scaling_factor))

        mag_hint_frame = tk.Frame(self.gyro_frame, bg=background_color)
        mag_hint_frame.grid(row=1, column=5, columnspan=2, padx=(int(20 * scaling_factor), int(5 * scaling_factor)), pady=(int(10 * scaling_factor), 0), sticky="w")
        
        l1 = tk.Frame(mag_hint_frame, bg=background_color)
        l1.pack(side=tk.TOP, anchor="w")
        tk.Label(l1, text="Calibrate Mag (Mag Cal): Move controller in a", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT)

        l2 = tk.Frame(mag_hint_frame, bg=background_color)
        l2.pack(side=tk.TOP, anchor="w")
        
        lnk = tk.Label(l2, text="'figure 8'", bg=background_color, fg=highlight_color, font=scale_font(("Arial", 12, "bold", "underline")), cursor="hand2")
        lnk.pack(side=tk.LEFT)
        lnk.bind("<Button-1>", lambda e: (logger.info(f"Opening YouTube link via webbrowser..."), webbrowser.open("https://youtu.be/J_cZnPcW-Yw?si=ID2vdzURiOph8x77&t=6")))
        
        tk.Label(l2, text=" pattern during calibration.", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT)

        tk.Label(self.gyro_frame, text="Activation:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).grid(row=1, column=0, padx=int(5 * scaling_factor), pady=(int(10 * scaling_factor), 0), sticky="e")
        self.gyro_act_switch = ToggleSwitch(self.gyro_frame, labels=["Toggle", "Hold"], values=["Toggle", "Hold"], initial_value=CONFIG.gyro_activation_mode, command=self.update_act_setting, bg_color=background_color)
        self.gyro_act_switch.grid(row=1, column=1, columnspan=2, padx=int(5 * scaling_factor), pady=(int(10 * scaling_factor), 0), sticky="w")
        tk.Label(self.gyro_frame, text="Stick Assist:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).grid(row=1, column=3, padx=(int(20 * scaling_factor), int(5 * scaling_factor)), pady=(int(10 * scaling_factor), 0), sticky="e")
        self.stick_scale = tk.Scale(self.gyro_frame, from_=0, to=10, resolution=0.2, orient=tk.HORIZONTAL, length=int(120 * scaling_factor), bg=background_color, fg=text_color, troughcolor=button_gray, activebackground=highlight_color, highlightthickness=0, bd=0, sliderrelief=tk.FLAT, sliderlength=int(15 * scaling_factor), width=int(15 * scaling_factor), font=scale_font(("Arial", 12, "bold")), command=self.on_gyro_setting_changed)
        self.stick_scale.set(getattr(CONFIG, "stick_mouse_sensitivity", 5.0))
        self.stick_scale.grid(row=1, column=4, columnspan=1, pady=(int(10 * scaling_factor), 0), sticky="w")


    def init_auto_disconnect_panel(self):
        self.auto_disconnect_frame = tk.LabelFrame(self.root, text=" Auto Disconnect ", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold")), padx=int(10 * scaling_factor), pady=int(10 * scaling_factor))
        self.auto_disconnect_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=int(5 * scaling_factor))
        
        tk.Label(self.auto_disconnect_frame, text="Auto Disconnect:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).grid(row=0, column=0, padx=int(5 * scaling_factor), sticky="e")
        self.auto_disconnect_switch = ToggleSwitch(self.auto_disconnect_frame, labels=["OFF", "Inactive", "Absolute"], values=["OFF", "Inactive", "Absolute"], initial_value=getattr(CONFIG, "auto_disconnect_mode", "OFF"), command=self.update_auto_disconnect_mode, bg_color=background_color)
        self.auto_disconnect_switch.grid(row=0, column=1, padx=int(5 * scaling_factor), sticky="w")
        
        tk.Label(self.auto_disconnect_frame, text="Disconnect after:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).grid(row=0, column=2, padx=(int(20 * scaling_factor), int(5 * scaling_factor)), sticky="e")
        
        # Validation to only allow digits in time entries
        def validate_numeric(char):
            return char.isdigit() or char == ""
        vcmd = (self.root.register(validate_numeric), '%S')
        
        # Day Entry
        self.day_entry = tk.Entry(self.auto_disconnect_frame, width=4, bg=button_gray, fg=text_color, insertbackground=text_color, bd=0, relief=tk.FLAT, font=scale_font(("Arial", 12, "bold")), justify=tk.CENTER, validate="key", validatecommand=vcmd)
        self.day_entry.insert(0, str(getattr(CONFIG, "auto_disconnect_days", 0)))
        self.day_entry.grid(row=0, column=3, padx=int(2 * scaling_factor))
        self.day_entry.is_time_entry = True
        tk.Label(self.auto_disconnect_frame, text="Day", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).grid(row=0, column=4, padx=(0, int(10 * scaling_factor)), sticky="w")
        
        # Hour Entry
        self.hour_entry = tk.Entry(self.auto_disconnect_frame, width=4, bg=button_gray, fg=text_color, insertbackground=text_color, bd=0, relief=tk.FLAT, font=scale_font(("Arial", 12, "bold")), justify=tk.CENTER, validate="key", validatecommand=vcmd)
        self.hour_entry.insert(0, str(getattr(CONFIG, "auto_disconnect_hours", 0)))
        self.hour_entry.grid(row=0, column=5, padx=int(2 * scaling_factor))
        self.hour_entry.is_time_entry = True
        tk.Label(self.auto_disconnect_frame, text="Hour", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).grid(row=0, column=6, padx=(0, int(10 * scaling_factor)), sticky="w")
        
        # Minute Entry
        self.minute_entry = tk.Entry(self.auto_disconnect_frame, width=4, bg=button_gray, fg=text_color, insertbackground=text_color, bd=0, relief=tk.FLAT, font=scale_font(("Arial", 12, "bold")), justify=tk.CENTER, validate="key", validatecommand=vcmd)
        self.minute_entry.insert(0, str(getattr(CONFIG, "auto_disconnect_minutes", 0)))
        self.minute_entry.grid(row=0, column=7, padx=int(2 * scaling_factor))
        self.minute_entry.is_time_entry = True
        tk.Label(self.auto_disconnect_frame, text="Minute", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).grid(row=0, column=8, padx=(0, int(10 * scaling_factor)), sticky="w")
        
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

    def update_mode_setting(self, val):
        CONFIG.gyro_mode = val
        self.on_gyro_setting_changed()

    def update_stabilized_gyro_setting(self, val):
        CONFIG.stabilized_gyro = val
        try:
            with open(CONFIG.config_file_path, 'r', encoding='utf-8') as f: data = yaml.safe_load(f) or {}
            data['stabilized_gyro'] = val
            with open(CONFIG.config_file_path, 'w', encoding='utf-8') as f: yaml.dump(data, f, default_flow_style=False)
            logger.info(f"9-Axis Stabilization (for 6-Axis): {val}")
        except Exception as e:
            logger.error(f"Failed to save stabilized gyro setting: {e}")

    def update_steam_roll_comp_setting(self, val):
        CONFIG.steam_roll_compensation = val
        CONFIG.save_config()
        logger.info(f"Roll Compensation: {val}")

    def update_virtual_gyro_soft_deadzone_setting(self, val):
        val = float(val)
        CONFIG.virtual_gyro_soft_deadzone = val
        try:
            with open(CONFIG.config_file_path, 'r', encoding='utf-8') as f: data = yaml.safe_load(f) or {}
            data['virtual_gyro_soft_deadzone'] = val
            with open(CONFIG.config_file_path, 'w', encoding='utf-8') as f: yaml.dump(data, f, default_flow_style=False)
            logger.info(f"Third-Party Gyro Deadzone: {val}")
        except Exception as e:
            logger.error(f"Failed to save virtual gyro soft deadzone setting: {e}")

    def update_mouse_setting(self, val):
        CONFIG.mouse_config.enabled = val
        try:
            with open(CONFIG.config_file_path, 'r', encoding='utf-8') as f: data = yaml.safe_load(f) or {}
            if 'mouse' not in data: data['mouse'] = {}
            data['mouse']['enabled'] = val
            with open(CONFIG.config_file_path, 'w', encoding='utf-8') as f: yaml.dump(data, f, default_flow_style=False)
        except Exception as e: logger.error(f"Failed to save mouse settings: {e}")

    def update_act_setting(self, val):
        CONFIG.gyro_activation_mode = val
        self.on_gyro_setting_changed()

    def update_mouse_sensitivity(self, val):
        new_sens = float(val)
        CONFIG.mouse_config.sensitivity = new_sens
        try:
            with open(CONFIG.config_file_path, 'r', encoding='utf-8') as f: data = yaml.safe_load(f) or {}
            if 'mouse' not in data: data['mouse'] = {}
            data['mouse']['sensitivity'] = new_sens
            with open(CONFIG.config_file_path, 'w', encoding='utf-8') as f: yaml.dump(data, f, default_flow_style=False)
        except Exception as e: logger.error(f"Failed to save mouse sensitivity: {e}")

    def update_ir_activate_threshold(self, val):
        new_val = int(float(val))
        CONFIG.mouse_config.ir_activate_threshold = new_val
        try:
            with open(CONFIG.config_file_path, 'r', encoding='utf-8') as f: data = yaml.safe_load(f) or {}
            if 'mouse' not in data: data['mouse'] = {}
            data['mouse']['ir_activate_threshold'] = new_val
            with open(CONFIG.config_file_path, 'w', encoding='utf-8') as f: yaml.dump(data, f, default_flow_style=False)
        except Exception as e: logger.error(f"Failed to save IR activate threshold: {e}")

    def on_gyro_setting_changed(self, *args):
        if not hasattr(self, 'sens_scale') or not hasattr(self, 'stick_scale'):
            return
        CONFIG.gyro_sensitivity = float(self.sens_scale.get())
        CONFIG.stick_mouse_sensitivity = float(self.stick_scale.get())
        try:
            with open(CONFIG.config_file_path, 'r', encoding='utf-8') as f: data = yaml.safe_load(f) or {}
            data['gyro_mode'] = CONFIG.gyro_mode
            data['gyro_sensitivity'] = CONFIG.gyro_sensitivity
            data['gyro_activation_mode'] = CONFIG.gyro_activation_mode
            data['stick_mouse_sensitivity'] = CONFIG.stick_mouse_sensitivity
            with open(CONFIG.config_file_path, 'w', encoding='utf-8') as f: yaml.dump(data, f, default_flow_style=False)
        except Exception as e: logger.error(f"Save Gyro settings failed: {e}")

    def on_calibrate_clicked(self):
        if not hasattr(self, 'current_controllers') or self.no_controllers: return
        
        self.calibrate_btn.config(state=tk.DISABLED, text="Starting in 3..", fg="#ffffff", disabledforeground="#ffffff")
        self.calib_frame.config(bg=highlight_color)
        self.calibrate_btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))
        
        self.root.after(1000, lambda: self.calibrate_btn.config(text="Starting in 2..", fg="#ffffff", disabledforeground="#ffffff"))
        self.root.after(2000, lambda: self.calibrate_btn.config(text="Starting in 1..", fg="#ffffff", disabledforeground="#ffffff"))
        
        def start_actual_calibration():
            for vc in self.current_controllers:
                if vc is not None: vc.start_calibration()
                
            self.calibrate_btn.config(text="Calibrating 5..", fg="#ffffff", disabledforeground="#ffffff")
            self.calib_frame.config(bg=highlight_color)
            self.calibrate_btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))
            
            self.root.after(1000, lambda: self.calibrate_btn.config(text="Calibrating 4..", fg="#ffffff", disabledforeground="#ffffff"))
            self.root.after(2000, lambda: self.calibrate_btn.config(text="Calibrating 3..", fg="#ffffff", disabledforeground="#ffffff"))
            self.root.after(3000, lambda: self.calibrate_btn.config(text="Calibrating 2..", fg="#ffffff", disabledforeground="#ffffff"))
            self.root.after(4000, lambda: self.calibrate_btn.config(text="Calibrating 1..", fg="#ffffff", disabledforeground="#ffffff"))
            
            self.root.after(5000, lambda: (
                self.calibrate_btn.config(state=tk.NORMAL, text="Calibration Done"), 
                self.calib_frame.config(bg=button_gray), 
                self.calibrate_btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))
            ))
            
        self.root.after(3000, start_actual_calibration)



    def start_custom_recording(self, key, entry, combo, custom_frame, mode_var):
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
            
            if not final_seq:
                custom_frame.pack_forget()
                combo.pack(side=tk.LEFT)
                combo.set("Default")
                setattr(CONFIG, f"{key}_mapping", "Default")
            else:
                mode = mode_var.get()
                val = f"Custom[{mode}]:" + "+".join(final_seq)
                setattr(CONFIG, f"{key}_mapping", val)
                entry.config(state="normal")
                entry.delete(0, tk.END)
                display_val = "+".join(final_seq).replace("VK_", "").replace("MB_", "").replace("BTN_", "")
                entry.insert(0, display_val)
                entry.config(state="readonly")
            self.on_setting_changed()

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

    def create_mapping_widget(self, parent, key, label_text):
        tk.Label(parent, text=label_text, bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(5 * scaling_factor), int(2 * scaling_factor)))
        container = tk.Frame(parent, bg=background_color)
        container.pack(side=tk.LEFT, padx=int(2 * scaling_factor))
        
        combo = ttk.Combobox(container, values=BACK_BUTTON_OPTIONS, font=scale_font(("Arial", 12, "bold")), state="readonly", width=11, justify="center")
        
        custom_frame = tk.Frame(container, bg=background_color)
        
        mode_var = tk.StringVar(value="Hold")
        def toggle_mode():
            new_mode = "Tap" if mode_var.get() == "Hold" else "Hold"
            mode_var.set(new_mode)
            mode_btn.config(text=new_mode)
            current_val = getattr(CONFIG, f"{key}_mapping")
            if current_val.startswith("Custom"):
                if current_val.startswith("Custom[Tap]:") or current_val.startswith("Custom[Hold]:"):
                    new_val = f"Custom[{new_mode}]:{current_val.split(':', 1)[1]}"
                else:
                    new_val = f"Custom[{new_mode}]:{current_val[7:]}"
                setattr(CONFIG, f"{key}_mapping", new_val)
                self.on_setting_changed()

        mode_btn = tk.Button(custom_frame, text="Hold", bg=button_gray, fg="white", font=scale_font(("Arial", 9, "bold")), bd=0, relief=tk.FLAT, command=toggle_mode, width=4)
        mode_btn.pack(side=tk.LEFT, padx=(0, int(2 * scaling_factor)), fill=tk.Y)
        
        entry = tk.Entry(custom_frame, font=scale_font(("Arial", 12, "bold")), width=11, justify="center", bg=button_gray, fg="white", readonlybackground=button_gray, insertbackground="white", bd=0, highlightthickness=0)
        entry.pack(side=tk.LEFT, fill=tk.Y)
        entry.is_custom_recording_entry = True
        entry.restart_custom_recording_fn = lambda: self.start_custom_recording(key, entry, combo, custom_frame, mode_var)
        entry.bind("<Button-1>", lambda e: entry.restart_custom_recording_fn())
        
        def on_close():
            custom_frame.pack_forget()
            combo.pack(side=tk.LEFT)
            combo.set("Default")
            setattr(CONFIG, f"{key}_mapping", "Default")
            self.on_setting_changed()
            if hasattr(self, 'focus_outline') and getattr(self.focus_outline, 'target_widget', None) == close_btn:
                try:
                    self.focus_outline.update(combo)
                except: pass

        close_btn = tk.Button(custom_frame, text="X", bg="#ff4444", fg="white", font=scale_font(("Arial", 10, "bold")), bd=0, relief=tk.FLAT, command=on_close)
        close_btn.pack(side=tk.LEFT, padx=(int(2 * scaling_factor), 0), fill=tk.Y)
        
        current_val = getattr(CONFIG, f"{key}_mapping")
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
                
            display_val = display_val.replace("VK_", "").replace("MB_", "").replace("BTN_", "")
            entry.insert(0, display_val)
            entry.config(state="readonly")
            custom_frame.pack(side=tk.LEFT)
            combo.set("Custom")
        else:
            combo.set(current_val)
            combo.pack(side=tk.LEFT)

        def on_combo_selected(event):
            if combo.get() == "Custom":
                combo.pack_forget()
                custom_frame.pack(side=tk.LEFT)
                self.start_custom_recording(key, entry, combo, custom_frame, mode_var)
            else:
                self.on_setting_changed(event)
                
        combo.bind("<<ComboboxSelected>>", on_combo_selected)
        setattr(self, f"{key}_combo", combo)
        setattr(self, f"{key}_custom_frame", custom_frame)
        setattr(self, f"{key}_entry", entry)
        setattr(self, f"{key}_mode_btn", mode_btn)
        setattr(self, f"{key}_mode_var", mode_var)

    def init_settings_panel(self):
        self.settings_frame = tk.Frame(self.root, bg=background_color)
        self.settings_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=int(5 * scaling_factor))
        row_global = tk.Frame(self.settings_frame, bg=background_color); row_global.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        
        # Driver Switch
        tk.Label(row_global, text="Driver:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(2 * scaling_factor)))
        self.driver_switch = ToggleSwitch(row_global, ["WinUHid", "ViGEmBus", "USBIP"], ["WinUHid", "ViGEmBus", "USBIP"], getattr(CONFIG, "driver_type", "WinUHid"), self.update_driver_type_setting, background_color)
        self.driver_switch.pack(side=tk.LEFT, padx=int(5 * scaling_factor))
        
        # Emu Mode
        tk.Label(row_global, text="Emu Mode:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(20 * scaling_factor), int(2 * scaling_factor)))
        
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
        tk.Label(row_global, text="Layout:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(20 * scaling_factor), int(2 * scaling_factor)))
        self.layout_switch = ToggleSwitch(row_global, ["Xbox", "Switch"], ["Xbox", "Switch"], CONFIG.abxy_mode, self.update_layout_setting, background_color)
        self.layout_switch.pack(side=tk.LEFT, padx=int(5 * scaling_factor))

        row_vibration = tk.Frame(self.settings_frame, bg=background_color); row_vibration.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        tk.Label(row_vibration, text="Rumble Mode:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(2 * scaling_factor)))
        self.rumble_mode_switch = ToggleSwitch(row_vibration, ["Xbox", "Switch"], ["Xbox", "Switch"], getattr(CONFIG, "rumble_mode", "Xbox"), self.update_rumble_mode_setting, background_color)
        self.rumble_mode_switch.pack(side=tk.LEFT, padx=int(5 * scaling_factor))
        self.update_dynamic_rumble_mode_options()

        tk.Label(row_vibration, text="Strength:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(20 * scaling_factor), int(2 * scaling_factor)))
        self.vibration_strength_scale = tk.Scale(row_vibration, from_=0, to=10, resolution=1, orient=tk.HORIZONTAL, length=int(120 * scaling_factor), bg=background_color, fg=text_color, troughcolor=button_gray, activebackground=highlight_color, highlightthickness=0, bd=0, sliderrelief=tk.FLAT, sliderlength=int(15 * scaling_factor), width=int(15 * scaling_factor), font=scale_font(("Arial", 12, "bold")), command=self.update_vibration_strength)
        self.vibration_strength_scale.set(getattr(CONFIG, "vibration_strength", 5))
        self.vibration_strength_scale.pack(side=tk.LEFT)

        self.vibration_frequency_label = tk.Label(row_vibration, text="Frequency:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold")))
        self.vibration_frequency_scale = tk.Scale(row_vibration, from_=1, to=10, resolution=1, orient=tk.HORIZONTAL, length=int(120 * scaling_factor), bg=background_color, fg=text_color, troughcolor=button_gray, activebackground=highlight_color, highlightthickness=0, bd=0, sliderrelief=tk.FLAT, sliderlength=int(15 * scaling_factor), width=int(15 * scaling_factor), font=scale_font(("Arial", 12, "bold")), command=self.update_vibration_frequency)
        self.vibration_frequency_scale.set(getattr(CONFIG, "vibration_frequency", 10))
        self.update_rumble_mode_ui(getattr(CONFIG, "rumble_mode", "Xbox"))

        row_mouse = tk.Frame(self.settings_frame, bg=background_color); row_mouse.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        tk.Label(row_mouse, text="Joy-con Mouse:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(2 * scaling_factor)))
        self.mouse_switch = ToggleSwitch(row_mouse, ["ON", "OFF"], [True, False], CONFIG.mouse_config.enabled, self.update_mouse_setting, background_color)
        self.mouse_switch.pack(side=tk.LEFT, padx=int(5 * scaling_factor))
        tk.Label(row_mouse, text="Sensitivity:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(2 * scaling_factor)))
        self.mouse_sens_scale = tk.Scale(row_mouse, from_=1, to=10, resolution=0.2, orient=tk.HORIZONTAL, length=int(120 * scaling_factor), bg=background_color, fg=text_color, troughcolor=button_gray, activebackground=highlight_color, highlightthickness=0, bd=0, sliderrelief=tk.FLAT, sliderlength=int(15 * scaling_factor), width=int(15 * scaling_factor), font=scale_font(("Arial", 12, "bold")), command=self.update_mouse_sensitivity)
        self.mouse_sens_scale.set(CONFIG.mouse_config.sensitivity); self.mouse_sens_scale.pack(side=tk.LEFT)
        tk.Label(row_mouse, text="Activate Threshold:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(2 * scaling_factor)))
        self.ir_activate_scale = tk.Scale(row_mouse, from_=1, to=3, resolution=1, orient=tk.HORIZONTAL, length=int(80 * scaling_factor), bg=background_color, fg=text_color, troughcolor=button_gray, activebackground=highlight_color, highlightthickness=0, bd=0, sliderrelief=tk.FLAT, sliderlength=int(15 * scaling_factor), width=int(15 * scaling_factor), font=scale_font(("Arial", 12, "bold")), command=self.update_ir_activate_threshold)
        self.ir_activate_scale.set(CONFIG.mouse_config.ir_activate_threshold); self.ir_activate_scale.pack(side=tk.LEFT)

        row_profile = tk.Frame(self.settings_frame, bg=background_color)
        row_profile.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        tk.Label(row_profile, text="Profile:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(5 * scaling_factor)))
        
        self.profile_combo = ttk.Combobox(row_profile, values=self.get_sorted_profiles(), state="readonly", font=scale_font(("Arial", 12, "bold")), width=15)
        self.profile_combo.set(CONFIG.active_profile)
        self.profile_combo.pack(side=tk.LEFT, padx=int(5 * scaling_factor))
        self.profile_combo.bind("<<ComboboxSelected>>", self.on_profile_selected)
        
        self.add_profile_btn = tk.Button(row_profile, text="Add", font=scale_font(("Arial", 12, "bold")), bg=button_gray, fg="white", relief=tk.FLAT, bd=0, command=self.on_add_profile)
        self.add_profile_btn.pack(side=tk.LEFT, padx=int(2 * scaling_factor))

        self.rename_profile_btn = tk.Button(row_profile, text="Rename", font=scale_font(("Arial", 12, "bold")), bg=button_gray, fg="white", relief=tk.FLAT, bd=0, command=self.on_rename_profile)
        self.rename_profile_btn.pack(side=tk.LEFT, padx=int(2 * scaling_factor))
        
        self.reset_profile_btn = tk.Button(row_profile, text="Reset", font=scale_font(("Arial", 12, "bold")), bg=button_gray, fg="white", relief=tk.FLAT, bd=0, command=self.on_reset_profile)
        self.reset_profile_btn.pack(side=tk.LEFT, padx=int(2 * scaling_factor))

        self.del_profile_btn = tk.Button(row_profile, text="Delete", font=scale_font(("Arial", 12, "bold")), bg=button_gray, fg="white", relief=tk.FLAT, bd=0, command=self.on_delete_profile)
        self.del_profile_btn.pack(side=tk.LEFT, padx=int(2 * scaling_factor))

        row_shared = tk.Frame(self.settings_frame, bg=background_color); row_shared.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        tk.Label(row_shared, text="Shared Buttons:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(5 * scaling_factor)))
        for key, label in [("home", "Home:"), ("capt", "Capture:"), ("c", "Chat:")]:
            self.create_mapping_widget(row_shared, key, label)

        row_pro = tk.Frame(self.settings_frame, bg=background_color); row_pro.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        tk.Label(row_pro, text="Pro Controller Buttons:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(5 * scaling_factor)))
        for key, label in [("gl", "GL:"), ("gr", "GR:")]:
            self.create_mapping_widget(row_pro, key, label)

        row_jc = tk.Frame(self.settings_frame, bg=background_color); row_jc.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        tk.Label(row_jc, text="Joy-con Rail Buttons:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(5 * scaling_factor)))
        for key, label in [("sll", "Left SL:"), ("srl", "Left SR:"), ("slr", "Right SL:"), ("srr", "Right SR:")]:
            self.create_mapping_widget(row_jc, key, label)

        row_gc = tk.Frame(self.settings_frame, bg=background_color); row_gc.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        tk.Label(row_gc, text="GameCube Controller:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(5 * scaling_factor)))
        
        self.gc_trigger_calib_btn = tk.Button(row_gc, text="Trigger Calibration", font=scale_font(("Arial", 12, "bold")), bg=button_gray, fg="white", relief=tk.FLAT, bd=0, command=self.on_gc_trigger_calib_clicked)
        self.gc_trigger_calib_btn.pack(side=tk.LEFT, padx=(int(5 * scaling_factor), int(10 * scaling_factor)))

        tk.Label(row_gc, text="Analog Trigger 100%:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(5 * scaling_factor), int(2 * scaling_factor)))
        
        self.gc_trigger_labels = ["Hair Trigger", "Before Click", "Fully Clicked"]
        self.gc_trigger_values = ["Hair Trigger", "100% at Bump", "100% at Max"]
        
        self.gc_trigger_combo = ttk.Combobox(row_gc, values=self.gc_trigger_labels, font=scale_font(("Arial", 12, "bold")), state="readonly", width=12, justify="center")
        
        current_val = getattr(CONFIG, "gc_trigger_mode", "100% at Bump")
        try:
            idx = self.gc_trigger_values.index(current_val)
            self.gc_trigger_combo.set(self.gc_trigger_labels[idx])
        except ValueError:
            self.gc_trigger_combo.set(self.gc_trigger_labels[1])
            
        def on_gc_trigger_combo_selected(event):
            selected_label = self.gc_trigger_combo.get()
            try:
                idx = self.gc_trigger_labels.index(selected_label)
                self.update_gc_trigger_mode_setting(self.gc_trigger_values[idx])
            except ValueError:
                pass
                
        self.gc_trigger_combo.bind("<<ComboboxSelected>>", on_gc_trigger_combo_selected)
        self.gc_trigger_combo.pack(side=tk.LEFT, padx=int(2 * scaling_factor))

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
        # 1. 讀取 (Removed load_config to prevent async save race condition)

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
                from tkinter import messagebox
                answer = messagebox.askyesno(
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
            if not is_driver_installed() and not getattr(CONFIG, 'driver_installed', False):
                from tkinter import messagebox
                answer = messagebox.askyesno(
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
            # 存檔
            CONFIG.save_config()
            
        self._refresh_mapping_comboboxes()
        self.force_refresh_player_slots()
 
    def force_refresh_player_slots(self):
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
        for key in ["home", "capt", "c", "gl", "gr", "sll", "srl", "slr", "srr"]:
            combo = getattr(self, f"{key}_combo", None)
            custom_frame = getattr(self, f"{key}_custom_frame", None)
            entry = getattr(self, f"{key}_entry", None)
            mode_btn = getattr(self, f"{key}_mode_btn", None)
            mode_var = getattr(self, f"{key}_mode_var", None)
            
            if combo:
                current_val = getattr(CONFIG, f"{key}_mapping")
                if current_val.startswith("Custom"):
                    combo.set("Custom")
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
                            
                        display_val = display_val.replace("VK_", "").replace("MB_", "").replace("BTN_", "")
                        entry.insert(0, display_val)
                        entry.config(state="readonly")
                        custom_frame.pack(side=tk.LEFT)
                else:
                    combo.set(current_val)
                    if custom_frame:
                        custom_frame.pack_forget()
                    combo.pack(side=tk.LEFT)
                    
        if hasattr(self, 'gc_trigger_combo'):
            try:
                idx = self.gc_trigger_values.index(CONFIG.gc_trigger_mode)
                self.gc_trigger_combo.set(self.gc_trigger_labels[idx])
            except ValueError:
                pass
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
        # 1. 讀取 (Removed load_config to prevent async save race condition)
        
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
            # 存檔
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
            self.rumble_mode_switch.update_options(["Xbox", "PS5"], ["Xbox", "PS5"], current_rumble)
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
            self.vibration_frequency_label.pack(side=tk.LEFT, padx=(int(20 * scaling_factor), int(2 * scaling_factor)))
            self.vibration_frequency_scale.pack(side=tk.LEFT)

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
        
        tk.Label(dialog, text=prompt, font=scale_font(("Arial", 12, "bold")), bg=background_color, fg=text_color).pack(pady=(int(15*scaling_factor), int(5*scaling_factor)))
        
        entry = tk.Entry(dialog, font=scale_font(("Arial", 12, "bold")), bg=button_gray, fg="white", insertbackground="white", justify="center")
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
        tk.Button(btn_frame, text="OK", font=scale_font(("Arial", 12, "bold")), bg=button_gray, fg="white", width=8, relief=tk.FLAT, bd=0, command=on_ok).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Cancel", font=scale_font(("Arial", 12, "bold")), bg=button_gray, fg="white", width=8, relief=tk.FLAT, bd=0, command=on_cancel).pack(side=tk.LEFT, padx=5)
        
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
        
        tk.Label(dialog, text=message, font=scale_font(("Arial", 12, "bold")), bg=background_color, fg=text_color, wraplength=int(310*scaling_factor), justify="center").pack(pady=(int(20*scaling_factor), int(10*scaling_factor)), expand=True)
        
        result = [None]
        btn_frame = tk.Frame(dialog, bg=background_color)
        btn_frame.pack(pady=(0, int(15*scaling_factor)))
        
        def set_res(res):
            result[0] = res
            dialog.destroy()
            
        if type == "yesno":
            tk.Button(btn_frame, text="Yes", font=scale_font(("Arial", 12, "bold")), bg=button_gray, fg="white", width=8, relief=tk.FLAT, bd=0, command=lambda: set_res(True)).pack(side=tk.LEFT, padx=5)
            tk.Button(btn_frame, text="No", font=scale_font(("Arial", 12, "bold")), bg=button_gray, fg="white", width=8, relief=tk.FLAT, bd=0, command=lambda: set_res(False)).pack(side=tk.LEFT, padx=5)
        else:
            tk.Button(btn_frame, text="OK", font=scale_font(("Arial", 12, "bold")), bg=button_gray, fg="white", width=8, relief=tk.FLAT, bd=0, command=lambda: set_res(True)).pack()
            
        dialog.bind("<Return>", lambda e: set_res(True))
        if type == "yesno":
            dialog.bind("<Escape>", lambda e: set_res(False))
        else:
            dialog.bind("<Escape>", lambda e: set_res(True))
            
        self.root.wait_window(dialog)
        return result[0]

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
        return sorted(list(CONFIG.profiles.keys()), key=sort_key)

    def on_cycle_profile(self):
        if not hasattr(CONFIG, 'active_profile') or not CONFIG.profiles:
            return
            
        # Execute on main thread to avoid Tkinter threading errors
        if threading.current_thread() != threading.main_thread():
            self.root.after(0, self.on_cycle_profile)
            return

        sorted_profiles = self.get_sorted_profiles()
        if not sorted_profiles: return
        
        # Initialize pending profile if not set
        if not hasattr(self, 'pending_profile') or not self.pending_profile:
            self.pending_profile = CONFIG.active_profile
            
        try:
            curr_idx = sorted_profiles.index(self.pending_profile)
            next_idx = (curr_idx + 1) % len(sorted_profiles)
        except ValueError:
            next_idx = 0
            
        self.pending_profile = sorted_profiles[next_idx]
        
        # Show notification immediately
        import utils
        utils.show_notification("Profile Switched", f"Current Profile: {self.pending_profile}")
        
        # Cancel any pending apply timer
        if hasattr(self, 'profile_apply_timer') and self.profile_apply_timer:
            self.root.after_cancel(self.profile_apply_timer)
            
        # Set timer to actually apply the profile after 1 second of inactivity
        self.profile_apply_timer = self.root.after(1000, self.apply_pending_profile)
        
    def apply_pending_profile(self):
        self.profile_apply_timer = None
        if not hasattr(self, 'pending_profile') or not self.pending_profile:
            return
            
        if self.pending_profile == getattr(CONFIG, 'active_profile', ""):
            return # No change
            
        # 1. Save current profile settings
        if hasattr(CONFIG, 'active_profile') and CONFIG.active_profile in CONFIG.profiles:
            CONFIG.profiles[CONFIG.active_profile]["driver_type"] = getattr(CONFIG, "driver_type", "WinUHid")
            CONFIG.profiles[CONFIG.active_profile]["simulation_mode"] = getattr(CONFIG, "simulation_mode", "PS5")

        # 2. Apply switch
        if CONFIG.switch_profile(self.pending_profile):
            self.profile_combo.set(self.pending_profile)
            self.apply_profile_switch()
            
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

        # 2. 切換至新的profile的Driver
        if driver_changed:
            if getattr(self, 'driver_switch', None):
                self.driver_switch.set_value(new_driver)
            self.update_driver_type_setting(new_driver)
            
        # 3. 切換至新的profile的Emu Mode (If driver changed, it was already applied, but we ensure UI is updated)
        if not driver_changed and emu_changed:
            if getattr(self, 'sim_mode_switch', None):
                self.sim_mode_switch.set_value(new_emu)
            self.update_sim_mode_setting(new_emu)
        elif getattr(self, 'sim_mode_switch', None):
            self.sim_mode_switch.set_value(new_emu)

        # 4. 切換至新的profile的custom buttons與其他設定
        self.refresh_ui_for_profile()

    def on_profile_selected(self, event):
        if hasattr(self, 'profile_apply_timer') and self.profile_apply_timer:
            self.root.after_cancel(self.profile_apply_timer)
            self.profile_apply_timer = None
        self.pending_profile = None
        
        selected_profile = self.profile_combo.get()
        if not selected_profile or selected_profile == getattr(CONFIG, "active_profile", ""):
            return
            
        # 1. 紀錄當下設定、Driver與Emu Mode至原本的profile
        if hasattr(CONFIG, 'active_profile') and CONFIG.active_profile in CONFIG.profiles:
            CONFIG.profiles[CONFIG.active_profile]["driver_type"] = getattr(CONFIG, "driver_type", "WinUHid")
            CONFIG.profiles[CONFIG.active_profile]["simulation_mode"] = getattr(CONFIG, "simulation_mode", "PS5")

        if CONFIG.switch_profile(selected_profile):
            self.apply_profile_switch()

    def on_add_profile(self):
        i = 1
        while f"Profile {i}" in CONFIG.profiles:
            i += 1
        new_name = f"Profile {i}"
        
        # 1. 紀錄當下設定、Driver與Emu Mode至原本的profile
        if hasattr(CONFIG, 'active_profile') and CONFIG.active_profile in CONFIG.profiles:
            CONFIG.profiles[CONFIG.active_profile]["driver_type"] = getattr(CONFIG, "driver_type", "WinUHid")
            CONFIG.profiles[CONFIG.active_profile]["simulation_mode"] = getattr(CONFIG, "simulation_mode", "PS5")
            
        if CONFIG.add_profile(new_name):
            self.profile_combo['values'] = self.get_sorted_profiles()
            self.profile_combo.set(CONFIG.active_profile)
            self.apply_profile_switch()

    def on_rename_profile(self):
        current_name = CONFIG.active_profile
        new_name = self.custom_askstring("Rename Profile", f"Rename '{current_name}' to:", initialvalue=current_name)
        if new_name and new_name != current_name:
            if new_name in CONFIG.profiles:
                self.custom_messagebox("Error", f"Profile '{new_name}' already exists.", type="error")
            else:
                if CONFIG.rename_profile(new_name):
                    self.profile_combo['values'] = self.get_sorted_profiles()
                    self.profile_combo.set(CONFIG.active_profile)

    def on_reset_profile(self):
        current_name = CONFIG.active_profile
        if self.custom_messagebox("Reset Profile", f"Are you sure you want to reset profile '{current_name}'?", type="yesno"):
            if CONFIG.reset_profile_to_default(current_name):
                # We can just apply the profile switch to reload everything from CONFIG.profiles
                self.apply_profile_switch()
                
    def on_delete_profile(self):
        if len(CONFIG.profiles) <= 1:
            self.custom_messagebox("Delete Profile", "Cannot delete the last profile.", type="warning")
            return
            
        current_name = CONFIG.active_profile
        if self.custom_messagebox("Delete Profile", f"Are you sure you want to delete profile '{current_name}'?", type="yesno"):
            if CONFIG.delete_profile():
                self.profile_combo['values'] = self.get_sorted_profiles()
                self.profile_combo.set(CONFIG.active_profile)
                # Since the old profile is deleted, we just apply the new profile directly
                self.apply_profile_switch()

    def refresh_ui_for_profile(self):
        self.layout_switch.set_value(CONFIG.abxy_mode)
        self.rumble_mode_switch.set_value(getattr(CONFIG, "rumble_mode", "Xbox"))
        self.update_rumble_mode_ui(getattr(CONFIG, "rumble_mode", "Xbox"))
        self.vibration_strength_scale.set(CONFIG.vibration_strength)
        self.vibration_frequency_scale.set(CONFIG.vibration_frequency)
        self._refresh_mapping_comboboxes()
        if hasattr(self, 'gc_trigger_combo'):
            current_val = getattr(CONFIG, "gc_trigger_mode", "100% at Bump")
            try:
                idx = self.gc_trigger_values.index(current_val)
                self.gc_trigger_combo.set(self.gc_trigger_labels[idx])
            except ValueError:
                self.gc_trigger_combo.set(self.gc_trigger_labels[1])
                
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
        if hasattr(self, 'steam_roll_comp_switch'):
            self.steam_roll_comp_switch.set_value(getattr(CONFIG, "steam_roll_compensation", False))
                
        # Update Cemuhook Sensitivity
        if hasattr(self, 'cemuhook_sens_scale'):
            self.cemuhook_sens_scale.set(getattr(CONFIG, "cemuhook_sensitivity", 1))
                
        # Update DJG Settings as the last step
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

    def on_setting_changed(self, event=None):
        def get_mapping(key):
            combo = getattr(self, f"{key}_combo", None)
            if combo is None: return "Default"
            val = combo.get()
            if val == "Custom":
                curr = getattr(CONFIG, f"{key}_mapping", "Default")
                if curr.startswith("Custom"):
                    return curr
            return val

        CONFIG.home_mapping = get_mapping("home")
        CONFIG.capt_mapping = get_mapping("capt")
        CONFIG.gl_mapping = get_mapping("gl")
        CONFIG.gr_mapping = get_mapping("gr")
        CONFIG.c_mapping = get_mapping("c")
        CONFIG.sll_mapping = get_mapping("sll")
        CONFIG.srl_mapping = get_mapping("srl")
        CONFIG.slr_mapping = get_mapping("slr")
        CONFIG.srr_mapping = get_mapping("srr")
        if hasattr(self, 'gc_trigger_combo'):
            pass # Value is already saved by the Combobox command
        CONFIG.save_config()
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
                if w > 100 and h > 100:
                    self.last_width = w
                    self.last_height = h
        except Exception:
            pass
            
        # Save last window size if we tracked a normal state size
        if getattr(self, 'last_width', None) is not None and getattr(self, 'last_height', None) is not None:
            CONFIG.window_width = self.last_width
            CONFIG.window_height = self.last_height
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
        self.start_discoverer_thread()
        
        self.power_listener.start()
        
        if CONFIG.start_minimized:
            self.hide_to_tray()
        else:
            self.root.deiconify()
            
        # Start battery refresh timer (5 minutes)
        self.root.after(300000, self.start_battery_refresh_timer)
            
        self.root.protocol("WM_DELETE_WINDOW", self.on_quit); self.root.mainloop()

if __name__ == "__main__":
    disable_power_throttling()
    win = ControllerWindow()
    win.init_interface(); win.start()