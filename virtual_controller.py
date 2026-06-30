import struct
import winuhid_client as winuhid
from vigem_commons import DS4_REPORT_EX, DS4_BUTTONS, DS4_DPAD_DIRECTIONS, DS4_SPECIAL_BUTTONS
import threading

VIRTUAL_DEVICE_CREATION_LOCK = threading.Lock()
import time

_vigem_import_lock = threading.Lock()
_vigem_module = None

def get_vigem():
    global _vigem_module
    with _vigem_import_lock:
        if _vigem_module is not None:
            return _vigem_module
        import sys
        for mod in list(sys.modules.keys()):
            if mod == 'vgamepad' or mod.startswith('vgamepad.'):
                sys.modules.pop(mod, None)
        import vgamepad as vigem
        _vigem_module = vigem
        return _vigem_module
import asyncio
import threading
import ctypes
import logging
import gc
from controller import (Controller, ControllerInputData, VibrationData,
                        NSO_GAMECUBE_CONTROLLER_PID)
from config import CONFIG, ButtonConfig, SWITCH_BUTTONS, XB_BUTTONS
from usbip_server import USBIPServer
from utils import USBIPAllocator
from dualsense_structs import DualSenseInputReport01
from dualsense_haptic import DualSenseHapticProcessor

logger = logging.getLogger(__name__)

MAC_TO_USBIP = {}


def get_ds4_dpad(up, down, left, right):
    if up and right: return DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_NORTHEAST
    if down and right: return DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_SOUTHEAST
    if down and left: return DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_SOUTHWEST
    if up and left: return DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_NORTHWEST
    if up: return DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_NORTH
    if down: return DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_SOUTH
    if left: return DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_WEST
    if right: return DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_EAST
    return DS4_DPAD_DIRECTIONS.DS4_BUTTON_DPAD_NONE

def float_to_byte(val):
    return int(max(0, min(255, round(val * 127.5 + 128))))

def detach_usbip_device(server_port: int):
    import subprocess
    import os
    import re
    
    usbip_exe = "C:\\Program Files\\USBip\\usbip.exe"
    if not os.path.exists(usbip_exe):
        return
        
    bus_id = f"1-{server_port - 3240 + 1}"
    try:
        res = subprocess.run([usbip_exe, "port"], capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
        if res.returncode != 0:
            return
            
        ports = res.stdout.split("Port ")
        for port_block in ports[1:]:
            lines = port_block.splitlines()
            if not lines:
                continue
            first_line = lines[0]
            port_match = re.match(r"^(\d+):", first_line)
            if port_match:
                port_num_str = port_match.group(1)
                if f":{server_port}" in port_block or f"/{bus_id}" in port_block:
                    logger.info(f"Detaching USBIP device on port {port_num_str} associated with server port {server_port} (bus_id: {bus_id})")
                    try:
                        subprocess.run([usbip_exe, "detach", "-p", port_num_str], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0, timeout=2.0)
                    except subprocess.TimeoutExpired:
                        logger.warning(f"Timeout while detaching USBIP port {port_num_str}. Process may be hung.")
    except Exception as e:
        logger.error(f"Error detaching USBIP device for port {server_port}: {e}")

def detach_all_usbip_devices():
    import subprocess
    import os
    import re
    
    usbip_exe = "C:\\Program Files\\USBip\\usbip.exe"
    if not os.path.exists(usbip_exe):
        return
        
    try:
        res = subprocess.run([usbip_exe, "port"], capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
        if res.returncode != 0:
            return
            
        ports = res.stdout.split("Port ")
        for port_block in ports[1:]:
            lines = port_block.splitlines()
            if not lines:
                continue
            first_line = lines[0]
            port_match = re.match(r"^(\d+):", first_line)
            if port_match:
                port_num_str = port_match.group(1)
                should_detach = False
                for p in range(3240, 3248):
                    bus_id = f"1-{p - 3240 + 1}"
                    if f":{p}" in port_block or f"/{bus_id}" in port_block:
                        should_detach = True
                        break
                if should_detach:
                    logger.info(f"Detaching USBIP device on port {port_num_str} associated with a virtual controller slot")
                    try:
                        subprocess.run([usbip_exe, "detach", "-p", port_num_str], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0, timeout=2.0)
                    except subprocess.TimeoutExpired:
                        logger.warning(f"Timeout while detaching USBIP port {port_num_str}. Process may be hung.")
    except Exception as e:
        logger.error(f"Error detaching all USBIP devices: {e}")

RUMBLE_WRITE_INTERVAL = 0.015
SWITCH_RUMBLE_TIMEOUT = 0.150
MAC_TO_PORT = {}

def get_or_assign_port_for_mac(mac):
    global MAC_TO_PORT
    if mac in MAC_TO_PORT:
        return MAC_TO_PORT[mac]
    
    import discoverer
    used_ports = set()
    for vc in discoverer.VIRTUAL_CONTROLLERS:
        if vc is not None:
            if hasattr(vc, 'server_port'):
                used_ports.add(vc.server_port)
            if hasattr(vc, 'server_port_l') and vc.server_port_l:
                used_ports.add(vc.server_port_l)
            if hasattr(vc, 'server_port_r') and vc.server_port_r:
                used_ports.add(vc.server_port_r)
    
    used_ports.update(MAC_TO_PORT.values())
    
    for p in range(3240, 3256):
        if p not in used_ports:
            MAC_TO_PORT[mac] = p
            return p
    return 3240




class VirtualController:
    @property
    def player_number(self):
        return self._player_number

    @player_number.setter
    def player_number(self, val):
        self._player_number = val

    def __init__(self, player_number: int, controllers=None, on_disconnected_callback=None, setup_usb=True):
        self._player_number = player_number
        self.controllers = controllers or []
        
        self.on_disconnected_callback = on_disconnected_callback
        
        if self.controllers:
            mac = self.controllers[0].device.address
            
            global MAC_TO_USBIP
            if mac in MAC_TO_USBIP:
                self.host_ip, self.bus_id, self.server_port = MAC_TO_USBIP[mac]
            else:
                self.host_ip, self.bus_id, self.server_port = USBIPAllocator.allocate()
                MAC_TO_USBIP[mac] = (self.host_ip, self.bus_id, self.server_port)
        else:
            self.host_ip, self.bus_id, self.server_port = USBIPAllocator.allocate()

        self.haptic_processor = DualSenseHapticProcessor(self._haptic_callback)
                    
        self.previous_buttons_left = 0x00000000
        self.previous_buttons_right = 0x00000000
        self.last_s2_lx = 0.0
        self.last_s2_ly = 0.0
        self.last_s2_rx = 0.0
        self.last_s2_ry = 0.0
        self.last_s2_gx = 0
        self.last_s2_gy = 0
        self.last_s2_gz = 0
        self.last_s2_ax = 0
        self.last_s2_ay = 0
        self.last_s2_az = 0
        self.next_vibration_event = None
        self.vg_controller = None
        self.switch_vibrations_left = [VibrationData() for _ in range(3)]
        self.switch_vibrations_right = [VibrationData() for _ in range(3)]
        self.vibration_dirty_l = False
        self.vibration_dirty_r = False
        self.slot_inputs = [[(0x0e1, 0, 0x1e1, 0)] for _ in range(3)]
        self.slot_inputs_right = [[(0x0e1, 0, 0x1e1, 0)] for _ in range(3)]
        self.rumble_force_clear = False
        
        # Thread-safe target vibration state, change event, and task reference
        self.vibration_lock = threading.Lock()
        self.target_vibration_l = VibrationData(lf_amp=0, hf_amp=0)
        self.target_vibration_r = VibrationData(lf_amp=0, hf_amp=0)
        self.latest_vibration_l = VibrationData(lf_amp=0, hf_amp=0)
        self.latest_vibration_r = VibrationData(lf_amp=0, hf_amp=0)
        self.frame_vibrations_l = [VibrationData(lf_amp=0, hf_amp=0) for _ in range(3)]
        self.frame_vibrations_r = [VibrationData(lf_amp=0, hf_amp=0) for _ in range(3)]
        self.latest_vibration = VibrationData(lf_amp=0, hf_amp=0)
        self.frame_vibrations = [VibrationData(lf_amp=0, hf_amp=0) for _ in range(3)]
        self.vibration_dirty = False
        self.last_rumble_active_time = 0.0
        self.vibration_changed_event = None
        self.active_vibration_task = None
        self.cycle_start_time = 0.0
        self.loop = None
        self.touch_tracking_id = 0
        self.was_touching = False
        self.was_touching_0 = False
        self.was_touching_1 = False
        self.touch_start_time = 0.0
        
        self.hold_mode = "Vertical"
        self.active_gyro_side = "Right"
        
        self.djg_last_dom_gyro_on = False
        self.djg_last_sub_gyro_on = False
        self.djg_accel_offset = [0.0, 0.0, 0.0]
        self.djg_cached_gyro = {'Left': [0.0, 0.0, 0.0], 'Right': [0.0, 0.0, 0.0]}
        self.djg_cached_accel = {'Left': [0.0, 0.0, 0.0], 'Right': [0.0, 0.0, 0.0]}
        self.djg_left_active = True
        self.djg_right_active = True
        
        self.mode = getattr(CONFIG, "simulation_mode", "PS5")
        self.driver_type = getattr(CONFIG, "driver_type", "WinUHid")
        if self.mode == "Switch1":
            self.hold_mode = "Vertical"
        if setup_usb:
            self._setup_vg_controller()
        else:
            self.vg_controller = None
            self.usbip_server = None
        
        self.state_lock = threading.RLock()
        self._disconnect_lock = asyncio.Lock()
        
        # Adaptive Trigger State Tracking (for Weapon mode recoil kicks)
        self.trigger_r_prev_force = 0
        self.trigger_l_prev_force = 0
        
        self.running = True
        self.update_thread = threading.Thread(target=self._1000hz_loop, daemon=True)
        self.update_thread.start()

    def _controller_mix_key(self, controller):
        try:
            return controller.device.address
        except Exception:
            return str(id(controller))

    def _clamp_stick_pair(self, stick):
        return (
            max(-1.0, min(1.0, stick[0])),
            max(-1.0, min(1.0, stick[1])),
        )

    def _controller_mapping_scope(self, controller):
        active = (
            getattr(controller, "gyro_mouse_enabled", False) or
            getattr(controller, "_in_app_gyro_mapping_active_this_frame", False)
        )
        return "in_app_gyro_mode_mappings" if active else None

    def _joystick_mapping_mode(self, key, controller):
        return CONFIG.get_mapping_setting_scoped(key, "Default", self._controller_mapping_scope(controller))

    def _update_merged_stick_mix(self, inputData, controller):
        if not hasattr(self, "_merged_stick_contribs"):
            self._merged_stick_contribs = {}
        route = getattr(inputData, "custom_joystick_mapping", None)
        left = (0.0, 0.0)
        right = (0.0, 0.0)
        if route:
            if route.get("target") == "left":
                left = inputData.left_stick
            elif route.get("target") == "right":
                right = inputData.right_stick
        elif controller.is_joycon_left():
            left = inputData.left_stick
        elif controller.is_joycon_right():
            right = inputData.right_stick
        else:
            left_mode = self._joystick_mapping_mode("l_joystick", controller)
            right_mode = self._joystick_mapping_mode("r_joystick", controller)
            if left_mode == "R Joystick":
                right = (right[0] + inputData.left_stick[0], right[1] + inputData.left_stick[1])
            elif left_mode == "L Joystick" or left_mode == "Default":
                left = (left[0] + inputData.left_stick[0], left[1] + inputData.left_stick[1])

            if right_mode == "L Joystick":
                left = (left[0] + inputData.right_stick[0], left[1] + inputData.right_stick[1])
            elif right_mode == "R Joystick" or right_mode == "Default":
                right = (right[0] + inputData.right_stick[0], right[1] + inputData.right_stick[1])

        self._merged_stick_contribs[self._controller_mix_key(controller)] = {"left": left, "right": right}
        active_keys = {self._controller_mix_key(c) for c in self.controllers}
        for key in list(self._merged_stick_contribs.keys()):
            if key not in active_keys:
                self._merged_stick_contribs.pop(key, None)

        sum_left = (0.0, 0.0)
        sum_right = (0.0, 0.0)
        for contrib in self._merged_stick_contribs.values():
            l = contrib.get("left", (0.0, 0.0))
            r = contrib.get("right", (0.0, 0.0))
            sum_left = (sum_left[0] + l[0], sum_left[1] + l[1])
            sum_right = (sum_right[0] + r[0], sum_right[1] + r[1])
        return self._clamp_stick_pair(sum_left), self._clamp_stick_pair(sum_right)

    def handle_djg_trigger(self, controller, pressed=True):
        activation = getattr(CONFIG, "djg_activation", "Toggle")
        if activation == "Toggle" and not pressed:
            return
            
        mode = getattr(CONFIG, "djg_mode", "Single Side Toggle")
        if mode == "Single Side Toggle":
            if controller.is_joycon_left():
                self.djg_left_active = not self.djg_left_active
            else:
                self.djg_right_active = not self.djg_right_active
        elif mode == "Switch Dominant Side":
            current = getattr(CONFIG, "djg_dominant_side", "Left")
            CONFIG.djg_dominant_side = "Right" if current == "Left" else "Left"
            CONFIG.save_config()
            self.djg_left_active = True
            self.djg_right_active = True
        elif mode == "Switch Gyro Side":
            current = getattr(CONFIG, "djg_dominant_side", "Left")
            new_side = "Right" if current == "Left" else "Left"
            self.active_gyro_side = new_side
            CONFIG.djg_dominant_side = new_side
            CONFIG.save_config()
            
        import utils
        if hasattr(utils, 'force_ui_update_callback') and utils.force_ui_update_callback:
            utils.force_ui_update_callback()

    def gyro_fusion_callback(self, inputData: ControllerInputData, controller):
        with self.state_lock:
            if getattr(controller, 'is_calibrating', False) or getattr(controller, 'is_mag_calibrating', False):
                controller._skip_gyro_mouse = False
                return

            if len(self.controllers) == 2 and getattr(CONFIG, "djg_enabled", False):
                djg_dom_side = getattr(CONFIG, "djg_dominant_side", "Left")
                djg_sub_side = "Right" if djg_dom_side == "Left" else "Left"
                
                side = "Left" if controller.is_joycon_left() else "Right"
                self.djg_cached_gyro[side] = inputData.gyroscope
                self.djg_cached_accel[side] = inputData.accelerometer
                
                dom_c = next((c for c in self.controllers if (c.is_joycon_left() and djg_dom_side == "Left") or (c.is_joycon_right() and djg_dom_side == "Right")), None)
                sub_c = next((c for c in self.controllers if (c.is_joycon_left() and djg_sub_side == "Left") or (c.is_joycon_right() and djg_sub_side == "Right")), None)
                
                dom_on = getattr(dom_c, 'gyro_active', True) if dom_c else False
                sub_on = getattr(sub_c, 'gyro_active', True) if sub_c else False
                
                # Check skip gyro mouse flag
                if dom_on and sub_on and getattr(CONFIG, "gyro_activation_mode", "Toggle") == "Always On":
                    if (controller.is_joycon_left() and djg_sub_side == "Left") or (controller.is_joycon_right() and djg_sub_side == "Right"):
                        controller._skip_gyro_mouse = True
                    else:
                        controller._skip_gyro_mouse = False
                else:
                    controller._skip_gyro_mouse = False
                
                if self.djg_last_dom_gyro_on and not dom_on and sub_on:
                    dom_accel = self.djg_cached_accel[djg_dom_side]
                    sub_accel = self.djg_cached_accel[djg_sub_side]
                    self.djg_accel_offset = [
                        dom_accel[0] - sub_accel[0],
                        dom_accel[1] - sub_accel[1],
                        dom_accel[2] - sub_accel[2]
                    ]
                elif not self.djg_last_dom_gyro_on and dom_on and sub_on:
                    dom_accel = self.djg_cached_accel[djg_dom_side]
                    sub_accel = self.djg_cached_accel[djg_sub_side]
                    self.djg_accel_offset = [
                        (sub_accel[0] + self.djg_accel_offset[0]) - dom_accel[0],
                        (sub_accel[1] + self.djg_accel_offset[1]) - dom_accel[1],
                        (sub_accel[2] + self.djg_accel_offset[2]) - dom_accel[2]
                    ]
                
                self.djg_last_dom_gyro_on = dom_on
                self.djg_last_sub_gyro_on = sub_on
                
                fused_gyro = [0.0, 0.0, 0.0]
                fused_accel = [0.0, 0.0, 0.0]
                
                dom_bias = dom_c.gyro_bias if dom_c else (0.0, 0.0, 0.0)
                sub_bias = sub_c.gyro_bias if sub_c else (0.0, 0.0, 0.0)
                my_bias = controller.gyro_bias
                
                if dom_on and sub_on:
                    dom_g = self.djg_cached_gyro[djg_dom_side]
                    sub_g = self.djg_cached_gyro[djg_sub_side]
                    dom_a = self.djg_cached_accel[djg_dom_side]
                    
                    import math
                    dom_adj = [dom_g[i] - dom_bias[i] for i in range(3)]
                    sub_adj = [sub_g[i] - sub_bias[i] for i in range(3)]
                    dom_mag = math.sqrt(dom_adj[0]**2 + dom_adj[1]**2 + dom_adj[2]**2)
                    
                    scale = 0.0
                    if dom_mag > 30.0:
                        scale = min(1.0, (dom_mag - 30.0) / 30.0)
                    
                    for i in range(3):
                        if (dom_adj[i] > 0 and sub_adj[i] > 0) or (dom_adj[i] < 0 and sub_adj[i] < 0):
                            sub_scaled = sub_adj[i] * scale
                            if abs(sub_scaled) > abs(dom_adj[i]):
                                sub_scaled = dom_adj[i]
                            fused_adj = dom_adj[i] + sub_scaled
                        else:
                            fused_adj = dom_adj[i]
                        
                        fused_gyro[i] = fused_adj + my_bias[i]
                        fused_accel[i] = dom_a[i] + self.djg_accel_offset[i]
                elif dom_on:
                    dom_g = self.djg_cached_gyro[djg_dom_side]
                    dom_a = self.djg_cached_accel[djg_dom_side]
                    for i in range(3):
                        dom_adj = dom_g[i] - dom_bias[i]
                        fused_gyro[i] = dom_adj + my_bias[i]
                        fused_accel[i] = dom_a[i] + self.djg_accel_offset[i]
                elif sub_on:
                    sub_g = self.djg_cached_gyro[djg_sub_side]
                    sub_a = self.djg_cached_accel[djg_sub_side]
                    for i in range(3):
                        sub_adj = sub_g[i] - sub_bias[i]
                        fused_gyro[i] = sub_adj + my_bias[i]
                        fused_accel[i] = sub_a[i]
                else:
                    for i in range(3):
                        fused_gyro[i] = my_bias[i]
                        fused_accel[i] = 0.0
                
                for i in range(3):
                    if abs(self.djg_accel_offset[i]) > 0.1:
                        self.djg_accel_offset[i] *= 0.99
                    else:
                        self.djg_accel_offset[i] = 0.0

                inputData.gyroscope = tuple(fused_gyro)
                inputData.accelerometer = tuple(fused_accel)

    def cleanup_vg_controller(self):
        for suffix in ('', '_l', '_r', '_pro'):
            port_attr = f'server_port{suffix}' if suffix else 'server_port'
            server_attr = f'usbip_server{suffix}' if suffix else 'usbip_server'
            
            if hasattr(self, server_attr) and getattr(self, server_attr):
                if hasattr(self, port_attr) and getattr(self, port_attr):
                    try:
                        detach_usbip_device(getattr(self, port_attr))
                    except Exception:
                        pass
                try:
                    getattr(self, server_attr).stop()
                except Exception:
                    pass
                setattr(self, server_attr, None)

        if hasattr(self, 'vg_controller_l') and self.vg_controller_l is not None:
            try:
                self.vg_controller_l.unregister_notification()
            except:
                pass
            try:
                self.vg_controller_l.close()
            except:
                pass
            self.vg_controller_l = None
        if hasattr(self, 'vg_controller_r') and self.vg_controller_r is not None:
            try:
                self.vg_controller_r.unregister_notification()
            except:
                pass
            try:
                self.vg_controller_r.close()
            except:
                pass
            self.vg_controller_r = None

        if self.vg_controller is not None:
            try:
                self.vg_controller.unregister_notification()
            except Exception as e:
                logger.debug(f"Unregister notification failed: {e}")
            if hasattr(self.vg_controller, 'cmp_func'):
                self.vg_controller.cmp_func = None
            if hasattr(self.vg_controller, 'close'):
                try:
                    self.vg_controller.close()
                except Exception as e:
                    logger.debug(f"Close failed: {e}")
            if hasattr(self.vg_controller, '_devicep') and self.vg_controller._devicep:
                try:
                    import vgamepad.win.vigem_client as vcli
                    if hasattr(self.vg_controller, '_busp') and self.vg_controller._busp:
                        vcli.vigem_target_remove(self.vg_controller._busp, self.vg_controller._devicep)
                except Exception as e:
                    logger.debug(f"ViGEm target remove failed: {e}")
            self.vg_controller = None

    def setup_virtual_device(self):
        with self.state_lock:
            if self.vg_controller is None and self.running:
                self._setup_vg_controller()

    def _setup_vg_controller(self):
        with VIRTUAL_DEVICE_CREATION_LOCK:
            import time
            time.sleep(0.5)
        server_port = self.server_port
        # Detach first while server socket is still active
        detach_usbip_device(server_port)
        if hasattr(self, 'usbip_server') and self.usbip_server:
            try:
                self.usbip_server.stop()
            except Exception:
                pass
            self.usbip_server = None

        if self.vg_controller is not None:
            self.cleanup_vg_controller()
            
            # Force cleanup of the old target
            gc.collect()
            time.sleep(0.5)

        driver_type = getattr(CONFIG, "driver_type", "WinUHid")

        if self.mode in ("Switch1", "Switch2"):
            import os
            import subprocess
            
            # 1. Explicitly detach all Switch1 sub-ports (server_port_l/r/pro) by their
            #    stored bus_id/host_ip, then stop the servers.
            for suffix in ('_l', '_r', '_pro'):
                port_attr = f'server_port{suffix}'
                bus_attr  = f'bus_id{suffix}'
                host_attr = f'host_ip{suffix}'
                svr_attr  = f'usbip_server{suffix}'
                svr = getattr(self, svr_attr, None)
                if svr is not None:
                    stored_port = getattr(self, port_attr, None)
                    if stored_port is not None:
                        try:
                            detach_usbip_device(stored_port)
                        except Exception:
                            pass
                    try:
                        svr.stop()
                    except Exception:
                        pass
                    setattr(self, svr_attr, None)
            
            # 2. Stop any top-level Switch2 server that might still be alive
            self.cleanup_vg_controller()
            
            # Force GC to release sockets/files
            gc.collect()
            time.sleep(0.5)
            
            if self.mode == "Switch2":
                usbip_exe = "C:\\Program Files\\USBip\\usbip.exe"
                
                # Try to start the Switch2 server; if port is occupied, allocate a fresh one.
                started = False
                for attempt in range(2):
                    try:
                        bus_id = self.bus_id
                        host_ip = self.host_ip
                        mac_address = self.controllers[0].device.address if self.controllers else None
                        
                        if attempt > 0:
                            # Re-allocate a fresh (host, bus_id, port) triple
                            host_ip, bus_id, server_port = USBIPAllocator.allocate()
                            self.host_ip   = host_ip
                            self.bus_id    = bus_id
                            self.server_port = server_port
                            logger.warning(f"Switch2 port conflict for Player {self.player_number}; retrying on {host_ip}:{server_port} bus={bus_id}")
                        
                        detach_usbip_device(server_port)
                        time.sleep(0.1)
                        
                        self.usbip_server = USBIPServer(
                            host=host_ip, port=server_port,
                            on_rumble_callback=self._usbip_rumble_callback,
                            bus_id=bus_id, mac_address=mac_address
                        )
                        self.usbip_server.start()
                        started = True
                        break
                    except Exception as e:
                        logger.error(f"Failed to start Switch2 USBIP Server (attempt {attempt+1}): {e}")
                        if self.usbip_server is not None:
                            try:
                                self.usbip_server.stop()
                            except Exception:
                                pass
                            self.usbip_server = None
                
                if started:
                    if os.path.exists(usbip_exe):
                        try:
                            detach_usbip_device(server_port)
                            time.sleep(0.2)
                            _sw2_attach_cmd = [usbip_exe, "-t", str(server_port), "attach", "-r", self.host_ip, "-b", self.bus_id]
                            logger.info(f"Switch2 USBIP attach cmd: {' '.join(_sw2_attach_cmd)}")
                            _sw2_proc = subprocess.Popen(
                                _sw2_attach_cmd,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
                            )
                            def _log_sw2_attach(proc, player_num, host, port, bus):
                                try:
                                    out, err = proc.communicate(timeout=10)
                                    rc = proc.returncode
                                    if out: logger.info(f"Player {player_num} SW2 attach stdout: {out.decode(errors='replace').strip()}")
                                    if err: logger.warning(f"Player {player_num} SW2 attach stderr: {err.decode(errors='replace').strip()}")
                                    if rc != 0:
                                        logger.error(f"Player {player_num} SW2 attach failed (rc={rc}) on {host}:{port} bus={bus}")
                                    else:
                                        logger.info(f"Player {player_num} SW2 attach OK on {host}:{port} bus={bus}")
                                except Exception as ex:
                                    logger.warning(f"Player {player_num} SW2 attach log error: {ex}")
                            threading.Thread(
                                target=_log_sw2_attach,
                                args=(_sw2_proc, self.player_number, self.host_ip, server_port, self.bus_id),
                                daemon=True
                            ).start()
                            logger.info(f"Attached virtual Switch2 Controller for Player {self.player_number} via USBIP on {self.host_ip}:{server_port} bus={self.bus_id}")
                        except Exception as e:
                            logger.error(f"Failed to attach USBIP device: {e}")
                    else:
                        logger.error(f"usbip.exe not found at {usbip_exe}!")
                else:
                    logger.error(f"Switch2 USBIP Server for Player {self.player_number} could not be started after retries.")
            else: # Switch1
                from usbip_server import USBIPJoyConLServer, USBIPJoyConRServer, USBIPProControllerServer
                self.usbip_server_l = None
                self.usbip_server_r = None
                self.usbip_server_pro = None
                
                # Check each controller
                for c in self.controllers:
                    mac_address = c.device.address
                    
                    host_ip, bus_id, port = USBIPAllocator.allocate()
                    
                    if c.is_joycon_left():
                        try:
                            self.server_port_l = port
                            self.bus_id_l = bus_id
                            self.host_ip_l = host_ip
                            detach_usbip_device(port)
                            
                            self.usbip_server_l = USBIPJoyConLServer(host=host_ip, port=port, on_rumble_callback=lambda d, p=port: self._usbip_rumble_callback(d, side="Left"), bus_id=bus_id, mac_address=mac_address)
                            self.usbip_server_l.start()
                            
                            usbip_exe = "C:\\Program Files\\USBip\\usbip.exe"
                            if os.path.exists(usbip_exe):
                                time.sleep(0.2)
                                subprocess.Popen([usbip_exe, "-t", str(port), "attach", "-r", host_ip, "-b", bus_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
                                logger.info(f"Attached virtual Joy-Con (L) for Player {self.player_number} via USBIP on {host_ip}:{port}")
                            else:
                                logger.error(f"usbip.exe not found at {usbip_exe}!")
                        except Exception as e:
                            logger.error(f"Failed to initialize Joy-Con (L) USBIP server/attach: {e}")
                            
                    elif c.is_joycon_right():
                        try:
                            self.server_port_r = port
                            self.bus_id_r = bus_id
                            self.host_ip_r = host_ip
                            detach_usbip_device(port)
                            
                            self.usbip_server_r = USBIPJoyConRServer(host=host_ip, port=port, on_rumble_callback=lambda d, p=port: self._usbip_rumble_callback(d, side="Right"), bus_id=bus_id, mac_address=mac_address)
                            self.usbip_server_r.start()
                            
                            usbip_exe = "C:\\Program Files\\USBip\\usbip.exe"
                            if os.path.exists(usbip_exe):
                                time.sleep(0.2)
                                subprocess.Popen([usbip_exe, "-t", str(port), "attach", "-r", host_ip, "-b", bus_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
                                logger.info(f"Attached virtual Joy-Con (R) for Player {self.player_number} via USBIP on {host_ip}:{port}")
                            else:
                                logger.error(f"usbip.exe not found at {usbip_exe}!")
                        except Exception as e:
                            logger.error(f"Failed to initialize Joy-Con (R) USBIP server/attach: {e}")
                            
                    elif c.is_pro_controller():
                        try:
                            self.server_port_pro = port
                            self.bus_id_pro = bus_id
                            self.host_ip_pro = host_ip
                            detach_usbip_device(port)
                            
                            self.usbip_server_pro = USBIPProControllerServer(host=host_ip, port=port, on_rumble_callback=lambda d, p=port: self._usbip_rumble_callback(d, side="Pro"), bus_id=bus_id, mac_address=mac_address)
                            self.usbip_server_pro.start()
                            
                            usbip_exe = "C:\\Program Files\\USBip\\usbip.exe"
                            if os.path.exists(usbip_exe):
                                time.sleep(0.2)
                                subprocess.Popen([usbip_exe, "-t", str(port), "attach", "-r", host_ip, "-b", bus_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
                                logger.info(f"Attached virtual Pro Controller for Player {self.player_number} via USBIP on {host_ip}:{port}")
                            else:
                                logger.error(f"usbip.exe not found at {usbip_exe}!")
                        except Exception as e:
                            logger.error(f"Failed to initialize Pro Controller USBIP server/attach: {e}")
                            
                    else:
                        logger.warning("GC Controller is temporarily excluded in Switch1 emulation mode")

            class MockGamepad:
                def __init__(self):
                    class MockClient:
                        is_connected = True
                    self.client = MockClient()
                def register_notification(self, callback_function):
                    pass
                def unregister_notification(self):
                    pass
                def update(self):
                    pass
                def close(self):
                    pass
            self.vg_controller = MockGamepad()
            self.driver_type = "USBIP"
        else:
            if driver_type == "USBIP" and self.mode == "PS5":
                import os
                import subprocess
                import time
                from usbip_dualsense_server import USBIPDualSenseServer

                usbip_exe = "C:\\Program Files\\USBip\\usbip.exe"
                mac_address = self.controllers[0].device.address if self.controllers else None

                # Mirror Switch2 pattern: use the MAC_TO_USBIP-cached allocation from __init__
                # (server_port was already detached at the top of _setup_vg_controller).
                # On conflict, re-allocate a fresh triple exactly like Switch2 does.
                started = False
                for attempt in range(2):
                    try:
                        host_ip = self.host_ip
                        bus_id = self.bus_id
                        port = self.server_port  # already detached above on attempt 0

                        if attempt > 0:
                            host_ip, bus_id, port = USBIPAllocator.allocate()
                            self.host_ip = host_ip
                            self.bus_id = bus_id
                            self.server_port = port
                            logger.warning(f"PS5 USBIP port conflict for Player {self.player_number}; retrying on {host_ip}:{port} bus={bus_id}")
                            detach_usbip_device(port)
                            time.sleep(0.1)

                        self.usbip_server = USBIPDualSenseServer(
                            host=host_ip, port=port,
                            on_rumble_callback=lambda d, p=port: self._dualsense_rumble_callback(d, side="Pro"),
                            bus_id=bus_id, mac_address=mac_address,
                            on_audio_data_callback=self._usbip_audio_callback
                        )
                        self.usbip_server.start()
                        started = True
                        break
                    except Exception as e:
                        logger.error(f"Failed to start PS5 USBIP Server (attempt {attempt + 1}): {e}")
                        if self.usbip_server is not None:
                            try:
                                self.usbip_server.stop()
                            except Exception:
                                pass
                            self.usbip_server = None

                if started:
                    threading.Thread(target=self._wasapi_loopback_thread, daemon=True).start()
                    # Disable the virtual DualSense audio playback endpoint in Windows so
                    # the game can drive the ISO-OUT haptic stream directly (an ENABLED
                    # endpoint is claimed/mixed by the Windows audio engine and starves
                    # the haptic channels).  Runs in the background with retries because
                    # the endpoint appears a moment after USBIP attach.
                    try:
                        from dualsense_audio_endpoint import disable_dualsense_audio_endpoint_async
                        disable_dualsense_audio_endpoint_async()
                    except Exception:
                        logger.debug("DualSense audio-endpoint auto-disable hook failed", exc_info=True)
                    if os.path.exists(usbip_exe):
                        try:
                            detach_usbip_device(self.server_port)
                            time.sleep(0.2)
                            _attach_cmd = [usbip_exe, "-t", str(self.server_port), "attach", "-r", self.host_ip, "-b", self.bus_id]
                            logger.info(f"PS5 USBIP attach cmd: {' '.join(_attach_cmd)}")
                            _attach_proc = subprocess.Popen(
                                _attach_cmd,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
                            )
                            def _log_attach_output(proc, player_num, host, port, bus):
                                try:
                                    out, err = proc.communicate(timeout=10)
                                    rc = proc.returncode
                                    if out: logger.info(f"Player {player_num} usbip attach stdout: {out.decode(errors='replace').strip()}")
                                    if err: logger.warning(f"Player {player_num} usbip attach stderr: {err.decode(errors='replace').strip()}")
                                    if rc != 0:
                                        logger.error(f"Player {player_num} usbip attach failed (rc={rc}) on {host}:{port} bus={bus}")
                                    else:
                                        logger.info(f"Player {player_num} usbip attach OK on {host}:{port} bus={bus}")
                                except Exception as ex:
                                    logger.warning(f"Player {player_num} usbip attach log error: {ex}")
                            threading.Thread(
                                target=_log_attach_output,
                                args=(_attach_proc, self.player_number, self.host_ip, self.server_port, self.bus_id),
                                daemon=True
                            ).start()
                            logger.info(f"Attached virtual DualSense for Player {self.player_number} via USBIP on {self.host_ip}:{self.server_port} bus={self.bus_id}")
                        except Exception as e:
                            logger.error(f"Failed to attach PS5 USBIP device: {e}")
                    else:
                        logger.error(f"usbip.exe not found at {usbip_exe}!")
                else:
                    logger.error(f"PS5 USBIP Server for Player {self.player_number} could not be started after retries.")

                class MockDualSenseGamepad:
                    def __init__(self, vc):
                        self.vc = vc
                        class MockClient:
                            is_connected = True
                        self.client = MockClient()
                    def register_notification(self, callback_function):
                        pass
                    def unregister_notification(self):
                        pass
                    def update(self):
                        if hasattr(self.vc, 'usbip_server') and self.vc.usbip_server:
                            self.vc.usbip_server.update_input(bytes(self.report))
                    def close(self):
                        pass

                self.vg_controller = MockDualSenseGamepad(self)
                self.vg_controller.report = DualSenseInputReport01()
                self.vg_controller.report.ReportId = 0x01
                self.vg_controller.report.LeftStickX = 128
                self.vg_controller.report.LeftStickY = 128
                self.vg_controller.report.RightStickX = 128
                self.vg_controller.report.RightStickY = 128
                self.vg_controller.report.PowerPercent = 10  # 100%
                self.vg_controller.report.PowerState = 2     # Normal
                self.vg_controller.report.PluggedHeadphones = 1
                self.vg_controller.report.PluggedMic = 1
                self.driver_type = "USBIP"
            elif driver_type == "ViGEmBus":
                try:
                    vigem = get_vigem()
                    if self.mode == "PS4":
                        self.vg_controller = vigem.VDS4Gamepad()
                        self.report_ex = DS4_REPORT_EX()
                        self.report_ex.Report.bThumbLX = 128
                        self.report_ex.Report.bThumbLY = 128
                        self.report_ex.Report.bThumbRX = 128
                        self.report_ex.Report.bThumbRY = 128
                        self.report_ex.Report.bBatteryLvl = 0xAF
                        self.report_ex.Report.bBatteryLvlSpecial = 0x08
                        self.ds4_timestamp = 0
                        logger.info("Switched to virtual PS4 controller via ViGEmBus")
                    else:
                        self.vg_controller = vigem.VX360Gamepad()
                        logger.info("Switched to virtual Xbox 360 controller via ViGEmBus")
                    self.driver_type = "ViGEmBus"
                except Exception as e:
                    logger.error(f"ViGEmBus initialization failed: {e}. Falling back to WinUHid.")
                    CONFIG.driver_type = "WinUHid"
                    CONFIG.simulation_mode = getattr(CONFIG, "winuhid_sim_mode", "PS5")
                    self.mode = CONFIG.simulation_mode
                    CONFIG.vigembus_installed = False
                    CONFIG.save_config()
                    driver_type = "WinUHid"

            if driver_type == "WinUHid":
                if self.mode == "PS4":
                    self.vg_controller = winuhid.VDS4Gamepad()
                    self.report = self.vg_controller.report
                    self.report.LeftStickX = 128
                    self.report.LeftStickY = 128
                    self.report.RightStickX = 128
                    self.report.RightStickY = 128
                    self.report.BatteryLevel = 0xAF
                    self.report.BatteryLevelSpecial = 0x08
                    logger.info("Switched to virtual PS4 controller via WinUHid")
                elif self.mode == "PS5":
                    self.vg_controller = winuhid.VDS5Gamepad()
                    self.report = self.vg_controller.report
                    self.report.LeftStickX = 128
                    self.report.LeftStickY = 128
                    self.report.RightStickX = 128
                    self.report.RightStickY = 128
                    self.report.BatteryPercent = 8
                    self.report.BatteryState = 2
                    self.report.Reserved3[0] = 0x08
                    logger.info("Switched to virtual PS5 controller via WinUHid")

                else:
                    self.vg_controller = winuhid.VX360Gamepad()
                    logger.info("Switched to virtual Xbox 360 controller via WinUHid")
                self.driver_type = "WinUHid"

            if self.vg_controller is not None and self.mode != "Switch1":
                self.vg_controller.register_notification(callback_function=self.vibration_callback)
            time.sleep(0.5)

        self.previous_buttons_left = 0x00000000
        self.previous_buttons_right = 0x00000000
        self.last_s2_lx = 0.0
        self.last_s2_ly = 0.0
        self.last_s2_rx = 0.0
        self.last_s2_ry = 0.0
        self.last_s2_gx = 0
        self.last_s2_gy = 0
        self.last_s2_gz = 0
        self.last_s2_ax = 0
        self.last_s2_ay = 0
        self.last_s2_az = 0
        self.was_touching = False
        self.was_touching_0 = False
        self.was_touching_1 = False
        self.touch_start_time = 0.0

    def set_mode(self, new_mode):
        if self.mode != new_mode:
            self.reset_inputs()
            with self.state_lock:
                self.mode = new_mode
                
                if self.mode == "Switch1":
                    self.hold_mode = "Vertical"
                elif self.is_single() and len(self.controllers) > 0:
                    from config import CONFIG
                    addr = self.controllers[0].device.address
                    if addr in CONFIG.joycon_hold_mode:
                        self.hold_mode = CONFIG.joycon_hold_mode[addr]
                    else:
                        self.hold_mode = "Vertical"
                
                if self.vg_controller is not None:
                    self._setup_vg_controller()
            self.reset_inputs()
            if self.loop and self.loop.is_running():
                asyncio.run_coroutine_threadsafe(self.update_leds(), self.loop)

    def vibration_callback(self, client, target, large_motor, small_motor, led_number, user_data):
        delay = getattr(CONFIG, "rumble_delay_ms", 0)
        if delay > 0:
            import threading
            threading.Timer(delay / 1000.0, self._vibration_callback_internal, args=(client, target, large_motor, small_motor, led_number, user_data)).start()
        else:
            self._vibration_callback_internal(client, target, large_motor, small_motor, led_number, user_data)

    def _vibration_callback_internal(self, client, target, large_motor, small_motor, led_number, user_data):
        import math
        import discoverer

        lf_val = int(800 * large_motor / 256)
        hf_val = int(800 * small_motor / 256)

        if self.loop is None or not self.loop.is_running():
            if discoverer.DISCOVERER_LOOP and discoverer.DISCOVERER_LOOP.is_running():
                self.loop = discoverer.DISCOVERER_LOOP
            else:
                try:
                    self.loop = asyncio.get_running_loop()
                except RuntimeError:
                    pass

        dt = time.perf_counter() - self.cycle_start_time
        slot_size = RUMBLE_WRITE_INTERVAL / 3.0
        slot = int(dt / slot_size) if slot_size > 0 else 0
        if slot < 0:
            slot = 0
        elif slot > 2:
            slot = 2

        with self.vibration_lock:
            # Accumulate into the shared buffer AND both per-side (L/R) buffers with the
            # same (mono) value. Merged Joy-Cons each consume their OWN side buffer; the
            # old single shared consume-once buffer let whichever Joy-Con polled first
            # clear the rumble, so the other side read latest_vibration (0 between game
            # updates) and stopped — that is the L/R complementary alternation. A single
            # controller just reads its own side; the unused side is harmless.
            for buf in (self.frame_vibrations, self.frame_vibrations_l, self.frame_vibrations_r):
                raw_lf = buf[slot].lf_amp + lf_val
                raw_hf = buf[slot].hf_amp + hf_val
                # Use non-linear scaling for summation to prevent clipping
                buf[slot].lf_amp = int(raw_lf) if raw_lf <= 560 else int(560 + 240 * math.tanh((raw_lf - 560) / 240))
                buf[slot].hf_amp = int(raw_hf) if raw_hf <= 560 else int(560 + 240 * math.tanh((raw_hf - 560) / 240))

            for lv in (self.latest_vibration, self.latest_vibration_l, self.latest_vibration_r):
                lv.lf_amp = lf_val
                lv.hf_amp = hf_val
            self.vibration_dirty = True
            self.vibration_dirty_l = True
            self.vibration_dirty_r = True

    def get_current_vibration_frames(self, is_left=True):
        if self.mode == "Switch1":
            with self.vibration_lock:
                vibs = self.switch_vibrations_left if is_left else self.switch_vibrations_right
                
                # Switch hardware timeout (Watchdog): Stop vibrating if no packet is received for 150ms
                # (Setting this too low will cause stuttering in games that send rumble at 50ms intervals)
                current_time = time.perf_counter()
                last_time = getattr(self, 'last_rumble_received_time', 0)
                
                if current_time - last_time > SWITCH_RUMBLE_TIMEOUT:
                    v1 = VibrationData()
                    v2 = VibrationData()
                    v3 = VibrationData()
                    self.switch_vibrations_left = [VibrationData() for _ in range(3)]
                    self.switch_vibrations_right = [VibrationData() for _ in range(3)]
                else:
                    v1 = VibrationData(
                        lf_freq=vibs[0].lf_freq, lf_amp=vibs[0].lf_amp, lf_en_tone=vibs[0].lf_en_tone,
                        hf_freq=vibs[0].hf_freq, hf_amp=vibs[0].hf_amp, hf_en_tone=vibs[0].hf_en_tone
                    )
                    v2 = VibrationData(
                        lf_freq=vibs[1].lf_freq, lf_amp=vibs[1].lf_amp, lf_en_tone=vibs[1].lf_en_tone,
                        hf_freq=vibs[1].hf_freq, hf_amp=vibs[1].hf_amp, hf_en_tone=vibs[1].hf_en_tone
                    )
                    v3 = VibrationData(
                        lf_freq=vibs[2].lf_freq, lf_amp=vibs[2].lf_amp, lf_en_tone=vibs[2].lf_en_tone,
                        hf_freq=vibs[2].hf_freq, hf_amp=vibs[2].hf_amp, hf_en_tone=vibs[2].hf_en_tone
                    )
                
                if is_left:
                    self.vibration_dirty_l = False
                else:
                    self.vibration_dirty_r = False
            
            is_zero = (v1.lf_amp == 0 and v1.hf_amp == 0 and v2.lf_amp == 0 and v2.hf_amp == 0 and v3.lf_amp == 0 and v3.hf_amp == 0)
            return v1, v2, v3, is_zero

        with self.vibration_lock:
            use_dualsense_stereo = self.mode == "PS5" and self.driver_type == "USBIP"

            if use_dualsense_stereo:
                current_time = time.perf_counter()
                last_received = getattr(self, 'last_rumble_received_time', 0)
                last_active = getattr(self, 'last_rumble_active_time', 0)
                last_time = max(last_received, last_active)

                if is_left:
                    latest_vibration = self.latest_vibration_l
                    frame_vibrations = self.frame_vibrations_l
                    vibration_dirty = self.vibration_dirty_l
                else:
                    latest_vibration = self.latest_vibration_r
                    frame_vibrations = self.frame_vibrations_r
                    vibration_dirty = self.vibration_dirty_r

                if vibration_dirty:
                    v1 = VibrationData(lf_amp=frame_vibrations[0].lf_amp, hf_amp=frame_vibrations[0].hf_amp, lf_freq=latest_vibration.lf_freq, hf_freq=latest_vibration.hf_freq)
                    v2 = VibrationData(lf_amp=frame_vibrations[1].lf_amp, hf_amp=frame_vibrations[1].hf_amp, lf_freq=latest_vibration.lf_freq, hf_freq=latest_vibration.hf_freq)
                    v3 = VibrationData(lf_amp=frame_vibrations[2].lf_amp, hf_amp=frame_vibrations[2].hf_amp, lf_freq=latest_vibration.lf_freq, hf_freq=latest_vibration.hf_freq)

                    if v1.lf_amp == 0 and v1.hf_amp == 0:
                        v1.lf_amp, v1.hf_amp = latest_vibration.lf_amp, latest_vibration.hf_amp
                    if v2.lf_amp == 0 and v2.hf_amp == 0:
                        v2.lf_amp, v2.hf_amp = v1.lf_amp, v1.hf_amp
                    if v3.lf_amp == 0 and v3.hf_amp == 0:
                        v3.lf_amp, v3.hf_amp = v2.lf_amp, v2.hf_amp

                    for f in frame_vibrations:
                        f.lf_amp = 0
                        f.hf_amp = 0

                    if is_left:
                        self.vibration_dirty_l = False
                    else:
                        self.vibration_dirty_r = False
                    self.cycle_start_time = time.perf_counter()
                else:
                    side_last_active = getattr(self,
                        'last_haptic_l_active_time' if is_left else 'last_haptic_r_active_time', 0)
                    lv = latest_vibration
                    if side_last_active > 0 and time.perf_counter() - side_last_active > SWITCH_RUMBLE_TIMEOUT:
                        lv = VibrationData()
                    v1 = VibrationData(lf_amp=lv.lf_amp, hf_amp=lv.hf_amp, lf_freq=lv.lf_freq, hf_freq=lv.hf_freq)
                    v2 = VibrationData(lf_amp=v1.lf_amp, hf_amp=v1.hf_amp, lf_freq=v1.lf_freq, hf_freq=v1.hf_freq)
                    v3 = VibrationData(lf_amp=v1.lf_amp, hf_amp=v1.hf_amp, lf_freq=v1.lf_freq, hf_freq=v1.hf_freq)

                is_zero = (v1.lf_amp == 0 and v1.hf_amp == 0 and v2.lf_amp == 0 and v2.hf_amp == 0 and v3.lf_amp == 0 and v3.hf_amp == 0)
                return v1, v2, v3, is_zero

            # Read THIS controller's own side buffer. Merged L+R Joy-Cons each consume
            # independently, so neither starves the other (the shared single-consumer
            # buffer caused the L/R complementary alternation). A single controller just
            # uses its own side.
            if is_left:
                side_frames = self.frame_vibrations_l
                side_latest = self.latest_vibration_l
                side_dirty = self.vibration_dirty_l
            else:
                side_frames = self.frame_vibrations_r
                side_latest = self.latest_vibration_r
                side_dirty = self.vibration_dirty_r

            if self.mode == "Switch2":
                current_time = time.perf_counter()
                last_active = getattr(self, 'last_rumble_active_time', 0)
                if last_active and current_time - last_active > SWITCH_RUMBLE_TIMEOUT:
                    self.frame_vibrations = [VibrationData() for _ in range(3)]
                    self.frame_vibrations_l = [VibrationData() for _ in range(3)]
                    self.frame_vibrations_r = [VibrationData() for _ in range(3)]
                    self.latest_vibration = VibrationData(lf_amp=0, hf_amp=0)
                    self.latest_vibration_l = VibrationData(lf_amp=0, hf_amp=0)
                    self.latest_vibration_r = VibrationData(lf_amp=0, hf_amp=0)
                    self.vibration_dirty = False
                    self.vibration_dirty_l = False
                    self.vibration_dirty_r = False
                    self.cycle_start_time = current_time
                    v1 = VibrationData()
                    v2 = VibrationData()
                    v3 = VibrationData()
                    return v1, v2, v3, True

            if side_dirty:
                v1 = VibrationData(lf_amp=side_frames[0].lf_amp, hf_amp=side_frames[0].hf_amp)
                v2 = VibrationData(lf_amp=side_frames[1].lf_amp, hf_amp=side_frames[1].hf_amp)
                v3 = VibrationData(lf_amp=side_frames[2].lf_amp, hf_amp=side_frames[2].hf_amp)

                if v1.lf_amp == 0 and v1.hf_amp == 0:
                    v1.lf_amp, v1.hf_amp = side_latest.lf_amp, side_latest.hf_amp
                if v2.lf_amp == 0 and v2.hf_amp == 0:
                    v2.lf_amp, v2.hf_amp = v1.lf_amp, v1.hf_amp
                if v3.lf_amp == 0 and v3.hf_amp == 0:
                    v3.lf_amp, v3.hf_amp = v2.lf_amp, v2.hf_amp

                for f in side_frames:
                    f.lf_amp = 0
                    f.hf_amp = 0
                if is_left:
                    self.vibration_dirty_l = False
                else:
                    self.vibration_dirty_r = False
                self.cycle_start_time = time.perf_counter()
            else:
                side_last_active = getattr(self,
                    'last_haptic_l_active_time' if is_left else 'last_haptic_r_active_time', 0)
                sl = side_latest
                if side_last_active > 0 and time.perf_counter() - side_last_active > SWITCH_RUMBLE_TIMEOUT:
                    sl = VibrationData()
                v1 = VibrationData(lf_amp=sl.lf_amp, hf_amp=sl.hf_amp)
                v2 = VibrationData(lf_amp=v1.lf_amp, hf_amp=v1.hf_amp)
                v3 = VibrationData(lf_amp=v1.lf_amp, hf_amp=v1.hf_amp)
                self.cycle_start_time = time.perf_counter()

            is_zero = (v1.lf_amp == 0 and v1.hf_amp == 0 and v2.lf_amp == 0 and v2.hf_amp == 0 and v3.lf_amp == 0 and v3.hf_amp == 0)
            return v1, v2, v3, is_zero

    async def init_added_controller(self, controller: Controller):
        controller.virtual_controller = self
        self.loop = asyncio.get_running_loop()
        if self.vibration_changed_event is None:
            self.vibration_changed_event = asyncio.Event()
        await self.update_leds()

        if self.mode == "Switch1":
            self.hold_mode = "Vertical"
            from usbip_server import USBIPJoyConLServer, USBIPJoyConRServer, USBIPProControllerServer
            import os
            import subprocess
            import time
            from utils import USBIPAllocator
            
            mac_address = controller.device.address
            if controller.is_joycon_left() and getattr(self, 'usbip_server_l', None) is None:
                host_ip, bus_id, port = USBIPAllocator.allocate()
                self.server_port_l = port
                self.bus_id_l = bus_id
                self.host_ip_l = host_ip
                try:
                    detach_usbip_device(port)
                except Exception:
                    pass
                self.usbip_server_l = USBIPJoyConLServer(host=host_ip, port=port, on_rumble_callback=lambda d, p=port: self._usbip_rumble_callback(d, side="Left"), bus_id=bus_id, mac_address=mac_address)
                self.usbip_server_l.start()
                usbip_exe = "C:\\Program Files\\USBip\\usbip.exe"
                if os.path.exists(usbip_exe):
                    time.sleep(0.2)
                    subprocess.Popen([usbip_exe, "-t", str(port), "attach", "-r", host_ip, "-b", bus_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
                    logger.info(f"Re-Attached virtual Joy-Con (L) for Player {self.player_number} via USBIP on {host_ip}:{port}")
            elif controller.is_joycon_right() and getattr(self, 'usbip_server_r', None) is None:
                host_ip, bus_id, port = USBIPAllocator.allocate()
                self.server_port_r = port
                self.bus_id_r = bus_id
                self.host_ip_r = host_ip
                try:
                    detach_usbip_device(port)
                except Exception:
                    pass
                self.usbip_server_r = USBIPJoyConRServer(host=host_ip, port=port, on_rumble_callback=lambda d, p=port: self._usbip_rumble_callback(d, side="Right"), bus_id=bus_id, mac_address=mac_address)
                self.usbip_server_r.start()
                usbip_exe = "C:\\Program Files\\USBip\\usbip.exe"
                if os.path.exists(usbip_exe):
                    time.sleep(0.2)
                    subprocess.Popen([usbip_exe, "-t", str(port), "attach", "-r", host_ip, "-b", bus_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
                    logger.info(f"Re-Attached virtual Joy-Con (R) for Player {self.player_number} via USBIP on {host_ip}:{port}")
            elif controller.is_pro_controller() and getattr(self, 'usbip_server_pro', None) is None:
                host_ip, bus_id, port = USBIPAllocator.allocate()
                self.server_port_pro = port
                self.bus_id_pro = bus_id
                self.host_ip_pro = host_ip
                try:
                    detach_usbip_device(port)
                except Exception:
                    pass
                self.usbip_server_pro = USBIPProControllerServer(host=host_ip, port=port, on_rumble_callback=lambda d, p=port: self._usbip_rumble_callback(d, side="Pro"), bus_id=bus_id, mac_address=mac_address)
                self.usbip_server_pro.start()
                usbip_exe = "C:\\Program Files\\USBip\\usbip.exe"
                if os.path.exists(usbip_exe):
                    time.sleep(0.2)
                    subprocess.Popen([usbip_exe, "-t", str(port), "attach", "-r", host_ip, "-b", bus_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
                    logger.info(f"Re-Attached virtual Pro Controller for Player {self.player_number} via USBIP on {host_ip}:{port}")
            
            if self.vg_controller is None:
                class MockGamepad:
                    def __init__(self):
                        class MockClient:
                            is_connected = True
                        self.client = MockClient()
                    def register_notification(self, callback_function):
                        pass
                    def unregister_notification(self):
                        pass
                    def update(self):
                        pass
                    def close(self):
                        pass
                self.vg_controller = MockGamepad()
                self.driver_type = "USBIP"
        elif self.is_single() and controller.is_joycon():
            addr = controller.device.address
            if addr in CONFIG.joycon_hold_mode:
                self.hold_mode = CONFIG.joycon_hold_mode[addr]
                logger.info(f"Loaded hold mode '{self.hold_mode}' for Joy-Con {addr}")
        elif len(self.controllers) == 2:
            left_mac = None
            right_mac = None
            for c in self.controllers:
                if c.is_joycon_left():
                    left_mac = c.device.address
                elif c.is_joycon_right():
                    right_mac = c.device.address
            if left_mac and right_mac:
                key = f"{left_mac}+{right_mac}"
                if key in CONFIG.merged_gyro_side:
                    self.active_gyro_side = CONFIG.merged_gyro_side[key]
                    logger.info(f"Loaded merged active gyro side '{self.active_gyro_side}' for combination {key}")
        
        # Reset Gyro Mouse state to prevent leftover state after Split/Merge
        controller.gyro_mouse_enabled = False
        controller.gr_was_pressed = False
        controller.prev_zr = False
        controller.prev_zl = False
        controller._own_gyro_trigger = False
        controller._shared_gyro_trigger = False
        controller._own_zr_pressed = False
        controller._shared_zr_pressed = False
        controller._own_zl_pressed = False
        controller._shared_zl_pressed = False
        controller._last_raw_buttons = 0
        controller._own_mode_shift_toggle = False
        controller._own_mode_shift_tap_edge = False
        controller._own_mode_shift_hold_pressed = False
        controller._own_mode_shift_active = False
        controller._shared_mode_shift_toggle = False
        controller._shared_mode_shift_hold_pressed = False
        controller._shared_mode_shift_active = False
        controller.gyro_target_vx = 0.0
        controller.gyro_target_vy = 0.0
        controller.current_vx = 0.0
        controller.current_vy = 0.0
        controller.interp_residual_x = 0.0
        controller.interp_residual_y = 0.0
        controller.gyro_steering_origin_accel = None
        
        def input_report_callback(inputData: ControllerInputData, controller: Controller):
            if self.vg_controller is None:
                return
                
            # Xbox One, Xbox360, PS4, PS5: abxy_mode=="Switch" triggers Switch layout swap.
            # Switch2 uses abxy_mode=="Xbox" (inverted naming convention).
            if self.mode == "Switch2" or self.mode == "Switch1":
                is_switch_layout = (CONFIG.abxy_mode == "Xbox")
            else:
                is_switch_layout = (CONFIG.abxy_mode == "Switch")
            
            if len(self.controllers) == 2 or controller.is_pro_controller():
                if getattr(CONFIG, "djg_enabled", False):
                    mode = getattr(CONFIG, "djg_mode", "Single Side Toggle")
                    if mode == "Switch Gyro Side":
                        controller.gyro_active = (controller.is_joycon_left() and self.active_gyro_side == "Left") or (controller.is_joycon_right() and self.active_gyro_side == "Right") or controller.is_pro_controller()
                    else:
                        controller.gyro_active = (controller.is_joycon_left() and getattr(self, 'djg_left_active', True)) or (controller.is_joycon_right() and getattr(self, 'djg_right_active', True))
                else:
                    controller.gyro_active = (controller.is_joycon_left() and self.active_gyro_side == "Left") or (controller.is_joycon_right() and self.active_gyro_side == "Right") or controller.is_pro_controller()
                controller.hold_mode = "Vertical"
            else:
                controller.gyro_active = True
                controller.hold_mode = getattr(self, "hold_mode", "Vertical")
            
            # In combined mode, share gyro trigger from any side to all controllers
            # Allows the gyro-active controller to receive the trigger signal
            is_merged = (len(self.controllers) == 2)
            for c in self.controllers:
                c.is_merged = is_merged

            if is_merged:
                # Sync Gyro Trigger
                if getattr(CONFIG, "djg_enabled", False):
                    shared_gyro = getattr(controller, '_own_gyro_trigger', False)
                else:
                    shared_gyro = any(getattr(c, '_own_gyro_trigger', False) for c in self.controllers)
                    
                # Sync ZR/ZL for Gyro Mouse clicks
                shared_zr = any(getattr(c, '_own_zr_pressed', False) for c in self.controllers)
                shared_zl = any(getattr(c, '_own_zl_pressed', False) for c in self.controllers)

                # Mode Shift back button: triggering it on either Joy-Con applies the Mode
                # Shift mapping layer to both sides. Merged Joy-Cons use one shared Tap
                # toggle; per-side toggles cannot be ORed because a right-side Tap must be
                # able to close a left-side Tap-entered Mode Shift.
                if not hasattr(self, '_mode_shift_shared_toggle'):
                    self._mode_shift_shared_toggle = False
                if getattr(controller, '_own_mode_shift_tap_edge', False):
                    self._mode_shift_shared_toggle = not self._mode_shift_shared_toggle
                    controller._own_mode_shift_tap_edge = False
                shared_mode_shift_toggle = bool(self._mode_shift_shared_toggle)
                shared_mode_shift_hold_pressed = any(getattr(c, '_own_mode_shift_hold_pressed', False) for c in self.controllers)
                shared_mode_shift = shared_mode_shift_toggle != shared_mode_shift_hold_pressed

                # Sync activation state before sharing gyro-derived outputs. In Hold mode,
                # the per-side gyro_mouse_enabled flag is driven by the shared trigger.
                if getattr(CONFIG, 'gyro_activation_mode', 'Hold') == 'Hold':
                    for c in self.controllers:
                        if getattr(CONFIG, "djg_enabled", False):
                            c.gyro_mouse_enabled = getattr(c, '_own_gyro_trigger', False)
                        else:
                            c.gyro_mouse_enabled = shared_gyro
                
                # Sync Steer Value (From the gyro-active controller)
                shared_steer = 0.0
                shared_rs = (0.0, 0.0)
                shared_gyro_rs = (0.0, 0.0)
                for c in self.controllers:
                    if getattr(c, 'gyro_active', False):
                        shared_steer = getattr(c, '_own_steer_value', 0.0)
                        if getattr(c, 'gyro_mouse_enabled', False) or shared_gyro:
                            shared_gyro_rs = getattr(c, '_gyro_rstick_out', (0.0, 0.0))
                    if c.is_joycon_right():
                        shared_rs = inputData.right_stick if c == controller else getattr(c, '_last_rs', (0.0, 0.0))

                for c in self.controllers:
                    if getattr(CONFIG, "djg_enabled", False):
                        c._shared_gyro_trigger = getattr(c, '_own_gyro_trigger', False)
                    else:
                        c._shared_gyro_trigger = shared_gyro
                    c._shared_zr_pressed = shared_zr
                    c._shared_zl_pressed = shared_zl
                    c._shared_mode_shift_toggle = shared_mode_shift_toggle
                    c._shared_mode_shift_hold_pressed = shared_mode_shift_hold_pressed
                    c._shared_mode_shift_active = shared_mode_shift
                    c._shared_steer_value = shared_steer
                    c._shared_gyro_rstick_out = shared_gyro_rs
                    c._shared_right_stick = shared_rs
                
                if controller.is_joycon_right():
                    controller._last_rs = inputData.right_stick
                
            else:
                # If not merged, ensure we don't use a stale shared steer value
                controller._shared_steer_value = getattr(controller, '_own_steer_value', 0.0)
                controller._shared_gyro_rstick_out = getattr(controller, '_gyro_rstick_out', (0.0, 0.0))
                controller._shared_gyro_trigger = getattr(controller, '_own_gyro_trigger', False)
                controller._shared_zr_pressed = getattr(controller, '_own_zr_pressed', False)
                controller._shared_zl_pressed = getattr(controller, '_own_zl_pressed', False)
                controller._shared_mode_shift_toggle = getattr(controller, '_own_mode_shift_toggle', False)
                controller._shared_mode_shift_hold_pressed = getattr(controller, '_own_mode_shift_hold_pressed', False)
                controller._shared_mode_shift_active = getattr(controller, '_own_mode_shift_active', False)
                
            current_buttons = inputData.buttons 
            
            # Mouse mappings consume stick input in controller.py. Gyro mouse alone should not
            # force-disable the virtual right stick.
            any_mouse_active = any(getattr(c, 'jc_mouse_active', False) or getattr(c, 'joystick_mouse_active', False) for c in self.controllers)
            if any_mouse_active:
                inputData.right_stick = (0.0, 0.0)

            # In-app Gyro "R Joystick" control mode: gyro motion drives the virtual right
            # stick. Add the gyro-derived deflection from the gyro-active controller and
            # clamp to the stick's maximum (unit magnitude).
            gyro_rstick_overlay = (0.0, 0.0)
            if getattr(CONFIG, "gyro_control_mode", "Mouse") == "R Joystick":
                gyro_rs = (0.0, 0.0)
                if is_merged:
                    gyro_rs = getattr(controller, '_shared_gyro_rstick_out', (0.0, 0.0))
                elif getattr(controller, 'gyro_mouse_enabled', False):
                    gyro_rs = getattr(controller, '_gyro_rstick_out', (0.0, 0.0))
                if gyro_rs[0] != 0.0 or gyro_rs[1] != 0.0:
                    if controller.is_joycon():
                        gyro_rstick_overlay = gyro_rs
                    else:
                        rx = inputData.right_stick[0] + gyro_rs[0]
                        ry = inputData.right_stick[1] + gyro_rs[1]
                        inputData.right_stick = (rx, ry)

            if len(self.controllers) == 1 and self.mode != "Switch1":
                custom_btns = getattr(inputData, 'custom_buttons_mask', 0)
                custom_btns &= current_buttons
                current_buttons &= ~custom_btns
                custom_stick_route = getattr(inputData, 'custom_joystick_mapping', None)

                def apply_custom_stick_route():
                    if not custom_stick_route:
                        return
                    sx, sy = custom_stick_route.get("stick", (0, 0))
                    if custom_stick_route.get("source") == "gyro":
                        if self.hold_mode == "Horizontal":
                            if controller.is_joycon_left():
                                sx, sy = -sy, sx
                            elif controller.is_joycon_right():
                                sx, sy = sy, -sx
                    elif self.hold_mode == "Horizontal":
                        if custom_stick_route.get("source") == "left":
                            sx, sy = -sy, sx
                        elif custom_stick_route.get("source") == "right":
                            sx, sy = sy, -sx
                    if custom_stick_route.get("target") == "left":
                        inputData.left_stick = (sx, sy)
                        inputData.right_stick = (0, 0)
                    elif custom_stick_route.get("target") == "right":
                        inputData.left_stick = (0, 0)
                        inputData.right_stick = (sx, sy)

                if controller.is_joycon_left():
                    if self.hold_mode == "Vertical":
                        if not custom_stick_route:
                            inputData.right_stick = inputData.left_stick
                            inputData.left_stick = (0, 0)
                        else:
                            apply_custom_stick_route()
                        
                        new_btns = current_buttons & ~(SWITCH_BUTTONS["UP"] | SWITCH_BUTTONS["DOWN"] | SWITCH_BUTTONS["LEFT"] | SWITCH_BUTTONS["RIGHT"] | SWITCH_BUTTONS["L"] | SWITCH_BUTTONS["ZL"] | SWITCH_BUTTONS["L_STK"] | SWITCH_BUTTONS["MINUS"])
                        
                        if current_buttons & SWITCH_BUTTONS["L_STK"]:
                            new_btns |= SWITCH_BUTTONS["R_STK"]
                            
                        if is_switch_layout:
                            if current_buttons & SWITCH_BUTTONS["UP"]: new_btns |= SWITCH_BUTTONS["Y"]
                            if current_buttons & SWITCH_BUTTONS["DOWN"]: new_btns |= SWITCH_BUTTONS["A"]
                            if current_buttons & SWITCH_BUTTONS["LEFT"]: new_btns |= SWITCH_BUTTONS["X"]
                            if current_buttons & SWITCH_BUTTONS["RIGHT"]: new_btns |= SWITCH_BUTTONS["B"]
                        else:
                            if current_buttons & SWITCH_BUTTONS["UP"]: new_btns |= SWITCH_BUTTONS["X"]
                            if current_buttons & SWITCH_BUTTONS["DOWN"]: new_btns |= SWITCH_BUTTONS["B"]
                            if current_buttons & SWITCH_BUTTONS["LEFT"]: new_btns |= SWITCH_BUTTONS["Y"]
                            if current_buttons & SWITCH_BUTTONS["RIGHT"]: new_btns |= SWITCH_BUTTONS["A"]
                            
                        if current_buttons & SWITCH_BUTTONS["L"]: new_btns |= SWITCH_BUTTONS["R"]
                        if current_buttons & SWITCH_BUTTONS["ZL"]: new_btns |= SWITCH_BUTTONS["ZR"]
                        if current_buttons & SWITCH_BUTTONS["MINUS"]: new_btns |= SWITCH_BUTTONS["PLUS"]
                        current_buttons = new_btns
                        
                    elif self.hold_mode == "Horizontal":
                        if not custom_stick_route:
                            lx, ly = inputData.left_stick
                            inputData.left_stick = (-ly, lx)
                            inputData.right_stick = (0, 0)
                        else:
                            apply_custom_stick_route()
                        
                        new_btns = current_buttons & ~(SWITCH_BUTTONS["UP"] | SWITCH_BUTTONS["DOWN"] | SWITCH_BUTTONS["LEFT"] | SWITCH_BUTTONS["RIGHT"] | SWITCH_BUTTONS["SL_L"] | SWITCH_BUTTONS["SR_L"] | SWITCH_BUTTONS["L"] | SWITCH_BUTTONS["ZL"] | SWITCH_BUTTONS["MINUS"])
                        
                        if is_switch_layout:
                            if current_buttons & SWITCH_BUTTONS["UP"]: new_btns |= SWITCH_BUTTONS["X"]
                            if current_buttons & SWITCH_BUTTONS["DOWN"]: new_btns |= SWITCH_BUTTONS["B"]
                            if current_buttons & SWITCH_BUTTONS["LEFT"]: new_btns |= SWITCH_BUTTONS["A"]
                            if current_buttons & SWITCH_BUTTONS["RIGHT"]: new_btns |= SWITCH_BUTTONS["Y"]
                        else:
                            if current_buttons & SWITCH_BUTTONS["UP"]: new_btns |= SWITCH_BUTTONS["Y"]
                            if current_buttons & SWITCH_BUTTONS["DOWN"]: new_btns |= SWITCH_BUTTONS["A"]
                            if current_buttons & SWITCH_BUTTONS["LEFT"]: new_btns |= SWITCH_BUTTONS["B"]
                            if current_buttons & SWITCH_BUTTONS["RIGHT"]: new_btns |= SWITCH_BUTTONS["X"]
                            
                        if current_buttons & SWITCH_BUTTONS["SL_L"]: new_btns |= SWITCH_BUTTONS["ZL"]
                        if current_buttons & SWITCH_BUTTONS["SR_L"]: new_btns |= SWITCH_BUTTONS["ZR"]
                        if current_buttons & SWITCH_BUTTONS["MINUS"]: new_btns |= SWITCH_BUTTONS["PLUS"]
                        current_buttons = new_btns
                elif controller.is_joycon_right():
                    if self.hold_mode == "Vertical":
                        if custom_stick_route:
                            apply_custom_stick_route()
                    elif self.hold_mode == "Horizontal":
                        if not custom_stick_route:
                            rx, ry = inputData.right_stick
                            inputData.right_stick = (ry, -rx)
                        else:
                            apply_custom_stick_route()
                        new_btns = current_buttons & ~(SWITCH_BUTTONS["X"] | SWITCH_BUTTONS["Y"] | SWITCH_BUTTONS["A"] | SWITCH_BUTTONS["B"] | SWITCH_BUTTONS["SL_R"] | SWITCH_BUTTONS["SR_R"] | SWITCH_BUTTONS["R"] | SWITCH_BUTTONS["ZR"] | SWITCH_BUTTONS["PLUS"] | SWITCH_BUTTONS["R_STK"])
                        
                        if is_switch_layout:
                            if current_buttons & SWITCH_BUTTONS["A"]: new_btns |= SWITCH_BUTTONS["X"]
                            if current_buttons & SWITCH_BUTTONS["X"]: new_btns |= SWITCH_BUTTONS["Y"]
                            if current_buttons & SWITCH_BUTTONS["B"]: new_btns |= SWITCH_BUTTONS["A"]
                            if current_buttons & SWITCH_BUTTONS["Y"]: new_btns |= SWITCH_BUTTONS["B"]
                        else:
                            if current_buttons & SWITCH_BUTTONS["A"]: new_btns |= SWITCH_BUTTONS["B"]
                            if current_buttons & SWITCH_BUTTONS["X"]: new_btns |= SWITCH_BUTTONS["A"]
                            if current_buttons & SWITCH_BUTTONS["B"]: new_btns |= SWITCH_BUTTONS["Y"]
                            if current_buttons & SWITCH_BUTTONS["Y"]: new_btns |= SWITCH_BUTTONS["X"]

                        if current_buttons & SWITCH_BUTTONS["SL_R"]: new_btns |= SWITCH_BUTTONS["ZL"]
                        if current_buttons & SWITCH_BUTTONS["SR_R"]: new_btns |= SWITCH_BUTTONS["ZR"]
                        if current_buttons & SWITCH_BUTTONS["PLUS"]: new_btns |= SWITCH_BUTTONS["PLUS"]
                        if current_buttons & SWITCH_BUTTONS["R_STK"]: new_btns |= SWITCH_BUTTONS["L_STK"]
                        current_buttons = new_btns
                
                current_buttons |= custom_btns

            inputData.gyro_rstick_overlay = gyro_rstick_overlay
                    
            if len(self.controllers) == 2:
                buttonsConfig = CONFIG.dual_joycons_config
                if controller.is_joycon_left(): self.previous_buttons_left = current_buttons
                else: self.previous_buttons_right = current_buttons
                buttons = self.previous_buttons_left | self.previous_buttons_right
            else:
                buttons = current_buttons
                if controller.is_joycon_left(): buttonsConfig = CONFIG.single_joycon_l_config
                elif controller.is_joycon_right(): buttonsConfig = CONFIG.single_joycon_r_config
                else: buttonsConfig = CONFIG.procon_config
                
            if is_merged and getattr(CONFIG, "djg_enabled", False):
                pass


            if getattr(CONFIG, "gyro_passthrough_mode", "Default") == "Cemuhook":
                import cemuhook_udp
                from discoverer import VIRTUAL_CONTROLLERS
                
                if self.mode == "Switch1":
                    send_cemuhook = True
                elif len(self.controllers) == 1:
                    send_cemuhook = True
                else:
                    send_cemuhook = False
                    if getattr(CONFIG, "djg_enabled", False):
                        dom_side = getattr(CONFIG, "djg_dominant_side", "Left")
                        if controller.is_joycon_left() and dom_side == "Left":
                            send_cemuhook = True
                        elif controller.is_joycon_right() and dom_side == "Right":
                            send_cemuhook = True
                    else:
                        if controller.is_joycon_left() and self.active_gyro_side == "Left":
                            send_cemuhook = True
                        elif controller.is_joycon_right() and self.active_gyro_side == "Right":
                            send_cemuhook = True
                
                if send_cemuhook:
                        model = 3 if (controller.is_joycon_left() or controller.is_joycon_right()) else 2
                        addr_str = (controller.device.address or "").replace(':', '').replace('-', '').upper()
                        try:
                            mac_bytes = bytes.fromhex(addr_str)
                            if len(mac_bytes) != 6:
                                raise ValueError("not a 6-byte MAC")
                        except (ValueError, AttributeError):
                            # device.address is a placeholder — try controller_info.mac_address
                            # (real BLE MAC set from firmware connected event for ESP32 path)
                            info_mac = (getattr(controller.controller_info, 'mac_address', None) or "")
                            info_str = info_mac.replace(':', '').replace('-', '').upper()
                            try:
                                mac_bytes = bytes.fromhex(info_str)
                                if len(mac_bytes) != 6:
                                    raise ValueError()
                            except (ValueError, AttributeError):
                                import hashlib
                                mac_bytes = hashlib.sha1(
                                    (controller.device.address or "esp32").encode()
                                ).digest()[:6]
                        
                        hold_mode = getattr(self, "hold_mode", "Vertical")
                        
                        # 1. 統一為標準的 V mode 物理軸向
                        # 根據實測，Joy-Con 2 (左/右) 與 Pro Controller 的原始 IMU 座標系完全一致
                        # 皆需要反轉三軸的重力向量 (X, Y, Z)，才能在 Yuzu 等模擬器中得到正確的旋轉方向與重力向量
                        base_gyro = (inputData.gyroscope[0], -inputData.gyroscope[1], -inputData.gyroscope[2])
                        base_accel = (-inputData.accelerometer[0], -inputData.accelerometer[1], -inputData.accelerometer[2])

                        # 2. 如果使用者選擇水平握持 (H mode)，套用對應的 90 度旋轉
                        if hold_mode == "Horizontal" and not controller.is_pro_controller():
                            if controller.is_joycon_right():
                                # 右手把：水平時 SL/SR 朝上，相當於順時針旋轉 90 度
                                emu_gyro = (-base_gyro[1], base_gyro[0], base_gyro[2])
                                emu_accel = (-base_accel[1], base_accel[0], base_accel[2])
                            else:
                                # 左手把：水平時 SL/SR 朝上，相當於逆時針旋轉 90 度
                                emu_gyro = (base_gyro[1], -base_gyro[0], base_gyro[2])
                                emu_accel = (base_accel[1], -base_accel[0], base_accel[2])
                        else:
                            # 垂直握持 (V mode)
                            emu_gyro = base_gyro
                            emu_accel = base_accel

                        # 3. 將物理軸向轉換為 DS4 軸向 (Cemuhook 要求標準 DS4 軸向)
                        # DS4 軸向定義為 Pitch (X), Yaw (Z), -Roll (Y)
                        ds4_gyro = (emu_gyro[0], emu_gyro[2], -emu_gyro[1])
                        ds4_accel = (emu_accel[0], emu_accel[2], -emu_accel[1])

                        cemuhook_udp.cemuhook_server.report_controller_data(
                            model, mac_bytes, 4, inputData, ds4_accel, ds4_gyro)
                
                # Zero out gyro/accel so the virtual controller driver gets no gyro
                inputData.gyroscope = (0.0, 0.0, 0.0)
                inputData.accelerometer = (0.0, 0.0, 0.0)

            if self.mode == "PS4":
                self.update_as_ps4(inputData, buttons, controller)
            elif self.mode == "PS5":
                self.update_as_ps5(inputData, buttons, controller)
            elif self.mode == "Switch2":
                self.update_as_switch2_pro(inputData, buttons, controller)
            elif self.mode == "Switch1":
                if controller.is_pro_controller() and getattr(self, 'usbip_server_pro', None) is not None:
                    self.update_as_switch1_pro(inputData, buttons, controller)
                elif controller.is_joycon_left() and getattr(self, 'usbip_server_l', None) is not None:
                    self.update_as_switch1_joycon_l(inputData, buttons, controller)
                elif controller.is_joycon_right() and getattr(self, 'usbip_server_r', None) is not None:
                    self.update_as_switch1_joycon_r(inputData, buttons, controller)
            else:
                self.update_as_xbox(inputData, buttons, controller, buttonsConfig)
            
            # Record raw buttons for shared click logic in next report
            controller._last_raw_buttons = current_buttons

        def wrapped_callback(inputData: ControllerInputData, controller: Controller):
            with self.state_lock:
                input_report_callback(inputData, controller)

        controller.set_input_report_callback(wrapped_callback)
        controller.gyro_fusion_callback = self.gyro_fusion_callback


    def _build_switch1_report(self, inputData: ControllerInputData, buttons: int, controller, device_type: str):
        state = bytearray(50)
        state[0] = 0x30
        
        if device_type == "L":
            state[2] = 0x9E
        elif device_type == "R":
            state[2] = 0x8E
        else: # Pro
            state[2] = 0x8E
        
        hold_mode = getattr(self, 'hold_mode', 'Vertical')
        # Scale Gyro to match the exact sensitivity expected by emulators for a Switch 1 Joy-Con.
        # Switch 2 controllers natively output ~16.384 LSB/dps (0.061 dps/LSB).
        # Switch 1 controllers natively output ~14.37 LSB/dps (0.0695 dps/LSB).
        # To make a Switch 2 controller perfectly emulate a Switch 1 controller, we must 
        # multiply its raw output by (14.37 / 16.384) = ~0.877 so that emulators calculate 
        # the exact physical rotation when they divide by their assumed 14.37 LSB/dps.
        # (0.0535 / 0.061) is mathematically ~0.87704, which perfectly achieves this.
        jc_gyro_scale = 0.0535 / 0.061
        jc_yaw_mult = 1.0

        if device_type != "Pro":
            import time as _t
            if getattr(controller, '_sw1_gyro_accum', None) is None:
                controller._sw1_gyro_accum = [0.0, 0.0, 0.0]
                controller._sw1_gyro_accum_time = 0.0
                controller._sw1_gyro_emit_frames = [(0.0, 0.0, 0.0)] * 3
                controller._sw1_gyro_last_t = _t.perf_counter()
                controller._sw1_gyro_last_timer = inputData.raw_data[1] if (len(inputData.raw_data) > 1 and inputData.raw_data[0] == 0x30) else None

            if len(inputData.raw_data) > 1 and inputData.raw_data[0] == 0x30:
                current_timer = inputData.raw_data[1]
                if controller._sw1_gyro_last_timer is not None:
                    timer_diff = (current_timer - controller._sw1_gyro_last_timer) & 0xFF
                    _dt = timer_diff * 0.005
                else:
                    _dt = 0.0075 # fallback
                controller._sw1_gyro_last_timer = current_timer
                _integration_dt = _dt
            else:
                _now = _t.perf_counter()
                raw_dt = _now - controller._sw1_gyro_last_t
                controller._sw1_gyro_last_t = _now

                # Prevent absurd _dt on lag spikes
                if raw_dt > 0.1: raw_dt = 0.015
                if raw_dt < 0.0: raw_dt = 0.001
                
                # Auto-adapt to System BLE polling rate (e.g. 66Hz or 20Hz) while eliminating jitter.
                # Uses an Exponential Moving Average (EMA) to find the steady "per-tick" cadence.
                if getattr(controller, '_sw1_gyro_smoothed_dt', None) is None:
                    controller._sw1_gyro_smoothed_dt = raw_dt
                else:
                    controller._sw1_gyro_smoothed_dt = 0.85 * controller._sw1_gyro_smoothed_dt + 0.15 * raw_dt
                
                _dt = raw_dt
                if getattr(controller, 'is_esp32s3_bridge', False):
                    # ESP32 bridge (USB Serial) has inherently low jitter and handles its own cadence perfectly.
                    # Bypassing the EMA filter ensures ESP32 maintains its exact original precise behavior.
                    _integration_dt = raw_dt
                else:
                    _integration_dt = controller._sw1_gyro_smoothed_dt

            # Accumulate true physical rotation.
            # _dt controls the 15ms emission pacing, _integration_dt handles smooth magnitude integration.
            controller._sw1_gyro_accum_time += _integration_dt
            _acc = controller._sw1_gyro_accum
            _rg = inputData.gyroscope
            
            for _i in range(3):
                _acc[_i] += _rg[_i] * _integration_dt

            _NATIVE_DT = 0.015
            should_emit = False
            
            # Strictly push only when 15ms has elapsed physically.
            # A real Joy-Con sends a 0x30 report with 3 IMU frames every 15ms (66.7Hz).
            if controller._sw1_gyro_accum_time >= _NATIVE_DT:
                _avg = tuple(_acc[_i] / 0.015 for _i in range(3))
                controller._sw1_gyro_emit_frames = [_avg, _avg, _avg]
                
                controller._sw1_gyro_accum = [0.0, 0.0, 0.0]
                controller._sw1_gyro_accum_time -= 0.015
                if controller._sw1_gyro_accum_time > 0.015:
                    controller._sw1_gyro_accum_time = 0.0 # Safety cap
                should_emit = True
                
            gsrc3 = controller._sw1_gyro_emit_frames
        else:
            if getattr(controller, '_sw1_gyro_history', None) is None:
                controller._sw1_gyro_history = [(0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)]
            controller._sw1_gyro_history.pop(0)
            controller._sw1_gyro_history.append(inputData.gyroscope)
            gsrc3 = list(controller._sw1_gyro_history)
            should_emit = True

        lx, ly, rx, ry = 0.0, 0.0, 0.0, 0.0
        gx, gy, gz, ax, ay, az = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
        custom_stick_route = getattr(inputData, 'custom_joystick_mapping', None)

        if device_type == "L":
            if custom_stick_route:
                lx = inputData.left_stick[0]
                ly = inputData.left_stick[1]
                rx = inputData.right_stick[0]
                ry = inputData.right_stick[1]
            else:
                lx = inputData.left_stick[0]
                ly = inputData.left_stick[1]
            
            if hold_mode == "Vertical":
                _gmap = lambda g: (g[1] * jc_gyro_scale, -g[0] * jc_gyro_scale, g[2] * jc_gyro_scale * jc_yaw_mult)
                ax, ay, az =  inputData.accelerometer[1],  -inputData.accelerometer[0],  inputData.accelerometer[2]
            else: # Horizontal
                _gmap = lambda g: (g[0] * jc_gyro_scale, g[1] * jc_gyro_scale, g[2] * jc_gyro_scale * jc_yaw_mult)
                ax, ay, az =  inputData.accelerometer[0], inputData.accelerometer[1],  inputData.accelerometer[2]

            if getattr(CONFIG, "gyro_mode", "World") == "Roll" and controller.gyro_mouse_enabled:
                lx = getattr(controller, '_shared_steer_value', getattr(controller, '_own_steer_value', 0.0))

        elif device_type == "R": # device_type == "R"
            if custom_stick_route:
                lx = inputData.left_stick[0]
                ly = inputData.left_stick[1]
                rx = inputData.right_stick[0]
                ry = inputData.right_stick[1]
            else:
                rx = inputData.right_stick[0]
                ry = inputData.right_stick[1]
            
            if hold_mode == "Vertical":
                _gmap = lambda g: (g[1] * jc_gyro_scale, g[0] * jc_gyro_scale, -g[2] * jc_gyro_scale * jc_yaw_mult)
                ax, ay, az =  inputData.accelerometer[1], inputData.accelerometer[0], -inputData.accelerometer[2]
            else: # Horizontal
                _gmap = lambda g: (-g[0] * jc_gyro_scale, g[1] * jc_gyro_scale, -g[2] * jc_gyro_scale * jc_yaw_mult)
                ax, ay, az = -inputData.accelerometer[0], inputData.accelerometer[1], -inputData.accelerometer[2]

            if getattr(CONFIG, "gyro_mode", "World") == "Roll" and controller.gyro_mouse_enabled:
                rx = getattr(controller, '_shared_steer_value', getattr(controller, '_own_steer_value', 0.0))
        else: # Pro
            lx = inputData.left_stick[0]
            ly = inputData.left_stick[1]
            rx = inputData.right_stick[0]
            ry = inputData.right_stick[1]

            _gmap = lambda g: (g[1], -g[0], g[2])
            ax, ay, az = inputData.accelerometer[1], -inputData.accelerometer[0], inputData.accelerometer[2]

            if getattr(CONFIG, "gyro_mode", "World") == "Roll" and controller.gyro_mouse_enabled:
                lx = getattr(controller, '_shared_steer_value', getattr(controller, '_own_steer_value', 0.0))

        rx, ry = self._add_gyro_rstick_overlay(rx, ry, inputData)

        def float_to_12bit(val):
            return int(max(0, min(4095, round((val + 1.0) * 2047.5))))

        lx_12 = float_to_12bit(lx)
        ly_12 = float_to_12bit(ly)
        rx_12 = float_to_12bit(rx)
        ry_12 = float_to_12bit(ry)
        
        controller._sw1_should_emit = should_emit

        # Left stick packed (bytes 6-8)
        state[6] = lx_12 & 0xff
        state[7] = ((lx_12 >> 8) & 0x0f) | ((ly_12 & 0x0f) << 4)
        state[8] = (ly_12 >> 4) & 0xff

        # Right stick packed (bytes 9-11)
        state[9] = rx_12 & 0xff
        state[10] = ((rx_12 >> 8) & 0x0f) | ((ry_12 & 0x0f) << 4)
        state[11] = (ry_12 >> 4) & 0xff

        b3, b4, b5 = 0, 0, 0
        
        if device_type == "L":
            if buttons & SWITCH_BUTTONS["DOWN"]:  b5 |= 0x01
            if buttons & SWITCH_BUTTONS["UP"]:    b5 |= 0x02
            if buttons & SWITCH_BUTTONS["RIGHT"]: b5 |= 0x04
            if buttons & SWITCH_BUTTONS["LEFT"]:  b5 |= 0x08
            if buttons & SWITCH_BUTTONS.get("SR_L", 0): b5 |= 0x10
            if buttons & SWITCH_BUTTONS.get("SL_L", 0): b5 |= 0x20
            if buttons & SWITCH_BUTTONS["L"]:     b5 |= 0x40
            if buttons & SWITCH_BUTTONS["ZL"]:    b5 |= 0x80
            
            if buttons & SWITCH_BUTTONS["MINUS"]: b4 |= 0x01
            if buttons & SWITCH_BUTTONS["L_STK"]: b4 |= 0x08
            if buttons & SWITCH_BUTTONS.get("CAPT", 0): b4 |= 0x20
        elif device_type == "R": # Right Joycon
            if buttons & SWITCH_BUTTONS["Y"]:     b3 |= 0x01
            if buttons & SWITCH_BUTTONS["X"]:     b3 |= 0x02
            if buttons & SWITCH_BUTTONS["B"]:     b3 |= 0x04
            if buttons & SWITCH_BUTTONS["A"]:     b3 |= 0x08
            if buttons & SWITCH_BUTTONS.get("SR_R", 0): b3 |= 0x10
            if buttons & SWITCH_BUTTONS.get("SL_R", 0): b3 |= 0x20
            if buttons & SWITCH_BUTTONS["R"]:     b3 |= 0x40
            if buttons & SWITCH_BUTTONS["ZR"]:    b3 |= 0x80
            
            if buttons & SWITCH_BUTTONS["PLUS"]:  b4 |= 0x02
            if buttons & SWITCH_BUTTONS["R_STK"]: b4 |= 0x04
            if buttons & SWITCH_BUTTONS.get("HOME", 0): b4 |= 0x10
        else: # Pro Controller
            if buttons & SWITCH_BUTTONS["Y"]:     b3 |= 0x01
            if buttons & SWITCH_BUTTONS["X"]:     b3 |= 0x02
            if buttons & SWITCH_BUTTONS["B"]:     b3 |= 0x04
            if buttons & SWITCH_BUTTONS["A"]:     b3 |= 0x08
            if buttons & SWITCH_BUTTONS.get("SR_R", 0): b3 |= 0x10
            if buttons & SWITCH_BUTTONS.get("SL_R", 0): b3 |= 0x20
            if buttons & SWITCH_BUTTONS["R"]:     b3 |= 0x40
            if buttons & SWITCH_BUTTONS["ZR"]:    b3 |= 0x80
            
            if buttons & SWITCH_BUTTONS["MINUS"]: b4 |= 0x01
            if buttons & SWITCH_BUTTONS["PLUS"]:  b4 |= 0x02
            if buttons & SWITCH_BUTTONS["R_STK"]: b4 |= 0x04
            if buttons & SWITCH_BUTTONS["L_STK"]: b4 |= 0x08
            if buttons & SWITCH_BUTTONS.get("HOME", 0): b4 |= 0x10
            if buttons & SWITCH_BUTTONS.get("CAPT", 0): b4 |= 0x20
            
            if buttons & SWITCH_BUTTONS["DOWN"]:  b5 |= 0x01
            if buttons & SWITCH_BUTTONS["UP"]:    b5 |= 0x02
            if buttons & SWITCH_BUTTONS["RIGHT"]: b5 |= 0x04
            if buttons & SWITCH_BUTTONS["LEFT"]:  b5 |= 0x08
            if buttons & SWITCH_BUTTONS.get("SR_L", 0): b5 |= 0x10
            if buttons & SWITCH_BUTTONS.get("SL_L", 0): b5 |= 0x20
            if buttons & SWITCH_BUTTONS["L"]:     b5 |= 0x40
            if buttons & SWITCH_BUTTONS["ZL"]:    b5 |= 0x80
            
        state[3] = b3
        state[4] = b4
        state[5] = b5

        def clamp_i16(v): return max(-32768, min(32767, int(round(v))))

        # The 3 IMU frames carry 3 sub-samples 5 ms apart (oldest=frame 0 .. newest=frame 2),
        # per imu_sensor_notes.md, so the host gets 5 ms-precision motion instead of one 15 ms
        # burst.  Same accel in all three (accel is a direct reading, not integrated); gyro is
        # the per-third sub-sample mapped through _gmap.  (For Pro / experiment, gsrc3 holds 3
        # identical samples → 3 identical frames, i.e. the original behaviour.)
        for _fi, _off in enumerate((13, 25, 37)):
            _g = _gmap(gsrc3[_fi])
            state[_off:_off + 12] = struct.pack('<6h',
                clamp_i16(ax), clamp_i16(ay), clamp_i16(az),
                clamp_i16(_g[0]), clamp_i16(_g[1]), clamp_i16(_g[2]))

        controller._sw1_should_emit = should_emit
        return state

    def update_as_switch1_joycon_l(self, inputData: ControllerInputData, buttons: int, controller):
        if self.driver_type == "USBIP":
            if hasattr(self, 'usbip_server_l') and self.usbip_server_l:
                state = self._build_switch1_report(inputData, buttons, controller, device_type="L")
                if getattr(controller, '_sw1_should_emit', True):
                    self.usbip_server_l.update_state(state)

    def update_as_switch1_joycon_r(self, inputData: ControllerInputData, buttons: int, controller):
        if self.driver_type == "USBIP":
            if hasattr(self, 'usbip_server_r') and self.usbip_server_r:
                state = self._build_switch1_report(inputData, buttons, controller, device_type="R")
                if getattr(controller, '_sw1_should_emit', True):
                    self.usbip_server_r.update_state(state)

    def update_as_switch1_pro(self, inputData: ControllerInputData, buttons: int, controller):
        if self.driver_type == "USBIP":
            if hasattr(self, 'usbip_server_pro') and self.usbip_server_pro:
                state = self._build_switch1_report(inputData, buttons, controller, device_type="Pro")
                self.usbip_server_pro.update_state(state)

    def _add_gyro_rstick_overlay(self, rx, ry, inputData):
        gx, gy = getattr(inputData, "gyro_rstick_overlay", (0.0, 0.0))
        if gx == 0.0 and gy == 0.0:
            return rx, ry
        rx += gx
        ry += gy
        mag = (rx * rx + ry * ry) ** 0.5
        if mag > 1.0:
            rx /= mag
            ry /= mag
        return rx, ry

    def update_as_ps4(self, inputData: ControllerInputData, buttons: int, controller: Controller):

        with self.state_lock:
            if self.vg_controller is None:
                return
            self._update_as_ps4_locked(inputData, buttons, controller)
            if getattr(self, 'driver_type', '') != "ViGEmBus":
                self.vg_controller.update()

    def _update_as_ps4_locked(self, inputData: ControllerInputData, buttons: int, controller: Controller):
        driver_type = self.driver_type
        if driver_type == "ViGEmBus":
            report = self.report_ex.Report
            
            ds4_buttons = 0
            if buttons & SWITCH_BUTTONS["Y"]: ds4_buttons |= DS4_BUTTONS.DS4_BUTTON_SQUARE
            if buttons & SWITCH_BUTTONS["X"]: ds4_buttons |= DS4_BUTTONS.DS4_BUTTON_TRIANGLE
            if buttons & SWITCH_BUTTONS["B"]: ds4_buttons |= DS4_BUTTONS.DS4_BUTTON_CROSS
            if buttons & SWITCH_BUTTONS["A"]: ds4_buttons |= DS4_BUTTONS.DS4_BUTTON_CIRCLE
            if buttons & SWITCH_BUTTONS["L"]: ds4_buttons |= DS4_BUTTONS.DS4_BUTTON_SHOULDER_LEFT
            if buttons & SWITCH_BUTTONS["R"]: ds4_buttons |= DS4_BUTTONS.DS4_BUTTON_SHOULDER_RIGHT
            if buttons & SWITCH_BUTTONS["ZL"]: ds4_buttons |= DS4_BUTTONS.DS4_BUTTON_TRIGGER_LEFT
            if buttons & SWITCH_BUTTONS["ZR"]: ds4_buttons |= DS4_BUTTONS.DS4_BUTTON_TRIGGER_RIGHT
            if buttons & SWITCH_BUTTONS["MINUS"]: ds4_buttons |= DS4_BUTTONS.DS4_BUTTON_SHARE
            if buttons & SWITCH_BUTTONS["PLUS"]: ds4_buttons |= DS4_BUTTONS.DS4_BUTTON_OPTIONS
            if buttons & SWITCH_BUTTONS["L_STK"]: ds4_buttons |= DS4_BUTTONS.DS4_BUTTON_THUMB_LEFT
            if buttons & SWITCH_BUTTONS["R_STK"]: ds4_buttons |= DS4_BUTTONS.DS4_BUTTON_THUMB_RIGHT

            up = bool(buttons & SWITCH_BUTTONS["UP"])
            down = bool(buttons & SWITCH_BUTTONS["DOWN"])
            left = bool(buttons & SWITCH_BUTTONS["LEFT"])
            right = bool(buttons & SWITCH_BUTTONS["RIGHT"])
            report.wButtons = ds4_buttons | get_ds4_dpad(up, down, left, right)

            report.bSpecial = 0
            if buttons & SWITCH_BUTTONS.get("HOME", 0): 
                report.bSpecial |= DS4_SPECIAL_BUTTONS.DS4_SPECIAL_BUTTON_PS

            capt = bool(buttons & SWITCH_BUTTONS.get("CAPT", 0))
            tpad_l = bool(buttons & SWITCH_BUTTONS.get("PS_L_Touch", 0))
            tpad_r = bool(buttons & SWITCH_BUTTONS.get("PS_R_Touch", 0))
            tpad_c = bool(buttons & SWITCH_BUTTONS.get("PS_C_Click", 0))

            is_touching = capt or tpad_l or tpad_r or tpad_c

            if is_touching:
                report.bSpecial |= DS4_SPECIAL_BUTTONS.DS4_SPECIAL_BUTTON_TOUCHPAD
                if not getattr(self, 'was_touching', False):
                    self.touch_tracking_id = (getattr(self, 'touch_tracking_id', 0) + 1) & 0x7F
                report.sCurrentTouch.bIsUpTrackingNum1 = self.touch_tracking_id

                if tpad_l:
                    report.sCurrentTouch.bTouchData1[0] = 0xE0
                    report.sCurrentTouch.bTouchData1[1] = 0x71
                    report.sCurrentTouch.bTouchData1[2] = 0x1D
                elif tpad_r:
                    report.sCurrentTouch.bTouchData1[0] = 0xA0
                    report.sCurrentTouch.bTouchData1[1] = 0x75
                    report.sCurrentTouch.bTouchData1[2] = 0x1D
                else:
                    report.sCurrentTouch.bTouchData1[0] = 0xC0
                    report.sCurrentTouch.bTouchData1[1] = 0x73
                    report.sCurrentTouch.bTouchData1[2] = 0x1D
            else:
                report.sCurrentTouch.bIsUpTrackingNum1 = 0x80 | getattr(self, 'touch_tracking_id', 0)

            self.was_touching = is_touching
            report.sCurrentTouch.bIsUpTrackingNum2 = 0x80

            if getattr(controller.controller_info, 'product_id', 0) == NSO_GAMECUBE_CONTROLLER_PID:
                report.bTriggerL = inputData.left_trigger
                report.bTriggerR = inputData.right_trigger
            else:
                report.bTriggerL = 255 if (buttons & SWITCH_BUTTONS["ZL"]) else 0
                report.bTriggerR = 255 if (buttons & SWITCH_BUTTONS["ZR"]) else 0

            # Joystick routing
            if not hasattr(self, 'last_lx'):
                self.last_lx = 128; self.last_ly = 128
                self.last_rx = 128; self.last_ry = 128
                self.last_gx = 0; self.last_gy = 0; self.last_gz = 0
                self.last_ax = 0; self.last_ay = 0; self.last_az = 0

            custom_stick_route = getattr(inputData, 'custom_joystick_mapping', None)
            if len(self.controllers) == 1:
                if not controller.is_joycon() and (
                    self._joystick_mapping_mode("l_joystick", controller) in ("L Joystick", "R Joystick") or
                    self._joystick_mapping_mode("r_joystick", controller) in ("L Joystick", "R Joystick")
                ):
                    mixed_left, mixed_right = self._update_merged_stick_mix(inputData, controller)
                    self.last_lx = float_to_byte(mixed_left[0])
                    self.last_ly = float_to_byte(-mixed_left[1])
                    self.last_rx = float_to_byte(mixed_right[0])
                    self.last_ry = float_to_byte(-mixed_right[1])
                elif custom_stick_route:
                    self.last_lx = float_to_byte(inputData.left_stick[0])
                    self.last_ly = float_to_byte(-inputData.left_stick[1])
                    self.last_rx = float_to_byte(inputData.right_stick[0])
                    self.last_ry = float_to_byte(-inputData.right_stick[1])
                elif controller.is_joycon_right():
                    if self.hold_mode == "Vertical":
                        self.last_rx = int(max(0, min(255, round(inputData.right_stick[0] * 127.5 + 128))))
                        self.last_ry = int(max(0, min(255, round(-inputData.right_stick[1] * 127.5 + 128))))
                        self.last_lx = 128
                        self.last_ly = 128
                    else:
                        self.last_lx = float_to_byte(inputData.right_stick[0])
                        self.last_ly = float_to_byte(-inputData.right_stick[1])
                        self.last_rx = 128
                        self.last_ry = 128
                else:
                    self.last_lx = float_to_byte(inputData.left_stick[0])
                    self.last_ly = float_to_byte(-inputData.left_stick[1])
                    self.last_rx = float_to_byte(inputData.right_stick[0])
                    self.last_ry = float_to_byte(-inputData.right_stick[1])

                rx_float = (self.last_rx - 128) / 127.5
                ry_float = -((self.last_ry - 128) / 127.5)
                rx_float, ry_float = self._add_gyro_rstick_overlay(rx_float, ry_float, inputData)
                self.last_rx = float_to_byte(rx_float)
                self.last_ry = float_to_byte(-ry_float)
                
                if self.hold_mode == "Horizontal" and not controller.is_pro_controller():
                    if controller.is_joycon_right():
                        self.last_gx = inputData.gyroscope[1]
                        self.last_gy = inputData.gyroscope[2]
                        self.last_gz = -inputData.gyroscope[0]
                        self.last_ax = -inputData.accelerometer[1]
                        self.last_ay = inputData.accelerometer[2]
                        self.last_az = inputData.accelerometer[0]
                    else:
                        self.last_gx = -inputData.gyroscope[1]
                        self.last_gy = inputData.gyroscope[2]
                        self.last_gz = inputData.gyroscope[0]
                        self.last_ax = -inputData.accelerometer[1]
                        self.last_ay = inputData.accelerometer[2]
                        self.last_az = -inputData.accelerometer[0]
                else:
                    self.last_gx = inputData.gyroscope[0]
                    self.last_gy = inputData.gyroscope[2]
                    self.last_gz = -inputData.gyroscope[1]
                    self.last_ax = inputData.accelerometer[0]
                    self.last_ay = inputData.accelerometer[2]
                    self.last_az = -inputData.accelerometer[1]
            else:
                mixed_left, mixed_right = self._update_merged_stick_mix(inputData, controller)
                mixed_right = self._add_gyro_rstick_overlay(mixed_right[0], mixed_right[1], inputData)
                self.last_lx = float_to_byte(mixed_left[0])
                self.last_ly = float_to_byte(-mixed_left[1])
                self.last_rx = float_to_byte(mixed_right[0])
                self.last_ry = float_to_byte(-mixed_right[1])

                is_passthrough_source = False
                if getattr(CONFIG, "djg_enabled", False):
                    dom_side = getattr(CONFIG, "djg_dominant_side", "Left")
                    if controller.is_joycon_left() and dom_side == "Left":
                        is_passthrough_source = True
                    elif controller.is_joycon_right() and dom_side == "Right":
                        is_passthrough_source = True
                else:
                    if controller.is_joycon_left() and self.active_gyro_side == "Left":
                        is_passthrough_source = True
                    elif controller.is_joycon_right() and self.active_gyro_side == "Right":
                        is_passthrough_source = True

                if is_passthrough_source:
                    self.last_gx = inputData.gyroscope[0]
                    self.last_gy = inputData.gyroscope[2]
                    self.last_gz = -inputData.gyroscope[1]
                    self.last_ax = inputData.accelerometer[0]
                    self.last_ay = inputData.accelerometer[2]
                    self.last_az = -inputData.accelerometer[1]

            if getattr(CONFIG, "gyro_mode", "World") == "Roll" and controller.gyro_mouse_enabled:
                steer = getattr(controller, '_shared_steer_value', controller._own_steer_value if hasattr(controller, '_own_steer_value') else 0.0)
                self.last_lx = int(max(0, min(255, round(steer * 127.5 + 128))))

            report.bThumbLX = self.last_lx
            report.bThumbLY = self.last_ly
            report.bThumbRX = self.last_rx
            report.bThumbRY = self.last_ry

            def clamp_short(val): return max(-32768, min(32767, int(val)))
            report.wGyroX = clamp_short(self.last_gx)
            report.wGyroY = clamp_short(self.last_gy)
            report.wGyroZ = clamp_short(self.last_gz)
            report.wAccelX = clamp_short(self.last_ax)
            report.wAccelY = clamp_short(self.last_ay)
            report.wAccelZ = clamp_short(self.last_az)
        else:
            self._update_ps_controller_locked(inputData, buttons, controller, self.vg_controller.report, mode="PS4")

    def update_as_ps5(self, inputData: ControllerInputData, buttons: int, controller: Controller):
        with self.state_lock:
            if self.vg_controller is None:
                return
            self._update_as_ps5_locked(inputData, buttons, controller)
            self.vg_controller.update()

    def _update_as_ps5_locked(self, inputData: ControllerInputData, buttons: int, controller: Controller):
        self._update_ps_controller_locked(inputData, buttons, controller, self.vg_controller.report, mode="PS5")

    def _update_ps_controller_locked(self, inputData: ControllerInputData, buttons: int, controller: Controller, report, mode: str):
        # 1. Map buttons
        report.ButtonSquare = 1 if (buttons & SWITCH_BUTTONS["Y"]) else 0
        report.ButtonTriangle = 1 if (buttons & SWITCH_BUTTONS["X"]) else 0
        report.ButtonCross = 1 if (buttons & SWITCH_BUTTONS["B"]) else 0
        report.ButtonCircle = 1 if (buttons & SWITCH_BUTTONS["A"]) else 0
        
        report.ButtonL1 = 1 if (buttons & SWITCH_BUTTONS["L"]) else 0
        report.ButtonR1 = 1 if (buttons & SWITCH_BUTTONS["R"]) else 0
        report.ButtonL2 = 1 if (buttons & SWITCH_BUTTONS["ZL"]) else 0
        report.ButtonR2 = 1 if (buttons & SWITCH_BUTTONS["ZR"]) else 0
        
        report.ButtonShare = 1 if (buttons & SWITCH_BUTTONS["MINUS"]) else 0
        report.ButtonOptions = 1 if (buttons & SWITCH_BUTTONS["PLUS"]) else 0
        report.ButtonL3 = 1 if (buttons & SWITCH_BUTTONS["L_STK"]) else 0
        report.ButtonR3 = 1 if (buttons & SWITCH_BUTTONS["R_STK"]) else 0
        
        report.ButtonHome = 1 if (buttons & SWITCH_BUTTONS.get("HOME", 0)) else 0

        if mode == "PS5":
            report.ButtonMute = 1 if (buttons & 0x10000000) else 0

        # 2. D-pad (Hat)
        up = bool(buttons & SWITCH_BUTTONS["UP"])
        down = bool(buttons & SWITCH_BUTTONS["DOWN"])
        left = bool(buttons & SWITCH_BUTTONS["LEFT"])
        right = bool(buttons & SWITCH_BUTTONS["RIGHT"])
        
        hat_x = -1 if left else (1 if right else 0)
        hat_y = -1 if up else (1 if down else 0)
        
        hat_val = 8
        if hat_x == 0 and hat_y == -1: hat_val = 0
        elif hat_x == 1 and hat_y == -1: hat_val = 1
        elif hat_x == 1 and hat_y == 0: hat_val = 2
        elif hat_x == 1 and hat_y == 1: hat_val = 3
        elif hat_x == 0 and hat_y == 1: hat_val = 4
        elif hat_x == -1 and hat_y == 1: hat_val = 5
        elif hat_x == -1 and hat_y == 0: hat_val = 6
        elif hat_x == -1 and hat_y == -1: hat_val = 7
        
        report.Hat = hat_val

        # 3. Touchpad
        capt = bool(buttons & SWITCH_BUTTONS.get("CAPT", 0))
        tpad_l = bool(buttons & SWITCH_BUTTONS.get("PS_L_Touch", 0))
        tpad_r = bool(buttons & SWITCH_BUTTONS.get("PS_R_Touch", 0))
        tpad_c = bool(buttons & SWITCH_BUTTONS.get("PS_C_Click", 0))

        # Set mechanical click button (CAPT and PS_C_Click trigger click)
        report.ButtonTouchpad = 1 if (capt or tpad_c) else 0

        # Touch Point 0: Left touch or Center touch (CAPT)
        touch_0_down = tpad_l or capt
        touch_0_x = 100 if tpad_l else 960
        touch_0_y = 512

        # Touch Point 1: Right touch
        touch_1_down = tpad_r
        touch_1_x = 1800
        touch_1_y = 512

        is_new_touch_0 = touch_0_down and not self.was_touching_0
        is_new_touch_1 = touch_1_down and not self.was_touching_1

        # Set touch states for both points
        self._set_touch_state(report, 0, touch_0_down, touch_0_x, touch_0_y, mode, is_new_touch_0)
        self._set_touch_state(report, 1, touch_1_down, touch_1_x, touch_1_y, mode, is_new_touch_1)

        self.was_touching_0 = touch_0_down
        self.was_touching_1 = touch_1_down
        self.was_touching = touch_0_down or touch_1_down

        # 4. Triggers
        if getattr(controller.controller_info, 'product_id', 0) == NSO_GAMECUBE_CONTROLLER_PID:
            report.LeftTrigger = inputData.left_trigger
            report.RightTrigger = inputData.right_trigger
            is_zl_pressed = inputData.left_trigger > 10
            is_zr_pressed = inputData.right_trigger > 10
        else:
            report.LeftTrigger = 255 if (buttons & SWITCH_BUTTONS["ZL"]) else 0
            report.RightTrigger = 255 if (buttons & SWITCH_BUTTONS["ZR"]) else 0
            is_zl_pressed = bool(buttons & SWITCH_BUTTONS["ZL"])
            is_zr_pressed = bool(buttons & SWITCH_BUTTONS["ZR"])
            
        import time
        now = time.perf_counter()
        
        if is_zl_pressed and not getattr(self, 'prev_zl_pressed', False):
            if getattr(self, 'last_lt_mode', 0) not in (0x00, 0x05):
                self.trigger_l_punch_end = now + 0.150
        self.prev_zl_pressed = is_zl_pressed

        if is_zr_pressed and not getattr(self, 'prev_zr_pressed', False):
            if getattr(self, 'last_rt_mode', 0) not in (0x00, 0x05):
                self.trigger_r_punch_end = now + 0.150
        self.prev_zr_pressed = is_zr_pressed
        
        if mode == "PS5":
            report.SequenceNumber = (report.SequenceNumber + 1) & 0xFF
        # 5. Joysticks Routing
        if not hasattr(self, 'last_lx'):
            self.last_lx = 128; self.last_ly = 128
            self.last_rx = 128; self.last_ry = 128
            self.last_gx = 0; self.last_gy = 0; self.last_gz = 0
            self.last_ax = 0; self.last_ay = 0; self.last_az = 0

        custom_stick_route = getattr(inputData, 'custom_joystick_mapping', None)
        if len(self.controllers) == 1:
            if not controller.is_joycon() and (
                self._joystick_mapping_mode("l_joystick", controller) in ("L Joystick", "R Joystick") or
                self._joystick_mapping_mode("r_joystick", controller) in ("L Joystick", "R Joystick")
            ):
                mixed_left, mixed_right = self._update_merged_stick_mix(inputData, controller)
                self.last_lx = float_to_byte(mixed_left[0])
                self.last_ly = float_to_byte(-mixed_left[1])
                self.last_rx = float_to_byte(mixed_right[0])
                self.last_ry = float_to_byte(-mixed_right[1])
            elif custom_stick_route:
                self.last_lx = float_to_byte(inputData.left_stick[0])
                self.last_ly = float_to_byte(-inputData.left_stick[1])
                self.last_rx = float_to_byte(inputData.right_stick[0])
                self.last_ry = float_to_byte(-inputData.right_stick[1])
            elif controller.is_joycon_right():
                if self.hold_mode == "Vertical":
                    self.last_rx = int(max(0, min(255, round(inputData.right_stick[0] * 127.5 + 128))))
                    self.last_ry = int(max(0, min(255, round(-inputData.right_stick[1] * 127.5 + 128))))
                    self.last_lx = 128
                    self.last_ly = 128
                else:
                    self.last_lx = float_to_byte(inputData.right_stick[0])
                    self.last_ly = float_to_byte(-inputData.right_stick[1])
                    self.last_rx = 128
                    self.last_ry = 128
            else:
                self.last_lx = float_to_byte(inputData.left_stick[0])
                self.last_ly = float_to_byte(-inputData.left_stick[1])
                self.last_rx = float_to_byte(inputData.right_stick[0])
                self.last_ry = float_to_byte(-inputData.right_stick[1])

            rx_float = (self.last_rx - 128) / 127.5
            ry_float = -((self.last_ry - 128) / 127.5)
            rx_float, ry_float = self._add_gyro_rstick_overlay(rx_float, ry_float, inputData)
            self.last_rx = float_to_byte(rx_float)
            self.last_ry = float_to_byte(-ry_float)
            
            if self.hold_mode == "Horizontal" and not controller.is_pro_controller():
                if controller.is_joycon_right():
                    self.last_gx = inputData.gyroscope[1]
                    self.last_gy = inputData.gyroscope[2]
                    self.last_gz = -inputData.gyroscope[0]
                    self.last_ax = -inputData.accelerometer[1]
                    self.last_ay = inputData.accelerometer[2]
                    self.last_az = inputData.accelerometer[0]
                else:
                    self.last_gx = -inputData.gyroscope[1]
                    self.last_gy = inputData.gyroscope[2]
                    self.last_gz = inputData.gyroscope[0]
                    self.last_ax = -inputData.accelerometer[1]
                    self.last_ay = inputData.accelerometer[2]
                    self.last_az = -inputData.accelerometer[0]
            else:
                self.last_gx = inputData.gyroscope[0]
                self.last_gy = inputData.gyroscope[2]
                self.last_gz = -inputData.gyroscope[1]
                self.last_ax = inputData.accelerometer[0]
                self.last_ay = inputData.accelerometer[2]
                self.last_az = -inputData.accelerometer[1]
        else:
            mixed_left, mixed_right = self._update_merged_stick_mix(inputData, controller)
            mixed_right = self._add_gyro_rstick_overlay(mixed_right[0], mixed_right[1], inputData)
            self.last_lx = float_to_byte(mixed_left[0])
            self.last_ly = float_to_byte(-mixed_left[1])
            self.last_rx = float_to_byte(mixed_right[0])
            self.last_ry = float_to_byte(-mixed_right[1])

            is_passthrough_source = False
            if getattr(CONFIG, "djg_enabled", False):
                dom_side = getattr(CONFIG, "djg_dominant_side", "Left")
                if controller.is_joycon_left() and dom_side == "Left":
                    is_passthrough_source = True
                elif controller.is_joycon_right() and dom_side == "Right":
                    is_passthrough_source = True
            else:
                if controller.is_joycon_left() and self.active_gyro_side == "Left":
                    is_passthrough_source = True
                elif controller.is_joycon_right() and self.active_gyro_side == "Right":
                    is_passthrough_source = True
                    
            if is_passthrough_source:
                self.last_gx = inputData.gyroscope[0]
                self.last_gy = inputData.gyroscope[2]
                self.last_gz = -inputData.gyroscope[1]
                self.last_ax = inputData.accelerometer[0]
                self.last_ay = inputData.accelerometer[2]
                self.last_az = -inputData.accelerometer[1]

        if getattr(CONFIG, "gyro_mode", "World") == "Roll" and controller.gyro_mouse_enabled:
            steer = getattr(controller, '_shared_steer_value', controller._own_steer_value if hasattr(controller, '_own_steer_value') else 0.0)
            self.last_lx = int(max(0, min(255, round(steer * 127.5 + 128))))

        report.LeftStickX = self.last_lx
        report.LeftStickY = self.last_ly
        report.RightStickX = self.last_rx
        report.RightStickY = self.last_ry

        # 6. Gyro/Accel raw signed short assignments
        def clamp_short(val): return max(-32768, min(32767, int(val)))
        if mode == "PS5":
            if getattr(self, 'driver_type', '') == "WinUHid":
                report.GyroX = clamp_short(self.last_gx)
                report.GyroY = clamp_short(self.last_gy)
                report.GyroZ = clamp_short(self.last_gz)
                report.AccelX = clamp_short(self.last_ax)
                report.AccelY = clamp_short(self.last_ay)
                report.AccelZ = clamp_short(self.last_az)
            else:
                report.AngularVelocityX = clamp_short(self.last_gx)   # Pitch <- gyroscope[0]
                report.AngularVelocityY = clamp_short(self.last_gz)   # Yaw   <- -gyroscope[1] (was Roll, swap with gz)
                report.AngularVelocityZ = clamp_short(self.last_gy)   # Roll  <- gyroscope[2]  (was Yaw, swap with gy)
                report.AccelerometerX = clamp_short(self.last_ax)
                report.AccelerometerY = clamp_short(self.last_ay)
                report.AccelerometerZ = clamp_short(self.last_az)
            # SensorTimestamp: DualSense reports in ~0.33us ticks (3MHz clock).
            # EA and strict DualSense games validate this increments monotonically.
            # At 250Hz USB polling, each frame = 4000us = ~12000 ticks.
            now_us = int(time.perf_counter() * 1_000_000) & 0xFFFFFFFF
            report.SensorTimestamp = (now_us * 3) & 0xFFFFFFFF  # Convert us -> 3MHz ticks
            # UNK_COUNTER: IMU packet sequence counter, increments each frame.
            report.UNK_COUNTER = (getattr(report, 'UNK_COUNTER', 0) + 1) & 0xFFFFFFFF
        else:
            report.GyroX = clamp_short(self.last_gx)
            report.GyroY = clamp_short(self.last_gy)
            report.GyroZ = clamp_short(self.last_gz)
            report.AccelX = clamp_short(self.last_ax)
            report.AccelY = clamp_short(self.last_ay)
            report.AccelZ = clamp_short(self.last_az)

    def _set_touch_state(self, report, touch_index, touch_down, touch_x, touch_y, mode, is_new_touch=False):
        if mode == "PS4":
            tp = report.TouchReports[0].TouchPoints[touch_index]
            if touch_down:
                if is_new_touch:
                    tp.ContactSeq = (tp.ContactSeq + 1) & 0x7F
                else:
                    tp.ContactSeq = tp.ContactSeq & 0x7F
            else:
                tp.ContactSeq = tp.ContactSeq | 0x80
                
            tp.XLowPart = touch_x & 0xFF
            tp.XHighPart = (touch_x >> 8) & 0xF
            tp.YLowPart = touch_y & 0xF
            tp.YHighPart = (touch_y >> 4) & 0xFF
            report.TouchReportCount = 1
            report.TouchReports[0].Timestamp = (report.TouchReports[0].Timestamp + 1) & 0xFF
        else:  # PS5
            tp = report.TouchReport.TouchPoints[touch_index]
            if touch_down:
                if is_new_touch:
                    tp.ContactSeq = (tp.ContactSeq + 1) & 0x7F
                else:
                    tp.ContactSeq = tp.ContactSeq & 0x7F
            else:
                tp.ContactSeq = tp.ContactSeq | 0x80
                
            tp.XLowPart = touch_x & 0xFF
            tp.XHighPart = (touch_x >> 8) & 0xF
            tp.YLowPart = touch_y & 0xF
            tp.YHighPart = (touch_y >> 4) & 0xFF
            report.TouchReport.Timestamp = (report.TouchReport.Timestamp + 1) & 0xFF

    def update_as_xbox(self, inputData: ControllerInputData, buttons: int, controller: Controller, buttonsConfig: ButtonConfig):
        with self.state_lock:
            if self.vg_controller is None:
                return
            # Phase 1: Button Mapping (Respects GUI layout setting)
            xb_btns = 0
            
            if CONFIG.abxy_mode == "Xbox":
                # When UI says "Xbox", we want "Switch layout" (positional match)
                if buttons & SWITCH_BUTTONS["Y"]: xb_btns |= XB_BUTTONS["X"]
                if buttons & SWITCH_BUTTONS["X"]: xb_btns |= XB_BUTTONS["Y"]
                if buttons & SWITCH_BUTTONS["B"]: xb_btns |= XB_BUTTONS["A"]
                if buttons & SWITCH_BUTTONS["A"]: xb_btns |= XB_BUTTONS["B"]
            else: # Switch layout in UI
                # When UI says "Switch", we want "Xbox layout" (name match)
                if buttons & SWITCH_BUTTONS["Y"]: xb_btns |= XB_BUTTONS["X"]
                if buttons & SWITCH_BUTTONS["X"]: xb_btns |= XB_BUTTONS["Y"]
                if buttons & SWITCH_BUTTONS["B"]: xb_btns |= XB_BUTTONS["A"]
                if buttons & SWITCH_BUTTONS["A"]: xb_btns |= XB_BUTTONS["B"]
                    
            if buttons & SWITCH_BUTTONS["L"]: xb_btns |= XB_BUTTONS["LB"]
            if buttons & SWITCH_BUTTONS["R"]: xb_btns |= XB_BUTTONS["RB"]
            
            if getattr(controller.controller_info, 'product_id', 0) == NSO_GAMECUBE_CONTROLLER_PID:
                lt = inputData.left_trigger
                rt = inputData.right_trigger
            else:
                lt = 255 if (buttons & SWITCH_BUTTONS["ZL"]) else 0
                rt = 255 if (buttons & SWITCH_BUTTONS["ZR"]) else 0
            
            if buttons & SWITCH_BUTTONS["MINUS"]: xb_btns |= XB_BUTTONS["BACK"]
            if buttons & SWITCH_BUTTONS["PLUS"]: xb_btns |= XB_BUTTONS["START"]
            if buttons & SWITCH_BUTTONS["L_STK"]: xb_btns |= XB_BUTTONS["L_STK"]
            if buttons & SWITCH_BUTTONS["R_STK"]: xb_btns |= XB_BUTTONS["R_STK"]
            
            if buttons & SWITCH_BUTTONS["UP"]: xb_btns |= XB_BUTTONS["UP"]
            if buttons & SWITCH_BUTTONS["DOWN"]: xb_btns |= XB_BUTTONS["DOWN"]
            if buttons & SWITCH_BUTTONS["LEFT"]: xb_btns |= XB_BUTTONS["LEFT"]
            if buttons & SWITCH_BUTTONS["RIGHT"]: xb_btns |= XB_BUTTONS["RIGHT"]
            
            if buttons & SWITCH_BUTTONS.get("HOME", 0): xb_btns |= XB_BUTTONS["GUIDE"]
            if buttons & SWITCH_BUTTONS.get("CAPT", 0): xb_btns |= XB_BUTTONS["BACK"]
 
            # Phase 2: Stick Routing (Mirrored from PS4 logic)
            if not hasattr(self, 'last_xb_lx'):
                self.last_xb_lx = 0.0; self.last_xb_ly = 0.0
                self.last_xb_rx = 0.0; self.last_xb_ry = 0.0
 
            custom_stick_route = getattr(inputData, 'custom_joystick_mapping', None)
            if len(self.controllers) == 1:
                if not controller.is_joycon() and (
                    self._joystick_mapping_mode("l_joystick", controller) in ("L Joystick", "R Joystick") or
                    self._joystick_mapping_mode("r_joystick", controller) in ("L Joystick", "R Joystick")
                ):
                    mixed_left, mixed_right = self._update_merged_stick_mix(inputData, controller)
                    self.last_xb_lx = mixed_left[0]
                    self.last_xb_ly = -mixed_left[1]
                    self.last_xb_rx = mixed_right[0]
                    self.last_xb_ry = -mixed_right[1]
                elif custom_stick_route:
                    self.last_xb_lx = inputData.left_stick[0]
                    self.last_xb_ly = -inputData.left_stick[1]
                    self.last_xb_rx = inputData.right_stick[0]
                    self.last_xb_ry = -inputData.right_stick[1]
                elif controller.is_joycon_right():
                    if self.hold_mode == "Vertical":
                        self.last_xb_rx = inputData.right_stick[0]
                        self.last_xb_ry = -inputData.right_stick[1]
                        self.last_xb_lx = 0.0; self.last_xb_ly = 0.0
                    else:
                        self.last_xb_lx = inputData.right_stick[0]
                        self.last_xb_ly = -inputData.right_stick[1]
                        self.last_xb_rx = 0.0
                        self.last_xb_ry = 0.0
                else:
                    self.last_xb_lx = inputData.left_stick[0]
                    self.last_xb_ly = -inputData.left_stick[1]
                    self.last_xb_rx = inputData.right_stick[0]
                    self.last_xb_ry = -inputData.right_stick[1]
            else:
                mixed_left, mixed_right = self._update_merged_stick_mix(inputData, controller)
                self.last_xb_lx = mixed_left[0]
                self.last_xb_ly = -mixed_left[1]
                self.last_xb_rx = mixed_right[0]
                self.last_xb_ry = -mixed_right[1]

            rx_float, ry_float = self._add_gyro_rstick_overlay(self.last_xb_rx, -self.last_xb_ry, inputData)
            self.last_xb_rx = rx_float
            self.last_xb_ry = -ry_float

            if getattr(CONFIG, "gyro_mode", "World") == "Roll" and controller.gyro_mouse_enabled:
                self.last_xb_lx = getattr(controller, '_shared_steer_value', controller._own_steer_value if hasattr(controller, '_own_steer_value') else 0.0)

            # Phase 3: Final Reporting
            if self.driver_type == "ViGEmBus":
                self.vg_controller.report.wButtons = xb_btns
                self.vg_controller.left_trigger(lt)
                self.vg_controller.right_trigger(rt)
                self.vg_controller.left_joystick_float(self.last_xb_lx, -self.last_xb_ly)
                self.vg_controller.right_joystick_float(self.last_xb_rx, -self.last_xb_ry)
                self.vg_controller.update()
            else:
                self.vg_controller.set_buttons(xb_btns)
                self.vg_controller.left_trigger(lt)
                self.vg_controller.right_trigger(rt)
                self.vg_controller.left_joystick_float(self.last_xb_lx, self.last_xb_ly)
                self.vg_controller.right_joystick_float(self.last_xb_rx, self.last_xb_ry)
                self.vg_controller.update()

    def is_single(self): 
        return len(self.controllers) == 1
    
    def is_single_joycon_right(self):
        return self.is_single() and len(self.controllers) > 0 and self.controllers[0].is_joycon_right()

    def is_single_joycon_left(self):
        return self.is_single() and len(self.controllers) > 0 and self.controllers[0].is_joycon_left()
        
    async def update_leds(self):
        for c in self.controllers: await c.set_leds(self.player_number)
        
    def add_controller(self, c): 
        self.controllers.append(c)
    
    def start_calibration(self):
        for c in self.controllers:
            if hasattr(c, 'start_calibration'):
                c.start_calibration()

    def start_mag_calibration(self):
        for c in self.controllers:
            if hasattr(c, 'start_mag_calibration'):
                c.start_mag_calibration()

    def stop_mag_calibration(self):
        for c in self.controllers:
            if hasattr(c, 'stop_mag_calibration'):
                c.stop_mag_calibration()

    def _1000hz_loop(self):
        import time
        last_time = time.perf_counter()
        while self.running:
            driver_type = getattr(self, 'driver_type', None)
            mode = getattr(self, 'mode', None)
            
            if driver_type != "ViGEmBus" or mode != "PS4":
                time.sleep(0.015)
                last_time = time.perf_counter()
                continue

            now = time.perf_counter()
            dt = now - last_time
            if dt < 0.001:
                time.sleep(0)
                continue
                
            last_time = now
            if dt > 0.05: dt = 0.015
            
            with self.state_lock:
                if not hasattr(self, 'vg_controller') or self.vg_controller is None:
                    continue
                
                driver_type = self.driver_type
                if driver_type == "ViGEmBus" and self.mode == "PS4":
                    ticks = int(dt * 187500)
                    self.ds4_timestamp = (getattr(self, 'ds4_timestamp', 0) + ticks) & 0xFFFF
                    
                    self.report_ex.Report.wTimestamp = self.ds4_timestamp
                    self.report_ex.Report.bTouchPacketsN = 1
                    self.touch_packet_counter = (getattr(self, 'touch_packet_counter', 0) + 1) & 0xFF
                    self.report_ex.Report.sCurrentTouch.bPacketCounter = self.touch_packet_counter
            
                    try:
                        import vgamepad.win.vigem_client as vcli
                        vcli.vigem_target_ds4_update_ex_ptr.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(DS4_REPORT_EX)]
                        busp = self.vg_controller.vbus.get_busp()
                        devicep = self.vg_controller._devicep
                        vcli.vigem_target_ds4_update_ex_ptr(busp, devicep, ctypes.byref(self.report_ex))
                    except Exception as e:
                        logger.error(f"Failed to update DS4 ex via ViGEmBus: {e}")
                        self.vg_controller.update()
        
        logger.info(f"Player {self.player_number}: Update loop thread finished.")
                
    def reset_inputs(self):
        """Reset all virtual inputs to neutral/released state."""
        with self.state_lock:
            if self.vg_controller is not None:
                driver_type = self.driver_type
                if self.mode == "Switch2":
                    self.last_s2_lx = 0.0; self.last_s2_ly = 0.0
                    self.last_s2_rx = 0.0; self.last_s2_ry = 0.0
                    self.last_s2_gx = 0; self.last_s2_gy = 0; self.last_s2_gz = 0
                    self.last_s2_ax = 0; self.last_s2_ay = 0; self.last_s2_az = 0

                    state = bytearray(64)
                    state[0] = 0x05
                    state[2] = 0x12

                    state[11] = 2048 & 0xff
                    state[12] = ((2048 >> 8) & 0x0f) | ((2048 & 0x0f) << 4)
                    state[13] = (2048 >> 4) & 0xff
                    state[14] = 2048 & 0xff
                    state[15] = ((2048 >> 8) & 0x0f) | ((2048 & 0x0f) << 4)
                    state[16] = (2048 >> 4) & 0xff

                    state[42] = 0x01
                    if hasattr(self, 'usbip_server') and self.usbip_server:
                        self.usbip_server.update_state(state)
                elif self.mode == "Switch1":
                    self.last_s2_lx = 0.0; self.last_s2_ly = 0.0; self.last_s2_rx = 0.0; self.last_s2_ry = 0.0
                    self.last_s2_gx = 0; self.last_s2_gy = 0; self.last_s2_gz = 0
                    self.last_s2_ax = 0; self.last_s2_ay = 0; self.last_s2_az = 0
                    
                    state_l = bytearray(50)
                    state_l[0] = 0x30
                    state_l[2] = 0x9E
                    state_l[6] = 0x00
                    state_l[7] = 0x08
                    state_l[8] = 0x80
                    state_l[9] = 0x00
                    state_l[10] = 0x08
                    state_l[11] = 0x80
                    
                    state_r = bytearray(50)
                    state_r[0] = 0x30
                    state_r[2] = 0x8E
                    state_r[6] = 0x00
                    state_r[7] = 0x08
                    state_r[8] = 0x80
                    state_r[9] = 0x00
                    state_r[10] = 0x08
                    state_r[11] = 0x80
                    
                    state_pro = bytearray(50)
                    state_pro[0] = 0x30
                    state_pro[2] = 0x8E
                    state_pro[6] = 0x00
                    state_pro[7] = 0x08
                    state_pro[8] = 0x80
                    state_pro[9] = 0x00
                    state_pro[10] = 0x08
                    state_pro[11] = 0x80
                    
                    if hasattr(self, 'usbip_server_l') and self.usbip_server_l:
                        self.usbip_server_l.update_state(state_l)
                    if hasattr(self, 'usbip_server_r') and self.usbip_server_r:
                        self.usbip_server_r.update_state(state_r)
                    if hasattr(self, 'usbip_server_pro') and self.usbip_server_pro:
                        self.usbip_server_pro.update_state(state_pro)
                else:  # ViGEmBus
                    if self.mode == "Xbox360":
                        self.vg_controller.reset()
                    else:  # PS4
                        self.report_ex = DS4_REPORT_EX()
                        self.report_ex.Report.bThumbLX = 128
                        self.report_ex.Report.bThumbLY = 128
                        self.report_ex.Report.bThumbRX = 128
                        self.report_ex.Report.bThumbRY = 128
                        self.report_ex.Report.bBatteryLvl = 0xAF
                        self.report_ex.Report.bBatteryLvlSpecial = 0x08

            logger.info(f"Player {self.player_number}: Virtual inputs reset to neutral.")
            self.previous_buttons_left = 0x00000000
            self.previous_buttons_right = 0x00000000
            self.was_touching = False
            self.was_touching_0 = False
            self.was_touching_1 = False
            self.touch_start_time = 0.0

    def force_close(self):
        """Synchronously and forcefully close the virtual device handle."""
        self.running = False
        
        # 1. Wait for the high-frequency update thread to terminate
        if hasattr(self, 'update_thread') and self.update_thread.is_alive():
            logger.info(f"Player {self.player_number}: Waiting for update thread to exit...")
            self.update_thread.join(timeout=0.5)
            
        # 2. Use the lock to ensure no other thread (like BLE callback) is using the gamepad
        with self.state_lock:
            if hasattr(self, 'vg_controller') and self.vg_controller is not None:
                logger.info(f"Player {self.player_number}: Forcefully destroying virtual device handle.")
                self.cleanup_vg_controller()
                
        server_port = self.server_port
        detach_usbip_device(server_port)
        if hasattr(self, 'usbip_server') and self.usbip_server:
            try:
                self.usbip_server.stop()
            except Exception:
                pass
            self.usbip_server = None
        
        # 3. Force garbage collection to ensure driver resources are released NOW
        gc.collect()

    async def disconnect(self, timeout=3.0, is_suspending=False):
        async with self._disconnect_lock:
            if not getattr(self, 'running', False) and self.vg_controller is None and not self.controllers:
                return
                
            self.running = False
            import time
            current_time = time.strftime("%H:%M:%S")
            logger.info(f"[{current_time}] Player {self.player_number}: Starting disconnect sequence (is_suspending={is_suspending})...")
            
            # Wait for the update thread to finish before proceeding with handle cleanup
            if hasattr(self, 'update_thread') and self.update_thread.is_alive():
                logger.info(f"Player {self.player_number}: Waiting for update thread to exit...")
                # Increase timeout to ensure thread actually finishes before handle is cleared
                self.update_thread.join(timeout=0.5)
                if self.update_thread.is_alive():
                    logger.warning(f"Player {self.player_number}: Update thread did not exit in time!")
            
            if not self.controllers and self.vg_controller is None:
                return
 
            logger.info(f"Player {self.player_number}: Cleaning up virtual device and physical connections...")
            
            with self.state_lock:
                self.cleanup_vg_controller()
                    
            server_port = self.server_port
            detach_usbip_device(server_port)
            if hasattr(self, 'usbip_server') and self.usbip_server:
                try:
                    self.usbip_server.stop()
                except Exception:
                    pass
                self.usbip_server = None
            
            # Explicitly trigger GC to help release driver handles
            gc.collect()
                
            disconnect_tasks = []
            for c in list(self.controllers):
                if hasattr(c, 'client') and c.client and c.client.is_connected:
                    logger.info(f"Player {self.player_number}: Disconnecting Bluetooth for {c.device.address}")
                    disconnect_tasks.append(asyncio.create_task(c.disconnect()))
                    
            if disconnect_tasks:
                try:
                    # Await the actual disconnection tasks
                    await asyncio.wait_for(asyncio.gather(*disconnect_tasks), timeout=timeout)
                except asyncio.TimeoutError:
                    logger.warning(f"Player {self.player_number}: Bluetooth disconnection timed out")
                except Exception as e:
                    logger.error(f"Player {self.player_number}: Error during Bluetooth disconnection: {e}")
                
            for c in list(self.controllers):
                if self.on_disconnected_callback:
                    try:
                        await self.on_disconnected_callback(c)
                    except Exception:
                        pass
                    
            self.controllers.clear()
            logger.info(f"Player {self.player_number}: Cleanup complete.")

    def trigger_disconnect(self):
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.disconnect(), self.loop)
        else:
            logger.error("Event loop not found or not running.")

    async def remove_controller(self, controller: Controller, clear_mac_port: bool = False) -> bool:
        if controller not in self.controllers:
            return False
            
        self.controllers.remove(controller)
        if clear_mac_port:
                # Only clear the MAC->port mapping when the physical controller truly disconnects
                # (BLE disconnect), NOT during merge/split operations where it stays connected.
                mac = controller.device.address
                global MAC_TO_PORT
                if mac in MAC_TO_PORT:
                    MAC_TO_PORT.pop(mac, None)
            
        if len(self.controllers) == 0:
            # Mark as not running immediately so any pending setup_virtual_device call
            # (e.g. from a split that was followed immediately by a merge) sees the flag
            # and skips creating a zombie USBIP server.
            self.running = False
            with self.state_lock:
                self.cleanup_vg_controller()
                
            if self.mode == "Switch2":
                server_port = self.server_port
                detach_usbip_device(server_port)
                if hasattr(self, 'usbip_server') and self.usbip_server:
                    try:
                        self.usbip_server.stop()
                    except Exception:
                        pass
                    self.usbip_server = None
                
            return True 
        else:
            if self.mode == "Switch1":
                if controller.is_joycon_left() and getattr(self, 'usbip_server_l', None) is not None:
                    if hasattr(self, 'server_port_l') and self.server_port_l:
                        try:
                            detach_usbip_device(self.server_port_l)
                        except Exception:
                            pass
                    try:
                        self.usbip_server_l.stop()
                    except Exception:
                        pass
                    self.usbip_server_l = None
                elif controller.is_joycon_right() and getattr(self, 'usbip_server_r', None) is not None:
                    if hasattr(self, 'server_port_r') and self.server_port_r:
                        try:
                            detach_usbip_device(self.server_port_r)
                        except Exception:
                            pass
                    try:
                        self.usbip_server_r.stop()
                    except Exception:
                        pass
                    self.usbip_server_r = None
                elif controller.is_pro_controller() and getattr(self, 'usbip_server_pro', None) is not None:
                    if hasattr(self, 'server_port_pro') and self.server_port_pro:
                        try:
                            detach_usbip_device(self.server_port_pro)
                        except Exception:
                            pass
                    try:
                        self.usbip_server_pro.stop()
                    except Exception:
                        pass
                    self.usbip_server_pro = None

            if getattr(self, 'running', True):
                try:
                    await self.init_added_controller(self.controllers[0])
                except Exception as e:
                    logger.error(f"Failed to re-init remaining controller after split/remove: {e}")
            return False

    def _dualsense_rumble_callback(self, out_data, side="Pro"):
        delay = getattr(CONFIG, "rumble_delay_ms", 0)
        if delay > 0:
            import threading
            threading.Timer(delay / 1000.0, self._dualsense_rumble_callback_internal, args=(out_data, side)).start()
        else:
            self._dualsense_rumble_callback_internal(out_data, side)

    def _dualsense_rumble_callback_internal(self, out_data, side="Pro"):
        if len(out_data) < 2:
            return

        if out_data[0] == 0x01:
            # Basic Rumble Report (ID=0x01)
            # Format: [0x01, RightMotor, LeftMotor]
            if len(out_data) >= 3:
                right_motor_weak = out_data[1]
                left_motor_strong = out_data[2]
                
                lf_amp = int((left_motor_strong / 255.0) * 1000)
                hf_amp = int((right_motor_weak / 255.0) * 1000)

                dt = time.perf_counter() - self.cycle_start_time
                slot_size = RUMBLE_WRITE_INTERVAL / 3.0
                slot = int(dt / slot_size) if slot_size > 0 else 0
                if slot < 0: slot = 0
                elif slot > 2: slot = 2
                
                import math
                with self.vibration_lock:
                    # Apply to Left (Xbox Rumble limit is 1000, so we use 700 + 300*tanh)
                    raw_lf_l = self.frame_vibrations_l[slot].lf_amp + lf_amp
                    raw_hf_l = self.frame_vibrations_l[slot].hf_amp + hf_amp
                    if raw_lf_l <= 700: self.frame_vibrations_l[slot].lf_amp = int(raw_lf_l)
                    else: self.frame_vibrations_l[slot].lf_amp = int(700 + 300 * math.tanh((raw_lf_l - 700) / 300))
                    if raw_hf_l <= 700: self.frame_vibrations_l[slot].hf_amp = int(raw_hf_l)
                    else: self.frame_vibrations_l[slot].hf_amp = int(700 + 300 * math.tanh((raw_hf_l - 700) / 300))
                    
                    # Apply to Right
                    raw_lf_r = self.frame_vibrations_r[slot].lf_amp + lf_amp
                    raw_hf_r = self.frame_vibrations_r[slot].hf_amp + hf_amp
                    if raw_lf_r <= 700: self.frame_vibrations_r[slot].lf_amp = int(raw_lf_r)
                    else: self.frame_vibrations_r[slot].lf_amp = int(700 + 300 * math.tanh((raw_lf_r - 700) / 300))
                    if raw_hf_r <= 700: self.frame_vibrations_r[slot].hf_amp = int(raw_hf_r)
                    else: self.frame_vibrations_r[slot].hf_amp = int(700 + 300 * math.tanh((raw_hf_r - 700) / 300))

        elif out_data[0] == 0x11:
            # Custom High-Fidelity Rumble Report (ID=0x11)
            # Format: [0x11, RightIntensity, LeftIntensity]
            if len(out_data) >= 3:
                right_intensity = out_data[1] # Small motor (HF)
                left_intensity = out_data[2]  # Large motor (LF)
                
                lf_amp = int(800 * left_intensity / 256)
                hf_amp = int(800 * right_intensity / 256)

                dt = time.perf_counter() - self.cycle_start_time
                slot_size = RUMBLE_WRITE_INTERVAL / 3.0
                slot = int(dt / slot_size) if slot_size > 0 else 0
                if slot < 0: slot = 0
                elif slot > 2: slot = 2
                
                import math
                with self.vibration_lock:
                    # WinUHid perfectly aligned frequencies
                    lf_freq = 0x0e1
                    hf_freq = 0x1e1
                        
                    # Apply to Left
                    raw_lf_l = self.frame_vibrations_l[slot].lf_amp + lf_amp
                    raw_hf_l = self.frame_vibrations_l[slot].hf_amp + hf_amp
                    if raw_lf_l <= 560: self.frame_vibrations_l[slot].lf_amp = int(raw_lf_l)
                    else: self.frame_vibrations_l[slot].lf_amp = int(560 + 240 * math.tanh((raw_lf_l - 560) / 240))
                    if raw_hf_l <= 560: self.frame_vibrations_l[slot].hf_amp = int(raw_hf_l)
                    else: self.frame_vibrations_l[slot].hf_amp = int(560 + 240 * math.tanh((raw_hf_l - 560) / 240))
                    self.frame_vibrations_l[slot].lf_freq = lf_freq
                    self.frame_vibrations_l[slot].hf_freq = hf_freq
                    
                    # Apply to Right
                    raw_lf_r = self.frame_vibrations_r[slot].lf_amp + lf_amp
                    raw_hf_r = self.frame_vibrations_r[slot].hf_amp + hf_amp
                    if raw_lf_r <= 560: self.frame_vibrations_r[slot].lf_amp = int(raw_lf_r)
                    else: self.frame_vibrations_r[slot].lf_amp = int(560 + 240 * math.tanh((raw_lf_r - 560) / 240))
                    if raw_hf_r <= 560: self.frame_vibrations_r[slot].hf_amp = int(raw_hf_r)
                    else: self.frame_vibrations_r[slot].hf_amp = int(560 + 240 * math.tanh((raw_hf_r - 560) / 240))
                    self.frame_vibrations_r[slot].lf_freq = lf_freq
                    self.frame_vibrations_r[slot].hf_freq = hf_freq

                    self.latest_vibration_l.lf_amp = lf_amp
                    self.latest_vibration_l.hf_amp = hf_amp
                    self.latest_vibration_l.lf_freq = lf_freq
                    self.latest_vibration_l.hf_freq = hf_freq
                    
                    self.latest_vibration_r.lf_amp = lf_amp
                    self.latest_vibration_r.hf_amp = hf_amp
                    self.latest_vibration_r.lf_freq = lf_freq
                    self.latest_vibration_r.hf_freq = hf_freq
                    
                    self.last_rumble_active_time = time.perf_counter()
                    self.vibration_dirty_l = True
                    self.vibration_dirty_r = True
            return
                
        elif out_data[0] == 0x02:
            # Standard USB Rumble Report (Extended)
            # Switch 2 processes vibration by slicing byte arrays directly instead of ctypes.
            # Using direct indexing is much faster and prevents dropping packets in the high-frequency Audio Haptic / Rumble loop.
            if len(out_data) >= 5:
                # If audio haptics are active, ignore standard rumble to prevent conflict!
                import time
                if hasattr(self, 'haptic_processor') and time.time() - self.haptic_processor.last_update_time < 0.2:
                    self.last_rumble_received_time = time.perf_counter()
                    return

                # Byte 3 is Right Motor (Weak), Byte 4 is Left Motor (Strong)
                right_motor_weak = out_data[3]
                left_motor_strong = out_data[4]
                
                lf_amp = int(800 * left_motor_strong / 256)
                hf_amp = int(800 * right_motor_weak / 256)
                
                # Adaptive Trigger Translation (Mode 0x26 Vibration and 0x02 Weapon Recoil)
                trigger_r_amp = 0
                trigger_r_freq = 0x0e1
                trigger_l_amp = 0
                trigger_l_freq = 0x0e1
                
                if len(out_data) >= 33:
                    import time
                    now = time.perf_counter()
                    
                    # Right Trigger FFB
                    rt_mode = out_data[11]
                    rt_payload = bytes(out_data[11:22])
                    
                    is_joycon = any(c.is_joycon() for c in getattr(self, 'controllers', []))
                    trigger_amp_max = int(800 * 0.5) if is_joycon else 800
                    
                    # Log for debugging
                    if rt_mode != 0:
                        if now - getattr(self, 'last_rt_log_time', 0) > 0.5 or rt_mode != getattr(self, 'last_rt_mode', 0):
                            logger.info(f"RT Adaptive FFB: Mode={hex(rt_mode)} Data={rt_payload[1:].hex()}")
                            self.last_rt_log_time = now
                            self.last_rt_mode = rt_mode
                            
                    # Trigger 150ms punch on payload change if physical trigger is currently held down
                    if rt_payload != getattr(self, 'trigger_r_prev_payload', b''):
                        self.trigger_r_prev_payload = rt_payload
                        if getattr(self, 'prev_zr_pressed', False) and rt_mode not in (0x00, 0x05):
                            self.trigger_r_punch_end = now + 0.150
                            
                    if now < getattr(self, 'trigger_r_punch_end', 0):
                        trigger_r_amp = trigger_amp_max
                        trigger_r_freq = 0x0e1

                    # Left Trigger FFB
                    lt_mode = out_data[22]
                    lt_payload = bytes(out_data[22:33])
                    
                    # Log for debugging
                    if lt_mode != 0:
                        if now - getattr(self, 'last_lt_log_time', 0) > 0.5 or lt_mode != getattr(self, 'last_lt_mode', 0):
                            logger.info(f"LT Adaptive FFB: Mode={hex(lt_mode)} Data={lt_payload[1:].hex()}")
                            self.last_lt_log_time = now
                            self.last_lt_mode = lt_mode
                            
                    # Trigger 150ms punch on payload change if physical trigger is currently held down
                    if lt_payload != getattr(self, 'trigger_l_prev_payload', b''):
                        self.trigger_l_prev_payload = lt_payload
                        if getattr(self, 'prev_zl_pressed', False) and lt_mode not in (0x00, 0x05):
                            self.trigger_l_punch_end = now + 0.150
                    if now < getattr(self, 'trigger_l_punch_end', 0):
                        trigger_l_amp = trigger_amp_max
                        trigger_l_freq = 0x0e1

                dt = time.perf_counter() - self.cycle_start_time
                slot_size = RUMBLE_WRITE_INTERVAL / 3.0
                slot = int(dt / slot_size) if slot_size > 0 else 0
                if slot < 0: slot = 0
                elif slot > 2: slot = 2
                
                import math
                with self.vibration_lock:
                    # WinUHid perfectly aligned frequencies
                    lf_freq = 0x0e1
                    hf_freq = 0x1e1
                        
                    # Apply to Left (mix standard rumble with left trigger rumble)
                    raw_lf_l = self.frame_vibrations_l[slot].lf_amp + lf_amp + trigger_l_amp
                    raw_hf_l = self.frame_vibrations_l[slot].hf_amp + hf_amp + trigger_l_amp
                    if raw_lf_l <= 560: self.frame_vibrations_l[slot].lf_amp = int(raw_lf_l)
                    else: self.frame_vibrations_l[slot].lf_amp = int(560 + 240 * math.tanh((raw_lf_l - 560) / 240))
                    if raw_hf_l <= 560: self.frame_vibrations_l[slot].hf_amp = int(raw_hf_l)
                    else: self.frame_vibrations_l[slot].hf_amp = int(560 + 240 * math.tanh((raw_hf_l - 560) / 240))
                    self.frame_vibrations_l[slot].lf_freq = trigger_l_freq if trigger_l_amp > 0 else lf_freq
                    self.frame_vibrations_l[slot].hf_freq = trigger_l_freq if trigger_l_amp > 0 else hf_freq
                    
                    # Apply to Right (mix standard rumble with right trigger rumble)
                    raw_lf_r = self.frame_vibrations_r[slot].lf_amp + lf_amp + trigger_r_amp
                    raw_hf_r = self.frame_vibrations_r[slot].hf_amp + hf_amp + trigger_r_amp
                    if raw_lf_r <= 560: self.frame_vibrations_r[slot].lf_amp = int(raw_lf_r)
                    else: self.frame_vibrations_r[slot].lf_amp = int(560 + 240 * math.tanh((raw_lf_r - 560) / 240))
                    if raw_hf_r <= 560: self.frame_vibrations_r[slot].hf_amp = int(raw_hf_r)
                    else: self.frame_vibrations_r[slot].hf_amp = int(560 + 240 * math.tanh((raw_hf_r - 560) / 240))
                    self.frame_vibrations_r[slot].lf_freq = trigger_r_freq if trigger_r_amp > 0 else lf_freq
                    self.frame_vibrations_r[slot].hf_freq = trigger_r_freq if trigger_r_amp > 0 else hf_freq

                    self.latest_vibration_l.lf_amp = min(1000, lf_amp + trigger_l_amp)
                    self.latest_vibration_l.hf_amp = min(1000, hf_amp + trigger_l_amp)
                    self.latest_vibration_l.lf_freq = trigger_l_freq if trigger_l_amp > 0 else lf_freq
                    self.latest_vibration_l.hf_freq = trigger_l_freq if trigger_l_amp > 0 else hf_freq
                    
                    self.latest_vibration_r.lf_amp = min(1000, lf_amp + trigger_r_amp)
                    self.latest_vibration_r.hf_amp = min(1000, hf_amp + trigger_r_amp)
                    self.latest_vibration_r.lf_freq = trigger_r_freq if trigger_r_amp > 0 else lf_freq
                    self.latest_vibration_r.hf_freq = trigger_r_freq if trigger_r_amp > 0 else hf_freq
                    
                    self.last_rumble_active_time = time.perf_counter()
                    self.vibration_dirty_l = True
                    self.vibration_dirty_r = True
                    
                # We can skip the LED/Audio flags check here to save time,
                # as they are processed synchronously in the main _process_output_report thread
                # with rate limiting. This callback focuses purely on minimizing latency for vibration packets.
                
        self.last_rumble_received_time = time.perf_counter()
        return

    def _wasapi_loopback_thread(self):
        # Disabled: We now perfectly capture the raw 4-channel audio stream from the USB Isochronous OUT endpoint directly.
        # Running WASAPI concurrently causes double-processing and audio sample interleaving which ruins haptics.
        return
        
        try:
            import soundcard as sc
            import numpy as np
        except ImportError:
            logger.error("soundcard or numpy not installed, WASAPI loopback disabled")
            return
            
        try:
            logger.info("Starting WASAPI loopback for DualSense Audio...")
            time.sleep(2)
            
            logger.info("Waiting for DualSense Audio to be activated by Windows/Game...")
            
            while getattr(self, 'usbip_server', None) is not None:
                audio_active = getattr(self.usbip_server, 'audio_active', False)
                if not audio_active:
                    time.sleep(0.5)
                    continue
                    
                try:
                    microphones = sc.all_microphones(include_loopback=True)
                    mic_names = [m.name for m in microphones]
                    
                    speaker = None
                    for m in microphones:
                        # We ONLY want the loopback device, not the physical microphone IN endpoint
                        if "麥克風" in m.name or "MICROPHONE" in m.name.upper():
                            continue
                        # Filter for Speaker devices or loopback (if applicable)
                        if "SPEAKER" in m.name.upper() or "喇叭" in m.name or "揚聲器" in m.name or "USB" in m.name.upper() or "DUALSENSE" in m.name.upper() or "WIRELESS" in m.name.upper() or "音訊" in m.name:
                            try:
                                with m.recorder(samplerate=48000) as test_mic:
                                    speaker = m
                                    break
                            except Exception as e:
                                if "DUALSENSE" in m.name.upper():
                                    logger.error(f"WASAPI: Found {m.name} but failed to open: {e}")
                                continue
                                
                    if speaker is None:
                        logger.warning(f"WASAPI: Could not find any suitable USB speaker among {mic_names}")
                        time.sleep(2)
                        continue

                    with speaker.recorder(samplerate=48000) as mic:
                        logger.info(f"WASAPI: Successfully bound to speaker: {speaker.name} ({speaker.channels} channels, 48000 Hz), starting capture...")
                        if speaker.channels < 4:
                            logger.error("WASAPI: CRITICAL WARNING - Speaker is NOT 4 channels! Haptics will not work! Please set it to Quadraphonic in Windows Sound Control Panel!")
                            
                        while getattr(self, 'usbip_server', None) is not None:
                            audio_active = getattr(self.usbip_server, 'audio_active', False)
                            if not audio_active:
                                logger.info("WASAPI: Audio streaming stopped, releasing capture...")
                                if hasattr(self, 'haptic_processor'):
                                    self.haptic_processor.reset()
                                time.sleep(0.1)
                                break
                                
                            data = mic.record(numframes=480) # 10ms at 48000Hz
                            if len(data) > 0:
                                # Ensure data is 4 channels by padding if necessary
                                if data.shape[1] < 4:
                                    pad = np.zeros((data.shape[0], 4 - data.shape[1]), dtype=data.dtype)
                                    data = np.concatenate((data, pad), axis=1)
                                elif data.shape[1] > 4:
                                    data = data[:, :4]
                                    
                                data_int16 = (data * 32767).astype(np.int16)
                                byte_data = data_int16.tobytes()
                                self._usbip_audio_callback(byte_data)
                except Exception as e:
                    logger.error(f"WASAPI Loopback Exception: {e}")
                    time.sleep(1)
        except Exception as e:
            logger.error(f"WASAPI Thread FATAL ERROR: {e}")

    def _usbip_audio_callback(self, data):
        """Handle 4-channel audio stream from DualSense USBIP ep=1 OUT"""
        if len(data) > 0 and hasattr(self, 'haptic_processor'):
            self.haptic_processor.process_audio_packet(data)

    def _haptic_callback(self, left_intensity, right_intensity, mode="CONTINUOUS"):
        def mix_freq(low, high, intensity):
            return int(low + ((high - low) * intensity + 127) // 255)

        # Scale intensity up to 255 for the freq mixer to preserve current freq behavior
        max_intensity_raw = max(left_intensity, right_intensity)
        freq_intensity = min(255, int((max_intensity_raw / 96.0) * 255.0))

        if mode == "TICK":
            hf_freq = mix_freq(0x1c8, 0x1f2, freq_intensity)
            lf_freq = mix_freq(0x0c8, 0x0e8, freq_intensity)
            hf_amp_pct = 100
            lf_amp_pct = 28
        elif mode == "PUNCH":
            hf_freq = mix_freq(0x158, 0x198, freq_intensity)
            lf_freq = mix_freq(0x0a8, 0x0d8, freq_intensity)
            hf_amp_pct = 68
            lf_amp_pct = 100
        elif mode == "TEXTURE":
            hf_freq = mix_freq(0x1a8, 0x1f8, freq_intensity)
            lf_freq = mix_freq(0x0f0, 0x130, freq_intensity)
            hf_amp_pct = 100
            lf_amp_pct = 42
        elif mode == "SILENCE":
            hf_freq = 0
            lf_freq = 0
            hf_amp_pct = 0
            lf_amp_pct = 0
        else: # CONTINUOUS
            hf_freq = mix_freq(0x180, 0x1a0, freq_intensity)
            lf_freq = mix_freq(0x0d0, 0x100, freq_intensity)
            hf_amp_pct = 100
            lf_amp_pct = 100

        # Note: We now properly separate Left and Right into their respective L/R states
        # 整體強度 x2，並套用 WinUHid PS5 Xbox Rumble 高低頻震動強度上限 (800)
        left_base_amp = min(800, int((left_intensity / 96.0) * 800.0 * 2.0))
        right_base_amp = min(800, int((right_intensity / 96.0) * 800.0 * 2.0))
        
        left_lf_amp = int(left_base_amp * (lf_amp_pct / 100.0))
        left_hf_amp = int(left_base_amp * (hf_amp_pct / 100.0))
        
        right_lf_amp = int(right_base_amp * (lf_amp_pct / 100.0))
        right_hf_amp = int(right_base_amp * (hf_amp_pct / 100.0))
        
        is_joycon = any(c.is_joycon() for c in getattr(self, 'controllers', []))
        if is_joycon:
            left_lf_amp = min(left_lf_amp, 480)
            right_lf_amp = min(right_lf_amp, 480)
        
        dt = time.perf_counter() - self.cycle_start_time
        slot_size = RUMBLE_WRITE_INTERVAL / 3.0
        slot = int(dt / slot_size) if slot_size > 0 else 0
        if slot < 0: slot = 0
        elif slot > 2: slot = 2

        import math
        with self.vibration_lock:
            # [Antigravity Fix] Applying slot-based summation for Audio Haptics
            # Apply to Left
            raw_lf_l = self.frame_vibrations_l[slot].lf_amp + left_lf_amp
            raw_hf_l = self.frame_vibrations_l[slot].hf_amp + left_hf_amp
            if raw_lf_l <= 560: self.frame_vibrations_l[slot].lf_amp = int(raw_lf_l)
            else: self.frame_vibrations_l[slot].lf_amp = int(560 + 240 * math.tanh((raw_lf_l - 560) / 240))
            if raw_hf_l <= 560: self.frame_vibrations_l[slot].hf_amp = int(raw_hf_l)
            else: self.frame_vibrations_l[slot].hf_amp = int(560 + 240 * math.tanh((raw_hf_l - 560) / 240))
            self.frame_vibrations_l[slot].lf_freq = lf_freq
            self.frame_vibrations_l[slot].hf_freq = hf_freq
            
            # Apply to Right
            raw_lf_r = self.frame_vibrations_r[slot].lf_amp + right_lf_amp
            raw_hf_r = self.frame_vibrations_r[slot].hf_amp + right_hf_amp
            if raw_lf_r <= 560: self.frame_vibrations_r[slot].lf_amp = int(raw_lf_r)
            else: self.frame_vibrations_r[slot].lf_amp = int(560 + 240 * math.tanh((raw_lf_r - 560) / 240))
            if raw_hf_r <= 560: self.frame_vibrations_r[slot].hf_amp = int(raw_hf_r)
            else: self.frame_vibrations_r[slot].hf_amp = int(560 + 240 * math.tanh((raw_hf_r - 560) / 240))
            self.frame_vibrations_r[slot].lf_freq = lf_freq
            self.frame_vibrations_r[slot].hf_freq = hf_freq
            
            now = time.perf_counter()
            # Only overwrite latest when amplitude is non-zero.  If the haptic
            # callback fires with a silent side (e.g. right=0 while left is
            # active), writing 0 into latest_r would immediately stop the R
            # motor on the next non-dirty read – producing the complementary
            # L↔R alternation stutter.  Holding the last non-zero value lets
            # the motor sustain; a per-side 150ms watchdog in
            # get_current_vibration_frames stops it after true silence.
            if left_lf_amp > 0 or left_hf_amp > 0:
                self.latest_vibration_l.lf_amp = left_lf_amp
                self.latest_vibration_l.hf_amp = left_hf_amp
                self.latest_vibration_l.lf_freq = lf_freq
                self.latest_vibration_l.hf_freq = hf_freq
                self.last_haptic_l_active_time = now

            if right_lf_amp > 0 or right_hf_amp > 0:
                self.latest_vibration_r.lf_amp = right_lf_amp
                self.latest_vibration_r.hf_amp = right_hf_amp
                self.latest_vibration_r.lf_freq = lf_freq
                self.latest_vibration_r.hf_freq = hf_freq
                self.last_haptic_r_active_time = now

            self.vibration_dirty_l = True
            self.vibration_dirty_r = True
            self.last_rumble_received_time = now

    def _usbip_rumble_callback(self, out_data, side="Left"):
        delay = getattr(CONFIG, "rumble_delay_ms", 0)
        if delay > 0:
            import threading
            threading.Timer(delay / 1000.0, self._usbip_rumble_callback_internal, args=(out_data, side)).start()
        else:
            self._usbip_rumble_callback_internal(out_data, side)

    def _usbip_rumble_callback_internal(self, out_data, side="Left"):
        # Disconnect active-push: bytearray(64) from usbip_server means connection dropped
        # In this case out_data[0] == 0x00, so we handle it separately to clear rumble state.
        if len(out_data) < 1:
            return
            
        if self.mode == "Switch1":
            if out_data[0] == 0: # Connection dropped
                with self.vibration_lock:
                    if side == "Left":
                        self.switch_vibrations_left = [VibrationData() for _ in range(3)]
                        self.vibration_dirty_l = True
                    elif side == "Right":
                        self.switch_vibrations_right = [VibrationData() for _ in range(3)]
                        self.vibration_dirty_r = True
                    else: # Pro
                        self.switch_vibrations_left = [VibrationData() for _ in range(3)]
                        self.switch_vibrations_right = [VibrationData() for _ in range(3)]
                        self.vibration_dirty_l = True
                        self.vibration_dirty_r = True
                return
                
            def decode_32bit_rumble(b):
                hf_amp = b[1] & 0xFE
                hf_amp_encoded = hf_amp >> 1
                lf_amp_encoded = (b[3] - 64) * 2 + (1 if (b[2] & 0x80) else 0)
                lf_amp_encoded = max(0, lf_amp_encoded)
                
                def encoded_to_linear(encoded):
                    if encoded <= 0:
                        return 0.0
                    if encoded >= 32:
                        return (2.0 ** (encoded / 32.0)) / 8.7
                    elif encoded >= 16:
                        return (2.0 ** (encoded / 16.0)) / 17.0
                    else:
                        return (encoded / 16.0) * 0.12
                
                hf_val = int(encoded_to_linear(hf_amp_encoded) * 800)
                lf_val = int(encoded_to_linear(lf_amp_encoded) * 800)
                
                hf_freq_encoded = b[0] | ((b[1] & 0x01) << 8)
                lf_freq_encoded = b[2] & 0x7F
                
                return VibrationData(
                    lf_freq=lf_freq_encoded,
                    lf_en_tone=False,
                    lf_amp=lf_val,
                    hf_freq=hf_freq_encoded,
                    hf_en_tone=False,
                    hf_amp=hf_val
                )

            if len(out_data) >= 6:
                rumble_mode = getattr(CONFIG, "rumble_mode", "Xbox")
                if side == "Left":
                    v1 = decode_32bit_rumble(out_data[2:6])
                    if rumble_mode != "Switch":
                        v1.lf_freq = 0x0e1; v1.hf_freq = 0x1e1
                    v2 = v1
                    v3 = v1
                    with self.vibration_lock:
                        self.switch_vibrations_left = [v1, v2, v3]
                        self.vibration_dirty_l = True
                elif side == "Right":
                    if len(out_data) >= 10:
                        v1 = decode_32bit_rumble(out_data[6:10])
                    else:
                        v1 = decode_32bit_rumble(out_data[2:6])
                    if rumble_mode != "Switch":
                        v1.lf_freq = 0x0e1; v1.hf_freq = 0x1e1
                    v2 = v1
                    v3 = v1
                    with self.vibration_lock:
                        self.switch_vibrations_right = [v1, v2, v3]
                        self.vibration_dirty_r = True
                else: # Pro
                    v1_left = decode_32bit_rumble(out_data[2:6])
                    if len(out_data) >= 10:
                        v1_right = decode_32bit_rumble(out_data[6:10])
                    else:
                        v1_right = decode_32bit_rumble(out_data[2:6])
                    if rumble_mode != "Switch":
                        v1_left.lf_freq = 0x0e1; v1_left.hf_freq = 0x1e1
                        v1_right.lf_freq = 0x0e1; v1_right.hf_freq = 0x1e1
                    with self.vibration_lock:
                        self.switch_vibrations_left = [v1_left, v1_left, v1_left]
                        self.switch_vibrations_right = [v1_right, v1_right, v1_right]
                        self.vibration_dirty_l = True
                        self.vibration_dirty_r = True
            
            self.last_rumble_received_time = time.perf_counter()
            return

        if out_data[0] != 0x02:
            # Explicitly clear all rumble state on disconnect (or unknown packet)
            with self.vibration_lock:
                self.frame_vibrations = [VibrationData() for _ in range(3)]
                for i in range(3):
                    self.slot_inputs[i].clear()
                    self.slot_inputs[i].append((0x0e1, 0, 0x1e1, 0))
                self.latest_vibration = VibrationData(lf_amp=0, hf_amp=0)
                for i in range(3):
                    self.slot_inputs_right[i].clear()
                    self.slot_inputs_right[i].append((0x0e1, 0, 0x1e1, 0))
                self.vibration_dirty = True
                self.rumble_force_clear = True  # Signal controller.py to reset rumble_stopped
                self.last_rumble_active_time = 0
            # Reset watchdog so it does NOT fire immediately after this clear
            self.last_rumble_received_time = time.perf_counter()
            return

        # Valid packet received. Only active rumble refreshes the hold watchdog; neutral
        # keepalives must not cut continuous rumble between active frames.
        packet_time = time.perf_counter()
        self.last_rumble_received_time = packet_time
            
        def is_neutral(offset):
            return (out_data[offset] == 0x87 and 
                    out_data[offset+1] == 0x01 and 
                    out_data[offset+2] == 0x20 and 
                    out_data[offset+3] == 0x11 and 
                    out_data[offset+4] == 0x00)
                    
        def vibration_data_from_bytes(b):
            value = int.from_bytes(b, byteorder='little')
            return VibrationData(
                lf_freq = value & 0x1FF,
                lf_en_tone = bool((value >> 9) & 1),
                lf_amp = (value >> 10) & 0x3FF,
                hf_freq = (value >> 20) & 0x1FF,
                hf_en_tone = bool((value >> 29) & 1),
                hf_amp = (value >> 30) & 0x3FF
            )
            
        def decode_5byte_frame(frame_bytes):
            value = int.from_bytes(frame_bytes, byteorder='little')
            lf_freq = value & 0x1FF
            lf_amp_10bit = (value >> 10) & 0x3FF
            hf_freq = (value >> 20) & 0x1FF
            hf_amp_10bit = (value >> 30) & 0x3FF
            
            # Extract 7-bit amplitudes
            lf_amp_7bit = int(lf_amp_10bit * 127 / 1023)
            hf_amp_7bit = int(hf_amp_10bit * 127 / 1023)
            
            # Scale to 0-800 scale
            lf_val = int(lf_amp_7bit * 800 / 127)
            hf_val = int(hf_amp_7bit * 800 / 127)
            
            return lf_freq, lf_val, hf_freq, hf_val
            
        active = False
        for b in out_data[2:]:
            if b != 0:
                active = True
                break
                
        if len(out_data) >= 23 and is_neutral(2) and is_neutral(18):
            active = False
            
        rumble_mode = getattr(CONFIG, "rumble_mode", "Xbox")
        
        if active:
            self.last_rumble_active_time = packet_time
            v1 = vibration_data_from_bytes(out_data[2:7])
            v2 = vibration_data_from_bytes(out_data[7:12])
            v3 = vibration_data_from_bytes(out_data[12:17])
            
            if rumble_mode != "Switch":
                XBOX_LF_FREQ = 0x0e1
                XBOX_HF_FREQ = 0x1e1
                
                v1.lf_freq = XBOX_LF_FREQ; v1.hf_freq = XBOX_HF_FREQ
                v2.lf_freq = XBOX_LF_FREQ; v2.hf_freq = XBOX_HF_FREQ
                v3.lf_freq = XBOX_LF_FREQ; v3.hf_freq = XBOX_HF_FREQ

            def _copy_vib(vd):
                return VibrationData(
                    lf_freq=vd.lf_freq, lf_en_tone=vd.lf_en_tone, lf_amp=vd.lf_amp,
                    hf_freq=vd.hf_freq, hf_en_tone=vd.hf_en_tone, hf_amp=vd.hf_amp)
            with self.vibration_lock:
                self.frame_vibrations = [v1, v2, v3]
                self.latest_vibration = v3
                self.vibration_dirty = True
                # Also populate the PER-SIDE buffers that get_current_vibration_frames()
                # actually consumes for Switch2 (the shared buffer alone is never read
                # there, which is why Switch2 rumble appeared dead). Use independent
                # copies so each side's consume (which zeroes its frames) can't clear
                # the other. Switch 2 sends one rumble for the merged pair, so both
                # sides get the same waveform.
                self.frame_vibrations_l = [_copy_vib(v1), _copy_vib(v2), _copy_vib(v3)]
                self.frame_vibrations_r = [_copy_vib(v1), _copy_vib(v2), _copy_vib(v3)]
                self.latest_vibration_l = _copy_vib(v3)
                self.latest_vibration_r = _copy_vib(v3)
                self.vibration_dirty_l = True
                self.vibration_dirty_r = True
        else:
            last_active = getattr(self, 'last_rumble_active_time', 0)
            if not last_active or packet_time - last_active > SWITCH_RUMBLE_TIMEOUT:
                with self.vibration_lock:
                    self.frame_vibrations = [VibrationData() for _ in range(3)]
                    self.latest_vibration = VibrationData()
                    self.vibration_dirty = True
                    self.frame_vibrations_l = [VibrationData() for _ in range(3)]
                    self.frame_vibrations_r = [VibrationData() for _ in range(3)]
                    self.latest_vibration_l = VibrationData()
                    self.latest_vibration_r = VibrationData()
                    self.vibration_dirty_l = True
                    self.vibration_dirty_r = True

    def update_as_switch2_pro(self, inputData: ControllerInputData, buttons: int, controller: Controller):
        state = bytearray(64)
        state[0] = 0x05
        state[2] = 0x12
        
        b5 = 0
        if buttons & SWITCH_BUTTONS["Y"]: b5 |= 0x01
        if buttons & SWITCH_BUTTONS["X"]: b5 |= 0x02
        if buttons & SWITCH_BUTTONS["B"]: b5 |= 0x04
        if buttons & SWITCH_BUTTONS["A"]: b5 |= 0x08
        if buttons & SWITCH_BUTTONS["R"]: b5 |= 0x40
        if buttons & SWITCH_BUTTONS["ZR"]: b5 |= 0x80
        state[5] = b5
        
        b6 = 0
        if buttons & SWITCH_BUTTONS["MINUS"]: b6 |= 0x01
        if buttons & SWITCH_BUTTONS["PLUS"]: b6 |= 0x02
        if buttons & SWITCH_BUTTONS["R_STK"]: b6 |= 0x04
        if buttons & SWITCH_BUTTONS["L_STK"]: b6 |= 0x08
        if buttons & SWITCH_BUTTONS.get("HOME", 0): b6 |= 0x10
        if buttons & SWITCH_BUTTONS.get("CAPT", 0): b6 |= 0x20
        if buttons & SWITCH_BUTTONS.get("C", 0): b6 |= 0x40
        state[6] = b6
        
        b7 = 0
        if buttons & SWITCH_BUTTONS["DOWN"]: b7 |= 0x01
        if buttons & SWITCH_BUTTONS["UP"]: b7 |= 0x02
        if buttons & SWITCH_BUTTONS["RIGHT"]: b7 |= 0x04
        if buttons & SWITCH_BUTTONS["LEFT"]: b7 |= 0x08
        if buttons & SWITCH_BUTTONS["L"]: b7 |= 0x40
        if buttons & SWITCH_BUTTONS["ZL"]: b7 |= 0x80
        state[7] = b7
        
        # Safeguard GL/GR for Joycons: only allow if mapped
        if controller.is_joycon():
            mapping_scope = self._controller_mapping_scope(controller)
            joycon_mappings = [
                CONFIG.get_mapping_setting_scoped("home", "Default", mapping_scope),
                CONFIG.get_mapping_setting_scoped("capt", "Capture" if getattr(CONFIG, "simulation_mode", "PS5") in ("Switch1", "Switch2") else "PrtSc", mapping_scope),
                CONFIG.get_mapping_setting_scoped("c", "Default", mapping_scope),
                CONFIG.get_mapping_setting_scoped("sll", "Default", mapping_scope),
                CONFIG.get_mapping_setting_scoped("srl", "Default", mapping_scope),
                CONFIG.get_mapping_setting_scoped("slr", "Default", mapping_scope),
                CONFIG.get_mapping_setting_scoped("srr", "Default", mapping_scope),
            ]
            if "GL" not in joycon_mappings:
                buttons &= ~SWITCH_BUTTONS.get("GL", 0x02000000)
            if "GR" not in joycon_mappings:
                buttons &= ~SWITCH_BUTTONS.get("GR", 0x01000000)

        b8 = 0
        if buttons & SWITCH_BUTTONS.get("GR", 0): b8 |= 0x01
        if buttons & SWITCH_BUTTONS.get("GL", 0): b8 |= 0x02
        state[8] = b8

        # Joystick and IMU routing
        custom_stick_route = getattr(inputData, 'custom_joystick_mapping', None)
        if len(self.controllers) == 1:
            if not controller.is_joycon() and (
                self._joystick_mapping_mode("l_joystick", controller) in ("L Joystick", "R Joystick") or
                self._joystick_mapping_mode("r_joystick", controller) in ("L Joystick", "R Joystick")
            ):
                mixed_left, mixed_right = self._update_merged_stick_mix(inputData, controller)
                self.last_s2_lx = mixed_left[0]
                self.last_s2_ly = mixed_left[1]
                self.last_s2_rx = mixed_right[0]
                self.last_s2_ry = mixed_right[1]
            elif custom_stick_route:
                self.last_s2_lx = inputData.left_stick[0]
                self.last_s2_ly = inputData.left_stick[1]
                self.last_s2_rx = inputData.right_stick[0]
                self.last_s2_ry = inputData.right_stick[1]
            elif controller.is_joycon_right():
                if self.hold_mode == "Vertical":
                    self.last_s2_rx = inputData.right_stick[0]
                    self.last_s2_ry = inputData.right_stick[1]
                    self.last_s2_lx = 0.0
                    self.last_s2_ly = 0.0
                else: # Horizontal
                    self.last_s2_lx = inputData.right_stick[0]
                    self.last_s2_ly = inputData.right_stick[1]
                    self.last_s2_rx = 0.0
                    self.last_s2_ry = 0.0
            else: # Joycon Left or Pro Controller
                self.last_s2_lx = inputData.left_stick[0]
                self.last_s2_ly = inputData.left_stick[1]
                self.last_s2_rx = inputData.right_stick[0]
                self.last_s2_ry = inputData.right_stick[1]

            self.last_s2_rx, self.last_s2_ry = self._add_gyro_rstick_overlay(
                self.last_s2_rx,
                self.last_s2_ry,
                inputData,
            )
            
            if self.hold_mode == "Horizontal" and not controller.is_pro_controller():
                if controller.is_joycon_right():
                    self.last_s2_gx = inputData.gyroscope[1]
                    self.last_s2_gy = inputData.gyroscope[2]
                    self.last_s2_gz = -inputData.gyroscope[0]
                    self.last_s2_ax = -inputData.accelerometer[1]
                    self.last_s2_ay = inputData.accelerometer[2]
                    self.last_s2_az = inputData.accelerometer[0]
                else:
                    self.last_s2_gx = -inputData.gyroscope[1]
                    self.last_s2_gy = inputData.gyroscope[2]
                    self.last_s2_gz = inputData.gyroscope[0]
                    self.last_s2_ax = -inputData.accelerometer[1]
                    self.last_s2_ay = inputData.accelerometer[2]
                    self.last_s2_az = -inputData.accelerometer[0]
            else:
                self.last_s2_gx = inputData.gyroscope[0]
                self.last_s2_gy = inputData.gyroscope[2]
                self.last_s2_gz = -inputData.gyroscope[1]
                self.last_s2_ax = inputData.accelerometer[0]
                self.last_s2_ay = inputData.accelerometer[2]
                self.last_s2_az = -inputData.accelerometer[1]
        else: # Dual Joycons (len == 2)
            mixed_left, mixed_right = self._update_merged_stick_mix(inputData, controller)
            mixed_right = self._add_gyro_rstick_overlay(mixed_right[0], mixed_right[1], inputData)
            self.last_s2_lx = mixed_left[0]
            self.last_s2_ly = mixed_left[1]
            self.last_s2_rx = mixed_right[0]
            self.last_s2_ry = mixed_right[1]
                
            if getattr(controller, 'gyro_active', False):
                self.last_s2_gx = inputData.gyroscope[0]
                self.last_s2_gy = inputData.gyroscope[2]
                self.last_s2_gz = -inputData.gyroscope[1]
                self.last_s2_ax = inputData.accelerometer[0]
                self.last_s2_ay = inputData.accelerometer[2]
                self.last_s2_az = -inputData.accelerometer[1]

        if getattr(CONFIG, "gyro_mode", "World") == "Roll" and controller.gyro_mouse_enabled:
            steer = getattr(controller, '_shared_steer_value', controller._own_steer_value if hasattr(controller, '_own_steer_value') else 0.0)
            self.last_s2_lx = steer

        def float_to_12bit(val):
            return int(max(0, min(4095, round((val + 1.0) * 2047.5))))
            
        lx = float_to_12bit(self.last_s2_lx)
        ly = float_to_12bit(self.last_s2_ly)
        rx = float_to_12bit(self.last_s2_rx)
        ry = float_to_12bit(self.last_s2_ry)
        
        state[11] = lx & 0xff
        state[12] = ((lx >> 8) & 0x0f) | ((ly & 0x0f) << 4)
        state[13] = (ly >> 4) & 0xff
        
        state[14] = rx & 0xff
        state[15] = ((rx >> 8) & 0x0f) | ((ry & 0x0f) << 4)
        state[16] = (ry >> 4) & 0xff
        
        # Microsecond timestamp (32-bit LE) at bytes 32-35
        now_us = int(time.perf_counter() * 1000000) & 0xffffffff
        state[32:36] = struct.pack("<I", now_us)
        
        # Byte 42 = 0x01
        state[42] = 0x01
        
        # Microsecond timestamp (32-bit LE) at bytes 43-46
        state[43:47] = struct.pack("<I", now_us)
        
        # IMU sequence counter (16-bit LE) at bytes 47-48
        imu_seq = getattr(controller, '_imu_seq', 0)
        state[47:49] = struct.pack("<H", imu_seq)
        controller._imu_seq = (imu_seq + 1) & 0xffff
        
        def clamp_i16(v): return max(-32768, min(32767, int(round(v))))

        raw_gx = clamp_i16(self.last_s2_gx)
        raw_gy = clamp_i16(self.last_s2_gz)  # Swap Yaw and Roll for Switch 2 Emu: Y is Roll
        raw_gz = clamp_i16(self.last_s2_gy)  # Z is Yaw
        raw_ax = clamp_i16(self.last_s2_ax)
        raw_ay = clamp_i16(self.last_s2_az)  # Y is Roll/Y-axis
        raw_az = clamp_i16(self.last_s2_ay)  # Z is Yaw/Z-axis

        state[49:61] = struct.pack('<6h', raw_ax, raw_ay, raw_az, raw_gx, raw_gy, raw_gz)
        
        if hasattr(self, 'usbip_server') and self.usbip_server:
            self.usbip_server.update_state(state)



def reset_vigem_bus(force=False):
    """
    Force-reset the global ViGEm bus handle used by vgamepad.
    This is critical for preventing BSOD (0x10D) during sleep/wake cycles.
    """
    if not force and getattr(CONFIG, "driver_type", "WinUHid") != "ViGEmBus":
        logger.info("reset_vigem_bus called (no-op for WinUHid)")
        return
        
    try:
        get_vigem()
    except Exception as e:
        logger.error(f"Cannot reset ViGEm bus: {e}")
        return
        
    import vgamepad.win.virtual_gamepad as vvg
    import gc
    
    logger.info("Resetting ViGEm bus handle for power state transition.")
    try:
        # 1. Clear the global singleton reference
        if hasattr(vvg, 'VBUS'):
            old_bus = vvg.VBUS
            vvg.VBUS = None
            # Explicitly delete to encourage immediate cleanup
            if old_bus:
                try:
                    # Access private members to force disconnect if __del__ hasn't run yet
                    import vgamepad.win.vigem_client as vcli
                    if hasattr(old_bus, '_busp') and old_bus._busp:
                        logger.debug("Manually disconnecting stale ViGEm bus.")
                        vcli.vigem_disconnect(old_bus._busp)
                        vcli.vigem_free(old_bus._busp)
                        old_bus._busp = None
                except:
                    pass
                del old_bus
        
        # 2. Collect garbage to ensure VBus.__del__ runs and driver handles are closed
        gc.collect()
        
        # 3. Create a fresh VBus instance for the next use cycle
        # We only do this if we are not currently suspending
        from discoverer import _IS_SUSPENDING
        if not _IS_SUSPENDING:
            vvg.VBUS = vvg.VBus()
            logger.info("New ViGEm bus handle initialized.")
        else:
            logger.info("ViGEm bus cleared for suspend. Will re-init on wake.")
    except Exception as e:
        logger.error(f"Error resetting ViGEm bus: {e}")
