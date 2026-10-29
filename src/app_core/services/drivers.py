"""Driver orchestration for the modern UI on top of the shared classic flows.

All configuration/VC changes run on core. Installer I/O runs on workers; generator
continuations resume on core, including when called by the startup sequence.
Hardware and packaging dependencies are deliberately resolved lazily.
"""
from .. import driver_actions
from .. import driver_transitions
from concurrent.futures import Future
from importlib import import_module
import logging
import threading
import time

from .. import events
from ..models import ActionResult, InstallStatus
from ..executor import on_core, run_flow
from ..models import DriverState, WiredState
from .drivers_install import DriverInstallMixin
from .drivers_checks import DriverChecksMixin

logger = logging.getLogger(__name__)
PROFILE_OUTPUT_CHANGED = events.PROFILE_OUTPUT_CHANGED
MODE_OPTIONS = (("ViGEmBus", ("Xbox360", "PS4")),
                ("USBIP", ("Switch1", "Switch2", "PS5")),
                ("WinUHid", ("Xbox One", "PS4", "PS5")))
MODE_ATTRIBUTES = {"ViGEmBus": "vigembus_sim_mode", "USBIP": "usbip_sim_mode",
                   "WinUHid": "winuhid_sim_mode"}


class DriverService(DriverInstallMixin, DriverChecksMixin):
    def __init__(self, bus, executor, ui_port, runtime, *, config=None,
                 modules=None, sleep=time.sleep, launch_elevated=None,
                 wait_process=None):
        self._bus, self._executor, self._ui, self._runtime = bus, executor, ui_port, runtime
        self._config_override = config
        self._modules = dict(modules or {})
        self._sleep = sleep
        self._launch_override, self._wait_override = launch_elevated, wait_process
        self._statuses = {}
        self._pending_check = True
        self._usbip_restart_pending = self._busy = self._transition = False
        self._wired_refresh_running = self._wired_prompt_shown = False
        self._hidhide_toggle_in_progress = False
        self._wired = WiredState()
        self._hidhide_installed_cached = bool(getattr(self._config, "hidhide_installed", False))
        self._hidhide_service = None
        self._hidhide_active = False
        self._packaged_available = False
        self._keyboard_backend = self._mouse_backend = "Win32 API"
        self._snapshot = DriverState()
        self._unsubscribe = bus.subscribe(events.WIRED_STATE_CHANGED, self._wired_changed)
        # Snapshot construction, including initial cached probes, belongs to core.
        self._executor.submit(self.refresh_state)

    @property
    def _config(self):
        return self._config_override if self._config_override is not None else self._module("config").CONFIG

    def _module(self, name):
        return self._modules[name] if name in self._modules else import_module(name)

    def state(self):
        return self._snapshot


    def _status(self, driver, **kwargs):
        key = driver.lower()
        status = getattr(self._module("driver_install_helper"), f"get_{key}_status")(**kwargs)
        self._statuses[driver] = status
        return status

    def _invalidate(self, key=None):
        self._module("driver_install_helper").invalidate_driver_status_cache(key)

    def _packaged(self):
        return bool(self._module("utils").is_packaged())

    def _describe_label(self, sentence=False):
        return self._module("app_core.helpers").wired_controller_label(self._wired.product_ids, sentence=sentence)

    def _publish(self):
        c = self._config
        driver = getattr(c, "driver_type", "WinUHid")
        preferred = getattr(c, "preferred_driver_type", driver)
        packaged = self._packaged()
        options = ("WinUHid", "ViGEmBus", "USBIP")
        # Preserve configured WinUHid until definitive startup revalidation.
        if packaged and not self._packaged_available and not (self._pending_check and preferred == "WinUHid"):
            options = ("ViGEmBus", "USBIP")
        statuses = tuple(InstallStatus(name, self._statuses[name].state,
                                      self._statuses[name].describe())
                         if name in self._statuses else InstallStatus(name)
                         for name in ("WinUHid", "ViGEmBus", "USBIP", "HidHide"))
        health = next(s.state for s in statuses if s.driver == driver)
        kind = "uninstall" if health == "healthy" else "repair" if health == "partial" and driver != "WinUHid" else "install"
        label = f"{kind.title()} {driver} Driver"
        if driver == "USBIP" and self._usbip_restart_pending:
            kind, label = "restart", "Restart Required"
        winhealthy = self._statuses.get("WinUHid") is not None and self._statuses["WinUHid"].state == "healthy"
        self._snapshot = DriverState(
            driver_type=driver, simulation_mode=getattr(c, "simulation_mode", "PS5"),
            abxy_mode=getattr(c, "abxy_mode", "Xbox"), driver_options=options,
            mode_options=MODE_OPTIONS, install_states=tuple((s.driver, s.state) for s in statuses),
            install_status=statuses,
            packaged_build=packaged, packaged_winuhid_available=self._packaged_available,
            pending_check=self._pending_check, action_label=label, action_kind=kind,
            needs_attention=not self._pending_check and health in ("absent", "partial"),
            keyboard_backend=self._keyboard_backend, mouse_backend=self._mouse_backend,
            keyboard_available=winhealthy, mouse_available=winhealthy,
            wired_settings_label=self._describe_label(),
            wired_auto_scan_enabled=bool(getattr(c, "wired_auto_scan_enabled", getattr(c, "wired_usb_enabled", True))),
            hidhide_hide_enabled=bool(getattr(c, "hidhide_hide_enabled", True)),
            hidhide_service_state=self._hidhide_service,
            hidhide_installed=self._hidhide_installed_cached, hidhide_active=self._hidhide_active,
            usbip_restart_pending=self._usbip_restart_pending,
            busy=self._busy or self._transition or self._hidhide_toggle_in_progress)
        self._bus.publish(events.DRIVER_STATE_CHANGED, self._snapshot)
        return self._snapshot

    @on_core
    def refresh_state(self, initialize_keyboard=False):
        for name in ("WinUHid", "ViGEmBus", "USBIP", "HidHide"):
            # Loaded USBIP components are expected to remain until reboot.
            if name == "USBIP" and self._usbip_restart_pending:
                continue
            self._status(name, use_cache=True)
        if self._packaged():
            self._packaged_available = bool(self._module("config").packaged_winuhid_available())
        self._hidhide_service = self._module("app_core.helpers").hidhide_service_state()
        if self._hidhide_service is not None:
            self._hidhide_installed_cached = self._hidhide_service
        try:
            self._hidhide_active = self._module("hidhide").is_active() if self._hidhide_installed_cached else False
        except Exception:
            self._hidhide_active = False
        self._refresh_output(initialize_keyboard)
        return self._publish()

    def _refresh_output(self, initialize=False):
        kb, mouse = self._module("keyboard_output"), self._module("raw_input_mouse")
        if initialize:
            kb.initialize()
        self._keyboard_backend = "WinUHid Virtual HID" if kb.effective_mode() == kb.RAW_INPUT else "Win32 API"
        status = self._statuses.get("WinUHid")
        self._mouse_backend = "WinUHid Virtual HID" if mouse.requested_mode() == "Raw Input" and status is not None and status.state == "healthy" else "Win32 API"
        self._bus.publish(PROFILE_OUTPUT_CHANGED, None)

    def _refresh_dependents(self):
        self._publish()
        self._runtime.refresh_snapshots()
        self._bus.publish(events.SETTINGS_CHANGED, events.SettingsChanged(None))

    def _flow(self, generator, transform=lambda value: value):
        return run_flow(self._executor, generator, transform)

    def _worker(self, fn):
        future = Future()
        def work():
            try:
                future.set_result(fn())
            except BaseException as exc:
                future.set_exception(exc)
        threading.Thread(target=work, name="driver-io", daemon=True).start()
        return future

    def _reject(self, reason):
        self._publish()
        return ActionResult(False, reason)

    @on_core
    def switch_driver(self, val, persist_preference=True):
        if self._busy or self._transition:
            return self._reject("busy")
        if val not in MODE_ATTRIBUTES:
            return self._reject("invalid_driver")
        return self._flow(self._switch_driver(val, persist_preference))

    def _switch_driver(self, val, persist_preference):
        self._transition = True
        self._publish()
        try:
            result = yield from driver_transitions.switch_driver(
                self._config, val, persist_preference, self._runtime.current_controllers,
                self._ui, self._module, lambda name, **kwargs: getattr(self, name)(**kwargs),
                self._transition_ui, self._status, self._invalidate, self._sleep,
                self._usbip_restart_pending)
            if not result.ok and val == "USBIP" and self._usbip_restart_pending:
                return ActionResult(False, "restart_required")
            return result
        finally:
            self._transition = False
            self._publish()
    def _transition_ui(self, action, *args):
        if action == "update_dynamic_rumble_mode_options":
            self._normalize_rumble()
        elif action == "_prompt_usbip_restart_required":
            self._prompt_usbip_restart_required()
        elif action == "force_refresh_player_slots":
            self._refresh_dependents()
        elif action == "update_driver_button":
            self._publish()
    @on_core
    def set_simulation_mode(self, val):
        if self._busy or self._transition:
            return self._reject("busy")
        result = driver_transitions.set_simulation_mode(
            self._config, val, self._runtime.current_controllers, self._transition_ui)
        self._publish()
        return result
    def _normalize_rumble(self):
        c = self._config
        usbip_ps5 = getattr(c, "driver_type", "WinUHid") == "USBIP" and getattr(c, "simulation_mode", "PS5") == "PS5"
        current = getattr(c, "rumble_mode", "Xbox")
        allowed = ("Xbox", "PS5") if usbip_ps5 else ("Xbox", "Switch")
        # Rumble values are persisted per Emu Mode category. Converting it
        # globally used to corrupt the Xbox category when returning to Xbox One.
        normalized = "PS5" if usbip_ps5 and current == "Switch" else "Switch" if not usbip_ps5 and current == "PS5" else current if current in allowed else "Xbox"
        if normalized != current:
            c.rumble_mode = normalized
            c.save_config()


    @on_core
    def set_layout(self, val):
        self._config.abxy_mode = val
        self._config.save_config()
        self._publish()
        self._bus.publish(events.SETTINGS_CHANGED, events.SettingsChanged(None))
        return ActionResult(True)

    @on_core
    def startup_driver_check(self, *, initialize_keyboard=True):
        return self._flow(self._startup_check(initialize_keyboard))

    def _startup_check(self, initialize_keyboard):
        yield self.check_driver_installation()
        self._pending_check = False
        self._refresh_output(initialize=initialize_keyboard)
        self._publish()
        # Classic continues discovery even when a driver install was declined.
        return True


    @on_core
    def toggle_keyboard_output(self):
        if self._busy:
            return self._reject("busy")
        def refresh():
            self._refresh_output()
            self._publish()
        return driver_transitions.toggle_keyboard_output(
            self._module("keyboard_output"), self._status, self._ui, refresh)

    @on_core
    def toggle_mouse_output(self):
        if self._busy:
            return self._reject("busy")
        def refresh():
            self._refresh_output()
            self._publish()
        return driver_transitions.toggle_mouse_output(
            self._config, self._module("raw_input_mouse"), self._status, self.wake_controller_mouse_output_reconcile, refresh)

    @on_core
    def wake_controller_mouse_output_reconcile(self):
        driver_transitions.wake_controller_mouse_output_reconcile(self._runtime.controller_registry())

    @on_core
    def _wired_changed(self, state):
        self._wired = state
        self._publish()

    @on_core
    def toggle_wired_auto_scan(self):
        driver_actions.toggle_auto_scan(
            self._config, self._module("discoverer").set_wired_auto_scan_enabled, self._publish)
        return ActionResult(True)
    @on_core
    def manual_wired_scan(self):
        self._module("discoverer").request_wired_rescan("manual_refresh", manual=True)
        self._publish()
        return ActionResult(True)

    @on_core
    def refresh_wired_status(self):
        if self._wired_refresh_running or self._busy or self._runtime.is_quitting or self._module("power_saving").is_full():
            return False
        self._wired_refresh_running = True
        # Copy on core; workers must not traverse the runtime's mutable VC list.
        from ..snapshots import wired_state
        wired = wired_state(self._runtime.current_controllers)
        return self._flow(self._poll_wired(wired))

    def _poll_wired(self, wired):
        try:
            state = yield self._worker(lambda: self._module("app_core.helpers").hidhide_service_state() if wired.detected else False)
            if self._runtime.is_quitting:
                return False
            self._wired = wired
            self._hidhide_service = state
            from ..lifecycle import BackendContext
            context = BackendContext(self, fields={"_wired_pro2_prompt_shown": "_wired_prompt_shown"})
            yield from driver_actions.apply_wired_probe(
                context, self._config, wired.detected, state, self._publish,
                self.ask_hidhide_auto_install, self.run_hidhide_install)
            return True
        finally:
            self._wired_refresh_running = False

    @on_core
    def ask_hidhide_auto_install(self):
        result = driver_actions.ask_hidhide_auto_install(
            self._config, self._ui, self._describe_label(sentence=True))
        self._publish()
        return result

    def _sync_hidhide_installed(self, save=True):
        state = self._module("app_core.helpers").hidhide_service_state()
        self._hidhide_service = state
        return driver_actions.sync_hidhide_installed(
            self._config, state,
            lambda value: setattr(self, "_hidhide_installed_cached", value), save)
    @on_core
    def on_driver_btn_clicked(self):
        if self._busy or self._transition:
            return self._reject("busy")
        driver = getattr(self._config, "driver_type", "WinUHid")
        if driver == "USBIP":
            return self.on_usbip_btn_clicked()
        if driver != "ViGEmBus":
            return self.on_winuhid_manager_driver_action()
        return self._flow(self._vigem_action())

    @on_core
    def on_vigembus_driver_action(self):
        """Manage ViGEmBus independently of the selected output driver."""
        if self._busy or self._transition:
            return self._reject("busy")
        return self._flow(self._vigem_action())

    def _vigem_action(self):
        return (yield from driver_actions.vigem_action(
            self._config, self._status, self._ui,
            lambda name, **kwargs: getattr(self, name)(**kwargs), self._publish))
    @on_core
    def on_winuhid_manager_driver_action(self):
        if self._busy or self._transition:
            return self._reject("busy")
        return self._flow(self._winuhid_action())

    def _winuhid_action(self):
        return (yield from driver_actions.winuhid_action(
            self._ui, self._status, lambda name, **kwargs: getattr(self, name)(**kwargs),
            lambda name: self.refresh_winuhid_driver_manager()))
    @on_core
    def refresh_winuhid_driver_manager(self, initialize_keyboard=False):
        if self._busy:
            return self._publish()
        self._invalidate("winuhid")
        self._status("WinUHid", use_cache=False)
        self._refresh_output(initialize_keyboard)
        return self._publish()

    @on_core
    def on_usbip_btn_clicked(self):
        if self._busy or self._transition:
            return self._reject("busy")
        if self._usbip_restart_pending:
            self._prompt_usbip_restart_required()
            return self._reject("restart_required")
        return self._flow(self._usbip_action())

    def _usbip_action(self):
        return (yield from driver_actions.usbip_action(
            self._ui, self._status, lambda name, **kwargs: getattr(self, name)(**kwargs),
            lambda name: self._prompt_usbip_restart_required(), self._usbip_restart_pending))
    @on_core
    def on_hidhide_button(self):
        if self._busy:
            return self._reject("busy")
        def steps():
            result = yield from driver_actions.hidhide_button(
                self._ui, self._module("app_core.helpers").hidhide_service_state,
                self._describe_label(sentence=True), self.run_hidhide_install, self.run_hidhide_uninstall)
            return self._reject("cancelled") if result is None else result
        return self._flow(steps())


    def _hide_detected_pro2_with_hidhide(self):
        driver_actions.hide_detected_pro2_with_hidhide(self._module)
    def _unhide_detected_pro2_with_hidhide(self):
        driver_actions.unhide_detected_pro2_with_hidhide(self._module)

    @on_core
    def toggle_hidhide_active(self):
        if self._hidhide_toggle_in_progress or self._busy:
            return self._reject("busy")
        return self._toggle_hidhide()

    def _toggle_hidhide(self):
        self._hidhide_toggle_in_progress = True
        self._publish()
        pending, result = [], []
        def complete(ok, reason):
            self._hidhide_toggle_in_progress = False
            installed = self._sync_hidhide_installed()
            try:
                self._hidhide_active = self._module("hidhide").is_active() if installed else False
            except Exception:
                self._hidhide_active = False
            self._publish()
            result.append(ActionResult(ok, reason))
        driver_actions.toggle_hidhide(
            self._config, self._module, self._ui,
            lambda milliseconds, callback: pending.append((milliseconds, callback)),
            lambda: not self._runtime.is_quitting,
            self._hide_detected_pro2_with_hidhide, self._unhide_detected_pro2_with_hidhide, complete)
        while pending:
            milliseconds, callback = pending.pop(0)
            self._sleep(milliseconds / 1000.)
            callback()
        return result[0]

    @on_core
    def close(self):
        """Release the wired-state subscription after polling has stopped."""
        self._unsubscribe()
