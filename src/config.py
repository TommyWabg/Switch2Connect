from dataclasses import dataclass
import os
import yaml
import logging
import sys
import threading

logger = logging.getLogger(__name__)

# (Standalone save_config removed as requested)

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
    "Capture": 0x00002000,
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
    "PS_L_Touch": 0x04000000,
    "PS_R_Touch": 0x08000000,
    "PS_C_Click": 0x20000000,
}

BACK_BUTTON_OPTIONS = [
    "Default", "Custom", "Gyro", "Calibration", "Sys Manager", "Change Profile", "Home", "Capture", "PrtSc", "Chat", "Mute", "Game Bar", "HDR Toggle", "PS_L_Touch", "PS_R_Touch", "PS_C_Click", 
    "A", "B", "X", "Y", "L", "R", "ZL", "ZR", 
    "MINUS", "PLUS", "L_STK", "R_STK", "UP", "DOWN", "LEFT", "RIGHT", "GL", "GR"
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

    def __init__(self, buttons_dict: dict[str, str], default_left: str = None, default_middle: str = None, default_right: str = None):
        self.left_button = SWITCH_BUTTONS.get(buttons_dict.get("left_button") or default_left, 0)
        self.middle_button = SWITCH_BUTTONS.get(buttons_dict.get("middle_button") or default_middle, 0)
        self.right_button = SWITCH_BUTTONS.get(buttons_dict.get("right_button") or default_right, 0)

@dataclass
class MouseConfig:
    enabled: bool
    sensitivity: float
    scroll_sensitivity: float
    ir_activate_threshold: int
    joycon_l_buttons: MouseButtonConfig
    joycon_r_buttons: MouseButtonConfig

    def __init__(self, config_dict: dict[str, str]):
        self.enabled = config_dict.get("enabled", False)
        self.sensitivity = config_dict.get("sensitivity", 1.0)
        self.scroll_sensitivity = config_dict.get("scroll_sensitivity", 1.0)
        self.ir_activate_threshold = int(config_dict.get("ir_activate_threshold", 1))
        buttons_config = config_dict.get("buttons", {})
        self.joycon_l_buttons = MouseButtonConfig(buttons_config.get("left_joycon", {}), default_left="L", default_right="ZL")
        self.joycon_r_buttons = MouseButtonConfig(buttons_config.get("right_joycon", {}), default_left="R", default_right="ZR")

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
        
        local_config = os.path.join(base_dir, 'config.yaml')
        
        def is_dir_writable(path):
            try:
                test_file = os.path.join(path, '.write_test')
                with open(test_file, 'w') as f:
                    f.write('test')
                os.remove(test_file)
                return True
            except Exception:
                return False

        def is_file_writable(filepath):
            if os.path.exists(filepath):
                try:
                    with open(filepath, 'r+'):
                        return True
                except Exception:
                    return False
            else:
                return is_dir_writable(os.path.dirname(filepath))

        use_appdata = False
        base_dir_lower = base_dir.lower()
        if "program files" in base_dir_lower or "windows\\system32" in base_dir_lower:
            use_appdata = True
        elif not is_file_writable(local_config):
            use_appdata = True

        if use_appdata:
            appdata_dir = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'Switch2Controllers')
            os.makedirs(appdata_dir, exist_ok=True)
            self.config_file_path = os.path.join(appdata_dir, 'config.yaml')
            
            if not os.path.exists(self.config_file_path):
                if os.path.exists(local_config):
                    try:
                        import shutil
                        shutil.copy(local_config, self.config_file_path)
                    except Exception as e:
                        logger.error(f"Failed to copy local config to AppData: {e}")
                else:
                    bundled_config = get_resource("config.yaml")
                    if os.path.exists(bundled_config):
                        import shutil
                        shutil.copy(bundled_config, self.config_file_path)
        else:
            self.config_file_path = local_config
            if not os.path.exists(self.config_file_path):
                bundled_config = get_resource("config.yaml")
                if os.path.exists(bundled_config):
                    import shutil
                    shutil.copy(bundled_config, self.config_file_path)

        self._save_lock = threading.Lock()
        self.load_config()

    def load_config(self):
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
        # Define categories and defaults for button remaps
        self.active_profile = config.get("active_profile", "Default")
        self.profiles = config.get("profiles", {})
        
        # Migration from old button_remaps
        old_button_remaps = config.get("button_remaps", {})
        if old_button_remaps and not self.profiles:
            self.profiles["Profile 1"] = old_button_remaps.copy()
            self.profiles["Default"] = {}
            self.active_profile = "Profile 1"
            
        if not self.profiles:
            self.profiles["Default"] = {}
            
        if self.active_profile not in self.profiles:
            self.active_profile = list(self.profiles.keys())[0] if self.profiles else "Default"
            if self.active_profile not in self.profiles:
                self.profiles[self.active_profile] = {}
        
        categories = ["xbox", "ps4", "ps5", "switch1", "switch2"]
        old_hold_mode = config.get("joycon_hold_mode", {}) or {}
        
        # Migrate old root level gyro_passthrough_mode to active profile
        old_gyro_passthrough = config.get("gyro_passthrough_mode")
        if old_gyro_passthrough is not None and self.active_profile in self.profiles:
            if "gyro_passthrough_mode" not in self.profiles[self.active_profile]:
                self.profiles[self.active_profile]["gyro_passthrough_mode"] = old_gyro_passthrough
        
        # Populate each category for all profiles
        for prof_name, prof_data in self.profiles.items():
            if "ps" in prof_data:
                import copy
                if "ps4" not in prof_data: prof_data["ps4"] = copy.deepcopy(prof_data["ps"])
                if "ps5" not in prof_data: prof_data["ps5"] = copy.deepcopy(prof_data["ps"])
                prof_data.pop("ps", None)

            for cat in categories:
                if cat not in prof_data:
                    prof_data[cat] = {}
                if "joycon_hold_mode" not in prof_data[cat]:
                    prof_data[cat]["joycon_hold_mode"] = old_hold_mode.copy()
                for key, def_val in self.get_default_category_dict(cat).items():
                    if key not in prof_data[cat]:
                        old_cat_data = old_button_remaps.get(cat, {})
                        if key == "abxy_mode":
                            top_level_def = "Switch" if cat in ("switch1", "switch2") else "Xbox"
                            val = config.get("abxy_mode", top_level_def)
                        elif key == "rumble_mode":
                            top_level_def = "Switch" if cat in ("switch1", "switch2") else "Xbox"
                            val = config.get("rumble_mode", top_level_def)
                        elif key == "vibration_strength_xbox":
                            val = old_cat_data.get("vibration_strength", config.get("vibration_strength_xbox", config.get("vibration_strength", def_val)))
                        elif key == "vibration_strength_switch":
                            val = old_cat_data.get("vibration_strength", config.get("vibration_strength_switch", config.get("vibration_strength", def_val)))
                        elif key == "vibration_frequency":
                            val = config.get("vibration_frequency", def_val)
                        elif key == "capt_mapping":
                            default_capt = "Capture" if cat in ("switch1", "switch2") else "PrtSc"
                            val = config.get("capt_mapping", default_capt)
                            if val in ("None", "CAPT", "Default", "Capture"):
                                val = "Capture" if cat in ("switch1", "switch2") else "PrtSc"
                        else:
                            val = config.get(key, def_val)
                        
                        if key == "rumble_mode" and val == "PC":
                            val = "Xbox"
                        prof_data[cat][key] = val
        
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
        self.gc_trigger_calibration_data = config.get("gc_trigger_calibration_data", {}) or {}
        self.merged_gyro_side = config.get("merged_gyro_side", {}) or {}
        
        # Persistent Cemuhook pad_id mapping
        self.cemuhook_mac_to_pad = config.get("cemuhook_mac_to_pad", {}) or {}
        self.cemuhook_pad_overwrite_idx = int(config.get("cemuhook_pad_overwrite_idx", 0))
        
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
        self.driver_type = config.get("driver_type", "WinUHid")
        if self.driver_type not in ["WinUHid", "ViGEmBus", "USBIP"]:
            self.driver_type = "WinUHid"
        
        self.simulation_mode = config.get("simulation_mode", "Xbox One")
        if self.simulation_mode == "Switch 2 Pro":
            self.simulation_mode = "Switch2"
            
        if self.simulation_mode == "Xbox":
            if self.driver_type == "ViGEmBus":
                self.simulation_mode = "Xbox360"
            else:
                self.simulation_mode = "Xbox One"

        self.vigembus_sim_mode = config.get("vigembus_sim_mode", None)
        self.winuhid_sim_mode = config.get("winuhid_sim_mode", None)
        self.usbip_sim_mode = config.get("usbip_sim_mode", None)
        
        if self.vigembus_sim_mode == "Xbox":
            self.vigembus_sim_mode = "Xbox360"
        if self.winuhid_sim_mode == "Xbox":
            self.winuhid_sim_mode = "Xbox One"
        
        if self.vigembus_sim_mode is None:
            if self.driver_type == "ViGEmBus":
                self.vigembus_sim_mode = self.simulation_mode if self.simulation_mode in ["Xbox360", "PS4"] else "Xbox360"
            else:
                self.vigembus_sim_mode = "Xbox360"
        if self.winuhid_sim_mode is None:
            if self.driver_type == "WinUHid":
                self.winuhid_sim_mode = self.simulation_mode if self.simulation_mode in ["Xbox One", "PS4", "PS5"] else "PS5"
            else:
                self.winuhid_sim_mode = "PS5"
        if self.usbip_sim_mode is None:
            if self.driver_type == "USBIP":
                self.usbip_sim_mode = self.simulation_mode if self.simulation_mode in ["Switch1", "Switch2"] else "Switch2"
            else:
                self.usbip_sim_mode = "Switch2"

        if self.driver_type == "USBIP":
            self.simulation_mode = self.usbip_sim_mode
        elif self.simulation_mode in ["Switch1", "Switch2"]:
            if self.driver_type == "ViGEmBus":
                self.simulation_mode = self.vigembus_sim_mode
            else:
                self.simulation_mode = self.winuhid_sim_mode

        self.vigembus_installed = config.get("vigembus_installed", False)
        self.window_width = config.get("window_width", None)
        self.window_height = config.get("window_height", None)
        self._auto_disconnect_mode = config.get("auto_disconnect_mode", "Absolute" if config.get("auto_disconnect_enabled", False) else "OFF")
        if self._auto_disconnect_mode not in ["OFF", "Inactive", "Absolute"]:
            self._auto_disconnect_mode = "Absolute" if config.get("auto_disconnect_enabled", False) else "OFF"
        self.auto_disconnect_days = int(config.get("auto_disconnect_days", 0))
        self.auto_disconnect_hours = int(config.get("auto_disconnect_hours", 0))
        self.auto_disconnect_minutes = int(config.get("auto_disconnect_minutes", 0))
        # abxy_mode, rumble_mode, vibration_strength, vibration_frequency are now properties managed per Emu Mode category

        logger.info(f"Config successfully loaded from {self.config_file_path}")

    @property
    def button_remaps(self):
        return self.profiles[self.active_profile]

    def add_profile(self, name):
        if name and name not in self.profiles:
            import copy
            self.profiles[name] = copy.deepcopy(self.profiles[self.active_profile])
            self.active_profile = name
            self.save_config()
            return True
        return False

    def rename_profile(self, new_name):
        if new_name and new_name not in self.profiles:
            self.profiles[new_name] = self.profiles.pop(self.active_profile)
            self.active_profile = new_name
            self.save_config()
            return True
        return False

    def delete_profile(self):
        if len(self.profiles) > 1:
            self.profiles.pop(self.active_profile)
            self.active_profile = list(self.profiles.keys())[0]
            self.save_config()
            return True
        return False

    def get_default_category_dict(self, cat):
        # Specific default values matching user's exact current config for each Emu Mode
        defaults = {
            "ps4": {
                "abxy_mode": "Xbox", "c_mapping": "Calibration", "capt_mapping": "Default",
                "gc_trigger_mode": "100% at Max", "gl_mapping": "PS_L_Touch", "gr_mapping": "PS_R_Touch",
                "home_mapping": "Default", "rumble_mode": "Xbox", "sll_mapping": "Default",
                "slr_mapping": "PS_R_Touch", "srl_mapping": "PS_L_Touch", "srr_mapping": "Change Profile",
                "vibration_frequency": 10, "vibration_strength": 5, "vibration_strength_switch": 5, "vibration_strength_xbox": 5
            },
            "ps5": {
                "abxy_mode": "Xbox", "c_mapping": "Default", "capt_mapping": "Default",
                "gc_trigger_mode": "100% at Max", "gl_mapping": "PS_L_Touch", "gr_mapping": "PS_R_Touch",
                "home_mapping": "Default", "rumble_mode": "Xbox", "sll_mapping": "Default",
                "slr_mapping": "PS_R_Touch", "srl_mapping": "PS_L_Touch", "srr_mapping": "Change Profile",
                "vibration_frequency": 10, "vibration_strength": 5, "vibration_strength_switch": 5, "vibration_strength_xbox": 5
            },
            "xbox": {
                "abxy_mode": "Xbox", "c_mapping": "Calibration", "capt_mapping": "Default",
                "gc_trigger_mode": "100% at Max", "gl_mapping": "Default", "gr_mapping": "Default",
                "home_mapping": "Default", "rumble_mode": "Xbox", "sll_mapping": "Default",
                "slr_mapping": "Default", "srl_mapping": "Default", "srr_mapping": "Change Profile",
                "vibration_frequency": 10, "vibration_strength": 10, "vibration_strength_switch": 5, "vibration_strength_xbox": 5
            },
            "switch1": {
                "abxy_mode": "Switch", "c_mapping": "Calibration", "capt_mapping": "Default",
                "gc_trigger_mode": "Hair Trigger", "gl_mapping": "Default", "gr_mapping": "Default",
                "home_mapping": "Default", "rumble_mode": "Switch", "sll_mapping": "Default",
                "slr_mapping": "Default", "srl_mapping": "Default", "srr_mapping": "Change Profile",
                "vibration_frequency": 10, "vibration_strength_switch": 5, "vibration_strength_xbox": 5
            },
            "switch2": {
                "abxy_mode": "Switch", "c_mapping": "Default", "capt_mapping": "Default",
                "gc_trigger_mode": "Hair Trigger", "gl_mapping": "Default", "gr_mapping": "Default",
                "home_mapping": "Default", "rumble_mode": "Switch", "sll_mapping": "Default",
                "slr_mapping": "GR", "srl_mapping": "GL", "srr_mapping": "Change Profile",
                "vibration_frequency": 10, "vibration_strength": 5, "vibration_strength_switch": 5, "vibration_strength_xbox": 5
            }
        }
        return defaults.get(cat, defaults["xbox"]).copy()

    def get_default_profile_dict(self):
        categories = ["xbox", "ps4", "ps5", "switch1", "switch2"]
        prof_data = {"gyro_passthrough_mode": "Default"}
        for cat in categories:
            prof_data[cat] = self.get_default_category_dict(cat)
            prof_data[cat]["joycon_hold_mode"] = {}
        return prof_data

    def reset_profile_to_default(self, name):
        if name in self.profiles:
            self.profiles[name] = self.get_default_profile_dict()
            self.save_config()
            return True
        return False

    def reset_category_to_default(self, name, cat):
        if name in self.profiles and cat in self.profiles[name]:
            old_hold_mode = self.profiles[name][cat].get("joycon_hold_mode", {})
            self.profiles[name][cat] = self.get_default_category_dict(cat)
            self.profiles[name][cat]["joycon_hold_mode"] = old_hold_mode
            self.save_config()
            return True
        return False

    def switch_profile(self, name):
        if name in self.profiles:
            self.active_profile = name
            self.save_config()
            return True
        return False
        
    @property
    def gyro_passthrough_mode(self):
        return self.profiles.get(self.active_profile, {}).get("gyro_passthrough_mode", "Default")

    @gyro_passthrough_mode.setter
    def gyro_passthrough_mode(self, value):
        if self.active_profile in self.profiles:
            self.profiles[self.active_profile]["gyro_passthrough_mode"] = value
        
    def save_config(self):
        # Snapshot config values in the calling thread
        data = {
            'driver_installed': self.driver_installed,
            'driver_type': self.driver_type,
            'vigembus_sim_mode': self.vigembus_sim_mode,
            'winuhid_sim_mode': self.winuhid_sim_mode,
            'usbip_sim_mode': self.usbip_sim_mode,
            'vigembus_installed': self.vigembus_installed,
            'window_width': self.window_width,
            'window_height': self.window_height,
            'auto_disconnect_enabled': self.auto_disconnect_enabled,
            'auto_disconnect_mode': self.auto_disconnect_mode,
            'auto_disconnect_days': self.auto_disconnect_days,
            'auto_disconnect_hours': self.auto_disconnect_hours,
            'auto_disconnect_minutes': self.auto_disconnect_minutes,
            'vibration_strength': self.vibration_strength,
            'vibration_strength_xbox': self.button_remaps.get(self.get_current_category(), {}).get("vibration_strength_xbox", 5),
            'vibration_strength_switch': self.button_remaps.get(self.get_current_category(), {}).get("vibration_strength_switch", 5),
            'vibration_frequency': self.vibration_frequency,
            'rumble_mode': self.rumble_mode,
            'simulation_mode': self.simulation_mode,
            'open_when_startup': self.open_when_startup,
            'start_minimized': self.start_minimized,
            'stabilized_gyro': self.stabilized_gyro,
            'steam_roll_compensation': self.steam_roll_compensation,
            'virtual_gyro_soft_deadzone': self.virtual_gyro_soft_deadzone,
            'abxy_mode': self.abxy_mode,
            'gl_mapping': self.gl_mapping,
            'gr_mapping': self.gr_mapping,
            'c_mapping': self.c_mapping,
            'slr_mapping': self.slr_mapping,
            'srl_mapping': self.srl_mapping,
            'sll_mapping': self.sll_mapping,
            'srr_mapping': self.srr_mapping,
            'home_mapping': self.home_mapping,
            'capt_mapping': self.capt_mapping,
            'gyro_mode': self.gyro_mode,
            'gyro_sensitivity': self.gyro_sensitivity,
            'gyro_activation_mode': self.gyro_activation_mode,
            'stick_mouse_sensitivity': self.stick_mouse_sensitivity,
            'gyro_bias_l': self.gyro_bias_l,
            'gyro_bias_r': self.gyro_bias_r,
            'stick_r_bias': self.stick_r_bias,
            'calibration_data': self.calibration_data,
            'mag_calibration_data': self.mag_calibration_data,
            'gc_trigger_calibration_data': self.gc_trigger_calibration_data,
            'gc_trigger_mode': self.gc_trigger_mode,
            'joycon_hold_mode': self.joycon_hold_mode,
            'merged_gyro_side': self.merged_gyro_side,
            'cemuhook_mac_to_pad': self.cemuhook_mac_to_pad,
            'cemuhook_pad_overwrite_idx': self.cemuhook_pad_overwrite_idx,
            'active_profile': self.active_profile,
            'profiles': self.profiles,
            'mouse': {
                'enabled': self.mouse_config.enabled,
                'sensitivity': self.mouse_config.sensitivity,
                'ir_activate_threshold': self.mouse_config.ir_activate_threshold,
            }
        }
        
        def _async_write():
            with self._save_lock:
                try:
                    existing_data = {}
                    if os.path.exists(self.config_file_path):
                        try:
                            with open(self.config_file_path, 'r', encoding='utf-8') as f:
                                existing_data = yaml.safe_load(f) or {}
                        except Exception:
                            pass
                    
                    existing_data.update(data)
                    
                    with open(self.config_file_path, 'w', encoding='utf-8') as f:
                        yaml.dump(existing_data, f, default_flow_style=False)
                    import time
                    logger.info(f"[{time.strftime('%H:%M:%S')}] Config saved successfully (async) to {self.config_file_path}")
                except Exception as e:
                    logger.error(f"Failed to save config asynchronously: {e}")

        threading.Thread(target=_async_write, daemon=True).start()

    @property
    def abxy_mode(self):
        cat = self.get_current_category()
        return self.button_remaps.get(cat, {}).get("abxy_mode", "Xbox")

    @abxy_mode.setter
    def abxy_mode(self, val):
        cat = self.get_current_category()
        if cat not in self.button_remaps: self.button_remaps[cat] = {}
        self.button_remaps[cat]["abxy_mode"] = val

    @property
    def rumble_mode(self):
        cat = self.get_current_category()
        return self.button_remaps.get(cat, {}).get("rumble_mode", "Xbox")

    @rumble_mode.setter
    def rumble_mode(self, val):
        cat = self.get_current_category()
        if cat not in self.button_remaps: self.button_remaps[cat] = {}
        self.button_remaps[cat]["rumble_mode"] = val

    @property
    def vibration_strength(self):
        cat = self.get_current_category()
        mode = self.rumble_mode.lower() 
        return int(self.button_remaps.get(cat, {}).get(f"vibration_strength_{mode}", 5))

    @vibration_strength.setter
    def vibration_strength(self, val):
        cat = self.get_current_category()
        mode = self.rumble_mode.lower() 
        if cat not in self.button_remaps: self.button_remaps[cat] = {}
        self.button_remaps[cat][f"vibration_strength_{mode}"] = int(val)

    @property
    def vibration_frequency(self):
        cat = self.get_current_category()
        return int(self.button_remaps.get(cat, {}).get("vibration_frequency", 10))

    @vibration_frequency.setter
    def vibration_frequency(self, val):
        cat = self.get_current_category()
        if cat not in self.button_remaps: self.button_remaps[cat] = {}
        self.button_remaps[cat]["vibration_frequency"] = int(val)

    def get_current_category(self):
        mode = getattr(self, "simulation_mode", "Xbox One")
        if mode == "Switch2":
            return "switch2"
        elif mode == "Switch1":
            return "switch1"
        elif mode == "PS4":
            return "ps4"
        elif mode == "PS5":
            return "ps5"
        else:
            return "xbox"

    @property
    def gl_mapping(self):
        cat = self.get_current_category()
        return self.button_remaps.get(cat, {}).get("gl_mapping", "Default")
    @gl_mapping.setter
    def gl_mapping(self, val):
        cat = self.get_current_category()
        if cat not in self.button_remaps: self.button_remaps[cat] = {}
        self.button_remaps[cat]["gl_mapping"] = val

    @property
    def gr_mapping(self):
        cat = self.get_current_category()
        return self.button_remaps.get(cat, {}).get("gr_mapping", "Gyro")
    @gr_mapping.setter
    def gr_mapping(self, val):
        cat = self.get_current_category()
        if cat not in self.button_remaps: self.button_remaps[cat] = {}
        self.button_remaps[cat]["gr_mapping"] = val

    @property
    def c_mapping(self):
        cat = self.get_current_category()
        return self.button_remaps.get(cat, {}).get("c_mapping", "Default")
    @c_mapping.setter
    def c_mapping(self, val):
        cat = self.get_current_category()
        if cat not in self.button_remaps: self.button_remaps[cat] = {}
        self.button_remaps[cat]["c_mapping"] = val

    @property
    def slr_mapping(self):
        cat = self.get_current_category()
        return self.button_remaps.get(cat, {}).get("slr_mapping", "Gyro")
    @slr_mapping.setter
    def slr_mapping(self, val):
        cat = self.get_current_category()
        if cat not in self.button_remaps: self.button_remaps[cat] = {}
        self.button_remaps[cat]["slr_mapping"] = val

    @property
    def srl_mapping(self):
        cat = self.get_current_category()
        return self.button_remaps.get(cat, {}).get("srl_mapping", "Default")
    @srl_mapping.setter
    def srl_mapping(self, val):
        cat = self.get_current_category()
        if cat not in self.button_remaps: self.button_remaps[cat] = {}
        self.button_remaps[cat]["srl_mapping"] = val

    @property
    def sll_mapping(self):
        cat = self.get_current_category()
        return self.button_remaps.get(cat, {}).get("sll_mapping", "Default")
    @sll_mapping.setter
    def sll_mapping(self, val):
        cat = self.get_current_category()
        if cat not in self.button_remaps: self.button_remaps[cat] = {}
        self.button_remaps[cat]["sll_mapping"] = val

    @property
    def srr_mapping(self):
        cat = self.get_current_category()
        return self.button_remaps.get(cat, {}).get("srr_mapping", "Default")
    @srr_mapping.setter
    def srr_mapping(self, val):
        cat = self.get_current_category()
        if cat not in self.button_remaps: self.button_remaps[cat] = {}
        self.button_remaps[cat]["srr_mapping"] = val

    @property
    def home_mapping(self):
        cat = self.get_current_category()
        return self.button_remaps.get(cat, {}).get("home_mapping", "Default")
    @home_mapping.setter
    def home_mapping(self, val):
        cat = self.get_current_category()
        if cat not in self.button_remaps: self.button_remaps[cat] = {}
        self.button_remaps[cat]["home_mapping"] = val

    @property
    def capt_mapping(self):
        cat = self.get_current_category()
        default_capt = "Capture" if cat in ("switch1", "switch2") else "PrtSc"
        return self.button_remaps.get(cat, {}).get("capt_mapping", default_capt)
    @capt_mapping.setter
    def capt_mapping(self, val):
        cat = self.get_current_category()
        if cat not in self.button_remaps: self.button_remaps[cat] = {}
        self.button_remaps[cat]["capt_mapping"] = val
        
    @property
    def joycon_hold_mode(self):
        cat = self.get_current_category()
        if cat not in self.button_remaps: self.button_remaps[cat] = {}
        if "joycon_hold_mode" not in self.button_remaps[cat]:
            self.button_remaps[cat]["joycon_hold_mode"] = {}
        return self.button_remaps[cat]["joycon_hold_mode"]
        
    @joycon_hold_mode.setter
    def joycon_hold_mode(self, val):
        cat = self.get_current_category()
        if cat not in self.button_remaps: self.button_remaps[cat] = {}
        self.button_remaps[cat]["joycon_hold_mode"] = val
    
    @property
    def auto_disconnect_mode(self):
        return self._auto_disconnect_mode
        
    @auto_disconnect_mode.setter
    def auto_disconnect_mode(self, val):
        if val in ["OFF", "Inactive", "Absolute"]:
            self._auto_disconnect_mode = val

    @property
    def auto_disconnect_enabled(self):
        return self._auto_disconnect_mode != "OFF"

    @auto_disconnect_enabled.setter
    def auto_disconnect_enabled(self, val):
        if val:
            if self._auto_disconnect_mode == "OFF":
                self._auto_disconnect_mode = "Absolute"
        else:
            self._auto_disconnect_mode = "OFF"
            
    @property
    def gc_trigger_mode(self):
        cat = self.get_current_category()
        return self.button_remaps.get(cat, {}).get("gc_trigger_mode", "100% at Bump")
        
    @gc_trigger_mode.setter
    def gc_trigger_mode(self, val):
        cat = self.get_current_category()
        if cat not in self.button_remaps: self.button_remaps[cat] = {}
        self.button_remaps[cat]["gc_trigger_mode"] = val
    
CONFIG = Config(get_resource("config.yaml"))

