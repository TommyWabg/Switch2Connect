from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QAction, QIcon, QGuiApplication, QCursor
from shiboken6 import isValid
from PySide6.QtWidgets import QWidget, QVBoxLayout, QSystemTrayIcon, QMenu
from qfluentwidgets import (FluentWindow, FluentIcon, NavigationItemPosition, BodyLabel,
                           PushButton, TransparentToolButton, RoundMenu, InfoBar, InfoBarPosition, IndeterminateProgressRing, qconfig, isDarkTheme)
from app_core import events
from app_core.models import SettingsState
from .qt_bridge import BusBridge, call_core
from .pages.devices import DevicesPage
from .pages.settings_full import SettingsPage
from .overlays.controller import install_overlays
from PySide6.QtWidgets import QApplication, QDialog
from . import theme
from .resources import resource_path

class MainWindow(FluentWindow):
    quit_requested = Signal(bool)
    def __init__(self, core, *, apply_priority=lambda policy: None):
        super().__init__()
        self.core = core
        self.setWindowTitle("Switch 2 Connect"); self.resize(1100, 760); self.setMinimumSize(960, 640)
        self.setWindowIcon(QIcon(str(resource_path("resources/images/icon.ico"))))
        self.titleBar.iconLabel.setFixedSize(16, 16)
        self._initializing = True; self._buffer = []; self._toast = None; self._closing = False
        self._tasks = {}; self.pages = {}; self.lazy_built = set()
        self.bridge = BusBridge(core.bus, self)
        self.bridge.event.connect(self.apply_event)
        self.status = BodyLabel("Connecting…", self.titleBar)
        self.profile = PushButton("Profile", self.titleBar)
        self.task_indicator = IndeterminateProgressRing(self.titleBar); self.task_indicator.setFixedSize(18, 18); self.task_indicator.hide()
        self.window_menu = TransparentToolButton(FluentIcon.MORE, self.titleBar); self.window_menu.setAccessibleName("More")
        for widget in (self.status, self.profile, self.task_indicator, self.window_menu):
            self.titleBar.hBoxLayout.insertWidget(self.titleBar.hBoxLayout.count()-1, widget, 0, Qt.AlignVCenter)
        qconfig.themeChangedFinished.connect(self._refresh_status)
        self.profile.clicked.connect(self.profile_menu); self.window_menu.clicked.connect(self.show_window_menu)
        self.devices = DevicesPage(core, self.toast, self)
        self.settings = SettingsPage(core, self.toast, lambda: self.quit_requested.emit(True), apply_priority, self, hide_to_tray=self.hide_to_tray)
        self.pages["devices"] = self.devices; self.pages["settings"] = self.settings
        for name, icon in (("devices", FluentIcon.GAME), ("mapping", FluentIcon.EDIT), ("gyro", FluentIcon.SYNC),
                           ("rumble", FluentIcon.VOLUME), ("profiles", FluentIcon.PEOPLE), ("settings", FluentIcon.SETTING)):
            page = self.pages.get(name)
            if page is None:
                page = QWidget(self); page.setObjectName(name); self.pages[name] = page
            self.addSubInterface(page, icon, name.title(), position=NavigationItemPosition.BOTTOM if name in ("profiles", "settings") else NavigationItemPosition.TOP)
        self.stackedWidget.currentChanged.connect(self._build_current)
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("Switch 2 Connect")
        menu = QMenu(); show = menu.addAction("Show"); menu.setDefaultAction(show)
        show.triggered.connect(self.show_from_tray)
        menu.addAction("Exit", lambda: self.quit_requested.emit(False)); self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_from_tray() if reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger) else None)
        self.overlays = install_overlays(core, self, route_toast=lambda event:
            self.toast(event.message, event.kind, event.title, event.auto_close_seconds))
        geometry = theme.preferences().value("geometry")
        restored = geometry is not None and self.restoreGeometry(geometry)
        if not restored or not any(self.frameGeometry().intersects(screen.availableGeometry()) for screen in QGuiApplication.screens()):
            self.resize(1100, 760)
            screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
            frame = self.frameGeometry()
            frame.moveCenter(screen.availableGeometry().center())
            self.move(frame.topLeft())
        call_core(core.snapshot_all, on_result=self._initial_snapshot, on_error=lambda exc: self.toast(str(exc), "error"))
    def _initial_snapshot(self, snapshot):
        for topic, value in snapshot.items():
            self._apply(topic, value)
        self._initializing = False
        pending, self._buffer = self._buffer, []
        for topic, value in pending:
            self._apply(topic, value)
    def apply_event(self, topic, value):
        if self._initializing:
            self._buffer.append((topic, value))
        else:
            self._apply(topic, value)
    def _apply(self, topic, value):
        if topic == events.CONTROLLERS_CHANGED:
            self.devices.apply_slots(value)
        elif topic == events.DRIVER_STATE_CHANGED:
            self.devices.apply_driver(value)
        elif topic == events.SETTINGS_CHANGED:
            if isinstance(value, SettingsState):
                self.devices.apply_settings(value); self.settings.apply_settings(value)
            else:
                call_core(self.core.settings.state, on_result=lambda state: self._apply(topic, state))
        elif topic == events.STATUS_CHANGED:
            self.devices.apply_status(value)
            self._status_state = value
            self._refresh_status()
        elif topic == events.PROFILES_CHANGED:
            self.profiles_state = value; self.profile.setText(value.active or "Profile")
        elif topic == events.TASK_STATE:
            if value.active:
                self._tasks[value.task_id] = value
            else:
                self._tasks.pop(value.task_id, None)
            self.task_indicator.setVisible(bool(self._tasks))
            self.task_indicator.setToolTip("\n".join(f"{task.title}: {task.message}" for task in self._tasks.values()))
            self.devices.task(value)
        elif topic == events.TOAST:
            self.toast(value.message, value.kind, value.title, value.auto_close_seconds)
        elif topic == events.QUIT_REQUESTED:
            if not self._closing:
                self.quit_requested.emit(False)
    def _refresh_status(self):
        state = getattr(self, "_status_state", None)
        if state is None:
            return
        colors = {"ok": "#6ccb5f", "warn": "#fce100", "error": "#ff99a4"} if isDarkTheme() else {"ok": "#0f7b0f", "warn": "#9d5d00", "error": "#c42b1c"}
        self.status.setText(f"● {state.text} · {state.connection_route}")
        self.status.setStyleSheet(f"color: {colors.get(state.level, state.color)}; padding: 4px 8px; border-radius: 10px; background-color: rgba(128, 128, 128, 20);")
    def _build_current(self, index):
        page = self.stackedWidget.widget(index); name = page.objectName()
        if name in ("devices", "settings") or name in self.lazy_built:
            return
        self.lazy_built.add(name)
        from importlib import import_module
        factory = import_module(f"ui_modern.pages.{name}").create_page
        box = QVBoxLayout(page); box.setContentsMargins(0, 0, 0, 0)
        page.content_page = factory(self.core, self.toast, page)
        box.addWidget(page.content_page)
    def navigate(self, name):
        self.switchTo(self.pages[name])
    def dispose(self):
        for dialog in QApplication.topLevelWidgets():
            if isinstance(dialog, QDialog) and self.isAncestorOf(dialog) and dialog.isVisible():
                dialog.reject()
        self.overlays.close()
        self.bridge.close()
        for page in self.pages.values():
            content = getattr(page, "content_page", page)
            if hasattr(content, "finish_edits"):
                content.finish_edits()
            if hasattr(content, "bridge"):
                content.bridge.close()

    def toast(self, message, kind="info", title="", seconds=4, undo=None):
        if self._closing:
            return
        if hasattr(self, "overlays") and (not self.isVisible() or not self.isActiveWindow()):
            self.overlays.notification.present(events.Toast(message, kind, title, seconds))
            return
        if self._toast is not None and isValid(self._toast):
            self._toast.close()
        factory = getattr(InfoBar, kind, InfoBar.info)
        self._toast = factory(title, message, duration=-1 if seconds is None else round(seconds * 1000), position=InfoBarPosition.TOP, parent=self)
        if undo:
            bar = self._toast
            button = PushButton("Undo", bar)
            def restore():
                if isValid(bar):
                    bar.close()
                undo()
            button.clicked.connect(restore)
            bar.addWidget(button)
    def profile_menu(self):
        menu = RoundMenu(parent=self)
        state = getattr(self, "profiles_state", None)
        for name in state.names if state else ():
            action = QAction(name, menu); action.setCheckable(True); action.setChecked(name == state.active)
            action.triggered.connect(lambda checked=False, name=name: call_core(self.core.profiles.select, name, on_result=self._profile_result, on_error=lambda exc: self.toast(str(exc), "error")))
            menu.addAction(action)
        menu.addSeparator(); action = QAction("Manage profiles…", menu)
        action.triggered.connect(lambda: self.navigate("profiles")); menu.addAction(action)
        self._profile_menu = menu; menu.exec(self.profile.mapToGlobal(self.profile.rect().bottomLeft()))
    def _profile_result(self, result):
        if not result.ok:
            self.toast(result.reason or "Could not switch profile.", "warning")
    def show_window_menu(self):
        menu = RoundMenu(parent=self); action = QAction("Hide to tray", menu)
        action.triggered.connect(self.hide_to_tray); menu.addAction(action)
        self._window_menu = menu; menu.exec(self.window_menu.mapToGlobal(self.window_menu.rect().bottomLeft()))
    def save_placement(self):
        if self.isVisible() and not self.isMinimized():
            if any(self.frameGeometry().intersects(s.availableGeometry()) for s in QGuiApplication.screens()):
                theme.preferences().setValue("geometry", self.saveGeometry())

    def hide_to_tray(self):
        self.save_placement()
        self.tray.show(); self.hide()
    def show_from_tray(self):
        self.showNormal(); self.raise_(); self.activateWindow()
    def closeEvent(self, event):
        event.ignore()
        if not self._closing:
            if theme.preferences().value("close_behavior", "Quit") == "Hide to tray":
                self.hide_to_tray()
            else:
                self.quit_requested.emit(False)
