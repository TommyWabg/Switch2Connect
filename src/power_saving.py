"""Central process-wide power-saving policy."""
import threading
import sys

MODES = ("Off", "Auto", "Full")
mode_changed = threading.Event()

def mode():
    try:
        from config import CONFIG
        value = getattr(CONFIG, "power_saving_mode", "Off")
    except Exception:
        value = "Off"
    return value if value in MODES else "Off"

def is_off(): return mode() == "Off"
def is_auto(): return mode() == "Auto"
def is_full(): return mode() == "Full"
def rumble_allowed(): return not is_full()
def motion_allowed(): return not is_full()
def precision_timing_allowed(): return not is_full()

def apply_process_priority():
    # Keep UI and high-rate wired input threads responsive in every mode.  This
    # is only the Python thread handoff interval; it does not request Windows'
    # 1 ms timer resolution or otherwise keep the CPU awake.
    sys.setswitchinterval(0.001)

def notify_mode_changed():
    apply_process_priority()
    try:
        import timer_resolution
        timer_resolution.set_suspended(is_full())
    except Exception:
        pass
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
