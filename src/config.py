# Switch2Connect - A Python and ESP32-S3 bridge utility for Switch 2 controller inputs.
# Copyright (C) 2026 TommyWabg
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Contact Information:
# Electronic Mail: tommyw9318@gmail.com

from dataclasses import dataclass
import os
import yaml
import logging
import sys
import threading

logger = logging.getLogger(__name__)

# Use the libyaml C loader/dumper when available. It is ~6x faster than the pure
# Python implementation and releases the GIL during (de)serialization, which keeps
# the Tkinter main thread responsive while the config is saved on a background
# thread (otherwise large configs cause a UI freeze, e.g. when switching profiles).
try:
    _YamlLoader = yaml.CSafeLoader
except AttributeError:
    _YamlLoader = yaml.SafeLoader
try:
    _YamlDumper = yaml.CDumper
except AttributeError:
    _YamlDumper = yaml.Dumper

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
    "GC_L_CLICK": 0x40000000,
    "GC_R_CLICK": 0x80000000,
}

BACK_BUTTON_OPTIONS = [
    "Default", "Custom", "In-app Gyro", "Gyro Lock", "DJG", "Mode Shift", "Calibration", "Task Manager", "Change Profile", "None", "Home", "Capture", "PrtSc", "On-Screen Keyboard", "Chat", "Mute", "Game Bar", "HDR Toggle", "Play/Pause", "Stop", "Next Track", "Previous Track", "Volume Up", "Volume Down", "Media Mute", "PS_L_Touch", "PS_R_Touch", "PS_C_Click",
    "A", "B", "X", "Y", "L", "R", "ZL", "ZR",
    "MINUS", "PLUS", "L_STK", "R_STK", "UP", "DOWN", "LEFT", "RIGHT", "GL", "GR",
    "m1 Left Click", "m2 Middle Click", "m3 Right Click", "m4 Backward", "m5 Forward",
]

# Back Button Option "Mouse Click" tokens -> the Custom mouse-button token they run as.
# Tk mouse numbering: MB_1=left, MB_2=middle, MB_3=right; MB_4/MB_5 = XBUTTON1/2.
MOUSE_CLICK_BACK_BUTTON_TOKENS = {
    "m1 Left Click": "MB_1",
    "m2 Middle Click": "MB_2",
    "m3 Right Click": "MB_3",
    "m4 Backward": "MB_4",
    "m5 Forward": "MB_5",
}

# Back Button Option floating selector layout. Each entry is (category title, rows),
# where a row is a list of stored tokens (the value get()/set() round-trips through
# config). The tokens are shown to the user via BACK_BUTTON_LABELS. The wide Switch
# Input block intentionally spans three rows under a single header.
BACK_BUTTON_CATEGORIES = [
    ("General", [
        ["Default", "Custom", "Mode Shift", "Change Profile", "None"],
    ]),
    ("In-app Gyro", [
        ["In-app Gyro", "Gyro Lock", "DJG", "Calibration"],
    ]),
    ("Switch Input", [
        ["ZL", "L", "MINUS", "Capture", "L_STK", "A", "X"],
        ["ZR", "R", "PLUS", "Home", "R_STK", "B", "Y"],
        ["UP", "DOWN", "LEFT", "RIGHT", "Chat", "GL", "GR"],
    ]),
    ("Media Keys", [
        ["Play/Pause", "Stop", "Next Track", "Previous Track", "Volume Up", "Volume Down", "Media Mute"],
    ]),
    ("PS Input", [
        ["PS_L_Touch", "PS_R_Touch", "PS_C_Click", "Mute"],
    ]),
    ("Windows", [
        ["Game Bar", "PrtSc", "On-Screen Keyboard", "Task Manager", "HDR Toggle"],
    ]),
    ("Mouse Click", [
        ["m1 Left Click", "m2 Middle Click", "m3 Right Click", "m4 Backward", "m5 Forward"],
    ]),
]

SWITCH_INPUT_DAMPENING_OPTIONS = [
    "ZL", "L", "MINUS", "Capture", "L_STK", "A", "X",
    "ZR", "R", "PLUS", "Home", "R_STK", "B", "Y",
    "UP", "DOWN", "LEFT", "RIGHT", "Chat", "GL", "GR",
]

_DAMPENING_INPUT_ALIASES = {
    "HOME": "Home",
    "CAPT": "Capture",
    "CAPTURE": "Capture",
    "C": "Chat",
    "CHAT": "Chat",
}

_LEGACY_DAMPENING_MODES = {
    "Off": [],
    "ZR Dampening": ["ZR"],
    "ZL Dampening": ["ZL"],
    "Both Dampening": ["ZL", "ZR"],
}


def normalize_dampening_inputs(value):
    if value is None:
        return []
    if isinstance(value, str):
        if value in _LEGACY_DAMPENING_MODES:
            return list(_LEGACY_DAMPENING_MODES[value])
        if value.strip() == "":
            return []
        raw_values = [part.strip() for part in value.replace("|", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        return []

    valid = set(SWITCH_INPUT_DAMPENING_OPTIONS)
    normalized = []
    for item in raw_values:
        token = str(item).strip()
        token = _DAMPENING_INPUT_ALIASES.get(token.upper(), token)
        if token in valid and token not in normalized:
            normalized.append(token)
    return normalized

# Display labels for tokens whose stored name differs from what the user should see.
# Tokens not listed here are shown verbatim (e.g. "Default", "A", "Home", "Gyro Lock").
BACK_BUTTON_LABELS = {
    "MINUS": "Minus",
    "PLUS": "Plus",
    "L_STK": "L Joystick Click",
    "R_STK": "R Joystick Click",
    "UP": "Dpad Up",
    "DOWN": "Dpad Down",
    "LEFT": "Dpad Left",
    "RIGHT": "Dpad Right",
    "PS_L_Touch": "Trackpad L Touch",
    "PS_R_Touch": "Trackpad R Touch",
    "PS_C_Click": "Trackpad Center Click",
    "PrtSc": "Print Screen",
    "Sys Manager": "Task Manager",
    "Media Mute": "Mute",
    "Mute": "PS Mute",
}


def back_button_label(token):
    """Friendly display label for a Back Button Option token (falls back to the token)."""
    return BACK_BUTTON_LABELS.get(token, token)

JOYSTICK_OPTIONS = ["Default", "R Joystick", "L Joystick", "WASD", "KB Arrow Keys", "Mouse", "Scroll Wheel", "Custom"]

# In-app Gyro Lock: a Back Button Option that pauses gyro control while staying in
# In-app Gyro mode. Stored in the Custom recorder form as "Custom[Hold|Tap]:GYRO_LOCK".
GYRO_LOCK_TOKEN = "GYRO_LOCK"
GYRO_LOCK_LABEL = "Gyro Lock"

# Mode Shift: a Back Button Option that applies the Mode Shift Mapping layer (the
# per-Gyro-Control In-app Gyro mapping store) while held (Hold) or toggled (Tap),
# independently of whether In-app Gyro mode is active. Stored in the Custom recorder
# form as "Custom[Hold|Tap]:MODE_SHIFT".
MODE_SHIFT_TOKEN = "MODE_SHIFT"
MODE_SHIFT_LABEL = "Mode Shift"

# In-app Gyro logic identical to Mode Shift / Gyro Lock, allowing Tap/Hold per-button
IN_APP_GYRO_TOKEN = "INAPPGYRO"
IN_APP_GYRO_LABEL = "In-app Gyro"


# Default Mode Shift auto-apply state per Gyro Control mode. When On, entering In-app
# Gyro mode automatically applies the Mode Shift Mapping layer; when Off the layer is
# applied only via the "Mode Shift" back button.
MODE_SHIFT_ENABLED_DEFAULTS = {"Mouse": True, "R Joystick": False, "Steering": False}
MODE_SHIFT_ENABLED_MIGRATION_KEY = "_mode_shift_enabled_migration_v1"

SHARED_BUTTON_MAPPING_DEFAULTS = {
    "plus_mapping": "Default",
    "minus_mapping": "Default",
    "a_mapping": "Default",
    "b_mapping": "Default",
    "x_mapping": "Default",
    "y_mapping": "Default",
    "up_mapping": "Default",
    "down_mapping": "Default",
    "left_mapping": "Default",
    "right_mapping": "Default",
    "zl_mapping": "Default",
    "l_mapping": "Default",
    "zr_mapping": "Default",
    "r_mapping": "Default",
    "l_stk_mapping": "Default",
    "r_stk_mapping": "Default",
    "l_joystick_mapping": "Default",
    "r_joystick_mapping": "Default",
    "l_joystick_mouse_sensitivity": 5.0,
    "r_joystick_mouse_sensitivity": 5.0,
    "l_joystick_scroll_mode": "Up/Down",
    "r_joystick_scroll_mode": "Up/Down",
    "l_joystick_scroll_activation": "Hold",
    "r_joystick_scroll_activation": "Hold",
}

JOYSTICK_CUSTOM_DEFAULTS = {
    "l_joystick_custom": {"up": "Default", "down": "Default", "left": "Default", "right": "Default"},
    "r_joystick_custom": {"up": "Default", "down": "Default", "left": "Default", "right": "Default"},
}

MAPPING_SCOPE_IN_APP_GYRO = "in_app_gyro_mode_mappings"

def build_in_app_gyro_mapping_defaults(stick_mouse_sensitivity=20.0):
    defaults = {key: "Default" for key in SHARED_BUTTON_MAPPING_DEFAULTS}
    for key in (
        "home_mapping", "capt_mapping", "c_mapping",
        "gl_mapping", "gr_mapping",
        "sll_mapping", "srl_mapping", "slr_mapping", "srr_mapping",
        "gc_l_click_mapping", "gc_r_click_mapping",
    ):
        defaults[key] = "Default"
    defaults.update({
        "l_joystick_mouse_sensitivity": float(stick_mouse_sensitivity),
        "r_joystick_mouse_sensitivity": float(stick_mouse_sensitivity),
        "l_joystick_scroll_mode": "Up/Down",
        "r_joystick_scroll_mode": "Up/Down",
        "l_joystick_scroll_activation": "Hold",
        "r_joystick_scroll_activation": "Hold",
        "zl_mapping": "Custom[Hold]:MB_3",
        "zr_mapping": "Custom[Hold]:MB_1",
        "r_joystick_mapping": "Mouse",
        "gc_trigger_mode": "Hair Trigger",
        "gc_l_click_mapping": "Custom[Hold]:MB_3",
        "gc_r_click_mapping": "Custom[Hold]:MB_1",
    })
    for custom_key, custom_defaults in JOYSTICK_CUSTOM_DEFAULTS.items():
        defaults[custom_key] = custom_defaults.copy()
    return defaults

# Separate In-app Gyro Mode Mapping store used when Gyro Control == "R Joystick".
# Unlike the Mouse store, every button defaults to "Default" (no mouse clicks), and
# the Analog Trigger 100% (gc_trigger_mode) reset default mirrors the emulation mode.
MAPPING_SCOPE_IN_APP_GYRO_RSTICK = "in_app_gyro_rstick_mode_mappings"

def build_in_app_gyro_rstick_mapping_defaults(gc_trigger_default="Hair Trigger"):
    defaults = {key: "Default" for key in SHARED_BUTTON_MAPPING_DEFAULTS}
    for key in (
        "home_mapping", "capt_mapping", "c_mapping",
        "gl_mapping", "gr_mapping",
        "sll_mapping", "srl_mapping", "slr_mapping", "srr_mapping",
        "gc_l_click_mapping", "gc_r_click_mapping",
        "zl_mapping", "zr_mapping",
        "l_joystick_mapping", "r_joystick_mapping",
    ):
        defaults[key] = "Default"
    defaults.update({
        "l_joystick_scroll_mode": "Up/Down",
        "r_joystick_scroll_mode": "Up/Down",
        "l_joystick_scroll_activation": "Hold",
        "r_joystick_scroll_activation": "Hold",
        "gc_trigger_mode": gc_trigger_default,
    })
    for custom_key, custom_defaults in JOYSTICK_CUSTOM_DEFAULTS.items():
        defaults[custom_key] = custom_defaults.copy()
    return defaults

# Separate Mode Shift Mapping store used when Gyro Control == "Steering". Mirrors the
# R Joystick store (every button defaults to "Default"); kept as its own scope so the
# Steering Mode Shift layer persists independently of the Mouse/R Joystick stores.
MAPPING_SCOPE_IN_APP_GYRO_STEERING = "in_app_gyro_steering_mode_mappings"

def build_in_app_gyro_steering_mapping_defaults(gc_trigger_default="Hair Trigger"):
    return build_in_app_gyro_rstick_mapping_defaults(gc_trigger_default)

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
        self.settings_generation = 0
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
            appdata_root = os.environ.get('APPDATA', os.path.expanduser('~'))
            appdata_dir = os.path.join(appdata_root, 'Switch 2 Connect')
            legacy_config = os.path.join(appdata_root, 'Switch2Controllers', 'config.yaml')
            os.makedirs(appdata_dir, exist_ok=True)
            self.config_file_path = os.path.join(appdata_dir, 'config.yaml')
            
            if not os.path.exists(self.config_file_path):
                if os.path.exists(legacy_config):
                    try:
                        import shutil
                        shutil.copy(legacy_config, self.config_file_path)
                        logger.info("Migrated configuration from the legacy Switch2Controllers AppData folder.")
                    except Exception as e:
                        logger.error(f"Failed to migrate legacy AppData config: {e}")
                elif os.path.exists(local_config):
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

    def _bump_settings_generation(self):
        try:
            self.settings_generation = int(getattr(self, "settings_generation", 0)) + 1
        except Exception:
            self.settings_generation = 1

    def load_config(self):
        config = {}
        try:
            with open(self.config_file_path, 'r', encoding='utf-8') as cf:
                config = yaml.load(cf, Loader=_YamlLoader) or {}
        except Exception as e:
            logger.error(f"Error loading config file: {e}")

        self.combine_joycons = config.get("combine_joycons", True)
        self.deadzone = config.get("deadzone", 50)
        self.controller_mode = config.get("controller_mode", "Xbox")
        self.ui_scale = float(config.get("ui_scale", 1.0))

        btns = config.get("buttons", {})
        self.dual_joycons_config = ButtonConfig(btns.get("dual_joycons", {}))
        self.single_joycon_l_config = ButtonConfig(btns.get("single_joycon_l", {}))
        self.single_joycon_r_config = ButtonConfig(btns.get("single_joycon_r", {}))
        self.procon_config = ButtonConfig(btns.get("procon", {}))

        self.mouse_config = MouseConfig(config.get("mouse", {}))
        # Define categories and defaults for button remaps
        self.active_profile = config.get("active_profile", "Default")
        self.profiles = config.get("profiles", {})
        self.profile_switching_combo_trigger = config.get("profile_switching_combo_trigger", "")
        # Change Profile behavior: "Auto" cycles + auto-applies after inactivity;
        # "Manual" opens a selection notification navigated by stick/Dpad + A/B.
        self.change_profile_mode = config.get("change_profile_mode", "Manual")
        self.profile_setting_defaults = self._build_profile_setting_defaults(config)
        
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
        
        categories = ["xbox", "ps4", "ps5_winuhid", "ps5_usbip", "switch1", "switch2"]
        old_hold_mode = config.get("joycon_hold_mode", {}) or {}
        
        profile_setting_defaults = self.get_default_profile_settings()
        for prof_name, prof_data in self.profiles.items():
            prof_data.setdefault("change_profile_list", False)
            prof_data.setdefault("profile_switching_combo", "")
            for key, def_val in profile_setting_defaults.items():
                if key not in prof_data:
                    prof_data[key] = config.get(key, def_val) if prof_name == self.active_profile else def_val
        
        # Populate each category for all profiles
        for prof_name, prof_data in self.profiles.items():
            if "ps" in prof_data:
                import copy
                if "ps4" not in prof_data: prof_data["ps4"] = copy.deepcopy(prof_data["ps"])
                if "ps5" not in prof_data: prof_data["ps5"] = copy.deepcopy(prof_data["ps"])
                prof_data.pop("ps", None)
                
            if "ps5" in prof_data:
                import copy
                if "ps5_winuhid" not in prof_data: prof_data["ps5_winuhid"] = copy.deepcopy(prof_data["ps5"])
                if "ps5_usbip" not in prof_data: prof_data["ps5_usbip"] = copy.deepcopy(prof_data["ps5"])
                prof_data.pop("ps5", None)

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
                        elif key == "vibration_strength_ps5":
                            val = old_cat_data.get("vibration_strength", config.get("vibration_strength_ps5", config.get("vibration_strength", def_val)))
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
                self.ensure_mapping_scope(cat, MAPPING_SCOPE_IN_APP_GYRO)
        
        self.gyro_smoothing = 0.0 
        
        self.gyro_bias_l = config.get("gyro_bias_l", [0.0, 0.0, 0.0])
        self.gyro_bias_r = config.get("gyro_bias_r", [0.0, 0.0, 0.0])
        self.stick_r_bias = config.get("stick_r_bias", [0.0, 0.0])
        
        # MAC address -> Calibration data mapping dictionary
        self.calibration_data = config.get("calibration_data", {}) or {}
        self.joystick_calibration_data = config.get("joystick_calibration_data", {}) or {}
        self.mag_calibration_data = config.get("mag_calibration_data", {}) or {}
        self.gc_trigger_calibration_data = config.get("gc_trigger_calibration_data", {}) or {}
        self.controller_calibration_aliases = config.get("controller_calibration_aliases", {}) or {}
        self.merged_gyro_side = config.get("merged_gyro_side", {}) or {}
        
        # Persistent Cemuhook pad_id mapping
        self.cemuhook_mac_to_pad = config.get("cemuhook_mac_to_pad", {}) or {}
        self.cemuhook_pad_overwrite_idx = int(config.get("cemuhook_pad_overwrite_idx", 0))
        
        self.open_when_startup = config.get("open_when_startup", False)
        self.start_minimized = config.get("start_minimized", False)
        self.driver_installed = config.get("driver_installed", False)
        # Wired USB Pro Controller 2 support + auto-hide of its physical HID via HidHide.
        self.wired_auto_scan_enabled = config.get(
            "wired_auto_scan_enabled",
            config.get("wired_usb_enabled", True),
        )
        # Backward-compatible alias for older code/configs. Semantically this now means
        # automatic wired discovery, not whether manual wired support exists.
        self.wired_usb_enabled = self.wired_auto_scan_enabled
        self.hidhide_installed = config.get("hidhide_installed", False)
        # User preference: whether the physical Pro Controller 2 HID should be hidden via
        # HidHide when connected. Disabling in the HidHide window sets this False so a later
        # replug is NOT re-hidden (third-party software can see the controller again).
        self.hidhide_hide_enabled = config.get("hidhide_hide_enabled", True)
        self.driver_type = config.get("driver_type", "WinUHid")
        if self.driver_type not in ["WinUHid", "ViGEmBus", "USBIP"]:
            self.driver_type = "WinUHid"
        
        self.simulation_mode = config.get("simulation_mode", "PS5")
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
        self.window_x = config.get("window_x", None)
        self.window_y = config.get("window_y", None)
        self._auto_disconnect_mode = config.get("auto_disconnect_mode", "Absolute" if config.get("auto_disconnect_enabled", False) else "OFF")
        if self._auto_disconnect_mode not in ["OFF", "Inactive", "Absolute"]:
            self._auto_disconnect_mode = "Absolute" if config.get("auto_disconnect_enabled", False) else "OFF"
        self.auto_disconnect_days = int(config.get("auto_disconnect_days", 0))
        self.auto_disconnect_hours = int(config.get("auto_disconnect_hours", 0))
        self.auto_disconnect_minutes = int(config.get("auto_disconnect_minutes", 0))
        self._normalize_mode_shift_enabled_profiles()

        # abxy_mode, rumble_mode, vibration_strength, vibration_frequency are now properties managed per Emu Mode category

        logger.info(f"Config successfully loaded from {self.config_file_path}")

    def _normalize_mode_shift_enabled_profiles(self):
        """Normalize old packaged configs to the per-Gyro-Control Mode Shift shape.

        Some packaged configs persisted every Mode Shift mode as False. Without a
        marker, treat that exact all-off shape as the old baseline and restore the
        Mouse default once. If the user later explicitly turns Mouse off, the marker
        prevents future loads from changing it back.
        """
        if not isinstance(getattr(self, "profiles", None), dict):
            return
        expected_modes = tuple(MODE_SHIFT_ENABLED_DEFAULTS.keys())
        for prof in self.profiles.values():
            if not isinstance(prof, dict):
                continue
            stored = prof.get("mode_shift_enabled")
            marker_set = bool(prof.get(MODE_SHIFT_ENABLED_MIGRATION_KEY, False))
            normalized = dict(MODE_SHIFT_ENABLED_DEFAULTS)
            if isinstance(stored, dict):
                for mode in expected_modes:
                    if mode in stored:
                        normalized[mode] = bool(stored[mode])
            elif isinstance(stored, bool):
                normalized["Mouse"] = bool(stored)
            all_modes_present = isinstance(stored, dict) and all(mode in stored for mode in expected_modes)
            all_off = all_modes_present and not any(bool(stored.get(mode)) for mode in expected_modes)
            if all_off and not marker_set:
                normalized["Mouse"] = MODE_SHIFT_ENABLED_DEFAULTS["Mouse"]
            prof["mode_shift_enabled"] = normalized
            prof[MODE_SHIFT_ENABLED_MIGRATION_KEY] = True

    def _build_profile_setting_defaults(self, config):
        saved_defaults = config.get("profile_defaults", {})
        if isinstance(saved_defaults, dict) and saved_defaults:
            config = {**config, **saved_defaults}
        deadzone = config.get("virtual_gyro_soft_deadzone", 0.0)
        if isinstance(deadzone, bool):
            deadzone = 0.0
        in_app_deadzone = config.get("in_app_gyro_soft_deadzone", 0.0)
        if isinstance(in_app_deadzone, bool):
            in_app_deadzone = 0.0
        try:
            impulse_trigger_strength = int(config.get("impulse_trigger_strength", 5))
        except (TypeError, ValueError):
            impulse_trigger_strength = 5
        return {
            "gyro_mode": config.get("gyro_mode", "World"),
            "gyro_control_mode": config.get("gyro_control_mode", "Mouse"),
            "gyro_sensitivity": float(config.get("gyro_sensitivity", 0.3)),
            "r_joystick_gyro_sensitivity": float(config.get("r_joystick_gyro_sensitivity", 5.0)),
            "gyro_activation_mode": config.get("gyro_activation_mode", "Toggle"),
            "stick_mouse_sensitivity": float(config.get("stick_mouse_sensitivity", 20.0)),
            "stabilized_gyro": bool(config.get("stabilized_gyro", False)),
            "virtual_gyro_soft_deadzone": float(deadzone),
            "in_app_gyro_soft_deadzone": float(in_app_deadzone),
            "gyro_passthrough_mode": config.get("gyro_passthrough_mode", "Default"),
            "cemuhook_sensitivity": int(config.get("cemuhook_sensitivity", 1)),
            "steam_roll_compensation": bool(config.get("steam_roll_compensation", False)),
            "djg_enabled": bool(config.get("djg_enabled", False)),
            "djg_dominant_side": config.get("djg_dominant_side", "Right"),
            "djg_mode": config.get("djg_mode", "Switch Dominant Side"),
            "djg_activation": config.get("djg_activation", "Hold"),
            "audio_haptics_enabled": config.get("audio_haptics_enabled", True),
            "adaptive_triggers_enabled": config.get("adaptive_triggers_enabled", True),
            "impulse_trigger_enabled": config.get("impulse_trigger_enabled", True),
            "impulse_trigger_dynamic_frequency": config.get("impulse_trigger_dynamic_frequency", True),
            "impulse_trigger_frequency": max(1, min(10, int(config.get("impulse_trigger_frequency", 10)))),
            "impulse_trigger_strength": max(1, min(10, impulse_trigger_strength)),
        }

    def get_default_profile_settings(self):
        defaults = getattr(self, "profile_setting_defaults", None)
        if defaults:
            return defaults.copy()
        return {
            "gyro_mode": "World",
            "gyro_control_mode": "Mouse",
            "gyro_sensitivity": 0.3,
            "r_joystick_gyro_sensitivity": 5.0,
            "gyro_activation_mode": "Toggle",
            "stick_mouse_sensitivity": 20.0,
            "stabilized_gyro": False,
            "virtual_gyro_soft_deadzone": 0.0,
            "in_app_gyro_soft_deadzone": 0.0,
            "gyro_passthrough_mode": "Default",
            "cemuhook_sensitivity": 1,
            "steam_roll_compensation": False,
            "djg_enabled": False,
            "djg_dominant_side": "Right",
            "djg_mode": "Switch Dominant Side",
            "djg_activation": "Hold",
            "audio_haptics_enabled": True,
            "adaptive_triggers_enabled": True,
            "impulse_trigger_enabled": True,
            "impulse_trigger_dynamic_frequency": True,
            "impulse_trigger_frequency": 10,
            "impulse_trigger_strength": 5,
        }

    @property
    def button_remaps(self):
        return self.profiles[self.active_profile]

    def add_profile(self, name):
        if name and name not in self.profiles:
            import copy
            self.profiles[name] = copy.deepcopy(self.profiles[self.active_profile])
            self.active_profile = name
            self._bump_settings_generation()
            self.save_config()
            return True
        return False

    def rename_profile(self, new_name):
        if new_name and new_name not in self.profiles:
            self.profiles[new_name] = self.profiles.pop(self.active_profile)
            self.active_profile = new_name
            self._bump_settings_generation()
            self.save_config()
            return True
        return False

    def delete_profile(self):
        if len(self.profiles) > 1:
            self.profiles.pop(self.active_profile)
            self.active_profile = list(self.profiles.keys())[0]
            self._bump_settings_generation()
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
            "ps5_winuhid": {
                "abxy_mode": "Xbox", "c_mapping": "Default", "capt_mapping": "Default",
                "gc_trigger_mode": "100% at Max", "gl_mapping": "PS_L_Touch", "gr_mapping": "PS_R_Touch",
                "home_mapping": "Default", "rumble_mode": "Xbox", "sll_mapping": "Default",
                "slr_mapping": "PS_R_Touch", "srl_mapping": "PS_L_Touch", "srr_mapping": "Change Profile",
                "vibration_frequency": 10, "vibration_strength": 5, "vibration_strength_switch": 5, "vibration_strength_xbox": 5, "vibration_strength_ps5": 10
            },
            "ps5_usbip": {
                "abxy_mode": "Xbox", "c_mapping": "Default", "capt_mapping": "Default",
                "gc_trigger_mode": "100% at Max", "gl_mapping": "PS_L_Touch", "gr_mapping": "PS_R_Touch",
                "home_mapping": "Default", "rumble_mode": "PS5", "sll_mapping": "Default",
                "slr_mapping": "PS_R_Touch", "srl_mapping": "PS_L_Touch", "srr_mapping": "Change Profile",
                "vibration_frequency": 10, "vibration_strength": 10, "vibration_strength_switch": 5, "vibration_strength_xbox": 5, "vibration_strength_ps5": 10
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
                "gc_l_click_mapping": "Default", "gc_r_click_mapping": "Default",
                "vibration_frequency": 10, "vibration_strength": 5, "vibration_strength_switch": 5, "vibration_strength_xbox": 5
            }
        }
        for cat_data in defaults.values():
            cat_data.update(SHARED_BUTTON_MAPPING_DEFAULTS)
            for custom_key, custom_defaults in JOYSTICK_CUSTOM_DEFAULTS.items():
                cat_data.setdefault(custom_key, custom_defaults.copy())
            scoped_defaults = build_in_app_gyro_mapping_defaults(self.get_default_profile_settings().get("stick_mouse_sensitivity", 20.0))
            cat_data.setdefault(MAPPING_SCOPE_IN_APP_GYRO, scoped_defaults)
            if "gc_l_click_mapping" not in cat_data:
                cat_data["gc_l_click_mapping"] = "Default"
            if "gc_r_click_mapping" not in cat_data:
                cat_data["gc_r_click_mapping"] = "Default"
            for key, val in list(cat_data.items()):
                if key == MAPPING_SCOPE_IN_APP_GYRO:
                    continue
                if key.endswith("_mapping") or key in SHARED_BUTTON_MAPPING_DEFAULTS or key in JOYSTICK_CUSTOM_DEFAULTS:
                    cat_data[MAPPING_SCOPE_IN_APP_GYRO].setdefault(key, val.copy() if isinstance(val, dict) else val)
        return defaults.get(cat, defaults["xbox"]).copy()

    def get_default_profile_dict(self):
        categories = ["xbox", "ps4", "ps5_winuhid", "ps5_usbip", "switch1", "switch2"]
        prof_data = {
            "driver_type": "WinUHid",
            "simulation_mode": "PS5",
            "assigned_apps": [],
            "change_profile_list": False,
            "profile_switching_combo": "",
            "mode_shift_enabled": dict(MODE_SHIFT_ENABLED_DEFAULTS),
            MODE_SHIFT_ENABLED_MIGRATION_KEY: True,
        }
        prof_data.update(self.get_default_profile_settings())
        for cat in categories:
            prof_data[cat] = self.get_default_category_dict(cat)
            prof_data[cat]["joycon_hold_mode"] = {}
        return prof_data

    def reset_profile_to_default(self, name):
        if name in self.profiles:
            self.profiles[name] = self.get_default_profile_dict()
            self._bump_settings_generation()
            self.save_config()
            return True
        return False

    def reset_category_to_default(self, name, cat):
        if name in self.profiles and cat in self.profiles[name]:
            old_hold_mode = self.profiles[name][cat].get("joycon_hold_mode", {})
            self.profiles[name][cat] = self.get_default_category_dict(cat)
            self.profiles[name][cat]["joycon_hold_mode"] = old_hold_mode
            self._bump_settings_generation()
            self.save_config()
            return True
        return False

    def switch_profile(self, name):
        if name in self.profiles:
            self.active_profile = name
            self._bump_settings_generation()
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

    @property
    def steam_roll_compensation(self):
        return self.profiles.get(self.active_profile, {}).get("steam_roll_compensation", False)

    @steam_roll_compensation.setter
    def steam_roll_compensation(self, value):
        if self.active_profile in self.profiles:
            self.profiles[self.active_profile]["steam_roll_compensation"] = value

    @property
    def cemuhook_sensitivity(self):
        return self.profiles.get(self.active_profile, {}).get("cemuhook_sensitivity", 1)

    @cemuhook_sensitivity.setter
    def cemuhook_sensitivity(self, value):
        if self.active_profile in self.profiles:
            self.profiles[self.active_profile]["cemuhook_sensitivity"] = int(value)

    @property
    def djg_enabled(self):
        return self.profiles.get(self.active_profile, {}).get("djg_enabled", False)

    @djg_enabled.setter
    def djg_enabled(self, value):
        if self.active_profile in self.profiles:
            self.profiles[self.active_profile]["djg_enabled"] = value

    @property
    def djg_dominant_side(self):
        return self.profiles.get(self.active_profile, {}).get("djg_dominant_side", "Left")

    @djg_dominant_side.setter
    def djg_dominant_side(self, value):
        if self.active_profile in self.profiles:
            self.profiles[self.active_profile]["djg_dominant_side"] = value
            
    @property
    def djg_mode(self):
        return self.profiles.get(self.active_profile, {}).get("djg_mode", "Single Side Toggle")

    @djg_mode.setter
    def djg_mode(self, value):
        if self.active_profile in self.profiles:
            self.profiles[self.active_profile]["djg_mode"] = value

    @property
    def in_app_gyro_soft_deadzone(self):
        return float(self._get_profile_setting("in_app_gyro_soft_deadzone"))

    @in_app_gyro_soft_deadzone.setter
    def in_app_gyro_soft_deadzone(self, value):
        self._set_profile_setting("in_app_gyro_soft_deadzone", float(value))

    @property
    def audio_haptics_enabled(self):
        return bool(self._get_profile_setting("audio_haptics_enabled"))

    @audio_haptics_enabled.setter
    def audio_haptics_enabled(self, value):
        self._set_profile_setting("audio_haptics_enabled", bool(value))

    @property
    def adaptive_triggers_enabled(self):
        return bool(self._get_profile_setting("adaptive_triggers_enabled"))

    @adaptive_triggers_enabled.setter
    def adaptive_triggers_enabled(self, value):
        self._set_profile_setting("adaptive_triggers_enabled", bool(value))

    @property
    def impulse_trigger_enabled(self):
        return bool(self._get_profile_setting("impulse_trigger_enabled"))

    @impulse_trigger_enabled.setter
    def impulse_trigger_enabled(self, value):
        self._set_profile_setting("impulse_trigger_enabled", bool(value))

    @property
    def impulse_trigger_dynamic_frequency(self):
        return bool(self._get_profile_setting("impulse_trigger_dynamic_frequency"))

    @impulse_trigger_dynamic_frequency.setter
    def impulse_trigger_dynamic_frequency(self, value):
        self._set_profile_setting("impulse_trigger_dynamic_frequency", bool(value))

    @property
    def impulse_trigger_frequency(self):
        try:
            value = int(self._get_profile_setting("impulse_trigger_frequency"))
        except (TypeError, ValueError):
            value = 10
        return max(1, min(10, value))

    @impulse_trigger_frequency.setter
    def impulse_trigger_frequency(self, value):
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = 10
        self._set_profile_setting("impulse_trigger_frequency", max(1, min(10, value)))

    @property
    def impulse_trigger_strength(self):
        try:
            value = int(self._get_profile_setting("impulse_trigger_strength"))
        except (TypeError, ValueError):
            value = 5
        return max(1, min(10, value))

    @impulse_trigger_strength.setter
    def impulse_trigger_strength(self, value):
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = 5
        self._set_profile_setting("impulse_trigger_strength", max(1, min(10, value)))

    @property
    def djg_activation(self):
        return self._get_profile_setting("djg_activation")

    @djg_activation.setter
    def djg_activation(self, value):
        self._set_profile_setting("djg_activation", value)
        
    @property
    def audio_haptics_enabled(self):
        return bool(self._get_profile_setting("audio_haptics_enabled"))

    @audio_haptics_enabled.setter
    def audio_haptics_enabled(self, value):
        self._set_profile_setting("audio_haptics_enabled", bool(value))

    @property
    def adaptive_triggers_enabled(self):
        return bool(self._get_profile_setting("adaptive_triggers_enabled"))

    @adaptive_triggers_enabled.setter
    def adaptive_triggers_enabled(self, value):
        self._set_profile_setting("adaptive_triggers_enabled", bool(value))


    def _get_profile_setting(self, key):
        prof = self.profiles.get(self.active_profile, {})
        if key in prof:
            return prof[key]
        # Avoid copying the whole defaults dict (get_default_profile_settings copies);
        # this is on the hot path via gyro_control_mode scope resolution.
        defaults = getattr(self, "profile_setting_defaults", None)
        if not defaults:
            defaults = self.get_default_profile_settings()
        return defaults.get(key)

    def _set_profile_setting(self, key, value):
        if self.active_profile in self.profiles:
            self.profiles[self.active_profile][key] = value

    @property
    def gyro_mode(self):
        return self._get_profile_setting("gyro_mode")

    @gyro_mode.setter
    def gyro_mode(self, value):
        self._set_profile_setting("gyro_mode", value)

    @property
    def gyro_control_mode(self):
        return self._get_profile_setting("gyro_control_mode")

    @gyro_control_mode.setter
    def gyro_control_mode(self, value):
        self._set_profile_setting("gyro_control_mode", value)

    @property
    def mode_shift_enabled(self):
        """Whether entering In-app Gyro mode auto-applies the Mode Shift Mapping layer.
        Stored per (profile, Gyro Control mode); defaults to On for Mouse and Off for
        R Joystick / Steering."""
        mode = self.gyro_control_mode
        stored = self.profiles.get(self.active_profile, {}).get("mode_shift_enabled")
        if isinstance(stored, dict) and mode in stored:
            return bool(stored[mode])
        return MODE_SHIFT_ENABLED_DEFAULTS.get(mode, False)

    @mode_shift_enabled.setter
    def mode_shift_enabled(self, value):
        if self.active_profile not in self.profiles:
            return
        prof = self.profiles[self.active_profile]
        stored = prof.get("mode_shift_enabled")
        stored = dict(stored) if isinstance(stored, dict) else dict(MODE_SHIFT_ENABLED_DEFAULTS)
        stored[self.gyro_control_mode] = bool(value)
        prof["mode_shift_enabled"] = stored
        prof[MODE_SHIFT_ENABLED_MIGRATION_KEY] = True
        self._bump_settings_generation()

    def active_in_app_gyro_scope(self):
        """Physical In-app Gyro mapping store for the current Gyro Control mode."""
        if self.gyro_control_mode == "Steering":
            return MAPPING_SCOPE_IN_APP_GYRO_STEERING
        if self.gyro_control_mode == "R Joystick":
            return MAPPING_SCOPE_IN_APP_GYRO_RSTICK
        return MAPPING_SCOPE_IN_APP_GYRO

    def _resolve_in_app_gyro_scope(self, scope):
        """Translate the logical In-app Gyro scope used by the UI/runtime into the
        physical store for the active Gyro Control mode (Mouse / R Joystick / Steering)."""
        if scope == MAPPING_SCOPE_IN_APP_GYRO and self.gyro_control_mode == "Steering":
            return MAPPING_SCOPE_IN_APP_GYRO_STEERING
        if scope == MAPPING_SCOPE_IN_APP_GYRO and self.gyro_control_mode == "R Joystick":
            return MAPPING_SCOPE_IN_APP_GYRO_RSTICK
        return scope

    @property
    def gyro_sensitivity(self):
        return float(self._get_profile_setting("gyro_sensitivity"))

    @gyro_sensitivity.setter
    def gyro_sensitivity(self, value):
        self._set_profile_setting("gyro_sensitivity", float(value))

    @property
    def r_joystick_gyro_sensitivity(self):
        return float(self._get_profile_setting("r_joystick_gyro_sensitivity"))

    @r_joystick_gyro_sensitivity.setter
    def r_joystick_gyro_sensitivity(self, value):
        self._set_profile_setting("r_joystick_gyro_sensitivity", float(value))

    @property
    def gyro_activation_mode(self):
        return self._get_profile_setting("gyro_activation_mode")

    @gyro_activation_mode.setter
    def gyro_activation_mode(self, value):
        self._set_profile_setting("gyro_activation_mode", value)

    @property
    def stick_mouse_sensitivity(self):
        return float(self._get_profile_setting("stick_mouse_sensitivity"))

    @stick_mouse_sensitivity.setter
    def stick_mouse_sensitivity(self, value):
        self._set_profile_setting("stick_mouse_sensitivity", float(value))

    @property
    def stabilized_gyro(self):
        return bool(self._get_profile_setting("stabilized_gyro"))

    @stabilized_gyro.setter
    def stabilized_gyro(self, value):
        self._set_profile_setting("stabilized_gyro", bool(value))

    @property
    def virtual_gyro_soft_deadzone(self):
        return float(self._get_profile_setting("virtual_gyro_soft_deadzone"))

    @virtual_gyro_soft_deadzone.setter
    def virtual_gyro_soft_deadzone(self, value):
        self._set_profile_setting("virtual_gyro_soft_deadzone", float(value))

    @property
    def gyro_passthrough_mode(self):
        return self._get_profile_setting("gyro_passthrough_mode")

    @gyro_passthrough_mode.setter
    def gyro_passthrough_mode(self, value):
        self._set_profile_setting("gyro_passthrough_mode", value)

    @property
    def steam_roll_compensation(self):
        return bool(self._get_profile_setting("steam_roll_compensation"))

    @steam_roll_compensation.setter
    def steam_roll_compensation(self, value):
        self._set_profile_setting("steam_roll_compensation", bool(value))

    @property
    def cemuhook_sensitivity(self):
        return int(self._get_profile_setting("cemuhook_sensitivity"))

    @cemuhook_sensitivity.setter
    def cemuhook_sensitivity(self, value):
        self._set_profile_setting("cemuhook_sensitivity", int(value))

    @property
    def djg_enabled(self):
        return bool(self._get_profile_setting("djg_enabled"))

    @djg_enabled.setter
    def djg_enabled(self, value):
        self._set_profile_setting("djg_enabled", bool(value))

    @property
    def djg_dominant_side(self):
        return self._get_profile_setting("djg_dominant_side")

    @djg_dominant_side.setter
    def djg_dominant_side(self, value):
        self._set_profile_setting("djg_dominant_side", value)

    @property
    def djg_mode(self):
        return self._get_profile_setting("djg_mode")

    @djg_mode.setter
    def djg_mode(self, value):
        self._set_profile_setting("djg_mode", value)


    def save_config(self):
        # Snapshot config values in the calling thread
        data = {
            'driver_installed': self.driver_installed,
            'wired_auto_scan_enabled': self.wired_auto_scan_enabled,
            'wired_usb_enabled': self.wired_auto_scan_enabled,
            'hidhide_installed': self.hidhide_installed,
            'hidhide_hide_enabled': self.hidhide_hide_enabled,
            'driver_type': self.driver_type,
            'vigembus_sim_mode': self.vigembus_sim_mode,
            'winuhid_sim_mode': self.winuhid_sim_mode,
            'usbip_sim_mode': self.usbip_sim_mode,
            'vigembus_installed': self.vigembus_installed,
            'window_width': self.window_width,
            'window_height': self.window_height,
            'window_x': self.window_x,
            'window_y': self.window_y,
            'ui_scale': self.ui_scale,
            'auto_disconnect_enabled': self.auto_disconnect_enabled,
            'auto_disconnect_mode': self.auto_disconnect_mode,
            'auto_disconnect_days': self.auto_disconnect_days,
            'auto_disconnect_hours': self.auto_disconnect_hours,
            'auto_disconnect_minutes': self.auto_disconnect_minutes,
            'vibration_strength': self.vibration_strength,
            'vibration_strength_xbox': self.button_remaps.get(self.get_current_category(), {}).get("vibration_strength_xbox", 5),
            'vibration_strength_switch': self.button_remaps.get(self.get_current_category(), {}).get("vibration_strength_switch", 5),
            'vibration_strength_ps5': self.button_remaps.get(self.get_current_category(), {}).get("vibration_strength_ps5", 10),
            'vibration_frequency': self.vibration_frequency,
            'rumble_delay_ms': getattr(self, "rumble_delay_ms", 0),
            'rumble_mode': self.rumble_mode,
            'simulation_mode': self.simulation_mode,
            'open_when_startup': self.open_when_startup,
            'start_minimized': self.start_minimized,
            'stabilized_gyro': self.stabilized_gyro,
            'virtual_gyro_soft_deadzone': self.virtual_gyro_soft_deadzone,
            'in_app_gyro_soft_deadzone': self.in_app_gyro_soft_deadzone,
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
            'r_joystick_gyro_sensitivity': self.r_joystick_gyro_sensitivity,
            'gyro_activation_mode': self.gyro_activation_mode,
            'stick_mouse_sensitivity': self.stick_mouse_sensitivity,
            'gyro_bias_l': self.gyro_bias_l,
            'gyro_bias_r': self.gyro_bias_r,
            'stick_r_bias': self.stick_r_bias,
            'calibration_data': self.calibration_data,
            'joystick_calibration_data': self.joystick_calibration_data,
            'mag_calibration_data': self.mag_calibration_data,
            'gc_trigger_calibration_data': self.gc_trigger_calibration_data,
            'controller_calibration_aliases': self.controller_calibration_aliases,
            'gc_trigger_mode': self.gc_trigger_mode,
            'gc_l_click_mapping': self.gc_l_click_mapping,
            'gc_r_click_mapping': self.gc_r_click_mapping,
            'joycon_hold_mode': self.joycon_hold_mode,
            'merged_gyro_side': self.merged_gyro_side,
            'cemuhook_mac_to_pad': self.cemuhook_mac_to_pad,
            'cemuhook_pad_overwrite_idx': self.cemuhook_pad_overwrite_idx,
            'active_profile': self.active_profile,
            'profile_switching_combo_trigger': getattr(self, "profile_switching_combo_trigger", ""),
            'change_profile_mode': getattr(self, "change_profile_mode", "Manual"),
            'profile_defaults': self.get_default_profile_settings(),
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
                                existing_data = yaml.load(f, Loader=_YamlLoader) or {}
                        except Exception:
                            pass

                    existing_data.update(data)

                    with open(self.config_file_path, 'w', encoding='utf-8') as f:
                        yaml.dump(existing_data, f, Dumper=_YamlDumper, default_flow_style=False)
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

    @property
    def rumble_delay_ms(self):
        cat = self.get_current_category()
        return int(self.button_remaps.get(cat, {}).get("rumble_delay_ms", 0))

    @rumble_delay_ms.setter
    def rumble_delay_ms(self, val):
        cat = self.get_current_category()
        if cat not in self.button_remaps: self.button_remaps[cat] = {}
        self.button_remaps[cat]["rumble_delay_ms"] = int(val)

    def get_current_category(self):
        mode = getattr(self, "simulation_mode", "PS5")
        if mode == "Switch2":
            return "switch2"
        elif mode == "Switch1":
            return "switch1"
        elif mode == "PS4":
            return "ps4"
        elif mode == "PS5":
            driver = getattr(self, "driver_type", "WinUHid")
            if driver == "USBIP":
                return "ps5_usbip"
            else:
                return "ps5_winuhid"
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
        return self.button_remaps.get(cat, {}).get("gr_mapping", "In-app Gyro")
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
        return self.button_remaps.get(cat, {}).get("slr_mapping", "In-app Gyro")
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
    def gc_l_click_mapping(self):
        cat = self.get_current_category()
        return self.button_remaps.get(cat, {}).get("gc_l_click_mapping", "Default")
    @gc_l_click_mapping.setter
    def gc_l_click_mapping(self, val):
        cat = self.get_current_category()
        if cat not in self.button_remaps: self.button_remaps[cat] = {}
        self.button_remaps[cat]["gc_l_click_mapping"] = val

    @property
    def gc_r_click_mapping(self):
        cat = self.get_current_category()
        return self.button_remaps.get(cat, {}).get("gc_r_click_mapping", "Default")
    @gc_r_click_mapping.setter
    def gc_r_click_mapping(self, val):
        cat = self.get_current_category()
        if cat not in self.button_remaps: self.button_remaps[cat] = {}
        self.button_remaps[cat]["gc_r_click_mapping"] = val

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

    def get_mapping_setting(self, key, default="Default"):
        cat = self.get_current_category()
        return self.button_remaps.get(cat, {}).get(f"{key}_mapping", default)

    def set_mapping_setting(self, key, val):
        cat = self.get_current_category()
        if cat not in self.button_remaps:
            self.button_remaps[cat] = {}
        old_val = self.button_remaps[cat].get(f"{key}_mapping", self._get_mapping_reset_default(cat, key, None))
        self.button_remaps[cat][f"{key}_mapping"] = val
        
        if key in ("l_joystick", "r_joystick") and old_val == "Custom" and val != "Custom":
            custom_key = f"{key}_custom"
            old_dict = self.button_remaps[cat].get(custom_key, {}).copy()
            new_dict = {}
            changed = False
            for d in ("up", "down", "left", "right", "click"):
                d_val = old_dict.get(d, "Default")
                if self._is_mode_shift_value(d_val) or self._is_in_app_gyro_value(d_val):
                    changed = True
                if f"{key}_{d}_mapping" in self.button_remaps[cat]:
                    del self.button_remaps[cat][f"{key}_{d}_mapping"]
            if changed:
                self.button_remaps[cat][custom_key] = new_dict
                self._sync_in_app_gyro_joystick_custom(cat, key, old_dict, new_dict, None)
                
        self._sync_in_app_gyro_mapping_key(cat, key, old_val, val, None)
        self._bump_settings_generation()

    def ensure_mapping_scope(self, cat=None, scope=None):
        if scope is None:
            return None
        if cat is None:
            cat = self.get_current_category()
        if cat not in self.button_remaps:
            self.button_remaps[cat] = {}
        scoped = self.button_remaps[cat].get(scope)
        if isinstance(scoped, dict):
            # Fast path: the scope is already initialized. Rebuilding the default
            # dicts and rescanning button_remaps on every call is unnecessary on
            # reads (set_mapping_setting* keeps the In-app Gyro sync up to date) and,
            # because this runs ~20+ times per input report, was the cause of the
            # In-app Gyro input lag/stutter.
            return scoped
        scoped = {}
        self.button_remaps[cat][scope] = scoped
        if scope in (MAPPING_SCOPE_IN_APP_GYRO, MAPPING_SCOPE_IN_APP_GYRO_RSTICK, MAPPING_SCOPE_IN_APP_GYRO_STEERING):
            if scope == MAPPING_SCOPE_IN_APP_GYRO_RSTICK:
                gc_trigger_default = self.get_default_category_dict(cat).get("gc_trigger_mode", "Hair Trigger")
                defaults = build_in_app_gyro_rstick_mapping_defaults(gc_trigger_default)
            elif scope == MAPPING_SCOPE_IN_APP_GYRO_STEERING:
                gc_trigger_default = self.get_default_category_dict(cat).get("gc_trigger_mode", "Hair Trigger")
                defaults = build_in_app_gyro_steering_mapping_defaults(gc_trigger_default)
            else:
                defaults = build_in_app_gyro_mapping_defaults(self.stick_mouse_sensitivity)
            for key, def_val in defaults.items():
                if key not in scoped:
                    scoped[key] = def_val.copy() if isinstance(def_val, dict) else def_val
            # On creation, reconcile the activation buttons with Controller Mapping for
            # the active scope: Mode Shift always, In-app Gyro only while Mode Shift is On.
            if scope == self.active_in_app_gyro_scope():
                mode_shift_on = self.mode_shift_enabled
                for key, val in list(self.button_remaps[cat].items()):
                    if not key.endswith("_mapping"):
                        continue
                    base_ms = self._is_mode_shift_value(val)
                    scoped_ms = self._is_mode_shift_value(scoped.get(key))
                    if base_ms or scoped_ms:
                        ms_val = val if base_ms else scoped.get(key)
                        self.button_remaps[cat][key] = ms_val
                        scoped[key] = ms_val
                    elif mode_shift_on and (self._is_in_app_gyro_value(val) or self._is_in_app_gyro_value(scoped.get(key))):
                        ia_val = val if self._is_in_app_gyro_value(val) else scoped.get(key)
                        self.button_remaps[cat][key] = ia_val
                        scoped[key] = ia_val
            return scoped
        for key, def_val in SHARED_BUTTON_MAPPING_DEFAULTS.items():
            if key not in scoped:
                scoped[key] = self.button_remaps[cat].get(key, def_val)
        for key, def_val in JOYSTICK_CUSTOM_DEFAULTS.items():
            if key not in scoped:
                base_val = self.button_remaps[cat].get(key, def_val)
                scoped[key] = base_val.copy() if isinstance(base_val, dict) else def_val.copy()
        for key, val in list(self.button_remaps[cat].items()):
            if key == scope:
                continue
            if key.endswith("_mapping") or key in SHARED_BUTTON_MAPPING_DEFAULTS or key in JOYSTICK_CUSTOM_DEFAULTS:
                scoped.setdefault(key, val.copy() if isinstance(val, dict) else val)
        return scoped

    def get_mapping_scope_dict(self, scope):
        """Return the raw mapping dict for a scope (resolved for the active Gyro
        Control mode). Lets per-report hot-path callers resolve once and index the
        dict directly (dict.get) instead of calling a resolving getter per key.
        No dict is rebuilt; ensure_mapping_scope fast-paths once initialized."""
        scope = self._resolve_in_app_gyro_scope(scope)
        cat = self.get_current_category()
        if scope:
            return self.ensure_mapping_scope(cat, scope)
        return self.button_remaps.get(cat, {})

    def get_mapping_setting_scoped(self, key, default="Default", scope=None):
        scope = self._resolve_in_app_gyro_scope(scope)
        cat = self.get_current_category()
        if scope:
            scoped = self.ensure_mapping_scope(cat, scope)
            val = scoped.get(f"{key}_mapping", default)
        else:
            val = self.get_mapping_setting(key, default)
        if val == "Gyro": val = "In-app Gyro"
        if val == "In-app Gyro":
            val = f"Custom[Hold]:{IN_APP_GYRO_TOKEN}"
        return val

    def set_mapping_setting_scoped(self, key, val, scope=None):
        scope = self._resolve_in_app_gyro_scope(scope)
        if scope:
            cat = self.get_current_category()
            scoped = self.ensure_mapping_scope(cat, scope)
            old_val = scoped.get(f"{key}_mapping", self._get_mapping_reset_default(cat, key, scope))
            scoped[f"{key}_mapping"] = val
            
            if key in ("l_joystick", "r_joystick") and old_val == "Custom" and val != "Custom":
                custom_key = f"{key}_custom"
                old_dict = scoped.get(custom_key, {}).copy()
                new_dict = {}
                changed = False
                for d in ("up", "down", "left", "right", "click"):
                    d_val = old_dict.get(d, "Default")
                    if self._is_mode_shift_value(d_val) or self._is_in_app_gyro_value(d_val):
                        changed = True
                    if f"{key}_{d}_mapping" in scoped:
                        del scoped[f"{key}_{d}_mapping"]
                if changed:
                    scoped[custom_key] = new_dict
                    self._sync_in_app_gyro_joystick_custom(cat, key, old_dict, new_dict, scope)
                    
            self._sync_in_app_gyro_mapping_key(cat, key, old_val, val, scope)
            self._bump_settings_generation()
            return
        self.set_mapping_setting(key, val)

    def _get_mapping_reset_default(self, cat, key, scope=None):
        if scope == MAPPING_SCOPE_IN_APP_GYRO:
            return build_in_app_gyro_mapping_defaults(self.stick_mouse_sensitivity).get(f"{key}_mapping", "Default")
        if scope == MAPPING_SCOPE_IN_APP_GYRO_RSTICK:
            gc_trigger_default = self.get_default_category_dict(cat).get("gc_trigger_mode", "Hair Trigger")
            return build_in_app_gyro_rstick_mapping_defaults(gc_trigger_default).get(f"{key}_mapping", "Default")
        if scope == MAPPING_SCOPE_IN_APP_GYRO_STEERING:
            gc_trigger_default = self.get_default_category_dict(cat).get("gc_trigger_mode", "Hair Trigger")
            return build_in_app_gyro_steering_mapping_defaults(gc_trigger_default).get(f"{key}_mapping", "Default")
        return self.get_default_category_dict(cat).get(f"{key}_mapping", "Default")

    def _is_mode_shift_value(self, val):
        return isinstance(val, str) and val.startswith("Custom") and val.endswith(":" + MODE_SHIFT_TOKEN)

    def _is_in_app_gyro_value(self, val):
        if not isinstance(val, str):
            return False
        if val in ("Gyro", "In-app Gyro"):
            return True
        if not val.startswith("Custom"):
            return False
        if "]:" in val:
            tokens = val.split("]:")[1].split("+")
        elif ":" in val:
            tokens = val.split(":")[1].split("+")
        else:
            return False
        return IN_APP_GYRO_TOKEN in tokens

    def _sync_in_app_gyro_mapping_key(self, cat, key, old_val, new_val, source_scope):
        if not key or key.endswith("_direction"):
            return
        mapping_key = f"{key}_mapping"
        active_scope = self.active_in_app_gyro_scope()
        # Resolve the "other" store to mirror into, plus the scope used to look up that
        # store's reset default when a synced value is cleared.
        if source_scope in (MAPPING_SCOPE_IN_APP_GYRO, MAPPING_SCOPE_IN_APP_GYRO_RSTICK, MAPPING_SCOPE_IN_APP_GYRO_STEERING):
            # Edited the active In-app Gyro store -> mirror into Controller Mapping.
            if source_scope != active_scope:
                return
            if cat not in self.button_remaps:
                self.button_remaps[cat] = {}
            target = self.button_remaps[cat]
            reset_scope = None
        else:
            # Edited Controller Mapping -> mirror into the active In-app Gyro store.
            if active_scope is None:
                return
            target = self.button_remaps[cat].get(active_scope)
            if not isinstance(target, dict):
                target = self.ensure_mapping_scope(cat, active_scope)
            reset_scope = active_scope

        # Mode Shift back button: ALWAYS cross-synced between the two stores, regardless
        # of the Mode Shift On/Off toggle. Setting a button to Mode Shift mirrors it (with
        # its Hold/Tap form); changing away resets the mirrored button to its default.
        if self._is_mode_shift_value(new_val):
            target[mapping_key] = new_val
            return

        if self._is_mode_shift_value(old_val) and self._is_mode_shift_value(target.get(mapping_key)):
            target[mapping_key] = self._get_mapping_reset_default(cat, key, reset_scope)
            return

        # In-app Gyro activation button: cross-synced only while Mode Shift is On (Off
        # keeps the two stores independent).
        if not self.mode_shift_enabled:
            return
        
        if self._is_in_app_gyro_value(new_val):
            target[mapping_key] = new_val
        elif self._is_in_app_gyro_value(old_val) and self._is_in_app_gyro_value(target.get(mapping_key)):
            target[mapping_key] = self._get_mapping_reset_default(cat, key, reset_scope)

    def _sync_in_app_gyro_joystick_custom(self, cat, key, old_dict, new_dict, source_scope):
        if key not in ("l_joystick", "r_joystick"):
            return
        active_scope = self.active_in_app_gyro_scope()
        if source_scope in (MAPPING_SCOPE_IN_APP_GYRO, MAPPING_SCOPE_IN_APP_GYRO_RSTICK, MAPPING_SCOPE_IN_APP_GYRO_STEERING):
            if source_scope != active_scope:
                return
            if cat not in self.button_remaps:
                self.button_remaps[cat] = {}
            target = self.button_remaps[cat]
            reset_scope = None
        else:
            if active_scope is None:
                return
            target = self.button_remaps[cat].get(active_scope)
            if not isinstance(target, dict):
                target = self.ensure_mapping_scope(cat, active_scope)
            reset_scope = active_scope
            
        custom_key = f"{key}_custom"
        target_dict = target.get(custom_key, {})
        if not isinstance(target_dict, dict):
            target_dict = {}
        target_dict = target_dict.copy()
            
        changed = False
        
        for d in ("up", "down", "left", "right", "click"):
            old_d = old_dict.get(d, "Default")
            new_d = new_dict.get(d, "Default")
            tgt_d = target_dict.get(d, "Default")
            if old_d == new_d:
                continue
            
            if self._is_mode_shift_value(new_d):
                target_dict[d] = new_d
                changed = True
                continue

            if self._is_mode_shift_value(old_d) and self._is_mode_shift_value(tgt_d):
                target_dict[d] = "Default"
                changed = True
                continue

            if not self.mode_shift_enabled:
                continue
            
            if self._is_in_app_gyro_value(new_d):
                target_dict[d] = new_d
                changed = True
            elif self._is_in_app_gyro_value(old_d) and self._is_in_app_gyro_value(tgt_d):
                target_dict[d] = "Default"
                changed = True
                    
        if changed:
            target[custom_key] = target_dict
            # Only set parent to Custom if there is actually a custom setting
            has_custom = any(v != "Default" for v in target_dict.values())
            if has_custom and target.get(f"{key}_mapping", "Default") != "Custom":
                target[f"{key}_mapping"] = "Custom"
            elif not has_custom and target.get(f"{key}_mapping", "Default") == "Custom":
                target[f"{key}_mapping"] = "Default"

    def sync_active_in_app_gyro_activation(self):
        """Union-sync the 'In-app Gyro' activation buttons between Controller Mapping
        and the active In-app Gyro scope (any button that is In-app Gyro in either
        becomes In-app Gyro in both). Call after a Gyro Control mode switch, since
        ensure_mapping_scope only performs this once on creation (reads fast-path).
        The Mode Shift back button always union-syncs; the In-app Gyro activation
        button only union-syncs while Mode Shift is On."""
        cat = self.get_current_category()
        if cat not in self.button_remaps:
            self.button_remaps[cat] = {}
        active_scope = self.active_in_app_gyro_scope()
        if active_scope is None:
            return
        scoped = self.ensure_mapping_scope(cat, active_scope)
        mode_shift_on = self.mode_shift_enabled
        for key, val in list(self.button_remaps[cat].items()):
            if key.endswith("_custom") and isinstance(val, dict):
                base_key = key[:-7] # remove _custom
                scoped_dict = scoped.get(key, {})
                changed_base = False
                changed_scoped = False
                for d in ("up", "down", "left", "right", "click"):
                    d_val = val.get(d, "Default")
                    d_scoped = scoped_dict.get(d, "Default")
                    base_ms = self._is_mode_shift_value(d_val)
                    scoped_ms = self._is_mode_shift_value(d_scoped)
                    if base_ms or scoped_ms:
                        ms_val = d_val if base_ms else d_scoped
                        if val.get(d) != ms_val:
                            val[d] = ms_val
                            changed_base = True
                        if scoped_dict.get(d) != ms_val:
                            scoped_dict[d] = ms_val
                            changed_scoped = True
                    elif mode_shift_on and (self._is_in_app_gyro_value(d_val) or self._is_in_app_gyro_value(d_scoped)):
                        ia_val = d_val if self._is_in_app_gyro_value(d_val) else d_scoped
                        if val.get(d) != ia_val:
                            val[d] = ia_val
                            changed_base = True
                        if scoped_dict.get(d) != ia_val:
                            scoped_dict[d] = ia_val
                            changed_scoped = True
                if changed_base:
                    self.button_remaps[cat][key] = val
                    has_custom_base = any(v != "Default" for v in val.values())
                    if has_custom_base and self.button_remaps[cat].get(f"{base_key}_mapping", "Default") != "Custom":
                        self.button_remaps[cat][f"{base_key}_mapping"] = "Custom"
                    elif not has_custom_base and self.button_remaps[cat].get(f"{base_key}_mapping", "Default") == "Custom":
                        self.button_remaps[cat][f"{base_key}_mapping"] = "Default"
                if changed_scoped:
                    scoped[key] = scoped_dict
                    has_custom_scoped = any(v != "Default" for v in scoped_dict.values())
                    if has_custom_scoped and scoped.get(f"{base_key}_mapping", "Default") != "Custom":
                        scoped[f"{base_key}_mapping"] = "Custom"
                    elif not has_custom_scoped and scoped.get(f"{base_key}_mapping", "Default") == "Custom":
                        scoped[f"{base_key}_mapping"] = "Default"
            elif key.endswith("_mapping"):
                base_ms = self._is_mode_shift_value(val)
                scoped_ms = self._is_mode_shift_value(scoped.get(key))
                if base_ms or scoped_ms:
                    ms_val = val if base_ms else scoped.get(key)
                    self.button_remaps[cat][key] = ms_val
                    scoped[key] = ms_val
                elif mode_shift_on and (self._is_in_app_gyro_value(val) or self._is_in_app_gyro_value(scoped.get(key))):
                    ia_val = val if self._is_in_app_gyro_value(val) else scoped.get(key)
                    self.button_remaps[cat][key] = ia_val
                    scoped[key] = ia_val
            elif key.endswith("_custom") and isinstance(val, dict):
                scoped_val = scoped.get(key, {})
                if not isinstance(scoped_val, dict):
                    scoped_val = {}
                changed_base = False
                changed_scoped = False
                val_copy = val.copy()
                scoped_copy = scoped_val.copy()
                for d in ("up", "down", "left", "right", "click"):
                    d_val = val.get(d, "Default")
                    s_val = scoped_val.get(d, "Default")
                    base_ms = self._is_mode_shift_value(d_val)
                    scoped_ms = self._is_mode_shift_value(s_val)
                    if base_ms or scoped_ms:
                        ms_val = d_val if base_ms else s_val
                        val_copy[d] = ms_val
                        scoped_copy[d] = ms_val
                        changed_base = changed_scoped = True
                    elif mode_shift_on and (self._is_in_app_gyro_value(d_val) or self._is_in_app_gyro_value(s_val)):
                        ia_val = d_val if self._is_in_app_gyro_value(d_val) else s_val
                        val_copy[d] = ia_val
                        scoped_copy[d] = ia_val
                        changed_base = changed_scoped = True
                if changed_base:
                    self.button_remaps[cat][key] = val_copy
                if changed_scoped:
                    scoped[key] = scoped_copy
        self._bump_settings_generation()

    def reset_in_app_gyro_mode_mapping(self):
        cat = self.get_current_category()
        if cat not in self.button_remaps:
            self.button_remaps[cat] = {}
        scope = self.active_in_app_gyro_scope()
        if scope == MAPPING_SCOPE_IN_APP_GYRO_RSTICK:
            gc_trigger_default = self.get_default_category_dict(cat).get("gc_trigger_mode", "Hair Trigger")
            defaults = build_in_app_gyro_rstick_mapping_defaults(gc_trigger_default)
        elif scope == MAPPING_SCOPE_IN_APP_GYRO_STEERING:
            gc_trigger_default = self.get_default_category_dict(cat).get("gc_trigger_mode", "Hair Trigger")
            defaults = build_in_app_gyro_steering_mapping_defaults(gc_trigger_default)
        else:
            defaults = build_in_app_gyro_mapping_defaults(self.stick_mouse_sensitivity)
        self.button_remaps[cat][scope] = {
            key: val.copy() if isinstance(val, dict) else val
            for key, val in defaults.items()
        }
        self._bump_settings_generation()

    def copy_controller_mapping_to_in_app_gyro_mode_mapping(self):
        cat = self.get_current_category()
        if cat not in self.button_remaps:
            self.button_remaps[cat] = {}
        scope = self.active_in_app_gyro_scope()
        copied = {}
        for key, val in self.button_remaps[cat].items():
            if key in (MAPPING_SCOPE_IN_APP_GYRO, MAPPING_SCOPE_IN_APP_GYRO_RSTICK, MAPPING_SCOPE_IN_APP_GYRO_STEERING, "joycon_hold_mode"):
                continue
            if key.endswith("_mapping") or key in SHARED_BUTTON_MAPPING_DEFAULTS or key in JOYSTICK_CUSTOM_DEFAULTS or key == "gc_trigger_mode":
                copied[key] = val.copy() if isinstance(val, dict) else val
        if scope == MAPPING_SCOPE_IN_APP_GYRO_RSTICK:
            gc_trigger_default = self.get_default_category_dict(cat).get("gc_trigger_mode", "Hair Trigger")
            defaults = build_in_app_gyro_rstick_mapping_defaults(gc_trigger_default)
        elif scope == MAPPING_SCOPE_IN_APP_GYRO_STEERING:
            gc_trigger_default = self.get_default_category_dict(cat).get("gc_trigger_mode", "Hair Trigger")
            defaults = build_in_app_gyro_steering_mapping_defaults(gc_trigger_default)
        else:
            defaults = build_in_app_gyro_mapping_defaults(self.stick_mouse_sensitivity)
        for key, val in defaults.items():
            copied.setdefault(key, val.copy() if isinstance(val, dict) else val)
        self.button_remaps[cat][scope] = copied
        self._bump_settings_generation()

    def get_joystick_custom(self, key):
        cat = self.get_current_category()
        custom_key = f"{key}_custom"
        defaults = JOYSTICK_CUSTOM_DEFAULTS.get(custom_key, {}).copy()
        stored = self.button_remaps.get(cat, {}).get(custom_key, {})
        if isinstance(stored, dict):
            defaults.update(stored)
        return defaults

    def set_joystick_custom(self, key, val):
        cat = self.get_current_category()
        old_val = self.get_joystick_custom(key)
        if cat not in self.button_remaps:
            self.button_remaps[cat] = {}
        defaults = JOYSTICK_CUSTOM_DEFAULTS.get(f"{key}_custom", {}).copy()
        if isinstance(val, dict):
            defaults.update(val)
        self.button_remaps[cat][f"{key}_custom"] = defaults
        self._sync_in_app_gyro_joystick_custom(cat, key, old_val, defaults, None)
        self._bump_settings_generation()

    def get_joystick_custom_scoped(self, key, scope=None):
        scope = self._resolve_in_app_gyro_scope(scope)
        if not scope:
            return self.get_joystick_custom(key)
        cat = self.get_current_category()
        scoped = self.ensure_mapping_scope(cat, scope)
        custom_key = f"{key}_custom"
        
        defaults = JOYSTICK_CUSTOM_DEFAULTS.get(custom_key, {}).copy()
        
        stored = scoped.get(custom_key, {})
        if isinstance(stored, dict):
            for k, v in stored.items():
                defaults[k] = v
        return defaults

    def set_joystick_custom_scoped(self, key, val, scope=None):
        scope = self._resolve_in_app_gyro_scope(scope)
        if not scope:
            self.set_joystick_custom(key, val)
            return
        cat = self.get_current_category()
        scoped = self.ensure_mapping_scope(cat, scope)
        old_val = self.get_joystick_custom_scoped(key, scope)
        defaults = JOYSTICK_CUSTOM_DEFAULTS.get(f"{key}_custom", {}).copy()
        if isinstance(val, dict):
            defaults.update(val)
        scoped[f"{key}_custom"] = defaults
        self._sync_in_app_gyro_joystick_custom(cat, key, old_val, defaults, scope)
        self._bump_settings_generation()

    def get_joystick_setting(self, key, setting, default=None):
        cat = self.get_current_category()
        defaults = SHARED_BUTTON_MAPPING_DEFAULTS
        full_key = f"{key}_{setting}"
        if default is None:
            default = defaults.get(full_key)
        return self.button_remaps.get(cat, {}).get(full_key, default)

    def set_joystick_setting(self, key, setting, value):
        cat = self.get_current_category()
        if cat not in self.button_remaps:
            self.button_remaps[cat] = {}
        self.button_remaps[cat][f"{key}_{setting}"] = value
        self._bump_settings_generation()

    def get_joystick_setting_scoped(self, key, setting, default=None, scope=None):
        scope = self._resolve_in_app_gyro_scope(scope)
        if not scope:
            return self.get_joystick_setting(key, setting, default)
        if scope == MAPPING_SCOPE_IN_APP_GYRO and setting == "mouse_sensitivity":
            return self.stick_mouse_sensitivity
        cat = self.get_current_category()
        scoped = self.ensure_mapping_scope(cat, scope)
        full_key = f"{key}_{setting}"
        if default is None:
            default = SHARED_BUTTON_MAPPING_DEFAULTS.get(full_key)
        return scoped.get(full_key, default)

    def set_joystick_setting_scoped(self, key, setting, value, scope=None):
        scope = self._resolve_in_app_gyro_scope(scope)
        if not scope:
            self.set_joystick_setting(key, setting, value)
            return
        if scope == MAPPING_SCOPE_IN_APP_GYRO and setting == "mouse_sensitivity":
            self.stick_mouse_sensitivity = float(value)
        cat = self.get_current_category()
        scoped = self.ensure_mapping_scope(cat, scope)
        scoped[f"{key}_{setting}"] = value
        self._bump_settings_generation()

    def get_scoped_category_setting(self, key, default=None, scope=None):
        scope = self._resolve_in_app_gyro_scope(scope)
        cat = self.get_current_category()
        if scope:
            scoped = self.ensure_mapping_scope(cat, scope)
            return scoped.get(key, default)
        return self.button_remaps.get(cat, {}).get(key, default)

    def set_scoped_category_setting(self, key, value, scope=None):
        scope = self._resolve_in_app_gyro_scope(scope)
        cat = self.get_current_category()
        if cat not in self.button_remaps:
            self.button_remaps[cat] = {}
        if scope:
            scoped = self.ensure_mapping_scope(cat, scope)
            scoped[key] = value
        else:
            self.button_remaps[cat][key] = value
        self._bump_settings_generation()

    def __getattr__(self, name):
        if name.endswith("_mapping"):
            return self.get_mapping_setting(name[:-8], "Default")
        if name.endswith("_custom"):
            return self.get_joystick_custom(name[:-7])
        raise AttributeError(name)
        
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
