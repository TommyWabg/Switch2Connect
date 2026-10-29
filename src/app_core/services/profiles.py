"""Profile orchestration for the modern UI on top of the shared classic flows.

Native pickers and keyboard/mouse recording belong to the UI; wrap pickers in
suspend_focus_polling().
"""
from .. import profile_navigation
from .. import profile_store
from contextlib import contextmanager
from concurrent.futures import Future
import logging
import os
import time
from typing import Protocol


from .. import events
from ..models import ActionResult, ProfileResult, ImportPreview, ExportPreview
from ..executor import on_core, run_flow
from .. import profile_io
from ..helpers import (CONTROLLER_RELATED_PRESENCE_KEYS, get_exe_display_name,
                       has_controller_related_data)
from ..models import ProfilesState

logger = logging.getLogger(__name__)


class DriverSwitcher(Protocol):
    """DriverService's on-core contract; ProfileService always calls it on core."""
    def switch_driver(self, val: str, persist_preference: bool = True) -> ActionResult | Future: ...
    def set_simulation_mode(self, val: str) -> ActionResult: ...
    def wake_controller_mouse_output_reconcile(self) -> None: ...


_profile_sort_key = profile_store.profile_sort_key


def get_foreground_app_path():
    return profile_store.get_foreground_app_path()

class ProfileService:
    def __init__(self, bus, executor, ui_port, runtime, *, driver_switcher: DriverSwitcher,
                 config=None, clock=time.perf_counter, foreground_app_reader=None,
                 keyboard_initialize=None, utils_module=None, button_map=None, autostart=True):
        self.bus, self._executor, self.ui_port, self.runtime = bus, executor, ui_port, runtime
        self._config = config
        self._drivers, self._clock = driver_switcher, clock
        self._foreground = foreground_app_reader or get_foreground_app_path
        self._keyboard_initialize = keyboard_initialize
        self._utils_module, self._button_map = utils_module, button_map
        self.pending_profile = self.profile_apply_timer = None
        self._focus_timer = self._capture_timer = None
        self._focus_suspensions = 0
        self._switching = self._stopped = False
        self._last_profile_nav_time = 0.0
        self.last_foreground_app_path = ""
        self._capture_callback = self._on_tokens = None
        self._capture_generation = 0
        self._captured = []
        self._waiting_release = False
        self._snapshot = ProfilesState()
        self._export_preview = ExportPreview()
        if autostart:
            self.start()


    @property
    def config(self):
        if self._config is None:
            from config import CONFIG
            return CONFIG
        return self._config

    @property
    def _utils(self):
        if self._utils_module is None:
            import utils
            return utils
        return self._utils_module

    @property
    def default_app_dir(self):
        return os.environ.get("ProgramFiles") or os.path.expanduser("~")

    def state(self):
        return self._snapshot


    def export_preview(self):
        """Cached live presence for the UI's export checklist (15565–15573)."""
        return self._export_preview


    def get_profile_for_app_path(self, path):
        return profile_store.get_profile_for_app_path(self.config, path, _profile_sort_key)
    def _apps(self, name):
        return profile_store.get_profile_assigned_apps(self.config, name)
    def _publish(self):
        cfg = self.config
        names = tuple(sorted(cfg.profiles, key=lambda n: (
            0 if cfg.profiles[n].get("change_profile_list", False) else 1, _profile_sort_key(n))))
        self._snapshot = ProfilesState(
            names, cfg.active_profile,
            tuple((n, tuple((a["path"], a["name"]) for a in self._apps(n))) for n in names),
            tuple(n for n in names if cfg.profiles[n].get("change_profile_list", False)),
            tuple((n, cfg.profiles[n].get("profile_switching_combo", "")) for n in names),
            getattr(cfg, "profile_switching_combo_trigger", ""),
            getattr(cfg, "change_profile_mode", "Manual"))
        live = {key: getattr(cfg, key, None) for key in CONTROLLER_RELATED_PRESENCE_KEYS}
        live["profiles"] = cfg.profiles
        self._export_preview = ExportPreview(names, cfg.active_profile, has_controller_related_data(live))
        self.bus.publish(events.PROFILES_CHANGED, self._snapshot)

    def _result(self, ok, reason=None, **kwargs):
        self._publish()
        return ProfileResult(bool(ok), reason, **kwargs)

    def _continue(self, value, callback, on_error=None):
        def steps():
            try:
                result = yield value
            except BaseException as exc:
                if on_error:
                    return on_error(exc)
                raise
            return callback(result)
        return run_flow(self._executor, steps())

    @on_core
    def start(self):
        self._stopped = False
        self.runtime.set_profile_handlers(cycle=self.on_cycle_profile, switch=self.on_profile_combo_switch,
                                          nav=self.on_profile_nav, confirm=self.on_profile_confirm,
                                          cancel=self.on_profile_cancel)
        if self._focus_timer is None:
            self._focus_timer = self._executor.call_every(1.0, self.poll_assigned_app_focus)
        self._publish()

    @on_core
    def stop(self):
        self._stopped = True
        self.runtime.set_profile_handlers()
        if self._focus_timer:
            self._focus_timer.cancel()
            self._focus_timer = None
        self._cancel_apply_timer()
        self.end_controller_combo_capture()
        self.on_profile_cancel()

    @on_core
    def refresh(self):
        self._publish()

    @on_core
    def save_active_profile_runtime_settings(self):
        profile_store.save_active_profile_runtime_settings(self.config)
        self._publish()
    @on_core
    def select(self, name):
        if self._switching:
            return self._result(False, "busy")
        if not name or name == self.config.active_profile or name not in self.config.profiles:
            return self._result(False, "unchanged" if name == self.config.active_profile else "missing_profile")
        from ..lifecycle import BackendContext
        context = BackendContext(self, fields={"app_profile_switching": "_selection_switching"},
            cancel=lambda timer: timer.cancel(), _set_profile_button_text=lambda: None,
            close_joystick_custom_popup=lambda: None, close_in_app_gyro_popup=lambda: None,
            refresh_joycon_ir_sensor_buttons=lambda: None, close_profile_popup_if_open=lambda: None)
        def steps():
            result = yield from profile_store.switch_to_profile(context, self.config, name, return_outcome=True)
            return result if isinstance(result, ProfileResult) else self._result(False, "switch_failed")
        return run_flow(self._executor, steps())

    switch_to_profile = select

    @on_core
    def apply_profile_switch(self):
        if self._switching:
            return self._result(False, "busy")
        if self.config.active_profile not in self.config.profiles:
            return self._result(False, "missing_profile")
        self._switching = True
        self._publish()
        def initialize_keyboard():
            if self._keyboard_initialize is None:
                import keyboard_output
                keyboard_output.initialize()
            else:
                self._keyboard_initialize()
        steps = profile_store.apply_profile_switch(
            self.config, self._profile_switch_ui, self._drivers.switch_driver,
            self._drivers.set_simulation_mode, initialize_keyboard,
            self._drivers.wake_controller_mouse_output_reconcile,
            self.cancel_all_calibration_after_profile_switch)
        def apply():
            try:
                outcome = yield from steps
            except BaseException as exc:
                return self._abort_profile_switch(exc)
            return self._finish_profile_switch(outcome)
        return run_flow(self._executor, apply())

    def _profile_switch_ui(self, action, value=None):
        if action == "refresh_ui_for_profile":
            from ..settings_writes import sync_active_mapping
            sync_active_mapping(self.config, save=True)
            self.bus.publish(events.SETTINGS_CHANGED, events.SettingsChanged(None))
            self._publish()
        return False

    def _abort_profile_switch(self, exc):
        self._switching = False
        self._publish()
        raise exc

    def _finish_profile_switch(self, outcome):
        try:
            ok = outcome is not False and getattr(outcome, "ok", True)
            return ProfileResult(ok, None if ok else getattr(outcome, "reason", "driver_rejected"))
        finally:
            self._switching = False

    @on_core
    def cancel_all_calibration_after_profile_switch(self):
        from ..calibration_data import cancel_controller_calibrations
        cancel_controller_calibrations(self.runtime.current_controllers)
        for index, vc in enumerate(self.runtime.current_controllers or []):
            self.bus.publish(events.CALIBRATION_REQUEST, events.CalibrationRequest(index, "cancel"))

    @on_core
    def add(self):
        if self._switching:
            return self._result(False, "busy")
        if not profile_store.add_profile(self.config):
            return self._result(False, "add_failed")
        return self.apply_profile_switch()
    @on_core
    def rename(self, new_name):
        if self._switching:
            return self._result(False, "busy")
        ok, reason = profile_store.rename_profile(self.config, new_name, self.ui_port, lambda: None)
        return self._result(ok, reason)

    @on_core
    def reset(self):
        if self._switching:
            return self._result(False, "busy")
        def steps():
            ok, reason = yield from profile_store.reset_selected_profile(self.config, self.ui_port, self.apply_profile_switch, lambda: None)
            return self._result(ok, reason)
        return run_flow(self._executor, steps())

    @on_core
    def delete(self):
        if self._switching:
            return self._result(False, "busy")
        def steps():
            ok, reason = yield from profile_store.delete_selected_profile(self.config, self.ui_port, self.apply_profile_switch, lambda: None, lambda: None)
            return self._result(ok, reason)
        return run_flow(self._executor, steps())

    def _set_apps(self, name, apps):
        if name not in self.config.profiles:
            return False
        profile_store.set_profile_assigned_apps(self.config, name, apps)
        return True
    @on_core
    def set_profile_assigned_apps(self, name, apps):
        ok = self._set_apps(name, apps)
        return self._result(ok, None if ok else "missing_profile")

    @on_core
    def add_assigned_app(self, path, *, index=None, profile_name=None):
        name = profile_name or self.config.active_profile
        if name not in self.config.profiles or not path:
            return self._result(False, "missing_profile" if name not in self.config.profiles else "empty_path")
        if index is None:
            index = len(self._apps(name))
        profile_store.assign_app(self.config, path, index, name, get_exe_display_name, lambda: None)
        return self._result(True)

    @on_core
    def remove_assigned_app(self, index, *, profile_name=None):
        name = profile_name or self.config.active_profile
        if name not in self.config.profiles:
            return self._result(False, "missing_profile")
        profile_store.remove_assigned_app(self.config, index, name, lambda: None)
        return self._result(True)

    @on_core
    def _change_focus_suspension(self, delta):
        self._focus_suspensions += delta

    @contextmanager
    def suspend_focus_polling(self):
        # The foreground-app poller must not fire a profile switch while a modal
        # file dialog owns the foreground window (same guard as choose_app_path).
        def change(delta):
            result = self._change_focus_suspension(delta)
            if hasattr(result, "result"):
                result.result()
        change(1)
        try:
            yield
        finally:
            change(-1)

    @on_core
    def poll_assigned_app_focus(self):
        if self._stopped or self.runtime.is_quitting:
            return
        profile_store.poll_assigned_app_focus(
            self.config, self._focus_suspensions, self._switching,
            self._foreground, self.get_profile_for_app_path, self.select,
            lambda path: setattr(self, "last_foreground_app_path", path))

    def _change_list_profiles(self):
        return [name for name in profile_store.get_sorted_profiles(self.config, _profile_sort_key)
                if self.config.profiles.get(name, {}).get("change_profile_list", False)]

    def _profile_after(self, milliseconds, callback, *args):
        return self._executor.call_later(milliseconds / 1000., callback, *args)

    def _close_selection(self, action):
        self.bus.publish(events.PROFILE_OVERLAY, events.ProfileOverlayEvent(action=action))

    def _cancel_apply_timer(self):
        if self.profile_apply_timer:
            self.profile_apply_timer.cancel()
            self.profile_apply_timer = None

    def _show_profile_selection_notification(self, manual):
        names = self._snapshot.change_list
        if not names:
            return
        selected = self.pending_profile if self.pending_profile in names else names[0]
        i = names.index(selected)
        self.bus.publish(events.PROFILE_OVERLAY, events.ProfileOverlayEvent(
            action="show", previous=names[(i - 1) % len(names)], current=selected,
            next=names[(i + 1) % len(names)], layout=getattr(self.config, "abxy_mode", "Xbox"),
            manual=manual, auto_close_ms=None if manual else 3000,
            auto_close_seconds=None if manual else 3.0))

    @on_core
    def on_cycle_profile(self):
        return profile_navigation.on_cycle_profile(
            self, self.config, self._utils, self._profile_after,
            lambda timer: timer.cancel(), lambda: True, self._clock,
            self._close_selection, self.select, self._result)
    @on_core
    def on_profile_nav(self, direction):
        return profile_navigation.on_profile_nav(
            self, self.config, self._utils, self._profile_after,
            lambda timer: timer.cancel(), lambda: True, self._clock,
            self._close_selection, self.select, self._result, direction)
    @on_core
    def on_profile_confirm(self):
        return profile_navigation.on_profile_confirm(
            self, self.config, self._utils, self._profile_after,
            lambda timer: timer.cancel(), lambda: True, self._clock,
            self._close_selection, self.select, self._result)
    @on_core
    def on_profile_cancel(self):
        return profile_navigation.on_profile_cancel(
            self, self.config, self._utils, self._profile_after,
            lambda timer: timer.cancel(), lambda: True, self._clock,
            self._close_selection, self.select, self._result)
    @on_core
    def on_profile_combo_switch(self, profile_name):
        result = self.select(profile_name)
        def notify(result):
            if result.ok:
                self.bus.publish(events.PROFILE_OVERLAY, events.ProfileOverlayEvent(
                    action="notification", title="Profile Switched", message=f"Current Profile: {profile_name}",
                    current=profile_name, auto_close_ms=3000, auto_close_seconds=3.0))
            return result
        return self._continue(result, notify)

    @on_core
    def apply_pending_profile(self):
        return profile_navigation.apply_pending_profile(
            self, self.config, self._utils, self._profile_after,
            lambda timer: timer.cancel(), lambda: True, self._clock,
            self._close_selection, self.select, self._result)
    @on_core
    def set_change_profile_mode(self, mode):
        if mode not in ("Auto", "Manual"):
            return self._result(False, "invalid_mode")
        from ..settings_writes import set_change_profile_mode
        set_change_profile_mode(self.config, mode)
        return self._result(True)

    @on_core
    def set_in_change_list(self, name, included):
        if name not in self.config.profiles:
            return self._result(False, "missing_profile")
        profile_store.set_in_change_list(self.config, name, included)
        return self._result(True)
    @staticmethod
    def _normalize_combo(value):
        return "+".join(profile_store.normalize_recorded_inputs(value.split("+") if isinstance(value, str) else value))

    @on_core
    def set_profile_switching_combo_trigger(self, value):
        profile_store.set_profile_switching_combo_trigger(self.config, self._normalize_combo(value))
        return self._result(True)

    @on_core
    def clear_profile_switching_combo_trigger(self):
        return self.set_profile_switching_combo_trigger("")

    @on_core
    def set_profile_combo(self, name, value):
        if name not in self.config.profiles:
            return self._result(False, "missing_profile")
        value = self._normalize_combo(value)
        if profile_store.combo_conflicts(self.config, name, value):
            # Classic ends recording, saves, then restarts it after 100 ms.
            self.config.save_config()
            return self._result(False, "duplicate_combo", restart_recording=True)
        profile_store.set_profile_switching_combo(self.config, name, value)
        return self._result(True)

    @on_core
    def clear_profile_combo(self, name):
        return self.set_profile_combo(name, "")

    @on_core
    def begin_controller_combo_capture(self, on_tokens):
        self.end_controller_combo_capture()
        self._capture_generation += 1
        generation = self._capture_generation
        self._on_tokens, self._captured = on_tokens, []
        profile_store.begin_combo_capture(self._capture_context(generation))
        def callback(states):
            self._executor.submit(self._capture_states, dict(states), generation)
        self._capture_callback = callback
        self._utils.profile_combo_record_callback = callback
        self._poll_controller_capture(generation)

    @on_core
    def end_controller_combo_capture(self):
        self._capture_generation += 1
        if self._capture_timer:
            self._capture_timer.cancel()
            self._capture_timer = None
        profile_store.end_combo_capture(self._capture_context(self._capture_generation), self._utils, self._capture_callback)
        self._capture_callback = self._on_tokens = None
        self._captured = []

    def _capture_states(self, states, generation, *, polling=False, any_pressed=None):
        if generation != self._capture_generation or self._on_tokens is None:
            return
        profile_store.capture_combo_states(self._capture_context(generation), states,
            self._captured, set(), self._finish_capture)

    def _poll_controller_capture(self, generation):
        if generation != self._capture_generation or self._on_tokens is None:
            return
        if self._button_map is None:
            from config import SWITCH_BUTTONS
            button_map = SWITCH_BUTTONS
        else:
            button_map = self._button_map
        def schedule(milliseconds):
            if generation == self._capture_generation and self._on_tokens is not None:
                self._capture_timer = self._executor.call_later(milliseconds / 1000, self._poll_controller_capture, generation)
        profile_store.poll_combo(self._capture_context(generation), button_map,
            self._captured, set(), self._finish_capture, schedule)

    def _capture_context(self, generation):
        from ..lifecycle import BackendContext
        return BackendContext(self, fields={
            "waiting_for_controller_release": "_waiting_release",
            "recorded_controller_buttons": "_recorded_buttons",
            "controller_buttons_pressed": "_controller_buttons_pressed", "recording_controllers": "_capture_active"},
            getters={"recording_controllers": lambda: getattr(self, "_capture_active", False) and generation == self._capture_generation and self._on_tokens is not None,
                     "current_controllers": lambda: self.runtime.current_controllers})

    def _finish_capture(self):
        callback, tokens = self._on_tokens, tuple(self._captured)
        self.end_controller_combo_capture()
        callback(tokens)

    def _read_import(self, path):
        imported, error = profile_io.read_import(path)
        if imported is None:
            self.ui_port.show_message("Import", error, kind="error")
            return None, "invalid_import"
        if any(not isinstance(n, str) for n in profile_io.imported_profiles(imported)):
            self.ui_port.show_message("Import", "This file is not a valid config file.", kind="error")
            return None, "invalid_profile_name"
        return imported, None

    @on_core
    def inspect_import(self, path):
        imported, reason = self._read_import(path)
        if imported is None:
            return ImportPreview(str(path), ok=False, reason=reason)
        profiles = imported.get("profiles")
        names = tuple(sorted(profiles, key=_profile_sort_key)) if isinstance(profiles, dict) else ()
        return ImportPreview(str(path), names, has_controller_related_data(imported))

    @on_core
    def do_import(self, path, selected, keep_current, include_controller_data):
        if self._switching:
            return self._result(False, "busy")
        imported, reason = self._read_import(path)
        if imported is None:
            return self._result(False, reason)
        profiles = imported.get("profiles")
        profiles = profiles if isinstance(profiles, dict) else {}
        selected = list(dict.fromkeys(selected))
        if any(n not in profiles for n in selected):
            return self._result(False, "missing_profile")
        if not selected and not include_controller_data:
            self.ui_port.show_message("Choose Profiles to Import", "Please select at least one profile"
                + (" or Controller Related Data." if has_controller_related_data(imported) else "."), kind="warning")
            return self._result(False, "empty_selection")
        if include_controller_data and not has_controller_related_data(imported):
            return self._result(False, "missing_controller_data")
        if include_controller_data and not self.ui_port.ask_yes_no("Choose Profiles to Import",
                "Warning: Current controller related data will be replaced.", yes="Proceed", no="Cancel"):
            return self._result(False, "controller_data_declined")
        cfg = self.config
        self.save_active_profile_runtime_settings()
        updates, imported_names = profile_io.import_updates(cfg, imported, selected, keep_current, include_controller_data)
        try:
            profile_io.write_config_yaml(cfg, updates)
        except Exception as exc:
            self.ui_port.show_message("Import", f"Failed to write the config file:\n{exc}", kind="error")
            return self._result(False, "write_failed")
        cfg.load_config()
        result = self.apply_profile_switch()
        def imported_result(result):
            # Re-save so a save queued before the import cannot land on top of it.
            cfg.save_config()
            self.ui_port.show_message("Import", profile_io.import_summary(imported_names, include_controller_data))
            return ProfileResult(result.ok, result.reason, names=tuple(imported_names))
        return self._continue(result, imported_result)

    @on_core
    def export(self, path, selected, include_controller_data):
        selected = list(dict.fromkeys(selected))
        if not selected and not include_controller_data:
            self.ui_port.show_message("Choose Profiles to Export", "Please select at least one profile or Controller Related Data.", kind="warning")
            return self._result(False, "empty_selection")
        if any(n not in self.config.profiles for n in selected):
            return self._result(False, "missing_profile")
        path = profile_io.export_file_path(os.fspath(path))
        try:
            profile_io.export_profiles(self.config, path, selected, include_controller_data)
        except Exception as exc:
            self.ui_port.show_message("Export", f"Failed to export:\n{exc}", kind="error")
            return self._result(False, "export_failed", path=path)
        self.ui_port.show_message("Export", profile_io.export_summary(selected, include_controller_data))
        return self._result(True, names=tuple(selected), path=path)
