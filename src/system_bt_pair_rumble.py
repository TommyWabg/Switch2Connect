"""Pair-scoped rumble cadence for merged Joy-Con on Windows System Bluetooth."""

from __future__ import annotations

import asyncio
import ctypes
import logging
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional


logger = logging.getLogger(__name__)

# Alternate L/R slots. Each side therefore receives a 15 ms sustain opportunity,
# staying below the measured 16.6 ms discontinuity threshold.
SYSTEM_BT_PAIR_SLOT_INTERVAL = 0.0070
SYSTEM_BT_PAIR_DIAG_INTERVAL = 1.0
SYSTEM_BT_STOP_SUCCESS_COUNT = 3
SYSTEM_BT_STOP_RETRY_WINDOW = 0.250
SYSTEM_BT_EDGE_LIFETIME = 0.100
SYSTEM_BT_SIDE_MIN_DISPATCH_INTERVAL = 0.012
# Keep a small tolerance below the 7 ms timer grid. Windows may wake a nominal
# slot a few hundred microseconds early; using the exact same value here silently
# rejected that slot and halved each side to ~48 writes/s.
SYSTEM_BT_PAIR_MIN_DISPATCH_INTERVAL = 0.0065
SYSTEM_BT_DEGRADED_SIDE_INTERVAL = 0.024
SYSTEM_BT_SLOW_WRITE_THRESHOLD = 0.012
SYSTEM_BT_CRITICAL_WRITE_THRESHOLD = 0.050
SYSTEM_BT_SLOW_WRITE_WINDOW = 0.250
SYSTEM_BT_SLOW_WRITE_COUNT = 3
SYSTEM_BT_HEALTHY_WRITE_THRESHOLD = 0.008
SYSTEM_BT_DEGRADED_HOLD = 0.500
SYSTEM_BT_TIMEOUT_COOLDOWN = 0.100
SYSTEM_BT_RECOVERY_SUCCESS_COUNT = 12


def _percentile(values, percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1,
                       int(round((len(ordered) - 1) * percentile))))
    return float(ordered[index])


class _WindowsHighResolutionTimer:
    """Thread-owned high-resolution waitable timer with a safe fallback."""

    CREATE_WAITABLE_TIMER_HIGH_RESOLUTION = 0x00000002
    TIMER_ALL_ACCESS = 0x001F0003
    WAIT_OBJECT_0 = 0x00000000
    INFINITE = 0xFFFFFFFF

    def __init__(self):
        self.handle = None
        if sys.platform != "win32":
            return
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create = kernel32.CreateWaitableTimerExW
            create.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                               ctypes.c_ulong, ctypes.c_ulong]
            create.restype = ctypes.c_void_p
            handle = create(
                None, None, self.CREATE_WAITABLE_TIMER_HIGH_RESOLUTION,
                self.TIMER_ALL_ACCESS)
            if not handle:
                return
            self._kernel32 = kernel32
            self.handle = handle
        except Exception:
            self.handle = None

    @property
    def available(self) -> bool:
        return bool(self.handle)

    def wait(self, seconds: float) -> bool:
        if not self.handle:
            return False
        due_time = ctypes.c_longlong(-max(1, int(seconds * 10_000_000)))
        try:
            set_timer = self._kernel32.SetWaitableTimerEx
            set_timer.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long,
                                  ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_void_p, ctypes.c_ulong]
            set_timer.restype = ctypes.c_int
            if not set_timer(self.handle, ctypes.byref(due_time), 0,
                             None, None, None, 0):
                return False
            wait_one = self._kernel32.WaitForSingleObject
            wait_one.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            wait_one.restype = ctypes.c_ulong
            wait_result = wait_one(self.handle, self.INFINITE)
            return wait_result == self.WAIT_OBJECT_0
        except Exception:
            return False

    def close(self) -> None:
        handle = self.handle
        self.handle = None
        if handle:
            try:
                cancel = self._kernel32.CancelWaitableTimer
                cancel.argtypes = [ctypes.c_void_p]
                cancel(handle)
                close = self._kernel32.CloseHandle
                close.argtypes = [ctypes.c_void_p]
                close(handle)
            except Exception:
                pass


@dataclass
class _SideState:
    controller: object
    payload: Optional[bytes] = None
    uuid: Optional[str] = None
    active: bool = False
    sustain: bool = True
    active_remaining: int = 0
    active_deadline: float = 0.0
    zero_remaining: int = 0
    zero_deadline: float = 0.0
    in_flight: bool = False
    edge_payload: Optional[bytes] = None
    edge_uuid: Optional[str] = None
    edge_remaining: int = 0
    edge_deadline: float = 0.0
    generation: int = 0
    edge_generation: int = 0
    active_ever_dispatched: bool = False
    edge_sent: int = 0
    edge_expired: int = 0
    tx_id: int = 0
    submitted: int = 0
    dispatched: int = 0
    skipped_busy: int = 0
    successful: int = 0
    last_dispatch_time: float = 0.0
    last_success_time: float = 0.0
    dispatch_intervals_ms: Optional[list] = None
    success_intervals_ms: Optional[list] = None
    write_latencies_ms: Optional[list] = None

    def __post_init__(self):
        self.dispatch_intervals_ms = []
        self.success_intervals_ms = []
        self.write_latencies_ms = []


class SystemBluetoothPairRumbleCoordinator:
    """Fixed-cadence hybrid scheduler for one merged System-BT Joy-Con pair.

    Non-zero edges are delivered once, stops have a bounded success budget, and
    repeated sustain frames resend the latest payload independent of user input.
    One pair-wide in-flight slot prevents the Windows BLE write queue from growing.
    """

    def __init__(self, virtual_controller, left, right, session_id: str):
        self._vc = virtual_controller
        self.session_id = str(session_id)
        self._slot_interval = SYSTEM_BT_PAIR_SLOT_INTERVAL
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._running = False
        self._thread = None
        self._side_order = ("Left", "Right")
        self._next_side_index = 0
        self._sides = {
            "Left": _SideState(left, tx_id=int(getattr(left, "vibration_packet_id", 0)) & 0x0F),
            "Right": _SideState(right, tx_id=int(getattr(right, "vibration_packet_id", 0)) & 0x0F),
        }
        self._diag_started = time.perf_counter()
        self._slot_count = 0
        self._slot_interval_sum_ms = 0.0
        self._slot_intervals_ms = []
        self._deadline_lateness_ms = []
        self._missed_deadlines = 0
        self._last_slot_time = 0.0
        self._timer_mode = "event"
        self._idle = True
        self._global_in_flight = 0
        self._global_busy_skips = 0
        self._global_interval_skips = 0
        self._last_global_dispatch = 0.0
        self._write_futures = set()
        self._transport_cooldown_until = 0.0
        self._degraded_until = 0.0
        self._degraded = False
        self._healthy_write_streak = 0
        self._timeout_cooldowns = 0
        self._degraded_entries = 0
        self._stale_completions = 0
        self._slow_write_times = []
        self._slow_write_outliers = 0

    def owns(self, controller) -> bool:
        return any(state.controller is controller for state in self._sides.values())

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"SystemBTPairRumble-{self.session_id}",
        )
        self._thread.start()
        logger.info(
            "System-BT merged rumble coordinator started session=%s slot=%.1fms",
            self.session_id,
            self._slot_interval * 1000.0,
            extra={"system_bt_merged": True},
        )

    def stop(self) -> None:
        self._running = False
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=0.25)
        self._thread = None
        with self._lock:
            futures = tuple(self._write_futures)
            for state in self._sides.values():
                state.payload = None
                state.active = False
                state.zero_remaining = 0
                state.edge_remaining = 0
        for future in futures:
            future.cancel()
        logger.info(
            "System-BT merged rumble coordinator stopped session=%s",
            self.session_id,
            extra={"system_bt_merged": True},
        )

    def submit(self, controller, uuid: str, payload: bytes, active: bool,
               sustain: bool = True) -> bool:
        side_name = "Left" if controller is self._sides["Left"].controller else (
            "Right" if controller is self._sides["Right"].controller else None
        )
        if side_name is None or not self._running:
            return False
        with self._lock:
            state = self._sides[side_name]
            was_active = state.active
            now = time.perf_counter()
            next_payload = bytes(payload)
            next_uuid = str(uuid)
            payload_changed = (
                self._payload_signature(state.payload) != self._payload_signature(next_payload) or
                state.uuid != next_uuid)
            state.payload = next_payload
            state.uuid = next_uuid
            state.submitted += 1
            if active:
                state.active = True
                state.sustain = bool(sustain)
                state.active_remaining = 0
                state.active_deadline = 0.0
                state.zero_remaining = 0
                state.zero_deadline = 0.0
                # Preserve a transition at least once.  For one-shot effects this
                # is the whole active phase; for sustain it is the initial edge.
                if not was_active or not state.sustain:
                    state.generation += 1
                    state.edge_payload = next_payload
                    state.edge_uuid = next_uuid
                    state.edge_remaining = 1
                    state.edge_deadline = now + SYSTEM_BT_EDGE_LIFETIME
                    state.edge_generation = state.generation
            else:
                state.active = False
                state.sustain = True
                state.active_remaining = 0
                state.active_deadline = 0.0
                # A stop can cross coordinator activation: the active packet may
                # have been written directly before this coordinator existed.  A
                # first/different zero therefore must be transmitted even when our
                # local state never observed active=True.  Identical producer zeros
                # do not continually re-arm the bounded stop sequence.
                if was_active or payload_changed:
                    state.generation += 1
                    state.zero_remaining = max(
                        state.zero_remaining, SYSTEM_BT_STOP_SUCCESS_COUNT)
                    state.zero_deadline = now + SYSTEM_BT_STOP_RETRY_WINDOW
        self._wake.set()
        return True

    @staticmethod
    def _payload_signature(payload):
        """Compare rumble content without the rolling packet-id nibble."""
        if payload is None:
            return None
        signature = bytearray(payload)
        if len(signature) > 1:
            signature[1] &= 0xF0
        return bytes(signature)

    @staticmethod
    def _stamp_packet_id(payload: bytes, packet_id: int) -> bytes:
        stamped = bytearray(payload)
        # Joy-Con payload layout: byte 0 transport prefix, byte 1 = 0x50 | id.
        if len(stamped) > 1:
            stamped[1] = 0x50 | (int(packet_id) & 0x0F)
        return bytes(stamped)

    def _has_pending_work(self) -> bool:
        with self._lock:
            for state in self._sides.values():
                if state.in_flight or state.zero_remaining > 0:
                    return True
                if state.edge_remaining > 0:
                    return True
                if state.active and (
                        state.sustain or state.active_remaining > 0):
                    return True
        return False

    def _reset_diagnostics_for_resume(self, now: float) -> None:
        with self._lock:
            for state in self._sides.values():
                state.submitted = 0
                state.dispatched = 0
                state.skipped_busy = 0
                state.successful = 0
                state.edge_sent = 0
                state.edge_expired = 0
                state.last_dispatch_time = 0.0
                state.last_success_time = 0.0
                state.dispatch_intervals_ms.clear()
                state.success_intervals_ms.clear()
                state.write_latencies_ms.clear()
            self._slot_count = 0
            self._slot_interval_sum_ms = 0.0
            self._slot_intervals_ms.clear()
            self._deadline_lateness_ms.clear()
            self._missed_deadlines = 0
            self._last_slot_time = 0.0
            self._global_busy_skips = 0
            self._global_interval_skips = 0
            self._timeout_cooldowns = 0
            self._degraded_entries = 0
            self._stale_completions = 0
            self._slow_write_outliers = 0
            self._diag_started = now

    def _run(self) -> None:
        timer = _WindowsHighResolutionTimer()
        self._timer_mode = "highres" if timer.available else "event"
        logger.info(
            "System-BT merged timer session=%s mode=%s",
            self.session_id, self._timer_mode,
            extra={"system_bt_merged": True},
        )
        deadline = time.perf_counter()
        try:
            while self._running:
                if not self._has_pending_work():
                    if not self._idle:
                        self._idle = True
                        logger.info(
                            "System-BT merged coordinator idle session=%s",
                            self.session_id,
                            extra={"system_bt_merged": True},
                        )
                    # No active/stop/in-flight payload exists. Do not arm the
                    # high-resolution timer merely to produce empty slots.
                    self._wake.wait()
                    self._wake.clear()
                    if not self._running:
                        break
                    if not self._has_pending_work():
                        continue
                    now = time.perf_counter()
                    self._reset_diagnostics_for_resume(now)
                    deadline = now
                    self._idle = False
                    logger.info(
                        "System-BT merged coordinator resumed session=%s",
                        self.session_id,
                        extra={"system_bt_merged": True},
                    )

                now = time.perf_counter()
                # A completion-aware dispatch may have happened on the asyncio
                # loop since this thread last ran. Always anchor the next wake to
                # the actual dispatch, never to an old absolute timer grid.
                with self._lock:
                    actual_next_due = (
                        self._last_global_dispatch + self._slot_interval
                        if self._last_global_dispatch else deadline)
                if actual_next_due > deadline:
                    deadline = actual_next_due
                remaining = deadline - now
                if remaining > 0:
                    self._wake.clear()
                    if timer.available:
                        if not timer.wait(remaining):
                            timer.close()
                            self._timer_mode = "event"
                            self._wake.wait(timeout=remaining)
                    else:
                        self._wake.wait(timeout=remaining)
                    if not self._running:
                        break
                    now = time.perf_counter()
                    if now < deadline:
                        continue

                lateness_ms = max(0.0, (now - deadline) * 1000.0)
                self._deadline_lateness_ms.append(lateness_ms)
                if lateness_ms > 1.0:
                    self._missed_deadlines += 1
                if self._last_slot_time:
                    interval_ms = (now - self._last_slot_time) * 1000.0
                    self._slot_interval_sum_ms += interval_ms
                    self._slot_intervals_ms.append(interval_ms)
                    self._slot_count += 1
                self._last_slot_time = now

                side_name = self._choose_side(now)
                dispatched_before = self._last_global_dispatch
                if side_name is not None:
                    self._dispatch_side(side_name, now)
                self._report_diagnostics(now)

                with self._lock:
                    dispatched_at = self._last_global_dispatch
                if dispatched_at > dispatched_before:
                    # Do not catch up to the old grid after a late wake.
                    deadline = dispatched_at + self._slot_interval
                else:
                    deadline = max(deadline + self._slot_interval,
                                   now + self._slot_interval)
        finally:
            timer.close()

    def _choose_side(self, now: float):
        """Pick the most overdue eligible side without replaying missed slots."""
        with self._lock:
            if now < self._transport_cooldown_until:
                return None
            side_interval = (SYSTEM_BT_DEGRADED_SIDE_INTERVAL if self._degraded
                             else SYSTEM_BT_SIDE_MIN_DISPATCH_INTERVAL)
            candidates = []
            for order, side_name in enumerate(self._side_order):
                state = self._sides[side_name]
                if state.payload is None or state.uuid is None or state.in_flight:
                    continue
                if state.edge_remaining > 0:
                    priority = 0
                elif not state.active and state.zero_remaining > 0:
                    priority = 1
                elif state.active and state.sustain:
                    if (state.last_dispatch_time and
                            now - state.last_dispatch_time < side_interval):
                        continue
                    priority = 2
                else:
                    continue
                # Oldest dispatch wins within a priority class. The rotating order
                # breaks the initial tie without permanently favoring Left.
                age_key = state.last_dispatch_time if state.last_dispatch_time else -1.0
                tie = (order - self._next_side_index) % len(self._side_order)
                candidates.append((priority, age_key, tie, side_name))
            if not candidates:
                return None
            side_name = min(candidates)[3]
            self._next_side_index = (self._side_order.index(side_name) + 1) % len(self._side_order)
            return side_name

    def _dispatch_side(self, side_name: str, dispatch_time=None) -> None:
        dispatch_time = time.perf_counter() if dispatch_time is None else dispatch_time
        with self._lock:
            state = self._sides[side_name]
            if dispatch_time < self._transport_cooldown_until:
                return
            if state.in_flight:
                state.skipped_busy += 1
                return
            if self._global_in_flight:
                self._global_busy_skips += 1
                return
            if (self._last_global_dispatch and dispatch_time - self._last_global_dispatch <
                    SYSTEM_BT_PAIR_MIN_DISPATCH_INTERVAL):
                self._global_interval_skips += 1
                return
            if state.payload is None or state.uuid is None:
                return
            if state.edge_remaining and state.edge_deadline and dispatch_time >= state.edge_deadline:
                state.edge_remaining = 0
                state.edge_payload = None
                state.edge_uuid = None
                state.edge_expired += 1
            if (not state.active and state.zero_remaining > 0 and
                    state.zero_deadline and dispatch_time >= state.zero_deadline):
                logger.warning(
                    "System-BT merged stop retry window expired session=%s side=%s remaining=%d",
                    self.session_id, side_name, state.zero_remaining,
                    extra={"system_bt_merged": True},
                )
                state.zero_remaining = 0
                state.zero_deadline = 0.0
                return
            kind = None
            raw_payload = None
            uuid = None
            if state.edge_remaining > 0:
                kind = "edge"
                raw_payload = state.edge_payload
                uuid = state.edge_uuid
            elif not state.active and state.zero_remaining > 0:
                kind = "zero"
                raw_payload = state.payload
                uuid = state.uuid
            elif state.active and state.sustain:
                side_interval = (SYSTEM_BT_DEGRADED_SIDE_INTERVAL if self._degraded
                                 else SYSTEM_BT_SIDE_MIN_DISPATCH_INTERVAL)
                if (state.last_dispatch_time and dispatch_time - state.last_dispatch_time <
                        side_interval):
                    return
                kind = "sustain"
                raw_payload = state.payload
                uuid = state.uuid
            else:
                return
            if raw_payload is None or uuid is None:
                return
            payload = self._stamp_packet_id(raw_payload, state.tx_id)
            controller = state.controller
            generation = (state.edge_generation if kind == "edge"
                          else state.generation)
            state.in_flight = True
            self._global_in_flight += 1
            self._last_global_dispatch = dispatch_time
            if state.last_dispatch_time:
                state.dispatch_intervals_ms.append(
                    (dispatch_time - state.last_dispatch_time) * 1000.0)
            state.last_dispatch_time = dispatch_time

        loop = getattr(self._vc, "loop", None)
        if loop is None or loop.is_closed():
            with self._lock:
                state.in_flight = False
                self._global_in_flight = max(0, self._global_in_flight - 1)
            return

        coro = controller._write_merged_system_bt_rumble(uuid, payload, self.session_id, side_name)
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            coro.close()
            with self._lock:
                state.in_flight = False
                self._global_in_flight = max(0, self._global_in_flight - 1)
            return

        with self._lock:
            state.tx_id = (state.tx_id + 1) & 0x0F
            controller.vibration_packet_id = state.tx_id
            state.dispatched += 1
            self._write_futures.add(future)
        future.add_done_callback(
            lambda completed, name=side_name, write_kind=kind, write_generation=generation,
                   started=dispatch_time:
            self._write_done(name, write_kind, write_generation, completed, started))

    def _write_done(self, side_name: str, kind: str, generation: int, future,
                    dispatch_time=None) -> None:
        completed_at = time.perf_counter()
        succeeded = False
        result_status = "error"
        try:
            result = future.result()
            succeeded = result is True or result == "ok"
            result_status = "ok" if succeeded else str(result or "error")
        except Exception:
            succeeded = False
        with self._lock:
            self._write_futures.discard(future)
            state = self._sides[side_name]
            state.in_flight = False
            self._global_in_flight = max(0, self._global_in_flight - 1)
            if dispatch_time is not None:
                latency = completed_at - dispatch_time
                state.write_latencies_ms.append(latency * 1000.0)
            else:
                latency = 0.0
            if result_status == "timeout":
                self._transport_cooldown_until = max(
                    self._transport_cooldown_until,
                    completed_at + SYSTEM_BT_TIMEOUT_COOLDOWN)
                self._timeout_cooldowns += 1
                self._degraded = True
                self._degraded_until = max(
                    self._degraded_until, completed_at + SYSTEM_BT_DEGRADED_HOLD)
                self._healthy_write_streak = 0
            elif not succeeded or latency >= SYSTEM_BT_SLOW_WRITE_THRESHOLD:
                immediate_degrade = (not succeeded or
                                     latency >= SYSTEM_BT_CRITICAL_WRITE_THRESHOLD)
                if latency >= SYSTEM_BT_SLOW_WRITE_THRESHOLD:
                    self._slow_write_outliers += 1
                    self._slow_write_times.append(completed_at)
                    cutoff = completed_at - SYSTEM_BT_SLOW_WRITE_WINDOW
                    self._slow_write_times = [
                        stamp for stamp in self._slow_write_times if stamp >= cutoff]
                sustained_slow = len(self._slow_write_times) >= SYSTEM_BT_SLOW_WRITE_COUNT
                if immediate_degrade or sustained_slow:
                    if not self._degraded:
                        self._degraded_entries += 1
                    self._degraded = True
                    self._degraded_until = max(
                        self._degraded_until, completed_at + SYSTEM_BT_DEGRADED_HOLD)
                    self._healthy_write_streak = 0
                    if sustained_slow:
                        self._slow_write_times.clear()
            elif latency <= SYSTEM_BT_HEALTHY_WRITE_THRESHOLD:
                self._healthy_write_streak = min(
                    SYSTEM_BT_RECOVERY_SUCCESS_COUNT,
                    self._healthy_write_streak + 1)
                if (self._degraded and completed_at >= self._degraded_until and
                        self._healthy_write_streak >= SYSTEM_BT_RECOVERY_SUCCESS_COUNT):
                    self._degraded = False
                    self._healthy_write_streak = 0
            if succeeded:
                state.successful += 1
                if state.last_success_time:
                    state.success_intervals_ms.append(
                        (completed_at - state.last_success_time) * 1000.0)
                state.last_success_time = completed_at
            # Stop budget measures successful physical writes, not submissions.
            # If a newer active payload arrived while this zero was in flight, it
            # already cancelled the stop sequence and must not be affected here.
            expected_generation = (state.edge_generation if kind == "edge"
                                   else state.generation)
            generation_matches = generation == expected_generation
            if not generation_matches:
                self._stale_completions += 1
            if (kind == "zero" and succeeded and generation_matches and
                    not state.active and state.zero_remaining > 0):
                state.zero_remaining -= 1
                if state.zero_remaining == 0:
                    state.zero_deadline = 0.0
            elif (kind == "edge" and succeeded and generation_matches and
                  state.edge_remaining > 0):
                state.edge_remaining = 0
                state.edge_payload = None
                state.edge_uuid = None
                state.edge_sent += 1
                state.active_ever_dispatched = True
                if not state.sustain:
                    state.active = False
        self._wake.set()
        # If a 7 ms slot was skipped only because this write was still active,
        # immediately use the now-free slot when a side is already overdue. The
        # global 7 ms minimum prevents this from becoming a completion burst.
        next_side = self._choose_side(completed_at)
        if next_side is not None:
            self._dispatch_side(next_side, completed_at)

    def _report_diagnostics(self, now: float) -> None:
        if now - self._diag_started < SYSTEM_BT_PAIR_DIAG_INTERVAL:
            return
        with self._lock:
            left = self._sides["Left"]
            right = self._sides["Right"]
            avg_slot = self._slot_interval_sum_ms / self._slot_count if self._slot_count else 0.0
            slot_p95 = _percentile(self._slot_intervals_ms, 0.95)
            slot_p99 = _percentile(self._slot_intervals_ms, 0.99)
            slot_max = max(self._slot_intervals_ms, default=0.0)
            late_p95 = _percentile(self._deadline_lateness_ms, 0.95)
            logger.info(
                "System-BT merged cadence session=%s timer=%s slot_avg=%.2fms "
                "slot_p95=%.2fms slot_p99=%.2fms slot_max=%.2fms "
                "late_p95=%.2fms missed=%d "
                "L_dispatch=%d L_success=%d L_gap_p95=%.2fms L_write_p95=%.2fms L_skip=%d "
                "L_dispatch_gap_p95=%.2fms L_edge=%d L_edge_expired=%d "
                "R_dispatch=%d R_success=%d R_gap_p95=%.2fms R_write_p95=%.2fms R_skip=%d "
                "R_dispatch_gap_p95=%.2fms R_edge=%d R_edge_expired=%d "
                "global_busy_skip=%d global_interval_skip=%d global_inflight=%d "
                "degraded=%d cooldown_ms=%.1f "
                "degraded_entries=%d timeout_cooldowns=%d slow_outliers=%d "
                "healthy_streak=%d stale_done=%d",
                self.session_id,
                self._timer_mode,
                avg_slot,
                slot_p95, slot_p99, slot_max, late_p95, self._missed_deadlines,
                left.dispatched,
                left.successful,
                _percentile(left.success_intervals_ms, 0.95),
                _percentile(left.write_latencies_ms, 0.95),
                left.skipped_busy,
                _percentile(left.dispatch_intervals_ms, 0.95),
                left.edge_sent, left.edge_expired,
                right.dispatched,
                right.successful,
                _percentile(right.success_intervals_ms, 0.95),
                _percentile(right.write_latencies_ms, 0.95),
                right.skipped_busy,
                _percentile(right.dispatch_intervals_ms, 0.95),
                right.edge_sent, right.edge_expired,
                self._global_busy_skips, self._global_interval_skips,
                self._global_in_flight, int(self._degraded),
                max(0.0, (self._transport_cooldown_until - now) * 1000.0),
                self._degraded_entries, self._timeout_cooldowns,
                self._slow_write_outliers, self._healthy_write_streak,
                self._stale_completions,
                extra={"system_bt_merged": True},
            )
            for state in (left, right):
                state.submitted = 0
                state.dispatched = 0
                state.skipped_busy = 0
                state.successful = 0
                state.dispatch_intervals_ms.clear()
                state.success_intervals_ms.clear()
                state.write_latencies_ms.clear()
            self._slot_count = 0
            self._slot_interval_sum_ms = 0.0
            self._slot_intervals_ms.clear()
            self._deadline_lateness_ms.clear()
            self._missed_deadlines = 0
            self._global_busy_skips = 0
            self._global_interval_skips = 0
            self._degraded_entries = 0
            self._timeout_cooldowns = 0
            self._stale_completions = 0
            self._slow_write_outliers = 0
            self._diag_started = now
