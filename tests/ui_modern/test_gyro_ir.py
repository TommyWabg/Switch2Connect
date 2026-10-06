from dataclasses import replace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from qfluentwidgets import SwitchButton
from app_core import events
from app_core.models import Control, Option, OptionCategory, GyroView, IrSensorView, InAppGyroView, SettingResult
from conftest import FakeCore, FakeService, ready
from ui_modern.pages.gyro import create_page, TUTORIAL_URL
from ui_modern.pages.ir import IrSensorDialog

CHOICES = (OptionCategory("General", ((Option("Default", "Default (IR Mouse)"),
    Option("Custom", "Record keys…"), Option("None", "None"), Option("Change Profile", "Change Profile")),)),
    OptionCategory("In-app Gyro", ((Option("In-app Gyro", "In-app Gyro"), Option("Gyro Lock", "Gyro Lock")),)),
    OptionCategory("Switch Input", ((Option("L", "L"), Option("R", "R")),)))
INPUTS = ("L", "ZL", "R", "ZR")
DEFAULTS = {"simul": "None", "deadzone_mode": (), "deadzone_amount": 15.,
    "deadzone_pause_after_pressed_ms": 100, "deadzone_pause_after_released_ms": 100,
    "deadzone_effect_after_released_ms": 200, "dampening_mode": (), "dampening_amount": 90,
    "dampening_effect_after_released_ms": 200}


def gyro_controls(values):
    specs = (
        ("gyro_passthrough_9axis_enabled", "9-axis Assist", False, (True, False), None, None, None),
        ("steam_roll_compensation", "Horizon Lock", False, (True, False), None, None, None),
        ("virtual_gyro_soft_deadzone", "Deadzone", 0., (), 0, 100, .5),
        ("gyro_passthrough_mode", "Destination", "Default", ("Default", "Cemuhook"), None, None, None),
        ("cemuhook_sensitivity", "Sensitivity", 1, (), 1, 5, 1),
        ("gyro_control_mode", "Control mode", "Mouse", ("Mouse", "R Joystick", "Steering"), None, None, None),
        ("gyro_sensitivity", "Sensitivity", 4., (), 1, 10, .2),
        ("gyro_mode", "Axes", "World", (Option("World", "9-Axis"), Option("Yaw", "6-Axis")), None, None, None),
        ("mode_shift_enabled", "Mode Shift", True, (True, False), None, None, None),
        ("in_app_gyro_soft_deadzone", "Deadzone", 0., (), 0, 100, .5),
        ("stick_mouse_sensitivity", "Stick Assist", 5., (), 0, 10, .2),
        ("djg_enabled", "Enable", True, (True, False), None, None, None),
        ("djg_mode", "Mode", "Single Side Toggle", ("Switch Dominant Side", "Switch Gyro Side", "Single Side Toggle"), None, None, None),
        ("djg_dominant_side", "Dominant Side", "Right", ("Left", "Right", "None") if values.get("djg_mode", "Single Side Toggle") == "Single Side Toggle" else ("Left", "Right"), None, None, None),
        ("djg_activation", "Activation", "Toggle", ("Hold", "Toggle"), None, None, None),
        ("gyro_activation_mode", "Legacy activation", "Toggle", (), None, None, None))
    controls = []
    mode = values.get("gyro_control_mode", "Mouse")
    for key, label, default, opts, lo, hi, step in specs:
        if key == "gyro_sensitivity" and mode == "R Joystick":
            key, default = "r_joystick_gyro_sensitivity", 5.
        visible = key != "gyro_activation_mode"
        if key == "cemuhook_sensitivity": visible = values.get("gyro_passthrough_mode") == "Cemuhook"
        if key == "gyro_mode": visible = mode != "Steering"
        if key == "stick_mouse_sensitivity": visible = mode == "Mouse"
        controls.append(Control(key, label, values.get(key, default),
            tuple(o if isinstance(o, Option) else Option(o, ("On" if o else "Off") if isinstance(o, bool) else o) for o in opts),
            lo, hi, step, visible))
    return GyroView(tuple(controls))


class T9Settings(FakeService):
    def __init__(self, core):
        super().__init__(core, "settings"); self.gyro = {}; self.ir = {}
        self.rebuild()

    def ir_sensor(self, side, scope=None):
        return next(v for v in self.core.settings_state.ir_sensors if v.side == side and v.scope == scope)

    def ir_sensor_context(self, side, scope=None, **context):
        data = self.sensor(side, scope, context)
        fn = data['function']
        mode = 'Tap' if fn.startswith('Custom[Tap]') else 'Hold' if fn.startswith('Custom') else None
        view = self.ir_sensor(side, scope)
        controls = tuple(replace(c, value=data['threshold'] if c.key == 'activate_threshold'
            else data['mouse'][c.key]) for c in view.controls)
        return replace(view, function=fn, display_text='Default (IR Mouse)' if fn == 'Default' else fn,
            mode=mode, activate_threshold=data['threshold'], mouse=tuple(data['mouse'].items()),
            in_app_gyro=tuple(data['gyro'].items()), controls=controls, is_gyro="INAPPGYRO" in fn, gyro_view=self.activation_view(data["gyro"]), **context)

    def sensor(self, side, scope, context=None):
        context = context or {"profile_name": self.core.profiles_state.active, "category": "xbox"}
        key = (context["profile_name"], context["category"], side, scope)
        return self.ir.setdefault(key, dict(function="Default", threshold=1,
            mouse=dict(sensitivity=4., left_click=("L",), right_click=("ZL",), middle_click=()), gyro=DEFAULTS.copy()))

    def rebuild(self):
        views = []
        for scope in (None, "in_app_gyro_mode_mappings"):
            for side in ("left", "right"):
                data = self.sensor(side, scope)
                fn = data["function"]; mode = "Tap" if fn.startswith("Custom[Tap]") else "Hold" if fn.startswith("Custom") else None
                controls = (Control("activate_threshold", "Activation Threshold", data["threshold"], minimum=1, maximum=3, step=1),
                    Control("sensitivity", "Sensitivity", data["mouse"]["sensitivity"], minimum=1, maximum=10, step=.2),
                    *(Control(k, k.replace("_", " ").title(), data["mouse"][k], INPUTS) for k in ("left_click", "right_click", "middle_click")))
                display = "Default (IR Mouse)" if fn == "Default" else "In-app Gyro" if "INAPPGYRO" in fn else fn
                views.append(IrSensorView(side, scope, fn, display,
                    mode, data["threshold"], tuple(data["mouse"].items()), tuple(data["gyro"].items()), CHOICES, controls, profile_name=self.core.profiles_state.active, category="xbox", is_gyro="INAPPGYRO" in fn, gyro_view=self.activation_view(data["gyro"])))
        self.core.settings_state = replace(self.core.settings_state, gyro=gyro_controls(self.gyro), ir_sensors=tuple(views), in_app_gyro=())

    def activation_view(self, data):
        controls = []
        for key, value in data.items():
            selection = key.endswith("mode"); numeric = key != "simul" and not selection
            visible = bool(data["deadzone_mode"]) if key.startswith("deadzone_") and not selection else bool(data["dampening_mode"]) if key.startswith("dampening_") and not selection else True
            labels = {"simul": "Simultaneous Input", "deadzone_mode": "Trigger Deadzone",
                "deadzone_amount": "Deadzone", "deadzone_pause_after_pressed_ms": "Gyro Pause After Pressed",
                "deadzone_pause_after_released_ms": "Gyro Pause After Released",
                "deadzone_effect_after_released_ms": "Deadzone Effect After Released",
                "dampening_mode": "Trigger Dampening", "dampening_amount": "Dampening Amount %",
                "dampening_effect_after_released_ms": "Dampening Effect After Released"}
            controls.append(Control(key, labels[key], value, INPUTS if selection else (),
                0 if numeric else None, 100 if key == "dampening_amount" else None, 1 if key == "dampening_amount" else None, visible))
        return InAppGyroView("joycon_ir_sensor", tuple(controls), CHOICES)

    def __getattr__(self, method):
        def call(*args, **kwargs):
            self.core.calls.append((f"settings.{method}", args, kwargs))
            if self.core.reject:
                return ready(ready(SettingResult(False, "Rejected setting")))
            if method == "set_gyro_setting": self.gyro[args[0]] = args[1]
            elif method == "set_gyro_control_mode": self.gyro["gyro_control_mode"] = args[0]
            elif method == "set_ir_function":
                side, fn, scope = args
                fn = {"In-app Gyro": "Custom[Hold]:INAPPGYRO", "Gyro Lock": "Custom[Hold]:GYRO_LOCK"}.get(fn, fn)
                data = self.sensor(side, scope, kwargs)
                if "INAPPGYRO" in data["function"] and "INAPPGYRO" not in fn:
                    data["gyro"] = DEFAULTS.copy()
                data["function"] = fn
                if "gyro_values" in kwargs:
                    data["gyro"].update(kwargs["gyro_values"])
            elif method == "set_ir_mapping_mode":
                side, mode, scope = args; data = self.sensor(side, scope, kwargs)
                data["function"] = f"Custom[{mode}]:{data['function'].split(':', 1)[1]}"
            elif method == "set_ir_activation_threshold": self.sensor(args[0], args[2], kwargs)["threshold"] = args[1]
            elif method == "set_ir_mouse_setting": self.sensor(args[0], args[3], kwargs)["mouse"][args[1]] = args[2]
            elif method == "set_ir_gyro_setting":
                self.sensor(args[0], args[3], kwargs)["gyro"][args[1]] = args[2]
            elif method == "set_change_profile_mode":
                self.core.settings_state = replace(self.core.settings_state, app=replace(self.core.settings_state.app, change_profile_mode=args[0]))
            self.rebuild()
            self.core.bus.publish(events.SETTINGS_CHANGED, events.SettingsChanged(frozenset(("gyro", "ir_sensor", "in_app_gyro"))))
            return ready(ready(SettingResult(True)))
        return call


class T9Core(FakeCore):
    def __init__(self):
        super().__init__(scenario="three"); self.settings = T9Settings(self)


@pytest.fixture
def t9(app, pump):
    core = T9Core(); toasts = []; windows = []
    def toast(message, *args, **kwargs): toasts.append((message, args, kwargs))
    def page():
        p = create_page(core, toast); p.resize(1000, 900); p.show(); windows.append(p); pump(); return p
    def dialog(side="left", scope=None):
        p = IrSensorDialog(side, scope, core=core, toast=toast); p.show(); windows.append(p); pump(); return p
    yield core, toasts, page, dialog
    for widget in windows:
        widget.close(); widget.deleteLater()
    pump()


@pytest.mark.parametrize("key,value", [("gyro_passthrough_9axis_enabled", True), ("steam_roll_compensation", True),
    ("gyro_passthrough_mode", "Cemuhook"), ("gyro_mode", "Yaw"), ("mode_shift_enabled", False),
    ("djg_enabled", False), ("djg_mode", "Switch Gyro Side"), ("djg_dominant_side", "Left"), ("djg_activation", "Hold")])
def test_each_gyro_choice(t9, pump, key, value):
    core, toasts, page, _ = t9; p = page(); w = p.controls[key]
    if isinstance(w, SwitchButton):
        w.setChecked(value)
    else:
        w.setCurrentIndex(next(i for i in range(w.count()) if w.itemData(i) == value))
    pump()
    assert ("settings.set_gyro_setting", (key, value), {}) in core.calls
    toasts[-1][2]["undo"](); pump()
    assert core.settings.gyro[key] != value


@pytest.mark.parametrize("key,value", [("virtual_gyro_soft_deadzone", 12.5), ("cemuhook_sensitivity", 3),
    ("gyro_sensitivity", 6.2), ("in_app_gyro_soft_deadzone", 7.5), ("stick_mouse_sensitivity", 2.4)])
def test_each_gyro_numeric(t9, pump, key, value):
    core, toasts, page, _ = t9; p = page(); w = p.controls[key]
    w.spin.setValue(value); w.finish(); pump()
    assert ("settings.set_gyro_setting", (key, value), {}) in core.calls
    names = [n for n, _, _ in core.calls]
    assert names.index("settings.begin_batch") < names.index("settings.set_gyro_setting") < names.index("settings.end_batch")
    assert "undo" in toasts[-1][2]


def test_mode_binding_visibility_in_place_and_legacy(t9, pump):
    core, _, page, _ = t9; p = page(); sensitivity = p.controls["gyro_sensitivity"]
    assert p.rows["cemuhook_sensitivity"].isHidden()
    p.controls["gyro_control_mode"].setCurrentIndex(1); pump()
    assert ("settings.set_gyro_control_mode", ("R Joystick",), {}) in core.calls
    assert p.controls["gyro_sensitivity"] is sensitivity
    sensitivity.spin.setValue(7.2); sensitivity.finish(); pump()
    assert ("settings.set_gyro_setting", ("r_joystick_gyro_sensitivity", 7.2), {}) in core.calls
    assert p.rows["stick_mouse_sensitivity"].isHidden()
    p.controls["gyro_control_mode"].setCurrentIndex(2); pump()
    assert p.rows["gyro_mode"].isHidden()
    core.settings.gyro["djg_mode"] = "Direct Merge"; core.settings.rebuild(); p.refresh()
    assert p.controls["djg_mode"].currentText() == "Direct merge (legacy)"
    assert "gyro_activation_mode" not in p.controls
    core.settings.gyro["gyro_sensitivity"] = .3; core.settings.rebuild(); p.refresh()
    assert sensitivity.spin.value() == .3


def test_task_tutorial_error_and_irrelevant_event(t9, pump, monkeypatch):
    core, _, page, _ = t9; p = page(); urls = []
    monkeypatch.setattr("ui_modern.pages.gyro.QDesktopServices.openUrl", lambda url: urls.append(url.toString()))
    p.tutorial.click(); assert urls == [TUTORIAL_URL]
    p.calibrate.click(); pump()
    assert ("players.start_gyro_calibration", (), {}) in core.calls
    core.bus.publish(events.TASK_STATE, events.TaskState("gyro_calibration", "Gyro", True, "Starting in 3 seconds")); pump()
    assert not p.calibrate.isEnabled(); assert "3 seconds" in p.calibration_row.captionLabel.text()
    core.bus.publish(events.TASK_STATE, events.TaskState("gyro_calibration", "Gyro", False, "Calibration completed")); pump()
    assert p.calibrate.isEnabled()
    called = []; monkeypatch.setattr(p, "refresh", lambda: called.append(True))
    core.bus.publish(events.SETTINGS_CHANGED, events.SettingsChanged(frozenset(("rumble",)))); pump()
    assert not called
    core.reject = True; p.controls["gyro_passthrough_mode"].setCurrentIndex(1); pump()
    assert p.rows["gyro_passthrough_mode"].captionLabel.text() == "Rejected setting"


def test_ir_context_mouse_threshold_and_modes(t9, pump):
    core, toasts, _, dialog = t9; p = dialog("right", "in_app_gyro_mode_mappings")
    context = dict(profile_name="Default", category="xbox")
    assert p.function.text() == "Default (IR Mouse)"
    assert p.controls["sensitivity"].spin.value() == 4
    p.controls["activate_threshold"].spin.setValue(3); p.controls["activate_threshold"].finish(); pump()
    assert ("settings.set_ir_activation_threshold", ("right", 3, p.scope), context) in core.calls
    core.profiles_state = replace(core.profiles_state, active="Gaming")
    core.driver_state = replace(core.driver_state, simulation_mode="PS4")
    p.controls["sensitivity"].spin.setValue(6.2); p.controls["sensitivity"].finish(); pump()
    assert ("settings.set_ir_mouse_setting", ("right", "sensitivity", 6.2, p.scope), context) in core.calls
    p.set_function("Custom[Hold]:VK_A"); pump()
    assert ("settings.set_ir_function", ("right", "Custom[Hold]:VK_A", p.scope), context) in core.calls
    # Writes continue to the frozen source even if the active profile changes.
    p.change_mode("Tap"); pump()
    assert ("settings.set_ir_mapping_mode", ("right", "Tap", p.scope), context) in core.calls


@pytest.mark.parametrize("key", ["left_click", "right_click", "middle_click"])
def test_click_multi_select(t9, pump, key):
    core, _, _, dialog = t9; p = dialog(); p.controls[key].click(); pump()
    picker = p._children[-1]
    for i in range(picker.listing.count()):
        picker.listing.item(i).setCheckState(Qt.Checked if i in (0, 2) else Qt.Unchecked)
    picker.save(); pump()
    assert ("settings.set_ir_mouse_setting", ("left", key, ["L", "R"], None), p.context) in core.calls
    assert p.controls[key].toolTip() == "L + R"


def test_ir_gyro_live_mirror_and_reset_undo(t9, pump):
    core, toasts, _, dialog = t9; p = dialog()
    p.set_function("In-app Gyro"); pump(timeout=.2)
    assert not p.gyro.isHidden() and p.mouse.isHidden()
    assert p.gyro_rows["deadzone_amount"].isHidden()
    p.pick_inputs("deadzone_mode", True); pump(); picker = p._children[-1]
    picker.listing.item(0).setCheckState(Qt.Checked); picker.save(); pump()
    assert not p.gyro_rows["deadzone_amount"].isHidden()
    for key in ("deadzone_amount", "deadzone_pause_after_pressed_ms", "deadzone_pause_after_released_ms", "deadzone_effect_after_released_ms", "dampening_amount", "dampening_effect_after_released_ms"):
        w = p.gyro_controls[key]; w.spin.setValue(42); w.finish(); pump()
        assert ("settings.set_ir_gyro_setting", ("left", key, 42, None), p.context) in core.calls
    assert core.settings.sensor("left", None)["gyro"]["deadzone_amount"] == 42
    p.set_function("None"); pump(); undo = toasts[-1][2]["undo"]
    assert core.settings.sensor("left", None)["gyro"]["deadzone_amount"] == 15
    undo(); pump(timeout=.3)
    assert core.settings.sensor("left", None)["gyro"]["deadzone_amount"] == 42
    p.reject(); pump()


def test_profile_mode_picker_and_recording(t9, pump):
    core, _, _, dialog = t9; p = dialog()
    p.set_function("Change Profile"); pump()
    assert not p.profile.isHidden() and p.mouse.isHidden()
    p.profile_mode.setCurrentIndex(0); pump()
    assert ("settings.set_change_profile_mode", ("Auto",), {}) in core.calls
    p.pick_function(); pump(); picker = p._children[-1]
    picker.search.setText("Gyro")
    if hasattr(picker, "items"):
        assert all("gyro" in picker.items.item(i).text().lower() for i in range(picker.items.count()))
    else:
        assert picker.lists[0].item(0).isHidden()
    picker.open_record(); pump(); record = p._children[-1]
    if hasattr(record, "toggle"):
        record.toggle()
    QTest.keyClick(getattr(record, "capture", record), Qt.Key_A)
    if hasattr(record, "add_tokens"):
        record.add_tokens(("BTN_L",))
    else:
        record.add_controller(("BTN_L",))
    record.stop()
    record.accept(); pump()
    assert ("settings.set_ir_function", ("left", "Custom[Hold]:VK_A+BTN_L", None), p.context) in core.calls
    assert any(n == "profiles.end_controller_combo_capture" for n, _, _ in core.calls)
    p.mode.setCurrentIndex(1); pump()
    assert ("settings.set_ir_mapping_mode", ("left", "Tap", None), p.context) in core.calls
    p.reset_button.click(); pump(); assert p.function.text() == "Default (IR Mouse)"


def test_numeric_throttle_release_and_close(t9, pump):
    core, _, page, _ = t9; p = page(); widget = p.controls["virtual_gyro_soft_deadzone"]
    widget.begin()
    for value in range(1, 81): widget.change(value / 2)
    assert not any(n == "settings.set_gyro_setting" for n, _, _ in core.calls)
    p.close(); pump()
    writes = [args for n, args, _ in core.calls if n == "settings.set_gyro_setting"]
    assert writes == [("virtual_gyro_soft_deadzone", 40)]
    assert sum(n == "settings.end_batch" for n, _, _ in core.calls) == 1


def test_gyro_undo_keeps_original_sensitivity_mode(t9, pump):
    core, toasts, page, _ = t9; p = page()
    widget = p.controls["gyro_sensitivity"]
    widget.spin.setValue(6.2); widget.finish(); pump()
    undo = toasts[-1][2]["undo"]
    p.controls["gyro_control_mode"].setCurrentIndex(1); pump()
    widget.spin.setValue(7.2); widget.finish(); pump()
    undo(); pump()
    assert core.settings.gyro["gyro_sensitivity"] == 4
    assert core.settings.gyro["r_joystick_gyro_sensitivity"] == 7.2


def test_gyro_ir_origin_and_remaining_controls(t9, pump):
    core, _, _, dialog = t9; p = dialog()
    core.profiles_state = replace(core.profiles_state, active="Gaming")
    p.set_function("In-app Gyro"); pump(timeout=.2)
    assert "simul" in p.gyro_controls
    p.pick_inputs("dampening_mode", True); pump(); picker = p._children[-1]
    picker.listing.item(1).setCheckState(Qt.Checked); picker.save(); pump()
    assert ("settings.set_ir_gyro_setting", ("left", "dampening_mode", ["ZL"], None), p.context) in core.calls
    assert not p.gyro_rows["dampening_amount"].isHidden()
    p.pick_simultaneous(CHOICES); pump(); p._children[-1].select(next(p._children[-1].items.item(i) for i in range(p._children[-1].items.count()) if p._children[-1].items.item(i).data(Qt.UserRole) == "R")); pump()
    assert ("settings.set_ir_gyro_setting", ("left", "simul", "R", None), p.context) in core.calls
    assert core.settings.sensor("left", None, p.context)["gyro"]["dampening_mode"] == ["ZL"]
    p.reject(); pump()


def test_picker_keyboard_save_cancel_and_empty_record(t9, pump):
    core, _, _, dialog = t9; p = dialog(); p.pick_inputs("middle_click"); pump()
    picker = p._children[-1]; picker.listing.setFocus()
    picker.listing.setCurrentRow(0); QTest.keyClick(picker.listing, Qt.Key_Space)
    QTest.keyClick(picker, Qt.Key_Return); pump()
    assert core.settings.sensor("left", None)["mouse"]["middle_click"] == ["L"]
    p.pick_function(); pump(); QTest.keyClick(p._children[-1], Qt.Key_Escape); pump()
    assert not p._children
    p.choose_function("Custom"); pump(); record = p._children[-1]
    if hasattr(record, "toggle"):
        record.toggle()
    QTest.keyClick(record, Qt.Key_Escape); pump()
    assert not p._children
    p.choose_function("Custom"); pump(); p._children[-1].accept(); pump()
    assert core.settings.sensor("left", None)["function"] == "Default"


def test_ir_gyro_undo_after_dialog_close(t9, pump):
    core, toasts, _, dialog = t9; p = dialog()
    p.set_function("In-app Gyro"); pump(timeout=.2)
    widget = p.gyro_controls["dampening_amount"]
    widget.spin.setValue(42); widget.finish(); pump()
    undo = toasts[-1][2]["undo"]
    p.reject(); core.profiles_state = replace(core.profiles_state, active="Gaming")
    undo(); pump(timeout=.3)
    assert core.settings.sensor("left", None, p.context)["gyro"]["dampening_amount"] == 90


def test_ir_reset_undo_after_dialog_deleted(t9, pump):
    core, toasts, _, dialog = t9; p = dialog()
    p.set_function("In-app Gyro"); pump(timeout=.2)
    p.gyro_controls["deadzone_amount"].spin.setValue(42); p.gyro_controls["deadzone_amount"].finish(); pump()
    p.set_function("None"); pump(); undo = toasts[-1][2]["undo"]
    p.reject(); p.deleteLater(); pump()
    undo(); pump(timeout=.3)
    assert core.settings.sensor("left", None)["function"] == "Custom[Hold]:INAPPGYRO"
    assert core.settings.sensor("left", None)["gyro"]["deadzone_amount"] == 42
