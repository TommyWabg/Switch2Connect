import asyncio
import json
import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass

import win32com.client
import serial

from config import get_driver_path, CONFIG
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
STATUS_PROBE_ATTEMPTS = 3
STATUS_PROBE_RETRY_DELAY_SECONDS = 0.15
STARTUP_STATUS_WAKE_DELAY_SECONDS = 0.5
STARTUP_STATUS_READ_WINDOW_SECONDS = 0.5

ESP32S3_LABEL = "ESP32-S3 CDC"
APP_FIRMWARE_VERSION = "0.11.2"
EXPECTED_FIRMWARE_PROFILE = "tinyusb_direct"
EXPECTED_FIRMWARE_BUILD = "cdc_bridge_1"
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
        self._write_lock = threading.Lock()
        self._write_count = 0
        self._response_queue = queue.Queue()
        self._closed_by_error = False
        ACTIVE_CLIENTS.append(self)

    def open(self):
        if self.is_connected:
            return
        if self._closed_by_error:
            raise OSError(f"ESP32-S3 port {self.port} was closed after a hardware disconnect; reconnect required")
        try:
            self.handle = serial.Serial(self.port, baudrate=2000000, timeout=0.1)
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
                    new_handle = serial.Serial(self.port, baudrate=2000000, timeout=0.1)
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
        self.is_connected = False
        if self.handle:
            try:
                self.handle.close()
            except Exception:
                pass
            self.handle = None
                
        if self._read_thread and self._read_thread.is_alive():
            self._read_thread.join(timeout=0.5)
            
        if self in ACTIVE_CLIENTS:
            ACTIVE_CLIENTS.remove(self)

    async def stop_notify(self, _uuid):
        self._read_stop.set()

    async def start_channel_notify(self, channel, uuid, callback):
        self.open()
        if channel not in self._notify_callbacks:
            self._notify_callbacks[int(channel)] = {}
        self._notify_callbacks[int(channel)][uuid] = callback
        self._ensure_read_thread()
        
    async def stop_channel_notify(self, channel, uuid):
        if channel in self._notify_callbacks:
            self._notify_callbacks[int(channel)].pop(uuid, None)

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
                return self._response_queue.get(timeout=timeout)
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
        """Push the latest rumble payload for one channel into the firmware shadow.

        Format: 'rs <ch> <hex>'.  The firmware stores this as the channel's latest
        rumble and a dedicated firmware task re-sends it to BLE at a steady,
        hardware-timed cadence (stamping a fresh packet-id each write).  The host
        therefore only sends when the rumble value CHANGES; the firmware owns the
        sustain, so rumble smoothness no longer depends on host/OS scheduling jitter.
        Requires firmware with the 'shadow' feature (v0.11.3+).
        """
        hexd = bytes(data).hex()
        cmd = f"rs {int(channel)} {hexd}\n"
        with self._write_lock:
            self.open()
            self._ensure_read_thread()
            try:
                self.handle.write(cmd.encode("ascii"))
                return True
            except Exception as e:
                logger.debug("Failed to write ESP32-S3 rs payload: %s", e)
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
            if ('"cmd":"scan_result"' in text or '"cmd":"connected"' in text
                    or '"cmd":"connect_fail"' in text or '"cmd":"connect_busy"' in text
                    or '"cmd":"disconnected"' in text):
                if self.event_callback:
                    try:
                        self.event_callback(json.loads(text))
                    except Exception:
                        pass
                # These are event-only; never go into the command response queue
                return
            if '"cmd":"debug"' in text:
                # Firmware debug messages must not pollute the status response queue:
                # send_manager_command("status lite") would return the debug JSON, parse
                # ble_channels=0, and falsely trigger a multi-controller disconnect.
                logger.debug("ESP32-S3 firmware debug: %s", text)
                
                
                try:
                    debug_data = json.loads(text)
                    print(f"[BLE Callback] {debug_data.get('msg')}")
                except Exception:
                    print(f"[BLE Callback] {text}")
                
                return
            self._response_queue.put(text)

    def _dispatch_binary_packet(self, data):
        if not data:
            return

        self.last_input_time = time.time()

        # High bit of the channel byte flags a command/ack response (vs an input
        # report). Command/ack notifications must NOT reach the input parser or they
        # are misparsed as random buttons/sticks on connect.
        is_command = bool(data[0] & 0x80)
        chan_id = data[0] & 0x7F

        if 1 <= chan_id <= MAX_ESP32S3_CHANNELS:
            channel = chan_id - 1
            report_payload = data[1:]

            channel_callbacks = self._notify_callbacks.get(channel, {})
            for uuid, cb in list(channel_callbacks.items()):
                # Route command/ack frames away from the input characteristic's
                # callback (the SW2 input stream lives on the ab7de9be… UUID).
                if is_command and str(uuid).lower().startswith(INPUT_UUID_PREFIX):
                    continue
                if not is_command and not str(uuid).lower().startswith(INPUT_UUID_PREFIX):
                    continue
                try:
                    cb(None, bytearray(report_payload))
                except Exception:
                    logger.exception("ESP32-S3 Serial input callback failed channel=%d", channel)
            return

        if data[0] == NINTENDO_INPUT_REPORT_ID:
            data = data[1:]
        
        channel_0_callbacks = self._notify_callbacks.get(0, {})
        for cb in list(channel_0_callbacks.values()):
            try:
                cb(None, bytearray(data))
            except Exception:
                logger.exception("ESP32-S3 Serial input callback failed")

    def _read_loop(self):
        buf = bytearray()
        while not self._read_stop.is_set() and self.is_connected and self.handle:
            try:
                if self.handle.in_waiting:
                    chunk = self.handle.read(self.handle.in_waiting or 1024)
                    if chunk:
                        buf.extend(chunk)
                else:
                    chunk = self.handle.read(1)
                    if chunk:
                        buf.extend(chunk)
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
                    self._queue_text_response(line)
                    continue

                if hdr_idx > 0:
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

class MockCharacteristic:
    def __init__(self, uuid, properties):
        self.uuid = uuid
        self.properties = properties
        self.handle = 1

class MockService:
    def __init__(self, uuid):
        self.uuid = uuid
        self.characteristics = [
            MockCharacteristic(COMMAND_WRITE_UUID, ["write-without-response"]),
            MockCharacteristic(INPUT_REPORT_UUID, ["notify"]),
            MockCharacteristic(COMMAND_RESPONSE_UUID, ["notify"]),
        ]

class ESP32S3ChannelClient:
    def __init__(self, shared_client: ESP32S3SerialClient, channel: int):
        self.shared_client = shared_client
        self.channel = int(channel)
        # When set (merged Joy-Con pair), rumble writes fan out to both channels in one
        # command so both motors fire in-phase from a single dispatch.
        self.mirror_channel = None
        self.services = [MockService("ab7de9be-89fe-49ad-828f-118f09df7fd2")]

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
            client.send_manager_command("scan off", timeout=0.5)
            client.send_manager_command("auto off", timeout=0.5)
            client.send_manager_command("ble disconnect", timeout=0.8)
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
        handle = serial.Serial(port, baudrate=2000000, timeout=0.1)
        payload = (command.strip() + "\n").encode("utf-8")

        handle.dtr = True
        handle.rts = True
        time.sleep(STARTUP_STATUS_WAKE_DELAY_SECONDS)
        try:
            handle.reset_output_buffer()
        except Exception:
            pass

        for attempt in range(STATUS_PROBE_ATTEMPTS):
            try:
                handle.reset_input_buffer()
            except Exception:
                pass

            handle.write(payload)
            chunks = []
            deadline = time.time() + STARTUP_STATUS_READ_WINDOW_SECONDS
            while time.time() < deadline:
                if handle.in_waiting:
                    text = handle.read(handle.in_waiting).decode("utf-8", errors="replace")
                    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
                    chunks.append(text)
                    combined = "".join(chunks)
                    if '"version"' in combined and '"cmd"' in combined:
                        return combined
                time.sleep(0.02)

            if attempt < STATUS_PROBE_ATTEMPTS - 1:
                time.sleep(STATUS_PROBE_RETRY_DELAY_SECONDS)
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
                    self.client.send_manager_command("auto off", timeout=0.5)
                    self.client.send_manager_command("ble disconnect", timeout=0.8)
                else:
                    # Shared bridge client: drop only THIS controller's BLE channel so
                    # the firmware actually disconnects the physical controller instead
                    # of keeping it connected (the channel is then free to be re-detected).
                    self.shared_client.send_manager_command(f"disc {self.channel}", timeout=0.5)
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

def _write_flash_args(manifest, firmware_root, profile, port, baud, no_stub, before="default_reset"):
    args = _common_esptool_args(port, baud, no_stub, "write_flash", before=before)
    args += ["--flash_mode", manifest.get("flashMode", "dio")]
    args += ["--flash_freq", manifest.get("flashFreq", "80m")]
    args += ["--flash_size", manifest.get("flashSize", "16MB")]
    for asset in profile.get("assets", []):
        path = os.path.join(firmware_root, asset["path"].replace("/", os.sep))
        if not os.path.exists(path):
            raise FileNotFoundError(path)
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

    if progress:
        progress({"percent": 25, "message": f"Flashing {profile.get('label', profile_id)}"})
        progress(f"{ESP32S3_LABEL}: flashing {profile.get('label', profile_id)}")
    try:
        _run_esptool_attempts([
            ("write flash using automatic reset", _write_flash_args(manifest, firmware_root, profile, port, baud, no_stub)),
            ("write flash using 115200 baud recovery", _write_flash_args(manifest, firmware_root, profile, port, 115200, False)),
            ("write flash using manual bootloader mode", _write_flash_args(manifest, firmware_root, profile, port, 115200, False, before="no_reset")),
        ], progress=progress)
    except Exception as e:
        raise RuntimeError(_serial_recovery_hint(port, str(e))) from e
    if progress:
        progress({"percent": 100, "message": "Firmware flashing completed"})
