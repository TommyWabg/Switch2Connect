# Switch2Connect - A Python and ESP32-S3 bridge utility for Switch 2 controller inputs.
# Copyright (C) 2026 TommyWabg
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Contact Information:
# Electronic Mail: tommyw9318@gmail.com

"""ESP32 bridge-level rumble dispatcher for merged Joy-Con pairs.

Problem solved here
-------------------
In merged Joy-Con mode (Left + Right pair sharing one ESP32-S3 bridge), each
Joy-Con's input loop independently calls set_vibration() which each write a
separate serial command ("wr 0 r <payload>" and "wr 1 r <payload>").  These
arrive at the firmware at different times; the BLE radio schedules them as two
independent BLE write events.  Because both connections share the same 7.5 ms
radio schedule, they tend to alternate: one Joy-Con fires while the other's BLE
window is closed, and vice versa.  Result: each motor only vibrates at ~30 Hz
and they appear to alternate ("left on, right off" / "right on, left off").

Fix
---
This module provides ESP32BridgeRumbleDispatcher, a bridge-level coordinator
that:
  1. Collects the latest Left / Right payloads via submit_left() / submit_right()
     (called from controller.set_vibration() instead of a direct BLE write).
  2. Runs a single asyncio task ticking at ESP32_BRIDGE_RUMBLE_PAIR_INTERVAL_SEC
     (default 15 ms, ~66 Hz), which is more conservative than the 7.5 ms BLE
     connection interval and avoids flooding the firmware with back-to-back writes.
  3. Sends ONE "wrpair" serial command carrying *different* L and R payloads in
     the same tick, so the firmware processes both in a single command-handling
     cycle and queues both BLE writes together.
  4. Hold-last-nonzero: if one side has no new dirty payload this tick but the
     global rumble is still considered active, it re-sends the last active payload
     so a controller whose input loop fires slightly later does not produce silence.
  5. Global idle zero: only after BOTH sides have been inactive for
     ESP32_BRIDGE_RUMBLE_GLOBAL_ZERO_AFTER_SEC (200 ms) is a final zero sent.
     Per-side auto-zero timers are NOT used — they caused L/R alternation at ~7 Hz.
  6. Per-side explicit stop: if a controller submits a zero payload (is_zero=True
     in its input loop), that side is marked wants_stop and no longer held until a
     new active payload arrives.

A/B test modes (set via CONFIG.esp32_bridge_pair_mode or module constant)
--------------------------------------------------------------------------
  "pair"   (default) – wrpair with independent L/R payloads
  "mirror"            – send same L payload to both channels via wrpair
  "single"            – bypass dispatcher entirely; controller writes directly
                        (set in controller.py, never reaches this dispatcher)

Paths NOT affected
------------------
- WinRT BLE path (is_esp32s3_bridge=False) — never reaches this module
- Single Joy-Con on bridge (is_merged=False) — condition not met, old path used
- Pro controller on bridge — uses its own set_vibration() path
- NSO GameCube controller — uses GCN PWM loop, bypasses set_vibration() entirely
"""

import asyncio
import logging
import sys
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Dispatch interval: 15 ms (~66 Hz).  Two BLE connections sharing one 7.5 ms radio
# can sustain ~66 writes/sec per channel, so this is the practical ceiling for a
# merged pair.  CRITICAL: Windows' default timer granularity is 15.6 ms, which
# makes asyncio.sleep(0.015) frequently overshoot to ~29 ms (the next tick) — only
# ~34 pairs/sec.  At that rate the ~12 ms of samples in each rumble packet leave a
# ~17 ms gap and the motor stutters.  _set_timer_resolution() raises the timer to
# 1 ms so the 15 ms tick is actually honoured (~66/sec), closing the gap.
ESP32_BRIDGE_RUMBLE_PAIR_INTERVAL_SEC = 0.015

# Global idle timeout — SAFETY NET ONLY.
# The real "stop" signal is an explicit zero payload from the controller (games
# reliably send a few zero frames when a rumble effect ends).  This timeout only
# guards the abnormal case where a game holds an active rumble then stops sending
# entirely without ever sending a zero.
#
# CRITICAL: this must be LONGER than the real rumble-update spacing, otherwise it
# fires *between* legitimate sparse updates and chops a continuous rumble.  Games
# update rumble as slowly as ~4/s (250 ms apart) — the old 200 ms value was
# shorter than that and force-stopped the motor ~4x/sec (global_zero=3/s in logs),
# which is exactly why merged rumble felt weak/sparse.  A Joy-Con sustains a
# rumble frame until the next one, so holding through the gap matches WinRT.
ESP32_BRIDGE_RUMBLE_GLOBAL_ZERO_AFTER_SEC = 1.0

# Default pair mode.  Override via CONFIG.esp32_bridge_pair_mode at runtime.
# "pair"   — wrpair with independent L/R payloads (best quality)
# "mirror" — wrpair with same L payload sent to both channels (A/B test)
ESP32_BRIDGE_PAIR_MODE = "pair"

# Fallback 17-byte Joy-Con zero payload used when no explicit zero has been
# received from the controller yet (e.g. on first global-idle expiry).
# Layout: 0x00 prefix + 0x50 packet-id + 3 x 5-byte frames.
# Frame encoding: lf_freq=0x0e1 (bits 0-8), hf_freq=0x1e1 (bits 20-28),
# all amplitude bits (10-19, 30-39) = 0.  Verified: lf_amp=0, hf_amp=0.
_ZERO_FRAME = bytes([0xE1, 0x00, 0x10, 0x1E, 0x00])
_ZERO_JOYCON_PAYLOAD = bytes([0x00, 0x50]) + _ZERO_FRAME * 3  # 17 bytes


def is_active_rumble_payload(payload: bytes) -> bool:
    """Return True if this Joy-Con rumble payload commands motor activity.

    Joy-Con HD Rumble packet layout:
      byte 0   : 0x00 (fixed prefix)
      byte 1   : 0x50 | packet_id
      bytes 2-6  : frame 0  (5-byte little-endian word)
      bytes 7-11 : frame 1
      bytes 12-16: frame 2

    Within each frame, lf_amp occupies bits 10-19 and hf_amp bits 30-39.
    Returns True if any frame has non-zero amplitude.
    Unknown / short payloads are treated as active to avoid false silencing.
    """
    if not payload or len(payload) < 17:
        return bool(payload)  # unknown layout → treat as active if non-empty
    for fo in (2, 7, 12):
        v = int.from_bytes(payload[fo:fo + 5], "little")
        if ((v >> 10) & 0x3FF) or ((v >> 30) & 0x3FF):
            return True
    return False


class _SideState:
    """Per-side (Left or Right) rumble state tracked by the dispatcher."""
    __slots__ = (
        "channel", "uuid", "payload", "last_nonzero",
        "dirty", "wants_stop", "explicit_zero_payload",
    )

    def __init__(self):
        self.channel: Optional[int] = None
        self.uuid: Optional[str] = None
        self.payload: Optional[bytes] = None
        self.last_nonzero: Optional[bytes] = None
        self.dirty: bool = False
        # Set True when controller submits explicit zero; cleared by next active.
        # While True, _pick() will NOT hold last_nonzero (motor is intentionally off).
        self.wants_stop: bool = False
        # Last zero payload received from this side's controller.  Used as the
        # payload for the global-idle zero so we send a proper controller-built
        # frame rather than the fallback constant.
        self.explicit_zero_payload: Optional[bytes] = None


class ESP32BridgeRumbleDispatcher:
    """Bridge-level rumble coordinator for a merged Joy-Con pair.

    One instance lives on the shared ESP32S3SerialClient for the lifetime of a
    merged pair session.  Both controllers (Left and Right) submit their payloads
    here; the dispatcher's asyncio task sends one wrpair command per tick.
    """

    def __init__(self, serial_client):
        # serial_client: ESP32S3SerialClient (has send_ble_write / send_ble_write_pair)
        self._client = serial_client
        self._lock = threading.Lock()
        self._left = _SideState()
        self._right = _SideState()

        # Global active tracking — updated when EITHER side submits a non-zero payload.
        self._last_active_t: float = 0.0
        # True once the final global-idle zero has been sent; reset on next active.
        self._global_zero_sent: bool = False

        # --- Diagnostics (reset every second) ---
        self._diag = {
            "pair": 0, "mirror": 0, "fallback": 0,
            "L_active": 0, "L_zero": 0, "L_hold": 0,
            "R_active": 0, "R_zero": 0, "R_hold": 0,
            "global_zero": 0,
        }
        self._diag_lock = threading.Lock()
        self._diag_t0 = time.perf_counter()
        self._last_dispatch_t = 0.0    # for actual interval measurement
        self._dispatch_interval_sum = 0.0
        self._dispatch_count = 0

        # Rolling 4-bit rumble packet-id PER CHANNEL, advanced by +1 on every BLE
        # write so each (re)sent frame is a UNIQUE rumble command.  The Joy-Con
        # dedups rumble writes by this id (byte[1] = 0x50 | id); without advancing
        # it, the hold re-sends collapse to a single motor event and the motor only
        # pulses ~1/sec.  Per-channel +1 replicates the original path, where each
        # controller's set_vibration() minted a new id on every input-loop call.
        self._tx_id_l = 0
        self._tx_id_r = 0
        self._timer_res_raised = False

        self._running = False
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Packet-id stamping
    # ------------------------------------------------------------------

    def _next_tx_id_l(self) -> int:
        self._tx_id_l = (self._tx_id_l + 1) & 0x0F
        return self._tx_id_l

    def _next_tx_id_r(self) -> int:
        self._tx_id_r = (self._tx_id_r + 1) & 0x0F
        return self._tx_id_r

    @staticmethod
    def _stamp_packet_id(payload: Optional[bytes], pid: int) -> Optional[bytes]:
        """Return payload with byte[1] rewritten to 0x50 | pid (rolling id).

        Joy-Con HD-rumble byte layout: byte0=0x00, byte1=0x50|packet_id.  Rewriting
        the id makes an otherwise-identical held frame a distinct command so the
        Joy-Con re-triggers the motor instead of discarding it as a duplicate.
        """
        if payload is None or len(payload) < 2:
            return payload
        b = bytearray(payload)
        b[1] = 0x50 | (pid & 0x0F)
        return bytes(b)

    # ------------------------------------------------------------------
    # Public API called from controller.set_vibration()
    # ------------------------------------------------------------------

    def submit_left(self, channel: int, uuid: str, payload: bytes) -> None:
        """Record a new Left Joy-Con rumble payload (non-blocking)."""
        self._submit(self._left, channel, uuid, payload)

    def submit_right(self, channel: int, uuid: str, payload: bytes) -> None:
        """Record a new Right Joy-Con rumble payload (non-blocking)."""
        self._submit(self._right, channel, uuid, payload)

    def _submit(self, side: _SideState, channel: int, uuid: str, payload: bytes) -> None:
        now = time.perf_counter()
        if len(payload) != 17:
            logger.warning("RUMBLE-SUBMIT unexpected payload len=%d (expected 17) hex=%s",
                           len(payload), bytes(payload).hex())
        active = is_active_rumble_payload(payload)
        with self._lock:
            side.channel = channel
            side.uuid = uuid
            side.payload = bytes(payload)
            side.dirty = True
            if active:
                side.last_nonzero = bytes(payload)
                side.wants_stop = False
                self._last_active_t = now
                self._global_zero_sent = False
            else:
                side.wants_stop = True
                side.explicit_zero_payload = bytes(payload)

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

    def _set_timer_resolution(self, enable: bool) -> None:
        """Raise/restore the Windows multimedia timer resolution to 1 ms.

        Default Windows granularity is 15.6 ms, which makes asyncio.sleep(0.015)
        overshoot to ~29 ms.  timeBeginPeriod(1) lets the 15 ms dispatch tick be
        honoured so rumble frames are spaced ~15 ms apart (no motor gap).  No-op
        off Windows.  Always balanced by a matching timeEndPeriod on stop.
        """
        if sys.platform != "win32":
            return
        try:
            import ctypes
            if enable and not self._timer_res_raised:
                ctypes.windll.winmm.timeBeginPeriod(1)
                self._timer_res_raised = True
            elif not enable and self._timer_res_raised:
                ctypes.windll.winmm.timeEndPeriod(1)
                self._timer_res_raised = False
        except Exception as e:
            logger.debug("timeBeginPeriod/timeEndPeriod failed: %s", e)

    def start(self) -> None:
        """Start the dispatch loop as an asyncio task in the running event loop."""
        if self._running:
            return
        self._running = True
        self._set_timer_resolution(True)
        self._last_dispatch_t = time.perf_counter()
        self._task = asyncio.create_task(self._dispatch_loop())
        logger.info(
            "ESP32 rumble dispatcher started  interval=%.0f ms  global_zero=%.0f ms  mode=%s",
            ESP32_BRIDGE_RUMBLE_PAIR_INTERVAL_SEC * 1000,
            ESP32_BRIDGE_RUMBLE_GLOBAL_ZERO_AFTER_SEC * 1000,
            self._get_pair_mode(),
        )

    def stop(self) -> None:
        """Stop the dispatch loop."""
        self._running = False
        if self._task is not None:
            try:
                self._task.cancel()
            except Exception:
                pass
            self._task = None
        self._set_timer_resolution(False)
        logger.debug("ESP32 rumble dispatcher stopped")

    # ------------------------------------------------------------------
    # Dispatch loop
    # ------------------------------------------------------------------

    async def _dispatch_loop(self) -> None:
        while self._running:
            t0 = time.perf_counter()
            try:
                if self._client.is_connected:
                    await self._do_dispatch()
            except Exception:
                logger.exception("ESP32 rumble dispatcher tick error")
            elapsed = time.perf_counter() - t0
            sleep_t = max(0.001, ESP32_BRIDGE_RUMBLE_PAIR_INTERVAL_SEC - elapsed)
            await asyncio.sleep(sleep_t)

    def _get_pair_mode(self) -> str:
        """Read pair mode from CONFIG if available, otherwise use module default."""
        try:
            from config import CONFIG as _cfg  # type: ignore[import]
            return getattr(_cfg, "esp32_bridge_pair_mode", ESP32_BRIDGE_PAIR_MODE)
        except Exception:
            return ESP32_BRIDGE_PAIR_MODE

    async def _do_dispatch(self) -> None:
        now = time.perf_counter()

        # Measure actual dispatch interval for diagnostics.
        if self._last_dispatch_t > 0:
            self._dispatch_interval_sum += (now - self._last_dispatch_t) * 1000.0
            self._dispatch_count += 1
        self._last_dispatch_t = now

        global_idle = False
        with self._lock:
            left, right = self._left, self._right

            # Fast path: BOTH sides explicitly stopped and their zero frames have
            # already been delivered (dirty consumed).  The motors are off; there is
            # nothing to keep sending.  This is the normal end-of-rumble case and it
            # exits WITHOUT waiting for the global timeout, so there is no churn.
            both_stopped = (left.wants_stop and not left.dirty and
                            right.wants_stop and not right.dirty)
            if both_stopped:
                return

            has_been_active = self._last_active_t > 0
            global_idle = has_been_active and (
                now - self._last_active_t >= ESP32_BRIDGE_RUMBLE_GLOBAL_ZERO_AFTER_SEC
            )

            if global_idle and self._global_zero_sent:
                # Safety-net zero already sent; idle until a new active arrives.
                return

            l_ch, r_ch = left.channel, right.channel
            l_uuid, r_uuid = left.uuid, right.uuid

            if global_idle:
                # SAFETY NET: a side held an active rumble but the game stopped
                # sending entirely without an explicit zero.  Force both motors off.
                l_payload = left.explicit_zero_payload or _ZERO_JOYCON_PAYLOAD
                r_payload = right.explicit_zero_payload or _ZERO_JOYCON_PAYLOAD
                l_hold = r_hold = False
                self._global_zero_sent = True
                left.wants_stop = True
                right.wants_stop = True
                with self._diag_lock:
                    self._diag["global_zero"] += 1
            else:
                # Active window: emit a clean PAIR every tick.  Each side contributes
                # its effective payload (active-held, freshly-submitted, or its own
                # zero if that side was stopped) so we never fall back to a lone
                # single-channel write that could re-introduce L/R alternation.
                l_payload, l_hold = self._pick(left)
                r_payload, r_hold = self._pick(right)

        if l_payload is None and r_payload is None:
            return

        self._record_diag(l_payload, r_payload, l_hold, r_hold)
        self._send(l_ch, r_ch, l_payload, r_payload,
                   l_uuid=l_uuid, r_uuid=r_uuid, zero_send=global_idle)

    def _pick(self, side: _SideState) -> tuple[Optional[bytes], bool]:
        """Choose this side's effective payload for the current tick.

        Returns (payload_or_None, was_held).  Called with self._lock held.

        Rules (in order):
          1. dirty → send exactly what was submitted (active or explicit zero),
             clear dirty, and update wants_stop accordingly.
          2. wants_stop → motor is intentionally off; emit this side's own zero
             payload so the pair stays balanced (the other side may be active).
             Marked not-held so diagnostics count it as a zero, not a hold.
          3. otherwise hold last_nonzero so a sparse-updating game (e.g. ~4 active
             frames/sec) keeps the motor fed between updates — matching how a
             Joy-Con sustains a rumble frame on the WinRT path.
        """
        if side.dirty:
            side.dirty = False
            if is_active_rumble_payload(side.payload):
                side.wants_stop = False
            else:
                side.wants_stop = True
            return side.payload, False

        if side.wants_stop:
            # Keep the channel paired with a zero frame rather than dropping it.
            return (side.explicit_zero_payload or _ZERO_JOYCON_PAYLOAD), False

        if side.last_nonzero is not None:
            return side.last_nonzero, True

        return None, False

    def _send(self, l_ch, r_ch, l_payload, r_payload, l_uuid, r_uuid, zero_send: bool) -> None:
        """Send one pair or individual write(s) based on current mode."""
        mode = self._get_pair_mode()

        # Stamp a fresh per-channel rolling packet-id on every write so held
        # re-sends are not discarded by the Joy-Con as duplicates.  Each channel
        # advances its own id by exactly +1, matching the original protocol.
        l_payload = self._stamp_packet_id(l_payload, self._next_tx_id_l())
        r_payload = self._stamp_packet_id(r_payload, self._next_tx_id_r())

        if l_payload is None or r_payload is None:
            # Only one side has data — fallback to individual writes.
            if l_payload is not None and l_ch is not None and l_uuid is not None:
                self._client.send_ble_write(l_ch, l_uuid, l_payload)
                with self._diag_lock:
                    self._diag["fallback"] += 1
            if r_payload is not None and r_ch is not None and r_uuid is not None:
                self._client.send_ble_write(r_ch, r_uuid, r_payload)
                with self._diag_lock:
                    self._diag["fallback"] += 1
            return

        if l_ch is None or r_ch is None:
            return

        if mode == "mirror":
            # Send same L payload to both channels (A/B test: compare to pair mode).
            ok = self._client.send_ble_write_pair(l_ch, r_ch, "r", l_payload, l_payload)
            with self._diag_lock:
                if ok:
                    self._diag["mirror"] += 1
                else:
                    self._diag["fallback"] += 2
        else:
            # "pair" mode (default): independent L/R payloads.
            ok = self._client.send_ble_write_pair(l_ch, r_ch, "r", l_payload, r_payload)
            with self._diag_lock:
                if ok:
                    self._diag["pair"] += 1
                else:
                    self._diag["fallback"] += 2
            if not ok and not zero_send:
                # wrpair failed — fall back to individual writes so rumble isn't lost.
                logger.debug("ESP32 wrpair failed; falling back to individual writes")
                if l_uuid:
                    self._client.send_ble_write(l_ch, l_uuid, l_payload)
                if r_uuid:
                    self._client.send_ble_write(r_ch, r_uuid, r_payload)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def _record_diag(self, l_payload, r_payload, l_hold, r_hold):
        now = time.perf_counter()
        with self._diag_lock:
            d = self._diag
            if l_payload is not None:
                if is_active_rumble_payload(l_payload):
                    d["L_active"] += 1
                else:
                    d["L_zero"] += 1
                if l_hold:
                    d["L_hold"] += 1
            if r_payload is not None:
                if is_active_rumble_payload(r_payload):
                    d["R_active"] += 1
                else:
                    d["R_zero"] += 1
                if r_hold:
                    d["R_hold"] += 1

            if now - self._diag_t0 >= 1.0:
                avg_itvl = (self._dispatch_interval_sum / self._dispatch_count
                            if self._dispatch_count else 0.0)
                logger.info(
                    "RUMBLE-DISPATCH pair=%d mirror=%d fallback=%d global_zero=%d "
                    "L_act=%d L_zer=%d L_hld=%d "
                    "R_act=%d R_zer=%d R_hld=%d "
                    "interval_avg=%.1f ms /s",
                    d["pair"], d["mirror"], d["fallback"], d["global_zero"],
                    d["L_active"], d["L_zero"], d["L_hold"],
                    d["R_active"], d["R_zero"], d["R_hold"],
                    avg_itvl,
                )
                for k in d:
                    d[k] = 0
                self._diag_t0 = now
                self._dispatch_interval_sum = 0.0
                self._dispatch_count = 0
