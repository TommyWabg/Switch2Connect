"""Profile-service parity tests: no CONFIG singleton, hardware or native pickers."""
from concurrent.futures import Future
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest
import yaml

from app_core import events
from app_core.bus import EventBus
from app_core.executor import CoreExecutor
from app_core.services.profiles import ProfileService, get_foreground_app_path
from app_core.ui_port import ScriptedUiPort


class Timer:
    def __init__(self, at, fn, args=(), every=None):
        self.at, self.fn, self.args, self.every = at, fn, args, every
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class Executor:
    def __init__(self):
        self.now = 10.0
        self.timers = []

    def is_core_thread(self):
        return True

    def submit(self, fn, *args, **kwargs):
        future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as exc:
            future.set_exception(exc)
        return future

    def call_later(self, seconds, fn, *args):
        timer = Timer(self.now + seconds, fn, args)
        self.timers.append(timer)
        return timer

    def call_every(self, seconds, fn):
        timer = self.call_later(seconds, fn)
        timer.every = seconds
        return timer

    def advance(self, seconds):
        target = self.now + seconds
        while True:
            due = [t for t in self.timers if not t.cancelled and t.at <= target]
            if not due:
                break
            timer = min(due, key=lambda t: t.at)
            self.now = timer.at
            timer.cancelled = True
            timer.fn(*timer.args)
            if timer.every:
                self.call_every(timer.every, timer.fn)
        self.now = target


class Config:
    def __init__(self, path):
        self.config_file_path = str(path)
        self._save_lock = threading.Lock()
        self.active_profile = "Profile 1"
        self.profiles = {"Profile 1": self.get_default_profile_dict(),
                         "Profile 2": self.get_default_profile_dict()}
        self.driver_type, self.simulation_mode, self.abxy_mode = "WinUHid", "PS5", "Xbox"
        self.change_profile_mode = "Manual"
        self.profile_switching_combo_trigger = "VK_Q"
        self.saves = []
        self.loads = 0
        self.driver_fallback_active = False
        self.calibration_data = {"local": [1]}
        self.activation_syncs = 0
        self.write_disk()

    def get_default_profile_dict(self):
        return {"driver_type": "WinUHid", "simulation_mode": "PS5", "assigned_apps": [],
                "change_profile_list": True, "profile_switching_combo": "",
                "keyboard_output_mode": None, "mouse_output_mode": None,
                "xbox": {"joycon_hold_mode": {}}}

    def save_config(self):
        self.saves.append(deepcopy(self.profiles))

    def sync_active_in_app_gyro_activation(self):
        self.activation_syncs += 1

    def write_disk(self, **updates):
        data = {"profiles": self.profiles, "active_profile": self.active_profile,
                "profile_switching_combo_trigger": self.profile_switching_combo_trigger,
                "change_profile_mode": self.change_profile_mode, "ui_mode": "modern",
                "window_x": 123, "calibration_data": {"local": [1]}, "custom_global": 9}
        data.update(updates)
        Path(self.config_file_path).write_text(yaml.safe_dump(data), encoding="utf-8")

    def load_config(self):
        self.loads += 1
        for key, value in yaml.safe_load(Path(self.config_file_path).read_text(encoding="utf-8")).items():
            setattr(self, key, value)

    def add_profile(self, name):
        self.profiles[name] = deepcopy(self.profiles[self.active_profile])
        self.active_profile = name
        self.save_config()
        return True

    def rename_profile(self, name):
        self.profiles[name] = self.profiles.pop(self.active_profile)
        self.active_profile = name
        self.save_config()
        return True

    def reset_profile_to_default(self, name):
        self.profiles[name] = self.get_default_profile_dict()
        self.save_config()
        return True

    def delete_profile(self):
        self.profiles.pop(self.active_profile)
        self.active_profile = next(iter(self.profiles))
        self.save_config()
        return True

    def switch_profile(self, name):
        self.active_profile = name
        self.save_config()
        return True


class Drivers:
    def __init__(self, config, log):
        self.config, self.log = config, log
        self.reject = False

    def switch_driver(self, val, persist_preference=True):
        attr = {"USBIP": "usbip_sim_mode", "ViGEmBus": "vigembus_sim_mode"}.get(val, "winuhid_sim_mode")
        self.log.append(("driver", val, persist_preference, getattr(self.config, attr)))
        if self.reject:
            return SimpleNamespace(ok=False, reason="denied")
        self.config.driver_type = val
        self.config.simulation_mode = getattr(self.config, attr)
        return SimpleNamespace(ok=True)

    def set_simulation_mode(self, val):
        self.log.append(("emu", val))
        if self.reject:
            return False
        self.config.simulation_mode = val

    def wake_controller_mouse_output_reconcile(self):
        self.log.append(("mouse",))


@pytest.fixture
def setup(core_tmp_path, monkeypatch):
    import app_core.services.profiles as module
    monkeypatch.setattr(module, "get_exe_display_name", lambda path: Path(path).stem)
    cfg = Config(core_tmp_path / "config.yaml")
    executor, bus, ui = Executor(), EventBus(), ScriptedUiPort()
    log = []
    runtime = SimpleNamespace(current_controllers=[], is_quitting=False,
                              set_profile_handlers=lambda **kwargs: log.append(("handlers", kwargs)))
    utils = SimpleNamespace(profile_selection_active=False, profile_combo_record_callback=None)
    drivers = Drivers(cfg, log)
    foreground = [""]
    service = ProfileService(bus, executor, ui, runtime, config=cfg, driver_switcher=drivers,
                             clock=lambda: executor.now, foreground_app_reader=lambda: foreground[0],
                             keyboard_initialize=lambda: log.append(("keyboard",)), utils_module=utils,
                             button_map={"A": 1, "B": 2, "Capture": 4, "PS_C_Click": 8})
    return SimpleNamespace(service=service, cfg=cfg, executor=executor, bus=bus, ui=ui,
                           runtime=runtime, utils=utils, drivers=drivers, foreground=foreground,
                           log=log, path=core_tmp_path)


def answer(setup, value):
    setup.ui.answers.append(value)


def test_snapshot_natural_sort_flags_legacy_and_immutable(setup):
    s, cfg = setup.service, setup.cfg
    cfg.profiles = {"Profile 10": cfg.get_default_profile_dict(), "Profile 2": cfg.get_default_profile_dict(),
                    "alpha": cfg.get_default_profile_dict()}
    cfg.profiles["alpha"]["change_profile_list"] = False
    cfg.profiles["Profile 10"].pop("assigned_apps")
    cfg.profiles["Profile 10"]["assigned_app"] = "game.exe"
    s.refresh()
    state = s.state()
    assert state.names == ("Profile 2", "Profile 10", "alpha")
    assert dict(s.state().assigned_apps).get("Profile 10", ()) == (("game.exe", "game"),)
    assert s.get_profile_for_app_path("game.exe") == "Profile 10"
    assert s.get_profile_for_app_path("") is None
    with pytest.raises(FrozenInstanceError):
        state.active = "bad"
    s.set_in_change_list("alpha", True)
    assert s.state().names[0] == "alpha"
    assert state.names[0] == "Profile 2"


def test_crud_saves_and_prompt_branches(setup):
    s, cfg = setup.service, setup.cfg
    s.add_assigned_app("game.exe")
    s.set_profile_combo(cfg.active_profile, "BTN_A")
    cfg.driver_type, cfg.simulation_mode = "ViGEmBus", "Xbox"
    assert s.add().ok
    assert cfg.active_profile == "Profile 3"
    assert cfg.profiles["Profile 1"]["driver_type"] == "ViGEmBus"
    assert cfg.profiles["Profile 3"]["assigned_apps"] == []
    assert cfg.profiles["Profile 3"]["profile_switching_combo"] == ""
    assert cfg.profiles["Profile 3"]["change_profile_list"]
    assert not s.rename("").ok
    assert not s.rename(cfg.active_profile).ok
    assert s.rename("Profile 2").reason == "duplicate_name"
    assert setup.ui.calls[-1].args == ("Error", "Profile 'Profile 2' already exists.")
    assert s.rename("Renamed").ok
    s.set_in_change_list("Renamed", False)
    answer(setup, False)
    assert s.reset().reason == "cancelled"
    answer(setup, True)
    assert s.reset().ok
    assert not cfg.profiles["Renamed"]["change_profile_list"]
    answer(setup, False)
    assert s.delete().reason == "cancelled"
    answer(setup, True)
    assert s.delete().ok
    assert cfg.active_profile == "Profile 1"
    cfg.profiles = {"Profile 1": cfg.profiles["Profile 1"]}
    assert s.delete().reason == "last_profile"
    assert setup.ui.calls[-1].args == ("Delete Profile", "Cannot delete the last profile.")


@pytest.mark.parametrize("method, config_method", [("add", "add_profile"), ("reset", "reset_profile_to_default"),
                                                  ("delete", "delete_profile"), ("rename", "rename_profile"),
                                                  ("select", "switch_profile")])
def test_config_operation_refusal_republishes(setup, method, config_method, monkeypatch):
    monkeypatch.setattr(setup.cfg, config_method, lambda *a: False)
    before = setup.service.state()
    seen = []
    setup.bus.subscribe(events.PROFILES_CHANGED, seen.append)
    if method in ("reset", "delete"):
        answer(setup, True)
    args = ("New",) if method == "rename" else ("Profile 2",) if method == "select" else ()
    assert not getattr(setup.service, method)(*args).ok
    assert seen[-1].active == before.active


@pytest.mark.parametrize("driver, emu, fallback, expected", [
    ("USBIP", "PS5", False, ("driver", "USBIP", True, "PS5")),
    ("ViGEmBus", "Xbox", False, ("driver", "ViGEmBus", True, "Xbox")),
    ("WinUHid", "PS4", False, ("emu", "PS4")),
    ("WinUHid", "Xbox", True, ("driver", "ViGEmBus", False, "Xbox")),
])
def test_switch_driver_resolution_order_calibration(setup, driver, emu, fallback, expected):
    cfg, s = setup.cfg, setup.service
    cfg.profiles["Profile 2"].update(driver_type=driver, simulation_mode=emu)
    cfg.driver_fallback_active = fallback
    controller = SimpleNamespace(is_calibrating=True, prev_calibration=True)
    custom = SimpleNamespace(cancel_back_button_calibration_state=lambda: setup.log.append(("cancel",)))
    setup.runtime.current_controllers = [SimpleNamespace(controllers=[controller, custom]), None]
    for topic in (events.SETTINGS_CHANGED, events.PROFILES_CHANGED, events.CALIBRATION_REQUEST):
        setup.bus.subscribe(topic, lambda payload, topic=topic: setup.log.append((topic, payload)))
    setup.log.clear()
    assert s.select("Profile 2").ok
    assert expected in setup.log
    assert cfg.profiles["Profile 2"]["driver_type"] == driver
    assert not controller.is_calibrating and not controller.prev_calibration
    significant = [x[0] for x in setup.log if x[0] != events.PROFILES_CHANGED]
    assert significant[:4] == [expected[0], "keyboard", "mouse", events.SETTINGS_CHANGED]
    assert (events.CALIBRATION_REQUEST, events.CalibrationRequest(0, "cancel")) in setup.log
    assert ("cancel",) in setup.log
    assert s.select("Profile 2").reason == "unchanged"
    assert s.select("missing").reason == "missing_profile"


def test_switch_missing_fields_and_driver_refusal(setup):
    s, cfg = setup.service, setup.cfg
    cfg.profiles["Profile 2"].pop("driver_type")
    cfg.profiles["Profile 2"].pop("simulation_mode")
    assert s.select("Profile 2").ok
    assert cfg.profiles["Profile 2"]["driver_type"] == "WinUHid"
    cfg.profiles["Profile 1"].update(driver_type="USBIP", simulation_mode="PS5")
    setup.drivers.reject = True
    assert s.select("Profile 1").reason == "denied"
    assert cfg.driver_type == "WinUHid"
    assert s.state().active == "Profile 1"
    cfg.active_profile = "absent"
    assert s.apply_profile_switch().reason == "missing_profile"


def test_assigned_apps_normalization_transfer_replace_remove(setup):
    s, cfg = setup.service, setup.cfg
    cfg.profiles["Profile 1"].pop("assigned_apps")
    cfg.profiles["Profile 1"]["assigned_app"] = {"path": "game.exe", "name": "Legacy"}
    s.refresh()
    assert dict(s.state().assigned_apps).get("Profile 1", ()) == (("game.exe", "Legacy"),)
    assert s.add_assigned_app("game.exe", profile_name="Profile 2").ok
    assert dict(s.state().assigned_apps).get("Profile 1", ()) == ()
    assert "assigned_app" not in cfg.profiles["Profile 1"]
    s.add_assigned_app("other.exe", index=0, profile_name="Profile 2")
    assert dict(s.state().assigned_apps).get("Profile 2", ())[0][0] == "other.exe"
    s.set_profile_assigned_apps("Profile 2", ["a.exe", {"path": "./a.exe"}, {"path": "", "name": "bad"},
                                              {"path": "b.exe", "name": "B"}])
    assert len(dict(s.state().assigned_apps).get("Profile 2", ())) == 2
    s.add_assigned_app("a.exe", profile_name="Profile 1")
    assert dict(s.state().assigned_apps).get("Profile 2", ()) == (("b.exe", "B"),)
    s.remove_assigned_app(10, profile_name="Profile 2")
    s.remove_assigned_app(0, profile_name="Profile 2")
    assert dict(s.state().assigned_apps).get("Profile 2", ()) == ()
    assert not s.add_assigned_app("").ok
    assert not s.add_assigned_app("g.exe", profile_name="missing").ok
    assert not s.remove_assigned_app(0, profile_name="missing").ok
    assert not s.set_profile_assigned_apps("missing", []).ok


def test_focus_timer_nested_suspension_exceptions_and_quit(setup, monkeypatch):
    s = setup.service
    s.add_assigned_app("game.exe", profile_name="Profile 2")
    setup.foreground[0] = "game.exe"
    with pytest.raises(ValueError):
        with s.suspend_focus_polling():
            with s.suspend_focus_polling():
                setup.executor.advance(1)
                assert s.state().active == "Profile 1"
            s.poll_assigned_app_focus()
            assert s.state().active == "Profile 1"
            raise ValueError("picker failed")
    setup.executor.advance(1)
    assert s.state().active == "Profile 2"
    s.select("Profile 1")
    s._switching = True
    s.poll_assigned_app_focus()
    assert s.state().active == "Profile 1"
    s._switching = False
    setup.runtime.is_quitting = True
    s.poll_assigned_app_focus()
    assert s.state().active == "Profile 1"
    setup.runtime.is_quitting = False
    monkeypatch.setattr(s, "_foreground", lambda: (_ for _ in ()).throw(OSError("blocked")))
    s.poll_assigned_app_focus()
    monkeypatch.setenv("ProgramFiles", "C:/Programs")
    assert s.default_app_dir == "C:/Programs"
    monkeypatch.delenv("ProgramFiles")
    assert s.default_app_dir


def test_overlay_manual_throttle_confirm_cancel_and_combo(setup):
    s, ex = setup.service, setup.executor
    overlays = []
    setup.bus.subscribe(events.PROFILE_OVERLAY, overlays.append)
    assert s.on_cycle_profile().ok
    assert setup.utils.profile_selection_active
    assert overlays[-1].manual and overlays[-1].auto_close_ms is None
    assert overlays[-1].current == "Profile 2" and overlays[-1].layout == "Xbox"
    assert s.on_profile_nav(1).ok
    assert s.on_profile_nav(-1).reason == "throttled"
    ex.advance(0.181)
    assert s.on_profile_nav(-1).ok
    assert s.on_profile_confirm().ok
    assert s.state().active == "Profile 2" and not setup.utils.profile_selection_active
    assert s.on_profile_confirm().reason == "inactive"
    assert s.on_profile_nav(1).reason == "inactive"
    s.on_cycle_profile()
    s.on_profile_cancel()
    assert s.pending_profile is None and not setup.utils.profile_selection_active
    assert overlays[-1].action == "cancel"
    assert s.on_profile_combo_switch("Profile 1").ok
    assert overlays[-1].title == "Profile Switched"
    assert overlays[-1].message == "Current Profile: Profile 1"
    assert overlays[-1].auto_close_ms == 3000


def test_overlay_auto_inactivity_timer_and_no_change(setup):
    s, ex = setup.service, setup.executor
    s.set_change_profile_mode("Auto")
    cfg = setup.cfg
    cfg.profiles["Profile 3"] = cfg.get_default_profile_dict()
    s.refresh()
    overlays = []
    setup.bus.subscribe(events.PROFILE_OVERLAY, overlays.append)
    s.on_cycle_profile()
    old = s.profile_apply_timer
    ex.advance(0.7)
    s.on_cycle_profile()
    assert old.cancelled and s.pending_profile == "Profile 3"
    ex.advance(0.7)
    assert cfg.active_profile == "Profile 1"
    ex.advance(0.31)
    assert cfg.active_profile == "Profile 3" and s.pending_profile is None
    assert not overlays[-1].manual and overlays[-1].auto_close_ms == 3000
    s.pending_profile = cfg.active_profile
    assert s.apply_pending_profile().reason == "unchanged"
    assert s.pending_profile == cfg.active_profile  # classic retains same-profile pending
    s.pending_profile = None
    assert not s.apply_pending_profile().ok
    assert s.set_change_profile_mode("bad").reason == "invalid_mode"
    assert not s.set_in_change_list("missing", True).ok
    for name in cfg.profiles:
        s.set_in_change_list(name, False)
    assert s.on_cycle_profile().reason == "empty_change_list"
    setup.utils.profile_selection_active = True
    ex.advance(0.2)
    assert s.on_profile_nav(1).reason == "empty_change_list"


def test_combo_normalization_duplicate_clear(setup):
    s = setup.service
    assert s.set_profile_switching_combo_trigger(["VK_CONTROL_L", "VK_RCONTROL", "VK_RSHIFT", "VK_ALT",
                                                  "VK_RWIN", "VK_A", "VK_A"]).ok
    assert s.state().combo_trigger == "VK_CONTROL+VK_SHIFT+VK_MENU+VK_LWIN+VK_A"
    s.clear_profile_switching_combo_trigger()
    assert s.state().combo_trigger == ""
    s.set_profile_combo("Profile 1", "BTN_A+BTN_B")
    result = s.set_profile_combo("Profile 2", "BTN_A+BTN_B")
    assert not result.ok and result.restart_recording
    assert dict(s.state().switching_combos)["Profile 2"] == ""
    assert not s.set_profile_combo("missing", "BTN_A").ok
    s.clear_profile_combo("Profile 1")
    assert s.set_profile_combo("Profile 2", "BTN_A+BTN_B").ok


def test_capture_quick_callbacks_initial_release_stale_and_ownership(setup):
    s, utils = setup.service, setup.utils
    c = SimpleNamespace(raw_buttons=1)
    setup.runtime.current_controllers = [SimpleNamespace(controllers=[c])]
    tokens = []
    s.begin_controller_combo_capture(tokens.append)
    callback = utils.profile_combo_record_callback
    callback({"A": True})
    callback({"A": False})
    callback({"A": True})
    callback({"A": True, "B": True})
    callback({"A": False, "B": False})
    assert tokens == [("BTN_A", "BTN_B")]
    assert utils.profile_combo_record_callback is None
    callback({"A": True})
    callback({"A": False})
    assert len(tokens) == 1
    c.raw_buttons = 0
    s.begin_controller_combo_capture(tokens.append)
    replacement = lambda states: None
    utils.profile_combo_record_callback = replacement
    s.end_controller_combo_capture()
    assert utils.profile_combo_record_callback is replacement
    setup.executor.advance(0.1)
    assert len(tokens) == 1


def test_capture_physical_poll_excludes_capture_and_releases(setup):
    s, ex = setup.service, setup.executor
    c = SimpleNamespace(raw_buttons=4)
    setup.runtime.current_controllers = [None, SimpleNamespace(controllers=[c])]
    tokens = []
    s.begin_controller_combo_capture(tokens.append)
    c.raw_buttons = 1
    ex.advance(0.05)
    assert s._waiting_release
    c.raw_buttons = 0
    ex.advance(0.05)
    c.raw_buttons = 3
    ex.advance(0.05)
    c.raw_buttons = 4
    ex.advance(0.05)
    assert not tokens
    c.raw_buttons = 0
    ex.advance(0.05)
    assert tokens == [("BTN_A", "BTN_B")]


def write_import(setup, data):
    path = setup.path / "incoming.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_import_export_round_trip_filters_live_store_and_unique_apps(setup):
    s, cfg = setup.service, setup.cfg
    s.add_assigned_app("game.exe")
    cfg.profiles["Profile 1"]["xbox"]["joycon_hold_mode"] = {"MAC": "Horizontal"}
    result = s.export(setup.path / "profiles.txt", ["Profile 1"], False)
    assert result.ok and result.path.endswith("profiles.yaml")
    data = yaml.safe_load(Path(result.path).read_text(encoding="utf-8"))
    assert set(data["profiles"]) == {"Profile 1"}
    assert "ui_mode" not in data and "window_x" not in data and "calibration_data" not in data
    assert data["profiles"]["Profile 1"]["assigned_apps"][0]["path"] == "game.exe"
    assert data["profiles"]["Profile 1"]["xbox"]["joycon_hold_mode"] == {}
    preview = s.inspect_import(result.path)
    assert preview.ok and preview.names == ("Profile 1",) and not preview.has_controller_data
    assert s.do_import(result.path, preview.names, True, False).names == ("Profile 1 (2)",)
    assert cfg.active_profile == "Profile 1"
    assert cfg.profiles["Profile 1"]["assigned_apps"] == []
    assert cfg.profiles["Profile 1 (2)"]["assigned_apps"][0]["path"] == "game.exe"
    assert cfg.ui_mode == "modern" and cfg.calibration_data == {"local": [1]}
    assert cfg.loads == 1
    assert cfg.saves[-1] == cfg.profiles
    assert s.do_import(result.path, preview.names, True, False).names == ("Profile 1 (3)",)


def test_import_replace_normalize_old_ir_and_empty_trigger(setup):
    s, cfg = setup.service, setup.cfg
    data = {"profiles": {"Old": {"assigned_apps": "bad", "assigned_app": "legacy.exe",
                                 "profile_switching_combo": 4, "change_profile_list": 1,
                                 "xbox": {"joycon_hold_mode": {"remote": 1}, "joycon_ir_sensor": {
                                     "left": {"ir_mouse": {"raw_input": "yes"}}}}}},
            "profile_switching_combo_trigger": "  ", "window_x": 999, "ui_mode": "classic",
            "calibration_data": {"remote": 2}, "custom_global": 17,
            "button_remaps": {"xbox": {"joycon_hold_mode": {"remote": 1}}}}
    path = write_import(setup, data)
    assert s.do_import(path, ["Old"], False, False).ok
    assert set(cfg.profiles) == {"Old"} and cfg.active_profile == "Old"
    old = cfg.profiles["Old"]
    assert old["mouse_output_mode"] == "Raw Input"
    assert old["profile_switching_combo"] == "" and old["assigned_apps"] == []
    assert "assigned_app" not in old and old["change_profile_list"] is True
    assert old["xbox"]["joycon_hold_mode"] == {}
    assert cfg.button_remaps["xbox"]["joycon_hold_mode"] == {}
    assert cfg.profile_switching_combo_trigger == "VK_Q"
    assert cfg.ui_mode == "modern" and cfg.window_x == 123
    assert cfg.calibration_data == {"local": [1]} and cfg.custom_global == 17


def test_import_controller_prompt_decline_accept_only_and_export(setup):
    s, cfg = setup.service, setup.cfg
    path = write_import(setup, {"profiles": {"Remote": cfg.get_default_profile_dict()},
                                "calibration_data": {"remote": [2]}, "controller_fast_cache_entries": {"bad": 1},
                                "custom_global": 55, "profile_switching_combo_trigger": "VK_R"})
    before = deepcopy(cfg.profiles)
    answer(setup, False)
    result = s.do_import(path, [], False, True)
    assert result.reason == "controller_data_declined" and cfg.profiles == before
    assert setup.ui.calls[-1].kwargs["yes"] == "Proceed"
    assert setup.ui.calls[-1].kwargs["no"] == "Cancel"
    answer(setup, True)
    assert s.do_import(path, [], False, True).ok
    assert cfg.profiles == before and cfg.calibration_data == {"remote": [2]}
    assert cfg.custom_global == 9 and cfg.profile_switching_combo_trigger == "VK_Q"
    assert not hasattr(cfg, "controller_fast_cache_entries")
    exported = s.export(setup.path / "controller.yaml", [], True)
    data = yaml.safe_load(Path(exported.path).read_text(encoding="utf-8"))
    assert data == {"calibration_data": {"remote": [2]}}
    assert setup.ui.calls[-1].args == ("Export", "Exported controller related data.")
    answer(setup, True)
    assert s.do_import(path, ["Remote"], True, True).ok
    assert cfg.profile_switching_combo_trigger == "VK_R"
    assert setup.ui.calls[-1].args[1].endswith("Controller related data imported.")
    exported = s.export(setup.path / "full.yaml", ["Remote"], True)
    data = yaml.safe_load(Path(exported.path).read_text(encoding="utf-8"))
    assert data["calibration_data"] == {"remote": [2]}
    assert data["active_profile"] == "Remote"
    assert setup.ui.calls[-1].args[1].endswith("Controller related data included.")


@pytest.mark.parametrize("data, reason", [(None, "invalid_import"), ([1], "invalid_import"),
                                        ({}, "invalid_import"), ({"profiles": "bad"}, "invalid_import"),
                                        ({"profiles": {1: {}}}, "invalid_profile_name")])
def test_invalid_import_preview_and_operation(setup, data, reason):
    path = write_import(setup, data)
    assert setup.service.inspect_import(path).reason == reason
    assert setup.service.do_import(path, [], True, False).reason == reason


def test_import_selection_and_read_write_errors(setup, monkeypatch):
    s, cfg = setup.service, setup.cfg
    assert s.inspect_import(setup.path / "absent").reason == "invalid_import"
    path = write_import(setup, {"profiles": {"Remote": {}}, "calibration_data": {"remote": 1}})
    assert s.do_import(path, [], True, False).reason == "empty_selection"
    assert s.do_import(path, ["missing"], True, False).reason == "missing_profile"
    path = write_import(setup, {"profiles": {"Remote": {}}})
    assert s.do_import(path, [], True, False).reason == "empty_selection"
    assert s.do_import(path, ["Remote"], True, True).reason == "missing_controller_data"
    assert not s.export(setup.path / "empty", [], False).ok
    assert not s.export(setup.path / "empty", ["missing"], False).ok
    before = deepcopy(cfg.profiles)
    monkeypatch.setattr("app_core.profile_io.yaml.dump", lambda *a, **kw: (_ for _ in ()).throw(OSError("denied")))
    assert s.do_import(path, ["Remote"], False, False).reason == "write_failed"
    assert cfg.profiles == before and cfg.loads == 0
    assert s.export(setup.path / "blocked", ["Profile 1"], False).reason == "export_failed"


def test_import_write_lock_reload_apply_resave_order(setup, monkeypatch):
    s, cfg = setup.service, setup.cfg
    path = write_import(setup, {"profiles": {"Remote": {}}})
    log = []
    dump, load, save = yaml.dump, cfg.load_config, cfg.save_config
    def locked_dump(*args, **kwargs):
        assert cfg._save_lock.locked()
        log.append("write")
        dump(*args, **kwargs)
    def reload():
        assert not cfg._save_lock.locked()
        log.append("reload")
        load()
    monkeypatch.setattr("app_core.profile_io.yaml.dump", locked_dump)
    monkeypatch.setattr(cfg, "load_config", reload)
    monkeypatch.setattr(cfg, "save_config", lambda: (log.append("save"), save()))
    assert s.do_import(path, ["Remote"], False, False).ok
    assert log == ["write", "reload", "save", "save", "save"]


def test_stop_cancels_service_timers_and_runtime_handlers(setup):
    s = setup.service
    handlers = setup.log[0][1]
    assert set(handlers) == {"cycle", "switch", "nav", "confirm", "cancel"}
    handlers["cycle"]()
    s.set_change_profile_mode("Auto")
    s.on_cycle_profile()
    timer = s.profile_apply_timer
    s.begin_controller_combo_capture(lambda tokens: None)
    s.stop()
    assert timer.cancelled and s._focus_timer is None and s._capture_timer is None
    assert not setup.utils.profile_selection_active
    setup.foreground[0] = "game.exe"
    s.poll_assigned_app_focus()
    assert s.pending_profile is None
    s.start()
    assert s._focus_timer is not None


def test_real_executor_mutations_and_focus_guard_are_marshaled(setup):
    executor = CoreExecutor()
    s = None
    try:
        s = ProfileService(setup.bus, executor, setup.ui, setup.runtime, config=setup.cfg,
                           driver_switcher=setup.drivers, keyboard_initialize=lambda: None,
                           utils_module=setup.utils, button_map={})
        executor.submit(lambda: None).result(timeout=2)
        future = s.rename("Threaded")
        assert isinstance(future, Future) and future.result(timeout=2).ok
        with s.suspend_focus_polling():
            assert executor.submit(lambda: s._focus_suspensions).result(timeout=2) == 1
        assert executor.submit(lambda: s._focus_suspensions).result(timeout=2) == 0
        assert s.state().active == "Threaded"
    finally:
        if s:
            s.stop().result(timeout=2)
        executor.shutdown()


def test_foreground_unavailable_returns_empty(monkeypatch):
    import ctypes
    monkeypatch.setattr(ctypes.windll.user32, "GetForegroundWindow", lambda: 0)
    assert get_foreground_app_path() == ""


def test_driver_future_holds_focus_and_crud_then_finishes_in_order(setup, monkeypatch):
    s, cfg = setup.service, setup.cfg
    pending = Future()
    cfg.profiles["Profile 2"].update(driver_type="USBIP", simulation_mode="PS5")
    monkeypatch.setattr(setup.drivers, "switch_driver", lambda *a, **kw: pending)
    setup.log.clear()
    switched = s.on_profile_combo_switch("Profile 2")
    assert isinstance(switched, Future) and not switched.done()
    assert s.state().active == "Profile 2"
    assert not setup.log  # keyboard/mouse and cancellation wait for driver
    for method, args in [(s.select, ("Profile 1",)), (s.add, ()), (s.rename, ("other",)),
                         (s.reset, ()), (s.delete, ()), (s.apply_profile_switch, ()),
                         (s.do_import, (setup.path / "unused", [], False, False))]:
        assert method(*args).reason == "busy"
    s.add_assigned_app("game.exe", profile_name="Profile 1")
    setup.foreground[0] = "game.exe"
    s.poll_assigned_app_focus()
    assert cfg.active_profile == "Profile 2"
    overlays = []
    setup.bus.subscribe(events.PROFILE_OVERLAY, overlays.append)
    cfg.driver_type = "USBIP"
    pending.set_result(SimpleNamespace(ok=True))
    assert switched.result().ok and not s._switching
    assert setup.log[:2] == [("keyboard",), ("mouse",)]
    assert overlays[-1].action == "notification"


def test_driver_future_error_releases_switch_guard(setup, monkeypatch):
    s = setup.service
    setup.cfg.profiles["Profile 2"]["driver_type"] = "USBIP"
    pending = Future()
    monkeypatch.setattr(setup.drivers, "switch_driver", lambda *a, **kw: pending)
    completion = s.select("Profile 2")
    pending.set_exception(OSError("driver failure"))
    with pytest.raises(OSError, match="driver failure"):
        completion.result()
    assert not s._switching
    assert s.state().active == "Profile 2"


def test_import_waits_for_driver_completion_before_resave_and_summary(setup, monkeypatch):
    s, cfg = setup.service, setup.cfg
    path = write_import(setup, {"profiles": {"Remote": {"driver_type": "USBIP"}}})
    pending = Future()
    monkeypatch.setattr(setup.drivers, "switch_driver", lambda *a, **kw: pending)
    result = s.do_import(path, ["Remote"], False, False)
    assert isinstance(result, Future) and not result.done()
    assert len(cfg.saves) == 1
    assert not any(c.name == "show_message" for c in setup.ui.calls)
    pending.set_result(SimpleNamespace(ok=True))
    assert result.result().ok and result.result().names == ("Remote",)
    assert len(cfg.saves) == 3
    assert setup.ui.calls[-1].args == ("Import", "Imported 1 profile(s).")


def test_capture_physical_poll_takes_first_controller_chord(setup):
    a, b = SimpleNamespace(raw_buttons=0), SimpleNamespace(raw_buttons=0)
    setup.runtime.current_controllers = [SimpleNamespace(controllers=[a, b])]
    tokens = []
    setup.service.begin_controller_combo_capture(tokens.append)
    a.raw_buttons, b.raw_buttons = 1, 2
    setup.executor.advance(0.05)
    a.raw_buttons = b.raw_buttons = 0
    setup.executor.advance(0.05)
    assert tokens == [("BTN_A",)]


def test_low_level_assignment_helpers_preserve_save_points(setup):
    count = len(setup.cfg.saves)
    setup.service.set_profile_assigned_apps("Profile 1", ["game.exe"])
    assert len(setup.cfg.saves) == count
    setup.service.add_assigned_app("game.exe", profile_name="Profile 2")
    assert len(setup.cfg.saves) == count + 1


def test_import_malformed_yaml_and_bad_saved_yaml(setup):
    s, cfg = setup.service, setup.cfg
    path = setup.path / "broken.yaml"
    path.write_text("x: [", encoding="utf-8")
    assert s.inspect_import(path).reason == "invalid_import"
    Path(cfg.config_file_path).write_text("x: [", encoding="utf-8")
    assert s.export(setup.path / "out.yaml", ["Profile 1"], False).reason == "export_failed"
    path = write_import(setup, {"profiles": {"Remote": {}}})
    assert s.do_import(path, ["Remote"], False, False).ok
    assert s.state().active == "Remote"


def test_export_preview_reads_live_controller_presence(setup):
    s, cfg = setup.service, setup.cfg
    preview = s.export_preview()
    assert preview.names == s.state().names and preview.has_controller_data
    cfg.calibration_data = {}
    s.refresh()
    assert not s.export_preview().has_controller_data
    cfg.profiles["Profile 1"]["xbox"]["joycon_hold_mode"] = {"MAC": "Vertical"}
    s.refresh()
    assert s.export_preview().has_controller_data
    with pytest.raises(FrozenInstanceError):
        preview.active = "bad"


def test_activation_sync_failure_does_not_abort_switch(setup, monkeypatch):
    monkeypatch.setattr(setup.cfg, "sync_active_in_app_gyro_activation",
                        lambda: (_ for _ in ()).throw(ValueError("broken")))
    assert setup.service.select("Profile 2").ok


def test_async_driver_completion_runs_on_real_core_thread(setup, monkeypatch):
    executor = CoreExecutor()
    pending = Future()
    threads = []
    s = None
    try:
        setup.cfg.profiles["Profile 2"]["driver_type"] = "USBIP"
        monkeypatch.setattr(setup.drivers, "switch_driver", lambda *a, **kw: pending)
        s = ProfileService(setup.bus, executor, setup.ui, setup.runtime, config=setup.cfg,
                           driver_switcher=setup.drivers, utils_module=setup.utils, button_map={},
                           keyboard_initialize=lambda: threads.append(threading.get_ident()))
        completion = s.select("Profile 2").result(timeout=2)
        assert isinstance(completion, Future)
        worker = threading.Thread(target=lambda: pending.set_result(SimpleNamespace(ok=True)))
        worker.start()
        worker.join(timeout=2)
        assert completion.result(timeout=2).ok
        assert threads == [executor.submit(threading.get_ident).result(timeout=2)]
        assert threads[0] != threading.get_ident()
    finally:
        if s:
            s.stop().result(timeout=2)
        executor.shutdown()
