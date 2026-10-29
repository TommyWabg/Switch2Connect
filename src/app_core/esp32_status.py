"""Connection header classification from the classic interface."""
import logging
import re
import time

logger = logging.getLogger(__name__)


def header_status(status, esp32_detected, was_ready, wired_detected, module):
    conn_method = "ESP32-S3" if esp32_detected else "System BLE"

    if status and esp32_detected:
        otg_only = getattr(status, 'otg_only', False)
        fw_installed = getattr(status, 'firmware_installed', False)
        if getattr(status, 'bridge_ready', False):
            try:
                _usb_sb = module("usb_serial_bridge")
                scan_active = _usb_sb.BRIDGE_SCAN_ACTIVE
            except Exception:
                scan_active = True
            if scan_active:
                conn_status = "Ready"
                status_color = "#55CC55"
            else:
                conn_status = "Initializing"
                status_color = "#888888"
        elif otg_only and not fw_installed:
            if was_ready:
                # Firmware was running but just stopped responding → brief disconnect transition
                conn_status = "Disconnect"
                status_color = "#888888"
            else:
                # OTG in ROM bootloader (no version reported) — ready for manual flash
                conn_status = "Boot"
                status_color = "#FF8800"
        elif getattr(status, 'firmware_update_required', False):
            conn_status = "Error"
            status_color = "#FF4444"
        elif getattr(status, 'board_present', False):
            conn_status = "Initializing"
            status_color = "#888888"
        else:
            conn_status = "Initializing"
            status_color = "#888888"
    else:
        try:
            is_system_bluetooth_available = module("discoverer").is_system_bluetooth_available
            bt_ok = is_system_bluetooth_available()
        except Exception:
            bt_ok = True
        if bt_ok:
            conn_status = "Ready"
            status_color = "#55CC55"
        else:
            # No Bluetooth radio (or it was switched off): the app keeps running in
            # wired-only mode, so report the USB route rather than a bare "Disconnect"
            # that made it look like nothing could connect at all.
            conn_method = "USB"
            if wired_detected:
                conn_status = "USB Connected"
                status_color = "#55CC55"
            else:
                conn_status = "Pending USB Connection"
                status_color = "#888888"

    return conn_method, conn_status, status_color


def keep_previous_probe(was_current, status):
    return bool(was_current
                and status
                and getattr(status, 'board_present', False)
                and not getattr(status, 'firmware_installed', False)
                and not getattr(status, 'otg_only', False))


def should_restart_discovery(current_seen, was_current, detected, was_detected,
                             discoverer_running, controllers):
    has_live_controllers = any(vc is not None for vc in controllers or [])
    return (((current_seen and not was_current) or (detected and not was_detected))
            and discoverer_running and not has_live_controllers)


def claim_auto_update(status, running, quitting, attempted):
    # Never auto-flash via OTG: esptool requires manual BOOT button hold on native USB
    if getattr(status, "otg_only", False):
        return False
    if (
        not status
        or not getattr(status, "board_present", False)
        or not getattr(status, "firmware_update_required", False)
        or not getattr(status, "status_text", "")
        or not getattr(status, "firmware_version", "")
        or running
        or quitting
    ):
        return False

    serial_port = getattr(status, "serial_port", None)
    if not serial_port:
        logger.warning("ESP32-S3 firmware update is required, but CH343 flashing port was not detected.")
        return False

    attempt_key = (
        serial_port.port,
        getattr(status, "firmware_version", ""),
        getattr(status, "firmware_mode", ""),
        getattr(status, "expected_version", ""),
    )
    if attempt_key in attempted:
        return False
    attempted.add(attempt_key)
    return True


def flash_progress(payload, current):
    current = float(current)
    percent = None
    message = None
    if isinstance(payload, dict):
        if "percent" in payload:
            percent = float(payload["percent"])
        elif "write_percent" in payload:
            percent = 25.0 + (max(0.0, min(100.0, float(payload["write_percent"]))) * 0.70)
        message = payload.get("message")
    else:
        text = str(payload)
        match = re.search(r"\((\d{1,3})\s*%\)", text)
        if match:
            percent = 25.0 + (max(0.0, min(100.0, float(match.group(1)))) * 0.70)

    if percent is None:
        # Plain esptool log lines are diagnostic text, not progress.  In
        # particular, retries during bootloader synchronization must not
        # appear as a misleading 16% partial flash.
        percent = current
    percent = max(current, min(100.0, percent))
    return percent, message


def flash_failure_message(error, module):
    error_str = str(error)
    try:
        flash_log = module("usb_serial_bridge").flash_log
        get_flash_log_path = module("usb_serial_bridge").get_flash_log_path
        flash_log(f"flash failed: {error_str}")
        _log_path = get_flash_log_path()
    except Exception:
        _log_path = ""
    if ("Could not communicate with ESP32-S3" in error_str
            or ("Could not put ESP32-S3" in error_str
                and "into flashing mode" in error_str)):
        return (
            "Could not enter flashing mode.\n\n"
            "To enter Boot mode manually:\n"
            "  1. Hold the BOOT button\n"
            "  2. Tap RESET once, then release BOOT\n"
            "  3. Click Repair to retry\n\n"
            f"Details logged to:\n{_log_path}"
        )
    return f"ESP32-S3 N16R8 firmware operation failed:\n{error}"


def refresh_esp32s3_status_async(s):
    if getattr(s, '_esp32s3_refresh_running', False) or getattr(s, 'is_quitting', False):
        return
    # The completed-flash dialog owns unplug detection until it and the
    # ESP32-S3 button can be removed in one UI transaction.
    if getattr(s, '_esp32s3_waiting_for_removal', False):
        return
    # Never probe the COM port while a firmware flash / replug is in progress —
    # doing so collides with esptool and re-occupies the port during replug.
    if getattr(s, '_esp32s3_firmware_busy', False):
        return
    s._esp32s3_refresh_running = True

    def worker():
        status = None
        detected = False
        try:
            status = s.module("usb_serial_bridge").detect_bridge()
            detected = bool(status and status.board_present)
        except Exception as e:
            logger.debug(f"ESP32-S3 async status refresh failed: {e}")

        return status, detected

    def apply_status(result):
        status, detected = result
        s._esp32s3_refresh_running = False
        if getattr(s, 'is_quitting', False):
            return
        was_current = s._esp32s3_current_seen
        was_detected = getattr(s, '_esp32s3_was_detected', False)

        # If the bridge was recently ready and the new probe returns
        # "no firmware" (board still physically present), this is almost
        # certainly a transient PermissionError because the discoverer's
        # shared_client is holding the COM port open. Discarding the
        # result keeps Boot-mode detection confined to the firmware-flash
        # UI and prevents it from disrupting any connection logic.
        # Exception: OTG-only boards have no CDC serial port for the
        # discoverer to hold open, so a firmware_installed=False probe
        # on OTG is a genuine boot+reset event and must not be discarded.
        if keep_previous_probe(was_current, status):
            return  # transient probe failure — keep previous state

        s.esp32s3_bridge_status = status
        s.esp32s3_detected = detected
        s._esp32s3_was_detected = detected
        s._esp32s3_current_seen = bool(status and getattr(status, "bridge_ready", False))
        s.refresh()
        s.auto_update(status)
        discoverer_running = s.discovery_running()
        # Never tear down a live bridge session: if a controller is already
        # connected, a restart would disconnect it. A transient status-probe
        # timeout can briefly drop bridge_ready and make it look like the
        # bridge "just became ready" again on the next poll — restarting then
        # would kick the user's controller mid-use.
        if should_restart_discovery(
                s._esp32s3_current_seen, was_current, detected, was_detected,
                discoverer_running, getattr(s, 'current_controllers', []) or []):
            logger.info("ESP32-S3 state changed. Restarting discoverer...")
            # Pause this 5 s status timer while the discoverer (re)opens the
            # bridge COM port. Otherwise a transient detect_bridge probe from a
            # later tick races the discoverer's persistent open on the freshly
            # hot-plugged port and one side gets "port occupied" — which is why
            # plugging the ESP32-S3 in AFTER launch failed but before launch
            # worked. Resume probing once the open window has passed.
            s._esp32s3_firmware_busy = True

            def _hotplug_restart():
                try:
                    s.start_discovery()
                finally:
                    s.schedule(4000, lambda: setattr(s, '_esp32s3_firmware_busy', False))

            s.schedule(100, _hotplug_restart)

    s.worker(worker, apply_status)
    return True


def wait_for_current_esp32s3_then(s, callback, attempts=24):
    if getattr(s, "is_quitting", False):
        return

    remaining = attempts

    def worker():
        status = None
        try:
            status = s.module("usb_serial_bridge").detect_bridge()
        except Exception as e:
            logger.debug(f"Waiting for ESP32-S3 firmware HID failed: {e}")

        return status

    def apply_status(result):
        status = result
        if getattr(s, "is_quitting", False):
            return
        s.esp32s3_bridge_status = status
        board_present = bool(status and getattr(status, "board_present", False))
        s.esp32s3_detected = board_present
        s._esp32s3_was_detected = board_present
        s._esp32s3_current_seen = bool(status and getattr(status, "bridge_ready", False))
        s.refresh()
        if s._esp32s3_current_seen or remaining <= 0:
            callback()
        elif not board_present:
            # ESP32 fully disconnected — clear state and fall back to System BLE immediately
            s.esp32s3_bridge_status = None
            s.esp32s3_detected = False
            s._esp32s3_was_detected = False
            s.refresh()
            callback()
        else:
            s.schedule(500, lambda: s.wait_current(callback, remaining - 1))

    s.worker(worker, apply_status, lambda: None)


def wait_for_esp32s3_removal_then(s, callback, should_continue=None, consecutive_missing=0, saw_absent=False):
    """Close the completed-flash dialog on unplug or replug initialization.

    Two consecutive absent samples avoid treating a transient detection failure
    during post-flash USB settling as a physical removal.  If the board returns
    after an observed absence, its first detected state is the replug
    initialization stage and closes the dialog immediately.
    """
    if getattr(s, "is_quitting", False):
        return
    if should_continue is not None and not should_continue():
        return

    previous_missing = consecutive_missing

    def worker():
        status = None
        detection_failed = False
        try:
            status = s.module("usb_serial_bridge").detect_bridge()
        except Exception as e:
            detection_failed = True
            logger.debug(f"Waiting for ESP32-S3 removal failed: {e}")

        return status, detection_failed

    def apply_status(result):
        status, detection_failed = result
        if getattr(s, "is_quitting", False):
            return
        if should_continue is not None and not should_continue():
            return
        board_present = bool(status and getattr(status, "board_present", False))
        missing = previous_missing + 1 if not board_present else 0
        replug_initializing = bool(
            not detection_failed and board_present and saw_absent)
        confirmed_removed = bool(
            not detection_failed and not board_present and missing >= 2)
        if replug_initializing or confirmed_removed:
            s._esp32s3_waiting_for_removal = False
            s.esp32s3_bridge_status = status if replug_initializing else None
            s.esp32s3_detected = replug_initializing
            s._esp32s3_was_detected = replug_initializing
            # bridge_ready only means the firmware answered its status probe.
            # Runtime readiness is established later by discoverer after it
            # opens CDC and sends "scan on"; keep this edge unconsumed here.
            s._esp32s3_current_seen = False
            s.refresh()
            callback("reinserted" if replug_initializing else "removed", status)
            return
        s.schedule(
            500,
            lambda: s.wait_removal(
                callback, should_continue, missing,
                saw_absent or (not detection_failed and not board_present)))

    s.worker(worker, apply_status, lambda: None)


def finish_manager_flash(s, action, done, discoverer_was_running, on_done, complete_progress, wait_replug):
    discovery_resumed = {"value": False}

    def resume_discovery(reinserted_status=None):
        if discovery_resumed["value"]:
            return
        discovery_resumed["value"] = True
        s._esp32s3_firmware_busy = False
        if discoverer_was_running:
            startup_context = None
            if (reinserted_status
                    and getattr(reinserted_status, "bridge_ready", False)
                    and getattr(reinserted_status, "firmware_current", False)
                    and getattr(reinserted_status, "serial_port", None)):
                startup_context = {
                    "status": reinserted_status,
                    "observed_mono": time.monotonic(),
                }
            s.start_discovery(startup_context)

    if done["ok"]:
        try:
            s.module("usb_serial_bridge").close_all_clients()
        except Exception:
            pass
        complete_progress()
        if action == "delete":
            on_done(True, "ESP32-S3 N16R8 firmware uninstalled successfully.")
            (yield s.refresh_status())
            resume_discovery()
        else:
            on_done(
                True,
                "ESP32-S3 N16R8 firmware installed successfully.\n\n"
                "Please replug the ESP32-S3 USB cable (unplug then reinsert) "
                "to complete initialization and avoid port conflicts.",
            )
            s._esp32s3_waiting_for_removal = True
            wait_replug(resume_discovery)
    else:
        s._esp32s3_firmware_busy = False
        on_done(False, flash_failure_message(done["error"], s.module))
        (yield s.refresh_status())
        if discoverer_was_running:
            s.start_discovery()

def prepare_flash(s):
    s._esp32s3_firmware_busy = True
    discoverer_was_running = s.discovery_running()
    if discoverer_was_running:
        s.stop_discovery()
    s.module("discoverer").emergency_cleanup()
    try:
        s.module("usb_serial_bridge").close_all_clients()
    except Exception:
        pass
    return discoverer_was_running


def resolve_manager_flash_port(config, status, bridge, requested_port=None, manual_port=None, selected_hint=None):
    if manual_port is None:
        manual_port = getattr(config, "esp32_serial_port_mode", "auto") == "manual"
    if requested_port is None:
        requested_port = ""
        if manual_port:
            requested_port = str(getattr(config, "esp32_serial_port", "") or "")
    if selected_hint is None:
        selected_hint = getattr(status, "serial_port", None) if status else None
    selected_port = bridge.resolve_flash_port(
        requested_port, manual=manual_port, preferred_info=selected_hint, timeout=3.0)
    return bridge.stabilize_flash_port(selected_port)


def flash_worker(flash_firmware, port, action, progress, done, progress_queue):
    try:
        flash_firmware(port, mode=action, progress=progress)
        done["ok"] = True
    except Exception as e:
        done["error"] = e
        logger.exception("ESP32-S3 firmware task failed")
    finally:
        progress_queue.put(("done", None))


def drain_flash_progress(progress_queue, apply_progress):
    import queue
    should_finish = False
    while True:
        try:
            kind, payload = progress_queue.get_nowait()
        except queue.Empty:
            break
        if kind == "progress":
            apply_progress(payload)
        elif kind == "done":
            should_finish = True
    return should_finish


def select_manager_port(config, selected_port):
    config.esp32_serial_port_mode = "manual" if selected_port else "auto"
    config.esp32_serial_port = selected_port.upper()
    config.save_config()

def maybe_auto_update(s, status, on_complete=None):
    if not claim_auto_update(
            status, getattr(s, "_esp32s3_auto_firmware_running", False),
            getattr(s, "is_quitting", False), s._esp32s3_auto_firmware_attempted):
        return False
    s._esp32s3_auto_firmware_running = True

    discoverer_was_running = s.discovery_running()
    if discoverer_was_running:
        s.stop_discovery()

    def completed(ok):
        s._esp32s3_auto_firmware_running = False

        def resume():
            # Reopen the port only after the device re-enumerates post-replug.
            s._esp32s3_firmware_busy = False
            s.start_discovery()

        if on_complete:
            s._esp32s3_firmware_busy = False
            s.refresh_async()
            on_complete(ok)
        elif ok or discoverer_was_running:
            if ok:
                # Keep the COM port free while the user replugs; resume once current.
                s.schedule(1000, lambda: s.wait_current(resume))
            else:
                s._esp32s3_firmware_busy = False
                s.refresh_async()
                s.schedule(0, s.start_discovery)
        else:
            s._esp32s3_firmware_busy = False
            s.refresh_async()

    logger.info(
        "ESP32-S3 firmware update required: current version=%s mode=%s expected=%s",
        getattr(status, "firmware_version", ""),
        getattr(status, "firmware_mode", ""),
        getattr(status, "expected_version", ""),
    )
    s.run_firmware("install", auto=True, status=status, on_complete=completed)
    return True

def finish_firmware_task(s, ui, action, auto, done, discoverer_was_running, on_complete, ESP32S3_LABEL):
    def resume_discovery():
        # Clear the busy flag only once we're ready to reopen the port, so the
        # 5 s status timer stays quiet through the whole flash + replug window.
        s._esp32s3_firmware_busy = False
        if discoverer_was_running:
            s.start_discovery()

    if done["ok"]:
        # Release the COM port so replug is clean and Windows doesn't report a stale handle.
        try:
            s.module("usb_serial_bridge").close_all_clients()
        except Exception:
            pass

        if action == "delete":
            # Uninstall succeeded — firmware is gone, no replug needed.
            ui.show_message(
                ESP32S3_LABEL,
                "ESP32-S3 N16R8 firmware uninstalled successfully.",
            )
        elif not auto:
            ui.show_message(
                ESP32S3_LABEL,
                "ESP32-S3 N16R8 firmware installed successfully.\n\n"
                "Please replug the ESP32-S3 USB cable (unplug then reinsert) "
                "to complete initialization and avoid port conflicts.",
            )
        else:
            # Auto-update path: show a brief replug reminder in a non-blocking way.
            ui.show_message(
                ESP32S3_LABEL,
                "Firmware auto-updated. Please replug the ESP32-S3 USB cable.",
            )

        if on_complete:
            # Auto-update path manages its own busy-clear + delayed restart.
            on_complete(done["ok"])
        elif action == "delete":
            # Firmware removed: nothing to wait for, resume discovery now.
            (yield s.refresh_status())
            resume_discovery()
        else:
            # Manual install/repair: the user must replug. Keep the COM port
            # free and wait until the device re-enumerates with current
            # firmware before reopening it, then resume discovery. This is what
            # stops the "port occupied" state that previously needed an app restart.
            s.schedule(1500, lambda: s.wait_current(resume_discovery))
    else:
        s._esp32s3_firmware_busy = False
        ui.show_message(
            ESP32S3_LABEL, flash_failure_message(done["error"], s.module), kind="error")
        (yield s.refresh_status())
        if on_complete:
            on_complete(done["ok"])
        elif discoverer_was_running:
            s.start_discovery()


def startup_detect(s, complete):
    def worker():
        status = None
        detected = False
        try:
            status = s.module("usb_serial_bridge").detect_bridge()
            detected = bool(status and status.board_present)
        except Exception as e:
            logger.debug(f"Startup ESP32-S3 detection failed: {e}")
        return status, detected
    def apply_status(result):
        status, detected = result
        if getattr(s, 'is_quitting', False):
            s.startup_cancelled()
            return
        s.esp32s3_bridge_status = status
        s.esp32s3_detected = detected
        s._esp32s3_was_detected = detected
        s._esp32s3_current_seen = bool(status and getattr(status, "bridge_ready", False))
        s.refresh()
        def after_auto_update(ok):
            if ok:
                s.schedule(1000, lambda: s.wait_current(lambda: complete()))
            else:
                s.schedule(0, complete)
        if s.auto_update(status, on_complete=after_auto_update):
            return
        bridge_context = None
        if (status and getattr(status, "bridge_ready", False)
                and getattr(status, "firmware_current", False)
                and getattr(status, "serial_port", None)):
            bridge_context = {"status": status, "observed_mono": time.monotonic()}
        complete(bridge_context)
    s.worker(worker, apply_status, lambda: None)

def refresh_status(s):
    try:
        try:
            status = s.module("usb_serial_bridge").detect_bridge()
            s.esp32s3_bridge_status = status
        except Exception:
            s.esp32s3_bridge_status = None
        s.esp32s3_detected = bool(s.esp32s3_bridge_status and s.esp32s3_bridge_status.board_present)
    except Exception as e:
        logger.debug(f"ESP32-S3 status refresh failed: {e}")
        s.esp32s3_bridge_status = None
        s.esp32s3_detected = False
    s.refresh()
    return s.esp32s3_bridge_status

def manager_preparation_failed(s, was_running, error, on_done):
    s._esp32s3_firmware_busy = False
    if was_running:
        s.start_discovery()
    on_done(False, str(error))


def port_choices(status, bridge):
    try:
        bridge = bridge("usb_serial_bridge") if callable(bridge) else bridge
        ports = bridge.list_bridge_port_candidates()
    except Exception as e:
        logger.debug(f"ESP32-S3 port list refresh failed: {e}")
        ports = []
    values = ["Auto"]
    choices = []
    for item in ports:
        verified = bool(status and status.serial_port
                        and status.serial_port.port.upper() == item.port.upper()
                        and getattr(status, "firmware_installed", False))
        label = bridge.format_port_label(item, verified=verified)
        values.append(label)
        choices.append((item, label, verified))
    return values, choices

def manager_probe(bridge, selected_port=None):
    detected_status = None
    succeeded = False
    try:
        detected_status = bridge.detect_bridge(selected_port, allow_unverified=True) if selected_port else bridge.detect_bridge(allow_unverified=True)
        succeeded = True
    except Exception as e:
        logger.debug(f"ESP32-S3 dialog status refresh failed: {e}")
    return detected_status, succeeded


def apply_manager_probe(s, status, render):
    s.esp32s3_bridge_status = status
    s.esp32s3_detected = bool(status and getattr(status, "board_present", False))
    render(status)


def boot_mode_guidance(status, action):
    current_otg = bool(status and getattr(status, "otg_only", False))
    current_boot = current_otg and not bool(status and getattr(status, "firmware_installed", False))
    if current_otg and not current_boot and action in ("install", "repair"):
        return (
            "ESP32 is not in Boot mode — firmware cannot be installed.\n\n"
            "To enter Boot mode:\n"
            "  1. Hold the BOOT button\n"
            "  2. Tap RESET once, then release BOOT\n"
            "  3. Wait for Status to show \"Boot\", then click Install"
        )
    return None
