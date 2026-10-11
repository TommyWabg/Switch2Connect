from dataclasses import replace
import pytest
from app_core import events
from app_core.events import TaskState, Toast

def test_build_navigation_and_diff(window, pump):
    w, core = window
    assert w.devices.cards[1].snapshot.is_merged_pair
    assert not w.lazy_built
    for name in ("mapping", "gyro", "rumble", "profiles", "settings", "devices"):
        w.navigate(name); pump()
        assert w.stackedWidget.currentWidget() is w.pages[name]
    assert w.lazy_built == {"mapping", "gyro", "rumble", "profiles"}
    objects = dict(w.devices.cards)
    core.slots = tuple(replace(s, controllers=tuple(replace(c, battery_level='low') for c in s.controllers)) for s in core.slots)
    core.bus.publish(events.CONTROLLERS_CHANGED, core.slots); pump()
    assert w.devices.cards == objects
    assert w.devices.cards[0].battery.levels == ('low',)


def test_devices_controls(window, pump):
    w, core = window; page = w.devices
    for control, value, expected in ((page.driver, "ViGEmBus", "drivers.switch_driver"), (page.emulate, "PS4", "drivers.set_simulation_mode"), (page.layout_control, "Switch", "drivers.set_layout"), (page.auto_mode, "Inactive", "settings.set_auto_disconnect_mode")):
        control.setCurrentItem(value); pump()
        assert (expected, (value,), {}) in core.calls
    page.duration_spins[0].setValue(1); pump()
    assert ("settings.set_auto_disconnect_time", (1, 0, 15), {}) in core.calls
    page.driver_action.click(); page.gyro_button.click(); page.cards[0].ping.click(); pump()
    for name in ("drivers.on_driver_btn_clicked", "players.start_gyro_calibration", "players.ping"):
        assert any(call[0]==name for call in core.calls)
    for name, args in (("calibrate_sticks", ()), ("toggle_mag_calibration", ("Left",)), ("split", ()), ("merge_select", ()), ("set_hold_mode", ("Horizontal",)), ("toggle_gyro_side", ("Right",)), ("disconnect", ())):
        page.cards[1].action(name, *args); pump()
        expected = "calibration.start_joystick" if name=="calibrate_sticks" else "players."+name
        assert (expected, (core.slots[1].slot_token, *args), {}) in core.calls


def test_rollback_settings_and_task(window, pump):
    w, core = window; core.reject = True
    w.devices.layout_control.setCurrentItem("Switch"); pump()
    assert w.devices.layout_control.currentRouteKey() == "Xbox"
    assert w._toast is not None
    core.reject = False
    w.settings.startup.setChecked(True); w.settings.minimized.setChecked(True); w.settings.power.setCurrentItem("Auto"); pump()
    for name in ("set_run_at_startup", "set_start_minimized", "set_power_saving_mode"):
        assert any(c[0]=="settings."+name for c in core.calls)
    core.bus.publish(events.TASK_STATE, TaskState("gyro_calibration", "Gyro", True, "3 seconds")); pump()
    assert w.devices.gyro_status.text() == "3 seconds"
    assert not w.devices.gyro_button.isEnabled()
    core.bus.publish(events.TOAST, Toast("persistent", auto_close_seconds=None)); pump()
    assert w._toast.duration == -1


def test_initial_event_buffer(window, pump):
    w, core = window
    w._initializing = True
    newer = replace(core.status, text="Newer")
    w.apply_event(events.STATUS_CHANGED, newer)
    w._initial_snapshot(core.snapshot_all())
    assert "Newer" in w.status.text()


def test_undo_and_player_menu_routes(window, pump):
    from qfluentwidgets import PushButton
    w, core = window
    w.devices.layout_control.setCurrentItem("Switch"); pump()
    undo=next(b for b in w._toast.findChildren(PushButton) if b.text()=="Undo")
    undo.click(); pump()
    assert core.driver_state.abxy_mode=="Xbox"
    card=w.devices.cards[1]
    card.open_menu(); pump()
    card.menu_actions["Calibrate magnetometer · Left"].trigger()
    card.menu_actions["Gyro side Right"].trigger()
    card.merge.click()
    card.menu_actions["Disconnect"].trigger()
    pump()
    assert ("players.toggle_mag_calibration", (core.slots[1].slot_token, "Left"), {}) in core.calls
    assert ("players.toggle_gyro_side", (core.slots[1].slot_token, "Right"), {}) in core.calls
    card.menu.close()


@pytest.mark.parametrize("expired_toast", [False, True])
def test_classic_switch_confirmation(window, pump, monkeypatch, expired_toast):
    w, core=window; restarts=[]
    if expired_toast:
        from shiboken6 import delete, isValid
        monkeypatch.setattr(w, 'isActiveWindow', lambda: True)
        w.toast('Previous change', undo=lambda: None)
        old = w._toast
        old.close()
        delete(old)
        assert not isValid(old)
    w.quit_requested.connect(restarts.append)
    w.settings.classic.click(); pump()
    assert not any(c[0]=="settings.set_ui_mode" for c in core.calls)
    w.settings.confirmation.accept(); pump(lambda: bool(restarts), timeout=1)
    assert ("settings.set_ui_mode", ("classic",), {}) in core.calls
    assert restarts==[True]


def test_toast_replaces_deleted_bar(window, monkeypatch):
    from shiboken6 import delete, isValid
    w, _ = window
    monkeypatch.setattr(w, 'isActiveWindow', lambda: True)
    w.toast('First')
    old = w._toast
    old.close()
    delete(old)
    w.toast('Second')
    assert isValid(w._toast) and w._toast.content == 'Second'


def test_change_notification_failure_does_not_interrupt_completion(app, pump):
    from PySide6.QtWidgets import QWidget
    from app_core.models import SettingResult
    from ui_modern.widgets.changes import apply_change
    owner = QWidget()
    calls = []
    owner.apply_priority = lambda policy: calls.append(('priority', policy))
    owner.request_restart = lambda: calls.append('restart')

    def broken_toast(*args, **kwargs):
        calls.append('toast')
        raise RuntimeError('Internal C++ object (InfoBar) already deleted')

    owner.toast = broken_toast
    apply_change(owner, lambda: SettingResult(True, ui_priority_policy='high', restart_required=True),
                 undo=lambda: None, restart=True, after=lambda result: calls.append('after'),
                 on_finished=lambda: calls.append('finished'))
    pump(lambda: 'toast' in calls)
    assert calls == [('priority', 'high'), 'restart', 'after', 'finished', 'toast']
    owner.close()
