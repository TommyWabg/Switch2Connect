"""Player actions moved from the classic PlayerInfoBlock."""
import asyncio
import logging

logger = logging.getLogger(__name__)

def ping(vc, schedule):
    from controller import VibrationData
    if vc is not None and getattr(vc, 'loop', None):
        vib = VibrationData(lf_amp=800, hf_amp=800)
        off = VibrationData(lf_amp=0, hf_amp=0)
        for controller in vc.controllers:
            asyncio.run_coroutine_threadsafe(controller.set_vibration(vib, vib, vib, ignore_freq_scaling=True, pair_sustain=False), vc.loop)
            schedule(100, lambda c=controller, loop=vc.loop, o=off:
                asyncio.run_coroutine_threadsafe(c.set_vibration(o, o, o, ignore_freq_scaling=True, pair_sustain=False), loop))
            schedule(200, lambda c=controller, loop=vc.loop, v=vib:
                asyncio.run_coroutine_threadsafe(c.set_vibration(v, v, v, ignore_freq_scaling=True, pair_sustain=False), loop))
            schedule(300, lambda c=controller, loop=vc.loop, o=off:
                asyncio.run_coroutine_threadsafe(c.set_vibration(o, o, o, ignore_freq_scaling=True, pair_sustain=False), loop))


def set_hold_mode(vc, config, val, refresh_image):
    if vc is not None:
        vc.hold_mode = val
        refresh_image()

        # Save hold mode mapped by MAC address for single joycons
        if vc.is_single() and len(vc.controllers) > 0:
            c = vc.controllers[0]
            if c.is_joycon():
                addr = c.device.address
                config.joycon_hold_mode[addr] = val
                config.save_config()

def toggle_gyro_side(vc, config, val, refresh):
    if vc is not None:
        djg_enabled = getattr(config, "djg_enabled", False)
        djg_mode = getattr(config, "djg_mode", "Single Side Toggle")

        if djg_enabled and djg_mode != "Switch Gyro Side":
            if val == "Left":
                vc.djg_left_active = not getattr(vc, 'djg_left_active', True)
            elif val == "Right":
                vc.djg_right_active = not getattr(vc, 'djg_right_active', True)
        else:
            vc.active_gyro_side = val
            if djg_mode == "Switch Gyro Side":
                config.djg_dominant_side = val
                config.save_config()

            if not vc.is_single() and len(vc.controllers) == 2:
                left_mac = None
                right_mac = None
                for c in vc.controllers:
                    if c.is_joycon_left():
                        left_mac = c.device.address
                    elif c.is_joycon_right():
                        right_mac = c.device.address
                if left_mac and right_mac:
                    key = f"{left_mac}+{right_mac}"
                    config.merged_gyro_side[key] = val
                    config.save_config()
        refresh()

def merge_select(vc, pending_merge_vc_index, registry, merge, refresh):
    if vc is not None:
        vc_index = vc.player_number - 1
        if pending_merge_vc_index is None:
            pending_merge_vc_index = vc_index
        elif pending_merge_vc_index == vc_index:
            pending_merge_vc_index = None
        else:
            v1 = registry[pending_merge_vc_index]
            v2 = vc
            is_opposite = (v1.is_single_joycon_left() and v2.is_single_joycon_right()) or \
                          (v1.is_single_joycon_right() and v2.is_single_joycon_left())

            if is_opposite:
                merge(pending_merge_vc_index, vc_index)
                pending_merge_vc_index = None
            else:
                pending_merge_vc_index = vc_index

        refresh(list(registry))
    return pending_merge_vc_index

async def disconnect_merged_sequential(vc, spacing=0.3):
    from controller import INPUT_REPORT_UUID, COMMAND_RESPONSE_UUID
    async with vc._disconnect_lock:
        if not getattr(vc, 'running', False) and vc.vg_controller is None and not vc.controllers:
            return

        vc.running = False
        import time
        import gc
        current_time = time.strftime("%H:%M:%S")
        logger.info(f"[{current_time}] Player {vc.player_number} (Merged): Starting safe sequential disconnect sequence...")

        # Wait for the update thread to finish before proceeding with handle cleanup
        if hasattr(vc, 'update_thread') and vc.update_thread.is_alive():
            logger.info(f"Player {vc.player_number}: Waiting for update thread to exit...")
            vc.update_thread.join(timeout=0.5)
            if vc.update_thread.is_alive():
                logger.warning(f"Player {vc.player_number}: Update thread did not exit in time!")

        if not vc.controllers and vc.vg_controller is None:
            return

        logger.info(f"Player {vc.player_number}: Cleaning up virtual device and physical connections sequentially...")

        with vc.state_lock:
            if hasattr(vc, 'vg_controller') and vc.vg_controller is not None:
                logger.info(f"Player {vc.player_number}: Unregistering notifications and clearing vg_controller")
                try:
                    vc.vg_controller.unregister_notification()
                except Exception as e:
                    logger.debug(f"Unregister notification failed: {e}")
                if hasattr(vc.vg_controller, 'cmp_func'):
                    vc.vg_controller.cmp_func = None
                if hasattr(vc.vg_controller, 'close'):
                    try:
                        vc.vg_controller.close()
                    except Exception:
                        pass
                vc.vg_controller = None

        gc.collect()

        # Disconnect each physical controller sequentially with a delay to prevent Windows BLE driver bottlenecks
        for c in list(vc.controllers):
            c.interp_running = False
            if hasattr(c, 'interp_thread') and c.interp_thread.is_alive():
                logger.info(f"Controller {c.device.address}: Joining interpolation thread (non-blocking)...")
                try:
                    await asyncio.to_thread(c.interp_thread.join, 0.5)
                except Exception as e:
                    logger.warning(f"Failed to join interpolation thread: {e}")

            if hasattr(c, 'client') and c.client and c.client.is_connected:
                logger.info(f"Safe Disconnect: Disconnecting {c.device.address}...")
                try:
                    await c.client.stop_notify(INPUT_REPORT_UUID)
                except Exception:
                    pass
                try:
                    await c.client.stop_notify(COMMAND_RESPONSE_UUID)
                except Exception:
                    pass

                try:
                    await asyncio.wait_for(c.client.disconnect(), timeout=2.5)
                except Exception as e:
                    logger.debug(f"Bluetooth disconnect error (ignored): {e}")

            # Call the disconnect callback while c.client is still not None to completely avoid AttributeError
            if vc.on_disconnected_callback:
                try:
                    await vc.on_disconnected_callback(c)
                except Exception as e:
                    logger.error(f"Error in on_disconnected_callback: {e}")

            c.client = None
            await asyncio.sleep(spacing)

        vc.controllers.clear()
        logger.info(f"Player {vc.player_number} (Merged): Safe sequential disconnect complete.")

def disconnect(vc, spacing=0.3):
    if not vc.is_single():
        if vc.loop and vc.loop.is_running():
            return asyncio.run_coroutine_threadsafe(disconnect_merged_sequential(vc, spacing), vc.loop)
        else:
            logger.error("Event loop not found or not running for merged controller.")
    else:
        return vc.trigger_disconnect()
    return None


def split(vc, split_controller):
    if vc is not None:
        vc_index = vc.player_number - 1
        split_controller(vc_index)


def toggle_mag_calibration(controller):
    if controller is None:
        return
    if not getattr(controller, 'is_mag_calibrating', False):
        controller.start_mag_calibration()
    else:
        controller.stop_mag_calibration()

def gyro_countdown(controllers, schedule, render, tick_ms=1000):
    render("Starting in 3..", "starting")
    schedule(tick_ms, lambda: render("Starting in 2..", "countdown"))
    schedule(2 * tick_ms, lambda: render("Starting in 1..", "countdown"))
    def start_actual_calibration():
        for vc in controllers():
            if vc is not None:
                vc.start_calibration()
        render("Calibrating 5..", "starting")
        schedule(tick_ms, lambda: render("Calibrating 4..", "countdown"))
        schedule(2 * tick_ms, lambda: render("Calibrating 3..", "countdown"))
        schedule(3 * tick_ms, lambda: render("Calibrating 2..", "countdown"))
        schedule(4 * tick_ms, lambda: render("Calibrating 1..", "countdown"))
        schedule(5 * tick_ms, lambda: render("Calibration Done", "done"))
    schedule(3 * tick_ms, start_actual_calibration)
