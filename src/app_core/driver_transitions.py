"""Classic output-driver and emulation-mode transitions."""
import asyncio
import logging
import os
import time

from .models import ActionResult

logger = logging.getLogger(__name__)


def settle_pnp(seconds, pump):
    end_t = time.time() + seconds
    while time.time() < end_t:
        pump()
        time.sleep(0.01)

def set_simulation_mode(config, val, controllers, ui_call):
    ui_call("close_joystick_custom_popup")
    # Do not reload CONFIG while asynchronous saves may be pending.

    old_mode = getattr(config, "simulation_mode", "PS5")

    if hasattr(config, 'active_profile') and config.active_profile in config.profiles:
        config.profiles[config.active_profile]["simulation_mode"] = val

    if old_mode == val:
        config.save_config()
        return ActionResult(True)

    config.simulation_mode = val
    driver_type = getattr(config, "driver_type", "WinUHid")
    if driver_type == "ViGEmBus":
        config.vigembus_sim_mode = val
    elif driver_type == "USBIP":
        config.usbip_sim_mode = val
    else:
        config.winuhid_sim_mode = val

    success = True
    reverted_vcs = []
    if controllers is not None:
        try:
            for vc in controllers:
                if vc is not None:
                    vc.set_mode(val)
                    reverted_vcs.append(vc)
        except Exception as e:
            logger.error(f"Failed to switch emulation mode: {e}")
            success = False

    if not success:
        # Revert config memory values by reloading from disk
        config.load_config()
        # Revert set_mode on already switched controllers
        for vc in reverted_vcs:
            if vc is not None:
                try:
                    vc.set_mode(old_mode)
                except Exception:
                    pass
        # Revert the UI switch
        ui_call("simulation_value", old_mode)
    else:
        config.save_config()
        ui_call("update_dynamic_rumble_mode_options")

    ui_call("close_joystick_custom_popup")
    ui_call("close_in_app_gyro_popup")
    ui_call("refresh_joycon_ir_sensor_buttons")
    ui_call("_refresh_mapping_comboboxes")
    ui_call("force_refresh_player_slots")
    return ActionResult(success, None if success else "recreation_failed")

def switch_driver(config, val, persist_preference, controllers, ui, module, action,
                  ui_call, status_query, invalidate, settle, restart_pending):
    ui_call("close_joystick_custom_popup")
    # Redirect a saved/manual WinUHid selection only when the external driver
    # is actually unavailable.  MSIX never installs it from inside the app.
    if (module("utils").is_packaged()
            and getattr(config, "driver_fallback_active", False)
            and val == "WinUHid"):
        val = "ViGEmBus"
    # Do not reload CONFIG while asynchronous saves may be pending.

    old_driver = getattr(config, "driver_type", "WinUHid")
    old_sim_mode = getattr(config, "simulation_mode", "PS5")

    # A successful USBIP uninstall is not complete until Windows restarts.
    # Block the selection before changing profile/config state and repeat the
    # restart prompt even if the UI was switched to another driver meanwhile.
    if val == "USBIP" and restart_pending:
        ui_call("driver_value", old_driver)
        ui_call("_prompt_usbip_restart_required")
        return ActionResult(False, "restart_required")

    if (persist_preference and hasattr(config, 'active_profile')
            and config.active_profile in config.profiles):
        config.profiles[config.active_profile]["driver_type"] = val

    if old_driver == val:
        config.save_config()
        return ActionResult(True)

    # Check driver installation BEFORE updating config or recreating controllers!
    if val == "ViGEmBus":
        if not (yield action("check_vigembus_installation", save=False)):
            # Revert to old driver
            ui_call("driver_value", old_driver)
            return ActionResult(False, "driver_unavailable")
    elif val == "USBIP":
        usbip_exe = "C:\\Program Files\\USBip\\usbip.exe"

        def usbip_ready():
            invalidate("usbip")
            status = status_query("USBIP")
            return status.installed or (
                status.unknown and os.path.exists(usbip_exe)), status

        ready, usbip_status = usbip_ready()
        if not ready:
            partial = usbip_status.state == "partial"
            answer = ui.ask_yes_no(
                "Repair USBIP Driver" if partial else "Install USBIP Driver",
                (("USBIP is partially installed.\n\n" + usbip_status.describe() + "\n\n"
                  "Do you want to remove it now? Restart Windows before "
                  "installing USBIP again.\n")
                 if partial else
                 "The USBIP driver is required but is not installed.\n\n"
                 "Do you want to install it now?\n") +
                "(Requires administrator privileges and will temporarily reset USB connections.)"
            )
            if answer:
                if partial:
                    (yield action("run_usbip_uninstall"))
                    ui_call("driver_value", old_driver)
                    return ActionResult(False, "driver_unavailable")
                (yield action("run_usbip_install", show_success_msg=True))
                if not usbip_ready()[0]:
                    ui_call("driver_value", old_driver)
                    return ActionResult(False, "driver_unavailable")
            else:
                ui_call("driver_value", old_driver)
                return ActionResult(False, "driver_unavailable")
    else:
        winuhid_status = status_query("WinUHid")
        if winuhid_status.unknown and module("app_core.helpers").verify_winuhid_runtime(attempts=2):
            # State unreadable but the driver actually works - do not prompt.
            logger.warning("WinUHid status undetermined: %s", winuhid_status.describe())
            winuhid_status = None
        if winuhid_status is not None and not winuhid_status.installed:
            if getattr(config, 'driver_installed', False):
                config.driver_installed = False
                config.save_config()
                ui_call("update_driver_button")
            answer = ui.ask_yes_no(
                "Repair Virtual Controller Driver" if winuhid_status.state == "partial" else "Install Virtual Controller Driver",
                (("WinUHid is partially installed.\n\n" + winuhid_status.describe() + "\n\n"
                  "Do you want to clean up and reinstall it now?")
                 if winuhid_status.state == "partial" else
                 "WinUHid driver is not installed.\n\nDo you want to install it now?")
                + "\n(Requires administrator privileges.)"
            )
            if answer:
                if winuhid_status.state == "partial" and not (yield action("run_driver_uninstall")):
                    ui_call("driver_value", old_driver)
                    return ActionResult(False, "driver_unavailable")
                (yield action("run_driver_install", show_success_msg=False))
                invalidate("winuhid")
                installed_now = status_query("WinUHid")
                if not (installed_now.installed
                        or (installed_now.unknown and module("app_core.helpers").verify_winuhid_runtime(attempts=2))):
                    ui_call("driver_value", old_driver)
                    return ActionResult(False, "driver_unavailable")
            else:
                ui_call("driver_value", old_driver)
                return ActionResult(False, "driver_unavailable")

    # If we got here, checking was successful! Apply the mode switch in memory:
    config.driver_type = val
    if persist_preference:
        config.preferred_driver_type = val
        config.driver_fallback_active = False
    ui_call("update_driver_button")

    # Load the remembered simulation mode for the target driver
    if val == "ViGEmBus":
        config.simulation_mode = config.vigembus_sim_mode
    elif val == "USBIP":
        config.simulation_mode = config.usbip_sim_mode
    else:
        config.simulation_mode = config.winuhid_sim_mode

    # Update sim mode switch options and set value
    if val == "ViGEmBus":
        ui_call("simulation_options", ["Xbox360", "PS4"], ["Xbox360", "PS4"], config.simulation_mode)
    elif val == "USBIP":
        ui_call("simulation_options", ["Switch1", "Switch2", "PS5"], ["Switch1", "Switch2", "PS5"], config.simulation_mode)
    else:
        ui_call("simulation_options", ["Xbox One", "PS4", "PS5"], ["Xbox One", "PS4", "PS5"], config.simulation_mode)

    ui_call("update_dynamic_rumble_mode_options")

    # Apply the driver change to all running virtual controllers immediately
    success = True
    if controllers is not None:
        try:
            # Pass 1: Cleanly close all running virtual controllers
            for vc in controllers:
                if vc is not None:
                    with vc.state_lock:
                        if hasattr(vc, 'vg_controller') and vc.vg_controller is not None:
                            vc.cleanup_vg_controller()

            # Wait for PnP subsystem to settle
            import gc
            gc.collect()
            settle(0.5)

            # Pass 2: Recreate them under the new driver/mode sequentially
            for i, vc in enumerate(controllers):
                if vc is not None:
                    if i > 0:
                        settle(0.2)
                    with vc.state_lock:
                        vc.mode = config.simulation_mode
                        if vc.mode == "Switch1":
                            vc.hold_mode = "Vertical"
                        elif vc.is_single() and len(vc.controllers) > 0:
                            addr = vc.controllers[0].device.address
                            if addr in config.joycon_hold_mode:
                                vc.hold_mode = config.joycon_hold_mode[addr]
                            else:
                                vc.hold_mode = "Vertical"
                        vc._setup_vg_controller()
                    if vc.loop and vc.loop.is_running():
                        asyncio.run_coroutine_threadsafe(vc.update_leds(), vc.loop)
        except Exception as e:
            logger.error(f"Failed to recreate controllers during driver mode switch: {e}")
            success = False

    if not success:
        # Revert config memory values by reloading from disk
        config.load_config()
        # Revert the GUI switches
        ui_call("driver_value", old_driver)
        ui_call("update_driver_button")

        if old_driver == "ViGEmBus":
            ui_call("simulation_options", ["Xbox360", "PS4"], ["Xbox360", "PS4"], old_sim_mode)
        elif old_driver == "USBIP":
            ui_call("simulation_options", ["Switch1", "Switch2", "PS5"], ["Switch1", "Switch2", "PS5"], old_sim_mode)
        else:
            ui_call("simulation_options", ["Xbox One", "PS4", "PS5"], ["Xbox One", "PS4", "PS5"], old_sim_mode)

        # Recreate controllers under old config
        if controllers is not None:
            try:
                for vc in controllers:
                    if vc is not None:
                        with vc.state_lock:
                            vc.mode = old_sim_mode
                            if vc.mode == "Switch1":
                                vc.hold_mode = "Vertical"
                            elif vc.is_single() and len(vc.controllers) > 0:
                                addr = vc.controllers[0].device.address
                                if addr in config.joycon_hold_mode:
                                    vc.hold_mode = config.joycon_hold_mode[addr]
                                else:
                                    vc.hold_mode = "Vertical"
                            vc._setup_vg_controller()
                        if vc.loop and vc.loop.is_running():
                            asyncio.run_coroutine_threadsafe(vc.update_leds(), vc.loop)
            except Exception as re_err:
                logger.error(f"Failed to restore controllers to old driver: {re_err}")
    else:
        config.save_config()

    ui_call("close_joystick_custom_popup")
    ui_call("close_in_app_gyro_popup")
    ui_call("refresh_joycon_ir_sensor_buttons")
    ui_call("_refresh_mapping_comboboxes")
    ui_call("force_refresh_player_slots")
    return ActionResult(success, None if success else "recreation_failed")


def wake_controller_mouse_output_reconcile(controllers):
    """Wake every physical controller, including Full power-saving sleepers."""
    seen = set()
    for vc in controllers:
        for controller in (getattr(vc, "controllers", ()) or ()):
            identity = id(controller)
            if identity in seen:
                continue
            seen.add(identity)
            wake = getattr(controller, "_interp_wake_event", None)
            if wake is not None:
                wake.set()


def toggle_keyboard_output(keyboard_output, status_query, ui, refresh):
    try:
        result = ActionResult(True)
        if keyboard_output.effective_mode() == keyboard_output.RAW_INPUT:
            keyboard_output.activate_standard(save=True)
        else:
            status = status_query("WinUHid", use_cache=False)
            if status.state != "healthy":
                return ActionResult(False, "driver_unavailable")
            if not keyboard_output.activate_raw_input(save=True):
                ui.show_message(
                    "Keyboard Raw Input Error",
                    "The WinUHid driver is present, but the virtual keyboard could not "
                    "be created. Keyboard output remains Standard.")
                result = ActionResult(False, "raw_input_failed")
        return result
    finally:
        refresh()


def toggle_mouse_output(config, raw_input_mouse, status_query, wake, refresh):
    try:
        status = status_query("WinUHid", use_cache=False)
        effective_raw = (raw_input_mouse.requested_mode() == "Raw Input"
                         and status.state == "healthy")
        if effective_raw:
            config.mouse_output_mode = "Standard"
        else:
            if status.state != "healthy":
                return ActionResult(False, "driver_unavailable")
            config.mouse_output_mode = "Raw Input"
        config.save_config()
        wake()
        return ActionResult(True)
    finally:
        refresh()
