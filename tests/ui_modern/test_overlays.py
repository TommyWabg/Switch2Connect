from dataclasses import replace
import threading
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget
from PySide6.QtTest import QTest
from app_core import events
from app_core.models import CalibrationSession, CalibrationState, StickProgress, SettingResult
from conftest import FakeCore, FakeService, ready
from ui_modern.overlays.notifications import NotificationOverlay, ProfileSelectionOverlay
from ui_modern.overlays.controller import install_overlays
from ui_modern.wizards.calibration import (JoystickCalibrationWizard, open_joystick_wizard,
    open_gc_trigger_wizard)


STICK = StickProgress("left", "left", "Left stick", 4, 0, False, False, "rotate", (2600, 1800))
JOYSTICK = CalibrationSession("joy-1", "joystick", (0, 100), "rotate", "Joysticks Calibration",
                              "Fully rotate each stick for 10 seconds.", sticks=(STICK,))
GC = CalibrationSession("gc-1", "gc_trigger", (0, 100), "sampling", "Step 1: Base State",
                        "Release both triggers completely.", step=0, trigger_values=(36, 37))


class CalibrationFake(FakeService):
    def state(self):
        return CalibrationState(tuple(self.core.sessions.values()))

    def __getattr__(self, method):
        def call(*args, **kwargs):
            self.core.calls.append(("calibration." + method, args, kwargs))
            if method.startswith("start_") and method in ("start_joystick", "start_gc_trigger"):
                session = JOYSTICK if method == "start_joystick" else GC
                self.core.sessions[session.session_id] = session
                self.core.bus.publish(events.CALIBRATION_SESSION, session)
                return ready(ready(session.session_id))
            if method == "cancel":
                session = replace(self.core.sessions[args[0]], active=False, reason="cancelled")
                self.core.sessions[args[0]] = session
                self.core.bus.publish(events.CALIBRATION_SESSION, session)
            return ready(SettingResult(True))
        return call


class T11Core(FakeCore):
    def __init__(self):
        super().__init__(scenario="three")
        self.sessions = {"joy-1": JOYSTICK, "gc-1": GC}
        self.calibration = CalibrationFake(self, "calibration")


def names(core):
    return [name for name, args, kwargs in core.calls]


def test_notification_replacement_and_monitor(app, pump):
    overlay = NotificationOverlay()
    sentinel = QWidget(); sentinel.show(); sentinel.activateWindow(); pump()
    active = app.activeWindow()
    overlay.present(events.Toast("First", "success", "Calibrated", .01))
    overlay.present(events.Toast("Second", "error", "Disconnected", None))
    pump(timeout=.25)
    assert overlay.isVisible() and overlay.message.text() == "Second"
    assert not overlay.dismiss_timer.isActive()
    assert overlay.kind == "error"
    assert overlay.grab().toImage().pixelColor(2, overlay.height() // 2).name() == "#c42b1c"
    # The offscreen backend activates every shown top-level regardless of flags.
    if app.platformName() != "offscreen":
        assert app.activeWindow() is active
    assert overlay.testAttribute(Qt.WA_ShowWithoutActivating)
    assert overlay.windowFlags() & Qt.WindowDoesNotAcceptFocus
    assert overlay.screen().availableGeometry().contains(overlay.geometry())
    overlay.present(events.Toast("Done", auto_close_seconds=.01)); pump(timeout=.25)
    assert not overlay.isVisible()
    overlay.close(); sentinel.close()


def test_profile_is_pure_view(app, pump):
    overlay = ProfileSelectionOverlay()
    for layout, hint in (("Switch", "A Confirm · B Cancel"), ("Xbox", "B Confirm · A Cancel")):
        overlay.apply_event(events.ProfileOverlayEvent("show", "Default", "Gaming", "Racing", layout, manual=True))
        assert hint in overlay.hints.text() and overlay.manual_active
        current = overlay.current
        overlay.apply_event(events.ProfileOverlayEvent("update", "Gaming", "Racing", "Default", layout, manual=True))
        assert overlay.current is current and overlay.current.text() == "Racing"
    overlay.apply_event(events.ProfileOverlayEvent("cancel")); pump(timeout=.2)
    assert not overlay.isVisible() and not overlay.manual_active
    overlay.close()


def test_joystick_progress_done_cancel_and_shortcuts(app, pump):
    core = T11Core(); dialog = JoystickCalibrationWizard(core, "joy-1"); dialog.show(); pump()
    plot, bar, counter, row = dialog.sticks["left"]
    assert bar.value() == 40 and not dialog.primary.isEnabled()
    updated = replace(JOYSTICK, phase="release", sticks=(replace(STICK, release_elapsed=1.5),))
    core.bus.publish(events.CALIBRATION_SESSION, updated); pump()
    assert bar.value() == 50 and plot is dialog.sticks["left"][0]
    done = replace(updated, completed=True, phase="done")
    core.bus.publish(events.CALIBRATION_SESSION, done); pump()
    assert dialog.auto_close.isActive() and dialog.auto_close.interval() == 3000
    QTest.keyClick(dialog, Qt.Key_Return); pump()
    assert not dialog.isVisible() and "calibration.cancel" not in names(core)
    dialog = JoystickCalibrationWizard(core, "joy-1"); dialog.show(); pump()
    QTest.keyClick(dialog, Qt.Key_Escape); pump()
    assert ("calibration.cancel", ("joy-1",), {}) in core.calls


def test_wizard_entry_points_and_gc_five_steps(app, pump):
    core = T11Core()
    joy = open_joystick_wizard(core, (0, 100)); pump()
    assert ("calibration.start_joystick", ((0, 100),), {}) in core.calls
    gc = open_gc_trigger_wizard(core, (0, 100)); pump()
    assert ("calibration.start_gc_trigger", ((0, 100),), {}) in core.calls
    for step in range(5):
        gc.apply_session(replace(GC, step=step, next_label="Finish" if step == 4 else "Next", trigger_values=(step * 50, 36)))
        assert gc.primary.text() == ("Finish" if step == 4 else "Next")
        gc.primary.click(); pump()
    assert names(core).count("calibration.next") == 5
    assert gc.bars[0].value() == 200
    core.bus.publish(events.CALIBRATION_SESSION, replace(GC, active=False, reason="disconnected")); pump()
    assert not gc.isVisible()
    joy.reject(); pump()


def test_overlay_install_events_worker_and_no_duplicate(app, pump):
    core = T11Core(); core.sessions = {}
    window = QWidget()
    controller = install_overlays(core, window)
    assert install_overlays(core, window) is controller
    joy = open_joystick_wizard(core, (0, 100), window); pump()
    assert len(controller.wizards) == 0
    assert joy.session_id == "joy-1"
    core.sessions["gc-1"] = GC
    worker = threading.Thread(target=lambda: core.bus.publish(events.CALIBRATION_SESSION, GC))
    worker.start(); worker.join(); pump()
    assert "gc-1" in controller.wizards
    core.bus.publish(events.TOAST, events.Toast("Hidden window notification", auto_close_seconds=3)); pump()
    assert controller.notification.isVisible() and not window.isVisible()
    controller.close(); joy.reject(); pump(); window.close()


def test_coalesced_sessions_and_profile_switch_cancel(app, pump):
    core = T11Core(); window = QWidget()
    controller = install_overlays(core, window); pump()
    assert set(controller.wizards) == {"joy-1", "gc-1"}
    core.sessions["joy-1"] = replace(JOYSTICK, sticks=(replace(STICK, rotate_elapsed=8),))
    core.bus.publish(events.CALIBRATION_SESSION, core.sessions["joy-1"])
    core.bus.publish(events.CALIBRATION_SESSION, GC); pump()
    assert controller.wizards["joy-1"].sticks["left"][1].value() == 80
    core.bus.publish(events.PROFILES_CHANGED, replace(core.profiles_state, active="Gaming")); pump()
    assert ("calibration.cancel", ("joy-1",), {"reason": "profile_changed"}) in core.calls
    assert "joy-1" not in controller.wizards and "gc-1" in controller.wizards
    controller.close(); pump(); window.close()
