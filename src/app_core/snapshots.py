"""Pure hardware-to-display copies of the classic player blocks.

No snapshot contains a controller, device, loop, mutable collection, or callable.
PID constants are copied here to avoid importing the hardware stack on import.
"""
from . import driver_actions
from .models import ControllerInfo, SlotInfo, WiredState

PRO_CONTROLLER2_PID = 0x2069
NSO_GAMECUBE_CONTROLLER_PID = 0x2073
PRO_CONTROLLER_PID = 0x2009
LED_PATTERNS = {1: 0x01, 2: 0x03, 3: 0x07, 4: 0x0F,
                5: 0x09, 6: 0x05, 7: 0x0D, 8: 0x06}


def battery_level(voltage):
    # No accepted input report yet is unknown, not low; clear any stale icon.
    if voltage is None:
        return None
    return "high" if voltage > 3.25 else "medium" if voltage > 3.125 else "low"


def controller_snapshot(controller, *, merged=False):
    pid = getattr(controller.controller_info, "product_id", None)
    if controller.is_joycon_left():
        kind, name = "joycon_left", "Joy-Con L"
    elif controller.is_joycon_right():
        kind, name = "joycon_right", "Joy-Con R"
    elif pid == NSO_GAMECUBE_CONTROLLER_PID:
        kind, name = "gamecube", "NSO GameCube Controller"
    elif pid == PRO_CONTROLLER_PID:
        kind, name = "pro", "Pro Controller"
    else:
        kind, name = "pro2", "Pro Controller 2"
    wired = controller.__class__.__name__ == "USBHidController"
    return ControllerInfo(
        kind=kind, display_name=name, address=str(controller.device.address),
        transport="wired" if wired else "esp32" if controller.__class__.__name__ == "ESP32S3Controller" else "ble",
        product_id=pid, battery_level=battery_level(controller.battery_voltage),
        is_mag_calibrating=bool(getattr(controller, "is_mag_calibrating", False)),
        mag_calibration_available=(controller.is_joycon_left() or controller.is_joycon_right()) if merged else pid != NSO_GAMECUBE_CONTROLLER_PID,
    )


def opposite(a, b):
    return bool(a and b and ((a.is_single_joycon_left() and b.is_single_joycon_right()) or
                            (a.is_single_joycon_right() and b.is_single_joycon_left())))


def build_slots(vcs, config, pending_merge_vc_index=None, *, registry=None):
    registry = vcs if registry is None else registry
    result = []
    for index in range(max(4, len(vcs))):
        vc = vcs[index] if index < len(vcs) else None
        if vc is None or not vc.controllers:
            result.append(SlotInfo(index=index, player_number=index + 1))
            continue
        merged = not vc.is_single()
        joycon = vc.is_single_joycon_left() or vc.is_single_joycon_right()
        controllers = tuple(controller_snapshot(c, merged=merged) for c in vc.controllers)
        selected = pending_merge_vc_index == vc.player_number - 1
        active = getattr(vc, "active_gyro_side", "Right")
        left = bool(getattr(vc, "djg_left_active", True))
        right = bool(getattr(vc, "djg_right_active", True))
        # Classic player1..player4 bitmaps use progressively lit LEDs; retain
        # the physical patterns for discoverer extra slots (controller.LED_PATTERN).
        result.append(SlotInfo(
            index=index, player_number=vc.player_number,
            leds=tuple(bool(LED_PATTERNS.get(vc.player_number, 0) & (1 << i)) for i in range(4)), controllers=controllers,
            is_merged_pair=merged, is_empty=False, slot_token=(index, id(vc)),
            kind="joycon_pair" if merged else controllers[0].kind,
            hold_mode=getattr(vc, "hold_mode", None) if joycon else None,
            active_gyro_side=active if merged else None,
            djg_left_active=left, djg_right_active=right,
            split_available=merged, merge_available=bool(joycon and (selected or any(opposite(vc, other) for other in registry if other is not vc))),
            pending_merge_index=pending_merge_vc_index,
            merge_target=bool(joycon and pending_merge_vc_index is not None and
                0 <= pending_merge_vc_index < len(registry) and opposite(vc, registry[pending_merge_vc_index])),
            hold_mode_available=bool(joycon and vc.mode != "Switch1"),
            gyro_side_available=bool(merged and vc.mode != "Switch1"),
            ping_available=bool(getattr(vc, "loop", None)),
        ))
    return result


def wired_state(vcs):
    detected, pids = driver_actions.wired_inventory(vcs)
    return WiredState(detected, tuple(pids))
