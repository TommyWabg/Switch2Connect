"""Player actions ported from PlayerInfoBlock; all entry points run on core."""
from .. import player_actions
import logging
from ..executor import on_core
from .. import events

logger = logging.getLogger(__name__)


class PlayerService:
    def __init__(self, runtime, *, calibration_tick=1.0):
        self.runtime = runtime
        self._executor = runtime._executor
        self.calibration_tick = calibration_tick
        self._calibrating = False

    def _resolve(self, slot_token):
        if self.runtime.is_quitting:
            return None
        if not isinstance(slot_token, tuple) or len(slot_token) != 2:
            return None
        index, identity = slot_token
        vcs = self.runtime.current_controllers
        if not isinstance(index, int) or index < 0 or index >= len(vcs):
            return None
        vc = vcs[index]
        if vc is None or id(vc) != identity or not vc.controllers:
            logger.info("Ignoring stale player-slot action: %r", slot_token)
            return None
        return vc

    @on_core
    def disconnect(self, slot_token):
        vc = self._resolve(slot_token)
        if vc is None:
            return False
        if not vc.loop or not vc.loop.is_running():
            return False
        future = player_actions.disconnect(vc, self.runtime.disconnect_spacing)
        def finished(source):
            try:
                source.result()
            except Exception:
                logger.exception("Player disconnect failed")
            finally:
                self._executor.submit(self.runtime.refresh_snapshots)
        if future is not None:
            future.add_done_callback(finished)
        return True

    @on_core
    def ping(self, slot_token):
        vc = self._resolve(slot_token)
        if vc is None:
            return False
        player_actions.ping(vc, lambda ms, callback: self._executor.call_later(ms / 1000., callback))
        return True
    @on_core
    def toggle_mag_calibration(self, slot_token, side=None):
        vc = self._resolve(slot_token)
        if vc is None:
            return False
        c = vc.controllers[0] if vc.is_single() else next((c for c in vc.controllers
            if (side in ("Left", "left") and c.is_joycon_left()) or
               (side in ("Right", "right") and c.is_joycon_right())), None)
        if c is None:
            return False
        player_actions.toggle_mag_calibration(c)
        self.runtime.refresh_snapshots()
        return True

    @on_core
    def split(self, slot_token):
        vc = self._resolve(slot_token)
        if vc is None:
            return False
        from discoverer import split_controller
        player_actions.split(vc, split_controller)
        self.runtime.refresh_snapshots()
        return True

    @on_core
    def merge_select(self, slot_token):
        vc = self._resolve(slot_token)
        if vc is None:
            return False
        from discoverer import merge_controllers
        pending = self.runtime.pending_merge_vc_index
        registry = self.runtime.controller_registry()
        if pending is not None and (pending < 0 or pending >= len(registry) or registry[pending] is None):
            self.runtime.pending_merge_vc_index = None
        self.runtime.pending_merge_vc_index = player_actions.merge_select(
            vc, self.runtime.pending_merge_vc_index, self.runtime.controller_registry(),
            merge_controllers, lambda controllers: None)
        self.runtime.update_controllers(list(self.runtime.controller_registry()))
        return True
    @on_core
    def set_hold_mode(self, slot_token, mode):
        vc = self._resolve(slot_token)
        if vc is None:
            return False
        player_actions.set_hold_mode(vc, self.runtime.config, mode, lambda: None)
        self.runtime.refresh_snapshots()
        return True
    @on_core
    def toggle_gyro_side(self, slot_token, side):
        vc = self._resolve(slot_token)
        if vc is None:
            return False
        player_actions.toggle_gyro_side(vc, self.runtime.config, side, self.runtime.refresh_snapshots)
        return True
    @on_core
    def start_gyro_calibration(self):
        if self._calibrating or self.runtime.is_quitting or not any(
                vc is not None and vc.controllers for vc in self.runtime.current_controllers):
            return False
        self._calibrating = True
        def render(message, phase):
            if phase == "done" or self.runtime.is_quitting:
                self._calibrating = False
            if not self.runtime.is_quitting:
                self.runtime.bus.publish(events.TASK_STATE,
                    events.TaskState("gyro_calibration", "Gyro Calibration", phase != "done", message))
                self.runtime.refresh_snapshots()
        def schedule(milliseconds, callback):
            self._executor.call_later(milliseconds / 1000, lambda: callback() if not self.runtime.is_quitting else setattr(self, "_calibrating", False))
        player_actions.gyro_countdown(lambda: self.runtime.current_controllers, schedule, render, 1000 * self.calibration_tick)
        return True

    async def _disconnect_merged_sequential(self, vc):
        await player_actions.disconnect_merged_sequential(vc, self.runtime.disconnect_spacing)
