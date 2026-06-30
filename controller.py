import bleak
from bleak import BleakScanner, BleakClient, BleakGATTCharacteristic, BleakError
from bleak.backends.device import BLEDevice
import asyncio
BLE_CONNECTION_LOCK = asyncio.Lock()
import logging
import bluetooth
import win32api
import win32con
from dataclasses import dataclass
import ctypes
import time
import threading
import math
import imufusion
import numpy as np
try:
    ctypes.windll.winmm.timeBeginPeriod(1)
except Exception:
    pass
from config import CONFIG, SWITCH_BUTTONS, GYRO_LOCK_TOKEN, MODE_SHIFT_TOKEN
from utils import (
    apply_calibration_to_axis, apply_radial_deadzone, get_stick_xy, press_or_release_mouse_button,
    reverse_bits, signed_looping_difference_16bit, to_hex, decodeu, decodes, 
    convert_mac_string_to_value, vector_normalize, vector_cross, vector_dot,
    quaternion_multiply, quaternion_normalize, quaternion_rotate_vector,
    quaternion_from_vectors, show_notification, force_ui_update, trigger_change_profile,
    trigger_switch_profile
)
import utils

logging.basicConfig(
    format='%(asctime)s.%(msecs)03d %(levelname)s:%(name)s:%(message)s',
    datefmt='%H:%M:%S'
)
logging.getLogger().setLevel(logging.INFO)
# Bleak's WinRT scanner logs every received advertisement at DEBUG and is extremely
# noisy; keep it quiet even if the root level is lowered for debugging (matches 0.10.1).
logging.getLogger("bleak").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Controller identification info
NINTENDO_VENDOR_ID = 0x057e
JOYCON2_RIGHT_PID = 0x2066
JOYCON2_LEFT_PID = 0x2067
PRO_CONTROLLER2_PID = 0x2069
NSO_GAMECUBE_CONTROLLER_PID = 0x2073
PRO_CONTROLLER_PID = 0x2009
JOYCON_L_PID = 0x2006
JOYCON_R_PID = 0x2007

CONTROLER_NAMES = {
    JOYCON2_RIGHT_PID: "Joy-con 2 (Right)",
    JOYCON2_LEFT_PID: "Joy-con 2 (Left)",
    PRO_CONTROLLER2_PID: "Pro Controller 2",
    NSO_GAMECUBE_CONTROLLER_PID: "NSO Gamecube Controller",
    PRO_CONTROLLER_PID: "Pro Controller",
    JOYCON_L_PID: "Joy-con (Left)",
    JOYCON_R_PID: "Joy-con (Right)"
}

_gc_debug_counter = 0

# BLE GATT Characteristics UUID
INPUT_REPORT_UUID = "ab7de9be-89fe-49ad-828f-118f09df7fd2"
VIBRATION_WRITE_JOYCON_R_UUID = "fa19b0fb-cd1f-46a7-84a1-bbb09e00c149"
VIBRATION_WRITE_JOYCON_L_UUID = "289326cb-a471-485d-a8f4-240c14f18241"
VIBRATION_WRITE_PRO_CONTROLLER_UUID = "cc483f51-9258-427d-a939-630c31f72b05"

COMMAND_WRITE_UUID = "649d4ac9-8eb7-4e6c-af44-1ea54fe5f005"
COMMAND_RESPONSE_UUID = "c765a961-d9d8-4d36-a20a-5315b111836a"

# Commands and subcommands
COMMAND_LEDS = 0x09
SUBCOMMAND_LEDS_SET_PLAYER = 0x07
COMMAND_VIBRATION = 0x0A
SUBCOMMAND_VIBRATION_PLAY_PRESET = 0x02
COMMAND_HAPTICS_INIT = 0x03
SUBCOMMAND_HAPTICS_ENABLE = 0x0A
COMMAND_MEMORY = 0x02
SUBCOMMAND_MEMORY_READ = 0x04
COMMAND_PAIR = 0x15
SUBCOMMAND_PAIR_SET_MAC = 0x01
SUBCOMMAND_PAIR_LTK1 = 0x04
SUBCOMMAND_PAIR_LTK2 = 0x02
SUBCOMMAND_PAIR_FINISH = 0x03
COMMAND_FEATURE = 0x0c
SUBCOMMAND_FEATURE_INIT = 0x02
SUBCOMMAND_FEATURE_ENABLE = 0x04

FEATURE_MOTION = 0x04
FEATURE_MOUSE = 0x10
FEATURE_MAGNOMETER = 0x80

# Addresses in controller memory
ADDRESS_CONTROLLER_INFO = 0x00013000
CALIBRATION_JOYSTICK_1 = 0x0130A8
CALIBRATION_JOYSTICK_2 = 0x0130E8
CALIBRATION_USER_JOYSTICK_1 = 0x1fc042
CALIBRATION_USER_JOYSTICK_2 = 0x1fc062

LED_PATTERN = {
    1: 0x01, 2: 0x03, 3: 0x07, 4: 0x0F,
    5: 0x09, 6: 0x05, 7: 0x0D, 8: 0x06,
}

### Dataclasses ###

@dataclass
class MouseState:
    x: int
    y: int
    lb: bool
    mb: bool 
    rb: bool
    ir_active: bool = False

@dataclass
class StickCalibrationData:
    center: tuple[int, int]
    max: tuple[int, int]
    min: tuple[int, int]

    def __init__(self, data: bytes):
        if len(data) >= 9:
            self.center = get_stick_xy(data[0:3])
            # Max/min are absolute offsets from center
            self.max = get_stick_xy(data[6:9])
            self.min = get_stick_xy(data[3:6])

            # Sanity check: all-zeros/FF, OR a center far from the ~2048 mid-point,
            # means the calibration read returned garbage (e.g. an intermittently
            # failed bridge read). Fall back to centered defaults so the stick can't
            # get stuck at an extreme, which shows up as continuous joystick input.
            cx, cy = self.center
            mx, my = self.max
            nx, ny = self.min
            invalid_center = (
                (self.center == (0, 0) and self.max == (0, 0))
                or (self.center == (4095, 4095) and self.max == (4095, 4095))
                or not (1024 <= cx <= 3072) or not (1024 <= cy <= 3072)
            )
            invalid_range = (
                mx <= 0 or my <= 0 or nx <= 0 or ny <= 0
                or mx > (4095 - cx) or my > (4095 - cy)
                or nx > cx or ny > cy
            )
            if invalid_center or invalid_range:
                self.center = (2048, 2048)
                self.max = (1500, 1500)
                self.min = (1500, 1500)
        else:
            self.center = (2048, 2048)
            self.max = (1500, 1500)
            self.min = (1500, 1500)

    def apply_calibration(self, raw_values: tuple[int, int], gain: float = 1.0):
        x = max(-1.0, min(1.0, apply_calibration_to_axis(raw_values[0], self.center[0], self.max[0], self.min[0]) * gain))
        y = max(-1.0, min(1.0, apply_calibration_to_axis(raw_values[1], self.center[1], self.max[1], self.min[1]) * gain))
        return apply_radial_deadzone(x, y, 0.03)

@dataclass
class ControllerInputData:
    raw_data: bytes
    time: int
    buttons: int
    left_stick: tuple[int, int]
    right_stick: tuple[int, int]
    mouse_coords: tuple[int, int]
    mouse_roughness: int
    mouse_distance: int
    magnometer: tuple[int, int, int]
    battery_voltage: float
    battery_current: float
    temperature: float
    accelerometer: tuple[int, int, int]
    gyroscope: tuple[int, int, int]
    left_trigger: int = 0
    right_trigger: int = 0
    left_trigger_raw: int = 0
    right_trigger_raw: int = 0
    custom_buttons_mask: int = 0

    def __init__(self, data: bytes, left_stick_calibration: StickCalibrationData, right_stick_calibration: StickCalibrationData, product_id: int = 0, gc_trigger_calib: list = None):
        self.raw_data = data
        
        if product_id == NSO_GAMECUBE_CONTROLLER_PID:
            self.time = data[0]
            
            b1 = data[2]
            b2 = data[3]
            b3 = data[4]
            
            buttons_val = 0
            if b1 & 0x01: buttons_val |= 0x00000004 # B
            if b1 & 0x02: buttons_val |= 0x00000008 # A
            if b1 & 0x04: buttons_val |= 0x00000001 # Y
            if b1 & 0x08: buttons_val |= 0x00000002 # X
            if b1 & 0x10: buttons_val |= 0x00000080 # R (digital click) -> map to ZR
            if b1 & 0x20: buttons_val |= 0x00000040 # Z -> map to R
            if b1 & 0x40: buttons_val |= 0x00000200 # Start -> PLUS
            
            if b2 & 0x01: buttons_val |= 0x00010000 # D-Down
            if b2 & 0x02: buttons_val |= 0x00040000 # D-Right
            if b2 & 0x04: buttons_val |= 0x00080000 # D-Left
            if b2 & 0x08: buttons_val |= 0x00020000 # D-Up
            if b2 & 0x10: buttons_val |= 0x00800000 # L (digital click) -> map to ZL
            if b2 & 0x20: buttons_val |= 0x00400000 # ZL -> map to L
            
            if b3 & 0x01: buttons_val |= 0x00001000 # Home
            if b3 & 0x02: buttons_val |= 0x00002000 # Capture
            if b3 & 0x10: buttons_val |= 0x00004000 # Chat (C Button)
            
            self.buttons = buttons_val
            
            self.left_stick = get_stick_xy(data[5:8])
            self.right_stick = get_stick_xy(data[8:11])
            
            if not gc_trigger_calib or len(gc_trigger_calib) < 6:
                if gc_trigger_calib and len(gc_trigger_calib) == 4:
                    # Upgrade from 4 to 6: min, max -> min, max, max
                    gc_trigger_calib = [gc_trigger_calib[0], gc_trigger_calib[1], gc_trigger_calib[1], gc_trigger_calib[2], gc_trigger_calib[3], gc_trigger_calib[3]]
                else:
                    gc_trigger_calib = [36, 190, 240, 36, 190, 240]
            
            mode = getattr(CONFIG, 'gc_trigger_mode', '100% at Bump')
            l_max = gc_trigger_calib[1] if mode == '100% at Bump' else gc_trigger_calib[2]
            r_max = gc_trigger_calib[4] if mode == '100% at Bump' else gc_trigger_calib[5]
                
            def remap_trigger_value(value: int, min_in: int, max_in: int) -> int:
                min_out, max_out = 0, 255
                clamped_value = max(min_in, min(value, max_in))
                if max_in > min_in:
                    percentage = (clamped_value - min_in) / (max_in - min_in)
                else:
                    percentage = 0.0
                return int(percentage * (max_out - min_out)) + min_out
                
            self.left_trigger_raw = data[12] if len(data) > 12 else 0
            self.right_trigger_raw = data[13] if len(data) > 13 else 0
            
            if mode == 'Hair Trigger':
                l_thresh = gc_trigger_calib[0] + (gc_trigger_calib[2] - gc_trigger_calib[0]) * 0.05
                r_thresh = gc_trigger_calib[3] + (gc_trigger_calib[5] - gc_trigger_calib[3]) * 0.05
                self.left_trigger = 255 if self.left_trigger_raw >= l_thresh else 0
                self.right_trigger = 255 if self.right_trigger_raw >= r_thresh else 0
            else:
                self.left_trigger = remap_trigger_value(self.left_trigger_raw, gc_trigger_calib[0], l_max)
                self.right_trigger = remap_trigger_value(self.right_trigger_raw, gc_trigger_calib[3], r_max)
            
            # Map analog triggers to digital ZL/ZR bits if pressed past 50% (128)
            # This ensures Switch 1 mode (which only reads digital bits) gets a responsive trigger
            # without requiring the user to physically bottom-out the controller.
            if self.left_trigger >= 128:
                self.buttons |= 0x00800000 # ZL
            if self.right_trigger >= 128:
                self.buttons |= 0x00000080 # ZR
                
            if mode != '100% at Max':
                if b1 & 0x10: self.buttons |= 0x80000000 # R (digital click) -> GC_R_CLICK
                if b2 & 0x10: self.buttons |= 0x40000000 # L (digital click) -> GC_L_CLICK
            else:
                if b1 & 0x10: self.buttons |= 0x00000080 # R (digital click) -> ZR
                if b2 & 0x10: self.buttons |= 0x00800000 # L (digital click) -> ZL
            
            self.mouse_coords = (0, 0)
            self.mouse_roughness = 0
            self.mouse_distance = 0
            self.magnometer = (0, 0, 0)
            self.battery_voltage = 3.7
            self.battery_current = 0.0
            self.temperature = 25.0
            
            # Explicitly mask out missing physical buttons on the NSO GameCube Controller
            # Missing: MINUS (0x0100), L3 (0x0800), R3 (0x0400), SL/SR etc.
            self.buttons &= ~(0x00000100 | 0x00000400 | 0x00000800)

            # NSO GameCube Protocol: IMU data actually starts at offset 34 based on raw data analysis
            if len(data) >= 46:
                global _gc_debug_counter
                _gc_debug_counter += 1
                if _gc_debug_counter % 125 == 0:
                    import logging
                self.accelerometer = (decodes(data[34:36]), decodes(data[36:38]), decodes(data[38:40]))
                self.gyroscope = (decodes(data[40:42]), decodes(data[42:44]), decodes(data[44:46]))
                self.magnometer = (0, 0, 0)
            else:
                self.accelerometer = (0, 0, 0)
                self.gyroscope = (0, 0, 0)
                self.magnometer = (0, 0, 0)
        else:
            self.time = decodeu(data[0:4])
            self.buttons = decodeu(data[4:8])
            self.left_stick = get_stick_xy(data[10:13])
            self.right_stick = get_stick_xy(data[13:16])
            self.mouse_coords = decodeu(data[16:18]), decodeu(data[18:20])
            self.mouse_roughness = decodeu(data[20:22])
            self.mouse_distance = decodeu(data[22:24])
            self.magnometer = decodes(data[25:27]), decodes(data[27:29]), decodes(data[29:31])
            self.battery_voltage = decodeu(data[31:33]) / 1000.0
            self.battery_current = decodeu(data[33:35]) / 100.0
            self.temperature = 25 + decodeu(data[46:48]) / 127.0
            self.accelerometer = decodes(data[48:50]), decodes(data[50:52]), decodes(data[52:54])
            self.gyroscope = decodes(data[54:56]), decodes(data[56:58]), decodes(data[58:60])

        stick_gain = 1.05 if product_id in (JOYCON_L_PID, JOYCON_R_PID, JOYCON2_LEFT_PID, JOYCON2_RIGHT_PID) else 1.0
        if left_stick_calibration:
            self.left_stick = left_stick_calibration.apply_calibration(self.left_stick, gain=stick_gain)
        if right_stick_calibration:
            self.right_stick = right_stick_calibration.apply_calibration(self.right_stick, gain=stick_gain)
            
    

@dataclass
class ControllerInfo:
    serial_number: str
    vendor_id: int
    product_id: int
    color1: bytes
    color2: bytes
    color3: bytes
    color4: bytes

    def __init__(self, data: bytes):
        self.serial_number = data[2:16].decode()
        self.vendor_id = decodeu(data[18:20])
        self.product_id = decodeu(data[20:22])
        self.color1 = data[25:28]
        self.color2 = data[28:31]
        self.color3 = data[31:34]
        self.color4 = data[34:37]

@dataclass
class VibrationData:
    lf_freq: int = 0x0e1
    lf_en_tone: bool = False
    lf_amp: int = 0x000
    hf_freq: int = 0x1e1
    hf_en_tone : int = False
    hf_amp: int = 0x000

    def get_bytes(self):
        value = 0x0000000000
        value |= (self.lf_freq & 0x1FF)        
        value |= int(self.lf_en_tone) << 9     
        value |= (self.lf_amp & 0x3FF) << 10   
        value |= (self.hf_freq & 0x1FF) << 20  
        value |= int(self.hf_en_tone) << 29    
        value |= (self.hf_amp & 0x3FF) << 30   
        return value.to_bytes(byteorder='little', length=5)

class Controller:
    def __init__(self, device: BLEDevice):
        self.device: BLEDevice = device
        self.client: BleakClient = None
        self.controller_info: ControllerInfo = None
        self.input_report_callback = None
        self.disconnected_callback = None
        self.left_stick_calibration: StickCalibrationData = None
        self.right_stick_calibration: StickCalibrationData = None
        self.previous_mouse_state: MouseState = None
        self.connected_at = None
        self.last_input_time = time.time()

        self.side_buttons_pressed = False
        self.response_future = None
        self.vibration_packet_id = 0
        self.battery_voltage = None
        
        self.gyro_mouse_enabled = False
        self.gr_was_pressed = False
        self.prev_zr = False
        self.prev_zl = False
        
        self.residual_x = 0.0
        self.residual_y = 0.0
        self.smooth_dx = 0.0
        self.smooth_dy = 0.0
        
        self.prev_screenshot = False
        self.prev_key_c = False
        self.last_click_event_time = 0.0
        
        self.gyro_target_vx = 0.0
        self.gyro_target_vy = 0.0
        self._gyro_rstick_out = (0.0, 0.0)
        self.jc_target_vx = 0.0
        self.jc_target_vy = 0.0    
        self.jc_mouse_active = False
        self.current_vx = 0.0
        self.current_vy = 0.0
        self.interp_residual_x = 0.0
        self.interp_residual_y = 0.0
        self.interp_task = None
        self.virtual_controller = None
        
        self.is_calibrating = False
        self.calibration_end_time = 0
        
        self.is_calibration_counting_down = False
        self.calibration_countdown_end = 0.0
        self.last_remaining_sec = None
        self.is_mag_calibration_waiting = False
        self.back_button_calibration_active = False
        self.prev_calibration = False
        
        # Set defaults, will load actual calibration offsets after connecting and getting device info
        self.gyro_bias = (0.0, 0.0, 0.0)
            
        self.calibration_samples_gyro = []
        self.calibration_samples_stick = []
        self.kp_scale_smoothed = 1.0
        self.km_scale_smoothed = 1.0
        self.hold_mode = "Vertical"
        
        # Sensor fusion state
        self.ahrs = imufusion.Ahrs()
        # Convention NWU, gain=0.1, range=2000 dps, accRejection=10 deg, magRejection=20 deg, recoveryTrigger=60000 samples
        self.ahrs.settings = imufusion.Settings(
            imufusion.CONVENTION_NWU,
            0.1,
            2000.0,
            10.0,
            20.0,
            60000
        )
        self.last_fusion_time = 0
        self.gyro_bias_integral = (0.0, 0.0, 0.0)
        self.gyro_start_time = 0
        self.gyro_active_side_prev = False
        self.gyro_steering_origin_accel = None
        
        self.is_mag_calibrating = False
        self.mag_bias = (0.0, 0.0, 0.0)
        self.mag_min = [32767, 32767, 32767]
        self.mag_max = [-32768, -32768, -32768]
        
        self.q_world_offset = None 
        self.gyro_moving_envelope = 0.0
        self._suspended = False
        self.prev_q = None
        
    @property
    def suspended(self):
        return self._suspended
        
    @suspended.setter
    def suspended(self, value):
        self._suspended = value
        if value:
            logger.info(f"Controller {self.device.address}: Input processing SUSPENDED.")
        else:
            logger.info(f"Controller {self.device.address}: Input processing RESUMED.")
            
    @property
    def orientation(self):
        q = self.ahrs.quaternion
        return (q.w, q.x, q.y, q.z)

    @orientation.setter
    def orientation(self, value):
        if value is None:
            self.ahrs.reset()
        
    def __repr__(self):
        return f"{CONTROLER_NAMES[self.controller_info.product_id]} : {self.device.address}"

    def start_calibration(self):
        self.is_calibrating = True
        self.calibration_end_time = time.perf_counter() + 5.0
        self.calibration_samples_gyro = []
        self.calibration_samples_stick = []
        
        logger.info(f"Calibration started for {self.device.address}. Please keep the controller stationary...")
    
    def start_mag_calibration(self):
        self.is_mag_calibrating = True
        self.mag_min = [32767, 32767, 32767]
        self.mag_max = [-32768, -32768, -32768]
        logger.info(f"Magnetometer calibration started for {self.device.address}. Please rotate the controller in all directions...")

    def stop_mag_calibration(self):
        if not self.is_mag_calibrating: return
        self.is_mag_calibrating = False
        
        # Calculate bias as the center of the min/max range
        bx = (self.mag_min[0] + self.mag_max[0]) / 2.0
        by = (self.mag_min[1] + self.mag_max[1]) / 2.0
        bz = (self.mag_min[2] + self.mag_max[2]) / 2.0
        self.mag_bias = (bx, by, bz)
        
        logger.info(f"Magnetometer calibration complete for {self.device.address}. Bias: ({bx:.1f}, {by:.1f}, {bz:.1f})")
        
        # Store in config
        CONFIG.mag_calibration_data[self.device.address] = list(self.mag_bias)
        CONFIG.save_config()

        # Reset orientation filter state to prevent continuous sensor fusion skew/direction issues
        ax, ay, az = getattr(self, 'last_accel', (0.0, 16384.0, 0.0))
        self._reset_orientation_from_accel(ax, ay, az)

    def _handle_calibration_button_pressed(self):
        vc = getattr(self, 'virtual_controller', None)
        if vc and len(vc.controllers) == 2:
            # Find the gyro-active controller in the merged pair
            gyro_ctrl = None
            for c in vc.controllers:
                if getattr(c, 'gyro_active', False):
                    gyro_ctrl = c
                    break
            if not gyro_ctrl:
                gyro_ctrl = self
                
            is_active = (getattr(gyro_ctrl, 'is_calibrating', False) or 
                         getattr(gyro_ctrl, 'is_mag_calibrating', False) or 
                         getattr(gyro_ctrl, 'is_calibration_counting_down', False) or
                         getattr(gyro_ctrl, 'is_mag_calibration_waiting', False))
                         
            if is_active:
                if getattr(gyro_ctrl, 'is_mag_calibration_waiting', False):
                    # Start Mag Calibration ONLY on the gyro active controller!
                    gyro_ctrl.is_mag_calibration_waiting = False
                    gyro_ctrl.start_mag_calibration()
                    show_notification("Switch 2 Controller", "Magnetometer calibration started. Please rotate the controller in all directions (figure-8 pattern), and press the Calibration button again to end.")
                elif getattr(gyro_ctrl, 'is_mag_calibrating', False):
                    # Stop Mag Calibration ONLY on the gyro active controller!
                    gyro_ctrl.stop_mag_calibration()
                    # Clear states on all controllers in the merged pair
                    for c in vc.controllers:
                        c.back_button_calibration_active = False
                        c.is_calibration_counting_down = False
                        c.is_calibrating = False
                        c.is_mag_calibration_waiting = False
                        c.is_mag_calibrating = False
                    show_notification("Switch 2 Controller", "Magnetometer calibration complete! Calibration data saved successfully.")
                else:
                    # Cancel active countdown/gyro calibration on ALL controllers in the merged pair
                    for c in vc.controllers:
                        c.is_calibration_counting_down = False
                        c.is_calibrating = False
                        c.is_mag_calibration_waiting = False
                        c.is_mag_calibrating = False
                        c.back_button_calibration_active = False
                    show_notification("Switch 2 Controller", "Calibration cancelled.")
            else:
                # Start Gyro countdown on BOTH controllers!
                for c in vc.controllers:
                    c.back_button_calibration_active = True
                    c.is_calibration_counting_down = True
                    c.calibration_countdown_end = time.perf_counter() + 5.0
                    c.last_remaining_sec = 5
                show_notification("Switch 2 Controller", "Gyro calibration starts in 5 seconds. Please keep the controllers stationary.")
            
            force_ui_update()
            return

        is_active = (getattr(self, 'is_calibrating', False) or 
                     getattr(self, 'is_mag_calibrating', False) or 
                     getattr(self, 'is_calibration_counting_down', False) or
                     getattr(self, 'is_mag_calibration_waiting', False))
        
        if is_active:
            if getattr(self, 'is_mag_calibration_waiting', False):
                self.is_mag_calibration_waiting = False
                self.start_mag_calibration()
                show_notification("Switch 2 Controller", "Magnetometer calibration started. Please rotate the controller in all directions (figure-8 pattern), and press the Calibration button again to end.")
            elif getattr(self, 'is_mag_calibrating', False):
                self.stop_mag_calibration()
                self.back_button_calibration_active = False
                show_notification("Switch 2 Controller", "Magnetometer calibration complete! Calibration data saved successfully.")
            else:
                self.is_calibration_counting_down = False
                self.is_calibrating = False
                self.is_mag_calibration_waiting = False
                self.back_button_calibration_active = False
                show_notification("Switch 2 Controller", "Calibration cancelled.")
        else:
            self.back_button_calibration_active = True
            self.is_calibration_counting_down = True
            self.calibration_countdown_end = time.perf_counter() + 5.0
            self.last_remaining_sec = 5
            show_notification("Switch 2 Controller", "Gyro calibration starts in 5 seconds. Please keep the controllers stationary.")
        
        force_ui_update()
    
    async def connect_ble(self):
        try:
            if (self.client is not None):
                raise Exception("Already connected")
        
            def disconnected_callback(client: BleakClient):
                if (self.disconnected_callback is not None):
                    asyncio.create_task(self.disconnected_callback(self))
        
            self.client = BleakClient(self.device, disconnected_callback=disconnected_callback)
            await self.client.connect(timeout=20.0)
        
            logger.info(f"Connected to {self.device.address}")
        
        except Exception as e:
            logger.error(f"Error occured during connection phase: {e}")
            if self.client:
                try:
                    await self.client.disconnect()
                except:
                    pass
            raise e

        import sys
        if sys.platform == "win32":
            wd_bluetooth = None
            try:
                import winrt.windows.devices.bluetooth as wd_bluetooth
            except ImportError:
                try:
                    import bleak_winrt.windows.devices.bluetooth as wd_bluetooth
                except ImportError:
                    logger.info("Windows Bluetooth WinRT components not found. Skipping throughput optimization.")

            if wd_bluetooth:
                try:
                    if hasattr(wd_bluetooth, 'BluetoothLEPreferredConnectionParameters'):
                        params = wd_bluetooth.BluetoothLEPreferredConnectionParameters.throughput_optimized
                        native_device = getattr(self.client, "_device", None)
                        if native_device is None and hasattr(self.client, "_backend"):
                            native_device = getattr(self.client._backend, "_device", None)
                        if native_device is None and hasattr(self.client, "_backend"):
                            native_device = getattr(self.client._backend, "_requester", None)

                        if native_device and (hasattr(native_device, 'request_preferred_connection_parameters_async') or hasattr(native_device, 'request_preferred_connection_parameters')):
                            request_result = None
                            if hasattr(native_device, 'request_preferred_connection_parameters_async'):
                                request_result = await native_device.request_preferred_connection_parameters_async(params)
                            elif hasattr(native_device, 'request_preferred_connection_parameters'):
                                request_result = native_device.request_preferred_connection_parameters(params)
                                
                            status_val = getattr(request_result, 'status', getattr(request_result, 'Status', request_result))
                            try:
                                status_name = status_val.name if hasattr(status_val, 'name') else str(status_val)
                            except Exception:
                                status_name = str(status_val)
                                
                            logger.info(f"Controller {self.device.address}: 7.5ms Request Result Status: {status_name}")
                        else:
                            logger.warning(f"Could not extract valid WinRT BluetoothLEDevice for {self.device.address}, optimization skipped.")
                    else:
                        logger.info("ThroughputOptimized not available on this Windows version.")
                except Exception as e:
                    logger.warning(f"Failed to apply ThroughputOptimized (non-fatal): {e}")

    async def initialize(self):
        try:
            # Allow the connection to stabilize
            await asyncio.sleep(0.5)
            
            # Explicit check before starting notification
            if not self.client.is_connected:
                logger.error(f"Device {self.device.address} disconnected before notify")
                raise BleakError("Disconnected during setup")

            self.response_future = None
            def command_response_callback(sender: BleakGATTCharacteristic, data: bytearray):
                future = self.response_future
                if future and not future.done():
                    expected = getattr(self, 'expected_command_id', None)
                    if expected is not None and len(data) > 0 and data[0] != expected:
                        logger.debug(f"Ignoring unexpected command response for cmd {data[0]}, expected {expected}")
                        return
                    try:
                        loop = future.get_loop()
                        loop.call_soon_threadsafe(future.set_result, bytearray(data))
                    except Exception:
                        pass
            
            # Dynamic UUID discovery for SW2 Protocol (e.g. GameCube Controller)
            self.command_write_uuid = COMMAND_WRITE_UUID
            self.command_response_uuid = COMMAND_RESPONSE_UUID
            is_sw2_device = False
            
            for service in self.client.services:
                if "ab7de9be" in str(service.uuid).lower():
                    is_sw2_device = True
                    wnr_chars = []
                    notify_chars = []
                    for char in service.characteristics:
                        props = char.properties
                        if "write-without-response" in props or "write" in props:
                            wnr_chars.append(char)
                        if "notify" in props:
                            notify_chars.append(char)
                    
                    wnr_chars.sort(key=lambda c: c.handle)
                    notify_chars.sort(key=lambda c: c.handle)
                    
                    # For SW2, the command channel is typically the 2nd WriteNoResp char (handle 0x0014)
                    if len(wnr_chars) >= 2:
                        self.command_write_uuid = wnr_chars[1].uuid
                    elif len(wnr_chars) == 1:
                        self.command_write_uuid = wnr_chars[0].uuid
                        
                    # Command response is typically the 3rd Notify char (handle 0x001A)
                    if len(notify_chars) >= 3:
                        self.command_response_uuid = notify_chars[2].uuid
                    elif len(notify_chars) > 0:
                        self.command_response_uuid = notify_chars[-1].uuid
                    
                    logger.info(f"SW2 Service detected. Using Write: {self.command_write_uuid}, Notify: {self.command_response_uuid}")
                    break

            logger.info(f"Starting command response notification for {self.device.address} on {self.command_response_uuid}...")
            for attempt in range(3):
                if not self.client.is_connected:
                    raise BleakError("Connection lost during notify retry")
                try:
                    await self.client.start_notify(self.command_response_uuid, command_response_callback)
                    break
                except Exception as e:
                    if attempt == 2: raise
                    logger.warning(f"Notify failed, retry {attempt+1}: {e}")
                    await asyncio.sleep(2.0)

            if is_sw2_device:
                logger.info(f"Running SW2 Device specific init sequence for {self.device.address}")
                sw2_init_commands = [
                    (0x03, 0x0d, b"\x01\x00\xff\xff\xff\xff\xff\xff"),
                    (0x07, 0x01, b""),
                    (0x16, 0x01, b""),
                    (0x15, 0x03, b"\x00"),
                    # FEATSEL: enable ONLY motion(0x04)+mouse(0x10)+magnetometer(0x80)=0x94,
                    # matching the known-good 0.10.1 build. Enabling all features (0xFF)
                    # turns on extra report fields that make the Joy-Con stream phantom
                    # ZL/ZR bits, firing those triggers continuously.
                    (0x0c, 0x02, b"\x94\x00\x00\x00"),
                    (0x11, 0x03, b""),
                    (0x0a, 0x08, b"\x01\xff\xff\xff\xff\xff\xff\xff\xff\x35\x00\x46\x00\x00\x00\x00\x00\x00\x00\x00"),
                    (0x0c, 0x04, b"\x94\x00\x00\x00"),
                    (0x03, 0x0a, b"\x09\x00\x00\x00"),
                    (0x10, 0x01, b""),
                    (0x01, 0x0c, b""),
                    (0x01, 0x01, b"\x00\x00\x00\x00"),
                    (0x09, 0x07, b"\x01\x00\x00\x00\x00\x00\x00\x00")
                ]
                _sw2_consec_fail = 0
                for cmd_id, subcmd_id, data in sw2_init_commands:
                    try:
                        await self.write_command(cmd_id, subcmd_id, data)
                        _sw2_consec_fail = 0
                        await asyncio.sleep(0.01)
                    except Exception as e:
                        logger.warning(f"SW2 Init command {cmd_id:02x}:{subcmd_id:02x} failed: {e}")
                        _sw2_consec_fail += 1
                        if _sw2_consec_fail >= 3:
                            raise Exception(
                                f"SW2 init aborted: {_sw2_consec_fail} consecutive command failures "
                                f"(last: {cmd_id:02x}:{subcmd_id:02x})"
                            )

            for _ri_attempt in range(3):
                try:
                    self.controller_info = await self.read_controller_info()
                    break
                except Exception as e:
                    if _ri_attempt == 2:
                        raise
                    logger.warning(f"read_controller_info attempt {_ri_attempt + 1} failed: {e}; retrying in 0.5s")
                    await asyncio.sleep(0.5)

            # GameCube AND Joy-Con 2 need input report Format 3 (0x30), like the
            # known-good 0.10.1 build. In the default format the Joy-Con's high
            # status byte (and the Left's bit-23) leak into the button field as
            # phantom ZL/ZR. Format 3 + the 0x03FFFFFF processing mask (non-GameCube)
            # together clear them. Pro Controller 2 streams a compatible format via
            # the SW2 init sequence and is left as-is.
            if self.controller_info.product_id in (
                NSO_GAMECUBE_CONTROLLER_PID, JOYCON2_LEFT_PID, JOYCON2_RIGHT_PID
            ):
                logger.info(f"Setting Input Mode to 0x30 (Format 3) for {self.device.address}")
                try:
                    set_input_mode_cmd = bytearray([
                        0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x03, 0x30
                    ])
                    await self.client.write_gatt_char(self.command_write_uuid, set_input_mode_cmd)
                except Exception as e:
                    logger.warning(f"Failed to set Input Mode: {e}")
            
            # After getting controller info, prioritize loading specific calibration from MAC address
            addr = self.device.address
            if addr in CONFIG.calibration_data:
                self.gyro_bias = tuple(CONFIG.calibration_data[addr])
                logger.info(f"Loaded per-device calibration for {addr}")
            elif self.is_joycon_left():
                self.gyro_bias = tuple(getattr(CONFIG, "gyro_bias_l", [0.0, 0.0, 0.0]))
            else:
                self.gyro_bias = tuple(getattr(CONFIG, "gyro_bias_r", [0.0, 0.0, 0.0]))
                
            mag_cal_data = getattr(CONFIG, "mag_calibration_data", {}) or {}
            if addr in mag_cal_data:
                self.mag_bias = tuple(mag_cal_data[addr])
                logger.info(f"Loaded per-device mag calibration for {addr}")
                
            try:
                self.stick_calibration, self.second_stick_calibration = await self.read_calibration_data()
            except Exception as e:
                logger.warning(f"Failed to read calibration data; using centered defaults: {e}")
                # Use centered defaults rather than None. With None the raw 0-4095 stick
                # value is passed straight through (uncalibrated), which the rest of the
                # pipeline reads as a stick pinned to an extreme -> continuous joystick
                # input. A failed read happens intermittently over the bridge; centered
                # defaults keep the stick neutral until a clean reconnect re-reads it.
                self.stick_calibration = StickCalibrationData(b'')
                self.second_stick_calibration = StickCalibrationData(b'')

            await self.enable_input_notify_callback()
            
            if getattr(self.controller_info, 'product_id', 0) == NSO_GAMECUBE_CONTROLLER_PID:
                await self.enableFeatures(0x27)
            elif not is_sw2_device:
                await self.enableFeatures(FEATURE_MOTION | FEATURE_MOUSE | FEATURE_MAGNOMETER)

            self.interp_running = True
            self.interp_thread = threading.Thread(target=self._interpolation_thread_loop, daemon=True)
            self.interp_thread.start()
        except Exception:
            await self.disconnect()
            raise

        logger.info(f"Successfully initialized {self.device.address} ({self.controller_info.product_id:04x}) : {self.controller_info}")
        self.connected_at = time.time()
        self.last_input_time = time.time()

    async def trigger_connection_haptics(self):
        try:
            bass_thump = VibrationData(lf_freq=0x060, lf_amp=0x350, hf_freq=0x0c0, hf_amp=0x250)
            sharp_click = VibrationData(hf_freq=0x1e2, hf_amp=0x300, lf_amp=0x030)
            stop_vibration = VibrationData() 

            await self.set_vibration(bass_thump, ignore_freq_scaling=True)
            await asyncio.sleep(0.2) 
            
            await self.set_vibration(stop_vibration, ignore_freq_scaling=True)
            await asyncio.sleep(0.01) 
            
            await self.set_vibration(sharp_click, ignore_freq_scaling=True)
            await asyncio.sleep(1.0) 
            
            await self.set_vibration(stop_vibration, ignore_freq_scaling=True)
            logger.info(f"Controller {self.device.address}: Connection haptic feedback triggered.")
        except Exception as e:
            logger.warning(f"Failed to trigger haptic feedback for {self.device.address}: {e}")

    async def connect(self):
        async with BLE_CONNECTION_LOCK:
            await self.connect_ble()
            await asyncio.sleep(0.3) 
            
            await self.initialize()
            await self.trigger_connection_haptics()
            
            await asyncio.sleep(0.1)

    @classmethod
    async def create_from_device(cls, device: BLEDevice):
        controller = cls(device)
        await controller.connect()
        return controller
    
    @classmethod
    async def create_from_mac_address(cls, mac_address):
        device = await BleakScanner.find_device_by_address(mac_address)
        return await cls.create_from_device(device)
        
    async def disconnect(self):
        if not getattr(self, 'interp_running', False) and not self.client:
            return
            
        logger.info(f"Controller {self.device.address}: Suspending interpolation...")
        self.interp_running = False
        
        # Join the interpolation thread if it exists and is running
        if hasattr(self, 'interp_thread') and self.interp_thread.is_alive():
            logger.info(f"Controller {self.device.address}: Joining interpolation thread...")
            self.interp_thread.join(timeout=0.5)
            
        if self.client:
            if self.client.is_connected:
                logger.info(f"Controller {self.device.address}: Disconnecting Bluetooth...")
                try:
                    # Explicitly stop notifications to prevent WinRT background callbacks from firing 
                    # after the event loop is closed, which causes RuntimeError.
                    try:
                        await self.client.stop_notify(INPUT_REPORT_UUID)
                    except Exception:
                        pass
                    try:
                        await self.client.stop_notify(COMMAND_RESPONSE_UUID)
                    except Exception:
                        pass
                        
                    # Faster timeout for sleep-time disconnection
                    await asyncio.wait_for(self.client.disconnect(), timeout=2.0)
                except Exception as e:
                    logger.debug(f"Bluetooth disconnect error (ignored): {e}")
            self.client = None
        logger.info(f"Controller {self.device.address}: Disconnected.")

    ### Commands & Features ###

    # Subclasses (e.g. ESP32S3Controller) can override this to tolerate slower
    # BLE round-trips through the bridge when other controllers are active.
    COMMAND_TIMEOUT: float = 2.0

    async def write_command(self, command_id: int, subcommand_id: int, command_data = b''):
        self.expected_command_id = command_id
        command_buffer = command_id.to_bytes() + b"\x91\x01" + subcommand_id.to_bytes() + b"\x00" + len(command_data).to_bytes() + b"\x00\x00" + command_data
        self.response_future = asyncio.get_running_loop().create_future()
        write_uuid = getattr(self, 'command_write_uuid', COMMAND_WRITE_UUID)
        await self.client.write_gatt_char(write_uuid, command_buffer)
        try:
            response_buffer = await asyncio.wait_for(self.response_future, timeout=self.COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            raise Exception(f"Command response timeout for {command_id}")
            
        if len(response_buffer) < 8 or response_buffer[0] != command_id or response_buffer[1] != 0x01:
            raise Exception(f"Unexpected response : {response_buffer}")
        return response_buffer[8:]

    async def enableFeatures(self, feature_flags: int):
        await self.write_command(COMMAND_FEATURE, SUBCOMMAND_FEATURE_INIT, feature_flags.to_bytes().ljust(4, b'\0'))
        
        if getattr(self, 'controller_info', None) and getattr(self.controller_info, 'product_id', 0) == NSO_GAMECUBE_CONTROLLER_PID:
            try:
                # Command 0x11, SubCmd 0x03
                cmd_11 = bytes([0x11, 0x91, 0x01, 0x03, 0x00, 0x00, 0x00, 0x00])
                await self.client.write_gatt_char(getattr(self, 'command_write_uuid', COMMAND_WRITE_UUID), cmd_11)
                await asyncio.sleep(0.05)
                
                # Command 0x0A, SubCmd 0x08
                cmd_0A = bytes([
                    0x0A, 0x91, 0x01, 0x08, 0x00, 0x14, 0x00, 0x00,
                    0x01, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 
                    0x35, 0x00, 0x46, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
                ])
                await self.client.write_gatt_char(getattr(self, 'command_write_uuid', COMMAND_WRITE_UUID), cmd_0A)
            except Exception as e:
                logger.warning(f"Failed to send GameCube SW2 IMU init sequence: {e}")
                
        await self.write_command(COMMAND_FEATURE, SUBCOMMAND_FEATURE_ENABLE, feature_flags.to_bytes().ljust(4, b'\0'))

    def _bridge_rumble_due(self):
        """Rate-gate continuous rumble for the ESP32-S3 bridge to the BLE connection
        interval (~7.5ms). Non-bridge (WinRT) controllers always return True because
        the OS BLE stack already paces their writes. Each command carries 3 frames
        that cover the interval, so pacing here keeps low latency without flooding the
        firmware's per-interval BLE write (which caused merge-mode rumble stutter)."""
        if not getattr(self, 'is_esp32s3_bridge', False):
            return True
        now_rt = time.perf_counter()
        if (now_rt - getattr(self, '_last_rumble_send_rt', 0.0)) >= 0.0075:
            self._last_rumble_send_rt = now_rt
            return True
        return False

    async def set_vibration(self, vibration: VibrationData, vibration2 = VibrationData(), vibration3 = VibrationData(), ignore_freq_scaling = False, vibration_r1 = None, vibration_r2 = None, vibration_r3 = None):
        # --- TEMP rumble-rate diagnostic (transport-agnostic): logs how many rumble
        # writes/sec each controller actually issues, so WinRT vs ESP32-bridge can be
        # compared directly (same dispatch code, so this isolates rate vs timing). ---
        try:
            _now_r = time.perf_counter()
            self._rumble_diag_count = getattr(self, '_rumble_diag_count', 0) + 1
            if _now_r - getattr(self, '_rumble_diag_t0', 0.0) >= 1.0:
                _side = 'L' if self.is_joycon_left() else ('R' if self.is_joycon_right() else 'P')
                logger.info("RUMBLE-RATE side=%s bridge=%s rate=%d/s",
                            _side, getattr(self, 'is_esp32s3_bridge', False), self._rumble_diag_count)
                self._rumble_diag_count = 0
                self._rumble_diag_t0 = _now_r
        except Exception:
            pass

        strength = getattr(CONFIG, "vibration_strength", 5)
        freq_setting = getattr(CONFIG, "vibration_frequency", 10)
        is_pro = self.is_pro_controller()

        is_switch1 = getattr(CONFIG, "simulation_mode", "PS5") == "Switch1"

        rumble_mode = getattr(CONFIG, "rumble_mode", "Xbox")

        if ignore_freq_scaling:
            lf_multiplier = 1.3
            hf_multiplier = 1.0 if is_pro else 0.6
        elif is_switch1:
            if is_pro:
                if rumble_mode == "Switch":
                    # LF: 2.6, HF: 4.0 (Doubled from 2.0)
                    lf_at_5 = 1.0 * (2.6 / 2.6)
                    hf_at_5 = 1.2 * (4.0 / 4.0)
                    
                    if strength <= 5.0:
                        lf_multiplier = (strength / 5.0) * lf_at_5
                        hf_multiplier = (strength / 5.0) * hf_at_5
                    else:
                        t = (strength - 5.0) / 5.0
                        lf_multiplier = lf_at_5 + (2.6 - lf_at_5) * t
                        hf_multiplier = hf_at_5 + (4.0 - hf_at_5) * t
                    
                    lf_multiplier = min(2.6, lf_multiplier)
                    hf_multiplier = min(4.0, hf_multiplier)
                else:
                    # Align Xbox Rumble to other modes' Xbox Rumble
                    target_lf_mult = (strength / 5.0) * 2.0
                    target_hf_scale = (strength / 5.0) * 1.0
                    
                    target_lf_mult *= (2.6 / 2.6)
                    target_hf_scale *= (2.0 / 2.0)
                    
                    lf_multiplier = min(2.6, target_lf_mult)
                    hf_multiplier = min(2.0, target_hf_scale)
            else:
                if rumble_mode == "Switch":
                    if strength <= 5.0:
                        lf_multiplier = (strength / 5.0) * 0.504
                        hf_multiplier = (strength / 5.0) * 0.336
                    else:
                        t = (strength - 5.0) / 5.0
                        lf_multiplier = 0.504 + (0.84 - 0.504) * t
                        hf_multiplier = 0.336 + (0.56 - 0.336) * t
                    lf_multiplier = min(0.84, lf_multiplier)
                    hf_multiplier = min(0.56, hf_multiplier)
                else:
                    # Align Xbox Rumble for Joy-Con using the same ratio (LF: 1.0x, HF: 0.5x) compared to standard modes
                    target_lf_mult = (strength / 5.0) * 2.0
                    target_hf_scale = (strength / 5.0) * 1.0
                    
                    target_lf_mult *= 1.0
                    target_hf_scale *= 0.5
                    
                    lf_multiplier = min(0.84, target_lf_mult)
                    hf_multiplier = min(0.56, target_hf_scale)
        elif rumble_mode in ("Switch", "PS5"):
            if is_pro:
                if strength <= 5.0:
                    lf_multiplier = (strength / 5.0)
                    hf_multiplier = (strength / 5.0) * 0.6
                else:
                    t = (strength - 5.0) / 5.0
                    lf_multiplier = 1.0 + (2.6 - 1.0) * t
                    hf_multiplier = 0.6 + (2.0 - 0.6) * t
                lf_multiplier = min(2.6, lf_multiplier)
                hf_multiplier = min(2.0, hf_multiplier)
            else:
                if strength <= 5.0:
                    lf_multiplier = (strength / 5.0) * 0.504
                    hf_multiplier = (strength / 5.0) * 0.672
                else:
                    t = (strength - 5.0) / 5.0
                    lf_multiplier = 0.504 + (0.84 - 0.504) * t
                    hf_multiplier = 0.672 + (1.12 - 0.672) * t
                lf_multiplier = min(0.84, lf_multiplier)
                hf_multiplier = min(1.12, hf_multiplier)
        else:
            # Low Frequency mapping based on 0.6.6: default (S=5) behaves like 0.6.6 strength=10 (factor = 2.0)
            target_lf_mult = (strength / 5.0) * 2.0

            # High Frequency mapping based on 0.6.6: default (S=5) behaves like 0.6.6 strength=5 (factor = 1.0)
            target_hf_scale = (strength / 5.0) * 1.0

            # Apply limits from 0.7.1 to prevent physical hardware limitations from being exceeded
            if is_pro:
                # Synchronize with Switch1 maximums
                target_lf_mult *= (2.6 / 2.6)
                target_hf_scale *= (2.0 / 2.0)
                lf_multiplier = min(2.6, target_lf_mult)
                hf_multiplier = min(2.0, target_hf_scale)
            else:
                lf_multiplier = min(0.84, target_lf_mult) # 0.84 is the Joy-Con LF upper limit (based on S=7)
                hf_multiplier = min(1.12, target_hf_scale)

        # LF frequency is constant (freq_factor_lf = 1.0) so frequency slider has no effect
        freq_factor_lf = 1.0
        # HF default (F=10) behaves like 0.6.6 frequency=5 (factor = 4/9)
        freq_factor_hf = (freq_setting - 1) * 4 / 81.0

        def scale_and_clamp(v: VibrationData) -> VibrationData:
            is_switch1 = getattr(CONFIG, "simulation_mode", "PS5") == "Switch1"

            is_pure_switch_rumble = rumble_mode in ("Switch", "PS5")

            if ignore_freq_scaling or (is_pure_switch_rumble and not is_switch1):
                scaled_lf_freq = min(511, max(1, int(v.lf_freq)))
                scaled_hf_freq = min(511, max(1, int(v.hf_freq)))
                lf_mask = 1.0
                hf_mask = 1.0
            else:
                if is_pure_switch_rumble:
                    scaled_lf_freq = min(511, max(1, int(v.lf_freq)))
                    scaled_hf_freq = min(511, max(1, int(v.hf_freq)))
                    
                    if is_switch1 and is_pro:
                        # Artificial Frequency Expander: Switch OS compresses Pro Controller frequencies.
                        # Map 0-24% frequency evenly to 0-100% (0-511). Cap at 511.
                        scaled_hf_freq = min(511, int(scaled_hf_freq * 4.167))
                        
                        # Map 98% LF (approx 501) to Xbox Rumble LF (0x0e1 / 225) to deepen bass within limits
                        if scaled_lf_freq <= 501:
                            scaled_lf_freq = 0x0e1
                        else:
                            scaled_lf_freq = min(511, int(0x0e1 + ((scaled_lf_freq - 501) / 10.0) * (511 - 0x0e1)))
                    elif is_switch1 and not is_pro:
                        # Joy-Con Frequency:
                        # HF: Map 0-73% frequency evenly to 0-100% (0-511). Cap at 511.
                        scaled_hf_freq = min(511, int(scaled_hf_freq * 1.369863))
                        
                        # LF: Map 0-100% (0-511) evenly to the new 0-100% output range (225-511). Cap at 511.
                        scaled_lf_freq = 225 + (scaled_lf_freq / 511.0) * (511 - 225)
                        scaled_lf_freq = min(511, max(225, int(scaled_lf_freq)))
                else:
                    # 1. First apply the frequency expansion mask (if Pro)
                    if is_pro:
                        expanded_lf_freq = v.lf_freq
                        expanded_hf_freq = v.hf_freq
                        
                        # Map 98% LF (approx 501) to Xbox Rumble LF (0x0e1 / 225) to deepen bass within limits
                        if expanded_lf_freq <= 501:
                            expanded_lf_freq = 0x0e1
                        else:
                            expanded_lf_freq = min(511, int(0x0e1 + ((expanded_lf_freq - 501) / 10.0) * (511 - 0x0e1)))
                    else:
                        expanded_lf_freq = v.lf_freq
                        expanded_hf_freq = v.hf_freq
                        
                    # 2. Then apply the Xbox Rumble frequency reduction mask on top of the expanded frequencies

                    new_min_lf = 0.5 * expanded_lf_freq + 0.5
                    temp_lf_freq = new_min_lf + (expanded_lf_freq - new_min_lf) * freq_factor_lf

                    new_min_hf = 0.5 * expanded_hf_freq + 0.5
                    temp_hf_freq = new_min_hf + (expanded_hf_freq - new_min_hf) * freq_factor_hf

                    # Clamped actual output frequencies
                    scaled_lf_freq = min(511, max(1, int(temp_lf_freq)))
                    scaled_hf_freq = min(511, max(1, int(temp_hf_freq)))

                # LF (large motor thumps) is not targeted for high-frequency small motor simulation masking
                lf_mask = 1.0

                # Determine if the target is mid-high frequency simulating small motor (using original frequency v.hf_freq)
                if v.hf_freq > 0 and not (not is_pro and is_switch1 and is_pure_switch_rumble):
                    # Calculate the dynamic range limits of the high-frequency channel
                    # Upper limit (for max hf_freq = 511) when slider is at maximum F=10 (non-stretching):
                    freq_factor_hf_at_10 = 4.0 / 9.0
                    max_hf_freq = min(511, max(1, int(256.0 + 255.0 * freq_factor_hf_at_10)))
                    # Lower limit is the current output low frequency (scaled_lf_freq)
                    min_hf_freq = scaled_lf_freq

                    denom = max(1.0, max_hf_freq - min_hf_freq)
                    hf_mapped = 1.0 + ((scaled_hf_freq - min_hf_freq) / denom) * 9.0
                    hf_mapped = min(10.0, max(1.0, hf_mapped))

                    # Calculate base mask values at Strength=5 (F=1 -> 0.25, F=5 -> 0.1, F=10 -> 0.24 for Pro; F=1 -> 0.19, F=5 -> 0.095, F=10 -> 0.06 for Joy-Con)
                    if is_pro:
                        if hf_mapped <= 5.0:
                            mask_at_5 = 0.25 - 0.0375 * (hf_mapped - 1.0)
                        else:
                            mask_at_5 = 0.1 + 0.028 * (hf_mapped - 5.0)
                    else:
                        if hf_mapped <= 5.0:
                            mask_at_5 = 0.19 - 0.02375 * (hf_mapped - 1.0)
                        else:
                            mask_at_5 = 0.095 + 0.00164 * (hf_mapped - 5.0)

                    # Calculate target mask values at Strength=10 (F=1 -> 0.34375, F=5 -> 0.1375, F=10 -> 0.33 for Pro; F=1 -> 0.8, F=5 -> 0.4, F=10 -> 0.4 for Joy-Con)
                    if is_pro:
                        if hf_mapped <= 5.0:
                            mask_at_10 = 0.34375 - 0.0515625 * (hf_mapped - 1.0)
                        else:
                            mask_at_10 = 0.1375 + 0.0385 * (hf_mapped - 5.0)
                    else:
                        if hf_mapped <= 5.0:
                            mask_at_10 = 0.391875 - 0.048984375 * (hf_mapped - 1.0)
                        else:
                            mask_at_10 = 0.1959375 + 0.0088125 * (hf_mapped - 5.0)

                    # Linearly interpolate mask between Strength=5 and Strength=10 curves
                    if strength >= 5:
                        t = (strength - 5.0) / 5.0
                        t = min(1.0, max(0.0, t))
                        hf_mask = mask_at_5 + (mask_at_10 - mask_at_5) * t
                    else:
                        hf_mask = mask_at_5
                        
                    # Scale the mask for the new intensity limits
                    if is_pro:
                        if is_switch1 and is_pure_switch_rumble:
                            hf_mask *= 2.0  # 4.0 / 2.0
                        elif is_switch1 or not is_pure_switch_rumble:
                            hf_mask *= 1.0  # 2.0 / 2.0
                    elif not is_pro and is_switch1:
                        hf_mask *= 1.0
                else:
                    hf_mask = 1.0

            scaled_lf = min(1023, max(0, int(v.lf_amp * lf_multiplier * lf_mask)))
            scaled_hf = min(1023, max(0, int(v.hf_amp * hf_multiplier * hf_mask)))
            
            return VibrationData(
                lf_freq=scaled_lf_freq,
                lf_en_tone=v.lf_en_tone,
                lf_amp=scaled_lf,
                hf_freq=scaled_hf_freq,
                hf_en_tone=v.hf_en_tone,
                hf_amp=scaled_hf
            )

        v1 = scale_and_clamp(vibration)
        v2 = scale_and_clamp(vibration2)
        v3 = scale_and_clamp(vibration3)

        motor_vibrations = (0x50 + (self.vibration_packet_id & 0x0F)).to_bytes(1, 'little') + v1.get_bytes() + v2.get_bytes() + v3.get_bytes()
        
        try:
            if getattr(self.controller_info, 'product_id', 0) == NSO_GAMECUBE_CONTROLLER_PID:
                # Use the SW2 command channel for GameCube rumble instead of hardcoded 0x0012
                uuid_to_use = getattr(self, 'command_write_uuid', COMMAND_WRITE_UUID)
                is_on = vibration.lf_amp > 0 or vibration.hf_amp > 0
                payload = bytearray([0x0A, 0x91, 0x01, 0x02, 0x00, 0x04, 0x00, 0x00, 0x01 if is_on else 0x00, 0x00, 0x00, 0x00])
            else:
                uuid_to_use = VIBRATION_WRITE_PRO_CONTROLLER_UUID if self.is_pro_controller() else (
                    VIBRATION_WRITE_JOYCON_L_UUID if self.is_joycon_left() else VIBRATION_WRITE_JOYCON_R_UUID
                )
                
                if self.is_pro_controller() and vibration_r1 is not None:
                    v1_r = scale_and_clamp(vibration_r1)
                    v2_r = scale_and_clamp(vibration_r2)
                    v3_r = scale_and_clamp(vibration_r3)
                    motor_vibrations_r = (0x50 + (self.vibration_packet_id & 0x0F)).to_bytes(1, 'little') + v1_r.get_bytes() + v2_r.get_bytes() + v3_r.get_bytes()
                    # The Pro controller expects Right side data first, then Left side data
                    # (Standard Nintendo Switch protocol expects Right Rumble then Left Rumble)
                    payload = b'\x00' + motor_vibrations_r + motor_vibrations
                else:
                    payload = (b'\x00' + motor_vibrations + motor_vibrations) if self.is_pro_controller() else (b'\x00' + motor_vibrations)

                # ESP32 bridge + merged Joy-Con pair rumble routing.
                # Default "shadow" mode pushes the latest payload to the firmware
                # rumble shadow; a firmware task re-sends it to BLE at a steady,
                # hardware-timed cadence (no Windows/asyncio jitter, no per-host
                # re-send loop).  Other modes are kept for A/B testing:
                #   "shadow" (default) ??firmware-driven sustain (smoothest)
                #   "pair" / "mirror"  ??host-driven wrpair dispatcher
                #   "single"           ??direct per-controller write (original)
                shared = getattr(self, 'shared_client', None)
                if (not self.is_pro_controller()
                        and getattr(self, 'is_esp32s3_bridge', False)
                        and getattr(self, 'is_merged', False)
                        and shared is not None):

                    # RUMBLE-SUBMIT diagnostics (per-controller, per-second)
                    try:
                        import time as _t
                        _now_sub = _t.perf_counter()
                        self._sub_n = getattr(self, '_sub_n', 0) + 1
                        if _now_sub - getattr(self, '_sub_t0', 0.0) >= 1.0:
                            from esp32_rumble_dispatcher import is_active_rumble_payload as _iap
                            _side = 'L' if self.is_joycon_left() else 'R'
                            logger.info(
                                "RUMBLE-SUBMIT side=%s total=%d/s len=%d active=%s",
                                _side, self._sub_n, len(payload),
                                _iap(payload),
                            )
                            self._sub_n = 0
                            self._sub_t0 = _now_sub
                    except Exception:
                        pass

                    try:
                        _pair_mode = getattr(__import__('config', fromlist=['CONFIG']).CONFIG,
                                             'esp32_bridge_pair_mode', 'shadow')
                    except Exception:
                        _pair_mode = 'shadow'

                    if _pair_mode == 'single':
                        # Fall through to direct write below for A/B comparison.
                        pass
                    elif _pair_mode == 'shadow':
                        # Firmware-driven sustain: just push the latest payload.
                        shared.send_rumble_shadow(self.channel, payload)
                        self.vibration_packet_id += 1
                        return
                    else:
                        dispatcher = shared.get_or_create_rumble_dispatcher()
                        if not dispatcher._running:
                            dispatcher.start()
                        if self.is_joycon_left():
                            dispatcher.submit_left(self.channel, uuid_to_use, payload)
                        else:
                            dispatcher.submit_right(self.channel, uuid_to_use, payload)
                        self.vibration_packet_id += 1
                        return

            await self.client.write_gatt_char(uuid_to_use, payload, response=False)
        except Exception as e:
            logger.debug(f"Vibration write failed: {e}")
            
        self.vibration_packet_id += 1

    async def set_leds(self, player_number: int, reversed=False):
        if player_number > 8: player_number = 8
        value = LED_PATTERN[player_number]
        if reversed: value = reverse_bits(value, 4)
        data = value.to_bytes().ljust(4, b'\0')
        await self.write_command(COMMAND_LEDS, SUBCOMMAND_LEDS_SET_PLAYER, data)

    async def play_vibration_preset(self, preset_id: int):
        await self.write_command(COMMAND_VIBRATION, SUBCOMMAND_VIBRATION_PLAY_PRESET, preset_id.to_bytes().ljust(4, b'\0'))

    async def read_memory(self, length: int, address: int):
        if length > 0x4F: raise Exception("Maximum read size is 0x4F bytes")
        data = await self.write_command(COMMAND_MEMORY, SUBCOMMAND_MEMORY_READ, length.to_bytes() + b'\x7e\0\0' + address.to_bytes(length=4,byteorder='little'))
        if (data[0] != length or decodeu(data[4:8]) != address):
            raise Exception(f"Unexpected response from read commmand : {data}")
        return data[8:]

    async def read_controller_info(self):
        info = await self.read_memory(0x40, ADDRESS_CONTROLLER_INFO)
        return ControllerInfo(info)

    async def read_calibration_data(self):
        calibration_data_1 = await self.read_memory(0x0b, CALIBRATION_USER_JOYSTICK_1)
        if (decodeu(calibration_data_1[:3]) == 0xFFFFFF):
            calibration_data_1 = await self.read_memory(0x0b, CALIBRATION_JOYSTICK_1)
        calibration_data_2 = await self.read_memory(0x0b, CALIBRATION_USER_JOYSTICK_2)
        if (decodeu(calibration_data_2[:3]) == 0xFFFFFF):
            calibration_data_2 = await self.read_memory(0x0b, CALIBRATION_JOYSTICK_2)

        if self.is_joycon_left():
            return StickCalibrationData(calibration_data_1), None
        if self.is_joycon_right():
            return None, StickCalibrationData(calibration_data_1)
        return StickCalibrationData(calibration_data_1), StickCalibrationData(calibration_data_2)

    async def pair(self, host_mac_value=None):
        # host_mac_value lets callers pair the controller to a host other than the
        # local PC Bluetooth adapter ??the ESP32-S3 bridge passes its own BLE MAC so
        # the controller bonds to the bridge and reconnects to it on a button press.
        if host_mac_value is None:
            from utils import get_local_mac_value
            host_mac_value = get_local_mac_value()
        mac_value = host_mac_value
        await self.write_command(COMMAND_PAIR, SUBCOMMAND_PAIR_SET_MAC,b"\x00\x02" +  mac_value.to_bytes(6, 'little') + mac_value.to_bytes(6, 'little'))
        ltk1 = bytes([0x00, 0xea, 0xbd, 0x47, 0x13, 0x89, 0x35, 0x42, 0xc6, 0x79, 0xee, 0x07, 0xf2, 0x53, 0x2c, 0x6c, 0x31])
        await self.write_command(COMMAND_PAIR, SUBCOMMAND_PAIR_LTK1, ltk1)
        ltk2 = bytes([0x00, 0x40, 0xb0, 0x8a, 0x5f, 0xcd, 0x1f, 0x9b, 0x41, 0x12, 0x5c, 0xac, 0xc6, 0x3f, 0x38, 0xa0, 0x73])
        await self.write_command(COMMAND_PAIR, SUBCOMMAND_PAIR_LTK2, ltk2)
        await self.write_command(COMMAND_PAIR, SUBCOMMAND_PAIR_FINISH, b'\0')

    async def enable_input_notify_callback(self):
        def input_report_callback(sender, data):
            if getattr(self, 'suspended', False) or getattr(self, '_is_suspending', False):
                return

            # --- TEMP input-rate diagnostic: how many input reports/sec reach this
            # controller's callback (compare WinRT vs bridge; pairs with RUMBLE-RATE). ---
            try:
                _now_i = time.perf_counter()
                self._input_diag_count = getattr(self, '_input_diag_count', 0) + 1
                if _now_i - getattr(self, '_input_diag_t0', 0.0) >= 1.0:
                    _side_i = 'L' if self.is_joycon_left() else ('R' if self.is_joycon_right() else 'P')
                    logger.info("INPUT-RATE side=%s bridge=%s rate=%d/s",
                                _side_i, getattr(self, 'is_esp32s3_bridge', False), self._input_diag_count)
                    self._input_diag_count = 0
                    self._input_diag_t0 = _now_i
            except Exception:
                pass

            # Debug log for the first few packets to see what's being sent on wake
            if not hasattr(self, '_packet_count'): self._packet_count = 0
            if self._packet_count < 5:
                self._packet_count += 1
                pid = getattr(self.controller_info, 'product_id', 0)
                log_fn = logger.info if pid in (JOYCON2_LEFT_PID, JOYCON2_RIGHT_PID) else logger.debug
                log_fn(f"[{time.strftime('%H:%M:%S')}] Controller {self.device.address} pid=0x{pid:04x} pkt#{self._packet_count} raw[0:16]={to_hex(data[0:16])}")

            gc_trigger_calib = getattr(CONFIG, 'gc_trigger_calibration_data', {}).get(self.device.address, [36, 190, 240, 36, 190, 240])
            inputData = ControllerInputData(data, self.stick_calibration, self.second_stick_calibration, getattr(self.controller_info, 'product_id', 0), gc_trigger_calib)

            # Connection settle gate: right after (re)connection the controller can
            # emit transient/garbage frames (or the wake button is still held), which
            # would otherwise be forwarded as real input the instant we start
            # listening ??firing mapped actions like screenshot or spamming keys
            # (IME freeze). Ignore input until the first neutral (no-buttons) frame
            # arrives or a short timeout elapses, establishing a clean baseline.
            if not getattr(self, '_input_settled', True):
                phys_buttons = inputData.buttons & 0x03FFFFFF
                if phys_buttons == 0 or time.time() >= getattr(self, '_input_settle_deadline', 0):
                    self._input_settled = True
                else:
                    return

            self.last_input_data = inputData

            # Reset inactivity timer if there is physical input change
            current_buttons = inputData.buttons & 0x03FFFFFF
            if not hasattr(self, '_prev_idle_buttons'):
                self._prev_idle_buttons = current_buttons
                self._prev_idle_lx = inputData.left_stick[0]
                self._prev_idle_ly = inputData.left_stick[1]
                self._prev_idle_rx = inputData.right_stick[0]
                self._prev_idle_ry = inputData.right_stick[1]
                self.last_input_time = time.time()
            elif current_buttons != self._prev_idle_buttons or \
                 abs(inputData.left_stick[0] - self._prev_idle_lx) > 0.05 or \
                 abs(inputData.left_stick[1] - self._prev_idle_ly) > 0.05 or \
                 abs(inputData.right_stick[0] - self._prev_idle_rx) > 0.05 or \
                 abs(inputData.right_stick[1] - self._prev_idle_ry) > 0.05:
                
                self.last_input_time = time.time()
                self._prev_idle_buttons = current_buttons
                self._prev_idle_lx = inputData.left_stick[0]
                self._prev_idle_ly = inputData.left_stick[1]
                self._prev_idle_rx = inputData.right_stick[0]
                self._prev_idle_ry = inputData.right_stick[1]

            self.battery_voltage = inputData.battery_voltage
            self.last_accel = inputData.accelerometer

            is_left = self.is_joycon_left()
            is_right = self.is_joycon_right()
            is_pro = self.is_pro_controller()
            raw_left_pressed  = bool(inputData.buttons & 0x01)
            raw_up_pressed    = bool(inputData.buttons & 0x02)
            raw_down_pressed  = bool(inputData.buttons & 0x04)
            raw_right_pressed = bool(inputData.buttons & 0x08)

            # Filter out virtual/garbage bits. The top byte of a Joy-Con/Pro report is a
            # STATUS byte (e.g. 0xE0), so bits 24-31 must be discarded (0x03FFFFFF) ??as
            # in 0.10.1 ??or they leak in as phantom GC_L/R_CLICK (0x40000000/0x80000000)
            # which the mapping turns into permanent ZL/ZR. Only the NSO GameCube
            # controller legitimately uses bits 30/31 (its digital trigger clicks), so it
            # keeps the wider 0xC3FFFFFF mask.
            if self.controller_info.product_id == NSO_GAMECUBE_CONTROLLER_PID:
                inputData.buttons &= 0xC3FFFFFF
            else:
                inputData.buttons &= 0x03FFFFFF
            self.raw_buttons = inputData.buttons

            if not getattr(self, 'is_calibrating', False) and not getattr(self, 'is_mag_calibrating', False):
                self.simulate_mouse(inputData)

            # 9-Axis continuous sensor fusion and stabilized gyro synthesis
            if not getattr(self, 'is_calibrating', False) and not getattr(self, 'is_mag_calibrating', False) and not getattr(self, 'is_calibration_counting_down', False) and not getattr(self, 'is_mag_calibration_waiting', False):
                bx, by, bz = self.gyro_bias
                raw_gx, raw_gy, raw_gz = inputData.gyroscope
                gyro_x = raw_gx - bx
                gyro_y = raw_gy - by
                gyro_z = raw_gz - bz

                now = time.perf_counter()
                if getattr(self, 'last_fusion_time', 0) == 0:
                    dt = 0.015
                else:
                    dt = now - self.last_fusion_time
                self.last_fusion_time = now
                if dt < 1e-5:
                    dt = 0.015
                self._last_dt = dt

                ax, ay, az = inputData.accelerometer
                self.true_accel = (ax, ay, az)
                mx, my, mz = inputData.magnometer
                self._mahony_update(gyro_x, gyro_y, gyro_z, ax, ay, az, mx, my, mz, dt)

            btn_states = {
                "GL": bool(inputData.buttons & 0x02000000) if is_pro else False,
                "GR": bool(inputData.buttons & 0x01000000) if is_pro else False,
                "C":  bool(inputData.buttons & 0x00004000),
                "HOME": bool(inputData.buttons & 0x00001000),
                "CAPT": bool(inputData.buttons & 0x00002000),
                "SL_L": bool(inputData.buttons & 0x00200000) if is_left else False,
                "SR_L": bool(inputData.buttons & 0x00100000) if is_left else False,
                "SL_R": bool(inputData.buttons & 0x00000020) if is_right else False,
                "SR_R": bool(inputData.buttons & 0x00000010) if is_right else False,
                "GC_L_CLICK": bool(inputData.buttons & 0x40000000),
                "GC_R_CLICK": bool(inputData.buttons & 0x80000000),
                "PLUS": bool(inputData.buttons & SWITCH_BUTTONS["PLUS"]),
                "MINUS": bool(inputData.buttons & SWITCH_BUTTONS["MINUS"]),
                "A": raw_right_pressed,
                "B": raw_down_pressed,
                "X": raw_up_pressed,
                "Y": raw_left_pressed,
                "UP": bool(inputData.buttons & SWITCH_BUTTONS["UP"]),
                "DOWN": bool(inputData.buttons & SWITCH_BUTTONS["DOWN"]),
                "LEFT": bool(inputData.buttons & SWITCH_BUTTONS["LEFT"]),
                "RIGHT": bool(inputData.buttons & SWITCH_BUTTONS["RIGHT"]),
                "ZL": bool(inputData.buttons & SWITCH_BUTTONS["ZL"]),
                "L": bool(inputData.buttons & SWITCH_BUTTONS["L"]),
                "ZR": bool(inputData.buttons & SWITCH_BUTTONS["ZR"]),
                "R": bool(inputData.buttons & SWITCH_BUTTONS["R"]),
                "L_STK": bool(inputData.buttons & SWITCH_BUTTONS["L_STK"]),
                "R_STK": bool(inputData.buttons & SWITCH_BUTTONS["R_STK"]),
            }
            self._profile_combo_btn_states = dict(btn_states)
            try:
                utils.record_profile_combo_controller_buttons(btn_states)
            except Exception:
                pass

            # Manual Change Profile selection: while active (and while "draining" after
            # confirm/cancel until A/B is released), read navigation from this controller
            # and suppress all virtual output (neutral report). Draining prevents the
            # confirm/cancel A/B press from leaking into the virtual controller.
            if utils.profile_selection_active or getattr(self, "_ps_drain", False):
                self._handle_profile_selection_input(inputData, btn_states, utils.profile_selection_active)
                inputData.buttons = 0
                inputData.left_stick = (0.0, 0.0)
                inputData.right_stick = (0.0, 0.0)
                inputData.gyroscope = (0.0, 0.0, 0.0)
                inputData.accelerometer = (0.0, 0.0, 0.0)
                try:
                    inputData.left_trigger = 0
                    inputData.right_trigger = 0
                except Exception:
                    pass
                if self.input_report_callback is not None:
                    self.input_report_callback(inputData, self)
                return
            elif getattr(self, "_ps_was_active", False):
                self._ps_was_active = False

            inputData.buttons &= ~(0x03FFFFFF)
            if self.controller_info.product_id == NSO_GAMECUBE_CONTROLLER_PID:
                inputData.buttons &= ~(0xC0000000)

            trigger_gyro = False
            trigger_djg = False
            trigger_screenshot = False
            trigger_key_c = False
            trigger_game_bar = False
            trigger_hdr_toggle = False
            trigger_sys_manager = False
            trigger_change_profile_btn = False

            mapping_pairs = [
                # (is_pressed, mapping_key, original_bit, default_action, btn_id)
                (btn_states["GL"], "gl", 0x02000000, None, "gl"),
                (btn_states["GR"], "gr", 0x01000000, None, "gr"),
                (btn_states["HOME"], "home", 0x00001000, "Home", "home"),
                (btn_states["CAPT"], "capt", 0x00002000, "Capture" if getattr(CONFIG, "simulation_mode", "PS5") in ("Switch1", "Switch2") else "PrtSc", "capt"),
                (btn_states["C"], "c", 0x00004000, "Chat" if getattr(CONFIG, "simulation_mode", "PS5") == "Switch2" else "Mute", "c"),
                (btn_states["SL_L"], "sll", 0x00200000, None, "sll"),
                (btn_states["SR_L"], "srl", 0x00100000, None, "srl"),
                (btn_states["SL_R"], "slr", 0x00000020, None, "slr"),
                (btn_states["SR_R"], "srr", 0x00000010, None, "srr"),
                (btn_states["GC_L_CLICK"], "gc_l_click", 0x40000000, "ZL", "gc_l_click"),
                (btn_states["GC_R_CLICK"], "gc_r_click", 0x80000000, "ZR", "gc_r_click"),
                (btn_states["PLUS"], "plus", SWITCH_BUTTONS["PLUS"], None, "plus"),
                (btn_states["MINUS"], "minus", SWITCH_BUTTONS["MINUS"], None, "minus"),
                (btn_states["A"], "a", SWITCH_BUTTONS["A"], None, "a"),
                (btn_states["B"], "b", SWITCH_BUTTONS["B"], None, "b"),
                (btn_states["X"], "x", SWITCH_BUTTONS["X"], None, "x"),
                (btn_states["Y"], "y", SWITCH_BUTTONS["Y"], None, "y"),
                (btn_states["UP"], "up", SWITCH_BUTTONS["UP"], None, "up"),
                (btn_states["DOWN"], "down", SWITCH_BUTTONS["DOWN"], None, "down"),
                (btn_states["LEFT"], "left", SWITCH_BUTTONS["LEFT"], None, "left"),
                (btn_states["RIGHT"], "right", SWITCH_BUTTONS["RIGHT"], None, "right"),
                (btn_states["ZL"], "zl", SWITCH_BUTTONS["ZL"], None, "zl"),
                (btn_states["L"], "l", SWITCH_BUTTONS["L"], None, "l"),
                (btn_states["ZR"], "zr", SWITCH_BUTTONS["ZR"], None, "zr"),
                (btn_states["R"], "r", SWITCH_BUTTONS["R"], None, "r"),
                (btn_states["L_STK"], "l_stk", SWITCH_BUTTONS["L_STK"], None, "l_stk"),
                (btn_states["R_STK"], "r_stk", SWITCH_BUTTONS["R_STK"], None, "r_stk"),
            ]

            def _profile_combo_token_pressed(token):
                if not token:
                    return False
                if token.startswith("BTN_"):
                    name = token[4:]
                    aliases = {
                        "Capture": "CAPT",
                        "PLUS": "PLUS",
                        "MINUS": "MINUS",
                    }
                    name = aliases.get(name, name)
                    return bool(profile_combo_btn_states.get(name, False))
                if token.startswith("VK_"):
                    name = token[3:]
                    try:
                        if len(name) == 1:
                            vk = ord(name)
                        else:
                            vk = getattr(win32con, f"VK_{name}", None)
                        return bool(vk and (win32api.GetAsyncKeyState(vk) & 0x8000))
                    except Exception:
                        return False
                if token.startswith("MB_"):
                    try:
                        btn_num = int(token[3:])
                    except ValueError:
                        return False
                    vk_map = {1: win32con.VK_LBUTTON, 2: win32con.VK_RBUTTON, 3: win32con.VK_MBUTTON}
                    vk = vk_map.get(btn_num)
                    try:
                        return bool(vk and (win32api.GetAsyncKeyState(vk) & 0x8000))
                    except Exception:
                        return False
                return False

            def _profile_combo_pressed(value):
                if not value:
                    return False
                if value.startswith("Custom[Tap]:"):
                    value = value[12:]
                elif value.startswith("Custom[Hold]:"):
                    value = value[13:]
                elif value.startswith("Custom:"):
                    value = value[7:]
                tokens = [token for token in value.split("+") if token]
                return bool(tokens) and all(_profile_combo_token_pressed(token) for token in tokens)

            profile_combo_trigger = getattr(CONFIG, "profile_switching_combo_trigger", "")
            profile_combo_target = None
            vc = getattr(self, "virtual_controller", None)
            profile_combo_btn_states = btn_states
            profile_combo_signature_owner = self
            if vc and len(getattr(vc, "controllers", [])) == 2:
                merged_profile_states = {}
                for c in getattr(vc, "controllers", []):
                    states = btn_states if c is self else getattr(c, "_profile_combo_btn_states", {})
                    for key, pressed in states.items():
                        merged_profile_states[key] = bool(merged_profile_states.get(key, False) or pressed)
                profile_combo_btn_states = merged_profile_states
                profile_combo_signature_owner = vc
            if _profile_combo_pressed(profile_combo_trigger):
                for profile_name, profile_data in getattr(CONFIG, "profiles", {}).items():
                    combo_value = profile_data.get("profile_switching_combo", "")
                    if profile_name != getattr(CONFIG, "active_profile", "") and _profile_combo_pressed(combo_value):
                        profile_combo_target = profile_name
                        break
            if profile_combo_target:
                signature = (profile_combo_trigger, profile_combo_target)
                if getattr(profile_combo_signature_owner, "_prev_profile_combo_signature", None) != signature:
                    trigger_switch_profile(profile_combo_target)
                profile_combo_signature_owner._prev_profile_combo_signature = signature
            else:
                profile_combo_signature_owner._prev_profile_combo_signature = None

            if not hasattr(self, 'active_custom_keys'):
                self.active_custom_keys = {}
            if not hasattr(self, 'active_custom_mouse_wheel'):
                self.active_custom_mouse_wheel = {}

            def get_base_mapping_action(mapping_key):
                if mapping_key == "gc_l_click" and getattr(CONFIG, "gc_trigger_mode", "100% at Bump") == "100% at Max":
                    return "ZL"
                if mapping_key == "gc_r_click" and getattr(CONFIG, "gc_trigger_mode", "100% at Bump") == "100% at Max":
                    return "ZR"
                return CONFIG.get_mapping_setting(mapping_key, "Default")

            # Single pre-pass over the base (Controller Mapping) actions to resolve both
            # the In-app Gyro activation trigger and the "Mode Shift" back button state.
            # The Mode Shift trigger lives in the base layer so it can switch INTO the
            # shifted layer; it supports Hold (while held) and Tap (toggle).
            # Tap and Hold share one armed pool so every Mode Shift button can
            # participate in the same enter/exit state machine.
            trigger_gyro = False
            mode_shift_hold_pressed = False
            if not hasattr(self, "_mode_shift_armed"):
                self._mode_shift_armed = set(getattr(self, "_mode_shift_tap_held", set()))
            mode_shift_pressed_ids = set()
            mode_shift_tap_edge = False
            for is_pressed, mapping_key, _ms_bit, default_action, btn_id in mapping_pairs:
                base_action = get_base_mapping_action(mapping_key)
                base_resolved = default_action if base_action == "Default" else base_action
                if is_pressed and base_resolved in ("Gyro", "In-app Gyro"):
                    trigger_gyro = True
                if isinstance(base_resolved, str) and base_resolved.startswith("Custom") and base_resolved.endswith(":" + MODE_SHIFT_TOKEN):
                    if is_pressed:
                        mode_shift_pressed_ids.add(btn_id)
                    if base_resolved.startswith("Custom[Tap]:"):
                        if is_pressed and btn_id not in self._mode_shift_armed:
                            mode_shift_tap_edge = True
                    else:  # Hold
                        if is_pressed:
                            mode_shift_hold_pressed = True
            is_merged = getattr(self, "is_merged", False)
            if mode_shift_tap_edge and not is_merged:
                self._mode_shift_toggle = not getattr(self, "_mode_shift_toggle", False)
            self._mode_shift_armed = mode_shift_pressed_ids
            # Compatibility for any older runtime state readers.
            self._mode_shift_tap_held = self._mode_shift_armed
            local_mode_shift_toggle = bool(getattr(self, "_mode_shift_toggle", False))
            # Share the Mode Shift back-button state across a merged Joy-Con pair so pressing
            # it on EITHER Joy-Con applies the Mode Shift layer to BOTH sides. Publish the
            # toggle and hold components separately because Hold must temporarily invert a
            # Tap-entered Mode Shift, not be ORed with it.
            self._own_mode_shift_toggle = local_mode_shift_toggle
            self._own_mode_shift_tap_edge = mode_shift_tap_edge
            self._own_mode_shift_hold_pressed = mode_shift_hold_pressed
            if is_merged:
                shared_mode_shift_toggle = bool(getattr(self, "_shared_mode_shift_toggle", False))
                shared_mode_shift_hold_pressed = bool(getattr(self, "_shared_mode_shift_hold_pressed", False))
                mode_shift_toggle = (not shared_mode_shift_toggle) if mode_shift_tap_edge else shared_mode_shift_toggle
            else:
                shared_mode_shift_hold_pressed = False
                mode_shift_toggle = local_mode_shift_toggle
            mode_shift_button_active = mode_shift_toggle != (mode_shift_hold_pressed or shared_mode_shift_hold_pressed)
            self._own_mode_shift_active = local_mode_shift_toggle != mode_shift_hold_pressed
            # In-app Gyro auto-applies the Mode Shift layer only when the per-(profile,
            # Gyro Control) Mode Shift toggle is On; the back button applies it regardless.
            in_app_gyro_active = getattr(self, "gyro_mouse_enabled", False) or trigger_gyro
            mapping_scope_active = (in_app_gyro_active and CONFIG.mode_shift_enabled) or mode_shift_button_active
            self._mode_shift_active = mapping_scope_active
            # Resolve the active In-app Gyro mapping dict once per report and index it
            # directly below, instead of calling a resolving getter for every button.
            mapping_scope_dict = CONFIG.get_mapping_scope_dict("in_app_gyro_mode_mappings") if mapping_scope_active else None
            trigger_calibration = False
            gyro_lock_hold_pressed = False
            if not hasattr(self, "_gyro_lock_tap_held"):
                self._gyro_lock_tap_held = set()
            for is_pressed, mapping_key, original_bit, default_action, btn_id in mapping_pairs:
                base_action = get_base_mapping_action(mapping_key)
                base_resolved = default_action if base_action == "Default" else base_action
                if is_pressed and base_resolved in ("Gyro", "In-app Gyro"):
                    trigger_gyro = True
                # The Mode Shift trigger is handled in the pre-pass above; skip it here so
                # it doesn't also emit whatever the shifted layer maps that button to.
                if isinstance(base_resolved, str) and base_resolved.startswith("Custom") and base_resolved.endswith(":" + MODE_SHIFT_TOKEN):
                    continue
                action = mapping_scope_dict.get(f"{mapping_key}_mapping", "Default") if mapping_scope_dict is not None else base_action
                resolved = default_action if action == "Default" else action

                # In-app Gyro Lock: pause gyro control while staying in In-app Gyro mode.
                # Stored as a Custom-form pseudo-mapping "Custom[Hold|Tap]:GYRO_LOCK".
                if isinstance(resolved, str) and resolved.endswith(":" + GYRO_LOCK_TOKEN) and resolved.startswith("Custom"):
                    if resolved.startswith("Custom[Tap]:"):
                        was_held = btn_id in self._gyro_lock_tap_held
                        if is_pressed and not was_held:
                            self._gyro_lock_toggle = not getattr(self, "_gyro_lock_toggle", False)
                            self._gyro_lock_tap_held.add(btn_id)
                        elif not is_pressed and was_held:
                            self._gyro_lock_tap_held.discard(btn_id)
                    else:  # Hold
                        if is_pressed:
                            gyro_lock_hold_pressed = True
                    continue

                # A "Mode Shift" mapping that ended up inside the shifted layer is a no-op
                # here (the trigger is resolved from the base layer in the pre-pass); skip
                # it so it isn't dispatched as a literal "MODE_SHIFT" key sequence.
                if isinstance(resolved, str) and resolved.startswith("Custom") and resolved.endswith(":" + MODE_SHIFT_TOKEN):
                    continue

                if isinstance(resolved, str) and resolved.startswith("Custom"):
                    is_custom = True
                    if resolved.startswith("Custom[Tap]:"):
                        seq_str = resolved[12:]
                        mode = "Tap"
                    elif resolved.startswith("Custom[Hold]:"):
                        seq_str = resolved[13:]
                        mode = "Hold"
                    elif resolved.startswith("Custom:"):
                        seq_str = resolved[7:]
                        mode = "Hold"
                    else:
                        is_custom = False
                        
                    if is_custom:
                        if not hasattr(self, 'active_tap_keys'): self.active_tap_keys = {}
                        if not hasattr(self, 'physical_tap_held'): self.physical_tap_held = set()
                        
                        # was_pressed tracks if we have processed this physical button press yet
                        was_pressed = btn_id in self.active_custom_keys or btn_id in self.physical_tap_held
                        
                        if is_pressed and not was_pressed:
                            seq = seq_str.split("+") if seq_str else []
                            if mode == "Tap":
                                self.physical_tap_held.add(btn_id)
                                self.active_tap_keys[btn_id] = (seq, time.perf_counter())
                                for k in seq:
                                    self._trigger_custom_os_key(k, True)
                            else:
                                self.active_custom_keys[btn_id] = (seq, time.perf_counter(), time.perf_counter())
                                for k in seq:
                                    self._trigger_custom_os_key(k, True)
                        elif not is_pressed and was_pressed:
                            if btn_id in self.active_custom_keys:
                                seq, _, _ = self.active_custom_keys.pop(btn_id)
                                for k in reversed(seq):
                                    self._trigger_custom_os_key(k, False)
                            # For Tap mode, we release after timeout, but we clear the physical held state here
                            if hasattr(self, 'physical_tap_held') and btn_id in self.physical_tap_held:
                                self.physical_tap_held.remove(btn_id)
                                
                elif btn_id in self.active_custom_keys or (hasattr(self, 'physical_tap_held') and btn_id in getattr(self, 'physical_tap_held', set())):
                    if btn_id in self.active_custom_keys:
                        seq, _, _ = self.active_custom_keys.pop(btn_id)
                        for k in reversed(seq):
                            self._trigger_custom_os_key(k, False)
                    if hasattr(self, 'physical_tap_held') and btn_id in self.physical_tap_held:
                        self.physical_tap_held.remove(btn_id)
                
                if is_pressed and not (isinstance(resolved, str) and resolved.startswith("Custom")):
                    if resolved in ("Gyro", "In-app Gyro"): trigger_gyro = True
                    elif resolved == "DJG": trigger_djg = True
                    elif resolved == "Home": inputData.buttons |= SWITCH_BUTTONS["HOME"]
                    elif resolved == "PrtSc": trigger_screenshot = True
                    elif resolved == "Chat":
                        inputData.buttons |= SWITCH_BUTTONS.get("C", 0x00004000)
                    elif resolved == "Mute": inputData.buttons |= 0x10000000
                    elif resolved == "Calibration": trigger_calibration = True
                    elif resolved == "Game Bar": trigger_game_bar = True
                    elif resolved == "HDR Toggle": trigger_hdr_toggle = True
                    elif resolved == "Sys Manager": trigger_sys_manager = True
                    elif resolved == "Change Profile": trigger_change_profile_btn = True
                    elif resolved is None:
                        inputData.buttons |= original_bit
                    elif resolved in SWITCH_BUTTONS:
                        inputData.buttons |= SWITCH_BUTTONS[resolved]
                        inputData.custom_buttons_mask |= SWITCH_BUTTONS[resolved]

            # In-app Gyro Lock state for this report (Hold = while held, Tap = toggled).
            self.gyro_lock_active = gyro_lock_hold_pressed or getattr(self, "_gyro_lock_toggle", False)

            # Apply active controller buttons and continuous mouse wheel
            now = time.perf_counter()
            
            if hasattr(self, 'active_tap_keys'):
                expired_taps = []
                for btn_id, (seq, trigger_time) in self.active_tap_keys.items():
                    if now - trigger_time >= 0.05: # 50ms tap duration
                        for k in reversed(seq):
                            self._trigger_custom_os_key(k, False)
                        expired_taps.append(btn_id)
                for btn_id in expired_taps:
                    del self.active_tap_keys[btn_id]
                    
            # Handle Hold auto-repeat (500ms initial delay, 30ms repeat interval)
            for btn_id, (seq, initial_time, last_repeat) in self.active_custom_keys.items():
                if now - initial_time >= 0.5:
                    if now - last_repeat >= 0.03:
                        for k in seq:
                            if k.startswith("VK_"): # Only auto-repeat keyboard keys
                                self._trigger_custom_os_key(k, True)
                        self.active_custom_keys[btn_id] = (seq, initial_time, now)
            
            # Combine both hold and tap keys for continuous hardware button injection (so console doesn't drop 1-frame taps)
            all_active_seqs = [seq for seq, _, _ in self.active_custom_keys.values()]
            if hasattr(self, 'active_tap_keys'):
                for btn_id, (seq, _) in self.active_tap_keys.items():
                    all_active_seqs.append(seq)

            for seq in all_active_seqs:
                for k in seq:
                    if k.startswith("BTN_"):
                        btn_name = k[4:]
                        if btn_name in SWITCH_BUTTONS:
                            inputData.buttons |= SWITCH_BUTTONS[btn_name]
                            inputData.custom_buttons_mask |= SWITCH_BUTTONS[btn_name]
                    elif k.startswith("MW_"):
                        # Mouse wheel only works in Hold mode due to its continuous nature, but we allow it here if they put it in Tap mode
                        # However, for Tap mode, it will keep scrolling while held, which is acceptable behavior.
                        pass

            for btn_id, (seq, _, _) in self.active_custom_keys.items():
                for k in seq:
                    if k.startswith("MW_"):
                        last_scroll = self.active_custom_mouse_wheel.get(btn_id, 0)
                        if now - last_scroll > 0.05: # 20 ticks per second
                            delta = 120 if k[3:] == "UP" else -120
                            win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, delta, 0)
                            self.active_custom_mouse_wheel[btn_id] = now

            if trigger_calibration and not getattr(self, 'prev_calibration', False):
                self._handle_calibration_button_pressed()
            self.prev_calibration = trigger_calibration

            if getattr(self, 'is_calibration_counting_down', False):
                inputData.left_stick = (0.0, 0.0)
                inputData.right_stick = (0.0, 0.0)
                inputData.gyroscope = (0.0, 0.0, 0.0)
                inputData.accelerometer = (0.0, 0.0, 0.0)
                
                remaining = int(math.ceil(self.calibration_countdown_end - time.perf_counter()))
                if remaining <= 0:
                    remaining = 0
                
                vc = getattr(self, 'virtual_controller', None)
                is_merged = vc and len(vc.controllers) == 2
                is_gyro_active = not is_merged or getattr(self, 'gyro_active', False)
                
                if getattr(self, 'last_remaining_sec', None) != remaining and remaining > 0:
                    self.last_remaining_sec = remaining
                    if is_gyro_active:
                        show_notification("Switch 2 Controller", f"Gyro calibration starts in {remaining} seconds. Please keep the controller stationary.")

                if time.perf_counter() >= self.calibration_countdown_end:
                    
                    self.is_calibration_counting_down = False
                    self.start_calibration()
                    if is_gyro_active:
                        show_notification("Switch 2 Controller", "Gyro calibration in progress... Please keep the controller stationary.")
                
                if self.input_report_callback is not None:
                    self.input_report_callback(inputData, self)
                return

            if getattr(self, 'is_mag_calibration_waiting', False):
                inputData.left_stick = (0.0, 0.0)
                inputData.right_stick = (0.0, 0.0)
                inputData.gyroscope = (0.0, 0.0, 0.0)
                inputData.accelerometer = (0.0, 0.0, 0.0)
                if self.input_report_callback is not None:
                    self.input_report_callback(inputData, self)
                return

            active_mapping_scope = "in_app_gyro_mode_mappings" if mapping_scope_dict is not None else None
            active_scope_dict = CONFIG.get_mapping_scope_dict(active_mapping_scope)
            if active_scope_dict.get("y_mapping", "Default") != "Default": raw_left_pressed = False
            if active_scope_dict.get("x_mapping", "Default") != "Default": raw_up_pressed = False
            if active_scope_dict.get("b_mapping", "Default") != "Default": raw_down_pressed = False
            if active_scope_dict.get("a_mapping", "Default") != "Default": raw_right_pressed = False
            inputData.buttons &= ~0x0F
            
            abxy_mode = getattr(CONFIG, "abxy_mode", "Xbox")
            is_switch_emu = getattr(CONFIG, "simulation_mode", "PS5") in ["Switch2", "Switch1"]
            if is_switch_emu:
                should_swap = (abxy_mode == "Xbox")
            else:
                should_swap = (abxy_mode == "Switch")
            
            if should_swap:
                if raw_down_pressed:  inputData.buttons |= 0x08
                if raw_right_pressed: inputData.buttons |= 0x04
                if raw_left_pressed:  inputData.buttons |= 0x02
                if raw_up_pressed:    inputData.buttons |= 0x01
            else:
                if raw_right_pressed: inputData.buttons |= 0x08
                if raw_down_pressed:  inputData.buttons |= 0x04
                if raw_up_pressed:    inputData.buttons |= 0x02
                if raw_left_pressed:  inputData.buttons |= 0x01

            # NSO GameCube Controller Switch Layout override:
            # GCN physical buttons map differently to Switch Pro buttons.
            # raw_right=GCN A, raw_left=GCN B, raw_down=GCN X, raw_up=GCN Y
            # Desired Switch Layout: GCN A?ro B, GCN B?ro Y, GCN X?ro A, GCN Y?ro X
            if (should_swap and
                    getattr(self, 'controller_info', None) and
                    getattr(self.controller_info, 'product_id', 0) == NSO_GAMECUBE_CONTROLLER_PID):
                # Clear the bits set by the generic Switch layout above
                inputData.buttons &= ~0x0F
                # Re-apply with GCN-specific mapping:
                # GCN X (raw_down) ??Pro A (0x08)
                if raw_down_pressed:  inputData.buttons |= 0x08
                # GCN A (raw_right) ??Pro B (0x04)
                if raw_right_pressed: inputData.buttons |= 0x04
                # GCN Y (raw_up) ??Pro X (0x02)
                if raw_up_pressed:    inputData.buttons |= 0x02
                # GCN B (raw_left) ??Pro Y (0x01)
                if raw_left_pressed:  inputData.buttons |= 0x01

            inputData.buttons |= getattr(inputData, 'custom_buttons_mask', 0)

            if trigger_screenshot and not getattr(self, 'prev_screenshot', False):
                win32api.keybd_event(0x5B, 0, 0, 0)
                win32api.keybd_event(0x2C, 0, 0, 0)
            elif not trigger_screenshot and getattr(self, 'prev_screenshot', False):
                win32api.keybd_event(0x2C, 0, win32con.KEYEVENTF_KEYUP, 0)
                win32api.keybd_event(0x5B, 0, win32con.KEYEVENTF_KEYUP, 0)
            self.prev_screenshot = trigger_screenshot

            if trigger_key_c and not getattr(self, 'prev_key_c', False):
                win32api.keybd_event(0x43, 0, 0, 0)
            elif not trigger_key_c and getattr(self, 'prev_key_c', False):
                win32api.keybd_event(0x43, 0, win32con.KEYEVENTF_KEYUP, 0)
            self.prev_key_c = trigger_key_c

            if trigger_game_bar and not getattr(self, 'prev_game_bar', False):
                win32api.keybd_event(0x5B, 0, 0, 0) # Win down
                win32api.keybd_event(0x47, 0, 0, 0) # G down
                win32api.keybd_event(0x47, 0, win32con.KEYEVENTF_KEYUP, 0) # G up
                win32api.keybd_event(0x5B, 0, win32con.KEYEVENTF_KEYUP, 0) # Win up
            self.prev_game_bar = trigger_game_bar

            if trigger_hdr_toggle and not getattr(self, 'prev_hdr_toggle', False):
                win32api.keybd_event(0x5B, 0, 0, 0) # Win down
                win32api.keybd_event(0x12, 0, 0, 0) # Alt down
                win32api.keybd_event(0x42, 0, 0, 0) # B down
                win32api.keybd_event(0x42, 0, win32con.KEYEVENTF_KEYUP, 0) # B up
                win32api.keybd_event(0x12, 0, win32con.KEYEVENTF_KEYUP, 0) # Alt up
                win32api.keybd_event(0x5B, 0, win32con.KEYEVENTF_KEYUP, 0) # Win up
            self.prev_hdr_toggle = trigger_hdr_toggle

            if trigger_sys_manager and not getattr(self, 'prev_sys_manager', False):
                win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0) # Ctrl down
                win32api.keybd_event(win32con.VK_SHIFT, 0, 0, 0) # Shift down
                win32api.keybd_event(win32con.VK_ESCAPE, 0, 0, 0) # Esc down
                win32api.keybd_event(win32con.VK_ESCAPE, 0, win32con.KEYEVENTF_KEYUP, 0) # Esc up
                win32api.keybd_event(win32con.VK_SHIFT, 0, win32con.KEYEVENTF_KEYUP, 0) # Shift up
                win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0) # Ctrl up
            self.prev_sys_manager = trigger_sys_manager

            if trigger_change_profile_btn and not profile_combo_target and not getattr(self, 'prev_change_profile_btn', False):
                trigger_change_profile()
            self.prev_change_profile_btn = trigger_change_profile_btn

            self._in_app_gyro_mapping_active_this_frame = mapping_scope_active
            self._apply_shared_joystick_mapping(inputData)

            if inputData.buttons & (SWITCH_BUTTONS.get("SR_R", 0) | SWITCH_BUTTONS.get("SL_R", 0) | SWITCH_BUTTONS.get("SL_L", 0) | SWITCH_BUTTONS.get("SR_L", 0)):
                self.side_buttons_pressed = True

            if getattr(self, 'gyro_fusion_callback', None):
                self.gyro_fusion_callback(inputData, self)

            if getattr(self, 'is_calibrating', False) or getattr(self, 'is_mag_calibrating', False):
                self.simulate_gyro_mouse(inputData, False, False, False)
            else:
                # Record own trigger state and use shared trigger (for combined mode cross-controller activation)
                self._own_gyro_trigger = trigger_gyro
                self._own_zr_pressed = bool(inputData.buttons & SWITCH_BUTTONS.get("ZR", 0))
                self._own_zl_pressed = bool(inputData.buttons & SWITCH_BUTTONS.get("ZL", 0))
                
                effective_gyro_trigger = trigger_gyro or getattr(self, '_shared_gyro_trigger', False)
                effective_zr = self._own_zr_pressed or getattr(self, '_shared_zr_pressed', False)
                effective_zl = self._own_zl_pressed or getattr(self, '_shared_zl_pressed', False)
                
                self.simulate_gyro_mouse(inputData, effective_gyro_trigger, effective_zr, effective_zl)

            if trigger_djg != getattr(self, 'prev_djg', False):
                vc = getattr(self, 'virtual_controller', None)
                if vc is not None:
                    vc.handle_djg_trigger(self, pressed=trigger_djg)
            self.prev_djg = trigger_djg

            # If Steam roll compensation is enabled, apply built-in anti-roll projection to gyroscope and accelerometer
            if not getattr(self, 'is_calibrating', False) and not getattr(self, 'is_mag_calibrating', False):
                if getattr(CONFIG, 'steam_roll_compensation', False):
                    # 1. Extract current gyroscope and accelerometer vectors
                    gx, gy, gz = inputData.gyroscope
                    ax, ay, az = inputData.accelerometer
                    
                    # 2. Calculate decoupled Pitch and Yaw using the built-in world-space projection algorithm
                    if getattr(self, 'hold_mode', 'Vertical') == 'Horizontal':
                        g_local = (0.0, gy, gz)
                    else:
                        g_local = (gx, 0.0, gz)
                    
                    g_world_abs = quaternion_rotate_vector(self.orientation, g_local)
                    
                    if self.is_pro_controller() or getattr(self, 'hold_mode', 'Vertical') == 'Vertical':
                        f_local = (0, 1, 0)
                    else:
                        f_local = (1, 0, 0)
                    
                    f_world = quaternion_rotate_vector(self.orientation, f_local)
                    
                    fh_x, fh_y = f_world[0], f_world[1]
                    fh_mag = math.sqrt(fh_x**2 + fh_y**2)
                    if fh_mag < 0.01:
                        r_h = (1, 0, 0)
                    else:
                        r_h = (fh_y / fh_mag, -fh_x / fh_mag, 0)
                    
                    decoupled_pitch = g_world_abs[0] * r_h[0] + g_world_abs[1] * r_h[1]
                    decoupled_yaw = g_world_abs[2]
                    
                    # 3. Calculate roll angle directly from local gravity vector to completely avoid Euler gimbal lock
                    q = self.orientation
                    q_inv = (q[0], -q[1], -q[2], -q[3])
                    gx_g, gy_g, gz_g = quaternion_rotate_vector(q_inv, (0.0, 0.0, -1.0))
                    
                    # 4. Apply accelerometer roll compensation using quaternion rotation in synchronization
                    if getattr(self, 'hold_mode', 'Vertical') == 'Horizontal':
                        # Horizontal mode: Roll is around X-axis (in Y-Z plane)
                        roll_rad = math.atan2(-gy_g, -gz_g)
                        # Construct roll quaternion (rotation around local X-axis)
                        q_roll = (math.cos(roll_rad / 2.0), math.sin(roll_rad / 2.0), 0.0, 0.0)
                        
                        ax_comp, ay_comp, az_comp = quaternion_rotate_vector(q_roll, (ax, ay, az))
                        
                        # Overwrite gyroscope with mapped decoupled values
                        gx_comp = 0.0
                        gy_comp = -decoupled_pitch
                        gz_comp = decoupled_yaw
                    else:
                        # Vertical / Pro Controller mode: Roll is around Y-axis (in X-Z plane)
                        roll_rad = math.atan2(gx_g, -gz_g)
                        # Construct roll quaternion (rotation around local Y-axis)
                        q_roll = (math.cos(roll_rad / 2.0), 0.0, math.sin(roll_rad / 2.0), 0.0)
                        
                        ax_comp, ay_comp, az_comp = quaternion_rotate_vector(q_roll, (ax, ay, az))
                        
                        # Overwrite gyroscope with mapped decoupled values
                        gx_comp = decoupled_pitch
                        gy_comp = 0.0
                        gz_comp = decoupled_yaw
                    
                    # 5. Overwrite inputData with compensated values
                    inputData.gyroscope = (gx_comp, gy_comp, gz_comp)
                    inputData.accelerometer = (ax_comp, ay_comp, az_comp)

            # Apply flat static deadzone (base_dz) to the final virtual controller gyroscope data
            if not getattr(self, 'is_calibrating', False) and not getattr(self, 'is_mag_calibrating', False):
                base_dz = float(getattr(CONFIG, 'virtual_gyro_soft_deadzone', 2.0))
                if base_dz > 0.0:
                    gx_dz, gy_dz, gz_dz = inputData.gyroscope
                    
                    if getattr(self, 'hold_mode', 'Vertical') == 'Horizontal':
                        # Apply base deadzone to Yaw (index 2)
                        if gz_dz > base_dz: gz_dz -= base_dz
                        elif gz_dz < -base_dz: gz_dz += base_dz
                        else: gz_dz = 0.0
                        
                        # Apply base deadzone to Pitch (index 1)
                        if gy_dz > base_dz: gy_dz -= base_dz
                        elif gy_dz < -base_dz: gy_dz += base_dz
                        else: gy_dz = 0.0
                    else:
                        # Apply base deadzone to Yaw (index 2)
                        if gz_dz > base_dz: gz_dz -= base_dz
                        elif gz_dz < -base_dz: gz_dz += base_dz
                        else: gz_dz = 0.0
                        
                        # Apply base deadzone to Pitch (index 0)
                        if gx_dz > base_dz: gx_dz -= base_dz
                        elif gx_dz < -base_dz: gx_dz += base_dz
                        else: gx_dz = 0.0
                    
                    inputData.gyroscope = (gx_dz, gy_dz, gz_dz)

            try:
                vc = getattr(self, 'virtual_controller', None)
                if vc is not None:
                    current_time = time.perf_counter()
                    last_rumble_time = getattr(self, 'last_rumble_time', 0)

                    if current_time - last_rumble_time >= 0.007:
                        self.last_rumble_time = current_time

                        if getattr(vc, 'rumble_force_clear', False):
                            self.rumble_stopped = False
                            self._zero_count = 0
                            vc.rumble_force_clear = False

                        use_dualsense_stereo = (
                            getattr(vc, 'mode', None) == "PS5" and
                            getattr(vc, 'driver_type', None) == "USBIP"
                        )

                        def dispatch_rumble_task(coro):
                            loop = getattr(vc, 'loop', None)
                            if loop and not loop.is_closed():
                                asyncio.run_coroutine_threadsafe(coro, loop)
                            else:
                                try:
                                    asyncio.get_running_loop().create_task(coro)
                                except RuntimeError:
                                    pass

                        if not use_dualsense_stereo:
                            v1, v2, v3, is_zero = vc.get_current_vibration_frames(is_left=self.is_joycon_left())

                            if not getattr(self, '_rumble_task_running', False):

                                async def safe_send_single(v1_c, v2_c, v3_c):
                                    self._rumble_task_running = True
                                    try:
                                        await self.set_vibration(v1_c, v2_c, v3_c)
                                    finally:
                                        self._rumble_task_running = False

                                if is_zero:
                                    if not getattr(self, 'rumble_stopped', False):
                                        self._zero_count = getattr(self, '_zero_count', 0) + 1
                                        dispatch_rumble_task(safe_send_single(v1, v2, v3))
                                        if self._zero_count >= 3:
                                            self.rumble_stopped = True
                                else:
                                    self.rumble_stopped = False
                                    self._zero_count = 0
                                    # Pace continuous rumble to the BLE connection interval
                                    # (~7.5ms) for the ESP32-S3 bridge. The 3 frames per
                                    # command already cover the interval; dispatching every
                                    # input report (faster, and doubled in merge mode) floods
                                    # the firmware's per-interval BLE write and causes the
                                    # stuttering. WinRT is paced by Windows' BLE stack instead.
                                    if self._bridge_rumble_due():
                                        dispatch_rumble_task(safe_send_single(v1, v2, v3))
                        else:
                            v1_l, v2_l, v3_l, is_zero_l = vc.get_current_vibration_frames(is_left=True)
                            v1_r, v2_r, v3_r, is_zero_r = vc.get_current_vibration_frames(is_left=False)

                            if self.is_pro_controller():
                                is_zero = is_zero_l and is_zero_r
                            elif self.is_joycon_left():
                                is_zero = is_zero_l
                            else:
                                is_zero = is_zero_r

                            if not getattr(self, '_rumble_task_running', False):

                                async def safe_send(v1_c_l, v2_c_l, v3_c_l, v1_c_r, v2_c_r, v3_c_r):
                                    self._rumble_task_running = True
                                    try:
                                        if self.is_pro_controller():
                                            await self.set_vibration(v1_c_l, v2_c_l, v3_c_l, False, v1_c_r, v2_c_r, v3_c_r)
                                        elif self.is_joycon_left():
                                            await self.set_vibration(v1_c_l, v2_c_l, v3_c_l)
                                        else:
                                            await self.set_vibration(v1_c_r, v2_c_r, v3_c_r)
                                    finally:
                                        self._rumble_task_running = False

                                if is_zero:
                                    if not getattr(self, 'rumble_stopped', False):
                                        self._zero_count = getattr(self, '_zero_count', 0) + 1
                                        dispatch_rumble_task(safe_send(v1_l, v2_l, v3_l, v1_r, v2_r, v3_r))
                                        if self._zero_count >= 3:
                                            self.rumble_stopped = True
                                else:
                                    self.rumble_stopped = False
                                    self._zero_count = 0
                                    # Pace continuous rumble to ~7.5ms for the bridge (see above).
                                    if self._bridge_rumble_due():
                                        dispatch_rumble_task(safe_send(v1_l, v2_l, v3_l, v1_r, v2_r, v3_r))
            
            except Exception as e:
                logger.debug(f"Sync rumble failed: {e}")

            if self.input_report_callback is not None:
                self.input_report_callback(inputData, self)

        # Arm the connection settle gate (see input_report_callback): suppress input
        # until the first neutral frame or this deadline, so connect-moment garbage /
        # a held wake-button can't fire mapped actions.
        self._input_settled = False
        self._input_settle_deadline = time.time() + 1.0

        if getattr(self.controller_info, 'product_id', 0) == NSO_GAMECUBE_CONTROLLER_PID:
            # GCN input callback: filter out short packets (command acks, Format 0) and
            # only process the 63-byte Format 3 input reports.
            def gc_input_report_callback(sender: BleakGATTCharacteristic, data: bytearray):
                if len(data) < 30:
                    return
                input_report_callback(sender, data)

            if getattr(self, 'is_esp32s3_bridge', False):
                # ESP32 bridge: the _dispatch_binary_packet() router already separates
                # command/ack frames from input frames by UUID prefix.  The MockService
                # only exposes INPUT_REPORT_UUID and COMMAND_RESPONSE_UUID as notify chars.
                # Subscribing gc_input_report_callback to COMMAND_RESPONSE_UUID would
                # overwrite the command_response_callback registered during initialize(),
                # causing all write_command() calls (including enableFeatures) to timeout.
                # Use the standard INPUT_REPORT_UUID subscription ??the bridge router
                # ensures GCN input frames reach this callback correctly.
                logger.info("GCN via ESP32 bridge: using standard INPUT_REPORT_UUID subscription")
                await self.client.start_notify(INPUT_REPORT_UUID, gc_input_report_callback)
            else:
                # WinRT BLE: The GCN controller may deliver input on any notify char in the
                # SW2 service (Windows may re-index handles).  Subscribe to all of them so
                # input is never missed; the gc_input_report_callback length-filter handles
                # non-input frames that also arrive on the subscribed characteristics.
                try:
                    notify_chars = []
                    for service in self.client.services:
                        if "ab7de9be" in str(service.uuid).lower():
                            for char in service.characteristics:
                                if "notify" in char.properties:
                                    notify_chars.append(char)
                    
                    if not notify_chars:
                        logger.warning("No notify characteristics found for GameCube! Falling back.")
                        await self.client.start_notify(INPUT_REPORT_UUID, gc_input_report_callback)
                    else:
                        for char in notify_chars:
                            try:
                                logger.info(f"Subscribing to GameCube notify characteristic: {char.uuid}")
                                await self.client.start_notify(char.uuid, gc_input_report_callback)
                            except Exception as e:
                                logger.warning(f"Failed to subscribe to {char.uuid}: {e}")
                except Exception as e:
                    logger.warning(f"Error subscribing to GameCube characteristics: {e}")
                    await self.client.start_notify(INPUT_REPORT_UUID, gc_input_report_callback)
        else:
            await self.client.start_notify(INPUT_REPORT_UUID, input_report_callback)

    def set_input_report_callback(self, callback):
        self.input_report_callback = callback

    def _reset_orientation_from_accel(self, ax, ay, az, mx=None, my=None, mz=None):
        norm = math.sqrt(ax*ax + ay*ay + az*az)
        if norm > 0.001:
            vx, vy, vz = ax / norm, ay / norm, az / norm
            if 1.0 + vz > 0.0001:
                q_raw = [1.0 + vz, vy, -vx, 0.0]
                q_norm = math.sqrt(q_raw[0]**2 + q_raw[1]**2 + q_raw[2]**2)
                q = [q_raw[0]/q_norm, q_raw[1]/q_norm, q_raw[2]/q_norm, 0.0]
            else:
                # Upside down
                q = [0.0, 1.0, 0.0, 0.0]
        else:
            q = [1.0, 0.0, 0.0, 0.0]
        
        self.ahrs.quaternion = imufusion.Quaternion(np.array(q))
        self.gyro_bias_integral = (0.0, 0.0, 0.0)
        self.q_world_offset = None
        self.gyro_moving_envelope = 0.0
        self.last_fusion_time = time.perf_counter()
        self.prev_q = None

    def _mahony_update(self, gx, gy, gz, ax, ay, az, mx, my, mz, dt):
        current_mode = getattr(CONFIG, "gyro_mode", "World")
        
        # 1. Convert raw gyroscope and accelerometer values into standard physical units
        # Deduct static bias and dynamic bias integral (dynamic bias is in rad/s, convert to dps)
        # - Pro Controller uses ST standard +-2000 dps (70 mdps/LSB -> 1000/70 = 14.285714 LSB/dps)
        # - Joy-Cons use Nintendo standard +-2000 dps (0.06103 dps/LSB -> 1/0.06103 = 16.384 LSB/dps)
        GYRO_SCALE = 14.285714 if self.is_pro_controller() else 16.384
        gx_dps = (gx / GYRO_SCALE) - math.degrees(self.gyro_bias_integral[0])
        gy_dps = (gy / GYRO_SCALE) - math.degrees(self.gyro_bias_integral[1])
        gz_dps = (gz / GYRO_SCALE) - math.degrees(self.gyro_bias_integral[2])
        
        # Accelerometer to g unit
        ax_g = ax / 16384.0
        ay_g = ay / 16384.0
        az_g = az / 16384.0
        
        # 2. Perform sensor fusion using C-extension imufusion
        gyro_arr = np.array([gx_dps, gy_dps, gz_dps], dtype=np.float64)
        accel_arr = np.array([ax_g, ay_g, az_g], dtype=np.float64)
        
        # Single smooth rational formula to dynamically scale blend_factor based on movement intensity.
        # This addresses centripetal acceleration (proportional to omega^2), which introduces a DC bias during waving.
        # - When still (envelope=0), blend_factor = 0.0 (100% raw accelerometer, effective Gain=0.1).
        # - For slow movements (envelope=5 dps), blend_factor = 0.95 (5% correction active, safe coordinate drift prevention).
        # - For high velocities (envelope>=50 dps), blend_factor approaches 0.995+ (completely locking out massive centripetal noise).
        envelope = getattr(self, 'gyro_moving_envelope', 0.0)
        blend_factor = (envelope / 0.26) / (1.0 + (envelope / 0.26))
        accel_blended = accel_arr * (1.0 - blend_factor) + self.ahrs.gravity * blend_factor
        
        if current_mode == "World" and (mx != 0 or my != 0 or mz != 0):
            mx_cal = mx - self.mag_bias[0]
            my_cal = my - self.mag_bias[1]
            mz_cal = mz - self.mag_bias[2]
            mag_arr = np.array([mx_cal, my_cal, mz_cal], dtype=np.float64)
            self.ahrs.update(gyro_arr, accel_blended, mag_arr, float(dt))
        else:
            self.ahrs.update_no_magnetometer(gyro_arr, accel_blended, float(dt))
            
        # 3. Dynamic On-the-fly Gyro Bias Calibration (Background PI loop)
        # To completely eliminate pullback/drift when stopping or still, we immediately cut off
        # the correction (integration) when movement stops (envelope < 0.25 or gyro_mag < 45)
        # OR when the controller is accelerating/decelerating (accel_err_total >= 150 LSB).
        # Any dynamic compensation is performed strictly during steady, non-accelerating movement states.
        raw_mag = math.sqrt(ax*ax + ay*ay + az*az)
        G_REF = 16384.0
        accel_err_total = abs(raw_mag - G_REF)
        gyro_mag = math.sqrt(gx**2 + gy**2 + gz**2)
        
        if accel_err_total < 150 and gyro_mag >= 45 and getattr(self, 'gyro_moving_envelope', 0.0) >= 0.25:
            g_est = self.ahrs.gravity
            v_pred = (g_est[0], g_est[1], g_est[2])
            v_meas = vector_normalize((ax, ay, az))
            error_accel = vector_cross(v_meas, v_pred)
            
            # Scale bias accumulation using dynamic tapering to prevent vibration leakage
            q_wxyz = self.ahrs.quaternion.wxyz
            q = (q_wxyz[0], q_wxyz[1], q_wxyz[2], q_wxyz[3])
            raw_world = quaternion_rotate_vector(q, (ax, ay, az))
            h_shake = math.sqrt(raw_world[0]**2 + raw_world[1]**2)
            v_shake_err = abs(raw_world[2] - G_REF)
            
            kp_scale = 1.0 / (1.0 + (h_shake / 1000.0)**4 + (v_shake_err / 8000.0)**2 + (gyro_mag / 4000.0)**4)
            ki_base = 30.0
            
            self.gyro_bias_integral = (
                self.gyro_bias_integral[0] + error_accel[0] * ki_base * dt * kp_scale,
                self.gyro_bias_integral[1] + error_accel[1] * ki_base * dt * kp_scale,
                self.gyro_bias_integral[2] + error_accel[2] * ki_base * dt * kp_scale
            )
        
    def simulate_mouse(self, inputData: ControllerInputData):
        mouse_config = CONFIG.mouse_config
        
        if mouse_config.enabled and self.is_joycon():
            # Check if mouse coordinate data is valid to mark the controller as active IR mouse
            _IR_THRESHOLD_MAP = {1: (1000, 4000), 2: (1500, 5000), 3: (3000, 10000)}
            _ir_dist, _ir_rough = _IR_THRESHOLD_MAP.get(mouse_config.ir_activate_threshold, (1000, 4000))
            ir_active = (inputData.mouse_distance != 0
                         and inputData.mouse_distance < _ir_dist
                         and inputData.mouse_roughness < _ir_rough)

            if ir_active:
                self.jc_mouse_active = True 
                
                # Determine which config to use
                mouseButtonsConfig = mouse_config.joycon_l_buttons if self.is_joycon_left() else mouse_config.joycon_r_buttons
                
                # Extract current button states
                lb = bool(inputData.buttons & mouseButtonsConfig.left_button) if mouseButtonsConfig.left_button else False
                mb = bool(inputData.buttons & mouseButtonsConfig.middle_button) if mouseButtonsConfig.middle_button else False
                rb = bool(inputData.buttons & mouseButtonsConfig.right_button) if mouseButtonsConfig.right_button else False
                
                # Consume/Clear these buttons so they don't trigger virtual controller outputs
                clear_mask = 0
                if mouseButtonsConfig.left_button: clear_mask |= mouseButtonsConfig.left_button
                if mouseButtonsConfig.middle_button: clear_mask |= mouseButtonsConfig.middle_button
                if mouseButtonsConfig.right_button: clear_mask |= mouseButtonsConfig.right_button
                inputData.buttons &= ~clear_mask

                x, y = inputData.mouse_coords
                if getattr(self, 'previous_mouse_state', None) is not None and self.previous_mouse_state.ir_active:
                    dx = signed_looping_difference_16bit(self.previous_mouse_state.x, x)
                    dy = signed_looping_difference_16bit(self.previous_mouse_state.y, y)

                    if dx != 0 or dy != 0:
                        self.jc_target_vx = dx * mouse_config.sensitivity * 0.009
                        self.jc_target_vy = dy * mouse_config.sensitivity * 0.009
                    else:
                        self.jc_target_vx = 0.0
                        self.jc_target_vy = 0.0
                else:
                    self.jc_target_vx = 0.0
                    self.jc_target_vy = 0.0

                # Get previous button states
                prev_lb = self.previous_mouse_state.lb if getattr(self, 'previous_mouse_state', None) is not None else False
                prev_mb = self.previous_mouse_state.mb if getattr(self, 'previous_mouse_state', None) is not None else False
                prev_rb = self.previous_mouse_state.rb if getattr(self, 'previous_mouse_state', None) is not None else False

                # Inject mouse clicks immediately
                mx, my = win32api.GetCursorPos()
                press_or_release_mouse_button(lb, prev_lb, win32con.MOUSEEVENTF_LEFTDOWN, mx, my)
                press_or_release_mouse_button(mb, prev_mb, win32con.MOUSEEVENTF_MIDDLEDOWN, mx, my)
                press_or_release_mouse_button(rb, prev_rb, win32con.MOUSEEVENTF_RIGHTDOWN, mx, my)

                # Scroll wheel handling
                if self.is_joycon_right():
                    scroll_value = inputData.right_stick[1]
                else:
                    scroll_value = inputData.left_stick[1]

                if abs(scroll_value) > 0.2:
                    win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, int(scroll_value * 60 * mouse_config.scroll_sensitivity), 0)
                            
                self.previous_mouse_state = MouseState(x, y, lb, mb, rb, ir_active)
            else:
                self.jc_mouse_active = False
                self.jc_target_vx = 0.0
                self.jc_target_vy = 0.0
                # Exited IR Mouse Mode: release any pressed mouse buttons instantly
                if getattr(self, 'previous_mouse_state', None) is not None:
                    mx, my = win32api.GetCursorPos()
                    press_or_release_mouse_button(False, self.previous_mouse_state.lb, win32con.MOUSEEVENTF_LEFTDOWN, mx, my)
                    press_or_release_mouse_button(False, self.previous_mouse_state.mb, win32con.MOUSEEVENTF_MIDDLEDOWN, mx, my)
                    press_or_release_mouse_button(False, self.previous_mouse_state.rb, win32con.MOUSEEVENTF_RIGHTDOWN, mx, my)
                self.previous_mouse_state = None
        else:
            self.jc_mouse_active = False
            self.jc_target_vx = 0.0
            self.jc_target_vy = 0.0
            # If mouse mode is disabled or it's not a Joycon, make sure any pressed mouse button is released!
            if getattr(self, 'previous_mouse_state', None) is not None:
                mx, my = win32api.GetCursorPos()
                press_or_release_mouse_button(False, self.previous_mouse_state.lb, win32con.MOUSEEVENTF_LEFTDOWN, mx, my)
                press_or_release_mouse_button(False, self.previous_mouse_state.mb, win32con.MOUSEEVENTF_MIDDLEDOWN, mx, my)
                press_or_release_mouse_button(False, self.previous_mouse_state.rb, win32con.MOUSEEVENTF_RIGHTDOWN, mx, my)
            self.previous_mouse_state = None

    def simulate_gyro_mouse(self, inputData: ControllerInputData, trigger_pressed: bool = False, zr_pressed: bool = False, zl_pressed: bool = False):

        if getattr(self, 'is_calibrating', False):
            if time.perf_counter() < self.calibration_end_time:
                self.calibration_samples_gyro.append(inputData.gyroscope)
                # Ensure ALL output variables are zeroed during calibration to stop leakage
                inputData.left_stick = (0.0, 0.0)
                inputData.right_stick = (0.0, 0.0)
                inputData.gyroscope = (0.0, 0.0, 0.0)
                inputData.accelerometer = (0.0, 0.0, 0.0)
                return
            else:
                self.is_calibrating = False

                if len(self.calibration_samples_gyro) > 0:
                    gx = sum(s[0] for s in self.calibration_samples_gyro) / len(self.calibration_samples_gyro)
                    gy = sum(s[1] for s in self.calibration_samples_gyro) / len(self.calibration_samples_gyro)
                    gz = sum(s[2] for s in self.calibration_samples_gyro) / len(self.calibration_samples_gyro)
                    self.gyro_bias = (gx, gy, gz)
                    
                    logger.info(f"Calibration complete for {self.device.address}. Gyro bias: ({gx:.1f}, {gy:.1f}, {gz:.1f})")
                    
                    # Store device-specific calibration data
                    CONFIG.calibration_data[self.device.address] = list(self.gyro_bias)
                    
                    if self.is_joycon_left():
                        CONFIG.gyro_bias_l = list(self.gyro_bias)
                    else:
                        CONFIG.gyro_bias_r = list(self.gyro_bias)
                    CONFIG.save_config()

                    if getattr(self, 'back_button_calibration_active', False):
                        vc = getattr(self, 'virtual_controller', None)
                        is_merged = vc and len(vc.controllers) == 2
                        is_gyro_active = not is_merged or getattr(self, 'gyro_active', False)
                        
                        if is_gyro_active:
                            self.is_mag_calibration_waiting = True
                            show_notification("Switch 2 Controller", "Gyro calibration complete! Press the Calibration button again to start Magnetometer calibration.")
                        else:
                            self.back_button_calibration_active = False

        if getattr(self, 'is_mag_calibrating', False):
            mx, my, mz = inputData.magnometer
            self.mag_min[0] = min(self.mag_min[0], mx)
            self.mag_min[1] = min(self.mag_min[1], my)
            self.mag_min[2] = min(self.mag_min[2], mz)
            self.mag_max[0] = max(self.mag_max[0], mx)
            self.mag_max[1] = max(self.mag_max[1], my)
            self.mag_max[2] = max(self.mag_max[2], mz)
            # Suppress all output during mag calibration
            inputData.left_stick = (0.0, 0.0)
            inputData.right_stick = (0.0, 0.0)
            inputData.gyroscope = (0.0, 0.0, 0.0)
            inputData.accelerometer = (0.0, 0.0, 0.0)
            return

        current_gyro_active = getattr(self, 'gyro_active', True)
        if current_gyro_active and not getattr(self, 'prev_gyro_active', True):
            if hasattr(self, 'true_accel'):
                self._reset_orientation_from_accel(*self.true_accel)
            else:
                ax_t, ay_t, az_t = inputData.accelerometer
                self._reset_orientation_from_accel(ax_t, ay_t, az_t)
        self.prev_gyro_active = current_gyro_active

        if getattr(self, '_skip_gyro_mouse', False) or not current_gyro_active:
            if hasattr(self, 'gyro_moving_envelope'):
                self.gyro_moving_envelope *= 0.88

            self.gyro_target_vx = 0.0
            self.gyro_target_vy = 0.0
            self._gyro_rstick_out = (0.0, 0.0)
            self.current_vx = 0.0
            self.current_vy = 0.0
            self.interp_residual_x = 0.0
            self.interp_residual_y = 0.0
            return

        activation_mode = getattr(CONFIG, "gyro_activation_mode", "Toggle")

        bx, by, bz = self.gyro_bias
        raw_gx, raw_gy, raw_gz = inputData.gyroscope
        ax, ay, az = inputData.accelerometer
        
        # Continuous Desk-Only Auto-Calibration:
        # Bias creep is instantly cut off (alpha = 0) whenever the controller is hand-held
        # or during movement/stopping states to prevent any cursor pullback.
        # It is allowed to slowly run (alpha = 0.001) ONLY when the controller is placed
        # absolutely still on a flat desk surface (moving_env < 0.05).
        accel_mag = math.sqrt(ax**2 + ay**2 + az**2)
        accel_err = abs(accel_mag - 16384.0)
        gyro_sub_mag = math.sqrt((raw_gx - bx)**2 + (raw_gy - by)**2 + (raw_gz - bz)**2)
        moving_env = getattr(self, 'gyro_moving_envelope', 0.0)
        
        if accel_err < 100 and gyro_sub_mag < 15 and moving_env < 0.05:
            alpha = 0.001
            self.gyro_bias = (
                (1.0 - alpha) * self.gyro_bias[0] + alpha * raw_gx,
                (1.0 - alpha) * self.gyro_bias[1] + alpha * raw_gy,
                (1.0 - alpha) * self.gyro_bias[2] + alpha * raw_gz
            )
            bx, by, bz = self.gyro_bias

        gyro_x = raw_gx - bx
        gyro_y = raw_gy - by
        gyro_z = raw_gz - bz

        if getattr(CONFIG, 'stabilized_gyro', False):
            gyro_scale = 14.285714 if self.is_pro_controller() else 16.384
            gyro_x -= math.degrees(self.gyro_bias_integral[0]) * gyro_scale
            gyro_y -= math.degrees(self.gyro_bias_integral[1]) * gyro_scale
            gyro_z -= math.degrees(self.gyro_bias_integral[2]) * gyro_scale

        inputData.gyroscope = (gyro_x, gyro_y, gyro_z)

        # Always extract decoupled movements and calculate soft deadzones
        # so that they can be applied to both the gyro mouse and virtual controller data.
        current_mode = getattr(CONFIG, "gyro_mode", "World")
        self.soft_dz_h = 0.0
        self.soft_dz_v = 0.0
        self.eff_h_final = 0.0
        self.eff_v_final = 0.0

        if current_mode in ["World", "Yaw"]:
            if self.is_pro_controller() or self.hold_mode == "Vertical":
                g_local = (gyro_x, 0.0, gyro_z)
            else:
                g_local = (0.0, gyro_y, gyro_z)
            
            if getattr(self, 'q_world_offset', None) is None:
                q_abs = self.orientation
                f_world = quaternion_rotate_vector(q_abs, (0, 1, 0))
                yaw_angle = math.atan2(f_world[0], f_world[1])
                self.q_world_offset = -yaw_angle
            
            g_world_abs = quaternion_rotate_vector(self.orientation, g_local)
            
            if self.is_pro_controller() or self.hold_mode == "Vertical":
                f_local = (0, 1, 0)
            else:
                f_local = (1, 0, 0)
            
            f_world = quaternion_rotate_vector(self.orientation, f_local)
            
            fh_x, fh_y = f_world[0], f_world[1]
            fh_mag = math.sqrt(fh_x**2 + fh_y**2)
            if fh_mag < 0.01:
                r_h = (1, 0, 0)
            else:
                r_h = (fh_y / fh_mag, -fh_x / fh_mag, 0)
            
            eff_h = -g_world_abs[2]
            eff_v = g_world_abs[0] * r_h[0] + g_world_abs[1] * r_h[1]
            
            gyro_scale = 14.285714 if self.is_pro_controller() else 16.384
            omega = math.sqrt(eff_h**2 + eff_v**2) / gyro_scale
            
            if not hasattr(self, 'gyro_moving_envelope'):
                self.gyro_moving_envelope = 0.0
            self.gyro_moving_envelope = 0.88 * self.gyro_moving_envelope + 0.12 * omega
            
            base_dz = 2.0 if self.is_joycon() else 1.0
            
            # Decay deadzone to 0 quickly (at 3.0 dps) to prevent asymmetric deadzone subtraction during slow turnarounds
            soft_dz = base_dz * (1.0 - min(1.0, self.gyro_moving_envelope / 3.0))
            
            self.soft_dz_h = soft_dz
            self.soft_dz_v = soft_dz
            
            if eff_h > soft_dz: self.eff_h_final = eff_h - soft_dz
            elif eff_h < -soft_dz: self.eff_h_final = eff_h + soft_dz
            
            if eff_v > soft_dz: self.eff_v_final = eff_v - soft_dz
            elif eff_v < -soft_dz: self.eff_v_final = eff_v + soft_dz
        
        rx, ry = inputData.right_stick

        ax, ay, az = inputData.accelerometer

        if activation_mode == "Hold":
            if trigger_pressed and not getattr(self, 'gr_was_pressed', False):
                # Reset orientation on activation to prevent jumps
                if hasattr(self, 'true_accel'):
                    self._reset_orientation_from_accel(*self.true_accel)
                else:
                    self._reset_orientation_from_accel(ax, ay, az)
                self.gyro_start_time = time.perf_counter()
                self.gyro_steering_origin_accel = (ax, ay, az)
            self.gyro_mouse_enabled = trigger_pressed
        else:
            if trigger_pressed and not self.gr_was_pressed:
                self.gyro_mouse_enabled = not self.gyro_mouse_enabled
                if self.gyro_mouse_enabled:
                    # Reset orientation on activation to prevent jumps
                    if hasattr(self, 'true_accel'):
                        self._reset_orientation_from_accel(*self.true_accel)
                    else:
                        self._reset_orientation_from_accel(ax, ay, az)
                    self.gyro_start_time = time.perf_counter()
                    self.gyro_steering_origin_accel = (ax, ay, az)
                
        self.gr_was_pressed = trigger_pressed

        if not self.gyro_mouse_enabled:
            self.gyro_steering_origin_accel = None

        if self.gyro_mouse_enabled:
            if getattr(self, 'gyro_steering_origin_accel', None) is None:
                self.gyro_steering_origin_accel = (ax, ay, az)
            # Dynamically extract and rotate stick inputs for Stick Assist
            is_merged = getattr(self, "is_merged", False)
            if is_merged:
                # In merge mode, restrict stick assist to the right stick
                sx, sy = getattr(self, '_shared_right_stick', inputData.right_stick)
            else:
                # In single mode
                if self.is_joycon_left():
                    sx, sy = inputData.left_stick
                    if getattr(self, 'hold_mode', 'Vertical') == 'Horizontal':
                        sx, sy = -sy, sx
                elif self.is_joycon_right():
                    sx, sy = inputData.right_stick
                    if getattr(self, 'hold_mode', 'Vertical') == 'Horizontal':
                        sx, sy = sy, -sx
                else:
                    sx, sy = inputData.right_stick
            
            target_vx = 0.0
            target_vy = 0.0
            
            now = time.perf_counter()
            current_mode = getattr(CONFIG, "gyro_mode", "World")
            if getattr(CONFIG, "gyro_control_mode", "Mouse") == "Steering":
                current_mode = "Roll"

            # Suppress movement ONLY during gyro startup (Auto-Leveling period)
            if now - self.gyro_start_time < 0.05:
                self.gyro_target_vx = 0.0
                self.gyro_target_vy = 0.0
                self._gyro_rstick_out = (0.0, 0.0)
                return
            
            gyro_deadzone = 0.2 
            
            if current_mode in ["World", "Yaw"]:
                sensitivity = getattr(CONFIG, "gyro_sensitivity", 0.3) * 2.0
                accel_factor = 0.002
                
                # Determine vertical sign (invert for Right Joycon in H-mode if needed)
                v_sign = -1.0
                if self.is_joycon_right() and self.hold_mode == "Horizontal":
                    v_sign = 1.0
                
                # Decoupled gyro mouse movement with 20ms click stabilization
                # Bypasses gyro coordinate changes for 20ms after click press-down to eliminate finger shake.
                if (now - getattr(self, "last_click_event_time", 0.0)) >= 0.02:
                    target_vx += self.eff_h_final * sensitivity * accel_factor
                    target_vy += self.eff_v_final * v_sign * sensitivity * accel_factor 
            elif current_mode == "Roll":
                ax, ay, az = inputData.accelerometer
                
                # Selection of the correct tilt axis based on orientation
                is_horizontal = (getattr(self, "hold_mode", "Horizontal") == "Horizontal")
                if is_horizontal:
                    # In H-mode, tilt is measured on the Y axis
                    # Correcting signs: CCW tilt should be Left (Negative Virtual X)
                    if self.is_joycon_right():
                        tilt_value = ay # Right Joycon CCW -> Y points Down -> ay negative. So Positive steer? No.
                    else:
                        tilt_value = -ay # Left Joycon CCW -> Y points Up -> ay positive. -ay negative.
                else:
                    # In V-mode or Pro Controller, tilt is on the X axis
                    tilt_value = ax
                
                # Adjust tilt_value based on the posture at the moment of activation (origin)
                if getattr(self, "gyro_steering_origin_accel", None) is not None:
                    orig_ax, orig_ay, orig_az = self.gyro_steering_origin_accel
                    if is_horizontal:
                        if self.is_joycon_right():
                            orig_tilt = orig_ay
                        else:
                            orig_tilt = -orig_ay
                    else:
                        orig_tilt = orig_ax
                    tilt_value -= orig_tilt
                
                tilt_normalized = tilt_value / 4000.0
                sensitivity = getattr(CONFIG, "gyro_sensitivity", 4.0) * 2.0
                # Sensitivity * 1.0 (Inverted sign based on user feedback)
                steer_value = max(-1.0, min(1.0, -tilt_normalized * sensitivity))
                
                # Store for virtual controller to apply to correct virtual axis
                self._own_steer_value = steer_value


            # Gyro Control == "R Joystick": map gyro angular velocity to a right-stick
            # deflection (push toward motion, recenter when still) instead of mouse motion.
            # The Sensitivity slider scales the velocity->deflection conversion; the result
            # is clamped to the right stick's maximum (unit magnitude).
            if getattr(CONFIG, "gyro_control_mode", "Mouse") == "R Joystick":
                gyro_scale = 14.285714 if self.is_pro_controller() else 16.384
                rstick_sens = getattr(CONFIG, "r_joystick_gyro_sensitivity", 5.0) * 8.0
                RSTICK_CONV = 0.002  # base velocity(dps)->deflection factor
                if current_mode in ["World", "Yaw"]:
                    v_sign = -1.0
                    if self.is_joycon_right() and self.hold_mode == "Horizontal":
                        v_sign = 1.0
                    rx = (self.eff_h_final / gyro_scale) * rstick_sens * RSTICK_CONV
                    ry = -((self.eff_v_final * v_sign) / gyro_scale) * rstick_sens * RSTICK_CONV
                elif current_mode == "Roll":
                    rx = getattr(self, "_own_steer_value", 0.0)
                    ry = 0.0
                else:
                    rx = ry = 0.0
                self._gyro_rstick_out = (rx, ry)
                # Suppress mouse motion while driving the stick.
                target_vx = 0.0
                target_vy = 0.0
            else:
                self._gyro_rstick_out = (0.0, 0.0)

            # In-app Gyro Lock: pause gyro motion output but stay in In-app Gyro mode.
            if getattr(self, "gyro_lock_active", False):
                target_vx = 0.0
                target_vy = 0.0
                self._gyro_rstick_out = (0.0, 0.0)

            self.gyro_target_vx = target_vx
            self.gyro_target_vy = target_vy

        else:
            self.gyro_target_vx = 0.0
            self.gyro_target_vy = 0.0
            self._gyro_rstick_out = (0.0, 0.0)
            self._own_steer_value = 0.0
            self._gyro_lock_toggle = False
            self.gyro_steering_origin_accel = None
            if getattr(self, 'prev_l_click', False): win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            if getattr(self, 'prev_r_click', False): win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
            self.prev_l_click = self.prev_r_click = False
            self.gyro_residual_x = self.gyro_residual_y = 0.0
            self.current_vx = self.current_vy = 0.0
            self.interp_residual_x = self.interp_residual_y = 0.0

    def _action_to_joystick_tokens(self, action, default_token=None):
        if action == "Default":
            return ([default_token] if default_token else []), []
        if not action:
            return [], []
        if isinstance(action, str) and action.startswith("Custom"):
            if action.startswith("Custom[Tap]:"):
                seq_str = action[12:]
                return [], [k for k in seq_str.split("+") if k]
            elif action.startswith("Custom[Hold]:"):
                seq_str = action[13:]
            elif action.startswith("Custom:"):
                seq_str = action[7:]
            else:
                seq_str = ""
            return [k for k in seq_str.split("+") if k], []
        if action in SWITCH_BUTTONS:
            return [f"BTN_{action}"], []
        named = {
            "Home": "BTN_HOME",
            "Capture": "BTN_CAPT",
            "Chat": "BTN_C",
            "PrtSc": "VK_SNAPSHOT",
            "Mute": "VK_VOLUME_MUTE",
        }
        token = named.get(action)
        return ([token] if token else []), []

    def _in_app_gyro_mapping_scope(self):
        # Follows the resolved Mode Shift state for this report (auto-apply when the
        # toggle is On during In-app Gyro, or while the Mode Shift back button is held).
        active = getattr(self, "_mode_shift_active", False) or getattr(self, "_in_app_gyro_mapping_active_this_frame", False)
        return "in_app_gyro_mode_mappings" if active else None

    def _handle_profile_selection_input(self, inputData, btn_states, selection_active):
        # Navigation for Manual Change Profile selection. Up = cycle back, Down = cycle
        # forward; A/B confirm/cancel. Mirrors the per-controller hold-orientation /
        # layout remapping that the virtual controller applies, so the buttons match
        # what the user sees. Single Joy-Cons use their directional buttons as ABXY,
        # so they navigate by stick only; Pro/merged also navigate by the real D-pad.
        TH = 0.6
        lx, ly = inputData.left_stick
        rx, ry = inputData.right_stick
        hold = getattr(self, "hold_mode", "Vertical")
        is_left = self.is_joycon_left()
        is_right = self.is_joycon_right()

        vc = getattr(self, "virtual_controller", None)
        merged = bool(getattr(self, "is_merged", False) or (vc and len(getattr(vc, "controllers", [])) == 2))
        pUP, pDOWN = bool(btn_states.get("UP")), bool(btn_states.get("DOWN"))
        pLEFT, pRIGHT = bool(btn_states.get("LEFT")), bool(btn_states.get("RIGHT"))
        pA, pB = bool(btn_states.get("A")), bool(btn_states.get("B"))
        pX, pY = bool(btn_states.get("X")), bool(btn_states.get("Y"))

        # Held-orientation vertical stick value (mirror virtual_controller rotations).
        # A merged pair is always held vertically: left Joy-Con contributes the left
        # stick/D-pad, right Joy-Con contributes the right stick.
        if merged and is_left:
            up = ly > TH
            down = ly < -TH
        elif merged and is_right:
            up = ry > TH
            down = ry < -TH
        elif not (is_left or is_right):
            up = ly > TH or ry > TH
            down = ly < -TH or ry < -TH
        elif is_left:
            held_v = lx if hold == "Horizontal" else ly
            up = held_v > TH
            down = held_v < -TH
        elif is_right:
            held_v = -rx if hold == "Horizontal" else ry
            up = held_v > TH
            down = held_v < -TH

        # Confirm/Cancel: first apply the V/H-mode mapping to find the physical buttons
        # at the bottom and right face positions, then apply the Switch/Xbox layout to
        # decide which is A (confirm) and which is B (cancel).
        layout = getattr(CONFIG, "abxy_mode", "Xbox")
        nav_button_now = False
        if merged and is_left:
            dpad_up, dpad_down = pUP, pDOWN
            up = up or dpad_up
            down = down or dpad_down
            nav_button_now = dpad_up or dpad_down
            bottom_pos, right_pos = False, False
        elif merged or not (is_left or is_right):
            # Pro / merged right Joy-Con: real D-pad is navigation, ABXY are physical.
            bottom_pos, right_pos = pB, pA
            up = up or pUP
            down = down or pDOWN
            nav_button_now = pUP or pDOWN
        elif is_left and hold == "Vertical":
            bottom_pos, right_pos = pDOWN, pRIGHT
        elif is_left and hold == "Horizontal":
            bottom_pos, right_pos = pLEFT, pDOWN
        elif is_right and hold == "Horizontal":
            if layout == "Switch":
                confirm, cancel = pX, pA
            else:
                confirm, cancel = pA, pX
            bottom_pos = right_pos = None
        else:  # right Joy-Con vertical
            bottom_pos, right_pos = pB, pA

        if not (is_right and hold == "Horizontal" and not merged):
            if layout == "Switch":
                confirm, cancel = right_pos, bottom_pos
            else:
                confirm, cancel = bottom_pos, right_pos

        # A Change Profile button press also cycles to the next profile (like Auto).
        # Buttons mapped to A or B are excluded since they are used for confirm/cancel.
        cp_now = False
        cp_map = {
            "gl": "GL", "gr": "GR", "sll": "SL_L", "srl": "SR_L", "slr": "SL_R", "srr": "SR_R",
            "gc_l_click": "GC_L_CLICK", "gc_r_click": "GC_R_CLICK",
            "home": "HOME", "capt": "CAPT", "c": "C", "plus": "PLUS", "minus": "MINUS",
            "x": "X", "y": "Y", "up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT",
            "zl": "ZL", "l": "L", "zr": "ZR", "r": "R", "l_stk": "L_STK", "r_stk": "R_STK",
        }
        for mkey, bkey in cp_map.items():
            if btn_states.get(bkey) and CONFIG.get_mapping_setting_scoped(mkey, "Default", None) == "Change Profile":
                cp_now = True
                break
        if nav_button_now:
            cp_now = False

        if not selection_active:
            # Draining after confirm/cancel: keep output suppressed until A and B are
            # released so the press doesn't leak into the virtual controller.
            if not confirm and not cancel:
                self._ps_drain = False
                self._ps_was_active = False
            self._ps_confirm_prev, self._ps_cancel_prev = confirm, cancel
            self._ps_cp_prev = cp_now
            return

        if not getattr(self, "_ps_was_active", False):
            # Just entered: seed prev states so a held button doesn't fire immediately.
            self._ps_up_prev, self._ps_down_prev = up, down
            self._ps_confirm_prev, self._ps_cancel_prev = confirm, cancel
            self._ps_cp_prev = cp_now
            self._ps_was_active = True
            self._ps_drain = False
            return

        if up and not self._ps_up_prev:
            utils.profile_nav(-1)
        if down and not self._ps_down_prev:
            utils.profile_nav(1)
        if cp_now and not getattr(self, "_ps_cp_prev", False):
            utils.profile_nav(1)
        if confirm and not self._ps_confirm_prev:
            utils.profile_confirm()
            self._ps_drain = True
        if cancel and not self._ps_cancel_prev:
            utils.profile_cancel()
            self._ps_drain = True

        self._ps_up_prev, self._ps_down_prev = up, down
        self._ps_confirm_prev, self._ps_cancel_prev = confirm, cancel
        self._ps_cp_prev = cp_now

    def _joystick_direction_tokens(self, key, direction_names):
        defaults = {"up": "VK_W", "down": "VK_S", "left": "VK_A", "right": "VK_D"}
        scope_dict = CONFIG.get_mapping_scope_dict(self._in_app_gyro_mapping_scope())
        mode = scope_dict.get(f"{key}_mapping", "Default")
        custom = scope_dict.get(f"{key}_custom", {}) if mode == "Custom" else {}
        hold_tokens = []
        tap_tokens_by_direction = {}
        for direction in direction_names:
            action = custom.get(direction, "Default") if mode == "Custom" else "Default"
            if action == "Custom":
                action = scope_dict.get(f"{key}_{direction}_mapping", "Default")
            hold, tap = self._action_to_joystick_tokens(action, defaults.get(direction))
            hold_tokens.extend(hold)
            if tap:
                tap_tokens_by_direction[direction] = tap
        return hold_tokens, tap_tokens_by_direction

    def _stick_sector_directions(self, stick):
        directions, _ = self._stick_sector_state(stick)
        return directions

    def _stick_sector_state(self, stick):
        x, y = stick
        magnitude = math.sqrt(x * x + y * y)
        center_deadzone = 0.40
        if magnitude < center_deadzone:
            return [], None
        sector = int(round((math.atan2(y, x) - math.pi / 2) / (math.pi / 4))) % 8
        sectors = [
            (["up"], "up"),
            (["up", "left"], "up_left"),
            (["left"], "left"),
            (["left", "down"], "left_down"),
            (["down"], "down"),
            (["down", "right"], "down_right"),
            (["right"], "right"),
            (["right", "up"], "right_up"),
        ]
        return sectors[sector]

    def _apply_joystick_tokens(self, key, hold_tokens, tap_tokens_by_direction, inputData, active_directions=None, input_state=None, center_reset=False):
        if not hasattr(self, "active_joystick_tokens"):
            self.active_joystick_tokens = {}
        if not hasattr(self, "joystick_tap_armed_inputs"):
            self.joystick_tap_armed_inputs = {}
        if not hasattr(self, "joystick_tap_armed_triggered_dirs"):
            self.joystick_tap_armed_triggered_dirs = {}
        if not hasattr(self, "joystick_tap_releases"):
            self.joystick_tap_releases = {}
        if not hasattr(self, "active_joystick_mouse_wheel"):
            self.active_joystick_mouse_wheel = {}

        now = time.perf_counter()
        active_directions = set(active_directions or [])
        old_tokens = self.active_joystick_tokens.get(key, set())
        new_tokens = set(hold_tokens)
        for token in old_tokens - new_tokens:
            if token.startswith("VK_") or token.startswith("MB_"):
                self._trigger_custom_os_key(token, False)
            elif token.startswith("MW_"):
                self.active_joystick_mouse_wheel.pop((key, token), None)
        for token in new_tokens - old_tokens:
            if token.startswith("VK_") or token.startswith("MB_"):
                self._trigger_custom_os_key(token, True)
        self.active_joystick_tokens[key] = new_tokens
        for token in new_tokens:
            if token.startswith("MW_"):
                wheel_key = (key, token)
                last_scroll = self.active_joystick_mouse_wheel.get(wheel_key, 0.0)
                if now - last_scroll > 0.05:
                    self._trigger_mouse_wheel_token(token)
                    self.active_joystick_mouse_wheel[wheel_key] = now

        armed_inputs = self.joystick_tap_armed_inputs.get(key, set())
        if not isinstance(armed_inputs, set):
            armed_inputs = {armed_inputs} if armed_inputs else set()
        previous_armed_inputs = set(armed_inputs)
        if center_reset:
            armed_inputs = set()
            previous_armed_inputs = set()
            self.joystick_tap_armed_triggered_dirs[key] = set()
        previous_triggered_dirs = set(self.joystick_tap_armed_triggered_dirs.get(key, set()))
        should_tap = input_state is not None and input_state not in armed_inputs
        if should_tap:
            # Keep only the actual input sector that just triggered; this clears all
            # other armed sectors without marking diagonal sectors as cardinal arms.
            trigger_directions = set(active_directions)
            if input_state and "_" in input_state:
                for previous_input in previous_armed_inputs:
                    if previous_input in ("up", "down", "left", "right") and previous_input in trigger_directions:
                        trigger_directions.discard(previous_input)
            elif input_state in ("up", "down", "left", "right"):
                for previous_input in previous_armed_inputs:
                    if isinstance(previous_input, str) and "_" in previous_input and input_state in previous_triggered_dirs:
                        trigger_directions.discard(input_state)
            armed_inputs = {input_state}
            self.joystick_tap_armed_triggered_dirs[key] = set(trigger_directions)
            for direction in trigger_directions:
                for token in tap_tokens_by_direction.get(direction, []):
                    if token.startswith("VK_") or token.startswith("MB_"):
                        self._trigger_custom_os_key(token, True)
                    elif token.startswith("BTN_"):
                        btn_name = token[4:]
                        if btn_name in SWITCH_BUTTONS:
                            inputData.buttons |= SWITCH_BUTTONS[btn_name]
                            inputData.custom_buttons_mask |= SWITCH_BUTTONS[btn_name]
                    elif token.startswith("MW_"):
                        self._trigger_mouse_wheel_token(token)
                        continue
                    self.joystick_tap_releases[(key, input_state, direction, token)] = now + 0.08
        self.joystick_tap_armed_inputs[key] = armed_inputs

        expired_taps = []
        for tap_key, release_time in list(self.joystick_tap_releases.items()):
            tap_owner = tap_key[0]
            token = tap_key[-1]
            if now >= release_time:
                if token.startswith("VK_") or token.startswith("MB_"):
                    still_held = any(token in tokens for tokens in self.active_joystick_tokens.values())
                    if not still_held:
                        self._trigger_custom_os_key(token, False)
                expired_taps.append(tap_key)
                continue
            if token.startswith("BTN_"):
                btn_name = token[4:]
                if btn_name in SWITCH_BUTTONS:
                    inputData.buttons |= SWITCH_BUTTONS[btn_name]
                    inputData.custom_buttons_mask |= SWITCH_BUTTONS[btn_name]
        for tap_key in expired_taps:
            self.joystick_tap_releases.pop(tap_key, None)

        for token in new_tokens:
            if token.startswith("BTN_"):
                btn_name = token[4:]
                if btn_name in SWITCH_BUTTONS:
                    inputData.buttons |= SWITCH_BUTTONS[btn_name]
                    inputData.custom_buttons_mask |= SWITCH_BUTTONS[btn_name]

    def _clear_joystick_input_state(self, key):
        if not hasattr(self, "active_joystick_tokens"):
            self.active_joystick_tokens = {}
        for token in self.active_joystick_tokens.pop(key, set()):
            if token.startswith("VK_") or token.startswith("MB_"):
                self._trigger_custom_os_key(token, False)
        if hasattr(self, "joystick_tap_armed_inputs"):
            self.joystick_tap_armed_inputs.pop(key, None)
        if hasattr(self, "joystick_tap_armed_triggered_dirs"):
            self.joystick_tap_armed_triggered_dirs.pop(key, None)
        if hasattr(self, "joystick_tap_releases"):
            for tap_key in list(self.joystick_tap_releases.keys()):
                if tap_key[0] == key:
                    token = tap_key[-1]
                    if token.startswith("VK_") or token.startswith("MB_"):
                        self._trigger_custom_os_key(token, False)
                    self.joystick_tap_releases.pop(tap_key, None)
        if hasattr(self, "active_joystick_mouse_wheel"):
            for wheel_key in list(self.active_joystick_mouse_wheel.keys()):
                if wheel_key[0] == key:
                    self.active_joystick_mouse_wheel.pop(wheel_key, None)
        if hasattr(self, "joystick_scroll_tap_armed"):
            self.joystick_scroll_tap_armed.pop(key, None)
        if hasattr(self, "joystick_scroll_last_time"):
            self.joystick_scroll_last_time.pop(key, None)
        if hasattr(self, "joystick_mouse_vectors"):
            self.joystick_mouse_vectors.pop(key, None)
            self._update_joystick_mouse_target()

    def _update_joystick_mouse_target(self):
        vectors = getattr(self, "joystick_mouse_vectors", {})
        self.jc_target_vx = sum(v[0] for v in vectors.values())
        self.jc_target_vy = sum(v[1] for v in vectors.values())
        self.joystick_mouse_active = any(abs(v[0]) > 0.001 or abs(v[1]) > 0.001 for v in vectors.values())

    def _apply_joystick_mouse(self, key, stick):
        if not hasattr(self, "joystick_mouse_vectors"):
            self.joystick_mouse_vectors = {}
        stick_deadzone = 0.05
        stick_magnitude = math.sqrt(stick[0] * stick[0] + stick[1] * stick[1])
        if stick_magnitude <= stick_deadzone:
            self.joystick_mouse_vectors[key] = (0.0, 0.0)
        else:
            scope = self._in_app_gyro_mapping_scope()
            stick_sens = float(CONFIG.get_joystick_setting_scoped(key, "mouse_sensitivity", 5.0, scope)) * 0.66
            normalized_mag = (stick_magnitude - stick_deadzone) / (1.0 - stick_deadzone)
            normalized_sx = (stick[0] / stick_magnitude) * normalized_mag
            normalized_sy = (stick[1] / stick_magnitude) * normalized_mag
            self.joystick_mouse_vectors[key] = (normalized_sx * stick_sens, normalized_sy * -stick_sens)
        self._update_joystick_mouse_target()

    def _apply_joystick_scroll_wheel(self, key, stick):
        now = time.perf_counter()
        if not hasattr(self, "joystick_scroll_last_time"):
            self.joystick_scroll_last_time = {}
        if not hasattr(self, "joystick_scroll_tap_armed"):
            self.joystick_scroll_tap_armed = {}
        if now - self.joystick_scroll_last_time.get(key, 0.0) < 0.03:
            return
        magnitude = math.sqrt(stick[0] * stick[0] + stick[1] * stick[1])
        deadzone = 0.20
        if magnitude <= deadzone:
            self.joystick_scroll_tap_armed.pop(key, None)
            return
        normalized_mag = (magnitude - deadzone) / (1.0 - deadzone)
        scope = self._in_app_gyro_mapping_scope()
        mode = CONFIG.get_joystick_setting_scoped(key, "scroll_mode", "Up/Down", scope)
        activation = CONFIG.get_joystick_setting_scoped(key, "scroll_activation", "Hold", scope)
        vertical = 0
        horizontal = 0
        step = max(1, int(120 * normalized_mag))
        directions, _ = self._stick_sector_state(stick)
        input_state = "_".join(directions) if directions else None
        if activation == "Tap":
            if input_state is None or self.joystick_scroll_tap_armed.get(key) == input_state:
                return
            self.joystick_scroll_tap_armed[key] = input_state
        if mode == "Up/Down":
            if "up" in directions:
                vertical = step
            elif "down" in directions:
                vertical = -step
        else:
            if "up" in directions:
                vertical = step
            elif "down" in directions:
                vertical = -step
            if "right" in directions:
                horizontal = step
            elif "left" in directions:
                horizontal = -step
        if vertical:
            win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, vertical, 0)
        if horizontal:
            win32api.mouse_event(getattr(win32con, "MOUSEEVENTF_HWHEEL", 0x01000), 0, 0, horizontal, 0)
        if vertical or horizontal:
            self.joystick_scroll_last_time[key] = now

    def _trigger_mouse_wheel_token(self, token):
        if token == "MW_UP":
            win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, 120, 0)
        elif token == "MW_DOWN":
            win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, -120, 0)

    def _apply_shared_joystick_mapping(self, inputData):
        scope_dict = CONFIG.get_mapping_scope_dict(self._in_app_gyro_mapping_scope())
        left_mode = scope_dict.get("l_joystick_mapping", "Default")
        right_mode = scope_dict.get("r_joystick_mapping", "Default")
        original_left = inputData.left_stick
        original_right = inputData.right_stick
        output_left = original_left
        output_right = original_right
        process_left = not self.is_joycon_right()
        process_right = not self.is_joycon_left()
        is_dual_stick_controller = not self.is_joycon()

        def rotate_stick_for_mapping(key, stick):
            if getattr(self, "hold_mode", "Vertical") != "Horizontal":
                return stick
            x, y = stick
            if key == "l_joystick" and self.is_joycon_left():
                return (-y, x)
            if key == "r_joystick" and self.is_joycon_right():
                return (y, -x)
            return stick

        def consume_stick(key, mode, stick):
            mapped_stick = rotate_stick_for_mapping(key, stick)
            if mode != "Mouse" and hasattr(self, "joystick_mouse_vectors"):
                self.joystick_mouse_vectors.pop(key, None)
                self._update_joystick_mouse_target()
            if mode == "Mouse":
                self._apply_joystick_mouse(key, mapped_stick)
                self._apply_joystick_tokens(key, [], {}, inputData, center_reset=True)
                return True
            if mode == "Scroll Wheel":
                self._apply_joystick_scroll_wheel(key, mapped_stick)
                self._apply_joystick_tokens(key, [], {}, inputData, center_reset=True)
                return True
            directions, input_state = self._stick_sector_state(mapped_stick)
            magnitude = math.sqrt(mapped_stick[0] * mapped_stick[0] + mapped_stick[1] * mapped_stick[1])
            center_deadzone = 0.20
            hold_tokens, tap_tokens = self._joystick_direction_tokens(key, directions) if mode in ("WASD", "Custom") else ([], {})
            self._apply_joystick_tokens(
                key,
                hold_tokens,
                tap_tokens,
                inputData,
                active_directions=directions,
                input_state=input_state,
                center_reset=magnitude < center_deadzone,
            )
            return mode in ("WASD", "Custom")

        active_modes = {
            "l_joystick": left_mode if process_left else None,
            "r_joystick": right_mode if process_right else None,
        }
        if not hasattr(self, "joystick_mapping_active_modes"):
            self.joystick_mapping_active_modes = {}
        for key, mode in active_modes.items():
            if self.joystick_mapping_active_modes.get(key) != mode:
                self._clear_joystick_input_state(key)
                self.joystick_mapping_active_modes[key] = mode

        if process_left and left_mode == "R Joystick":
            if not is_dual_stick_controller:
                output_left = (0.0, 0.0)
                output_right = original_left
                inputData.custom_joystick_mapping = {"source": "left", "target": "right", "stick": original_left}
            self._apply_joystick_tokens("l_joystick", [], {}, inputData, center_reset=True)
        elif process_left and left_mode == "L Joystick":
            if not is_dual_stick_controller:
                inputData.custom_joystick_mapping = {"source": "left", "target": "left", "stick": original_left}
            self._apply_joystick_tokens("l_joystick", [], {}, inputData, center_reset=True)
        elif process_left and consume_stick("l_joystick", left_mode, original_left):
            output_left = (0.0, 0.0)
        else:
            self._apply_joystick_tokens("l_joystick", [], {}, inputData, center_reset=True)

        if process_right and right_mode == "L Joystick":
            if not is_dual_stick_controller:
                output_right = (0.0, 0.0)
                output_left = original_right
                inputData.custom_joystick_mapping = {"source": "right", "target": "left", "stick": original_right}
            self._apply_joystick_tokens("r_joystick", [], {}, inputData, center_reset=True)
        elif process_right and right_mode == "R Joystick":
            if not is_dual_stick_controller:
                inputData.custom_joystick_mapping = {"source": "right", "target": "right", "stick": original_right}
            self._apply_joystick_tokens("r_joystick", [], {}, inputData, center_reset=True)
        elif process_right and consume_stick("r_joystick", right_mode, original_right):
            output_right = (0.0, 0.0)
        else:
            self._apply_joystick_tokens("r_joystick", [], {}, inputData, center_reset=True)

        if process_left and left_mode == "L Joystick":
            output_left = original_left
        if process_right and right_mode == "R Joystick":
            output_right = original_right

        inputData.left_stick = output_left
        inputData.right_stick = output_right

    def _trigger_custom_os_key(self, k, is_down):
        if k.startswith("VK_"):
            vk_name = k[3:]
            import win32con
            import win32api
            vk_code = getattr(win32con, f"VK_{vk_name}", None)
            if vk_code is None:
                if len(vk_name) == 1:
                    vk_code = ord(vk_name)
                elif vk_name in ("CONTROL", "CONTROL_L", "CONTROL_R"): vk_code = win32con.VK_CONTROL
                elif vk_name in ("SHIFT", "SHIFT_L", "SHIFT_R"): vk_code = win32con.VK_SHIFT
                elif vk_name in ("ALT_L", "ALT_R"): vk_code = win32con.VK_MENU
                elif vk_name == "MENU": vk_code = win32con.VK_MENU
                elif vk_name == "SPACE": vk_code = win32con.VK_SPACE
                elif vk_name == "RETURN": vk_code = win32con.VK_RETURN
                elif vk_name == "TAB": vk_code = win32con.VK_TAB
                elif vk_name == "ESCAPE": vk_code = win32con.VK_ESCAPE
                elif vk_name == "BACKSPACE": vk_code = win32con.VK_BACK
                elif vk_name == "UP": vk_code = win32con.VK_UP
                elif vk_name == "DOWN": vk_code = win32con.VK_DOWN
                elif vk_name == "LEFT": vk_code = win32con.VK_LEFT
                elif vk_name == "RIGHT": vk_code = win32con.VK_RIGHT
            if vk_code:
                if is_down:
                    win32api.keybd_event(vk_code, 0, 0, 0)
                else:
                    win32api.keybd_event(vk_code, 0, win32con.KEYEVENTF_KEYUP, 0)
        elif k.startswith("MB_"):
            btn = k[3:]
            import win32con
            import win32api
            flags = 0
            if btn == "1": flags = win32con.MOUSEEVENTF_LEFTDOWN if is_down else win32con.MOUSEEVENTF_LEFTUP
            elif btn == "2": flags = win32con.MOUSEEVENTF_MIDDLEDOWN if is_down else win32con.MOUSEEVENTF_MIDDLEUP
            elif btn == "3": flags = win32con.MOUSEEVENTF_RIGHTDOWN if is_down else win32con.MOUSEEVENTF_RIGHTUP
            if flags:
                win32api.mouse_event(flags, 0, 0, 0, 0)

    def _interpolation_thread_loop(self):
        last_time = time.perf_counter()
        while self.interp_running:
            if self.client and self.client.is_connected and (self.gyro_mouse_enabled or getattr(self, 'jc_mouse_active', False) or getattr(self, 'joystick_mouse_active', False)):
                if getattr(self, 'is_calibrating', False):
                    self.current_vx = 0.0
                    self.current_vy = 0.0
                else:
                    self.current_vx = self.gyro_target_vx + getattr(self, 'jc_target_vx', 0.0)
                    self.current_vy = self.gyro_target_vy + getattr(self, 'jc_target_vy', 0.0)

                now = time.perf_counter()
                dt = now - last_time
                last_time = now
                
                if dt > 0.05: dt = 0.015 

                time_scale = dt / 0.001
                step_x = self.current_vx * time_scale
                step_y = self.current_vy * time_scale

                total_dx = step_x + self.interp_residual_x
                total_dy = step_y + self.interp_residual_y

                move_x = int(total_dx)
                move_y = int(total_dy)

                self.interp_residual_x = total_dx - move_x
                self.interp_residual_y = total_dy - move_y

                if move_x != 0 or move_y != 0:
                    win32api.mouse_event(win32con.MOUSEEVENTF_MOVE, move_x, move_y, 0, 0)
            else:
                last_time = time.perf_counter()

            time.sleep(0.001)

    ### Info Helpers ###

    def is_joycon_right(self):
        return self.controller_info.product_id == JOYCON2_RIGHT_PID

    def is_joycon_left(self):
        return self.controller_info.product_id == JOYCON2_LEFT_PID
    
    def is_joycon(self):
        return self.is_joycon_left() or self.is_joycon_right()
    
    def is_pro_controller(self):
        return self.controller_info.product_id in (PRO_CONTROLLER2_PID, PRO_CONTROLLER_PID, NSO_GAMECUBE_CONTROLLER_PID)

    def has_second_stick(self):
        return self.controller_info.product_id in [PRO_CONTROLLER2_PID, PRO_CONTROLLER_PID, NSO_GAMECUBE_CONTROLLER_PID]
