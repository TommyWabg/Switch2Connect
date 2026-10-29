"""Settings views and executor adapters for the shared classic write paths."""
from .. import settings_writes
from copy import deepcopy
import importlib
import logging

from ..events import SETTINGS_CHANGED, SettingsChanged
from ..executor import on_core
from ..models import (AppSettingsView, AutoDisconnectView, Control,
    DeadzoneRow, DeadzoneView, GcTriggerView, GyroView, IrSensorView,
    JoystickSettingsView, MappingRow, MappingView, Option, OptionCategory, InAppGyroView,
    RumbleView, SettingResult, SettingsState, freeze)

logger = logging.getLogger(__name__)
SHIFT_SCOPE = "in_app_gyro_mode_mappings"
SCOPES = (None, SHIFT_SCOPE)
INPUTS = (
    ("home", "Home:", "system"), ("capt", "Capture:", "system"),
    ("c", "Chat:", "system"), ("plus", "Plus:", "system"),
    ("minus", "Minus:", "system"),
    *((k, k.upper() + ":", "face") for k in ("a", "b", "x", "y")),
    *((k, k.title() + ":", "dpad") for k in ("up", "down", "left", "right")),
    *((k, k.upper() + ":", "shoulders") for k in ("zl", "l", "zr", "r")),
    ("l_stk", "L Joystick Click:", "sticks"), ("r_stk", "R Joystick Click:", "sticks"),
    ("gl", "GL:", "back"), ("gr", "GR:", "back"),
    ("sll", "Left SL:", "rail"), ("srl", "Left SR:", "rail"),
    ("slr", "Right SL:", "rail"), ("srr", "Right SR:", "rail"),
    ("gc_l_click", "L Click:", "gamecube"), ("gc_r_click", "R Click:", "gamecube"),
    ("l_joystick", "L Joystick:", "sticks"), ("r_joystick", "R Joystick:", "sticks"),
)
IN_APP_DEFAULTS = {
    "simul": "None", "deadzone_mode": [], "deadzone_amount": 15.0,
    "deadzone_pause_after_pressed_ms": 100, "deadzone_pause_after_released_ms": 100,
    "deadzone_effect_after_released_ms": 200, "dampening_mode": [],
    "dampening_amount": 90, "dampening_effect_after_released_ms": 200,
}
POWER_MESSAGES = settings_writes.POWER_MESSAGES


def custom_parts(value):
    if not isinstance(value, str) or not value.startswith("Custom"):
        return None, None
    mode = "Tap" if value.startswith("Custom[Tap]:") else "Hold"
    return mode, value.split(":", 1)[1] if ":" in value else ""


class SettingsService:
    def __init__(self, bus, executor, ui_port, runtime, *, config=None,
                 catalogue=None, modules=importlib.import_module):
        self._bus, self._executor, self._ui_port, self._runtime = bus, executor, ui_port, runtime
        self._config = config if config is not None else runtime.config
        self._catalogue = catalogue if catalogue is not None else modules("config")
        self._modules = modules
        self._batch_depth = 0
        self._deadzone_dirty = False
        self._last_axis_mode = "World"
        self._state = None
        self._unsubscribe = bus.subscribe(SETTINGS_CHANGED, self._settings_changed)
        # CONFIG getters can normalize stores, so initialization also runs on core.
        if executor.is_core_thread():
            self._rebuild()
        else:
            executor.submit(self._rebuild).result()

    def state(self):
        return self._state


    def mapping(self, scope=None):
        return next(v for v in self._state.mappings if v.scope == scope)

    def joystick(self, key, scope=None):
        return next(v for v in self._state.joysticks if v.key == key and v.scope == scope)

    def ir_sensor(self, side, scope=None):
        return next(v for v in self._state.ir_sensors if v.side == side and v.scope == scope)

    @on_core
    def ir_sensor_context(self, side, scope=None, *, profile_name=None, category=None):
        """Read a frozen captured origin; CONFIG normalization stays on core."""
        return self._ir_view(side, scope, profile_name or self._config.active_profile,
                             category or self._config.get_current_category())

    def _ir_view(self, side, scope, profile_name, category):
        c = self._config
        values = c.get_joycon_ir_sensor_settings_scoped(side, profile_name, category, scope)
        text, mode = self._display(values["function"])
        if values["function"] == "Default":
            text = "Default (IR Mouse)"
        ir_options = tuple(OptionCategory(cat.title, tuple(tuple(
            Option(o.value, "Default (IR Mouse)" if o.value == "Default" else o.label)
            for o in row) for row in cat.rows)) for cat in self._options())
        return IrSensorView(side, scope, values["function"], text, mode,
            values["activate_threshold"], freeze(values["ir_mouse"]), freeze(values["in_app_gyro"]),
            ir_options, (
                Control("activate_threshold", "Activation Threshold:", values["activate_threshold"], minimum=1, maximum=3, step=1),
                Control("sensitivity", "Sensitivity:", values["ir_mouse"]["sensitivity"], minimum=1, maximum=10, step=.2),
                *(Control(k, k.replace("_", " ").title() + ":", freeze(values["ir_mouse"].get(k)),
                    tuple(self._catalogue.SWITCH_INPUT_DAMPENING_OPTIONS))
                    for k in ("left_click", "right_click", "middle_click"))), profile_name, category,
            self._selected_option(values["function"]),
            custom_parts(values["function"])[1] == self._catalogue.IN_APP_GYRO_TOKEN,
            self._activation_view("joycon_ir_sensor", values["in_app_gyro"]))

    def in_app_gyro(self, key):
        return next(v for v in self._state.in_app_gyro if v.key == key)

    @on_core
    def start(self):
        """Classic gyro-panel initialization starts the selected Cemuhook server."""
        if getattr(self._config, "gyro_passthrough_mode", "Default") == "Cemuhook":
            self._modules("cemuhook_udp").cemuhook_server.start()
        self.normalize_rumble_mode()

    def _settings_changed(self, payload):
        if self._executor.is_core_thread():
            self._rebuild()
        else:
            self._executor.submit(self._rebuild)

    def _changed(self, *keys, save=True):
        if save:
            self._config.save_config()
        self._rebuild()
        self._bus.publish(SETTINGS_CHANGED, SettingsChanged(frozenset(keys) if keys else None))
        return SettingResult(True)

    def _reject(self, reason):
        self._changed(save=False)
        return SettingResult(False, reason)

    def _options(self, joystick=False):
        c = self._catalogue
        if joystick:
            return (OptionCategory("Joystick", (tuple(Option(x, x) for x in c.JOYSTICK_OPTIONS),)),)
        # The classic categorized selector offers all categories for every button.
        return tuple(OptionCategory(title, tuple(tuple(Option(x, c.back_button_label(x))
            for x in row) for row in rows)) for title, rows in c.BACK_BUTTON_CATEGORIES)

    def _display(self, value, key=None):
        from ..helpers import format_input_display, parse_mouse_click_mapping
        mouse = parse_mouse_click_mapping(value)
        if mouse:
            return self._catalogue.back_button_label(mouse[0]), mouse[1]
        mode, payload = custom_parts(value)
        if mode:
            labels = {self._catalogue.GYRO_LOCK_TOKEN: "Gyro Lock",
                      self._catalogue.MODE_SHIFT_TOKEN: "Mode Shift",
                      self._catalogue.IN_APP_GYRO_TOKEN: "In-app Gyro"}
            text = labels.get(payload, format_input_display(payload))
            if payload == self._catalogue.IN_APP_GYRO_TOKEN and key:
                simul = self._config.get_mapping_setting_scoped(key + "_in_app_gyro_simul", "None", None)
                if simul not in ("None", "Default"):
                    _, tokens = custom_parts(simul)
                    text += " + " + format_input_display(tokens if tokens is not None else
                        {"HOME": "Home", "CAPTURE": "Capture", "PRTSC": "PrtSc"}.get(simul, simul))
            return text, mode
        return self._catalogue.back_button_label(value), None

    def _selected_option(self, value):
        from ..helpers import parse_mouse_click_mapping
        mouse = parse_mouse_click_mapping(value)
        if mouse:
            return mouse[0]
        mode, payload = custom_parts(value)
        if mode:
            return {self._catalogue.GYRO_LOCK_TOKEN: "Gyro Lock",
                    self._catalogue.MODE_SHIFT_TOKEN: "Mode Shift",
                    self._catalogue.IN_APP_GYRO_TOKEN: "In-app Gyro"}.get(payload, "Custom")
        return value

    def _mapping_row(self, key, label, group, value, options=None, visible=True, default="Default"):
        text, mode = self._display(value, key)
        selected = self._selected_option(value)
        configurable = selected in ("In-app Gyro", "Change Profile") or (
            key in ("l_joystick", "r_joystick") and selected in ("Mouse", "Scroll Wheel", "Custom"))
        _, payload = custom_parts(value)
        return MappingRow(key, label, group, freeze(value), text, mode, value == default,
                          options if options is not None else self._options(), visible, selected, configurable,
                          tuple((m, f"Custom[{m}]:{payload}") for m in ("Hold", "Tap")) if payload is not None else ())

    def _canonical(self, value):
        return settings_writes.canonical_mapping(value, self._catalogue)

    def _rebuild(self):
        c = self._config
        if getattr(c, "gyro_mode", "World") in ("World", "Yaw"):
            self._last_axis_mode = c.gyro_mode
        mappings, joysticks, gc, ir = [], [], [], []
        for scope in SCOPES:
            gc_mode = (getattr(c, "gc_trigger_mode", "100% at Bump") if scope is None else
                c.get_scoped_category_setting("gc_trigger_mode", "Hair Trigger", scope))
            gc_view = GcTriggerView(scope, gc_mode,
                (Option("Hair Trigger", "Hair Trigger"), Option("100% at Bump", "Before Click"),
                 Option("100% at Max", "Fully Clicked")), gc_mode != "100% at Max")
            gc.append(gc_view)
            rows = []
            for key, label, group in INPUTS:
                value = c.get_mapping_setting_scoped(key, "Default", scope)
                text, mode = self._display(value, key)
                if key in ("l_joystick", "r_joystick") and value == "Scroll Wheel":
                    mode = c.get_joystick_setting_scoped(key, "scroll_activation", "Hold", scope)
                row = self._mapping_row(key, label, group, value,
                    self._options(key in ("l_joystick", "r_joystick")),
                    group != "gamecube" or gc_view.click_mapping_visible)
                if mode != row.mode:
                    from dataclasses import replace
                    row = replace(row, mode=mode)
                rows.append(row)
            mappings.append(MappingView(scope, tuple(rows)))
            for key in ("l_joystick", "r_joystick"):
                sens = c.get_joystick_setting_scoped(key, "mouse_sensitivity", 5.0, scope)
                scroll = c.get_joystick_setting_scoped(key, "scroll_mode", "Up/Down", scope)
                activation = c.get_joystick_setting_scoped(key, "scroll_activation", "Hold", scope)
                joysticks.append(JoystickSettingsView(key, scope, sens, scroll, activation, (
                        Control("mouse_sensitivity", "Sensitivity:", sens, minimum=0, maximum=10, step=.2),
                        Control("scroll_mode", "Scroll Wheel Mode:", scroll, ("Up/Down", "Up/Down/Left/Right")),
                        Control("scroll_activation", "Activation:", activation, ("Hold", "Tap"))),
                    tuple(self._mapping_row(key + "_" + direction, direction.title(), "sticks", value)
                          for direction, value in ((d, c.get_joystick_custom_scoped(key, scope).get(d, "Default"))
                              for d in ("up", "down", "left", "right", "click")))))
            for side in ("left", "right"):
                ir.append(self._ir_view(side, scope, c.active_profile, c.get_current_category()))
        dz = c.get_joystick_deadzone_settings()
        deadzones = DeadzoneView(tuple(DeadzoneRow(k, v["left"], v["right"], v["linked"]) for k, v in dz.items()))
        driver, sim = getattr(c, "driver_type", "WinUHid"), getattr(c, "simulation_mode", "PS5")
        usbps5 = driver == "USBIP" and sim == "PS5"
        rumble_mode = getattr(c, "rumble_mode", "Xbox")
        g = lambda name, default: getattr(c, name, default)
        impulse_dynamic = g("impulse_trigger_dynamic_frequency", True)
        rumble = RumbleView(rumble_mode,
            (Option("Xbox", "Xbox"), Option("PS5", "PS5 / HD Rumble") if usbps5 else Option("Switch", "Switch")),
            rumble_mode not in ("Switch", "PS5"), g("vibration_strength", 5), g("vibration_frequency", 5),
            g("rumble_delay_ms", 0), usbps5, g("audio_haptics_enabled", True), g("adaptive_triggers_enabled", True),
            driver == "WinUHid" and sim == "Xbox One", g("impulse_trigger_enabled", True), impulse_dynamic,
            g("impulse_trigger_frequency", 5), g("impulse_trigger_strength", 5), (
                Control("vibration_strength", "Strength:", g("vibration_strength", 5), minimum=0, maximum=10, step=1),
                Control("vibration_frequency", "Frequency:", g("vibration_frequency", 5), minimum=1, maximum=10, step=1,
                        visible=rumble_mode not in ("Switch", "PS5")),
                Control("impulse_trigger_frequency", "Frequency:", g("impulse_trigger_frequency", 10), minimum=1,
                        maximum=10, step=1, visible=not impulse_dynamic),
                Control("rumble_delay_ms", "Delay:", g("rumble_delay_ms", 0), step=1),
                Control("impulse_trigger_strength", "Strength:", g("impulse_trigger_strength", 5), minimum=1, maximum=10, step=1),
                *(Control(key, label, g(key, True), (Option(True, "On"), Option(False, "Off")), visible=usbps5)
                  for key, label in (("audio_haptics_enabled", "Audio Haptics:"), ("adaptive_triggers_enabled", "Adaptive Triggers:"))),
                *(Control(key, label, g(key, True), (Option(True, "On"), Option(False, "Off")),
                    visible=driver == "WinUHid" and sim == "Xbox One") for key, label in (
                    ("impulse_trigger_enabled", "Impulse Trigger:"), ("impulse_trigger_dynamic_frequency", "Dynamic Frequency:")))))
        self._state = SettingsState(tuple(mappings), tuple(joysticks), deadzones, tuple(gc), tuple(ir),
            self._gyro_view(), rumble,
            AutoDisconnectView(g("auto_disconnect_mode", "OFF"), g("auto_disconnect_days", 0),
                g("auto_disconnect_hours", 0), g("auto_disconnect_minutes", 0)),
            AppSettingsView(g("open_when_startup", False), g("start_minimized", False),
                g("power_saving_mode", "Off"), g("ui_mode", "classic"), g("change_profile_mode", "Manual")),
            tuple(self._activation_view(key) for key in (
                *(k for k, _, _ in INPUTS),
                *(stick + "_" + direction for stick in ("l_joystick", "r_joystick")
                  for direction in ("up", "down", "left", "right", "click")))))

    def _activation_view(self, key, values=None):
        values = {k: deepcopy(values.get(k, v)) for k, v in IN_APP_DEFAULTS.items()} if values is not None else {k: self._config.get_mapping_setting_scoped(key + "_in_app_gyro_" + k, deepcopy(v), None)
                  for k, v in IN_APP_DEFAULTS.items()}
        dz = bool(self._catalogue.normalize_dampening_inputs(values["deadzone_mode"]))
        damp = bool(self._catalogue.normalize_dampening_inputs(values["dampening_mode"]))
        labels = {"simul": "Simultaneous Input:", "deadzone_mode": "Trigger Deadzone:",
                  "deadzone_amount": "Deadzone:", "deadzone_pause_after_pressed_ms": "Gyro Pause After Pressed:",
                  "deadzone_pause_after_released_ms": "Gyro Pause After Released:",
                  "deadzone_effect_after_released_ms": "Deadzone Effect After Released:",
                  "dampening_mode": "Trigger Dampening:", "dampening_amount": "Dampening Amount %:",
                  "dampening_effect_after_released_ms": "Dampening Effect After Released:"}
        controls = []
        for name, value in values.items():
            selection = name.endswith("mode")
            numeric = name != "simul" and not selection
            visible = dz if name.startswith("deadzone_") and not selection else damp if name.startswith("dampening_") and not selection else True
            controls.append(Control(name, labels[name], freeze(value),
                tuple(self._catalogue.SWITCH_INPUT_DAMPENING_OPTIONS) if selection else (),
                0 if numeric else None, 100 if name == "dampening_amount" else None,
                1 if name == "dampening_amount" else None, visible))
        return InAppGyroView(key, tuple(controls), self._options(),
            self._mapping_row(key + "_in_app_gyro_simul", "Simultaneous input", "activation",
                              next(c.value for c in controls if c.key == "simul"), default="None"))

    def _gyro_view(self):
        c = self._config
        control = getattr(c, "gyro_control_mode", "Mouse")
        sensitivity = "r_joystick_gyro_sensitivity" if control == "R Joystick" else "gyro_sensitivity"
        specs = (
            ("gyro_passthrough_9axis_enabled", "9-axis Assist:", False, (True, False), None, None, None, True),
            ("steam_roll_compensation", "Horizon Lock:", False, (True, False), None, None, None, True),
            ("virtual_gyro_soft_deadzone", "Deadzone:", 0., (), 0, 100, .5, True),
            ("gyro_passthrough_mode", "Mode:", "Default", ("Default", "Cemuhook"), None, None, None, True),
            ("cemuhook_sensitivity", "Sensitivity:", 1, (), 1, 5, 1, getattr(c, "gyro_passthrough_mode", "Default") == "Cemuhook"),
            ("djg_enabled", "DJG:", False, (True, False), None, None, None, getattr(c, "simulation_mode", "") != "Switch1"),
            ("djg_dominant_side", "Dominant Side:", "Right", ("Left", "Right", "None") if getattr(c, "djg_mode", "Single Side Toggle") == "Single Side Toggle" else ("Left", "Right"), None, None, None, True),
            ("djg_mode", "Mode:", "Single Side Toggle", ("Switch Dominant Side", "Switch Gyro Side", "Single Side Toggle"), None, None, None, True),
            ("djg_activation", "Activation:", "Toggle", ("Hold", "Toggle"), None, None, None, True),
            ("gyro_activation_mode", "In-app Gyro:", "Toggle", (), None, None, None, False),
            ("gyro_control_mode", "Gyro Control:", "Mouse", ("Mouse", "R Joystick", "Steering"), None, None, None, True),
            (sensitivity, "Sensitivity:", 5. if control == "R Joystick" else .3, (), 1, 10, .2, True),
            ("gyro_mode", "Mode:", "World", ("World", "Yaw"), None, None, None, control != "Steering"),
            ("in_app_gyro_soft_deadzone", "Deadzone:", 0., (), 0, 100, .5, True),
            ("mode_shift_enabled", "Mode Shift:", False, (True, False), None, None, None, True),
            ("stick_mouse_sensitivity", "Stick Assist:", 5., (), 0, 10, .2, control == "Mouse"),
        )
        return GyroView(tuple(Control(k, label, freeze(getattr(c, k, default)),
            (Option("World", "9-Axis"), Option("Yaw", "6-Axis")) if k == "gyro_mode" else tuple(
                Option(value, ("On" if value else "Off") if k == "mode_shift_enabled" else
                       ("ON" if value else "OFF") if isinstance(value, bool) else value) for value in opts), lo, hi, step, visible)
            for k, label, default, opts, lo, hi, step, visible in specs))

    def _write_mapping(self, key, value, scope):
        settings_writes.write_mapping(self._config, key, value, scope)
        settings_writes.sync_joystick_direction(self._config, key, value, scope)

    @on_core
    def set_mapping(self, key, value, scope=None):
        if scope not in SCOPES:
            return self._reject("invalid_scope")
        settings_writes.apply_mapping_selection(self._config, key, value, scope,
            self._catalogue, lambda selected, value: self._write_mapping(key, value, scope))
        return self._changed(key, "mapping", "ir_sensor")

    @on_core
    def set_custom_mapping(self, key, tokens, mode="Hold", scope=None):
        if mode not in ("Hold", "Tap"):
            return self._reject("invalid_mode")
        if scope not in SCOPES:
            return self._reject("invalid_scope")
        tokens = tokens.split("+") if isinstance(tokens, str) and tokens else list(tokens) if tokens else []
        if tokens != [self._catalogue.IN_APP_GYRO_TOKEN] and key not in ("l_joystick", "r_joystick"):
            settings_writes.clear_mapping_selection(self._config, key, "Custom", getattr(self._catalogue, "IN_APP_GYRO_LABEL", "In-app Gyro"))
        settings_writes.commit_recorded_mapping(self._config, key, tokens, mode, scope)
        return self._changed(key, "mapping", "ir_sensor")

    @on_core
    def set_mapping_mode(self, key, mode, scope=None):
        if mode not in ("Hold", "Tap"):
            return self._reject("invalid_mode")
        current = self._canonical(self._config.get_mapping_setting_scoped(key, "Default", scope))
        value = settings_writes.mapping_mode_value(current, mode)
        if value is None:
            return self._reject("mapping_has_no_mode")
        self._write_mapping(key, value, scope)
        return self._changed(key, "mapping", "ir_sensor")

    @on_core
    def reset_mapping(self, key, scope=None):
        settings_writes.reset_mapping(self._config, key, scope)
        return self._changed(key, "mapping", "in_app_gyro", "ir_sensor")

    def _clear_activation(self, key):
        settings_writes.clear_in_app_gyro_settings(self._config, key)
    @on_core
    def copy_controller_mapping_to_mode_shift(self):
        settings_writes.on_use_default_controller_mapping_for_gyro_mode(self._config)
        return self._changed("mapping", "ir_sensor", "gc_trigger", save=False)
    @on_core
    def reset_mode_shift_mapping(self):
        settings_writes.on_reset_in_app_gyro_mode_mapping(self._config)
        return self._changed("mapping", "ir_sensor", "gc_trigger", save=False)
    @on_core
    def set_joystick_setting(self, key, setting, value, scope=None):
        if setting not in ("mouse_sensitivity", "scroll_mode", "scroll_activation"):
            return self._reject("invalid_joystick_setting")
        def sync_mouse(value):
            self._config.stick_mouse_sensitivity = value
            settings_writes.save_gyro_settings(
                self._config,
                getattr(self._config, "r_joystick_gyro_sensitivity", 5.) if getattr(self._config, "gyro_control_mode", "Mouse") == "R Joystick" else getattr(self._config, "gyro_sensitivity", .3),
                value)
        settings_writes.set_joystick_setting(self._config, key, setting, value, scope, sync_mouse)
        return self._changed(key, setting, "joystick", "gyro", save=False)

    @on_core
    def set_deadzone(self, family, side, value, *, profile_name=None, category=None, save=False):
        settings_writes.set_deadzone_percent(self._config, family, side, int(float(value)), profile_name, category)
        self._deadzone_dirty = not save
        return self._changed("deadzone", save=save)

    @on_core
    def set_deadzone_linked(self, family, linked, *, profile_name=None, category=None, save=False):
        settings_writes.set_deadzone_linked(self._config, family, bool(linked), profile_name, category)
        self._deadzone_dirty = not save
        return self._changed("deadzone", save=save)

    @on_core
    def commit_deadzones(self):
        dirty, self._deadzone_dirty = self._deadzone_dirty, False
        return self._changed("deadzone", save=dirty)

    @on_core
    def set_gc_trigger_mode(self, mode, scope=None):
        mode = {"Before Click": "100% at Bump", "Fully Clicked": "100% at Max"}.get(mode, mode)
        settings_writes.set_gc_trigger_mode(self._config, mode, scope)
        return self._changed("gc_trigger", "mapping", save=False)

    @on_core
    def set_in_app_gyro_setting(self, key, setting, value, scope=None, *, save=True):
        try:
            value = self._in_app_value(setting, value)
        except ValueError as error:
            return self._reject(str(error))
        mapping_key = key + "_in_app_gyro_" + setting
        self._write_mapping(mapping_key, value, None)
        return self._changed(mapping_key, "in_app_gyro", "mapping", "ir_sensor", save=save)

    def _in_app_value(self, setting, value):
        if setting not in IN_APP_DEFAULTS:
            raise ValueError("invalid_in_app_setting")
        if setting in ("deadzone_mode", "dampening_mode"):
            selected = self._catalogue.normalize_dampening_inputs(value)
            value = settings_writes.normalize_activation_inputs(selected, self._catalogue.SWITCH_INPUT_DAMPENING_OPTIONS)
        elif setting != "simul":
            if setting == "dampening_amount":
                try:
                    value = int(float(value))
                except (TypeError, ValueError):
                    raise ValueError("transient_numeric_edit")
            else:
                value = settings_writes.parse_activation_number(value, integer=setting != "deadzone_amount")
                if value is None:
                    raise ValueError("transient_numeric_edit")
        elif isinstance(value, str):
            value = self._canonical(value)
        return value

    @on_core
    def set_ir_gyro_setting(self, side, setting, value, scope=None, *, profile_name=None, category=None):
        try:
            value = self._in_app_value(setting, value)
        except ValueError as error:
            return self._reject(str(error))
        c = self._config
        c.set_joycon_ir_in_app_gyro_setting_scoped(side, setting, value, profile_name, category, scope)
        return self._changed("ir_sensor", "in_app_gyro")


    @on_core
    def set_ir_function(self, side, value, scope=None, *, profile_name=None, category=None, gyro_values=None):
        def write_gyro_values():
            if gyro_values is not None:
                for key, setting in gyro_values.items():
                    self._config.set_joycon_ir_in_app_gyro_setting_scoped(side, key, setting, profile_name, category, scope)
        settings_writes.set_ir_function(
            self._config, side, value, profile_name, category, scope, self._canonical,
            lambda value: custom_parts(value)[1] == self._catalogue.IN_APP_GYRO_TOKEN,
            lambda: None, write_gyro_values, lambda value: None, save=False)
        return self._changed("ir_sensor", "mapping")

    @on_core
    def set_ir_mapping_mode(self, side, mode, scope=None, *, profile_name=None, category=None):
        if mode not in ("Hold", "Tap"):
            return self._reject("invalid_mode")
        value = self._config.get_joycon_ir_sensor_settings_scoped(side, profile_name, category, scope)["function"]
        old_mode, payload = custom_parts(value)
        if old_mode is None:
            return self._reject("mapping_has_no_mode")
        return self.set_ir_function(side, f"Custom[{mode}]:{payload}", scope,
            profile_name=profile_name, category=category)

    @on_core
    def set_ir_activation_threshold(self, side, value, scope=None, *, profile_name=None, category=None):
        settings_writes.set_ir_threshold(self._config, side, value, profile_name, category, scope)
        return self._changed("ir_sensor", save=False)

    @on_core
    def set_ir_mouse_setting(self, side, key, value, scope=None, *, profile_name=None, category=None):
        if key == "sensitivity":
            value = float(value)
        settings_writes.set_ir_mouse(self._config, side, key, value, profile_name, category, scope)
        return self._changed("ir_sensor", save=False)

    @on_core
    def set_gyro_setting(self, key, value):
        c = self._config
        keys = {control.key for control in self._state.gyro.controls} | {"gyro_sensitivity", "r_joystick_gyro_sensitivity"}
        if key not in keys:
            return self._reject("invalid_gyro_setting")
        handlers = {
            "gyro_passthrough_9axis_enabled": "update_stabilized_gyro_setting",
            "steam_roll_compensation": "update_steam_roll_comp_setting",
            "virtual_gyro_soft_deadzone": "update_virtual_gyro_soft_deadzone_setting",
            "in_app_gyro_soft_deadzone": "update_in_app_gyro_soft_deadzone_setting",
            "cemuhook_sensitivity": "update_cemuhook_sensitivity",
            "djg_activation": "update_djg_activation_setting",
            "mode_shift_enabled": "update_mode_shift_setting",
        }
        if key in handlers:
            getattr(settings_writes, handlers[key])(c, value)
            return self._changed(key, "gyro", "mapping", "ir_sensor", save=False)
        if key == "gyro_passthrough_mode":
            settings_writes.update_passthrough_mode(
                c, value, self._modules("cemuhook_udp").cemuhook_server, lambda mode: None)
            return self._changed(key, "gyro", save=False)
        djg_handlers = {"djg_enabled": "update_djg_enabled_setting",
                        "djg_mode": "update_djg_mode_setting",
                        "djg_dominant_side": "update_djg_dominant_setting"}
        if key in djg_handlers:
            getattr(settings_writes, djg_handlers[key])(
                c, value, self._runtime.controller_registry(), lambda action, value=None: None)
            self._runtime.refresh_snapshots()
            return self._changed(key, "gyro", "mapping", "ir_sensor", save=False)
        if key == "gyro_control_mode":
            return self.set_gyro_control_mode(value)
        if key in ("gyro_mode", "gyro_activation_mode", "gyro_sensitivity",
                   "r_joystick_gyro_sensitivity", "stick_mouse_sensitivity"):
            setattr(c, key, float(value) if key.endswith("sensitivity") else value)
            sensitivity = getattr(c, "r_joystick_gyro_sensitivity", 5.) if getattr(c, "gyro_control_mode", "Mouse") == "R Joystick" else getattr(c, "gyro_sensitivity", .3)
            settings_writes.save_gyro_settings(c, sensitivity, getattr(c, "stick_mouse_sensitivity", 5.))
            if key == "gyro_mode" and value in ("World", "Yaw"):
                self._last_axis_mode = value
            return self._changed(key, "gyro", "mapping", "ir_sensor", save=False)
        return self._reject("invalid_gyro_setting")
    @on_core
    def set_gyro_control_mode(self, mode, *, current_sensitivity=None, axis_mode=None):
        if mode not in ("Mouse", "R Joystick", "Steering"):
            return self._reject("invalid_gyro_control")
        c = self._config
        def save_sensitivity():
            if current_sensitivity is not None:
                name = "r_joystick_gyro_sensitivity" if getattr(c, "gyro_control_mode", "Mouse") == "R Joystick" else "gyro_sensitivity"
                setattr(c, name, float(current_sensitivity))
        if mode == "Steering" and getattr(c, "gyro_mode", "World") in ("World", "Yaw"):
            self._last_axis_mode = c.gyro_mode
        settings_writes.update_gyro_control_mode(
            c, mode, save_sensitivity,
            axis_mode if axis_mode in ("World", "Yaw") else self._last_axis_mode,
            lambda action, value: None)
        return self._changed("gyro", "mapping", "ir_sensor", "mode_shift_enabled", save=False)

    @on_core
    def normalize_rumble_mode(self):
        settings_writes.normalize_rumble_mode(self._config)
        return self._changed("rumble", save=False)

    @on_core
    def set_rumble_mode(self, mode):
        c = self._config
        usbps5 = getattr(c, "driver_type", "WinUHid") == "USBIP" and getattr(c, "simulation_mode", "PS5") == "PS5"
        mode = "PS5" if usbps5 and mode == "Switch" else "Switch" if not usbps5 and mode == "PS5" else mode
        if mode not in ("Xbox", "PS5" if usbps5 else "Switch"):
            return self._reject("invalid_rumble_mode")
        settings_writes.update_rumble_mode_setting(c, mode)
        return self._changed("rumble_mode", "rumble", save=False)

    @on_core
    def set_rumble_setting(self, key, value):
        c = self._config
        handlers = {
            "vibration_strength": settings_writes.update_vibration_strength,
            "vibration_frequency": settings_writes.update_vibration_frequency,
            "rumble_delay_ms": settings_writes.set_rumble_delay,
            "adaptive_triggers_enabled": settings_writes.update_adaptive_triggers,
            "impulse_trigger_strength": settings_writes.update_impulse_strength,
            "impulse_trigger_frequency": settings_writes.update_fixed_frequency,
        }
        if key in handlers:
            handlers[key](c, value)
        elif key == "audio_haptics_enabled":
            settings_writes.update_audio_haptics(c, value, self._runtime.current_controllers)
        elif key == "impulse_trigger_enabled":
            settings_writes.update_impulse_enabled(c, value, self._runtime.controller_registry())
        elif key == "impulse_trigger_dynamic_frequency":
            settings_writes.update_dynamic_frequency(c, value, lambda: None)
        else:
            return self._reject("invalid_rumble_setting")
        return self._changed(key, "rumble", save=False)
    @on_core
    def set_auto_disconnect_mode(self, mode):
        settings_writes.update_auto_disconnect_mode(self._config, mode)
        return self._changed("auto_disconnect", save=False)
    @on_core
    def set_auto_disconnect_time(self, days, hours, minutes):
        if not settings_writes.set_auto_disconnect_time(self._config, days, hours, minutes):
            return self._reject("invalid_time")
        return self._changed("auto_disconnect", save=False)
    @on_core
    def set_run_at_startup(self, enabled):
        settings_writes.update_startup_setting(self._config, bool(enabled), self._modules("utils").set_startup)
        return self._changed("open_when_startup", "app", save=False)
    @on_core
    def set_start_minimized(self, enabled):
        settings_writes.update_minimized_setting(self._config, bool(enabled))
        return self._changed("start_minimized", "app", save=False)
    @on_core
    def set_change_profile_mode(self, mode):
        settings_writes.set_change_profile_mode(self._config, mode)
        return self._changed("change_profile_mode", "mapping", save=False)

    @on_core
    def set_power_saving_mode(self, mode, *, do_not_show_again=False):
        if mode not in ("Off", "Auto", "Full"):
            return self._reject("invalid_power_saving_mode")
        c = self._config
        current = getattr(c, "power_saving_mode", "Off")
        flag = f"power_saving_{mode.lower()}_warning_suppressed"
        suppress = do_not_show_again
        if mode != "Off" and not getattr(c, flag, False):
            proceed, checked = self._ui_port.ask_yes_no_opt_out(
                f"Power Saving: {mode}", POWER_MESSAGES[mode],
                yes="Proceed", no="Cancel", opt_out_label="Do not show again.")
            suppress = suppress or checked
            if not proceed:
                return self._reject("cancelled")
        # Suppression saved only on Proceed; cancelling must leave state unchanged.
        if mode != "Off" and suppress:
            setattr(c, flag, True)
        settings_writes.apply_power_saving_mode(
            c, current, mode, self._runtime.current_controllers, self._modules, lambda mode: None)
        self._changed("power_saving_mode", "app", save=False)
        return SettingResult(True, ui_priority_policy="above_normal" if mode in ("Auto", "Full") else "restore_base")

    @on_core
    def set_ui_mode(self, mode):
        if mode not in ("classic", "modern"):
            return self._reject("invalid_ui_mode")
        settings_writes.set_ui_mode(self._config, mode)
        self._changed("ui_mode", "app", save=False)
        return SettingResult(True, restart_required=True)

    @on_core
    def begin_batch(self):
        if self._batch_depth == 0:
            self._previous_suppression = getattr(self._config, "_save_suppressed", False)
            self._config.suppress_saves(True)
        self._batch_depth += 1
        return self._changed("batch", save=False)

    @on_core
    def end_batch(self):
        if self._batch_depth == 0:
            return SettingResult(False, "no_batch")
        self._batch_depth -= 1
        if self._batch_depth == 0:
            # Save suppression stuck: restore BEFORE saving, even if save raises.
            self._config.suppress_saves(self._previous_suppression)
            return self._changed("batch", save=False)
        return SettingResult(True)

    @on_core
    def close(self):
        self._unsubscribe()
        if self._batch_depth:
            self._batch_depth = 1
            self.end_batch()
        self.commit_deadzones()
