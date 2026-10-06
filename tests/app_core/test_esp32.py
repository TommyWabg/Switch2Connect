from concurrent.futures import Future
from dataclasses import FrozenInstanceError
from types import SimpleNamespace as NS

import pytest

from app_core.bus import EventBus
from app_core import events
from app_core.models import WiredState
from app_core.services.esp32 import Esp32Service, ESP32_FIRMWARE_SESSION
from app_core.ui_port import ScriptedUiPort


class Executor:
    def __init__(self):
        self.timers = []
        self.core = True

    def is_core_thread(self):
        return self.core

    def submit(self, fn, *args):
        future = Future()
        try:
            previous, self.core = self.core, True
            future.set_result(fn(*args))
        except BaseException as exc:
            future.set_exception(exc)
        finally:
            self.core = previous
        return future

    def call_later(self, seconds, fn):
        timer = NS(seconds=seconds, fn=fn, cancelled=False, repeat=False)
        timer.cancel = lambda: setattr(timer, "cancelled", True)
        self.timers.append(timer)
        return timer

    def call_every(self, seconds, fn):
        timer = self.call_later(seconds, fn)
        timer.repeat = True
        return timer

    def tick(self, seconds):
        timer = next(t for t in self.timers if t.seconds == seconds and not t.cancelled)
        if not timer.repeat:
            timer.cancelled = True
        timer.fn()


def port(name="COM4", **kw):
    return NS(port=name, name="Board", manufacturer="Vendor", device_id="USB-id",
              serial_number="serial", location="location", is_otg=kw.get("is_otg", False), confidence=80)


def status(*, present=True, ready=False, installed=True, update=False, otg=False, unverified=False, version="1", mode="bridge", expected="2"):
    return NS(board_present=present, serial_port=port(is_otg=otg) if present or unverified else None,
              bridge_ready=ready, firmware_installed=installed, firmware_current=ready,
              firmware_update_required=update, otg_only=otg, firmware_version=version,
              firmware_mode=mode, expected_version=expected, status_text="status" if installed else "",
              unverified_candidate=unverified)


@pytest.fixture
def env():
    log, workers, probes, flash = [], [], [], []
    bus, executor, ui = EventBus(), Executor(), ScriptedUiPort()
    config = NS(esp32_serial_port_mode="auto", esp32_serial_port="", esp32_serial_device_id="old-id",
                esp32_serial_number="old-serial", esp32_serial_location="old-location")
    config.save_config = lambda: log.append("save")
    runtime = NS(config=config, is_quitting=False, current_controllers=[], wired_state=WiredState(), running=True)
    runtime.is_discoverer_running = lambda: runtime.running
    runtime.stop_discoverer = lambda: (log.append(("stop", service.state().busy)), setattr(runtime, "running", False))
    runtime.start_discoverer = lambda context=None: (log.append(("start", context)), setattr(runtime, "running", True))
    def detect(*args, **kwargs):
        log.append(("detect", args, kwargs))
        result = probes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
    def resolve(requested, **kwargs):
        log.append(("resolve", requested, kwargs))
        return kwargs["preferred_info"] or port(requested or "COM4")
    def flashing(selected, mode, progress):
        log.append(("flash", selected, mode))
        for payload in flash:
            if isinstance(payload, Exception):
                raise payload
            progress(payload)
    bridge = NS(ESP32S3_LABEL="ESP32-S3 CDC", BRIDGE_SCAN_ACTIVE=True, detect_bridge=detect,
                list_bridge_port_candidates=lambda: [port(), port("COM9")],
                format_port_label=lambda p, verified=False: p.port + (" verified" if verified else " candidate"),
                close_all_clients=lambda: log.append("close"), resolve_flash_port=resolve,
                stabilize_flash_port=lambda p: p, flash_firmware=flashing,
                flash_log=lambda msg: log.append(msg), get_flash_log_path=lambda: "flash.log")
    discoverer = NS(emergency_cleanup=lambda: log.append("cleanup"), is_system_bluetooth_available=lambda: True)
    power = NS(is_full=lambda: False)
    service = Esp32Service(bus, executor, ui, runtime, bridge=bridge, discoverer=discoverer,
                           power_saving=power, worker_start=workers.append)
    def work():
        workers.pop(0)()
        if any(t.seconds == .05 and not t.cancelled for t in executor.timers):
            executor.tick(.05)
    return NS(**locals())


def test_startup_context_history_and_immutable_snapshots(env):
    e = env
    e.probes.append(status(ready=True))
    future = e.service.startup_detect_and_maybe_update()
    assert not future.done()
    e.work()
    assert future.result()["status"].bridge_ready
    assert e.service._was_detected and e.service._current_seen
    assert e.service.state().device_id == "USB-id"
    assert (e.service.status().connection_route, e.service.status().text) == ("ESP32-S3", "Ready")
    assert e.service.status().color == "#55CC55"
    assert e.service.startup_detect_and_maybe_update() is future
    with pytest.raises(FrozenInstanceError):
        e.service.state().busy = True
    e.probes.append(status(ready=True))
    e.service.refresh_status_async()
    e.work()
    assert not any(t.seconds == .1 for t in e.executor.timers)


@pytest.mark.parametrize("live,running,restart", [(False, True, True), (True, True, False), (False, False, False)])
def test_hotplug_restart_only_empty_running_discovery(env, live, running, restart):
    e = env
    e.runtime.current_controllers = [object()] if live else []
    e.runtime.running = running
    e.probes.append(status(ready=True))
    assert e.service.refresh_status_async()
    assert not e.service.refresh_status_async()
    e.work()
    assert e.service.state().busy == restart
    if restart:
        assert not e.service.refresh_status_async()
        e.executor.tick(.1)
        assert e.log[-1][0] == "start"
        e.executor.tick(4)
        assert not e.service.state().busy


def test_readiness_edge_and_no_repeat_restart(env):
    e = env
    e.service._set_status(status(ready=False))
    e.probes.append(status(ready=True))
    e.service.refresh_status_async(); e.work()
    assert e.service.state().busy
    e.executor.tick(.1); e.executor.tick(4)
    e.probes.append(status(ready=True))
    e.service.refresh_status_async(); e.work()
    assert not e.service.state().busy


def test_transient_cdc_failure_retains_ready(env):
    probe = status(installed=False)
    e = env
    e.service._set_status(status(ready=True))
    old = e.service.state()
    e.probes.append(probe)
    e.service.refresh_status_async(); e.work()
    assert e.service.state() is old
    assert e.service._current_seen


def test_otg_boot_transition_and_fast_poll(env):
    e = env
    e.service._set_status(status(ready=True, otg=True))
    e.probes.append(status(installed=False, otg=True))
    e.service.refresh_status_async(); e.work()
    assert e.service.state().otg_boot
    assert e.service.status().text == "Disconnect"
    e.probes.append(status(installed=False, otg=True))
    e.executor.tick(.5); e.work()
    assert e.service.status().text == "Boot"
    e.executor.tick(.5)
    assert not e.service._fast_active


def test_fast_poll_thirty_attempt_limit_and_quit(env):
    e = env
    e.service._header = NS(text="Disconnect")
    e.service._fast_active = True
    e.service._busy = True
    for _ in range(30):
        e.service._fast_poll()
    assert not e.service._fast_active
    assert not e.workers
    e.runtime.is_quitting = True
    e.service._fast_active = True
    e.service._fast_poll()
    assert not e.service._fast_active


@pytest.mark.parametrize("bt,wired,route,text,color", [
    (True, False, "System BLE", "Ready", "#55CC55"),
    (False, False, "USB", "Pending USB Connection", "#888888"),
    (False, True, "USB", "USB Connected", "#55CC55"),
])
def test_header_system_and_wired_events(env, bt, wired, route, text, color):
    e = env
    e.discoverer.is_system_bluetooth_available = lambda: bt
    e.bus.publish(events.WIRED_STATE_CHANGED, WiredState(wired))
    assert (e.service.status().connection_route, e.service.status().text, e.service.status().color) == (route, text, color)


def test_header_scan_inactive_update_error_and_bt_failure(env):
    e = env
    e.bridge.BRIDGE_SCAN_ACTIVE = False
    e.service._set_status(status(ready=True))
    assert e.service.status().text == "Initializing"
    e.service._set_status(status(update=True))
    assert e.service.status().level == "error"
    def fail():
        raise RuntimeError()
    e.discoverer.is_system_bluetooth_available = fail
    e.service._set_status(None)
    assert e.service.status().text == "Ready"


def test_periodic_full_power_saving_and_callbacks(env):
    e = env
    calls = []
    e.power.is_full = lambda: True
    e.service.start_periodic([lambda: calls.append("wired")])
    assert not e.workers
    e.power.is_full = lambda: False
    e.executor.tick(5)
    assert len(e.workers) == 1 and calls == ["wired"]
    e.service.start_periodic()
    assert len([t for t in e.executor.timers if t.repeat]) == 1
    e.service.close()
    assert all(t.cancelled for t in e.executor.timers)


def test_manager_candidates_and_port_selection_save_identity(env):
    e = env
    e.service._set_status(status(ready=True))
    choices = e.service.list_ports()
    assert choices[0].verified and not choices[1].verified
    e.probes.append(status(present=False, installed=False, unverified=True))
    assert e.service.set_port_mode(choices[1]).ok
    assert e.config.esp32_serial_port_mode == "manual" and e.config.esp32_serial_port == "COM9"
    assert e.config.esp32_serial_device_id == "old-id"
    e.work()
    assert e.service.state().unverified_candidate and not e.service.state().detected
    assert e.service.state().firmware_text == "Unverified flashing candidate"
    e.probes.append(None)
    assert e.service.set_port_mode("Auto").ok
    e.work()
    assert e.service.port_mode().mode == "auto"
    assert e.log.count("save") == 2


def test_port_selection_rejected_busy_without_config_changes(env):
    e = env
    e.service._busy = True
    assert not e.service.set_port_mode("COM4").ok
    assert e.config.esp32_serial_port == ""
    assert not e.service.refresh_manager_status_async()
    assert e.service.list_ports() == ()


@pytest.mark.parametrize("action", ["install", "repair"])
def test_manual_flash_order_progress_and_two_absent_samples(env, action):
    e = env
    sessions = []
    e.bus.subscribe(ESP32_FIRMWARE_SESSION, sessions.append)
    e.service._manager_status = status()
    e.flash.extend(["diagnostic", {"write_percent": 50}, {"percent": 20}, "Writing (100 %)"])
    future = getattr(e.service, action)()
    assert e.log[:3] == [("stop", True), "cleanup", "close"]
    assert e.service.state().busy
    assert not e.service.close_firmware_session().ok
    e.work()
    assert future.result().ok
    assert e.service.firmware_session().phase == "awaiting_replug"
    assert e.service.firmware_session().can_close
    assert [s.percent for s in sessions if s.phase == "flashing"] == [0, 0, 0, 60, 60, 95]
    assert e.service.state().busy and not e.service.refresh_status_async()
    e.probes.extend([None, None])
    e.executor.tick(1.5); e.work()
    assert e.service._waiting
    e.executor.tick(.5); e.work()
    assert not e.service.state().busy
    assert not e.service._current_seen
    assert e.service.firmware_session().phase == "done"
    assert e.log[-1] == ("start", None)


def test_removal_reinsert_context_leaves_readiness_edge_unconsumed(env):
    e = env
    e.service._manager_status = status()
    e.service.install(); e.work()
    ready = status(ready=True)
    e.probes.extend([None, ready])
    e.executor.tick(1.5); e.work()
    e.executor.tick(.5); e.work()
    assert e.service._was_detected and not e.service._current_seen
    assert e.log[-1][1]["status"] is ready


def test_removal_detection_error_is_not_physical_removal(env):
    e = env
    e.service._manager_status = status()
    e.service.install(); e.work()
    e.probes.extend([RuntimeError("probe"), RuntimeError("probe"), status(ready=True)])
    e.executor.tick(1.5); e.work()
    e.executor.tick(.5); e.work()
    assert e.service._waiting
    e.executor.tick(.5); e.work()
    assert e.service._waiting
    assert e.service.close_firmware_session().ok
    assert not e.service._busy
    starts = len([x for x in e.log if isinstance(x, tuple) and x[0] == "start"])
    e.service.close_firmware_session()
    assert len([x for x in e.log if isinstance(x, tuple) and x[0] == "start"]) == starts


@pytest.mark.parametrize("answer", [True, False])
def test_delete_prompt_and_success(env, answer):
    e = env
    e.ui.answers.append(answer)
    e.service._manager_status = status()
    result = e.service.delete()
    if answer:
        e.work()
        e.probes.append(None); e.work()
        assert result.result().ok
        assert e.service.firmware_session().phase == "done"
        assert not e.service.state().busy
    else:
        assert not result.ok and result.reason == "cancelled"
        assert not e.workers and "cleanup" not in e.log
    call = next(c for c in e.ui.calls if c.name == "ask_yes_no")
    assert call.args == ("ESP32-S3 CDC", "Erase ESP32-S3 N16R8 firmware?")




def test_otg_boot_manual_flash_allowed_but_never_auto_flash(env):
    e = env
    e.service._manager_status = status(otg=True, installed=False)
    result = e.service.install(); e.work()
    assert result.result().ok
    e.service.close_firmware_session()
    e.service._set_status(status(update=True, otg=True))
    assert not e.service._maybe_auto_update(e.service._status)
    assert not e.service._auto_attempted


@pytest.mark.parametrize("failure,expected", [
    (RuntimeError("bad"), "firmware operation failed"),
    (RuntimeError("Could not communicate with ESP32-S3"), "Could not enter flashing mode"),
    (RuntimeError("Could not put ESP32-S3 into flashing mode"), "Could not enter flashing mode"),
])
def test_flash_error_resumes_and_preserves_guidance(env, failure, expected):
    e = env
    e.service._manager_status = status()
    e.flash.append(failure)
    future = e.service.repair(); e.work()
    e.probes.append(None); e.work()
    assert not future.result().ok
    assert expected in e.service.firmware_session().message
    assert e.service.firmware_session().can_close
    assert not e.service.state().busy and e.runtime.running
    assert any(isinstance(x, str) and "flash failed:" in x for x in e.log)


def test_resolver_failure_rolls_back_discovery_and_busy(env):
    e = env
    def fail(*a, **kw):
        raise RuntimeError("missing port")
    e.bridge.resolve_flash_port = fail
    result = e.service.install(); e.work()
    assert not result.result().ok
    assert not e.service.state().busy and e.runtime.running
    assert not any(isinstance(x, tuple) and x[0] == "flash" for x in e.log)


def test_auto_update_dedupe_tuple_and_busy_order(env):
    e = env
    old = status(update=True)
    assert e.service._maybe_auto_update(old)
    assert e.log[0] == ("stop", False)
    e.work()
    assert e.service.state().busy
    e.probes.append(status(ready=True))
    e.executor.tick(1); e.work()
    assert not e.service.state().busy and e.runtime.running
    assert not e.service._maybe_auto_update(old)
    assert e.service._maybe_auto_update(status(update=True, expected="3"))
    assert len(e.service._auto_attempted) == 2


@pytest.mark.parametrize("change", ["board", "update", "text", "version", "port", "otg"])
def test_auto_update_guards(env, change):
    e = env
    s = status(update=True)
    setattr(s, {"board": "board_present", "update": "firmware_update_required", "text": "status_text",
                "version": "firmware_version", "port": "serial_port", "otg": "otg_only"}[change], True if change == "otg" else None)
    assert not e.service._maybe_auto_update(s)
    assert not e.workers


def test_startup_auto_update_success_delayed_current_wait(env):
    e = env
    e.runtime.running = False
    e.probes.append(status(update=True))
    result = e.service.startup_detect_and_maybe_update(); e.work(); e.work()
    assert not result.done()
    # Classic completion clears busy and schedules a normal refresh, then waits.
    e.probes.append(status(ready=True)); e.work()
    e.probes.append(status(ready=False)); e.executor.tick(1); e.work()
    assert not result.done()
    e.probes.append(status(ready=True)); e.executor.tick(.5); e.work()
    assert result.result() is None
    assert not e.runtime.running
    assert e.service._was_detected and e.service._current_seen


def test_startup_auto_update_failure_continues(env):
    e = env
    e.runtime.running = False
    e.probes.append(status(update=True))
    e.flash.append(RuntimeError("failed"))
    result = e.service.startup_detect_and_maybe_update(); e.work(); e.work()
    e.probes.append(None); e.work(); e.executor.tick(0)
    assert result.done() and result.result() is None


def test_current_wait_disappearance_and_attempt_limit(env):
    e = env
    done = []
    e.probes.append(None)
    e.service._wait_current(lambda: done.append(True)); e.work()
    assert done == [True] and not e.service._was_detected
    e.probes.append(status())
    e.service._wait_current(lambda: done.append(True), remaining=0); e.work()
    assert done == [True, True]


def test_manager_probe_failure_retains_snapshot_and_list_error(env):
    e = env
    e.service._set_status(status(ready=True))
    old = e.service.state()
    e.probes.append(PermissionError())
    assert e.service.refresh_manager_status_async(); e.work()
    assert e.service.state() is old
    def fail():
        raise RuntimeError()
    e.bridge.list_bridge_port_candidates = fail
    assert e.service.list_ports() == ()


def test_close_cancels_pending_startup_and_off_core_dispatch(env):
    e = env
    e.executor.core = False
    submitted = e.service.startup_detect_and_maybe_update()
    result = submitted.result()
    e.service.close().result()
    assert result.result() is False
    e.probes.append(status(ready=True)); e.work()
    assert not e.service.state().detected
    assert e.service.install().result().reason == "quitting"


def test_chosen_different_running_otg_port_cannot_bypass_guard(env):
    e = env
    e.service._manager_status = status()
    e.bridge.resolve_flash_port = lambda *a, **kw: port("COM9", is_otg=True)
    e.probes.append(status(otg=True))
    future = e.service.install("COM9"); e.work()
    assert not future.result().ok
    assert "not in Boot mode" in e.service.firmware_session().message
    assert not any(isinstance(x, tuple) and x[0] == "flash" for x in e.log)
    assert e.runtime.running and not e.service._busy


def test_erase_and_error_probe_before_discovery_resume(env):
    e = env
    e.ui.answers.append(True)
    result = e.service.delete(); e.work()
    assert not result.done() and not e.runtime.running
    e.probes.append(None); e.work()
    assert result.result().ok
    assert [x[0] for x in e.log if isinstance(x, tuple)][-2:] == ["detect", "start"]


def test_normal_probe_failure_falls_back_and_startup_failure_continues(env):
    e = env
    e.probes.append(RuntimeError("no device"))
    future = e.service.startup_detect_and_maybe_update(); e.work()
    assert future.result() is None and not e.service.state().detected
    e.probes.append(RuntimeError("no device"))
    assert e.service.refresh_status_async(); e.work()
    assert e.service.status().connection_route == "System BLE"


def test_wait_current_full_twenty_four_half_second_retries(env):
    e = env
    done = []
    e.probes.extend([status()] * 25)
    e.service._wait_current(lambda: done.append(True)); e.work()
    for _ in range(24):
        assert not done
        e.executor.tick(.5); e.work()
    assert done == [True] and not e.probes


def test_old_removal_timer_cannot_probe_new_session(env):
    e = env
    e.service.install(); e.work()
    assert e.service.close_firmware_session().ok
    e.service.install(); e.work()
    assert not e.workers
    e.executor.tick(1.5)  # first session's stale settle timer
    assert not e.workers
    e.probes.append(None)
    e.executor.tick(1.5); e.work()
    assert e.service._waiting


def test_shutdown_during_flash_resolves_future_without_restart(env):
    e = env
    result = e.service.install()
    e.bus.publish(events.QUIT_REQUESTED)
    assert result.result().reason == "quitting"
    assert all(t.cancelled for t in e.executor.timers)
    e.work()
    assert not e.runtime.running


def test_real_executor_worker_events_and_config_on_core():
    import threading
    from app_core.executor import CoreExecutor
    from app_core.ui_port import NullUiPort
    executor, bus = CoreExecutor(), EventBus()
    threads = []
    bus.subscribe(events.ESP32_STATE_CHANGED, lambda _: threads.append(executor.is_core_thread()))
    runtime = NS(is_quitting=False, current_controllers=[], running=False, config=NS(esp32_serial_port_mode="auto"))
    runtime.is_discoverer_running = lambda: False
    runtime.start_discoverer = lambda context=None: None
    runtime.stop_discoverer = lambda: None
    detector_threads = []
    def detect():
        detector_threads.append(threading.current_thread())
        return status(ready=True)
    bridge = NS(detect_bridge=detect, BRIDGE_SCAN_ACTIVE=True)
    service = Esp32Service(bus, executor, NullUiPort(), runtime, bridge=bridge)
    try:
        startup = service.startup_detect_and_maybe_update().result(timeout=2)
        assert startup.result(timeout=2)["status"].bridge_ready
        assert threads and all(threads)
        assert detector_threads[0] is not threading.current_thread()
        service.close().result(timeout=2)
    finally:
        executor.shutdown()


def test_import_does_not_load_hardware_or_toolkits():
    import subprocess
    import sys
    from pathlib import Path
    source = str(Path(__file__).resolve().parents[2] / "src")
    script = f"""
import sys
sys.path.insert(0, {source!r})
import app_core.services.esp32
assert not any(name in sys.modules for name in ('tkinter', 'PySide6', 'usb_serial_bridge', 'discoverer', 'config'))
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_flash_progress_and_completion_use_classic_fifty_ms_queue(env):
    e = env
    e.flash.append({"percent": 50})
    result = e.service.install()
    e.workers.pop(0)()
    assert not result.done()
    assert e.service.firmware_session().percent == 0
    e.executor.tick(.05)
    assert result.result().ok
    assert e.service.firmware_session().percent == 100


@pytest.mark.parametrize("action", ["install", "repair"])
def test_otg_running_blocked_before_cleanup(env, action):
    e = env
    e.service._manager_status = status(otg=True)
    result = getattr(e.service, action)()
    assert not result.ok and result.reason == "boot_mode_required"
    assert not e.workers and not e.log
    assert "Wait for Status" in e.ui.calls[-1].args[1]


def test_raised_probe_error_clears_status_like_classic(env):
    e = env
    e.service._set_status(status(ready=True))
    e.probes.append(PermissionError("occupied"))
    e.service.refresh_status_async(); e.work()
    assert not e.service.state().detected
    assert not e.service._current_seen
