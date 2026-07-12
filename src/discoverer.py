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

"""A class used to find switch 2 controllers via Bluetooth
"""
import threading
from bleak import BleakScanner, BleakClient, BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError
import asyncio
import logging
import json
import bluetooth
import yaml
from utils import to_hex, convert_mac_string_to_value, decodeu, show_notification
import time
from controller import Controller, ControllerInputData, NINTENDO_VENDOR_ID, CONTROLER_NAMES, VibrationData, NSO_GAMECUBE_CONTROLLER_PID
from virtual_controller import VirtualController
from config import CONFIG

logger = logging.getLogger(__name__)

NINTENDO_BLUETOOTH_MANUFACTURER_ID = 0x0553
VIRTUAL_CONTROLLERS = [None] * 10
UPDATE_CALLBACK = None
DISCOVERER_LOOP = None
DISCONNECT_CALLBACK = None
IS_SHUTTING_DOWN = False
DISCOVERY_LOCK = threading.Lock()
_CURRENTLY_DISCOVERING = False
_IS_SUSPENDING = False
GLOBAL_LOCK = None
CONNECTION_LOCK = None
# Set to False while the BLE scanner is in the error-retry loop (Bluetooth off/unavailable).
# The GUI header reads this to show "Disconnect" instead of "Ready" for the system BLE route.
_SYSTEM_BT_AVAILABLE = True

def is_system_bluetooth_available() -> bool:
    return _SYSTEM_BT_AVAILABLE

async def auto_disconnect_checker(quit_event):
    logger.info("Auto disconnect checker task started.")
    while not quit_event.is_set():
        try:
            await asyncio.sleep(1.0)
            if not getattr(CONFIG, "auto_disconnect_enabled", False):
                continue
            
            days = getattr(CONFIG, "auto_disconnect_days", 0)
            hours = getattr(CONFIG, "auto_disconnect_hours", 0)
            minutes = getattr(CONFIG, "auto_disconnect_minutes", 0)
            
            timeout = (days * 86400) + (hours * 3600) + (minutes * 60)
            if timeout <= 0:
                continue
                
            now = time.time()
            
            mode = getattr(CONFIG, "auto_disconnect_mode", "Absolute")
            for vc in VIRTUAL_CONTROLLERS:
                if vc is not None and getattr(vc, 'running', False):
                    should_disconnect = False
                    for c in vc.controllers:
                        if mode == "Inactive":
                            last_input = getattr(c, 'last_input_time', None)
                            if last_input is not None and (now - last_input) >= timeout:
                                should_disconnect = True
                                break
                        else: # Absolute
                            connected_at = getattr(c, 'connected_at', None)
                            if connected_at is not None and (now - connected_at) >= timeout:
                                should_disconnect = True
                                break
                    if should_disconnect:
                        if mode == "Inactive":
                            logger.info(f"Auto Disconnect: Player {vc.player_number} inactivity duration exceeded limit. Disconnecting...")
                            vc.trigger_disconnect()
                        else:
                            logger.info(f"Auto Disconnect: Player {vc.player_number} connection duration exceeded limit. Disconnecting...")
                            vc.trigger_disconnect()
        except Exception as e:
            logger.error(f"Error in auto_disconnect_checker: {e}")

async def run_discovery(update_controllers_threadsafe, quit_event):
    global VIRTUAL_CONTROLLERS, UPDATE_CALLBACK, DISCOVERER_LOOP, DISCONNECT_CALLBACK, _CURRENTLY_DISCOVERING
    global GLOBAL_LOCK, CONNECTION_LOCK
    
    with DISCOVERY_LOCK:
        if _CURRENTLY_DISCOVERING:
            logger.warning("Discovery already running. Skipping...")
            return
"""A class used to find switch 2 controllers via Bluetooth
"""
import threading
from bleak import BleakScanner, BleakClient, BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError
import asyncio
import logging
import json
import bluetooth
import yaml
from utils import to_hex, convert_mac_string_to_value, decodeu, show_notification
import time
from controller import Controller, ControllerInputData, NINTENDO_VENDOR_ID, CONTROLER_NAMES, VibrationData, NSO_GAMECUBE_CONTROLLER_PID
from virtual_controller import VirtualController
from config import CONFIG

logger = logging.getLogger(__name__)

NINTENDO_BLUETOOTH_MANUFACTURER_ID = 0x0553
VIRTUAL_CONTROLLERS = [None] * 10
UPDATE_CALLBACK = None
DISCOVERER_LOOP = None
DISCONNECT_CALLBACK = None
IS_SHUTTING_DOWN = False
DISCOVERY_LOCK = threading.Lock()
_CURRENTLY_DISCOVERING = False
_IS_SUSPENDING = False
GLOBAL_LOCK = None
CONNECTION_LOCK = None
# Set to False while the BLE scanner is in the error-retry loop (Bluetooth off/unavailable).
# The GUI header reads this to show "Disconnect" instead of "Ready" for the system BLE route.
_SYSTEM_BT_AVAILABLE = True

def is_system_bluetooth_available() -> bool:
    return _SYSTEM_BT_AVAILABLE

async def auto_disconnect_checker(quit_event):
    logger.info("Auto disconnect checker task started.")
    while not quit_event.is_set():
        try:
            await asyncio.sleep(1.0)
            if not getattr(CONFIG, "auto_disconnect_enabled", False):
                continue
            
            days = getattr(CONFIG, "auto_disconnect_days", 0)
            hours = getattr(CONFIG, "auto_disconnect_hours", 0)
            minutes = getattr(CONFIG, "auto_disconnect_minutes", 0)
            
            timeout = (days * 86400) + (hours * 3600) + (minutes * 60)
            if timeout <= 0:
                continue
                
            now = time.time()
            
            mode = getattr(CONFIG, "auto_disconnect_mode", "Absolute")
            for vc in VIRTUAL_CONTROLLERS:
                if vc is not None and getattr(vc, 'running', False):
                    should_disconnect = False
                    for c in vc.controllers:
                        if mode == "Inactive":
                            last_input = getattr(c, 'last_input_time', None)
                            if last_input is not None and (now - last_input) >= timeout:
                                should_disconnect = True
                                break
                        else: # Absolute
                            connected_at = getattr(c, 'connected_at', None)
                            if connected_at is not None and (now - connected_at) >= timeout:
                                should_disconnect = True
                                break
                    if should_disconnect:
                        if mode == "Inactive":
                            logger.info(f"Auto Disconnect: Player {vc.player_number} inactivity duration exceeded limit. Disconnecting...")
                            vc.trigger_disconnect()
                        else:
                            logger.info(f"Auto Disconnect: Player {vc.player_number} connection duration exceeded limit. Disconnecting...")
                            vc.trigger_disconnect()
        except Exception as e:
            logger.error(f"Error in auto_disconnect_checker: {e}")

async def run_discovery(update_controllers_threadsafe, quit_event):
    global VIRTUAL_CONTROLLERS, UPDATE_CALLBACK, DISCOVERER_LOOP, DISCONNECT_CALLBACK, _CURRENTLY_DISCOVERING
    global GLOBAL_LOCK, CONNECTION_LOCK, _SYSTEM_BT_AVAILABLE

    with DISCOVERY_LOCK:
        if _CURRENTLY_DISCOVERING:
            logger.warning("Discovery already running. Skipping...")
            return
        _CURRENTLY_DISCOVERING = True

    usb_hid_task = None
    try:
        UPDATE_CALLBACK = update_controllers_threadsafe
        DISCOVERER_LOOP = asyncio.get_running_loop()
    
        GLOBAL_LOCK = asyncio.Lock()
        CONNECTION_LOCK = asyncio.Lock()
        connected_mac_addresses: list[str] = []
    
        logger.info("Discovery starting: Performing initial cleanup of stale controllers...")
        for i, vc in enumerate(VIRTUAL_CONTROLLERS):
            if vc is not None:
                try:
                    # Force disconnect and destruction of virtual device
                    await vc.disconnect(is_suspending=False)
                except Exception as e:
                    logger.error(f"Error in initial cleanup of controller {i}: {e}")
                VIRTUAL_CONTROLLERS[i] = None
            
        # Detach all possible USBIP ports to clear stale attachments
        try:
            from virtual_controller import detach_all_usbip_devices
            detach_all_usbip_devices()
        except Exception as e:
            logger.error(f"Error in initial USBIP port cleanup: {e}")
    
        if UPDATE_CALLBACK:
            UPDATE_CALLBACK(list(VIRTUAL_CONTROLLERS))

        # Wired USB controllers (e.g. Pro Controller 2) run on an independent transport,
        # so watch for them concurrently with whichever BLE route is chosen below.
        usb_hid_task = asyncio.create_task(run_usb_hid_discovery(quit_event))

        try:
            import usb_serial_bridge as _usb_serial_bridge_mod
            from usb_serial_bridge import (
                detect_bridge,
                create_esp32s3_controller,
                ESP32S3Controller,
                ESP32S3SerialClient,
                ESP32S3_LABEL,
                MAX_ESP32S3_CHANNELS,
                MAX_ESP32S3_GROUPS,
            )
            bridge = detect_bridge()
        except Exception as e:
            bridge = None
            logger.debug(f"ESP32-S3 bridge detection failed: {e}")

        logger.info(
            "Controller connection route check: ESP32-S3 present=%s firmware_current=%s transport_ready=%s version=%s serial=%s usb=%s",
            bool(bridge and bridge.board_present),
            bool(bridge and bridge.firmware_current),
            bool(bridge and getattr(bridge, "bridge_ready", False)),
            getattr(bridge, "firmware_version", "") if bridge else "",
            getattr(getattr(bridge, "serial_port", None), "port", "") if bridge else "",
            bool(bridge and getattr(bridge, "usb_present", False)),
        )

        if bridge and bridge.firmware_current and not getattr(bridge, "bridge_ready", False) and not getattr(bridge, "otg_only", False):
            logger.info(f"{ESP32S3_LABEL} firmware is installed, waiting for USB CDC transport before using system bluetooth.")
            for attempt in range(24):
                if quit_event.is_set():
                    break
                await asyncio.sleep(0.5)
                try:
                    bridge = detect_bridge()
                except Exception as e:
                    bridge = None
                    logger.debug(f"ESP32-S3 bridge retry detection failed: {e}")
                if bridge and getattr(bridge, "bridge_ready", False):
                    logger.info(f"{ESP32S3_LABEL} USB CDC transport became ready after {(attempt + 1) * 0.5:.1f}s.")
                    break
            if bridge and not getattr(bridge, "bridge_ready", False):
                logger.warning(
                    "%s firmware is installed but USB CDC transport is not ready. Falling back to system bluetooth.",
                    ESP32S3_LABEL,
                )

        if bridge and getattr(bridge, "bridge_ready", False) and bridge.serial_port:
            logger.info(f"Controller connection route: ESP32-S3 USB CDC ({ESP32S3_LABEL})")
            fallback_to_system_bluetooth = False
        
            while not quit_event.is_set():
                checker_task = None
                shared_client = None
                worker_task = None
                try:
                    try:
                        current_bridge = detect_bridge()
                    except Exception as e:
                        current_bridge = None
                        logger.debug("ESP32-S3 bridge redetection failed before route start: %s", e)

                    # The bridge was already positively identified before
                    # entering this route. During OTG reconnects, a second
                    # status probe can briefly miss the JSON reply even though
                    # the COM transport is usable. Keep the known-good port
                    # unless the port disappears entirely.
                    if current_bridge and current_bridge.serial_port:
                        if getattr(current_bridge, "bridge_ready", False):
                            bridge = current_bridge
                        elif getattr(bridge, "serial_port", None) and current_bridge.serial_port.port == bridge.serial_port.port:
                            logger.debug(
                                "%s status not ready on redetection; keeping known port %s.",
                                ESP32S3_LABEL,
                                bridge.serial_port.port,
                            )
                        else:
                            bridge = current_bridge
                    elif not getattr(bridge, "serial_port", None):
                        logger.info("%s USB CDC transport is no longer available. Falling back to system bluetooth.", ESP32S3_LABEL)
                        fallback_to_system_bluetooth = True
                        break

                    shared_client = ESP32S3SerialClient(bridge.serial_port.port)
                    try:
                        open_success = False
                        for _ in range(5):
                            try:
                                shared_client.open()
                                open_success = True
                                break
                            except PermissionError:
                                time.sleep(0.5)
                        if not open_success:
                            shared_client.open() # Try one last time to throw the exception if still failing
                    except OSError as e:
                        logger.warning(
                            "%s USB CDC transport could not be opened: %s. Falling back to system bluetooth.",
                            ESP32S3_LABEL,
                            e,
                        )
                        fallback_to_system_bluetooth = True
                        break
                    checker_task = asyncio.create_task(auto_disconnect_checker(quit_event))

                    async def esp32_disconnected_controller(controller: Controller):
                        ch = getattr(controller, 'channel', None)
                        logger.info(f"{ESP32S3_LABEL} disconnected channel={ch}")

                        # Issue 3: actively tell the firmware to drop this channel's BLE
                        # link. The callback fires both when the firmware already lost the
                        # link (no-op on the firmware side) and when the main program
                        # decides to disconnect (user removal / auto-disconnect / shutdown).
                        # Sending "disc <ch>" is safe either way and frees the firmware
                        # channel so the controller can be re-detected later (issue 4).
                        if ch is not None and shared_client is not None:
                            try:
                                await asyncio.to_thread(
                                    shared_client.send_manager_command, f"disc {ch}", timeout=0.5
                                )
                            except Exception:
                                logger.debug("Failed to send disc for channel %s", ch, exc_info=True)
                            # Resume scanning so the controller can re-advertise and be
                            # detected again. disc alone does not restart the scan.
                            try:
                                await asyncio.to_thread(
                                    shared_client.send_manager_command, "scan on", timeout=0.5
                                )
                            except Exception:
                                pass

                        # Clear all tracking state so the same controller can advertise,
                        # be detected and reconnect cleanly (issue 4). Clear by BOTH the
                        # controller_info MAC and the device.address — a stale entry left
                        # in connected_mac_addresses makes the scan_result filter drop the
                        # controller's reconnect ads forever ("can't search after close").
                        if ch is not None:
                            controllers_by_channel.pop(ch, None)
                            missing_counts_by_channel.pop(ch, None)
                        macs_to_clear = set()
                        ci_mac = getattr(getattr(controller, 'controller_info', None), 'mac_address', None)
                        if ci_mac:
                            macs_to_clear.add(ci_mac.upper())
                        dev_mac = getattr(getattr(controller, 'device', None), 'address', None)
                        if dev_mac:
                            macs_to_clear.add(dev_mac.upper())
                        for mac_u in macs_to_clear:
                            while mac_u in connected_mac_addresses:
                                connected_mac_addresses.remove(mac_u)
                            bridge_connecting_macs.discard(mac_u)
                            bridge_connecting_since.pop(mac_u, None)
                            bridge_retry_counts.pop(mac_u, None)
                            bridge_pending_pair.discard(mac_u)
                        logger.info(
                            "Bridge disconnect cleanup: ch=%s cleared=%s; still-connected=%s",
                            ch, sorted(macs_to_clear), list(connected_mac_addresses),
                        )

                        async with GLOBAL_LOCK:
                            for i, vc in enumerate(VIRTUAL_CONTROLLERS[:]):
                                if vc is not None and controller in getattr(vc, "controllers", []):
                                    # Mirror the WinRT path: only free the slot when
                                    # remove_controller reports the group is now EMPTY
                                    # (it returns True and tears down USBIP / detaches the
                                    # virtual device then). For a merged group it returns
                                    # False after stopping just this controller's USBIP and
                                    # re-initing the rest — nulling the slot anyway would
                                    # orphan the remaining controller's still-running USBIP
                                    # server, leaving the virtual device stuck attached.
                                    try:
                                        became_empty = await vc.remove_controller(controller)
                                    except Exception:
                                        logger.exception("Failed to remove ESP32-S3 bridge controller")
                                        became_empty = True
                                    if became_empty:
                                        VIRTUAL_CONTROLLERS[i] = None

                            if IS_SHUTTING_DOWN or _IS_SUSPENDING:
                                return

                            reorder_controllers()

                            if UPDATE_CALLBACK is not None:
                                UPDATE_CALLBACK(list(VIRTUAL_CONTROLLERS))

                            await update_all_player_leds()

                    DISCONNECT_CALLBACK = esp32_disconnected_controller

                    bridge_connecting_macs = set()
                    # MACs that have a BLE connection established at the firmware level but
                    # whose PC-side init (add_esp32_channel / controller.initialize) is still
                    # running. Counted in active_count so the status loop uses a generous
                    # miss_limit during the init window instead of restarting after 4 misses.
                    bridge_init_macs: set[str] = set()
                    # Serializes ESP32 controller init sequences so that two controllers
                    # connecting simultaneously don't flood the shared serial port with
                    # competing SW2 init commands, causing response timeouts and failed
                    # read_controller_info() → disconnect.
                    esp32_init_lock = asyncio.Lock()
                    # MAC → addr_type (0=public, 1=random), populated from scan_result events
                    bridge_mac_addr_type: dict[str, int] = {}
                    # MAC → retry count for y700-style fast-window reconnect
                    bridge_retry_counts: dict[str, int] = {}
                    # MAC → monotonic time we started connecting; used by the watchdog in
                    # the status loop to clear stuck connects so a later scan_result retries.
                    bridge_connecting_since: dict[str, float] = {}
                    # MAC → monotonic time the PC-side init started; used by the watchdog
                    # to abort inits that have been running too long (e.g. after a serial
                    # port error leaves init commands timing out). 45 s matches 3 × the
                    # SW2 consecutive-failure abort window so it only triggers if the fast
                    # abort somehow didn't fire.
                    bridge_init_since: dict[str, float] = {}
                    # MACs that connected in pairing mode and must run the Switch 2
                    # application-level pair() handshake (SET_MAC to the bridge) once
                    # GATT is up, so the controller bonds to the bridge.
                    bridge_pending_pair: set[str] = set()
                    # The bridge's own BLE MAC (str + int), read from the firmware status.
                    # Used as the host MAC in pair() and to recognise reconnect ads
                    # addressed to this bridge.
                    esp32_mac_str = None
                    esp32_mac_value = None

                    # Block controller connections until "scan on" is sent and the
                    # bridge is fully armed. Cleared at session start so the GUI shows
                    # "Initializing" instead of "Ready" during the setup window.
                    _usb_serial_bridge_mod.BRIDGE_SCAN_ACTIVE = False

                    def bridge_event_callback(event):
                        try:
                            cmd = event.get("cmd")

                            if cmd == "connected":
                                # Drop stale connections that arrive before the bridge is
                                # fully armed (before "scan on"). The firmware may auto-
                                # reconnect lingering links from the previous session
                                # during the auto off / ble disconnect window.
                                if not _usb_serial_bridge_mod.BRIDGE_SCAN_ACTIVE:
                                    mac = (event.get("mac") or "").upper()
                                    channel = int(event.get("channel", -1))
                                    logger.info(
                                        "ESP32 bridge not yet scanning; dropping early connected event "
                                        "ch=%s mac=%s", channel, mac
                                    )
                                    if channel >= 0:
                                        try:
                                            shared_client.send_manager_command(
                                                f"disc {channel}", timeout=0.3
                                            )
                                        except Exception:
                                            pass
                                    return

                                channel = int(event.get("channel", -1))
                                mac = (event.get("mac") or "").upper()
                                if channel < 0 or not mac:
                                    return
                                if mac not in connected_mac_addresses:
                                    connected_mac_addresses.append(mac)
                                bridge_connecting_macs.discard(mac)
                                bridge_retry_counts.pop(mac, None)
                                if DISCOVERER_LOOP and DISCOVERER_LOOP.is_running():
                                    async def handle_connected(ch=channel, m=mac):
                                        if ch not in controllers_by_channel:
                                            bridge_init_macs.add(m)
                                            bridge_init_since.setdefault(m, time.time())
                                            try:
                                                ctrl = await add_esp32_channel(ch, m)
                                                if ctrl is not None:
                                                    controllers_by_channel[ch] = ctrl
                                                    missing_counts_by_channel[ch] = 0
                                            finally:
                                                bridge_init_macs.discard(m)
                                                bridge_init_since.pop(m, None)
                                    asyncio.run_coroutine_threadsafe(handle_connected(), DISCOVERER_LOOP)
                                return

                            if cmd == "connect_fail":
                                # Firmware could not establish BLE connection to this MAC.
                                # Clear connecting state so the next scan_result triggers a retry.
                                mac = (event.get("mac") or "").upper()
                                if not mac:
                                    return
                                retries = bridge_retry_counts.get(mac, 0)
                                bridge_retry_counts[mac] = retries + 1
                                # y700: fast reconnect window — retry up to 10 times then give up.
                                if retries < 10 and DISCOVERER_LOOP and DISCOVERER_LOOP.is_running():
                                    addr_type = bridge_mac_addr_type.get(mac, 0)
                                    logger.info(
                                        "Bridge connect_fail for %s (attempt %d/10); retrying in 800 ms",
                                        mac, retries + 1,
                                    )
                                    async def _retry(m=mac, t=addr_type):
                                        await asyncio.sleep(0.8)
                                        bridge_connecting_macs.discard(m)
                                        cmd_str = f"conn {t} {m}"
                                        await asyncio.to_thread(
                                            shared_client.send_manager_command, cmd_str, timeout=0.3
                                        )
                                        bridge_connecting_macs.add(m)
                                    asyncio.run_coroutine_threadsafe(_retry(), DISCOVERER_LOOP)
                                else:
                                    # Give up; let the controller advertise again naturally.
                                    bridge_connecting_macs.discard(mac)
                                    if retries >= 10:
                                        bridge_retry_counts.pop(mac, None)
                                        logger.warning(
                                            "Bridge connect_fail for %s: gave up after 10 attempts", mac
                                        )
                                return

                            if cmd == "connect_busy":
                                # Firmware was already connecting when we sent conn; remove from
                                # connecting set so we retry when the next scan_result arrives.
                                mac = (event.get("mac") or "").upper()
                                bridge_connecting_macs.discard(mac)
                                return

                            if cmd == "disconnected":
                                # Firmware lost the BLE link for a channel.  Handle immediately
                                # so the Python host tracks the disconnect in real time instead
                                # of waiting for 3 consecutive missing-from-status polls.
                                # This also prevents the "disconnected" JSON from poisoning the
                                # send_manager_command("status lite") response queue, which
                                # would return channel_mask=0 and falsely disconnect ALL channels.
                                channel = int(event.get("channel", -1))
                                if channel >= 0 and DISCOVERER_LOOP and DISCOVERER_LOOP.is_running():
                                    async def handle_disconnected(ch=channel):
                                        controller = controllers_by_channel.pop(ch, None)
                                        missing_counts_by_channel.pop(ch, None)
                                        if controller is not None:
                                            await esp32_disconnected_controller(controller)
                                    asyncio.run_coroutine_threadsafe(handle_disconnected(), DISCOVERER_LOOP)
                                return

                            if cmd == "scan_result":
                                if not _usb_serial_bridge_mod.BRIDGE_SCAN_ACTIVE:
                                    return

                                mac = event.get("mac", "").upper()
                                addr_type = int(event.get("type", 0))
                                is_directed = bool(event.get("directed", 0))

                                # Remember addr_type for later retry use.
                                if mac:
                                    bridge_mac_addr_type[mac] = addr_type

                                # Filter already connecting / connected.
                                if mac in bridge_connecting_macs or mac in connected_mac_addresses:
                                    logger.debug(
                                        "scan_result for %s filtered (connecting=%s connected=%s)",
                                        mac, mac in bridge_connecting_macs, mac in connected_mac_addresses,
                                    )
                                    return

                                if is_directed:
                                    # Directed advertising is addressed to THIS bridge
                                    # specifically: a controller that was connected to us and
                                    # is trying to reconnect fast. The earlier filter already
                                    # dropped MACs we're already connecting/connected to, so
                                    # just connect — do NOT gate on calibration_data, which is
                                    # keyed by MAC and would be empty for a controller that has
                                    # not been gyro-calibrated yet, making reconnect impossible.
                                    logger.info("Reconnecting (directed) to %s via bridge", mac)
                                    bridge_connecting_macs.add(mac)
                                    if DISCOVERER_LOOP and DISCOVERER_LOOP.is_running():
                                        async def _conn_directed(m=mac, t=addr_type):
                                            await asyncio.to_thread(
                                                shared_client.send_manager_command,
                                                f"conn {t} {m}", timeout=0.3
                                            )
                                        asyncio.run_coroutine_threadsafe(_conn_directed(), DISCOVERER_LOOP)
                                    return

                                # --- Undirected advertising: parse manufacturer data ---
                                data_hex = event.get("data", "")
                                if not data_hex:
                                    return

                                try:
                                    raw_bytes = bytes.fromhex(data_hex)
                                except ValueError:
                                    return

                                pos = 0
                                nintendo_manufacturer_data = None
                                while pos < len(raw_bytes):
                                    ad_len = raw_bytes[pos]
                                    if ad_len == 0 or pos + 1 + ad_len > len(raw_bytes):
                                        break
                                    ad_type = raw_bytes[pos + 1]
                                    if ad_type == 0xFF and ad_len >= 3:
                                        company_id = raw_bytes[pos + 2] | (raw_bytes[pos + 3] << 8)
                                        if company_id == NINTENDO_BLUETOOTH_MANUFACTURER_ID:
                                            nintendo_manufacturer_data = raw_bytes[pos + 4 : pos + 1 + ad_len]
                                            break
                                    pos += 1 + ad_len

                                if not nintendo_manufacturer_data or len(nintendo_manufacturer_data) < 7:
                                    return

                                vendor_id = decodeu(nintendo_manufacturer_data[3:5])
                                product_id = decodeu(nintendo_manufacturer_data[5:7])

                                if vendor_id != NINTENDO_VENDOR_ID or product_id not in CONTROLER_NAMES:
                                    return

                                # Bytes 10..16 carry the MAC of the host this controller is
                                # currently bonded to (0 = pairing mode / not bonded). This is
                                # exactly how the WinRT path decides pair-vs-reconnect.
                                reconnect_mac = (
                                    decodeu(nintendo_manufacturer_data[10:16])
                                    if len(nintendo_manufacturer_data) >= 16 else 0
                                )

                                # Ghost connection: controller advertising while we think it's connected.
                                if mac in connected_mac_addresses:
                                    logger.info(f"Ghost connection detected for {mac}. Clearing state and waiting for cleanup.")
                                    try:
                                        connected_mac_addresses.remove(mac)
                                    except ValueError:
                                        pass

                                    bridge_connecting_macs.add(mac)
                                    if DISCOVERER_LOOP and DISCOVERER_LOOP.is_running():
                                        async def cleanup_ghost():
                                            for ch, ctrl in list(controllers_by_channel.items()):
                                                if getattr(ctrl.controller_info, 'mac_address', '').upper() == mac:
                                                    await asyncio.to_thread(shared_client.send_manager_command, f"disc {ch}", timeout=0.5)
                                                    break
                                            await asyncio.sleep(2.0)
                                            bridge_connecting_macs.discard(mac)
                                            logger.info(f"Ghost connection cleanup complete for {mac}. Ready for pairing.")
                                        asyncio.run_coroutine_threadsafe(cleanup_ghost(), DISCOVERER_LOOP)
                                    return

                                # Decide pair vs reconnect vs ignore based on which host
                                # the controller is bonded to (mirrors the WinRT path).
                                if reconnect_mac == 0:
                                    # Pairing mode (SYNC held / not bonded): connect AND run the
                                    # Switch 2 pair() handshake so it bonds to THIS bridge.
                                    logger.info(
                                        "Found pairing device %s %s via bridge (will pair to bridge)",
                                        CONTROLER_NAMES[product_id], mac,
                                    )
                                    bridge_pending_pair.add(mac)
                                elif esp32_mac_value is not None and reconnect_mac == esp32_mac_value:
                                    # Already bonded to this bridge — straight reconnect.
                                    logger.info("Reconnecting to %s via bridge", mac)
                                    bridge_pending_pair.discard(mac)
                                elif esp32_mac_value is not None:
                                    # Bonded to a DIFFERENT host (e.g. the PC). The controller
                                    # would accept then drop our connection (disc 574). Don't
                                    # fight it — the user must hold SYNC to re-bond to the bridge.
                                    logger.info(
                                        "Controller %s is paired to another host (reconnect_mac=%012X, bridge=%012X); "
                                        "hold SYNC on the controller to pair it to the bridge.",
                                        mac, reconnect_mac, esp32_mac_value,
                                    )
                                    return
                                else:
                                    # Bridge MAC unknown (older firmware): fall back to old behaviour.
                                    logger.info("Reconnecting to %s via bridge", mac)

                                bridge_connecting_macs.add(mac)
                                # y700 approach: Python drives the connection explicitly so we can
                                # retry on connect_fail with proper backoff.
                                if DISCOVERER_LOOP and DISCOVERER_LOOP.is_running():
                                    async def _conn_undirected(m=mac, t=addr_type):
                                        await asyncio.to_thread(
                                            shared_client.send_manager_command,
                                            f"conn {t} {m}", timeout=0.3
                                        )
                                    asyncio.run_coroutine_threadsafe(_conn_undirected(), DISCOVERER_LOOP)

                        except Exception as e:
                            logger.exception("Exception in bridge_event_callback!")

                    shared_client.event_callback = bridge_event_callback

                    # Issue 5: arm the bridge for a fresh session. "auto off" hands
                    # connection control to the main program; "ble disconnect" drops any
                    # stale links left over from a previous unclean shutdown / crash so
                    # the firmware channels start empty and controllers re-advertise and
                    # reconnect cleanly; "scan on" starts reporting advertisements.
                    await asyncio.to_thread(shared_client.send_manager_command, "auto off", timeout=0.5)
                    await asyncio.to_thread(shared_client.send_manager_command, "ble disconnect", timeout=0.8)

                    # Read the bridge's own BLE MAC so we can pair controllers to it
                    # (Switch 2 SET_MAC handshake) and recognise reconnect ads aimed at us.
                    try:
                        from utils import convert_mac_string_to_value
                        status_reply = await asyncio.to_thread(
                            shared_client.send_manager_command, "status lite", timeout=1.0
                        )
                        if status_reply:
                            s = json.loads(status_reply)
                            m = (s.get("mac") or "").strip().upper()
                            if m and m != "00:00:00:00:00:00":
                                esp32_mac_str = m
                                esp32_mac_value = convert_mac_string_to_value(m)
                                logger.info("ESP32-S3 bridge BLE MAC: %s", esp32_mac_str)
                    except Exception:
                        logger.debug("Could not read ESP32-S3 bridge BLE MAC", exc_info=True)
                    if esp32_mac_value is None:
                        logger.warning("ESP32-S3 bridge MAC unknown; controllers paired to another host won't reconnect until firmware reports its MAC.")

                    await asyncio.to_thread(shared_client.send_manager_command, "scan on", timeout=0.5)
                    _usb_serial_bridge_mod.BRIDGE_SCAN_ACTIVE = True
                    logger.info("ESP32-S3 Bridge is scanning. Monitoring up to %d physical channels / %d controller groups.",
                                MAX_ESP32S3_CHANNELS,
                                MAX_ESP32S3_GROUPS)

                    controllers_by_channel = {}
                    missing_counts_by_channel = {}
                    last_wait_log = 0.0
                    missed_status_count = 0

                    async def add_esp32_channel(channel: int, mac: str = None):
                        controller = ESP32S3Controller(bridge.serial_port.port, channel=channel, shared_client=shared_client)
                        # The real BLE MAC is known up-front from the firmware's "connected"
                        # event. Assign it as the controller's address BEFORE initialize()
                        # so per-controller state (gyro/mag calibration, cemuhook pad MAC,
                        # USBIP serial, paired-device detection) is keyed by the physical
                        # MAC instead of the "ESP32-S3-N16R8-CHn" placeholder. Without this
                        # bytes.fromhex(device.address) crashes the input callback and
                        # paired controllers are never recognised for directed reconnect.
                        if mac:
                            mac = mac.upper()
                            controller.device.address = mac
                            try:
                                controller.controller_info.mac_address = mac
                            except Exception:
                                pass
                        controller.disconnected_callback = esp32_disconnected_controller
                        async with esp32_init_lock:
                            try:
                                await controller.initialize()
                            except Exception:
                                logger.exception(
                                    "ESP32 channel=%d init failed; clearing tracking so controller can advertise again",
                                    channel,
                                )
                                # Do NOT send "disc <ch>" here — issuing disc on a BLE
                                # channel that was already torn down by a serial error can
                                # crash the firmware and disconnect ALL controllers.
                                # The channel will free itself: either it is already gone
                                # (serial error path) or the firmware will time it out.
                                # Just wipe PC-side state so the next scan_result retries.
                                controller.client = None
                                mac_key = mac.upper() if mac else None
                                if mac_key:
                                    while mac_key in connected_mac_addresses:
                                        connected_mac_addresses.remove(mac_key)
                                    bridge_connecting_macs.discard(mac_key)
                                    bridge_connecting_since.pop(mac_key, None)
                                    bridge_retry_counts.pop(mac_key, None)
                                return None

                        # If this controller connected in pairing mode, run the Switch 2
                        # application-level pair() handshake using the BRIDGE's MAC so the
                        # controller bonds to the bridge and will reconnect to it on a
                        # button press (mirrors the WinRT path's controller.pair()).
                        if mac and mac.upper() in bridge_pending_pair:
                            bridge_pending_pair.discard(mac.upper())
                            if esp32_mac_value is not None:
                                try:
                                    await controller.pair(host_mac_value=esp32_mac_value)
                                    logger.info("Paired %s to bridge (%s)", mac.upper(), esp32_mac_str)
                                except Exception:
                                    logger.exception("Failed to pair %s to bridge", mac.upper())
                            else:
                                logger.warning("Cannot pair %s to bridge: bridge MAC unknown.", mac.upper())

                        # MAC is normally supplied by the connected event above; fall back to
                        # controller_info if a caller invoked us without one.
                        if not mac:
                            wait_time = 0.0
                            while not getattr(controller.controller_info, 'mac_address', None) and wait_time < 3.0:
                                await asyncio.sleep(0.1)
                                wait_time += 0.1
                            mac = getattr(controller.controller_info, 'mac_address', None)
                        if mac:
                            mac = mac.upper()
                            # initialize() replaced controller_info via read_controller_info(),
                            # wiping the mac_address we set earlier. Restore it (and the
                            # device address) so disconnect cleanup keyed on the MAC works —
                            # otherwise a stale connected_mac_addresses entry makes the
                            # controller's reconnect ads get filtered forever.
                            controller.device.address = mac
                            try:
                                controller.controller_info.mac_address = mac
                            except Exception:
                                pass
                            if mac not in connected_mac_addresses:
                                connected_mac_addresses.append(mac)
                            bridge_connecting_macs.discard(mac)
                            bridge_connecting_since.pop(mac, None)

                        # Issue 2: do NOT start the per-controller _poll_status here. It
                        # disconnects on a single empty/timeout status reply, and when a
                        # second controller connects its init sequence floods the shared
                        # serial link — starving the first controller's status probe and
                        # falsely disconnecting it. The single status loop below monitors
                        # every channel with miss tolerance and is the only disconnect
                        # detector for the bridge route.

                        async with GLOBAL_LOCK:
                            virtual_controller = None
                            created_virtual_controller = False
                            if CONFIG.combine_joycons and not controller.side_buttons_pressed:
                                if controller.is_joycon_left():
                                    virtual_controller = next(
                                        filter(lambda vc: vc is not None and vc.is_single_joycon_right(), VIRTUAL_CONTROLLERS[:MAX_ESP32S3_GROUPS]),
                                        None,
                                    )
                                elif controller.is_joycon_right():
                                    virtual_controller = next(
                                        filter(lambda vc: vc is not None and vc.is_single_joycon_left(), VIRTUAL_CONTROLLERS[:MAX_ESP32S3_GROUPS]),
                                        None,
                                    )

                            if virtual_controller is None:
                                free_slots = [i for i, c in enumerate(VIRTUAL_CONTROLLERS[:MAX_ESP32S3_GROUPS]) if c is None]
                                if not free_slots:
                                    logger.warning("ESP32-S3 channel=%d connected but max group limit reached (%d).", channel, MAX_ESP32S3_GROUPS)
                                    await controller.disconnect()
                                    return None
                                slot_index = free_slots[0]
                                virtual_controller = VirtualController(slot_index + 1, [controller], esp32_disconnected_controller, setup_usb=False)
                                VIRTUAL_CONTROLLERS[slot_index] = virtual_controller
                                created_virtual_controller = True
                            else:
                                virtual_controller.add_controller(controller)

                            await virtual_controller.init_added_controller(controller)
                            reorder_controllers()
                            if UPDATE_CALLBACK is not None:
                                UPDATE_CALLBACK(list(VIRTUAL_CONTROLLERS))
                            await update_all_player_leds()

                        if created_virtual_controller:
                            await asyncio.to_thread(virtual_controller.setup_virtual_device)
                        async def _connection_haptics(c=controller):
                            await c.trigger_connection_haptics()
                        asyncio.create_task(_connection_haptics())
                        logger.info("Controller connected via ESP32-S3 N16R8 channel=%d", channel)
                        return controller

                    while not quit_event.is_set():
                        # Fast-detect hardware disconnect: the serial read loop sets
                        # _closed_by_error on SerialException/OSError, which means the
                        # physical USB link dropped. No point waiting for miss_limit
                        # timeouts — restart the bridge loop immediately.
                        if getattr(shared_client, '_closed_by_error', False):
                            logger.warning(
                                "ESP32-S3 serial port closed by hardware disconnect. Restarting bridge connection loop..."
                            )
                            break

                        # Extend timeout when controllers are actively connecting, initializing,
                        # or already connected — multiple simultaneous BLE handshakes and
                        # SW2 init sequences can delay firmware responses significantly.
                        status_timeout = 1.5 if (controllers_by_channel or bridge_connecting_macs or bridge_init_macs) else 0.5
                        reply = await asyncio.to_thread(shared_client.send_manager_command, "status lite", timeout=status_timeout)
                        if not reply:
                            missed_status_count += 1
                            # Allow more misses when controllers are in-flight — a busy BLE
                            # stack can silence "status lite" for several seconds without the
                            # bridge actually being gone. bridge_init_macs counts controllers
                            # whose BLE is up but PC-side init hasn't finished yet.
                            active_count = len(controllers_by_channel) + len(bridge_connecting_macs) + len(bridge_init_macs)
                            miss_limit = 4 + active_count * 4
                            if missed_status_count < miss_limit:
                                logger.warning(
                                    "%s status unavailable (%d/%d). Keeping USB CDC route.",
                                    ESP32S3_LABEL,
                                    missed_status_count,
                                    miss_limit,
                                )
                                await asyncio.sleep(0.5)
                                continue
                            try:
                                current_bridge = detect_bridge()
                            except Exception:
                                current_bridge = None
                            if not current_bridge or not current_bridge.serial_port:
                                logger.info("%s status unavailable and bridge is gone. Falling back to system bluetooth.", ESP32S3_LABEL)
                                fallback_to_system_bluetooth = True
                            else:
                                logger.warning("ESP32-S3 status unavailable. Restarting bridge connection loop...")
                            break
                        missed_status_count = 0
                        try:
                            status = json.loads(reply)
                        except Exception:
                            status = {}
                        channel_mask = int(status.get("ble_channels", 0) or 0)

                        for channel in list(controllers_by_channel):
                            if not (channel_mask & (1 << channel)):
                                missing_counts_by_channel[channel] = missing_counts_by_channel.get(channel, 0) + 1
                                if missing_counts_by_channel[channel] < 3:
                                    logger.warning(
                                        "ESP32-S3 channel=%d missing from status (%d/3); waiting before disconnect.",
                                        channel,
                                        missing_counts_by_channel[channel],
                                    )
                                    continue
                                controller = controllers_by_channel.pop(channel)
                                missing_counts_by_channel.pop(channel, None)
                                mac = getattr(controller.controller_info, 'mac_address', None)
                                if mac and mac.upper() in connected_mac_addresses:
                                    connected_mac_addresses.remove(mac.upper())
                                await esp32_disconnected_controller(controller)
                            else:
                                missing_counts_by_channel[channel] = 0

                        # Issue 1 watchdog: a "conn" can be lost (controller stopped
                        # advertising, firmware dropped the link during GATT discovery
                        # without emitting connect_fail, etc.). Clear MACs that have been
                        # "connecting" too long so the next scan_result triggers a fresh
                        # connect attempt instead of staying stuck on "Found pairing device".
                        now_mono = time.time()
                        stuck_cleared = False
                        for m in list(bridge_connecting_macs):
                            started = bridge_connecting_since.get(m)
                            if started is None:
                                bridge_connecting_since[m] = now_mono
                            elif now_mono - started > 12.0:
                                logger.info(
                                    "Bridge connect watchdog: clearing stuck connect for %s after %.0fs",
                                    m, now_mono - started,
                                )
                                bridge_connecting_macs.discard(m)
                                bridge_connecting_since.pop(m, None)
                                bridge_retry_counts.pop(m, None)
                                stuck_cleared = True
                        for m in list(bridge_connecting_since):
                            if m not in bridge_connecting_macs:
                                bridge_connecting_since.pop(m, None)
                        if stuck_cleared:
                            # Abort the firmware's pending connect (without dropping any
                            # already-connected channels) and make sure it is scanning,
                            # so a connect that never completed cannot leave the bridge
                            # unable to find any controller until it is replugged.
                            try:
                                await asyncio.to_thread(shared_client.send_manager_command, "cancel", timeout=0.5)
                                await asyncio.to_thread(shared_client.send_manager_command, "scan on", timeout=0.5)
                            except Exception:
                                pass

                        # Init watchdog: if a MAC has been in the PC-side init phase for
                        # too long (serial error left SW2 init commands timing out), remove
                        # it so the bridge is no longer counted in active_count and the
                        # miss_limit is not inflated. The fast-abort in controller.py will
                        # raise an exception first; this is a last-resort safety net.
                        for m in list(bridge_init_macs):
                            started = bridge_init_since.get(m)
                            if started is None:
                                bridge_init_since[m] = now_mono
                            elif now_mono - started > 45.0:
                                logger.warning(
                                    "Bridge init watchdog: removing stuck init for %s after %.0fs; "
                                    "bridge will remain able to find controllers.",
                                    m, now_mono - started,
                                )
                                bridge_init_macs.discard(m)
                                bridge_init_since.pop(m, None)
                                while m in connected_mac_addresses:
                                    connected_mac_addresses.remove(m)
                                bridge_connecting_macs.discard(m)
                        for m in list(bridge_init_since):
                            if m not in bridge_init_macs:
                                bridge_init_since.pop(m, None)

                        await asyncio.sleep(0.5)

                    checker_task.cancel()
                    try:
                        await checker_task
                    except asyncio.CancelledError:
                        pass

                    # The bridge route is ending (USB unplugged, status lost, fallback,
                    # or restart). Remove every controller still attached to it so it
                    # does not linger as a ghost in the player slots. Without this,
                    # unplugging the ESP32-S3 while a controller is connected leaves a
                    # dead controller stuck in its slot. esp32_disconnected_controller
                    # also frees the virtual device / USBIP server.
                    for ghost in list(controllers_by_channel.values()):
                        try:
                            await esp32_disconnected_controller(ghost)
                        except Exception:
                            logger.exception("Failed to remove bridge controller during teardown")
                    controllers_by_channel.clear()
                    missing_counts_by_channel.clear()

                    if shared_client:
                        try:
                            if fallback_to_system_bluetooth or quit_event.is_set():
                                # Issue 5: bring the bridge to a fully idle state before we
                                # let go of it — stop scanning, keep auto-connect disabled,
                                # and drop every BLE link so no controller stays connected
                                # once the main program stops working. The firmware will not
                                # resume scanning on the resulting disconnect events because
                                # scan_mode is now off. On the next app start the discoverer
                                # re-arms the bridge with "auto off" + "scan on".
                                await asyncio.to_thread(shared_client.send_manager_command, "scan off", timeout=0.5)
                                await asyncio.to_thread(shared_client.send_manager_command, "auto off", timeout=0.5)
                                await asyncio.to_thread(shared_client.send_manager_command, "ble disconnect", timeout=0.8)
                        except Exception:
                            pass
                        await shared_client.disconnect()

                    _usb_serial_bridge_mod.BRIDGE_SCAN_ACTIVE = False

                    if quit_event.is_set():
                        return
                    if fallback_to_system_bluetooth:
                        break
                    await asyncio.sleep(2.0)
                    continue
                except Exception:
                    if worker_task:
                        worker_task.cancel()
                        try:
                            await worker_task
                        except asyncio.CancelledError:
                            pass
                    if checker_task:
                        checker_task.cancel()
                        try:
                            await checker_task
                        except asyncio.CancelledError:
                            pass
                    if shared_client:
                        try:
                            await shared_client.disconnect()
                        except Exception:
                            pass
                    if not quit_event.is_set():
                        logger.exception(f"{ESP32S3_LABEL} bridge connection failed. Retrying in 2 seconds...")
                        await asyncio.sleep(2.0)
                if not fallback_to_system_bluetooth:
                    continue
                logger.info("Controller connection route: ESP32-S3 unavailable, switching to system bluetooth")

        host_mac_value = None
        logger.info("Controller connection route: system bluetooth")
        
        # Robust retry loop to wait for Windows Bluetooth service and BLE stack to initialize (critical for startup)
        bluetooth_initialized = False
        retries = 15
        for attempt in range(retries):
            if quit_event.is_set():
                logger.info("Quit event set during Bluetooth initialization. Aborting discovery.")
                with DISCOVERY_LOCK:
                    _CURRENTLY_DISCOVERING = False
                return
        
            try:
                from utils import get_local_mac_value
                host_mac_value = get_local_mac_value()
            
                # Test scanner initialization to verify WinRT stack is ready
                scanner = BleakScanner()
            
                bluetooth_initialized = True
                logger.info(f"Bluetooth adapter and stack initialized successfully. Host MAC: {host_mac_value}")
                break
            except Exception as e:
                logger.warning(f"Waiting for Bluetooth adapter/stack initialization (attempt {attempt + 1}/{retries}): {e}")
                await asyncio.sleep(2.0)

        if not bluetooth_initialized:
            logger.error("Bluetooth adapter/stack failed to initialize after multiple attempts. Discovery aborted.")
            with DISCOVERY_LOCK:
                _CURRENTLY_DISCOVERING = False
            return

        # Start auto disconnect checker task
        checker_task = asyncio.create_task(auto_disconnect_checker(quit_event))
        pending_connections_count = 0

        async def start_all_pending_virtual_usb():
            logger.info("Initializing virtual USB/device setup for all pending controllers in parallel...")
            tasks = []
            for vc in VIRTUAL_CONTROLLERS:
                if vc is not None:
                    tasks.append(asyncio.to_thread(vc.setup_virtual_device))
            if tasks:
                await asyncio.gather(*tasks)

        async def trigger_connection_haptics(controller):
            if getattr(controller, "_connection_haptics_done", False):
                return
            controller._connection_haptics_done = True
            await controller.trigger_connection_haptics()

        async def disconnected_controller(controller: Controller):
            logger.info(f"Controller disconected {controller.client.address}")
        
            if controller.client.address in connected_mac_addresses:
                connected_mac_addresses.remove(controller.client.address)
            
            async with GLOBAL_LOCK:
                for i, vc in enumerate(VIRTUAL_CONTROLLERS[:]):
                    if vc is not None and await vc.remove_controller(controller):
                        VIRTUAL_CONTROLLERS[i] = None
        
                if IS_SHUTTING_DOWN or _IS_SUSPENDING:
                    return
                
                reorder_controllers()
            
                if UPDATE_CALLBACK is not None:
                    UPDATE_CALLBACK(list(VIRTUAL_CONTROLLERS))
            
                await update_all_player_leds()

        DISCONNECT_CALLBACK = disconnected_controller

        _connecting_macs: set[str] = set()

        async def add_controller(device: BLEDevice, paired: bool):
            nonlocal pending_connections_count
            controller = None
            try:
                # 1. Serialize BLE connection & pairing phase to prevent WinRT concurrency crashes
                async with CONNECTION_LOCK:
                    controller = Controller(device)
                    await controller.connect_ble()
                    logger.info(f"Controller connected via system bluetooth: {device.address}")
                    controller.disconnected_callback = disconnected_controller
                
                    await controller.initialize()
                
                    if not paired:
                        await controller.pair()
                        logger.info(f"Paired successfully to {device.address}")
                    # BLE connection confirmed — promote to connected so scanner won't retry
                    _connecting_macs.discard(device.address)
                    connected_mac_addresses.append(device.address)
            
                # 4. Integrate the controller into VIRTUAL_CONTROLLERS under the global lock to prevent race conditions
                async with GLOBAL_LOCK:
                    virtual_controller = None
                    if CONFIG.combine_joycons and not controller.side_buttons_pressed:
                        if controller.is_joycon_left():
                            virtual_controller = next(filter(lambda vc: vc is not None and vc.is_single_joycon_right(), VIRTUAL_CONTROLLERS), None)
                        elif controller.is_joycon_right():
                            virtual_controller = next(filter(lambda vc: vc is not None and vc.is_single_joycon_left(), VIRTUAL_CONTROLLERS), None)

                    if virtual_controller is None:
                        slot_index = next(i for i, c in enumerate(VIRTUAL_CONTROLLERS) if c == None)
                        virtual_controller = VirtualController(slot_index + 1, [controller], disconnected_controller, setup_usb=False)
                        VIRTUAL_CONTROLLERS[slot_index] = virtual_controller
                    else:
                        virtual_controller.add_controller(controller)
                
                    await virtual_controller.init_added_controller(controller)
                
                    reorder_controllers()
                
                    if UPDATE_CALLBACK is not None:
                        UPDATE_CALLBACK(list(VIRTUAL_CONTROLLERS))
                
                    await update_all_player_leds()

                    logger.info(VIRTUAL_CONTROLLERS)
                
                    pending_connections_count = max(0, pending_connections_count - 1)
                    logger.info(f"Controller {device.address} connected. Remaining pending connections: {pending_connections_count}")
                    if pending_connections_count == 0:
                        await start_all_pending_virtual_usb()
                        for vc in VIRTUAL_CONTROLLERS:
                            if vc is not None:
                                for c in getattr(vc, "controllers", []):
                                    asyncio.create_task(trigger_connection_haptics(c))
            except Exception:
                logger.exception(f"Unable to initialize device {device.address}")
                if device.address in connected_mac_addresses:
                    connected_mac_addresses.remove(device.address)
                _connecting_macs.discard(device.address)
                async with GLOBAL_LOCK:
                    pending_connections_count = max(0, pending_connections_count - 1)
                    logger.info(f"Connection failed for {device.address}. Remaining pending connections: {pending_connections_count}")
                    if pending_connections_count == 0:
                        await start_all_pending_virtual_usb()
                if controller is not None:
                    try:
                        await controller.disconnect()
                    except Exception:
                        pass
                print("\nConnection failed. Please press a button on the controller or hold SYNC to re-pair.")
        
            finally:
                _connecting_macs.discard(device.address)

        async def callback(device: BLEDevice, advertising_data: AdvertisementData):
            nonlocal pending_connections_count
            if device.address in connected_mac_addresses or device.address in _connecting_macs:
                return
            nintendo_manufacturer_data = advertising_data.manufacturer_data.get(NINTENDO_BLUETOOTH_MANUFACTURER_ID)
            if nintendo_manufacturer_data:
                vendor_id = decodeu(nintendo_manufacturer_data[3:5])
                product_id = decodeu(nintendo_manufacturer_data[5:7])
                reconnect_mac = decodeu(nintendo_manufacturer_data[10:16])
                if vendor_id == NINTENDO_VENDOR_ID and product_id in CONTROLER_NAMES:
                    logger.debug(f"Manufacturer data: {to_hex(nintendo_manufacturer_data)}")
                    if reconnect_mac == 0:
                        logger.info(f"Found pairing device {CONTROLER_NAMES[product_id]} {device.address}")
                        _connecting_macs.add(device.address)
                        async with GLOBAL_LOCK:
                            pending_connections_count += 1
                        asyncio.create_task(add_controller(device, False))
                    elif reconnect_mac == host_mac_value:
                        logger.info(f"Found already paired device {CONTROLER_NAMES[product_id]} {device.address}")
                        _connecting_macs.add(device.address)
                        async with GLOBAL_LOCK:
                            pending_connections_count += 1
                        asyncio.create_task(add_controller(device, True))

        while not quit_event.is_set():
            try:
                async with BleakScanner(callback) as scanner:
                    _SYSTEM_BT_AVAILABLE = True
                    print("Press a button on a paired controller, or hold sync button on an unpaired controller")
                    while not quit_event.is_set():
                        await asyncio.sleep(1.0)
                        # On Windows, check if the watcher was aborted (e.g. Bluetooth turned off)
                        if hasattr(scanner, '_backend') and hasattr(scanner._backend, 'watcher'):
                            status = getattr(scanner._backend.watcher, 'status', None)
                            if status is not None and hasattr(status, 'value'):
                                if status.value == 4: # BluetoothLEAdvertisementWatcherStatus.Aborted
                                    logger.warning("Bluetooth watcher aborted (likely Bluetooth turned off). Restarting scanner...")
                                    _SYSTEM_BT_AVAILABLE = False
                                    break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _SYSTEM_BT_AVAILABLE = False
                logger.error(f"Bluetooth scanner error: {e}. Retrying in 2 seconds...")
                await asyncio.sleep(2.0)
    finally:
        if usb_hid_task is not None:
            usb_hid_task.cancel()
            try:
                await usb_hid_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("USB HID discovery task teardown error", exc_info=True)
        with DISCOVERY_LOCK:
            _CURRENTLY_DISCOVERING = False
        logger.info(f"[{time.strftime('%H:%M:%S')}] Discovery loop exited. Starting session cleanup...")
        # Use a copy to avoid issues if the list is modified during iteration
        vcs_to_disconnect = [vc for vc in VIRTUAL_CONTROLLERS if vc is not None]
        if vcs_to_disconnect:
            # CRITICAL: We now use is_suspending=False even during suspend
            # to ensure the ViGEmBus handles are closed cleanly.
            # Our "Triple Protection" in gui.py handles the wake-prevention.
            await asyncio.gather(*[vc.disconnect(is_suspending=False) for vc in vcs_to_disconnect])
        logger.info(f"[{time.strftime('%H:%M:%S')}] Discovery session cleanup complete.")
        # Allow WinRT background events (like services_changed_handler) to clear out before closing the loop
        await asyncio.sleep(0.5)

async def run_usb_hid_discovery(quit_event):
    """Concurrent watcher for wired USB Pro Controller 2 devices.

    Polls hidapi for the controller, hides its physical HID via HidHide, drives it
    through the shared Controller pipeline, and occupies a normal player slot. Always
    runs in the background alongside whichever BLE route ``run_discovery`` is using.
    """
    try:
        from usb_hid_controller import USBHidController, enumerate_pro_controller2
        import hidhide
    except Exception as e:
        logger.info("Wired USB support unavailable (missing hidapi?): %s", e)
        return
    logger.info("Wired USB watcher started (polling for Pro Controller 2, VID 057E/PID 2069).")

    known: dict = {}          # device key -> USBHidController
    connecting: set = set()
    # Every physical HID instance we've added to the HidHide blacklist. Entries persist
    # across unplug/replug so a reconnecting controller stays hidden the instant it
    # reappears (never briefly visible to third-party software). Cleared only on teardown.
    hidden_instances: set = set()

    def _device_key(entry):
        # The HID path is unique per physical device/port; Nintendo serials are all '00'.
        path = entry.get("path")
        if path is not None:
            return path if isinstance(path, str) else bytes(path)
        return (entry.get("serial_number") or "").strip().upper()

    async def _remove(controller, key):
        known.pop(key, None)
        instance_id = getattr(controller, "_hidhide_instance_id", None)
        async with GLOBAL_LOCK:
            for i, vc in enumerate(VIRTUAL_CONTROLLERS[:]):
                if vc is not None and controller in getattr(vc, "controllers", []):
                    try:
                        became_empty = await vc.remove_controller(controller)
                    except Exception:
                        logger.exception("Failed to remove wired USB controller")
                        became_empty = True
                    if became_empty:
                        VIRTUAL_CONTROLLERS[i] = None
            if not (IS_SHUTTING_DOWN or _IS_SUSPENDING):
                reorder_controllers()
                if UPDATE_CALLBACK is not None:
                    UPDATE_CALLBACK(list(VIRTUAL_CONTROLLERS))
                await update_all_player_leds()
        try:
            await controller.disconnect()
        except Exception:
            pass
        # Intentionally do NOT unhide on unplug: leaving the instance blacklisted keeps the
        # physical controller hidden from third-party software the moment it is replugged,
        # instead of it being briefly visible until the watcher re-hides it. The blacklist
        # entry is cleaned up on app teardown (see the finally block below).

    async def _add(entry, key):
        controller = None
        instance_id = None
        try:
            if not getattr(CONFIG, "wired_usb_enabled", True):
                return
            # Hide the physical HID first (whitelists our own process so we keep access).
            instance_id = hidhide.hid_path_to_instance_id(entry.get("path"))
            # Only hide when the user hasn't disabled HidHide. hide_device() re-activates
            # HidHide filtering, so hiding here regardless of preference would silently undo
            # a Disable the next time the controller is replugged.
            if instance_id and hidhide.is_available() and getattr(CONFIG, "hidhide_hide_enabled", True):
                hidhide.hide_device(instance_id)
                hidden_instances.add(instance_id)

            controller = USBHidController(entry)
            controller._hidhide_instance_id = instance_id

            async def _on_disc(c, _k=key):
                await _remove(c, _k)
            controller.disconnected_callback = _on_disc

            await controller.initialize()

            async with GLOBAL_LOCK:
                slot_index = next((i for i, c in enumerate(VIRTUAL_CONTROLLERS) if c is None), None)
                if slot_index is None:
                    logger.warning("Wired USB pad connected but no free player slot.")
                    await controller.disconnect()
                    if instance_id:
                        hidhide.unhide_device(instance_id)
                        hidden_instances.discard(instance_id)
                    return
                vc = VirtualController(slot_index + 1, [controller], _on_disc, setup_usb=False)
                VIRTUAL_CONTROLLERS[slot_index] = vc
                await vc.init_added_controller(controller)
                reorder_controllers()
                if UPDATE_CALLBACK is not None:
                    UPDATE_CALLBACK(list(VIRTUAL_CONTROLLERS))
                await update_all_player_leds()

            await asyncio.to_thread(vc.setup_virtual_device)
            async def _connection_haptics(c_ref=controller):
                await c_ref.trigger_connection_haptics()
            asyncio.create_task(_connection_haptics())
            known[key] = controller
            logger.info("Wired USB Pro Controller 2 added (%s)", controller.device.address)
        except Exception:
            logger.exception("Failed to add wired USB controller")
            if controller is not None:
                try:
                    await controller.disconnect()
                except Exception:
                    pass
            if instance_id:
                try:
                    hidhide.unhide_device(instance_id)
                    hidden_instances.discard(instance_id)
                except Exception:
                    pass
        finally:
            connecting.discard(key)

    try:
        while not quit_event.is_set():
            try:
                if not getattr(CONFIG, "wired_usb_enabled", True):
                    for key in list(known):
                        controller = known.get(key)
                        if controller is not None:
                            await _remove(controller, key)
                    connecting.clear()
                    await asyncio.sleep(1.0)
                    continue
                chosen = {}
                # hidapi enumeration is a blocking syscall. When no wired pad is
                # present (the common case for wireless-only users) it always falls
                # back to enumerating every HID device on the system, which can take
                # tens of milliseconds. Running it inline on this event loop stalls
                # the BLE rumble dispatch that shares the loop, producing an even ~1Hz
                # gap in continuous vibration. Offload it to a thread so the wireless
                # rumble timing matches 0.12.1 (which had no wired watcher at all).
                entries = await asyncio.to_thread(enumerate_pro_controller2)
                for entry in entries:
                    key = _device_key(entry)
                    if key and key not in chosen:
                        chosen[key] = entry
                for key, entry in chosen.items():
                    if key in known or key in connecting:
                        continue
                    connecting.add(key)
                    asyncio.create_task(_add(entry, key))
                for key in list(known):
                    if key not in chosen:
                        controller = known.get(key)
                        if controller is not None:
                            await _remove(controller, key)
            except Exception:
                logger.exception("Wired USB discovery poll error")
            await asyncio.sleep(1.0)
    finally:
        # Unhide everything we ever hid — including instances whose controllers were already
        # unplugged (and thus dropped from `known`) — so no device is left invisible to the
        # system after teardown.
        for instance_id in list(hidden_instances):
            try:
                hidhide.unhide_device(instance_id)
            except Exception:
                pass
        hidden_instances.clear()


def start_discoverer(update_controllers_threadsafe, quit_event):
    asyncio.run(run_discovery(update_controllers_threadsafe, quit_event))

def reorder_controllers():
    global VIRTUAL_CONTROLLERS
    with DISCOVERY_LOCK:

        active_vcs = []
        for vc in VIRTUAL_CONTROLLERS:
            if vc is not None:
                active_vcs.append(vc)
        
        if not active_vcs:
            return

        # Priority: Pro Controller > GameCube > Combined Joycon > Left Joycon > Right Joycon
        def get_priority(vc):
            if vc.is_single():
                c = vc.controllers[0]
                if c.is_pro_controller(): return 0
                if c.controller_info.product_id == NSO_GAMECUBE_CONTROLLER_PID: return 1
                if c.is_joycon_left(): return 3
                if c.is_joycon_right(): return 4
            else:
                # Combined Joycon pair
                return 2
            return 5

        active_vcs.sort(key=get_priority)
        
        new_list = [None] * 10
        for i, vc in enumerate(active_vcs):
            new_list[i] = vc
            vc.player_number = i + 1
        
        VIRTUAL_CONTROLLERS[:] = new_list

def set_shutting_down(val):
    global IS_SHUTTING_DOWN
    IS_SHUTTING_DOWN = val

def set_suspending(val):
    global _IS_SUSPENDING
    _IS_SUSPENDING = val

def emergency_cleanup():
    """Forcefully clear VIRTUAL_CONTROLLERS without waiting for a loop."""
    global VIRTUAL_CONTROLLERS
    logger.info("Emergency cleanup: Force clearing all stale controllers.")
    for i in range(len(VIRTUAL_CONTROLLERS)):
        vc = VIRTUAL_CONTROLLERS[i]
        if vc is not None:
            try:
                vc.force_close()
            except:
                pass
        VIRTUAL_CONTROLLERS[i] = None
        
    # Detach all possible USBIP ports to clear stale attachments
    try:
        from virtual_controller import detach_all_usbip_devices
        detach_all_usbip_devices()
    except Exception as e:
        logger.debug(f"Detach USBIP ports in emergency_cleanup failed: {e}")
    
    try:
        from virtual_controller import reset_vigem_bus
        reset_vigem_bus()
    except Exception as e:
        logger.debug(f"Reset bus in emergency_cleanup failed: {e}")
        
    if UPDATE_CALLBACK:
        UPDATE_CALLBACK(list(VIRTUAL_CONTROLLERS))

async def update_all_player_leds():
    for vc in VIRTUAL_CONTROLLERS:
        if vc is not None:
            for c in vc.controllers:
                await c.set_leds(vc.player_number)

async def _split_controller_async(vc_index):
    global GLOBAL_LOCK
    if GLOBAL_LOCK is None:
        return
    new_vc = None
    async with GLOBAL_LOCK:
        vc = VIRTUAL_CONTROLLERS[vc_index]
        if vc is not None and not vc.is_single():
            c2 = vc.controllers.pop()
            await vc.init_added_controller(vc.controllers[0]) # reinit first
            
            slot_index = next(i for i, c in enumerate(VIRTUAL_CONTROLLERS) if c == None)
            new_vc = VirtualController(slot_index + 1, [c2], DISCONNECT_CALLBACK, setup_usb=False)
            
            if vc.mode == "Switch1":
                # Switch1: split without resetting USBIP. Transfer the appropriate server to new_vc.
                with vc.state_lock, new_vc.state_lock:
                    new_vc.mode = "Switch1"
                    new_vc.hold_mode = "Vertical"
                    new_vc.driver_type = "USBIP"
                    class MockGamepad:
                        def register_notification(self, callback_function): pass
                        def unregister_notification(self): pass
                        def update(self): pass
                        def close(self): pass
                    new_vc.vg_controller = MockGamepad()
                    
                    if c2.is_joycon_right():
                        new_vc.usbip_server_r = getattr(vc, 'usbip_server_r', None)
                        new_vc.server_port_r = getattr(vc, 'server_port_r', None)
                        new_vc.bus_id_r = getattr(vc, 'bus_id_r', None)
                        new_vc.host_ip_r = getattr(vc, 'host_ip_r', None)
                        if new_vc.usbip_server_r:
                            new_vc.usbip_server_r.on_rumble_callback = lambda d, p=new_vc.server_port_r: new_vc._usbip_rumble_callback(d, side="Right")
                        vc.usbip_server_r = None
                    elif c2.is_joycon_left():
                        new_vc.usbip_server_l = getattr(vc, 'usbip_server_l', None)
                        new_vc.server_port_l = getattr(vc, 'server_port_l', None)
                        new_vc.bus_id_l = getattr(vc, 'bus_id_l', None)
                        new_vc.host_ip_l = getattr(vc, 'host_ip_l', None)
                        if new_vc.usbip_server_l:
                            new_vc.usbip_server_l.on_rumble_callback = lambda d, p=new_vc.server_port_l: new_vc._usbip_rumble_callback(d, side="Left")
                        vc.usbip_server_l = None

            VIRTUAL_CONTROLLERS[slot_index] = new_vc
            await new_vc.init_added_controller(c2)
            
            reorder_controllers()

            if UPDATE_CALLBACK is not None:
                UPDATE_CALLBACK(list(VIRTUAL_CONTROLLERS))
                
            await update_all_player_leds()

    if new_vc is not None:
        if vc.mode == "Switch1":
            pass # Handled above
        else:
            with vc.state_lock:
                vc.cleanup_vg_controller()
            await asyncio.to_thread(vc.setup_virtual_device)
            await asyncio.to_thread(new_vc.setup_virtual_device)


def split_controller(vc_index):
    if DISCOVERER_LOOP and DISCOVERER_LOOP.is_running():
        asyncio.run_coroutine_threadsafe(_split_controller_async(vc_index), DISCOVERER_LOOP)

async def _merge_controllers_async(vc_index1, vc_index2):
    global GLOBAL_LOCK
    if GLOBAL_LOCK is None:
        return
    async with GLOBAL_LOCK:
        # Ensure vc_index1 is the lower index to prioritize Player 1
        if vc_index1 > vc_index2:
            vc_index1, vc_index2 = vc_index2, vc_index1
            
        vc1 = VIRTUAL_CONTROLLERS[vc_index1]
        vc2 = VIRTUAL_CONTROLLERS[vc_index2]
        
        if vc1 is not None and vc2 is not None and vc1.is_single() and vc2.is_single():
            c2 = vc2.controllers[0]
            
            # Switch1 Emu Mode: extract the usbip servers from vc2 BEFORE removing the controller
            # so remove_controller's cleanup won't shut them down!
            if vc1.mode == "Switch1":
                with vc1.state_lock, vc2.state_lock:
                    if c2.is_joycon_right():
                        vc1.usbip_server_r = getattr(vc2, 'usbip_server_r', None)
                        vc1.server_port_r = getattr(vc2, 'server_port_r', None)
                        vc1.bus_id_r = getattr(vc2, 'bus_id_r', None)
                        vc1.host_ip_r = getattr(vc2, 'host_ip_r', None)
                        if vc1.usbip_server_r:
                            vc1.usbip_server_r.on_rumble_callback = lambda d, p=vc1.server_port_r: vc1._usbip_rumble_callback(d, side="Right")
                        vc2.usbip_server_r = None
                        vc2.server_port_r = None
                    elif c2.is_joycon_left():
                        vc1.usbip_server_l = getattr(vc2, 'usbip_server_l', None)
                        vc1.server_port_l = getattr(vc2, 'server_port_l', None)
                        vc1.bus_id_l = getattr(vc2, 'bus_id_l', None)
                        vc1.host_ip_l = getattr(vc2, 'host_ip_l', None)
                        if vc1.usbip_server_l:
                            vc1.usbip_server_l.on_rumble_callback = lambda d, p=vc1.server_port_l: vc1._usbip_rumble_callback(d, side="Left")
                        vc2.usbip_server_l = None
                        vc2.server_port_l = None
                        
            await vc2.remove_controller(c2)
            VIRTUAL_CONTROLLERS[vc_index2] = None
            
            vc1.add_controller(c2)
            await vc1.init_added_controller(c2)
            
            reorder_controllers()

            if UPDATE_CALLBACK is not None:
                UPDATE_CALLBACK(list(VIRTUAL_CONTROLLERS))
                
            await update_all_player_leds()
            
            if vc1.mode == "Switch1":
                pass # We already transferred it above, nothing else to do here
            else:
                with vc1.state_lock:
                    vc1.cleanup_vg_controller()
                await asyncio.to_thread(vc1.setup_virtual_device)

def merge_controllers(vc_index1, vc_index2):
    if DISCOVERER_LOOP and DISCOVERER_LOOP.is_running():
        asyncio.run_coroutine_threadsafe(_merge_controllers_async(vc_index1, vc_index2), DISCOVERER_LOOP)

if __name__ == "__main__":
    start_discoverer(None, threading.Event())
