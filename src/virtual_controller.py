import winuhid_client as vgamepad
import asyncio
import threading
import ctypes
import logging
import gc
from controller import Controller, ControllerInputData, VibrationData
from config import CONFIG, ButtonConfig, SWITCH_BUTTONS, XB_BUTTONS

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



class VirtualController:
    def __init__(self, player_number: int, on_disconnected_callback=None):
        self.player_number = player_number
        self.controllers = []
        self.on_disconnected_callback = on_disconnected_callback
        self.previous_buttons_left = 0x00000000
        self.previous_buttons_right = 0x00000000
        self.next_vibration_event = None
        self.vg_controller = None
        self.loop = None
        self.touch_tracking_id = 0
        self.was_touching = False
        
        self.hold_mode = "Horizontal"
        self.active_gyro_side = "Right"
        
        self.mode = getattr(CONFIG, "simulation_mode", "Xbox")
        self._setup_vg_controller()
        
        self.state_lock = threading.Lock()
        self._disconnect_lock = asyncio.Lock()
        self.running = True
        self.update_thread = threading.Thread(target=self._1000hz_loop, daemon=True)
        self.update_thread.start()

    def _setup_vg_controller(self):
        if self.vg_controller is not None:
            try:
                self.vg_controller.unregister_notification()
            except Exception:
                pass
            try:
                self.vg_controller.close()
            except Exception:
                pass
            self.vg_controller = None

        if self.mode == "PS4":
            self.vg_controller = vgamepad.VDS4Gamepad()
            self.report = self.vg_controller.report
            self.report.LeftStickX = 128
            self.report.LeftStickY = 128
            self.report.RightStickX = 128
            self.report.RightStickY = 128
            self.report.BatteryLevel = 0xAF
            self.report.BatteryLevelSpecial = 0x08
            logger.info("Switched to virtual PS4 controller via WinUHid")
        elif self.mode == "PS5":
            self.vg_controller = vgamepad.VDS5Gamepad()
            self.report = self.vg_controller.report
            self.report.LeftStickX = 128
            self.report.LeftStickY = 128
            self.report.RightStickX = 128
            self.report.RightStickY = 128
            self.report.BatteryPercent = 10
            self.report.BatteryState = 2
            logger.info("Switched to virtual PS5 controller via WinUHid")
        else:
            self.vg_controller = vgamepad.VX360Gamepad()
            logger.info("Switched to virtual Xbox 360 controller via WinUHid")

        self.vg_controller.register_notification(callback_function=self.vibration_callback)

    def set_mode(self, new_mode):
        if self.mode != new_mode:
            self.mode = new_mode
            self._setup_vg_controller()
            if self.loop and self.loop.is_running():
                asyncio.run_coroutine_threadsafe(self.update_leds(), self.loop)

    def vibration_callback(self, client, target, large_motor, small_motor, led_number, user_data):
        vibrationData = VibrationData()
        vibrationData.lf_amp = int(800 * large_motor / 256)
        vibrationData.hf_amp = int(800 * small_motor / 256)

        if self.next_vibration_event:
            self.next_vibration_event.set()
        
        self.next_vibration_event = asyncio.Event()
        if large_motor == 0 and small_motor == 0:
            self.next_vibration_event.set()
            # Send zero vibration to physical controllers immediately
            async def send_zero_vibration():
                zero_vib = VibrationData()
                zero_vib.lf_amp = 0
                zero_vib.hf_amp = 0
                tasks = [c.set_vibration(zero_vib) for c in self.controllers]
                await asyncio.gather(*tasks)
            if self.loop and self.loop.is_running():
                asyncio.run_coroutine_threadsafe(send_zero_vibration(), self.loop)
            return

        stop_event = self.next_vibration_event
        async def send_vibration_task():
            for _ in range(500):
                if stop_event.is_set(): break
                tasks = [c.set_vibration(vibrationData) for c in self.controllers]
                await asyncio.gather(*tasks)
                await asyncio.sleep(0.02)

        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(send_vibration_task(), self.loop)

    async def init_added_controller(self, controller: Controller):
        controller.virtual_controller = self
        self.loop = asyncio.get_running_loop()
        await self.update_leds()

        if self.is_single() and controller.is_joycon():
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
        
        def input_report_callback(inputData: ControllerInputData, controller: Controller):
            
            if self.vg_controller is None:
                return
            
            if len(self.controllers) == 2 or controller.is_pro_controller():
                controller.gyro_active = (controller.is_joycon_left() and self.active_gyro_side == "Left") or (controller.is_joycon_right() and self.active_gyro_side == "Right") or controller.is_pro_controller()
                controller.hold_mode = "Vertical"
            else:
                controller.gyro_active = True
                controller.hold_mode = getattr(self, "hold_mode", "Horizontal")
            
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
            
            if len(self.controllers) == 1:
                if controller.is_joycon_left():
                    if self.hold_mode == "Vertical":
                        inputData.right_stick = inputData.left_stick
                        inputData.left_stick = (0, 0)
                        
                        new_btns = current_buttons & ~(SWITCH_BUTTONS["UP"] | SWITCH_BUTTONS["DOWN"] | SWITCH_BUTTONS["LEFT"] | SWITCH_BUTTONS["RIGHT"] | SWITCH_BUTTONS["L"] | SWITCH_BUTTONS["ZL"] | SWITCH_BUTTONS["L_STK"] | SWITCH_BUTTONS["MINUS"])
                        
                        if current_buttons & SWITCH_BUTTONS["L_STK"]:
                            new_btns |= SWITCH_BUTTONS["R_STK"]
                            
                        if CONFIG.abxy_mode == "Switch":
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
                        
                        if CONFIG.abxy_mode == "Switch":
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
                        
                        if CONFIG.abxy_mode == "Switch":
                            if current_buttons & SWITCH_BUTTONS["A"]: new_btns |= SWITCH_BUTTONS["X"]
                            if current_buttons & SWITCH_BUTTONS["X"]: new_btns |= SWITCH_BUTTONS["Y"]
                            if current_buttons & SWITCH_BUTTONS["B"]: new_btns |= SWITCH_BUTTONS["A"]
                            if current_buttons & SWITCH_BUTTONS["Y"]: new_btns |= SWITCH_BUTTONS["B"]
                        else:
                            if current_buttons & SWITCH_BUTTONS["A"]: new_btns |= SWITCH_BUTTONS["B"]
                            if current_buttons & SWITCH_BUTTONS["X"]: new_btns |= SWITCH_BUTTONS["A"]
                            if current_buttons & SWITCH_BUTTONS["B"]: new_btns |= SWITCH_BUTTONS["Y"]
                            if current_buttons & SWITCH_BUTTONS["Y"]: new_btns |= SWITCH_BUTTONS["X"]

                        if current_buttons & SWITCH_BUTTONS["SL_R"]: new_btns |= SWITCH_BUTTONS["R"]
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
            else:
                self.update_as_xbox(inputData, buttons, controller, buttonsConfig)
            
            # Record raw buttons for shared click logic in next report
            controller._last_raw_buttons = current_buttons

        controller.set_input_report_callback(input_report_callback)

    def update_as_ps4(self, inputData: ControllerInputData, buttons: int, controller: Controller):
        with self.state_lock:
            self._update_as_ps4_locked(inputData, buttons, controller)

    def _update_as_ps4_locked(self, inputData: ControllerInputData, buttons: int, controller: Controller):
        self._update_ps_controller_locked(inputData, buttons, controller, self.vg_controller.report, mode="PS4")

    def update_as_ps5(self, inputData: ControllerInputData, buttons: int, controller: Controller):
        with self.state_lock:
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

        # 2. D-pad (Hat)
        up = bool(buttons & SWITCH_BUTTONS["UP"])
        down = bool(buttons & SWITCH_BUTTONS["DOWN"])
        left = bool(buttons & SWITCH_BUTTONS["LEFT"])
        right = bool(buttons & SWITCH_BUTTONS["RIGHT"])
        
        hat_x = -1 if left else (1 if right else 0)
        hat_y = -1 if up else (1 if down else 0)
        
        if mode == "PS4":
            if vgamepad._winuhid_devs:
                vgamepad._winuhid_devs.WinUHidPS4SetHatState(ctypes.byref(report), hat_x, hat_y)
        else:
            if vgamepad._winuhid_devs:
                vgamepad._winuhid_devs.WinUHidPS5SetHatState(ctypes.byref(report), hat_x, hat_y)

        # 3. Touchpad
        capt = bool(buttons & SWITCH_BUTTONS.get("CAPT", 0))
        tpad_l = bool(buttons & SWITCH_BUTTONS.get("PSTPAD_L", 0))
        tpad_r = bool(buttons & SWITCH_BUTTONS.get("PSTPAD_R", 0))

        is_touching = capt or tpad_l or tpad_r
        report.ButtonTouchpad = 1 if is_touching else 0

        touch_x = 0
        touch_y = 0
        if is_touching:
            if tpad_l:
                touch_x = 480
                touch_y = 512
            elif tpad_r:
                touch_x = 1440
                touch_y = 512
            else:
                touch_x = 960
                touch_y = 512

        if mode == "PS4":
            if vgamepad._winuhid_devs:
                vgamepad._winuhid_devs.WinUHidPS4SetTouchState(ctypes.byref(report), 0, is_touching, touch_x, touch_y)
        else:
            if vgamepad._winuhid_devs:
                vgamepad._winuhid_devs.WinUHidPS5SetTouchState(ctypes.byref(report), 0, is_touching, touch_x, touch_y)

        # 4. Triggers
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
                    self.last_gx = -inputData.gyroscope[1]
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

    def update_as_xbox(self, inputData: ControllerInputData, buttons: int, controller: Controller, buttonsConfig: ButtonConfig):
        with self.state_lock:
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
                    
                self.vg_controller.update()
        
        logger.info(f"Player {self.player_number}: Update loop thread finished.")
                
    def reset_inputs(self):
        """Reset all virtual inputs to neutral/released state."""
        with self.state_lock:
            if self.vg_controller is not None:
                if self.mode == "Xbox":
                    if vgamepad._winuhid_devs:
                        vgamepad._winuhid_devs.WinUHidXOneInitializeInputReport(ctypes.byref(self.vg_controller.report))
                elif self.mode == "PS4":
                    if vgamepad._winuhid_devs:
                        vgamepad._winuhid_devs.WinUHidPS4InitializeInputReport(ctypes.byref(self.vg_controller.report))
                elif self.mode == "PS5":
                    if vgamepad._winuhid_devs:
                        vgamepad._winuhid_devs.WinUHidPS5InitializeInputReport(ctypes.byref(self.vg_controller.report))
            logger.info(f"Player {self.player_number}: Virtual inputs reset to neutral.")

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
                try:
                    # Crucial: unregister notifications to stop driver-level callbacks
                    self.vg_controller.unregister_notification()
                except Exception as e:
                    logger.debug(f"Unregister notification failed: {e}")
                try:
                    self.vg_controller.close()
                except Exception as e:
                    logger.debug(f"Close failed: {e}")
                
                # Explicitly clear the reference while holding the lock
                self.vg_controller = None
        
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
                if hasattr(self, 'vg_controller') and self.vg_controller is not None:
                    logger.info(f"Player {self.player_number}: Unregistering notifications and clearing vg_controller")
                    try:
                        self.vg_controller.unregister_notification()
                    except Exception as e:
                        logger.debug(f"Unregister notification failed: {e}")
                    try:
                        self.vg_controller.close()
                    except Exception as e:
                        logger.debug(f"Close failed: {e}")
                    self.vg_controller = None
            
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

    async def remove_controller(self, controller: Controller) -> bool:
        if controller in self.controllers:
            self.controllers.remove(controller)
            
        if len(self.controllers) == 0:
            if hasattr(self, 'vg_controller') and self.vg_controller is not None:
                try:
                    self.vg_controller.unregister_notification()
                except Exception:
                    pass
                try:
                    self.vg_controller.close()
                except Exception:
                    pass
                self.vg_controller = None
                
            return True 
        else:
            if getattr(self, 'running', True):
                await self.init_added_controller(self.controllers[0])
            return False

def reset_vigem_bus():
    """
    WinUHid migration: this is now a no-op as WinUHid does not use a ViGEm bus.
    """
    logger.info("reset_vigem_bus called (no-op for WinUHid)")