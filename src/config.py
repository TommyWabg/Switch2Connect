from dataclasses import dataclass
import os
import yaml
import logging
import sys

logger = logging.getLogger(__name__)


SWITCH_BUTTONS = {
    "Y":     0x00000001,
    "X":     0x00000002,
    "B":     0x00000004,
    "A":     0x00000008,
    "SR_R":  0x00000010,
    "SL_R":  0x00000020,
    "R":     0x00000040,
    "ZR":    0x00000080,
    "MINUS": 0x00000100,
    "PLUS":  0x00000200,
    "R_STK": 0x00000400,
    "L_STK": 0x00000800,
    "HOME":  0x00001000,
    "CAPT":  0x00002000,
    "C":     0x00004000,
    "DOWN":  0x00010000,
    "UP":    0x00020000,
    "RIGHT": 0x00040000,
    "LEFT":  0x00080000,
    "SR_L":  0x00100000,
    "SL_L":  0x00200000,
    "L":     0x00400000,
    "ZL":    0x00800000,
    "GR":    0x01000000,
    "GL":    0x02000000,
    "PSTPAD_L": 0x04000000,
    "PSTPAD_R": 0x08000000,
}

BACK_BUTTON_OPTIONS = [
    "Default", "Gyro", "Calibration", "Home", "Capture", "Chat", "PSTPAD_L", "PSTPAD_R", 
    "A", "B", "X", "Y", "L", "R", "ZL", "ZR", 
    "MINUS", "PLUS", "L_STK", "R_STK", "UP", "DOWN", "LEFT", "RIGHT"
]

XB_BUTTONS = {
    "UP": 0x0001,
    "DOWN": 0x0002,
    "LEFT": 0x0004,
    "RIGHT": 0x0008,
    "START": 0x0010,
    "BACK": 0x0020,
    "L_STK": 0x0040,
    "R_STK": 0x0080,
    "LB": 0x0100,
    "RB": 0x0200,
    "GUIDE": 0x0400,
    "A": 0x1000,
    "B": 0x2000,
    "X": 0x4000,
    "Y": 0x8000,
}

@dataclass
class ButtonConfig:
    buttons: dict[int, int]
    left_trigger: list[int]
    right_trigger: list[int]

    def __init__(self, buttons_dict: dict[str, str]):
        self.buttons = {}
        self.left_trigger = []
        self.right_trigger = []

        default_keys = ["A", "B", "X", "Y", "L", "R", "ZL", "ZR", "MINUS", "PLUS", "L_STK", "R_STK", "UP", "DOWN", "LEFT", "RIGHT"]
        for k in default_keys:
            if k in XB_BUTTONS and k in SWITCH_BUTTONS:
                self.buttons[SWITCH_BUTTONS[k]] = XB_BUTTONS[k]
                
        self.left_trigger.append(SWITCH_BUTTONS["ZL"])
        self.right_trigger.append(SWITCH_BUTTONS["ZR"])

        for k, v in buttons_dict.items():
            if k not in SWITCH_BUTTONS:
                continue
            
            switch_button = SWITCH_BUTTONS[k]
            if v == "LT":
                self.left_trigger.append(switch_button)
            elif v == "RT":
                self.right_trigger.append(switch_button)
            elif v in XB_BUTTONS:
                self.buttons[switch_button] = XB_BUTTONS[v]

    def convert_buttons(self, switch_buttons: int):
        xb_buttons = 0x0000
        for switch_button, xb_button in self.buttons.items():
            if switch_buttons & switch_button:
                xb_buttons |= xb_button

        left_trigger = any([b & switch_buttons for b in self.left_trigger])
        right_trigger = any([b & switch_buttons for b in self.right_trigger])

        return xb_buttons, left_trigger, right_trigger

@dataclass
class MouseButtonConfig:
    left_button: int
    middle_button: int
    right_button: int

    def __init__(self, buttons_dict: dict[str, str]):
        self.left_button = SWITCH_BUTTONS.get(buttons_dict.get("left_button"), 0)
        self.middle_button = SWITCH_BUTTONS.get(buttons_dict.get("middle_button"), 0)
        self.right_button = SWITCH_BUTTONS.get(buttons_dict.get("right_button"), 0)

@dataclass
class MouseConfig:
    enabled: bool
    sensitivity: float
    scroll_sensitivity: float
    joycon_l_buttons: MouseButtonConfig
    joycon_r_buttons: MouseButtonConfig

    def __init__(self, config_dict: dict[str, str]):
        self.enabled = config_dict.get("enabled", False)
        self.sensitivity = config_dict.get("sensitivity", 1.0)
        self.scroll_sensitivity = config_dict.get("scroll_sensitivity", 1.0)
        buttons_config = config_dict.get("buttons", {})
        self.joycon_l_buttons = MouseButtonConfig(buttons_config.get("left_joycon", {}))
        self.joycon_r_buttons = MouseButtonConfig(buttons_config.get("right_joycon", {}))

def get_app_root():
    if hasattr(sys, '_MEIPASS'):
        return sys._MEIPASS
    current_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(current_dir) == 'src':
        return os.path.dirname(current_dir)
    return current_dir

def get_driver_path(filename: str):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, 'drivers', filename)
    return os.path.join(get_app_root(), 'drivers', filename)

def get_resource(resource_path: str):
    return os.path.join(get_app_root(), 'resources', resource_path)

class Config:
    def __init__(self, config_file_path: str):
        if hasattr(sys, 'frozen'):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = get_app_root()
        
        self.config_file_path = os.path.join(base_dir, 'config.yaml')
        
        if not os.path.exists(self.config_file_path):
            bundled_config = get_resource("config.yaml")
            if os.path.exists(bundled_config):
                import shutil
                shutil.copy(bundled_config, self.config_file_path)

        config = {}
        try:
            with open(self.config_file_path, 'r', encoding='utf-8') as cf:
                config = yaml.safe_load(cf) or {}
        except Exception as e:
            logger.error(f"Error loading config file: {e}")

        
        self.combine_joycons = config.get("combine_joycons", True)
        self.deadzone = config.get("deadzone", 50)
        self.controller_mode = config.get("controller_mode", "Xbox")

        btns = config.get("buttons", {})
        self.dual_joycons_config = ButtonConfig(btns.get("dual_joycons", {}))
        self.single_joycon_l_config = ButtonConfig(btns.get("single_joycon_l", {}))
        self.single_joycon_r_config = ButtonConfig(btns.get("single_joycon_r", {}))
        self.procon_config = ButtonConfig(btns.get("procon", {}))

        self.mouse_config = MouseConfig(config.get("mouse", {}))
        self.gl_mapping = config.get("gl_mapping", "Default")
        self.gr_mapping = config.get("gr_mapping", "Gyro")
        self.c_mapping = config.get("c_mapping", "Default")
        self.slr_mapping = config.get("slr_mapping", "Gyro")
        self.srl_mapping = config.get("srl_mapping", "Default")
        self.sll_mapping = config.get("sll_mapping", "Default")
        self.srr_mapping = config.get("srr_mapping", "Default")
        self.home_mapping = config.get("home_mapping", "Default")
        _capt = config.get("capt_mapping", "Capture")
        self.capt_mapping = "Capture" if _capt in ("None", "CAPT", "Default") else _capt
        self.abxy_mode = config.get("abxy_mode", "Xbox") 
        
        self.gyro_mode = config.get("gyro_mode", "World")
        self.gyro_sensitivity = float(config.get("gyro_sensitivity", 0.3))
        self.gyro_smoothing = 0.0 
        self.gyro_activation_mode = config.get("gyro_activation_mode", "Toggle")
        self.stick_mouse_sensitivity = float(config.get("stick_mouse_sensitivity", 20.0))
        
        self.gyro_bias_l = config.get("gyro_bias_l", [0.0, 0.0, 0.0])
        self.gyro_bias_r = config.get("gyro_bias_r", [0.0, 0.0, 0.0])
        self.stick_r_bias = config.get("stick_r_bias", [0.0, 0.0])
        
        # MAC address -> Calibration data mapping dictionary
        self.calibration_data = config.get("calibration_data", {}) or {}
        self.mag_calibration_data = config.get("mag_calibration_data", {}) or {}
        self.joycon_hold_mode = config.get("joycon_hold_mode", {}) or {}
        self.merged_gyro_side = config.get("merged_gyro_side", {}) or {}
        
        self.simulation_mode = config.get("simulation_mode", "Xbox")
        self.open_when_startup = config.get("open_when_startup", False)
        self.start_minimized = config.get("start_minimized", False)
        self.stabilized_gyro = config.get("stabilized_gyro", False)
        self.steam_roll_compensation = config.get("steam_roll_compensation", False)
        val = config.get("virtual_gyro_soft_deadzone", 2.0)
        if isinstance(val, bool):
            self.virtual_gyro_soft_deadzone = 2.0 if val else 0.0
        else:
            self.virtual_gyro_soft_deadzone = float(val)
        self.driver_installed = config.get("driver_installed", False)

        logger.info(f"Config successfully loaded from {self.config_file_path}")
        
    def save_config(self):
        try:
            # Read current file to preserve comments/other sections if possible
            # (Though yaml.dump will lose comments anyway, but we load first)
            data = {}
            if os.path.exists(self.config_file_path):
                with open(self.config_file_path, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f) or {}
            
            data['driver_installed'] = self.driver_installed
            
            data['simulation_mode'] = self.simulation_mode
            data['open_when_startup'] = self.open_when_startup
            data['start_minimized'] = self.start_minimized
            data['stabilized_gyro'] = self.stabilized_gyro
            data['steam_roll_compensation'] = self.steam_roll_compensation
            data['virtual_gyro_soft_deadzone'] = self.virtual_gyro_soft_deadzone
            data['abxy_mode'] = self.abxy_mode
            data['gl_mapping'] = self.gl_mapping
            data['gr_mapping'] = self.gr_mapping
            data['c_mapping'] = self.c_mapping
            data['slr_mapping'] = self.slr_mapping
            data['srl_mapping'] = self.srl_mapping
            data['sll_mapping'] = self.sll_mapping
            data['srr_mapping'] = self.srr_mapping
            data['home_mapping'] = self.home_mapping
            data['capt_mapping'] = self.capt_mapping
            
            data['gyro_mode'] = self.gyro_mode
            data['gyro_sensitivity'] = self.gyro_sensitivity
            data['gyro_activation_mode'] = self.gyro_activation_mode
            data['stick_mouse_sensitivity'] = self.stick_mouse_sensitivity
            
            data['gyro_bias_l'] = self.gyro_bias_l
            data['gyro_bias_r'] = self.gyro_bias_r
            data['stick_r_bias'] = self.stick_r_bias
            data['calibration_data'] = self.calibration_data
            data['mag_calibration_data'] = self.mag_calibration_data
            data['joycon_hold_mode'] = self.joycon_hold_mode
            data['merged_gyro_side'] = self.merged_gyro_side
            
            if 'mouse' not in data:
                data['mouse'] = {}
            data['mouse']['enabled'] = self.mouse_config.enabled
            data['mouse']['sensitivity'] = self.mouse_config.sensitivity
            
            with open(self.config_file_path, 'w', encoding='utf-8') as f:
                yaml.dump(data, f, default_flow_style=False)
            import time
            logger.info(f"[{time.strftime('%H:%M:%S')}] Config saved successfully to {self.config_file_path}")
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
    
CONFIG = Config(get_resource("config.yaml"))

