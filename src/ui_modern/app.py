"""Modern application lifecycle. Importing this module does not start hardware."""
import os
import sys
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication
from .qt_bridge import QtUiPort, call_core, gui_dispatcher
from .main_window import MainWindow
from .theme import initialize_theme, current_ui_priority, set_ui_priority
from .restart import schedule_relaunch

class ApplicationSession:
    def __init__(self, app, core_factory, *, exit_process=os._exit):
        self.app, self.exit_process = app, exit_process
        self.quitting = False; self.restart = False
        self.dispatch = gui_dispatcher()
        self.ui_port = QtUiPort()
        self.core = core_factory(self.ui_port)
        self._set_priority = set_ui_priority
        self.base_priority = current_ui_priority()
        self.window = MainWindow(self.core, apply_priority=self.apply_priority)
        self.ui_port.bind(self.window, self.core.profiles)
        self.window.quit_requested.connect(self.quit)
        app.setQuitOnLastWindowClosed(False)
    def apply_priority(self, policy):
        if policy == "above_normal":
            self._set_priority(1)
        elif policy == "restore_base" and self.base_priority is not None:
            self._set_priority(self.base_priority)
    def start(self):
        def initial(state):
            self.apply_priority("above_normal" if state.app.power_saving_mode in ("Auto", "Full") else "restore_base")
            self.window.tray.show()
            if not state.app.start_minimized:
                self.window.show()
            call_core(self.core.start, on_error=lambda exc: self.window.toast(str(exc), "error"))
        call_core(self.core.settings.state, on_result=initial)
    def quit(self, restart=False):
        if self.quitting:
            return
        self.quitting = True; self.restart = restart; self.window._closing = True
        self.window.save_placement()
        self.ui_port.cancel_all()
        self.window.dispose()
        self.window.hide(); self.window.tray.hide()
        call_core(self.core.shutdown, lambda: self.dispatch.requested.emit(self._done), on_error=lambda exc: self.window.toast(str(exc), "error"))
    def _done(self):
        self.window.bridge.close()
        self.app.quit()
        try:
            if self.restart:
                schedule_relaunch()
        finally:
            self.exit_process(0)


def build_session(app, core_factory=None, *, exit_process=os._exit):
    if core_factory is None:
        from app_core import AppCore
        core_factory = AppCore
    initialize_theme(app)
    from .resources import resource_path
    icon = QIcon(str(resource_path("resources/images/icon.ico")))
    app.setWindowIcon(icon)
    session = ApplicationSession(app, core_factory, exit_process=exit_process)
    session.window.setWindowIcon(icon); session.window.tray.setIcon(icon)
    return session


def main(argv=None, *, core_factory=None, exit_process=os._exit):
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(argv or sys.argv)
    session = build_session(app, core_factory, exit_process=exit_process)
    session.start()
    return app.exec()
