"""Mapping UI tests: FakeCore plus the hardware-free settings rig."""
import importlib.util
import subprocess
import sys
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace
import pytest
from PySide6.QtCore import Qt, QEvent, QPoint, QPointF, QCoreApplication
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from app_core import events
from app_core.services.settings import SettingsService, INPUTS
from app_core.ui_port import ScriptedUiPort
from conftest import FakeCore, ready
from ui_modern.pages.mapping import (MappingPage, RecordDialog, DeadzoneDialog,
    JoystickDialog, ActivationDialog, ChangeProfileDialog, SHIFT_SCOPE)
from ui_modern.widgets.recording import canonical_tokens

spec = importlib.util.spec_from_file_location("t8_settings_fakes", Path(__file__).parents[1] / "app_core/test_settings_service.py")
settings_fakes = importlib.util.module_from_spec(spec); spec.loader.exec_module(settings_fakes)


def test_mapping_import_does_not_load_config_or_hardware():
    result = subprocess.run([sys.executable, "-c",
        "import sys; sys.path.insert(0,'src'); import ui_modern.pages.mapping; "
        "assert not any(x in sys.modules for x in ('config','controller','virtual_controller','discoverer','gui'))"],
        cwd=Path(__file__).parents[2], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


class RecordingSettings:
    def __init__(self, core, service):
        self.core, self.service = core, service

    def __getattr__(self, name):
        fn = getattr(self.service, name)
        if name in ("state", "mapping", "joystick", "in_app_gyro", "ir_sensor"):
            return fn
        def invoke(*args, **kwargs):
            self.core.calls.append(("settings." + name, args, kwargs))
            return ready(ready(fn(*args, **kwargs)))
        return invoke


class CaptureProfiles:
    def __init__(self, core):
        self.core = core; self.callback = None
    def state(self):
        return self.core.profiles_state
    def begin_controller_combo_capture(self, callback):
        self.core.calls.append(("profiles.begin_controller_combo_capture", (), {}))
        self.callback = callback
        return ready(None)
    def end_controller_combo_capture(self):
        self.core.calls.append(("profiles.end_controller_combo_capture", (), {}))
        self.callback = None
        return ready(None)


class MappingCore(FakeCore):
    def __init__(self):
        super().__init__()
        self.config_fake = settings_fakes.Config()
        service = SettingsService(self.bus, settings_fakes.InlineExecutor(), ScriptedUiPort(),
            SimpleNamespace(config=self.config_fake), catalogue=settings_fakes.catalogue())
        self.settings = RecordingSettings(self, service)
        self.profiles = CaptureProfiles(self)


@pytest.fixture
def mapping(app, pump, monkeypatch):
    monkeypatch.setattr("ui_modern.widgets.recording.pressed_keyboard_tokens", lambda: ())
    core = MappingCore(); toasts = []
    page = MappingPage(core, lambda *a, **k: toasts.append((a, k)))
    page.resize(1100, 760); page.show(); pump()
    yield page, core, toasts
    for child in page.findChildren(RecordDialog):
        child.reject()
    if getattr(page, "dialog", None) is not None:
        page.dialog.reject()
    page.bridge.close(); page.hide(); page.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete); pump()


@pytest.mark.parametrize("scope", [None, SHIFT_SCOPE])
@pytest.mark.parametrize("key,label,group", INPUTS)
def test_every_mapping_control_and_undo(mapping, pump, scope, key, label, group):
    page, core, toasts = mapping
    row = page.rows[scope, key]
    assert row.snapshot.label == label and row.snapshot.group == group
    row.choose("WASD" if key.endswith("joystick") else "A"); pump()
    assert ("settings.set_mapping", (key, "WASD" if key.endswith("joystick") else "A", scope), {}) in core.calls
    assert not row.reset.isHidden()
    toasts[-1][1]["undo"](); pump()
    assert core.settings.mapping(scope).rows[[r.key for r in core.settings.mapping(scope).rows].index(key)].is_default
    row.choose("None"); pump(); row.restore_default(); pump()
    assert ("settings.reset_mapping", (key, scope), {}) in core.calls


def test_scopes_filters_gc_and_integration(mapping, pump, monkeypatch):
    page, core, _ = mapping
    identities = {k: id(v) for k, v in page.rows.items()}
    page.changed_only.setChecked(True); assert page.rows[None, "a"].isHidden()
    page.rows[None, "a"].choose("B"); pump(); assert not page.rows[None, "a"].isHidden()
    page.scope_control.setCurrentItem("shift"); pump()
    assert page.scope == SHIFT_SCOPE and not page.shift_actions.isHidden()
    page.gc_modes[SHIFT_SCOPE].setCurrentItem("100% at Max"); pump()
    assert page.rows[SHIFT_SCOPE, "gc_l_click"].isHidden()
    assert page.rows[SHIFT_SCOPE, "gc_r_click"].isHidden()
    page.copy.click(); page.dialog.yesButton.click(); pump(timeout=.3)
    assert any(c[0] == "settings.copy_controller_mapping_to_mode_shift" for c in core.calls)
    page.reset_shift.click(); page.dialog.cancelButton.click(); pump(timeout=.3)
    assert not any(c[0] == "settings.reset_mode_shift_mapping" for c in core.calls)
    page.reset_shift.click(); page.dialog.yesButton.click(); pump(timeout=.3)
    assert any(c[0] == "settings.reset_mode_shift_mapping" for c in core.calls)
    assert identities == {k: id(v) for k, v in page.rows.items()}



def test_picker_categories_search_keyboard_and_modes(mapping, pump):
    page, core, _ = mapping; row = page.rows[None, "a"]
    row.pick(); picker = row.picker; pump()
    assert picker.categories.count() == len(row.snapshot.options) + 1
    default = next(picker.items.item(i) for i in range(picker.items.count()) if picker.items.item(i).data(Qt.UserRole) == "Default")
    assert default.checkState() == Qt.Checked
    picker.search.setText("Mode Shift"); pump(); assert picker.items.count() == 1
    QTest.keyClick(picker.search, Qt.Key_Down); QTest.keyClick(picker.items, Qt.Key_Return); pump()
    assert row.snapshot.stored_value == "Custom[Hold]:MODE_SHIFT"
    row.open_picker(); row.picker.activation.setCurrentItem("Tap"); pump()
    assert ("settings.set_mapping_mode", ("a", "Tap", None), {}) in core.calls
    row.choose("In-app Gyro"); pump(); row.open_picker(); row.picker.configure_button.click(); pump()
    assert isinstance(page.dialog, ActivationDialog); page.dialog.reject()
    row.choose("Change Profile"); pump(); row.open_picker(); row.picker.configure_button.click(); pump()
    assert isinstance(page.dialog, ChangeProfileDialog)
    page.dialog.mode.setCurrentItem("Auto"); pump()
    assert ("settings.set_change_profile_mode", ("Auto",), {}) in core.calls


def test_recording_keyboard_mouse_wheel_controller_save_and_stale_cancel(mapping, pump):
    page, core, _ = mapping
    dialog = RecordDialog(core, "a", SHIFT_SCOPE, page.toast, page); dialog.show(); pump()
    callback = core.profiles.callback
    QTest.keyClick(dialog.capture, Qt.Key_A, Qt.ControlModifier | Qt.ShiftModifier)
    QTest.mouseClick(dialog.capture, Qt.RightButton)
    wheel = QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(), QPoint(0, 120), Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    dialog.eventFilter(dialog.capture, wheel)
    callback(("BTN_A", "BTN_A")); pump()
    assert dialog.tokens == ["VK_CONTROL", "VK_SHIFT", "VK_A", "MB_3", "MW_UP", "BTN_A"]
    dialog.mode.setCurrentItem("Tap"); dialog.save.click(); pump()
    assert core.config_fake.maps[SHIFT_SCOPE, "a"] == "Custom[Tap]:VK_CONTROL+VK_SHIFT+VK_A+MB_3+MW_UP+BTN_A"
    dialog = RecordDialog(core, "b", None, page.toast, page); dialog.show(); pump()
    late_callback = core.profiles.callback; before = len(core.calls)
    dialog.clear.click(); late_callback(("BTN_X",)); pump(); assert not dialog.tokens
    late_callback = core.profiles.callback
    QTest.keyClick(dialog.capture, Qt.Key_Escape); pump()
    late_callback(("BTN_Y",)); dialog.save_recording(); pump()
    assert not any(c[0] == "settings.set_custom_mapping" for c in core.calls[before:])
    assert not dialog.active and core.profiles.callback is None
    assert canonical_tokens(["VK_RSHIFT", "VK_ALT", "VK_A", "VK_LCONTROL", "VK_A"]) == ["VK_CONTROL", "VK_SHIFT", "VK_MENU", "VK_A"]


@pytest.mark.parametrize("mode", ["Mouse", "Scroll Wheel", "Custom"])
@pytest.mark.parametrize("scope", [None, SHIFT_SCOPE])
def test_joystick_dialogs(mapping, pump, mode, scope):
    page, core, _ = mapping
    page.rows[scope, "l_joystick"].choose(mode); pump()
    page.rows[scope, "l_joystick"].open_picker(); page.rows[scope, "l_joystick"].picker.configure_button.click(); pump()
    dialog = page.dialog; assert isinstance(dialog, JoystickDialog)
    if mode == "Mouse":
        assert dialog.sensitivity.slider.minimum() == 0 and dialog.sensitivity.slider.maximum() == 50
        dialog.sensitivity.slider.setValue(31); dialog.sensitivity.finish(); pump()
        assert ("settings.set_joystick_setting", ("l_joystick", "mouse_sensitivity", 6.2, scope), {}) in core.calls
    elif mode == "Scroll Wheel":
        dialog.scroll.setCurrentItem("Up/Down/Left/Right"); dialog.activation.setCurrentItem("Tap"); pump()
        assert core.settings.joystick("l_joystick", scope).scroll_activation == "Tap"
        assert ("settings.set_joystick_setting", ("l_joystick", "scroll_mode", "Up/Down/Left/Right", scope), {}) in core.calls
    else:
        assert set(dialog.rows) == {"up", "down", "left", "right", "click"}
        for direction, row in dialog.rows.items():
            row.choose("A"); pump()
            assert core.calls[-1] == ("settings.set_mapping", (f"l_joystick_{direction}", "A", scope), {})


def test_deadzone_batch_link_and_close(mapping, pump):
    page, core, _ = mapping; dialog = DeadzoneDialog(core, page.toast, page); dialog.show(); pump()
    for family in dialog.links:
        for side in ("left", "right"):
            slider = dialog.sliders[family, side]
            assert slider.slider.minimum() == 0 and slider.slider.maximum() == 100
            before = len(core.calls)
            slider.slider.setValue(21); slider.slider.setValue(22); slider.slider.setValue(23)
            assert not any(c[0] == "settings.set_deadzone" for c in core.calls[before:])
            slider.finish(); pump()
            assert dialog.read_row(family).left == dialog.read_row(family).right == 23
            assert not core.config_fake._save_suppressed
        dialog.links[family].setChecked(False); pump()
        dialog.sliders[family, "left"].slider.setValue(40); dialog.sliders[family, "left"].finish(); pump()
        assert dialog.read_row(family).right == 23
    dialog.reject(); pump()
    assert ("settings.commit_deadzones", (), {}) in core.calls


def test_activation_defaults_selection_clamping_close_persistence(mapping, pump):
    page, core, _ = mapping; dialog = ActivationDialog(core, "a", page.toast, page); dialog.show(); pump()
    assert dialog.values()["deadzone_amount"] == 15.0
    assert dialog.fields["deadzone_pause_after_pressed_ms"].text() == "100"
    assert dialog.field_rows["deadzone_group"].isHidden()
    for name, boxes in dialog.selections.items():
        for token, box in boxes.items():
            box.setChecked(True); pump()
            assert token in dialog.values()[name]
    assert not dialog.field_rows["deadzone_group"].isHidden()
    for name, field in dialog.fields.items():
        if name == "dampening_amount":
            assert field.slider.maximum() == 100
            field.slider.setValue(80); field.finish(); pump()
            assert dialog.values()[name] == 80
        else:
            field.setFocus(); field.setText("-9"); field.textEdited.emit("-9"); pump()
            assert dialog.values()[name] == 0
            field.setText("5000000"); field.textEdited.emit("5000000"); pump()
            assert dialog.values()[name] == 5000000
            field.setText(""); field.textEdited.emit(""); pump()
            assert dialog.values()[name] == 5000000
            field.setText("nan"); field.textEdited.emit("nan"); pump()
            assert dialog.values()[name] == 5000000
    dialog.simultaneous.choose("Custom[Tap]:VK_CONTROL+VK_A"); pump()
    assert ("settings.set_in_app_gyro_setting", ("a", "simul", "Custom[Tap]:VK_CONTROL+VK_A", None), {}) in core.calls
    before = core.config_fake.saves; dialog.reject(); pump()
    assert core.config_fake.saves == before


def test_public_ir_recorder_empty_value_focus_loss_and_origin_cancel(mapping, pump):
    page, core, _ = mapping; writes = []
    dialog = RecordDialog(core, parent=page, toast=page.toast, mode="Tap", writer=writes.append,
                          keyboard_snapshot=lambda: ("VK_CONTROL", "VK_LWIN", "VK_A"))
    dialog.show(); pump()
    dialog.eventFilter(dialog, QEvent(QEvent.WindowDeactivate))
    assert dialog.value == "Custom[Tap]:VK_CONTROL+VK_LWIN+VK_A"
    dialog.clear.click(); dialog.add_tab.click(); assert dialog.tokens == ["VK_TAB"]
    dialog.clear.click(); dialog.save.click(); pump()
    assert writes == ["Default"]
    dialog = JoystickDialog(core, "l_joystick", None, "Mouse", page.toast, page)
    dialog.show(); pump(); dialog.sensitivity.slider.setValue(31)
    before = len(core.calls)
    core.profiles_state = replace(core.profiles_state, active="Gaming")
    core.bus.publish(events.PROFILES_CHANGED, core.profiles_state); pump()
    assert not dialog.isVisible() and not core.config_fake._save_suppressed
    assert not any(call[0] == "settings.set_joystick_setting" for call in core.calls[before:])


def test_relevant_settings_events_and_existing_factories(mapping, pump, monkeypatch):
    page, core, _ = mapping; count = []
    monkeypatch.setattr(page, "sync_reads", lambda: count.append(True))
    core.bus.publish(events.SETTINGS_CHANGED, events.SettingsChanged(frozenset({"rumble"}))); pump()
    assert not count
    core.bus.publish(events.SETTINGS_CHANGED, events.SettingsChanged(frozenset({"mapping"}))); pump()
    assert len(count) == 1
    calls = []
    monkeypatch.setattr("ui_modern.pages.ir.IrSensorDialog",
        lambda side, scope, **kw: calls.append((side, scope, kw)) or ChangeProfileDialog(core, page.toast, page))
    monkeypatch.setattr("ui_modern.wizards.calibration.open_gc_trigger_wizard",
        lambda selected_core, **kw: calls.append((selected_core, kw)) or None)
    page.open_ir("right", SHIFT_SCOPE); pump()
    assert calls[-1][:2] == ("right", SHIFT_SCOPE)
    page.dialog.reject(); page.calibrate_gc()
    assert calls[-1] == (core, {"parent": page.window()})


@pytest.mark.parametrize("succeeds", [True, False])
def test_numeric_normalization_waits_for_core_completion(mapping, pump, monkeypatch, succeeds):
    page, core, _ = mapping; dialog = ActivationDialog(core, "a", page.toast, page)
    dialog.show(); pump(); pending = Future()
    original = core.settings.set_in_app_gyro_setting
    monkeypatch.setattr(core.settings, "set_in_app_gyro_setting", lambda *a, **k: pending)
    field = dialog.fields["deadzone_amount"]; field.setFocus()
    field.setText("42.5"); field.textEdited.emit("42.5")
    dialog.normalize("deadzone_amount")
    assert field.text() == "42.5" and dialog.values()["deadzone_amount"] == 15.0
    field.setText(""); field.textEdited.emit(""); dialog.normalize("deadzone_amount")
    assert field.text() == "42.5"
    if succeeds:
        original("a", "deadzone_amount", 42.5, None); pending.set_result(True)
    else:
        pending.set_exception(RuntimeError("Save failed"))
    pump()
    assert field.text() == ("42.5" if succeeds else "15.0") and not dialog.numeric_pending
    before = core.config_fake.saves; dialog.reject(); pump()
    assert core.config_fake.saves == before
