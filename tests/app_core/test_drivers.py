"""Driver parity tests: every dependency is fake; no elevated/hardware I/O."""
from concurrent.futures import Future
from dataclasses import FrozenInstanceError
from types import SimpleNamespace as NS
import copy
import threading

import pytest

from app_core.bus import EventBus
from app_core.executor import CoreExecutor
from app_core import events
from app_core.models import WiredState
from app_core.services.drivers import DriverService, PROFILE_OUTPUT_CHANGED
from app_core.ui_port import ScriptedUiPort


class Status:
    def __init__(self, state):
        self.state = state
    @property
    def installed(self): return self.state == "healthy"
    @property
    def unknown(self): return self.state == "unknown"
    @property
    def absent(self): return self.state == "absent"
    def describe(self): return "status: " + self.state


class Config(NS):
    def save_config(self):
        self.trace.append(("save", self.driver_type, self.simulation_mode))
    def load_config(self):
        self.trace.append(("load",))
        for key, value in self.disk.items():
            setattr(self, key, copy.deepcopy(value))
    def _bump_settings_generation(self):
        self.trace.append(("bump",))


class VC:
    def __init__(self, trace, name, fail=False):
        self.trace, self.name, self.fail = trace, name, fail
        self.state_lock = threading.RLock()
        self.vg_controller, self.loop = object(), None
        self.controllers = [NS(device=NS(address=name), _interp_wake_event=threading.Event())]
        self.mode, self.hold_mode = "PS5", "Horizontal"
    def is_single(self): return True
    def cleanup_vg_controller(self):
        self.trace.append(("cleanup", self.name))
        self.vg_controller = None
    def _setup_vg_controller(self):
        self.trace.append(("setup", self.name, self.mode))
        if self.fail:
            self.fail = False
            raise RuntimeError("recreation failed")
    def set_mode(self, val):
        self.trace.append(("mode", self.name, val))
        if self.fail:
            self.fail = False
            raise RuntimeError("mode failed")
        self.mode = val


def resolve(value):
    while isinstance(value, Future):
        value = value.result(timeout=10)
    return value


@pytest.mark.parametrize("health,pending,attention", [
    ("healthy", False, False), ("healthy", True, False),
    ("absent", True, False), ("unknown", False, False),
    ("absent", False, True), ("partial", False, True),
])
def test_driver_attention_is_independent_of_action_visibility(rig, health, pending, attention):
    rig.service._statuses["WinUHid"] = Status(health)
    rig.service._pending_check = pending
    state = resolve(rig.executor.submit(rig.service._publish))
    assert state.needs_attention is attention
    if health == "healthy":
        assert state.action_kind == "uninstall"


@pytest.fixture
def rig(core_tmp_path):
    trace = []
    config = Config(driver_type="WinUHid", preferred_driver_type="WinUHid", driver_fallback_active=False,
                    simulation_mode="PS5", winuhid_sim_mode="PS5", vigembus_sim_mode="Xbox360",
                    usbip_sim_mode="Switch1", abxy_mode="Xbox", active_profile="Default",
                    profiles={"Default": {}}, joycon_hold_mode={"a": "Horizontal"},
                    driver_installed=True, vigembus_installed=True, hidhide_installed=False,
                    hidhide_hide_enabled=True, hidhide_install_prompt_suppressed=False,
                    mouse_output_mode="Standard", keyboard_output_mode="Standard", rumble_mode="Xbox",
                    trace=trace)
    config.disk = {k: copy.deepcopy(v) for k, v in vars(config).items() if k != "trace"}
    health = {name: Status("healthy") for name in ("WinUHid", "ViGEmBus", "USBIP")}
    health["HidHide"] = Status("absent")
    queried = []
    helper = NS(invalidate_driver_status_cache=lambda key=None: trace.append(("invalidate", key)))
    for name in health:
        def get(name=name, **kw):
            queried.append(name)
            return health[name]
        setattr(helper, "get_" + name.lower() + "_status", get)
    helper.DRIVER_SPECS = {"ViGEmBus": NS(display_name="ViGEmBus", run=("exe", "installer.exe", "/quiet"))}
    helper.make_download_dir = lambda key: str(core_tmp_path)
    def download(spec, folder, cb):
        trace.append(("download", threading.get_ident()))
        cb("installer.exe", 10, 20)
        return str(core_tmp_path / "installer.exe")
    helper.download_spec_files = download
    kb = NS(RAW_INPUT="Raw Input", mode="Standard")
    kb.effective_mode = lambda: kb.mode
    kb.initialize = lambda: trace.append(("keyboard.initialize",))
    def standard(save=True):
        trace.append(("keyboard.standard", save))
        kb.mode = "Standard"
    def raw(save=True):
        trace.append(("keyboard.raw", save))
        kb.mode = "Raw Input"
        return True
    kb.activate_standard, kb.activate_raw_input = standard, raw
    mouse = NS(requested_mode=lambda: config.mouse_output_mode, shutdown=lambda: trace.append(("mouse.shutdown",)))
    hh = NS(installed=False, active=False)
    hh.is_active = lambda: hh.active
    def set_active(val):
        trace.append(("active", val))
        hh.active = val
    hh.set_active = set_active
    hh.hid_path_to_instance_id = lambda path: "instance:" + path
    hh.hide_device = lambda val: trace.append(("hide", val))
    hh.unhide_device = lambda val: trace.append(("unhide", val))
    helpers = NS(verify_winuhid_runtime=lambda **kw: True, verify_vigembus_runtime=lambda **kw: True,
                 verify_vigembus_ready=lambda **kw: True, hidhide_service_state=lambda: hh.installed,
                 removal_verified=lambda status, probe: status.absent or status.unknown and not probe(attempts=2),
                 wired_controller_label=lambda ids, sentence=False: "A wired controller was" if sentence else "Wired Controller")
    package = NS(value=False)
    capability = NS(value=True)
    config_module = NS(packaged_winuhid_available=lambda: capability.value,
                       refresh_packaged_winuhid_capability=lambda: capability.value,
                       confirm_packaged_winuhid_capability=lambda value: value,
                       get_driver_path=lambda name: str(core_tmp_path / name))
    discoverer = NS(emergency_cleanup=lambda: trace.append(("emergency",)),
                    set_wired_auto_scan_enabled=lambda val: trace.append(("auto_scan", val)),
                    request_wired_rescan=lambda *a, **kw: trace.append(("rescan", a, kw)))
    runtime = NS(current_controllers=[], is_quitting=False, running=False)
    runtime.controller_registry = lambda: runtime.current_controllers
    def stop():
        trace.append(("stop",))
        runtime.running = False
    def start():
        trace.append(("start",))
        runtime.running = True
    runtime.stop_discoverer, runtime.start_discoverer = stop, start
    runtime.is_discoverer_running = lambda: runtime.running
    bus, executor, ui = EventBus(), CoreExecutor(), ScriptedUiPort()
    runtime.refresh_snapshots = lambda: bus.publish(events.CONTROLLERS_CHANGED, ())
    power = NS(full=False)
    modules = {"driver_install_helper": helper, "utils": NS(is_packaged=lambda: package.value),
               "app_core.helpers": helpers, "keyboard_output": kb, "raw_input_mouse": mouse,
               "config": config_module, "discoverer": discoverer, "hidhide": hh,
               "power_saving": NS(is_full=lambda: power.full),
               "usb_hid_controller": NS(enumerate_wired_controllers=lambda **kw: [{"path": "pad"}]),
               "subprocess": NS(Popen=lambda args: trace.append(("restart", args)))}
    launch = NS(handle=12, exit=0, error=None)
    def elevated(file, args, **kw):
        trace.append(("launch", file, args, kw, threading.get_ident()))
        if launch.error: raise launch.error
        return launch.handle
    service = DriverService(bus, executor, ui, runtime, config=config, modules=modules,
                            sleep=lambda sec: trace.append(("sleep", sec)),
                            launch_elevated=elevated, wait_process=lambda handle: launch.exit)
    resolve(executor.submit(lambda: None))
    trace.clear()
    r = NS(service=service, config=config, trace=trace, health=health, queried=queried, helpers=helpers,
           hh=hh, kb=kb, runtime=runtime, ui=ui, bus=bus, executor=executor, package=package,
           capability=capability, launch=launch, power=power, folder=core_tmp_path, helper=helper)
    yield r
    executor.shutdown(timeout=5)


def payload(r, name):
    path = r.folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("fake payload", encoding="utf-8")
    return path


def test_snapshot_is_frozen_and_pending_packaged_choice(rig):
    r = rig
    r.package.value = True
    r.capability.value = False
    resolve(r.service.refresh_state())
    assert r.service.state().driver_options == ("WinUHid", "ViGEmBus", "USBIP")
    with pytest.raises(FrozenInstanceError): r.service.state().busy = True
    assert dict(r.service.state().install_states)["WinUHid"] == "healthy"


def test_switch_two_pass_settle_hold_and_events(rig):
    r = rig
    r.runtime.current_controllers = [VC(r.trace, "a"), None, VC(r.trace, "b")]
    observed = []
    r.bus.subscribe(events.SETTINGS_CHANGED, observed.append)
    result = resolve(r.service.switch_driver("USBIP"))
    assert result.ok
    relevant = [x for x in r.trace if x[0] in ("cleanup", "setup", "sleep")]
    assert relevant == [("cleanup", "a"), ("cleanup", "b"), ("sleep", .5), ("setup", "a", "Switch1"), ("sleep", .2), ("setup", "b", "Switch1")]
    assert r.runtime.current_controllers[0].hold_mode == "Vertical"
    assert r.config.profiles["Default"]["driver_type"] == "USBIP"
    assert r.config.preferred_driver_type == "USBIP"
    assert observed[-1] == events.SettingsChanged(None)


def test_switch_rollback_and_remembered_hold(rig):
    r = rig
    r.runtime.current_controllers = [VC(r.trace, "a"), VC(r.trace, "b", fail=True)]
    result = resolve(r.service.switch_driver("ViGEmBus"))
    assert not result.ok and result.reason == "recreation_failed"
    assert r.config.driver_type == "WinUHid"
    assert r.runtime.current_controllers[0].hold_mode == "Horizontal"
    assert ("setup", "a", "PS5") in r.trace
    assert ("load",) in r.trace
    assert not r.service.state().busy


@pytest.mark.parametrize("driver", ["WinUHid", "ViGEmBus", "USBIP"])
@pytest.mark.parametrize("health", ["absent", "partial", "unknown"])
def test_switch_declined_unavailable(rig, driver, health):
    r = rig
    r.config.driver_type = "ViGEmBus" if driver == "WinUHid" else "WinUHid"
    r.health[driver] = Status(health)
    r.helpers.verify_winuhid_runtime = r.helpers.verify_vigembus_runtime = lambda **kw: False
    r.ui.answers.append(False)
    old = r.config.driver_type
    assert not resolve(r.service.switch_driver(driver)).ok
    assert r.config.driver_type == old
    assert not any(x[0] == "cleanup" for x in r.trace)


def test_switch_same_driver_and_nonpersisted_preference(rig):
    r = rig
    assert resolve(r.service.switch_driver("WinUHid")).ok
    assert len([x for x in r.trace if x[0] == "save"]) == 1
    assert resolve(r.service.switch_driver("ViGEmBus", persist_preference=False)).ok
    assert r.config.preferred_driver_type == "WinUHid"
    assert r.config.profiles["Default"]["driver_type"] == "WinUHid"


def test_switch_usbip_partial_removes_then_blocks_preference(rig):
    r = rig
    r.health["USBIP"] = Status("partial")
    r.ui.answers.extend([True])
    r.service.run_usbip_uninstall = lambda: setattr(r.service, "_usbip_restart_pending", True) or True
    assert resolve(r.service.switch_driver("USBIP")).reason == "restart_required"
    before = copy.deepcopy(r.config.profiles)
    r.ui.answers.append(False)
    assert resolve(r.service.switch_driver("USBIP")).reason == "restart_required"
    assert r.config.profiles == before


@pytest.mark.parametrize("driver", ["WinUHid", "ViGEmBus"])
def test_switch_partial_repair_failure(rig, driver):
    r = rig
    r.config.driver_type = "USBIP"
    r.health[driver] = Status("partial")
    r.ui.answers.append(True)
    if driver == "WinUHid": r.service.run_driver_uninstall = lambda: False
    else: r.service.run_vigembus_uninstall = lambda: False
    assert not resolve(r.service.switch_driver(driver)).ok
    assert r.config.driver_type == "USBIP"




@pytest.mark.parametrize("driver", ["WinUHid", "ViGEmBus", "USBIP"])
@pytest.mark.parametrize("health", ["healthy", "absent", "partial", "unknown"])
def test_startup_checks_continue_and_initialize_after_check(rig, driver, health):
    r = rig
    r.config.driver_type = driver
    r.health[driver] = Status(health)
    if health in ("absent", "partial") or driver == "USBIP" and health == "unknown":
        r.ui.answers.append(False)
    observed = []
    r.bus.subscribe(PROFILE_OUTPUT_CHANGED, lambda _: observed.append(tuple(r.trace)))
    assert resolve(r.service.startup_driver_check()) is True
    assert not r.service.state().pending_check
    assert any(("keyboard.initialize",) in t for t in observed)


@pytest.mark.parametrize("available,smoke", [(False, False), (False, True), (True, True)])
def test_packaged_startup_restores_or_falls_back(rig, available, smoke):
    r = rig
    r.package.value, r.capability.value = True, available
    r.helpers.verify_winuhid_runtime = lambda **kw: smoke
    assert resolve(r.service.startup_driver_check())
    if available or smoke:
        assert r.config.driver_type == "WinUHid"
        assert "WinUHid" in r.service.state().driver_options
    else:
        assert r.config.driver_type == "ViGEmBus" and r.config.driver_fallback_active
        assert r.config.preferred_driver_type == "WinUHid"
        assert r.service.state().driver_options == ("ViGEmBus", "USBIP")
        assert resolve(r.service.switch_driver("WinUHid")).ok


@pytest.mark.parametrize("packaged", [True, False])
def test_vigem_connection_failure(rig, packaged):
    r = rig
    r.package.value = packaged
    r.config.driver_type = "ViGEmBus"
    r.helpers.verify_vigembus_runtime = lambda **kw: False
    assert not resolve(r.service.check_vigembus_installation())
    assert r.config.driver_type == ("ViGEmBus" if packaged else "WinUHid")
    assert r.ui.calls[-1].args[0] == "ViGEmBus Connection Error"


def test_keyboard_and_mouse_backends_and_wake(rig):
    r = rig
    vc = VC(r.trace, "a")
    r.runtime.current_controllers = [vc, vc]
    assert resolve(r.service.toggle_keyboard_output()).ok
    assert r.service.state().keyboard_backend == "WinUHid Virtual HID"
    assert resolve(r.service.toggle_keyboard_output()).ok
    assert resolve(r.service.toggle_mouse_output()).ok
    assert vc.controllers[0]._interp_wake_event.is_set()
    assert r.config.mouse_output_mode == "Raw Input"
    assert resolve(r.service.toggle_mouse_output()).ok
    r.health["WinUHid"] = Status("absent")
    assert not resolve(r.service.toggle_keyboard_output()).ok
    assert not resolve(r.service.toggle_mouse_output()).ok
    r.health["WinUHid"] = Status("healthy")
    r.kb.activate_raw_input = lambda **kw: False
    assert resolve(r.service.toggle_keyboard_output()).reason == "raw_input_failed"
    assert r.ui.calls[-1].args[0] == "Keyboard Raw Input Error"


def test_wired_events_scan_and_full_power_guard(rig):
    r = rig
    r.bus.publish(events.WIRED_STATE_CHANGED, WiredState(True, (0x2073,)))
    resolve(r.executor.submit(lambda: None))
    assert r.service._wired.product_ids == (0x2073,)
    assert resolve(r.service.toggle_wired_auto_scan()).ok
    assert not r.config.wired_auto_scan_enabled and not r.config.wired_usb_enabled
    assert resolve(r.service.manual_wired_scan()).ok
    assert ("rescan", ("manual_refresh",), {"manual": True}) in r.trace
    r.power.full = True
    assert resolve(r.service.refresh_wired_status()) is False


@pytest.mark.parametrize("install,suppress", [(False, False), (False, True), (True, False), (True, True)])
def test_opt_out_checkbox_contract(rig, install, suppress):
    r = rig
    r.ui.ask_yes_no_opt_out = lambda *a, **kw: (install, suppress)
    assert resolve(r.service.ask_hidhide_auto_install()) is install
    assert r.config.hidhide_install_prompt_suppressed is suppress
    assert bool([x for x in r.trace if x[0] == "save"]) is suppress


def test_opt_out_scripted_single_prompt(rig):
    rig.ui.answers.append((False, True))
    assert not resolve(rig.service.ask_hidhide_auto_install())
    assert rig.config.hidhide_install_prompt_suppressed


def test_wired_poll_prompt_once_replug_and_unknown_retention(rig):
    r = rig
    pad = type("USBHidController", (), {"usb_product_id": 0x2069})()
    r.runtime.current_controllers = [NS(controllers=[pad])]
    r.ui.ask_yes_no_opt_out = lambda *a, **kw: (False, False)
    calls = []
    r.ui.ask_yes_no_opt_out = lambda *a, **kw: calls.append(a) or (False, False)
    assert resolve(r.service.refresh_wired_status())
    assert resolve(r.service.refresh_wired_status())
    assert len(calls) == 1
    r.runtime.current_controllers = []
    resolve(r.service.refresh_wired_status())
    r.runtime.current_controllers = [NS(controllers=[pad])]
    resolve(r.service.refresh_wired_status())
    assert len(calls) == 2
    r.hh.installed = True
    resolve(r.service.refresh_wired_status())
    r.hh.installed = None
    resolve(r.service.refresh_wired_status())
    assert r.service.state().hidhide_installed
    assert r.service.state().hidhide_service_state is None


@pytest.mark.parametrize("succeeds", [True, False])
def test_hidhide_filter_retry_and_preference(rig, succeeds):
    r = rig
    r.hh.installed = True
    writes = []
    def write(target):
        writes.append(target)
        if succeeds and len(writes) == 3: r.hh.active = target
    r.hh.set_active = write
    result = resolve(r.service.toggle_hidhide_active())
    assert result.ok is succeeds
    assert len(writes) == (3 if succeeds else 5)
    assert ("sleep", .05) in r.trace
    assert len([x for x in r.trace if x == ("sleep", .15)]) == (2 if succeeds else 4)
    if succeeds:
        r.hh.set_active = lambda target: setattr(r.hh, "active", target)
        assert resolve(r.service.toggle_hidhide_active()).ok
        assert not r.config.hidhide_hide_enabled
        assert ("unhide", "instance:pad") in r.trace
    else:
        assert "Configuration Client" in r.ui.calls[-1].args[1]


@pytest.mark.parametrize("method,name,driver", [
    ("run_driver_install", "install_driver.ps1", "WinUHid"),
    ("run_driver_uninstall", "uninstall_driver.ps1", "WinUHid"),
    ("run_vigembus_uninstall", "uninstall_vigembus.ps1", "ViGEmBus"),
    ("run_usbip_install", "install_usbip.ps1", "USBIP"),
])
@pytest.mark.parametrize("outcome", ["success", "uac_cancel", "failed", "missing", "exception"])
def test_local_installer_verification_cleanup_progress(rig, method, name, driver, outcome):
    r = rig
    r.runtime.running = True
    if outcome != "missing": payload(r, name)
    if "uninstall" in method:
        r.health[driver] = Status("absent" if outcome == "success" else "healthy")
    else:
        r.health[driver] = Status("absent" if outcome == "failed" else "healthy")
    if outcome == "uac_cancel": r.launch.handle = None
    if outcome == "failed": r.launch.exit = 9
    if outcome == "exception": r.launch.error = RuntimeError("launch failed")
    result = resolve(getattr(r.service, method)())
    assert bool(result) is (outcome == "success")
    assert r.trace[0] == ("stop",)
    assert r.trace[1] == ("emergency",)
    assert r.trace[-1] == ("start",)
    assert r.runtime.running and not r.service.state().busy
    calls = [c.name for c in r.ui.calls]
    if outcome == "missing":
        assert "progress" not in calls
    else:
        assert "progress" in calls and "progress.close" in calls
    launches = [x for x in r.trace if x[0] == "launch"]
    if launches:
        core_ident = resolve(r.executor.submit(threading.get_ident))
        assert launches[0][-1] != core_ident
        assert "-WindowStyle Hidden" in launches[0][2]
    if outcome == "success" and driver == "WinUHid" and "uninstall" in method:
        assert not r.config.driver_installed
        assert r.config.keyboard_output_mode == "Standard"
        assert ("keyboard.standard", False) in r.trace
        assert ("mouse.shutdown",) in r.trace


@pytest.mark.parametrize("code", [0, 9])
def test_usbip_install_eventually_ready_overrides_exit_code(rig, code):
    r = rig
    payload(r, "install_usbip.ps1")
    r.launch.exit = code
    seq = iter([Status("partial"), Status("absent"), Status("healthy")])
    r.helper.get_usbip_status = lambda **kw: next(seq)
    assert resolve(r.service.run_usbip_install())
    assert [x for x in r.trace if x[0] == "sleep"] == [("sleep", 1), ("sleep", 1)]
    assert r.service.state().install_status[2].state == "healthy"


def test_packaged_payloads_no_download_and_winuhid_unavailable(rig, monkeypatch):
    r = rig
    r.package.value = True
    payload(r, "USBip-0.9.7.7-x64.exe")
    assert resolve(r.service.run_usbip_install())
    launch = next(x for x in r.trace if x[0] == "launch")
    assert launch[1].endswith("USBip-0.9.7.7-x64.exe")
    assert "/MERGETASKS" in launch[2]
    assert not resolve(r.service.run_driver_install())
    assert not resolve(r.service.download_and_install_driver("ViGEmBus", lambda: True))
    assert not any(x[0] == "download" for x in r.trace)


@pytest.mark.parametrize("outcome", ["success", "uac_cancel", "failed", "missing"])
def test_usbip_uninstall_restart_gate_no_post_removal_probe(rig, monkeypatch, outcome):
    r = rig
    r.runtime.running = True
    monkeypatch.setattr("app_core.services.drivers_install.os.path.exists", lambda path: outcome != "missing")
    if outcome == "uac_cancel": r.launch.handle = None
    if outcome == "failed": r.launch.exit = 1
    if outcome == "success": r.ui.answers.append(False)
    r.queried.clear()
    assert bool(resolve(r.service.run_usbip_uninstall())) is (outcome == "success")
    assert not r.service.state().busy and r.runtime.running
    assert "USBIP" not in r.queried
    assert r.service.state().usbip_restart_pending is (outcome == "success")
    if outcome == "success":
        r.config.driver_type = "USBIP"
        resolve(r.service.refresh_state())
        assert r.service.state().action_label == "Restart Required"
        assert "USBIP" not in r.queried
        before = copy.deepcopy(r.config.profiles)
        r.ui.answers.append(False)
        assert resolve(r.service.switch_driver("USBIP")).reason == "restart_required"
        assert r.config.profiles == before


@pytest.mark.parametrize("answer", [True, False])
def test_restart_prompt_and_failure(rig, answer):
    r = rig
    r.ui.answers.append(answer)
    resolve(r.executor.submit(r.service._prompt_usbip_restart_required))
    assert bool([x for x in r.trace if x[0] == "restart"]) is answer
    if answer:
        def fail(args): raise OSError("restart denied")
        r.service._modules["subprocess"].Popen = fail
        r.ui.answers.append(True)
        resolve(r.executor.submit(r.service._prompt_usbip_restart_required))
        assert "restart denied" in r.ui.calls[-1].args[1]


@pytest.mark.parametrize("method,name", [("run_hidhide_install", "install_hidhide.ps1"), ("run_hidhide_uninstall", "uninstall_hidhide.ps1")])
@pytest.mark.parametrize("code,installed,expected", [(0, False, True), (3010, False, True), (9, False, False), (9, True, True), (0, None, True)])
def test_hidhide_installer_exit_and_service_truth(rig, method, name, code, installed, expected):
    r = rig
    payload(r, "hidhide/" + name)
    r.launch.exit, r.hh.installed = code, installed
    if method == "run_hidhide_uninstall":
        expected = code in (0, 3010) and installed is False
    if expected: r.ui.answers.append(False)
    assert bool(resolve(getattr(r.service, method)())) is expected
    assert not r.service.state().busy
    if installed is None:
        assert r.config.hidhide_installed is False


@pytest.mark.parametrize("cancel", [True, False])
def test_hidhide_missing_and_uac_cancel(rig, cancel):
    r = rig
    if cancel:
        payload(r, "hidhide/install_hidhide.ps1")
        r.launch.handle = None
    assert not resolve(r.service.run_hidhide_install())
    assert not r.service.state().busy


@pytest.mark.parametrize("outcome", ["success", "download_error", "uac_cancel", "verify_fail", "exit_fail"])
def test_vigem_download_on_worker_and_discovery_restoration(rig, outcome):
    r = rig
    r.runtime.running = True
    if outcome == "download_error":
        def fail(*args): raise OSError("offline")
        r.helper.download_spec_files = fail
    if outcome == "uac_cancel": r.launch.handle = None
    if outcome == "exit_fail": r.launch.exit = 5
    result = resolve(r.service.download_and_install_driver("ViGEmBus", lambda: outcome != "verify_fail"))
    assert bool(result) is (outcome == "success")
    assert r.runtime.running and not r.service.state().busy
    assert r.trace[:2] == [("stop",), ("emergency",)]
    assert r.trace[-1] == ("start",)
    if outcome in ("uac_cancel", "download_error"):
        assert not r.service._last_driver_action_launched


def test_vigem_bundled_install_and_missing(rig, monkeypatch):
    r = rig
    r.package.value = True
    msi = payload(r, "ViGEmBusSetup_x64.msi")
    monkeypatch.setattr("app_core.driver_actions.glob.glob", lambda *a, **kw: [str(msi)])
    assert resolve(r.service.install_vigembus_driver())
    launch = next(x for x in r.trace if x[0] == "launch")
    assert launch[1] == "msiexec.exe" and "/qn /norestart" in launch[2]
    assert not any(x[0] in ("stop", "emergency", "download") for x in r.trace)
    monkeypatch.setattr("app_core.driver_actions.glob.glob", lambda *a, **kw: [])
    assert not resolve(r.service.run_vigembus_install())


@pytest.mark.parametrize("driver,health", [("WinUHid", "healthy"), ("WinUHid", "partial"), ("ViGEmBus", "healthy"), ("ViGEmBus", "partial"), ("USBIP", "healthy"), ("USBIP", "partial"), ("USBIP", "absent")])
@pytest.mark.parametrize("answer", [True, False])
def test_driver_action_routing_prompts(rig, driver, health, answer):
    r = rig
    r.config.driver_type = driver
    r.health[driver] = Status(health)
    r.ui.answers.append(answer)
    actions = []
    for name in ("run_driver_uninstall", "run_driver_install", "run_vigembus_uninstall", "install_vigembus_driver", "run_usbip_uninstall", "run_usbip_install"):
        setattr(r.service, name, lambda *a, name=name, **kw: actions.append(name) or True)
    result = resolve(r.service.on_driver_btn_clicked())
    assert result.ok is answer
    assert bool(actions) is answer
    if answer and health == "partial" and driver != "USBIP": assert len(actions) == 2


@pytest.mark.parametrize("driver", ["WinUHid", "ViGEmBus"])
def test_driver_action_absent_direct_install(rig, driver):
    r = rig
    r.config.driver_type = driver
    r.health[driver] = Status("absent")
    name = "run_driver_install" if driver == "WinUHid" else "install_vigembus_driver"
    setattr(r.service, name, lambda **kw: True)
    assert resolve(r.service.on_driver_btn_clicked()).ok
    assert not [c for c in r.ui.calls if c.name.startswith("ask")]




@pytest.mark.parametrize("installed,answer", [(True, True), (True, False), (False, True), (False, False)])
def test_hidhide_header_action(rig, installed, answer):
    r = rig
    r.hh.installed = installed
    r.ui.answers.append(answer)
    r.service.run_hidhide_install = r.service.run_hidhide_uninstall = lambda: True
    assert bool(resolve(r.service.on_hidhide_button())) is answer


def test_worker_busy_guard_and_core_continuation(rig):
    r = rig
    payload(r, "install_driver.ps1")
    started, release = threading.Event(), threading.Event()
    def wait(handle):
        started.set()
        assert release.wait(5)
        return 0
    r.service._wait_override = wait
    inner = r.service.run_driver_install().result(timeout=5)
    assert isinstance(inner, Future)
    assert started.wait(5)
    assert r.service.state().busy
    assert resolve(r.service.switch_driver("USBIP")).reason == "busy"
    assert resolve(r.service.set_simulation_mode("PS4")).reason == "busy"
    assert not resolve(r.service.run_hidhide_install())
    assert resolve(r.executor.submit(lambda: "core responsive")) == "core responsive"
    release.set()
    assert resolve(inner)
    assert not r.service.state().busy


def test_winuhid_manager_refresh_initialize_and_labels(rig):
    r = rig
    r.kb.mode = "Raw Input"
    r.config.mouse_output_mode = "Raw Input"
    state = resolve(r.service.refresh_winuhid_driver_manager(initialize_keyboard=True))
    assert state.keyboard_backend == state.mouse_backend == "WinUHid Virtual HID"
    assert ("keyboard.initialize",) in r.trace


def test_log_readers_select_important_lines(rig, monkeypatch):
    r = rig
    monkeypatch.setenv("TEMP", str(r.folder))
    payload(r, "Switch2Connect_WinUHid_install.log").write_text("noise\nerror one\nerror one\nfailed two", encoding="utf-8")
    from app_core import driver_logs
    text = driver_logs.read_winuhid_operation_log("install")
    assert text.count("error one") == 1 and "noise" not in text and "Log:" in text
    assert "not available" in driver_logs.read_winuhid_operation_log("uninstall")
    assert "not available" in driver_logs.read_vigembus_uninstall_log()
    payload(r, "Switch2Connect_ViGEmBus_uninstall.log").write_text("noise\nfailed removal", encoding="utf-8")
    assert driver_logs.read_vigembus_uninstall_log() == "failed removal"


@pytest.mark.parametrize("driver", ["WinUHid", "ViGEmBus", "USBIP"])
def test_switch_repair_or_install_completes_before_recreation(rig, driver):
    r = rig
    r.config.driver_type = "USBIP" if driver == "WinUHid" else "WinUHid"
    r.health[driver] = Status("partial" if driver != "USBIP" else "absent")
    r.ui.answers.append(True)
    r.runtime.current_controllers = [VC(r.trace, "a")]
    r.service.run_driver_uninstall = r.service.run_vigembus_uninstall = lambda: True
    def install(**kw):
        r.trace.append(("fake_install", driver))
        r.health[driver] = Status("healthy")
        return True
    r.service.run_driver_install = r.service.install_vigembus_driver = r.service.run_usbip_install = install
    assert resolve(r.service.switch_driver(driver)).ok
    assert r.trace.index(("fake_install", driver)) < r.trace.index(("cleanup", "a"))
    assert r.config.driver_type == driver


def test_switch_installer_failure_reverts_selection(rig):
    r = rig
    r.health["USBIP"] = Status("absent")
    r.ui.answers.append(True)
    r.service.run_usbip_install = lambda **kw: False
    assert resolve(r.service.switch_driver("USBIP")).reason == "driver_unavailable"
    assert r.service.state().driver_type == "WinUHid"


def test_switch_schedules_leds_on_each_vc_loop(rig, monkeypatch):
    r = rig
    vc = VC(r.trace, "a")
    vc.loop = NS(is_running=lambda: True)
    async def leds(): pass
    vc.update_leds = leds
    r.runtime.current_controllers = [vc]
    submitted = []
    def schedule(coro, loop):
        submitted.append(loop)
        coro.close()
    monkeypatch.setattr("app_core.driver_transitions.asyncio.run_coroutine_threadsafe", schedule)
    assert resolve(r.service.switch_driver("ViGEmBus")).ok
    assert submitted == [vc.loop]


@pytest.mark.parametrize("health", ["absent", "partial"])
@pytest.mark.parametrize("driver", ["WinUHid", "ViGEmBus", "USBIP"])
def test_startup_accepts_install_or_repair(rig, health, driver):
    r = rig
    r.config.driver_type = driver
    r.health[driver] = Status(health)
    r.ui.answers.append(True)
    actions = []
    for name in ("run_driver_uninstall", "run_driver_install", "run_vigembus_uninstall", "install_vigembus_driver", "run_usbip_uninstall", "run_usbip_install"):
        setattr(r.service, name, lambda *a, name=name, **kw: actions.append(name) or True)
    assert resolve(r.service.startup_driver_check())
    assert actions
    if driver != "USBIP" and health == "partial": assert len(actions) == 2


def test_elevation_native_flags_and_process_handle_close(rig, monkeypatch):
    import ctypes
    import sys
    r = rig
    class ShellInfo(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("fMask", ctypes.c_ulong),
                    ("hwnd", ctypes.c_void_p), ("lpVerb", ctypes.c_wchar_p),
                    ("lpFile", ctypes.c_wchar_p), ("lpParameters", ctypes.c_wchar_p),
                    ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int),
                    ("hProcess", ctypes.c_void_p)]
    monkeypatch.setitem(sys.modules, "app_core.helpers", NS(SHELLEXECUTEINFOW=ShellInfo))
    calls = []
    def execute(pointer):
        info = pointer._obj
        calls.append((info.lpVerb, info.nShow, info.fMask, info.hwnd))
        info.hProcess = 123
        return True
    waits = iter([258, 0])
    def exit_code(handle, pointer):
        pointer._obj.value = 3010
        return True
    fake = NS(shell32=NS(ShellExecuteExW=execute),
              user32=NS(AllowSetForegroundWindow=lambda value: calls.append(value)),
              kernel32=NS(WaitForSingleObject=lambda *a: next(waits), GetExitCodeProcess=exit_code,
                          CloseHandle=lambda handle: calls.append(("close", handle))))
    monkeypatch.setattr(ctypes, "windll", fake)
    r.service._launch_override = r.service._wait_override = None
    r.ui.native_window_handle = lambda: 456
    handle = r.service._launch_elevated("powershell.exe", "-NoProfile")
    assert handle == 123
    assert ("runas", 0, 0x40, 456) in calls
    assert r.service._wait_process(handle) == 3010
    assert calls[-1] == ("close", 123)
    assert ("sleep", .2) in r.trace
    fake.shell32.ShellExecuteExW = lambda info: False
    assert r.service._launch_elevated("powershell.exe", "") is None


def test_vigembus_action_independent_of_active_driver(rig):
    rig.service._vigem_action = lambda: 'vigem_flow'
    rig.service._flow = lambda flow: flow
    before = (rig.config.driver_type, rig.config.simulation_mode)
    assert resolve(rig.service.on_vigembus_driver_action()) == 'vigem_flow'
    assert (rig.config.driver_type, rig.config.simulation_mode) == before
    rig.service._busy = True
    assert not resolve(rig.service.on_vigembus_driver_action()).ok


def test_mode_rollback_layout_revert_and_rumble(rig):
    r = rig
    a, b = VC(r.trace, "a"), VC(r.trace, "b", fail=True)
    r.runtime.current_controllers = [a, b]
    assert not resolve(r.service.set_simulation_mode("PS4")).ok
    assert a.mode == r.config.simulation_mode == "PS5"
    assert resolve(r.service.set_simulation_mode("PS4")).ok
    assert r.config.winuhid_sim_mode == "PS4"
    assert resolve(r.service.set_simulation_mode("PS4")).ok
    assert resolve(r.service.set_layout("Switch")).ok
    assert r.config.abxy_mode == "Switch"
    r.config.driver_type = "USBIP"
    r.config.rumble_mode = "Switch"
    resolve(r.service.set_simulation_mode("PS5"))
    assert r.config.rumble_mode == "PS5"
