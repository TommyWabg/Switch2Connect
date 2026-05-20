import bleak
from bleak import BleakScanner, BleakClient, BleakGATTCharacteristic, BleakError
from bleak.backends.device import BLEDevice
import asyncio
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
from config import CONFIG, SWITCH_BUTTONS
from utils import (
    apply_calibration_to_axis, get_stick_xy, press_or_release_mouse_button,
    reverse_bits, signed_looping_difference_16bit, to_hex, decodeu, decodes, 
    convert_mac_string_to_value, vector_normalize, vector_cross, vector_dot,
    quaternion_multiply, quaternion_normalize, quaternion_rotate_vector,
    quaternion_from_vectors, show_notification, force_ui_update
)

logging.basicConfig(
    format='%(asctime)s.%(msecs)03d %(levelname)s:%(name)s:%(message)s',
    datefmt='%H:%M:%S'
)
logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# Controller identification info
NINTENDO_VENDOR_ID = 0x057e
JOYCON2_RIGHT_PID = 0x2066
JOYCON2_LEFT_PID = 0x2067
PRO_CONTROLLER2_PID = 0x2069
NSO_GAMECUBE_CONTROLLER_PID = 0x2073

CONTROLER_NAMES = {
    JOYCON2_RIGHT_PID: "Joy-con 2 (Right)",
    JOYCON2_LEFT_PID: "Joy-con 2 (Left)",
    PRO_CONTROLLER2_PID: "Pro Controller 2",
    NSO_GAMECUBE_CONTROLLER_PID: "NSO Gamecube Controller"
}

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

@dataclass
class StickCalibrationData:
    center: tuple[int, int]
    max: tuple[int, int]
    min: tuple[int, int]

    def __init__(self, data: bytes):
        if len(data) >= 9:
            self.center = get_stick_xy(data[0:3])
            # Max/min are absolute offsets from center
            self.max = get_stick_xy(data[3:6])
            self.min = get_stick_xy(data[6:9])
            
            # Sanity check: if calibration is all zeros/FF, use defaults to prevent stuck stick
            if self.center == (0, 0) and self.max == (0, 0):
                self.center = (2048, 2048)
                self.max = (1500, 1500)
                self.min = (1500, 1500)
        else:
            self.center = (2048, 2048)
            self.max = (1500, 1500)
            self.min = (1500, 1500)

    def apply_calibration(self, raw_values: tuple[int, int]):
        return (apply_calibration_to_axis(raw_values[0], self.center[0], self.max[0], self.min[0]), 
                apply_calibration_to_axis(raw_values[1], self.center[1], self.max[1], self.min[1]))

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

    def __init__(self, data: bytes, left_stick_calibration: StickCalibrationData, right_stick_calibration: StickCalibrationData):
        self.raw_data = data
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

        if left_stick_calibration:
            self.left_stick = left_stick_calibration.apply_calibration(self.left_stick)
        if right_stick_calibration:
            self.right_stick = right_stick_calibration.apply_calibration(self.right_stick)
            
    

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
            show_notification("Switch 2 Controller", "Gyro calibration starts in 5 seconds. Please keep the controller stationary.")
        
        force_ui_update()
    
    async def connect(self):
        if (self.client is not None):
            raise Exception("Already connected")
        
        def disconnected_callback(client: BleakClient):
            if (self.disconnected_callback is not None):
                asyncio.create_task(self.disconnected_callback(self))
        
        try:
            self.client = BleakClient(self.device, disconnected_callback=disconnected_callback)
            await self.client.connect(timeout=20.0)
            
            logger.info(f"Connected to {self.device.address}")
            
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
                            device = getattr(self.client._backend, "_requester", None)
                            if device:
                                if hasattr(device, 'request_preferred_connection_parameters_async'):
                                    await device.request_preferred_connection_parameters_async(params)
                                elif hasattr(device, 'request_preferred_connection_parameters'):
                                    device.request_preferred_connection_parameters(params)
                                logger.info(f"ThroughputOptimized applied for {self.device.address}")
                        else:
                            logger.info("ThroughputOptimized not available on this Windows version.")
                    except Exception as e:
                        logger.warning(f"Failed to apply ThroughputOptimized (non-fatal): {e}")

            # Allow the connection to stabilize
            await asyncio.sleep(2.0)
            
            # Explicit check before starting notification
            if not self.client.is_connected:
                logger.error(f"Device {self.device.address} disconnected before notify")
                raise BleakError("Disconnected during setup")

            self.response_future = None
            def command_response_callback(sender: BleakGATTCharacteristic, data: bytearray):
                if self.response_future:
                    self.response_future.set_result(data)
            
            logger.info(f"Starting command response notification for {self.device.address}...")
            for attempt in range(3):
                if not self.client.is_connected:
                    raise BleakError("Connection lost during notify retry")
                try:
                    await self.client.start_notify(COMMAND_RESPONSE_UUID, command_response_callback)
                    break
                except Exception as e:
                    if attempt == 2: raise
                    logger.warning(f"Notify failed, retry {attempt+1}: {e}")
                    await asyncio.sleep(2.0)

            self.controller_info = await self.read_controller_info()
            
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
                
            self.stick_calibration, self.second_stick_calibration = await self.read_calibration_data()

            await self.enable_input_notify_callback()
            
            await self.enableFeatures(FEATURE_MOTION | FEATURE_MOUSE | FEATURE_MAGNOMETER)

            self.interp_running = True
            self.interp_thread = threading.Thread(target=self._interpolation_thread_loop, daemon=True)
            self.interp_thread.start()
        except Exception:
            await self.disconnect()
            raise

        logger.info(f"Successfully initialized {self.device.address} ({self.controller_info.product_id:04x}) : {self.controller_info}")
        try:
            bass_thump = VibrationData(lf_freq=0x060, lf_amp=0x350, hf_freq=0x0c0, hf_amp=0x250)
            sharp_click = VibrationData(hf_freq=0x1e2, hf_amp=0x300, lf_amp=0x030)
            stop_vibration = VibrationData() 

            await self.set_vibration(bass_thump)
            await asyncio.sleep(0.2) 
            
            await self.set_vibration(stop_vibration)
            await asyncio.sleep(0.01) 
            
            await self.set_vibration(sharp_click)
            await asyncio.sleep(1) 
            
            await self.set_vibration(stop_vibration)
            logger.info("Connection haptic feedback triggered.")
        except Exception as e:
            logger.warning(f"Failed to trigger haptic feedback: {e}")

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

    async def write_command(self, command_id: int, subcommand_id: int, command_data = b''):
        command_buffer = command_id.to_bytes() + b"\x91\x01" + subcommand_id.to_bytes() + b"\x00" + len(command_data).to_bytes() + b"\x00\x00" + command_data
        self.response_future = asyncio.get_running_loop().create_future()
        await self.client.write_gatt_char(COMMAND_WRITE_UUID, command_buffer)
        response_buffer = await self.response_future
        if len(response_buffer) < 8 or response_buffer[0] != command_id or response_buffer[1] != 0x01:
            raise Exception(f"Unexpected response : {response_buffer}")
        return response_buffer[8:]

    async def enableFeatures(self, feature_flags: int):
        await self.write_command(COMMAND_FEATURE, SUBCOMMAND_FEATURE_INIT, feature_flags.to_bytes().ljust(4, b'\0'))
        await self.write_command(COMMAND_FEATURE, SUBCOMMAND_FEATURE_ENABLE, feature_flags.to_bytes().ljust(4, b'\0'))

    async def set_vibration(self, vibration: VibrationData, vibration2 = VibrationData(), vibration3 = VibrationData()):
        motor_vibrations = (0x50 + (self.vibration_packet_id & 0x0F)).to_bytes() + vibration.get_bytes() + vibration2.get_bytes() + vibration3.get_bytes()
        if self.is_joycon_left():
            await self.client.write_gatt_char(VIBRATION_WRITE_JOYCON_L_UUID, (b'\x00' + motor_vibrations))
        elif self.is_joycon_right():
            await self.client.write_gatt_char(VIBRATION_WRITE_JOYCON_R_UUID, (b'\x00' + motor_vibrations))
        elif self.is_pro_controller():
            await self.client.write_gatt_char(VIBRATION_WRITE_PRO_CONTROLLER_UUID, (b'\x00' + motor_vibrations + motor_vibrations))
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

    async def pair(self):
        from utils import get_local_mac_value
        mac_value = get_local_mac_value()
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
            
            # Debug log for the first few packets to see what's being sent on wake
            if not hasattr(self, '_packet_count'): self._packet_count = 0
            if self._packet_count < 3:
                self._packet_count += 1
                logger.debug(f"[{time.strftime('%H:%M:%S')}] Controller {self.device.address} first packet {self._packet_count}: {to_hex(data[3:6])}")
                
            inputData = ControllerInputData(data, self.stick_calibration, self.second_stick_calibration)
            self.battery_voltage = inputData.battery_voltage
            self.last_accel = inputData.accelerometer

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
                mx, my, mz = inputData.magnometer
                self._mahony_update(gyro_x, gyro_y, gyro_z, ax, ay, az, mx, my, mz, dt)

            btn_states = {
                "GL": bool(inputData.buttons & 0x02000000),
                "GR": bool(inputData.buttons & 0x01000000),
                "C":  bool(inputData.buttons & 0x00004000),
                "CAPT": bool(inputData.buttons & 0x00002000),
                "SL_L": bool(inputData.buttons & 0x00200000),
                "SR_L": bool(inputData.buttons & 0x00100000),
                "SL_R": bool(inputData.buttons & 0x00000020),
                "SR_R": bool(inputData.buttons & 0x00000010)
            }

            inputData.buttons &= ~(0x03306030)

            trigger_gyro = False
            trigger_screenshot = btn_states["CAPT"]
            trigger_key_c = False

            mapping_pairs = [
                (btn_states["GL"], getattr(CONFIG, "gl_mapping", "None"), 0x02000000),
                (btn_states["GR"], getattr(CONFIG, "gr_mapping", "None"), 0x01000000),
                (btn_states["C"],  getattr(CONFIG, "c_mapping", "None"), 0x00004000),
                (btn_states["SL_L"], getattr(CONFIG, "sll_mapping", "None"), 0x00200000),
                (btn_states["SR_L"], getattr(CONFIG, "srl_mapping", "None"), 0x00100000),
                (btn_states["SL_R"], getattr(CONFIG, "slr_mapping", "None"), 0x00000020),
                (btn_states["SR_R"], getattr(CONFIG, "srr_mapping", "None"), 0x00000010)
            ]

            trigger_calibration = False
            for is_pressed, action, original_bit in mapping_pairs:
                if is_pressed:
                    if action == "Gyro": trigger_gyro = True
                    elif action == "CAPT": trigger_screenshot = True
                    elif action == "C": trigger_key_c = True
                    elif action == "Calibration": trigger_calibration = True
                    elif action == "None":
                        inputData.buttons |= original_bit
                    elif action in SWITCH_BUTTONS:
                        inputData.buttons |= SWITCH_BUTTONS[action]

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

            raw_left_pressed  = bool(inputData.buttons & 0x01)
            raw_up_pressed    = bool(inputData.buttons & 0x02)
            raw_down_pressed  = bool(inputData.buttons & 0x04)
            raw_right_pressed = bool(inputData.buttons & 0x08)
            inputData.buttons &= ~0x0F
            
            abxy_mode = getattr(CONFIG, "abxy_mode", "Xbox")
            if abxy_mode == "Switch":
                if raw_down_pressed:  inputData.buttons |= 0x08
                if raw_right_pressed: inputData.buttons |= 0x04
                if raw_left_pressed:  inputData.buttons |= 0x02
                if raw_up_pressed:    inputData.buttons |= 0x01
            else:
                if raw_right_pressed: inputData.buttons |= 0x08
                if raw_down_pressed:  inputData.buttons |= 0x04
                if raw_up_pressed:    inputData.buttons |= 0x02
                if raw_left_pressed:  inputData.buttons |= 0x01

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

            if inputData.buttons & (SWITCH_BUTTONS.get("SR_R", 0) | SWITCH_BUTTONS.get("SL_R", 0) | SWITCH_BUTTONS.get("SL_L", 0) | SWITCH_BUTTONS.get("SR_L", 0)):
                self.side_buttons_pressed = True

            if getattr(self, 'is_calibrating', False) or getattr(self, 'is_mag_calibrating', False):
                self.simulate_gyro_mouse(inputData, False, False, False)
            else:
                self.simulate_mouse(inputData)
                # Record own trigger state and use shared trigger (for combined mode cross-controller activation)
                self._own_gyro_trigger = trigger_gyro
                self._own_zr_pressed = bool(inputData.buttons & SWITCH_BUTTONS.get("ZR", 0))
                self._own_zl_pressed = bool(inputData.buttons & SWITCH_BUTTONS.get("ZL", 0))
                
                effective_gyro_trigger = trigger_gyro or getattr(self, '_shared_gyro_trigger', False)
                effective_zr = self._own_zr_pressed or getattr(self, '_shared_zr_pressed', False)
                effective_zl = self._own_zl_pressed or getattr(self, '_shared_zl_pressed', False)
                
                self.simulate_gyro_mouse(inputData, effective_gyro_trigger, effective_zr, effective_zl)


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
                        if self.is_joycon_right():
                            gx_comp = 0.0
                            gy_comp = decoupled_pitch
                            gz_comp = decoupled_yaw
                        else:
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

            if self.input_report_callback is not None:
                self.input_report_callback(inputData, self)

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
            self.jc_mouse_active = True 
            
            if inputData.mouse_distance != 0 and inputData.mouse_distance < 1000 and inputData.mouse_roughness < 4000:
                x, y = inputData.mouse_coords
                mouseButtonsConfig = mouse_config.joycon_l_buttons if self.is_joycon_left() else mouse_config.joycon_r_buttons
                lb = inputData.buttons & mouseButtonsConfig.left_button
                mb = inputData.buttons & mouseButtonsConfig.middle_button
                rb = inputData.buttons & mouseButtonsConfig.right_button
                
                inputData.buttons &= ~(mouseButtonsConfig.left_button | mouseButtonsConfig.middle_button | mouseButtonsConfig.right_button)

                if getattr(self, 'previous_mouse_state', None) is not None:
                    dx = signed_looping_difference_16bit(self.previous_mouse_state.x, x)
                    dy = signed_looping_difference_16bit(self.previous_mouse_state.y ,y)

                    if dx != 0 or dy != 0:
                        self.jc_target_vx = dx * mouse_config.sensitivity * 0.009
                        self.jc_target_vy = dy * mouse_config.sensitivity * 0.009
                    else:
                        self.jc_target_vx = 0.0
                        self.jc_target_vy = 0.0

                    mx, my = win32api.GetCursorPos()
                    press_or_release_mouse_button(lb, self.previous_mouse_state.lb, win32con.MOUSEEVENTF_LEFTDOWN, mx, my)
                    press_or_release_mouse_button(mb, self.previous_mouse_state.mb, win32con.MOUSEEVENTF_MIDDLEDOWN, mx, my)
                    press_or_release_mouse_button(rb, self.previous_mouse_state.rb, win32con.MOUSEEVENTF_RIGHTDOWN, mx, my)

                    if self.is_joycon_right():
                        scroll_value = inputData.right_stick[1]
                    else:
                        scroll_value = inputData.left_stick[1]

                    if abs(scroll_value) > 0.2:
                        win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, int(scroll_value * 60 * mouse_config.scroll_sensitivity), 0)
                        
                self.previous_mouse_state = MouseState(x, y, bool(lb), bool(mb), bool(rb))
            else:
                self.previous_mouse_state = None
                self.jc_target_vx = 0.0
                self.jc_target_vy = 0.0
        else:
            self.jc_mouse_active = False
            self.jc_target_vx = 0.0
            self.jc_target_vy = 0.0

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

        if not getattr(self, 'gyro_active', True):
            # Reset all speed states to prevent drift when switching Gyro sides
            self.gyro_target_vx = 0.0
            self.gyro_target_vy = 0.0
            self.current_vx = 0.0
            self.current_vy = 0.0
            self.interp_residual_x = 0.0
            self.interp_residual_y = 0.0
            self.gyro_mouse_enabled = False
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
                self._reset_orientation_from_accel(ax, ay, az)
                self.gyro_start_time = time.perf_counter()
            self.gyro_mouse_enabled = trigger_pressed
        else:
            if trigger_pressed and not self.gr_was_pressed:
                self.gyro_mouse_enabled = not self.gyro_mouse_enabled
                if self.gyro_mouse_enabled:
                    # Reset orientation on activation to prevent jumps
                    self._reset_orientation_from_accel(ax, ay, az)
                    self.gyro_start_time = time.perf_counter() 
                
        self.gr_was_pressed = trigger_pressed

        if self.gyro_mouse_enabled:
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

            # Hybrid Mouse Button Mapping (Only if NOT in Steering/Roll mode)
            if current_mode != "Roll":
                is_merged = getattr(self, "is_merged", False)
                is_pro = self.is_pro_controller()
                
                if is_pro or is_merged:
                    # Dual / Pro Mode: ZR is Left, ZL is Right
                    current_l_click = zr_pressed
                    current_r_click = zl_pressed
                else:
                    # Split Mode (Single Joycon behavior)
                    if self.is_joycon_right():
                        current_l_click = bool(inputData.buttons & SWITCH_BUTTONS.get("ZR", 0))
                        current_r_click = bool(inputData.buttons & SWITCH_BUTTONS.get("R", 0))
                    else:
                        current_l_click = bool(inputData.buttons & SWITCH_BUTTONS.get("ZL", 0))
                        current_r_click = bool(inputData.buttons & SWITCH_BUTTONS.get("L", 0))

                # Detect button press and release for clicks
                prev_l_click = getattr(self, 'prev_l_click', False)
                prev_r_click = getattr(self, 'prev_r_click', False)

                # Inject mouse clicks immediately
                if current_l_click and not prev_l_click: win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                elif not current_l_click and prev_l_click: win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                
                if current_r_click and not prev_r_click: win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
                elif not current_r_click and prev_r_click: win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)

                # Track click stabilization window (ONLY on press / down event)
                if (current_l_click and not prev_l_click) or (current_r_click and not prev_r_click):
                    self.last_click_event_time = now

                self.prev_l_click = current_l_click
                self.prev_r_click = current_r_click

            # Suppress movement ONLY during gyro startup (Auto-Leveling period)
            if now - self.gyro_start_time < 0.05:
                self.gyro_target_vx = 0.0
                self.gyro_target_vy = 0.0
                return
            
            gyro_deadzone = 0.2 
            
            if current_mode in ["World", "Yaw"]:
                sensitivity = getattr(CONFIG, "gyro_sensitivity", 0.3)
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
                
                tilt_normalized = tilt_value / 4000.0  
                sensitivity = getattr(CONFIG, "gyro_sensitivity", 4.0)
                # Sensitivity * 1.0 (Inverted sign based on user feedback)
                steer_value = max(-1.0, min(1.0, -tilt_normalized * sensitivity))
                
                # Store for virtual controller to apply to correct virtual axis
                self._own_steer_value = steer_value


            # Analog Stick Mouse Movement (Stick Assist) - Only if NOT in Steering mode
            if current_mode != "Roll":
                stick_deadzone = 0.05 
                stick_sens = getattr(CONFIG, "stick_mouse_sensitivity", 20.0) * 0.66
                
                stick_magnitude = math.sqrt(sx**2 + sy**2)
                
                if stick_magnitude > stick_deadzone:
                    normalized_mag = (stick_magnitude - stick_deadzone) / (1.0 - stick_deadzone)
                    normalized_sx = (sx / stick_magnitude) * normalized_mag
                    normalized_sy = (sy / stick_magnitude) * normalized_mag
                    
                    target_vx += normalized_sx * stick_sens
                    target_vy += normalized_sy * -stick_sens

            self.gyro_target_vx = target_vx
            self.gyro_target_vy = target_vy

        else:
            self.gyro_target_vx = 0.0
            self.gyro_target_vy = 0.0
            self._own_steer_value = 0.0
            if getattr(self, 'prev_l_click', False): win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            if getattr(self, 'prev_r_click', False): win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
            self.prev_l_click = self.prev_r_click = False
            self.gyro_residual_x = self.gyro_residual_y = 0.0
            self.current_vx = self.current_vy = 0.0
            self.interp_residual_x = self.interp_residual_y = 0.0

    def _interpolation_thread_loop(self):
        last_time = time.perf_counter()
        while self.interp_running:
            if self.client and self.client.is_connected and (self.gyro_mouse_enabled or getattr(self, 'jc_mouse_active', False)):
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
        return self.controller_info.product_id == PRO_CONTROLLER2_PID

    def has_second_stick(self):
        return self.controller_info.product_id in [PRO_CONTROLLER2_PID, NSO_GAMECUBE_CONTROLLER_PID]
