"""Headless lifecycle port of ControllerWindow; hardware imports stay lazy."""
from . import lifecycle
from concurrent.futures import Future
import logging
import queue
import threading

from . import events
from .executor import on_core
from .snapshots import build_slots, wired_state

logger = logging.getLogger(__name__)


class AppRuntime:
    def __init__(self, bus, executor, ui_port, *, suspend_settle=1.0,
                 resume_delay=7.0, disconnect_spacing=0.3,
                 shutdown_settle=3.5, shutdown_timeout=20.0,
                 discoverer_join_timeout=5.0, wired_debounce=0.5, config=None):
        self.bus, self._executor, self.ui_port = bus, executor, ui_port
        self._config_override = config
        self.current_controllers = []  # Only read/mutate on the core thread.
        self.pending_merge_vc_index = None
        self.quit_event = threading.Event()
        self.discoverer_thread = None
        self.discoverer_callback = self._discoverer_callback
        self.is_quitting = self.is_cleaning_up = False
        self._is_restarting_discovery = self._started = self._startup_detection_done = False
        self._callback_lock = threading.Lock()
        self._callback_pending = False
        self._latest_vcs = []
        self._profile_handlers = {}
        self._timers = []
        self._wired_device_change_after_id = None
        self.wired_device_event_queue = queue.Queue()
        self.power_listener = self.wired_device_listener = None
        self.suspend_settle, self.resume_delay = suspend_settle, resume_delay
        self.disconnect_spacing, self.shutdown_settle = disconnect_spacing, shutdown_settle
        self.shutdown_timeout = shutdown_timeout
        self.discoverer_join_timeout = min(5.0, discoverer_join_timeout)
        self.wired_debounce = wired_debounce
        self.wired_state = wired_state([])
        from .models import SlotInfo
        self.slots = tuple(SlotInfo(i, i + 1) for i in range(4))

    @property
    def config(self):
        if self._config_override is not None:
            return self._config_override
        from config import CONFIG
        return CONFIG

    @on_core
    def start(self):
        if self._started or self.is_quitting:
            return
        self._started = True
        from .win_listeners import PowerListener, WiredDeviceChangeListener
        self.power_listener = PowerListener(self.handle_power_event)
        self.wired_device_listener = WiredDeviceChangeListener(self.wired_device_event_queue)
        self._register_callbacks()
        self.power_listener.start()
        try:
            self.wired_device_listener.start()
            self._timers.append(self._executor.call_every(0.25, self.poll_wired_device_events))
        except Exception:
            logger.debug("Failed to start wired device listener", exc_info=True)
        self._timers.append(self._executor.call_every(300, self._battery_refresh))

    def _discoverer_callback(self, vcs):
        # Multiple discovery notifications queued before the core claims this job
        # collapse to the newest list. Never expose the producer's mutable list.
        with self._callback_lock:
            if self.is_quitting:
                return
            self._latest_vcs = list(vcs)
            if self._callback_pending:
                return
            self._callback_pending = True
        self._executor.submit(self._drain_controllers)

    def _drain_controllers(self):
        with self._callback_lock:
            vcs = self._latest_vcs
            self._callback_pending = False
        if not self.is_quitting:
            self.update_controllers(vcs)

    @on_core
    def update_controllers(self, vcs):
        self.current_controllers = list(vcs)
        self.refresh_snapshots()

    @on_core
    def refresh_snapshots(self):
        if self.is_quitting:
            return
        self.wired_state = wired_state(self.current_controllers)
        # Fast path also refreshes the header through DriverService's wired subscription.
        self.bus.publish(events.WIRED_STATE_CHANGED, self.wired_state)
        self.slots = tuple(build_slots(
            self.current_controllers, self.config, self.pending_merge_vc_index,
            registry=self.controller_registry()))
        self.bus.publish(events.CONTROLLERS_CHANGED, self.slots)

    def controller_registry(self):
        from discoverer import VIRTUAL_CONTROLLERS
        return VIRTUAL_CONTROLLERS

    def _battery_refresh(self):
        lifecycle.battery_refresh(self, lambda controllers: self.refresh_snapshots())

    @on_core
    def stop_discoverer(self):
        lifecycle.stop_discoverer(self, self.discoverer_join_timeout)
    @on_core
    def start_discoverer(self, startup_bridge_context=None):
        lifecycle.start_discoverer(self, startup_bridge_context, self.discoverer_join_timeout)
    def is_discoverer_running(self):
        return bool(self.discoverer_thread and self.discoverer_thread.is_alive())

    @on_core
    def run_startup_sequence(self, pre_discovery_steps):
        """Run ordered zero-argument steps on core, then launch discovery once.

        A step may return a Future: its completion is marshalled back to core,
        without blocking core (even if the Future needs core work to finish).
        False aborts; True/None continue; any other result becomes the latest
        startup bridge context (normally {'status': status, 'observed_mono': t}).
        Futures resolve to the same values. Exceptions abort and reach the
        returned completion Future. The driver and ESP32 services own detection, firmware
        delays, driver checks and keyboard initialization; they supply closures for those steps.
        """
        completion = Future()
        if self._startup_detection_done or self.is_quitting:
            completion.set_result(False)
            return completion
        self._startup_detection_done = True
        steps = iter(pre_discovery_steps)
        def advance(value=None, context=None):
            if self.is_quitting or value is False:
                completion.set_result(False)
                return
            if value is not None and value is not True:
                context = value
            try:
                step = next(steps, None)
                if step is None:
                    self.start_discoverer(context)
                    completion.set_result(True)
                    return
                result = step()
                if isinstance(result, Future):
                    def finished(f):
                        if self.is_quitting:
                            if not completion.done():
                                completion.set_result(False)
                            return
                        def apply():
                            try:
                                advance(f.result(), context)
                            except Exception as exc:
                                if not completion.done():
                                    completion.set_exception(exc)
                        try:
                            self._executor.submit(apply)
                        except RuntimeError:
                            if not completion.done():
                                completion.set_result(False)
                    result.add_done_callback(finished)
                else:
                    advance(result, context)
            except Exception as exc:
                completion.set_exception(exc)
        advance()
        return completion

    @on_core
    def set_profile_handlers(self, *, cycle=None, switch=None, nav=None, confirm=None, cancel=None):
        """Profile hotkey hooks: accept the classic utils callback arguments, run on core."""
        self._profile_handlers = dict(cycle=cycle, switch=switch, nav=nav, confirm=confirm, cancel=cancel)

    @on_core
    def _profile_request(self, name, *args, **kwargs):
        self.bus.publish(events.PROFILE_OVERLAY, events.ProfileOverlayEvent(
            "request", request=name, direction=args[0] if name == "nav" and args else None,
            current=args[0] if name == "switch" and args else None))
        handler = self._profile_handlers.get(name)
        if handler:
            return handler(*args, **kwargs)
        logger.info("Profile handler %s is not installed", name)

    def _register_callbacks(self):
        import utils
        for attr, name in (("change_profile_callback", "cycle"), ("switch_profile_callback", "switch"),
                           ("profile_nav_callback", "nav"), ("profile_confirm_callback", "confirm"),
                           ("profile_cancel_callback", "cancel")):
            setattr(utils, attr, lambda *a, _name=name, **kw: self._profile_request(_name, *a, **kw))
        utils.force_ui_update_callback = self.refresh_snapshots
        utils.show_notification_callback = self.show_notification
        utils.joystick_calibration_callback = lambda vc: self._calibration_request(vc, "start")
        utils.joystick_calibration_cancel_callback = lambda vc: self._calibration_request(vc, "cancel")

    @on_core
    def _calibration_request(self, vc, action):
        self.bus.publish(events.CALIBRATION_REQUEST, events.CalibrationRequest(vc.player_number - 1, action))

    @on_core
    def show_notification(self, title, message):
        lower = message.lower()
        if any(word in lower for word in ("started", "progress", "stationary")):
            kind = "warning"
        elif "complete" in lower or "success" in lower:
            kind = "success"
        elif "cancelled" in lower:
            kind = "error"
        else:
            kind = "info"
        # Gyro completion stays visible: it carries instructions for Mag start.
        duration = 3.0 if ("magnetometer calibration complete" in lower or "cancelled" in lower or "profile" in title.lower()) else None
        self.bus.publish(events.TOAST, events.Toast(message, kind, title, duration))

    @on_core
    def _schedule_wired_device_change_rescan(self, reason="device_arrival", candidate_path=None):
        def rescan(reason, candidate_path=None):
            if not self.is_quitting:
                from discoverer import request_wired_rescan
                request_wired_rescan(reason, candidate_path=candidate_path)
        lifecycle.schedule_wired_rescan(self, self.config,
            lambda ms, callback: self._executor.call_later(ms / 1000, callback),
            lambda timer: timer.cancel(), rescan, reason, candidate_path, self.wired_debounce * 1000)

    @on_core
    def poll_wired_device_events(self):
        lifecycle.poll_wired_device_events(lifecycle.BackendContext(self,
            schedule_rescan=self._schedule_wired_device_change_rescan))

    def handle_power_event(self, wparam):
        """Suspend cleanup runs on the PowerListener thread, as in classic."""
        if self.is_quitting:
            return
        from importlib import import_module
        def restart_after(seconds, callback):
            self._timers.append(self._executor.call_later(
                self.resume_delay, lambda: callback() if not self.is_quitting else None))
        lifecycle.handle_power_event(
            self, wparam, import_module,
            lambda: self._executor.submit(self.update_controllers, []),
            self.start_discoverer, restart_after, self.suspend_settle)
    @on_core
    def stop_periodic(self):
        """Cancel lifecycle polling before service teardown begins."""
        for timer in self._timers:
            timer.cancel()
        if self._wired_device_change_after_id:
            self._wired_device_change_after_id.cancel()

    @on_core
    def shutdown(self, on_done):
        if self.is_cleaning_up:
            return
        try:
            if self.wired_device_listener:
                self.wired_device_listener.stop()
        except Exception:
            logger.debug("Failed to stop wired device listener", exc_info=True)
        self.is_cleaning_up = self.is_quitting = True
        from discoverer import set_shutting_down
        set_shutting_down(True)
        for timer in self._timers:
            timer.cancel()
        if self._wired_device_change_after_id:
            self._wired_device_change_after_id.cancel()
        def prepare():
            # Read the latest list and mark terminal VC state on core. The cleanup
            # worker owns the physical-loop wait, so the UI remains responsive.
            self._shutdown_virtual_devices_for_power_event(detach_timeout=1.5)
            return [vc for vc in self.current_controllers
                    if vc and getattr(vc, "loop", None) and vc.loop.is_running()]
        def cleanup():
            lifecycle.quit_worker(
                lambda: self._executor.submit(prepare).result(),
                lambda: self._executor.submit(done), self.disconnect_spacing,
                self.shutdown_settle, self.shutdown_timeout)
        def done():
            self.bus.publish(events.SHUTDOWN_COMPLETE)
            on_done()  # UI owns destruction and os._exit(0).
        threading.Thread(target=cleanup, daemon=True).start()

    @on_core
    def _shutdown_virtual_devices_for_power_event(self, detach_timeout=1.0):
        lifecycle.shutdown_virtual_devices(self, detach_timeout)
