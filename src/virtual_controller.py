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
from controller import Controller, ControllerInputData, VibrationData, NSO_GAMECUBE_CONTROLLER_PID
from config import CONFIG, ButtonConfig, SWITCH_BUTTONS, XB_BUTTONS
from usbip_server import USBIPServer
from utils import USBIPAllocator

logger = logging.getLogger(__name__)

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
                    subprocess.run([usbip_exe, "detach", "-p", port_num_str], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
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
                    subprocess.run([usbip_exe, "detach", "-p", port_num_str], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
    except Exception as e:
        logger.error(f"Error detaching all USBIP devices: {e}")

RUMBLE_WRITE_INTERVAL = 0.015
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
            if 'MAC_TO_USBIP' not in globals():
                MAC_TO_USBIP = {}
                
            if mac in MAC_TO_USBIP:
                self.host_ip, self.bus_id, self.server_port = MAC_TO_USBIP[mac]
            else:
                self.host_ip, self.bus_id, self.server_port = USBIPAllocator.allocate()
                MAC_TO_USBIP[mac] = (self.host_ip, self.bus_id, self.server_port)
        else:
            self.host_ip, self.bus_id, self.server_port = USBIPAllocator.allocate()
                    
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
        self.target_vibration = VibrationData(lf_amp=0, hf_amp=0)
        self.latest_vibration = VibrationData(lf_amp=0, hf_amp=0)
        self.frame_vibrations = [VibrationData(lf_amp=0, hf_amp=0) for _ in range(3)]
        self.vibration_dirty = False
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
        
        self.mode = getattr(CONFIG, "simulation_mode", "Xbox One")
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
        self.running = True
        self.update_thread = threading.Thread(target=self._1000hz_loop, daemon=True)
        self.update_thread.start()

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
                            subprocess.Popen(
                                [usbip_exe, "-t", str(server_port), "attach", "-r", self.host_ip, "-b", self.bus_id],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
                            )
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
            if driver_type == "ViGEmBus":
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
                    self.report.BatteryPercent = 10
                    self.report.BatteryState = 2
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
            raw_lf = self.frame_vibrations[slot].lf_amp + lf_val
            raw_hf = self.frame_vibrations[slot].hf_amp + hf_val
            
            if raw_lf <= 560:
                self.frame_vibrations[slot].lf_amp = int(raw_lf)
            else:
                self.frame_vibrations[slot].lf_amp = int(560 + 240 * math.tanh((raw_lf - 560) / 240))
                
            if raw_hf <= 560:
                self.frame_vibrations[slot].hf_amp = int(raw_hf)
            else:
                self.frame_vibrations[slot].hf_amp = int(560 + 240 * math.tanh((raw_hf - 560) / 240))
            
            self.latest_vibration.lf_amp = lf_val
            self.latest_vibration.hf_amp = hf_val
            
            self.vibration_dirty = True

    def get_current_vibration_frames(self, is_left=True):
        if self.mode == "Switch1":
            with self.vibration_lock:
                vibs = self.switch_vibrations_left if is_left else self.switch_vibrations_right
                
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
            if self.vibration_dirty:
                v1 = VibrationData(lf_amp=self.frame_vibrations[0].lf_amp, hf_amp=self.frame_vibrations[0].hf_amp)
                v2 = VibrationData(lf_amp=self.frame_vibrations[1].lf_amp, hf_amp=self.frame_vibrations[1].hf_amp)
                v3 = VibrationData(lf_amp=self.frame_vibrations[2].lf_amp, hf_amp=self.frame_vibrations[2].hf_amp)
                
                if v1.lf_amp == 0 and v1.hf_amp == 0:
                    v1.lf_amp, v1.hf_amp = self.latest_vibration.lf_amp, self.latest_vibration.hf_amp
                if v2.lf_amp == 0 and v2.hf_amp == 0:
                    v2.lf_amp, v2.hf_amp = v1.lf_amp, v1.hf_amp
                if v3.lf_amp == 0 and v3.hf_amp == 0:
                    v3.lf_amp, v3.hf_amp = v2.lf_amp, v2.hf_amp

                for f in self.frame_vibrations:
                    f.lf_amp = 0
                    f.hf_amp = 0
                self.vibration_dirty = False
                self.cycle_start_time = time.perf_counter()
            else:
                v1 = VibrationData(lf_amp=self.latest_vibration.lf_amp, hf_amp=self.latest_vibration.hf_amp)
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
                shared_gyro = any(getattr(c, '_own_gyro_trigger', False) for c in self.controllers)
                # Sync ZR/ZL for Gyro Mouse clicks
                shared_zr = any(getattr(c, '_own_zr_pressed', False) for c in self.controllers)
                shared_zl = any(getattr(c, '_own_zl_pressed', False) for c in self.controllers)
                
                # Sync Steer Value (From the gyro-active controller)
                shared_steer = 0.0
                shared_rs = (0.0, 0.0)
                for c in self.controllers:
                    if getattr(c, 'gyro_active', False):
                        shared_steer = getattr(c, '_own_steer_value', 0.0)
                    if c.is_joycon_right():
                        shared_rs = inputData.right_stick if c == controller else getattr(c, '_last_rs', (0.0, 0.0))

                for c in self.controllers:
                    c._shared_gyro_trigger = shared_gyro
                    c._shared_zr_pressed = shared_zr
                    c._shared_zl_pressed = shared_zl
                    c._shared_steer_value = shared_steer
                    c._shared_right_stick = shared_rs
                
                if controller.is_joycon_right():
                    controller._last_rs = inputData.right_stick
                
                # Sync activation state across controllers for consistent steering/mouse behavior
                # Only for Hold mode; Toggle mode naturally syncs via shared trigger
                if getattr(CONFIG, 'gyro_activation_mode', 'Hold') == 'Hold':
                    for c in self.controllers:
                        c.gyro_mouse_enabled = shared_gyro
            else:
                # If not merged, ensure we don't use a stale shared steer value
                controller._shared_steer_value = getattr(controller, '_own_steer_value', 0.0)
                controller._shared_gyro_trigger = getattr(controller, '_own_gyro_trigger', False)
                controller._shared_zr_pressed = getattr(controller, '_own_zr_pressed', False)
                controller._shared_zl_pressed = getattr(controller, '_own_zl_pressed', False)
                
            current_buttons = inputData.buttons 
            
            if len(self.controllers) == 1 and self.mode != "Switch1":
                if controller.is_joycon_left():
                    if self.hold_mode == "Vertical":
                        inputData.right_stick = inputData.left_stick
                        inputData.left_stick = (0, 0)
                        
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
                        lx, ly = inputData.left_stick
                        inputData.left_stick = (-ly, lx)
                        inputData.right_stick = (0, 0)
                        
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
                        pass 
                    elif self.hold_mode == "Horizontal":
                        rx, ry = inputData.right_stick
                        inputData.right_stick = (ry, -rx)
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

        lx, ly, rx, ry = 0.0, 0.0, 0.0, 0.0
        gx, gy, gz, ax, ay, az = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0

        if device_type == "L":
            lx = inputData.left_stick[0]
            ly = inputData.left_stick[1]
            
            if hold_mode == "Vertical":
                gx, gy, gz =  inputData.gyroscope[1] * 0.25,  -inputData.gyroscope[0] * 0.25,  inputData.gyroscope[2] * 0.25
                ax, ay, az =  inputData.accelerometer[1],  -inputData.accelerometer[0],  inputData.accelerometer[2]
            else: # Horizontal
                gx, gy, gz =  inputData.gyroscope[0], inputData.gyroscope[1],  inputData.gyroscope[2]
                ax, ay, az =  inputData.accelerometer[0], inputData.accelerometer[1],  inputData.accelerometer[2]
                
            if getattr(CONFIG, "gyro_mode", "World") == "Roll" and controller.gyro_mouse_enabled:
                lx = getattr(controller, '_shared_steer_value', getattr(controller, '_own_steer_value', 0.0))
                
        elif device_type == "R": # device_type == "R"
            rx = inputData.right_stick[0]
            ry = inputData.right_stick[1]
            
            if hold_mode == "Vertical":
                gx, gy, gz =  inputData.gyroscope[1] * 0.25, inputData.gyroscope[0] * 0.25, -inputData.gyroscope[2] * 0.25
                ax, ay, az =  inputData.accelerometer[1], inputData.accelerometer[0], -inputData.accelerometer[2]
            else: # Horizontal
                gx, gy, gz = -inputData.gyroscope[0], inputData.gyroscope[1], -inputData.gyroscope[2]
                ax, ay, az = -inputData.accelerometer[0], inputData.accelerometer[1], -inputData.accelerometer[2]
                
            if getattr(CONFIG, "gyro_mode", "World") == "Roll" and controller.gyro_mouse_enabled:
                rx = getattr(controller, '_shared_steer_value', getattr(controller, '_own_steer_value', 0.0))
        else: # Pro
            lx = inputData.left_stick[0]
            ly = inputData.left_stick[1]
            rx = inputData.right_stick[0]
            ry = inputData.right_stick[1]
            
            gx, gy, gz = inputData.gyroscope[1], -inputData.gyroscope[0], inputData.gyroscope[2]
            ax, ay, az = inputData.accelerometer[1], -inputData.accelerometer[0], inputData.accelerometer[2]

            if getattr(CONFIG, "gyro_mode", "World") == "Roll" and controller.gyro_mouse_enabled:
                lx = getattr(controller, '_shared_steer_value', getattr(controller, '_own_steer_value', 0.0))


        def float_to_12bit(val):
            return int(max(0, min(4095, round((val + 1.0) * 2047.5))))

        lx_12 = float_to_12bit(lx)
        ly_12 = float_to_12bit(ly)
        rx_12 = float_to_12bit(rx)
        ry_12 = float_to_12bit(ry)

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


        imu_frame = struct.pack('<6h', 
            clamp_i16(ax), clamp_i16(ay), clamp_i16(az), 
            clamp_i16(gx), clamp_i16(gy), clamp_i16(gz)
        )
        
        # Switch 1 standard requires 3 IMU frames (each 12 bytes) per packet.
        # We duplicate the latest IMU frame 3 times to ensure smooth integration in the host emulator.
        state[13:25] = imu_frame
        state[25:37] = imu_frame
        state[37:49] = imu_frame

        return state

    def update_as_switch1_joycon_l(self, inputData: ControllerInputData, buttons: int, controller):
        if self.driver_type == "USBIP":
            if hasattr(self, 'usbip_server_l') and self.usbip_server_l:
                state = self._build_switch1_report(inputData, buttons, controller, device_type="L")
                self.usbip_server_l.update_state(state)

    def update_as_switch1_joycon_r(self, inputData: ControllerInputData, buttons: int, controller):
        if self.driver_type == "USBIP":
            if hasattr(self, 'usbip_server_r') and self.usbip_server_r:
                state = self._build_switch1_report(inputData, buttons, controller, device_type="R")
                self.usbip_server_r.update_state(state)

    def update_as_switch1_pro(self, inputData: ControllerInputData, buttons: int, controller):
        if self.driver_type == "USBIP":
            if hasattr(self, 'usbip_server_pro') and self.usbip_server_pro:
                state = self._build_switch1_report(inputData, buttons, controller, device_type="Pro")
                self.usbip_server_pro.update_state(state)

    def update_as_ps4(self, inputData: ControllerInputData, buttons: int, controller: Controller):

        with self.state_lock:
            if self.vg_controller is None:
                return
            self._update_as_ps4_locked(inputData, buttons, controller)

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

            if len(self.controllers) == 1:
                if controller.is_joycon_right():
                    if self.hold_mode == "Vertical":
                        self.last_rx = int(max(0, min(255, round(inputData.right_stick[0] * 127.5 + 128))))
                        self.last_ry = int(max(0, min(255, round(-inputData.right_stick[1] * 127.5 + 128))))
                        self.last_lx = 128
                        self.last_ly = 128
                    else:
                        self.last_lx = float_to_byte(inputData.right_stick[0])
                        self.last_ly = float_to_byte(-inputData.right_stick[1])
                else:
                    self.last_lx = float_to_byte(inputData.left_stick[0])
                    self.last_ly = float_to_byte(-inputData.left_stick[1])
                    self.last_rx = float_to_byte(inputData.right_stick[0])
                    self.last_ry = float_to_byte(-inputData.right_stick[1])
                
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
                if controller.is_joycon_left():
                    self.last_lx = float_to_byte(inputData.left_stick[0])
                    self.last_ly = float_to_byte(-inputData.left_stick[1])
                elif controller.is_joycon_right():
                    self.last_rx = float_to_byte(inputData.right_stick[0])
                    self.last_ry = float_to_byte(-inputData.right_stick[1])
                    
                if getattr(controller, 'gyro_active', False):
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
        
        if mode == "PS4":
            if winuhid._winuhid_devs:
                winuhid._winuhid_devs.WinUHidPS4SetHatState(ctypes.byref(report), hat_x, hat_y)
        else:
            if winuhid._winuhid_devs:
                winuhid._winuhid_devs.WinUHidPS5SetHatState(ctypes.byref(report), hat_x, hat_y)

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
        else:
            report.LeftTrigger = 255 if (buttons & SWITCH_BUTTONS["ZL"]) else 0
            report.RightTrigger = 255 if (buttons & SWITCH_BUTTONS["ZR"]) else 0

        # 5. Joysticks Routing
        if not hasattr(self, 'last_lx'):
            self.last_lx = 128; self.last_ly = 128
            self.last_rx = 128; self.last_ry = 128
            self.last_gx = 0; self.last_gy = 0; self.last_gz = 0
            self.last_ax = 0; self.last_ay = 0; self.last_az = 0

        if len(self.controllers) == 1:
            if controller.is_joycon_right():
                if self.hold_mode == "Vertical":
                    self.last_rx = int(max(0, min(255, round(inputData.right_stick[0] * 127.5 + 128))))
                    self.last_ry = int(max(0, min(255, round(-inputData.right_stick[1] * 127.5 + 128))))
                    self.last_lx = 128
                    self.last_ly = 128
                else:
                    self.last_lx = float_to_byte(inputData.right_stick[0])
                    self.last_ly = float_to_byte(-inputData.right_stick[1])
            else:
                self.last_lx = float_to_byte(inputData.left_stick[0])
                self.last_ly = float_to_byte(-inputData.left_stick[1])
                self.last_rx = float_to_byte(inputData.right_stick[0])
                self.last_ry = float_to_byte(-inputData.right_stick[1])
            
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
            if controller.is_joycon_left():
                self.last_lx = float_to_byte(inputData.left_stick[0])
                self.last_ly = float_to_byte(-inputData.left_stick[1])
            elif controller.is_joycon_right():
                self.last_rx = float_to_byte(inputData.right_stick[0])
                self.last_ry = float_to_byte(-inputData.right_stick[1])
                
            if getattr(controller, 'gyro_active', False):
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
            
            if self.mode == "Xbox360":
                if CONFIG.abxy_mode == "Switch":
                    if buttons & SWITCH_BUTTONS["Y"]: xb_btns |= XB_BUTTONS["Y"]
                    if buttons & SWITCH_BUTTONS["X"]: xb_btns |= XB_BUTTONS["X"]
                    if buttons & SWITCH_BUTTONS["B"]: xb_btns |= XB_BUTTONS["B"]
                    if buttons & SWITCH_BUTTONS["A"]: xb_btns |= XB_BUTTONS["A"]
                else:
                    if buttons & SWITCH_BUTTONS["Y"]: xb_btns |= XB_BUTTONS["Y"]
                    if buttons & SWITCH_BUTTONS["X"]: xb_btns |= XB_BUTTONS["X"]
                    if buttons & SWITCH_BUTTONS["B"]: xb_btns |= XB_BUTTONS["B"]
                    if buttons & SWITCH_BUTTONS["A"]: xb_btns |= XB_BUTTONS["A"]
            else:
                if CONFIG.abxy_mode == "Switch":
                    if buttons & SWITCH_BUTTONS["Y"]: xb_btns |= XB_BUTTONS["X"]
                    if buttons & SWITCH_BUTTONS["X"]: xb_btns |= XB_BUTTONS["Y"]
                    if buttons & SWITCH_BUTTONS["B"]: xb_btns |= XB_BUTTONS["A"]
                    if buttons & SWITCH_BUTTONS["A"]: xb_btns |= XB_BUTTONS["B"]
                else:
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
 
            if len(self.controllers) == 1:
                if controller.is_joycon_right():
                    if self.hold_mode == "Vertical":
                        self.last_xb_rx = inputData.right_stick[0]
                        self.last_xb_ry = -inputData.right_stick[1]
                        self.last_xb_lx = 0.0; self.last_xb_ly = 0.0
                    else:
                        self.last_xb_lx = inputData.right_stick[0]
                        self.last_xb_ly = -inputData.right_stick[1]
                else:
                    self.last_xb_lx = inputData.left_stick[0]
                    self.last_xb_ly = -inputData.left_stick[1]
                    self.last_xb_rx = inputData.right_stick[0]
                    self.last_xb_ry = -inputData.right_stick[1]
            else:
                if controller.is_joycon_left():
                    self.last_xb_lx = inputData.left_stick[0]
                    self.last_xb_ly = -inputData.left_stick[1]
                elif controller.is_joycon_right():
                    self.last_xb_rx = inputData.right_stick[0]
                    self.last_xb_ry = -inputData.right_stick[1]

            if getattr(CONFIG, "gyro_mode", "World") == "Roll" and controller.gyro_mouse_enabled:
                self.last_xb_lx = getattr(controller, '_shared_steer_value', controller._own_steer_value if hasattr(controller, '_own_steer_value') else 0.0)

            # Phase 3: Final Reporting
            if self.driver_type == "ViGEmBus":
                self.vg_controller.report.wButtons = xb_btns
                self.vg_controller.left_trigger(lt)
                self.vg_controller.right_trigger(rt)
                self.vg_controller.left_joystick_float(self.last_xb_lx, -self.last_xb_ly)
                self.vg_controller.right_joystick_float(self.last_xb_rx, -self.last_xb_ry)
            else:
                self.vg_controller.set_buttons(xb_btns)
                self.vg_controller.left_trigger(lt)
                self.vg_controller.right_trigger(rt)
                self.vg_controller.left_joystick_float(self.last_xb_lx, self.last_xb_ly)
                self.vg_controller.right_joystick_float(self.last_xb_rx, self.last_xb_ry)

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
                else:
                    if self.mode == "Switch1":
                        pass
                    else:
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

    def _usbip_rumble_callback(self, out_data, side="Left"):
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
            # Reset watchdog so it does NOT fire immediately after this clear
            self.last_rumble_received_time = time.perf_counter()
            return

        # Valid rumble packet: update watchdog timestamp
        self.last_rumble_received_time = time.perf_counter()
            
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
            v1 = vibration_data_from_bytes(out_data[2:7])
            v2 = vibration_data_from_bytes(out_data[7:12])
            v3 = vibration_data_from_bytes(out_data[12:17])
            
            if rumble_mode != "Switch":
                XBOX_LF_FREQ = 0x0e1
                XBOX_HF_FREQ = 0x1e1
                
                v1.lf_freq = XBOX_LF_FREQ; v1.hf_freq = XBOX_HF_FREQ
                v2.lf_freq = XBOX_LF_FREQ; v2.hf_freq = XBOX_HF_FREQ
                v3.lf_freq = XBOX_LF_FREQ; v3.hf_freq = XBOX_HF_FREQ

            with self.vibration_lock:
                self.frame_vibrations = [v1, v2, v3]
                self.latest_vibration = v3
                self.vibration_dirty = True
        else:
            with self.vibration_lock:
                self.frame_vibrations = [VibrationData() for _ in range(3)]
                self.latest_vibration = VibrationData()
                self.vibration_dirty = True

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
            joycon_mappings = [
                getattr(CONFIG, "home_mapping", "Default"),
                getattr(CONFIG, "capt_mapping", "Capture" if getattr(CONFIG, "simulation_mode", "Xbox One") in ("Switch1", "Switch2") else "PrtSc"),
                getattr(CONFIG, "c_mapping", "Default"),
                getattr(CONFIG, "sll_mapping", "Default"),
                getattr(CONFIG, "srl_mapping", "Default"),
                getattr(CONFIG, "slr_mapping", "Default"),
                getattr(CONFIG, "srr_mapping", "Default"),
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
        if len(self.controllers) == 1:
            if controller.is_joycon_right():
                if self.hold_mode == "Vertical":
                    self.last_s2_rx = inputData.right_stick[0]
                    self.last_s2_ry = inputData.right_stick[1]
                    self.last_s2_lx = 0.0
                    self.last_s2_ly = 0.0
                else: # Horizontal
                    self.last_s2_lx = inputData.right_stick[0]
                    self.last_s2_ly = inputData.right_stick[1]
            else: # Joycon Left or Pro Controller
                self.last_s2_lx = inputData.left_stick[0]
                self.last_s2_ly = inputData.left_stick[1]
                self.last_s2_rx = inputData.right_stick[0]
                self.last_s2_ry = inputData.right_stick[1]
            
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
            if controller.is_joycon_left():
                self.last_s2_lx = inputData.left_stick[0]
                self.last_s2_ly = inputData.left_stick[1]
            elif controller.is_joycon_right():
                self.last_s2_rx = inputData.right_stick[0]
                self.last_s2_ry = inputData.right_stick[1]
                
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