"""Composition tests exercise real services against inert hardware modules."""
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace as NS
import ast
import sys
import threading

import pytest

from app_core import events, models
from app_core.app import AppCore
from app_core.runtime import AppRuntime
from app_core.executor import CoreExecutor
from app_core.ui_port import ScriptedUiPort
from test_settings_service import Config, catalogue
from test_drivers import rig  # noqa: F401  (fixture)


@pytest.fixture
def composed(rig, monkeypatch):
    trace, timers = [], []
    config = Config()
    config.profiles = {"Base": {"driver_type": "WinUHid", "simulation_mode": "PS5"}}
    config.abxy_mode = "Xbox"
    config.profile_switching_combo_trigger = ""
    config.get_default_profile_dict = lambda: config.profiles["Base"].copy()
    config.active_profile = "Base"
    modules = rig.service._modules.copy()
    modules["config"] = NS(**vars(catalogue()), CONFIG=config,
                         packaged_winuhid_available=lambda: True)
    modules["keyboard_output"] = NS(initialize=lambda: trace.append("keyboard"),
        get_backend_label=lambda: "Win32 API", is_available=lambda: True,
        effective_mode=lambda: "Standard", RAW_INPUT="Raw Input")
    modules["discoverer"].set_shutting_down = lambda value: trace.append("runtime.shutdown")
    modules["usb_serial_bridge"] = NS(detect_bridge=lambda: trace.append("detect") or None,
        shutdown_all_bridges=lambda: trace.append("bridges.close"))
    modules["virtual_controller"] = NS(detach_all_usbip_devices=lambda **kw: None)
    for name, module in modules.items():
        if name != "app_core.helpers":
            monkeypatch.setitem(sys.modules, name, module)
    from app_core.services.drivers import DriverService
    monkeypatch.setattr(DriverService, "_module", lambda self, name: modules[name])
    class Listener:
        def __init__(self, *a): pass
        def start(self): trace.append("listener.start")
        def stop(self): trace.append("listener.stop")
    monkeypatch.setattr("app_core.win_listeners.PowerListener", Listener)
    monkeypatch.setattr("app_core.win_listeners.WiredDeviceChangeListener", Listener)
    monkeypatch.setattr(AppRuntime, "start_discoverer", lambda self, context=None: trace.append(("discover", context)))
    original = CoreExecutor.call_every
    def every(self, seconds, fn, **kw):
        timer = original(self, seconds, fn, **kw)
        timers.append((seconds, fn, timer))
        return timer
    monkeypatch.setattr(CoreExecutor, "call_every", every)
    core = AppCore(ScriptedUiPort(), config=config)
    core.executor.submit(lambda: None).result(timeout=5)
    yield NS(core=core, trace=trace, timers=timers, config=config)
    if core._shutdown_future is None:
        core.shutdown(lambda: None).result(timeout=5)
    core.executor.shutdown(timeout=5)


def test_startup_order_and_jobs(composed, monkeypatch):
    c, trace = composed.core, composed.trace
    pending = Future()
    def check(self, **kw):
        assert kw == {"initialize_keyboard": False}
        trace.append("driver")
        self._executor.submit(pending.set_result, models.ActionResult(True))
        return pending
    monkeypatch.setattr(type(c.drivers), "startup_driver_check", check)
    c.bus.subscribe(events.PROFILES_CHANGED, lambda _: trace.append("profiles"))
    trace.clear()
    completion = c.start()
    assert completion.result(timeout=5) is True
    assert c.start() is completion
    sequence = [x for x in trace if x in ("detect", "driver", "keyboard") or isinstance(x, tuple)]
    assert sequence == ["detect", "driver", "keyboard", ("discover", None)]
    assert trace.index("keyboard") < len(trace) - 1
    assert "profiles" in trace[trace.index("keyboard") + 1:]
    assert sorted(t[0] for t in composed.timers) == [.25, 1., 5., 300.]
    before = len(composed.timers)
    c.start().result(timeout=5)
    assert len(composed.timers) == before
    assert c.runtime.config is composed.config


def test_full_guard_and_shared_tick(composed, monkeypatch):
    c = composed.core
    c.start().result(timeout=5)
    calls = []
    monkeypatch.setattr(c.esp32, "refresh_status_async", lambda: calls.append("esp32"))
    c.esp32._callbacks = (lambda: calls.append("wired"),)
    power = sys.modules["power_saving"]
    monkeypatch.setattr(power, "is_full", lambda: True)
    c.executor.submit(c.esp32._periodic_tick).result(timeout=5)
    assert calls == []
    monkeypatch.setattr(power, "is_full", lambda: False)
    c.executor.submit(c.esp32._periodic_tick).result(timeout=5)
    assert calls == ["esp32", "wired"]


def test_shutdown_orders_services(composed, monkeypatch):
    c = composed.core
    c.start().result(timeout=5)
    order = []
    for service, method, label in [(c.profiles, "stop", "profiles"),
                                    (c.settings, "close", "settings"),
                                    (c.calibration, "close", "calibration"),
                                    (c.esp32, "close", "esp32")]:
        original = getattr(service, method)
        def wrapped(original=original, label=label):
            order.append(label)
            return original()
        monkeypatch.setattr(service, method, wrapped)
    done_threads = []
    future = c.shutdown(lambda: (order.append("done"), done_threads.append(c.executor.is_core_thread())))
    future.result(timeout=5)
    c.executor.shutdown(timeout=5)
    assert all(t.cancelled for seconds, _, t in composed.timers if seconds in (1, 5))
    assert order == ["profiles", "settings", "calibration", "esp32", "done"]
    assert composed.trace.index("runtime.shutdown") < composed.trace.index("bridges.close")
    assert done_threads == [True]
    assert c.executor._stopped


def test_snapshot(composed):
    c = composed.core
    snapshots = c.snapshot_all().result(timeout=5)
    assert len(snapshots[events.CONTROLLERS_CHANGED]) == 4
    assert snapshots[events.SETTINGS_CHANGED] is c.settings.state()


def test_initial_snapshot_waits_for_constructor_driver_refresh(composed, monkeypatch):
    from app_core.services.drivers import DriverService
    entered, release = threading.Event(), threading.Event()
    original = DriverService.refresh_state
    refresh_threads = []

    def delayed(self):
        entered.set()
        assert release.wait(timeout=5)
        refresh_threads.append(self._executor.is_core_thread())
        return original(self)

    monkeypatch.setattr(DriverService, "refresh_state", delayed)
    composed.config.abxy_mode = "Switch"
    core = AppCore(ScriptedUiPort(), config=composed.config)
    try:
        assert entered.wait(timeout=5)
        snapshot = core.snapshot_all()
        assert isinstance(snapshot, Future) and not snapshot.done()
        release.set()
        driver = snapshot.result(timeout=5)[events.DRIVER_STATE_CHANGED]
        assert driver.abxy_mode == "Switch"
        assert driver.driver_type == composed.config.driver_type
        assert refresh_threads == [True]
        assert core.executor.submit(core.snapshot_all).result(timeout=5)[events.DRIVER_STATE_CHANGED] is driver
    finally:
        release.set()
        core.shutdown(lambda: None).result(timeout=5)
        core.executor.shutdown(timeout=5)


def test_every_topic_has_a_publisher():
    # Audit AST rather than requiring destructive driver/firmware actions just to
    # observe every event. Existing service tests exercise those action branches.
    topics = {value for name, value in vars(events).items() if name.isupper() and isinstance(value, str)}
    published = set()
    for path in (Path(__file__).parents[2] / "src/app_core").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "publish" and node.args:
                arg = node.args[0]
                name = arg.attr if isinstance(arg, ast.Attribute) else arg.id if isinstance(arg, ast.Name) else None
                if name and hasattr(events, name):
                    published.add(getattr(events, name))
    assert topics <= published

def test_startup_preserves_bridge_context(composed, monkeypatch):
    c = composed.core
    context = {"status": object(), "observed_mono": 123.0}
    detected = Future()
    monkeypatch.setattr(c.esp32, "startup_detect_and_maybe_update", lambda: detected)
    monkeypatch.setattr(c.drivers, "startup_driver_check", lambda **kw: models.ActionResult(True))
    future = c.start()
    c.executor.submit(lambda: None).result(timeout=5)
    assert not future.done()
    detected.set_result(context)
    assert future.result(timeout=5)
    assert composed.trace[-1] == ("discover", context)


def test_shutdown_during_startup_never_launches_discovery(composed, monkeypatch):
    c = composed.core
    detected = Future()
    c.esp32._startup = detected
    monkeypatch.setattr(c.esp32, "startup_detect_and_maybe_update", lambda: detected)
    started = c.start()
    c.executor.submit(lambda: None).result(timeout=5)
    c.shutdown(lambda: None).result(timeout=5)
    assert started.result(timeout=5) is False
    assert not any(isinstance(x, tuple) and x[0] == "discover" for x in composed.trace)


def test_shutdown_aborts_pending_driver_check(composed, monkeypatch):
    c = composed.core
    pending = Future()
    checking = threading.Event()
    def check(**kw):
        checking.set()
        return pending
    monkeypatch.setattr(c.drivers, "startup_driver_check", check)
    started = c.start()
    assert checking.wait(timeout=5)
    c.shutdown(lambda: None).result(timeout=5)
    assert started.result(timeout=5) is False
    c.executor.shutdown(timeout=5)
    # A worker finishing after teardown must not submit startup work to a stopped executor.
    pending.set_result(models.ActionResult(True))
    assert not any(isinstance(x, tuple) and x[0] == "discover" for x in composed.trace)
