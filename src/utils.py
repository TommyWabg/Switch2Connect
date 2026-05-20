import win32api
import winreg
import sys
import os
import math
from config import CONFIG

def to_hex(buffer):
    return " ".join("{:02x}".format(x) for x in buffer)

def decodeu(data: bytes):
    return int.from_bytes(data, byteorder='little', signed=False)

def decodes(data: bytes):
    return int.from_bytes(data, byteorder='little', signed=True)

_CACHED_LOCAL_MAC_VALUE = None

def convert_mac_string_to_value(mac: str):
    # Handle colons, dashes, and spaces robustly and convert to integer
    cleaned = mac.replace(":", "").replace("-", "").strip()
    return int(cleaned, 16)

def get_local_mac_value():
    global _CACHED_LOCAL_MAC_VALUE
    if _CACHED_LOCAL_MAC_VALUE is not None:
        return _CACHED_LOCAL_MAC_VALUE
    
    import bluetooth
    addr_info = bluetooth.read_local_bdaddr()
    if addr_info and len(addr_info) > 0:
        _CACHED_LOCAL_MAC_VALUE = convert_mac_string_to_value(addr_info[0])
        return _CACHED_LOCAL_MAC_VALUE
    raise RuntimeError("No local Bluetooth adapter found or Bluetooth is disabled.")

def get_stick_xy(data: bytes):
    """Convert 3 bytes containing stick x y values into these values"""
    value = decodeu(data)
    x = value & 0xFFF
    y = value >> 12

    return x, y

def signed_looping_difference_16bit(a, b):
    diff = (b - a) % 65536
    return diff - 65536 if diff > 32768 else diff

def apply_calibration_to_axis(raw_value, center, max_abs, min_abs):
    signed_value = raw_value - center
    if signed_value > CONFIG.deadzone:
        return min(signed_value / max_abs, 1)
    if signed_value < -CONFIG.deadzone:
        return -min(-signed_value / min_abs, 1)
    return 0

def press_or_release_mouse_button(state: bool, prev_state: bool, button: int, mouse_x: int, mouse_y):
    if (state and not prev_state):
        win32api.mouse_event(button, mouse_x, mouse_y, 0, 0)
    if (not state and prev_state):
        win32api.mouse_event(button << 1, mouse_x, mouse_y, 0, 0)

def reverse_bits(n: int, no_of_bits: int):
    result = 0
    for i in range(no_of_bits):
        result <<= 1
        result |= n & 1
        n >>= 1
    return result

def vector_normalize(v):
    mag = math.sqrt(sum(x*x for x in v))
    if mag == 0: return (0, 0, 0)
    return tuple(x/mag for x in v)

def vector_cross(a, b):
    return (
        a[1]*b[2] - a[2]*b[1],
        a[2]*b[0] - a[0]*b[2],
        a[0]*b[1] - a[1]*b[0]
    )

def vector_dot(a, b):
    return sum(x*y for x, y in zip(a, b))

def quaternion_multiply(q, p):
    w1, x1, y1, z1 = q
    w2, x2, y2, z2 = p
    return (
        w1*w2 - x1*x2 - y1*y2 - z1*z2,
        w1*x2 + x1*w2 + y1*z2 - z1*y2,
        w1*y2 - x1*z2 + y1*w2 + z1*x2,
        w1*z2 + x1*y2 - y1*x2 + z1*w2
    )

def quaternion_normalize(q):
    mag = math.sqrt(sum(x*x for x in q))
    if mag == 0: return (1, 0, 0, 0)
    return tuple(x/mag for x in q)

def quaternion_rotate_vector(q, v):
    qv = (0, v[0], v[1], v[2])
    q_inv = (q[0], -q[1], -q[2], -q[3])
    res = quaternion_multiply(quaternion_multiply(q, qv), q_inv)
    return (res[1], res[2], res[3])

def quaternion_from_vectors(v_from, v_to):
    v_from = vector_normalize(v_from)
    v_to = vector_normalize(v_to)
    dot = vector_dot(v_from, v_to)
    if dot < -0.999999:
        axis = vector_cross((1, 0, 0), v_from)
        if math.sqrt(sum(x*x for x in axis)) < 0.000001:
            axis = vector_cross((0, 1, 0), v_from)
        return quaternion_normalize((0, axis[0], axis[1], axis[2]))
    elif dot > 0.999999:
        return (1, 0, 0, 0)
    
    s = math.sqrt((1 + dot) * 2)
    inv_s = 1 / s
    cross = vector_cross(v_from, v_to)
    return quaternion_normalize((s * 0.5, cross[0] * inv_s, cross[1] * inv_s, cross[2] * inv_s))

def set_startup(enabled: bool):
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    app_name = "Switch2Controllers"
    
    if hasattr(sys, 'frozen'):
        # Executable path
        app_path = sys.executable
    else:
        # Python script path
        app_path = f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}"'

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, app_path)
        else:
            try:
                winreg.DeleteValue(key, app_name)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except Exception as e:
        print(f"Error setting startup: {e}")
        return False

show_notification_callback = None

def show_notification(title, message):
    global show_notification_callback
    if show_notification_callback is not None:
        show_notification_callback(title, message)
    else:
        # Fallback to console print
        print(f"[{title}] {message}", flush=True)

force_ui_update_callback = None

def force_ui_update():
    global force_ui_update_callback
    if force_ui_update_callback is not None:
        force_ui_update_callback()