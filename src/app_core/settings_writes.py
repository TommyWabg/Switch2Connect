"""Settings writes moved from the classic interface."""
import logging

logger = logging.getLogger(__name__)

def update_auto_disconnect_mode(config, val):
    config.auto_disconnect_mode = val
    config.save_config()

def update_stabilized_gyro_setting(config, val):
    # Pass-through only: ON selects the quality-gated V2 9-axis estimator;
    # OFF selects the independent pure V2 6-axis estimator.
    config.gyro_passthrough_9axis_enabled = val
    config._bump_settings_generation()
    config.save_config()
    logger.info(f"Pass-through V2 9-axis Assist: {val}")

def update_steam_roll_comp_setting(config, val):
    # Drives V2 Horizon while V2 is active, Legacy Horizon Lock otherwise.
    config.steam_roll_compensation = val
    config._bump_settings_generation()
    config.save_config()
    logger.info(f"Horizon Lock: {val}")

def update_virtual_gyro_soft_deadzone_setting(config, val):
    val = float(val)
    config.virtual_gyro_soft_deadzone = val
    config.save_config()
    logger.info(f"Gyro Pass-Through Deadzone: {val}")

def update_in_app_gyro_soft_deadzone_setting(config, val):
    val = float(val)
    config.in_app_gyro_soft_deadzone = val
    config.save_config()
    logger.info(f"In-app Gyro Deadzone: {val}")

def update_cemuhook_sensitivity(config, val):
    val = int(float(val))
    config.cemuhook_sensitivity = val
    config.save_config()
    logger.info(f"Cemuhook Sensitivity updated to {val}")

def update_djg_activation_setting(config, val):
    config.djg_activation = val
    config.save_config()
    logger.info(f"DJG Activation: {val}")

def update_vibration_strength(config, val):
    try:
        config.vibration_strength = int(float(val))
        config.save_config()
    except Exception as e:
        logger.error(f"Failed to save vibration strength setting: {e}")

def update_vibration_frequency(config, val):
    try:
        config.vibration_frequency = int(float(val))
        config.save_config()
    except Exception as e:
        logger.error(f"Failed to save vibration frequency setting: {e}")

def update_startup_setting(config, val, set_startup):
    config.open_when_startup = val
    set_startup(val)
    config.save_config()

def update_minimized_setting(config, val):
    config.start_minimized = val
    config.save_config()

def update_rumble_mode_setting(config, val):
    config.rumble_mode = val
    config.save_config()

def update_mode_shift_setting(config, val):
    # Stored per (profile, Gyro Control mode). Controls only whether In-app Gyro
    # auto-applies the Mode Shift Mapping; the mapping tab stays visible regardless.
    config.mode_shift_enabled = bool(val)
    # Turning Mode Shift On re-engages the In-app Gyro activation-button sync between
    # Controller Mapping and the Mode Shift Mapping store (Off leaves them independent).
    if val:
        config.sync_active_in_app_gyro_activation()
    config.save_config()

def update_passthrough_mode(config, mode, cemuhook_server, refresh):
    config.gyro_passthrough_mode = mode
    config.save_config()
    if mode == "Cemuhook":
        cemuhook_server.start()
    else:
        cemuhook_server.stop()
    refresh(mode)
    logger.info(f"Gyro Passthrough Mode updated to {mode}")

def set_auto_disconnect_time(config, days_str, hours_str, minutes_str):
    try:

        days = int(days_str) if days_str else 0
        hours = int(hours_str) if hours_str else 0
        minutes = int(minutes_str) if minutes_str else 0

        config.auto_disconnect_days = days
        config.auto_disconnect_hours = hours
        config.auto_disconnect_minutes = minutes
        config.save_config()
        return True
    except Exception as e:
        logger.error(f"Failed to save auto disconnect settings: {e}")
        return False

def set_rumble_delay(config, val_str):
    val_str = str(val_str).strip()
    if not val_str:
        val = 0
    else:
        try:
            val = int(val_str)
        except ValueError:
            val = 0
    config.rumble_delay_ms = val
    config.save_config()


def update_djg_enabled_setting(config, val, controllers, refresh):
    config.djg_enabled = val
    config.save_config()
    logger.info(f"DJG Enabled: {val}")
    if not val:
        for vc in controllers:
            if vc:
                side = getattr(config, "djg_dominant_side", "Right")
                vc.active_gyro_side = side if side in ("Left", "Right") else "Right"
    refresh("players")


def update_djg_mode_setting(config, val, controllers, refresh):
    legacy_direct_merge = val == "Direct Merge"
    if legacy_direct_merge:
        # The config setter performs the atomic legacy migration so None is
        # not rejected against the previously selected non-Single mode.
        config.djg_mode = "Direct Merge"
        val = config.djg_mode
        refresh("dominant_var", config.djg_dominant_side)
    elif val != "Single Side Toggle" and getattr(config, "djg_dominant_side", "Right") == "None":
        config.djg_dominant_side = "Right"
        refresh("dominant_var", "Right")
        refresh("dominant_switch", "Right")
    if not legacy_direct_merge:
        config.djg_mode = val
    config.save_config()
    logger.info(f"DJG Mode: {val}")
    refresh("mode_var", val)
    refresh("mode_ui")
    refresh("players")


def update_djg_dominant_setting(config, val, controllers, refresh):
    if val not in ("Left", "Right", "None"):
        val = "Right"
    if val == "None" and getattr(config, "djg_mode", "Single Side Toggle") != "Single Side Toggle":
        val = "Right"
    refresh("dominant_var", val)
    if val in ("Left", "Right"):
        refresh("dominant_switch", val)
    config.djg_dominant_side = val
    config.save_config()
    logger.info(f"DJG Dominant Side: {val}")
    if not getattr(config, "djg_enabled", False):
        for vc in controllers:
            if vc:
                vc.active_gyro_side = val if val in ("Left", "Right") else "Right"
    else:
        mode = getattr(config, "djg_mode", "Single Side Toggle")
        if mode == "Switch Dominant Side":
            for vc in controllers:
                if vc:
                    vc.djg_left_active = True
                    vc.djg_right_active = True
        elif mode == "Switch Gyro Side":
            for vc in controllers:
                if vc:
                    vc.active_gyro_side = val
    refresh("players")


def save_gyro_settings(config, sensitivity, stick_sensitivity, updating=False):
    if not updating:
        if getattr(config, "gyro_control_mode", "Mouse") == "R Joystick":
            config.r_joystick_gyro_sensitivity = float(sensitivity)
        else:
            config.gyro_sensitivity = float(sensitivity)
    config.stick_mouse_sensitivity = float(stick_sensitivity)
    config.set_joystick_setting_scoped("l_joystick", "mouse_sensitivity", config.stick_mouse_sensitivity, "in_app_gyro_mode_mappings")
    config.set_joystick_setting_scoped("r_joystick", "mouse_sensitivity", config.stick_mouse_sensitivity, "in_app_gyro_mode_mappings")
    config.save_config()


def apply_power_saving_mode(config, current, target, controllers, module, apply_priority):
    previous_mode = current
    config.power_saving_mode = target
    config.save_config()
    module("power_saving").notify_mode_changed()
    apply_priority(target)
    module("discoverer").request_wired_rescan("power_saving_mode_changed", manual=False)
    for vc in controllers or ():
        wake_update = getattr(vc, "_update_wake", None)
        if wake_update is not None:
            wake_update.set()
        for controller in getattr(vc, "controllers", ()) or ():
            controller._poke_rumble_scheduler()
            apply_mode = getattr(controller, "on_power_saving_mode_changed", None)
            if callable(apply_mode):
                apply_mode(previous_mode, target)


def on_use_default_controller_mapping_for_gyro_mode(config):
    config.copy_controller_mapping_to_in_app_gyro_mode_mapping()
    config.save_config()


def on_reset_in_app_gyro_mode_mapping(config):
    config.reset_in_app_gyro_mode_mapping()
    mapping_keys = [
        "home", "capt", "c", "plus", "minus", "a", "b", "x", "y",
        "up", "down", "left", "right", "zl", "l", "zr", "r",
        "l_stk", "r_stk", "gl", "gr", "sll", "srl", "slr", "srr",
        "gc_l_click", "gc_r_click"
    ]
    for key in mapping_keys:
        config.set_mapping_setting_scoped(f"{key}_in_app_gyro_simul", "None", None)
    config.save_config()


def update_audio_haptics(config, val, controllers):
    config.audio_haptics_enabled = val
    config.save_config()
    if controllers is not None:
        for vc in controllers:
            if vc is not None and getattr(vc, 'mode', '') == "PS5" and getattr(config, "driver_type", "WinUHid") == "USBIP":
                try:
                    vc._setup_vg_controller()
                except Exception:
                    pass


def update_adaptive_triggers(config, val):
    config.adaptive_triggers_enabled = val
    config.save_config()


def clear_active_xbox_impulses(controllers):
    for vc in controllers:
        if (vc is not None and getattr(vc, 'mode', '') == "Xbox One"
                and getattr(vc, 'driver_type', '') == "WinUHid"):
            try:
                vc.clear_xbox_impulse_triggers()
            except Exception:
                pass


def update_impulse_enabled(config, value, controllers):
    config.impulse_trigger_enabled = value
    config.save_config()
    if not value:
        clear_active_xbox_impulses(controllers)


def update_dynamic_frequency(config, value, refresh_frequency_visibility):
    config.impulse_trigger_dynamic_frequency = value
    config.save_config()
    refresh_frequency_visibility()


def update_impulse_strength(config, value):
    config.impulse_trigger_strength = int(float(value))
    config.save_config()


def update_fixed_frequency(config, value):
    config.impulse_trigger_frequency = int(float(value))
    config.save_config()


def update_gyro_control_mode(config, val, save_sensitivity, axis_mode, refresh):
    save_sensitivity()
    config.gyro_control_mode = val
    if val == "Steering":
        config.gyro_mode = "Roll"
    elif getattr(config, "gyro_mode", "World") == "Roll":
        config.gyro_mode = axis_mode
    refresh("control", val)
    # The active In-app Gyro store switches with the mode; re-sync its In-app Gyro
    # activation buttons with Controller Mapping, then refresh the mapping tab so it
    # shows the mappings (and synced In-app Gyro buttons) for the selected mode.
    config.sync_active_in_app_gyro_activation()
    config.save_config()
    refresh("mapping", val)
    # The active In-app Gyro scope changed with the mode; refresh the IR buttons
    # so their labels reflect the reconciled function for the selected mode.
    refresh("ir", val)


def clear_in_app_gyro_settings(config, key):
    config.set_mapping_setting_scoped(f"{key}_in_app_gyro_simul", "None", None)
    config.set_mapping_setting_scoped(f"{key}_in_app_gyro_dampening_mode", "Off", None)
    config.set_mapping_setting_scoped(f"{key}_in_app_gyro_dampening_amount", 90, None)
    config.set_mapping_setting_scoped(f"{key}_in_app_gyro_dampening_effect_after_released_ms", 200, None)
    config.set_mapping_setting_scoped(f"{key}_in_app_gyro_deadzone_mode", [], None)
    config.set_mapping_setting_scoped(f"{key}_in_app_gyro_deadzone_amount", 15.0, None)
    config.set_mapping_setting_scoped(f"{key}_in_app_gyro_deadzone_pause_after_pressed_ms", 100, None)
    config.set_mapping_setting_scoped(f"{key}_in_app_gyro_deadzone_pause_after_released_ms", 100, None)
    config.set_mapping_setting_scoped(f"{key}_in_app_gyro_deadzone_effect_after_released_ms", 200, None)


def sync_joystick_direction(config, key, value, mapping_scope):
    base_key, sep, direction = key.rpartition("_")
    if sep and base_key in ("l_joystick", "r_joystick") and direction in ("up", "down", "left", "right", "click"):
        current = config.get_joystick_custom_scoped(base_key, mapping_scope)
        current[direction] = value
        config.set_joystick_custom_scoped(base_key, current, mapping_scope)


def set_joystick_setting(config, key, setting, value, mapping_scope=None, sync_mouse=None):
    config.set_joystick_setting_scoped(key, setting, float(value) if setting == "mouse_sensitivity" else value, mapping_scope)
    if setting == "mouse_sensitivity" and mapping_scope == "in_app_gyro_mode_mappings" and sync_mouse is not None:
        sync_mouse(float(value))
    config.save_config()


def set_deadzone_percent(config, family, side, value, profile_name=None, category=None):
    config.set_joystick_deadzone_percent(family, side, value, profile_name, category)


def set_deadzone_linked(config, family, linked, profile_name=None, category=None):
    return config.set_joystick_deadzone_linked(family, linked, profile_name, category)


def set_gc_trigger_mode(config, val, mapping_scope=None):
    if mapping_scope is None:
        config.gc_trigger_mode = val
    else:
        config.set_scoped_category_setting("gc_trigger_mode", val, mapping_scope)
    config.save_config()


def set_ir_function(config, side, value, profile_name, category, mapping_scope,
                    canonical, is_in_app_gyro_function, close_gyro, refresh, refresh_function, save=True):
    old_value = config.get_joycon_ir_sensor_settings_scoped(side, profile_name, category, mapping_scope).get("function", "Default")
    value = canonical(value)
    leaving_in_app_gyro = is_in_app_gyro_function(old_value) and not is_in_app_gyro_function(value)
    if leaving_in_app_gyro:
        close_gyro()
    config.set_joycon_ir_sensor_setting_scoped(side, "function", value, profile_name, category, mapping_scope)
    if leaving_in_app_gyro:
        config.reset_joycon_ir_in_app_gyro_settings_scoped(side, profile_name, category, mapping_scope)
    if save:
        config.save_config()
    refresh()
    refresh_function(value)


def set_ir_threshold(config, side, value, profile_name=None, category=None, mapping_scope=None):
    config.set_joycon_ir_sensor_setting_scoped(side, "activate_threshold", int(float(value)), profile_name, category, mapping_scope)
    config.save_config()


def set_ir_mouse(config, side, setting_key, tokens, profile_name=None, category=None, mapping_scope=None):
    config.set_joycon_ir_mouse_setting_scoped(side, setting_key, tokens, profile_name, category, mapping_scope)
    config.save_config()

def normalize_rumble_mode(config):
    driver_type = getattr(config, "driver_type", "WinUHid")
    sim_mode = getattr(config, "simulation_mode", "PS5")
    current_rumble = getattr(config, "rumble_mode", "Xbox")
    is_usbip_ps5 = driver_type == "USBIP" and sim_mode == "PS5"
    allowed_values = ["Xbox", "PS5"] if is_usbip_ps5 else ["Xbox", "Switch"]

    # Rumble values are persisted per Emu Mode category.  "PS5" is only a
    # valid alias for USBIP PS5 audio/HD rumble; converting it globally used
    # to corrupt the Xbox category when returning to Xbox One.  Recover old
    # corrupted Xbox/WinUHid values by mapping PS5 back to Switch instead.
    normalized_rumble = current_rumble
    if is_usbip_ps5 and current_rumble == "Switch":
        normalized_rumble = "PS5"
    elif not is_usbip_ps5 and current_rumble == "PS5":
        normalized_rumble = "Switch"
    elif current_rumble not in allowed_values:
        logger.warning(
            "Unsupported rumble mode %r for driver=%s emu_mode=%s; using %s",
            current_rumble, driver_type, sim_mode, allowed_values[0],
        )
        normalized_rumble = allowed_values[0]

    if normalized_rumble != current_rumble:
        logger.info(
            "Normalized rumble mode %s -> %s for driver=%s emu_mode=%s",
            current_rumble, normalized_rumble, driver_type, sim_mode,
        )
        config.rumble_mode = normalized_rumble
        config.save_config()
    current_rumble = normalized_rumble

    return driver_type, sim_mode, current_rumble, is_usbip_ps5

def write_mapping(config, key, value, scope, live_save=lambda key, value: None):
    config.set_mapping_setting_scoped(key, value, scope)
    live_save(key, value)


def parse_activation_number(raw, min_value=0.0, max_value=None, integer=False):
    raw = (raw or "").strip() if isinstance(raw, str) else raw
    if raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    value = max(float(min_value), value)
    if max_value is not None:
        value = min(float(max_value), value)
    return int(round(value)) if integer else float(value)


def normalize_activation_inputs(selected, options):
    return [token for token in options if token in selected]


def mapping_mode_value(current_val, new_mode):
    from .helpers import parse_mouse_click_mapping
    mouse_click_mapping = parse_mouse_click_mapping(current_val)
    if mouse_click_mapping:
        option_token, _mode = mouse_click_mapping
        from config import MOUSE_CLICK_BACK_BUTTON_TOKENS
        return f"Custom[{new_mode}]:{MOUSE_CLICK_BACK_BUTTON_TOKENS[option_token]}"
    elif isinstance(current_val, str) and current_val.startswith("Custom"):
        if current_val.startswith("Custom[Tap]:") or current_val.startswith("Custom[Hold]:"):
            return f"Custom[{new_mode}]:{current_val.split(':', 1)[1]}"
        else:
            return f"Custom[{new_mode}]:{current_val[7:]}"
    return None

def clear_mapping_selection(config, key, selected, in_app_gyro_label):
    if selected != in_app_gyro_label:
        clear_in_app_gyro_settings(config, key)


def canonical_mapping(value, catalogue, mode="Hold"):
    tokens = {getattr(catalogue, "GYRO_LOCK_LABEL", "Gyro Lock"): catalogue.GYRO_LOCK_TOKEN,
              getattr(catalogue, "MODE_SHIFT_LABEL", "Mode Shift"): catalogue.MODE_SHIFT_TOKEN,
              getattr(catalogue, "IN_APP_GYRO_LABEL", "In-app Gyro"): catalogue.IN_APP_GYRO_TOKEN,
              "Gyro": catalogue.IN_APP_GYRO_TOKEN,
              **catalogue.MOUSE_CLICK_BACK_BUTTON_TOKENS}
    return f"Custom[{mode}]:{tokens[value]}" if isinstance(value, str) and value in tokens else value


def reset_mapping(config, key, scope, live_save=lambda key, value: None, reset_value=None):
    if reset_value is None:
        reset_value = "None" if key.endswith("_in_app_gyro_simul") else "Default"
    write_mapping(config, key, reset_value, scope, live_save)
    sync_joystick_direction(config, key, reset_value, scope)
    if reset_value == "Default":
        clear_in_app_gyro_settings(config, key)


def recorded_mapping_value(tokens, mode, prefix=None):
    from .profile_store import normalize_recorded_inputs
    final_seq = normalize_recorded_inputs(tokens)
    if not final_seq:
        return "Default"
    val_content = "+".join(final_seq)
    if prefix:
        return f"Custom[{mode}]:{prefix}+{val_content}"
    else:
        return f"Custom[{mode}]:{val_content}"

POWER_MESSAGES = {
    "Auto": (
        "Auto mode reduces idle CPU power use by requesting precision timing only "
        "while it is needed.\n\n"
        "Buttons, sticks, triggers and motion controls remain fully enabled.\n\n"
        "HD Rumble, Audio Haptics and Impulse Trigger feedback may be less consistent, "
        "delayed, interrupted or occasionally lost."
    ),
    "Full": (
        "Full mode prioritizes the lowest CPU power use.\n\n"
        "Buttons, sticks, triggers and non-motion mappings remain enabled.\n\n"
        "Vibration output and motion-based and mouse features are disabled, including Gyro Pass-Through, In-app Gyro, "
        "DJG, IR Mouse, and Joystick Mouse.\n\n"
        "Periodic ESP32 and Wired/HidHide scanner will be disabled.\n\n"
        "Automatic controller scanning pauses after 1 Pro/NSO GCN controller "
        "or 2 Joy-Con controllers are connected. Existing controller connections "
        "will not be disconnected."
    ),
}

def sync_active_mapping(config, save=False):
    try:
        config.sync_active_in_app_gyro_activation()
        if save:
            config.save_config()
    except Exception:
        logger.exception("Failed to sync active Mode Shift mapping state")


def apply_mapping_selection(config, key, selected, scope, catalogue, apply, mode=None):
    if key not in ("l_joystick", "r_joystick"):
        clear_mapping_selection(config, key, selected, getattr(catalogue, "IN_APP_GYRO_LABEL", "In-app Gyro"))
    if mode is None:
        current_val = config.get_mapping_setting_scoped(key, "Default", scope)
        mode = "Tap" if isinstance(current_val, str) and current_val.startswith("Custom[Tap]:") else "Hold"
    if mode not in ("Hold", "Tap"):
        mode = "Hold"
    value = canonical_mapping(selected, catalogue, mode)
    apply(selected, value)


def commit_recorded_mapping(config, key, tokens, mode, scope, live_save=lambda key, value: None, prefix=None):
    value = recorded_mapping_value(tokens, mode, prefix)
    write_mapping(config, key, value, scope, live_save)
    sync_joystick_direction(config, key, value, scope)
    return value

def set_change_profile_mode(config, val):
    config.change_profile_mode = val
    config.save_config()


def set_ui_mode(config, mode):
    """Persist the interface choice synchronously: both callers exit right after."""
    from .profile_io import write_config_yaml
    config.ui_mode = mode
    write_config_yaml(config, {"ui_mode": mode})
