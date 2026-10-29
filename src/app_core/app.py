"""Composition and lifecycle of the UI-independent application."""
from concurrent.futures import Future
import logging
from threading import RLock

from . import events, lifecycle
from .bus import EventBus
from .executor import CoreExecutor, on_core, run_flow
from .runtime import AppRuntime
from .services.players import PlayerService
from .services.drivers import DriverService
from .services.esp32 import Esp32Service
from .services.profiles import ProfileService
from .services.settings import SettingsService
from .services.calibration import CalibrationService

logger = logging.getLogger(__name__)


class AppCore:
    def __init__(self, ui_port, *, config=None):
        self.ui_port = ui_port
        self.bus = EventBus()
        self.executor = self._executor = CoreExecutor()
        self._lock = RLock()
        self._start_future = self._shutdown_future = None
        try:
            self.executor.submit(self._compose, config).result()
        except BaseException:
            self.executor.shutdown()
            raise

    def _compose(self, config):
        self.runtime = AppRuntime(self.bus, self.executor, self.ui_port, config=config)
        config = self.runtime.config
        self.players = PlayerService(self.runtime)
        self.drivers = DriverService(self.bus, self.executor, self.ui_port, self.runtime, config=config)
        self.esp32 = Esp32Service(self.bus, self.executor, self.ui_port, self.runtime, config=config)
        self.profiles = ProfileService(self.bus, self.executor, self.ui_port, self.runtime,
                                       driver_switcher=self.drivers, config=config, autostart=False)
        self.settings = SettingsService(self.bus, self.executor, self.ui_port, self.runtime, config=config)
        self.calibration = CalibrationService(self.bus, self.executor, self.ui_port, self.runtime,
                                             config=config)
        self.profiles.refresh()

    def _dispatch(self, fn, completion):
        """Flatten both dispatch and operation Futures without waiting on core."""
        def adopt(value):
            if isinstance(value, Future):
                value.add_done_callback(resolved)
            elif not completion.done():
                completion.set_result(value)
        def resolved(source):
            try:
                adopt(source.result())
            except BaseException as exc:
                if not completion.done():
                    completion.set_exception(exc)
        try:
            adopt(fn() if self.executor.is_core_thread() else self.executor.submit(fn))
        except BaseException as exc:
            completion.set_exception(exc)

    def start(self):
        """Return one Future resolving to discovery startup success (bool)."""
        with self._lock:
            if self._start_future is None:
                self._start_future = Future()
                self._dispatch(self._start, self._start_future)
            return self._start_future

    def _start(self):
        if self._shutdown_future is not None:
            return False
        self.runtime.start()
        self.profiles.start()
        # Calibration owns exactly one CALIBRATION_REQUEST subscription.
        self.settings.start()
        self.esp32.start_periodic((self.drivers.refresh_wired_status,), immediate=False)
        return self.runtime.run_startup_sequence([
            self.esp32.startup_detect_and_maybe_update,
            self._driver_check,
        ])

    def _driver_check(self):
        return run_flow(self.executor, lifecycle.startup_driver_steps(
            lambda: self.drivers.startup_driver_check(initialize_keyboard=False),
            self._initialize_keyboard, self._refresh_profile_combo, lambda context: None))

    def _initialize_keyboard(self):
        import keyboard_output
        keyboard_output.initialize()

    def _refresh_profile_combo(self):
        self.profiles.refresh()
        self.bus.publish(events.PROFILE_OUTPUT_CHANGED, None)

    def shutdown(self, on_done):
        """Return one completion Future; on_done runs on core before executor stop."""
        with self._lock:
            if self._shutdown_future is None:
                self._shutdown_future = Future()
                self._dispatch(lambda: self._shutdown(on_done), self._shutdown_future)
            return self._shutdown_future

    def _shutdown(self, on_done):
        completion = Future()
        self.runtime.is_quitting = True
        if self._start_future is not None and not self._start_future.done():
            self._start_future.set_result(False)
        self.runtime.stop_periodic()
        self.esp32.stop_periodic()
        self.profiles.stop()
        self.settings.close()
        self.calibration.close()
        self.esp32.close()
        self.drivers.close()
        self.bus.publish(events.QUIT_REQUESTED)
        def done():
            try:
                on_done()
            except BaseException as exc:
                completion.set_exception(exc)
            else:
                completion.set_result(None)
            finally:
                self.executor.shutdown(wait=False)
        self.runtime.shutdown(done)
        return completion

    @on_core
    def snapshot_all(self):
        """Current state of every service; runs on core after queued constructor refreshes.

        Services assign their state before publishing it, so reading on core never
        returns something older than an event the UI has already received."""
        return {
            events.CONTROLLERS_CHANGED: self.runtime.slots,
            events.WIRED_STATE_CHANGED: self.runtime.wired_state,
            events.STATUS_CHANGED: self.esp32.status(),
            events.DRIVER_STATE_CHANGED: self.drivers.state(),
            events.ESP32_STATE_CHANGED: self.esp32.state(),
            events.PROFILES_CHANGED: self.profiles.state(),
            events.SETTINGS_CHANGED: self.settings.state(),
            events.CALIBRATION_SESSION: self.calibration.state(),
            events.ESP32_FIRMWARE_SESSION: self.esp32.firmware_session(),
            events.ESP32_PORTS_CHANGED: self.esp32.ports(),
        }
