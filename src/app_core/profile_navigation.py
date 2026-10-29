"""Classic profile selection state machine with injected UI scheduling."""



def on_cycle_profile(owner, config, utils, schedule, cancel, is_ui_thread, clock, close_selection, switch, result):
    if not hasattr(config, 'active_profile') or not config.profiles:
        return result(False, "empty_change_list")

    # Execute on main thread to avoid Tkinter threading errors
    if not is_ui_thread():
        schedule(0, owner.on_cycle_profile)
        return

    sorted_profiles = owner._change_list_profiles()
    if not sorted_profiles:
        return result(False, "empty_change_list")

    # Initialize pending profile if not set
    if not hasattr(owner, 'pending_profile') or not owner.pending_profile:
        owner.pending_profile = config.active_profile

    try:
        curr_idx = sorted_profiles.index(owner.pending_profile)
        next_idx = (curr_idx + 1) % len(sorted_profiles)
    except ValueError:
        next_idx = 0

    owner.pending_profile = sorted_profiles[next_idx]

    manual = getattr(config, "change_profile_mode", "Manual") == "Manual"

    # Cancel any pending auto-apply timer
    if hasattr(owner, 'profile_apply_timer') and owner.profile_apply_timer:
        cancel(owner.profile_apply_timer)
        owner.profile_apply_timer = None

    if manual:
        # Enter selection mode: pause virtual output, wait for A (confirm) / B (cancel).
        utils.profile_selection_active = True
        owner._show_profile_selection_notification(True)
    else:
        # Auto: show the selection and auto-apply after a second of inactivity.
        utils.profile_selection_active = False
        owner._show_profile_selection_notification(False)
        owner.profile_apply_timer = schedule(1000, owner.apply_pending_profile)
    return result(True)


def on_profile_nav(owner, config, utils, schedule, cancel, is_ui_thread, clock, close_selection, switch, result, direction):
    if not is_ui_thread():
        schedule(0, owner.on_profile_nav, direction)
        return
    if not utils.profile_selection_active:
        return result(False, "inactive")
    # Debounce so a single flick / Dpad tap doesn't advance multiple steps even
    # when reported by both controllers of a merged pair.
    now = clock()
    if now - getattr(owner, "_last_profile_nav_time", 0.0) < 0.18:
        return result(False, "throttled")
    owner._last_profile_nav_time = now
    lst = owner._change_list_profiles()
    if not lst:
        return result(False, "empty_change_list")
    sel = owner.pending_profile if owner.pending_profile in lst else lst[0]
    idx = lst.index(sel)
    owner.pending_profile = lst[(idx + direction) % len(lst)]
    owner._show_profile_selection_notification(True)
    return result(True)


def on_profile_confirm(owner, config, utils, schedule, cancel, is_ui_thread, clock, close_selection, switch, result):
    if not is_ui_thread():
        schedule(0, owner.on_profile_confirm)
        return
    if not utils.profile_selection_active:
        return result(False, "inactive")
    utils.profile_selection_active = False
    close_selection("confirm")
    target = getattr(owner, "pending_profile", None)
    owner.pending_profile = None
    if target and target != getattr(config, "active_profile", ""):
        return switch(target)
    return result(True)


def on_profile_cancel(owner, config, utils, schedule, cancel, is_ui_thread, clock, close_selection, switch, result):
    if not is_ui_thread():
        schedule(0, owner.on_profile_cancel)
        return
    utils.profile_selection_active = False
    close_selection("cancel")
    owner.pending_profile = None
    return result(True)


def apply_pending_profile(owner, config, utils, schedule, cancel, is_ui_thread, clock, close_selection, switch, result):
    owner.profile_apply_timer = None
    if not hasattr(owner, 'pending_profile') or not owner.pending_profile:
        return result(False, "unchanged")

    if owner.pending_profile == getattr(config, 'active_profile', ""):
        return result(False, "unchanged") # No change

    switched = switch(owner.pending_profile)

    owner.pending_profile = None
    return switched
