"""Classic profile store operations shared by both interfaces."""
import ctypes
from ctypes import wintypes
import logging
import os
from .helpers import get_exe_display_name, normalize_app_path

def get_profile_assigned_apps(config, profile_name):
    profile_data = config.profiles.get(profile_name, {})
    assigned_apps = profile_data.get("assigned_apps")
    if isinstance(assigned_apps, list):
        apps = []
        for app in assigned_apps:
            if isinstance(app, str):
                app = {"path": app, "name": get_exe_display_name(app)}
            if isinstance(app, dict) and app.get("path"):
                apps.append({
                    "path": app.get("path", ""),
                    "name": app.get("name") or get_exe_display_name(app.get("path")),
                })
        return apps

    assigned_app = profile_data.get("assigned_app", {})
    if isinstance(assigned_app, str) and assigned_app:
        return [{"path": assigned_app, "name": get_exe_display_name(assigned_app)}]
    if isinstance(assigned_app, dict) and assigned_app.get("path"):
        return [{
            "path": assigned_app.get("path", ""),
            "name": assigned_app.get("name") or get_exe_display_name(assigned_app.get("path")),
        }]
    return []

def set_profile_assigned_apps(config, profile_name, apps):
    if profile_name not in config.profiles:
        return True
    normalized_seen = set()
    normalized_apps = []
    for app in apps:
        app_path = app.get("path") if isinstance(app, dict) else str(app)
        normalized_path = normalize_app_path(app_path)
        if not normalized_path or normalized_path in normalized_seen:
            continue
        normalized_seen.add(normalized_path)
        normalized_apps.append({
            "path": os.path.normpath(app_path),
            "name": (app.get("name") if isinstance(app, dict) else None) or get_exe_display_name(app_path),
        })
    config.profiles[profile_name]["assigned_apps"] = normalized_apps
    config.profiles[profile_name].pop("assigned_app", None)

def save_active_profile_runtime_settings(config):
    if hasattr(config, 'active_profile') and config.active_profile in config.profiles:
        config.profiles[config.active_profile]["driver_type"] = getattr(config, "driver_type", "WinUHid")
        config.profiles[config.active_profile]["simulation_mode"] = getattr(config, "simulation_mode", "PS5")

def set_profile_switching_combo(config, profile_name, value):
    if profile_name in config.profiles:
        config.profiles[profile_name]["profile_switching_combo"] = value
        config.save_config()

def set_profile_switching_combo_trigger(config, value):
    config.profile_switching_combo_trigger = value
    config.save_config()

def get_sorted_profiles(config, sort_key):
    return sorted(
        list(config.profiles.keys()),
        key=lambda name: (0 if config.profiles.get(name, {}).get("change_profile_list", False) else 1, sort_key(name))
    )

def clear_app_from_other_profiles(config, app_path, current_profile):
    normalized_path = normalize_app_path(app_path)
    if not normalized_path:
        return
    for profile_name in list(config.profiles.keys()):
        if profile_name == current_profile:
            continue
        apps = get_profile_assigned_apps(config, profile_name)
        filtered_apps = [
            app for app in apps
            if normalize_app_path(app.get("path")) != normalized_path
        ]
        if len(filtered_apps) != len(apps):
            set_profile_assigned_apps(config, profile_name, filtered_apps)

def get_profile_for_app_path(config, app_path, sort_key):
    normalized_path = normalize_app_path(app_path)
    if not normalized_path:
        return None

    for profile_name in get_sorted_profiles(config, sort_key):
        for assigned_app in get_profile_assigned_apps(config, profile_name):
            if normalize_app_path(assigned_app.get("path")) == normalized_path:
                return profile_name
    return None

def set_in_change_list(config, profile_name, enabled, refresh_callback=None):
    if profile_name in config.profiles:
        config.profiles[profile_name]["change_profile_list"] = bool(enabled)
        config.save_config()
        if refresh_callback:
            refresh_callback()


def prepare_profile_switch(config):
    new_profile_name = getattr(config, 'active_profile', "")
    if not new_profile_name or new_profile_name not in config.profiles:
        return

    new_driver = config.profiles[new_profile_name].get("driver_type")
    if not new_driver:
        new_driver = getattr(config, "driver_type", "WinUHid")
        config.profiles[new_profile_name]["driver_type"] = new_driver
    # Only a completed startup check may activate the runtime-only fallback.
    # Do not turn an early cached capability miss into a profile change.
    temporary_driver_fallback = bool(
        new_driver == "WinUHid"
        and getattr(config, "driver_fallback_active", False))
    if temporary_driver_fallback:
        new_driver = "ViGEmBus"

    new_emu = config.profiles[new_profile_name].get("simulation_mode")
    if not new_emu:
        new_emu = getattr(config, "simulation_mode", "PS5")
        config.profiles[new_profile_name]["simulation_mode"] = new_emu

    config.save_config()

    driver_changed = getattr(config, "driver_type", "") != new_driver
    emu_changed = getattr(config, "simulation_mode", "") != new_emu

    # Pre-set the target driver's default to avoid double recreation in update_driver_type_setting
    if new_driver == "ViGEmBus":
        config.vigembus_sim_mode = new_emu
    elif new_driver == "USBIP":
        config.usbip_sim_mode = new_emu
    else:
        config.winuhid_sim_mode = new_emu

    return new_driver, new_emu, temporary_driver_fallback, driver_changed, emu_changed


def add_profile(config):
    i = 1
    while f"Profile {i}" in config.profiles:
        i += 1
    new_name = f"Profile {i}"

    if hasattr(config, 'active_profile') and config.active_profile in config.profiles:
        config.profiles[config.active_profile]["driver_type"] = getattr(config, "driver_type", "WinUHid")
        config.profiles[config.active_profile]["simulation_mode"] = getattr(config, "simulation_mode", "PS5")

    if config.add_profile(new_name):
        set_profile_assigned_apps(config, config.active_profile, [])
        config.profiles[config.active_profile]["change_profile_list"] = True
        config.profiles[config.active_profile]["profile_switching_combo"] = ""
        config.save_config()
        return True
    return False


def reset_profile(config, current_name):
    keep_change_profile_list = bool(config.profiles.get(current_name, {}).get("change_profile_list", False))
    if config.reset_profile_to_default(current_name):
        if current_name in config.profiles:
            config.profiles[current_name]["change_profile_list"] = keep_change_profile_list
            config.save_config()
        return True
    return False


logger = logging.getLogger(__name__)

def initialize_foreground_api():
    ctypes.windll.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    ctypes.windll.kernel32.OpenProcess.restype = wintypes.HANDLE

    ctypes.windll.kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    ctypes.windll.kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    ctypes.windll.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    ctypes.windll.kernel32.CloseHandle.restype = wintypes.BOOL
    ctypes.windll.user32.GetForegroundWindow.argtypes = []
    ctypes.windll.user32.GetForegroundWindow.restype = wintypes.HWND


def get_foreground_app_path(normalize_path=normalize_app_path):
    try:
        import win32process
        initialize_foreground_api()
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return ""

        _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid:
            return ""

        process_handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process_handle:
            return ""

        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(process_handle, 0, buffer, ctypes.byref(size)):
                return normalize_path(buffer.value)
        finally:
            ctypes.windll.kernel32.CloseHandle(process_handle)
    except Exception as e:
        logger.debug(f"Failed to read foreground app path: {e}")
    return ""


def assign_app(config, app_path, index, current_profile, display_name, refresh):
    app_name = display_name(app_path)
    apps = get_profile_assigned_apps(config, current_profile)
    new_app = {
        "path": os.path.normpath(app_path),
        "name": app_name,
    }
    if index < len(apps):
        apps[index] = new_app
    else:
        apps.append(new_app)
    clear_app_from_other_profiles(config, app_path, current_profile)
    set_profile_assigned_apps(config, current_profile, apps)
    config.save_config()
    refresh()


def remove_assigned_app(config, index, current_profile, refresh):
    if not current_profile or current_profile not in config.profiles:
        return
    apps = get_profile_assigned_apps(config, current_profile)
    if 0 <= index < len(apps):
        apps.pop(index)
    set_profile_assigned_apps(config, current_profile, apps)
    config.save_config()
    refresh()


def poll_assigned_app_focus(config, suspended, switching, foreground, lookup, switch, set_last):
    try:
        if not suspended and not switching:
            foreground_app_path = foreground()
            target_profile = lookup(foreground_app_path)
            if target_profile and target_profile != getattr(config, "active_profile", ""):
                switch(target_profile)
            set_last(foreground_app_path)
    except Exception as e:
        logger.debug(f"Assigned app focus poll failed: {e}")


def rename_profile(config, new_name, ui, refresh):
    current_name = config.active_profile
    if new_name and new_name != current_name:
        if new_name in config.profiles:
            ui.show_message("Error", f"Profile '{new_name}' already exists.", kind="error")
            return False, "duplicate_name"
        else:
            if config.rename_profile(new_name):
                refresh()
                return True, None
            return False, None
    return False, "unchanged"

def apply_profile_switch(config, ui, switch_driver, switch_mode, initialize_keyboard, wake_mouse, cancel_calibration):
    ui("close_joystick_custom_popup")
    prepared = prepare_profile_switch(config)
    if prepared is None:
        return
    new_driver, new_emu, temporary_driver_fallback, driver_changed, emu_changed = prepared

    outcome = None

    if driver_changed:
        if ui("has_driver_switch"):
            ui("driver", new_driver)
        outcome = yield switch_driver(
            new_driver,
            persist_preference=not temporary_driver_fallback)

    if not driver_changed and emu_changed:
        if ui("has_sim_mode_switch"):
            ui("mode", new_emu)
        outcome = yield switch_mode(new_emu)
    elif ui("has_sim_mode_switch"):
        ui("mode", new_emu)

    # Reconcile the Profile-scoped keyboard backend immediately. Controller
    # workers observe settings_generation and reconcile their mouse devices.
    initialize_keyboard()
    wake_mouse()
    ui("refresh_profile_switching_combo_trigger_ui")
    ui("refresh_driver_manager")

    ui("refresh_ui_for_profile")
    cancel_calibration()
    return outcome

def switch_to_profile(s, config, profile_name, return_outcome=False):
    if not profile_name or profile_name == getattr(config, "active_profile", ""):
        return False
    if profile_name not in config.profiles:
        return False

    if hasattr(s, 'profile_apply_timer') and s.profile_apply_timer:
        s.cancel(s.profile_apply_timer)
        s.profile_apply_timer = None
    s.pending_profile = None
    s.save_active_profile_runtime_settings()

    if config.switch_profile(profile_name):
        s._set_profile_button_text()
        s.app_profile_switching = True
        try:
            outcome = yield s.apply_profile_switch()
        finally:
            s.app_profile_switching = False
        s.close_joystick_custom_popup()
        s.close_in_app_gyro_popup()
        s.refresh_joycon_ir_sensor_buttons()
        s.close_profile_popup_if_open()
        return outcome if return_outcome else True
    return False

def reset_selected_profile(config, ui, apply, refresh):
    current_name = config.active_profile
    if ui.ask_yes_no("Reset Profile", f"Are you sure you want to reset profile '{current_name}'?"):
        if reset_profile(config, current_name):
            yield apply()
            refresh()
            return True, None
        return False, "reset_failed"
    return False, "cancelled"


def delete_selected_profile(config, ui, apply, set_button, refresh):
    if len(config.profiles) <= 1:
        ui.show_message("Delete Profile", "Cannot delete the last profile.", kind="warning")
        return False, "last_profile"
    current_name = config.active_profile
    if ui.ask_yes_no("Delete Profile", f"Are you sure you want to delete profile '{current_name}'?"):
        if config.delete_profile():
            set_button()
            yield apply()
            refresh()
            return True, None
        return False, "delete_failed"
    return False, "cancelled"

def normalize_recorded_inputs(seq):
    final_seq = []
    for k in seq:
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
        if nk not in final_seq:
            final_seq.append(nk)
    return final_seq

def capture_combo_states(s, states, recorded_seq, pressed_keys, end_recording):
    if not getattr(s, 'recording_controllers', False):
        return
    any_pressed = any(bool(v) for v in states.values())
    if getattr(s, 'waiting_for_controller_release', False):
        if not any_pressed:
            s.waiting_for_controller_release = False
        return
    if any_pressed:
        for btn_name, pressed in states.items():
            if pressed:
                token = f"BTN_{btn_name}"
                s.recorded_controller_buttons.add(token)
                if token not in recorded_seq:
                    recorded_seq.append(token)
    s.controller_buttons_pressed = any_pressed
    if not any_pressed and s.recorded_controller_buttons and not pressed_keys:
        end_recording()

def poll_combo(s, button_map, recorded_seq, pressed_keys, end_recording, schedule):
    if not getattr(s, 'recording_controllers', False):
        return
    any_pressed = False
    reverse_map = {v: k for k, v in button_map.items() if k not in ["Capture", "PS_C_Click"]}
    for vc in getattr(s, 'current_controllers', []):
        if vc is None:
            continue
        for c in vc.controllers:
            raw = getattr(c, 'raw_buttons', 0)
            if raw:
                any_pressed = True
                if not getattr(s, 'waiting_for_controller_release', False) and not s.recorded_controller_buttons:
                    for bit, btn_name in reverse_map.items():
                        if raw & bit:
                            token = f"BTN_{btn_name}"
                            s.recorded_controller_buttons.add(token)
                            if token not in recorded_seq:
                                recorded_seq.append(token)
    if getattr(s, 'waiting_for_controller_release', False):
        if not any_pressed:
            s.waiting_for_controller_release = False
    else:
        s.controller_buttons_pressed = any_pressed
        if not any_pressed and s.recorded_controller_buttons and not pressed_keys:
            end_recording()
            return
    schedule(50)

def combo_conflicts(config, unique_profile, value):
    if unique_profile and value:
        for profile_name, profile_data in config.profiles.items():
            if profile_name != unique_profile and profile_data.get("profile_switching_combo", "") == value:
                return True
    return False

def begin_combo_capture(s):
    s.recording_controllers = True
    s.recorded_controller_buttons = set()
    s.controller_buttons_pressed = False
    s.waiting_for_controller_release = True


def end_combo_capture(s, utils, callback):
    s.recording_controllers = False
    if getattr(utils, "profile_combo_record_callback", None) is callback:
        utils.profile_combo_record_callback = None

def profile_sort_key(s):
    import re
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
