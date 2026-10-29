"""Install once in MainWindow; subscribes through the shared Qt bus bridge."""
from PySide6.QtCore import QObject
from .notifications import NotificationOverlay, ProfileSelectionOverlay
from ..qt_bridge import BusBridge
from ..qt_bridge import call_core
from ..wizards.calibration import JoystickCalibrationWizard, GcTriggerWizard, _dialogs


class OverlayController(QObject):
    def __init__(self, core, window, *, route_toast=None):
        super().__init__(window)
        self.core, self.window = core, window
        self.notification = NotificationOverlay(window)
        self.profile = ProfileSelectionOverlay(window)
        self.route_toast = route_toast or (lambda event: self.notification.present(event))
        self.wizards = {}
        self._active_profile = core.profiles.state().active
        self.bridge = BusBridge(core.bus, self)
        if route_toast is None:
            self.bridge.toast.connect(self.route_toast)
        self.bridge.profile_overlay.connect(self.profile_event)
        self.bridge.calibration_session.connect(self.session_event)
        self.bridge.profiles_changed.connect(self.profile_changed)
        self.bridge.shutdown_complete.connect(lambda _: self.close())
        for session in core.calibration.state().sessions:
            self.session_event(session)

    def profile_event(self, event):
        if event.action == "notification":
            self.route_toast(event)
        else:
            self.profile.apply_event(event)

    def session_event(self, session):
        for cached in self.core.calibration.state().sessions:
            if cached.session_id != session.session_id:
                self._show_session(cached)
        self._show_session(session)

    def _show_session(self, session):
        if not session.active:
            return
        # A Devices/Mapping entry point may already have opened this session.
        existing = next((d for d in _dialogs if not d._closed and
                         (d.session_id == session.session_id or d.session_id is None and d.kind == session.kind)), None)
        if existing:
            if existing.session_id is None:
                existing.session_id = session.session_id
            existing.apply_session(session); return
        if session.session_id in self.wizards:
            return
        cls = JoystickCalibrationWizard if session.kind == "joystick" else GcTriggerWizard
        dialog = cls(self.core, session.session_id, self.window)
        self.wizards[session.session_id] = dialog
        dialog.finished.connect(lambda _: self.wizards.pop(session.session_id, None))
        dialog.show()

    def profile_changed(self, state):
        changed = state.active != self._active_profile
        self._active_profile = state.active
        if changed:
            sessions = [session for session in self.core.calibration.state().sessions
                        if session.kind == "joystick" and session.active]
            for session in sessions:
                call_core(self.core.calibration.cancel, session.session_id, reason="profile_changed")
            if sessions:
                self.notification.dismiss()

    def close(self):
        self.bridge.close(); self.notification.close(); self.profile.close()
        for dialog in tuple(self.wizards.values()):
            dialog.reject()


def install_overlays(core, window, *, route_toast=None):
    """Attach desktop overlays to the main window once."""
    if not hasattr(window, "_overlays"):
        window._overlays = OverlayController(core, window, route_toast=route_toast)
    return window._overlays
