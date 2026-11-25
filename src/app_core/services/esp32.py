"""ESP32 orchestration for the modern UI on top of the shared classic flows.

Hardware helpers remain lazy. All worker results and progress return to core;
the firmware manager owns its probes independently from normal discovery.
"""
from .. import esp32_status
from concurrent.futures import Future
import importlib
import logging
import queue
import threading
from typing import Protocol

from .. import events
from ..models import PortChoice, PortMode, FirmwareSession, OperationResult
from ..executor import on_core, run_flow
from ..models import Esp32State, StatusInfo

logger = logging.getLogger(__name__)
ESP32_STATE_CHANGED = events.ESP32_STATE_CHANGED
STATUS_CHANGED = events.STATUS_CHANGED
ESP32_FIRMWARE_SESSION = events.ESP32_FIRMWARE_SESSION
ESP32_PORTS_CHANGED = events.ESP32_PORTS_CHANGED


class RuntimePort(Protocol):
    is_quitting: bool
    current_controllers: list
    config: object
    def is_discoverer_running(self) -> bool: ...
    def stop_discoverer(self): ...
    def start_discoverer(self, startup_bridge_context=None): ...


class _PortResolutionError(RuntimeError):
    """Resolver failures are displayed verbatim by the classic manager."""


class _BootModeRequired(RuntimeError):
    pass


class Esp32Service:
    def __init__(self, bus, executor, ui_port, runtime: RuntimePort, *, config=None,
                 bridge=None, discoverer=None, power_saving=None, worker_start=None):
        self._bus, self._executor, self._ui, self._runtime = bus, executor, ui_port, runtime
        self._config_override = config
        self._bridge_override, self._discoverer_override = bridge, discoverer
        self._power_override = power_saving
        self._worker_start = worker_start or self._start_thread
        self._status = self._manager_status = None
        self._detected = self._was_detected = self._current_seen = False
        self._busy = self._refresh_running = self._waiting = False
        self._closed = self._header_was_ready = self._fast_active = False
        self._fast_count = self._generation = 0
        self._auto_running = False
        self._auto_attempted = set()
        self._startup = None
        self._resume_running = False
        self._resume_callback = None
        self._operation_future = self._auto_callback = self._progress_handle = None
        self._timers, self._callbacks = [], ()
        self._periodic = None
        self._ports = ()
        self._port_infos = {}
        self._state = Esp32State()
        self._header = StatusInfo()
        self._session = FirmwareSession()
        self._mode = PortMode()
        self._wired = bool(getattr(getattr(runtime, "wired_state", None), "detected", False))
        self._unsubscribers = [
            bus.subscribe(events.WIRED_STATE_CHANGED, self._wired_changed),
            bus.subscribe(events.CONTROLLERS_CHANGED, lambda _: self.recompute_status()),
            bus.subscribe(events.SETTINGS_CHANGED, lambda _: self.recompute_status()),
            bus.subscribe(events.QUIT_REQUESTED, lambda _: self.close()),
        ]

    @staticmethod
    def _start_thread(fn):
        threading.Thread(target=fn, daemon=True).start()

    @property
    def _config(self):
        return self._config_override if self._config_override is not None else self._runtime.config

    def _bridge(self):
        return self._bridge_override if self._bridge_override is not None else importlib.import_module("usb_serial_bridge")

    def _discoverer(self):
        return self._discoverer_override if self._discoverer_override is not None else importlib.import_module("discoverer")

    def _quitting(self):
        return self._closed or self._runtime.is_quitting

    def _later(self, seconds, fn):
        def run():
            if timer in self._timers:
                self._timers.remove(timer)
            fn()
        timer = self._executor.call_later(seconds, run)
        self._timers.append(timer)
        return timer

    def _worker(self, work, apply):
        def run():
            try:
                value, error = work(), None
            except Exception as exc:
                value, error = None, exc
            try:
                self._executor.submit(apply, value, error)
            except RuntimeError:
                logger.debug("ESP32 worker completed after executor shutdown")
        self._worker_start(run)

    def state(self):
        return self._state


    def status(self):
        return self._header

    def firmware_session(self):
        return self._session

    def port_mode(self):
        return self._mode

    def ports(self):
        return self._ports

    def _publish(self):
        s = self._status
        p = getattr(s, "serial_port", None)
        otg = bool(getattr(s, "otg_only", False))
        installed = bool(getattr(s, "firmware_installed", False))
        self._state = Esp32State(
            detected=self._detected,
            status=getattr(s, "status_text", ""), firmware_version=getattr(s, "firmware_version", None),
            port=getattr(p, "port", None), transport=("OTG" if otg else "CDC") if s else None,
            busy=self._busy,
            firmware_current=bool(getattr(s, "firmware_current", False)), firmware_installed=installed,
            expected_version=getattr(s, "expected_version", ""), firmware_mode=getattr(s, "firmware_mode", ""),
            device_id=getattr(p, "device_id", ""), serial_number=getattr(p, "serial_number", ""),
            location=getattr(p, "location", ""), otg_boot=otg and not installed,
            unverified_candidate=bool(getattr(s, "unverified_candidate", False)),
            firmware_text=self._firmware_text(s))
        c = self._config
        self._mode = PortMode(*(getattr(c, key, default) for key, default in (
            ("esp32_serial_port_mode", "auto"), ("esp32_serial_port", ""),
            ("esp32_serial_device_id", ""), ("esp32_serial_number", ""), ("esp32_serial_location", ""))))
        self._bus.publish(ESP32_STATE_CHANGED, self._state)
        self.recompute_status()

    def _firmware_text(self, s):
        version = getattr(s, "firmware_version", "")
        if getattr(s, "bridge_ready", False):
            return f"Installed ({version})"
        if getattr(s, "firmware_current", False):
            return f"Installed ({version}, waiting for USB transport)"
        if getattr(s, "firmware_update_required", False):
            return f"Update required ({version or 'unknown'} -> {getattr(s, 'expected_version', '') or 'bundled'})"
        if getattr(s, "unverified_candidate", False):
            return "Unverified flashing candidate"
        if getattr(s, "board_present", False):
            boot = getattr(s, "otg_only", False) and not getattr(s, "firmware_installed", False)
            if getattr(self._config, "esp32_serial_port_mode", "auto") == "manual" and not getattr(s, "firmware_installed", False) and not boot:
                return "Selected port did not answer ESP32-S3 firmware status"
            return "Boot mode" if boot else "Detected, waiting for status"
        return "Not installed"

    def _set_status(self, status, *, current=True):
        self._status = status
        self._detected = self._was_detected = bool(getattr(status, "board_present", False))
        self._current_seen = bool(current and getattr(status, "bridge_ready", False))
        self._publish()

    @on_core
    def _wired_changed(self, state):
        self._wired = state.detected
        self.recompute_status()

    @on_core
    def recompute_status(self):
        if self._quitting():
            return
        def module(name):
            return self._bridge() if name == "usb_serial_bridge" else self._discoverer()
        route, text, color = esp32_status.header_status(
            self._status, self._detected, self._header_was_ready, self._wired, module)
        self._header = StatusInfo(route, text, "ok" if color == "#55CC55" else "error" if color == "#FF4444" else "warn", color)
        self._header_was_ready = text == "Ready" and self._detected
        self._bus.publish(STATUS_CHANGED, self._header)
        if text == "Disconnect" and not self._fast_active:
            self._fast_active, self._fast_count = True, 0
            self._later(.5, self._fast_poll)

    def _fast_poll(self):
        if self._quitting():
            self._fast_active = False
            return
        self.refresh_status_async()
        self._fast_count += 1
        if self._header.text != "Disconnect" or self._fast_count >= 30:
            self._fast_active, self._fast_count = False, 0
        else:
            self._later(.5, self._fast_poll)

    @on_core
    def refresh_status_async(self):
        return esp32_status.refresh_esp32s3_status_async(self._classic_context())

    def _classic_context(self):
        from ..lifecycle import BackendContext
        def worker(work, apply, on_schedule_error=None):
            generation = self._generation
            def finish(result, error):
                if generation == self._generation:
                    apply(result)
                else:
                    self._refresh_running = False
            self._worker(work, finish)
        def start_discovery(context=None):
            if self._session.phase == "awaiting_replug" and not self._waiting:
                self._session_changed("done", 100, self._session.message, True, self._session.action)
            self._runtime.start_discoverer(context)
        return BackendContext(self, fields={
            "esp32s3_bridge_status": "_status", "esp32s3_detected": "_detected",
            "_esp32s3_was_detected": "_was_detected", "_esp32s3_current_seen": "_current_seen",
            "_esp32s3_refresh_running": "_refresh_running", "_esp32s3_firmware_busy": "_busy",
            "_esp32s3_waiting_for_removal": "_waiting", "_esp32s3_auto_firmware_running": "_auto_running", "_esp32s3_auto_firmware_attempted": "_auto_attempted"},
            getters={"is_quitting": self._quitting,
                     "current_controllers": lambda: self._runtime.current_controllers},
            module=lambda name: self._bridge() if name == "usb_serial_bridge" else self._discoverer(), schedule=lambda ms, fn: self._later(ms / 1000., fn),
            worker=worker, refresh=self._publish, auto_update=self._maybe_auto_update,
            refresh_status=self._refresh_after_flash,
            wait_current=self._wait_current,
            wait_removal=lambda callback, guard, missing, absent: esp32_status.wait_for_esp32s3_removal_then(
                self._classic_context(), callback, guard, missing, absent),
            discovery_running=self._runtime.is_discoverer_running,
            start_discovery=start_discovery,
            stop_discovery=self._runtime.stop_discoverer,
            refresh_async=self.refresh_status_async,
            run_firmware=lambda action, **kw: self._begin_flash(action, auto=kw["auto"], status=kw["status"], callback=kw["on_complete"]),
            on_write=lambda name, value: self._publish() if name == "_esp32s3_firmware_busy" else None)


    @on_core
    def start_periodic(self, callbacks=(), *, immediate=True):
        """Start the immediate/5 s classic tick; callbacks share its Full guard."""
        self._callbacks = tuple(callbacks)
        if self._periodic is None and not self._quitting():
            self._periodic = self._executor.call_every(5, self._periodic_tick)
            self._timers.append(self._periodic)
            if immediate:
                self._periodic_tick()

    @on_core
    def stop_periodic(self):
        if self._periodic is not None:
            self._periodic.cancel()
            self._periodic = None

    def _periodic_tick(self):
        from ..lifecycle import connection_refresh
        power = self._power_override if self._power_override is not None else importlib.import_module("power_saving")
        connection_refresh(self._quitting(), power,
            (self.refresh_status_async, *self._callbacks, self.recompute_status))

    @on_core
    def list_ports(self):
        """Enumerate every candidate, without admitting it to normal discovery."""
        if self._quitting() or self._busy:
            return self._ports
        try:
            bridge = self._bridge()
            _values, choices = esp32_status.port_choices(self._manager_status or self._status, bridge)
            self._port_infos = {p.port.upper(): p for p, _label, _verified in choices}
            self._ports = tuple(PortChoice(p.port, label,
                *(getattr(p, k, default) for k, default in (("name", ""), ("manufacturer", ""), ("device_id", ""), ("serial_number", ""), ("location", ""), ("is_otg", False), ("confidence", 0))),
                verified=verified) for p, label, verified in choices)
        except Exception:
            self._ports, self._port_infos = (), {}
        self._bus.publish(ESP32_PORTS_CHANGED, self._ports)
        self._publish()
        return self._ports

    def _choice_port(self, choice):
        if choice is None or choice == "Auto":
            return ""
        if isinstance(choice, PortChoice):
            return choice.port.upper()
        return next((p.port.upper() for p in self._ports if p.label == choice), str(choice).upper())

    @on_core
    def set_port_mode(self, port_choice="Auto"):
        """Persist Auto/a listed label/PortChoice/COM name and probe for the view.

        Only verified hardware probes remember identity fields, as in classic.
        """
        if self._busy or self._quitting():
            self._publish()
            return OperationResult(False, "busy" if self._busy else "quitting")
        port = self._choice_port(port_choice)
        esp32_status.select_manager_port(self._config, port)
        self._publish()
        self.refresh_manager_status_async()
        return OperationResult(True)

    @on_core
    def refresh_manager_status_async(self):
        """Manager-only 500 ms monitor entry point for the pure-view dialog."""
        if self._busy or self._quitting() or self._refresh_running:
            return False
        self._refresh_running = True
        generation = self._generation
        def apply(status, error):
            self._refresh_running = False
            if not self._quitting() and not self._busy and generation == self._generation and error is None:
                # Normal discovery excludes blank/generic serial devices. Only
                # the manager may expose them, without marking detection true.
                self._manager_status = status
                esp32_status.apply_manager_probe(self._classic_context(), status, lambda status: self._publish())
        self._worker(lambda: esp32_status.manager_probe(self._bridge()), lambda result, error: apply(result[0], None) if result and result[1] else apply(None, error or RuntimeError("probe failed")))
        return True


    def _label(self):
        try:
            return self._bridge().ESP32S3_LABEL
        except Exception:
            return "ESP32-S3 CDC"

    @on_core
    def install(self, port_choice=None):
        """Return a flash-completion Future, or a rejected OperationResult.

        Off-core dispatch adds the standard outer executor Future. Success means
        flash completed; the session may still own COM while awaiting replug.
        """
        return self._begin_flash("install", port_choice)

    @on_core
    def repair(self, port_choice=None):
        return self._begin_flash("repair", port_choice)

    @on_core
    def delete(self, port_choice=None):
        return self._begin_flash("delete", port_choice)

    def _session_changed(self, phase, percent, message, can_close, action):
        self._session = FirmwareSession(phase, percent, message, can_close, action)
        self._bus.publish(ESP32_FIRMWARE_SESSION, self._session)
        title = {"install": "Installing", "repair": "Repairing", "delete": "Deleting"}.get(action, "")
        title = f"{title} ESP32-S3 N16R8 Firmware".strip()
        self._bus.publish(events.TASK_STATE, events.TaskState("esp32-firmware", title, phase == "flashing", message, percent / 100))

    def _begin_flash(self, action, choice=None, *, auto=False, status=None, callback=None):
        if self._busy or self._quitting() or self._refresh_running:
            self._publish()
            return OperationResult(False, "quitting" if self._quitting() else "busy")
        if action == "delete" and not self._ui.ask_yes_no(self._label(), "Erase ESP32-S3 N16R8 firmware?", danger=True):
            self._publish()
            return OperationResult(False, "cancelled")
        status = status or self._manager_status or self._status
        guidance = esp32_status.boot_mode_guidance(status, action)
        if guidance:
            self._ui.show_message(self._label(), guidance, kind="error")
            self._publish()
            return OperationResult(False, "boot_mode_required")
        # COM Port Release Protection Mechanism
        # Mark firmware busy BEFORE stopping discovery so the 5 s status timer can't
        # sneak in a COM-port probe between stop and flash (which would block esptool
        # or re-occupy the port across the replug).
        self._busy = True
        self._generation += 1
        self._resume_running = self._runtime.is_discoverer_running()
        self._auto_callback = callback
        self._operation_future = Future()
        completion = self._operation_future
        self._publish()
        title = {"install": "Installing", "repair": "Repairing", "delete": "Deleting"}[action] + " ESP32-S3 N16R8 Firmware"
        self._session_changed("flashing", 0, "", False, action)
        handle = self._progress_handle = self._ui.progress(title)
        try:
            self._resume_running = esp32_status.prepare_flash(self._classic_context())
        except Exception as exc:
            self._flash_done(action, auto, handle, None, exc)
            return completion
        requested = self._choice_port(choice) if choice is not None else str(getattr(self._config, "esp32_serial_port", "") or "")
        manual = choice != "Auto" and (bool(requested) if choice is not None else getattr(self._config, "esp32_serial_port_mode", "auto") == "manual")
        hint = self._port_infos.get(requested.upper()) or getattr(status, "serial_port", None)
        progress_queue = queue.Queue()
        done = {"ok": False, "error": None}
        def progress(payload):
            progress_queue.put(("progress", payload))
        def work():
            bridge = self._bridge()
            # Keep the exact candidate already detected by the firmware dialog.
            # Closing the active serial client can make Windows temporarily remove
            # and recreate the COM device; follow saved physical identity.
            if auto:
                selected = getattr(status, "serial_port", None)
                if selected is None:
                    raise _PortResolutionError("Could not find the ESP32-S3 N16R8 CH343/COM flashing port.\nConnect the flashing Type-C/CH343P port and try again.")
            else:
                try:
                    selected = esp32_status.resolve_manager_flash_port(self._config, status, bridge, requested if manual else "", manual, hint)
                except Exception as exc:
                    raise _PortResolutionError(str(exc)) from exc
            # A caller may choose a different port without first probing it in
            # the manager. Never let that bypass the running-OTG guard.
            if getattr(selected, "is_otg", False) and action in ("install", "repair"):
                selected_status = status
                shown_port = getattr(getattr(status, "serial_port", None), "port", "")
                if shown_port.upper() != selected.port.upper():
                    selected_status = bridge.detect_bridge(selected.port, allow_unverified=True)
                if selected_status is None or getattr(selected_status, "firmware_installed", False):
                    raise _BootModeRequired("ESP32 is not in Boot mode — firmware cannot be installed.\n\n"
                        "To enter Boot mode:\n  1. Hold the BOOT button\n  2. Tap RESET once, then release BOOT\n"
                        '  3. Wait for Status to show "Boot", then click Install')
            verb = {"install": "Installing", "repair": "Repairing", "delete": "Deleting"}[action]
            if auto:
                verb = verb.lower()
            progress({"message": f"{self._label()}: {verb} firmware on {selected.port}..."})
            esp32_status.flash_worker(bridge.flash_firmware, selected.port, action, progress, done, progress_queue)
        def poll_progress():
            if self._quitting():
                return
            finished = esp32_status.drain_flash_progress(progress_queue, lambda payload: self._progress(payload, handle))
            if finished:
                self._flash_done(action, auto, handle, None, done["error"])
            else:
                self._later(.05, poll_progress)
        def completed(value, error):
            if error is not None:
                done["error"] = error
                progress_queue.put(("done", None))
        self._worker(work, completed)
        self._later(.05, poll_progress)
        return completion

    def _progress(self, payload, handle):
        if self._quitting() or self._session.phase != "flashing":
            return
        percent, message = esp32_status.flash_progress(payload, self._session.percent)
        message = message or self._session.message
        handle.update(message=message, fraction=percent / 100)
        self._session_changed("flashing", percent, message, False, self._session.action)

    def _flash_done(self, action, auto, handle, value, error):
        handle.close()
        if self._progress_handle is handle:
            self._progress_handle = None
        if self._quitting():
            if self._operation_future and not self._operation_future.done():
                self._operation_future.set_result(OperationResult(False, "quitting"))
            return
        if isinstance(error, (_PortResolutionError, _BootModeRequired)):
            self._busy = False
            self._publish()
            if self._resume_running:
                self._resume_running = False
                self._runtime.start_discoverer()
            self._session_changed("error", 100, str(error), True, action)
            if isinstance(error, _BootModeRequired):
                self._ui.show_message(self._label(), str(error), kind="error")
            self._operation_future.set_result(OperationResult(False, str(error)))
            return
        if not auto:
            self._finish_manager_flash(action, error)
            return
        completion, callback = self._operation_future, self._auto_callback
        self._auto_callback = None
        message = esp32_status.flash_failure_message(error, self._classic_context().module) if error else "Firmware auto-updated. Please replug the ESP32-S3 USB cable."
        self._session_changed("error" if error else "awaiting_replug", 100, message, True, action)
        def steps():
            yield from esp32_status.finish_firmware_task(
                self._classic_context(), self._ui, action, auto,
                {"ok": error is None, "error": error}, self._resume_running, callback, self._label())
            result = OperationResult(error is None, str(error) if error else None)
            if completion and not completion.done():
                completion.set_result(result)
            return result
        run_flow(self._executor, steps())




    @on_core
    def close_firmware_session(self):
        if not self._session.can_close:
            return OperationResult(False, "flashing")
        if self._waiting:
            self._session_changed("done", self._session.percent, self._session.message, True, self._session.action)
            if self._resume_callback is not None:
                self._resume_callback()
            self._waiting = False
        return OperationResult(True)

    def _wait_current(self, callback, remaining=24):
        esp32_status.wait_for_current_esp32s3_then(self._classic_context(), callback, remaining)

    def _maybe_auto_update(self, status, on_complete=None):
        return esp32_status.maybe_auto_update(self._classic_context(), status, on_complete)


    @on_core
    def startup_detect_and_maybe_update(self):
        if self._startup is not None:
            return self._startup
        self._startup = Future()
        def finish(context=None):
            if not self._startup.done():
                if self._session.phase == "awaiting_replug":
                    self._session_changed("done", 100, self._session.message, True, "install")
                self._startup.set_result(context)
        if self._quitting():
            finish(False)
        else:
            context = self._classic_context()
            context._callbacks["startup_cancelled"] = lambda: finish(False)
            esp32_status.startup_detect(context, finish)
        return self._startup


    @on_core
    def close(self):
        self._closed = True
        self._fast_active = False
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        if self._progress_handle is not None:
            self._progress_handle.close()
            self._progress_handle = None
        if self._startup is not None and not self._startup.done():
            self._startup.set_result(False)
        if self._operation_future is not None and not self._operation_future.done():
            self._operation_future.set_result(OperationResult(False, "quitting"))

    def _refresh_after_flash(self):
        from types import SimpleNamespace
        completion = Future()
        self._refresh_running = True
        def work():
            context = SimpleNamespace(module=self._classic_context().module, refresh=lambda: None)
            return esp32_status.refresh_status(context)
        def apply(status, error):
            self._refresh_running = False
            self._status = status
            self._detected = bool(status and status.board_present)
            self._publish()
            completion.set_result(status)
        self._worker(work, apply)
        return completion

    def _finish_manager_flash(self, action, error):
        def on_done(ok, message):
            phase = "error" if not ok else "done" if action == "delete" else "awaiting_replug"
            self._session_changed(phase, 100, message, True, action)
        def wait_replug(resume):
            self._resume_callback = resume
            generation = self._generation
            def complete(kind, status):
                self._session_changed("done", 100, self._session.message, True, action)
                resume(status if kind == "reinserted" else None)
            self._later(1.5, lambda: esp32_status.wait_for_esp32s3_removal_then(
                self._classic_context(), complete,
                lambda: self._waiting and generation == self._generation))
        completion = self._operation_future
        def steps():
            yield from esp32_status.finish_manager_flash(
                self._classic_context(), action, {"ok": error is None, "error": error},
                self._resume_running, on_done, lambda: None, wait_replug)
            if completion and not completion.done():
                completion.set_result(OperationResult(error is None, str(error) if error else None))
        run_flow(self._executor, steps())
