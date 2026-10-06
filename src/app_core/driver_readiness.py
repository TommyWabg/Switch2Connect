"""Classic driver readiness checks shared by both interfaces."""
import logging
import os

logger = logging.getLogger(__name__)


def run_check(steps):
    """Classic actions finish inline; the modern executor awaits yielded futures."""
    try:
        value = next(steps)
        while True:
            value = steps.send(value)
    except StopIteration as done:
        return done.value

def check_vigembus_installation(config, ui, module, action, refresh, packaged_options,
                               close_manager, status_query, select_driver,
                               restart_pending=False, save=True):
    status = status_query("ViGEmBus")
    if status.unknown:
        # The query failed (e.g. pnputil without /properties); asking the user to
        # repair a state we cannot read only nags them. The runtime bus connection
        # is the authoritative answer, so try that before prompting for anything.
        logger.warning("ViGEmBus status undetermined: %s", status.describe())
        if module("app_core.helpers").verify_vigembus_runtime(attempts=2):
            config.vigembus_installed = True
            if save:
                config.save_config()
            return True

    if not status.installed:
        config.vigembus_installed = False
        if save:
            config.save_config()

        partial = status.state == "partial"
        answer = ui.ask_yes_no(
            "Repair ViGEmBus Driver" if partial else "Install ViGEmBus Driver",
            (("ViGEmBus is partially installed and cannot start.\n\n"
             f"{status.describe()}\n\nDo you want to clean it up and reinstall it now?\n")
             if partial else
             ("ViGEmBus driver is not installed.\n\nDo you want to "
              + ("install" if module("utils").is_packaged() else "download and install")
              + " it now?\n")) +
            "(Requires administrator privileges.)"
        )

        if answer:
            if partial and not (yield action("run_vigembus_uninstall")):
                return False
            installed = (yield action("install_vigembus_driver", show_success_msg=True))
            if installed:
                config.vigembus_installed = True
                if save:
                    config.save_config()
                return True
        return False

    if module("app_core.helpers").verify_vigembus_runtime():
        config.vigembus_installed = True
        if save:
            config.save_config()
        return True

    ui.show_message(
        "ViGEmBus Connection Error",
        "ViGEmBus files and device are present, but the runtime bus connection failed.\n\n"
        f"{status.describe()}\n\nUse Repair ViGEmBus Driver to cleanly reinstall it."
    )
    if module("utils").is_packaged():
        # WinUHid is not shipped by the Store build.  Do not silently switch
        # to a driver that the package deliberately cannot install.
        config.vigembus_installed = False
        if save:
            config.save_config()
        refresh()
        return False
    config.driver_type = "WinUHid"
    config.simulation_mode = config.winuhid_sim_mode
    config.vigembus_installed = False
    if save:
        config.save_config()
    select_driver("WinUHid")
    refresh()
    return False

def check_driver_installation(config, ui, module, action, refresh, packaged_options,
                             close_manager, status_query, select_driver,
                             restart_pending=False, save=True):
    # MSIX may consume a separately installed WinUHid, but it never installs
    # or repairs one.  Refresh the live capability silently before choosing
    # a driver so a stale config/profile cannot re-enable unavailable paths.
    if module("utils").is_packaged():
        previous = bool(getattr(config, "driver_installed", False))
        available = bool(module("config").refresh_packaged_winuhid_capability())
        preferred = getattr(
            config, "preferred_driver_type",
            getattr(config, "driver_type", "WinUHid"))
        # PnP enumeration can transiently miss WinUHid during startup. A
        # successful native report submission is definitive and prevents a
        # false early downgrade from becoming the session's driver choice.
        if (not available and preferred == "WinUHid"
                and module("app_core.helpers").verify_winuhid_runtime(
                    attempts=4, delay_seconds=0.25)):
            available = bool(
                module("config").confirm_packaged_winuhid_capability(True))
        config.driver_installed = available
        if available and preferred == "WinUHid":
            was_fallback = bool(getattr(
                config, "driver_fallback_active", False))
            config.driver_type = "WinUHid"
            config.simulation_mode = getattr(
                config, "winuhid_sim_mode", "PS5")
            config.driver_fallback_active = False
            if was_fallback:
                logger.info(
                    "WinUHid startup revalidation succeeded; restored configured driver")
        elif (not available
              and getattr(config, "driver_type", "WinUHid") == "WinUHid"):
            # Effective fallback for this process only. save_config()
            # serializes preferred_driver_type while this flag is active.
            config.driver_type = "ViGEmBus"
            config.simulation_mode = getattr(config, "vigembus_sim_mode", "Xbox360")
            config.driver_fallback_active = True
            logger.warning(
                "WinUHid startup revalidation failed; using temporary ViGEmBus fallback")
        if save and previous != available:
            config.save_config()
        packaged_options(available)
        if available and config.driver_type == "WinUHid":
            # The refreshed stack check or native smoke test already proved
            # this packaged-session path. Do not immediately reinterpret a
            # stale PnP cache below as a missing installation.
            return
    # If driver type is USBIP, check USBIP driver instead
    if getattr(config, "driver_type", "") == "USBIP":
        if restart_pending:
            return
        usbip_status = status_query("USBIP")
        if usbip_status.unknown:
            # Undetermined is not "missing": fall back to the executable the
            # rest of the app invokes rather than nagging on every launch.
            logger.warning("USBIP status undetermined: %s", usbip_status.describe())
        usbip_ready = usbip_status.installed or (
            usbip_status.unknown
            and os.path.exists("C:\\Program Files\\USBip\\usbip.exe"))
        if not usbip_ready:
            partial = usbip_status.state == "partial"
            answer = ui.ask_yes_no(
                "Repair USBIP Driver" if partial else "Install USBIP Driver",
                (("USBIP is partially installed and cannot be used reliably.\n\n"
                  f"{usbip_status.describe()}\n\nDo you want to remove it now? "
                  "Restart Windows before installing USBIP again.\n")
                 if partial else
                 "Switch emulation is selected, but the USBIP driver is not installed.\n\n"
                 "Do you want to install it now?\n") +
                "(Requires administrator privileges and will temporarily reset USB connections.)"
            )
            if answer:
                if partial:
                    (yield action("run_usbip_uninstall"))
                    return
                (yield action("run_usbip_install", show_success_msg=False))
        return

    driver_type = getattr(config, "driver_type", "WinUHid")
    if (driver_type != "WinUHid"
            or (module("utils").is_packaged() and not module("config").packaged_winuhid_available())):
        close_manager()
    if driver_type == "ViGEmBus":
        (yield action("check_vigembus_installation", save=save))
        return

    winuhid_status = status_query("WinUHid")
    if winuhid_status.unknown:
        # Undetermined is not broken: fall back to the runtime smoke test rather
        # than prompting for a repair that this machine's pnputil cannot perform.
        logger.warning("WinUHid status undetermined: %s", winuhid_status.describe())
        if module("app_core.helpers").verify_winuhid_runtime(attempts=2):
            config.driver_installed = True
            if save:
                config.save_config()
            return

    if winuhid_status.installed:
        config.driver_installed = True
        if save:
            config.save_config()
        return

    if getattr(config, 'driver_installed', False):
        config.driver_installed = False
        if save:
            config.save_config()
        refresh()

    partial = winuhid_status.state == "partial"
    if partial:
        prompt = (
            "WinUHid is only partially installed and cannot be used reliably.\n\n"
            f"{winuhid_status.describe()}\n\n"
            "Do you want to clean up and reinstall it now?\n(Requires administrator privileges.)"
        )
    else:
        prompt = "WinUHid driver is not installed.\n\nDo you want to install it now?\n(Requires administrator privileges.)"
    answer = ui.ask_yes_no(
        "Repair WinUHid Driver" if partial else "Install WinUHid Driver", prompt)

    if answer:
        if winuhid_status.state == "partial" and not (yield action("run_driver_uninstall")):
            return
        (yield action("run_driver_install", show_success_msg=False))

