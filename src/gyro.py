"""Consolidated gyro implementation.

The V2 pipeline and Phase 0 diagnostics are kept in separate marked sections
while sharing one production module.
"""

from __future__ import annotations

import atexit
from collections import deque
from dataclasses import asdict, dataclass
import datetime as _datetime
import itertools
import json
import logging
import math
import os
from pathlib import Path
import queue
import statistics
import threading
import time
from typing import Callable, Iterator, Mapping, Sequence

logger = logging.getLogger(__name__)

# =============================================================================
# V2 gyro pipeline: canonical units, timing, bias, magnetometer and AHRS
# =============================================================================

S2_ACCEL_LSB_PER_G = 4096.0
S2_GYRO_LSB_PER_DPS_PRO = 14.285714
S2_GYRO_LSB_PER_DPS_JOYCON = 16.384

V2_DEFAULT_DT_SECONDS = 0.015
V2_MIN_DT_SECONDS = 0.001
V2_MAX_DT_SECONDS = 0.050
V2_RESET_GAP_SECONDS = 0.100
V2_AHRS_GAIN = 0.1
V2_GYRO_RANGE_DPS = 2000.0
V2_ACCEL_REJECTION_DEG = 10.0
V2_MAG_REJECTION_DEG = 20.0
V2_RECOVERY_SECONDS = 5.0
V2_RATE_WINDOW_SECONDS = 2.0
V2_RATE_UPDATE_SECONDS = 1.0
V2_RUNTIME_BIAS_DWELL_SECONDS = 5.0
V2_RUNTIME_BIAS_CUTOFF_HZ = 0.02
V2_RUNTIME_BIAS_LIMIT_DPS = 5.0
V2_STATIONARY_GYRO_LIMIT_DPS = 3.0
V2_STATIONARY_ACCEL_TOLERANCE_G = 0.03
V2_STATIONARY_ACCEL_SD_LIMIT_G = 0.005
V2_STATIONARY_GYRO_SD_LIMIT_DPS = 0.15
V2_STATIONARY_VARIANCE_WINDOW_SECONDS = 0.5
V2_STATIONARY_MOVEMENT_HINT_DPS = 0.05
V2_MAG_BASELINE_SECONDS = 3.0
V2_MAG_BASELINE_MIN_SAMPLES = 100
V2_MAG_RATIO_MIN = 0.7
V2_MAG_RATIO_MAX = 1.3
V2_MAG_JUMP_RATIO = 0.15
V2_MAG_RECOVERY_SECONDS = 1.0
V2_MAG_DIRECTION_REJECTION_DEG = 20.0
V2_MAG_DIRECTION_RECOVERY_DEG = 15.0
V2_MAG_MOTION_AWARE_DIRECTION_ENABLED = True
V2_MAG_DIRECTION_CHECK_MAX_RATE_DPS = 3.0
V2_MAG_DIRECTION_MOTION_EXIT_RATE_DPS = 1.5
V2_MAG_DIRECTION_SETTLING_SECONDS = 0.2
MAG_REFERENCE_MODELS = ("Legacy", "Shadow", "Adaptive")
PRODUCTION_MAG_REFERENCE_MODEL = "Adaptive"
MAG_REFERENCE_MODEL = os.environ.get(
    "SWITCH2_MAG_REFERENCE_MODEL",
    PRODUCTION_MAG_REFERENCE_MODEL).strip()
if MAG_REFERENCE_MODEL not in MAG_REFERENCE_MODELS:
    MAG_REFERENCE_MODEL = PRODUCTION_MAG_REFERENCE_MODEL
V2_MAG_ADAPTIVE_CANDIDATE_SECONDS = 0.25
V2_MAG_ADAPTIVE_TRACK_SECONDS = 20.0
V2_MAG_ADAPTIVE_STABLE_STEP_RATIO = 0.05
V2_MAG_ADAPTIVE_DIRECTION_SECONDS = 0.5
V2_MAG_ADAPTIVE_DIRECTION_STABILITY_DEG = 2.0
MOTION_MAG_CLOSURE_REVALIDATION_SECONDS = 0.5
MOTION_MAG_CLOSURE_REVALIDATION_MAX_INNOVATION_DEG = 20.0
V2_HEADING_CORRECTION_LEGACY_MAX_DPS = 0.25
V2_HEADING_AUTHORITY_LEVELS_DPS = (0.25, 0.5, 0.75)
# Production default validated by the Phase 5 selected-output and Phase 6
# interference/recovery recordings.  Keep 0.25 and 0.75 as explicit,
# reversible overrides; 0.75 remains evaluation-only rather than the default.
V2_HEADING_PRODUCTION_MAX_DPS = 0.5
try:
    _heading_authority_requested = float(os.environ.get(
        "SWITCH2_GYRO_HEADING_MAX_DPS",
        str(V2_HEADING_PRODUCTION_MAX_DPS)))
except (TypeError, ValueError):
    _heading_authority_requested = V2_HEADING_PRODUCTION_MAX_DPS
V2_HEADING_CORRECTION_MAX_DPS = next(
    (level for level in V2_HEADING_AUTHORITY_LEVELS_DPS
     if abs(_heading_authority_requested - level) < 1e-9),
    V2_HEADING_PRODUCTION_MAX_DPS,
)
V2_HEADING_CORRECTION_KP = 0.20
V2_HEADING_CORRECTION_DEADBAND_DEG = 0.3
V2_HEADING_CORRECTION_RAMP_SECONDS = 0.5
V2_HEADING_CONTROLLER_MODES = ("Legacy", "Shadow", "V2")
V2_HEADING_CONTROLLER_MODE = os.environ.get(
    "SWITCH2_GYRO_HEADING_CONTROLLER", "V2").strip()
if V2_HEADING_CONTROLLER_MODE not in V2_HEADING_CONTROLLER_MODES:
    V2_HEADING_CONTROLLER_MODE = "V2"
IN_APP_HORIZON_PIPELINE_MODES = ("Legacy", "Shadow", "V2")
IN_APP_HORIZON_PIPELINE_MODE = os.environ.get(
    "SWITCH2_INAPP_HORIZON_PIPELINE", "V2").strip()
if IN_APP_HORIZON_PIPELINE_MODE not in IN_APP_HORIZON_PIPELINE_MODES:
    IN_APP_HORIZON_PIPELINE_MODE = "V2"
V2_POST_MOTION_GATE_MODES = ("Legacy", "Shadow", "V2")
V2_POST_MOTION_GATE_MODE = os.environ.get(
    "SWITCH2_GYRO_POST_MOTION_GATE", "V2").strip()
if V2_POST_MOTION_GATE_MODE not in V2_POST_MOTION_GATE_MODES:
    V2_POST_MOTION_GATE_MODE = "V2"
V2_POST_MOTION_OBSERVATION_SECONDS = 0.3
V2_POST_MOTION_INNOVATION_SD_LIMIT_DEG = 3.0
MAG_CALIBRATION_MODELS = ("HardIron", "SoftIronShadow", "SoftIron")
# Validated production selection.  Environment overrides remain available for
# immediate per-feature rollback to SoftIronShadow/HardIron and Shadow/Off.
MAG_CALIBRATION_MODEL = os.environ.get(
    "SWITCH2_MAG_CALIBRATION_MODEL", "SoftIron").strip()
if MAG_CALIBRATION_MODEL not in MAG_CALIBRATION_MODELS:
    MAG_CALIBRATION_MODEL = "SoftIron"
HEADING_INITIALIZATION_MODES = ("Legacy", "Deferred")
HEADING_INITIALIZATION_MODE = os.environ.get(
    "SWITCH2_GYRO_HEADING_INITIALIZATION", "Deferred").strip()
if HEADING_INITIALIZATION_MODE not in HEADING_INITIALIZATION_MODES:
    HEADING_INITIALIZATION_MODE = "Deferred"
V2_OUTPUT_MODES = ("Legacy", "Shadow", "V2")
PASSTHROUGH_HEADING_OUTPUT_MODES = ("Legacy", "Shadow", "V2")
# V2 is the validated production selection.  Legacy and Shadow remain
# available as immediate rollback/diagnostic overrides.
PASSTHROUGH_HEADING_OUTPUT_MODE = os.environ.get(
    "SWITCH2_GYRO_PASSTHROUGH_HEADING_OUTPUT", "V2").strip()
if PASSTHROUGH_HEADING_OUTPUT_MODE not in PASSTHROUGH_HEADING_OUTPUT_MODES:
    PASSTHROUGH_HEADING_OUTPUT_MODE = "V2"
PASSTHROUGH_HEADING_OUTPUT_LOWPASS_SECONDS = 0.75
PASSTHROUGH_HEADING_OUTPUT_SLEW_DPS_PER_SECOND = 0.25
PASSTHROUGH_HEADING_OUTPUT_ENTER_DPS = 0.04
PASSTHROUGH_HEADING_OUTPUT_EXIT_DPS = 0.02
PASSTHROUGH_HEADING_OUTPUT_SAFETY_RELEASE_STEP_DPS = 0.10
PASSTHROUGH_MOVING_YAW_BIAS_MODES = ("Off", "Shadow", "V2")
PASSTHROUGH_MOVING_YAW_BIAS_MODE = os.environ.get(
    "SWITCH2_PASSTHROUGH_MOVING_YAW_BIAS", "Off").strip()
if PASSTHROUGH_MOVING_YAW_BIAS_MODE not in PASSTHROUGH_MOVING_YAW_BIAS_MODES:
    PASSTHROUGH_MOVING_YAW_BIAS_MODE = "Off"
MOVING_YAW_BIAS_WINDOW_SECONDS = 2.0
MOVING_YAW_BIAS_MIN_RATE_DPS = 3.0
MOVING_YAW_BIAS_MAX_RATE_DPS = 30.0
MOVING_YAW_BIAS_ACCEL_TOLERANCE_G = 0.03
MOVING_YAW_BIAS_RATE_LOWPASS_SECONDS = 0.15
MOVING_YAW_BIAS_WINDOW_RATE_SD_LIMIT_DPS = 3.0
MOVING_YAW_BIAS_DIRECTION_ERROR_LIMIT_DPS = 5.0
MOVING_YAW_BIAS_RAW_LIMIT_DPS = 0.25
MOVING_YAW_BIAS_APPLIED_LIMIT_DPS = 0.10
MOVING_YAW_BIAS_SCALE_LIMIT_DPS = 3.0
MOVING_YAW_BIAS_DIRECTION_SPREAD_LIMIT_DPS = 0.75
MOVING_YAW_BIAS_MIN_WINDOWS_PER_DIRECTION = 2
MOVING_YAW_BIAS_SLEW_DPS_PER_SECOND = 0.01
MOTION_MAG_CLOSURE_MODES = ("Off", "Shadow", "V2")
PRODUCTION_INAPP_MOTION_MAG_CLOSURE_MODE = "V2"
PRODUCTION_PASSTHROUGH_MOTION_MAG_CLOSURE_MODE = "V2"
INAPP_MOTION_MAG_CLOSURE_MODE = os.environ.get(
    "SWITCH2_INAPP_MOTION_MAG_CLOSURE",
    PRODUCTION_INAPP_MOTION_MAG_CLOSURE_MODE).strip()
if INAPP_MOTION_MAG_CLOSURE_MODE not in MOTION_MAG_CLOSURE_MODES:
    INAPP_MOTION_MAG_CLOSURE_MODE = (
        PRODUCTION_INAPP_MOTION_MAG_CLOSURE_MODE)
PASSTHROUGH_MOTION_MAG_CLOSURE_MODE = os.environ.get(
    "SWITCH2_PASSTHROUGH_MOTION_MAG_CLOSURE",
    PRODUCTION_PASSTHROUGH_MOTION_MAG_CLOSURE_MODE).strip()
if PASSTHROUGH_MOTION_MAG_CLOSURE_MODE not in MOTION_MAG_CLOSURE_MODES:
    PASSTHROUGH_MOTION_MAG_CLOSURE_MODE = (
        PRODUCTION_PASSTHROUGH_MOTION_MAG_CLOSURE_MODE)
MOTION_MAG_CLOSURE_RATE_LOWPASS_SECONDS = 0.15
MOTION_MAG_CLOSURE_INNOVATION_LOWPASS_SECONDS = 1.5
MOTION_MAG_CLOSURE_KP = 0.8
MOTION_MAG_CLOSURE_MAX_DPS = 0.10
MOTION_MAG_CLOSURE_MAX_RATE_FRACTION = 0.02
MOTION_MAG_CORRECTION_LAWS = ("Legacy", "DynamicRate", "RobustBiasRate")
PRODUCTION_MOTION_MAG_CORRECTION_LAW = "RobustBiasRate"
MOTION_MAG_CORRECTION_LAW = os.environ.get(
    "SWITCH2_MOTION_MAG_CORRECTION_LAW",
    PRODUCTION_MOTION_MAG_CORRECTION_LAW).strip()
if MOTION_MAG_CORRECTION_LAW not in MOTION_MAG_CORRECTION_LAWS:
    MOTION_MAG_CORRECTION_LAW = PRODUCTION_MOTION_MAG_CORRECTION_LAW
MOTION_MAG_DYNAMIC_CORRECTION_RATIO = 0.80
MOTION_MAG_DYNAMIC_ATTACK_SLEW_RATIO_PER_SECOND = 0.40
MOTION_MAG_CLOSURE_MOTION_ENTER_DPS = 3.0
MOTION_MAG_CLOSURE_MOTION_EXIT_DPS = 0.5
MOTION_MAG_CLOSURE_BIN_COUNT = 24
MOTION_MAG_CLOSURE_BIN_SAMPLE_SECONDS = 0.25
MOTION_MAG_CLOSURE_BIN_MIN_SAMPLES = 8
MOTION_MAG_CLOSURE_BIN_SD_LIMIT_DEG = 3.0
MOTION_MAG_CLOSURE_BASE_CONFIDENCE = 0.25
MOTION_MAG_CLOSURE_LOW_MOTION_FULL_DPS = 1.0
MOTION_MAG_CLOSURE_DYNAMIC_ACCEL_LOWPASS_SECONDS = 0.12
MOTION_MAG_CLOSURE_DYNAMIC_ACCEL_ENTER_DPS2 = 1200.0
MOTION_MAG_CLOSURE_DYNAMIC_ACCEL_EXIT_DPS2 = 600.0
MOTION_MAG_CLOSURE_DYNAMIC_ENTER_SECONDS = 0.08
MOTION_MAG_CLOSURE_SETTLING_SECONDS = 0.20
MOTION_MAG_CLOSURE_SETTLING_ZERO_SECONDS = 0.05
MOTION_MAG_CLOSURE_BIN_PAUSE_LIMIT_SECONDS = 0.10
MOTION_MAG_CLOSURE_SLEW_DPS_PER_SECOND = 0.01
MOTION_MAG_CONSUMER_REENTRY_ZERO_SECONDS = 0.05
MOTION_MAG_CONSUMER_REENTRY_RAMP_SECONDS = 0.25
MOTION_MAG_RELATIVE_DELTA_REJECTION_DEG = 0.75
MOTION_MAG_RELATIVE_DELTA_REJECTION_SECONDS = 0.20
MOTION_MAG_CONSISTENCY_WINDOW_SECONDS = 1.0
MOTION_MAG_CONSISTENCY_MIN_SECONDS = 0.5
MOTION_MAG_CONSISTENCY_BUCKET_SECONDS = 0.10
MOTION_MAG_CONSISTENCY_MIN_SAMPLES = 5
MOTION_MAG_CONSISTENCY_MIN_NET_GYRO_DEG = 0.10
MOTION_MAG_CONSISTENCY_FULL_NET_GYRO_DEG = 0.50
MOTION_MAG_CONSISTENCY_MIN_RAW_SAMPLES = 30
MOTION_MAG_CONSISTENCY_MIN_RAW_GYRO_DELTA_DEG = 0.005
MOTION_MAG_CONFIDENCE_RELEASE_SECONDS = 3.0
MOTION_MAG_CONFIDENCE_INVALID_RESET_SECONDS = 5.0
MOTION_MAG_AUTHORITY_MISMATCH_SECONDS = 1.0
MOTION_MAG_AUTHORITY_CONFIRM_BUCKETS = 2
ROBUST_BIAS_BUCKET_SECONDS = 0.10
ROBUST_BIAS_OBSERVATION_CAP_DPS = 0.25
ROBUST_BIAS_ESTIMATE_SECONDS = 5.0
ROBUST_BIAS_OUTPUT_CAP_DPS = 0.10
ROBUST_BIAS_MIN_BUCKETS = 10
ROBUST_BIAS_FULL_BUCKETS = 30
ROBUST_BIAS_DECAY_DELAY_SECONDS = 1.0
TRANSIENT_RECOVERY_VALIDATION_SECONDS = 0.40
TRANSIENT_RECOVERY_SAMPLE_SECONDS = 0.10
TRANSIENT_RECOVERY_MIN_SAMPLES = 3
TRANSIENT_RECOVERY_STABILITY_DEG = 0.35
TRANSIENT_RECOVERY_MIN_ERROR_DEG = 0.15
TRANSIENT_RECOVERY_MAX_DEBT_DEG = 3.0
TRANSIENT_RECOVERY_KP = 0.8
TRANSIENT_RECOVERY_OUTPUT_CAP_DPS = 0.30
MOTION_MAG_CONSISTENCY_CORRELATION_FLOOR = 0.50
MOTION_MAG_CONSISTENCY_CORRELATION_FULL = 0.85
MOTION_MAG_CONSISTENCY_SCALE_MIN = 0.75
MOTION_MAG_CONSISTENCY_SCALE_MAX = 1.25
MOTION_MAG_COMMIT_SCALE_MIN = 0.85
MOTION_MAG_COMMIT_SCALE_MAX = 1.15
MOTION_MAG_MAX_BIAS_RATE_DPS = 0.50


@dataclass(frozen=True)
class V2Timing:
    dt_seconds: float
    status: str
    integrate: bool
    reset_estimator: bool
    allow_bias_update: bool


@dataclass(frozen=True)
class CanonicalSensorFrame:
    accelerometer_g: tuple[float, float, float]
    gyroscope_dps: tuple[float, float, float]
    magnetometer_lsb: tuple[float, float, float]
    gyro_lsb_per_dps: float
    timing: V2Timing

    def to_dict(self) -> dict:
        return asdict(self)


def _fill_buffer(buffer, values):
    """Refill a preallocated 3-element buffer in place and return it."""
    buffer[0] = values[0]
    buffer[1] = values[1]
    buffer[2] = values[2]
    return buffer


def _quaternion_rotate_vector_wxyz(
    quaternion_wxyz: Sequence[float], vector: Sequence[float]
) -> tuple[float, float, float]:
    q = tuple(float(value) for value in quaternion_wxyz)
    v = _vector3(vector, "vector")
    if len(q) != 4 or not all(math.isfinite(value) for value in q):
        raise ValueError("quaternion_wxyz must contain four finite numbers")
    norm = math.sqrt(sum(value * value for value in q))
    if norm <= 1e-9:
        raise ValueError("quaternion_wxyz must be non-zero")
    w, x, y, z = (value / norm for value in q)
    tx = 2.0 * (y * v[2] - z * v[1])
    ty = 2.0 * (z * v[0] - x * v[2])
    tz = 2.0 * (x * v[1] - y * v[0])
    return (
        v[0] + w * tx + (y * tz - z * ty),
        v[1] + w * ty + (z * tx - x * tz),
        v[2] + w * tz + (x * ty - y * tx),
    )


def build_v2_gyro_output(
    corrected_gyro_dps: Sequence[float],
    orientation_wxyz: Sequence[float],
    *,
    gyro_lsb_per_dps: float,
    is_pro_controller: bool,
    hold_mode: str,
    horizon_lock: bool,
    orientation_valid: bool,
    heading_constrained: bool,
    soft_deadzone_lsb: float = 0.0,
) -> dict:
    """Build a Phase 5/6 candidate without mutating a Legacy input report."""
    # A local corrected-rate output does not consume orientation.  Requiring a
    # magnetic heading here used to delay (or permanently block) V2 even with
    # Horizon disabled.  Only the projected Horizon output needs an orientation.
    if horizon_lock and not orientation_valid:
        return {
            "available": False,
            "reason": "orientation-uninitialised",
            "horizon_lock": bool(horizon_lock),
            "heading_constrained": bool(heading_constrained),
        }
    gyro_dps = _vector3(corrected_gyro_dps, "corrected_gyro_dps")
    scale = float(gyro_lsb_per_dps)
    if not math.isfinite(scale) or scale <= 0.0:
        raise ValueError("gyro_lsb_per_dps must be positive and finite")
    gyro_lsb = tuple(value * scale for value in gyro_dps)
    horizontal = str(hold_mode) == "Horizontal"
    vertical_axes = bool(is_pro_controller) or not horizontal
    output = gyro_lsb
    if horizon_lock:
        local = (gyro_lsb[0], 0.0, gyro_lsb[2]) if vertical_axes else (
            0.0, gyro_lsb[1], gyro_lsb[2])
        world = _quaternion_rotate_vector_wxyz(orientation_wxyz, local)
        forward_local = (0.0, 1.0, 0.0) if vertical_axes else (1.0, 0.0, 0.0)
        forward_world = _quaternion_rotate_vector_wxyz(
            orientation_wxyz, forward_local)
        horizontal_magnitude = math.hypot(forward_world[0], forward_world[1])
        right_horizontal = ((1.0, 0.0, 0.0) if horizontal_magnitude < 0.01 else
                            (forward_world[1] / horizontal_magnitude,
                             -forward_world[0] / horizontal_magnitude, 0.0))
        pitch = world[0] * right_horizontal[0] + world[1] * right_horizontal[1]
        yaw = world[2]
        output = (pitch, 0.0, yaw) if vertical_axes else (0.0, -pitch, yaw)
    deadzone = max(0.0, float(soft_deadzone_lsb))
    if deadzone:
        values = list(output)
        for axis in ((0, 2) if vertical_axes else (1, 2)):
            value = values[axis]
            values[axis] = (value - deadzone if value > deadzone else
                            value + deadzone if value < -deadzone else 0.0)
        output = tuple(values)
    return {
        "available": True,
        "reason": None,
        "gyroscope": list(output),
        "corrected_local_gyroscope": list(gyro_lsb),
        "horizon_lock": bool(horizon_lock),
        "heading_constrained": bool(heading_constrained),
        "orientation_valid": True,
    }


class PassthroughHeadingOutputFilter:
    """Turn a noisy heading-controller candidate into low-frequency authority."""

    def __init__(self):
        self.filtered_rate_dps = 0.0
        self.output_rate_dps = 0.0
        self.active = False

    def reset(self) -> None:
        self.filtered_rate_dps = 0.0
        self.output_rate_dps = 0.0
        self.active = False

    def update(
        self,
        target_rate_dps: float,
        dt_seconds: float,
        *,
        eligible: bool,
        correction_authorized: bool,
    ) -> dict:
        try:
            target = float(target_rate_dps)
            dt = float(dt_seconds)
        except (TypeError, ValueError):
            target = 0.0
            dt = 0.0
        if not math.isfinite(target):
            target = 0.0
        target = max(
            -V2_HEADING_CORRECTION_MAX_DPS,
            min(V2_HEADING_CORRECTION_MAX_DPS, target),
        )
        valid_dt = math.isfinite(dt) and 0.0 < dt <= V2_MAX_DT_SECONDS
        if not bool(eligible) or not valid_dt:
            self.reset()
            return {
                "target_rate_dps": target,
                "filtered_rate_dps": 0.0,
                "output_rate_dps": 0.0,
                "active": False,
                "authorized": False,
                "reset": True,
                "safety_release_active": False,
            }
        if not bool(correction_authorized):
            # Revoke magnetic authority immediately, but remove its already
            # selected output over a few frames so a higher authority cap cannot
            # create a one-frame mouse jump at motion/disturbance entry.
            self.filtered_rate_dps = 0.0
            self.active = False
            release = min(
                PASSTHROUGH_HEADING_OUTPUT_SAFETY_RELEASE_STEP_DPS,
                abs(self.output_rate_dps),
            )
            self.output_rate_dps -= math.copysign(
                release, self.output_rate_dps) if release else 0.0
            if abs(self.output_rate_dps) <= 1e-12:
                self.output_rate_dps = 0.0
            return {
                "target_rate_dps": target,
                "filtered_rate_dps": 0.0,
                "output_rate_dps": self.output_rate_dps,
                "active": False,
                "authorized": False,
                "reset": True,
                "safety_release_active": self.output_rate_dps != 0.0,
                "safety_release_max_step_dps": (
                    PASSTHROUGH_HEADING_OUTPUT_SAFETY_RELEASE_STEP_DPS),
            }

        alpha = 1.0 - math.exp(
            -dt / PASSTHROUGH_HEADING_OUTPUT_LOWPASS_SECONDS)
        self.filtered_rate_dps += alpha * (
            target - self.filtered_rate_dps)
        magnitude = abs(self.filtered_rate_dps)
        if self.active:
            if magnitude <= PASSTHROUGH_HEADING_OUTPUT_EXIT_DPS:
                self.active = False
        elif magnitude >= PASSTHROUGH_HEADING_OUTPUT_ENTER_DPS:
            self.active = True

        desired = self.filtered_rate_dps if self.active else 0.0
        maximum_step = PASSTHROUGH_HEADING_OUTPUT_SLEW_DPS_PER_SECOND * dt
        delta = max(
            -maximum_step,
            min(maximum_step, desired - self.output_rate_dps),
        )
        self.output_rate_dps += delta
        if not self.active and abs(self.output_rate_dps) <= maximum_step:
            self.output_rate_dps = 0.0
        return {
            "target_rate_dps": target,
            "filtered_rate_dps": self.filtered_rate_dps,
            "output_rate_dps": self.output_rate_dps,
            "active": self.active,
            "authorized": True,
            "reset": False,
            "safety_release_active": False,
            "lowpass_seconds": PASSTHROUGH_HEADING_OUTPUT_LOWPASS_SECONDS,
            "slew_rate_dps_per_second": (
                PASSTHROUGH_HEADING_OUTPUT_SLEW_DPS_PER_SECOND),
            "enter_threshold_dps": PASSTHROUGH_HEADING_OUTPUT_ENTER_DPS,
            "exit_threshold_dps": PASSTHROUGH_HEADING_OUTPUT_EXIT_DPS,
        }


class MotionMagneticClosureEstimator:
    """Low-frequency magnetic path-closure authority, emitted only in motion."""

    def __init__(self, correction_law: str | None = None):
        self.correction_law = (
            correction_law
            if correction_law in MOTION_MAG_CORRECTION_LAWS
            else MOTION_MAG_CORRECTION_LAW)
        self.reset()

    def reset(self) -> None:
        self.measurement_epoch = getattr(self, "measurement_epoch", -1) + 1
        self.anchor_heading_deg = None
        self.unwrapped_gyro_heading_deg = 0.0
        self.unwrapped_magnetic_heading_deg = 0.0
        self.gyro_relative_heading_deg = 0.0
        self.magnetic_relative_heading_deg = 0.0
        self.relative_closure_error_deg = 0.0
        self.raw_magnetic_heading_delta_deg = 0.0
        self.normalized_magnetic_heading_delta_deg = 0.0
        self.gyro_world_yaw_delta_deg = 0.0
        self.gyro_reference_heading_delta_deg = 0.0
        self.heading_delta_direction_agreement = True
        self.relative_delta_innovation_deg = 0.0
        self.relative_delta_rejected = False
        self._relative_delta_reject_elapsed = 0.0
        self._consistency_samples = deque()
        self._raw_consistency_samples = deque()
        self._consistency_seconds = 0.0
        self._consistency_clock = 0.0
        self.consistency_correlation = 0.0
        self.consistency_scale = 0.0
        self.consistency_confidence = 0.0
        self.rolling_consistency_confidence = 0.0
        self.aggregate_consistency_confidence = 0.0
        self._validated_consistency_confidence = 0.0
        self._confidence_invalid_elapsed = 0.0
        self._authority_mismatch_elapsed = 0.0
        self._authority_confirmation_buckets = 0
        self.authority_suspended = False
        self.observed_bias_rate_dps = 0.0
        self.bias_rate_valid = False
        self.commit_scale_valid = False
        self.committed_error_increment_deg = 0.0
        self.frozen_correction_blocked = False
        self.robust_bias_bucket_elapsed = 0.0
        self.robust_bias_bucket_magnetic_delta_deg = 0.0
        self.robust_bias_bucket_gyro_delta_deg = 0.0
        self.robust_bias_raw_observation_dps = 0.0
        self.robust_bias_bounded_observation_dps = 0.0
        self.robust_bias_estimate_dps = 0.0
        self.robust_bias_valid_buckets = 0
        self.robust_bias_observation_age_seconds = float("inf")
        self.robust_bias_confidence = 0.0
        self.robust_bias_observation_clipped = False
        self.robust_bias_decay_active = False
        self.transient_epoch = getattr(self, "transient_epoch", -1) + 1
        self.transient_event_active = False
        self.transient_event_valid = False
        self.transient_event_error_deg = 0.0
        self.transient_validation_elapsed = 0.0
        self.transient_validation_bucket_elapsed = 0.0
        self.transient_validation_samples = deque()
        self.transient_confidence = 0.0
        self.transient_commit_increment_deg = 0.0
        self.transient_authority_active = False
        self._transient_was_dynamic = False
        self.window_accepted = False
        self.window_magnetic_delta_deg = 0.0
        self.window_gyro_delta_deg = 0.0
        self.committed_heading_error_deg = 0.0
        self.residual_state = "Reset"
        self._consistency_reference_ready = False
        self._was_stationary = True
        self._consistency_bucket_elapsed = 0.0
        self._consistency_bucket_mag_delta = 0.0
        self._consistency_bucket_gyro_delta = 0.0
        self.confidence_state = "Waiting for Motion"
        self._relative_last_magnetic_heading = None
        self._relative_last_gyro_heading = None
        self._relative_anchor_required = True
        self._last_current_heading = None
        self._last_magnetic_heading = None
        self._last_raw_innovation = None
        self.filtered_rate_dps = 0.0
        self.filtered_innovation_deg = 0.0
        self._last_rate_dps = None
        self.filtered_angular_acceleration_dps2 = 0.0
        self._dynamic_enter_elapsed = 0.0
        self._dynamic_active = False
        self._last_valid_motion_state = "not-initialized"
        self._revalidation_required = True
        self._revalidation_elapsed = 0.0
        self._settling_elapsed = MOTION_MAG_CLOSURE_SETTLING_SECONDS
        self._output_rate_dps = 0.0
        self._bin_elapsed = 0.0
        self._bin_pause_elapsed = 0.0
        self._bin_sample_key = None
        self._bins = [{} for _ in range(MOTION_MAG_CLOSURE_BIN_COUNT)]
        self._last_state = self._state(["not-initialized"])

    @staticmethod
    def _smoothstep(value: float) -> float:
        value = max(0.0, min(1.0, value))
        return value * value * (3.0 - 2.0 * value)

    @classmethod
    def _correlation_confidence(cls, mag_values, gyro_values):
        if len(mag_values) < 2 or len(mag_values) != len(gyro_values):
            return 0.0, 0.0, 0.0
        mag_mean = statistics.fmean(mag_values)
        gyro_mean = statistics.fmean(gyro_values)
        covariance = statistics.fmean(
            (mag_value - mag_mean) * (gyro_value - gyro_mean)
            for mag_value, gyro_value in zip(mag_values, gyro_values))
        mag_variance = statistics.fmean(
            (value - mag_mean) ** 2 for value in mag_values)
        gyro_variance = statistics.fmean(
            (value - gyro_mean) ** 2 for value in gyro_values)
        denominator = math.sqrt(mag_variance * gyro_variance)
        correlation = covariance / denominator if denominator > 1e-12 else 0.0
        scale = covariance / gyro_variance if gyro_variance > 1e-12 else 0.0
        correlation_weight = cls._smoothstep(
            (correlation - MOTION_MAG_CONSISTENCY_CORRELATION_FLOOR)
            / (MOTION_MAG_CONSISTENCY_CORRELATION_FULL
               - MOTION_MAG_CONSISTENCY_CORRELATION_FLOOR))
        if (scale < MOTION_MAG_CONSISTENCY_SCALE_MIN
                or scale > MOTION_MAG_CONSISTENCY_SCALE_MAX):
            scale_weight = 0.0
        else:
            scale_weight = max(0.0, 1.0 - abs(scale - 1.0) / 0.5)
        return correlation, scale, correlation_weight * scale_weight

    def _bin_state(self, index: int, orientation_bin: str) -> dict:
        item = self._bins[index].setdefault(
            orientation_bin, {"cw": [], "ccw": []})
        cw, ccw = item["cw"], item["ccw"]
        cw_mean = statistics.fmean(cw) if cw else None
        ccw_mean = statistics.fmean(ccw) if ccw else None
        offset = scale = None
        residuals = []
        if cw_mean is not None:
            residuals.extend(value - cw_mean for value in cw)
        if ccw_mean is not None:
            residuals.extend(value - ccw_mean for value in ccw)
        sd = statistics.pstdev(residuals) if len(residuals) >= 2 else None
        if cw_mean is not None and ccw_mean is not None:
            offset = 0.5 * (cw_mean + ccw_mean)
            scale = 0.5 * (cw_mean - ccw_mean)
        confident = bool(
            len(cw) >= MOTION_MAG_CLOSURE_BIN_MIN_SAMPLES
            and len(ccw) >= MOTION_MAG_CLOSURE_BIN_MIN_SAMPLES
            and sd is not None and sd <= MOTION_MAG_CLOSURE_BIN_SD_LIMIT_DEG)
        cw_fraction = min(1.0, len(cw) / MOTION_MAG_CLOSURE_BIN_MIN_SAMPLES)
        ccw_fraction = min(1.0, len(ccw) / MOTION_MAG_CLOSURE_BIN_MIN_SAMPLES)
        sample_confidence = math.sqrt(cw_fraction * ccw_fraction)
        if sd is None:
            spread_confidence = 0.0
        else:
            spread_confidence = max(0.0, min(
                1.0,
                (2.0 * MOTION_MAG_CLOSURE_BIN_SD_LIMIT_DEG - sd)
                / MOTION_MAG_CLOSURE_BIN_SD_LIMIT_DEG,
            ))
        model_confidence = sample_confidence * spread_confidence
        return {
            "index": index,
            "orientation_bin": orientation_bin,
            "cw_samples": len(cw),
            "ccw_samples": len(ccw),
            "offset_deg": offset,
            "directional_component_deg": scale,
            "sd_deg": sd,
            "confidence": confident,
            "sample_confidence": sample_confidence,
            "spread_confidence": spread_confidence,
            "model_confidence": model_confidence,
        }

    def _state(self, blocked_reasons, **values) -> dict:
        state = {
            "estimator_mode": "Shadow",
            "blocked_reasons": list(blocked_reasons),
            "anchor_heading_deg": self.anchor_heading_deg,
            "unwrapped_gyro_heading_deg": self.unwrapped_gyro_heading_deg,
            "unwrapped_magnetic_heading_deg": (
                self.unwrapped_magnetic_heading_deg),
            "filtered_rate_dps": self.filtered_rate_dps,
            "filtered_innovation_deg": self.filtered_innovation_deg,
            "motion_weight": 0.0,
            "quality_weight": 0.0,
            "local_confidence": 0.0,
            "effective_confidence": 0.0,
            "motion_state": "not-initialized",
            "settling_active": False,
            "settling_authority_weight": 0.0,
            "revalidation_required": self._revalidation_required,
            "revalidation_elapsed_seconds": self._revalidation_elapsed,
            "error_debt_deg": self.filtered_innovation_deg,
            "raw_relative_closure_error_deg": self.relative_closure_error_deg,
            "filtered_relative_closure_error_deg": self.filtered_innovation_deg,
            "consumer_measurement_deg": self.filtered_innovation_deg,
            "gyro_relative_heading_deg": self.gyro_relative_heading_deg,
            "magnetic_relative_heading_deg": self.magnetic_relative_heading_deg,
            "relative_closure_error_deg": self.relative_closure_error_deg,
            "raw_magnetic_heading_delta_deg": (
                self.raw_magnetic_heading_delta_deg),
            "normalized_magnetic_heading_delta_deg": (
                self.normalized_magnetic_heading_delta_deg),
            "gyro_world_yaw_delta_deg": self.gyro_world_yaw_delta_deg,
            "gyro_reference_heading_delta_deg": (
                self.gyro_reference_heading_delta_deg),
            "heading_delta_direction_agreement": (
                self.heading_delta_direction_agreement),
            "relative_delta_innovation_deg": self.relative_delta_innovation_deg,
            "relative_delta_rejected": self.relative_delta_rejected,
            "relative_delta_reject_elapsed_seconds": (
                self._relative_delta_reject_elapsed),
            "measurement_epoch": self.measurement_epoch,
            "consistency_correlation": self.consistency_correlation,
            "consistency_scale": self.consistency_scale,
            "consistency_confidence": self.consistency_confidence,
            "rolling_consistency_confidence": (
                self.rolling_consistency_confidence),
            "aggregate_consistency_confidence": (
                self.aggregate_consistency_confidence),
            "consistency_samples": len(self._consistency_samples),
            "consistency_seconds": self._consistency_seconds,
            "window_accepted": self.window_accepted,
            "window_magnetic_delta_deg": self.window_magnetic_delta_deg,
            "window_gyro_delta_deg": self.window_gyro_delta_deg,
            "committed_heading_error_deg": self.committed_heading_error_deg,
            "residual_state": self.residual_state,
            "confidence_state": self.confidence_state,
            "authority_suspended": self.authority_suspended,
            "observed_bias_rate_dps": self.observed_bias_rate_dps,
            "bias_rate_valid": self.bias_rate_valid,
            "commit_scale_valid": self.commit_scale_valid,
            "committed_error_increment_deg": (
                self.committed_error_increment_deg),
            "invalid_confidence_debt_seconds": (
                self._confidence_invalid_elapsed),
            "frozen_correction_blocked": self.frozen_correction_blocked,
            "robust_bias_bucket_elapsed_seconds": (
                self.robust_bias_bucket_elapsed),
            "robust_bias_bucket_magnetic_delta_deg": (
                self.robust_bias_bucket_magnetic_delta_deg),
            "robust_bias_bucket_gyro_delta_deg": (
                self.robust_bias_bucket_gyro_delta_deg),
            "robust_bias_raw_observation_dps": (
                self.robust_bias_raw_observation_dps),
            "robust_bias_bounded_observation_dps": (
                self.robust_bias_bounded_observation_dps),
            "robust_bias_estimate_dps": self.robust_bias_estimate_dps,
            "robust_bias_valid_buckets": self.robust_bias_valid_buckets,
            "robust_bias_observation_age_seconds": (
                self.robust_bias_observation_age_seconds),
            "robust_bias_confidence": self.robust_bias_confidence,
            "robust_bias_observation_clipped": (
                self.robust_bias_observation_clipped),
            "robust_bias_decay_active": self.robust_bias_decay_active,
            "transient_epoch": self.transient_epoch,
            "transient_event_active": self.transient_event_active,
            "transient_event_valid": self.transient_event_valid,
            "transient_event_error_deg": self.transient_event_error_deg,
            "transient_validation_elapsed_seconds": (
                self.transient_validation_elapsed),
            "transient_validation_samples": len(
                self.transient_validation_samples),
            "transient_confidence": self.transient_confidence,
            "transient_commit_increment_deg": (
                self.transient_commit_increment_deg),
            "transient_authority_active": self.transient_authority_active,
            "absolute_cap_dps": 0.0,
            "relative_cap_dps": 0.0,
            "correction_target_dps": 0.0,
            "correction_law": self.correction_law,
            "raw_proportional_target_dps": 0.0,
            "confidence_weighted_target_dps": 0.0,
            "dynamic_safety_cap_dps": 0.0,
            "attack_slew_dps_per_second": 0.0,
            "limiting_reason": "blocked",
            "candidate_rate_dps": 0.0,
            "applied_rate_dps": 0.0,
            "output_active": False,
        }
        state.update(values)
        return state

    def _robust_bias_snapshot(self) -> dict:
        return {
            "estimate": self.robust_bias_estimate_dps,
            "valid_buckets": self.robust_bias_valid_buckets,
            "age": self.robust_bias_observation_age_seconds,
            "confidence": self.robust_bias_confidence,
            "raw": self.robust_bias_raw_observation_dps,
            "bounded": self.robust_bias_bounded_observation_dps,
            "clipped": self.robust_bias_observation_clipped,
            "decay": self.robust_bias_decay_active,
        }

    def _restore_robust_bias_snapshot(self, state: dict,
                                      dt_seconds: float = 0.0) -> None:
        self.robust_bias_estimate_dps = state["estimate"]
        self.robust_bias_valid_buckets = state["valid_buckets"]
        self.robust_bias_observation_age_seconds = state["age"]
        self.robust_bias_confidence = state["confidence"]
        self.robust_bias_raw_observation_dps = state["raw"]
        self.robust_bias_bounded_observation_dps = state["bounded"]
        self.robust_bias_observation_clipped = state["clipped"]
        self.robust_bias_decay_active = state["decay"]
        dt_value = max(0.0, float(dt_seconds))
        if math.isfinite(self.robust_bias_observation_age_seconds):
            self.robust_bias_observation_age_seconds += dt_value
            if (self.robust_bias_observation_age_seconds
                    > ROBUST_BIAS_DECAY_DELAY_SECONDS):
                self.robust_bias_decay_active = True
                decay = math.exp(-dt_value / ROBUST_BIAS_ESTIMATE_SECONDS)
                self.robust_bias_estimate_dps *= decay
                self.robust_bias_confidence *= decay

    def reanchor(self) -> None:
        """Clear heading debt without discarding the verified gyro-bias model."""
        self.suspend("reference-reanchored", preserve_bias=True)
        self._revalidation_required = False
        self._revalidation_elapsed = MOTION_MAG_CLOSURE_REVALIDATION_SECONDS
        self.confidence_state = (
            "Ready" if self.robust_bias_confidence > 0.0
            else "Collecting")

    def suspend(self, *reasons: str, preserve_bias: bool = False,
                dt_seconds: float = 0.0) -> dict:
        # Output authority is revoked in the same frame.  The low-frequency
        # estimate remains continuous so recovery cannot create a heading snap.
        preserved_robust = (
            self._robust_bias_snapshot()
            if preserve_bias and self.correction_law == "RobustBiasRate"
            else None)
        self._output_rate_dps = 0.0
        if not self._relative_anchor_required:
            self.measurement_epoch += 1
        self.filtered_innovation_deg = 0.0
        self._last_raw_innovation = None
        self.gyro_relative_heading_deg = 0.0
        self.magnetic_relative_heading_deg = 0.0
        self.relative_closure_error_deg = 0.0
        self.raw_magnetic_heading_delta_deg = 0.0
        self.normalized_magnetic_heading_delta_deg = 0.0
        self.gyro_world_yaw_delta_deg = 0.0
        self.gyro_reference_heading_delta_deg = 0.0
        self.heading_delta_direction_agreement = True
        self.relative_delta_innovation_deg = 0.0
        self.relative_delta_rejected = False
        self._relative_delta_reject_elapsed = 0.0
        self._consistency_samples.clear()
        self._raw_consistency_samples.clear()
        self._consistency_seconds = 0.0
        self._consistency_clock = 0.0
        self.consistency_correlation = 0.0
        self.consistency_scale = 0.0
        self.consistency_confidence = 0.0
        self.rolling_consistency_confidence = 0.0
        self.aggregate_consistency_confidence = 0.0
        self._validated_consistency_confidence = 0.0
        self._confidence_invalid_elapsed = 0.0
        self._authority_mismatch_elapsed = 0.0
        self._authority_confirmation_buckets = 0
        self.authority_suspended = False
        self.observed_bias_rate_dps = 0.0
        self.bias_rate_valid = False
        self.commit_scale_valid = False
        self.committed_error_increment_deg = 0.0
        self.frozen_correction_blocked = False
        self.robust_bias_bucket_elapsed = 0.0
        self.robust_bias_bucket_magnetic_delta_deg = 0.0
        self.robust_bias_bucket_gyro_delta_deg = 0.0
        self.robust_bias_raw_observation_dps = 0.0
        self.robust_bias_bounded_observation_dps = 0.0
        self.robust_bias_estimate_dps = 0.0
        self.robust_bias_valid_buckets = 0
        self.robust_bias_observation_age_seconds = float("inf")
        self.robust_bias_confidence = 0.0
        self.robust_bias_observation_clipped = False
        self.robust_bias_decay_active = False
        self.transient_epoch += 1
        self.transient_event_active = False
        self.transient_event_valid = False
        self.transient_event_error_deg = 0.0
        self.transient_validation_elapsed = 0.0
        self.transient_validation_bucket_elapsed = 0.0
        self.transient_validation_samples.clear()
        self.transient_confidence = 0.0
        self.transient_commit_increment_deg = 0.0
        self.transient_authority_active = False
        self._transient_was_dynamic = False
        self.window_accepted = False
        self.window_magnetic_delta_deg = 0.0
        self.window_gyro_delta_deg = 0.0
        self.committed_heading_error_deg = 0.0
        self.residual_state = "Reset"
        self._consistency_reference_ready = False
        self._was_stationary = True
        self._consistency_bucket_elapsed = 0.0
        self._consistency_bucket_mag_delta = 0.0
        self._consistency_bucket_gyro_delta = 0.0
        self.confidence_state = "Waiting for Motion"
        self._relative_last_magnetic_heading = None
        self._relative_last_gyro_heading = None
        self._relative_anchor_required = True
        self._revalidation_required = True
        self._revalidation_elapsed = 0.0
        self._bin_elapsed = 0.0
        self._bin_pause_elapsed = 0.0
        self._bin_sample_key = None
        if preserved_robust is not None:
            self._restore_robust_bias_snapshot(
                preserved_robust, dt_seconds)
        suspended_reasons = list(reasons or ("suspended",))
        self._last_state = self._state(
            suspended_reasons,
            motion_state="suspended",
            previous_motion_state=self._last_valid_motion_state,
            suspended_reason=suspended_reasons,
            stop_zero_enforced=True,
        )
        return dict(self._last_state)

    def update(self, current_heading_deg: float, magnetic_heading_deg: float,
               yaw_rate_dps: float, accelerometer_g: Sequence[float],
               timing: V2Timing, *, magnetic_quality_valid: bool,
               magnetic_direction_valid: bool = True,
               stationary_hint: bool = False,
               gyro_world_yaw_rate_dps: float | None = None,
               gyro_reference_heading_deg: float | None = None) -> dict:
        current = float(current_heading_deg)
        magnetic = float(magnetic_heading_deg)
        heading_rate = float(yaw_rate_dps)
        world_yaw_rate = (
            heading_rate if gyro_world_yaw_rate_dps is None
            else float(gyro_world_yaw_rate_dps))
        if not math.isfinite(world_yaw_rate):
            world_yaw_rate = 0.0
        gyro_reference = (
            current if gyro_reference_heading_deg is None
            else float(gyro_reference_heading_deg))
        if not math.isfinite(gyro_reference):
            gyro_reference = current
        relative_rate_law = self.correction_law in (
            "DynamicRate", "RobustBiasRate")
        rate = world_yaw_rate if relative_rate_law else heading_rate
        accel = _vector3(accelerometer_g, "accelerometer_g")
        if self.anchor_heading_deg is None:
            self.anchor_heading_deg = current
            self.unwrapped_gyro_heading_deg = current
            self.unwrapped_magnetic_heading_deg = magnetic
        if self._last_current_heading is not None:
            self.unwrapped_gyro_heading_deg += _wrap_degrees(
                current - self._last_current_heading)
        if self._last_magnetic_heading is not None:
            self.unwrapped_magnetic_heading_deg += _wrap_degrees(
                magnetic - self._last_magnetic_heading)
        self._last_current_heading = current
        self._last_magnetic_heading = magnetic
        blocked = []
        if timing.status != "valid" or not timing.integrate:
            blocked.append("timing")
        if not magnetic_quality_valid:
            blocked.append("magnetic-quality")
        if not magnetic_direction_valid:
            blocked.append("magnetic-direction")
            self._revalidation_required = True
            self._revalidation_elapsed = 0.0
            self.filtered_innovation_deg = 0.0
            self._last_raw_innovation = None
        preserved_robust = (
            self._robust_bias_snapshot()
            if (MAG_REFERENCE_MODEL == "Adaptive"
                and self.correction_law == "RobustBiasRate"
                and (not magnetic_quality_valid
                     or not magnetic_direction_valid))
            else None)
        if not magnetic_quality_valid or not magnetic_direction_valid:
            if not self._relative_anchor_required:
                self.measurement_epoch += 1
            self.gyro_relative_heading_deg = 0.0
            self.magnetic_relative_heading_deg = 0.0
            self.relative_closure_error_deg = 0.0
            self.raw_magnetic_heading_delta_deg = 0.0
            self.normalized_magnetic_heading_delta_deg = 0.0
            self.gyro_world_yaw_delta_deg = 0.0
            self.gyro_reference_heading_delta_deg = 0.0
            self.heading_delta_direction_agreement = True
            self.relative_delta_innovation_deg = 0.0
            self.relative_delta_rejected = False
            self._relative_delta_reject_elapsed = 0.0
            self._relative_last_magnetic_heading = None
            self._relative_last_gyro_heading = None
            self._relative_anchor_required = True
            self._consistency_samples.clear()
            self._raw_consistency_samples.clear()
            self._consistency_seconds = 0.0
            self._consistency_clock = 0.0
            self.consistency_correlation = 0.0
            self.consistency_scale = 0.0
            self.consistency_confidence = 0.0
            self.rolling_consistency_confidence = 0.0
            self.aggregate_consistency_confidence = 0.0
            self._validated_consistency_confidence = 0.0
            self._confidence_invalid_elapsed = 0.0
            self._authority_mismatch_elapsed = 0.0
            self._authority_confirmation_buckets = 0
            self.authority_suspended = False
            self.observed_bias_rate_dps = 0.0
            self.bias_rate_valid = False
            self.commit_scale_valid = False
            self.committed_error_increment_deg = 0.0
            self.frozen_correction_blocked = False
            self.robust_bias_bucket_elapsed = 0.0
            self.robust_bias_bucket_magnetic_delta_deg = 0.0
            self.robust_bias_bucket_gyro_delta_deg = 0.0
            self.robust_bias_raw_observation_dps = 0.0
            self.robust_bias_bounded_observation_dps = 0.0
            self.robust_bias_estimate_dps = 0.0
            self.robust_bias_valid_buckets = 0
            self.robust_bias_observation_age_seconds = float("inf")
            self.robust_bias_confidence = 0.0
            self.robust_bias_observation_clipped = False
            self.robust_bias_decay_active = False
            self.transient_epoch += 1
            self.transient_event_active = False
            self.transient_event_valid = False
            self.transient_event_error_deg = 0.0
            self.transient_validation_elapsed = 0.0
            self.transient_validation_bucket_elapsed = 0.0
            self.transient_validation_samples.clear()
            self.transient_confidence = 0.0
            self.transient_commit_increment_deg = 0.0
            self.transient_authority_active = False
            self._transient_was_dynamic = False
            self.window_accepted = False
            self.window_magnetic_delta_deg = 0.0
            self.window_gyro_delta_deg = 0.0
            self.committed_heading_error_deg = 0.0
            self.residual_state = "Reset"
            self._consistency_reference_ready = False
            self._consistency_bucket_elapsed = 0.0
            self._consistency_bucket_mag_delta = 0.0
            self._consistency_bucket_gyro_delta = 0.0
            self.confidence_state = "Magnetic Interference"
            if preserved_robust is not None:
                self._restore_robust_bias_snapshot(
                    preserved_robust, timing.dt_seconds)
        accel_error = abs(math.sqrt(sum(value * value for value in accel)) - 1.0)
        if accel_error > 0.03:
            blocked.append("linear-acceleration")
        raw_innovation = _wrap_degrees(magnetic - current)
        if (self._last_raw_innovation is not None
                and abs(_wrap_degrees(
                    raw_innovation - self._last_raw_innovation)) > 20.0):
            blocked.append("innovation-jump")
        self._last_raw_innovation = raw_innovation
        revalidation_reason = None
        self.committed_error_increment_deg = 0.0
        self.transient_commit_increment_deg = 0.0
        self.frozen_correction_blocked = False
        if self._revalidation_required:
            if not magnetic_direction_valid:
                self._revalidation_elapsed = 0.0
                revalidation_reason = "closure-revalidation-direction"
            elif (abs(raw_innovation)
                  > MOTION_MAG_CLOSURE_REVALIDATION_MAX_INNOVATION_DEG):
                self._revalidation_elapsed = 0.0
                revalidation_reason = "closure-revalidation-innovation"
            elif not blocked:
                self._revalidation_elapsed += max(0.0, timing.dt_seconds)
                revalidation_reason = "closure-revalidation"
                if (self._revalidation_elapsed
                        >= MOTION_MAG_CLOSURE_REVALIDATION_SECONDS):
                    self._revalidation_required = False
                    self._revalidation_elapsed = (
                        MOTION_MAG_CLOSURE_REVALIDATION_SECONDS)
                    revalidation_reason = None
                    self.filtered_innovation_deg = (
                        0.0 if relative_rate_law
                        else raw_innovation)
                    self._settling_elapsed = 0.0
            if revalidation_reason is not None:
                blocked.append(revalidation_reason)
        if relative_rate_law:
            window_committed_this_frame = False
            innovation_update_seconds = 0.0
            if self._relative_anchor_required:
                if not blocked and not self._revalidation_required:
                    self.gyro_relative_heading_deg = 0.0
                    self.magnetic_relative_heading_deg = 0.0
                    self.relative_closure_error_deg = 0.0
                    self.raw_magnetic_heading_delta_deg = 0.0
                    self.normalized_magnetic_heading_delta_deg = 0.0
                    self.gyro_world_yaw_delta_deg = 0.0
                    self.gyro_reference_heading_delta_deg = 0.0
                    self.heading_delta_direction_agreement = True
                    self.relative_delta_innovation_deg = 0.0
                    self.relative_delta_rejected = False
                    self._relative_delta_reject_elapsed = 0.0
                    self._relative_last_magnetic_heading = magnetic
                    self._relative_last_gyro_heading = gyro_reference
                    self._relative_anchor_required = False
                    self.residual_state = "Reanchoring"
                rate = 0.0
                controller_innovation = 0.0
            else:
                if self._relative_last_magnetic_heading is not None:
                    self.raw_magnetic_heading_delta_deg = _wrap_degrees(
                        magnetic - self._relative_last_magnetic_heading)
                    # Magnetic heading and the independent 6-axis quaternion
                    # heading share the same NWU Euler-heading convention.
                    self.normalized_magnetic_heading_delta_deg = (
                        self.raw_magnetic_heading_delta_deg)
                self._relative_last_magnetic_heading = magnetic
                if timing.integrate and timing.dt_seconds > 0.0:
                    self.gyro_world_yaw_delta_deg = (
                        world_yaw_rate * timing.dt_seconds)
                    if self._relative_last_gyro_heading is not None:
                        self.gyro_reference_heading_delta_deg = _wrap_degrees(
                            gyro_reference - self._relative_last_gyro_heading)
                    else:
                        self.gyro_reference_heading_delta_deg = 0.0
                    self._relative_last_gyro_heading = gyro_reference
                    rate = (
                        self.gyro_reference_heading_delta_deg
                        / timing.dt_seconds)
                mag_delta = self.normalized_magnetic_heading_delta_deg
                gyro_delta = self.gyro_reference_heading_delta_deg
                self.heading_delta_direction_agreement = bool(
                    abs(mag_delta) <= 1e-9 or abs(gyro_delta) <= 1e-9
                    or mag_delta * gyro_delta > 0.0)
                self.relative_delta_innovation_deg = _wrap_degrees(
                    mag_delta - gyro_delta)
                moving = abs(rate) > MOTION_MAG_CLOSURE_MOTION_EXIT_DPS
                self.relative_delta_rejected = bool(
                    moving and abs(self.relative_delta_innovation_deg)
                    > MOTION_MAG_RELATIVE_DELTA_REJECTION_DEG)
                if self.relative_delta_rejected:
                    self._relative_delta_reject_elapsed += max(
                        0.0, timing.dt_seconds)
                    if (self._relative_delta_reject_elapsed
                            >= MOTION_MAG_RELATIVE_DELTA_REJECTION_SECONDS):
                        blocked.append("relative-heading-delta")
                else:
                    self._relative_delta_reject_elapsed = 0.0
                self._consistency_clock += max(0.0, timing.dt_seconds)
                bucket_completed = False
                current_consistency_evidence = 0.0
                sample_valid = bool(
                    moving and not self.relative_delta_rejected
                    and not blocked)
                if self.correction_law == "RobustBiasRate":
                    dt_value = max(0.0, timing.dt_seconds)
                    if sample_valid:
                        self.robust_bias_observation_age_seconds = 0.0
                        self.robust_bias_decay_active = False
                        self.robust_bias_bucket_elapsed += dt_value
                        self.robust_bias_bucket_magnetic_delta_deg += mag_delta
                        self.robust_bias_bucket_gyro_delta_deg += gyro_delta
                        if (self.robust_bias_bucket_elapsed
                                >= ROBUST_BIAS_BUCKET_SECONDS):
                            bucket_seconds = self.robust_bias_bucket_elapsed
                            self.robust_bias_raw_observation_dps = (
                                (self.robust_bias_bucket_magnetic_delta_deg
                                 - self.robust_bias_bucket_gyro_delta_deg)
                                / bucket_seconds)
                            self.robust_bias_bounded_observation_dps = max(
                                -ROBUST_BIAS_OBSERVATION_CAP_DPS,
                                min(ROBUST_BIAS_OBSERVATION_CAP_DPS,
                                    self.robust_bias_raw_observation_dps))
                            self.robust_bias_observation_clipped = bool(
                                abs(self.robust_bias_raw_observation_dps)
                                > ROBUST_BIAS_OBSERVATION_CAP_DPS)
                            robust_alpha = 1.0 - math.exp(
                                -bucket_seconds
                                / ROBUST_BIAS_ESTIMATE_SECONDS)
                            self.robust_bias_estimate_dps += robust_alpha * (
                                self.robust_bias_bounded_observation_dps
                                - self.robust_bias_estimate_dps)
                            self.robust_bias_valid_buckets += 1
                            self.robust_bias_bucket_elapsed = 0.0
                            self.robust_bias_bucket_magnetic_delta_deg = 0.0
                            self.robust_bias_bucket_gyro_delta_deg = 0.0
                    else:
                        if math.isfinite(
                                self.robust_bias_observation_age_seconds):
                            self.robust_bias_observation_age_seconds += dt_value
                        if (self.robust_bias_observation_age_seconds
                                > ROBUST_BIAS_DECAY_DELAY_SECONDS):
                            self.robust_bias_decay_active = True
                            decay = math.exp(
                                -dt_value / ROBUST_BIAS_ESTIMATE_SECONDS)
                            self.robust_bias_estimate_dps *= decay
                    sample_progress = self._smoothstep(
                        (self.robust_bias_valid_buckets
                         - ROBUST_BIAS_MIN_BUCKETS)
                        / (ROBUST_BIAS_FULL_BUCKETS
                           - ROBUST_BIAS_MIN_BUCKETS))
                    if math.isfinite(
                            self.robust_bias_observation_age_seconds):
                        age_weight = math.exp(
                            -max(0.0,
                                 self.robust_bias_observation_age_seconds
                                 - ROBUST_BIAS_DECAY_DELAY_SECONDS)
                            / ROBUST_BIAS_ESTIMATE_SECONDS)
                    else:
                        age_weight = 0.0
                    self.robust_bias_confidence = max(
                        0.0, min(1.0, sample_progress * age_weight))
                if (sample_valid and abs(gyro_delta)
                        >= MOTION_MAG_CONSISTENCY_MIN_RAW_GYRO_DELTA_DEG):
                    self._raw_consistency_samples.append(
                        (self._consistency_clock, mag_delta, gyro_delta))
                while (self._raw_consistency_samples
                       and self._consistency_clock
                       - self._raw_consistency_samples[0][0]
                       > MOTION_MAG_CONSISTENCY_WINDOW_SECONDS):
                    self._raw_consistency_samples.popleft()
                raw_seconds = (
                    self._consistency_clock
                    - self._raw_consistency_samples[0][0]
                    if self._raw_consistency_samples else 0.0)
                if (len(self._raw_consistency_samples)
                        >= MOTION_MAG_CONSISTENCY_MIN_RAW_SAMPLES
                        and raw_seconds
                        >= MOTION_MAG_CONSISTENCY_MIN_SECONDS):
                    raw_mag_values = [
                        item[1] for item in self._raw_consistency_samples]
                    raw_gyro_values = [
                        item[2] for item in self._raw_consistency_samples]
                    (rolling_correlation, rolling_scale,
                     self.rolling_consistency_confidence) = (
                        self._correlation_confidence(
                            raw_mag_values, raw_gyro_values))
                else:
                    rolling_correlation = 0.0
                    rolling_scale = 0.0
                    self.rolling_consistency_confidence = 0.0
                if self.rolling_consistency_confidence > 0.0:
                    current_consistency_evidence = (
                        self.rolling_consistency_confidence)
                    self._validated_consistency_confidence = max(
                        self._validated_consistency_confidence,
                        current_consistency_evidence)
                    self.consistency_confidence = (
                        self._validated_consistency_confidence)
                    if (self.rolling_consistency_confidence
                            >= self.aggregate_consistency_confidence):
                        self.consistency_correlation = rolling_correlation
                        self.consistency_scale = rolling_scale
                if sample_valid:
                    self._consistency_bucket_elapsed += max(
                        0.0, timing.dt_seconds)
                    self._consistency_bucket_mag_delta += mag_delta
                    self._consistency_bucket_gyro_delta += gyro_delta
                    if (self._consistency_bucket_elapsed
                            >= MOTION_MAG_CONSISTENCY_BUCKET_SECONDS):
                        self._consistency_samples.append((
                            self._consistency_clock,
                            self._consistency_bucket_mag_delta,
                            self._consistency_bucket_gyro_delta,
                        ))
                        self._consistency_seconds += (
                            self._consistency_bucket_elapsed)
                        self._consistency_bucket_elapsed = 0.0
                        self._consistency_bucket_mag_delta = 0.0
                        self._consistency_bucket_gyro_delta = 0.0
                        bucket_completed = True
                        if (self._consistency_reference_ready
                                and self._consistency_samples):
                            confirm_mag = self._consistency_samples[-1][1]
                            confirm_gyro = self._consistency_samples[-1][2]
                            confirm_scale = (
                                confirm_mag / confirm_gyro
                                if abs(confirm_gyro) > 1e-9 else 0.0)
                            confirm_bias_rate = (
                                (confirm_mag - confirm_gyro)
                                / MOTION_MAG_CONSISTENCY_BUCKET_SECONDS)
                            confirmation_compatible = bool(
                                confirm_mag * confirm_gyro > 0.0
                                and abs(confirm_gyro) >= 0.02
                                    and MOTION_MAG_COMMIT_SCALE_MIN
                                    <= confirm_scale
                                    <= MOTION_MAG_COMMIT_SCALE_MAX
                                and abs(confirm_bias_rate)
                                    <= MOTION_MAG_MAX_BIAS_RATE_DPS)
                            if (confirmation_compatible
                                    and (not self.window_accepted
                                         or self.authority_suspended)):
                                self._authority_confirmation_buckets += 1
                                if (self._authority_confirmation_buckets
                                        >= MOTION_MAG_AUTHORITY_CONFIRM_BUCKETS):
                                    self.window_accepted = True
                                    self.authority_suspended = False
                                    self._authority_mismatch_elapsed = 0.0
                                    self._authority_confirmation_buckets = 0
                                    self.residual_state = "Tracking"
                                    self.confidence_state = "Ready"
                            elif not confirmation_compatible:
                                self._authority_confirmation_buckets = 0
                elif moving:
                    # Reject only this sample.  Preserving the unfinished
                    # bucket avoids making confidence impossible when a
                    # low-rate magnetometer delivers an occasional jump.
                    self.confidence_state = "Collecting"
                if (len(self._consistency_samples)
                        >= MOTION_MAG_CONSISTENCY_MIN_SAMPLES
                        and self._consistency_seconds
                        >= MOTION_MAG_CONSISTENCY_MIN_SECONDS):
                    mag_values = [item[1] for item in self._consistency_samples]
                    gyro_values = [item[2] for item in self._consistency_samples]
                    net_mag = sum(mag_values)
                    net_gyro = sum(gyro_values)
                    aggregate_scale = (
                        net_mag / net_gyro
                        if abs(net_gyro) > 1e-9 else 0.0)
                    if (aggregate_scale
                            < MOTION_MAG_CONSISTENCY_SCALE_MIN
                            or aggregate_scale
                            > MOTION_MAG_CONSISTENCY_SCALE_MAX):
                        scale_weight = 0.0
                    else:
                        scale_weight = max(
                            0.0, 1.0
                            - abs(aggregate_scale - 1.0) / 0.5)
                    residual_rms = math.sqrt(statistics.fmean(
                        (mag_value
                         - aggregate_scale * gyro_value) ** 2
                        for mag_value, gyro_value
                        in zip(mag_values, gyro_values)))
                    mean_signal = max(
                        0.01,
                        statistics.fmean(abs(value)
                                         for value in mag_values))
                    noise_weight = self._smoothstep(
                        (0.75 - residual_rms / mean_signal) / 0.50)
                    net_motion_weight = self._smoothstep(
                        (abs(net_gyro)
                         - MOTION_MAG_CONSISTENCY_MIN_NET_GYRO_DEG)
                        / (MOTION_MAG_CONSISTENCY_FULL_NET_GYRO_DEG
                           - MOTION_MAG_CONSISTENCY_MIN_NET_GYRO_DEG))
                    direction_weight = (
                        1.0 if net_mag * net_gyro > 0.0 else 0.0)
                    self.observed_bias_rate_dps = (
                        (net_mag - net_gyro) / self._consistency_seconds
                        if self._consistency_seconds > 1e-9 else 0.0)
                    self.bias_rate_valid = bool(
                        abs(self.observed_bias_rate_dps)
                        <= MOTION_MAG_MAX_BIAS_RATE_DPS)
                    self.commit_scale_valid = bool(
                        MOTION_MAG_COMMIT_SCALE_MIN <= aggregate_scale
                        <= MOTION_MAG_COMMIT_SCALE_MAX)
                    aggregate_weight = (
                        net_motion_weight * direction_weight * noise_weight)
                    self.aggregate_consistency_confidence = (
                        aggregate_weight * scale_weight)
                    current_consistency_evidence = max(
                        self.rolling_consistency_confidence,
                        self.aggregate_consistency_confidence)
                    self._validated_consistency_confidence = max(
                        self._validated_consistency_confidence,
                        current_consistency_evidence)
                    self.consistency_confidence = (
                        self._validated_consistency_confidence)
                    if (self.rolling_consistency_confidence
                            >= self.aggregate_consistency_confidence):
                        self.consistency_correlation = rolling_correlation
                        self.consistency_scale = rolling_scale
                    else:
                        self.consistency_correlation = 0.0
                        self.consistency_scale = aggregate_scale
                    if abs(net_gyro) < MOTION_MAG_CONSISTENCY_MIN_NET_GYRO_DEG:
                        self.confidence_state = "Low Net Rotation"
                    elif direction_weight == 0.0:
                        self.confidence_state = "Direction Mismatch"
                        self.authority_suspended = True
                        self._authority_confirmation_buckets = 0
                    elif scale_weight == 0.0:
                        self.confidence_state = "Scale Mismatch"
                        self._authority_mismatch_elapsed += max(
                            0.0, timing.dt_seconds)
                        if (self._authority_mismatch_elapsed
                                >= MOTION_MAG_AUTHORITY_MISMATCH_SECONDS):
                            self.authority_suspended = True
                            self._authority_confirmation_buckets = 0
                    elif noise_weight == 0.0:
                        self.confidence_state = "Magnetic Noise"
                        self._authority_mismatch_elapsed += max(
                            0.0, timing.dt_seconds)
                        if (self._authority_mismatch_elapsed
                                >= MOTION_MAG_AUTHORITY_MISMATCH_SECONDS):
                            self.authority_suspended = True
                            self._authority_confirmation_buckets = 0
                    elif current_consistency_evidence > 0.0:
                        self.confidence_state = "Ready"
                        self._authority_mismatch_elapsed = 0.0
                    else:
                        self.confidence_state = "Collecting"
                else:
                    if not self._consistency_reference_ready:
                        self.consistency_correlation = 0.0
                        self.consistency_scale = 0.0
                        self.consistency_confidence = 0.0
                    if self.window_accepted:
                        self.confidence_state = "Ready"
                    elif moving:
                        self.confidence_state = "Collecting"
                    else:
                        self.confidence_state = "Waiting for Motion"
                window_complete = bool(
                    len(self._consistency_samples)
                    >= MOTION_MAG_CONSISTENCY_MIN_SAMPLES
                    and self._consistency_seconds
                    >= MOTION_MAG_CONSISTENCY_MIN_SECONDS)
                window_expired = bool(
                    self._consistency_seconds
                    >= MOTION_MAG_CONSISTENCY_WINDOW_SECONDS)
                window_trusted = bool(
                    window_complete and current_consistency_evidence > 0.0
                    and not blocked and moving
                    and not self.relative_delta_rejected)
                commit_allowed = bool(
                    window_trusted and self.bias_rate_valid
                    and self.commit_scale_valid)
                if window_trusted:
                    self._confidence_invalid_elapsed = max(
                        0.0,
                        self._confidence_invalid_elapsed
                        - self._consistency_seconds)
                    self._authority_mismatch_elapsed = 0.0
                    self.window_magnetic_delta_deg = sum(
                        item[1] for item in self._consistency_samples)
                    self.window_gyro_delta_deg = sum(
                        item[2] for item in self._consistency_samples)
                    if self._consistency_reference_ready:
                        if commit_allowed:
                            innovation_update_seconds = (
                                self._consistency_seconds)
                            self.magnetic_relative_heading_deg += (
                                self.window_magnetic_delta_deg)
                            self.gyro_relative_heading_deg += (
                                self.window_gyro_delta_deg)
                            self.committed_error_increment_deg = (
                                self.window_magnetic_delta_deg
                                - self.window_gyro_delta_deg)
                            self.window_accepted = True
                            self.authority_suspended = False
                            self._authority_confirmation_buckets = 0
                            window_committed_this_frame = True
                            self.residual_state = "Tracking"
                        else:
                            self.authority_suspended = True
                            self._authority_confirmation_buckets = 0
                            self.residual_state = "Frozen"
                            self.confidence_state = (
                                "Bias Rate Rejected"
                                if not self.bias_rate_valid
                                else "Commit Scale Rejected")
                    else:
                        # A recovered window establishes a fresh reference;
                        # it is never repaid as correction debt.
                        self._consistency_reference_ready = True
                        self.residual_state = "Reanchoring"
                    self._consistency_samples.clear()
                    self._consistency_seconds = 0.0
                elif window_expired:
                    invalid_seconds = max(
                        self._consistency_seconds,
                        MOTION_MAG_CONSISTENCY_WINDOW_SECONDS)
                    self._confidence_invalid_elapsed += invalid_seconds
                    self._validated_consistency_confidence *= math.exp(
                        -invalid_seconds
                        / MOTION_MAG_CONFIDENCE_RELEASE_SECONDS)
                    self.consistency_confidence = (
                        self._validated_consistency_confidence)
                    hard_confidence_reset = bool(
                        self._confidence_invalid_elapsed
                        >= MOTION_MAG_CONFIDENCE_INVALID_RESET_SECONDS)
                    # One uncertain window only decays previously validated
                    # authority.  Repeated inconsistency resets the reference
                    # so stale debt can never persist indefinitely.
                    if (self._consistency_reference_ready
                            and hard_confidence_reset):
                        self.measurement_epoch += 1
                        self.gyro_relative_heading_deg = 0.0
                        self.magnetic_relative_heading_deg = 0.0
                        self.relative_closure_error_deg = 0.0
                        self.filtered_innovation_deg = 0.0
                        self.committed_heading_error_deg = 0.0
                    if hard_confidence_reset:
                        self._consistency_reference_ready = False
                        self.window_accepted = False
                        self._validated_consistency_confidence = 0.0
                        self.consistency_confidence = 0.0
                        self._confidence_invalid_elapsed = 0.0
                        self._authority_mismatch_elapsed = 0.0
                        self._authority_confirmation_buckets = 0
                        self.authority_suspended = False
                        self.residual_state = "Reset"
                    else:
                        self.residual_state = "Frozen"
                        self.confidence_state = "Collecting"
                    self._consistency_samples.clear()
                    self._consistency_seconds = 0.0
                elif not moving:
                    self.residual_state = "Frozen"
                    self.confidence_state = "Waiting for Motion"
                self.relative_closure_error_deg = _wrap_degrees(
                    self.magnetic_relative_heading_deg
                    - self.gyro_relative_heading_deg)
                self.committed_heading_error_deg = (
                    self.relative_closure_error_deg)
                controller_innovation = self.relative_closure_error_deg
        else:
            window_committed_this_frame = True
            innovation_update_seconds = max(0.0, timing.dt_seconds)
            controller_innovation = raw_innovation
            self.relative_closure_error_deg = raw_innovation

        raw_stationary = bool(
            abs(rate) <= MOTION_MAG_CLOSURE_MOTION_EXIT_DPS)
        if relative_rate_law:
            if raw_stationary and not self._was_stationary:
                self._consistency_samples.clear()
                self._raw_consistency_samples.clear()
                self._consistency_seconds = 0.0
                self._consistency_bucket_elapsed = 0.0
                self._consistency_bucket_mag_delta = 0.0
                self._consistency_bucket_gyro_delta = 0.0
                self.residual_state = "Frozen"
                self.confidence_state = "Waiting for Motion"
            self._was_stationary = raw_stationary
        innovation_tracking_allowed = bool(
            not relative_rate_law
            or (window_committed_this_frame and not raw_stationary))
        if timing.dt_seconds > 0.0:
            rate_alpha = 1.0 - math.exp(
                -timing.dt_seconds / MOTION_MAG_CLOSURE_RATE_LOWPASS_SECONDS)
            self.filtered_rate_dps += rate_alpha * (
                rate - self.filtered_rate_dps)
            if magnetic_direction_valid and innovation_tracking_allowed:
                innovation_alpha = 1.0 - math.exp(
                    -innovation_update_seconds
                    / MOTION_MAG_CLOSURE_INNOVATION_LOWPASS_SECONDS)
                innovation = controller_innovation
                innovation_delta = _wrap_degrees(
                    innovation - self.filtered_innovation_deg)
                self.filtered_innovation_deg = _wrap_degrees(
                    self.filtered_innovation_deg
                    + innovation_alpha * innovation_delta)
        speed = abs(self.filtered_rate_dps)
        if self._last_rate_dps is None or timing.dt_seconds <= 0.0:
            raw_angular_acceleration_dps2 = 0.0
        else:
            raw_angular_acceleration_dps2 = abs(
                rate - self._last_rate_dps) / timing.dt_seconds
        self._last_rate_dps = rate
        if timing.dt_seconds > 0.0:
            dynamic_alpha = 1.0 - math.exp(
                -timing.dt_seconds
                / MOTION_MAG_CLOSURE_DYNAMIC_ACCEL_LOWPASS_SECONDS)
            self.filtered_angular_acceleration_dps2 += dynamic_alpha * (
                raw_angular_acceleration_dps2
                - self.filtered_angular_acceleration_dps2)
        filtered_angular_acceleration_dps2 = (
            self.filtered_angular_acceleration_dps2)
        dynamic_weight = 1.0 - self._smoothstep(
            (filtered_angular_acceleration_dps2
             - MOTION_MAG_CLOSURE_DYNAMIC_ACCEL_EXIT_DPS2)
            / (MOTION_MAG_CLOSURE_DYNAMIC_ACCEL_ENTER_DPS2
               - MOTION_MAG_CLOSURE_DYNAMIC_ACCEL_EXIT_DPS2))
        if accel_error <= 0.015:
            accel_quality_weight = 1.0
        else:
            accel_quality_weight = self._smoothstep(
                (0.03 - accel_error) / 0.015)
        # Runtime bias learning deliberately treats very smooth sub-3 dps
        # motion conservatively.  Closure must not copy that classification or
        # it would suppress the exact slow-motion repayment window.  The hint
        # is diagnostic only above this estimator's hard stop guard.
        if (not raw_stationary
                and filtered_angular_acceleration_dps2
                >= MOTION_MAG_CLOSURE_DYNAMIC_ACCEL_ENTER_DPS2):
            self._dynamic_enter_elapsed = min(
                MOTION_MAG_CLOSURE_DYNAMIC_ENTER_SECONDS,
                self._dynamic_enter_elapsed + max(0.0, timing.dt_seconds),
            )
        elif not self._dynamic_active:
            self._dynamic_enter_elapsed = 0.0
        if (not self._dynamic_active
                and self._dynamic_enter_elapsed
                >= MOTION_MAG_CLOSURE_DYNAMIC_ENTER_SECONDS):
            self._dynamic_active = True
        if (self._dynamic_active
                and filtered_angular_acceleration_dps2
                <= MOTION_MAG_CLOSURE_DYNAMIC_ACCEL_EXIT_DPS2):
            self._dynamic_active = False
            self._dynamic_enter_elapsed = 0.0
        hard_dynamic = accel_quality_weight <= 0.0
        dynamic = bool(self._dynamic_active or hard_dynamic)
        if raw_stationary:
            motion_state = "stationary"
            self._settling_elapsed = 0.0
        elif dynamic:
            motion_state = "dynamic"
            self._settling_elapsed = 0.0
        else:
            self._settling_elapsed = min(
                MOTION_MAG_CLOSURE_SETTLING_SECONDS,
                self._settling_elapsed + max(0.0, timing.dt_seconds),
            )
            motion_state = (
                "low-motion-stable"
                if speed <= MOTION_MAG_CLOSURE_MOTION_ENTER_DPS
                else "stable-motion")
        settling_active = bool(
            not raw_stationary and not dynamic
            and self._settling_elapsed < MOTION_MAG_CLOSURE_SETTLING_SECONDS)
        if raw_stationary or dynamic:
            settling_authority_weight = 0.0
        elif self._settling_elapsed <= MOTION_MAG_CLOSURE_SETTLING_ZERO_SECONDS:
            settling_authority_weight = 0.0
        elif self._settling_elapsed >= MOTION_MAG_CLOSURE_SETTLING_SECONDS:
            settling_authority_weight = 1.0
        else:
            settling_authority_weight = self._smoothstep(
                (self._settling_elapsed
                 - MOTION_MAG_CLOSURE_SETTLING_ZERO_SECONDS)
                / (MOTION_MAG_CLOSURE_SETTLING_SECONDS
                   - MOTION_MAG_CLOSURE_SETTLING_ZERO_SECONDS))
        self._last_valid_motion_state = motion_state
        motion_weight = self._smoothstep(
            (speed - MOTION_MAG_CLOSURE_MOTION_EXIT_DPS)
            / (MOTION_MAG_CLOSURE_LOW_MOTION_FULL_DPS
               - MOTION_MAG_CLOSURE_MOTION_EXIT_DPS))
        # The low-pass is useful while moving but must never create a release
        # tail after the physical gyro rate has stopped.
        if raw_stationary:
            motion_weight = 0.0
        if self.correction_law == "RobustBiasRate":
            dt_value = max(0.0, timing.dt_seconds)
            hard_transient_invalid = bool(
                timing.status != "valid" or not timing.integrate
                or not magnetic_quality_valid
                or not magnetic_direction_valid
                or "innovation-jump" in blocked
                or self.relative_delta_rejected)
            if dynamic and not self._transient_was_dynamic:
                # A new rapid-motion event invalidates any unconsumed old
                # event.  Each output consumer observes this epoch change and
                # clears its own transient debt independently.
                self.transient_epoch += 1
                self.transient_event_active = True
                self.transient_event_valid = not hard_transient_invalid
                self.transient_event_error_deg = 0.0
                self.transient_validation_elapsed = 0.0
                self.transient_validation_bucket_elapsed = 0.0
                self.transient_validation_samples.clear()
                self.transient_confidence = 0.0
                self.transient_authority_active = False
            if dynamic and self.transient_event_active:
                if hard_transient_invalid:
                    self.transient_event_valid = False
                elif self.transient_event_valid:
                    self.transient_event_error_deg = max(
                        -TRANSIENT_RECOVERY_MAX_DEBT_DEG,
                        min(TRANSIENT_RECOVERY_MAX_DEBT_DEG,
                            self.transient_event_error_deg
                            + self.relative_delta_innovation_deg))
                self.transient_validation_elapsed = 0.0
                self.transient_validation_bucket_elapsed = 0.0
                self.transient_validation_samples.clear()
            elif self.transient_event_active:
                validation_valid = bool(
                    self.transient_event_valid
                    and not hard_transient_invalid
                    and not raw_stationary
                    and settling_authority_weight > 0.0)
                if validation_valid:
                    self.transient_event_error_deg = max(
                        -TRANSIENT_RECOVERY_MAX_DEBT_DEG,
                        min(TRANSIENT_RECOVERY_MAX_DEBT_DEG,
                            self.transient_event_error_deg
                            + self.relative_delta_innovation_deg))
                    self.transient_validation_elapsed += dt_value
                    self.transient_validation_bucket_elapsed += dt_value
                    if (self.transient_validation_bucket_elapsed
                            >= TRANSIENT_RECOVERY_SAMPLE_SECONDS):
                        self.transient_validation_samples.append(
                            self.transient_event_error_deg)
                        self.transient_validation_bucket_elapsed = 0.0
                    if (self.transient_validation_elapsed
                            >= TRANSIENT_RECOVERY_VALIDATION_SECONDS
                            and len(self.transient_validation_samples)
                            >= TRANSIENT_RECOVERY_MIN_SAMPLES):
                        samples = list(self.transient_validation_samples)
                        stable = bool(
                            max(samples) - min(samples)
                            <= TRANSIENT_RECOVERY_STABILITY_DEG)
                        final_error = statistics.median(samples[-3:])
                        sign_consistent = all(
                            abs(value) < TRANSIENT_RECOVERY_MIN_ERROR_DEG
                            or value * final_error > 0.0
                            for value in samples)
                        if stable and sign_consistent:
                            committed = max(
                                -TRANSIENT_RECOVERY_MAX_DEBT_DEG,
                                min(TRANSIENT_RECOVERY_MAX_DEBT_DEG,
                                    final_error))
                            if (abs(committed)
                                    >= TRANSIENT_RECOVERY_MIN_ERROR_DEG):
                                self.transient_commit_increment_deg = committed
                                stability_weight = self._smoothstep(
                                    (TRANSIENT_RECOVERY_STABILITY_DEG
                                     - (max(samples) - min(samples)))
                                    / TRANSIENT_RECOVERY_STABILITY_DEG)
                                self.transient_confidence = stability_weight
                                self.transient_authority_active = True
                            self.transient_event_active = False
                        else:
                            # Do not retain an ambiguous event as future debt.
                            self.transient_event_active = False
                            self.transient_confidence = 0.0
                            self.transient_authority_active = False
                else:
                    self.transient_validation_elapsed = 0.0
                    self.transient_validation_bucket_elapsed = 0.0
                    self.transient_validation_samples.clear()
                    if hard_transient_invalid:
                        self.transient_event_active = False
                        self.transient_confidence = 0.0
                        self.transient_authority_active = False
            self._transient_was_dynamic = dynamic
        bin_index = int(
            ((magnetic % 360.0) / 360.0) * MOTION_MAG_CLOSURE_BIN_COUNT
        ) % MOTION_MAG_CLOSURE_BIN_COUNT
        orientation_bin = _gravity_orientation_bin(accel)
        bin_state = self._bin_state(bin_index, orientation_bin)
        direction = "cw" if self.filtered_rate_dps >= 0.0 else "ccw"
        bin_sample_key = (bin_index, orientation_bin, direction)
        bin_key_changed = bin_sample_key != self._bin_sample_key
        if bin_key_changed:
            self._bin_sample_key = bin_sample_key
            self._bin_elapsed = 0.0
            self._bin_pause_elapsed = 0.0
        learning_eligible = bool(
            not blocked and motion_weight > 0.0
            and not dynamic and settling_authority_weight > 0.0
            and dynamic_weight > 0.0
            and not relative_rate_law)
        bin_timer_reset_reason = None
        if learning_eligible:
            self._bin_pause_elapsed = 0.0
            self._bin_elapsed += (
                timing.dt_seconds * settling_authority_weight)
            if self._bin_elapsed >= MOTION_MAG_CLOSURE_BIN_SAMPLE_SECONDS:
                self._bin_elapsed -= MOTION_MAG_CLOSURE_BIN_SAMPLE_SECONDS
                samples = self._bins[bin_index][orientation_bin][direction]
                samples.append(self.filtered_innovation_deg)
                if len(samples) > 32:
                    del samples[:-32]
                bin_state = self._bin_state(bin_index, orientation_bin)
        else:
            hard_bin_reset = bool(blocked or raw_stationary)
            if hard_bin_reset:
                self._bin_elapsed = 0.0
                self._bin_pause_elapsed = 0.0
                bin_timer_reset_reason = (
                    "blocked" if blocked else "stationary")
            else:
                self._bin_pause_elapsed += max(0.0, timing.dt_seconds)
                if (self._bin_pause_elapsed
                        > MOTION_MAG_CLOSURE_BIN_PAUSE_LIMIT_SECONDS):
                    self._bin_elapsed = 0.0
                    bin_timer_reset_reason = "pause-timeout"
        corrected_innovation = self.filtered_innovation_deg
        if self.correction_law == "RobustBiasRate":
            local_confidence = max(
                self.robust_bias_confidence, self.transient_confidence)
        elif self.correction_law == "DynamicRate":
            local_confidence = self.consistency_confidence
        else:
            local_confidence = max(
                MOTION_MAG_CLOSURE_BASE_CONFIDENCE,
                float(bin_state.get("model_confidence", 0.0)))
        if (not relative_rate_law
                and bin_state["confidence"]
                and bin_state["offset_deg"] is not None):
            corrected_innovation = _wrap_degrees(
                corrected_innovation - bin_state["offset_deg"])
        instantaneous_quality = (
            accel_quality_weight * dynamic_weight
            * settling_authority_weight
            if not blocked else 0.0)
        effective_confidence = max(
            0.0, min(1.0, local_confidence * instantaneous_quality))
        raw_proportional_target = (
            self.robust_bias_estimate_dps * motion_weight
            if self.correction_law == "RobustBiasRate"
            else MOTION_MAG_CLOSURE_KP * corrected_innovation * motion_weight)
        confidence_weighted_target = (
            raw_proportional_target * effective_confidence)
        dynamic_safety_cap = (
            abs(rate) * MOTION_MAG_DYNAMIC_CORRECTION_RATIO)
        if self.correction_law == "RobustBiasRate":
            absolute_cap = ROBUST_BIAS_OUTPUT_CAP_DPS
            relative_cap = dynamic_safety_cap
            correction_limit = min(absolute_cap, relative_cap)
            attack_slew_dps_per_second = (
                abs(rate) * MOTION_MAG_DYNAMIC_ATTACK_SLEW_RATIO_PER_SECOND)
        elif self.correction_law == "DynamicRate":
            absolute_cap = dynamic_safety_cap
            relative_cap = dynamic_safety_cap
            correction_limit = min(
                dynamic_safety_cap, MOTION_MAG_MAX_BIAS_RATE_DPS)
            attack_slew_dps_per_second = (
                abs(rate) * MOTION_MAG_DYNAMIC_ATTACK_SLEW_RATIO_PER_SECOND)
        else:
            absolute_cap = MOTION_MAG_CLOSURE_MAX_DPS * effective_confidence
            relative_cap = (
                abs(rate) * MOTION_MAG_CLOSURE_MAX_RATE_FRACTION
                * effective_confidence)
            correction_limit = min(absolute_cap, relative_cap)
            attack_slew_dps_per_second = (
                MOTION_MAG_CLOSURE_SLEW_DPS_PER_SECOND)
        correction_target = 0.0
        limiting_reason = "blocked"
        tracking_authority = bool(
            self.correction_law != "DynamicRate"
            or self.residual_state == "Tracking")
        self.frozen_correction_blocked = bool(
            self.correction_law == "DynamicRate"
            and not tracking_authority)
        correction_eligible = bool(
            not blocked and motion_weight > 0.0
            and not dynamic and settling_authority_weight > 0.0
            and effective_confidence > 0.0
            and tracking_authority
            and (self.correction_law != "DynamicRate"
                 or (self.window_accepted
                     and not self.authority_suspended)))
        if correction_eligible:
            requested_target = (
                confidence_weighted_target
                if relative_rate_law
                else raw_proportional_target)
            correction_target = max(
                -correction_limit,
                min(correction_limit, requested_target))
            limiting_reason = (
                ("robust-bias-cap" if self.correction_law == "RobustBiasRate"
                 else "dynamic-rate-cap")
                if abs(requested_target) > correction_limit
                else "none")
        if not correction_eligible:
            self._output_rate_dps = 0.0
        else:
            target = correction_target
            # A sign reversal must return through zero before authority can be
            # granted in the opposite direction.
            if (self._output_rate_dps != 0.0 and target != 0.0
                    and self._output_rate_dps * target < 0.0):
                target = 0.0
            maximum_step = (
                attack_slew_dps_per_second
                * max(0.0, timing.dt_seconds))
            delta = max(
                -maximum_step,
                min(maximum_step, target - self._output_rate_dps))
            self._output_rate_dps += delta
            if abs(delta) + 1e-12 < abs(target - (self._output_rate_dps - delta)):
                limiting_reason = "attack-slew"
            # A falling confidence/rate cap is a hard authority boundary; a
            # previous larger output may never leak through while slewing down.
            self._output_rate_dps = max(
                -correction_limit,
                min(correction_limit, self._output_rate_dps))
            if abs(self._output_rate_dps) <= 1e-12:
                self._output_rate_dps = 0.0
        candidate = self._output_rate_dps
        input_direction_preserved = bool(
            rate == 0.0 or candidate == 0.0
            or (rate + candidate) * rate > 0.0)
        if not input_direction_preserved:
            candidate = 0.0
            self._output_rate_dps = 0.0
        self._last_state = self._state(
            blocked,
            anchor_heading_deg=self.anchor_heading_deg,
            raw_innovation_deg=raw_innovation,
            absolute_heading_innovation_deg=raw_innovation,
            gyro_world_yaw_rate_dps=world_yaw_rate,
            gyro_relative_heading_deg=self.gyro_relative_heading_deg,
            magnetic_relative_heading_deg=self.magnetic_relative_heading_deg,
            relative_closure_error_deg=self.relative_closure_error_deg,
            relative_anchor_required=self._relative_anchor_required,
            raw_magnetic_heading_delta_deg=(
                self.raw_magnetic_heading_delta_deg),
            normalized_magnetic_heading_delta_deg=(
                self.normalized_magnetic_heading_delta_deg),
            gyro_world_yaw_delta_deg=self.gyro_world_yaw_delta_deg,
            gyro_reference_heading_delta_deg=(
                self.gyro_reference_heading_delta_deg),
            heading_delta_direction_agreement=(
                self.heading_delta_direction_agreement),
            corrected_innovation_deg=corrected_innovation,
            motion_weight=motion_weight,
            quality_weight=effective_confidence,
            local_confidence=local_confidence,
            effective_confidence=effective_confidence,
            motion_state=motion_state,
            angular_acceleration_dps2=raw_angular_acceleration_dps2,
            raw_angular_acceleration_dps2=raw_angular_acceleration_dps2,
            filtered_angular_acceleration_dps2=(
                filtered_angular_acceleration_dps2),
            stationary_hint=bool(stationary_hint),
            dynamic_weight=dynamic_weight,
            dynamic_active=self._dynamic_active,
            dynamic_enter_elapsed_seconds=self._dynamic_enter_elapsed,
            accel_quality_weight=accel_quality_weight,
            settling_active=settling_active,
            settling_authority_weight=settling_authority_weight,
            settling_elapsed_seconds=self._settling_elapsed,
            revalidation_required=self._revalidation_required,
            revalidation_elapsed_seconds=self._revalidation_elapsed,
            revalidation_reason=revalidation_reason,
            correction_eligible=correction_eligible,
            learning_eligible=learning_eligible,
            error_debt_deg=corrected_innovation,
            raw_relative_closure_error_deg=self.relative_closure_error_deg,
            filtered_relative_closure_error_deg=self.filtered_innovation_deg,
            consumer_measurement_deg=corrected_innovation,
            absolute_cap_dps=absolute_cap,
            relative_cap_dps=relative_cap,
            correction_law=self.correction_law,
            raw_proportional_target_dps=raw_proportional_target,
            confidence_weighted_target_dps=confidence_weighted_target,
            dynamic_safety_cap_dps=dynamic_safety_cap,
            attack_slew_dps_per_second=attack_slew_dps_per_second,
            limiting_reason=limiting_reason,
            correction_target_dps=correction_target,
            candidate_rate_dps=candidate,
            output_active=candidate != 0.0,
            heading_bin=bin_state,
            bin_sample_key=list(bin_sample_key),
            bin_sample_elapsed_seconds=self._bin_elapsed,
            bin_pause_elapsed_seconds=self._bin_pause_elapsed,
            bin_timer_reset_reason=bin_timer_reset_reason,
            bin_key_changed=bin_key_changed,
            input_direction_preserved=input_direction_preserved,
            stop_zero_enforced=(
                motion_weight == 0.0 or bool(blocked)
                or dynamic or settling_authority_weight == 0.0),
        )
        return dict(self._last_state)

    def snapshot(self) -> dict:
        return dict(self._last_state)


class MovingYawBiasObserver:
    """Estimate additive world-yaw gyro bias during smooth magnetic motion.

    The observer never mutates an estimator or an output.  A consumer may use
    its confidence-gated candidate independently, which keeps 6-axis and
    In-App paths isolated from the Pass-Through experiment.
    """

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self._last_gyro_heading = None
        self._last_magnetic_heading = None
        self._filtered_rate = None
        self._direction = None
        self._elapsed = 0.0
        self._gyro_delta = 0.0
        self._magnetic_delta = 0.0
        self._window_rates = []
        self._windows = {"cw": [], "ccw": []}
        self.filtered_candidate_dps = 0.0
        self._last_state = self._state(["not-observing"])

    def _reset_window(self) -> None:
        self._direction = None
        self._elapsed = 0.0
        self._gyro_delta = 0.0
        self._magnetic_delta = 0.0
        self._window_rates.clear()

    def _state(self, blocked_reasons, **values) -> dict:
        cw = self._windows["cw"] if hasattr(self, "_windows") else []
        ccw = self._windows["ccw"] if hasattr(self, "_windows") else []
        cw_mean = statistics.fmean(cw) if cw else None
        ccw_mean = statistics.fmean(ccw) if ccw else None
        residuals = []
        if cw_mean is not None:
            residuals.extend(value - cw_mean for value in cw)
        if ccw_mean is not None:
            residuals.extend(value - ccw_mean for value in ccw)
        sd = statistics.pstdev(residuals) if len(residuals) >= 2 else None
        additive_candidate = scale_component = None
        if cw_mean is not None and ccw_mean is not None:
            # An additive gyro bias has the same sign in both directions,
            # whereas scale/geometry error reverses with motion direction.
            additive_candidate = 0.5 * (cw_mean + ccw_mean)
            scale_component = 0.5 * (cw_mean - ccw_mean)
        direction_difference = abs(scale_component) if scale_component is not None else None
        confident = bool(
            len(cw) >= MOVING_YAW_BIAS_MIN_WINDOWS_PER_DIRECTION
            and len(ccw) >= MOVING_YAW_BIAS_MIN_WINDOWS_PER_DIRECTION
            and additive_candidate is not None
            and abs(additive_candidate) <= MOVING_YAW_BIAS_RAW_LIMIT_DPS
            and abs(scale_component) <= MOVING_YAW_BIAS_SCALE_LIMIT_DPS
            and sd is not None
            and sd <= MOVING_YAW_BIAS_DIRECTION_SPREAD_LIMIT_DPS)
        state = {
            "observer_mode": "Shadow",
            "observing": not blocked_reasons,
            "eligible": not blocked_reasons,
            "blocked_reasons": list(blocked_reasons),
            "direction": self._direction,
            "observation_seconds": self._elapsed,
            "gyro_heading_delta_deg": self._gyro_delta,
            "magnetic_heading_delta_deg": self._magnetic_delta,
            "raw_candidate_dps": None,
            "additive_candidate_dps": additive_candidate,
            "scale_component_dps": scale_component,
            "filtered_candidate_dps": self.filtered_candidate_dps,
            "cw_candidate_dps": cw_mean,
            "ccw_candidate_dps": ccw_mean,
            "cw_windows": len(cw),
            "ccw_windows": len(ccw),
            "direction_difference_dps": direction_difference,
            "candidate_sd_dps": sd,
            "confidence": confident,
            "applied_dps": 0.0,
        }
        state.update(values)
        return state

    def update(
        self,
        gyro_heading_deg: float,
        magnetic_heading_deg: float,
        angular_rate_dps: float,
        accelerometer_g: Sequence[float],
        timing: V2Timing,
        *,
        magnetic_quality_valid: bool,
    ) -> dict:
        gyro_heading = float(gyro_heading_deg)
        magnetic_heading = float(magnetic_heading_deg)
        rate = float(angular_rate_dps)
        accel = _vector3(accelerometer_g, "accelerometer_g")
        blocked = []
        if timing.status != "valid" or not timing.integrate:
            blocked.append("timing")
        if not magnetic_quality_valid:
            blocked.append("magnetic-quality")
        accel_magnitude = math.sqrt(sum(value * value for value in accel))
        if abs(accel_magnitude - 1.0) > MOVING_YAW_BIAS_ACCEL_TOLERANCE_G:
            blocked.append("linear-acceleration")
        if self._filtered_rate is None:
            self._filtered_rate = rate
        elif timing.dt_seconds > 0.0:
            alpha = 1.0 - math.exp(
                -timing.dt_seconds / MOVING_YAW_BIAS_RATE_LOWPASS_SECONDS)
            self._filtered_rate += alpha * (rate - self._filtered_rate)
        filtered_rate = self._filtered_rate
        magnitude = abs(filtered_rate)
        if magnitude < MOVING_YAW_BIAS_MIN_RATE_DPS:
            blocked.append("rate-low")
        elif magnitude > MOVING_YAW_BIAS_MAX_RATE_DPS:
            blocked.append("rate-high")

        gyro_step = magnetic_step = None
        if self._last_gyro_heading is not None:
            gyro_step = _wrap_degrees(gyro_heading - self._last_gyro_heading)
        if self._last_magnetic_heading is not None:
            magnetic_step = _wrap_degrees(
                magnetic_heading - self._last_magnetic_heading)
        self._last_gyro_heading = gyro_heading
        self._last_magnetic_heading = magnetic_heading
        direction = "cw" if filtered_rate >= 0.0 else "ccw"
        if self._direction is not None and direction != self._direction:
            blocked.append("direction-change")
        if blocked or gyro_step is None or magnetic_step is None:
            self._reset_window()
            self._last_state = self._state(blocked or ["initial-sample"])
            return dict(self._last_state)

        self._direction = direction
        self._elapsed += timing.dt_seconds
        self._gyro_delta += gyro_step
        self._magnetic_delta += magnetic_step
        self._window_rates.append(filtered_rate)
        raw_candidate = None
        rejected_reason = None
        completed_direction = None
        if self._elapsed >= MOVING_YAW_BIAS_WINDOW_SECONDS:
            direction_error = (
                (self._gyro_delta - self._magnetic_delta) / self._elapsed)
            rate_sd = (statistics.pstdev(self._window_rates)
                       if len(self._window_rates) >= 2 else math.inf)
            if rate_sd > MOVING_YAW_BIAS_WINDOW_RATE_SD_LIMIT_DPS:
                rejected_reason = "rate-variance"
            elif abs(direction_error) > MOVING_YAW_BIAS_DIRECTION_ERROR_LIMIT_DPS:
                rejected_reason = "direction-error"
            else:
                raw_candidate = direction_error
                completed_direction = direction
                values = self._windows[direction]
                values.append(raw_candidate)
                if len(values) > 8:
                    del values[:-8]
            self._reset_window()
        self._last_state = self._state(
            [], raw_candidate_dps=raw_candidate,
            completed_window=raw_candidate is not None,
            completed_direction=completed_direction,
            window_rejected_reason=rejected_reason)
        additive = self._last_state.get("additive_candidate_dps")
        if (raw_candidate is not None
                and self._last_state.get("confidence")
                and additive is not None):
            target = max(-MOVING_YAW_BIAS_RAW_LIMIT_DPS,
                         min(MOVING_YAW_BIAS_RAW_LIMIT_DPS, additive))
            self.filtered_candidate_dps += 0.2 * (
                target - self.filtered_candidate_dps)
            self._last_state["filtered_candidate_dps"] = (
                self.filtered_candidate_dps)
        return dict(self._last_state)

    def snapshot(self) -> dict:
        return dict(self._last_state)

    def suspend(self, *reasons: str) -> dict:
        self._reset_window()
        self._last_state = self._state(list(reasons) or ["suspended"])
        return dict(self._last_state)


class PassthroughMovingYawBiasConsumer:
    """Apply a confidence-gated observer result within a shared authority cap."""

    def __init__(self):
        self.applied_dps = 0.0

    def reset(self) -> None:
        self.applied_dps = 0.0

    def update(self, candidate_dps: float, dt_seconds: float, *,
               assist_enabled: bool, authorized: bool,
               heading_output_rate_dps: float,
               mode: str = PASSTHROUGH_MOVING_YAW_BIAS_MODE) -> dict:
        if mode not in PASSTHROUGH_MOVING_YAW_BIAS_MODES:
            mode = "Off"
        try:
            candidate = float(candidate_dps)
            dt = float(dt_seconds)
        except (TypeError, ValueError):
            candidate, dt = 0.0, 0.0
        if not math.isfinite(candidate):
            candidate = 0.0
        valid_dt = math.isfinite(dt) and 0.0 < dt <= V2_MAX_DT_SECONDS
        requested = max(-MOVING_YAW_BIAS_APPLIED_LIMIT_DPS,
                        min(MOVING_YAW_BIAS_APPLIED_LIMIT_DPS, candidate))
        active_mode = mode == "V2" and bool(assist_enabled)
        desired = requested if active_mode and authorized and valid_dt else 0.0
        if not assist_enabled or mode != "V2" or not valid_dt:
            self.applied_dps = 0.0
        else:
            step = MOVING_YAW_BIAS_SLEW_DPS_PER_SECOND * dt
            delta = max(-step, min(step, desired - self.applied_dps))
            self.applied_dps += delta
        remaining = max(
            0.0,
            V2_HEADING_CORRECTION_MAX_DPS - abs(float(heading_output_rate_dps)))
        self.applied_dps = max(-remaining, min(remaining, self.applied_dps))
        return {
            "consumer_mode": mode,
            "requested_applied_dps": requested,
            "budgeted_applied_dps": self.applied_dps,
            "applied_dps": self.applied_dps,
            "authorized": bool(authorized),
            "assist_enabled": bool(assist_enabled),
            "shadow_only": mode == "Shadow",
            "authority_budget_dps": V2_HEADING_CORRECTION_MAX_DPS,
            "heading_output_rate_dps": float(heading_output_rate_dps),
            "remaining_authority_dps": remaining,
        }


def apply_world_yaw_bias_correction(
    corrected_gyro_dps: Sequence[float],
    orientation_wxyz: Sequence[float],
    bias_dps: float,
) -> dict:
    """Subtract a world-vertical yaw bias expressed in the sensor body frame."""
    gyro = _vector3(corrected_gyro_dps, "corrected_gyro_dps")
    q = tuple(float(value) for value in orientation_wxyz)
    if len(q) != 4 or not all(math.isfinite(value) for value in q):
        raise ValueError("orientation_wxyz must contain four finite numbers")
    q_inverse = (q[0], -q[1], -q[2], -q[3])
    vertical_body = _quaternion_rotate_vector_wxyz(q_inverse, (0.0, 0.0, 1.0))
    vector = tuple(float(bias_dps) * value for value in vertical_body)
    output = tuple(gyro[i] - vector[i] for i in range(3))
    return {
        "gyroscope_dps": list(output),
        "input_gyroscope_dps": list(gyro),
        "candidate_vector_body_dps": list(vector),
        "applied_bias_dps": float(bias_dps),
    }


def apply_world_yaw_rate_correction(
    corrected_gyro_dps: Sequence[float],
    orientation_wxyz: Sequence[float],
    correction_rate_dps: float,
) -> dict:
    """Add a world-yaw correction rate in the sensor body frame."""
    gyro = _vector3(corrected_gyro_dps, "corrected_gyro_dps")
    q = tuple(float(value) for value in orientation_wxyz)
    if len(q) != 4 or not all(math.isfinite(value) for value in q):
        raise ValueError("orientation_wxyz must contain four finite numbers")
    q_inverse = (q[0], -q[1], -q[2], -q[3])
    vertical_body = _quaternion_rotate_vector_wxyz(
        q_inverse, (0.0, 0.0, 1.0))
    vector = tuple(float(correction_rate_dps) * value
                   for value in vertical_body)
    output = tuple(gyro[i] + vector[i] for i in range(3))
    return {
        "gyroscope_dps": list(output),
        "input_gyroscope_dps": list(gyro),
        "correction_vector_body_dps": list(vector),
        "applied_rate_dps": float(correction_rate_dps),
    }


class MotionMagneticClosureConsumer:
    """Independent closed-loop correction state for one output consumer."""

    def __init__(self):
        self.reset()

    def reset(self, *, preserve_session: bool = False) -> None:
        session_accumulated = (
            getattr(self, "session_accumulated_applied_correction_deg", 0.0)
            if preserve_session else 0.0)
        self.measurement_origin_deg = None
        self.accumulated_applied_correction_deg = 0.0
        self.session_accumulated_applied_correction_deg = session_accumulated
        self.output_rate_dps = 0.0
        self._measurement_epoch = None
        self._estimator_was_allowed = False
        self._reentry_elapsed = 0.0
        self.repayment_budget_remaining_deg = 0.0
        self.repayment_budget_used_deg = 0.0
        self._transient_epoch = None
        self.transient_debt_remaining_deg = 0.0
        self.transient_debt_used_deg = 0.0

    @staticmethod
    def _smoothstep(value: float) -> float:
        value = max(0.0, min(1.0, value))
        return value * value * (3.0 - 2.0 * value)

    def update(self, estimator_state: Mapping | None, *, mode: str,
               eligible: bool, dt_seconds: float,
               authority_scale: float = 1.0) -> dict:
        state = dict(estimator_state or {})
        if mode not in MOTION_MAG_CLOSURE_MODES:
            mode = "Off"
        dt = max(0.0, float(dt_seconds))
        authority_scale = max(0.0, min(1.0, float(authority_scale)))
        selected = bool(eligible) and mode in ("Shadow", "V2")
        try:
            measurement_epoch = int(state.get("measurement_epoch", 0))
        except (TypeError, ValueError):
            measurement_epoch = 0
        epoch_changed = bool(
            self._measurement_epoch is not None
            and self._measurement_epoch != measurement_epoch)
        if epoch_changed:
            self.reset(preserve_session=True)
        self._measurement_epoch = measurement_epoch
        measurement = float(state.get(
            "consumer_measurement_deg",
            state.get("error_debt_deg", 0.0)) or 0.0)
        if not math.isfinite(measurement):
            measurement = 0.0
        try:
            transient_epoch = int(state.get("transient_epoch", 0))
        except (TypeError, ValueError):
            transient_epoch = 0
        transient_epoch_changed = bool(
            self._transient_epoch is not None
            and self._transient_epoch != transient_epoch)
        if transient_epoch_changed:
            self.transient_debt_remaining_deg = 0.0
            self.output_rate_dps = 0.0
        self._transient_epoch = transient_epoch
        if not selected:
            self.reset()
            state.update({
                "consumer_mode": mode,
                "eligible": bool(eligible),
                "consumer_measurement_origin_deg": None,
                "consumer_residual_deg": 0.0,
                "accumulated_applied_correction_deg": 0.0,
                "session_accumulated_applied_correction_deg": 0.0,
                "consumer_reentry_ramp_progress": 0.0,
                "repayment_budget_remaining_deg": 0.0,
                "repayment_budget_used_deg": 0.0,
                "transient_debt_remaining_deg": 0.0,
                "transient_debt_used_deg": 0.0,
                "consumer_transient_epoch": transient_epoch,
                "consumer_transient_epoch_changed": transient_epoch_changed,
                "consumer_requested_rate_dps": 0.0,
                "consumer_feedback_invariant_error_deg": 0.0,
                "consumer_feedback_invariant_valid": True,
                "consumer_measurement_epoch": measurement_epoch,
                "consumer_epoch_changed": epoch_changed,
                "applied_rate_dps": 0.0,
                "applied": False,
                "shadow_only": mode == "Shadow" and bool(eligible),
                "stop_zero_enforced": True,
            })
            return state

        origin_initialized = self.measurement_origin_deg is not None
        if not origin_initialized:
            self.measurement_origin_deg = measurement
            self.accumulated_applied_correction_deg = 0.0
            self.output_rate_dps = 0.0
        robust_bias_mode = state.get("correction_law") == "RobustBiasRate"
        budget_enforced = (
            "committed_error_increment_deg" in state
            and not robust_bias_mode)
        committed_increment = abs(float(
            state.get("committed_error_increment_deg", 0.0) or 0.0))
        if origin_initialized and math.isfinite(committed_increment):
            self.repayment_budget_remaining_deg += committed_increment
        transient_commit = float(
            state.get("transient_commit_increment_deg", 0.0) or 0.0)
        if (robust_bias_mode and origin_initialized
                and math.isfinite(transient_commit)):
            self.transient_debt_remaining_deg = max(
                -TRANSIENT_RECOVERY_MAX_DEBT_DEG,
                min(TRANSIENT_RECOVERY_MAX_DEBT_DEG,
                    self.transient_debt_remaining_deg + transient_commit))
        residual = _wrap_degrees(
            measurement - self.measurement_origin_deg
            - self.accumulated_applied_correction_deg)
        estimator_allowed = bool(state.get("correction_eligible", False))
        if estimator_allowed and not self._estimator_was_allowed:
            self._reentry_elapsed = 0.0
        if estimator_allowed:
            self._reentry_elapsed = min(
                MOTION_MAG_CONSUMER_REENTRY_RAMP_SECONDS,
                self._reentry_elapsed + dt)
        else:
            self._reentry_elapsed = 0.0
        self._estimator_was_allowed = estimator_allowed
        if self._reentry_elapsed <= MOTION_MAG_CONSUMER_REENTRY_ZERO_SECONDS:
            ramp = 0.0
        elif self._reentry_elapsed >= MOTION_MAG_CONSUMER_REENTRY_RAMP_SECONDS:
            ramp = 1.0
        else:
            ramp = self._smoothstep(
                (self._reentry_elapsed - MOTION_MAG_CONSUMER_REENTRY_ZERO_SECONDS)
                / (MOTION_MAG_CONSUMER_REENTRY_RAMP_SECONDS
                   - MOTION_MAG_CONSUMER_REENTRY_ZERO_SECONDS))
        confidence = max(0.0, min(
            1.0, float(state.get("effective_confidence", 0.0) or 0.0)))
        motion_weight = max(0.0, min(
            1.0, float(state.get("motion_weight", 0.0) or 0.0)))
        if robust_bias_mode:
            bias_requested = float(
                state.get("correction_target_dps", 0.0) or 0.0) * ramp
            transient_requested = (
                TRANSIENT_RECOVERY_KP
                * self.transient_debt_remaining_deg
                * confidence * motion_weight * ramp)
            transient_requested = max(
                -TRANSIENT_RECOVERY_OUTPUT_CAP_DPS,
                min(TRANSIENT_RECOVERY_OUTPUT_CAP_DPS,
                    transient_requested))
            requested = bias_requested + transient_requested
            cap = min(
                abs(float(
                    state.get("dynamic_safety_cap_dps", 0.0) or 0.0)),
                (TRANSIENT_RECOVERY_OUTPUT_CAP_DPS
                 if abs(self.transient_debt_remaining_deg) > 1e-9
                 else ROBUST_BIAS_OUTPUT_CAP_DPS))
        else:
            bias_requested = 0.0
            transient_requested = 0.0
            requested = (
                MOTION_MAG_CLOSURE_KP * residual
                * confidence * motion_weight * ramp)
            cap = min(
                abs(float(
                    state.get("dynamic_safety_cap_dps", 0.0) or 0.0)),
                MOTION_MAG_MAX_BIAS_RATE_DPS)
        target = max(-cap, min(cap, requested))
        if not estimator_allowed:
            self.output_rate_dps = 0.0
        else:
            if (self.output_rate_dps != 0.0 and target != 0.0
                    and self.output_rate_dps * target < 0.0):
                target = 0.0
            slew = max(0.0, float(
                state.get("attack_slew_dps_per_second", 0.0) or 0.0))
            maximum_step = slew * dt
            self.output_rate_dps += max(
                -maximum_step,
                min(maximum_step, target - self.output_rate_dps))
            self.output_rate_dps = max(
                -cap, min(cap, self.output_rate_dps))
        candidate = self.output_rate_dps
        applied = (
            candidate * authority_scale if mode == "V2" else 0.0)
        if budget_enforced and dt > 0.0:
            budget_rate = self.repayment_budget_remaining_deg / dt
            if mode == "V2":
                applied = max(-budget_rate, min(budget_rate, applied))
                if authority_scale > 0.0:
                    candidate = applied / authority_scale
                    self.output_rate_dps = candidate
            else:
                candidate = max(-budget_rate, min(budget_rate, candidate))
                self.output_rate_dps = candidate
        feedback_rate = applied if mode == "V2" else candidate
        feedback_delta = feedback_rate * dt
        self.accumulated_applied_correction_deg += feedback_delta
        transient_fraction = (
            min(1.0, abs(transient_requested)
                / max(1e-12, abs(bias_requested)
                      + abs(transient_requested)))
            if robust_bias_mode else 0.0)
        transient_feedback_delta = feedback_delta * transient_fraction
        transient_used = 0.0
        if (self.transient_debt_remaining_deg != 0.0
                and transient_feedback_delta
                * self.transient_debt_remaining_deg > 0.0):
            transient_used = min(
                abs(self.transient_debt_remaining_deg),
                abs(transient_feedback_delta))
            self.transient_debt_remaining_deg -= math.copysign(
                transient_used, self.transient_debt_remaining_deg)
            if abs(self.transient_debt_remaining_deg) <= 1e-9:
                self.transient_debt_remaining_deg = 0.0
        self.transient_debt_used_deg += transient_used
        used_delta = min(
            self.repayment_budget_remaining_deg, abs(feedback_delta))
        self.repayment_budget_remaining_deg = max(
            0.0, self.repayment_budget_remaining_deg - used_delta)
        self.repayment_budget_used_deg += used_delta
        self.session_accumulated_applied_correction_deg += applied * dt
        residual_after = _wrap_degrees(
            measurement - self.measurement_origin_deg
            - self.accumulated_applied_correction_deg)
        invariant_error = _wrap_degrees(
            (self.accumulated_applied_correction_deg + residual_after)
            - (measurement - self.measurement_origin_deg))
        invariant_valid = bool(
            math.isfinite(invariant_error) and abs(invariant_error) <= 1e-6)
        if not invariant_valid:
            self.output_rate_dps = 0.0
            applied = 0.0
        state.update({
            "consumer_mode": mode,
            "eligible": bool(eligible),
            "consumer_measurement_origin_deg": self.measurement_origin_deg,
            "consumer_residual_deg": residual_after,
            "accumulated_applied_correction_deg": (
                self.accumulated_applied_correction_deg),
            "session_accumulated_applied_correction_deg": (
                self.session_accumulated_applied_correction_deg),
            "consumer_reentry_ramp_progress": ramp,
            "repayment_budget_remaining_deg": (
                self.repayment_budget_remaining_deg),
            "repayment_budget_used_deg": self.repayment_budget_used_deg,
            "transient_debt_remaining_deg": (
                self.transient_debt_remaining_deg),
            "transient_debt_used_deg": self.transient_debt_used_deg,
            "consumer_transient_epoch": transient_epoch,
            "consumer_transient_epoch_changed": transient_epoch_changed,
            "consumer_transient_requested_dps": transient_requested,
            "consumer_requested_rate_dps": requested,
            "consumer_feedback_invariant_error_deg": invariant_error,
            "consumer_feedback_invariant_valid": invariant_valid,
            "consumer_measurement_epoch": measurement_epoch,
            "consumer_epoch_changed": epoch_changed,
            "candidate_rate_dps": candidate,
            "applied_rate_dps": applied,
            "consumer_authority_scale": authority_scale,
            "applied": applied != 0.0,
            "shadow_only": mode == "Shadow",
            "stop_zero_enforced": not estimator_allowed,
        })
        return state


def select_motion_magnetic_closure(
    estimator_state: Mapping | None,
    *,
    mode: str,
    eligible: bool,
    authority_scale: float = 1.0,
    consumer: MotionMagneticClosureConsumer | None = None,
    dt_seconds: float = 0.0,
) -> dict:
    """Select an independent consumer without retaining post-stop output."""
    if consumer is not None:
        return consumer.update(
            estimator_state, mode=mode, eligible=eligible,
            dt_seconds=dt_seconds, authority_scale=authority_scale)
    if mode not in MOTION_MAG_CLOSURE_MODES:
        mode = "Off"
    state = dict(estimator_state or {})
    try:
        candidate = float(state.get("candidate_rate_dps", 0.0))
    except (TypeError, ValueError):
        candidate = 0.0
    if not math.isfinite(candidate):
        candidate = 0.0
    try:
        consumer_cap = abs(float(state.get(
            "dynamic_safety_cap_dps", MOTION_MAG_CLOSURE_MAX_DPS)))
    except (TypeError, ValueError):
        consumer_cap = MOTION_MAG_CLOSURE_MAX_DPS
    if not math.isfinite(consumer_cap):
        consumer_cap = MOTION_MAG_CLOSURE_MAX_DPS
    candidate = max(-consumer_cap, min(consumer_cap, candidate))
    try:
        authority_scale = float(authority_scale)
    except (TypeError, ValueError):
        authority_scale = 0.0
    if not math.isfinite(authority_scale):
        authority_scale = 0.0
    authority_scale = max(0.0, min(1.0, authority_scale))
    applied = (
        candidate * authority_scale
        if mode == "V2" and bool(eligible) else 0.0)
    state.update({
        "consumer_mode": mode,
        "eligible": bool(eligible),
        "candidate_rate_dps": candidate,
        "applied_rate_dps": applied,
        "consumer_authority_scale": authority_scale,
        "applied": applied != 0.0,
        "shadow_only": mode == "Shadow" and bool(eligible),
        "stop_zero_enforced": bool(
            state.get("motion_weight", 0.0) == 0.0 or not eligible),
    })
    return state


def apply_passthrough_heading_correction(
    corrected_gyro_dps: Sequence[float],
    heading_correction_rate_dps: float,
    *,
    assist_enabled: bool,
    horizon_lock: bool,
    controller_mode: str = PASSTHROUGH_HEADING_OUTPUT_MODE,
) -> dict:
    """Apply gated low-frequency heading authority to local yaw output.

    Horizon projection already consumes the corrected AHRS heading, so direct
    yaw-rate authority is restricted to Horizon Off.  Shadow computes and logs
    the candidate without changing output; Legacy disables it entirely.
    """
    gyro = _vector3(corrected_gyro_dps, "corrected_gyro_dps")
    try:
        rate = float(heading_correction_rate_dps)
    except (TypeError, ValueError):
        rate = 0.0
    if not math.isfinite(rate):
        rate = 0.0
    rate = max(
        -V2_HEADING_CORRECTION_MAX_DPS,
        min(V2_HEADING_CORRECTION_MAX_DPS, rate),
    )
    if controller_mode not in PASSTHROUGH_HEADING_OUTPUT_MODES:
        controller_mode = "V2"
    eligible = bool(assist_enabled) and not bool(horizon_lock)
    candidate_rate = rate if eligible and controller_mode != "Legacy" else 0.0
    candidate = (gyro[0], gyro[1], gyro[2] + candidate_rate)
    applied = eligible and controller_mode == "V2" and candidate_rate != 0.0
    output = candidate if controller_mode == "V2" and eligible else gyro
    return {
        "gyroscope_dps": list(output),
        "input_gyroscope_dps": list(gyro),
        "candidate_gyroscope_dps": list(candidate),
        "candidate_rate_dps": candidate_rate,
        "applied_rate_dps": candidate_rate if applied else 0.0,
        "assist_enabled": bool(assist_enabled),
        "horizon_lock": bool(horizon_lock),
        "controller_mode": controller_mode,
        "eligible": eligible,
        "applied": applied,
        "shadow_only": eligible and controller_mode == "Shadow",
    }


def build_v2_accelerometer_output(
    accelerometer_lsb: Sequence[float],
    orientation_wxyz: Sequence[float],
    *,
    is_pro_controller: bool,
    hold_mode: str,
    horizon_lock: bool,
    orientation_valid: bool,
) -> dict:
    """Roll-compensate the accelerometer so it shares the V2 Horizon gyro basis.

    Mirrors the Legacy Horizon roll compensation, but driven by the V2 fusion
    quaternion.  Without this the V2 gyro is pre-rotated into the horizon basis
    while the accelerometer still reports raw local gravity, and Steam Input
    reads the two against incompatible bases.
    """
    if not horizon_lock:
        return {"available": False, "reason": "horizon-lock-disabled"}
    if not orientation_valid:
        return {"available": False, "reason": "orientation-uninitialised"}
    accel = _vector3(accelerometer_lsb, "accelerometer_lsb")
    q = tuple(float(value) for value in orientation_wxyz)
    if len(q) != 4:
        raise ValueError("orientation_wxyz must contain four finite numbers")
    q_inv = (q[0], -q[1], -q[2], -q[3])
    down_body = _quaternion_rotate_vector_wxyz(q_inv, (0.0, 0.0, -1.0))
    # Same axis convention as build_v2_gyro_output, so gyro and accelerometer
    # cannot disagree about which axis roll is measured around.
    vertical_axes = bool(is_pro_controller) or str(hold_mode) != "Horizontal"
    if vertical_axes:
        # Roll is around the local Y-axis (in the X-Z plane).
        roll_rad = math.atan2(down_body[0], -down_body[2])
        q_roll = (math.cos(roll_rad / 2.0), 0.0, math.sin(roll_rad / 2.0), 0.0)
    else:
        # Roll is around the local X-axis (in the Y-Z plane).
        roll_rad = math.atan2(-down_body[1], -down_body[2])
        q_roll = (math.cos(roll_rad / 2.0), math.sin(roll_rad / 2.0), 0.0, 0.0)
    return {
        "available": True,
        "reason": None,
        "accelerometer": list(_quaternion_rotate_vector_wxyz(q_roll, accel)),
        "roll_radians": roll_rad,
    }


def build_in_app_horizon_output(
    corrected_gyro_lsb: Sequence[float],
    orientation_wxyz: Sequence[float],
    *,
    is_pro_controller: bool,
    hold_mode: str,
) -> dict:
    """Project In-App gyro into its always-on Horizon basis."""
    candidate = build_v2_gyro_output(
        corrected_gyro_lsb,
        orientation_wxyz,
        gyro_lsb_per_dps=1.0,
        is_pro_controller=is_pro_controller,
        hold_mode=hold_mode,
        horizon_lock=True,
        orientation_valid=True,
        heading_constrained=False,
    )
    gyro = candidate["gyroscope"]
    vertical_axes = bool(is_pro_controller) or str(hold_mode) != "Horizontal"
    return {
        "horizontal_lsb": -float(gyro[2]),
        "vertical_lsb": float(gyro[0] if vertical_axes else -gyro[1]),
        "projected_gyroscope": list(gyro),
        "horizon_lock": True,
    }


def validate_soft_iron_matrix(matrix) -> tuple[bool, dict]:
    """Conservatively validate a finite, invertible 3x3 correction matrix."""
    try:
        rows = tuple(tuple(float(value) for value in row) for row in matrix)
    except (TypeError, ValueError):
        return False, {"reason": "invalid-shape"}
    if len(rows) != 3 or any(len(row) != 3 for row in rows):
        return False, {"reason": "invalid-shape"}
    if not all(math.isfinite(value) for row in rows for value in row):
        return False, {"reason": "non-finite"}
    a, b, c = rows
    determinant = (
        a[0] * (b[1] * c[2] - b[2] * c[1])
        - a[1] * (b[0] * c[2] - b[2] * c[0])
        + a[2] * (b[0] * c[1] - b[1] * c[0]))
    row_norms = [math.sqrt(sum(value * value for value in row)) for row in rows]
    condition_proxy = max(row_norms) / max(1e-12, min(row_norms))
    inverse_condition_proxy = math.inf
    if abs(determinant) > 1e-12:
        inverse = (
            ((b[1] * c[2] - b[2] * c[1]) / determinant,
             (a[2] * c[1] - a[1] * c[2]) / determinant,
             (a[1] * b[2] - a[2] * b[1]) / determinant),
            ((b[2] * c[0] - b[0] * c[2]) / determinant,
             (a[0] * c[2] - a[2] * c[0]) / determinant,
             (a[2] * b[0] - a[0] * b[2]) / determinant),
            ((b[0] * c[1] - b[1] * c[0]) / determinant,
             (a[1] * c[0] - a[0] * c[1]) / determinant,
             (a[0] * b[1] - a[1] * b[0]) / determinant),
        )
        matrix_frobenius = math.sqrt(
            sum(value * value for row in rows for value in row))
        inverse_frobenius = math.sqrt(
            sum(value * value for row in inverse for value in row))
        inverse_condition_proxy = (
            matrix_frobenius * inverse_frobenius / 3.0)
    valid = (
        abs(determinant) > 1e-6
        and condition_proxy <= 3.0
        and inverse_condition_proxy <= 3.0)
    return valid, {
        "reason": None if valid else "ill-conditioned",
        "determinant": determinant,
        "condition_proxy": condition_proxy,
        "inverse_condition_proxy": inverse_condition_proxy,
        "matrix": [list(row) for row in rows],
    }


def apply_soft_iron_matrix(vector, matrix) -> tuple[float, float, float]:
    v = _vector3(vector, "magnetometer_lsb")
    valid, quality = validate_soft_iron_matrix(matrix)
    if not valid:
        raise ValueError(quality["reason"])
    rows = quality["matrix"]
    return tuple(sum(rows[i][j] * v[j] for j in range(3)) for i in range(3))


def _gravity_orientation_bin(accelerometer_g) -> str:
    x, y, z = _vector3(accelerometer_g, "accelerometer_g")
    magnitude = math.sqrt(x * x + y * y + z * z)
    if magnitude <= 1e-9:
        return "invalid"
    nx, ny, nz = x / magnitude, y / magnitude, z / magnitude
    flat_threshold = math.cos(math.radians(30.0))
    if nz >= flat_threshold:
        return "flat-up"
    if nz <= -flat_threshold:
        return "flat-down"
    if abs(nx) >= abs(ny):
        return "right-tilt" if nx >= 0.0 else "left-tilt"
    return "backward-tilt" if ny >= 0.0 else "forward-tilt"


class StationaryRuntimeBias:
    """Conservative, stationary-only runtime gyro bias estimator for V2."""

    def __init__(self):
        self.bias_dps = [0.0, 0.0, 0.0]
        self.stationary_seconds = 0.0
        self._window = deque()
        self._window_seconds = 0.0
        self._window_sum = [0.0, 0.0, 0.0, 0.0]
        self._window_sum_sq = [0.0, 0.0, 0.0, 0.0]

    def reset_stationary_state(self) -> None:
        self.stationary_seconds = 0.0
        self._window.clear()
        self._window_seconds = 0.0
        self._window_sum[:] = (0.0, 0.0, 0.0, 0.0)
        self._window_sum_sq[:] = (0.0, 0.0, 0.0, 0.0)

    def reset(self) -> None:
        self.bias_dps[:] = (0.0, 0.0, 0.0)
        self.reset_stationary_state()

    def update(
        self,
        gyroscope_dps: Sequence[float],
        accelerometer_g: Sequence[float],
        timing: V2Timing,
        *,
        movement_hint_dps: float | None = None,
    ) -> tuple[tuple[float, float, float], dict]:
        gyro = _vector3(gyroscope_dps, "gyroscope_dps")
        accel = _vector3(accelerometer_g, "accelerometer_g")
        corrected = tuple(gyro[i] - self.bias_dps[i] for i in range(3))
        accel_magnitude = math.sqrt(sum(value * value for value in accel))
        reasons = []
        if not timing.allow_bias_update:
            reasons.append("timing")
        if abs(accel_magnitude - 1.0) >= V2_STATIONARY_ACCEL_TOLERANCE_G:
            reasons.append("accel-magnitude")
        if any(abs(value) >= V2_STATIONARY_GYRO_LIMIT_DPS for value in corrected):
            reasons.append("gyro-magnitude")
        if movement_hint_dps is not None and movement_hint_dps >= V2_STATIONARY_MOVEMENT_HINT_DPS:
            reasons.append("movement-hint")

        learning = False
        accel_sd = 0.0
        gyro_sd = [0.0, 0.0, 0.0]
        if reasons:
            self.reset_stationary_state()
        else:
            dt = timing.dt_seconds
            values = (accel_magnitude, *corrected)
            self._window.append((dt, *values))
            self._window_seconds += dt
            for index, value in enumerate(values):
                self._window_sum[index] += value
                self._window_sum_sq[index] += value * value
            while self._window and self._window_seconds - self._window[0][0] >= V2_STATIONARY_VARIANCE_WINDOW_SECONDS:
                removed = self._window.popleft()
                self._window_seconds -= removed[0]
                for index, value in enumerate(removed[1:]):
                    self._window_sum[index] -= value
                    self._window_sum_sq[index] -= value * value
            if len(self._window) >= 2:
                count = len(self._window)
                deviations = [
                    math.sqrt(max(0.0, self._window_sum_sq[index] / count
                                  - (self._window_sum[index] / count) ** 2))
                    for index in range(4)
                ]
                accel_sd = deviations[0]
                gyro_sd = deviations[1:]
            variance_ready = self._window_seconds >= V2_STATIONARY_VARIANCE_WINDOW_SECONDS * 0.9
            if variance_ready and (
                accel_sd >= V2_STATIONARY_ACCEL_SD_LIMIT_G
                or any(value >= V2_STATIONARY_GYRO_SD_LIMIT_DPS for value in gyro_sd)
            ):
                reasons.append("variance")
                self.reset_stationary_state()
            else:
                self.stationary_seconds += dt
                if variance_ready and self.stationary_seconds >= V2_RUNTIME_BIAS_DWELL_SECONDS:
                    alpha = 1.0 - math.exp(-2.0 * math.pi * V2_RUNTIME_BIAS_CUTOFF_HZ * dt)
                    for axis in range(3):
                        estimate = self.bias_dps[axis] + alpha * corrected[axis]
                        self.bias_dps[axis] = max(
                            -V2_RUNTIME_BIAS_LIMIT_DPS,
                            min(V2_RUNTIME_BIAS_LIMIT_DPS, estimate),
                        )
                    corrected = tuple(gyro[i] - self.bias_dps[i] for i in range(3))
                    learning = True

        return corrected, {
            "runtime_bias_dps": list(self.bias_dps),
            "stationary_seconds": self.stationary_seconds,
            "stationary": not reasons,
            "learning": learning,
            "blocked_reasons": reasons,
            "accel_magnitude_g": accel_magnitude,
            "accel_magnitude_sd_g": accel_sd,
            "gyro_axis_sd_dps": gyro_sd,
            "corrected_gyro_dps": list(corrected),
        }


class MagnetometerQualityGate:
    """Session-scoped Phase 4 magnitude gate with conservative 6-axis fallback."""

    def __init__(self):
        self.baseline_magnitude = None
        self.baseline_source = None
        self.candidate_baseline_magnitude = None
        self.candidate_stability_confidence = 0.0
        self.magnitude_stability_confidence = 0.0
        self.baseline_epoch = 0
        self._baseline_reanchor_pending = False
        self._direction_candidate_innovation_deg = None
        self._direction_candidate_seconds = 0.0
        self._baseline_samples = []
        self._baseline_seconds = 0.0
        self._last_magnitude = None
        self._recovery_seconds = 0.0
        self.disturbance_count = 0
        self.fallback_count = 0
        self.heading_initialized = False
        self.heading_reference_offset_deg = None
        self.direction_valid = False
        self._direction_recovery_seconds = 0.0
        self._direction_motion_active = False
        self._direction_settling_seconds = 0.0
        self._post_motion_active = False
        self._post_motion_elapsed = 0.0
        self._post_motion_innovations = []
        self._last_magnetic_heading = None
        self._last_current_heading = None

    @staticmethod
    def _alpha(dt_seconds: float, time_constant: float) -> float:
        if time_constant <= 0.0:
            return 1.0
        return 1.0 - math.exp(-max(0.0, dt_seconds) / time_constant)

    def _observe_adaptive_candidate(self, magnitude: float,
                                    timing: V2Timing) -> bool:
        """Track stable field strength independently from correction gates."""
        if timing.status != "valid" or not timing.integrate:
            self.candidate_stability_confidence = 0.0
            return False
        if self.candidate_baseline_magnitude is None:
            self.candidate_baseline_magnitude = magnitude
            self.candidate_stability_confidence = 1.0
            return True
        previous = self.candidate_baseline_magnitude
        relative_step = abs(magnitude - previous) / max(1e-6, previous)
        stable = relative_step <= V2_MAG_ADAPTIVE_STABLE_STEP_RATIO
        if stable:
            alpha = self._alpha(
                timing.dt_seconds, V2_MAG_ADAPTIVE_CANDIDATE_SECONDS)
            self.candidate_baseline_magnitude += alpha * (
                magnitude - self.candidate_baseline_magnitude)
            self.candidate_stability_confidence = min(
                1.0,
                self.candidate_stability_confidence
                + timing.dt_seconds / V2_MAG_ADAPTIVE_CANDIDATE_SECONDS)
        else:
            self.candidate_baseline_magnitude = magnitude
            self.candidate_stability_confidence = 0.0
        return stable

    def _promote_adaptive_baseline(self, source: str) -> None:
        candidate = self.candidate_baseline_magnitude
        if candidate is None or candidate <= 1e-6:
            return
        self.baseline_magnitude = float(candidate)
        self.baseline_source = source
        self.baseline_epoch += 1
        self._baseline_reanchor_pending = True
        self._recovery_seconds = V2_MAG_RECOVERY_SECONDS
        self._last_magnitude = float(candidate)

    def reset(self) -> None:
        self.__init__()

    def evaluate(
        self,
        magnetometer_lsb: Sequence[float],
        timing: V2Timing,
        *,
        calibration_valid: bool,
        reference_magnitude_lsb: float | None = None,
    ) -> tuple[bool, dict]:
        mag = _vector3(magnetometer_lsb, "magnetometer_lsb")
        magnitude = math.sqrt(sum(value * value for value in mag))
        reasons = []
        if not calibration_valid:
            reasons.append("calibration-invalid")
        if not any(mag):
            reasons.append("magnetometer-zero")
        if not timing.integrate:
            reasons.append("timing")

        try:
            reference_magnitude = float(reference_magnitude_lsb)
        except (TypeError, ValueError):
            reference_magnitude = math.nan
        reference_valid = bool(
            calibration_valid and math.isfinite(reference_magnitude)
            and reference_magnitude > 1e-6)
        adaptive_observing = MAG_REFERENCE_MODEL in ("Shadow", "Adaptive")
        adaptive_active = MAG_REFERENCE_MODEL == "Adaptive"
        basic_valid = not reasons
        candidate_stable = False
        if basic_valid and adaptive_observing:
            candidate_stable = self._observe_adaptive_candidate(
                magnitude, timing)

        # Adaptive sessions use the first calibrated, non-zero measurement as
        # their local reference. Figure-8 data calibrates the sensor; it is not
        # treated as an immutable environmental field-strength baseline.
        if (adaptive_active and basic_valid
                and self.baseline_magnitude is None):
            self._promote_adaptive_baseline("session-bootstrap")
            self.magnitude_stability_confidence = 1.0
        elif self.baseline_magnitude is None and reference_valid:
            self.baseline_magnitude = reference_magnitude
            self.baseline_source = "figure-8-calibration"
            self._baseline_samples.clear()
            self._baseline_seconds = 0.0

        if not reasons and self.baseline_magnitude is None:
            if timing.status == "valid":
                self._baseline_samples.append(magnitude)
                self._baseline_seconds += timing.dt_seconds
            if (self._baseline_seconds >= V2_MAG_BASELINE_SECONDS
                    and len(self._baseline_samples) >= V2_MAG_BASELINE_MIN_SAMPLES):
                self.baseline_magnitude = statistics.median(self._baseline_samples)
                self.baseline_source = "session-acquisition"
                self._baseline_samples.clear()
            else:
                reasons.append("baseline-acquiring")

        ratio = None
        if self.baseline_magnitude:
            ratio = magnitude / self.baseline_magnitude
            magnitude_jump = bool(
                self._last_magnitude
                and abs(magnitude - self._last_magnitude)
                / self._last_magnitude > V2_MAG_JUMP_RATIO)
            ratio_outside = ratio < V2_MAG_RATIO_MIN or ratio > V2_MAG_RATIO_MAX
            if adaptive_active and basic_valid:
                baseline_promoted = False
                if magnitude_jump:
                    if (candidate_stable
                            and self.candidate_stability_confidence >= 1.0):
                        self._promote_adaptive_baseline(
                            "adaptive-environment")
                        ratio = magnitude / self.baseline_magnitude
                        baseline_promoted = True
                    else:
                        reasons.extend((
                            "magnitude-jump", "baseline-adapting"))
                if ratio_outside and not baseline_promoted:
                    if (candidate_stable
                            and self.candidate_stability_confidence >= 1.0):
                        self._promote_adaptive_baseline(
                            "adaptive-environment")
                        ratio = magnitude / self.baseline_magnitude
                        baseline_promoted = True
                    else:
                        if "baseline-adapting" not in reasons:
                            reasons.append("baseline-adapting")
                elif not magnitude_jump and not baseline_promoted:
                    alpha = self._alpha(
                        timing.dt_seconds, V2_MAG_ADAPTIVE_TRACK_SECONDS)
                    self.baseline_magnitude += alpha * (
                        magnitude - self.baseline_magnitude)
                    ratio = magnitude / self.baseline_magnitude
                self.magnitude_stability_confidence = (
                    self.candidate_stability_confidence)
            else:
                if ratio_outside:
                    reasons.append("magnitude-ratio")
                if magnitude_jump:
                    reasons.append("magnitude-jump")

        magnitude_valid = not reasons
        if magnitude_valid:
            self._recovery_seconds += timing.dt_seconds
            self._last_magnitude = magnitude
        else:
            if any(reason not in {"baseline-acquiring"} for reason in reasons):
                self.disturbance_count += 1
            self._recovery_seconds = 0.0

        recovering = bool(
            not adaptive_active and self.baseline_magnitude
            and self._recovery_seconds < V2_MAG_RECOVERY_SECONDS)
        use_magnetometer = magnitude_valid and not recovering
        if not use_magnetometer:
            self.fallback_count += 1
            self.direction_valid = False
            self._direction_recovery_seconds = 0.0
        return use_magnetometer, {
            "calibration_valid": bool(calibration_valid),
            "baseline_magnitude": self.baseline_magnitude,
            "baseline_source": self.baseline_source,
            "reference_model": MAG_REFERENCE_MODEL,
            "candidate_baseline_magnitude": self.candidate_baseline_magnitude,
            "candidate_stability_confidence": (
                self.candidate_stability_confidence),
            "magnitude_stability_confidence": (
                self.magnitude_stability_confidence),
            "baseline_epoch": self.baseline_epoch,
            "baseline_reanchor_pending": self._baseline_reanchor_pending,
            "reference_magnitude_lsb": (
                reference_magnitude if reference_valid else None),
            "baseline_samples": len(self._baseline_samples),
            "magnitude": magnitude,
            "magnitude_ratio": ratio,
            "magnitude_valid": magnitude_valid,
            "recovering": recovering,
            "probe_direction": False,
            "reasons": reasons,
            "recovery_seconds": self._recovery_seconds,
            "disturbance_count": self.disturbance_count,
            "fallback_count": self.fallback_count,
        }

    def evaluate_direction(self, magnetic_heading_deg: float,
                           current_heading_deg: float, timing: V2Timing,
                           *, angular_rate_dps: float = 0.0,
                           defer_initialization: bool = False) -> dict:
        raw_innovation = _wrap_degrees(
            magnetic_heading_deg - current_heading_deg)
        magnetic_heading_rate = current_heading_rate = 0.0
        if timing.dt_seconds > 0.0:
            if self._last_magnetic_heading is not None:
                magnetic_heading_rate = _wrap_degrees(
                    magnetic_heading_deg - self._last_magnetic_heading) / timing.dt_seconds
            if self._last_current_heading is not None:
                current_heading_rate = _wrap_degrees(
                    current_heading_deg - self._last_current_heading) / timing.dt_seconds
        self._last_magnetic_heading = float(magnetic_heading_deg)
        self._last_current_heading = float(current_heading_deg)
        motion_exited = False
        if V2_MAG_MOTION_AWARE_DIRECTION_ENABLED:
            if angular_rate_dps > V2_MAG_DIRECTION_CHECK_MAX_RATE_DPS:
                self._direction_motion_active = True
                self._direction_settling_seconds = 0.0
                self._post_motion_active = False
                self._post_motion_elapsed = 0.0
                self._post_motion_innovations.clear()
            elif self._direction_motion_active:
                if (angular_rate_dps <= V2_MAG_DIRECTION_MOTION_EXIT_RATE_DPS
                        and timing.status == "valid"):
                    self._direction_settling_seconds += timing.dt_seconds
                else:
                    self._direction_settling_seconds = 0.0
                if self._direction_settling_seconds >= V2_MAG_DIRECTION_SETTLING_SECONDS:
                    self._direction_motion_active = False
                    self._direction_settling_seconds = 0.0
                    motion_exited = True
        else:
            self._direction_motion_active = False
            self._direction_settling_seconds = 0.0
        # A deferred consumer starts in an arbitrary 6-axis yaw frame.  Anchor
        # magnetic north into that frame once, then keep the offset fixed.  This
        # prevents an initial north snap while preserving relative low-frequency
        # magnetic drift correction.
        reference_reanchored = bool(
            MAG_REFERENCE_MODEL == "Adaptive"
            and self._baseline_reanchor_pending
            and self.heading_initialized)
        if reference_reanchored:
            self._baseline_reanchor_pending = False
            self.heading_reference_offset_deg = _wrap_degrees(
                current_heading_deg - magnetic_heading_deg)
            self.direction_valid = True
            self._direction_recovery_seconds = V2_MAG_RECOVERY_SECONDS
            self._post_motion_active = False
            self._post_motion_elapsed = 0.0
            self._post_motion_innovations.clear()
            self._last_magnetic_heading = float(magnetic_heading_deg)
            self._last_current_heading = float(current_heading_deg)
        initialise = bool(
            not self.heading_initialized
            and (MAG_REFERENCE_MODEL == "Adaptive"
                 or not self._direction_motion_active))
        initialization_method = None
        if initialise:
            self.heading_initialized = True
            self.direction_valid = True
            self._direction_recovery_seconds = V2_MAG_RECOVERY_SECONDS
            self._baseline_reanchor_pending = False
            if defer_initialization:
                self.heading_reference_offset_deg = _wrap_degrees(
                    current_heading_deg - magnetic_heading_deg)
                initialization_method = "reference-offset"
            else:
                self.heading_reference_offset_deg = None
                initialization_method = "snap"

        if self.heading_reference_offset_deg is None:
            aligned_magnetic_heading = float(magnetic_heading_deg)
        else:
            aligned_magnetic_heading = _wrap_degrees(
                magnetic_heading_deg + self.heading_reference_offset_deg)
        innovation = _wrap_degrees(
            aligned_magnetic_heading - current_heading_deg)

        if (motion_exited and self.heading_initialized and not initialise
                and V2_POST_MOTION_GATE_MODE == "V2"):
            self._post_motion_active = True
            self._post_motion_elapsed = 0.0
            self._post_motion_innovations.clear()
        if self._post_motion_active:
            self._post_motion_innovations.append(innovation)
            if timing.status == "valid":
                self._post_motion_elapsed += timing.dt_seconds
            if self._post_motion_elapsed >= V2_POST_MOTION_OBSERVATION_SECONDS:
                radians = [math.radians(value) for value in self._post_motion_innovations]
                mean = math.degrees(math.atan2(
                    sum(math.sin(value) for value in radians),
                    sum(math.cos(value) for value in radians)))
                deviations = [_wrap_degrees(value - mean)
                              for value in self._post_motion_innovations]
                deviation_sd = math.sqrt(
                    sum(value * value for value in deviations) / len(deviations))
                self._post_motion_active = False
                if (abs(mean) <= V2_MAG_DIRECTION_RECOVERY_DEG
                        and deviation_sd <= V2_POST_MOTION_INNOVATION_SD_LIMIT_DEG):
                    self.direction_valid = True
                    self._direction_recovery_seconds = V2_MAG_RECOVERY_SECONDS
                else:
                    self.direction_valid = False
                    self._direction_recovery_seconds = 0.0
                post_motion_mean = mean
                post_motion_sd = deviation_sd
            else:
                post_motion_mean = None
                post_motion_sd = None
        else:
            post_motion_mean = None
            post_motion_sd = None
        motion_suspended = self._direction_motion_active or self._post_motion_active
        if initialise or reference_reanchored:
            # Do not reject the pre-snap innovation on the same frame.  A
            # reference-offset initialization already has zero aligned error;
            # Legacy snap applies its alignment immediately after this gate.
            pass
        elif motion_suspended:
            # A sample-time offset or tilt-compensation transient becomes a large
            # heading innovation during real rotation.  Do not interpret that as
            # magnetic interference and do not feed the unverified field to AHRS.
            # Preserve an already valid direction; an invalid direction may only
            # recover during a low-dynamic interval.
            if not self.direction_valid:
                self._direction_recovery_seconds = 0.0
        elif abs(innovation) > V2_MAG_DIRECTION_REJECTION_DEG:
            self.direction_valid = False
            self._direction_recovery_seconds = 0.0
        elif not self.direction_valid:
            if abs(innovation) <= V2_MAG_DIRECTION_RECOVERY_DEG and timing.status == "valid":
                self._direction_recovery_seconds += timing.dt_seconds
            else:
                self._direction_recovery_seconds = 0.0
            if self._direction_recovery_seconds >= V2_MAG_RECOVERY_SECONDS:
                self.direction_valid = True

        # A valid field-strength sample can remain direction-rejected forever
        # when the local magnetic frame changed while the old offset stayed
        # frozen. In Adaptive mode, accept a new local heading epoch only after
        # the rejected direction itself is stable during a low-motion period.
        # Reanchoring maps the current magnetic heading onto the current gyro
        # heading, so this operation applies zero correction and creates no
        # stored heading debt.
        direction_candidate_ready = False
        if (MAG_REFERENCE_MODEL == "Adaptive"
                and not self.direction_valid
                and not motion_suspended
                and timing.status == "valid"
                and timing.integrate
                and angular_rate_dps <= V2_MAG_DIRECTION_MOTION_EXIT_RATE_DPS):
            candidate = self._direction_candidate_innovation_deg
            if (candidate is None
                    or abs(_wrap_degrees(innovation - candidate))
                    > V2_MAG_ADAPTIVE_DIRECTION_STABILITY_DEG):
                self._direction_candidate_innovation_deg = innovation
                self._direction_candidate_seconds = 0.0
            else:
                alpha = min(1.0, max(0.0, timing.dt_seconds) / 0.25)
                self._direction_candidate_innovation_deg = _wrap_degrees(
                    candidate + alpha * _wrap_degrees(innovation - candidate))
                self._direction_candidate_seconds += max(
                    0.0, timing.dt_seconds)
            if (self._direction_candidate_seconds
                    >= V2_MAG_ADAPTIVE_DIRECTION_SECONDS):
                self.heading_reference_offset_deg = _wrap_degrees(
                    current_heading_deg - magnetic_heading_deg)
                aligned_magnetic_heading = float(current_heading_deg)
                innovation = 0.0
                self.direction_valid = True
                self._direction_recovery_seconds = V2_MAG_RECOVERY_SECONDS
                self._direction_candidate_innovation_deg = None
                self._direction_candidate_seconds = 0.0
                reference_reanchored = True
                direction_candidate_ready = True
        else:
            self._direction_candidate_innovation_deg = None
            self._direction_candidate_seconds = 0.0
        return {
            "magnetic_heading_deg": float(magnetic_heading_deg),
            "raw_magnetic_heading_deg": float(magnetic_heading_deg),
            "aligned_magnetic_heading_deg": aligned_magnetic_heading,
            "heading_reference_offset_deg": self.heading_reference_offset_deg,
            "current_heading_deg": float(current_heading_deg),
            "heading_innovation_deg": innovation,
            "raw_heading_innovation_deg": raw_innovation,
            "aligned_heading_innovation_deg": innovation,
            "heading_initialised_this_frame": initialise,
            "heading_reference_reanchored": reference_reanchored,
            "heading_initialization_method": initialization_method,
            "heading_initialized": self.heading_initialized,
            "direction_valid": self.direction_valid,
            "direction_recovery_seconds": self._direction_recovery_seconds,
            "direction_candidate_seconds": self._direction_candidate_seconds,
            "direction_candidate_ready": direction_candidate_ready,
            "direction_check_suspended": motion_suspended,
            "direction_angular_rate_dps": float(angular_rate_dps),
            "magnetic_heading_rate_dps": magnetic_heading_rate,
            "current_heading_rate_dps": current_heading_rate,
            "direction_motion_active": self._direction_motion_active,
            "direction_settling_seconds": self._direction_settling_seconds,
            "direction_state": (
                "motion" if self._direction_motion_active else
                "post-motion-observing" if self._post_motion_active else
                "direction-innovation" if not self.direction_valid
                and abs(innovation) > V2_MAG_DIRECTION_REJECTION_DEG else
                "direction-recovering" if not self.direction_valid else
                "valid"),
            "post_motion_gate_mode": V2_POST_MOTION_GATE_MODE,
            "post_motion_elapsed": self._post_motion_elapsed,
            "post_motion_samples": len(self._post_motion_innovations),
            "post_motion_innovation_mean_deg": post_motion_mean,
            "post_motion_innovation_sd_deg": post_motion_sd,
            "post_motion_shadow_candidate": (
                motion_exited and V2_POST_MOTION_GATE_MODE == "Shadow"),
            # Once north has been initialised, a magnetic fallback does not make
            # the gyro/accelerometer orientation disappear.  direction_valid is
            # the separate authority for injecting magnetometer measurements.
            "orientation_valid": (
                self.heading_initialized
                if V2_MAG_MOTION_AWARE_DIRECTION_ENABLED
                else self.heading_initialized and self.direction_valid
            ),
        }


class V2AhrsShadow:
    """V2 orientation estimator, independent of the Legacy Mahony fusion.

    Named "Shadow" from when it only ever observed; it now backs the V2 output
    path as well.  It still produces no output itself -- it only reports
    orientation and quality, and the caller decides what reaches the pad.
    """

    def __init__(
        self,
        ahrs,
        *,
        buffer_factory: Callable[[], object],
        settings_factory: Callable[..., object],
        convention,
        recovery_seconds: float = V2_RECOVERY_SECONDS,
        heading_function: Callable[[Sequence[float], Sequence[float]], float] | None = None,
    ):
        self.ahrs = ahrs
        # Three mutable 3-element buffers, allocated once and refilled in place.
        # This path runs on every input report, so it must not allocate per frame.
        self._gyro_buf = buffer_factory()
        self._accel_buf = buffer_factory()
        self._mag_buf = buffer_factory()
        self._heading_accel_buf = buffer_factory()
        self._settings_factory = settings_factory
        self._convention = convention
        self._recovery_seconds = float(recovery_seconds)
        self._heading_function = heading_function
        self._recovery_samples = 0
        self._rate_window = deque()
        self._rate_window_seconds = 0.0
        self._rate_update_elapsed = 0.0
        self.runtime_bias = StationaryRuntimeBias()
        self.magnetometer_gate = MagnetometerQualityGate()
        self.moving_yaw_bias_observer = MovingYawBiasObserver()
        self.motion_magnetic_closure = MotionMagneticClosureEstimator()
        self._heading_correction_ramp_elapsed = 0.0
        self._soft_iron_source = object()
        self._soft_iron_valid = False
        self._soft_iron_quality = {"reason": "not-configured"}
        self.reset_count = 0
        self._apply_settings(V2_DEFAULT_DT_SECONDS)

    def _reset_heading_correction_ramp(self) -> None:
        self._heading_correction_ramp_elapsed = 0.0

    def _heading_correction_ramp_progress(self) -> float:
        if V2_HEADING_CORRECTION_RAMP_SECONDS <= 0.0:
            return 1.0
        return min(
            1.0,
            self._heading_correction_ramp_elapsed
            / V2_HEADING_CORRECTION_RAMP_SECONDS,
        )

    def _apply_settings(self, dt_seconds: float) -> None:
        recovery_samples = max(500, min(1000, round(self._recovery_seconds / dt_seconds)))
        tolerance = max(1, round(self._recovery_samples * 0.05))
        if self._recovery_samples and abs(recovery_samples - self._recovery_samples) <= tolerance:
            return
        self.ahrs.settings = self._settings_factory(
            self._convention,
            V2_AHRS_GAIN,
            V2_GYRO_RANGE_DPS,
            V2_ACCEL_REJECTION_DEG,
            V2_MAG_REJECTION_DEG,
            recovery_samples,
        )
        self._recovery_samples = recovery_samples

    def _observe_sample_rate(self, timing: V2Timing) -> None:
        if timing.status != "valid":
            return
        dt = timing.dt_seconds
        self._rate_window.append(dt)
        self._rate_window_seconds += dt
        self._rate_update_elapsed += dt
        while self._rate_window and self._rate_window_seconds - self._rate_window[0] >= V2_RATE_WINDOW_SECONDS:
            self._rate_window_seconds -= self._rate_window.popleft()
        if self._rate_window_seconds < V2_RATE_UPDATE_SECONDS or self._rate_update_elapsed < V2_RATE_UPDATE_SECONDS:
            return
        self._rate_update_elapsed = 0.0
        self._apply_settings(statistics.median(self._rate_window))

    def update(
        self,
        frame: CanonicalSensorFrame,
        *,
        persistent_gyro_bias_dps: Sequence[float] = (0.0, 0.0, 0.0),
        magnetometer_bias_lsb: Sequence[float] = (0.0, 0.0, 0.0),
        movement_hint_dps: float | None = None,
        magnetometer_calibration_valid: bool = True,
        magnetometer_reference_magnitude_lsb: float | None = None,
        magnetometer_enabled: bool = True,
        soft_iron_matrix=None,
        soft_iron_model: str | None = None,
        orientation_consumer_active: bool = False,
        closure_reference_heading_deg: float | None = None,
        closure_reference_orientation_wxyz: Sequence[float] | None = None,
    ) -> dict:
        timing = frame.timing
        if timing.reset_estimator:
            self.ahrs.reset()
            self.runtime_bias.reset_stationary_state()
            self.magnetometer_gate.reset()
            self.moving_yaw_bias_observer.reset()
            self.motion_magnetic_closure.reset()
            self._reset_heading_correction_ramp()
            self.reset_count += 1
        if not timing.integrate:
            return self.snapshot(timing.status, "not-integrated")

        self._observe_sample_rate(timing)
        gyro_bias = _vector3(persistent_gyro_bias_dps, "persistent_gyro_bias_dps")
        mag_bias = _vector3(magnetometer_bias_lsb, "magnetometer_bias_lsb")
        gyro_after_persistent = tuple(frame.gyroscope_dps[i] - gyro_bias[i] for i in range(3))
        gyro, bias_state = self.runtime_bias.update(
            gyro_after_persistent,
            frame.accelerometer_g,
            timing,
            movement_hint_dps=movement_hint_dps,
        )
        accel = frame.accelerometer_g
        hard_iron_mag = tuple(
            frame.magnetometer_lsb[i] - mag_bias[i] for i in range(3))
        if soft_iron_matrix is not self._soft_iron_source:
            self._soft_iron_source = soft_iron_matrix
            self._soft_iron_valid, self._soft_iron_quality = (
                validate_soft_iron_matrix(soft_iron_matrix))
        soft_valid = self._soft_iron_valid
        soft_quality = self._soft_iron_quality
        if soft_valid:
            rows = soft_quality["matrix"]
            soft_mag = tuple(
                sum(rows[i][j] * hard_iron_mag[j] for j in range(3))
                for i in range(3))
        else:
            soft_mag = hard_iron_mag
        # Magnitude/ellipsoid fit quality alone cannot prove tilt-compensated
        # heading quality.  Keep every fitted 3x3 matrix in Shadow until a
        # direction-aware validation exists; only explicit SoftIron may apply.
        soft_auto_eligible = False
        soft_applied = soft_valid and MAG_CALIBRATION_MODEL == "SoftIron"
        mag = soft_mag if soft_applied else hard_iron_mag
        gyro_array = _fill_buffer(self._gyro_buf, gyro)
        accel_array = _fill_buffer(self._accel_buf, accel)
        mag_array = _fill_buffer(self._mag_buf, mag)
        if magnetometer_enabled:
            use_magnetometer, mag_quality = self.magnetometer_gate.evaluate(
                mag,
                timing,
                calibration_valid=magnetometer_calibration_valid,
                reference_magnitude_lsb=(
                    magnetometer_reference_magnitude_lsb),
            )
            mag_quality["calibration_model"] = MAG_CALIBRATION_MODEL
            mag_quality["soft_iron_valid"] = soft_valid
            mag_quality["soft_iron_applied"] = soft_applied
            mag_quality["soft_iron_model"] = soft_iron_model
            mag_quality["soft_iron_auto_eligible"] = soft_auto_eligible
            mag_quality["soft_iron_quality"] = soft_quality
            mag_quality["hard_iron_magnitude"] = math.sqrt(
                sum(value * value for value in hard_iron_mag))
            mag_quality["soft_iron_magnitude"] = math.sqrt(
                sum(value * value for value in soft_mag))
            mag_quality["hard_iron_vector_lsb"] = list(hard_iron_mag)
            mag_quality["soft_iron_vector_lsb"] = list(soft_mag)
            mag_quality["orientation_bin"] = _gravity_orientation_bin(accel)
        else:
            use_magnetometer = False
            mag_quality = {
                "calibration_valid": bool(magnetometer_calibration_valid),
                "magnitude_valid": False,
                "direction_valid": False,
                "recovering": False,
                "reasons": ["six-axis-selected"],
            }
        moving_yaw_bias = self.moving_yaw_bias_observer.snapshot()
        motion_closure = self.motion_magnetic_closure.snapshot()
        if use_magnetometer and self._heading_function is not None:
            heading_accel = accel_array
            if closure_reference_orientation_wxyz is not None:
                try:
                    reference_q = tuple(
                        float(value)
                        for value in closure_reference_orientation_wxyz)
                    if len(reference_q) != 4:
                        raise ValueError("reference quaternion length")
                    inverse_q = (
                        reference_q[0], -reference_q[1],
                        -reference_q[2], -reference_q[3])
                    stabilized_gravity = _quaternion_rotate_vector_wxyz(
                        inverse_q, (0.0, 0.0, 1.0))
                    heading_accel = _fill_buffer(
                        self._heading_accel_buf, stabilized_gravity)
                    mag_quality["heading_gravity_source"] = (
                        "six-axis-quaternion")
                    mag_quality["heading_gravity_body"] = list(
                        stabilized_gravity)
                except (TypeError, ValueError):
                    mag_quality["heading_gravity_source"] = (
                        "raw-accelerometer-fallback")
            else:
                mag_quality["heading_gravity_source"] = "raw-accelerometer"
            magnetic_heading = float(self._heading_function(
                heading_accel, mag_array))
            current_heading = _quaternion_heading_deg(self.ahrs.quaternion)
            angular_rate_dps = math.sqrt(sum(value * value for value in gyro))
            direction = self.magnetometer_gate.evaluate_direction(
                magnetic_heading, current_heading, timing,
                angular_rate_dps=angular_rate_dps,
                defer_initialization=(
                    orientation_consumer_active
                    and HEADING_INITIALIZATION_MODE == "Deferred"))
            quaternion = self.ahrs.quaternion
            orientation_wxyz = (
                float(quaternion.w), float(quaternion.x),
                float(quaternion.y), float(quaternion.z))
            world_gyro_dps = _quaternion_rotate_vector_wxyz(
                orientation_wxyz, gyro)
            world_yaw_rate_dps = world_gyro_dps[2]
            mag_quality.update(direction)
            moving_yaw_bias = self.moving_yaw_bias_observer.update(
                current_heading,
                direction["aligned_magnetic_heading_deg"],
                direction["current_heading_rate_dps"],
                accel,
                timing,
                magnetic_quality_valid=bool(
                    mag_quality.get("magnitude_valid", False)
                    and not mag_quality.get("recovering", False)),
            )
            if (MAG_REFERENCE_MODEL == "Adaptive"
                    and (direction.get("heading_initialised_this_frame", False)
                         or direction.get(
                             "heading_reference_reanchored", False))):
                # Establish a zero-error local epoch immediately. This clears
                # old heading debt without waiting for deliberate yaw motion.
                self.motion_magnetic_closure.reanchor()
            motion_closure = self.motion_magnetic_closure.update(
                current_heading,
                direction["aligned_magnetic_heading_deg"],
                direction["current_heading_rate_dps"],
                accel,
                timing,
                magnetic_quality_valid=bool(
                    mag_quality.get("magnitude_valid", False)
                    and not mag_quality.get("recovering", False)
                    and direction.get("heading_initialized", False)),
                magnetic_direction_valid=bool(
                    direction.get("direction_valid", False)),
                stationary_hint=bool(bias_state.get("stationary", False)),
                gyro_world_yaw_rate_dps=world_yaw_rate_dps,
                gyro_reference_heading_deg=closure_reference_heading_deg,
            )
            if direction["heading_initialised_this_frame"]:
                self._reset_heading_correction_ramp()
                initialization_method = direction[
                    "heading_initialization_method"]
                if initialization_method == "snap":
                    self.ahrs.heading = magnetic_heading
                self.ahrs.update_no_magnetometer(
                    gyro_array, accel_array, timing.dt_seconds)
                mag_quality["heading_initialization_method"] = initialization_method
                mag_quality["heading_initialization_mode"] = (
                    HEADING_INITIALIZATION_MODE)
                mag_quality["heading_initialization_delta_deg"] = _wrap_degrees(
                    magnetic_heading - current_heading)
                update_mode = "9-axis-initialized"
            elif direction["direction_check_suspended"]:
                self._reset_heading_correction_ramp()
                self.ahrs.update_no_magnetometer(
                    gyro_array, accel_array, timing.dt_seconds)
                update_mode = "6-axis-motion"
            elif direction["direction_valid"]:
                # Heading-only, explicitly low-frequency magnetic authority.
                # Never inject a synthetic rate into corrected_gyro_dps and do
                # not give the magnetometer authority over tilt/roll.
                heading_error = direction["aligned_heading_innovation_deg"]
                correction = _compute_heading_correction(
                    heading_error,
                    timing.dt_seconds,
                    self._heading_correction_ramp_progress(),
                )
                heading_step = correction["applied_step_deg"]
                self.ahrs.heading = current_heading + heading_step
                self.ahrs.update_no_magnetometer(
                    gyro_array, accel_array, timing.dt_seconds)
                self._heading_correction_ramp_elapsed = min(
                    V2_HEADING_CORRECTION_RAMP_SECONDS,
                    self._heading_correction_ramp_elapsed + timing.dt_seconds,
                )
                mag_quality["heading_controller"] = correction
                mag_quality["heading_error_deg"] = correction["heading_error_deg"]
                mag_quality["heading_correction_step_deg"] = heading_step
                mag_quality["heading_correction_rate_dps"] = correction[
                    "applied_rate_dps"]
                update_mode = "9-axis"
            else:
                self._reset_heading_correction_ramp()
                self.ahrs.update_no_magnetometer(
                    gyro_array, accel_array, timing.dt_seconds)
                update_mode = "6-axis-direction"
        elif use_magnetometer:
            moving_yaw_bias = self.moving_yaw_bias_observer.suspend(
                "heading-unavailable")
            motion_closure = self.motion_magnetic_closure.suspend(
                "heading-unavailable")
            self.ahrs.update(
                gyro_array, accel_array, mag_array, timing.dt_seconds)
            update_mode = "9-axis"
        else:
            moving_yaw_bias = self.moving_yaw_bias_observer.suspend(
                "magnetic-quality" if magnetometer_enabled
                else "six-axis-selected")
            motion_closure = self.motion_magnetic_closure.suspend(
                "magnetic-quality" if magnetometer_enabled
                else "six-axis-selected",
                preserve_bias=bool(
                    magnetometer_enabled
                    and MAG_REFERENCE_MODEL == "Adaptive"),
                dt_seconds=timing.dt_seconds)
            self._reset_heading_correction_ramp()
            self.ahrs.update_no_magnetometer(
                gyro_array, accel_array, timing.dt_seconds)
            update_mode = "6-axis"
        mag_quality["ahrs_ignored_after_update"] = bool(
            self.ahrs.internal_states.magnetometer_ignored)
        mag_quality.setdefault(
            "orientation_valid",
            ((not bool(self.ahrs.flags.initialising)) if not magnetometer_enabled else
             (self.magnetometer_gate.heading_initialized
              if V2_MAG_MOTION_AWARE_DIRECTION_ENABLED
              else self.magnetometer_gate.heading_initialized
              and self.magnetometer_gate.direction_valid)),
        )
        mag_quality["magnetometer_enabled"] = bool(magnetometer_enabled)
        result = self.snapshot(timing.status, update_mode, bias_state, mag_quality)
        result["moving_yaw_bias"] = moving_yaw_bias
        result["motion_magnetic_closure"] = motion_closure
        result["heading_assist"] = {
            "mode": "low-frequency" if magnetometer_enabled else "off",
            "controller_mode": (
                V2_HEADING_CONTROLLER_MODE if magnetometer_enabled else "off"),
            "active": update_mode in ("9-axis", "9-axis-initialized"),
            "motion_frozen": update_mode == "6-axis-motion",
            "quality_rejected": update_mode in (
                "6-axis", "6-axis-direction"),
            # Magnetic correction is confined to AHRS heading.  It is never
            # added to corrected_gyro_dps or a mouse velocity.
            "direct_rate_injection": False,
        }
        if magnetometer_enabled:
            result["heading_assist"]["ramp_progress"] = (
                self._heading_correction_ramp_progress())
        return result

    def snapshot(self, timing_status: str, update_mode: str,
                 bias_state: Mapping | None = None, mag_quality: Mapping | None = None) -> dict:
        quaternion = self.ahrs.quaternion
        internal = self.ahrs.internal_states
        flags = self.ahrs.flags
        return {
            "timing_status": str(timing_status),
            "update_mode": str(update_mode),
            "orientation_wxyz": [
                float(quaternion.w), float(quaternion.x),
                float(quaternion.y), float(quaternion.z),
            ],
            "recovery_seconds": self._recovery_seconds,
            "recovery_samples": self._recovery_samples,
            "reset_count": self.reset_count,
            "runtime_bias": dict(bias_state or {
                "runtime_bias_dps": list(self.runtime_bias.bias_dps),
                "stationary_seconds": self.runtime_bias.stationary_seconds,
                "stationary": False,
                "learning": False,
                "blocked_reasons": ["not-integrated"],
            }),
            "magnetometer_quality": dict(mag_quality or {}),
            "ahrs": {
                "acceleration_error_deg": float(internal.acceleration_error),
                "accelerometer_ignored": bool(internal.accelerometer_ignored),
                "acceleration_recovery_trigger": float(internal.acceleration_recovery_trigger),
                "magnetic_error_deg": float(internal.magnetic_error),
                "magnetometer_ignored": bool(internal.magnetometer_ignored),
                "magnetic_recovery_trigger": float(internal.magnetic_recovery_trigger),
                "initialising": bool(flags.initialising),
                "angular_rate_recovery": bool(flags.angular_rate_recovery),
            },
        }


def _vector3(values: Sequence[float], name: str) -> tuple[float, float, float]:
    try:
        vector = tuple(float(value) for value in values)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain three finite numbers") from exc
    if len(vector) != 3 or not all(math.isfinite(value) for value in vector):
        raise ValueError(f"{name} must contain three finite numbers")
    return vector


def _wrap_degrees(value: float) -> float:
    return (float(value) + 180.0) % 360.0 - 180.0


def _compute_heading_correction(
    heading_error_deg: float,
    dt_seconds: float,
    ramp_progress: float,
    *,
    controller_mode: str = V2_HEADING_CONTROLLER_MODE,
) -> dict:
    """Return Legacy and Phase 1-3 heading-only correction candidates.

    V2 uses proportional authority outside a continuous deadband.  Shadow
    records that candidate while applying the old fixed-rate controller.
    """
    error = _wrap_degrees(heading_error_deg)
    dt = max(0.0, float(dt_seconds))
    progress = max(0.0, min(1.0, float(ramp_progress)))
    legacy_max_step = V2_HEADING_CORRECTION_LEGACY_MAX_DPS * dt
    legacy_step = max(-legacy_max_step, min(legacy_max_step, error))
    legacy_rate = legacy_step / dt if dt > 0.0 else 0.0

    magnitude = abs(error)
    effective_error = 0.0
    if magnitude > V2_HEADING_CORRECTION_DEADBAND_DEG + 1e-12:
        effective_error = math.copysign(
            magnitude - V2_HEADING_CORRECTION_DEADBAND_DEG, error)
    effective_max_rate = V2_HEADING_CORRECTION_MAX_DPS * progress
    proposed_rate = max(
        -effective_max_rate,
        min(effective_max_rate, V2_HEADING_CORRECTION_KP * effective_error),
    )
    proposed_step = proposed_rate * dt
    applied_v2 = controller_mode == "V2"
    applied_rate = proposed_rate if applied_v2 else legacy_rate
    applied_step = proposed_step if applied_v2 else legacy_step
    return {
        "controller_mode": controller_mode,
        "heading_error_deg": error,
        "effective_heading_error_deg": effective_error,
        "deadband_active": effective_error == 0.0,
        "deadband_deg": V2_HEADING_CORRECTION_DEADBAND_DEG,
        "proportional_gain_per_second": V2_HEADING_CORRECTION_KP,
        "maximum_rate_dps": V2_HEADING_CORRECTION_MAX_DPS,
        "legacy_maximum_rate_dps": V2_HEADING_CORRECTION_LEGACY_MAX_DPS,
        "authority_level_dps": V2_HEADING_CORRECTION_MAX_DPS,
        "authority_phase": (
            "phase-7-evaluation" if V2_HEADING_CORRECTION_MAX_DPS == 0.75
            else "phase-4" if V2_HEADING_CORRECTION_MAX_DPS == 0.5
            else "phase-3-rollback"),
        "ramp_seconds": V2_HEADING_CORRECTION_RAMP_SECONDS,
        "ramp_progress": progress,
        "effective_maximum_rate_dps": effective_max_rate,
        "legacy_rate_dps": legacy_rate,
        "legacy_step_deg": legacy_step,
        "proposed_rate_dps": proposed_rate,
        "proposed_step_deg": proposed_step,
        "applied_rate_dps": applied_rate,
        "applied_step_deg": applied_step,
        "shadow_only": controller_mode == "Shadow",
    }


def quaternion_heading_deg(quaternion_wxyz: Sequence[float]) -> float:
    w, x, y, z = (float(value) for value in quaternion_wxyz)
    return math.degrees(math.atan2(w * z + x * y, 0.5 - y * y - z * z))


def _quaternion_heading_deg(quaternion) -> float:
    return quaternion_heading_deg((
        quaternion.w, quaternion.x, quaternion.y, quaternion.z))


def gyro_lsb_per_dps(*, is_pro_controller: bool) -> float:
    return S2_GYRO_LSB_PER_DPS_PRO if is_pro_controller else S2_GYRO_LSB_PER_DPS_JOYCON


def canonical_timing(dt_seconds: float | None) -> V2Timing:
    """Classify live/replay timing without feeding unsafe gaps to future V2 state."""
    if dt_seconds is None:
        return V2Timing(V2_DEFAULT_DT_SECONDS, "initial", True, False, False)
    try:
        dt = float(dt_seconds)
    except (TypeError, ValueError):
        dt = math.nan
    if not math.isfinite(dt) or dt <= 0.0:
        return V2Timing(0.0, "invalid", False, True, False)
    if dt > V2_RESET_GAP_SECONDS:
        return V2Timing(0.0, "resync", False, True, False)
    if dt < V2_MIN_DT_SECONDS:
        return V2Timing(V2_MIN_DT_SECONDS, "clamped-low", True, False, False)
    if dt > V2_MAX_DT_SECONDS:
        return V2Timing(V2_MAX_DT_SECONDS, "clamped-high", True, False, False)
    return V2Timing(dt, "valid", True, False, True)


def canonicalize_sensor_frame(
    accelerometer_lsb: Sequence[float],
    gyroscope_lsb: Sequence[float],
    magnetometer_lsb: Sequence[float],
    dt_seconds: float | None,
    *,
    is_pro_controller: bool,
) -> CanonicalSensorFrame:
    """Return a new canonical frame; the supplied Legacy values are untouched."""
    accel = _vector3(accelerometer_lsb, "accelerometer_lsb")
    gyro = _vector3(gyroscope_lsb, "gyroscope_lsb")
    mag = _vector3(magnetometer_lsb, "magnetometer_lsb")
    gyro_scale = gyro_lsb_per_dps(is_pro_controller=is_pro_controller)
    return CanonicalSensorFrame(
        accelerometer_g=tuple(value / S2_ACCEL_LSB_PER_G for value in accel),
        gyroscope_dps=tuple(value / gyro_scale for value in gyro),
        magnetometer_lsb=mag,
        gyro_lsb_per_dps=gyro_scale,
        timing=canonical_timing(dt_seconds),
    )


def canonicalize_replay_frame(frame: Mapping) -> CanonicalSensorFrame:
    family = str((frame.get("controller") or {}).get("family", "other"))
    if family not in {"pro", "joycon-left", "joycon-right"}:
        raise ValueError(f"unsupported controller family: {family!r}")
    return canonicalize_sensor_frame(
        frame["accelerometer"],
        frame["gyroscope"],
        frame["magnetometer"],
        frame.get("dt"),
        is_pro_controller=family == "pro",
    )

# =============================================================================
# Phase 0 diagnostics and replay recorder (observational; disabled by default)
# =============================================================================
REPLAY_FORMAT = "switch2connect.gyro-phase0"
REPLAY_SCHEMA_VERSION = 1
_STOP = object()


class ReplayFormatError(ValueError):
    """Raised when a replay file is not a supported Phase 0 recording."""


def _replay_vector3(value) -> list[float]:
    try:
        values = list(value)
    except (TypeError, ValueError):
        values = []
    if len(values) != 3:
        return [0.0, 0.0, 0.0]
    return [float(values[0]), float(values[1]), float(values[2])]


def _replay_vector2(value) -> list[float]:
    try:
        values = list(value)
    except (TypeError, ValueError):
        values = []
    if len(values) != 2:
        return [0.0, 0.0]
    return [float(values[0]), float(values[1])]


def _finite_number(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _controller_family(controller) -> str:
    for method_name, family in (
        ("is_pro_controller", "pro"),
        ("is_joycon_left", "joycon-left"),
        ("is_joycon_right", "joycon-right"),
    ):
        method = getattr(controller, method_name, None)
        try:
            if method is not None and method():
                return family
        except Exception:
            continue
    return "other"


def _controller_metadata(controller) -> dict:
    device = getattr(controller, "device", None)
    info = getattr(controller, "controller_info", None)
    return {
        "id": str(getattr(device, "address", "unknown") or "unknown"),
        "product_id": int(getattr(info, "product_id", 0) or 0),
        "family": _controller_family(controller),
        "hold_mode": str(getattr(controller, "hold_mode", "Vertical")),
        "merged": bool(getattr(controller, "is_merged", False)),
        "gyro_active": bool(getattr(controller, "gyro_active", True)),
    }


def make_header(created_utc: str | None = None) -> dict:
    """Return the versioned first record used by every replay file."""
    return {
        "record_type": "header",
        "format": REPLAY_FORMAT,
        "schema_version": REPLAY_SCHEMA_VERSION,
        "created_utc": created_utc or _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
        "clock": "time.perf_counter_ns",
        "units": {
            "raw_accelerometer": "sensor_lsb",
            "raw_gyroscope": "sensor_lsb",
            "raw_magnetometer": "sensor_lsb",
            "dt": "seconds",
            "processing": "nanoseconds",
        },
    }


def validate_replay_record(record: Mapping, *, require_sample: bool = False) -> None:
    """Validate the stable envelope needed by offline Legacy/V2 replay tools."""
    if not isinstance(record, Mapping):
        raise ReplayFormatError("replay record must be a JSON object")
    record_type = record.get("record_type")
    if require_sample and record_type != "sample":
        raise ReplayFormatError("expected a sample record")
    if record_type == "header":
        if record.get("format") != REPLAY_FORMAT:
            raise ReplayFormatError("unsupported replay format")
        if record.get("schema_version") != REPLAY_SCHEMA_VERSION:
            raise ReplayFormatError("unsupported replay schema version")
        return
    if record_type != "sample":
        raise ReplayFormatError(f"unsupported record_type: {record_type!r}")
    if record.get("schema_version") != REPLAY_SCHEMA_VERSION:
        raise ReplayFormatError("unsupported sample schema version")
    raw = record.get("raw_sensor")
    if not isinstance(raw, Mapping):
        raise ReplayFormatError("sample is missing raw_sensor")
    for key in ("accelerometer", "gyroscope", "magnetometer"):
        value = raw.get(key)
        if not isinstance(value, list) or len(value) != 3:
            raise ReplayFormatError(f"raw_sensor.{key} must contain three values")


def iter_replay_records(path, *, include_header: bool = False) -> Iterator[dict]:
    """Stream validated records from a Phase 0 JSONL recording."""
    header_seen = False
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                validate_replay_record(record)
            except (json.JSONDecodeError, ReplayFormatError) as exc:
                raise ReplayFormatError(f"line {line_number}: {exc}") from exc
            if not header_seen:
                if record.get("record_type") != "header":
                    raise ReplayFormatError("first replay record must be a header")
                header_seen = True
                if include_header:
                    yield record
                continue
            if record.get("record_type") == "header":
                raise ReplayFormatError(f"line {line_number}: duplicate header")
            yield record
    if not header_seen:
        raise ReplayFormatError("replay file is empty or missing its header")


def replay_sensor_frames(path) -> Iterator[dict]:
    """Yield the minimal, backend-independent input consumed by future replay engines."""
    for record in iter_replay_records(path):
        yield {
            "sequence": int(record["sequence"]),
            "controller": dict(record.get("controller") or {}),
            "dt": _finite_number((record.get("fusion") or {}).get("dt"), 0.0),
            "accelerometer": tuple(record["raw_sensor"]["accelerometer"]),
            "gyroscope": tuple(record["raw_sensor"]["gyroscope"]),
            "magnetometer": tuple(record["raw_sensor"]["magnetometer"]),
            "buttons": int((record.get("input") or {}).get("buttons", 0)),
        }


class GyroPhase0Recorder:
    """Bounded, non-blocking JSONL recorder for the input notification hot path."""

    MAG_TESTER_COLUMNS = (
        "Time_s", "Controller", "Controller_Type", "Mag_Calibration",
        "Mag_Status", "Magnitude", "Reference_Magnitude", "Magnitude_Ratio",
        "Direction_Valid", "Motion_State", "Gyro_Yaw_Rate_dps",
        "Correction_Law", "Model_Confidence", "Effective_Authority",
        "Correction_Eligible", "Robust_Estimate_dps",
        "Candidate_Correction_dps",
        "Pass_Applied_Correction_dps", "Pass_Session_Accumulated_deg",
        "Blocked_Reason",
    )
    MAG_TESTER_COLUMN_WIDTHS = (
        12, 18, 16, 18, 22, 12, 20, 17, 16, 20, 20, 18, 18, 20, 22, 22,
        26, 32, 30, None,
    )

    def __init__(self, enabled: bool = False, output_path=None, queue_size: int = 8192):
        self.enabled = bool(enabled)
        self.monitoring_enabled = False
        self.output_path = Path(output_path) if output_path else self._default_output_path()
        self._queue_size = max(16, int(queue_size))
        self._queue = queue.Queue(maxsize=self._queue_size)
        self._sequence = itertools.count(1)
        self._thread = None
        self._thread_lock = threading.Lock()
        self._closed = False
        self._dropped = 0
        self._writer_error = None
        self._text_mode = False
        self._recording_metadata = {}
        self._recording_started_ns = None
        self._latest_lock = threading.Lock()
        self._latest_snapshots = {}

    @staticmethod
    def _default_output_path() -> Path:
        configured = (os.environ.get("SWITCH2_GYRO_PHASE0_PATH") or "").strip()
        if configured:
            return Path(configured)
        stamp = _datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        return Path.cwd() / "logs" / f"gyro_phase0_{stamp}.jsonl"

    @property
    def dropped_records(self) -> int:
        return self._dropped

    @property
    def writer_error(self):
        return self._writer_error

    @property
    def is_recording(self) -> bool:
        return bool(self.enabled and not self._closed)

    def set_monitoring(self, enabled: bool) -> None:
        if not self._closed:
            self.monitoring_enabled = bool(enabled)

    def latest_snapshots(self) -> dict:
        with self._latest_lock:
            return {key: dict(value)
                    for key, value in self._latest_snapshots.items()}

    def start_recording(self, output_path, *, text_mode: bool = False,
                        metadata: Mapping | None = None) -> bool:
        """Start a new runtime recording session without blocking sensor input."""
        with self._thread_lock:
            if self._closed or self.enabled or (
                    self._thread is not None and self._thread.is_alive()):
                return False
            self.output_path = Path(output_path)
            self._queue = queue.Queue(maxsize=self._queue_size)
            self._sequence = itertools.count(1)
            self._dropped = 0
            self._writer_error = None
            self._text_mode = bool(text_mode)
            self._recording_metadata = dict(metadata or {})
            self._recording_started_ns = time.perf_counter_ns()
            self.enabled = True
        if not self._ensure_writer():
            self.enabled = False
            return False
        return True

    def stop_recording(self, timeout: float = 5.0):
        """Stop accepting records, flush the writer, and return the output path."""
        self.enabled = False
        thread = self._thread
        if thread is None:
            return self.output_path
        try:
            self._queue.put(_STOP, timeout=max(0.0, timeout))
        except queue.Full:
            self._writer_error = RuntimeError(
                "Unable to stop Mag Tester writer because its queue is full")
            return self.output_path
        thread.join(timeout=max(0.0, timeout))
        if thread.is_alive():
            self._writer_error = RuntimeError(
                "Mag Tester writer did not finish before timeout")
        else:
            with self._thread_lock:
                if self._thread is thread:
                    self._thread = None
        return self.output_path

    def _ensure_writer(self) -> bool:
        if not self.enabled or self._closed:
            return False
        if self._thread is not None:
            return True
        with self._thread_lock:
            if self._thread is None:
                try:
                    self._thread = threading.Thread(
                        target=self._writer_loop,
                        daemon=True,
                        name="GyroPhase0Writer",
                    )
                    self._thread.start()
                except Exception as exc:
                    # Diagnostics must never take down the controller callback.
                    self._thread = None
                    self._writer_error = exc
                    self.enabled = False
                    logger.error("Unable to start Gyro Phase 0 writer: %s", exc)
                    return False
        return True

    def begin_sample(self, controller, input_data):
        if (not self.enabled and not self.monitoring_enabled) or self._closed:
            return None
        if self.enabled and not self._ensure_writer():
            return None
        started_ns = time.perf_counter_ns()
        return {
            "record_type": "sample",
            "schema_version": REPLAY_SCHEMA_VERSION,
            "sequence": next(self._sequence),
            "host_monotonic_ns": started_ns,
            "controller": _controller_metadata(controller),
            "raw_sensor": {
                "accelerometer": _replay_vector3(getattr(input_data, "accelerometer", (0, 0, 0))),
                "gyroscope": _replay_vector3(getattr(input_data, "gyroscope", (0, 0, 0))),
                "magnetometer": _replay_vector3(getattr(input_data, "magnometer", (0, 0, 0))),
                "sensor_time": int(getattr(input_data, "time", 0) or 0),
            },
            "input": {
                "buttons": int(getattr(input_data, "buttons", 0) or 0),
                "left_stick": _replay_vector2(getattr(input_data, "left_stick", (0, 0))),
                "right_stick": _replay_vector2(getattr(input_data, "right_stick", (0, 0))),
                "left_trigger": int(getattr(input_data, "left_trigger", 0) or 0),
                "right_trigger": int(getattr(input_data, "right_trigger", 0) or 0),
            },
            "processing": {"started_ns": started_ns},
        }

    def capture_fusion(self, trace, controller, dt, settings: Mapping | None = None) -> None:
        if trace is None or trace.get("_finished"):
            return
        ahrs = getattr(controller, "ahrs", None)
        internal = getattr(ahrs, "internal_states", None)
        flags = getattr(ahrs, "flags", None)
        trace["fusion"] = {
            "captured_ns": time.perf_counter_ns(),
            "dt": _finite_number(dt, 0.0),
            "orientation_wxyz": _replay_vector3((0, 0, 0)) + [0.0],
            "static_gyro_bias_lsb": _replay_vector3(getattr(controller, "gyro_bias", (0, 0, 0))),
            "dynamic_gyro_bias_rad_s": _replay_vector3(getattr(controller, "gyro_bias_integral", (0, 0, 0))),
            "mag_bias_lsb": _replay_vector3(getattr(controller, "mag_bias", (0, 0, 0))),
            "moving_envelope_dps": _finite_number(getattr(controller, "gyro_moving_envelope", 0.0)),
            "settings": dict(settings or {}),
            "ahrs": {
                "acceleration_error_deg": _finite_number(getattr(internal, "acceleration_error", 0.0)),
                "accelerometer_ignored": bool(getattr(internal, "accelerometer_ignored", False)),
                "acceleration_recovery_trigger": _finite_number(getattr(internal, "acceleration_recovery_trigger", 0.0)),
                "magnetic_error_deg": _finite_number(getattr(internal, "magnetic_error", 0.0)),
                "magnetometer_ignored": bool(getattr(internal, "magnetometer_ignored", False)),
                "magnetic_recovery_trigger": _finite_number(getattr(internal, "magnetic_recovery_trigger", 0.0)),
                "initialising": bool(getattr(flags, "initialising", False)),
                "angular_rate_recovery": bool(getattr(flags, "angular_rate_recovery", False)),
            },
        }
        try:
            orientation = list(getattr(controller, "orientation"))
            if len(orientation) == 4:
                trace["fusion"]["orientation_wxyz"] = [float(value) for value in orientation]
        except Exception:
            pass

    def capture_v2_canonical(self, trace, canonical_frame: Mapping) -> None:
        """Attach Phase 1 Shadow data without changing the version-1 envelope."""
        if trace is None or trace.get("_finished"):
            return
        trace["v2_canonical"] = dict(canonical_frame)

    def capture_v2_fusion(self, trace, fusion_snapshot: Mapping) -> None:
        """Attach independent Phase 2 AHRS state to an opt-in diagnostic sample."""
        if trace is None or trace.get("_finished"):
            return
        trace["v2_fusion"] = dict(fusion_snapshot)

    def capture_pre_horizon(self, trace, controller, input_data) -> None:
        if trace is None or trace.get("_finished"):
            return
        trace["pre_horizon"] = {
            "captured_ns": time.perf_counter_ns(),
            "gyroscope": _replay_vector3(getattr(input_data, "gyroscope", (0, 0, 0))),
            "accelerometer": _replay_vector3(getattr(input_data, "accelerometer", (0, 0, 0))),
            "gyro_mouse_enabled": bool(getattr(controller, "gyro_mouse_enabled", False)),
            "gyro_target_velocity": [
                _finite_number(getattr(controller, "gyro_target_vx", 0.0)),
                _finite_number(getattr(controller, "gyro_target_vy", 0.0)),
            ],
            "gyro_rstick_output": _replay_vector2(getattr(controller, "_gyro_rstick_out", (0, 0))),
            "in_app_horizon": dict(getattr(
                controller, "_in_app_v2_metadata", {}) or {}),
        }

    def capture_v2_output(self, trace, candidate: Mapping,
                          legacy_candidate: Mapping | None = None) -> None:
        """Attach the Phase 5/6 candidate; actual output remains independently selected."""
        if trace is None or trace.get("_finished"):
            return
        trace["v2_output"] = dict(candidate)
        if isinstance(legacy_candidate, Mapping) and legacy_candidate.get("available"):
            trace["legacy_candidate_output"] = dict(legacy_candidate)

    def finish_sample(self, trace, controller, input_data, status: str = "normal") -> None:
        if trace is None or trace.get("_finished"):
            return
        trace["_finished"] = True
        finished_ns = time.perf_counter_ns()
        selected_output = {
            "gyroscope": _replay_vector3(getattr(input_data, "gyroscope", (0, 0, 0))),
            "accelerometer": _replay_vector3(getattr(input_data, "accelerometer", (0, 0, 0))),
            "buttons": int(getattr(input_data, "buttons", 0) or 0),
            "left_stick": _replay_vector2(getattr(input_data, "left_stick", (0, 0))),
            "right_stick": _replay_vector2(getattr(input_data, "right_stick", (0, 0))),
        }
        trace["selected_output"] = dict(selected_output)
        legacy_candidate = trace.get("legacy_candidate_output")
        trace["legacy_output"] = dict(selected_output)
        if isinstance(legacy_candidate, Mapping) and legacy_candidate.get("available"):
            trace["legacy_output"]["gyroscope"] = _replay_vector3(
                legacy_candidate.get("gyroscope", (0, 0, 0)))
        candidate = trace.get("v2_output")
        if isinstance(candidate, Mapping) and candidate.get("available"):
            try:
                if candidate.get("applied_to_output"):
                    candidate["applied_gyroscope"] = list(
                        selected_output["gyroscope"])
                    candidate["applied_accelerometer"] = list(
                        selected_output["accelerometer"])
                v2_gyro = _replay_vector3(candidate.get(
                    "applied_gyroscope" if candidate.get("applied_to_output")
                    else "target_gyroscope",
                    candidate.get("gyroscope", (0, 0, 0))))
                legacy_gyro = trace["legacy_output"]["gyroscope"]
                difference = [v2_gyro[index] - legacy_gyro[index] for index in range(3)]
                trace["v2_comparison"] = {
                    "gyroscope_difference": difference,
                    "difference_magnitude": math.sqrt(sum(value * value for value in difference)),
                    "legacy_selected": not bool(candidate.get("applied_to_output")),
                }
            except (TypeError, ValueError):
                trace["v2_comparison"] = {"error": "invalid-v2-candidate"}
        processing = trace.setdefault("processing", {})
        processing["finished_ns"] = finished_ns
        processing["elapsed_ns"] = max(0, finished_ns - int(processing.get("started_ns", finished_ns)))
        processing["status"] = str(status)
        queued_record = dict(trace)
        queued_record.pop("_finished", None)
        snapshot = self._mag_tester_snapshot(queued_record)
        controller_id = snapshot["controller_id"]
        with self._latest_lock:
            self._latest_snapshots[controller_id] = snapshot
        if not self.enabled:
            return
        try:
            self._queue.put_nowait(queued_record)
        except queue.Full:
            self._dropped += 1

    @staticmethod
    def _number(value, default=0.0) -> float:
        try:
            number = float(value)
            return number if math.isfinite(number) else float(default)
        except (TypeError, ValueError):
            return float(default)

    def _mag_tester_snapshot(self, record: Mapping) -> dict:
        controller = dict(record.get("controller") or {})
        fusion = dict(record.get("v2_fusion") or {})
        quality = dict(fusion.get("magnetometer_quality") or {})
        closure = dict(fusion.get("motion_magnetic_closure") or {})
        pass_closure = dict(
            (record.get("v2_output") or {}).get(
                "motion_magnetic_closure") or {})
        in_app = dict(
            (record.get("pre_horizon") or {}).get("in_app_horizon") or {})
        in_app_closure = dict(in_app.get("motion_magnetic_closure") or {})
        calibration_accepted = bool(
            quality.get("calibration_valid")
            and quality.get("baseline_magnitude") is not None)
        magnitude_valid = bool(quality.get("magnitude_valid"))
        direction_valid = bool(quality.get("direction_valid"))
        recovering = bool(quality.get("recovering"))
        quality_reasons = [str(value) for value in
                           (quality.get("reasons") or [])]
        if not calibration_accepted:
            mag_status = "Calibration Missing"
        elif "baseline-adapting" in quality_reasons:
            mag_status = "Adapting Baseline"
        elif not magnitude_valid:
            mag_status = "Interference Detected"
        elif recovering:
            mag_status = "Recovering"
        elif not direction_valid:
            mag_status = "Direction Rejected"
        else:
            mag_status = "Valid"
        blocked = [str(value) for value in
                   (closure.get("blocked_reasons") or [])]
        controller_id = str(controller.get("id") or "Unknown")
        return {
            "host_monotonic_ns": int(record.get("host_monotonic_ns") or 0),
            "controller_id": controller_id,
            "controller_type": str(controller.get("family") or "unknown"),
            "calibration_accepted": calibration_accepted,
            "mag_status": mag_status,
            "calibration_model": quality.get("calibration_model"),
            "magnitude": self._number(quality.get("magnitude")),
            "reference_magnitude": self._number(
                quality.get("baseline_magnitude"),
                quality.get("reference_magnitude_lsb") or 0.0),
            "magnitude_ratio": self._number(quality.get("magnitude_ratio")),
            "direction_valid": direction_valid,
            "heading_error_deg": self._number(
                closure.get("relative_closure_error_deg")),
            "gyro_relative_heading_deg": self._number(
                closure.get("gyro_relative_heading_deg")),
            "magnetic_relative_heading_deg": self._number(
                closure.get("magnetic_relative_heading_deg")),
            "raw_magnetic_heading_delta_deg": self._number(
                closure.get("raw_magnetic_heading_delta_deg")),
            "normalized_magnetic_heading_delta_deg": self._number(
                closure.get("normalized_magnetic_heading_delta_deg")),
            "gyro_world_yaw_delta_deg": self._number(
                closure.get("gyro_world_yaw_delta_deg")),
            "gyro_reference_heading_delta_deg": self._number(
                closure.get("gyro_reference_heading_delta_deg")),
            "heading_delta_direction_agreement": bool(
                closure.get("heading_delta_direction_agreement", True)),
            "absolute_heading_innovation_deg": self._number(
                quality.get("aligned_heading_innovation_deg")),
            "motion_state": str(closure.get("motion_state") or "Unavailable"),
            "gyro_yaw_rate_dps": self._number(
                closure.get("filtered_rate_dps")),
            "confidence": self._number(
                closure.get("effective_confidence")),
            "correction_eligible": bool(closure.get("correction_eligible")),
            "correction_law": str(
                closure.get("correction_law") or "Unavailable"),
            "raw_correction_demand_dps": self._number(
                closure.get("raw_proportional_target_dps")),
            "confidence_target_dps": self._number(
                closure.get("confidence_weighted_target_dps")),
            "dynamic_safety_cap_dps": self._number(
                closure.get("dynamic_safety_cap_dps")),
            "attack_slew_dps_per_second": self._number(
                closure.get("attack_slew_dps_per_second")),
            "limiting_reason": str(
                closure.get("limiting_reason") or "Unavailable"),
            "candidate_correction_dps": self._number(
                closure.get("candidate_rate_dps")),
            "pass_applied_correction_dps": self._number(
                pass_closure.get("applied_rate_dps")),
            "in_app_applied_correction_dps": self._number(
                in_app_closure.get("applied_rate_dps")),
            "pass_consumer_residual_deg": self._number(
                pass_closure.get("consumer_residual_deg")),
            "pass_accumulated_correction_deg": self._number(
                pass_closure.get("accumulated_applied_correction_deg")),
            "pass_reentry_ramp_progress": self._number(
                pass_closure.get("consumer_reentry_ramp_progress")),
            "in_app_consumer_residual_deg": self._number(
                in_app_closure.get("consumer_residual_deg")),
            "in_app_accumulated_correction_deg": self._number(
                in_app_closure.get("accumulated_applied_correction_deg")),
            "pass_session_accumulated_correction_deg": self._number(
                pass_closure.get(
                    "session_accumulated_applied_correction_deg")),
            "in_app_session_accumulated_correction_deg": self._number(
                in_app_closure.get(
                    "session_accumulated_applied_correction_deg")),
            "in_app_reentry_ramp_progress": self._number(
                in_app_closure.get("consumer_reentry_ramp_progress")),
            "raw_relative_closure_error_deg": self._number(
                closure.get("raw_relative_closure_error_deg")),
            "filtered_relative_closure_error_deg": self._number(
                closure.get("filtered_relative_closure_error_deg")),
            "consumer_measurement_deg": self._number(
                closure.get("consumer_measurement_deg")),
            "relative_delta_innovation_deg": self._number(
                closure.get("relative_delta_innovation_deg")),
            "relative_delta_rejected": bool(
                closure.get("relative_delta_rejected", False)),
            "pass_invariant_error_deg": self._number(
                pass_closure.get("consumer_feedback_invariant_error_deg")),
            "pass_invariant_valid": bool(
                pass_closure.get("consumer_feedback_invariant_valid", True)),
            "in_app_invariant_error_deg": self._number(
                in_app_closure.get("consumer_feedback_invariant_error_deg")),
            "in_app_invariant_valid": bool(
                in_app_closure.get("consumer_feedback_invariant_valid", True)),
            "measurement_epoch": int(closure.get("measurement_epoch", 0) or 0),
            "consistency_correlation": self._number(
                closure.get("consistency_correlation")),
            "consistency_scale": self._number(
                closure.get("consistency_scale")),
            "consistency_confidence": self._number(
                closure.get("consistency_confidence")),
            "confidence_state": (
                "Reference Ready"
                if (quality.get("reference_model") == "Adaptive"
                    and str(closure.get("confidence_state"))
                    == "Waiting for Motion")
                else str(closure.get("confidence_state") or "Unavailable")),
            "authority_suspended": bool(
                closure.get("authority_suspended", False)),
            "observed_bias_rate_dps": self._number(
                closure.get("observed_bias_rate_dps")),
            "bias_rate_valid": bool(
                closure.get("bias_rate_valid", False)),
            "commit_scale_valid": bool(
                closure.get("commit_scale_valid", False)),
            "committed_error_increment_deg": self._number(
                closure.get("committed_error_increment_deg")),
            "invalid_confidence_debt_seconds": self._number(
                closure.get("invalid_confidence_debt_seconds")),
            "frozen_correction_blocked": bool(
                closure.get("frozen_correction_blocked", False)),
            "pass_repayment_budget_remaining_deg": self._number(
                pass_closure.get("repayment_budget_remaining_deg")),
            "pass_repayment_budget_used_deg": self._number(
                pass_closure.get("repayment_budget_used_deg")),
            "in_app_repayment_budget_remaining_deg": self._number(
                in_app_closure.get("repayment_budget_remaining_deg")),
            "in_app_repayment_budget_used_deg": self._number(
                in_app_closure.get("repayment_budget_used_deg")),
            "robust_bias_raw_observation_dps": self._number(
                closure.get("robust_bias_raw_observation_dps")),
            "robust_bias_bounded_observation_dps": self._number(
                closure.get("robust_bias_bounded_observation_dps")),
            "robust_bias_estimate_dps": self._number(
                closure.get("robust_bias_estimate_dps")),
            "robust_bias_valid_buckets": int(
                closure.get("robust_bias_valid_buckets", 0) or 0),
            "robust_bias_observation_age_seconds": self._number(
                closure.get("robust_bias_observation_age_seconds")),
            "robust_bias_confidence": self._number(
                closure.get("robust_bias_confidence")),
            "robust_bias_observation_clipped": bool(
                closure.get("robust_bias_observation_clipped", False)),
            "robust_bias_decay_active": bool(
                closure.get("robust_bias_decay_active", False)),
            "transient_event_active": bool(
                closure.get("transient_event_active", False)),
            "transient_event_error_deg": self._number(
                closure.get("transient_event_error_deg")),
            "transient_validation_elapsed_seconds": self._number(
                closure.get("transient_validation_elapsed_seconds")),
            "transient_confidence": self._number(
                closure.get("transient_confidence")),
            "transient_commit_increment_deg": self._number(
                closure.get("transient_commit_increment_deg")),
            "pass_transient_debt_remaining_deg": self._number(
                pass_closure.get("transient_debt_remaining_deg")),
            "pass_transient_debt_used_deg": self._number(
                pass_closure.get("transient_debt_used_deg")),
            "in_app_transient_debt_remaining_deg": self._number(
                in_app_closure.get("transient_debt_remaining_deg")),
            "in_app_transient_debt_used_deg": self._number(
                in_app_closure.get("transient_debt_used_deg")),
            "rolling_consistency_confidence": self._number(
                closure.get("rolling_consistency_confidence")),
            "aggregate_consistency_confidence": self._number(
                closure.get("aggregate_consistency_confidence")),
            "consistency_samples": int(
                closure.get("consistency_samples", 0) or 0),
            "heading_gravity_source": str(
                quality.get("heading_gravity_source") or "Unavailable"),
            "residual_state": str(
                closure.get("residual_state") or "Unavailable"),
            "window_accepted": bool(
                closure.get("window_accepted", False)),
            "window_magnetic_delta_deg": self._number(
                closure.get("window_magnetic_delta_deg")),
            "window_gyro_delta_deg": self._number(
                closure.get("window_gyro_delta_deg")),
            "committed_heading_error_deg": self._number(
                closure.get("committed_heading_error_deg")),
            "pass_epoch_changed": bool(
                pass_closure.get("consumer_epoch_changed", False)),
            "in_app_epoch_changed": bool(
                in_app_closure.get("consumer_epoch_changed", False)),
            "blocked_reasons": blocked,
        }

    @staticmethod
    def _text_value(value) -> str:
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if isinstance(value, float):
            return f"{value:.6f}"
        return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")

    @classmethod
    def _fixed_width_text_line(cls, values) -> str:
        cells = []
        for value, width in zip(values, cls.MAG_TESTER_COLUMN_WIDTHS):
            text = cls._text_value(value)
            if width is None:
                cells.append(text)
                continue
            if len(text) > width:
                text = text[:max(0, width - 3)] + "..."
            cells.append(text.ljust(width))
        return "  ".join(cells).rstrip()

    def _mag_tester_row(self, record: Mapping) -> str:
        item = self._mag_tester_snapshot(record)
        start_ns = self._recording_started_ns or item["host_monotonic_ns"]
        values = (
            max(0.0, (item["host_monotonic_ns"] - start_ns) / 1e9),
            item["controller_id"], item["controller_type"],
            "Accepted" if item["calibration_accepted"] else "Not Available",
            item["mag_status"], item["magnitude"], item["reference_magnitude"],
            item["magnitude_ratio"], item["direction_valid"],
            item["motion_state"], item["gyro_yaw_rate_dps"],
            item["correction_law"],
            (item["robust_bias_confidence"]
             if item["correction_law"] == "RobustBiasRate"
             else item["consistency_confidence"]),
            item["confidence"], item["correction_eligible"],
            item["robust_bias_estimate_dps"],
            item["candidate_correction_dps"],
            item["pass_applied_correction_dps"],
            item["pass_session_accumulated_correction_deg"],
            ",".join(item["blocked_reasons"]) or "None",
        )
        return self._fixed_width_text_line(values)

    def _writer_loop(self) -> None:
        try:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            with self.output_path.open("w", encoding="utf-8", newline="\n") as handle:
                if self._text_mode:
                    handle.write("# Switch2Connect Mag Tester Recording\n")
                    for key, value in self._recording_metadata.items():
                        handle.write(
                            f"# {self._text_value(key)}: {self._text_value(value)}\n")
                    handle.write(
                        self._fixed_width_text_line(self.MAG_TESTER_COLUMNS)
                        + "\n")
                else:
                    handle.write(json.dumps(make_header(), separators=(",", ":"), ensure_ascii=False) + "\n")
                pending_flush = 0
                last_flush = time.monotonic()
                written = 0
                while True:
                    item = self._queue.get()
                    if item is _STOP:
                        break
                    if self._text_mode:
                        handle.write(self._mag_tester_row(item) + "\n")
                    else:
                        handle.write(json.dumps(item, separators=(",", ":"), ensure_ascii=False) + "\n")
                    written += 1
                    pending_flush += 1
                    now = time.monotonic()
                    if pending_flush >= 128 or now - last_flush >= 1.0:
                        handle.flush()
                        pending_flush = 0
                        last_flush = now
                if self._text_mode:
                    handle.write(f"# Samples: {written}\n")
                    handle.write(f"# Dropped_Records: {self._dropped}\n")
                    handle.write(
                        f"# Recording_Stopped_UTC: {_datetime.datetime.now(_datetime.timezone.utc).isoformat()}\n")
                handle.flush()
        except Exception as exc:
            self._writer_error = exc
            logger.error("Gyro Phase 0 writer stopped: %s", exc)

    def close(self, timeout: float = 2.0) -> None:
        if self._closed:
            return
        self.stop_recording(timeout)
        self.monitoring_enabled = False
        self._closed = True


def _recorder_from_environment() -> GyroPhase0Recorder:
    enabled = (os.environ.get("SWITCH2_GYRO_PHASE0") or "0").strip() == "1"
    try:
        queue_size = int(os.environ.get("SWITCH2_GYRO_PHASE0_QUEUE", "8192"))
    except ValueError:
        queue_size = 8192
    return GyroPhase0Recorder(enabled=enabled, queue_size=queue_size)


GYRO_PHASE0_RECORDER = _recorder_from_environment()
atexit.register(GYRO_PHASE0_RECORDER.close)
__all__ = [
    "CanonicalSensorFrame", "GYRO_PHASE0_RECORDER", "GyroPhase0Recorder",
    "MAG_REFERENCE_MODEL", "MAG_REFERENCE_MODELS",
    "PRODUCTION_MAG_REFERENCE_MODEL",
    "MagnetometerQualityGate", "REPLAY_FORMAT", "REPLAY_SCHEMA_VERSION",
    "ReplayFormatError", "S2_ACCEL_LSB_PER_G", "S2_GYRO_LSB_PER_DPS_JOYCON",
    "S2_GYRO_LSB_PER_DPS_PRO", "StationaryRuntimeBias", "V2AhrsShadow",
    "V2Timing", "V2_OUTPUT_MODES", "PASSTHROUGH_HEADING_OUTPUT_MODES",
    "PASSTHROUGH_MOVING_YAW_BIAS_MODES", "MovingYawBiasObserver",
    "MOTION_MAG_CLOSURE_MODES", "MOTION_MAG_CORRECTION_LAWS",
    "PRODUCTION_MOTION_MAG_CORRECTION_LAW",
    "PRODUCTION_INAPP_MOTION_MAG_CLOSURE_MODE",
    "PRODUCTION_PASSTHROUGH_MOTION_MAG_CLOSURE_MODE",
    "MOTION_MAG_CORRECTION_LAW", "INAPP_MOTION_MAG_CLOSURE_MODE",
    "PASSTHROUGH_MOTION_MAG_CLOSURE_MODE",
    "MotionMagneticClosureEstimator", "MotionMagneticClosureConsumer",
    "PassthroughHeadingOutputFilter", "PassthroughMovingYawBiasConsumer",
    "apply_passthrough_heading_correction", "build_v2_accelerometer_output",
    "apply_world_yaw_bias_correction",
    "apply_world_yaw_rate_correction", "select_motion_magnetic_closure",
    "apply_soft_iron_matrix", "build_in_app_horizon_output",
    "build_v2_gyro_output", "canonical_timing", "canonicalize_replay_frame",
    "canonicalize_sensor_frame", "gyro_lsb_per_dps", "iter_replay_records",
    "make_header", "replay_sensor_frames", "validate_replay_record",
    "validate_soft_iron_matrix", "quaternion_heading_deg",
]
