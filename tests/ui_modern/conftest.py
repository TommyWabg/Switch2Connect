import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import time
import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QFontDatabase, QFont
from PySide6.QtWidgets import QApplication
from app_core.bus import EventBus
from app_core import events
from app_core.models import (ControllerInfo, SlotInfo, DriverState, StatusInfo, ProfilesState, SettingsState,
    DeadzoneView, GyroView, RumbleView, AutoDisconnectView, AppSettingsView, SettingResult, ActionResult, ProfileResult)


def ready(value):
    future = Future(); future.set_result(value); return future


class FakeService:
    def __init__(self, core, name):
        self.core, self.name = core, name
    def state(self):
        if self.name == "calibration":
            from app_core.models import CalibrationState
            return CalibrationState()
        return self.core.driver_state if self.name == "drivers" else self.core.settings_state if self.name == "settings" else self.core.profiles_state
    @contextmanager
    def suspend_focus_polling(self):
        self.core.calls.append(("profiles.suspend_enter", (), {}))
        yield
        self.core.calls.append(("profiles.suspend_exit", (), {}))
    def mapping(self, scope=None):
        from app_core.models import MappingView
        return MappingView(scope, ())
    def ir_sensor(self, side, scope=None):
        from app_core.models import IrSensorView
        return IrSensorView(side, scope, 'Default', 'Default (IR Mouse)', None, 1, (), (), (), ())
    def __getattr__(self, method):
        def call(*args, **kwargs):
            self.core.calls.append((f"{self.name}.{method}", args, kwargs))
            if self.core.reject:
                return ready(ready(SettingResult(False, "rolled_back")))
            if self.name == "drivers":
                key = {"switch_driver": "driver_type", "set_simulation_mode": "simulation_mode", "set_layout": "abxy_mode"}.get(method)
                if key:
                    self.core.driver_state = replace(self.core.driver_state, **{key: args[0]})
                result = ActionResult(True)
            elif self.name == "settings":
                if method == "set_auto_disconnect_mode":
                    self.core.settings_state = replace(self.core.settings_state, auto_disconnect=replace(self.core.settings_state.auto_disconnect, mode=args[0]))
                elif method == "set_auto_disconnect_time":
                    self.core.settings_state = replace(self.core.settings_state, auto_disconnect=AutoDisconnectView(self.core.settings_state.auto_disconnect.mode, *args))
                else:
                    key = {"set_run_at_startup": "run_at_startup", "set_start_minimized": "start_minimized", "set_power_saving_mode": "power_saving_mode", "set_ui_mode": "ui_mode"}.get(method)
                    if key:
                        self.core.settings_state = replace(self.core.settings_state, app=replace(self.core.settings_state.app, **{key: args[0]}))
                result = SettingResult(True, restart_required=method == "set_ui_mode", ui_priority_policy="above_normal" if method == "set_power_saving_mode" else None)
            elif self.name == "profiles":
                result = ProfileResult(True)
            else:
                result = True
            return ready(ready(result))
        return call

class FakeCore:
    def __init__(self, ui_port=None, scenario="zero"):
        self.ui_port = ui_port; self.bus = EventBus(); self.calls = []; self.reject = False
        for name in ("settings", "drivers", "players", "profiles", "calibration"):
            setattr(self, name, FakeService(self, name))
        controllers = [ControllerInfo("pro2", "Switch 2 Pro Controller", "A4:38:CC:12:34:56", "ble", battery_level="high")]
        if scenario == "three":
            controllers += [ControllerInfo("joycon2left", "Joy-Con 2 (L)", "A4:38:CC:12:34:57", "esp32", battery_level="medium"), ControllerInfo("joycon2right", "Joy-Con 2 (R)", "A4:38:CC:12:34:58", "esp32", battery_level="low"), ControllerInfo("gamecube", "GameCube Controller", "USB\\VID_057E&PID_2073", "wired")]
        count = 0 if scenario == "zero" else 3 if scenario == "three" else 1
        self.slots = tuple(SlotInfo(i, i+1, leds=tuple(j==i for j in range(4)), controllers=tuple(controllers[1:3]) if i==1 and count==3 else (controllers[0 if i==0 else 3],) if i<count else (), is_empty=i>=count, is_merged_pair=i==1 and count==3, slot_token=(i, 100+i) if i<count else None, split_available=i==1 and count==3, gyro_side_available=i==1 and count==3, ping_available=i<count) for i in range(4))
        self.driver_state = DriverState(driver_type="WinUHid", simulation_mode="Xbox One", abxy_mode="Xbox", driver_options=("WinUHid", "ViGEmBus", "USBIP"), mode_options=(("WinUHid", ("Xbox One", "PS4", "PS5")), ("ViGEmBus", ("Xbox360", "PS4")), ("USBIP", ("Switch1", "Switch2", "PS5"))), needs_attention=scenario=="install", pending_check=False, install_states=(("WinUHid", "absent" if scenario=="install" else "healthy"), ("ViGEmBus", "healthy"), ("USBIP", "healthy")))
        self.status = StatusInfo("ESP32-S3" if scenario=="three" else "System BLE", "Ready", "ok", "#107c10")
        self.profiles_state = ProfilesState(("Default", "Gaming"), "Default")
        rumble = RumbleView("Default", (), False, 1, 1, 0, False, False, False, False, False, False, 0, 0, ())
        from app_core.models import GcTriggerView, Option
        self.settings_state = SettingsState((), (), DeadzoneView(()), tuple(GcTriggerView(scope, "Hair Trigger", (Option("Hair Trigger", "Hair trigger"),)) for scope in (None, "in_app_gyro_mode_mappings")), (), GyroView(()), rumble, AutoDisconnectView("OFF", 0, 0, 15), AppSettingsView(False, False, "Off", "modern"))
    def snapshot_all(self):
        return {events.CONTROLLERS_CHANGED: self.slots, events.DRIVER_STATE_CHANGED: self.driver_state, events.STATUS_CHANGED: self.status, events.PROFILES_CHANGED: self.profiles_state, events.SETTINGS_CHANGED: self.settings_state}
    def start(self):
        self.calls.append(("start", (), {})); return ready(True)
    def shutdown(self, on_done):
        self.calls.append(("shutdown", (), {})); on_done(); return ready(None)

@pytest.fixture(scope="session")
def app():
    app = QApplication.instance() or QApplication([])
    # Windows offscreen plugin does not enumerate system fonts automatically.
    for name in ("segoeui.ttf", "segoeuib.ttf", "segoeuil.ttf", "seguisb.ttf", "seguisym.ttf"):
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))
    app.setFont(QFont("Segoe UI", 10))
    app.setQuitOnLastWindowClosed(False)
    return app

@pytest.fixture(autouse=True)
def isolated_preferences(monkeypatch, core_tmp_path):
    from ui_modern import theme
    from qfluentwidgets import qrouter
    qrouter.history.clear()
    qrouter.stackHistories.clear()
    prefs = QSettings(str(core_tmp_path / "ui.ini"), QSettings.IniFormat)
    monkeypatch.setattr(theme, "preferences", lambda: prefs)
    yield prefs

@pytest.fixture
def pump(app):
    def run(predicate=lambda: False, timeout=0.1):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            if predicate():
                return
            time.sleep(0.001)
    return run

@pytest.fixture
def window(app, pump):
    from ui_modern.main_window import MainWindow
    core = FakeCore(scenario="three")
    window = MainWindow(core); window.setMicaEffectEnabled(False); window.show(); pump()
    yield window, core
    window.dispose(); window.tray.hide(); window.hide(); window.deleteLater(); pump()
