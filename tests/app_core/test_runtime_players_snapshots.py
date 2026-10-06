"""Hardware-free runtime, player and snapshot checks, including timing and cross-thread handoffs."""
import asyncio
from concurrent.futures import Future
from dataclasses import FrozenInstanceError
import sys
import threading
import time
from types import SimpleNamespace as NS

import pytest

from app_core.bus import EventBus
from app_core.executor import CoreExecutor
from app_core.ui_port import NullUiPort
from app_core.runtime import AppRuntime
from app_core.services.players import PlayerService
from app_core.snapshots import battery_level, build_slots, wired_state
from app_core import events


class Controller:
    def __init__(self, side="pro2", voltage=None, record=None):
        self.side, self.record = side, record if record is not None else []
        self.device = NS(address=side)
        self.controller_info = NS(product_id={"pro": 0x2009, "pro2": 0x2069, "gamecube": 0x2073,
                                             "left": 0x2067, "right": 0x2066}[side])
        self.battery_voltage = voltage
        self.is_mag_calibrating = False
        self._interp_wake_event = NS(set=lambda: self.record.append("wake"))
        self.client = NS(is_connected=True)
    def is_joycon_left(self): return self.side == "left"
    def is_joycon_right(self): return self.side == "right"
    def is_joycon(self): return self.side in ("left", "right")
    def start_mag_calibration(self): self.is_mag_calibrating = True
    def stop_mag_calibration(self): self.is_mag_calibrating = False
    def _release_standard_mouse_buttons(self): self.record.append("release_mouse")
    async def disconnect(self): self.record.append("disconnect_" + self.side)
    async def set_vibration(self, *args, **kwargs):
        self.record.append((args[0].lf_amp, kwargs))


class VC:
    def __init__(self, *controllers, player=1, record=None):
        self.controllers = list(controllers)
        self.player_number, self.mode, self.hold_mode = player, "PS5", "Vertical"
        self.active_gyro_side = "Right"
        self.record = record if record is not None else []
        self.loop = None
        self.running = True
        self.vg_controller = None
        self._usbip_reconnect_lock = threading.Lock()
        self._usbip_reconnect_generation = 0
        self._usbip_reconnect_active = True
        self._disconnect_lock = asyncio.Lock()
        self.state_lock = threading.Lock()
        self.on_disconnected_callback = None
    def is_single(self): return len(self.controllers) == 1
    def is_single_joycon_left(self): return self.is_single() and self.controllers[0].is_joycon_left()
    def is_single_joycon_right(self): return self.is_single() and self.controllers[0].is_joycon_right()
    def reset_inputs(self): self.record.append("reset_inputs")
    def force_close(self, **kwargs): self.record.append(("force_close", kwargs))
    def start_calibration(self): self.record.append("start_calibration")


@pytest.fixture
def rig(monkeypatch):
    record = []
    config = NS(djg_enabled=False, djg_mode="Single Side Toggle", joycon_hold_mode={},
                merged_gyro_side={}, wired_auto_scan_enabled=True,
                save_config=lambda: record.append("save"))
    monkeypatch.setitem(sys.modules, "config", NS(CONFIG=config))
    discoverer = NS(VIRTUAL_CONTROLLERS=[], _IS_SUSPENDING=False)
    def suspend(value):
        discoverer._IS_SUSPENDING = value
        record.append(("suspend", value))
    discoverer.set_suspending = suspend
    discoverer.set_shutting_down = lambda value: record.append(("shutdown", value))
    discoverer.emergency_cleanup = lambda: record.append("emergency")
    discoverer.merge_controllers = lambda a, b: record.append(("merge", a, b))
    discoverer.split_controller = lambda a: record.append(("split", a))
    discovery_entered = threading.Event()
    def start_discoverer(cb, quit, ctx):
        record.append(("discovery", ctx))
        discovery_entered.set()
        quit.wait(2)
    discoverer.start_discoverer = start_discoverer
    monkeypatch.setitem(sys.modules, "app_core.helpers", NS(_set_current_thread_priority=lambda value: None))
    discoverer.request_wired_rescan = lambda reason, **kw: record.append(("rescan", reason, kw))
    discoverer.notify_bluetooth_radio_changed = lambda: record.append("radio")
    monkeypatch.setitem(sys.modules, "discoverer", discoverer)
    monkeypatch.setitem(sys.modules, "virtual_controller", NS(
        detach_all_usbip_devices=lambda **kw: record.append(("detach", kw)),
        reset_vigem_bus=lambda: record.append("reset_bus")))
    monkeypatch.setitem(sys.modules, "keyboard_output", NS(release_all=lambda: record.append("keyboard")))
    monkeypatch.setitem(sys.modules, "raw_input_mouse", NS(shutdown=lambda: record.append("mouse_shutdown")))
    monkeypatch.setitem(sys.modules, "usb_serial_bridge", NS(shutdown_all_bridges=lambda: record.append("bridges")))
    executor = CoreExecutor()
    runtime = AppRuntime(EventBus(), executor, NullUiPort(), suspend_settle=0.001,
                         resume_delay=0.04, disconnect_spacing=0.001,
                         shutdown_settle=0.001, wired_debounce=0.02)
    yield NS(runtime=runtime, executor=executor, record=record, discoverer=discoverer,
             config=config, discovery_entered=discovery_entered, players=PlayerService(runtime, calibration_tick=0.005))
    runtime.quit_event.set()
    if runtime.discoverer_thread:
        runtime.discoverer_thread.join(2)
    executor.shutdown(timeout=2)


def load(rig, vcs):
    rig.discoverer.VIRTUAL_CONTROLLERS = vcs
    rig.runtime.update_controllers(vcs).result(2)
    return [(i, id(vc)) for i, vc in enumerate(vcs)]


def barrier(rig): rig.executor.submit(lambda: None).result(2)


@pytest.mark.parametrize("voltage,level", [(None, None), (3.251, "high"), (3.25, "medium"),
                                           (3.126, "medium"), (3.125, "low"), (0, "low")])
def test_battery_boundaries(voltage, level): assert battery_level(voltage) == level


@pytest.mark.parametrize("side,kind", [("pro", "pro"), ("pro2", "pro2"), ("gamecube", "gamecube"),
                                      ("left", "joycon_left"), ("right", "joycon_right")])
def test_snapshot_kinds_and_orientation(side, kind):
    vc = VC(Controller(side, 3.3))
    vc.hold_mode = "Horizontal"
    slot = build_slots([vc], NS())[0]
    assert slot.kind == kind
    assert slot.controllers[0].kind == kind
    assert slot.controllers[0].mag_calibration_available == (side != "gamecube")
    assert slot.hold_mode_available == (side in ("left", "right"))
    assert slot.leds == (True, False, False, False)
    assert slot.slot_token == (0, id(vc))
    with pytest.raises(FrozenInstanceError): slot.controllers[0].address = "bad"
    vc.mode = "Switch1"
    assert not build_slots([vc], NS())[0].hold_mode_available
    vc.controllers.clear()
    assert slot.controllers[0].battery_level == "high"


def test_pair_and_merge_visibility():
    left, right = VC(Controller("left")), VC(Controller("right"), player=2)
    slots = build_slots([left, right], NS(), 0)
    assert slots[0].merge_available and slots[1].merge_available
    assert build_slots([left], NS(), 0)[0].merge_available
    assert not build_slots([left], NS())[0].merge_available
    pair = VC(Controller("right"), Controller("left"))
    pair.djg_left_active, pair.djg_right_active = False, True
    slot = build_slots([pair], NS(djg_enabled=True))[0]
    assert slot.kind == "joycon_pair" and slot.split_available and slot.gyro_side_available
    assert not slot.djg_left_active and slot.djg_right_active
    pair.mode = "Switch1"
    assert not build_slots([pair], NS())[0].gyro_side_available
    assert len(build_slots([None] * 5, NS())) == 5


def test_wired_derivation():
    usb = type("USBHidController", (Controller,), {})("pro")
    usb.usb_product_id = 0x2009
    hidden = Controller()
    hidden._hidhide_instance_id = "instance"
    state = wired_state([VC(usb, hidden), None])
    assert state.detected and state.product_ids == (0x2009,)
    del usb.usb_product_id
    assert wired_state([VC(usb)]).product_ids == (0x2069,)
    assert wired_state([VC(hidden)]).product_ids == ()


def test_discovery_coalesces_before_core_claims(rig):
    entered, release = threading.Event(), threading.Event()
    rig.executor.submit(lambda: (entered.set(), release.wait(2)))
    assert entered.wait(2)
    received = []
    rig.runtime.bus.subscribe(events.CONTROLLERS_CHANGED, received.append)
    a, b = VC(Controller("left")), VC(Controller("right"))
    rig.discoverer.VIRTUAL_CONTROLLERS = [b]
    rig.runtime.discoverer_callback([a])
    rig.runtime.discoverer_callback([b])
    release.set()
    barrier(rig)
    assert len(received) == 1 and received[0][0].kind == "joycon_right"
    assert rig.executor.submit(lambda: rig.runtime.current_controllers[0] is b).result(2)


def test_merge_state_machine_and_stale_token(rig):
    left, another, right = VC(Controller("left")), VC(Controller("left"), player=2), VC(Controller("right"), player=3)
    a, b, c = load(rig, [left, another, right])
    assert rig.players.merge_select(a).result(2)
    assert rig.runtime.pending_merge_vc_index == 0
    rig.players.merge_select(a).result(2)
    assert rig.runtime.pending_merge_vc_index is None
    rig.players.merge_select(a).result(2)
    rig.players.merge_select(b).result(2)
    assert rig.runtime.pending_merge_vc_index == 1
    rig.players.merge_select(c).result(2)
    assert ("merge", 1, 2) in rig.record and rig.runtime.pending_merge_vc_index is None
    load(rig, [VC(Controller()), another, right])
    assert rig.players.disconnect(a).result(2) is False
    assert not left.record
    assert rig.players.ping((-1, id(left))).result(2) is False
    # Lost selected registry entry: move selection safely rather than dereference None.
    rig.runtime.pending_merge_vc_index = 0
    rig.discoverer.VIRTUAL_CONTROLLERS[0] = None
    rig.players.merge_select(c).result(2)
    assert rig.runtime.pending_merge_vc_index == 2


def test_hold_mag_split_and_gyro_save_semantics(rig):
    left = VC(Controller("left"))
    token = load(rig, [left])[0]
    rig.players.set_hold_mode(token, "Horizontal").result(2)
    assert rig.config.joycon_hold_mode == {"left": "Horizontal"}
    rig.players.toggle_mag_calibration(token).result(2)
    assert left.controllers[0].is_mag_calibrating
    rig.players.toggle_mag_calibration(token).result(2)
    assert not left.controllers[0].is_mag_calibrating
    assert rig.players.disconnect(token).result(2) is False  # No running VC loop.
    pair = VC(Controller("left"), Controller("right"))
    token = load(rig, [pair])[0]
    rig.players.split(token).result(2)
    assert ("split", 0) in rig.record
    rig.config.djg_enabled = True
    rig.players.toggle_gyro_side(token, "Left").result(2)
    assert pair.djg_left_active is False
    assert not rig.config.merged_gyro_side
    rig.config.djg_mode = "Switch Gyro Side"
    saves = rig.record.count("save")
    rig.players.toggle_gyro_side(token, "Left").result(2)
    assert rig.config.merged_gyro_side == {"left+right": "Left"}
    assert rig.config.djg_dominant_side == "Left"
    assert rig.record.count("save") == saves + 2


def test_single_disconnect_publishes_empty_slot_after_completion(rig, vc_loop):
    vc = VC(Controller())
    vc.loop = vc_loop
    entered, release, refreshed = threading.Event(), threading.Event(), threading.Event()
    threads = []

    async def disconnect():
        entered.set()
        await asyncio.to_thread(release.wait, 2)
        # The discoverer callback can fail without updating the registry.
        try:
            raise RuntimeError("discoverer callback failed")
        except Exception:
            pass
        vc.controllers.clear()

    vc.trigger_disconnect = lambda: asyncio.run_coroutine_threadsafe(disconnect(), vc.loop)
    token = load(rig, [vc])[0]

    def observe(slots):
        if not slots[0].controllers:
            threads.append(rig.executor.is_core_thread())
            refreshed.set()

    rig.runtime.bus.subscribe(events.CONTROLLERS_CHANGED, observe)
    try:
        assert rig.players.disconnect(token).result(2)
        assert entered.wait(2)
        assert rig.runtime.slots[0].controllers
        assert not refreshed.is_set()
        release.set()
        assert refreshed.wait(2)
        assert not rig.runtime.slots[0].controllers
        assert threads == [True]
        assert rig.players.disconnect(token).result(2) is False
    finally:
        release.set()


def test_suspend_resume_order_and_resuspend_guard(rig):
    vc = VC(Controller(record=rig.record), record=rig.record)
    load(rig, [vc])
    rig.runtime.handle_power_event(4)
    assert rig.record == [("suspend", True), "keyboard", "reset_inputs", "wake", "release_mouse",
                          ("force_close", {}), "mouse_shutdown", "reset_bus"]
    assert rig.runtime.quit_event.is_set()
    rig.runtime.handle_power_event(7)
    barrier(rig)
    assert rig.record[-2:] == [("suspend", False), "emergency"]
    assert not rig.runtime.quit_event.is_set() and rig.runtime.current_controllers == []
    rig.runtime.handle_power_event(4)
    time.sleep(0.08)
    barrier(rig)
    assert not any(isinstance(item, tuple) and item[0] == "discovery" for item in rig.record)


def test_resume_duplicate_cleanup_but_one_restart(rig):
    rig.runtime.handle_power_event(7)
    rig.runtime.handle_power_event(0x12)
    time.sleep(0.08)
    barrier(rig)
    assert rig.record.count("emergency") == 2
    assert rig.discovery_entered.wait(2)
    assert rig.record.count(("discovery", None)) == 1


def test_wired_drain_keeps_each_kind_and_debounces(rig):
    q = rig.runtime.wired_device_event_queue
    q.put({"reason": "old", "path": "old"})
    q.put({"kind": "bluetooth_radio"})
    q.put({"reason": "new", "path": "new"})
    rig.runtime.poll_wired_device_events().result(2)
    q.put({"reason": "latest", "path": "latest"})
    rig.runtime.poll_wired_device_events().result(2)
    time.sleep(0.05)
    barrier(rig)
    assert rig.record == ["radio", ("rescan", "latest", {"candidate_path": "latest"})]
    rig.config.wired_auto_scan_enabled = False
    q.put({"reason": "disabled"})
    rig.runtime.poll_wired_device_events().result(2)
    time.sleep(0.03)
    assert len(rig.record) == 2


def test_startup_future_context_and_abort(rig):
    gate = Future()
    called = []
    completion = rig.runtime.run_startup_sequence([
        lambda: called.append(rig.executor.is_core_thread()) or gate,
        lambda: called.append(rig.executor.is_core_thread()) or True]).result(2)
    assert not completion.done()
    gate.set_result({"status": "ready", "observed_mono": 123})
    assert completion.result(2) is True
    assert called == [True, True]
    assert rig.discovery_entered.wait(2)
    assert rig.record[0] == ("discovery", {"status": "ready", "observed_mono": 123})
    rig.runtime.stop_discoverer().result(2)
    rig.runtime._startup_detection_done = False
    assert rig.runtime.run_startup_sequence([lambda: False]).result(2).result(2) is False


def test_gyro_countdown_and_guard(rig):
    vc = VC(Controller())
    load(rig, [vc])
    states, finished = [], threading.Event()
    def receive(state):
        states.append((state.message, state.active))
        if not state.active: finished.set()
    rig.runtime.bus.subscribe(events.TASK_STATE, receive)
    assert rig.players.start_gyro_calibration().result(2)
    assert not rig.players.start_gyro_calibration().result(2)
    assert finished.wait(2)
    assert states == [("Starting in 3..", True), ("Starting in 2..", True), ("Starting in 1..", True),
                      ("Calibrating 5..", True), ("Calibrating 4..", True), ("Calibrating 3..", True),
                      ("Calibrating 2..", True), ("Calibrating 1..", True), ("Calibration Done", False)]
    assert vc.record == ["start_calibration"]


def test_notification_semantics_and_callbacks(rig, monkeypatch):
    utils = NS()
    monkeypatch.setitem(sys.modules, "utils", utils)
    rig.executor.submit(rig.runtime._register_callbacks).result(2)
    received, requests = [], []
    rig.runtime.bus.subscribe(events.TOAST, received.append)
    rig.runtime.bus.subscribe(events.CALIBRATION_REQUEST, requests.append)
    utils.show_notification_callback("Gyro", "Calibration complete.").result(2)
    utils.show_notification_callback("Mag", "Magnetometer calibration complete.").result(2)
    utils.show_notification_callback("Profile", "Changed").result(2)
    utils.show_notification_callback("Calibration", "Calibration cancelled.").result(2)
    assert [(r.kind, r.auto_close_seconds) for r in received] == [
        ("success", None), ("success", 3), ("info", 3), ("error", 3)]
    utils.joystick_calibration_callback(VC(Controller(), player=3)).result(2)
    assert requests == [events.CalibrationRequest(2)]
    assert utils.change_profile_callback().result(2) is None
    calls = []
    rig.runtime.set_profile_handlers(nav=lambda direction: calls.append(direction)).result(2)
    utils.profile_nav_callback(-1).result(2)
    assert calls == [-1]


@pytest.fixture
def vc_loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(2)
    loop.close()


@pytest.mark.parametrize("has_vcs", [False, True])
def test_shutdown_order_even_no_vcs(rig, vc_loop, has_vcs):
    if has_vcs:
        a, b = VC(Controller("left", record=rig.record), record=rig.record), VC(Controller("right", record=rig.record), player=2, record=rig.record)
        a.loop = b.loop = vc_loop
        load(rig, [a, b])
        # Registry-only VC must also be marked and closed before disconnect.
        rig.discoverer.VIRTUAL_CONTROLLERS.append(VC(Controller(), record=rig.record))
    rig.runtime.wired_device_listener = NS(stop=lambda: rig.record.append("listener_stop"))
    done = threading.Event()
    rig.runtime.shutdown(lambda: (rig.record.append("done"), done.set())).result(2)
    rig.runtime.shutdown(lambda: rig.record.append("duplicate")).result(2)
    assert done.wait(2)
    expected = ["listener_stop", ("shutdown", True), ("detach", {"timeout": 1.5})]
    if has_vcs:
        expected += [("force_close", {"usbip_already_detached": True})] * 3
        expected += ["disconnect_left", "disconnect_right"]
        assert not a.running and a._suppress_usbip_reconnect and a._usbip_reconnect_generation == 1
        assert a.controllers[0].suspended
    assert rig.record == expected + ["bridges", "done"]


def test_merged_disconnect_callback_before_client_clear(rig, monkeypatch, vc_loop):
    monkeypatch.setitem(sys.modules, "controller", NS(INPUT_REPORT_UUID="input", COMMAND_RESPONSE_UUID="command"))
    controllers = [Controller("left", record=rig.record), Controller("right", record=rig.record)]
    for c in controllers:
        async def stop(uuid, c=c): rig.record.append((c.side, "stop", uuid))
        async def disconnect(c=c): rig.record.append((c.side, "physical"))
        c.client = NS(is_connected=True, stop_notify=stop, disconnect=disconnect)
    vc = VC(*controllers, record=rig.record)
    vc.loop = vc_loop
    async def callback(c):
        assert c.client is not None
        rig.record.append((c.side, "callback"))
    vc.on_disconnected_callback = callback
    token = load(rig, [vc])[0]
    asyncio.run_coroutine_threadsafe(rig.players._disconnect_merged_sequential(vc), vc_loop).result(2)
    assert rig.record == [(side, action, uuid) if action == "stop" else (side, action)
        for side in ("left", "right") for action, uuid in (("stop", "input"), ("stop", "command"), ("physical", None), ("callback", None))]
    assert vc.controllers == [] and all(c.client is None for c in controllers)
    assert rig.players.disconnect(token).result(2) is False

def test_ping_exact_schedule_and_highlight(rig, monkeypatch, vc_loop):
    monkeypatch.setitem(sys.modules, "controller", NS(VibrationData=lambda **kw: NS(**kw)))
    scheduled = []
    monkeypatch.setattr(rig.executor, "call_later", lambda delay, fn, *args: scheduled.append((delay, fn, args)))
    c = Controller(record=rig.record)
    vc = VC(c)
    vc.loop = vc_loop
    token = load(rig, [vc])[0]
    assert rig.players.ping(token).result(2)
    assert [delay for delay, _, _ in scheduled] == [0.1, 0.2, 0.3]
    for _, fn, args in scheduled:
        rig.executor.submit(fn, *args).result(2)
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), vc_loop).result(2)
    assert [amplitude for amplitude, _ in rig.record] == [800, 0, 800, 0]
    assert all(kwargs == {"ignore_freq_scaling": True, "pair_sustain": False} for _, kwargs in rig.record)


def test_start_listener_timer_registration_and_battery_refresh(rig, monkeypatch):
    registration, periodic = [], []
    class Power:
        def __init__(self, callback): self.callback = callback
        def start(self): registration.append("power")
    class Wired:
        def __init__(self, queue): self.queue = queue
        def start(self): registration.append("wired")
    monkeypatch.setitem(sys.modules, "app_core.win_listeners", NS(PowerListener=Power, WiredDeviceChangeListener=Wired))
    monkeypatch.setitem(sys.modules, "utils", NS())
    def every(seconds, fn):
        periodic.append((seconds, fn))
        return NS(cancel=lambda: None)
    monkeypatch.setattr(rig.executor, "call_every", every)
    rig.runtime.start().result(2)
    rig.runtime.start().result(2)
    assert registration == ["power", "wired"]
    assert [seconds for seconds, _ in periodic] == [0.25, 300]
    received, wired, drivers = [], [], []
    rig.runtime.bus.subscribe(events.CONTROLLERS_CHANGED, received.append)
    rig.runtime.bus.subscribe(events.WIRED_STATE_CHANGED, wired.append)
    rig.runtime.bus.subscribe(events.DRIVER_STATE_CHANGED, drivers.append)
    c = Controller(voltage=None)
    load(rig, [VC(c)])
    c.battery_voltage = 3.3
    rig.executor.submit(periodic[1][1]).result(2)
    assert received[0][0].controllers[0].battery_level is None
    assert received[1][0].controllers[0].battery_level == "high"
    assert len(wired) == 2 and not drivers
    rig.runtime.is_quitting = True
    rig.executor.submit(periodic[1][1]).result(2)
    assert len(received) == 2


def test_production_delays_and_dataclass_positional_compatibility(rig):
    runtime = AppRuntime(rig.runtime.bus, rig.executor, rig.runtime.ui_port)
    assert (runtime.suspend_settle, runtime.resume_delay, runtime.disconnect_spacing,
            runtime.shutdown_settle, runtime.shutdown_timeout, runtime.discoverer_join_timeout,
            runtime.wired_debounce) == (1, 7, 0.3, 3.5, 20, 5, 0.5)
    from app_core.models import ControllerInfo, SlotInfo
    c = ControllerInfo("pro", "Pro", "MAC", "ble", None, None, True)
    assert c.is_mag_calibrating
    slot = SlotInfo(0, 1, (), (), False, True, None, None, True, True)
    assert slot.djg_left_active and slot.djg_right_active


def test_startup_future_can_require_core_work(rig):
    def step():
        return rig.executor.submit(lambda: {"status": "core-produced"})
    completion = rig.runtime.run_startup_sequence([step]).result(2)
    assert completion.result(2)
    assert rig.discovery_entered.wait(2)
    assert rig.record == [("discovery", {"status": "core-produced"})]


def test_shutdown_during_resume_wait_cancels_restart(rig):
    rig.runtime.handle_power_event(7)
    done = threading.Event()
    rig.runtime.shutdown(done.set).result(2)
    assert done.wait(2)
    time.sleep(0.08)
    barrier(rig)
    assert not rig.discovery_entered.is_set()


def test_suspend_runs_synchronously_on_listener_thread(rig):
    # Windows waits for WM_POWERBROADCAST to return before sleeping, so the
    # suspend cleanup must complete on the caller's thread, not be deferred.
    import threading
    seen = []
    original = rig.runtime.quit_event.set
    rig.runtime.quit_event.set = lambda: (seen.append(threading.current_thread()), original())[1]
    assert rig.runtime.handle_power_event(4) is None
    assert seen == [threading.current_thread()]
