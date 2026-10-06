"""Discovery and power-shutdown operations from the classic interface."""
import asyncio
import logging
import queue
import threading
import time

from .helpers import _set_current_thread_priority

logger = logging.getLogger(__name__)

def stop_discoverer(owner, join_timeout=5.0):
    if hasattr(owner, 'discoverer_thread') and owner.discoverer_thread and owner.discoverer_thread.is_alive():
        logger.info("Stopping discoverer thread...")
        owner.quit_event.set()
        owner.discoverer_thread.join(timeout=join_timeout)
        owner.discoverer_thread = None

def start_discoverer(owner, startup_bridge_context=None, join_timeout=5.0):
    stop_discoverer(owner, join_timeout)
    owner.quit_event.clear()

    def run():
        _set_current_thread_priority(1)
        from discoverer import start_discoverer
        start_discoverer(owner.discoverer_callback, owner.quit_event, startup_bridge_context)

    logger.info("Starting discoverer thread...")
    owner.discoverer_thread = threading.Thread(target=run, daemon=True)
    owner.discoverer_thread.start()

def shutdown_virtual_devices(owner, detach_timeout=1.0):
    """Detach USBIP first, then stop every virtual-device server.

    Windows must not be allowed to tear down this process while usbip-win2
    still has an attached device backed by our user-mode socket.  Mark every
    controller terminal before detaching so a PS5 disconnect callback cannot
    race the shutdown by starting its reconnect worker.
    """
    vcs = [vc for vc in getattr(owner, 'current_controllers', []) or [] if vc is not None]
    try:
        from discoverer import VIRTUAL_CONTROLLERS
        for vc in VIRTUAL_CONTROLLERS:
            if vc is not None and vc not in vcs:
                vcs.append(vc)
    except Exception:
        pass
    for vc in vcs:
        try:
            vc.running = False
            vc._suppress_usbip_reconnect = True
            with vc._usbip_reconnect_lock:
                vc._usbip_reconnect_generation += 1
                vc._usbip_reconnect_active = False
            for controller in getattr(vc, 'controllers', []) or []:
                controller.interp_running = False
                controller.suspended = True
                controller._is_suspending = True
        except Exception:
            logger.debug("Failed to mark virtual controller for power shutdown", exc_info=True)

    # One inventory query handles every player.  This must happen before any
    # server/socket is stopped, otherwise the kernel USBIP client observes an
    # abrupt transport loss and can display an error during Windows shutdown.
    try:
        from virtual_controller import detach_all_usbip_devices
        detach_all_usbip_devices(timeout=detach_timeout)
    except Exception:
        logger.debug("Power shutdown USBIP detach failed", exc_info=True)

    for vc in vcs:
        try:
            vc.force_close(usbip_already_detached=True)
        except Exception:
            logger.debug("Power shutdown virtual-device close failed", exc_info=True)


def handle_power_event(owner, wparam, module, clear_ui, start_discovery, restart_after, suspend_settle=1.0):
    current_time = time.strftime("%H:%M:%S")
    if wparam == 4:
        logger.info(f"[{current_time}] System Suspend detected (PBT_APMSUSPEND). Starting cleanup...")
        module("discoverer").set_suspending(True)
        # Release the shared virtual/standard keyboard before controller
        # threads stop producing edges; otherwise a held mapping can remain
        # logically down across sleep until the next physical transition.
        module("keyboard_output").release_all()

        # Match the proven 2.1 suspend path.  Do not touch CDC here: once the
        # host sleeps, heartbeat stops naturally and firmware releases BLE via
        # its host-lease timeout without a serial write/close race.
        if hasattr(owner, 'current_controllers'):
            for vc in owner.current_controllers:
                if vc is not None:
                    vc.running = False
                    vc.reset_inputs()
                    for c in vc.controllers:
                        c.interp_running = False
                        c.suspended = True
                        c._is_suspending = True
                        c._interp_wake_event.set()
                        c._release_standard_mouse_buttons()
                    vc.force_close()
        # VMouse devices represent live physical connections; do not leave
        # their handles or latched buttons alive across system sleep.
        module("raw_input_mouse").shutdown()

        # CRITICAL: Reset the ViGEm bus singleton to release the driver handle entirely
        reset_vigem_bus = module("virtual_controller").reset_vigem_bus
        reset_vigem_bus()

        # Preserve the 2.1 settling interval for OS driver handle cleanup.
        time.sleep(suspend_settle)

        owner.quit_event.set()
        owner._is_restarting_discovery = False
        logger.info(f"[{current_time}] Suspend preparation complete. quit_event set.")

    elif wparam in [7, 0x0012]: # PBT_APMRESUMESUSPEND or PBT_APMRESUMEAUTOMATIC
        event_name = "PBT_APMRESUMESUSPEND" if wparam == 7 else "PBT_APMRESUMEAUTOMATIC"
        logger.info(f"[{current_time}] System Resume detected ({event_name}).")

        # Reset suspension state immediately
        module("discoverer").set_suspending(False)
        owner.quit_event.clear()

        # CRITICAL: Force immediate cleanup of any potentially stale handles that survived
        # This also re-initializes the ViGEm bus singleton via its internal call.
        module("discoverer").emergency_cleanup()

        # Force UI to clear old/stale controller displays immediately
        clear_ui()

        logger.info(f"[{current_time}] quit_event cleared. UI cleared. Preparing to restart discovery...")

        if getattr(owner, '_is_restarting_discovery', False):
            logger.info("Restart already in progress. Skipping...")
            return
        owner._is_restarting_discovery = True

        def restart():
            try:
                # Longer delay to ensure Bluetooth radio and driver handles are stable
                # 7 seconds is safer for some slower BT adapters on wake

                if not getattr(owner, '_is_restarting_discovery', False): return

                # Double-check we didn't suspend again during the sleep
                _IS_SUSPENDING = module("discoverer")._IS_SUSPENDING
                if _IS_SUSPENDING:
                    logger.info("System is suspending again. Aborting restart.")
                    owner._is_restarting_discovery = False
                    return

                logger.info("Restarting discovery loop...")
                start_discovery()
            except Exception as e:
                logger.error(f"Restart failed: {e}")
            finally:
                owner._is_restarting_discovery = False

        restart_after(7.0, restart)


def restart_in_thread(seconds, callback):
    def restart():
        time.sleep(seconds)
        callback()
    threading.Thread(target=restart, daemon=True).start()


def quit_worker(prepare, done, spacing=0.3, settle=3.5, timeout=20.0):
    try:
        vcs = prepare()
        if vcs:
            async def disconnect():
                for vc in vcs:
                    if hasattr(vc, 'vg_controller') and vc.vg_controller:
                        try: vc.vg_controller.unregister_notification()
                        except: pass
                    for c in vc.controllers[:]:
                        if c.client and c.client.is_connected:
                            await c.disconnect()
                            await asyncio.sleep(spacing)
                await asyncio.sleep(settle)

            fut = asyncio.run_coroutine_threadsafe(disconnect(), vcs[0].loop)
            try:
                # Increased timeout protection to 20 seconds to ensure clean sequential shutdown for 3+ controllers
                fut.result(timeout=timeout)
            except:
                pass
        # Issue 5: even if there were no active virtual controllers (e.g. a
        # controller was mid-connect), make sure the ESP32-S3 stops scanning,
        # disables auto-connect and drops any remaining BLE links before exit,
        # so nothing stays connected and the bridge idles until the next launch.
        try:
            from usb_serial_bridge import shutdown_all_bridges
            shutdown_all_bridges()
        except Exception:
            pass
    except: pass
    finally: done()


class BackendContext:
    def __init__(self, owner, fields=None, getters=None, **callbacks):
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_fields", fields or {})
        object.__setattr__(self, "_getters", getters or {})
        object.__setattr__(self, "_callbacks", callbacks)

    def __getattr__(self, name):
        if name in self._callbacks:
            return self._callbacks[name]
        if name in self._getters:
            return self._getters[name]()
        field = self._fields.get(name, name)
        return self._owner[field] if isinstance(self._owner, dict) else getattr(self._owner, field)

    def __setattr__(self, name, value):
        field = self._fields.get(name, name)
        if isinstance(self._owner, dict):
            self._owner[field] = value
        else:
            setattr(self._owner, field, value)
        changed = self._callbacks.get("on_write")
        if changed is not None:
            changed(name, value)

def poll_wired_device_events(s):
    if getattr(s, 'is_quitting', False):
        return
    # Keep the latest of each kind: the queue now carries wired-controller events and
    # Bluetooth-radio events, and collapsing to a single "latest" would let one kind
    # swallow the other.
    latest = {}
    try:
        q = getattr(s, "wired_device_event_queue", None)
        if q is not None:
            while True:
                try:
                    event = q.get_nowait()
                except queue.Empty:
                    break
                latest[event.get("kind", "wired")] = event
    except Exception:
        latest = {}

    wired_event = latest.get("wired")
    if wired_event:
        s.schedule_rescan(
            wired_event.get("reason", "device_arrival"),
            wired_event.get("path"),
        )

    if latest.get("bluetooth_radio"):
        try:
            from discoverer import notify_bluetooth_radio_changed
            notify_bluetooth_radio_changed()
        except Exception:
            logger.debug("Bluetooth radio change notification failed", exc_info=True)


def schedule_wired_rescan(s, config, schedule, cancel, rescan, reason="device_arrival", candidate_path=None, delay_ms=500):
    if not bool(getattr(config, "wired_auto_scan_enabled", getattr(config, "wired_usb_enabled", True))):
        return
    pending = getattr(s, "_wired_device_change_after_id", None)
    if pending is not None:
        try:
            cancel(pending)
        except Exception:
            pass
    s._wired_device_change_after_id = schedule(
        delay_ms,
        lambda r=reason, p=candidate_path: rescan(r, candidate_path=p),
    )

def battery_refresh(s, refresh):
    if not getattr(s, 'is_quitting', False):
        if hasattr(s, 'current_controllers') and s.current_controllers:
            try:
                refresh(s.current_controllers)
            except Exception as e:
                logger.debug(f"Failed to refresh battery indicators: {e}")


def connection_refresh(is_quitting, power_saving, callbacks):
    if not is_quitting:
        if not power_saving.is_full():
            for callback in callbacks:
                callback()


def startup_driver_steps(check, initialize_keyboard, refresh_combo, start_discovery, context=None):
    try:
        yield check()
        initialize_keyboard()
        refresh_combo()
    except Exception as e:
        logger.debug(f"Startup driver check failed: {e}")
    start_discovery(context)
