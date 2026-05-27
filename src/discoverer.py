"""A class used to find switch 2 controllers via Bluetooth
"""
import threading
from bleak import BleakScanner, BleakClient, BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError
import asyncio
import logging
import bluetooth
import yaml
from utils import to_hex, convert_mac_string_to_value, decodeu, show_notification
import time
from controller import Controller, ControllerInputData, NINTENDO_VENDOR_ID, CONTROLER_NAMES, VibrationData, NSO_GAMECUBE_CONTROLLER_PID
from virtual_controller import VirtualController
from config import CONFIG

logger = logging.getLogger(__name__)

NINTENDO_BLUETOOTH_MANUFACTURER_ID = 0x0553
VIRTUAL_CONTROLLERS = [None] * 8
UPDATE_CALLBACK = None
DISCOVERER_LOOP = None
DISCONNECT_CALLBACK = None
IS_SHUTTING_DOWN = False
DISCOVERY_LOCK = threading.Lock()
_CURRENTLY_DISCOVERING = False
_IS_SUSPENDING = False
GLOBAL_LOCK = None
CONNECTION_LOCK = None

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
            from discoverer import VIRTUAL_CONTROLLERS
            
            for vc in VIRTUAL_CONTROLLERS:
                if vc is not None and getattr(vc, 'running', False):
                    should_disconnect = False
                    for c in vc.controllers:
                        connected_at = getattr(c, 'connected_at', None)
                        if connected_at is not None and (now - connected_at) >= timeout:
                            should_disconnect = True
                            break
                    if should_disconnect:
                        logger.info(f"Auto Disconnect: Player {vc.player_number} connection duration exceeded limit. Disconnecting...")
                        vc.trigger_disconnect()
                        show_notification("Auto Disconnect", f"Player {vc.player_number} has been auto-disconnected after reaching the set time limit.")
        except Exception as e:
            logger.error(f"Error in auto_disconnect_checker: {e}")

async def run_discovery(update_controllers_threadsafe, quit_event):
    global VIRTUAL_CONTROLLERS, UPDATE_CALLBACK, DISCOVERER_LOOP, DISCONNECT_CALLBACK, _CURRENTLY_DISCOVERING
    global GLOBAL_LOCK, CONNECTION_LOCK
    
    with DISCOVERY_LOCK:
        if _CURRENTLY_DISCOVERING:
            logger.warning("Discovery already running. Skipping...")
            return
        _CURRENTLY_DISCOVERING = True
    
    UPDATE_CALLBACK = update_controllers_threadsafe
    DISCOVERER_LOOP = asyncio.get_running_loop()
    
    GLOBAL_LOCK = asyncio.Lock()
    CONNECTION_LOCK = asyncio.Lock()
    
    # NEW: Thoroughly cleanup stale controllers from previous session/sleep
    # This ensures a fresh state every time the discovery loop starts.
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
        from virtual_controller import detach_usbip_device
        for p in range(3240, 3248):
            detach_usbip_device(p)
    except Exception as e:
        logger.error(f"Error in initial USBIP port cleanup: {e}")
    
    if UPDATE_CALLBACK:
        UPDATE_CALLBACK(list(VIRTUAL_CONTROLLERS))
    
    try:
        host_mac_value = None
        connected_mac_addresses: list[str] = []
        
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

        # Tracks MACs that are in the process of connecting (but not yet established).
        # Separate from connected_mac_addresses so a failed attempt is immediately
        # retryable once the scanner sees the device advertising again.
        _connecting_macs: set[str] = set()

        async def add_controller(device: BLEDevice, paired: bool):
            nonlocal pending_connections_count
            controller = None
            try:
                # 1. Serialize BLE connection & pairing phase to prevent WinRT concurrency crashes
                async with CONNECTION_LOCK:
                    controller = Controller(device)
                    await controller.connect_ble()
                    logger.info(f"Connected to BLE for {device.address}")
                    controller.disconnected_callback = disconnected_controller
                    if not paired:
                        await controller.pair()
                        logger.info(f"Paired successfully to {device.address}")
                    # BLE connection confirmed — promote to connected so scanner won't retry
                    _connecting_macs.discard(device.address)
                    connected_mac_addresses.append(device.address)

                # 2. Perform GATT setup and calibration reading outside the CONNECTION_LOCK
                await controller.initialize()
                
                # 3. Trigger haptic feedback asynchronously in the background
                asyncio.create_task(controller.trigger_connection_haptics())

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

        async with BleakScanner(callback) as scanner:
            print("Presss a button on a paired controller, or hold sync button on an unpaired controller")
            await asyncio.get_event_loop().run_in_executor(None, quit_event.wait)
    finally:
        with DISCOVERY_LOCK:
            _CURRENTLY_DISCOVERING = False
        import time
        logger.info(f"[{time.strftime('%H:%M:%S')}] Discovery loop exited. Starting session cleanup...")
        # Use a copy to avoid issues if the list is modified during iteration
        vcs_to_disconnect = [vc for vc in VIRTUAL_CONTROLLERS if vc is not None]
        if vcs_to_disconnect:
            # CRITICAL: We now use is_suspending=False even during suspend
            # to ensure the ViGEmBus handles are closed cleanly.
            # Our "Triple Protection" in gui.py handles the wake-prevention.
            await asyncio.gather(*[vc.disconnect(is_suspending=False) for vc in vcs_to_disconnect])
        logger.info(f"[{time.strftime('%H:%M:%S')}] Discovery session cleanup complete.")

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
        
        new_list = [None] * 8
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
    # The actual cleanup of VIRTUAL_CONTROLLERS is now handled 
    # at the start of run_discovery() or via emergency_cleanup().

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
        from virtual_controller import detach_usbip_device
        for p in range(3240, 3248):
            detach_usbip_device(p)
    except Exception as e:
        logger.debug(f"Detach USBIP ports in emergency_cleanup failed: {e}")
    
    # Also reset the ViGEm bus handle to ensure driver stability
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
            # Use setup_usb=False so the USBIP attach runs outside the lock in a thread,
            # matching start_all_pending_virtual_usb and avoiding event-loop blocking.
            new_vc = VirtualController(slot_index + 1, [c2], DISCONNECT_CALLBACK, setup_usb=False)
            VIRTUAL_CONTROLLERS[slot_index] = new_vc
            await new_vc.init_added_controller(c2)
            
            reorder_controllers()

            if UPDATE_CALLBACK is not None:
                UPDATE_CALLBACK(list(VIRTUAL_CONTROLLERS))
                
            await update_all_player_leds()

    # Start the virtual USB device AFTER releasing the lock so subprocess calls
    # (detach + usbip attach) don't block the asyncio event loop or hold the lock.
    if new_vc is not None:
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
            await vc2.remove_controller(c2)
            VIRTUAL_CONTROLLERS[vc_index2] = None
            
            vc1.add_controller(c2)
            await vc1.init_added_controller(c2)
            
            reorder_controllers()

            if UPDATE_CALLBACK is not None:
                UPDATE_CALLBACK(list(VIRTUAL_CONTROLLERS))
                
            await update_all_player_leds()

def merge_controllers(vc_index1, vc_index2):
    if DISCOVERER_LOOP and DISCOVERER_LOOP.is_running():
        asyncio.run_coroutine_threadsafe(_merge_controllers_async(vc_index1, vc_index2), DISCOVERER_LOOP)

if __name__ == "__main__":
    start_discoverer(None, threading.Event())