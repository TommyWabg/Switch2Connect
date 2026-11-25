"""Classic joystick and trigger calibration data operations."""
import logging

logger = logging.getLogger(__name__)

def build_sources(vc):
    sources = []
    for controller in vc.controllers:
        if controller.is_joycon_left():
            sources.append({"controller": controller, "side": "left", "label": "L Joystick", "key": f"{controller.device.address}:left"})
        elif controller.is_joycon_right():
            sources.append({"controller": controller, "side": "right", "label": "R Joystick", "key": f"{controller.device.address}:right"})
        else:
            sources.append({"controller": controller, "side": "right", "label": "R Joystick", "key": f"{controller.device.address}:right"})
            sources.append({"controller": controller, "side": "left", "label": "L Joystick", "key": f"{controller.device.address}:left"})
    side_order = {"left": 0, "right": 1}
    return sorted(sources, key=lambda s: side_order.get(s["side"], 2))

def read_raw(source):
    input_data = getattr(source["controller"], "last_input_data", None)
    if input_data is None:
        return (2048, 2048)
    attr = "raw_left_stick" if source["side"] == "left" else "raw_right_stick"
    return tuple(int(v) for v in getattr(input_data, attr, (2048, 2048)))

def save_joysticks(config, data, read_raw, controllers, controller_calibration_keys, normalize_calibration_key):
    updates_by_controller = {}
    for item in data:
        source = item["source"]
        samples = item["idle_samples"] or [read_raw(source)]
        cx = int(round(sum(s[0] for s in samples) / len(samples)))
        cy = int(round(sum(s[1] for s in samples) / len(samples)))
        cal = {
            "center": [cx, cy],
            "max": [max(1, item["observed_max"][0] - cx), max(1, item["observed_max"][1] - cy)],
            "min": [max(1, cx - item["observed_min"][0]), max(1, cy - item["observed_min"][1])],
        }
        controller = source["controller"]
        updates_by_controller.setdefault(controller, {})[source["side"]] = cal

    store = getattr(config, "joystick_calibration_data", {}) or {}
    for controller, sides in updates_by_controller.items():
        existing = {}
        keys = controller_calibration_keys(controller)
        normalized_keys = {normalize_calibration_key(key) for key in keys}
        for key in keys:
            if isinstance(store.get(key), dict):
                existing.update(store[key])
        for key, value in store.items():
            if normalize_calibration_key(key) in normalized_keys and isinstance(value, dict):
                existing.update(value)
        existing.update(sides)
        for key in keys:
            store[key] = existing
    config.joystick_calibration_data = store
    config.save_config()

    for controller in controllers:
        try:
            controller.apply_in_app_joystick_calibration()
        except Exception as e:
            logger.warning(f"Failed to apply joystick calibration for {controller.device.address}: {e}")

def save_gc_triggers(config, controller, values):
    config.gc_trigger_calibration_data[controller.device.address] = values
    config.save_config()
    logger.info(f"Saved GC Trigger Calibration for {controller.device.address}: {config.gc_trigger_calibration_data[controller.device.address]}")


def tick_rotate(data, dt, read_raw, distance, move_threshold, rotate_seconds):
    for item in data:
        if item["rotate_done"]:
            continue
        sample = read_raw(item["source"])
        item["observed_min"][0] = min(item["observed_min"][0], sample[0])
        item["observed_min"][1] = min(item["observed_min"][1], sample[1])
        item["observed_max"][0] = max(item["observed_max"][0], sample[0])
        item["observed_max"][1] = max(item["observed_max"][1], sample[1])
        if distance(sample, item["prev"]) >= move_threshold:
            item["rotate_elapsed"] += dt
        item["prev"] = sample
        if item["rotate_elapsed"] >= rotate_seconds:
            item["rotate_done"] = True


def tick_release(data, dt, now, read_raw, distance, move_threshold, touch_threshold,
                 still_before_countdown, release_seconds):
    for item in data:
        if item["release_done"]:
            continue
        sample = read_raw(item["source"])
        moved = distance(sample, item["prev"]) >= move_threshold
        touched = distance(sample, item["anchor"]) >= touch_threshold
        if moved or touched:
            item["still_since"] = None
            item["anchor"] = sample
        elif item["still_since"] is None:
            item["still_since"] = now
            item["anchor"] = sample

        if item["still_since"] is not None and now - item["still_since"] >= still_before_countdown:
            item["release_elapsed"] += dt
            item["idle_samples"].append(sample)
        item["prev"] = sample
        if item["release_elapsed"] >= release_seconds:
            item["release_done"] = True

def distance(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])

def tick_session(s, clock, items, alive, refresh, finish):
    if not alive():
        return
    now = clock()
    dt = min(0.1, max(0.0, now - s.last_tick))
    s.last_tick = now

    if s.stage == "rotate":
        tick_rotate(items, dt, read_raw, distance, s.MOVE_THRESHOLD, s.ROTATE_SECONDS)
        if all(item["rotate_done"] for item in items):
            s.stage = "release"
            for item in items:
                sample = read_raw(item["source"])
                item["prev"] = sample
                item["anchor"] = sample
                item["still_since"] = now
                item["idle_samples"] = []
            refresh()
    elif s.stage == "release":
        tick_release(items, dt, now, read_raw, distance, s.MOVE_THRESHOLD, s.TOUCH_THRESHOLD, s.STILL_BEFORE_COUNTDOWN, s.RELEASE_SECONDS)
        if all(item["release_done"] for item in items):
            finish()
            return False

    refresh()
    return True

def build_session_data(sources):
    data = {}
    for source in sources:
        sample = read_raw(source)
        data[source["key"]] = {
            "source": source,
            "rotate_elapsed": 0.0,
            "release_elapsed": 0.0,
            "observed_min": list(sample),
            "observed_max": list(sample),
            "prev": sample,
            "still_since": None,
            "anchor": sample,
            "idle_samples": [],
            "rotate_done": False,
            "release_done": False,
        }
    return data

def validate_sources(vc, notify):
    sources = build_sources(vc)
    if not sources:
        notify("Joysticks Calibration", "No joystick found for this player slot.")
        return None
    if any(getattr(source["controller"], "last_input_data", None) is None for source in sources):
        notify("Joysticks Calibration", "Waiting for joystick input data. Please try again in a moment.")
        return None
    return sources


def set_flags(controllers, active):
    for controller in controllers or []:
        controller.is_joystick_calibrating = active
        controller.back_button_calibration_active = active


def finish_joystick_session(s, config, items, controllers, keys, normalize, render, schedule_close):
    s.completed = True
    save_joysticks(config, items, read_raw, controllers, keys, normalize)
    set_flags(controllers, False)
    render()
    schedule_close(3000)


def sample_gc(s, controller, render):
    if hasattr(controller, 'last_input_data') and controller.last_input_data:
        l = controller.last_input_data.left_trigger_raw
        r = controller.last_input_data.right_trigger_raw
        render(l, r)
        if s.step == 0:
            s.values[0] = l
            s.values[3] = r
        elif s.step == 1:
            if l > s.values[1]: s.values[1] = l
        elif s.step == 2:
            if l > s.values[2]: s.values[2] = l
        elif s.step == 3:
            if r > s.values[4]: s.values[4] = r
        elif s.step == 4:
            if r > s.values[5]: s.values[5] = r


def next_gc(s, config, controller, ui, render, close):
    if s.step == 0:
        s.step = 1
        render(s.step, "title")
        s.values[1] = 0
    elif s.step == 1:
        s.step = 2
        render(s.step, "title")
        s.values[2] = 0
    elif s.step == 2:
        s.step = 3
        render(s.step, "title")
        s.values[4] = 0
    elif s.step == 3:
        s.step = 4
        render(s.step, "title")
        s.values[5] = 0
        render(s.step, "finish")
    elif s.step == 4:
        save_gc_triggers(config, controller, list(s.values))
        ui.show_message("Success", "GameCube Trigger Calibration saved successfully!")
        close()


GC_STEPS = (
    ("Step 1: Base State", "Release both triggers completely and wait a moment.\nThen click Next."),
    ("Step 2: Left Trigger (Bump)", "Press the LEFT trigger down just until you feel the click (bump).\nHold it there and click Next."),
    ("Step 3: Left Trigger (Max)", "Fully press the LEFT trigger all the way down past the click.\nWhile holding it down, click Next."),
    ("Step 4: Right Trigger (Bump)", "Press the RIGHT trigger down just until you feel the click (bump).\nHold it there and click Next."),
    ("Step 5: Right Trigger (Max)", "Fully press the RIGHT trigger all the way down past the click.\nWhile holding it down, click Finish."),
)

def cancel_controller_calibrations(controllers):
    calibration_active = False
    for vc in controllers or []:
        for controller in getattr(vc, "controllers", []) or []:
            if (getattr(controller, "is_calibration_counting_down", False) or
                    getattr(controller, "is_calibrating", False) or
                    getattr(controller, "is_mag_calibration_waiting", False) or
                    getattr(controller, "is_mag_calibrating", False) or
                    getattr(controller, "is_joystick_calibrating", False) or
                    getattr(controller, "back_button_calibration_active", False)):
                calibration_active = True
            cancel = getattr(controller, "cancel_back_button_calibration_state", None)
            if callable(cancel):
                cancel()
            else:
                controller.is_calibration_counting_down = False
                controller.is_calibrating = False
                controller.is_mag_calibration_waiting = False
                controller.is_mag_calibrating = False
                controller.is_joystick_calibrating = False
                controller.back_button_calibration_active = False
                controller.prev_calibration = False
    return calibration_active
