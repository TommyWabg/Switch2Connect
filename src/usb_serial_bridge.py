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

import asyncio
import hashlib
import json
import logging
import os
import queue
import re
import threading
import time
import ctypes
from dataclasses import dataclass

import win32com.client
import serial

from config import get_driver_path, CONFIG
_PERF_DIAGNOSTICS = os.environ.get('SWITCH2_PERF_DIAGNOSTICS', '0') == '1'
from controller import (
    Controller,
    ControllerInfo,
    PRO_CONTROLLER2_PID,
    NINTENDO_VENDOR_ID,
    COMMAND_WRITE_UUID,
    COMMAND_RESPONSE_UUID,
    INPUT_REPORT_UUID,
    VIBRATION_WRITE_PRO_CONTROLLER_UUID,
    VIBRATION_WRITE_JOYCON_L_UUID,
    VIBRATION_WRITE_JOYCON_R_UUID,
    StickCalibrationData,
    VibrationData,
)
NINTENDO_INPUT_REPORT_ID = 0x05

logger = logging.getLogger(__name__)
MANAGER_COMMAND_LOCK = threading.Lock()
ACTIVE_CLIENTS = []

# Set True only after "scan on" is sent and the bridge is fully armed.
# GUI reads this to show "Initializing" vs "Ready"; bridge_event_callback
# uses it to gate controller connections until the session is ready.
BRIDGE_SCAN_ACTIVE = False

CDC_WAKE_DELAY_SECONDS = 0.1
CDC_WRITE_TIMEOUT_SECONDS = 0.003

def _set_current_thread_priority(level):
    try:
        if os.name == "nt":
            kernel32 = ctypes.windll.kernel32
            kernel32.SetThreadPriority(kernel32.GetCurrentThread(), int(level))
    except Exception:
        pass
STATUS_PROBE_ATTEMPTS = 3
STATUS_PROBE_RETRY_DELAY_SECONDS = 0.15
STARTUP_STATUS_WAKE_DELAY_SECONDS = 0.5
STARTUP_STATUS_READ_WINDOW_SECONDS = 0.5

ESP32S3_LABEL = "ESP32-S3 CDC"
APP_FIRMWARE_VERSION = "1.2"
EXPECTED_FIRMWARE_PROFILE = "tinyusb_direct"
EXPECTED_FIRMWARE_BUILD = "cdc_bridge_3"
MAX_ESP32S3_CHANNELS = 8
MAX_ESP32S3_GROUPS = 4
NINTENDO_REPORT_SIZE = 64
# Prefix of the SW2 input-report characteristic UUID. Frames flagged as command/ack
# responses (channel byte high bit set) are routed away from this characteristic's
# callback so they aren't misparsed as controller input.
INPUT_UUID_PREFIX = "ab7de9be"
DEFAULT_STICK_CALIBRATION = bytes.fromhex("00 08 80 dc c5 5d dc c5 5d")

@dataclass
class PortInfo:
    port: str
    name: str
    manufacturer: str
    device_id: str
    likely_ch343: bool
    is_otg: bool = False

@dataclass
class BridgeStatus:
    serial_port: PortInfo | None
    firmware_installed: bool
    status_text: str = ""
    firmware_version: str = ""
    firmware_mode: str = ""
    firmware_profile: str = ""
    expected_version: str = ""
    firmware_current: bool = False
    firmware_update_required: bool = False
    bridge_ready: bool = False
    usb_present: bool = False
    hid_path: str | None = None # For compatibility with discoverer.py
    otg_only: bool = False  # True when only native OTG/VID-303A port detected, no CH343P

    @property
    def board_present(self):
        return self.serial_port is not None or self.usb_present

class DummyBleDevice:
    def __init__(self, address: str, name: str):
        self.address = address
        self.name = name

def _wake_serial_port(handle):
    handle.dtr = True
    handle.rts = True
    time.sleep(CDC_WAKE_DELAY_SECONDS)
    try:
        handle.reset_input_buffer()
    except Exception:
        pass
    try:
        handle.reset_output_buffer()
    except Exception:
        pass

class ESP32S3SerialClient:
    def __init__(self, port: str):
        self.port = port
        self.handle = None
        self.is_connected = False
        self._read_thread = None
        self._read_stop = threading.Event()
        self.event_callback = None
        self._notify_callbacks = {}
        self._notify_lock = threading.RLock()
        # The serial reader only frames and publishes. Controller input work is
        # isolated on a latest-only consumer so a slow mapping/gyro/USBIP callback
        # can never stop CDC draining. Command/ACK/event callbacks keep FIFO order
        # on a separate low-volume dispatcher.
        self._input_condition = threading.Condition()
        self._input_slots = {}
        self._input_consumer_stop = False
        self._input_consumer_threads = {}
        # Compatibility alias for diagnostics/tests that tracked the original
        # shared consumer. It points at channel 0's worker.
        self._input_consumer_thread = None
        self._input_sequence = 0
        self._input_rr_cursor = None
        self._input_diag_t0 = time.perf_counter()
        self._input_diag = {}
        self._control_dispatch_queue = queue.Queue(maxsize=256)
        self._control_dispatch_stop = False
        self._control_dispatch_thread = None
        self._control_dispatch_drop = 0
        self._cdc_diag_t0 = time.perf_counter()
        self._cdc_diag_bytes = 0
        self._cdc_diag_reads = 0
        self._cdc_diag_input = 0
        self._cdc_diag_command = 0
        self._cdc_diag_text = 0
        self._cdc_diag_resync = 0
        self._cdc_diag_backlog_max = 0
        self._cdc_diag_gap_us = 0.0
        self._cdc_diag_gap_count = 0
        self._cdc_diag_gap_max_us = 0.0
        self._cdc_last_drain_end = time.perf_counter()
        self._gatt_lock = threading.RLock()
        self._gatt_chars = {}
        self._gatt_done_channels = set()
        self._write_lock = threading.Lock()
        # Shadow rumble publication must never block an Audio Haptic cadence
        # thread on USB CDC.  Keep one latest payload per channel and let a
        # persistent bridge-level worker perform the serial writes.
        self._rumble_tx_condition = threading.Condition()
        self._rumble_tx_slots = {}
        self._rumble_tx_stop = False
        self._rumble_tx_thread = None
        self._write_count = 0
        self._response_queue = queue.Queue()
        self._closed_by_error = False
        self.firmware_features = {}
        ACTIVE_CLIENTS.append(self)

    @property
    def supports_direct_rumble(self) -> bool:
        return bool(self.firmware_features.get("direct_rumble"))

    @property
    def supports_latest_rumble_shadow(self) -> bool:
        return bool(self.firmware_features.get("shadow_latest"))

    def _update_firmware_features(self, text) -> None:
        try:
            data = json.loads(text)
            if data.get("cmd") == "status" and isinstance(data.get("features"), dict):
                self.firmware_features = dict(data["features"])
        except Exception:
            pass

    def open(self, fast=False):
        if self.is_connected:
            return
        if self._closed_by_error:
            raise OSError(f"ESP32-S3 port {self.port} was closed after a hardware disconnect; reconnect required")
        try:
            self.handle = serial.Serial(
                self.port, baudrate=2000000, timeout=0.1,
                write_timeout=CDC_WRITE_TIMEOUT_SECONDS)
            if fast:
                # The startup probe verified this bridge moments ago. Avoid a
                # second fixed CDC wake delay; the first manager command remains
                # the transport validation point.
                self.handle.dtr = True
                self.handle.rts = True
                try:
                    self.handle.reset_input_buffer()
                    self.handle.reset_output_buffer()
                except Exception:
                    pass
            else:
                _wake_serial_port(self.handle)
            self.is_connected = True
        except Exception as e:
            raise OSError(f"Unable to open ESP32-S3 serial port {self.port}: {e}")

    def _try_reopen(self) -> bool:
        """Retry reopening the port indefinitely until success or _read_stop is set.
        Must only be called from the read thread.

        Acquires _write_lock around each serial.Serial() call so that concurrent
        send_ble_write calls either block briefly while we swap the handle or find
        is_connected=True after we succeed and return immediately.

        Returns True if the port was recovered, False only when _read_stop is set
        (graceful shutdown) — never returns False due to retry exhaustion.
        """
        if self.handle:
            try:
                self.handle.close()
            except Exception:
                pass
            self.handle = None
        self.is_connected = False
        with self._input_condition:
            self._input_slots.clear()
            self._input_rr_cursor = None

        attempt = 0
        while not self._read_stop.is_set():
            # A concurrent thread (e.g. send_manager_command) may have already
            # recovered the port while we were sleeping.
            if self.is_connected:
                return True
            if attempt > 0:
                time.sleep(0.12)
            attempt += 1
            try:
                with self._write_lock:
                    if self.is_connected:
                        return True
                    new_handle = serial.Serial(
                        self.port, baudrate=2000000, timeout=0.1,
                        write_timeout=CDC_WRITE_TIMEOUT_SECONDS)
                    _wake_serial_port(new_handle)
                    self.handle = new_handle
                    self.is_connected = True
                return True
            except Exception:
                pass

        return self.is_connected  # _read_stop set — check if another thread recovered

    def _ensure_read_thread(self):
        self._read_stop.clear()
        if self._read_thread and self._read_thread.is_alive():
            return
        self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._read_thread.start()

    def _ensure_input_consumer(self, channel):
        channel = int(channel)
        with self._input_condition:
            self._input_consumer_stop = False
            thread = self._input_consumer_threads.get(channel)
            if thread and thread.is_alive():
                return
            thread = threading.Thread(
                target=self._input_consumer_loop, args=(channel,), daemon=True,
                name=f"ESP32InputConsumer-{self.port}-ch{channel}")
            self._input_consumer_threads[channel] = thread
            if channel == 0:
                self._input_consumer_thread = thread
            thread.start()

    def _ensure_control_dispatch_worker(self):
        if not self._control_dispatch_thread or not self._control_dispatch_thread.is_alive():
            self._control_dispatch_stop = False
            self._control_dispatch_thread = threading.Thread(
                target=self._control_dispatch_loop, daemon=True,
                name=f"ESP32ControlDispatch-{self.port}")
            self._control_dispatch_thread.start()

    def _drain_response_queue(self):
        while True:
            try:
                self._response_queue.get_nowait()
            except queue.Empty:
                break

    async def disconnect(self):
        self.close_sync()

    def close_sync(self):
        self._read_stop.set()
        with self._input_condition:
            self._input_consumer_stop = True
            self._input_slots.clear()
            self._input_condition.notify_all()
        self._control_dispatch_stop = True
        try:
            self._control_dispatch_queue.put_nowait(("stop",))
        except queue.Full:
            pass
        with self._rumble_tx_condition:
            self._rumble_tx_stop = True
            self._rumble_tx_slots.clear()
            self._rumble_tx_condition.notify_all()
        self.is_connected = False
        if self.handle:
            try:
                self.handle.close()
            except Exception:
                pass
            self.handle = None
                
        if self._read_thread and self._read_thread.is_alive():
            self._read_thread.join(timeout=0.5)
        for thread in tuple(self._input_consumer_threads.values()):
            if thread.is_alive():
                thread.join(timeout=0.5)
        self._input_consumer_threads.clear()
        self._input_consumer_thread = None
        if self._control_dispatch_thread and self._control_dispatch_thread.is_alive():
            self._control_dispatch_thread.join(timeout=0.5)
        if self._rumble_tx_thread and self._rumble_tx_thread.is_alive():
            self._rumble_tx_thread.join(timeout=0.5)
        while True:
            try:
                self._control_dispatch_queue.get_nowait()
            except queue.Empty:
                break
            
        if self in ACTIVE_CLIENTS:
            ACTIVE_CLIENTS.remove(self)

    def _ensure_rumble_tx_worker(self):
        with self._rumble_tx_condition:
            if self._rumble_tx_thread and self._rumble_tx_thread.is_alive():
                return
            self._rumble_tx_stop = False
            self._rumble_tx_thread = threading.Thread(
                target=self._rumble_tx_loop,
                daemon=True,
                name=f"ESP32RumbleTx-{self.port}",
            )
            self._rumble_tx_thread.start()

    def _rumble_tx_loop(self):
        """Write only the newest shadow payloads outside Audio/input workers."""
        while True:
            with self._rumble_tx_condition:
                while not self._rumble_tx_stop and not self._rumble_tx_slots:
                    self._rumble_tx_condition.wait()
                if self._rumble_tx_stop:
                    return
                slots = self._rumble_tx_slots
                self._rumble_tx_slots = {}

            # Preserve publication order between channels within this snapshot.
            # A newer value arriving during a write remains in the next snapshot
            # and supersedes any intermediate frame for that channel.
            for channel, data in slots.items():
                self._write_rumble_shadow(channel, data)

    def _write_rumble_shadow(self, channel: int, data) -> bool:
        hexd = bytes(data).hex()
        cmd = f"rs {int(channel)} {hexd}\n"
        with self._write_lock:
            try:
                self.open()
                self._ensure_read_thread()
                self.handle.write(cmd.encode("ascii"))
                return True
            except Exception as e:
                logger.debug("Failed to write ESP32-S3 rs payload: %s", e)
                return False








    async def stop_notify(self, _uuid):
        self._read_stop.set()

    @staticmethod
    def _norm_uuid(uuid):
        return str(uuid).lower()

    def _handle_gatt_event(self, event):
        try:
            channel = int(event.get("channel", 0))
        except Exception:
            return
        cmd = event.get("cmd")
        with self._gatt_lock:
            if cmd == "gatt_char":
                handle = int(event.get("handle", 0))
                uuid = self._norm_uuid(event.get("uuid", ""))
                props_text = str(event.get("props", "") or "")
                if not handle or not uuid:
                    return
                properties = []
                if "n" in props_text:
                    properties.append("notify")
                if "w" in props_text:
                    properties.append("write-without-response")
                if "W" in props_text:
                    properties.append("write")
                if "r" in props_text:
                    properties.append("read")
                service = self._norm_uuid(event.get("service", "ab7de9be-89fe-49ad-828f-118f09df7fd0"))
                self._gatt_chars.setdefault(channel, {})[handle] = {
                    "uuid": uuid,
                    "properties": properties,
                    "service": service,
                    "handle": handle,
                }
            elif cmd == "gatt_done":
                self._gatt_done_channels.add(channel)

    def get_channel_services(self, channel):
        channel = int(channel)
        with self._gatt_lock:
            chars = list((self._gatt_chars.get(channel) or {}).values())
        if not chars:
            return [MockService("ab7de9be-89fe-49ad-828f-118f09df7fd2")]

        by_service = {}
        for char in sorted(chars, key=lambda c: c.get("handle", 0)):
            by_service.setdefault(char["service"], []).append(
                MockCharacteristic(char["uuid"], char["properties"], handle=char["handle"])
            )
        return [MockService(service_uuid, characteristics) for service_uuid, characteristics in by_service.items()]

    def _uuid_for_handle(self, channel, handle):
        with self._gatt_lock:
            char = (self._gatt_chars.get(int(channel)) or {}).get(int(handle))
        return char.get("uuid") if char else None

    async def start_channel_notify(self, channel, uuid, callback):
        self.open()
        channel = int(channel)
        key = self._norm_uuid(uuid)
        with self._notify_lock:
            if channel not in self._notify_callbacks:
                self._notify_callbacks[channel] = {}
            callbacks = self._notify_callbacks[channel].setdefault(key, [])
            if callback not in callbacks:
                callbacks.append(callback)
        self._ensure_read_thread()
        
    async def stop_channel_notify(self, channel, uuid):
        channel = int(channel)
        key = self._norm_uuid(uuid)
        with self._notify_lock:
            if channel in self._notify_callbacks:
                self._notify_callbacks[channel].pop(key, None)
        with self._input_condition:
            self._input_slots.pop((channel, key), None)

    async def write_gatt_char(self, _uuid, data, response=False):
        await self.write_channel_gatt_char(0, _uuid, data, response=response)

    async def write_channel_gatt_char(self, channel, _uuid, data, response=False, mirror_channel=None):
        del response
        self.send_ble_write(int(channel), _uuid, data, mirror_channel=mirror_channel)
        return

    def write_output_report(self, data, report_id=1):
        del data, report_id
        return

    def send_manager_command(self, command: str, timeout=2.0):
        with MANAGER_COMMAND_LOCK:
            try:
                self.open()
            except OSError as e:
                logger.debug(f"ESP32-S3 port not available for command '{command}': {e}")
                return ""
            self._ensure_read_thread()
            self._drain_response_queue()

            # Only the atomic line write needs _write_lock (shared with vibration
            # writes). The response wait below must NOT hold it, otherwise a status
            # poll blocks all vibration writes for up to `timeout` — which in merge
            # mode (busier firmware -> slower status reply) shows up as periodic
            # vibration drop-outs. MANAGER_COMMAND_LOCK still serialises command
            # request/response cycles so responses can't get mixed up.
            with self._write_lock:
                try:
                    self.handle.write((command + "\n").encode("utf-8"))
                except Exception as e:
                    logger.debug(f"Failed to write manager command: {e}")
                    return ""

            try:
                response = self._response_queue.get(timeout=timeout)
                self._update_firmware_features(response)
                return response
            except queue.Empty:
                return ""

    def connect_mac(self, mac_str: str, addr_type: int = 0, timeout=10.0):
        with MANAGER_COMMAND_LOCK:
            self.open()
            self._ensure_read_thread()
            self._drain_response_queue()
            
            cmd = f"conn {addr_type} {mac_str}\n"
            try:
                self.handle.write(cmd.encode("ascii"))
            except Exception as e:
                logger.debug(f"Failed to write conn command: {e}")
                return None

            deadline = time.time() + timeout
            while time.time() < deadline:
                if not self.handle or not self.is_connected:
                    return None
                try:
                    text = self._response_queue.get(timeout=0.1)
                    if '"cmd":"connected"' in text:
                        try:
                            data = json.loads(text)
                            if data.get("mac", "").lower() == mac_str.lower():
                                return int(data.get("channel", -1))
                        except Exception:
                            pass
                    elif '"cmd":"connect_fail"' in text:
                        try:
                            data = json.loads(text)
                            if data.get("mac", "").lower() == mac_str.lower():
                                logger.debug(f"Firmware reported connect_fail for {mac_str}")
                                return None
                        except Exception:
                            pass
                except queue.Empty:
                    continue
            return None

    def send_command_line(self, command: str) -> bool:
        """Fire-and-forget manager command (no response expected).

        Used for commands like 'inputsrc <ch> legacy' whose firmware reply is a
        debug JSON that never enters the response queue; send_manager_command
        would always block until its timeout for those. Uses the lightweight
        _write_lock (like send_ble_write) so it cannot stall rumble/status polls.
        """
        with self._write_lock:
            self.open()
            self._ensure_read_thread()
            try:
                self.handle.write((command.strip() + "\n").encode("ascii"))
                return True
            except Exception as e:
                logger.debug("Failed to write ESP32-S3 command line %r: %s", command, e)
                return False

    def send_fire_and_forget(self, command: str) -> bool:
        """Write a manager command that has no response contract.

        This intentionally does not wait for the manager response queue.  Lifecycle
        completion is reported through bridge events, while status remains the only
        request/response manager operation.
        """
        return self.send_command_line(command)

    def send_ble_write(self, channel: int, uuid: str, data, mirror_channel=None):
        uuid_text = str(uuid).lower()
        if uuid_text == COMMAND_WRITE_UUID.lower():
            kind = "c"
        elif uuid_text in {
            VIBRATION_WRITE_PRO_CONTROLLER_UUID.lower(),
            VIBRATION_WRITE_JOYCON_L_UUID.lower(),
            VIBRATION_WRITE_JOYCON_R_UUID.lower(),
        }:
            kind = "r"
        else:
            logger.debug("ESP32-S3 write ignored for unsupported UUID %s", uuid)
            return False

        payload = bytes(data).hex()
        # A merged Joy-Con pair fans the SAME rumble frame to both channels in ONE command
        # ("ch,mirror") so the firmware writes both motors in-phase from a single dispatch.
        if mirror_channel is not None and kind == "r":
            chan_field = f"{int(channel)},{int(mirror_channel)}"
        else:
            chan_field = f"{int(channel)}"
        # Use the lightweight write lock (atomic line write) rather than the heavy
        # command lock, so vibration writes are never stalled by a status poll
        # waiting on its response. This removes the periodic merge-mode rumble gaps.
        with self._write_lock:
            self.open()
            self._ensure_read_thread()
            try:
                self.handle.write(f"wr {chan_field} {kind} {payload}\n".encode("ascii"))
                return True
            except Exception as e:
                logger.debug("Failed to write ESP32-S3 BLE payload: %s", e)
                return False

    def send_ble_write_pair(self, left_channel: int, right_channel: int, kind: str,
                            left_data, right_data) -> bool:
        """Send different rumble payloads to left and right channels in one 'wrpair' command.

        The firmware processes both payloads in a single command-handling cycle,
        queueing both BLE writes together and preventing the ~30 Hz L/R alternation
        that occurs when the two writes are sent as separate 'wr' commands.
        Falls back gracefully: returns False if the write fails so the caller can
        fall back to individual send_ble_write() calls.
        """
        left_hex = bytes(left_data).hex()
        right_hex = bytes(right_data).hex()
        cmd = f"wrpair {int(left_channel)} {int(right_channel)} {kind} {left_hex} {right_hex}\n"
        with self._write_lock:
            self.open()
            self._ensure_read_thread()
            try:
                self.handle.write(cmd.encode("ascii"))
                return True
            except Exception as e:
                logger.debug("Failed to write ESP32-S3 wrpair payload: %s", e)
                return False

    def send_rumble_shadow(self, channel: int, data) -> bool:
        """Publish the latest rumble payload without waiting for USB CDC.

        Format: 'rs <ch> <hex>'.  The firmware stores this as the channel's latest
        rumble and a dedicated firmware task re-sends it to BLE at a steady,
        hardware-timed cadence (stamping a fresh packet-id each write).  The host
        therefore only sends when the rumble value CHANGES; the firmware owns the
        sustain, so rumble smoothness no longer depends on host/OS scheduling jitter.
        Requires firmware with the 'shadow' feature (v0.11.3+).
        """
        self._ensure_rumble_tx_worker()
        with self._rumble_tx_condition:
            self._rumble_tx_slots[int(channel)] = bytes(data)
            self._rumble_tx_condition.notify()
        return True

    def send_rumble_direct(self, channel: int, data) -> bool:
        """Write one rumble payload straight to the controller's BLE rumble char.

        Format: 'rd <ch> <hex>'.  Unlike 'rs' (which queues into the firmware's shared
        15ms audio-haptics FIFO), 'rd' writes immediately -- the 0.12.2 behaviour -- so
        ordinary rumble stays smooth.  The host uses this for ESP32-S3 merged Joy-Con
        rumble whenever no audio-haptic stream is present.
        """
        if not self.supports_direct_rumble:
            return False
        hexd = bytes(data).hex()
        cmd = f"rd {int(channel)} {hexd}\n"
        with self._write_lock:
            self.open()
            self._ensure_read_thread()
            try:
                self.handle.write(cmd.encode("ascii"))
                return True
            except Exception as e:
                logger.debug("Failed to write ESP32-S3 rd payload: %s", e)
                return False

    def get_or_create_rumble_dispatcher(self):
        """Return (creating on first call) the shared rumble dispatcher for this bridge.

        The dispatcher is created lazily and shared between the Left and Right
        Joy-Con controllers that share this serial client in merged mode.
        """
        if not hasattr(self, '_rumble_dispatcher') or self._rumble_dispatcher is None:
            from esp32_rumble_dispatcher import ESP32BridgeRumbleDispatcher
            self._rumble_dispatcher = ESP32BridgeRumbleDispatcher(self)
        return self._rumble_dispatcher

    def stop_rumble_dispatcher(self):
        """Stop and discard the rumble dispatcher (called on bridge disconnect)."""
        disp = getattr(self, '_rumble_dispatcher', None)
        if disp is not None:
            disp.stop()
            self._rumble_dispatcher = None

    def _enqueue_control_dispatch(self, item):
        self._ensure_control_dispatch_worker()
        try:
            self._control_dispatch_queue.put_nowait(item)
            return True
        except queue.Full:
            self._control_dispatch_drop += 1
            return False

    def _control_dispatch_loop(self):
        while not self._control_dispatch_stop:
            try:
                item = self._control_dispatch_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if not item or item[0] == "stop":
                continue
            kind = item[0]
            try:
                if kind == "notify":
                    _, callbacks, payload, channel, uuid_text = item
                    for callback in callbacks:
                        try:
                            callback(None, bytearray(payload))
                        except Exception:
                            logger.exception(
                                "ESP32-S3 control callback failed channel=%d uuid=%s",
                                channel, uuid_text)
                elif kind == "event":
                    _, callback, event = item
                    callback(event)
            except Exception:
                logger.exception("ESP32-S3 control dispatcher failed")

    def _publish_input(self, channel, source_uuid, payload, callbacks):
        self._ensure_input_consumer(channel)
        key = (int(channel), self._norm_uuid(source_uuid or INPUT_REPORT_UUID))
        now = time.perf_counter()
        with self._input_condition:
            self._input_sequence += 1
            if _PERF_DIAGNOSTICS:
                diag = self._input_diag.setdefault(int(channel), {
                    "publish": 0, "overwrite": 0, "dispatch": 0, "error": 0,
                    "age_us": 0.0, "max_age_us": 0.0,
                    "callback_us": 0.0, "max_callback_us": 0.0,
                })
                diag["publish"] += 1
                if key in self._input_slots:
                    diag["overwrite"] += 1
            self._input_slots[key] = (
                now, self._input_sequence, tuple(callbacks), bytes(payload))
            # Consumers are channel-affine; wake all so the matching channel is
            # never left asleep behind an unrelated consumer.
            self._input_condition.notify_all()

    def _take_next_input_locked(self, channel):
        keys = [key for key in self._input_slots if key[0] == channel]
        if not keys:
            return None
        keys.sort()
        key = keys[0]
        if self._input_rr_cursor is not None:
            for candidate in keys:
                if candidate > self._input_rr_cursor:
                    key = candidate
                    break
        self._input_rr_cursor = key
        return key, self._input_slots.pop(key)

    def _input_consumer_loop(self, channel):
        # Keep input above normal but below USB audio workers (priority 2).
        _set_current_thread_priority(1)
        while True:
            with self._input_condition:
                while (not self._input_consumer_stop and
                       not any(key[0] == channel for key in self._input_slots)):
                    self._input_condition.wait(timeout=0.5)
                if self._input_consumer_stop:
                    return
                selected = self._take_next_input_locked(channel)
            if selected is None:
                continue
            (channel, uuid_text), (published, _sequence, callbacks, payload) = selected
            callback_started = time.perf_counter() if _PERF_DIAGNOSTICS else 0.0
            callback_errors = 0
            for callback in callbacks:
                try:
                    callback(None, bytearray(payload))
                except Exception:
                    callback_errors += 1
                    logger.exception(
                        "ESP32-S3 input callback failed channel=%d uuid=%s",
                        channel, uuid_text)
            if _PERF_DIAGNOSTICS:
                callback_finished = time.perf_counter()
                with self._input_condition:
                    diag = self._input_diag.setdefault(channel, {
                        "publish": 0, "overwrite": 0, "dispatch": 0, "error": 0,
                        "age_us": 0.0, "max_age_us": 0.0,
                        "callback_us": 0.0, "max_callback_us": 0.0,
                    })
                    age_us = (callback_started - published) * 1_000_000.0
                    callback_us = (callback_finished - callback_started) * 1_000_000.0
                    diag["dispatch"] += 1
                    diag["error"] += callback_errors
                    diag["age_us"] += age_us
                    diag["max_age_us"] = max(diag["max_age_us"], age_us)
                    diag["callback_us"] += callback_us
                    diag["max_callback_us"] = max(diag["max_callback_us"], callback_us)
                    snapshots = self._maybe_log_input_diagnostics_locked()
                self._emit_input_diagnostics(snapshots)
            # Bound bursts and give priority-2 audio workers an immediate chance
            # to run when both Joy-Con channels are continuously active.
            time.sleep(0)

    def _maybe_log_input_diagnostics_locked(self):
        if not _PERF_DIAGNOSTICS:
            return None
        now = time.perf_counter()
        if now - self._input_diag_t0 < 1.0:
            return None
        snapshots = []
        for channel, diag in sorted(self._input_diag.items()):
            dispatch = max(1, diag["dispatch"])
            snapshots.append((
                channel, diag["publish"], diag["overwrite"], diag["dispatch"],
                diag["age_us"] / dispatch, diag["max_age_us"],
                diag["callback_us"] / dispatch, diag["max_callback_us"],
                diag["error"],
            ))
        self._input_diag.clear()
        self._input_diag_t0 = now
        return snapshots

    def _emit_input_diagnostics(self, snapshots):
        if not snapshots:
            return
        for snapshot in snapshots:
            (channel, publish, overwrite, dispatch, avg_age_us, max_age_us,
             avg_callback_us, max_callback_us, errors) = snapshot
            logger.info(
                "ESP32-INPUT ch=%d publish=%d overwrite=%d dispatch=%d "
                "age_avg=%.0fus age_max=%.0fus callback_avg=%.0fus "
                "callback_max=%.0fus errors=%d",
                channel, publish, overwrite, dispatch, avg_age_us, max_age_us,
                avg_callback_us, max_callback_us, errors)

    def _queue_text_response(self, data):
        if not data:
            return
        text = bytes(data).decode("utf-8", errors="ignore")
        text = re.sub(r"\x1b\[[0-9;]*m", "", text).strip()
        if "{" not in text and "}" not in text:
            logger.debug("ESP32-S3 raw: %s", text)
            return
        start = text.find("{")
        if start < 0:
            start = 0
        end = text.rfind("}")
        if end >= start:
            text = text[start:end + 1]
        else:
            text = text[start:]
        if text:
            if ('"cmd":"gatt_char"' in text or '"cmd":"gatt_done"' in text):
                try:
                    self._handle_gatt_event(json.loads(text))
                except Exception:
                    logger.debug("Failed to parse ESP32-S3 GATT metadata: %s", text, exc_info=True)
                return
            if ('"cmd":"scan_result"' in text or '"cmd":"connected"' in text
                    or '"cmd":"link_up"' in text or '"cmd":"gatt_ready"' in text
                    or '"cmd":"connect_fail"' in text or '"cmd":"connect_busy"' in text
                    or '"cmd":"disconnected"' in text):
                if self.event_callback:
                    try:
                        self._enqueue_control_dispatch(
                            ("event", self.event_callback, json.loads(text)))
                    except Exception:
                        pass
                # These are event-only; never go into the command response queue
                return
            if '"cmd":"debug"' in text:
                # Firmware debug messages must not pollute the status response queue:
                # send_manager_command("status lite") would return the debug JSON, parse
                # ble_channels=0, and falsely trigger a multi-controller disconnect.
                try:
                    debug_data = json.loads(text)
                    message = str(debug_data.get('msg') or '')
                    if message.startswith('QOS '):
                        logger.debug("ESP32-S3 firmware status: %s", message)
                    else:
                        logger.debug("ESP32-S3 firmware debug: %s", message)
                except Exception:
                    logger.debug("ESP32-S3 firmware debug parse failed: %s", text)
                return
            if '"cmd":"qos"' in text:
                try:
                    qos = json.loads(text)
                    logger.debug("ESP32-S3 firmware status: %s", qos)
                except Exception:
                    logger.debug("ESP32-S3 firmware QoS parse failed: %s", text)
                return
            self._response_queue.put(text)

    def _dispatch_binary_packet(self, data):
        if not data:
            return

        self.last_input_time = time.time()

        # High bit flags command/ack frames. 0x40 flags v2 handle-routed notify
        # frames: <0x40|channel> <handle_le16> <payload...>.
        is_command = bool(data[0] & 0x80)
        has_handle = bool(data[0] & 0x40) and not is_command
        chan_id = data[0] & (0x3F if has_handle else 0x7F)

        if 1 <= chan_id <= MAX_ESP32S3_CHANNELS:
            channel = chan_id - 1
            source_uuid = None
            if has_handle:
                if len(data) < 3:
                    return
                handle = data[1] | (data[2] << 8)
                source_uuid = self._uuid_for_handle(channel, handle)
                report_payload = data[3:]
            else:
                report_payload = data[1:]

            with self._notify_lock:
                channel_callbacks = {
                    uuid: tuple(callbacks)
                    for uuid, callbacks in self._notify_callbacks.get(channel, {}).items()
                }
            for uuid, callbacks in list(channel_callbacks.items()):
                uuid_text = str(uuid).lower()
                if source_uuid is not None and uuid_text != source_uuid:
                    continue
                if is_command and uuid_text.startswith(INPUT_UUID_PREFIX):
                    continue
                if not is_command and source_uuid is None and not uuid_text.startswith(INPUT_UUID_PREFIX):
                    continue
                if is_command:
                    # ACK/command notifications are ordered and must never be
                    # coalesced.  Keep them off the CDC reader nonetheless.
                    self._enqueue_control_dispatch(
                        ("notify", callbacks, bytes(report_payload), channel, uuid_text))
                else:
                    # Ordinary input is state, not an event stream.  Publish only
                    # the latest state so CDC/UDP stalls cannot replay stale input.
                    self._publish_input(
                        channel, source_uuid or uuid_text, report_payload, callbacks)
            return

        if data[0] == NINTENDO_INPUT_REPORT_ID:
            data = data[1:]
        
        with self._notify_lock:
            callbacks = tuple(
                callback
                for uuid, registered in self._notify_callbacks.get(0, {}).items()
                if str(uuid).lower().startswith(INPUT_UUID_PREFIX)
                for callback in registered
            )
        if callbacks:
            self._publish_input(0, INPUT_REPORT_UUID, data, callbacks)

    def _read_loop(self):
        _set_current_thread_priority(2)
        buf = bytearray()
        while not self._read_stop.is_set() and self.is_connected and self.handle:
            try:
                waiting = self.handle.in_waiting
                if waiting:
                    now = time.perf_counter()
                    gap_us = (now - self._cdc_last_drain_end) * 1_000_000.0
                    self._cdc_diag_gap_us += gap_us
                    self._cdc_diag_gap_count += 1
                    self._cdc_diag_gap_max_us = max(self._cdc_diag_gap_max_us, gap_us)
                    self._cdc_diag_backlog_max = max(self._cdc_diag_backlog_max, waiting)
                    chunk = self.handle.read(waiting or 1024)
                    if chunk:
                        buf.extend(chunk)
                else:
                    chunk = self.handle.read(1)
                    if chunk:
                        buf.extend(chunk)
                if chunk:
                    self._cdc_diag_bytes += len(chunk)
                    self._cdc_diag_reads += 1
                self._cdc_last_drain_end = time.perf_counter()
            except serial.SerialException as e:
                logger.warning("ESP32-S3 Serial read loop error on %s: %s — retrying until recovered", self.port, e)
                if self._try_reopen():
                    logger.info("ESP32-S3 Serial port %s recovered", self.port)
                    buf = bytearray()
                    # Continue the outer while loop with the new handle
                else:
                    # _read_stop was set (graceful shutdown) — exit cleanly
                    if self in ACTIVE_CLIENTS:
                        ACTIVE_CLIENTS.remove(self)
                    break
            except OSError as e:
                logger.warning("ESP32-S3 Serial read loop OS error on %s: %s — retrying until recovered", self.port, e)
                if self._try_reopen():
                    logger.info("ESP32-S3 Serial port %s recovered", self.port)
                    buf = bytearray()
                else:
                    if self in ACTIVE_CLIENTS:
                        ACTIVE_CLIENTS.remove(self)
                    break
            except Exception as e:
                logger.debug("ESP32-S3 Serial read loop transient error on %s: %s", self.port, e)
                time.sleep(0.01)
                continue

            while True:
                nl_idx = buf.find(b"\n")
                hdr_idx = buf.find(b"\xaa\x55")

                if nl_idx != -1 and (hdr_idx == -1 or nl_idx < hdr_idx):
                    line = bytes(buf[:nl_idx + 1])
                    buf = buf[nl_idx + 1:]
                    self._cdc_diag_text += 1
                    self._queue_text_response(line)
                    continue

                if hdr_idx > 0:
                    self._cdc_diag_resync += hdr_idx
                    buf = buf[hdr_idx:]
                    continue

                if hdr_idx == 0:
                    if len(buf) < 3:
                        break

                    frame_len = buf[2]

                    if frame_len >= 2 and len(buf) >= 4 and 1 <= (buf[3] & 0x7F) <= MAX_ESP32S3_CHANNELS:
                        # buf[3] is (channel+1) with the high bit flagging command/ack;
                        # mask 0x7F only for the range check, keep the flag in `data`.
                        total_size = 3 + frame_len
                        if len(buf) < total_size:
                            break
                        data = bytes(buf[3:total_size])
                    elif frame_len <= NINTENDO_REPORT_SIZE and len(buf) >= 4 and 0 <= buf[3] < MAX_ESP32S3_CHANNELS:
                        total_size = 4 + frame_len
                        if len(buf) < total_size:
                            break
                        data = bytes([buf[3] + 1]) + bytes(buf[4:total_size])
                    else:
                        total_size = 2 + 1 + frame_len
                        if len(buf) < total_size:
                            break
                        data = bytes(buf[3:total_size])

                    buf = buf[total_size:]
                    self._dispatch_binary_packet(data)
                    continue

                break

    def _maybe_log_cdc_diagnostics(self):
        return

class MockCharacteristic:
    def __init__(self, uuid, properties, handle=1):
        self.uuid = uuid
        self.properties = properties
        self.handle = int(handle)

class MockService:
    def __init__(self, uuid, characteristics=None):
        self.uuid = uuid
        self.characteristics = characteristics if characteristics is not None else [
            MockCharacteristic(COMMAND_WRITE_UUID, ["write-without-response"], handle=1),
            MockCharacteristic(INPUT_REPORT_UUID, ["notify"], handle=2),
            MockCharacteristic(COMMAND_RESPONSE_UUID, ["notify"], handle=3),
        ]

class ESP32S3ChannelClient:
    def __init__(self, shared_client: ESP32S3SerialClient, channel: int):
        self.shared_client = shared_client
        self.channel = int(channel)
        # When set (merged Joy-Con pair), rumble writes fan out to both channels in one
        # command so both motors fire in-phase from a single dispatch.
        self.mirror_channel = None
        self._fallback_services = [MockService("ab7de9be-89fe-49ad-828f-118f09df7fd2")]

    @property
    def services(self):
        services = self.shared_client.get_channel_services(self.channel)
        return services or self._fallback_services

    @property
    def is_connected(self):
        return self.shared_client.is_connected

    async def disconnect(self):
        return

    async def stop_notify(self, _uuid):
        await self.shared_client.stop_channel_notify(self.channel, _uuid)

    async def start_notify(self, _uuid, callback):
        await self.shared_client.start_channel_notify(self.channel, _uuid, callback)

    async def write_gatt_char(self, _uuid, data, response=False):
        await self.shared_client.write_channel_gatt_char(self.channel, _uuid, data, response=response)

    def send_manager_command(self, command: str, timeout=2.0):
        return self.shared_client.send_manager_command(command, timeout=timeout)

    def send_fire_and_forget(self, command: str) -> bool:
        return self.shared_client.send_fire_and_forget(command)

    def send_command_line(self, command: str) -> bool:
        return self.shared_client.send_command_line(command)

def scan_serial_ports():
    ports_by_name = {}

    def is_likely_esp32s3(name: str, manufacturer: str, device_id: str):
        haystack = " ".join([name, manufacturer, device_id]).upper()
        return (
            "CH343" in haystack
            or "WCH" in haystack
            or "ESP32" in haystack
            or "USB JTAG" in haystack
            or "VID_1A86&PID_55D3" in haystack
            or "VID_303A&PID_1001" in haystack
            or "VID_303A&PID_4001" in haystack
            or "VID:PID=303A:1001" in haystack
            or "VID:PID=303A:4001" in haystack
            or "303A:1001" in haystack
            or "303A:4001" in haystack
        )

    def is_otg_port(name: str, manufacturer: str, device_id: str):
        # Native USB/OTG port (ESP32-S3 built-in, VID 303A) — cannot be auto-flash via esptool DTR/RTS
        haystack = " ".join([name, manufacturer, device_id]).upper()
        return (
            "VID_303A&PID_1001" in haystack
            or "VID_303A&PID_4001" in haystack
            or "VID:PID=303A:1001" in haystack
            or "VID:PID=303A:4001" in haystack
            or "303A:1001" in haystack
            or "303A:4001" in haystack
            or ("USB JTAG" in haystack and "CH343" not in haystack)
        )

    def add_port(port: str, name: str, manufacturer: str, device_id: str):
        if not port:
            return
        port = port.upper()
        likely = is_likely_esp32s3(name, manufacturer, device_id)
        otg = is_otg_port(name, manufacturer, device_id)
        existing = ports_by_name.get(port)
        if existing:
            ports_by_name[port] = PortInfo(
                port,
                existing.name or name,
                existing.manufacturer or manufacturer,
                existing.device_id or device_id,
                existing.likely_ch343 or likely,
                existing.is_otg or otg,
            )
        else:
            ports_by_name[port] = PortInfo(port, name, manufacturer, device_id, likely, otg)

    # pyserial provides the COM path and hardware ID for normal CDC/CH343 boards.
    # Prefer it at startup: WMI's full PnP traversal is comparatively expensive.
    try:
        from serial.tools import list_ports
        for item in list_ports.comports():
            port = str(getattr(item, "device", "") or getattr(item, "name", "") or "")
            name = str(getattr(item, "description", "") or port)
            manufacturer = str(getattr(item, "manufacturer", "") or "")
            device_id = str(getattr(item, "hwid", "") or "")
            add_port(port, name, manufacturer, device_id)
    except Exception as e:
        logger.debug(f"ESP32-S3 pyserial port scan failed: {e}")
    likely_count = sum(p.likely_ch343 for p in ports_by_name.values())
    if likely_count:
        return sorted(ports_by_name.values(), key=lambda p: int(re.sub(r"\D", "", p.port) or "9999"))

    try:
        wmi = win32com.client.GetObject("winmgmts://./root/cimv2")
        for item in wmi.ExecQuery("SELECT Name,Manufacturer,DeviceID FROM Win32_PnPEntity"):
            name = str(getattr(item, "Name", "") or "")
            match = re.search(r"\((COM\d+)\)", name, re.IGNORECASE)
            if not match:
                continue
            manufacturer = str(getattr(item, "Manufacturer", "") or "")
            device_id = str(getattr(item, "DeviceID", "") or "")
            add_port(match.group(1), name, manufacturer, device_id)
    except Exception as e:
        logger.debug(f"ESP32-S3 serial scan failed: {e}")
    try:
        from serial.tools import list_ports
        for item in list_ports.comports():
            port = str(getattr(item, "device", "") or getattr(item, "name", "") or "")
            name = str(getattr(item, "description", "") or port)
            manufacturer = str(getattr(item, "manufacturer", "") or "")
            device_id = str(getattr(item, "hwid", "") or "")
            add_port(port, name, manufacturer, device_id)
    except Exception as e:
        logger.debug(f"ESP32-S3 pyserial port scan failed: {e}")
    return sorted(ports_by_name.values(), key=lambda p: int(re.sub(r"\D", "", p.port) or "9999"))

def close_all_clients():
    """Close all active ESP32-S3 serial clients so COM port is released for reflashing or replug."""
    for client in list(ACTIVE_CLIENTS):
        try:
            client.close_sync()
        except Exception:
            pass


def shutdown_all_bridges():
    """Bring every active ESP32-S3 bridge to a fully idle state before the app exits.

    Stops scanning, disables firmware auto-connect, and drops all BLE links so no
    controller stays connected after the main program closes. The firmware will not
    resume scanning on the resulting disconnect events because scan_mode is now off.
    """
    for client in list(ACTIVE_CLIENTS):
        try:
            client.send_fire_and_forget("scan off")
            client.send_fire_and_forget("auto off")
            client.send_fire_and_forget("ble disconnect")
        except Exception:
            pass

def _active_client_for_port(port: str):
    port = (port or "").upper()
    for client in list(ACTIVE_CLIENTS):
        if getattr(client, "port", "").upper() == port and getattr(client, "is_connected", False):
            return client
    return None

def send_serial_command(port: str, command="status lite", timeout=1.2):
    active_client = _active_client_for_port(port)
    if active_client is not None:
        return active_client.send_manager_command(command, timeout=timeout)

    handle = None
    try:
        handle = serial.Serial(
            port, baudrate=2000000, timeout=0.1,
            write_timeout=CDC_WRITE_TIMEOUT_SECONDS)
        payload = (command.strip() + "\n").encode("utf-8")
        deadline = time.monotonic() + max(0.05, float(timeout))

        # A healthy, already-running CDC bridge replies immediately.  The legacy
        # path always slept 500 ms after every temporary COM open, and startup
        # performed that probe repeatedly.  Try once right away, retaining the
        # wake/retry path below for a board that is still enumerating.
        handle.dtr = True
        handle.rts = True
        try:
            handle.reset_output_buffer()
        except Exception:
            pass

        def read_until(read_deadline, chunks):
            while time.monotonic() < read_deadline:
                if handle.in_waiting:
                    text = handle.read(handle.in_waiting).decode("utf-8", errors="replace")
                    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
                    chunks.append(text)
                    combined = "".join(chunks)
                    if '"version"' in combined and '"cmd"' in combined:
                        return combined
                time.sleep(0.01)
            return ""

        chunks = []
        handle.write(payload)
        immediate_reply = read_until(
            min(deadline, time.monotonic() + min(0.12, max(0.0, float(timeout)))),
            chunks,
        )
        if immediate_reply:
            return immediate_reply

        # Preserve the firmware boot/CDC settle allowance only after the fast
        # probe missed.  A single monotonic deadline now bounds all retries.
        wake_remaining = STARTUP_STATUS_WAKE_DELAY_SECONDS - 0.12
        if wake_remaining > 0 and time.monotonic() < deadline:
            time.sleep(min(wake_remaining, max(0.0, deadline - time.monotonic())))

        for attempt in range(STATUS_PROBE_ATTEMPTS):
            if time.monotonic() >= deadline:
                break
            try:
                handle.reset_input_buffer()
            except Exception:
                pass

            handle.write(payload)
            reply = read_until(
                min(deadline, time.monotonic() + STARTUP_STATUS_READ_WINDOW_SECONDS),
                chunks,
            )
            if reply:
                return reply

            if attempt < STATUS_PROBE_ATTEMPTS - 1 and time.monotonic() < deadline:
                time.sleep(min(STATUS_PROBE_RETRY_DELAY_SECONDS, max(0.0, deadline - time.monotonic())))
        return "".join(chunks)
    except Exception as e:
        logger.debug(f"ESP32-S3 serial command failed on {port}: {e}")
        return ""
    finally:
        if handle:
            try:
                handle.close()
            except Exception:
                pass

def get_serial_status(port_info: PortInfo | None):
    if not port_info:
        return ""
    text = send_serial_command(port_info.port, "status lite", timeout=1.2)
    if '"version"' in text and '"cmd"' in text:
        return text
    return ""

def _read_json_string(text: str, name: str):
    if not text:
        return ""
    try:
        data = json.loads(text)
        value = data.get(name, "")
        return str(value) if value is not None else ""
    except Exception:
        pass
    matches = re.findall(r'"' + re.escape(name) + r'"\s*:\s*"([^"]*)"', text, re.IGNORECASE)
    return matches[-1] if matches else ""

def _normalize_firmware_version(version: str):
    match = re.search(r"\d+(?:\.\d+){1,3}", version or "")
    return match.group(0) if match else (version or "").strip().lower()

def _app_firmware_version(version: str):
    return _normalize_firmware_version(version)

def get_expected_firmware_version():
    return APP_FIRMWARE_VERSION

def parse_firmware_identity(status_text: str):
    raw_version = _read_json_string(status_text, "version")
    return {
        "version": _app_firmware_version(raw_version),
        "mode": _read_json_string(status_text, "mode"),
        "profile": _read_json_string(status_text, "profile"),
        "build": _read_json_string(status_text, "build"),
    }

def is_expected_firmware(status_text: str, expected_version: str | None = None):
    identity = parse_firmware_identity(status_text)
    version = identity["version"]
    expected = expected_version if expected_version is not None else get_expected_firmware_version()
    return bool(
        version
        and _app_firmware_version(version) == _app_firmware_version(expected)
        and identity["profile"] == EXPECTED_FIRMWARE_PROFILE
        and identity["build"] == EXPECTED_FIRMWARE_BUILD
    )

def detect_bridge():
    serials = scan_serial_ports()
    candidates = [p for p in serials if p.likely_ch343]
    # Prefer CH343P (UART) ports for flashing; OTG ports cannot be auto-flashed via DTR/RTS
    ch343_candidates = [p for p in candidates if not p.is_otg]
    otg_candidates = [p for p in candidates if p.is_otg]
    otg_only = bool(otg_candidates and not ch343_candidates)

    flash_candidates = ch343_candidates if ch343_candidates else candidates
    serial_port = None
    serial_status_text = ""

    for candidate in flash_candidates:
        status_text = get_serial_status(candidate)
        if status_text:
            serial_port = candidate
            serial_status_text = status_text
            break

    if serial_port is None and flash_candidates:
        serial_port = flash_candidates[0]

    expected_version = get_expected_firmware_version()
    identity = parse_firmware_identity(serial_status_text)

    firmware_installed = bool(serial_status_text)
    firmware_current = bool(firmware_installed and is_expected_firmware(serial_status_text, expected_version))
    # OTG CDC port is a valid bridge transport; only auto-flashing is restricted for OTG
    bridge_ready = bool(serial_port and firmware_current)
    usb_present = bool(serial_port)
    firmware_update_required = bool(serial_port and firmware_installed and not firmware_current)
    return BridgeStatus(
        serial_port,
        firmware_installed,
        serial_status_text,
        identity["version"],
        identity["mode"],
        identity["profile"],
        expected_version,
        firmware_current,
        firmware_update_required,
        bridge_ready,
        usb_present,
        serial_port.port if serial_port else None,
        otg_only,
    )

class ESP32S3Controller(Controller):
    # Bridge round-trips (PC → USB serial → ESP32 → BLE → controller → BLE → ESP32 →
    # USB serial → PC) are slower than direct BLE, especially when other controllers
    # are active. Use a longer command timeout to reduce spurious init failures.
    COMMAND_TIMEOUT: float = 4.0

    def __init__(self, port: str, channel: int = 0, shared_client: ESP32S3SerialClient | None = None):
        channel = int(channel)
        super().__init__(DummyBleDevice(f"ESP32-S3-N16R8-CH{channel + 1}", ESP32S3_LABEL))
        self.port = port
        self.hid_path = port
        self.channel = channel
        self.shared_client = shared_client
        self._owns_client = shared_client is None
        self.client = ESP32S3SerialClient(port) if shared_client is None else ESP32S3ChannelClient(shared_client, channel)
        self.controller_info = ControllerInfo.__new__(ControllerInfo)
        self.controller_info.serial_number = f"ESP32S3-N16R8-CH{channel + 1}"
        self.controller_info.vendor_id = NINTENDO_VENDOR_ID
        self.controller_info.product_id = PRO_CONTROLLER2_PID
        self.controller_info.color1 = b"\x00\x00\x00"
        self.controller_info.color2 = b"\xff\xff\xff"
        self.controller_info.color3 = b"\x00\xc3\xe3"
        self.controller_info.color4 = b"\xff\xff\xff"
        self.stick_calibration = StickCalibrationData(DEFAULT_STICK_CALIBRATION)
        self.second_stick_calibration = StickCalibrationData(DEFAULT_STICK_CALIBRATION)
        self.left_stick_calibration = self.stick_calibration
        self.right_stick_calibration = self.second_stick_calibration
        self.side_buttons_pressed = False
        self.battery_voltage = 3.7
        self.is_esp32s3_bridge = True

    def _read_stopped(self):
        client = self.shared_client if self.shared_client is not None else self.client
        read_stop = getattr(client, "_read_stop", None)
        return bool(read_stop and read_stop.is_set())

    async def initialize(self):
        if hasattr(self.client, "open"):
            self.client.open()
        elif self.shared_client:
            self.shared_client.open()
            
        await super().initialize()
        
    async def connect(self, quit_event=None):
        await self.initialize()
        self.polling_task = asyncio.create_task(self._poll_status())

    async def _poll_status(self):
        while self.interp_running and self.client and not self._read_stopped():
            await asyncio.sleep(1.0)
            if not self.client or not self.interp_running:
                break
            if getattr(self, 'last_input_time', 0) > 0 and time.time() - self.last_input_time < 3.0:
                continue
            
            try:
                reply = await asyncio.to_thread(self.client.send_manager_command, "status lite", timeout=0.5)
            except Exception:
                reply = ""
            try:
                status = json.loads(reply) if reply else {}
                channel_mask = int(status.get("ble_channels", 0))
            except Exception:
                channel_mask = 0
            if not reply or not (channel_mask & (1 << self.channel)):
                if getattr(self, "disconnected_callback", None):
                    logger.info("ESP32-S3 BLE controller disconnected from bridge.")
                    asyncio.create_task(self.disconnected_callback(self))
                break

    async def disconnect(self):
        self.interp_running = False
        if hasattr(self, "polling_task") and self.polling_task:
            self.polling_task.cancel()
        if hasattr(self, "interp_thread") and self.interp_thread.is_alive():
            self.interp_thread.join(timeout=0.5)
        if self.client:
            try:
                if self._owns_client:
                    # Standalone single-controller client owns the whole bridge.
                    self.client.send_fire_and_forget("auto off")
                    self.client.send_fire_and_forget("ble disconnect")
                else:
                    # Shared bridge client: drop only THIS controller's BLE channel so
                    # the firmware actually disconnects the physical controller instead
                    # of keeping it connected (the channel is then free to be re-detected).
                    self.shared_client.send_fire_and_forget(f"disc {self.channel}")
            except Exception:
                logger.debug("ESP32-S3 BLE disconnect command failed", exc_info=True)
            if self._owns_client:
                await self.client.disconnect()
            self.client = None


    async def set_vibration(self, vibration: VibrationData, *args, **kwargs):
        return await Controller.set_vibration(self, vibration, *args, **kwargs)

    async def play_vibration_preset(self, preset_id: int):
        del preset_id
        return


async def create_esp32s3_controller(quit_event=None):
    bridge = detect_bridge()
    if not bridge or not bridge.serial_port:
        raise RuntimeError("ESP32-S3 N16R8 firmware COM port was not detected.")
    controller = ESP32S3Controller(bridge.serial_port.port)
    await controller.connect(quit_event)
    return controller

import subprocess

def get_firmware_root():
    return get_driver_path(os.path.join("esp32s3", "firmware", "v5.9"))

def get_esptool_path():
    return get_driver_path(os.path.join("esp32s3", "tools", "esptool.exe"))

def load_firmware_manifest():
    manifest_path = os.path.join(get_firmware_root(), "firmware_manifest.json")
    with open(manifest_path, "r", encoding="utf-8-sig") as f:
        return json.load(f)

def _run_esptool(args, progress=None, timeout=None):
    esptool = get_esptool_path()
    if not os.path.exists(esptool):
        raise FileNotFoundError(f"Missing esptool.exe: {esptool}")
    cmd = [esptool] + args
    if progress:
        progress("esptool " + " ".join(args))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    lines = []
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            lines.append(line)
            if progress:
                progress(line)
                match = re.search(r"\((\d{1,3})\s*%\)", line)
                if match:
                    percent = max(0, min(100, int(match.group(1))))
                    progress({"write_percent": percent, "message": line})
        proc.wait(timeout=timeout)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        raise
    output = "\n".join(lines)
    if proc.returncode != 0:
        raise RuntimeError(f"esptool failed with exit code {proc.returncode}\n{output}")
    return output

def _common_esptool_args(port: str, baud: int, no_stub: bool, command: str, before="default_reset", after="hard_reset"):
    args = ["--chip", "esp32s3"]
    if no_stub:
        args.append("--no-stub")
    args += ["-p", port, "-b", str(baud), "--before", before, "--after", after, command]
    return args

def _serial_recovery_hint(port: str, detail: str):
    return (
        f"Could not put ESP32-S3 N16R8 on {port} into flashing mode.\n\n"
        "Try these steps:\n"
        "1. Use the CH343P/UART Type-C port for flashing, not the native USB/OTG HID port.\n"
        "2. Unplug and reconnect the board, then try Repair.\n"
        "3. If it still times out: hold BOOT/IO0, tap RESET/EN once, release BOOT after the log shows Connecting.\n"
        "4. Check that no serial monitor, ESP-IDF monitor, or other app is using the same COM port.\n\n"
        f"Last esptool output:\n{detail}"
    )

def _run_esptool_attempts(attempts, progress=None):
    last_error = None
    for label, args in attempts:
        if progress:
            progress(f"{ESP32S3_LABEL}: {label}")
        try:
            return _run_esptool(args, progress=progress)
        except Exception as e:
            last_error = e
            if progress:
                progress(f"{ESP32S3_LABEL}: {label} failed: {str(e).splitlines()[0]}")
            time.sleep(0.5)
    raise last_error

def _validate_firmware_assets(manifest, firmware_root, profile):
    """Return verified flash assets and fail before esptool can modify a device."""
    root = os.path.realpath(firmware_root)
    assets = profile.get("assets", [])
    if not isinstance(assets, list) or not assets:
        raise RuntimeError(f"Firmware profile has no assets: {profile.get('id', '<unknown>')}")

    expected_offsets = {0x0, 0x8000, 0x10000}
    offsets = set()
    verified = []
    for asset in assets:
        relative_path = asset.get("path")
        expected_hash = str(asset.get("sha256", "")).lower()
        try:
            offset = int(str(asset.get("offset", "")), 0)
        except ValueError as e:
            raise RuntimeError(f"Invalid firmware asset offset: {asset.get('offset')}") from e
        if not relative_path or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise RuntimeError(f"Invalid firmware asset metadata at offset 0x{offset:X}")
        path = os.path.realpath(os.path.join(root, relative_path.replace("/", os.sep)))
        try:
            if os.path.commonpath([root, path]) != root:
                raise RuntimeError(f"Firmware asset is outside the firmware directory: {relative_path}")
        except ValueError as e:
            raise RuntimeError(f"Invalid firmware asset path: {relative_path}") from e
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        digest = hashlib.sha256()
        with open(path, "rb") as binary:
            for chunk in iter(lambda: binary.read(1024 * 1024), b""):
                digest.update(chunk)
        actual_hash = digest.hexdigest()
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"Firmware asset checksum mismatch: {relative_path} "
                f"(expected {expected_hash}, got {actual_hash})"
            )
        if offset in offsets:
            raise RuntimeError(f"Duplicate firmware asset offset: 0x{offset:X}")
        offsets.add(offset)
        verified.append({"offset": asset["offset"], "path": path})

    if profile.get("id") == EXPECTED_FIRMWARE_PROFILE and offsets != expected_offsets:
        raise RuntimeError("Production firmware profile must contain 0x0, 0x8000, and 0x10000 assets")
    return verified


def _write_flash_args(manifest, firmware_root, profile, port, baud, no_stub,
                      before="default_reset", verified_assets=None):
    args = _common_esptool_args(port, baud, no_stub, "write_flash", before=before)
    args += ["--flash_mode", manifest.get("flashMode", "dio")]
    args += ["--flash_freq", manifest.get("flashFreq", "80m")]
    args += ["--flash_size", manifest.get("flashSize", "16MB")]
    for asset in verified_assets if verified_assets is not None else _validate_firmware_assets(manifest, firmware_root, profile):
        path = asset["path"]
        args += [asset["offset"], path]
    return args


def release_port(port: str):
    logger.info(f"Releasing COM port {port} before flashing...")
    for client in list(ACTIVE_CLIENTS):
        if client.port == port:
            try:
                client.close_sync()
            except Exception as e:
                logger.debug(f"Failed to close client on {port}: {e}")


def flash_firmware(port: str, mode="install", profile_id=EXPECTED_FIRMWARE_PROFILE, progress=None):
    release_port(port)
    time.sleep(0.5) # Give the OS time to release the handle completely
    
    manifest = load_firmware_manifest()
    firmware_root = get_firmware_root()
    baud = 115200 if mode == "repair" else 460800
    no_stub = mode == "repair"

    if progress:
        progress({"percent": 3, "message": f"Checking chip on {port}"})
        progress(f"{ESP32S3_LABEL}: checking chip on {port}")
    try:
        chip_output = _run_esptool_attempts([
            ("chip check using automatic reset", _common_esptool_args(port, baud, no_stub, "chip_id")),
            ("chip check using 115200 baud recovery", _common_esptool_args(port, 115200, False, "chip_id")),
            ("chip check using manual bootloader mode", _common_esptool_args(port, 115200, False, "chip_id", before="no_reset")),
        ], progress=progress)
    except Exception as e:
        raise RuntimeError(_serial_recovery_hint(port, str(e))) from e
    try:
        chip_text = chip_output.upper().replace("_", "-")
    except Exception:
        chip_text = ""
    if "ESP32-S3" not in chip_text:
        raise RuntimeError("The selected serial port did not identify as ESP32-S3.")
    if progress:
        progress({"percent": 15, "message": "ESP32-S3 detected"})

    if mode in ("delete", "erase"):
        if progress:
            progress({"percent": 30, "message": "Erasing flash"})
            progress(f"{ESP32S3_LABEL}: erasing flash")
        try:
            _run_esptool_attempts([
                ("erase using automatic reset (stub)", _common_esptool_args(port, 460800, False, "erase_flash")),
                ("erase using manual bootloader mode (stub)", _common_esptool_args(port, 115200, False, "erase_flash", before="no_reset")),
                ("erase using 115200 baud recovery (stub)", _common_esptool_args(port, 115200, False, "erase_flash")),
            ], progress=progress)
        except Exception as e:
            raise RuntimeError(_serial_recovery_hint(port, str(e))) from e
        if progress:
            progress({"percent": 100, "message": "Erase completed"})
        return

    profile = next((p for p in manifest.get("profiles", []) if p.get("id") == profile_id), None)
    if profile is None:
        raise RuntimeError(f"Firmware profile not found: {profile_id}")
    verified_assets = _validate_firmware_assets(manifest, firmware_root, profile)

    if progress:
        progress({"percent": 25, "message": f"Flashing {profile.get('label', profile_id)}"})
        progress(f"{ESP32S3_LABEL}: flashing {profile.get('label', profile_id)}")
    try:
        _run_esptool_attempts([
            ("write flash using automatic reset", _write_flash_args(manifest, firmware_root, profile, port, baud, no_stub, verified_assets=verified_assets)),
            ("write flash using 115200 baud recovery", _write_flash_args(manifest, firmware_root, profile, port, 115200, False, verified_assets=verified_assets)),
            ("write flash using manual bootloader mode", _write_flash_args(manifest, firmware_root, profile, port, 115200, False, before="no_reset", verified_assets=verified_assets)),
        ], progress=progress)
    except Exception as e:
        raise RuntimeError(_serial_recovery_hint(port, str(e))) from e
    if progress:
        # Deliberately match the 1.1 completion boundary: a successful esptool write
        # is installation success. CDC identity is checked after the user re-plugs,
        # when Windows has finished re-enumerating the device.
        progress({"percent": 100, "message": "Firmware flashing completed"})
