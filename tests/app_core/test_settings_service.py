"""Settings service tests with a standalone CONFIG fake and inert hardware modules."""
import ast
from concurrent.futures import Future
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace as NS
import subprocess
import sys

import pytest

from app_core import events
from app_core.bus import EventBus
from app_core.executor import CoreExecutor
from app_core.services.settings import SettingsService, SHIFT_SCOPE, IN_APP_DEFAULTS, POWER_MESSAGES
from app_core.ui_port import ScriptedUiPort


def catalogue():
    # Load only pure catalogue literals/functions, never the CONFIG singleton.
    source = Path(__file__).parents[2] / "src" / "config.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names = {"BACK_BUTTON_CATEGORIES", "BACK_BUTTON_LABELS", "JOYSTICK_OPTIONS",
             "MOUSE_CLICK_BACK_BUTTON_TOKENS", "SWITCH_INPUT_DAMPENING_OPTIONS",
             "_DAMPENING_INPUT_ALIASES", "_LEGACY_DAMPENING_MODES", "GYRO_LOCK_TOKEN",
             "MODE_SHIFT_TOKEN", "IN_APP_GYRO_TOKEN"}
    nodes = [n for n in tree.body if (isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id in names for t in n.targets)) or
        (isinstance(n, ast.FunctionDef) and n.name in {"back_button_label", "normalize_dampening_inputs"})]
    data = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), data)
    return NS(**{k: v for k, v in data.items() if not k.startswith("__")})


class InlineExecutor:
    def is_core_thread(self): return True
    def submit(self, fn, *args, **kwargs):
        future = Future()
        try: future.set_result(fn(*args, **kwargs))
        except BaseException as exc: future.set_exception(exc)
        return future


class Config:
    def __init__(self):
        self.maps, self.custom, self.joystick, self.scoped, self.ir = {}, {}, {}, {}, {}
        self.active_profile = "Base"
        self.driver_type, self.simulation_mode = "WinUHid", "PS5"
        self.gyro_mode, self.gyro_control_mode = "World", "Mouse"
        self.gyro_sensitivity, self.r_joystick_gyro_sensitivity = 2., 7.
        self.djg_dominant_side, self.djg_mode, self.djg_enabled = "Right", "Single Side Toggle", False
        self.rumble_mode = "Xbox"
        self.power_saving_mode = "Off"
        self.saves, self.disk_saves, self.generation, self.syncs = 0, 0, 0, 0
        self._save_suppressed = self._save_pending = False
        self.deadzone = {k: dict(left=10, right=10, linked=True) for k in
                         ("pro_controller", "joycon", "nso_gamecube_controller")}
        self.calls = []
    def save_config(self):
        self.saves += 1
        if self._save_suppressed: self._save_pending = True
        else: self.disk_saves += 1
    def suppress_saves(self, on):
        self._save_suppressed = on
        if not on and self._save_pending:
            self._save_pending = False
            self.save_config()
    def _bump_settings_generation(self): self.generation += 1
    def get_current_category(self): return "ps5_usbip" if self.driver_type == "USBIP" else "ps5_winuhid"
    def get_mapping_setting_scoped(self, key, default="Default", scope=None): return self.maps.get((scope, key), deepcopy(default))
    def set_mapping_setting_scoped(self, key, value, scope=None):
        self.maps[scope, key] = deepcopy(value)
        self.generation += 1
    def get_joystick_custom_scoped(self, key, scope=None): return self.custom.get((scope, key), {})
    def set_joystick_custom_scoped(self, key, value, scope=None): self.custom[scope, key] = deepcopy(value)
    def get_joystick_setting_scoped(self, key, setting, default=None, scope=None): return self.joystick.get((scope, key, setting), default)
    def set_joystick_setting_scoped(self, key, setting, value, scope=None): self.joystick[scope, key, setting] = value
    def get_scoped_category_setting(self, key, default=None, scope=None): return self.scoped.get((scope, key), default)
    def set_scoped_category_setting(self, key, value, scope=None): self.scoped[scope, key] = value
    def sync_active_in_app_gyro_activation(self): self.syncs += 1
    def copy_controller_mapping_to_in_app_gyro_mode_mapping(self): self.calls.append("copy")
    def reset_in_app_gyro_mode_mapping(self): self.calls.append("reset")
    def get_joystick_deadzone_settings(self, *args): return self.deadzone
    def set_joystick_deadzone_percent(self, family, side, value, *args):
        row = self.deadzone[family]; row[side] = max(0, min(100, value))
        if row["linked"]: row["right" if side == "left" else "left"] = row[side]
    def set_joystick_deadzone_linked(self, family, value, *args):
        row = self.deadzone[family]; row["linked"] = value
        if value: row["right"] = row["left"]
    def get_joycon_ir_sensor_settings_scoped(self, side, profile_name=None, category=None, scope=None):
        ident = (profile_name or self.active_profile, category or self.get_current_category(), scope, side)
        return self.ir.setdefault(ident, dict(function="Default", activate_threshold=1,
            ir_mouse=dict(sensitivity=4., left_click=[], right_click=[], middle_click=[]), in_app_gyro=deepcopy(IN_APP_DEFAULTS)))
    def set_joycon_ir_sensor_setting_scoped(self, side, key, value, *args): self.get_joycon_ir_sensor_settings_scoped(side, *args)[key] = value
    def set_joycon_ir_mouse_setting_scoped(self, side, key, value, *args): self.get_joycon_ir_sensor_settings_scoped(side, *args)["ir_mouse"][key] = deepcopy(value)
    def get_joycon_ir_in_app_gyro_setting_scoped(self, side, key, default=None, *args): return self.get_joycon_ir_sensor_settings_scoped(side, *args)["in_app_gyro"].get(key, default)
    def set_joycon_ir_in_app_gyro_setting_scoped(self, side, key, value, *args): self.get_joycon_ir_sensor_settings_scoped(side, *args)["in_app_gyro"][key] = deepcopy(value)
    def reset_joycon_ir_in_app_gyro_settings_scoped(self, side, *args): self.get_joycon_ir_sensor_settings_scoped(side, *args)["in_app_gyro"] = deepcopy(IN_APP_DEFAULTS)


@pytest.fixture
def rig():
    c, bus, ui, record = Config(), EventBus(), ScriptedUiPort(), []
    runtime = NS(config=c, current_controllers=[], is_quitting=False,
                 refresh_snapshots=lambda: record.append("refresh"))
    runtime.controller_registry = lambda: runtime.current_controllers
    modules = {
        "cemuhook_udp": NS(cemuhook_server=NS(start=lambda: record.append("start_udp"), stop=lambda: record.append("stop_udp"))),
        "utils": NS(set_startup=lambda value: record.append(("startup", value))),
        "power_saving": NS(notify_mode_changed=lambda: record.append("power")),
        "discoverer": NS(request_wired_rescan=lambda *a, **kw: record.append(("rescan", a, kw))),
    }
    service = SettingsService(bus, InlineExecutor(), ui, runtime, catalogue=catalogue(), modules=modules.__getitem__)
    changes = []
    bus.subscribe(events.SETTINGS_CHANGED, changes.append)
    return NS(c=c, bus=bus, ui=ui, record=record, runtime=runtime, s=service, changes=changes)


def test_views_and_refresh_are_immutable(rig):
    e = rig
    assert len(e.s.mapping().rows) == 29
    assert e.s.mapping().rows[1].label == "Capture:"
    assert {v.title for v in e.s.mapping().rows[0].options} == {"General", "In-app Gyro", "Switch Input", "PS Input", "Windows", "Media Keys", "Mouse Click"}
    before = e.s.state()
    with pytest.raises(FrozenInstanceError): before.app.ui_mode = "modern"
    e.c.maps[None, "a"] = "Custom[Tap]:VK_A+MB_1+VK_CONTROL"
    e.bus.publish(events.SETTINGS_CHANGED, events.SettingsChanged())
    row = next(r for r in e.s.mapping().rows if r.key == "a")
    assert row.display_text == "KBA+M1+CONTROL" and row.mode == "Tap" and not row.is_default
    assert before is not e.s.state()
    assert e.s.ir_sensor("left").display_text == "Default (IR Mouse)"


@pytest.mark.parametrize("key", ["home", "a", "gl", "sll", "gc_l_click", "l_joystick_up"])
def test_mapping_and_reset_clear_base_auxiliary(rig, key):
    e = rig
    assert e.s.set_mapping(key, "In-app Gyro", SHIFT_SCOPE).ok
    assert e.c.maps[SHIFT_SCOPE, key] == "Custom[Hold]:INAPPGYRO"
    e.s.set_in_app_gyro_setting(key, "deadzone_amount", 25, SHIFT_SCOPE)
    e.s.set_mapping_mode(key, "Tap", SHIFT_SCOPE)
    assert e.c.maps[SHIFT_SCOPE, key] == "Custom[Tap]:INAPPGYRO"
    assert e.c.maps[None, key + "_in_app_gyro_deadzone_amount"] == 25
    e.s.set_mapping(key, "A", SHIFT_SCOPE)
    assert e.c.maps[None, key + "_in_app_gyro_deadzone_amount"] == 15.
    e.s.reset_mapping(key, SHIFT_SCOPE)
    assert e.c.maps[SHIFT_SCOPE, key] == "Default"
    assert e.c.maps[None, key + "_in_app_gyro_dampening_mode"] == "Off"
    assert all(isinstance(x.keys, frozenset) for x in e.changes)
    if key == "l_joystick_up": assert e.c.custom[SHIFT_SCOPE, "l_joystick"]["up"] == "Default"


def test_custom_modes_mouse_labels_and_reset_simultaneous(rig):
    e = rig
    assert not e.s.set_mapping_mode("a", "Hold").ok
    assert not e.s.set_custom_mapping("a", ["VK_A"], "Invalid").ok
    e.s.set_custom_mapping("a", ["VK_A", "MB_1"], "Tap")
    assert e.c.maps[None, "a"] == "Custom[Tap]:VK_A+MB_1"
    e.s.set_mapping("b", "m3 Right Click")
    e.s.set_mapping_mode("b", "Tap")
    assert e.c.maps[None, "b"] == "Custom[Tap]:MB_3"
    assert next(r for r in e.s.mapping().rows if r.key == "b").display_text == "m3 Right Click"
    e.s.reset_mapping("a_in_app_gyro_simul")
    assert e.c.maps[None, "a_in_app_gyro_simul"] == "None"
    e.s.set_custom_mapping("a", [])
    assert e.c.maps[None, "a"] == "Default"
    assert not e.s.set_mapping("a", "A", "bad").ok


def test_joystick_scopes_deadzone_deferred_save_and_gc_visibility(rig):
    e = rig
    e.s.set_mapping("l_joystick", "Scroll Wheel", SHIFT_SCOPE)
    e.s.set_joystick_setting("l_joystick", "scroll_mode", "Up/Down/Left/Right", SHIFT_SCOPE)
    e.s.set_joystick_setting("l_joystick", "scroll_activation", "Tap", SHIFT_SCOPE)
    e.s.set_joystick_setting("l_joystick", "mouse_sensitivity", "3.4", SHIFT_SCOPE)
    assert e.s.joystick("l_joystick", SHIFT_SCOPE).scroll_activation == "Tap"
    assert e.c.stick_mouse_sensitivity == 3.4
    assert e.c.joystick[SHIFT_SCOPE, "r_joystick", "mouse_sensitivity"] == 3.4
    assert not e.s.set_joystick_setting("l_joystick", "bad", 5).ok
    saves = e.c.saves
    e.s.set_deadzone("joycon", "left", 45)
    assert e.c.deadzone["joycon"]["right"] == 45 and e.c.saves == saves
    e.s.set_deadzone_linked("joycon", False)
    e.s.set_deadzone("joycon", "right", 20)
    e.s.commit_deadzones(); e.s.commit_deadzones()
    assert e.c.saves == saves + 1
    assert e.s.state().deadzones.rows[1].left == 45
    e.s.set_gc_trigger_mode("100% at Max", SHIFT_SCOPE)
    assert not next(r for r in e.s.mapping(SHIFT_SCOPE).rows if r.key == "gc_l_click").visible
    assert next(r for r in e.s.mapping().rows if r.key == "gc_l_click").visible
    e.s.set_gc_trigger_mode("Hair Trigger")
    assert e.c.gc_trigger_mode == "Hair Trigger"
    e.s.copy_controller_mapping_to_mode_shift(); e.s.reset_mode_shift_mapping()
    assert e.c.calls == ["copy", "reset"]


@pytest.mark.parametrize("setting,value,expected", [
    ("deadzone_amount", "-5", 0.), ("deadzone_amount", "500", 500.),
    ("deadzone_pause_after_pressed_ms", "2.5", 2), ("deadzone_pause_after_released_ms", "3.6", 4),
    ("deadzone_effect_after_released_ms", "-3", 0), ("dampening_amount", "90.9", 90),
    ("dampening_effect_after_released_ms", "4.6", 5),
    ("deadzone_mode", ["ZR", "ZL", "invalid"], ["ZL", "ZR"]),
    ("dampening_mode", "Both Dampening", ["ZL", "ZR"]), ("simul", "Gyro Lock", "Custom[Hold]:GYRO_LOCK"),
])
def test_activation_numeric_rules(rig, setting, value, expected):
    e = rig
    assert e.s.set_in_app_gyro_setting("a", setting, value, SHIFT_SCOPE).ok
    assert e.c.maps[None, "a_in_app_gyro_" + setting] == expected
    assert (SHIFT_SCOPE, "a_in_app_gyro_" + setting) not in e.c.maps


@pytest.mark.parametrize("invalid", ["", "no", None])
def test_transient_numeric_retains_last_commit(rig, invalid):
    e = rig
    e.s.set_in_app_gyro_setting("a", "deadzone_amount", 42)
    saves = e.c.saves
    assert not e.s.set_in_app_gyro_setting("a", "deadzone_amount", invalid).ok
    assert e.c.maps[None, "a_in_app_gyro_deadzone_amount"] == 42 and e.c.saves == saves


def test_ir_origin_context_live_mirror_and_no_close_save(rig):
    e = rig
    e.s.set_ir_function("left", "In-app Gyro", SHIFT_SCOPE)
    e.c.active_profile = "Other"
    e.s.set_ir_gyro_setting("left", "deadzone_amount", 48, SHIFT_SCOPE, profile_name="Base", category="ps5_winuhid")
    origin = e.c.ir["Base", "ps5_winuhid", SHIFT_SCOPE, "left"]
    assert origin["in_app_gyro"]["deadzone_amount"] == 48
    assert e.s.ir_sensor("left", SHIFT_SCOPE).in_app_gyro != tuple(origin["in_app_gyro"].items())
    saves = e.c.saves
    assert e.c.saves == saves
    e.s.set_ir_function("left", "Default", SHIFT_SCOPE, profile_name="Base")
    assert origin["in_app_gyro"]["deadzone_amount"] == 15.
    e.s.set_ir_activation_threshold("right", "3", SHIFT_SCOPE)
    e.s.set_ir_mouse_setting("right", "sensitivity", "6.2", SHIFT_SCOPE)
    e.s.set_ir_mouse_setting("right", "left_click", ["ZL"], SHIFT_SCOPE)
    assert e.s.ir_sensor("right", SHIFT_SCOPE).activate_threshold == 3
    assert dict(e.s.ir_sensor("right", SHIFT_SCOPE).mouse)["sensitivity"] == 6.2


def test_gyro_generation_server_sync_modes_and_visibility(rig):
    e = rig
    for key in ("gyro_passthrough_9axis_enabled", "steam_roll_compensation"):
        e.s.set_gyro_setting(key, True)
    assert e.c.generation == 2
    e.s.set_gyro_setting("gyro_passthrough_mode", "Cemuhook")
    e.s.start()
    e.s.set_gyro_setting("gyro_passthrough_mode", "Default")
    assert e.record[:3] == ["start_udp", "start_udp", "stop_udp"]
    e.s.set_gyro_setting("cemuhook_sensitivity", "3.8")
    assert e.c.cemuhook_sensitivity == 3
    e.s.set_gyro_control_mode("R Joystick", current_sensitivity=4.)
    assert e.c.gyro_sensitivity == 4. and e.c.r_joystick_gyro_sensitivity == 7.
    assert not next(c for c in e.s.state().gyro.controls if c.key == "stick_mouse_sensitivity").visible
    e.s.set_gyro_control_mode("Steering", current_sensitivity=6.)
    assert e.c.gyro_mode == "Roll"
    e.s.set_gyro_control_mode("Mouse", axis_mode="Yaw")
    assert e.c.gyro_mode == "Yaw" and e.c.r_joystick_gyro_sensitivity == 6.
    e.s.set_gyro_setting("mode_shift_enabled", True)
    assert e.c.syncs == 4
    e.s.set_gyro_setting("stick_mouse_sensitivity", "3")
    assert e.c.joystick[SHIFT_SCOPE, "r_joystick", "mouse_sensitivity"] == 3.
    for key in ("gyro_mode", "gyro_activation_mode", "gyro_sensitivity", "r_joystick_gyro_sensitivity", "virtual_gyro_soft_deadzone", "in_app_gyro_soft_deadzone", "djg_activation"):
        e.s.set_gyro_setting(key, "World" if key == "gyro_mode" else "Toggle" if key in ("gyro_activation_mode", "djg_activation") else 2.)
    assert not e.s.set_gyro_setting("bad", True).ok
    assert not e.s.set_gyro_control_mode("bad").ok


def test_djg_side_effects(rig):
    e = rig
    vc = NS(active_gyro_side="Left", djg_left_active=False, djg_right_active=False)
    e.runtime.current_controllers = [None, vc]
    e.s.set_gyro_setting("djg_enabled", False)
    assert vc.active_gyro_side == "Right"
    e.s.set_gyro_setting("djg_dominant_side", "Left")
    assert vc.active_gyro_side == "Left"
    e.s.set_gyro_setting("djg_enabled", True)
    e.s.set_gyro_setting("djg_mode", "Switch Dominant Side")
    e.s.set_gyro_setting("djg_dominant_side", "Left")
    assert vc.djg_left_active and vc.djg_right_active
    e.s.set_gyro_setting("djg_mode", "Switch Gyro Side")
    e.s.set_gyro_setting("djg_dominant_side", "None")
    assert e.c.djg_dominant_side == "Right" and vc.active_gyro_side == "Right"
    e.s.set_gyro_setting("djg_dominant_side", "bad")
    assert e.c.djg_dominant_side == "Right"
    e.c.djg_dominant_side = "None"
    e.s.set_gyro_setting("djg_mode", "Switch Dominant Side")
    assert e.c.djg_dominant_side == "Right"


def test_rumble_alias_and_runtime_effects(rig):
    e = rig
    e.c.driver_type = "USBIP"; e.c.rumble_mode = "Switch"
    e.s.normalize_rumble_mode()
    assert e.c.rumble_mode == "PS5" and not e.s.state().rumble.frequency_visible
    assert e.s.state().rumble.options[1].label == "PS5 / HD Rumble"
    assert e.s.state().rumble.audio_haptics_available
    rebuilt, cleared = [], []
    e.runtime.current_controllers = [NS(mode="PS5", _setup_vg_controller=lambda: rebuilt.append(1)), None]
    e.s.set_rumble_setting("audio_haptics_enabled", False)
    assert rebuilt == [1]
    e.s.set_rumble_setting("adaptive_triggers_enabled", False)
    e.c.driver_type = "WinUHid"; e.c.simulation_mode = "Xbox One"
    e.runtime.current_controllers = [NS(mode="Xbox One", driver_type="WinUHid", clear_xbox_impulse_triggers=lambda: cleared.append(1))]
    e.s.normalize_rumble_mode()
    assert e.c.rumble_mode == "Switch"
    e.s.set_rumble_mode("Xbox")
    assert e.s.state().rumble.frequency_visible
    e.s.set_rumble_setting("impulse_trigger_enabled", False)
    assert cleared == [1]
    for key in ("vibration_strength", "vibration_frequency", "impulse_trigger_frequency", "impulse_trigger_strength"):
        e.s.set_rumble_setting(key, "6.8")
        assert getattr(e.c, key) == 6
    e.s.set_rumble_setting("impulse_trigger_dynamic_frequency", False)
    assert e.s.state().rumble.controls[2].visible
    e.s.set_rumble_setting("rumble_delay_ms", "bad")
    assert e.c.rumble_delay_ms == 0
    e.s.set_rumble_setting("rumble_delay_ms", "22")
    assert e.c.rumble_delay_ms == 22
    assert not e.s.set_rumble_setting("bad", True).ok
    assert not e.s.set_rumble_mode("bad").ok
    e.c.rumble_mode = "bad"; e.s.normalize_rumble_mode()
    assert e.c.rumble_mode == "Xbox"


@pytest.mark.parametrize("answer", [False, True])
@pytest.mark.parametrize("mode", ["Auto", "Full"])
def test_power_warning_branches_and_optout(rig, answer, mode):
    e = rig
    e.ui.answers.append((answer, False))
    result = e.s.set_power_saving_mode(mode, do_not_show_again=True)
    assert result.ok == answer
    assert e.ui.calls[-1].args == (f"Power Saving: {mode}", POWER_MESSAGES[mode])
    assert e.ui.calls[-1].kwargs["yes"] == "Proceed"
    if not answer:
        assert e.c.power_saving_mode == "Off" and e.c.saves == 0
        assert not hasattr(e.c, f"power_saving_{mode.lower()}_warning_suppressed")
    else:
        assert result.ui_priority_policy == "above_normal"
        assert getattr(e.c, f"power_saving_{mode.lower()}_warning_suppressed")
        e.s.set_power_saving_mode(mode)
        assert len(e.ui.calls) == 1
        assert e.s.set_power_saving_mode("Off").ui_priority_policy == "restore_base"


def test_app_time_batch_close_and_power_wake(rig):
    e = rig
    e.s.begin_batch(); e.s.begin_batch()
    e.s.set_start_minimized(True); e.s.set_run_at_startup(True)
    assert e.c.disk_saves == 0
    e.s.end_batch(); assert e.c._save_suppressed
    e.s.end_batch(); assert e.c.disk_saves == 1 and not e.c._save_suppressed
    assert not e.s.end_batch().ok
    assert not e.s.set_ui_mode("bad").ok
    assert e.s.state().app.start_minimized
    e.s.set_auto_disconnect_mode("Inactive")
    e.s.set_auto_disconnect_time("", "100", "120")
    before = e.s.state().auto_disconnect
    assert before.hours == 100 and before.minutes == 120
    assert not e.s.set_auto_disconnect_time("3", "bad", "4").ok
    assert e.s.state().auto_disconnect == before
    e.s.set_change_profile_mode("Auto")
    assert e.s.state().app.change_profile_mode == "Auto"
    e.runtime.current_controllers = [NS(_update_wake=NS(set=lambda: e.record.append("wake")), controllers=[
        NS(_poke_rumble_scheduler=lambda: e.record.append("poke"), on_power_saving_mode_changed=lambda a, b: e.record.append((a, b)))])]
    e.s.set_power_saving_mode("Off")
    assert e.record[-3:] == ["wake", "poke", ("Off", "Off")]
    assert not e.s.set_power_saving_mode("bad").ok
    e.s.begin_batch(); e.s.set_start_minimized(False); e.s.close()
    assert not e.c._save_suppressed


def test_off_core_mutation_returns_future():
    c, executor = Config(), CoreExecutor()
    try:
        s = SettingsService(EventBus(), executor, ScriptedUiPort(), NS(config=c), catalogue=catalogue())
        result = s.set_start_minimized(True)
        assert isinstance(result, Future) and result.result(2).ok
        assert s.state().app.start_minimized
        s.close().result(2)
    finally:
        executor.shutdown()


def test_optout_port_and_batch_flush_failure(rig):
    e = rig
    calls = []
    def warning(*args, **kw):
        calls.append((args, kw)); return True, True
    e.ui.ask_yes_no_opt_out = warning
    assert e.s.set_power_saving_mode("Full").ok
    assert calls[-1][1] == dict(yes="Proceed", no="Cancel", opt_out_label="Do not show again.")
    assert e.c.power_saving_full_warning_suppressed
    e.s.begin_batch(); e.s.set_start_minimized(True)
    def fail(): raise OSError("disk failure")
    e.c.save_config = fail
    with pytest.raises(OSError): e.s.end_batch()
    assert not e.c._save_suppressed and not e.s.end_batch().ok




def test_axes_restore_without_ui_value_and_external_batch_ownership(rig):
    e = rig
    e.s.set_gyro_setting("gyro_mode", "Yaw")
    e.s.set_gyro_control_mode("Steering")
    e.s.set_gyro_control_mode("Mouse")
    assert e.c.gyro_mode == "Yaw"
    e.c.suppress_saves(True)
    e.s.begin_batch(); e.s.set_start_minimized(True); e.s.end_batch()
    assert e.c._save_suppressed
    e.c.suppress_saves(False)
    assert e.c.disk_saves >= 1


def test_t6_modules_import_without_ui_or_hardware():
    src = str(Path(__file__).parents[2] / "src")
    script = f"""
import sys
sys.path.insert(0, {src!r})
import app_core.services.settings
import app_core.services.calibration
for prefix in ('tkinter', 'PySide6', 'gui', 'config', 'controller', 'virtual_controller', 'gyro'):
    assert not any(n == prefix or n.startswith(prefix + '.') for n in sys.modules), prefix
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr

@pytest.mark.parametrize("answer,checked", [(False, False), (False, True), (True, False), (True, True)])
def test_power_checkbox_suppression_only_on_proceed(rig, answer, checked):
    e = rig
    e.ui.answers.append((answer, checked))
    result = e.s.set_power_saving_mode("Full")
    assert result.ok is answer
    assert getattr(e.c, "power_saving_full_warning_suppressed", False) is (answer and checked)
    assert e.c.power_saving_mode == ("Full" if answer else "Off")
    assert len([call for call in e.ui.calls if call.name.startswith("ask")]) == 1


def test_ir_captured_context_read(rig):
    origin = rig.s.ir_sensor('left')
    assert origin.profile_name == rig.c.active_profile
    assert origin.category == rig.c.get_current_category()
    view = rig.s.ir_sensor_context('right', profile_name='Other', category='switch2')
    assert (view.profile_name, view.category) == ('Other', 'switch2')
    assert rig.c.active_profile == 'Base'


@pytest.mark.parametrize("value,selected", [
    ("Custom[Tap]:VK_F2", "Custom"), ("Custom[Hold]:INAPPGYRO", "In-app Gyro"),
    ("Custom[Hold]:GYRO_LOCK", "Gyro Lock"), ("Custom[Hold]:MODE_SHIFT", "Mode Shift"),
    ("Custom[Tap]:MB_1", "m1 Left Click"),
])
def test_mapping_and_ir_picker_semantics_are_read_models(rig, value, selected):
    rig.s.set_mapping('a', value)
    row = next(row for row in rig.s.mapping().rows if row.key == 'a')
    assert row.selected_option == selected
    rig.s.set_ir_function('left', value)
    view = rig.s.ir_sensor('left')
    assert view.selected_option == selected
    assert view.mode == ('Tap' if '[Tap]' in value else 'Hold')
    assert view.is_gyro is (selected == 'In-app Gyro')


def test_ir_scoped_gyro_changes_and_reset_undo_publish_authoritative_views(rig):
    context = dict(profile_name='Base', category='ps5_winuhid')
    seen = []
    rig.bus.subscribe(events.SETTINGS_CHANGED, seen.append)
    rig.s.set_ir_function('left', 'In-app Gyro', **context)
    rig.s.set_ir_gyro_setting('left', 'deadzone_amount', 42.5, **context)
    old = rig.s.ir_sensor('left')
    rig.c.active_profile = 'Other'
    rig.s.set_ir_function('left', 'None', **context)
    assert rig.s.ir_sensor_context('left', **context).is_gyro is False
    rig.s.set_ir_function('left', old.function, **context, gyro_values=dict(old.in_app_gyro))
    restored = rig.s.ir_sensor_context('left', **context)
    assert restored.is_gyro
    assert dict(restored.in_app_gyro)['deadzone_amount'] == 42.5
    assert next(c.value for c in restored.gyro_view.controls if c.key == 'deadzone_amount') == 42.5
    assert rig.c.active_profile == 'Other'
    assert len(seen) == 4


@pytest.mark.parametrize("value, expected", [("nan", 0.0), ("inf", float("inf"))])
def test_activation_numeric_parser_uses_classic_clamping(rig, value, expected):
    from app_core.settings_writes import parse_activation_number
    assert parse_activation_number(value) == expected
    assert rig.s.set_in_app_gyro_setting("a", "deadzone_amount", value).ok
    assert rig.c.get_mapping_setting_scoped("a_in_app_gyro_deadzone_amount", None, None) == expected


def test_ui_mode_is_written_before_returning(rig, core_tmp_path):
    import threading, yaml
    e = rig
    e.c.config_file_path, e.c._save_lock = str(core_tmp_path / "config.yaml"), threading.Lock()
    (core_tmp_path / "config.yaml").write_text(yaml.safe_dump({"ui_mode": "classic", "other": 1}), encoding="utf-8")
    assert e.s.set_ui_mode("modern").restart_required
    assert yaml.safe_load((core_tmp_path / "config.yaml").read_text(encoding="utf-8")) == {"ui_mode": "modern", "other": 1}
    assert e.c.ui_mode == "modern"
