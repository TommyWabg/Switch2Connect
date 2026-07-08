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

import struct
import logging
import time
import queue
import ctypes
import os
import random
import sys
import threading
import heapq

import numpy as np  # module-level import: avoid repeated in-function import cost on each diagnostic cycle

# Request 1ms timer resolution on Windows so time.sleep() is accurate enough for ISO pacing.
try:
    sys.setswitchinterval(0.001)
    ctypes.windll.winmm.timeBeginPeriod(1)
except Exception:
    pass

# A real USB DualSense exposes NO serial number (iSerialNumber = 0 in the device
# descriptor), and neither do we — see dualsense_descriptors.py.  A PlayStation HID
# serial makes strict tools (e.g. SpecialK) treat the pad as a *Bluetooth* DualSense
# and expect BT-format reports, which breaks audio haptics.  Windows still keeps the
# device stable per USBIP port/location, so dropping the serial does not regress
# endpoint identity.
logger_boot = logging.getLogger(__name__)
logger_boot.info("DualSense USBIP device: no USB serial (matches a real DualSense)")

from usbip_server import USBIPServer, USBIP_VERSION, OP_REP_DEVLIST, OP_REP_IMPORT, USBIP_RET_SUBMIT, is_socket_disconnect
from dualsense_descriptors import (
    DUALSENSE_DEVICE_DESCRIPTOR, 
    DUALSENSE_CONFIGURATION_DESCRIPTOR, 
    DUALSENSE_CONFIGURATION_DESCRIPTOR_NO_AUDIO,
    DUALSENSE_HID_REPORT_DESCRIPTOR,
    DUALSENSE_STRING_LANG,
    DUALSENSE_STRING_MANUFACTURER,
    DUALSENSE_STRING_PRODUCT,
    DUALSENSE_STRING_AUDIO,
    DUALSENSE_STRING_HID
)
from dualsense_haptic import DualSenseHapticProcessor
from dualsense_structs import DualSenseInputReport01, DualSenseOutputReport02

logger = logging.getLogger(__name__)
_PERF_DIAGNOSTICS = os.environ.get('SWITCH2_PERF_DIAGNOSTICS', '0') == '1'
EP1_OUT_PACING_ENABLED = os.environ.get('SWITCH2_EP1_OUT_PACING', '1') != '0'
EP1_OUT_FAST_TX_ENABLED = os.environ.get('SWITCH2_EP1_OUT_FAST_TX', '1') != '0'
DIAGNOSTIC_LOG_INTERVAL = 2.0
HAPTIC_PROCESS_INTERVAL = 0.015
ISO_BACKLOG_RELIEF_DEPTH = 4
ISO_BACKLOG_LOG_INTERVAL = 2.0
DUALSENSE_USBIP_DESCRIPTOR_PROFILE = "ds5bridge-full-speed-audio-ep1-adaptive-1ms"
MIC_ISO_BYTES_PER_FRAME = 96  # 1ch * 16-bit * 48kHz / 1000 USB frames
ISO_RESET_BARRIER_SEC = 0.025
AUDIO_OUT_PACER_LEAD_SEC = 0.0010
AUDIO_OUT_IDLE_RESET_SEC = 0.5
AUDIO_OUT_MAX_DEADLINE_SPAN_SEC = 0.016
AUDIO_OUT_MAX_MISSED_TICKS = 8
AUDIO_OUT_MIN_COMPLETE_GAP_SEC = 0.0015
AUDIO_OUT_CATCHUP_GAP_SEC = 0.005
AUDIO_OUT_WARMUP_URBS = 64
AUDIO_OUT_WARMUP_LATENCY_SLACK_SEC = 0.0020
AUDIO_OUT_LOCK_STABLE_URBS = 24
AUDIO_OUT_LOCK_JITTER_SEC = 0.0025
AUDIO_OUT_LOCK_INTERVAL_TOLERANCE_SEC = 0.0030
AUDIO_OUT_SOFT_CLEAR_LOG_INTERVAL = 2.0
AUDIO_OUT_SOFT_CLEAR_MAX_QUEUE = 1
AUDIO_OUT_SOFT_CLEAR_MAX_TX_AUDIO = 1
AUDIO_OUT_SOFT_CLEAR_MAX_SCHED_DEPTH = 1
AUDIO_OUT_SOFT_CLEAR_MAX_LATENCY_SEC = 0.025
AUDIO_OUT_SOFT_CLEAR_MAX_COMPLETE_AGE_SEC = 0.100
# The completion cadence must equal the audio rate (one packet-set per `duration`),
# NOT a fixed per-URB latency cap.  usbaudio keeps 2-3 ISO URBs in flight, so a URB
# legitimately completes `duration * pipeline_depth` after its own submit; capping
# per-URB latency forces early completion during the vhci's bursty TCP delivery, which
# collapses the accumulated clock and makes usbaudio see a runaway stream clock ->
# CLEAR_FEATURE(ENDPOINT_HALT) loop.  The accumulated clock is primary; this cap is only
# a runaway safety (4x duration) for a pathological backlog, and triggering it resyncs.
AUDIO_OUT_RUNAWAY_CAP_SEC = 0.040
OUTPUT_REPORT_QUEUE_MAX = 256
OUTPUT_REPORT_BACKLOG_LOG_INTERVAL = 2.0
TX_PRIORITY_CONTROL = 0
TX_PRIORITY_AUDIO_ISO = 1
TX_PRIORITY_HID_IN = 2
TX_PRIORITY_MIC_ISO = 3
TX_PRIORITY_OTHER = 4

_NATIVE_TIMING_LIB = None
_NATIVE_TIMING_LOAD_ATTEMPTED = False


def _native_timing_candidates():
    names = ("dualsense_haptic_native.dll",)
    module_dir = os.path.dirname(os.path.abspath(__file__))
    roots = [
        module_dir,
        os.path.join(os.path.dirname(module_dir), "drivers"),
        os.path.join(os.path.dirname(module_dir), "native"),
    ]
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        roots.extend([base, os.path.join(base, "drivers"), os.path.join(base, "src")])
    for root in roots:
        for name in names:
            yield os.path.join(root, name)


def _set_thread_priority(level):
    try:
        if sys.platform == "win32":
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetCurrentThread()
            kernel32.SetThreadPriority(handle, level)
    except Exception:
        pass

def _load_native_timing():
    global _NATIVE_TIMING_LIB, _NATIVE_TIMING_LOAD_ATTEMPTED
    if _NATIVE_TIMING_LOAD_ATTEMPTED:
        return _NATIVE_TIMING_LIB
    _NATIVE_TIMING_LOAD_ATTEMPTED = True
    for path in _native_timing_candidates():
        if not os.path.exists(path):
            continue
        try:
            lib = ctypes.CDLL(path)
            lib.ds_precise_sleep_us.argtypes = [ctypes.c_int]
            lib.ds_precise_sleep_us.restype = None
            _NATIVE_TIMING_LIB = lib
            logger.info("Loaded native USB audio timing helper: %s", path)
            return lib
        except Exception as exc:
            logger.warning("Failed to load native USB audio timing helper %s: %s", path, exc)
            continue
    logger.warning(
        "Native USB audio timing helper not found; falling back to imprecise sleep. Searched: %s",
        list(_native_timing_candidates()),
    )
    return None

class USBIPDualSenseServer(USBIPServer):
    def __init__(self, host="127.0.0.1", port=3240, on_rumble_callback=None, bus_id="1-1", mac_address=None, on_audio_data_callback=None, on_disconnect_callback=None, enable_audio=True):
        super().__init__(host=host, port=port, on_rumble_callback=on_rumble_callback, bus_id=bus_id, mac_address=mac_address, on_audio_data_callback=on_audio_data_callback, on_disconnect_callback=on_disconnect_callback)
        self.enable_audio = enable_audio
        
        # 徹底拆分 IN (麥克風) 與 OUT (喇叭/震動) 的佇列，避免互相阻塞
        self.pending_iso_out_urbs = queue.Queue()
        self.pending_iso_in_urbs = queue.Queue()
        self._latest_audio_out_packet = None
        self._audio_out_event = threading.Event()
        self._audio_out_lock = threading.Lock()
        self._audio_out_stop = False
        self.last_audio_log = 0
        self.dropped_audio_packets = 0
        self.skipped_haptic_packets = 0
        self.iso_out_backlog_relief_count = 0
        self.iso_in_backlog_relief_count = 0
        self._last_iso_shape_by_ep = {}
        self._audio_stream_lock = threading.Lock()
        self._audio_stream_generation = {"out": 0, "in": 0}
        self._audio_stream_active = {"out": False, "in": False}
        self._audio_pacers = {"out": self._IsoPacer(), "in": self._IsoPacer()}
        self._audio_reset_barrier_until = {"out": 0.0, "in": 0.0}
        self._next_haptic_capture_time = 0.0
        self._dead_socket_ids = set()
        self._dead_socket_lock = threading.Lock()
        self._audio_urb_meta = {}
        self._audio_urb_meta_lock = threading.Lock()
        self.rumble_queue = queue.Queue(maxsize=3)
        self.output_report_queue = queue.Queue(maxsize=OUTPUT_REPORT_QUEUE_MAX)
        self.output_report_dropped = 0
        self.tx_queue = queue.PriorityQueue()
        self._tx_seq = 0
        self._tx_seq_lock = threading.Lock()
        self._tx_audio_count_lock = threading.Lock()
        self._tx_audio_counts = {"out": 0, "in": 0}
        self.tx_audio_stale_dropped = {"out": 0, "in": 0}
        self._audio_out_last_submit_time = 0.0
        self._audio_out_last_submit_seq = 0
        self._audio_out_last_complete_time = 0.0
        self._audio_out_last_complete_seq = 0
        self._audio_out_last_complete_latency = 0.0
        self._audio_out_last_pacer_wait = 0.0
        self._audio_out_last_pacer_late = 0.0
        self._audio_out_last_pacer_duration = 0.0
        self._audio_out_last_pacer_first = False
        self._audio_out_last_pacer_reset = False
        self._audio_out_last_due_delay = 0.0
        self._audio_out_last_schedule_depth = 0
        self._audio_out_schedule_lock = threading.Lock()
        self._audio_out_next_due = 0.0
        self._audio_out_clock_active = False
        self._audio_out_missed_tick_skips = 0
        self._audio_out_clock_resyncs = 0
        self._audio_out_last_complete_interval = 0.0
        self._audio_out_last_submit_interval = 0.0
        self._audio_out_last_worker_wakeup_late = 0.0
        self._audio_out_last_target_latency = 0.0
        self._audio_out_last_latency_cap_hit = False
        self._audio_out_last_target_source = "init"
        self._audio_out_latency_cap_hits = 0
        self._audio_out_last_start_frame = 0
        self._audio_out_last_seqnum = 0
        self._audio_out_frame_clock_valid = False
        self._audio_out_frame_clock_base_frame = 0
        self._audio_out_frame_clock_base_time = 0.0
        self._audio_out_last_frame_phase_error = 0.0
        self._audio_out_warmup_remaining = AUDIO_OUT_WARMUP_URBS
        self._audio_out_warmup_count = 0
        self._audio_out_clock_locked = False
        self._audio_out_submit_interval_ema = 0.0
        self._audio_out_submit_jitter_ema = 0.0
        self._audio_out_stable_submit_count = 0
        self._audio_out_latency_ceiling_hits = 0
        self._audio_out_soft_clear_count = 0
        self._audio_out_hard_clear_count = 0
        self._audio_out_soft_clear_window_count = 0
        self._audio_out_last_soft_clear_log = 0.0
        self._audio_out_soft_clear_max_latency = 0.0
        self._audio_out_soft_clear_max_wake_late = 0.0
        self._audio_out_soft_clear_max_queue = 0
        self._audio_out_warmup_hold = False
        self._native_timing = _load_native_timing()
        
        self.last_state = DualSenseInputReport01()
        self.last_state.ReportId = 0x01
        self.last_state.LeftStickX = 128
        self.last_state.LeftStickY = 128
        self.last_state.RightStickX = 128
        self.last_state.RightStickY = 128
        self.last_state.Hat = 0x08
        self.last_state.PowerPercent = 10
        self.last_state.PowerState = 2
        
        self.last_state.PluggedUsbData = 1
        self.last_state.PluggedMic = 0
        self.last_state.PluggedHeadphones = 0
        self.last_state.MicMuted = 1
        self.audio_active = False
        self.dualsense_haptics_blocked = False
        self._audio_host_mute = [0, 0, 0]  # speaker, mic, reserved/line
        self._audio_host_volume_units = [0, 48 * 256, 48 * 256]

        # Virtual microphone: the Switch 2 controller has NO physical microphone, so the
        # ISO IN endpoint (EP 0x82) always streams silence.  The endpoint/descriptor are
        # kept so games that require a DualSense mic still enumerate one, but we never
        # capture the host mic — doing so opened a second WASAPI capture stream on our own
        # virtual endpoint (a self-capture feedback loop) that made usbaudio.sys halt
        # EP 0x82 and abort the connection the instant a game opened the mic.
        self.mic_active = False
        logger.info("DualSense USBIP descriptor profile: %s", DUALSENSE_USBIP_DESCRIPTOR_PROFILE)

    def _stream_key(self, direction, ep):
        if direction == 0 and ep == 1:
            return "out"
        if direction == 1 and ep == 2:
            return "in"
        return None

    def _current_stream_generation(self, key):
        with self._audio_stream_lock:
            return self._audio_stream_generation.get(key, 0)

    def _stream_generation_is_current(self, key, generation):
        with self._audio_stream_lock:
            return self._audio_stream_generation.get(key, 0) == generation

    def _get_stream_pacer(self, key):
        with self._audio_stream_lock:
            return self._audio_pacers[key]

    def _reset_audio_out_scheduler(self):
        with self._audio_out_schedule_lock:
            self._audio_out_next_due = 0.0
            self._audio_out_clock_active = False
            self._audio_out_last_due_delay = 0.0
            self._audio_out_last_schedule_depth = 0
            self._audio_out_last_pacer_wait = 0.0
            self._audio_out_last_pacer_late = 0.0
            self._audio_out_last_pacer_duration = 0.0
            self._audio_out_last_pacer_first = True
            self._audio_out_last_pacer_reset = True
            self._audio_out_last_worker_wakeup_late = 0.0
            self._audio_out_last_target_latency = 0.0
            self._audio_out_last_latency_cap_hit = False
            self._audio_out_last_target_source = "reset"
            self._audio_out_last_start_frame = 0
            self._audio_out_last_seqnum = 0
            self._audio_out_frame_clock_valid = False
            self._audio_out_frame_clock_base_frame = 0
            self._audio_out_frame_clock_base_time = 0.0
            self._audio_out_last_frame_phase_error = 0.0
            self._audio_out_warmup_remaining = AUDIO_OUT_WARMUP_URBS
            self._audio_out_warmup_count = 0
            self._audio_out_clock_locked = False
            self._audio_out_submit_interval_ema = 0.0
            self._audio_out_submit_jitter_ema = 0.0
            self._audio_out_stable_submit_count = 0
            self._audio_out_warmup_hold = False

    def _is_soft_audio_out_clear(
        self,
        queue_depth,
        tx_depth,
        sched_depth,
        complete_age_sec,
        complete_latency_sec,
        target_latency_sec,
        shape,
    ):
        if not getattr(self, "audio_active", False):
            return False
        if queue_depth > AUDIO_OUT_SOFT_CLEAR_MAX_QUEUE:
            return False
        if tx_depth > AUDIO_OUT_SOFT_CLEAR_MAX_TX_AUDIO:
            return False
        if sched_depth > AUDIO_OUT_SOFT_CLEAR_MAX_SCHED_DEPTH:
            return False
        if complete_age_sec < 0.0 or complete_age_sec > AUDIO_OUT_SOFT_CLEAR_MAX_COMPLETE_AGE_SEC:
            return False
        if complete_latency_sec < 0.0 or complete_latency_sec > AUDIO_OUT_SOFT_CLEAR_MAX_LATENCY_SEC:
            return False
        if target_latency_sec < 0.0 or target_latency_sec > AUDIO_OUT_SOFT_CLEAR_MAX_LATENCY_SEC:
            return False
        if not shape or shape.get("direction") != "OUT" or shape.get("packets", 0) <= 0:
            return False
        return True

    def _record_audio_out_soft_clear(
        self,
        queue_depth,
        tx_depth,
        sched_depth,
        target_latency_ms,
        wake_late_ms,
        complete_latency_ms,
        warmup_remaining,
        warmup_count,
        target_source,
    ):
        now_time = time.perf_counter()
        self._audio_out_soft_clear_count += 1
        self._audio_out_soft_clear_window_count += 1
        self._audio_out_warmup_hold = True
        self._audio_out_soft_clear_max_queue = max(self._audio_out_soft_clear_max_queue, queue_depth)
        self._audio_out_soft_clear_max_latency = max(
            self._audio_out_soft_clear_max_latency,
            complete_latency_ms,
            target_latency_ms,
        )
        self._audio_out_soft_clear_max_wake_late = max(
            self._audio_out_soft_clear_max_wake_late,
            wake_late_ms,
        )
        if now_time - self._audio_out_last_soft_clear_log < AUDIO_OUT_SOFT_CLEAR_LOG_INTERVAL:
            return
        logger.info(
            "Audio OUT soft CLEAR_FEATURE summary: clears=%d total=%d hard=%d queue_max=%d tx_audio=%d sched_depth=%d target=%s target_latency=%.2fms complete_latency_max=%.2fms wake_late_max=%.2fms warmup_hold=%d warmup=%d warmup_count=%d",
            self._audio_out_soft_clear_window_count,
            self._audio_out_soft_clear_count,
            self._audio_out_hard_clear_count,
            self._audio_out_soft_clear_max_queue,
            tx_depth,
            sched_depth,
            target_source,
            target_latency_ms,
            self._audio_out_soft_clear_max_latency,
            self._audio_out_soft_clear_max_wake_late,
            1 if self._audio_out_warmup_hold else 0,
            warmup_remaining,
            warmup_count,
        )
        self._audio_out_last_soft_clear_log = now_time
        self._audio_out_soft_clear_window_count = 0
        self._audio_out_soft_clear_max_latency = 0.0
        self._audio_out_soft_clear_max_wake_late = 0.0
        self._audio_out_soft_clear_max_queue = 0

    def _audio_out_duration(self, out_data, num_packets):
        if num_packets > 0:
            return max(0.001, num_packets * 0.001)
        if out_data:
            return max(0.001, len(out_data) / 384000.0)
        return 0.001

    def _schedule_audio_out_urb(self, urb, duration):
        with self._audio_out_schedule_lock:
            depth = self.pending_iso_out_urbs.qsize()
            self._audio_out_last_schedule_depth = depth
            self._audio_out_last_pacer_duration = duration
            self._audio_out_last_pacer_first = not self._audio_out_clock_active
            self._audio_out_last_pacer_reset = False
            self._audio_out_last_due_delay = 0.0
            self._audio_out_last_pacer_wait = 0.0
            self._audio_out_last_pacer_late = 0.0
        self.pending_iso_out_urbs.put(urb)

    def _audio_out_precise_wait(self, wait):
        if wait <= 0:
            return
        target = time.perf_counter() + wait
        native = getattr(self, "_native_timing", None)
        if native is not None:
            try:
                native.ds_precise_sleep_us(max(1, int(wait * 1000000.0)))
                return
            except Exception:
                self._native_timing = None
        while True:
            remaining = target - time.perf_counter()
            if remaining <= 0:
                return
            if remaining > 0.002:
                time.sleep(0.001)
            else:
                break
        while time.perf_counter() < target:
            time.sleep(0)

    def _set_stream_reset_barrier(self, key, duration=ISO_RESET_BARRIER_SEC):
        until = time.perf_counter() + duration
        with self._audio_stream_lock:
            self._audio_reset_barrier_until[key] = max(
                self._audio_reset_barrier_until.get(key, 0.0),
                until,
            )

    def _wait_stream_reset_barrier(self, key):
        with self._audio_stream_lock:
            wait = self._audio_reset_barrier_until.get(key, 0.0) - time.perf_counter()
        if wait > 0:
            time.sleep(wait)

    def _reset_stream_pacer(self, key, reason="reset"):
        with self._audio_stream_lock:
            self._audio_pacers[key] = self._IsoPacer()
            generation = self._audio_stream_generation.get(key, 0)
            active = self._audio_stream_active.get(key)
        logger.info(
            "Audio %s pacer %s: active=%s generation=%d",
            key.upper(),
            reason,
            active,
            generation,
        )

    def _reset_audio_stream(self, key, active=None, reason="reset", drain=False):
        with self._audio_stream_lock:
            self._audio_stream_generation[key] = self._audio_stream_generation.get(key, 0) + 1
            self._audio_pacers[key] = self._IsoPacer()
            if active is not None:
                self._audio_stream_active[key] = bool(active)
            generation = self._audio_stream_generation[key]
        if key == "out":
            self._reset_audio_out_scheduler()
        if drain:
            self._drain_audio_queue(key)
        logger.info(
            "Audio %s stream %s: active=%s generation=%d",
            key.upper(),
            reason,
            self._audio_stream_active.get(key),
            generation,
        )

    def _invalidate_audio_stream(self, key, active=None, reason="reset", drain=False, reset_pacer=False):
        with self._audio_stream_lock:
            self._audio_stream_generation[key] = self._audio_stream_generation.get(key, 0) + 1
            if reset_pacer:
                self._audio_pacers[key] = self._IsoPacer()
            if active is not None:
                self._audio_stream_active[key] = bool(active)
            generation = self._audio_stream_generation[key]
        if key == "out" and reset_pacer:
            self._reset_audio_out_scheduler()
        if drain:
            self._drain_audio_queue(key)
        logger.info(
            "Audio %s stream %s: active=%s generation=%d",
            key.upper(),
            reason,
            self._audio_stream_active.get(key),
            generation,
        )

    def _drain_audio_queue(self, key):
        q = self.pending_iso_out_urbs if key == "out" else self.pending_iso_in_urbs
        completed = 0
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                break
            if item is None:
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
                break
            # Complete (RET_SUBMIT) every queued URB instead of dropping it.  A dropped-
            # but-unanswered URB stays pending in the vhci and eventually times out ->
            # device teardown.  _fast_complete_urb honors socket-dead and UNLINK, so a
            # genuinely dead socket / unlinked URB is still skipped safely.
            self._fast_complete_urb(item, key)
            completed += 1
        if completed:
            logger.info("Fast-completed %d queued audio %s ISO URBs on stream reset", completed, key.upper())

    def _fast_complete_urb(self, urb, stream_key):
        """Send RET_SUBMIT for a URB we are no longer pacing (stream reset / drain).

        Accepts any of the queue tuple layouts (10 base, 11 +generation, 13 +duration
        /scheduled_at).  generation is passed as None so _send_iso_reply does not treat
        it as stale — the reply is gated only by socket-dead and UNLINK inside it.
        """
        try:
            if urb is None:
                return
            sock, seqnum, devid, direction, ep, transfer_length, out_data, start_frame, num_packets, iso_descriptors = urb[:10]
            self._send_iso_reply(sock, seqnum, devid, direction, ep, transfer_length, out_data, start_frame, num_packets, iso_descriptors, stream_key, None)
        except Exception:
            logger.debug("Fast-complete of queued audio URB failed", exc_info=True)

    def _mark_socket_dead(self, sock):
        with self._dead_socket_lock:
            self._dead_socket_ids.add(id(sock))
        try:
            self._reset_audio_stream("out", active=False, reason="socket dead", drain=True)
            self._reset_audio_stream("in", active=False, reason="socket dead", drain=True)
            self._drop_tx_audio_stream_items("out")
            self._drop_tx_audio_stream_items("in")
        except Exception:
            logger.debug("Failed to reset audio streams for dead socket", exc_info=True)

    def _mark_socket_alive(self, sock):
        with self._dead_socket_lock:
            self._dead_socket_ids.discard(id(sock))
        try:
            self._reset_audio_stream("out", active=False, reason="socket alive", drain=True)
            self._reset_audio_stream("in", active=False, reason="socket alive", drain=True)
            self._drop_tx_audio_stream_items("out")
            self._drop_tx_audio_stream_items("in")
        except Exception:
            logger.debug("Failed to reset audio streams for new socket", exc_info=True)

    def _socket_is_dead(self, sock):
        with self._dead_socket_lock:
            return id(sock) in self._dead_socket_ids

    def _remember_audio_urb(self, seqnum, stream_key, endpoint):
        with self._audio_urb_meta_lock:
            self._audio_urb_meta[seqnum] = (stream_key, endpoint, time.perf_counter())
            if len(self._audio_urb_meta) > 4096:
                for old_seq in sorted(self._audio_urb_meta)[:2048]:
                    self._audio_urb_meta.pop(old_seq, None)

    def _forget_audio_urb(self, seqnum):
        with self._audio_urb_meta_lock:
            return self._audio_urb_meta.pop(seqnum, None)

    def _peek_audio_urb(self, seqnum):
        with self._audio_urb_meta_lock:
            return self._audio_urb_meta.get(seqnum)

    def _claim_audio_urb_completion(self, seqnum):
        self._forget_audio_urb(seqnum)
        return self._claim_urb_completion(seqnum)

    def _note_tx_audio_enqueued(self, stream_key):
        if stream_key not in ("out", "in"):
            return
        with self._tx_audio_count_lock:
            self._tx_audio_counts[stream_key] = self._tx_audio_counts.get(stream_key, 0) + 1

    def _note_tx_audio_removed(self, stream_key, stale=False):
        if stream_key not in ("out", "in"):
            return
        with self._tx_audio_count_lock:
            self._tx_audio_counts[stream_key] = max(0, self._tx_audio_counts.get(stream_key, 0) - 1)
            if stale:
                self.tx_audio_stale_dropped[stream_key] = self.tx_audio_stale_dropped.get(stream_key, 0) + 1

    def _note_tx_audio_stale_dropped(self, stream_key):
        if stream_key not in ("out", "in"):
            return
        with self._tx_audio_count_lock:
            self.tx_audio_stale_dropped[stream_key] = self.tx_audio_stale_dropped.get(stream_key, 0) + 1

    def _tx_audio_count(self, stream_key):
        with self._tx_audio_count_lock:
            return self._tx_audio_counts.get(stream_key, 0)

    def _drop_tx_audio_stream_items(self, stream_key, complete=False):
        """Pull this stream's queued replies out of the TX queue on a stream reset.

        complete=True (CLEAR_FEATURE / stream restart while the socket is alive): send
        each RET_SUBMIT so the host's URBs are completed instead of silently dropped
        (a dropped-but-unanswered URB hangs the vhci -> device teardown).  complete=False
        (socket dead/replaced): just claim+drop — there is no live peer to answer, and
        sending on the dead socket would only re-enter _mark_socket_dead.
        Sending honors socket-dead and UNLINK.
        """
        pulled = []
        with self.tx_queue.mutex:
            kept = []
            for item in self.tx_queue.queue:
                if len(item) >= 8:
                    _priority, _tx_seq, sock, payload, seqnum, item_stream_key, _generation, claim_audio = item
                    if payload is not None and item_stream_key == stream_key:
                        pulled.append((sock, payload, seqnum, claim_audio))
                        continue
                kept.append(item)
            self.tx_queue.queue[:] = kept
            heapq.heapify(self.tx_queue.queue)
        for _ in range(len(pulled)):
            self._note_tx_audio_removed(stream_key, stale=not complete)
        answered = 0
        for sock, payload, seqnum, claim_audio in pulled:
            if complete:
                try:
                    with self.send_lock:
                        if self._socket_is_dead(sock):
                            if claim_audio and seqnum is not None:
                                self._claim_audio_urb_completion(seqnum)
                            continue
                        if claim_audio and seqnum is not None and not self._claim_audio_urb_completion(seqnum):
                            continue
                        sock.sendall(payload)
                        answered += 1
                    continue
                except Exception as e:
                    if is_socket_disconnect(e):
                        self._mark_socket_dead(sock)
                        continue
                    logger.debug("Fast-complete of queued TX item failed", exc_info=True)
                    continue
            if claim_audio and seqnum is not None:
                try:
                    self._claim_audio_urb_completion(seqnum)
                except Exception:
                    pass
        if pulled:
            if complete:
                logger.info("Fast-completed %d queued audio %s TX replies on stream reset", answered, stream_key.upper())
            else:
                logger.info("Dropped %d stale audio %s TX completions", len(pulled), stream_key.upper())
        return len(pulled)

    def _on_urb_unlink(self, seqnum, was_pending):
        meta = self._forget_audio_urb(seqnum)
        if not meta:
            return
        stream_key, endpoint, queued_at = meta
        now_time = time.perf_counter()
        log_key = f"last_iso_{stream_key}_unlink_log"
        if now_time - getattr(self, log_key, 0) > 1.0:
            setattr(self, log_key, now_time)
            logger.info(
                "Host UNLINK audio %s URB: seq=%d endpoint=0x%02x pending=%s age_ms=%.1f",
                stream_key.upper(),
                seqnum,
                endpoint,
                was_pending,
                (now_time - queued_at) * 1000.0,
            )

    def start(self):
        super().start()
        self.iso_out_thread = threading.Thread(target=self._iso_out_loop, daemon=True)
        self.iso_in_thread = threading.Thread(target=self._iso_in_loop, daemon=True)
        self.audio_out_thread = threading.Thread(target=self._audio_out_loop, daemon=True)
        self.rumble_thread = threading.Thread(target=self._rumble_worker, daemon=True)
        self.output_report_thread = threading.Thread(target=self._output_report_worker, daemon=True)
        self.tx_thread = threading.Thread(target=self._tx_writer_loop, daemon=True)
        self.iso_out_thread.start()
        self.iso_in_thread.start()
        self.audio_out_thread.start()
        self.rumble_thread.start()
        self.output_report_thread.start()
        self.tx_thread.start()

    def _data_phase_loop(self, sock):
        _set_thread_priority(2)
        super()._data_phase_loop(sock)

    def _in_urb_loop(self):
        """Override the base _in_urb_loop to use precise waiting for HID IN pacing.
        This prevents blocking time.sleep() from causing 15ms+ delay stacking under load,
        which drops the controller's effective polling rate and causes input lag/stuttering.
        """
        _set_thread_priority(1)  # Priority 1 (Above Normal) so it doesn't starve Audio OUT (Priority 2)
        next_due = time.perf_counter()
        while self.running:
            try:
                urb = self.pending_in_urbs.get()
                if urb is None:
                    break
                sock, seqnum, devid, direction, ep = urb
                if not self._claim_urb_completion(seqnum):
                    continue  # UNLINKed before we got to it

                now = time.perf_counter()
                
                # Catch up logic: if we are late by more than 5ms, reset grid
                if next_due < now - 0.005:
                    next_due = now
                
                wait = next_due - now
                if wait > 0:
                    self._audio_out_precise_wait(wait)
                
                self._process_deferred_in_urb(sock, seqnum, devid, direction, ep)
                
                next_due += 0.001
            except Exception as e:
                if self.running:
                    logger.debug(f"DualSense IN URB worker error: {e}")

    class _IsoPacer:
        """Complete isochronous URBs at the nominal USB cadence (1 ms per ISO packet).

        A real DualSense consumes/produces exactly one ISO packet per USB frame, and
        usbaudio.sys derives the endpoint's stream clock from URB *completion timing*.
        DS5_Bridge/TinyUSB let the USB frame scheduler clock the endpoint; in USBIP we
        have to reproduce that cadence by completing RET_SUBMIT at absolute deadlines.
        """

        LEAD = 0.0010
        RESYNC_GAP = 0.5
        MAX_BEHIND = 0.020
        MIN_DURATION = 0.001

        def __init__(self):
            self.deadline = 0.0
            self.first_after_reset = True
            self.last_wait = 0.0
            self.last_late = 0.0
            self.last_duration = 0.0
            self.last_was_first = False
            self.last_was_reset = False

        def reset(self, immediate_first=True):
            self.deadline = 0.0
            self.first_after_reset = bool(immediate_first)
            self.last_wait = 0.0
            self.last_late = 0.0
            self.last_duration = 0.0
            self.last_was_first = False
            self.last_was_reset = True

        def pace_duration(self, duration):
            duration = max(self.MIN_DURATION, float(duration))
            now = time.perf_counter()
            first = self.first_after_reset or self.deadline == 0.0
            reset_clock = first or now - self.deadline > self.RESYNC_GAP

            if reset_clock:
                self.deadline = now
            elif now - self.deadline > self.MAX_BEHIND:
                self.deadline = now

            target = self.deadline if first else self.deadline - self.LEAD
            wait = target - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
                self.last_wait = wait
                self.last_late = 0.0
            else:
                self.last_wait = 0.0
                self.last_late = -wait

            self.deadline += duration
            self.last_duration = duration
            self.last_was_first = first
            self.last_was_reset = reset_clock
            self.first_after_reset = False

        def pace(self, data_length, rate_bytes_per_sec):
            duration = data_length / float(rate_bytes_per_sec) if rate_bytes_per_sec > 0 else self.MIN_DURATION
            self.pace_duration(duration)

    def _iso_out_loop(self):
        """喇叭/觸覺串流：USBIP completion 預設快速完成，haptic 另行節流。"""
        _set_thread_priority(15)
        while getattr(self, 'running', False):
            try:
                urb = self.pending_iso_out_urbs.get()
                if urb is None:
                    break
                if len(urb) == 10:
                    sock, seqnum, devid, direction, ep, transfer_length, out_data, start_frame, num_packets, iso_descriptors = urb
                    generation = self._current_stream_generation("out")
                    duration = self._audio_out_duration(out_data, num_packets)
                    scheduled_at = time.perf_counter()
                elif len(urb) == 11:
                    sock, seqnum, devid, direction, ep, transfer_length, out_data, start_frame, num_packets, iso_descriptors, generation = urb
                    duration = self._audio_out_duration(out_data, num_packets)
                    scheduled_at = time.perf_counter()
                else:
                    sock, seqnum, devid, direction, ep, transfer_length, out_data, start_frame, num_packets, iso_descriptors, generation, duration, scheduled_at = urb
                if self._socket_is_dead(sock):
                    self._claim_audio_urb_completion(seqnum)
                    continue
                if not self._stream_generation_is_current("out", generation):
                    # Stale (stream was reset while this URB queued): skip pacing and
                    # haptic work, but still COMPLETE it — never drop a live URB.
                    self._send_iso_reply(sock, seqnum, devid, direction, ep, transfer_length, out_data, start_frame, num_packets, iso_descriptors, "out", None)
                    continue

                now_before_wait = time.perf_counter()
                wait = 0.0
                first = False
                reset = False
                due_time = now_before_wait
                latency_cap_hit = False
                target_source = "immediate"
                frame_phase_error = 0.0
                frame_clock_valid = False
                if EP1_OUT_PACING_ENABLED:
                    submit_due = scheduled_at + duration
                    warmup_ceiling_due = scheduled_at + duration + AUDIO_OUT_WARMUP_LATENCY_SLACK_SEC
                    # Runaway safety only — the accumulated clock (below) is primary.
                    runaway_due = scheduled_at + AUDIO_OUT_RUNAWAY_CAP_SEC
                    with self._audio_out_schedule_lock:
                        submit_interval = self._audio_out_last_submit_interval
                        if submit_interval > 0:
                            if self._audio_out_submit_interval_ema == 0.0:
                                self._audio_out_submit_interval_ema = submit_interval
                                self._audio_out_submit_jitter_ema = 0.0
                            else:
                                delta = abs(submit_interval - self._audio_out_submit_interval_ema)
                                self._audio_out_submit_interval_ema = (
                                    self._audio_out_submit_interval_ema * 0.875 + submit_interval * 0.125
                                )
                                self._audio_out_submit_jitter_ema = (
                                    self._audio_out_submit_jitter_ema * 0.875 + delta * 0.125
                                )
                            stable_interval = (
                                abs(self._audio_out_submit_interval_ema - duration) <= AUDIO_OUT_LOCK_INTERVAL_TOLERANCE_SEC
                                and self._audio_out_submit_jitter_ema <= AUDIO_OUT_LOCK_JITTER_SEC
                            )
                            if stable_interval:
                                self._audio_out_stable_submit_count += 1
                            else:
                                self._audio_out_stable_submit_count = 0
                                self._audio_out_clock_locked = False
                            if self._audio_out_stable_submit_count >= AUDIO_OUT_LOCK_STABLE_URBS:
                                self._audio_out_clock_locked = True
                        if self._audio_out_warmup_remaining > 0:
                            self._audio_out_warmup_remaining -= 1
                            self._audio_out_warmup_count += 1

                        idle = (
                            self._audio_out_next_due == 0.0
                            or now_before_wait - self._audio_out_next_due > AUDIO_OUT_IDLE_RESET_SEC
                        )
                        first = (not self._audio_out_clock_active) or idle
                        reset = idle and self._audio_out_clock_active
                        frame_clock_valid = num_packets > 0 and start_frame not in (0, 0xffffffff)
                        if frame_clock_valid:
                            if not self._audio_out_frame_clock_valid:
                                self._audio_out_frame_clock_valid = True
                                self._audio_out_frame_clock_base_frame = start_frame
                                self._audio_out_frame_clock_base_time = scheduled_at
                            frame_delta = (start_frame - self._audio_out_frame_clock_base_frame) & 0xffffffff
                            if frame_delta > 0x7fffffff:
                                self._audio_out_frame_clock_valid = False
                                frame_clock_valid = False
                            else:
                                frame_due = self._audio_out_frame_clock_base_time + ((frame_delta + num_packets) * 0.001)
                                frame_phase_error = frame_due - submit_due
                        if first:
                            due_time = submit_due
                            self._audio_out_clock_active = True
                            target_source = "submit_first"
                            if reset:
                                self._audio_out_clock_resyncs += 1
                        else:
                            clock_due = self._audio_out_next_due
                            if clock_due < submit_due:
                                # We fell behind real time -> floor at this URB's own
                                # duration so we never complete earlier than its data.
                                due_time = submit_due
                                reset = True
                                target_source = "submit_floor"
                                self._audio_out_clock_resyncs += 1
                            elif clock_due > runaway_due:
                                # Pathological: the accumulated clock raced >4x duration
                                # ahead of real time (huge burst).  Complete at the cap
                                # and resync so latency stays bounded.  Not a normal path.
                                due_time = runaway_due
                                reset = True
                                latency_cap_hit = True
                                target_source = "runaway_cap"
                                self._audio_out_latency_cap_hits += 1
                                self._audio_out_clock_resyncs += 1
                            else:
                                # Normal path: hold to the accumulated audio-rate clock so
                                # bursty vhci delivery is smoothed into steady completions.
                                due_time = clock_due
                                target_source = "clock"
                        warmup_active = self._audio_out_warmup_remaining > 0 or not self._audio_out_clock_locked
                        hard_ceiling_due = warmup_ceiling_due if warmup_active else runaway_due
                        if due_time > hard_ceiling_due:
                            due_time = hard_ceiling_due
                            reset = True
                            latency_cap_hit = True
                            if warmup_active and self._audio_out_warmup_hold:
                                target_source = "warmup_hold_ceiling"
                            else:
                                target_source = "warmup_ceiling" if warmup_active else "runaway_ceiling"
                            self._audio_out_latency_ceiling_hits += 1
                            self._audio_out_clock_resyncs += 1
                        grid_due = due_time
                        self._audio_out_next_due = grid_due + duration
                        if self._audio_out_last_complete_time:
                            gap_sec = AUDIO_OUT_CATCHUP_GAP_SEC if grid_due < now_before_wait else AUDIO_OUT_MIN_COMPLETE_GAP_SEC
                            min_gap_due = self._audio_out_last_complete_time + gap_sec
                            if due_time < min_gap_due and min_gap_due <= hard_ceiling_due:
                                due_time = min_gap_due
                                target_source += "+gap"
                        self._audio_out_last_target_latency = max(0.0, due_time - scheduled_at)
                        self._audio_out_last_latency_cap_hit = latency_cap_hit
                        self._audio_out_last_start_frame = start_frame
                        self._audio_out_last_seqnum = seqnum
                        self._audio_out_frame_clock_valid = frame_clock_valid
                        self._audio_out_last_frame_phase_error = frame_phase_error
                        self._audio_out_last_target_source = target_source
                    wait = due_time - time.perf_counter()
                    if wait > 0:
                        self._audio_out_precise_wait(wait)
                    after_wait = time.perf_counter()
                    late = max(0.0, after_wait - due_time)
                    if late > duration:
                        missed = min(AUDIO_OUT_MAX_MISSED_TICKS, int(late // max(duration, 0.001)))
                        if missed > 0:
                            with self._audio_out_schedule_lock:
                                self._audio_out_missed_tick_skips += missed
                    self._audio_out_last_pacer_wait = max(0.0, wait)
                    self._audio_out_last_pacer_late = late
                    self._audio_out_last_worker_wakeup_late = late
                    self._audio_out_last_due_delay = max(0.0, due_time - scheduled_at)
                else:
                    self._audio_out_last_pacer_wait = 0.0
                    self._audio_out_last_pacer_late = 0.0
                    self._audio_out_last_worker_wakeup_late = 0.0
                    self._audio_out_last_due_delay = 0.0
                    self._audio_out_last_target_latency = 0.0
                    self._audio_out_last_latency_cap_hit = False
                    self._audio_out_last_start_frame = start_frame
                    self._audio_out_last_seqnum = seqnum
                    self._audio_out_frame_clock_valid = False
                    self._audio_out_last_frame_phase_error = 0.0
                    self._audio_out_last_target_source = target_source
                self._audio_out_last_pacer_duration = duration
                self._audio_out_last_pacer_first = first
                self._audio_out_last_pacer_reset = reset

                # Diagnostic only: a persistently deep queue means the pacer is behind.
                # We do NOT burst-complete to drain it — bursting past the nominal cadence
                # makes usbaudio.sys's stream clock look too fast and provokes a
                # CLEAR_FEATURE(ENDPOINT_HALT) loop.  The pacer's bounded catch-up
                # (MAX_BEHIND) drains small lags on its own; this only surfaces the depth.
                backlog_depth = self.pending_iso_out_urbs.qsize()
                if backlog_depth >= ISO_BACKLOG_RELIEF_DEPTH:
                    self.iso_out_backlog_relief_count += 1
                    now_time = time.perf_counter()
                    if now_time - getattr(self, 'last_iso_out_backlog_log', 0) > ISO_BACKLOG_LOG_INTERVAL:
                        self.last_iso_out_backlog_log = now_time
                        logger.warning(
                            "Audio OUT ISO queue deep: queue=%d tx_audio=%d observed=%d",
                            backlog_depth,
                            self._tx_audio_count("out"),
                            self.iso_out_backlog_relief_count,
                        )

                # Hand fresh audio to the haptic/diagnostic worker only while the USBIP
                # transport is caught up.  Under backlog pressure the endpoint clock has
                # priority; the next fresh packet will refresh haptics once the queue is
                # back under control.
                if len(out_data) > 0 and backlog_depth < ISO_BACKLOG_RELIEF_DEPTH:
                    self._queue_audio_out_packet(out_data)
                elif len(out_data) > 0:
                    self.skipped_haptic_packets += 1

                self._send_iso_reply(sock, seqnum, devid, direction, ep, transfer_length, out_data, start_frame, num_packets, iso_descriptors, "out", generation)
            except Exception as e:
                if is_socket_disconnect(e):
                    logger.info(f"ISO OUT socket disconnected: {e}")
                else:
                    logger.debug(f"ISO OUT error: {e}")

    def _iso_in_loop(self):
        """麥克風串流：同樣以 1ms/packet 節奏完成，避免 capture clock 亂飄。"""
        _set_thread_priority(15)
        while getattr(self, 'running', False):
            try:
                urb = self.pending_iso_in_urbs.get()
                if urb is None: break
                if len(urb) == 10:
                    sock, seqnum, devid, direction, ep, transfer_length, out_data, start_frame, num_packets, iso_descriptors = urb
                    generation = self._current_stream_generation("in")
                else:
                    sock, seqnum, devid, direction, ep, transfer_length, out_data, start_frame, num_packets, iso_descriptors, generation = urb
                if self._socket_is_dead(sock):
                    self._claim_audio_urb_completion(seqnum)
                    continue
                if not self._stream_generation_is_current("in", generation):
                    # Stale mic URB: still complete it (silence) instead of dropping.
                    self._send_iso_reply(sock, seqnum, devid, direction, ep, transfer_length, out_data, start_frame, num_packets, iso_descriptors, "in", None)
                    continue

                backlog_depth = self.pending_iso_in_urbs.qsize()
                if backlog_depth >= 16:
                    # Windows normally keeps several capture URBs queued. Treat this
                    # as diagnostic pressure only; skipping the pacer makes the mic
                    # clock run too fast and provokes EP 0x82 resets.
                    now_time = time.perf_counter()
                    if now_time - getattr(self, 'last_iso_in_backlog_log', 0) > ISO_BACKLOG_LOG_INTERVAL:
                        self.last_iso_in_backlog_log = now_time
                        logger.info("Audio IN ISO queue depth high: queue=%d paced", backlog_depth)
                # 1ch * 16-bit * 48kHz = 96000 B/s. Pace by the compact PCM
                # payload we actually return, not the 98-byte max-packet slots.
                paced_length = self._mic_iso_actual_total(num_packets, iso_descriptors, transfer_length)
                self._get_stream_pacer("in").pace(paced_length, 96000)
                self._wait_stream_reset_barrier("in")
                self._send_iso_reply(sock, seqnum, devid, direction, ep, transfer_length, b"", start_frame, num_packets, iso_descriptors, "in", generation)
            except Exception as e:
                if is_socket_disconnect(e):
                    logger.info(f"ISO IN socket disconnected: {e}")
                else:
                    logger.debug(f"ISO IN error: {e}")

    def _audio_out_loop(self):
        """Process latest EP1 audio/haptics off the USBIP submit path.

        This is intentionally latest-only.  USB audio URB completion still uses
        the original ISO queue/pacer, while haptic translation never builds a
        backlog that can steal CPU/GIL from USBIP.
        """
        next_process_time = 0.0
        while getattr(self, 'running', False):
            if not self._audio_out_event.wait(timeout=1.0):
                continue
            now_time = time.perf_counter()
            if now_time < next_process_time:
                time.sleep(next_process_time - now_time)

            with self._audio_out_lock:
                out_data = self._latest_audio_out_packet
                self._latest_audio_out_packet = None
                self._audio_out_event.clear()
                should_stop = self._audio_out_stop and out_data is None

            if should_stop:
                return
            if out_data is None:
                continue
            try:
                self._process_audio_out_packet(out_data)
            except Exception:
                logger.debug("Audio OUT worker failed", exc_info=True)
            next_process_time = time.perf_counter() + HAPTIC_PROCESS_INTERVAL
            time.sleep(0)

    def _queue_audio_out_packet(self, out_data):
        now_time = time.perf_counter()
        if now_time < self._next_haptic_capture_time:
            self.skipped_haptic_packets += 1
            return

        replaced_stale = False
        if not self._audio_out_lock.acquire(False):
            self.skipped_haptic_packets += 1
            return
        try:
            self._next_haptic_capture_time = now_time + HAPTIC_PROCESS_INTERVAL
            if self._latest_audio_out_packet is not None:
                # Drop stale haptic-processing data, not USB audio URBs.  The audio
                # endpoint keeps its timing; only downstream rumble translation is
                # latest-only under load.
                self.dropped_audio_packets += 1
                replaced_stale = True
            self._latest_audio_out_packet = out_data
            self._audio_out_event.set()
        finally:
            self._audio_out_lock.release()

        if replaced_stale:
            if now_time - getattr(self, 'last_audio_drop_log', 0) > 5.0:
                previous_count = getattr(self, 'last_audio_drop_log_count', 0)
                replaced_delta = self.dropped_audio_packets - previous_count
                self.last_audio_drop_log_count = self.dropped_audio_packets
                previous_skipped = getattr(self, 'last_haptic_skip_log_count', 0)
                skipped_delta = self.skipped_haptic_packets - previous_skipped
                self.last_haptic_skip_log_count = self.skipped_haptic_packets
                self.last_audio_drop_log = now_time
                logger.info(
                    "Audio OUT haptic latest-only active: skipped=%d replaced=%d total_replaced=%d",
                    skipped_delta,
                    replaced_delta,
                    self.dropped_audio_packets,
                )

    def _audio_out_pending_count(self):
        try:
            with self._audio_out_lock:
                return 1 if self._latest_audio_out_packet is not None else 0
        except Exception:
            return -1

    @staticmethod
    def _audio_channel_stats(channel):
        if channel.size == 0:
            # Return a literal string to avoid allocating np.zeros() on the empty-data path.
            return "peak=0 rms=0.0 mean=0.0 mean_abs=0.0 min=0 max=0 nz=0/0"
        ch = channel.astype(np.int64)
        abs_ch = np.abs(ch)
        peak = int(abs_ch.max())
        rms = float(np.sqrt(np.mean(ch.astype(np.float64) * ch.astype(np.float64))))
        mean = float(ch.mean())
        mean_abs = float(abs_ch.mean())
        nonzero = int(np.count_nonzero(ch))
        return (
            f"peak={peak} rms={rms:.1f} mean={mean:.1f} "
            f"mean_abs={mean_abs:.1f} min={int(ch.min())} max={int(ch.max())} "
            f"nz={nonzero}/{ch.size}"
        )

    def _process_audio_out_packet(self, out_data):
        now_time = time.perf_counter()
        if _PERF_DIAGNOSTICS and now_time - getattr(self, 'last_audio_log', 0) > DIAGNOSTIC_LOG_INTERVAL:
            self.last_audio_log = now_time
            try:
                usable_length = (len(out_data) // 8) * 8
                trailing_bytes = len(out_data) - usable_length
                a = np.frombuffer(out_data[:usable_length], dtype='<i2').reshape(-1, 4)
                frames = int(a.shape[0])
                aud_peak = int(np.abs(a[:, :2]).max()) if a.size else 0
                hap = a[:, 2:4] if a.size else np.zeros((0, 2), dtype=np.int16)
                hap_abs = np.abs(hap.astype(np.int64)) if hap.size else np.zeros((0, 2), dtype=np.int64)
                hap_peak = int(hap_abs.max()) if hap.size else 0
                hap_rms = float(np.sqrt(np.mean(hap.astype(np.float64) * hap.astype(np.float64)))) if hap.size else 0.0
                hap_mean_abs = float(hap_abs.mean()) if hap.size else 0.0
                hap_nonzero = int(np.count_nonzero(hap)) if hap.size else 0
                if hap.size:
                    flat_max = int(np.argmax(hap_abs))
                    max_frame = flat_max // 2
                    max_channel = 2 + (flat_max % 2)
                else:
                    max_frame = -1
                    max_channel = -1
                sample_count = min(12, frames)
                haptic_samples = [
                    (int(a[i, 2]), int(a[i, 3]))
                    for i in range(sample_count)
                ]
                logger.info(
                    "Audio OUT ep1 raw: bytes=%d usable=%d trailing=%d frames=%d "
                    "audio_active=%s dropped=%d queue=%d | ch0 %s | ch1 %s | ch2 %s | ch3 %s",
                    len(out_data), usable_length, trailing_bytes, frames,
                    getattr(self, 'audio_active', False),
                    self.dropped_audio_packets,
                    self._audio_out_pending_count(),
                    self._audio_channel_stats(a[:, 0]) if a.size else self._audio_channel_stats(np.zeros(0, dtype=np.int16)),
                    self._audio_channel_stats(a[:, 1]) if a.size else self._audio_channel_stats(np.zeros(0, dtype=np.int16)),
                    self._audio_channel_stats(a[:, 2]) if a.size else self._audio_channel_stats(np.zeros(0, dtype=np.int16)),
                    self._audio_channel_stats(a[:, 3]) if a.size else self._audio_channel_stats(np.zeros(0, dtype=np.int16)),
                )
                logger.info(
                    "Audio OUT ep1 haptic raw detail: ch2/3 peak=%d rms=%.1f mean_abs=%.1f "
                    "nz=%d/%d max_at=frame%d/ch%d first_pairs=%s",
                    hap_peak, hap_rms, hap_mean_abs,
                    hap_nonzero, int(hap.size), max_frame, max_channel, haptic_samples,
                )
                if getattr(self, 'audio_active', False) and aud_peak > 0 and hap_peak == 0:
                    logger.info(
                        "Audio OUT diagnostic: Windows shared audio is delivering game audio but "
                        "no haptic channels; check the game's audio-haptics routing or "
                        "exclusive/direct access to the audio interface."
                    )
            except Exception:
                logger.debug("ep1 audio diagnostic failed", exc_info=True)
        if getattr(self, 'on_audio_data_callback', None):
            self.on_audio_data_callback(out_data)

    def _read_mic_samples(self, nbytes):
        """Return `nbytes` of mic PCM for an ISO IN reply.

        The Switch 2 controller has no physical microphone, so the DualSense mic
        endpoint always streams silence.  We deliberately do NOT capture the host
        mic: that opened a second capture stream on our own virtual endpoint and made
        usbaudio.sys halt EP 0x82 and abort the connection.
        """
        return b"\x00" * nbytes

    @staticmethod
    def _mic_iso_packet_actual_length(requested_length):
        return max(0, min(int(requested_length), MIC_ISO_BYTES_PER_FRAME))

    def _mic_iso_actual_total(self, num_packets, iso_descriptors, transfer_length):
        if num_packets > 0 and len(iso_descriptors) == num_packets * 16:
            total = 0
            for i in range(num_packets):
                _offset, length, _act_len, _pkt_status = struct.unpack("!IIII", iso_descriptors[i*16:(i+1)*16])
                total += self._mic_iso_packet_actual_length(length)
            return total
        if num_packets > 0:
            return min(max(0, transfer_length), MIC_ISO_BYTES_PER_FRAME * num_packets)
        return max(0, min(transfer_length, MIC_ISO_BYTES_PER_FRAME))

    @staticmethod
    def _uac_entity_index(entity_id):
        if entity_id == 0x02:  # Speaker Feature Unit
            return 0
        if entity_id == 0x05:  # Mic Feature Unit
            return 1
        return 2

    @staticmethod
    def _uac_volume_range(index):
        # DS5_Bridge ranges: speaker = -100 dB..0 dB, mic/line = 0..48 dB.
        if index == 0:
            return (-100 * 256, 0, 1 * 256)
        return (0, 48 * 256, 0x007A)

    def _handle_uac1_feature_unit_request(self, req_type, request, value, index, length, out_data):
        ctrl_sel = (value >> 8) & 0xFF
        entity_id = (index >> 8) & 0xFF
        channel = value & 0xFF
        audio_index = self._uac_entity_index(entity_id)

        if entity_id not in (0x02, 0x05):
            if req_type & 0x80:
                return b"\x00" * length
            return b""

        # UAC1 Feature Unit controls: 0x01 mute, 0x02 volume.
        if ctrl_sel == 0x01:
            if req_type & 0x80:
                return bytes([self._audio_host_mute[audio_index] & 0x01])[:length]
            if out_data:
                self._audio_host_mute[audio_index] = 1 if out_data[0] else 0
            return b""

        if ctrl_sel == 0x02:
            min_units, max_units, res_units = self._uac_volume_range(audio_index)
            if req_type & 0x80:
                if request == 0x82:  # GET_MIN
                    units = min_units
                elif request == 0x83:  # GET_MAX
                    units = max_units
                elif request == 0x84:  # GET_RES
                    units = res_units
                else:  # GET_CUR
                    units = self._audio_host_volume_units[audio_index]
                return struct.pack("<h", int(units))[:length]
            if request == 0x01 and len(out_data) >= 2:  # SET_CUR
                units = struct.unpack_from("<h", out_data, 0)[0]
                self._audio_host_volume_units[audio_index] = max(min_units, min(max_units, units))
                logger.info(
                    "UAC volume set: entity=0x%02x channel=%d units=%d",
                    entity_id,
                    channel,
                    self._audio_host_volume_units[audio_index],
                )
            return b""

        if req_type & 0x80:
            return b"\x00" * length
        return b""


    def stop(self):
        try:
            with self._audio_out_lock:
                self._audio_out_stop = True
                self._latest_audio_out_packet = None
                self._audio_out_event.set()
        except Exception:
            pass
        # Wake the paced ISO and output-report workers so they exit promptly.
        for q in (self.pending_iso_out_urbs, self.pending_iso_in_urbs, self.output_report_queue):
            try:
                q.put_nowait(None)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
            except Exception:
                pass
        try:
            self._enqueue_tx(None, None, TX_PRIORITY_OTHER)
        except Exception:
            pass
        super().stop()

    def _enqueue_tx(self, sock, payload, priority=TX_PRIORITY_OTHER,
                    seqnum=None, stream_key=None, generation=None, claim_audio=False):
        with self._tx_seq_lock:
            self._tx_seq += 1
            seq = self._tx_seq
        self._note_tx_audio_enqueued(stream_key)
        self.tx_queue.put((
            priority, seq, sock, payload, seqnum, stream_key, generation, claim_audio
        ))

    def _tx_writer_loop(self):
        _set_thread_priority(2)
        while getattr(self, 'running', False):
            try:
                item = self.tx_queue.get(timeout=1.0)
                if len(item) == 4:
                    priority, _seq, sock, payload = item
                    seqnum = None
                    stream_key = None
                    generation = None
                    claim_audio = False
                else:
                    priority, _seq, sock, payload, seqnum, stream_key, generation, claim_audio = item
                if payload is None:
                    break
                if stream_key in ("out", "in"):
                    self._note_tx_audio_removed(stream_key)
                if sock is None or self._socket_is_dead(sock):
                    if claim_audio and seqnum is not None:
                        self._claim_audio_urb_completion(seqnum)
                    continue
                # A stale generation must NOT drop the reply (USBIP requires every URB be
                # completed unless UNLINKed); always send, gated only by socket-dead and
                # UNLINK.  generation now only gates upstream pacing/haptic work.
                started = time.perf_counter()
                try:
                    with self.send_lock:
                        if self._socket_is_dead(sock):
                            if claim_audio and seqnum is not None:
                                self._claim_audio_urb_completion(seqnum)
                            continue
                        audio_meta = self._peek_audio_urb(seqnum) if claim_audio and seqnum is not None else None
                        if claim_audio and seqnum is not None and not self._claim_audio_urb_completion(seqnum):
                            continue
                        if not self._socket_is_dead(sock):
                            sock.sendall(payload)
                            if stream_key == "out" and seqnum is not None:
                                now_done = time.perf_counter()
                                self._audio_out_last_complete_time = now_done
                                self._audio_out_last_complete_seq = seqnum
                                if audio_meta:
                                    self._audio_out_last_complete_latency = now_done - audio_meta[2]
                except Exception as e:
                    if is_socket_disconnect(e):
                        self._mark_socket_dead(sock)
                        logger.info(f"DualSense TX socket disconnected: {e}")
                        continue
                    raise
                elapsed = time.perf_counter() - started
                if _PERF_DIAGNOSTICS and elapsed > 0.002:
                    now_time = time.perf_counter()
                    if now_time - getattr(self, 'last_tx_slow_log', 0) > 1.0:
                        self.last_tx_slow_log = now_time
                        logger.info(
                            "DualSense TX slow send: %.2fms priority=%d queue=%d",
                            elapsed * 1000.0,
                            priority,
                            self.tx_queue.qsize(),
                        )
            except queue.Empty:
                pass
            except Exception:
                logger.debug("DualSense TX writer failed", exc_info=True)

    def _update_haptics_gate_fast(self, out_data):
        if len(out_data) < 11 or out_data[0] != 0x02:
            return
        mute_control = out_data[10]
        blocked = bool(mute_control & 0x8c)
        previous = getattr(self, 'dualsense_haptics_blocked', False)
        self.dualsense_haptics_blocked = blocked
        if blocked != previous:
            now_time = time.perf_counter()
            if now_time - getattr(self, 'last_haptics_gate_fast_log', 0) > 0.25:
                self.last_haptics_gate_fast_log = now_time
                logger.info("DS5 Haptics gate fast: %s", "blocked" if blocked else "active")

    def _queue_output_report(self, out_data):
        payload = bytes(out_data)
        try:
            self.output_report_queue.put_nowait(payload)
        except queue.Full:
            self.output_report_dropped += 1
            try:
                self.output_report_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.output_report_queue.put_nowait(payload)
            except queue.Full:
                pass
            now_time = time.perf_counter()
            if now_time - getattr(self, 'last_output_report_backlog_log', 0) > OUTPUT_REPORT_BACKLOG_LOG_INTERVAL:
                self.last_output_report_backlog_log = now_time
                logger.warning(
                    "DualSense output report worker backlog: queue=%d dropped_oldest=%d",
                    self.output_report_queue.qsize(),
                    self.output_report_dropped,
                )

    def _output_report_worker(self):
        while getattr(self, 'running', False):
            try:
                payload = self.output_report_queue.get(timeout=1.0)
                if payload is None:
                    break
                started = time.perf_counter()
                self._process_output_report(payload)
                elapsed = time.perf_counter() - started
                if _PERF_DIAGNOSTICS and elapsed > 0.004:
                    now_time = time.perf_counter()
                    if now_time - getattr(self, 'last_output_report_slow_log', 0) > OUTPUT_REPORT_BACKLOG_LOG_INTERVAL:
                        self.last_output_report_slow_log = now_time
                        logger.info(
                            "DualSense output report worker slow: %.2fms queue=%d",
                            elapsed * 1000.0,
                            self.output_report_queue.qsize(),
                        )
            except queue.Empty:
                pass
            except Exception:
                logger.debug("DualSense output report worker failed", exc_info=True)

    def _send_iso_reply(self, sock, seqnum, devid, direction, ep, transfer_length, out_data, start_frame, num_packets, iso_descriptors, stream_key=None, generation=None):
        """共用的等時傳輸回覆封裝"""
        status = 0
        descriptor_offsets = []
        descriptor_lengths = []
        packet_actual_lengths = []
        reply_iso_descriptors = b""
        if num_packets > 0:
            if direction == 0:
                # usbip-win2 requires ret.actual_length to match the OUT URB transfer
                # length for isochronous OUT completions.  Per-packet actual_length is
                # not used by Windows for OUT data, but returning the packet length
                # matches the Linux usbip stub behavior and keeps the audio stack
                # stable during enumeration.
                if len(iso_descriptors) == num_packets * 16:
                    for i in range(num_packets):
                        offset, length, _act_len, _pkt_status = struct.unpack("!IIII", iso_descriptors[i*16:(i+1)*16])
                        descriptor_offsets.append(offset)
                        descriptor_lengths.append(length)
                        packet_actual_lengths.append(length)
                        reply_iso_descriptors += struct.pack("!IIII", offset, length, length, 0)
                else:
                    offset = 0
                    packet_len = transfer_length // num_packets if num_packets > 0 else 0
                    for _i in range(num_packets):
                        remaining = max(0, transfer_length - offset)
                        length = min(packet_len, remaining)
                        descriptor_offsets.append(offset)
                        descriptor_lengths.append(length)
                        packet_actual_lengths.append(length)
                        reply_iso_descriptors += struct.pack("!IIII", offset, length, length, 0)
                        offset += length
            elif len(iso_descriptors) == num_packets * 16:
                # ISO IN (mic): usbip-win2 keeps the original 98-byte request
                # offsets but consumes a compact payload whose size is the sum of
                # per-packet actual_length.  A 48 kHz mono 16-bit mic produces
                # 96 bytes per 1 ms frame, so report 96 actual bytes for each
                # 98-byte slot instead of advertising a faster-than-real clock.
                for i in range(num_packets):
                    offset, length, _act_len, _pkt_status = struct.unpack("!IIII", iso_descriptors[i*16:(i+1)*16])
                    actual = self._mic_iso_packet_actual_length(length)
                    descriptor_offsets.append(offset)
                    descriptor_lengths.append(length)
                    packet_actual_lengths.append(actual)
                    reply_iso_descriptors += struct.pack("!IIII", offset, length, actual, 0)
        if packet_actual_lengths:
            logged_actual_total = sum(packet_actual_lengths)
            log_key = 'last_iso_out_shape_log' if direction == 0 else 'last_iso_in_shape_log'
            now_time = time.perf_counter()
            if now_time - getattr(self, log_key, 0) > 5.0:
                setattr(self, log_key, now_time)
                logger.info(
                    "ISO %s shape: ep=%d transfer=%d packets=%d desc_offsets=%s desc_lengths=%s act_lengths=%s actual_total=%d",
                    "IN" if direction == 1 else "OUT",
                    ep,
                    transfer_length,
                    num_packets,
                    descriptor_offsets[:4],
                    descriptor_lengths[:4],
                    packet_actual_lengths[:4],
                    logged_actual_total,
                )
            # Key by the USB endpoint *address* (IN endpoints carry the 0x80 bit) so the
            # CLEAR_FEATURE handler, which looks up by `index & 0xff` (e.g. 0x82), finds
            # the mic's last ISO shape instead of always logging last_iso=None.
            ep_addr = (ep | 0x80) if direction == 1 else ep
            self._last_iso_shape_by_ep[ep_addr] = {
                "direction": "IN" if direction == 1 else "OUT",
                "transfer": transfer_length,
                "packets": num_packets,
                "desc_offsets": descriptor_offsets[:4],
                "desc_lengths": descriptor_lengths[:4],
                "act_lengths": packet_actual_lengths[:4],
                "actual_total": logged_actual_total,
            }
        actual_length = (sum(packet_actual_lengths) if direction == 1 and packet_actual_lengths
                         else transfer_length if direction == 1
                         else len(out_data))
        # For IN transfers (mic, EP 0x82) deliver real captured audio; falls back to
        # silence when the mic isn't active or capture is unavailable.
        reply_data = self._read_mic_samples(actual_length) if direction == 1 else b""
        
        ret_header = struct.pack(
            "!IIIII i IiiI 8s",
            USBIP_RET_SUBMIT, seqnum, devid, direction, ep, status, actual_length,
            start_frame, num_packets, 0, b"\x00" * 8
        )
        
        if stream_key is not None and not (stream_key == "out" and EP1_OUT_FAST_TX_ENABLED):
            self._wait_stream_reset_barrier(stream_key)

        if self._socket_is_dead(sock):
            self._claim_audio_urb_completion(seqnum)
            return
        # NOTE: a stale generation must NOT suppress the reply.  USBIP requires every
        # submitted URB to be completed (RET_SUBMIT) unless it was UNLINKed; silently
        # dropping a stale-but-live URB leaves it pending in the vhci until Windows times
        # out and tears the device down (the "switch app -> instant disconnect" bug).
        # Generation only gates pacing/haptic work upstream; here we always answer,
        # gated solely by socket-dead (above) and UNLINK (_claim_* below).

        payload = ret_header
        if direction == 1 and actual_length > 0:
            payload += reply_data
        if num_packets > 0:
            payload += reply_iso_descriptors
        if stream_key == "out" and EP1_OUT_FAST_TX_ENABLED:
            started = time.perf_counter()
            try:
                with self.send_lock:
                    if self._socket_is_dead(sock):
                        self._claim_audio_urb_completion(seqnum)
                        return
                    # Always answer a live URB (see note above); only UNLINK suppresses.
                    audio_meta = self._peek_audio_urb(seqnum)
                    if not self._claim_audio_urb_completion(seqnum):
                        return
                    sock.sendall(payload)
                    now_done = time.perf_counter()
                    if self._audio_out_last_complete_time:
                        self._audio_out_last_complete_interval = now_done - self._audio_out_last_complete_time
                    self._audio_out_last_complete_time = now_done
                    self._audio_out_last_complete_seq = seqnum
                    if audio_meta:
                        self._audio_out_last_complete_latency = now_done - audio_meta[2]
            except Exception as e:
                if is_socket_disconnect(e):
                    self._mark_socket_dead(sock)
                    logger.info(f"DualSense TX socket disconnected: {e}")
                    return
                raise
            elapsed = time.perf_counter() - started
            if _PERF_DIAGNOSTICS and elapsed > 0.002:
                now_time = time.perf_counter()
                if now_time - getattr(self, 'last_audio_out_fast_tx_slow_log', 0) > 1.0:
                    self.last_audio_out_fast_tx_slow_log = now_time
                    logger.info(
                        "Audio OUT fast TX slow send: %.2fms queue=%d",
                        elapsed * 1000.0,
                        self.pending_iso_out_urbs.qsize(),
                    )
            return
        if stream_key == "out":
            priority = TX_PRIORITY_AUDIO_ISO
        elif stream_key == "in":
            priority = TX_PRIORITY_MIC_ISO
        else:
            priority = TX_PRIORITY_OTHER
        self._enqueue_tx(
            sock, payload, priority,
            seqnum=seqnum, stream_key=stream_key,
            generation=generation, claim_audio=True
        )

    def update_input(self, report):
        """Update the 64-byte input report payload"""
        if isinstance(report, bytes):
            if len(report) == 64:
                ctypes.memmove(ctypes.addressof(self.last_state), report, 64)
        else:
            # Assuming it's a DualSenseInputReport01 object
            ctypes.memmove(ctypes.addressof(self.last_state), ctypes.addressof(report), 64)

    def _process_deferred_in_urb(self, sock, seqnum, devid, direction, ep):
        """DualSense HID input is on endpoint 4; the base Nintendo server only accepts EP1."""
        status = 0
        reply_data = b""

        if ep == 4:
            with self.lock:
                reply_data = bytes(self.last_state)
        else:
            status = -1

        actual_length = len(reply_data)
        ret_header = struct.pack(
            "!IIIII i IIII 8s",
            USBIP_RET_SUBMIT, seqnum, devid, direction, ep, status, actual_length,
            0, 0, 0, b"\x00" * 8
        )

        try:
            # Inline Fast TX: Bypass tx_queue entirely. 
            # _in_urb_loop is now precisely paced, so we can send directly.
            # We use send_lock to prevent garbling concurrent Audio OUT payloads.
            with self.send_lock:
                if not self._socket_is_dead(sock):
                    sock.sendall(ret_header + reply_data)
        except Exception:
            pass

    def _process_output_report(self, out_data):
        if len(out_data) >= 48 and out_data[0] == 0x02:
            report = DualSenseOutputReport02.from_buffer_copy(out_data[:48])
            mute_control = out_data[10] if len(out_data) > 10 else 0
            haptic_power_save = (mute_control & 0x04) != 0
            audio_power_save = (mute_control & 0x08) != 0
            haptic_mute = (mute_control & 0x80) != 0
            blocked = haptic_power_save or audio_power_save or haptic_mute
            if blocked != getattr(self, 'dualsense_haptics_blocked', False):
                logger.info(
                    "DS5 Haptics gate: %s (haptic_power_save=%d audio_power_save=%d haptic_mute=%d)",
                    "blocked" if blocked else "active",
                    int(haptic_power_save), int(audio_power_save), int(haptic_mute),
                )
            self.dualsense_haptics_blocked = blocked
            
            log_parts = []
            if report.AllowRightTriggerFFB:
                log_parts.append(f"RT: {out_data[11:22].hex()}")
            if report.AllowLeftTriggerFFB:
                log_parts.append(f"LT: {out_data[22:33].hex()}")
            if report.AllowLedColor:
                log_parts.append(f"RGB: ({report.LedRed},{report.LedGreen},{report.LedBlue})")
            if report.AllowHeadphoneVolume:
                log_parts.append(f"HPVol: {report.VolumeHeadphones}")
            if report.AllowAudioMute:
                log_parts.append(f"MicMute: {report.MicMute}")
                # Sync mic mute state back to input report
                self.last_state.MicMuted = report.MicMute
                
            if log_parts:
                now_time = time.perf_counter()
                if now_time - getattr(self, 'last_feature_log', 0) > 0.5:
                    logger.info("DS5 Output Features: " + " | ".join(log_parts))
                    self.last_feature_log = now_time

        if self.on_rumble_callback:
            self.on_rumble_callback(out_data)

    def _rumble_worker(self):
        while getattr(self, 'running', False):
            try:
                # 等待新的震動封包
                payload = self.rumble_queue.get(timeout=1.0)
                if self.on_rumble_callback:
                    self.on_rumble_callback(payload)
            except queue.Empty:
                pass
            except Exception as e:
                logger.debug(f"Rumble dispatch error: {e}")

    def _on_translated_rumble(self, left_intensity, right_intensity):
        """取代原本的 callback 邏輯，改為將封包放入非同步佇列"""
        payload = bytearray(3)
        payload[0] = 0x11
        payload[1] = right_intensity
        payload[2] = left_intensity
        
        # 使用 put_nowait，如果實體搖桿來不及消化，就直接丟棄該幀以保住影片播放
        try:
            self.rumble_queue.put_nowait(payload)
        except queue.Full:
            try:
                self.rumble_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.rumble_queue.put_nowait(payload)
            except queue.Full:
                pass

    def _get_device_desc(self):
        path = f"/sys/devices/virtual/usbip/{self.bus_id}".encode('ascii')
        busid_bytes = self.bus_id.encode('ascii')
        devnum = self.devnum
        
        return struct.pack(
            "!256s32sIIIHHHBBBBBB",
            path,
            busid_bytes,
            1,      # busnum
            devnum, # devnum
            2,      # speed = Full Speed (12Mbps), matching DS5_Bridge / DualSense audio timing
            0x054c, # idVendor = Sony
            0x0ce6, # idProduct = DualSense
            0x0100, # bcdDevice = 1.00
            0x00,   # bDeviceClass
            0x00,   # bDeviceSubClass
            0x00,   # bDeviceProtocol
            0x01,   # bConfigurationValue
            0x01,   # bNumConfigurations
            0x04 if self.enable_audio else 0x01 # bNumInterfaces (UAC1 Control, Streaming OUT, Streaming IN mic, HID) OR just HID
        )

    def _send_devlist(self, sock):
        reply_header = struct.pack("!HHI I", USBIP_VERSION, OP_REP_DEVLIST, 0, 1)
        dev_desc = self._get_device_desc()

        # 4 Interfaces (must match bNumInterfaces in the device descriptor above)
        iface0 = struct.pack("!BBBB", 0x01, 0x01, 0x00, 0x00) # Interface 0: Audio Control
        iface1 = struct.pack("!BBBB", 0x01, 0x02, 0x00, 0x00) # Interface 1: Audio Streaming OUT
        iface2 = struct.pack("!BBBB", 0x01, 0x02, 0x00, 0x00) # Interface 2: Audio Streaming IN (mic)
        iface3 = struct.pack("!BBBB", 0x03, 0x00, 0x00, 0x00) # Interface 3: HID
        
        if self.enable_audio:
            sock.sendall(reply_header + dev_desc + iface0 + iface1 + iface2 + iface3)
        else:
            # Only 1 Interface: HID (must match Interface 0 in NO_AUDIO descriptor)
            iface_hid_only = struct.pack("!BBBB", 0x03, 0x00, 0x00, 0x00)
            sock.sendall(reply_header + dev_desc + iface_hid_only)

    def _handle_submit(self, sock, seqnum, devid, direction, ep, transfer_length, setup, out_data, start_frame=0, num_packets=0, iso_descriptors=b""):
        status = 0
        actual_length = 0
        reply_data = b""
        
        if ep == 0: # Control
            reply_data = self._handle_control_request(setup, transfer_length, out_data)
            actual_length = len(reply_data)
            if direction == 0 and len(out_data) > 0:
                actual_length = len(out_data)
        elif ep == 4: # HID IN
            if direction == 1: # IN (Read input state)
                # Defer to background thread
                self._mark_urb_pending(seqnum)
                self.pending_in_urbs.put((sock, seqnum, devid, direction, ep))
                return
        elif ep == 3: # HID OUT
            if direction == 0: # OUT
                if len(out_data) > 0:
                    self._update_haptics_gate_fast(out_data)
                    self._queue_output_report(out_data)
                actual_length = len(out_data)
        elif ep == 1 and direction == 0: # Audio Streaming OUT (Haptic/Speaker)
            # Schedule EP1 OUT completion at submit time.  This mirrors a real USB ISO
            # pipeline more closely than sleeping only after the worker dequeues the URB.
            self._mark_urb_pending(seqnum)
            self._remember_audio_urb(seqnum, "out", 0x01)
            submit_now = time.perf_counter()
            if self._audio_out_last_submit_time:
                self._audio_out_last_submit_interval = submit_now - self._audio_out_last_submit_time
            self._audio_out_last_submit_time = submit_now
            self._audio_out_last_submit_seq = seqnum
            generation = self._current_stream_generation("out")
            duration = self._audio_out_duration(out_data, num_packets)
            scheduled_at = submit_now
            self._schedule_audio_out_urb(
                (sock, seqnum, devid, direction, ep, transfer_length, out_data,
                 start_frame, num_packets, iso_descriptors, generation, duration, scheduled_at),
                duration,
            )
            return
        elif ep == 2 and direction == 1: # Audio Streaming IN (Microphone)
            # Pace the mic IN on the background _iso_in_loop at the real 1ms/packet
            # cadence.  Completing IN URBs inline/instantly makes usbaudio.sys's capture
            # clock appear to run arbitrarily fast, so it resets EP 0x82 in a
            # CLEAR_FEATURE(ENDPOINT_HALT) loop until the connection aborts.
            self._mark_urb_pending(seqnum)
            self._remember_audio_urb(seqnum, "in", 0x82)
            generation = self._current_stream_generation("in")
            self.pending_iso_in_urbs.put(
                (sock, seqnum, devid, direction, ep, transfer_length, b"",
                 start_frame, num_packets, iso_descriptors, generation))
            return
        else:
            status = -1
            
        ret_header = struct.pack(
            "!IIIII i IiiI 8s",
            USBIP_RET_SUBMIT, seqnum, devid, direction, ep, status, actual_length,
            start_frame, num_packets, 0, b"\x00" * 8
        )
        
        reply_iso_descriptors = b""
        if num_packets > 0 and len(iso_descriptors) == num_packets * 16:
            # Reconstruct iso_descriptors with actual_length = length for OUT transfers
            for i in range(num_packets):
                chunk = iso_descriptors[i*16:(i+1)*16]
                offset, length, act_len, pkt_status = struct.unpack("!IIII", chunk)
                if direction == 0 or direction == 1: # OUT or IN
                    act_len = length
                reply_iso_descriptors += struct.pack("!IIII", offset, length, act_len, 0)
        
        payload = ret_header
        if direction == 1 and actual_length > 0:
            payload += reply_data
        # USBIP RET_SUBMIT payload layout is IN data first, then ISO descriptors.
        # OUT isochronous replies carry descriptors only.
        if num_packets > 0:
            payload += reply_iso_descriptors
        priority = TX_PRIORITY_CONTROL if ep == 0 else TX_PRIORITY_OTHER
        self._enqueue_tx(sock, payload, priority)

    def _handle_control_request(self, setup, transfer_length, out_data=b""):
        req_type, request, value, index, length = struct.unpack("<BBHHH", setup)
        # DEBUG, not INFO: this fires on the recv thread for every control request; at
        # INFO it added a console write per request to the recv thread's hot path.  The
        # CLEAR_FEATURE(ENDPOINT_HALT) WARNING below still surfaces the important events.
        logger.debug(f"Control Req: type={req_type:#04x}, req={request:#04x}, val={value:#06x}, idx={index:#06x}, len={length}")
        
        # Standard Device-to-Host Get Descriptor
        if req_type == 0x80 and request == 0x06:
            desc_type = value >> 8
            desc_idx = value & 0xff
            
            if desc_type == 0x01: # Device Descriptor
                return DUALSENSE_DEVICE_DESCRIPTOR[:length]
                
            elif desc_type == 0x02: # Configuration Descriptor
                if self.enable_audio:
                    return DUALSENSE_CONFIGURATION_DESCRIPTOR[:length]
                else:
                    return DUALSENSE_CONFIGURATION_DESCRIPTOR_NO_AUDIO[:length]
                
            elif desc_type == 0x03: # String Descriptor
                if desc_idx == 0:
                    return DUALSENSE_STRING_LANG[:length]
                elif desc_idx == 1:
                    return DUALSENSE_STRING_MANUFACTURER[:length]
                elif desc_idx == 2:
                    return DUALSENSE_STRING_PRODUCT[:length]
                elif desc_idx == 3:
                    # No serial (iSerialNumber = 0), matching a real DualSense — Windows
                    # never requests this index.  Return an empty string descriptor if a
                    # host asks anyway.  Multiple players stay distinct via their unique
                    # USBIP bus_id (port/location), the same way two real DualSense pads on
                    # two USB ports are told apart without relying on a serial string.
                    return bytes([0x02, 0x03])[:length]
                elif desc_idx == 4:
                    return DUALSENSE_STRING_AUDIO[:length]
                elif desc_idx == 5:
                    return DUALSENSE_STRING_HID[:length]
            
            elif desc_type == 0x22: # HID Report Descriptor
                data = DUALSENSE_HID_REPORT_DESCRIPTOR
                if len(data) < length:
                    data = data + b"\x00" * (length - len(data))
                return data[:length]
        
        # Interface-specific Get Descriptor (HID)
        if req_type == 0x81 and request == 0x06:
            desc_type = value >> 8
            if desc_type == 0x22: # HID Report Descriptor
                return DUALSENSE_HID_REPORT_DESCRIPTOR[:length]
                
        # HID Class Requests
        elif req_type == 0x21 and request == 0x09: # SET_REPORT
            # Steam sends SET_REPORT to initialize features.
            # But it can also send Output Report 0x02 via SET_REPORT!
            report_id = value & 0xff
            if (report_id == 0x01 or report_id == 0x02) and len(out_data) > 0:
                self._process_output_report(out_data)
            return b""
        
        elif req_type == 0xA1 and request == 0x01: # GET_REPORT
            report_type = value >> 8
            report_id = value & 0xFF
            if report_type == 3: # Feature Report
                if report_id == 0x05:
                    # Calibration Data: Prevent Division By Zero in SDL2 / Sony SDK
                    calib = bytearray(max(41, length))
                    calib[0] = 0x05
                    # Format: 3 biases, 6 gyro plus/minus, 2 gyro speed plus/minus, 6 acc plus/minus (all little-endian shorts)
                    struct.pack_into("<hhh hhhhhh hh hhhhhh", calib, 1, 
                        0, 0, 0,                               # Gyro Biases (Pitch, Yaw, Roll)
                        8192, -8192, 8192, -8192, 8192, -8192, # Gyro Plus/Minus
                        500, 500,                              # Gyro Speed Plus/Minus (test speed in deg/s)
                        8192, -8192, 8192, -8192, 8192, -8192  # Acc Plus/Minus
                    )
                    return bytes(calib)[:length]
                elif report_id == 0x09:
                    # MAC Address: Required by SDL2 / Sony SDK to uniquely identify the controller
                    mac = bytearray(max(20, length))
                    mac[0] = 0x09
                    if getattr(self, 'mac_address', None):
                        try:
                            mac_parts = [int(x, 16) for x in self.mac_address.split(':')]
                            mac[1:7] = mac_parts[:6]
                        except:
                            mac[1:7] = b'\x00\x11\x22\x33\x44\x55'
                    else:
                        mac[1:7] = b'\x00\x11\x22\x33\x44\x55'
                    return bytes(mac)[:length]
                elif report_id == 0x20:
                    # Firmware Version (Structured as DualSense)
                    fw = bytearray(max(64, length))
                    fw[0] = 0x20
                    # Build Date at 28-39 (12 bytes)
                    fw[28:40] = b'Aug 20 2020\0'
                    # Build Time at 40-48 (9 bytes)
                    fw[40:49] = b'12:00:00\0'
                    # fw_type at 49
                    fw[49] = 0x00
                    # fw_version at 50-53: embed devnum in low byte so each instance
                    # reports a distinct version; prevents Steam/WGI from treating
                    # two virtual DualSense devices as the same physical controller.
                    struct.pack_into("<I", fw, 50, 0x01000000 | (self.devnum & 0xFF))
                    # hw_version at 54-57
                    struct.pack_into("<I", fw, 54, 0x01000000)
                    return bytes(fw)[:length]

                elif report_id == 0x03:
                    # Capabilities
                    cap = bytearray(max(48, length))
                    cap[0] = 0x03
                    cap[2] = 0x28 # Magic for capabilities
                    cap[4] = 0xFF # All features supported (sensors, lightbar, vibration, touchpad)
                    cap[5] = 0x00 # Device type
                    return bytes(cap)[:length]
                elif report_id == 0x81:
                    # Bluetooth MAC Address (Often requested by games expecting BT connection)
                    mac_bt = bytearray(max(64, length))
                    mac_bt[0] = 0x81
                    if getattr(self, 'mac_address', None):
                        try:
                            mac_bt[1:7] = [int(x, 16) for x in self.mac_address.split(':')][:6]
                        except:
                            mac_bt[1:7] = b'\x00\x11\x22\x33\x44\x55'
                    else:
                        mac_bt[1:7] = b'\x00\x11\x22\x33\x44\x55'
                    return bytes(mac_bt)[:length]

                # Default for other feature reports: non-zero buffer to look like a real initialized device
                buf = bytearray(max(1, length))
                buf[0] = report_id
                for i in range(1, len(buf)):
                    buf[i] = 0x11 # non-zero dummy pattern
                return bytes(buf)[:length]
            elif report_type == 1: # Input Report
                if report_id == 0x01:
                    with self.lock:
                        return bytes(self.last_state)[:length]
            return b""
            
        if req_type == 0x21 and request == 0x09: # SET_REPORT
            return b""
                
        # UAC1 Audio Class Requests
        if (req_type & 0x60) == 0x20: # Class request (IN or OUT)
            ctrl_sel = (value >> 8) & 0xFF
            entity_id = (index >> 8) & 0xFF
            if entity_id in (0x02, 0x05) and ctrl_sel in (0x01, 0x02):
                return self._handle_uac1_feature_unit_request(
                    req_type, request, value, index, length, out_data
                )
            # AUDIO_CS_REQ_CUR = 0x01, AUDIO_CS_REQ_MIN = 0x02, AUDIO_CS_REQ_MAX = 0x03, AUDIO_CS_REQ_RES = 0x04
            # We just mock the responses to keep Windows happy
            if req_type in (0xA1, 0xA2): # GET_CUR / GET_MIN / etc.
                if length == 1:
                    return b"\x00"
                elif length == 2:
                    if request == 0x82: # GET_MIN
                        return b"\x00\x80" # -32768
                    elif request == 0x83: # GET_MAX
                        return b"\x00\x00" # 0
                    elif request == 0x84: # GET_RES
                        return b"\x00\x01" # 1
                    else: # GET_CUR
                        return b"\x00\x00"
                elif length == 3:
                    return b"\x80\xBB\x00" # 48000
                elif length == 4:
                    return struct.pack("<I", 48000)
                else:
                    return b"\x00" * length
            else: # SET_CUR (OUT)
                if request == 0x0B: # SET_INTERFACE
                    # Setting alt setting activates audio streaming
                    self.audio_active = (value != 0)
                return b""

        if request == 0x0B and req_type in (0x01, 0x11): # Standard Set Interface
            # index = interface number, value = alternate setting.  Interface 1 is the
            # speaker/haptic stream (EP 0x01 OUT); interface 2 is the microphone stream
            # (EP 0x82 IN) — the mic always streams silence (no physical Switch 2 mic).
            alt_active = (value != 0)
            if index == 1:
                self.audio_active = alt_active
                self._reset_audio_stream("out", active=alt_active, reason=f"SET_INTERFACE alt={value}", drain=not alt_active)
                if not alt_active and getattr(self, 'on_audio_data_callback', None):
                    self.on_audio_data_callback(None)
                logger.info("Audio OUT interface %d alt=%d active=%s", index, value, alt_active)
            elif index == 2:
                self.mic_active = alt_active
                self._reset_audio_stream("in", active=alt_active, reason=f"SET_INTERFACE alt={value}", drain=not alt_active)
                logger.info("Audio IN (mic) interface %d alt=%d active=%s (silent)", index, value, alt_active)
            else:
                logger.info("Interface %d alt=%d active=%s", index, value, alt_active)
            return b""

        # --- Standard requests: answer these *correctly* so the Windows audio engine
        # (audiodg) initialises the UAC endpoint cleanly instead of re-probing in a loop.
        # A failed probe here starves the shared control endpoint and blocks the game's
        # HID init, which is why some titles (e.g. BF6) had no rumble until the audio
        # device was toggled once. ---

        # GET_STATUS: device = self-powered (0x0001); interface & endpoint = 0x0000.
        # Reporting 0x0001 for an endpoint sets the HALT bit -> CLEAR_FEATURE storms and
        # endless stream re-init.
        if request == 0x00 and req_type in (0x80, 0x81, 0x82):
            status = 0x0001 if req_type == 0x80 else 0x0000
            return struct.pack("<H", status)[:length]

        # GET_CONFIGURATION -> report we are configured (bConfigurationValue = 1).
        if req_type == 0x80 and request == 0x08:
            return bytes([0x01])[:length]

        # GET_INTERFACE -> current alternate setting of the addressed interface.
        # audiodg queries this on the streaming interfaces; an empty reply (old behaviour)
        # is treated as a failed query and triggers a re-probe.
        if req_type == 0x81 and request == 0x0a:
            if index == 1:
                alt = 1 if getattr(self, 'audio_active', False) else 0
            elif index == 2:
                alt = 1 if getattr(self, 'mic_active', False) else 0
            else:
                alt = 0
            return bytes([alt])[:length]

        # SET_CONFIGURATION / CLEAR_FEATURE / SET_FEATURE -> ACK (no data stage).
        if req_type in (0x00, 0x01, 0x02) and request in (0x01, 0x03, 0x09):
            if req_type == 0x02 and request == 0x01:
                endpoint = index & 0xff
                shape = getattr(self, "_last_iso_shape_by_ep", {}).get(endpoint)
                clear_kind = "hard"
                should_log_clear_warning = True
                if endpoint == 0x01:
                    # Read the depth BEFORE draining so the log shows the real backlog
                    # at halt time (drain=True empties the queue, which would always
                    # log queue=0 and hide the accumulation that caused the halt).
                    now_clear = time.perf_counter()
                    queue_depth = self.pending_iso_out_urbs.qsize()
                    tx_depth = self._tx_audio_count("out")
                    submit_age_ms = ((now_clear - self._audio_out_last_submit_time) * 1000.0
                                     if self._audio_out_last_submit_time else -1.0)
                    complete_age_ms = ((now_clear - self._audio_out_last_complete_time) * 1000.0
                                       if self._audio_out_last_complete_time else -1.0)
                    complete_latency_ms = self._audio_out_last_complete_latency * 1000.0
                    pacer_wait_ms = self._audio_out_last_pacer_wait * 1000.0
                    pacer_late_ms = self._audio_out_last_pacer_late * 1000.0
                    pacer_expected_ms = self._audio_out_last_pacer_duration * 1000.0
                    pacer_first = 1 if self._audio_out_last_pacer_first else 0
                    pacer_reset = 1 if self._audio_out_last_pacer_reset else 0
                    due_delay_ms = self._audio_out_last_due_delay * 1000.0
                    sched_depth = self._audio_out_last_schedule_depth
                    missed_ticks = self._audio_out_missed_tick_skips
                    clock_resyncs = self._audio_out_clock_resyncs
                    submit_interval_ms = self._audio_out_last_submit_interval * 1000.0
                    complete_interval_ms = self._audio_out_last_complete_interval * 1000.0
                    wake_late_ms = self._audio_out_last_worker_wakeup_late * 1000.0
                    target_latency_ms = self._audio_out_last_target_latency * 1000.0
                    latency_cap_hit = 1 if self._audio_out_last_latency_cap_hit else 0
                    latency_cap_hits = self._audio_out_latency_cap_hits
                    frame_clock_valid = 1 if self._audio_out_frame_clock_valid else 0
                    frame_phase_error_ms = self._audio_out_last_frame_phase_error * 1000.0
                    start_frame_log = self._audio_out_last_start_frame
                    seqnum_log = self._audio_out_last_seqnum
                    target_source = self._audio_out_last_target_source
                    warmup_remaining = self._audio_out_warmup_remaining
                    warmup_count = self._audio_out_warmup_count
                    clock_locked = 1 if self._audio_out_clock_locked else 0
                    submit_ema_ms = self._audio_out_submit_interval_ema * 1000.0
                    submit_jitter_ms = self._audio_out_submit_jitter_ema * 1000.0
                    stable_submit_count = self._audio_out_stable_submit_count
                    latency_ceiling_hits = self._audio_out_latency_ceiling_hits
                    soft_clear = self._is_soft_audio_out_clear(
                        queue_depth,
                        tx_depth,
                        sched_depth,
                        complete_age_ms / 1000.0,
                        complete_latency_ms / 1000.0,
                        target_latency_ms / 1000.0,
                        shape,
                    )
                    if soft_clear:
                        clear_kind = "soft"
                        should_log_clear_warning = False
                        tx_dropped = 0
                        self._record_audio_out_soft_clear(
                            queue_depth,
                            tx_depth,
                            sched_depth,
                            target_latency_ms,
                            wake_late_ms,
                            complete_latency_ms,
                            warmup_remaining,
                            warmup_count,
                            target_source,
                        )
                    else:
                        self._audio_out_hard_clear_count += 1
                        self._invalidate_audio_stream(
                            "out",
                            active=getattr(self, "audio_active", True),
                            reason="CLEAR_FEATURE(ENDPOINT_HALT)",
                            drain=True,
                            reset_pacer=True,
                        )
                        tx_dropped = self._drop_tx_audio_stream_items("out", complete=True)
                elif endpoint == 0x82:
                    self._set_stream_reset_barrier("in")
                    queue_depth = self.pending_iso_in_urbs.qsize()
                    tx_depth = self._tx_audio_count("in")
                    submit_age_ms = -1.0
                    complete_age_ms = -1.0
                    complete_latency_ms = -1.0
                    pacer_wait_ms = -1.0
                    pacer_late_ms = -1.0
                    pacer_expected_ms = -1.0
                    pacer_first = 0
                    pacer_reset = 0
                    due_delay_ms = -1.0
                    sched_depth = -1
                    missed_ticks = -1
                    clock_resyncs = -1
                    submit_interval_ms = -1.0
                    complete_interval_ms = -1.0
                    wake_late_ms = -1.0
                    target_latency_ms = -1.0
                    latency_cap_hit = 0
                    latency_cap_hits = -1
                    frame_clock_valid = 0
                    frame_phase_error_ms = -1.0
                    start_frame_log = -1
                    seqnum_log = -1
                    target_source = "n/a"
                    warmup_remaining = -1
                    warmup_count = -1
                    clock_locked = 0
                    submit_ema_ms = -1.0
                    submit_jitter_ms = -1.0
                    stable_submit_count = -1
                    latency_ceiling_hits = -1
                    self._reset_audio_stream(
                        "in",
                        active=getattr(self, "mic_active", False),
                        reason="CLEAR_FEATURE(ENDPOINT_HALT)",
                        drain=True,
                    )
                    tx_dropped = self._drop_tx_audio_stream_items("in", complete=True)
                else:
                    queue_depth = 0
                    tx_depth = 0
                    tx_dropped = 0
                    submit_age_ms = -1.0
                    complete_age_ms = -1.0
                    complete_latency_ms = -1.0
                    pacer_wait_ms = -1.0
                    pacer_late_ms = -1.0
                    pacer_expected_ms = -1.0
                    pacer_first = 0
                    pacer_reset = 0
                    due_delay_ms = -1.0
                    sched_depth = -1
                    missed_ticks = -1
                    clock_resyncs = -1
                    submit_interval_ms = -1.0
                    complete_interval_ms = -1.0
                    wake_late_ms = -1.0
                    target_latency_ms = -1.0
                    latency_cap_hit = 0
                    latency_cap_hits = -1
                    frame_clock_valid = 0
                    frame_phase_error_ms = -1.0
                    start_frame_log = -1
                    seqnum_log = -1
                    target_source = "n/a"
                    warmup_remaining = -1
                    warmup_count = -1
                    clock_locked = 0
                    submit_ema_ms = -1.0
                    submit_jitter_ms = -1.0
                    stable_submit_count = -1
                    latency_ceiling_hits = -1
                if should_log_clear_warning:
                    logger.warning(
                        "Host sent CLEAR_FEATURE(ENDPOINT_HALT) to endpoint 0x%02x clear=%s soft_clears=%d hard_clears=%d queue=%d sched_depth=%d tx_audio=%d txq=%d tx_dropped=%d pacing=%s fast_tx=%s target=%s target_latency=%.2fms cap_hit=%d cap_hits=%d ceiling_hits=%d warmup_hold=%d warmup=%d warmup_count=%d locked=%d stable=%d submit_ema=%.2fms submit_jitter=%.2fms frame_clock=%d start_frame=%d seq=%d phase=%.2fms pacer_wait=%.2fms pacer_late=%.2fms due_delay=%.2fms expected=%.2fms first=%d reset=%d missed_ticks=%d clock_resyncs=%d submit_interval=%.2fms complete_interval=%.2fms wake_late=%.2fms last_submit_age=%.2fms last_complete_age=%.2fms last_complete_latency=%.2fms last_iso=%s",
                        endpoint,
                        clear_kind,
                        self._audio_out_soft_clear_count,
                        self._audio_out_hard_clear_count,
                        queue_depth,
                        sched_depth,
                        tx_depth,
                        self.tx_queue.qsize(),
                        tx_dropped,
                        "on" if EP1_OUT_PACING_ENABLED else "off",
                        "on" if EP1_OUT_FAST_TX_ENABLED else "off",
                        target_source,
                        target_latency_ms,
                        latency_cap_hit,
                        latency_cap_hits,
                        latency_ceiling_hits,
                        1 if self._audio_out_warmup_hold else 0,
                        warmup_remaining,
                        warmup_count,
                        clock_locked,
                        stable_submit_count,
                        submit_ema_ms,
                        submit_jitter_ms,
                        frame_clock_valid,
                        start_frame_log,
                        seqnum_log,
                        frame_phase_error_ms,
                        pacer_wait_ms,
                        pacer_late_ms,
                        due_delay_ms,
                        pacer_expected_ms,
                        pacer_first,
                        pacer_reset,
                        missed_ticks,
                        clock_resyncs,
                        submit_interval_ms,
                        complete_interval_ms,
                        wake_late_ms,
                        submit_age_ms,
                        complete_age_ms,
                        complete_latency_ms,
                        shape,
                    )
            return b""

        # Fallback for unhandled Device-to-Host (GET) requests to return zeroed data.
        # This acts as a default response for Audio Class GET_MIN/MAX/CUR for Volume/Mute,
        # which effectively reports fixed maximum volume (0dB) and unmuted (0).
        if req_type & 0x80:
            return b"\x00" * transfer_length
            
        return b""
