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
from controller import Controller, INPUT_REPORT_UUID, COMMAND_RESPONSE_UUID
from discoverer import start_discoverer, set_shutting_down, set_suspending, emergency_cleanup
from config import get_resource, CONFIG, BACK_BUTTON_OPTIONS, get_driver_path
from virtual_controller import VirtualController
from discoverer import split_controller, merge_controllers, VIRTUAL_CONTROLLERS
from utils import set_startup
import pystray
from pystray import MenuItem as item
from PIL import Image, ImageTk
import win32gui
import win32con

logger = logging.getLogger(__name__)

scaling_factor = 1.2

def scale_font(font_tuple):
    if not font_tuple:
        return font_tuple
    if isinstance(font_tuple, tuple) and len(font_tuple) >= 2:
        family, size = font_tuple[0], font_tuple[1]
        weight = font_tuple[2] if len(font_tuple) > 2 else ""
        if size != 8:
            size = size + 2
        else:
            size = size + 1
        return (family, int(size), weight)
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

controller_frame_size = 200
battery_height = 40

# Current Color Scheme (Space Gray / Cyan Accent)
background_color = "#2D2D2D"
block_color = "#3C3C3C"
player_number_bg_color = "#2D2D2D"
highlight_color = "#00C3E3"
text_color = "#FFFFFF"
button_gray = "#4B4B4B"

CONTROLLER_UPDATED_EVENT = '<<ControllersUpdated>>'
pending_merge_vc_index = None

class ToggleSwitch(tk.Frame):
    def __init__(self, parent, labels, values, initial_value, command, bg_color):
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
            
            btn = tk.Button(frame, text=label, width=8, font=scale_font(("Arial", 12, "bold")),
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
                asyncio.run_coroutine_threadsafe(controller.set_vibration(vib), self.current_vc.loop)
                self.parent.after(100, lambda c=controller, loop=self.current_vc.loop, o=off: 
                    asyncio.run_coroutine_threadsafe(c.set_vibration(o), loop))
                self.parent.after(200, lambda c=controller, loop=self.current_vc.loop, v=vib: 
                    asyncio.run_coroutine_threadsafe(c.set_vibration(v), loop))
                self.parent.after(300, lambda c=controller, loop=self.current_vc.loop, o=off: 
                    asyncio.run_coroutine_threadsafe(c.set_vibration(o), loop))
            
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
            self.current_vc.active_gyro_side = val
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
            self.window.update(list(VIRTUAL_CONTROLLERS))

    def _update_controller_image(self):
        if self.current_vc is None: return
        if not self.current_vc.is_single():
            image = self.joycon2leftandright
        elif self.current_vc.is_single_joycon_right():
            image = self.joycon2right_sideway if self.current_vc.hold_mode == "Horizontal" else self.joycon2right_vertical
        elif self.current_vc.is_single_joycon_left():
            image = self.joycon2left_sideway if self.current_vc.hold_mode == "Horizontal" else self.joycon2left_vertical
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
            if c is not None:
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

            if not getattr(self, 'gyro_btn_l', None):
                self.gyro_frame_l = tk.Frame(self.battery_frame, bg=block_color)
                self.gyro_frame_r = tk.Frame(self.battery_frame, bg=block_color)
                self.gyro_btn_l = tk.Button(self.gyro_frame_l, text="L Gyro", font=scale_font(("Arial", 8, "bold")), bd=0, relief=tk.FLAT, command=lambda: self._on_gyro_side_toggled("Left"))
                self.gyro_btn_r = tk.Button(self.gyro_frame_r, text="R Gyro", font=scale_font(("Arial", 8, "bold")), bd=0, relief=tk.FLAT, command=lambda: self._on_gyro_side_toggled("Right"))
                self.gyro_btn_l.pack(); self.gyro_btn_r.pack()

            self.gyro_frame_l.place(relx=0.04, rely=0.5, anchor=tk.W)
            self.gyro_frame_r.place(relx=0.96, rely=0.5, anchor=tk.E)
            if virtualController.active_gyro_side == "Left":
                self.gyro_frame_l.config(bg=highlight_color)
                self.gyro_frame_r.config(bg=button_gray)
            else:
                self.gyro_frame_l.config(bg=button_gray)
                self.gyro_frame_r.config(bg=highlight_color)
            self.gyro_btn_l.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))
            self.gyro_btn_r.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))
            for b in [self.gyro_btn_l, self.gyro_btn_r]: b.config(bg=button_gray, fg="#FFFFFF")
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

                if not getattr(self, 'mode_switch', None):
                    self.mode_switch = ToggleSwitch(self.battery_frame, ["V", "H"], ["Vertical", "Horizontal"], virtualController.hold_mode, self._on_hold_mode_toggled, block_color)
                    for btn_data in self.mode_switch.buttons:
                        btn_data[0].config(font=scale_font(("Arial", 9, "bold")), width=2, padx=0, pady=0)
                self.mode_switch.place(relx=0.98, rely=0.5, anchor=tk.E)
                self.mode_switch.set_value(virtualController.hold_mode)
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
        if is_final_complete or is_cancelled:
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

    def check_driver_installation(self):
        # 如果yaml裡有已安裝的紀錄：開啟app時不再檢查是否有安裝，無條件開啟app
        if getattr(CONFIG, 'driver_installed', False):
            return

        import subprocess
        
        driver_exists = False
        try:
            result = subprocess.run(
                ["pnputil", "/enum-devices", "/deviceid", "Root\\WinUHid"],
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
            )
            if result.returncode == 0 and "root\\winuhid" in result.stdout.lower():
                driver_exists = True
        except Exception as e:
            logger.error(f"Error checking driver installation via pnputil: {e}")
            import winreg
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\WUDF\Services\WinUHidDriver")
                winreg.CloseKey(key)
                driver_exists = True
            except FileNotFoundError:
                pass
            
        if driver_exists:
            # 如果檢查結果是已安裝，自動記錄到yaml裡
            CONFIG.driver_installed = True
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
        import subprocess
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

        install_bat = get_driver_path("install.bat")
        if os.path.exists(install_bat):
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
                
                proc = subprocess.Popen(f'"{install_bat}"', shell=True)
                
                def check_process():
                    if proc.poll() is None:
                        progress_win.after(500, check_process)
                    else:
                        progress_win.grab_release()
                        progress_win.destroy()
                            
                progress_win.after(500, check_process)
                self.root.wait_window(progress_win)
                
                # Now that progress_win is destroyed, its event grab is released
                driver_installed_ok = False
                try:
                    res = subprocess.run(
                        ["pnputil", "/enum-devices", "/deviceid", "Root\\WinUHid"],
                        capture_output=True,
                        text=True,
                        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
                    )
                    if res.returncode == 0 and "root\\winuhid" in res.stdout.lower():
                        driver_installed_ok = True
                except Exception:
                    # Fallback to registry check
                    import winreg
                    try:
                        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\WUDF\Services\WinUHidDriver")
                        winreg.CloseKey(key)
                        driver_installed_ok = True
                    except FileNotFoundError:
                        pass
                    
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
            messagebox.showerror("Error", "Could not find install.bat. Please verify the integrity of the application files.")

        if discoverer_was_running:
            self.start_discoverer_thread()

    def run_driver_uninstall(self):
        import subprocess
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

        uninstall_bat = get_driver_path("uninstall.bat")
        if os.path.exists(uninstall_bat):
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
                
                proc = subprocess.Popen(f'"{uninstall_bat}"', shell=True)
                
                def check_process():
                    if proc.poll() is None:
                        progress_win.after(500, check_process)
                    else:
                        progress_win.grab_release()
                        progress_win.destroy()
                            
                progress_win.after(500, check_process)
                self.root.wait_window(progress_win)
                
                # Now that progress_win is destroyed, its event grab is released
                driver_removed_ok = True
                try:
                    res = subprocess.run(
                        ["pnputil", "/enum-devices", "/deviceid", "Root\\WinUHid"],
                        capture_output=True,
                        text=True,
                        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
                    )
                    if res.returncode == 0 and "root\\winuhid" in res.stdout.lower():
                        driver_removed_ok = False
                except Exception:
                    # Fallback to registry check
                    import winreg
                    try:
                        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\WUDF\Services\WinUHidDriver")
                        winreg.CloseKey(key)
                        driver_removed_ok = False
                    except FileNotFoundError:
                        pass
                    
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
            messagebox.showerror("Error", "Could not find uninstall.bat. Please verify the integrity of the application files.")

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

    def on_driver_btn_clicked(self):
        if getattr(CONFIG, 'driver_installed', False):
            from tkinter import messagebox
            if messagebox.askyesno("Uninstall Driver", "Are you sure you want to uninstall the WinUHid driver?\n(Requires administrator privileges.)"):
                self.run_driver_uninstall()
        else:
            self.run_driver_install()

    def update_driver_button(self):
        if not hasattr(self, 'driver_btn') or not self.driver_btn:
            return
        installed = getattr(CONFIG, 'driver_installed', False)
        text = "Uninstall WinUHid Driver" if installed else "Install WinUHid Driver"
        self.driver_btn.config(text=text)


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
        
        # 2. Get and set global scaling factor
        global scaling_factor, controller_frame_size, battery_height
        scaling_factor = 1.2
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
        default_w = 1240
        default_h = 1020
        w = default_w
        h = default_h
        self.root.geometry(f"{w}x{h}+50+50")
        self.root.minsize(1240, 920)
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
        self.init_gyro_settings_panel()
        self.init_auto_disconnect_panel()

        # New centralized button row above Gyro Settings
        self.top_btn_frame = tk.Frame(self.root, bg=background_color)
        self.top_btn_frame.pack(side=tk.BOTTOM, pady=(0, int(5 * scaling_factor)))

        # Driver Install/Uninstall Button
        self.driver_frame = tk.Frame(self.top_btn_frame, bg=button_gray)
        self.driver_frame.pack(side=tk.LEFT, padx=int(5 * scaling_factor))
        self.driver_btn = tk.Button(self.driver_frame, text="", bg=button_gray, fg=text_color, bd=0, relief=tk.FLAT, font=scale_font(("Arial", 10, "bold")), command=self.on_driver_btn_clicked)
        self.driver_btn.pack(padx=int(2 * scaling_factor), pady=int(2 * scaling_factor))
        self.update_driver_button()

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

        self.update([None])

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
        self.comp_frame = tk.LabelFrame(self.root, text=" Gyro Passthrough ", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold")), padx=int(10 * scaling_factor), pady=int(10 * scaling_factor))
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

    def init_gyro_settings_panel(self):
        self.gyro_frame = tk.LabelFrame(self.root, text=" Built-in Gyro ", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold")), padx=int(10 * scaling_factor), pady=int(10 * scaling_factor))
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
        self.auto_disconnect_switch = ToggleSwitch(self.auto_disconnect_frame, labels=["ON", "OFF"], values=[True, False], initial_value=getattr(CONFIG, "auto_disconnect_enabled", False), command=self.update_auto_disconnect_enabled, bg_color=background_color)
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
        tk.Label(self.auto_disconnect_frame, text="Day", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).grid(row=0, column=4, padx=(0, int(10 * scaling_factor)), sticky="w")
        
        # Hour Entry
        self.hour_entry = tk.Entry(self.auto_disconnect_frame, width=4, bg=button_gray, fg=text_color, insertbackground=text_color, bd=0, relief=tk.FLAT, font=scale_font(("Arial", 12, "bold")), justify=tk.CENTER, validate="key", validatecommand=vcmd)
        self.hour_entry.insert(0, str(getattr(CONFIG, "auto_disconnect_hours", 0)))
        self.hour_entry.grid(row=0, column=5, padx=int(2 * scaling_factor))
        tk.Label(self.auto_disconnect_frame, text="Hour", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).grid(row=0, column=6, padx=(0, int(10 * scaling_factor)), sticky="w")
        
        # Minute Entry
        self.minute_entry = tk.Entry(self.auto_disconnect_frame, width=4, bg=button_gray, fg=text_color, insertbackground=text_color, bd=0, relief=tk.FLAT, font=scale_font(("Arial", 12, "bold")), justify=tk.CENTER, validate="key", validatecommand=vcmd)
        self.minute_entry.insert(0, str(getattr(CONFIG, "auto_disconnect_minutes", 0)))
        self.minute_entry.grid(row=0, column=7, padx=int(2 * scaling_factor))
        tk.Label(self.auto_disconnect_frame, text="Minute", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).grid(row=0, column=8, padx=(0, int(10 * scaling_factor)), sticky="w")
        
        # Bind events
        self.day_entry.bind("<KeyRelease>", self.on_auto_disconnect_time_changed)
        self.hour_entry.bind("<KeyRelease>", self.on_auto_disconnect_time_changed)
        self.minute_entry.bind("<KeyRelease>", self.on_auto_disconnect_time_changed)

    def update_auto_disconnect_enabled(self, val):
        CONFIG.auto_disconnect_enabled = val
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
        try:
            with open(CONFIG.config_file_path, 'r', encoding='utf-8') as f: data = yaml.safe_load(f) or {}
            data['steam_roll_compensation'] = val
            with open(CONFIG.config_file_path, 'w', encoding='utf-8') as f: yaml.dump(data, f, default_flow_style=False)
            logger.info(f"Roll Compensation: {val}")
        except Exception as e:
            logger.error(f"Failed to save steam roll compensation setting: {e}")

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



    def init_settings_panel(self):
        self.settings_frame = tk.Frame(self.root, bg=background_color)
        self.settings_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=int(5 * scaling_factor))
        row_global = tk.Frame(self.settings_frame, bg=background_color); row_global.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        tk.Label(row_global, text="Emu Mode:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(2 * scaling_factor)))
        self.sim_mode_switch = ToggleSwitch(row_global, ["Xbox", "PS4", "PS5"], ["Xbox", "PS4", "PS5"], getattr(CONFIG, "simulation_mode", "Xbox"), self.update_sim_mode_setting, background_color)
        self.sim_mode_switch.pack(side=tk.LEFT, padx=int(5 * scaling_factor))
        tk.Label(row_global, text="Layout:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(20 * scaling_factor), int(2 * scaling_factor)))
        self.layout_switch = ToggleSwitch(row_global, ["Xbox", "Switch"], ["Xbox", "Switch"], CONFIG.abxy_mode, self.update_layout_setting, background_color)
        self.layout_switch.pack(side=tk.LEFT, padx=int(5 * scaling_factor))
        tk.Label(row_global, text="Vibration:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(20 * scaling_factor), int(2 * scaling_factor)))
        self.vibration_strength_scale = tk.Scale(row_global, from_=0, to=10, resolution=1, orient=tk.HORIZONTAL, length=int(120 * scaling_factor), bg=background_color, fg=text_color, troughcolor=button_gray, activebackground=highlight_color, highlightthickness=0, bd=0, sliderrelief=tk.FLAT, sliderlength=int(15 * scaling_factor), width=int(15 * scaling_factor), font=scale_font(("Arial", 12, "bold")), command=self.update_vibration_strength)
        self.vibration_strength_scale.set(getattr(CONFIG, "vibration_strength", 5))
        self.vibration_strength_scale.pack(side=tk.LEFT)

        row_mouse = tk.Frame(self.settings_frame, bg=background_color); row_mouse.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        tk.Label(row_mouse, text="Joy-con Mouse:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(2 * scaling_factor)))
        self.mouse_switch = ToggleSwitch(row_mouse, ["ON", "OFF"], [True, False], CONFIG.mouse_config.enabled, self.update_mouse_setting, background_color)
        self.mouse_switch.pack(side=tk.LEFT, padx=int(5 * scaling_factor))
        tk.Label(row_mouse, text="Sensitivity:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(2 * scaling_factor)))
        self.mouse_sens_scale = tk.Scale(row_mouse, from_=1, to=10, resolution=0.2, orient=tk.HORIZONTAL, length=int(120 * scaling_factor), bg=background_color, fg=text_color, troughcolor=button_gray, activebackground=highlight_color, highlightthickness=0, bd=0, sliderrelief=tk.FLAT, sliderlength=int(15 * scaling_factor), width=int(15 * scaling_factor), font=scale_font(("Arial", 12, "bold")), command=self.update_mouse_sensitivity)
        self.mouse_sens_scale.set(CONFIG.mouse_config.sensitivity); self.mouse_sens_scale.pack(side=tk.LEFT)

        row_shared = tk.Frame(self.settings_frame, bg=background_color); row_shared.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        tk.Label(row_shared, text="Shared Buttons:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(5 * scaling_factor)))
        for key, label in [("home", "Home:"), ("capt", "Capture:"), ("c", "Chat:")]:
            tk.Label(row_shared, text=label, bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(5 * scaling_factor), int(2 * scaling_factor)))
            combo = ttk.Combobox(row_shared, values=BACK_BUTTON_OPTIONS, font=scale_font(("Arial", 12, "bold")), state="readonly", width=11, justify="center")
            combo.set(getattr(CONFIG, f"{key}_mapping")); combo.pack(side=tk.LEFT, padx=int(2 * scaling_factor))
            combo.bind("<<ComboboxSelected>>", self.on_setting_changed)
            setattr(self, f"{key}_combo", combo)

        row_pro = tk.Frame(self.settings_frame, bg=background_color); row_pro.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        tk.Label(row_pro, text="Pro Controller Buttons:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(5 * scaling_factor)))
        for key, label in [("gl", "GL:"), ("gr", "GR:")]:
            tk.Label(row_pro, text=label, bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(5 * scaling_factor), int(2 * scaling_factor)))
            combo = ttk.Combobox(row_pro, values=BACK_BUTTON_OPTIONS, font=scale_font(("Arial", 12, "bold")), state="readonly", width=11, justify="center")
            combo.set(getattr(CONFIG, f"{key}_mapping")); combo.pack(side=tk.LEFT, padx=int(2 * scaling_factor))
            combo.bind("<<ComboboxSelected>>", self.on_setting_changed)
            setattr(self, f"{key}_combo", combo)

        row_jc = tk.Frame(self.settings_frame, bg=background_color); row_jc.pack(side=tk.TOP, fill=tk.X, pady=int(5 * scaling_factor))
        tk.Label(row_jc, text="Joy-con Rail Buttons:", bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(10 * scaling_factor), int(5 * scaling_factor)))
        for key, label in [("sll", "Left SL:"), ("srl", "Left SR:"), ("slr", "Right SL:"), ("srr", "Right SR:")]:
            tk.Label(row_jc, text=label, bg=background_color, fg=text_color, font=scale_font(("Arial", 12, "bold"))).pack(side=tk.LEFT, padx=(int(5 * scaling_factor), int(2 * scaling_factor)))
            combo = ttk.Combobox(row_jc, values=BACK_BUTTON_OPTIONS, font=scale_font(("Arial", 12, "bold")), state="readonly", width=11, justify="center")
            combo.set(getattr(CONFIG, f"{key}_mapping")); combo.pack(side=tk.LEFT, padx=int(2 * scaling_factor))
            combo.bind("<<ComboboxSelected>>", self.on_setting_changed)
            setattr(self, f"{key}_combo", combo)

    def update_sim_mode_setting(self, val):
        CONFIG.simulation_mode = val
        if hasattr(self, 'current_controllers'):
            for vc in self.current_controllers:
                if vc is not None: vc.set_mode(val)
        CONFIG.save_config()

    def update_layout_setting(self, val):
        CONFIG.abxy_mode = val
        self.on_setting_changed()

    def update_vibration_strength(self, val):
        try:
            CONFIG.vibration_strength = int(float(val))
            CONFIG.save_config()
        except Exception as e:
            logger.error(f"Failed to save vibration strength setting: {e}")

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

    def on_setting_changed(self, event=None):
        CONFIG.home_mapping = self.home_combo.get()
        CONFIG.capt_mapping = self.capt_combo.get()
        CONFIG.gl_mapping = self.gl_combo.get()
        CONFIG.gr_mapping = self.gr_combo.get()
        CONFIG.c_mapping = self.c_combo.get()
        CONFIG.sll_mapping = self.sll_combo.get()
        CONFIG.srl_mapping = self.srl_combo.get()
        CONFIG.slr_mapping = self.slr_combo.get()
        CONFIG.srr_mapping = self.srr_combo.get()
        try:
            with open(CONFIG.config_file_path, 'r', encoding='utf-8') as f: data = yaml.safe_load(f) or {}
            data['abxy_mode'] = CONFIG.abxy_mode  
            for k in ['home_mapping','capt_mapping','gl_mapping','gr_mapping','c_mapping','sll_mapping','srl_mapping','slr_mapping','srr_mapping']: data[k] = getattr(CONFIG, k)
            with open(CONFIG.config_file_path, 'w', encoding='utf-8') as f: yaml.dump(data, f, default_flow_style=False)
        except Exception as e: logger.error(f"Failed to save settings: {e}")
        self.root.focus_set()

    def update(self, controllers_info):
        if self.main_frame is None:
            self.main_frame = tk.Frame(self.root, bg=background_color); self.main_frame.pack(pady=(10, 5), fill=tk.Y)
            self.players_info = None
        self.current_controllers = controllers_info
        # A slot is only "connected" if the VirtualController exists AND has physical controllers
        any_connected = any(c is not None and len(getattr(c, 'controllers', [])) > 0 for c in controllers_info)
        self.no_controllers = not any_connected
        if any_connected:
            if self.players_info is None:
                for w in self.main_frame.winfo_children(): w.destroy()
                self.players_info = [PlayerInfoBlock(self.main_frame, self) for i in range(4)]
                for p in self.players_info: p.main_frame.pack(padx=10, pady=10, side=tk.LEFT)
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
            
        self.root.protocol("WM_DELETE_WINDOW", self.on_quit); self.root.mainloop()

if __name__ == "__main__":
    win = ControllerWindow()
    win.init_interface(); win.start()