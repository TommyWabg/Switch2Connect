"""Driver manager actions moved from the classic interface."""
import glob
import sys
import logging
import os

from . import driver_logs
from .models import ActionResult

logger = logging.getLogger(__name__)


class _Action:
    def __init__(self, action):
        self.action = action
        self.name = None

    def __call__(self, name, **kwargs):
        self.name = name
        return self.action(name, **kwargs)


def _result(ok, action, status):
    reason = None if ok else "cancelled" if action.name is None else "driver_action_failed"
    if not ok and status.state == "partial" and action.name in ("run_driver_uninstall", "run_vigembus_uninstall"):
        reason = "uninstall_failed"
    return ActionResult(bool(ok), reason)


def vigem_action(config, status_query, ui, action, refresh):
    ok = False
    action = _Action(action)
    vigem_status = status_query("ViGEmBus")
    if vigem_status.state == "healthy":
        if ui.ask_yes_no("Uninstall Driver", "Are you sure you want to uninstall the ViGEmBus driver?\n(Requires administrator privileges.)"):
            ok = (yield action("run_vigembus_uninstall"))
    elif vigem_status.state == "partial":
        if ui.ask_yes_no(
            "Repair Driver",
            "ViGEmBus is partially installed. Clean up all broken nodes and reinstall it?\n\n"
            f"{vigem_status.describe()}\n\n(Requires administrator privileges.)"
        ):
            if (yield action("run_vigembus_uninstall")):
                installed = ok = (yield action("install_vigembus_driver", show_success_msg=True))
                if installed:
                    config.vigembus_installed = True
                    config.save_config()
                refresh()
    else:
        installed = ok = (yield action("install_vigembus_driver", show_success_msg=True))
        if installed:
            config.vigembus_installed = True
            config.save_config()
        refresh()
    return _result(ok, action, vigem_status)


def winuhid_action(ui, status_query, action, refresh, restart_pending=False):
    ok = False
    action = _Action(action)
    status = status_query("WinUHid", use_cache=False)
    if status.state == "healthy":
        if ui.ask_yes_no(
                "Uninstall WinUHid Driver",
                "Uninstall WinUHid and use Win32 API output until it is reinstalled?\n"
                "(Requires administrator privileges.)"):
            ok = (yield action("run_driver_uninstall"))
    elif status.state == "partial":
        if ui.ask_yes_no(
                "Repair WinUHid Driver",
                "WinUHid is partially installed. Clean up the broken installation "
                "and reinstall it?\n\n" + status.describe()
                + "\n\n(Requires administrator privileges.)"):
            if (yield action("run_driver_uninstall")):
                ok = (yield action("run_driver_install"))
    else:
        ok = (yield action("run_driver_install"))
    refresh("manager")
    return _result(ok, action, status)


def usbip_action(ui, status_query, action, refresh, restart_pending=False):
    ok = False
    action = _Action(action)
    if restart_pending:
        refresh("restart")
        return ActionResult(False, "restart_required")
    install_warning = (
        "WARNING: During the installation of USBIP-win2, Windows USB hubs will restart briefly, "
        "which will temporarily disconnect other USB peripherals (mice, keyboards, etc.).\n\n"
        "Do you want to proceed?\n(Requires administrator privileges.)"
    )
    status = status_query("USBIP")
    if status.state == "healthy":
        if ui.ask_yes_no("Uninstall USBIP Driver", "Are you sure you want to uninstall the USBIP driver?\n(Requires administrator privileges.)"):
            ok = (yield action("run_usbip_uninstall"))
    elif status.state == "partial":
        if ui.ask_yes_no(
            "Repair USBIP Driver",
            "USBIP is partially installed. Remove the broken installation now?\n\n"
            f"{status.describe()}\n\nWindows must be restarted before USBIP can be installed again.\n\n"
            "Removing USBIP requires administrator privileges."
        ):
            ok = (yield action("run_usbip_uninstall"))
    else:
        if status.unknown:
            logger.warning("USBIP status undetermined: %s", status.describe())
        if ui.ask_yes_no(
            "Install USBIP Driver",
            "Are you sure you want to install the USBIP driver?\n\n" + install_warning
        ):
            ok = (yield action("run_usbip_install"))
    return _result(ok, action, status)


def hidhide_install(ui, module, script, sync, refresh, invalidate, status_query, prompt_restart=True):
    # Install HidHide from the bundled installer script (both builds); nothing downloaded.
    code = yield script("install_hidhide.ps1", "Installing HidHide...\nPlease authorize the UAC prompt if asked.")
    if code is None:
        return False  # cancelled / could not start
    ok = sync()
    refresh()

    if code not in (0, 3010) and not ok:
        invalidate("hidhide")
        ui.show_message(
            "Error",
            "HidHide installation did not complete.\n\n"
            f"Exit code: {code}\n{status_query('HidHide').describe()}")
        return False

    # Centered success notification (on the main window).
    ui.show_message("Success", "HidHide installed successfully.")

    # HidHide's filter driver needs a reboot to attach to already-connected
    # controllers. Ask the user first — never auto-restart.
    if prompt_restart and ui.ask_yes_no(
        "Restart Required",
        "A restart is required to finish HidHide setup and hide the physical "
        "controller.\n\nRestart now?",
    ):
        try:
            subprocess = module("subprocess")
            subprocess.Popen(["shutdown", "/r", "/t", "0"])
        except Exception as e:
            ui.show_message("Error", f"Could not restart automatically: {e}\nPlease restart manually.")
    return bool(ok or code in (0, 3010))


def hidhide_uninstall(ui, module, script, sync, refresh, invalidate, status_query, prompt_restart=True):
    # Uninstall from the bundled script (both builds); nothing downloaded.
    code = yield script("uninstall_hidhide.ps1", "Uninstalling HidHide...\nPlease authorize the UAC prompt if asked.")
    if code is None:
        return  # cancelled / could not start
    # An undeterminable state must not read as "removed" here, so require a
    # definite False before declaring the service gone.
    still = module("app_core.helpers").hidhide_service_state() is not False
    sync()
    refresh()

    if code not in (0, 3010) or still:
        log_path = os.path.join(
            os.environ.get("TEMP", ""), "Switch2Connect_HidHide_uninstall.log")
        try:
            with open(log_path, "r", encoding="utf-8-sig", errors="replace") as stream:
                details = stream.read().strip().splitlines()[-12:]
            detail_text = "\n\n" + "\n".join(details) if details else ""
        except OSError:
            detail_text = ""
        ui.show_message(
            "Error",
            "HidHide uninstallation failed or left the driver service installed."
            f"\n\nExit code: {code}{detail_text}",
        )
        return False

    # The service and package are gone, but the loaded driver file may remain
    # pending removal until reboot. Report the pending restart without restoring
    # the Installed state in the UI.
    ui.show_message(
        "Success",
        "HidHide removal started. A restart is required to complete the uninstall.",
    )
    if ui.ask_yes_no(
        "Restart Required",
        "A restart is required to finish removing HidHide.\n\nRestart now?",
    ):
        try:
            subprocess = module("subprocess")
            subprocess.Popen(["shutdown", "/r", "/t", "0"])
        except Exception as e:
            ui.show_message("Error", f"Could not restart automatically: {e}\nPlease restart manually.")
    return True


def verify_winuhid_install(module, invalidate, runtime_probe=None):
    invalidate("winuhid")
    driver_status = module("driver_install_helper").get_winuhid_status()
    # When the layers cannot be read, the runtime smoke test is the verdict;
    # otherwise a Win10 install that actually succeeded reports as failed.
    runtime_ok = ((driver_status.installed or driver_status.unknown)
                  and (runtime_probe or module("app_core.helpers").verify_winuhid_runtime)())
    return driver_status, runtime_ok


def finish_winuhid_install(config, code, driver_status, runtime_ok, ui, module, wake, reconcile, refresh, show_success_msg=True):
    driver_installed_ok = code == 0 and runtime_ok
    if driver_installed_ok:
        config.driver_installed = True
        # Device creation follows the active Profile, never Driver Mode.
        module("keyboard_output").initialize()
        config._bump_settings_generation()
        config.save_config()
        wake()
        reconcile()
        if show_success_msg:
            ui.show_message("Success", "WinUHid driver installed successfully.")
    else:
        install_log = driver_logs.read_winuhid_operation_log("install")
        ui.show_message(
            "Error",
            "Driver installation was not completed or failed.\n\n"
            f"Exit code: {code}\nRuntime smoke test: {runtime_ok}\n"
            f"{driver_status.describe()}\n\nInstaller details:\n{install_log}"
        )
        if getattr(config, "keyboard_output_mode", None) == module("keyboard_output").RAW_INPUT:
            module("keyboard_output").activate_raw_input(save=False)
    refresh()
    return bool(driver_installed_ok)


def verify_winuhid_uninstall(module, invalidate, code):
    invalidate("winuhid")
    driver_status = module("driver_install_helper").get_winuhid_status()
    driver_removed_ok = code == 0 and module("app_core.helpers").removal_verified(
        driver_status, module("app_core.helpers").verify_winuhid_runtime)
    return driver_status, driver_removed_ok


def finish_winuhid_uninstall(config, code, driver_status, driver_removed_ok, ui, module, wake, reconcile, refresh, switch_driver=None):
    if driver_removed_ok:
        config.driver_installed = False
        module("keyboard_output").activate_standard(save=False)
        module("raw_input_mouse").shutdown()
        # Preserve each Profile's requested modes.  With the driver
        # absent their effective output is Win32 API; reinstalling
        # restores the saved Profile preference.
        config._bump_settings_generation()
        config.save_config()
        wake()
        reconcile()
        if module("utils").is_packaged():
            module("config").refresh_packaged_winuhid_capability()
            # A removed active WinUHid cannot keep virtual devices
            # alive.  Move the current profile back to ViGEmBus;
            # the normal driver-change path performs its readiness
            # check and recreates the virtual controller safely.
            if getattr(config, "driver_type", "") == "WinUHid":
                yield switch_driver("ViGEmBus")
        ui.show_message("Success", "WinUHid driver uninstalled successfully.")
    else:
        uninstall_log = driver_logs.read_winuhid_operation_log("uninstall")
        ui.show_message(
            "Error",
            "Driver uninstallation failed or left WinUHid components behind.\n\n"
            f"Exit code: {code}\n{driver_status.describe()}"
            f"\n\nUninstaller details:\n{uninstall_log}"
        )
        if getattr(config, "keyboard_output_mode", None) == module("keyboard_output").RAW_INPUT:
            module("keyboard_output").activate_raw_input(save=False)
    refresh()
    return bool(driver_removed_ok)


def hide_detected_pro2_with_hidhide(module):
    try:
        hidhide = module("hidhide")
        enumerate_wired_controllers = module("usb_hid_controller").enumerate_wired_controllers
        for entry in enumerate_wired_controllers(reason="hidhide_action"):
            instance_id = hidhide.hid_path_to_instance_id(entry.get("path"))
            if instance_id:
                hidhide.hide_device(instance_id)
    except Exception as e:
        logger.debug("Failed to add detected wired controller to HidHide: %s", e)


def unhide_detected_pro2_with_hidhide(module):
    try:
        hidhide = module("hidhide")
        enumerate_wired_controllers = module("usb_hid_controller").enumerate_wired_controllers
        for entry in enumerate_wired_controllers(reason="hidhide_action"):
            instance_id = hidhide.hid_path_to_instance_id(entry.get("path"))
            if instance_id:
                hidhide.unhide_device(instance_id)
    except Exception as e:
        logger.debug("Failed to remove detected wired controller from HidHide: %s", e)


def sync_hidhide_installed(config, state, cache, save=True):
    """Refresh the cached and persisted HidHide flag without guessing.

    Returns the effective installed flag. An undeterminable state keeps the
    previous answer rather than persisting a wrong False.
    """
    if state is None:
        logger.warning("HidHide state undetermined; keeping the previous value.")
        return bool(getattr(config, "hidhide_installed", False))
    cache(state)
    config.hidhide_installed = state
    if save:
        config.save_config()
    return state


def toggle_auto_scan(config, set_enabled, refresh):
    val = not bool(getattr(config, "wired_auto_scan_enabled", getattr(config, "wired_usb_enabled", True)))
    config.wired_auto_scan_enabled = val
    config.wired_usb_enabled = val
    config.save_config()
    set_enabled(val)
    refresh()


def wired_inventory(controllers_info):
    detected = False
    wired_pids = set()
    for vc in controllers_info or []:
        if vc is None:
            continue
        for controller in getattr(vc, "controllers", []) or []:
            if controller is None:
                continue
            is_usb_hid = controller.__class__.__name__ == "USBHidController"
            if is_usb_hid or getattr(controller, "_hidhide_instance_id", None):
                detected = True
            if is_usb_hid:
                wired_pids.add(getattr(controller, "usb_product_id", 0x2069))
    return detected, sorted(wired_pids)


def run_driver_install(c, show_success_msg=True):
    if c.module("utils").is_packaged():
        c.ui.show_message(
            "Unavailable",
            "WinUHid Driver Mode is not available in the Microsoft Store version."
        )
        return False
    # Drivers are bundled in the package (both builds); install from the local
    # files below — never downloaded (Store policy 10.2.10.1 / 10.1.5).

    # Stop discoverer before installation
    discoverer_was_running = False
    if c.running():
        discoverer_was_running = True
        c.stop()
        
    # Run emergency cleanup to close all virtual controller handles immediately
    c.module("discoverer").emergency_cleanup()

    install_ps1 = c.module("config").get_driver_path("install_driver.ps1")
    if os.path.exists(install_ps1):
        try:
            proc_exit_code = [(yield c.process(
                "Driver Installation", "Installing WinUHid Driver...\nPlease authorize the UAC prompt if asked.",
                "powershell.exe", driver_logs.ps_hidden_args(install_ps1)))]
            if proc_exit_code[0] is None:
                c.ui.show_message("Error", "Driver installation was cancelled or failed to start (UAC prompt declined).")
                if discoverer_was_running:
                    c.start()
                return

            logger.info(f"Driver installer process exited with code: {proc_exit_code[0]}")
            
            driver_status, runtime_ok = verify_winuhid_install(
                c.module, c.invalidate, c.module("app_core.helpers").verify_winuhid_runtime)
            driver_installed_ok = finish_winuhid_install(
                c.config, proc_exit_code[0], driver_status, runtime_ok,
                c.ui, c.module, c.wake,
                c.output, c.refresh,
                show_success_msg)
        except Exception as e:
            c.ui.show_message("Error", f"Failed to start the installer: {e}")
    else:
        c.ui.show_message("Error", "Could not find install_driver.ps1. Please verify the integrity of the application files.")

    if discoverer_was_running:
        c.start()
    return bool(locals().get('driver_installed_ok', False))

def run_driver_uninstall(c):
    # MSIX may remove an externally installed WinUHid, but it must never
    # install or repair one.  The uninstall script is bundled locally so
    # this path does not download or acquire software at runtime.

    # Stop discoverer before uninstallation
    discoverer_was_running = False
    if c.running():
        discoverer_was_running = True
        c.stop()

    # Run emergency cleanup to close all virtual controller handles immediately
    c.module("discoverer").emergency_cleanup()

    uninstall_ps1 = c.module("config").get_driver_path("uninstall_driver.ps1")
    if os.path.exists(uninstall_ps1):
        try:
            proc_exit_code = [(yield c.process(
                "Driver Uninstallation", "Uninstalling WinUHid Driver...\nPlease authorize the UAC prompt if asked.",
                "powershell.exe", driver_logs.ps_hidden_args(uninstall_ps1)))]
            if proc_exit_code[0] is None:
                c.ui.show_message("Error", "Driver uninstallation was cancelled or failed to start (UAC prompt declined).")
                if discoverer_was_running:
                    c.start()
                return

            logger.info(f"Driver uninstaller process exited with code: {proc_exit_code[0]}")
            
            # Now that progress_win is destroyed, check if it was removed
            driver_status, driver_removed_ok = verify_winuhid_uninstall(
                c.module, c.invalidate, proc_exit_code[0])
            driver_removed_ok = (yield from finish_winuhid_uninstall(
                c.config, proc_exit_code[0], driver_status, driver_removed_ok,
                c.ui, c.module, c.wake,
                c.output, c.refresh,
                c.switch))
        except Exception as e:
            c.ui.show_message("Error", f"Failed to start the uninstaller: {e}")
    else:
        c.ui.show_message("Error", "Could not find uninstall_driver.ps1. Please verify the integrity of the application files.")

    if discoverer_was_running:
        c.start()
    return bool(locals().get('driver_removed_ok', False))

def run_vigembus_uninstall(c):
    # Uninstall from the bundled script (both builds); nothing downloaded.

    # Stop discoverer before uninstallation
    discoverer_was_running = False
    if c.running():
        discoverer_was_running = True
        c.stop()
        
    # Run emergency cleanup to close all virtual controller handles immediately
    c.module("discoverer").emergency_cleanup()

    uninstall_ps1 = c.module("config").get_driver_path("uninstall_vigembus.ps1")
    if os.path.exists(uninstall_ps1):
        try:
            proc_exit_code = [(yield c.process(
                "ViGEmBus Uninstallation", "Uninstalling ViGEmBus Driver...\nPlease authorize the UAC prompt if asked.",
                "powershell.exe", driver_logs.ps_hidden_args(uninstall_ps1)))]
            if proc_exit_code[0] is None:
                c.ui.show_message("Error", "ViGEmBus uninstallation was cancelled or failed to start (UAC prompt declined).")
                if discoverer_was_running:
                    c.start()
                return

            logger.info(f"ViGEmBus uninstaller process exited with code: {proc_exit_code[0]}")
            
            c.invalidate("vigembus")
            status = c.module("driver_install_helper").get_vigembus_status()
            driver_removed_ok = proc_exit_code[0] == 0 and c.module("app_core.helpers").removal_verified(
                status, c.module("app_core.helpers").verify_vigembus_runtime)
            if driver_removed_ok:
                c.config.vigembus_installed = False
                c.config.save_config()
                c.ui.show_message("Success", "ViGEmBus driver uninstalled successfully. A system reboot is highly recommended.")
            else:
                c.ui.show_message(
                    "Error",
                    "ViGEmBus uninstallation failed or left components behind.\n\n"
                    f"Exit code: {proc_exit_code[0]}\n{status.describe()}\n\n"
                    f"Uninstaller details:\n{driver_logs.read_vigembus_uninstall_log()}"
                )
            c.refresh()
        except Exception as e:
            c.ui.show_message("Error", f"Failed to start the uninstaller: {e}")
    else:
        c.ui.show_message("Error", "Could not find uninstall_vigembus.ps1. Please verify the integrity of the application files.")

    if discoverer_was_running:
        c.start()
    return bool(locals().get('driver_removed_ok', False))

def run_vigembus_install(c, show_success_msg=True):
    """Install ViGEmBus from the ViGEmBus MSI bundled inside the package (vgamepad),
    elevated and silent — no network access. Used by the MSIX/packaged build."""
    # Locate the bundled ViGEmBusSetup MSI (vgamepad ships it under win/vigem/install).
    roots = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        roots.append(base)
    roots.append(c.source_directory)
    try:
        roots.append(os.path.dirname(os.path.abspath(c.module("vgamepad").__file__)))
    except Exception:
        pass
    msi = None
    for root in roots:
        hits = glob.glob(os.path.join(root, "vgamepad", "win", "vigem", "install", "x64", "ViGEmBusSetup_x64.msi"))
        hits += glob.glob(os.path.join(root, "**", "ViGEmBusSetup_x64.msi"), recursive=True)
        if hits:
            msi = hits[0]
            break
    if not msi or not os.path.exists(msi):
        c.ui.show_message("Error", "Could not find the bundled ViGEmBus installer. Please verify the application files.")
        return False

    code = yield c.process(
        "Install ViGEmBus Driver", "Installing ViGEmBus driver...\nPlease authorize the UAC prompt if asked.",
        "msiexec.exe", f'/i "{msi}" /qn /norestart', collect_exit=False)
    if code is None:
        c.ui.show_message("Error", "ViGEmBus install was cancelled or failed to start (UAC prompt declined).")
        return

    c.invalidate("vigembus")
    ok = c.module("app_core.helpers").verify_vigembus_ready()
    if ok:
        if show_success_msg:
            c.ui.show_message("Success", "ViGEmBus driver installed successfully.")
    else:
        c.ui.show_message(
            "Error",
            "ViGEmBus installation was not completed. A system restart may be required; please try again if the issue persists.")
    return ok


def poll_process(handle, kernel, schedule, complete, collect_exit=True):
    import ctypes
    from ctypes import wintypes
    def check_process():
        if handle:
            res = kernel.WaitForSingleObject(handle, 0)
            if res == 258:
                schedule(200, check_process)
                return
            code = wintypes.DWORD()
            if collect_exit:
                kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel.CloseHandle(handle)
            complete(code.value)
        else:
            complete(None)
    schedule(200, check_process)


def toggle_hidhide(config, module, ui, schedule, alive, hide, unhide, complete):
    # Capture the intended target once, from the current live driver state.
    try:
        hidhide = module("hidhide")
        if module("app_core.helpers").hidhide_service_state() is not True:
            complete(False, "driver_unavailable")
            return
        target_active = not hidhide.is_active()
    except Exception as e:
        logger.error(f"Failed to read HidHide state: {e}")
        complete(False, "driver_unavailable")
        return

    max_attempts = 5

    def attempt(n):
        success = False
        try:
            hidhide = module("hidhide")
            if target_active:
                hide()
                hidhide.set_active(True)
            else:
                unhide()
                hidhide.set_active(False)
            # Verify the write actually landed rather than trusting the IOCTL return.
            success = (hidhide.is_active() == target_active)
        except Exception as e:
            logger.error(f"Failed to toggle HidHide (attempt {n}): {e}")

        if success:
            # Persist the preference so the wired watcher won't re-hide (and thus
            # re-activate) the controller on the next replug after a Disable.
            config.hidhide_hide_enabled = target_active
            config.save_config()
            complete(True, None)
            return
        if n < max_attempts and alive():
            # Retry until the driver confirms the new state (or attempts run out).
            schedule(150, lambda: attempt(n + 1))
            return
        complete(False, "verification_failed")
        if alive():
            ui.show_message(
                "Error",
                "Failed to apply HidHide setting.\n\nPlease make sure 'HidHide Configuration Client' is CLOSED."
            )

    # Give the kernel driver a moment before the first write/verify.
    schedule(50, lambda: attempt(1))


def wait_process(handle, kernel, sleep):
    pending, result = [], []
    poll_process(handle, kernel, lambda ms, callback: pending.append((ms, callback)), result.append)
    while pending:
        milliseconds, callback = pending.pop(0)
        sleep(milliseconds / 1000.)
        callback()
    return result[0]


def save_hidhide_prompt_suppression(config, checked):
    if checked:
        config.hidhide_install_prompt_suppressed = True
        config.save_config()


def ask_hidhide_auto_install(config, ui, label, save_in_ui=False):
    install, suppress = ui.ask_yes_no_opt_out(
        "Install HidHide",
        f"{label} detected.\n\n"
        "HidHide hides the controller's physical HID so games only see "
        "the virtual controller (no double input).\n\n"
        "Install it now?\n(Requires administrator privileges.)",
        opt_out_label="Do not show again")
    if not save_in_ui:
        save_hidhide_prompt_suppression(config, suppress)
    return bool(install)


def verify_usbip_install(c):
    verified_status = [None]
    verify_delays_ms = (0, 1000, 1000, 2000, 4000)
    def finish_verification(attempt=0):
        installed = False
        try:
            c.invalidate("usbip")
            status = c.module("driver_install_helper").get_usbip_status()
            verified_status[0] = status
            installed = status.installed or (
                status.unknown
                and os.path.exists("C:\\Program Files\\USBip\\usbip.exe"))
        except Exception as exc:
            logger.warning(
                "USBIP post-install verification attempt %s failed: %s",
                attempt + 1,
                exc,
            )
        if installed or attempt >= len(verify_delays_ms) - 1:
            return verified_status[0]
        yield verify_delays_ms[attempt + 1]
        return (yield from finish_verification(attempt + 1))
    return (yield from finish_verification())


def run_usbip_install(c, show_success_msg=True):
    # Install USBIP from the bundled installer (both builds); nothing downloaded.

    # Stop discoverer before installation
    discoverer_was_running = False
    if c.running():
        discoverer_was_running = True
        c.stop()

    # Run emergency cleanup to close all virtual controller handles immediately
    c.module("discoverer").emergency_cleanup()

    packaged_install = c.module("utils").is_packaged()
    install_payload = c.module("config").get_driver_path(
        "USBip-0.9.7.7-x64.exe" if packaged_install else "install_usbip.ps1")
    if os.path.exists(install_payload):
        try:
            if packaged_install:
                file = install_payload
                params = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /MERGETASKS="!desktopicon"'
                directory = os.path.dirname(install_payload)
            else:
                file, params, directory = "powershell.exe", driver_logs.ps_hidden_args(install_payload), None
            code, status = yield c.process(
                "USBIP Driver Installation", "Installing USBIP-win2 Driver...\nPlease authorize the UAC prompt if asked.",
                file, params, directory, verify=lambda: verify_usbip_install(c))
            if code is None:
                c.ui.show_message("Error", "USBIP driver installation was cancelled or failed to start (UAC prompt declined).")
                if discoverer_was_running:
                    c.start()
                return False
            proc_exit_code, verified_status = [code], [status]

            logger.info(f"USBIP driver installer process exited with code: {proc_exit_code[0]}")
            
            usbip_status = verified_status[0]
            if usbip_status is None:
                c.invalidate("usbip")
                usbip_status = c.module("driver_install_helper").get_usbip_status()
            usbip_installed_ok = usbip_status.installed or (
                usbip_status.unknown
                and os.path.exists("C:\\Program Files\\USBip\\usbip.exe"))
            if usbip_installed_ok:
                c.pending(False)
                if proc_exit_code[0] != 0:
                    logger.warning(
                        "USBIP installer returned code %s but final verification succeeded",
                        proc_exit_code[0],
                    )
                if show_success_msg:
                    c.ui.show_message("Success", "USBIP-win2 driver installed successfully.")
            else:
                c.ui.show_message(
                    "Error",
                    "USBIP driver installation was not completed or failed.\n\n"
                    f"Exit code: {proc_exit_code[0]}\n{usbip_status.describe()}"
                )
            c.refresh_usbip()
        except Exception as e:
            c.ui.show_message("Error", f"Failed to start the USBIP installer: {e}")
    else:
        c.ui.show_message(
            "Error",
            f"Could not find {os.path.basename(install_payload)}. "
            "Please verify the integrity of the application files.",
        )

    if discoverer_was_running:
        c.start()
    return bool(locals().get("usbip_installed_ok", False))


def run_usbip_uninstall(c):
    # Both builds invoke the registered Inno uninstaller directly. USBIP keeps
    # driver components loaded until reboot, so do not inspect or clean them up
    # synchronously after a successful uninstall.

    # Stop discoverer before uninstallation
    discoverer_was_running = False
    if c.running():
        discoverer_was_running = True
        c.stop()
        
    # Run emergency cleanup to close all virtual controller handles immediately
    c.module("discoverer").emergency_cleanup()

    uninstaller_exe = "C:\\Program Files\\USBip\\unins000.exe"
    if not os.path.exists(uninstaller_exe):
        c.ui.show_message(
            "Error",
            "Could not find the USBIP uninstaller. Reinstall USBIP, then try "
            "uninstalling it again.",
        )
        if discoverer_was_running:
            c.start()
        return False

    proc_exit_code = [None]
    try:
        proc_exit_code[0] = yield c.process(
            "USBIP Driver Uninstallation", "Running USBIP-win2 Uninstaller...\nPlease authorize the UAC prompt if asked.",
            uninstaller_exe, "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART", os.path.dirname(uninstaller_exe))
        if proc_exit_code[0] is None:
            c.ui.show_message("Error", "USBIP driver uninstallation was cancelled or failed to start (UAC prompt declined).")
            if discoverer_was_running:
                c.start()
            return False

        logger.info(f"USBIP driver uninstaller process exited with code: {proc_exit_code[0]}")
    except Exception as e:
        c.ui.show_message("Error", f"Failed to start the USBIP uninstaller: {e}")
        if discoverer_was_running:
            c.start()
        return False

    if proc_exit_code[0] != 0:
        c.ui.show_message(
            "Error",
            "USBIP uninstallation failed.\n\n"
            f"Exit code: {proc_exit_code[0]}",
        )
        if discoverer_was_running:
            c.start()
        return False

    # Do not call get_usbip_status/update_usbip_button here. The loaded USBIP
    # driver, services and device node are expected to remain until reboot.
    c.pending(True)

    if discoverer_was_running:
        c.start()
    c.ui.show_message(
        "Success",
        "USBIP removal started. A restart is required to complete the uninstall.",
    )
    c.restart_prompt()
    return True


def _run_hidhide_script(c, script_name, wait_text):
    """Run a bundled HidHide install/uninstall PowerShell script elevated (runas),
    blocking until it exits. Returns the process exit code, or None on cancel/error."""
    ps1 = c.module("config").get_driver_path(os.path.join("hidhide", script_name))
    if not os.path.exists(ps1):
        c.ui.show_message("Error", f"Could not find {script_name}. Please verify the application files.")
        return None
    exit_code = [None]
    try:
        exit_code[0] = yield c.process(
            "HidHide", wait_text, "powershell.exe", driver_logs.ps_hidden_args(ps1))
        if exit_code[0] is None:
            c.ui.show_message("Error", "HidHide operation was cancelled (UAC prompt declined).")
            return None
    except Exception as e:
        c.ui.show_message("Error", f"Failed to run HidHide operation: {e}")
    return exit_code[0]


def _prompt_usbip_restart_required(ui, module):
    """Remind the user that USBIP remains pending removal until reboot."""
    if not ui.ask_yes_no(
        "Restart Required",
        "A restart is required to finish removing USBIP.\n\nRestart now?",
    ):
        return
    try:
        module("subprocess").Popen(["shutdown", "/r", "/t", "0"])
    except Exception as exc:
        ui.show_message(
            "Error",
            f"Could not restart automatically: {exc}\nPlease restart manually.",
        )


def prepare_download(c, driver_key, action):
    spec = c.module("driver_install_helper").DRIVER_SPECS.get(driver_key)
    if spec is None:
        c.ui.show_message("Error", f"Unknown driver: {driver_key}")
        return None

    # Present-tense / past-tense words for dialog and result messages.
    gerund = "Installing" if action == "install" else "Uninstalling"
    past = "installed" if action == "install" else "uninstalled"
    title_verb = "Install" if action == "install" else "Uninstall"
    dl_key = driver_key if action == "install" else f"{driver_key}_uninstall"

    # Tracks whether the elevated installer/uninstaller actually started (vs a
    # download failure or a declined UAC prompt). Callers can read it afterwards.
    c.owner._last_driver_action_launched = True

    # Stop discoverer and release virtual controller handles first.
    discoverer_was_running = False
    if c.running():
        discoverer_was_running = True
        c.stop()
    c.module("discoverer").emergency_cleanup()

    return spec, gerund, past, title_verb, dl_key, discoverer_was_running


def finish_download(c, driver_key, verify_fn, action, show_success_msg, spec, past, state, discoverer_was_running):
    action_ok = False
    try:
        action_ok = state["exit_code"] == 0 and bool(verify_fn())
    except Exception as e:
        logger.error(f"Driver verification failed for {driver_key} ({action}): {e}")

    if action_ok:
        if show_success_msg:
            c.ui.show_message("Success", f"{spec.display_name} driver {past} successfully.")
    elif state["error"] is None:
        detail = ""
        if driver_key == "WinUHid" and action == "uninstall":
            detail = "\n\n" + driver_logs.read_winuhid_uninstall_log()
        elif driver_key == "ViGEmBus" and action == "uninstall":
            detail = "\n\n" + driver_logs.read_vigembus_uninstall_log()
        c.ui.show_message(
            "Error",
            f"{spec.display_name} {action} was not completed or failed "
            f"(exit code: {state['exit_code']}).\n"
            "A system restart may be required. Please try again if the issue persists."
            f"{detail}"
        )

    if discoverer_was_running:
        c.start()
    return action_ok


def download_payload(module, spec, dl_key, progress_cb):
    try:
        helper = module("driver_install_helper")
        dest_dir = helper.make_download_dir(dl_key)
        installer = helper.download_spec_files(spec, dest_dir, progress_cb)
        return installer, None
    except Exception as e:
        return None, str(e)


def installer_command(spec, installer_path):
    kind = spec.run[0]
    if kind == "exe":
        lp_file = installer_path
        lp_params = spec.run[2]
    else:
        lp_file = "powershell.exe"
        lp_params = driver_logs.ps_hidden_args(installer_path)
    return lp_file, lp_params


def report_download_error(c, spec, action, error, discoverer_was_running):
    c.owner._last_driver_action_launched = False
    c.ui.show_message(
        "Download Error",
        f"Failed to download the {spec.display_name} {action} script:\n{error}\n\n"
        "Please check your internet connection and try again.")
    if discoverer_was_running:
        c.start()


def download_driver(c, driver_key, verify_fn, action, show_success_msg, operation, process_job, sleep):
    if c.module("utils").is_packaged():
        c.ui.show_message("Unavailable", "WinUHid Driver Mode is not available in the Microsoft Store version.")
        return False
    prepared = prepare_download(c, driver_key, action)
    if prepared is None:
        return False
    spec, gerund, past, title_verb, dl_key, discoverer_was_running = prepared
    state = {"error": None, "exit_code": None}
    def job(progress):
        def update(filename, downloaded, total):
            if total and total > 0:
                progress.update(f"Downloading {spec.display_name} driver... {int(downloaded * 100 / total)}%", downloaded / total)
            else:
                progress.update(f"Downloading {spec.display_name} driver... {downloaded / (1024 * 1024):.1f} MB")
        installer, error = download_payload(c.module, spec, dl_key, update)
        state["error"] = error
        if error:
            return "download_error", error
        progress.update(f"{gerund} {spec.display_name} driver...\nPlease authorize the UAC prompt if asked.")
        sleep(0.05)
        file, params = installer_command(spec, installer)
        return process_job(file, params, directory=os.path.dirname(installer))
    def finish(result):
        code, error = result
        if code == "download_error":
            report_download_error(c, spec, action, error, discoverer_was_running)
        else:
            state["exit_code"] = code
            if code is None:
                c.owner._last_driver_action_launched = False
                c.ui.show_message("Error", f"{spec.display_name} {action} was cancelled or failed to start (UAC prompt declined).")
                if discoverer_was_running:
                    c.start()
        return finish_download(c, driver_key, verify_fn, action, show_success_msg,
                               spec, past, state, discoverer_was_running)
    return operation(f"{title_verb} {spec.display_name} Driver",
                     f"Downloading {spec.display_name} driver...", job, finish)

def hidhide_button(ui, service_state, label, install, uninstall):
    installed = service_state() is True
    if installed:
        if ui.ask_yes_no("Uninstall HidHide", "Uninstall the HidHide driver?\n(Requires administrator privileges.)"):
            return (yield uninstall())
    else:
        if ui.ask_yes_no(
            "Install HidHide",
            f"{label} detected.\n\nHidHide "
            "hides the physical controller's HID so games only see the virtual "
            "controller. Install it now?\n(Requires administrator privileges.)",
        ):
            return (yield install())

def apply_wired_probe(s, config, detected, hidhide_state, refresh, ask_install, install):
    hh_installed = False
    if detected:
        hh_installed = s._hidhide_installed_cached if hidhide_state is None else hidhide_state
    s._hidhide_installed_cached = hh_installed
    refresh()
    if (detected and not hh_installed
            and not getattr(config, 'hidhide_install_prompt_suppressed', False)
            and not getattr(s, '_wired_pro2_prompt_shown', False)):
        s._wired_pro2_prompt_shown = True
        if ask_install():
            yield install(prompt_restart=True)
    if not detected:
        s._wired_pro2_prompt_shown = False
