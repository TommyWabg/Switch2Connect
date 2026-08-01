"""Motion-only state machine for the Joy-Con IR Mouse activation gate."""

from dataclasses import dataclass


IR_MOUSE_FREE_SECONDS = 0.600
IR_MOUSE_VERIFY_SECONDS = 0.300
IR_MOUSE_VERIFY_BINS = 3
IR_MOUSE_MIN_MOVING_SAMPLES = 6
IR_MOUSE_MIN_ACTIVE_BINS = 2
IR_MOUSE_MIN_PATH = 48
IR_MOUSE_MIN_SPAN = 24
IR_MOUSE_REQUIRED_WINDOWS = 2
IR_MOUSE_FAST_PATH = 256
IR_MOUSE_FAST_SPAN = 128


@dataclass(frozen=True)
class IrMouseVerificationEvent:
    result: str
    reason: str
    samples: int
    moving_samples: int
    active_bins: int
    path: int
    span_x: int
    span_y: int
    max_delta: int
    streak: int


@dataclass(frozen=True)
class IrMouseActivationState:
    mode_active: bool = False
    threshold_since: float | None = None
    previous_coords: tuple[int, int] | None = None
    latched: bool = False
    window_since: float | None = None
    window_origin: tuple[int, int] | None = None
    samples: int = 0
    moving_samples: int = 0
    active_bins_mask: int = 0
    path: int = 0
    min_x: int = 0
    max_x: int = 0
    min_y: int = 0
    max_y: int = 0
    max_delta: int = 0
    qualified_streak: int = 0


def _looping_difference_16bit(previous, current):
    """Return the shortest signed delta between two unsigned 16-bit values."""
    return ((int(current) - int(previous) + 0x8000) & 0xFFFF) - 0x8000


def _empty_window(state, now, coords, mode_active=None, streak=None):
    return IrMouseActivationState(
        state.mode_active if mode_active is None else bool(mode_active),
        state.threshold_since,
        coords,
        False,
        float(now),
        coords,
        0, 0, 0, 0, 0, 0, 0, 0, 0,
        state.qualified_streak if streak is None else int(streak),
    )


def advance_ir_mouse_activation(threshold_active, coords, now, state):
    """Advance free-mode, motion verification, and threshold-held latch state.

    Roughness and distance are intentionally absent. Verification uses only
    displacement frequency, duration, path length, and circular coordinate span.
    Returns ``(new_state, activation_origin, verification_event)``.
    """
    if not threshold_active:
        return IrMouseActivationState(), None, None

    now = float(now)
    coords = (int(coords[0]), int(coords[1]))
    if state.previous_coords is None:
        return IrMouseActivationState(False, now, coords), None, None

    threshold_since = state.threshold_since if state.threshold_since is not None else now
    dx = _looping_difference_16bit(state.previous_coords[0], coords[0])
    dy = _looping_difference_16bit(state.previous_coords[1], coords[1])
    abs_dx, abs_dy = abs(dx), abs(dy)
    has_displacement = abs_dx != 0 or abs_dy != 0

    if state.latched:
        return IrMouseActivationState(
            True, threshold_since, coords, True, qualified_streak=state.qualified_streak
        ), None, None

    activation_origin = state.previous_coords if has_displacement and not state.mode_active else None
    if now - threshold_since < IR_MOUSE_FREE_SECONDS:
        return IrMouseActivationState(
            has_displacement, threshold_since, coords, False
        ), activation_origin, None

    if state.window_since is None or state.window_origin is None:
        state = _empty_window(state, now, state.previous_coords, mode_active=has_displacement)

    elapsed = max(0.0, now - state.window_since)
    bin_width = IR_MOUSE_VERIFY_SECONDS / IR_MOUSE_VERIFY_BINS
    bin_index = min(IR_MOUSE_VERIFY_BINS - 1, int(elapsed / bin_width))
    rel_x = _looping_difference_16bit(state.window_origin[0], coords[0])
    rel_y = _looping_difference_16bit(state.window_origin[1], coords[1])
    samples = state.samples + 1
    moving_samples = state.moving_samples + int(has_displacement)
    active_bins_mask = state.active_bins_mask | ((1 << bin_index) if has_displacement else 0)
    path = state.path + abs_dx + abs_dy
    min_x, max_x = min(state.min_x, rel_x), max(state.max_x, rel_x)
    min_y, max_y = min(state.min_y, rel_y), max(state.max_y, rel_y)
    max_delta = max(state.max_delta, abs_dx + abs_dy)

    updated = IrMouseActivationState(
        has_displacement, threshold_since, coords, False,
        state.window_since, state.window_origin,
        samples, moving_samples, active_bins_mask, path,
        min_x, max_x, min_y, max_y, max_delta,
        state.qualified_streak,
    )
    if elapsed < IR_MOUSE_VERIFY_SECONDS:
        return updated, activation_origin, None

    active_bins = active_bins_mask.bit_count()
    span_x, span_y = max_x - min_x, max_y - min_y
    span = max(span_x, span_y)
    normal_ok = (
        moving_samples >= IR_MOUSE_MIN_MOVING_SAMPLES
        and active_bins >= IR_MOUSE_MIN_ACTIVE_BINS
        and path >= IR_MOUSE_MIN_PATH
        and span >= IR_MOUSE_MIN_SPAN
    )
    fast_ok = (
        active_bins == IR_MOUSE_VERIFY_BINS
        and path >= IR_MOUSE_FAST_PATH
        and span >= IR_MOUSE_FAST_SPAN
    )
    streak = state.qualified_streak + 1 if normal_ok else 0
    should_latch = fast_ok or streak >= IR_MOUSE_REQUIRED_WINDOWS
    if should_latch:
        reason = "fast_motion" if fast_ok else "sustained_motion"
        event = IrMouseVerificationEvent(
            "latch", reason, samples, moving_samples, active_bins,
            path, span_x, span_y, max_delta, streak,
        )
        return IrMouseActivationState(
            True, threshold_since, coords, True, qualified_streak=streak
        ), activation_origin, event

    reason = "qualified" if normal_ok else "insufficient_motion"
    event = IrMouseVerificationEvent(
        "continue" if normal_ok else "reject", reason,
        samples, moving_samples, active_bins, path,
        span_x, span_y, max_delta, streak,
    )
    return _empty_window(updated, now, coords, mode_active=has_displacement, streak=streak), activation_origin, event
