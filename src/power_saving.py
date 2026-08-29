"""Central process-wide power-saving policy."""
import threading
import sys
import os
import ctypes

MODES = ("Off", "Auto", "Full")
mode_changed = threading.Event()
_mode = "Off"
_off_timer_lease = False
_startup_process_priority = None

if os.name == "nt":
    try:
        _kernel32 = ctypes.windll.kernel32
        _startup_process_priority = int(
            _kernel32.GetPriorityClass(_kernel32.GetCurrentProcess()))
    except Exception:
        _startup_process_priority = None

def mode():
    return _mode

def is_off(): return mode() == "Off"
def is_auto(): return mode() == "Auto"
def is_full(): return mode() == "Full"
def rumble_allowed(): return not is_full()
def motion_allowed(): return not is_full()
def precision_timing_allowed(): return not is_full()

def apply_process_priority():
    """Apply the execution policy belonging exclusively to the current mode."""
    if is_off():
        # Switch2Connect 2.2 imported usbip_dualsense_server process-wide, and
        # that module unconditionally selected a 1 ms GIL hand-off interval.
        # Restoring Python's ~5 ms default here cut every two-thread virtual
        # output path to roughly 160-190 Hz, regardless of backend.  Keep the
        # proven 2.2 interval; this controls runnable-thread fairness and is not
        # itself a periodic wake source.
        sys.setswitchinterval(0.001)
        target_priority = 0x00000080  # HIGH_PRIORITY_CLASS
    else:
        # Auto/Full retain the same fair hand-off interval while saving power by
        # suspending timers, rumble, motion, and idle workers instead.
        sys.setswitchinterval(0.001)
        target_priority = _startup_process_priority

    if os.name == "nt" and target_priority:
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.SetPriorityClass(
                kernel32.GetCurrentProcess(), int(target_priority))
        except Exception:
            pass

def _apply_timer_policy():
    global _off_timer_lease
    try:
        import timer_resolution
        if is_off():
            timer_resolution.set_suspended(False)
            if not _off_timer_lease:
                _off_timer_lease = timer_resolution.acquire()
        else:
            if _off_timer_lease:
                timer_resolution.release()
                _off_timer_lease = False
            timer_resolution.set_suspended(is_full())
    except Exception:
        pass

def notify_mode_changed():
    global _mode
    try:
        from config import CONFIG
        value = getattr(CONFIG, "power_saving_mode", "Off")
    except Exception:
        value = "Off"
    _mode = value if value in MODES else "Off"
    apply_process_priority()
    _apply_timer_policy()
    mode_changed.set()

def full_scan_capacity_reached(controllers):
    if not is_full():
        return False
    joycons = 0
    for controller in controllers or ():
        try:
            if controller.is_pro_controller() or controller.is_nso_gamecube_controller():
                return True
            if controller.is_joycon():
                joycons += 1
        except Exception:
            continue
    return joycons >= 2
