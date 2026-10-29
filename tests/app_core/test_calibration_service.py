"""Scripted raw input, deterministic executor clock, no hardware or real workers."""
from concurrent.futures import Future
from dataclasses import FrozenInstanceError
from types import SimpleNamespace as NS

import pytest

from app_core import events
from app_core.bus import EventBus
from app_core.services.calibration import CalibrationService, CALIBRATION_SESSION
from app_core.ui_port import ScriptedUiPort


class Timer:
    def __init__(self, when, fn, interval=None):
        self.when, self.fn, self.interval, self.cancelled = when, fn, interval, False
    def cancel(self): self.cancelled = True


class Executor:
    def __init__(self): self.now, self.timers = 0., []
    def is_core_thread(self): return True
    def submit(self, fn, *args, **kwargs):
        future = Future()
        try: future.set_result(fn(*args, **kwargs))
        except BaseException as exc: future.set_exception(exc)
        return future
    def call_later(self, seconds, fn, *args):
        timer = Timer(self.now + seconds, lambda: fn(*args)); self.timers.append(timer); return timer
    def call_every(self, seconds, fn):
        timer = Timer(self.now + seconds, fn, seconds); self.timers.append(timer); return timer
    def advance(self, seconds):
        target = self.now + seconds
        while True:
            timers = [t for t in self.timers if not t.cancelled and t.when <= target + 1e-9]
            if not timers: break
            timer = min(timers, key=lambda t: t.when)
            self.now = timer.when
            timer.fn()
            if timer.interval and not timer.cancelled: timer.when = self.now + timer.interval
            else: timer.cancelled = True
        self.now = target


class Controller:
    def __init__(self, side="pro", address="AA:BB:CC:DD:EE:FF"):
        self.side, self.device = side, NS(address=address)
        self.controller_info = NS(product_id=0x2073 if side == "gc" else 0x2069)
        self.last_input_data = NS(raw_left_stick=(2048, 2048), raw_right_stick=(2048, 2048), left_trigger_raw=36, right_trigger_raw=37)
        self.applied = 0
    def is_joycon_left(self): return self.side == "left"
    def is_joycon_right(self): return self.side == "right"
    def apply_in_app_joystick_calibration(self): self.applied += 1


@pytest.fixture
def rig(core_tmp_path):
    saves, refresh = [], []
    config = NS(joystick_calibration_data={"aa-bb-cc-dd-ee-ff": {"old": 1}},
        gc_trigger_calibration_data={}, config_file_path=str(core_tmp_path / "config.yaml"), save_config=lambda: saves.append(1))
    controller, executor, bus, ui = Controller(), Executor(), EventBus(), ScriptedUiPort()
    vc = NS(controllers=[controller], running=True)
    runtime = NS(current_controllers=[vc], config=config, is_quitting=False,
                 refresh_snapshots=lambda: refresh.append(1), app_profile_poll_suspended=False)
    def norm(key): return "".join(x for x in key.upper() if x in "0123456789ABCDEF")
    service = CalibrationService(bus, executor, ui, runtime, clock=lambda: executor.now,
        calibration_keys=lambda c: [c.device.address, norm(c.device.address)], normalize_key=norm,
        modules=lambda name: NS(NSO_GAMECUBE_CONTROLLER_PID=0x2073))
    progress, toasts = [], []
    bus.subscribe(CALIBRATION_SESSION, progress.append)
    bus.subscribe(events.TOAST, toasts.append)
    return NS(s=service, c=controller, config=config, vc=vc, runtime=runtime, bus=bus, ui=ui,
        ex=executor, token=(0, id(vc)), saves=saves, refresh=refresh,
        progress=progress, toasts=toasts)


def rotate(e):
    for i in range(211):
        value = (1000, 1100) if i % 2 else (3000, 3100)
        e.c.last_input_data.raw_left_stick = value
        e.c.last_input_data.raw_right_stick = value
        e.ex.advance(.05)


def test_joystick_rotation_release_alias_save_and_autoclose(rig):
    e = rig
    sid = e.s.start_joystick(e.token)
    assert e.s.start_joystick(e.token) == sid
    assert e.c.is_joystick_calibrating and e.c.back_button_calibration_active
    e.ex.advance(5.)
    assert e.progress[-1].sticks[0].rotate_elapsed == 0
    rotate(e)
    assert e.progress[-1].phase == "release" and len(e.progress[-1].sticks) == 2
    e.c.last_input_data.raw_left_stick = e.c.last_input_data.raw_right_stick = (2048, 2048)
    e.ex.advance(2.)
    assert not e.saves
    e.ex.advance(3.3)
    state = e.progress[-1]
    assert state.phase == "done" and state.completed
    assert len(e.saves) == 1 and e.c.applied == 1
    assert not e.c.is_joystick_calibrating and not e.c.back_button_calibration_active
    a = e.config.joystick_calibration_data[e.c.device.address]
    assert a["old"] == 1 and a["left"]["center"] == [2048, 2048]
    assert a["left"]["max"] == [952, 1052] and a["left"]["min"] == [1048, 948]
    assert e.config.joystick_calibration_data["AABBCCDDEEFF"] == a
    with pytest.raises(FrozenInstanceError): state.phase = "changed"
    e.ex.advance(3.)
    assert not e.progress[-1].active
    assert not e.s.cancel(sid).ok


def test_per_stick_movement_and_touch_reset(rig):
    e = rig
    e.s.start_joystick(e.token)
    for i in range(211):
        e.c.last_input_data.raw_left_stick = (2000 + i * 20, 2048)
        e.ex.advance(.05)
    state = e.progress[-1]
    assert state.phase == "rotate" and state.sticks[0].rotate_done and not state.sticks[1].rotate_done
    for i in range(211):
        e.c.last_input_data.raw_right_stick = (2000 + i * 20, 2048)
        e.ex.advance(.05)
    assert e.progress[-1].phase == "release"
    e.ex.advance(2.3)
    elapsed = e.progress[-1].sticks[0].release_elapsed
    assert elapsed > 0
    # Slow accumulated displacement trips the anchor touch threshold at 45.
    for i in range(1, 11):
        e.c.last_input_data.raw_left_stick = (6200 + 5 * i, 2048)
        e.ex.advance(.05)
    assert e.progress[-1].sticks[0].release_elapsed == pytest.approx(elapsed + .4)
    assert e.progress[-1].sticks[0].phase == "still"
    e.ex.advance(1.)
    assert not e.saves


@pytest.mark.parametrize("side,expected", [("left", ("left",)), ("right", ("right",)), ("pro", ("left", "right"))])
def test_source_selection_and_cancel(rig, side, expected):
    e = rig; e.c.side = side
    sid = e.s.start_joystick(e.token)
    assert tuple(s.side for s in e.progress[-1].sticks) == expected
    assert e.s.cancel(sid).ok
    e.ex.advance(20.)
    assert not e.saves and not e.c.is_joystick_calibrating
    assert e.toasts[-1].message == "Calibration cancelled."


def test_stale_waiting_disconnected_and_request_events(rig):
    e = rig
    assert e.s.start_joystick((0, 42)) is None
    e.c.last_input_data = None
    assert e.s.start_joystick(e.token) is None
    assert e.toasts[-1].message == "Waiting for joystick input data. Please try again in a moment."
    e.c.last_input_data = NS(raw_left_stick=(2048, 2048), raw_right_stick=(2048, 2048))
    e.bus.publish(events.CALIBRATION_REQUEST, events.CalibrationRequest(0))
    assert e.progress[-1].active
    e.bus.publish(events.CALIBRATION_REQUEST, events.CalibrationRequest(0, "cancel"))
    assert not e.progress[-1].active
    e.s.start_joystick(e.token)
    e.runtime.current_controllers = []
    e.ex.advance(.05)
    assert e.progress[-1].reason == "disconnected" and not e.c.is_joystick_calibrating
    assert not e.saves
    e.bus.publish(events.CALIBRATION_REQUEST, events.CalibrationRequest(4))


def test_no_sticks_notification_and_apply_failure_cleanup(rig):
    e = rig
    e.vc.controllers = []
    assert e.s.start_joystick(e.token) is None
    assert e.toasts[-1].message == "No joystick found for this player slot."
    e.vc.controllers = [e.c]
    def fail(): raise OSError("controller vanished")
    e.c.apply_in_app_joystick_calibration = fail
    e.s.start_joystick(e.token); rotate(e)
    e.c.last_input_data.raw_left_stick = e.c.last_input_data.raw_right_stick = (2048, 2048)
    e.ex.advance(5.5)
    assert e.progress[-1].completed and not e.c.is_joystick_calibrating and len(e.saves) == 1


def test_gc_five_steps_sampling_finish_only(rig):
    e = rig; e.c.controller_info.product_id = 0x2073
    sid = e.s.start_gc_trigger(e.token)
    assert e.s.start_gc_trigger() == sid
    assert e.progress[-1].trigger_values == (36, 37)
    for step, l, r in [(1, 180, 37), (2, 240, 37), (3, 36, 185), (4, 36, 245)]:
        assert e.s.next(sid).ok
        e.c.last_input_data.left_trigger_raw, e.c.last_input_data.right_trigger_raw = l, r
        e.ex.advance(.1)
        assert e.progress[-1].step == step and not e.saves
    assert e.progress[-1].next_label == "Finish"
    assert e.s.next(sid).ok
    assert e.config.gc_trigger_calibration_data[e.c.device.address] == [36, 180, 240, 37, 185, 245]
    assert len(e.saves) == 1
    assert not e.progress[-1].active and e.progress[-1].completed
    assert e.ui.calls[-1].args == ("Success", "GameCube Trigger Calibration saved successfully!")
    assert not e.s.next(sid).ok


def test_gc_not_found_cancel_and_disconnect(rig):
    e = rig
    assert e.s.start_gc_trigger() is None
    assert e.ui.calls[-1].args == ("Not Found", "No NSO GameCube Controller is currently connected.")
    e.c.controller_info.product_id = 0x2073
    sid = e.s.start_gc_trigger()
    e.s.next(sid); e.s.cancel(sid)
    assert not e.saves and not e.config.gc_trigger_calibration_data
    sid = e.s.start_gc_trigger()
    e.vc.controllers = []
    assert not e.s.next(sid).ok
    assert e.progress[-1].reason == "disconnected"


def test_close_clears_flags(rig):
    e = rig
    e.s.start_joystick(e.token)
    e.s.close()
    assert not e.c.is_joystick_calibrating and not e.progress[-1].active
    assert e.s.start_joystick(e.token) is None


def test_replaced_controller_and_vc_stopped_cancel(rig):
    e = rig
    e.s.start_joystick(e.token)
    e.vc.running = False
    e.ex.advance(.05)
    assert not e.c.is_joystick_calibrating and not e.saves
    e.vc.running = True
    e.s.start_joystick(e.token)
    e.vc.controllers = [Controller()]
    e.ex.advance(.05)
    assert e.progress[-1].reason == "disconnected" and not e.c.is_joystick_calibrating
