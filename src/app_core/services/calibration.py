"""Joystick and GameCube trigger calibration sessions for the modern UI.

CALIBRATION_SESSION publishes each CalibrationSession; state() caches CalibrationState.
Wizard rendering lives in the UI. Call close() before runtime/executor shutdown.
"""
from .. import calibration_data
import importlib
import itertools
import logging
import time

from .. import events
from ..models import StickProgress, CalibrationSession, CalibrationState
from ..executor import on_core
from ..models import SettingResult

logger = logging.getLogger(__name__)
CALIBRATION_SESSION = events.CALIBRATION_SESSION
GC_STEPS = calibration_data.GC_STEPS


class CalibrationService:
    ROTATE_SECONDS = 10.
    RELEASE_SECONDS = 3.
    STILL_BEFORE_COUNTDOWN = 2.
    MOVE_THRESHOLD = 10
    TOUCH_THRESHOLD = 45

    def __init__(self, bus, executor, ui_port, runtime, *, config=None,
                 clock=time.perf_counter, calibration_keys=None, normalize_key=None,
                 modules=importlib.import_module):
        self._bus, self._executor, self._ui_port, self._runtime = bus, executor, ui_port, runtime
        self._config = config if config is not None else runtime.config
        self._clock, self._keys, self._normalize = clock, calibration_keys, normalize_key
        self._modules = modules
        self._sessions = {}
        self._ids = itertools.count(1)
        self._state = CalibrationState()
        self._closed = False
        self._unsubscribe = bus.subscribe(events.CALIBRATION_REQUEST, self._request)

    def state(self):
        return self._state

    def _resolve(self, token, *, allow_empty=False):
        if self._closed or self._runtime.is_quitting or not isinstance(token, tuple) or len(token) != 2:
            return None
        index, identity = token
        vcs = self._runtime.current_controllers
        if not isinstance(index, int) or index < 0 or index >= len(vcs):
            return None
        vc = vcs[index]
        return vc if vc is not None and id(vc) == identity and (vc.controllers or allow_empty) and getattr(vc, "running", True) else None

    def _request(self, request):
        self._executor.submit(self._handle_request, request)

    def _handle_request(self, request):
        index = request.slot_index
        vcs = self._runtime.current_controllers
        if not isinstance(index, int) or index < 0 or index >= len(vcs) or vcs[index] is None:
            return
        token = (index, id(vcs[index]))
        if request.action == "start":
            self.start_joystick(token)
        elif request.action == "cancel":
            for sid, session in tuple(self._sessions.items()):
                if session["token"] == token and session["kind"] == "joystick":
                    self.cancel(sid)

    def _notify(self, title, message):
        self._bus.publish(events.TOAST, events.Toast(message, title=title, auto_close_seconds=3.))

    @on_core
    def start_joystick(self, slot_token):
        vc = self._resolve(slot_token, allow_empty=True)
        if vc is None:
            return None
        for sid, item in self._sessions.items():
            if item["token"] == slot_token and item["kind"] == "joystick" and item["active"]:
                return sid
        sources = calibration_data.validate_sources(vc, self._notify)
        if sources is None:
            return None
        sid = f"joystick-{next(self._ids)}"
        data = list(calibration_data.build_session_data(sources).values())
        session = dict(kind="joystick", token=slot_token, vc=vc, controllers=tuple(vc.controllers),
                       stage="rotate", last_tick=self._clock(), data=data, active=True,
                       completed=False, reason=None, timer=None)
        self._sessions[sid] = session
        calibration_data.set_flags(session["controllers"], True)
        session["timer"] = self._executor.call_every(.05, lambda: self._tick(sid))
        self._publish(sid)
        self._runtime.refresh_snapshots()
        return sid


    def _tick(self, sid):
        session = self._sessions.get(sid)
        if not session or not session["active"]:
            return
        vc = self._resolve(session["token"])
        if vc is None or any(not any(c is current for current in vc.controllers) for c in session["controllers"]):
            self.cancel(sid, reason="disconnected")
            return
        if session["kind"] == "gc_trigger":
            self._sample_gc(session)
            self._publish(sid)
            return
        if session["completed"]:
            return
        from ..lifecycle import BackendContext
        context = BackendContext(session, getters={
            name: lambda name=name: getattr(self, name) for name in (
                "MOVE_THRESHOLD", "TOUCH_THRESHOLD", "ROTATE_SECONDS", "RELEASE_SECONDS", "STILL_BEFORE_COUNTDOWN")})
        calibration_data.tick_session(context, self._clock, session["data"], lambda: True,
            lambda: None, lambda: self._finish_joystick(sid, session))
        self._publish(sid)

    def _finish_joystick(self, sid, session):
        if self._keys is None or self._normalize is None:
            controller_module = self._modules("controller")
            keys_fn = self._keys or controller_module.controller_calibration_keys
            normalize = self._normalize or controller_module.normalize_calibration_key
        else:
            keys_fn, normalize = self._keys, self._normalize
        from ..lifecycle import BackendContext
        def render():
            session["stage"] = "done"
            session["timer"].cancel()
        def schedule_close(milliseconds):
            session["timer"] = self._executor.call_later(milliseconds / 1000, self._close_session, sid)
        calibration_data.finish_joystick_session(BackendContext(session), self._config,
            session["data"], session["controllers"], keys_fn, normalize, render, schedule_close)
        self._bus.publish(events.SETTINGS_CHANGED, events.SettingsChanged(frozenset({"joystick_calibration_data"})))
        self._runtime.refresh_snapshots()

    @on_core
    def start_gc_trigger(self, slot_token=None):
        vc = self._resolve(slot_token) if slot_token is not None else None
        from_module = self._modules("controller")
        pid = from_module.NSO_GAMECUBE_CONTROLLER_PID
        candidates = ((slot_token, vc),) if slot_token is not None else tuple(
            ((i, id(v)), v) for i, v in enumerate(self._runtime.current_controllers) if v is not None)
        controller = None
        for token, candidate in candidates:
            if self._resolve(token) is None:
                continue
            controller = next((c for c in candidate.controllers if getattr(c.controller_info, "product_id", 0) == pid), None)
            if controller is not None:
                vc, slot_token = candidate, token
                break
        if controller is None:
            self._ui_port.show_message("Not Found", "No NSO GameCube Controller is currently connected.")
            return None
        for sid, session in self._sessions.items():
            if session["kind"] == "gc_trigger" and session["controller"] is controller and session["active"]:
                return sid
        sid = f"gc-trigger-{next(self._ids)}"
        session = dict(kind="gc_trigger", token=slot_token, vc=vc, controller=controller,
            controllers=(controller,), stage="sampling", step=0,
            values=[36, 190, 240, 36, 190, 240], raw=(0, 0), active=True,
            completed=False, reason=None, timer=None)
        self._sessions[sid] = session
        self._sample_gc(session)
        session["timer"] = self._executor.call_every(.05, lambda: self._tick(sid))
        self._publish(sid)
        return sid

    @staticmethod
    def _sample_gc(session):
        from ..lifecycle import BackendContext
        calibration_data.sample_gc(BackendContext(session), session["controller"],
            lambda l, r: session.update(raw=(l, r)))

    @on_core
    def next(self, session_id):
        session = self._sessions.get(session_id)
        if not session or session["kind"] != "gc_trigger" or not session["active"]:
            return SettingResult(False, "invalid_session")
        if self._resolve(session["token"]) is None or not any(c is session["controller"] for c in session["vc"].controllers):
            self.cancel(session_id, reason="disconnected")
            return SettingResult(False, "disconnected")
        from ..lifecycle import BackendContext
        def close():
            session["completed"] = True
            self._bus.publish(events.SETTINGS_CHANGED, events.SettingsChanged(frozenset({"gc_trigger_calibration_data"})))
            self._close_session(session_id)
        calibration_data.next_gc(BackendContext(session), self._config, session["controller"],
            self._ui_port, lambda step, phase: None, close)
        if session["active"]:
            self._publish(session_id)
        return SettingResult(True)

    @on_core
    def cancel(self, session_id, *, reason="cancelled"):
        session = self._sessions.get(session_id)
        if not session or not session["active"]:
            return SettingResult(False, "invalid_session")
        session["reason"] = reason
        if session["kind"] == "joystick":
            calibration_data.set_flags(session["controllers"], False)
            self._notify("Switch 2 Connect", "Calibration cancelled.")
        self._close_session(session_id)
        self._runtime.refresh_snapshots()
        return SettingResult(True)

    def _close_session(self, sid):
        session = self._sessions.get(sid)
        if session is None or not session["active"]:
            return
        if session["timer"]:
            session["timer"].cancel()
        session["active"] = False
        session["stage"] = "closed"
        if session["kind"] == "joystick":
            calibration_data.set_flags(session["controllers"], False)
        self._publish(sid)

    def _session_view(self, sid, session):
        if session["kind"] == "gc_trigger":
            title, text = GC_STEPS[session["step"]]
            return CalibrationSession(sid, "gc_trigger", session["token"], session["stage"], title, text,
                session["active"], session["completed"], step=session["step"],
                trigger_values=session["raw"], next_label="Finish" if session["step"] == 4 else "Next", reason=session["reason"])
        phase = session["stage"]
        sticks = []
        for item in session["data"]:
            rotate = phase == "rotate"
            stick_phase = "rotate" if rotate else "release" if item["still_since"] is None else (
                "idle_sampling" if self._clock() - item["still_since"] >= self.STILL_BEFORE_COUNTDOWN else "still")
            source = item["source"]
            sticks.append(StickProgress(source["key"], source["side"], source["label"],
                item["rotate_elapsed"], item["release_elapsed"], item["rotate_done"], item["release_done"],
                stick_phase, item["prev"]))
        text = "Please fully rotate both joysticks for 10 seconds each." if phase == "rotate" else (
            "Joysticks calibration done." if session["completed"] else "Please release and don't touch the joysticks for 3 seconds.")
        return CalibrationSession(sid, "joystick", session["token"], phase, "Joysticks Calibration", text,
            session["active"], session["completed"], tuple(sticks), reason=session["reason"])

    def _publish(self, sid):
        self._state = CalibrationState(tuple(self._session_view(k, s) for k, s in self._sessions.items()))
        self._bus.publish(CALIBRATION_SESSION, next(v for v in self._state.sessions if v.session_id == sid))

    @on_core
    def close(self):
        if self._closed:
            return
        self._closed = True
        self._unsubscribe()
        for sid, session in tuple(self._sessions.items()):
            if session["active"]:
                self.cancel(sid)
