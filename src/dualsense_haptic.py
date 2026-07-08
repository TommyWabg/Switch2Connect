# Switch2Connect - A Python and ESP32-S3 bridge utility for Switch 2 controller inputs.
# Copyright (C) 2026 TommyWabg
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Contact Information:
# Electronic Mail: tommyw9318@gmail.com

import logging
import math
import os
import sys
import time
import ctypes

import numpy as np

logger = logging.getLogger(__name__)
_PERF_DIAGNOSTICS = os.environ.get('SWITCH2_PERF_DIAGNOSTICS', '0') == '1'
_NATIVE_HAPTIC_DISABLED = os.environ.get('SWITCH2_DISABLE_NATIVE_HAPTIC', '0') == '1'
_NATIVE_HAPTIC_LIB = None
_NATIVE_HAPTIC_LOAD_ATTEMPTED = False


class _NativeHapticResult(ctypes.Structure):
    _fields_ = [
        ("mode", ctypes.c_int),
        ("left_intensity", ctypes.c_int),
        ("right_intensity", ctypes.c_int),
        ("left_lf_freq", ctypes.c_int),
        ("left_lf_amp", ctypes.c_int),
        ("left_hf_freq", ctypes.c_int),
        ("left_hf_amp", ctypes.c_int),
        ("right_lf_freq", ctypes.c_int),
        ("right_lf_amp", ctypes.c_int),
        ("right_hf_freq", ctypes.c_int),
        ("right_hf_amp", ctypes.c_int),
    ]


def _native_haptic_candidates():
    names = ("dualsense_haptic_native.dll",)
    roots = []
    module_dir = os.path.dirname(os.path.abspath(__file__))
    roots.append(module_dir)
    roots.append(os.path.join(os.path.dirname(module_dir), "drivers"))
    roots.append(os.path.join(os.path.dirname(module_dir), "native"))
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        roots.append(base)
        roots.append(os.path.join(base, "drivers"))
        roots.append(os.path.join(base, "src"))
    for root in roots:
        for name in names:
            yield os.path.join(root, name)


def _load_native_haptic():
    global _NATIVE_HAPTIC_LIB, _NATIVE_HAPTIC_LOAD_ATTEMPTED
    if _NATIVE_HAPTIC_DISABLED:
        return None
    if _NATIVE_HAPTIC_LOAD_ATTEMPTED:
        return _NATIVE_HAPTIC_LIB
    _NATIVE_HAPTIC_LOAD_ATTEMPTED = True
    for path in _native_haptic_candidates():
        if not os.path.exists(path):
            continue
        try:
            lib = ctypes.CDLL(path)
            lib.ds_haptic_create.argtypes = []
            lib.ds_haptic_create.restype = ctypes.c_void_p
            lib.ds_haptic_destroy.argtypes = [ctypes.c_void_p]
            lib.ds_haptic_destroy.restype = None
            lib.ds_haptic_reset.argtypes = [ctypes.c_void_p]
            lib.ds_haptic_reset.restype = None
            lib.ds_haptic_process.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.POINTER(_NativeHapticResult),
            ]
            lib.ds_haptic_process.restype = ctypes.c_int
            _NATIVE_HAPTIC_LIB = lib
            logger.info("Loaded native DualSense haptic processor: %s", path)
            return lib
        except Exception as exc:
            logger.warning("Failed to load native DualSense haptic processor %s: %s", path, exc)
    logger.warning(
        "Native DualSense haptic processor not found; falling back to numpy path. Searched: %s",
        list(_native_haptic_candidates()),
    )
    return None


class DualSenseHapticProcessor:
    """Convert DualSense 4-channel audio haptics into Switch HD-rumble values.

    DualSense USB audio OUT is 4-channel 16-bit 48 kHz PCM. Channels 0/1 are
    game audio, channels 2/3 are the haptic actuator streams. This processor
    mirrors the reference VIIPER route: track activity with an envelope, analyze
    the haptic channels in a small spectral window, then emit low/high frequency
    amplitudes for each side.
    """

    CHANNELS = 4
    BYTES_PER_SAMPLE = 2
    FRAME_SIZE = CHANNELS * BYTES_PER_SAMPLE

    DOWNSAMPLE_FACTOR = 16
    SPECTRAL_RATE = 3000
    SPECTRAL_WINDOW = 64
    SPECTRAL_HOP = 36
    LOW_BIN_MIN = 2
    LOW_BIN_MAX = 5
    HIGH_BIN_MIN = 6
    HIGH_BIN_MAX = 13
    # Match PS5 Emu Mode / Xbox Rumble / Frequency=10 HF ceiling:
    # int(256 + 255 * (4 / 9)) == 369.
    HF_OUTPUT_MAX_FREQUENCY = 369
    # Raw HF band edges (peak-bin frequencies for HIGH_BIN_MIN/MAX):
    # round(6 * 3000 / 64) = 281, round(13 * 3000 / 64) = 609.  The 8 discrete bins
    # span 281..609 Hz; instead of hard-clamping everything >369 down to 369 (which
    # collapsed 6 of the 8 bins onto the ceiling), we linearly redistribute the full
    # raw range into 281..HF_OUTPUT_MAX_FREQUENCY so high-frequency detail survives.
    HF_RAW_MIN_FREQUENCY = 281
    HF_RAW_MAX_FREQUENCY = 609
    # Low band peak-bin frequencies for LOW_BIN_MIN/MAX: round(2*3000/64)=94,
    # round(5*3000/64)=234.  Redistribute that raw span into the full output range
    # [70,300] (like _remap_hf_frequency) instead of hard-clamping to the same window.
    LF_OUTPUT_MIN_FREQUENCY = 70
    LF_OUTPUT_MAX_FREQUENCY = 300
    LF_RAW_MIN_FREQUENCY = 94
    LF_RAW_MAX_FREQUENCY = 234

    ACTIVITY_ENVELOPE_THRESHOLD = 512
    ACTIVITY_PEAK_THRESHOLD = 2048
    SILENCE_PACKETS_BEFORE_STOP = 30
    SAFE_MAXIMUM_AMPLITUDE = 29000
    ISOLATED_IMPULSE_PEAK_THRESHOLD = 30000
    ISOLATED_IMPULSE_NEIGHBOR_THRESHOLD = 64
    ISOLATED_IMPULSE_NEIGHBOR_FRAMES = 8
    SPARSE_CLICK_MAX_ACTIVE_SAMPLES = 32
    FULLSCALE_OUTLIER_MAX_SAMPLES = 16
    FULLSCALE_OUTLIER_NEIGHBOR_RATIO = 0.75
    DIAGNOSTIC_LOG_INTERVAL = 2.0

    # Match y700's raw02 stage: spectral RMS maps to Switch physical amplitude
    # first; controller.py applies the BLE 10-bit gain conversion at write time.
    OUTPUT_MAX_AMPLITUDE = 29000

    def __init__(self, callback):
        self.callback = callback
        self.last_log_time = 0
        self._native_lib = _load_native_haptic()
        self._native_handle = self._native_lib.ds_haptic_create() if self._native_lib else None
        self._native_failed = False
        self.reset()

    def __del__(self):
        try:
            if getattr(self, "_native_lib", None) and getattr(self, "_native_handle", None):
                self._native_lib.ds_haptic_destroy(self._native_handle)
                self._native_handle = None
        except Exception:
            pass

    def reset(self):
        if getattr(self, "_native_lib", None) and getattr(self, "_native_handle", None):
            try:
                self._native_lib.ds_haptic_reset(self._native_handle)
            except Exception:
                self._native_failed = True
        # Pre-allocated ndarray buffers: fill(0) instead of rebuilding list each reset.
        # This eliminates per-reset GC pressure and removes the per-analysis
        # np.asarray(list) conversion cost in _analyze_band().
        if not hasattr(self, 'spectral_left') or not isinstance(self.spectral_left, np.ndarray):
            self.spectral_left = np.zeros(self.SPECTRAL_WINDOW, dtype=np.int32)
            self.spectral_right = np.zeros(self.SPECTRAL_WINDOW, dtype=np.int32)
        else:
            self.spectral_left.fill(0)
            self.spectral_right.fill(0)
        self.spectral_count = 0
        self.downsample_sum_left = 0
        self.downsample_sum_right = 0
        self.downsample_count = 0
        self.envelope_left = 0
        self.envelope_right = 0
        self.silence_packets = 0
        self.output_active = False

    def process_audio_packet(self, data: bytes):
        if not data:
            return
        if getattr(self, "_native_handle", None) and not getattr(self, "_native_failed", False):
            try:
                return self._process_audio_packet_native(data)
            except Exception:
                self._native_failed = True
                logger.warning("Native DualSense haptic processor failed; falling back to Python", exc_info=True)

        usable_length = (len(data) // self.FRAME_SIZE) * self.FRAME_SIZE
        if usable_length == 0:
            return
        if usable_length != len(data):
            data = data[:usable_length]

        # Vectorised with NumPy: the previous per-frame Python loop ran ~48k
        # iterations/second under continuous audio, monopolising the GIL and starving
        # the USBIP socket/HID threads -> input stutter/latency (e.g. The Last of Us).
        # NumPy array ops release the GIL and are far cheaper.
        arr = np.frombuffer(data, dtype="<i2").reshape(-1, self.CHANNELS)
        frames = arr.shape[0]
        left = arr[:, 2].astype(np.int32)
        right = arr[:, 3].astype(np.int32)
        left, left_impulses = self._suppress_isolated_fullscale_impulses(left)
        right, right_impulses = self._suppress_isolated_fullscale_impulses(right)
        abs_left = np.abs(left)
        abs_right = np.abs(right)

        peak_left = int(abs_left.max())
        peak_right = int(abs_right.max())
        mean_left = int(abs_left.sum()) // frames
        mean_right = int(abs_right.sum()) // frames

        # Decimate by DOWNSAMPLE_FACTOR.  The spectral stage below keeps the same
        # full-window cadence as the reference implementation: do not analyze a
        # zero-padded/partial window, because that creates artificial broadband
        # energy that feels like a weak high-frequency buzz.
        ds_left, ds_right = self._feed_downsampled(left, right)

        self.envelope_left = self._smooth_envelope(self.envelope_left, mean_left)
        self.envelope_right = self._smooth_envelope(self.envelope_right, mean_right)

        activity = (
            self.envelope_left >= self.ACTIVITY_ENVELOPE_THRESHOLD
            or self.envelope_right >= self.ACTIVITY_ENVELOPE_THRESHOLD
            or peak_left >= self.ACTIVITY_PEAK_THRESHOLD
            or peak_right >= self.ACTIVITY_PEAK_THRESHOLD
        )
        now = time.time()
        if _PERF_DIAGNOSTICS and now - getattr(self, 'last_input_log_time', 0) > self.DIAGNOSTIC_LOG_INTERVAL:
            logger.info(
                "Haptic PCM input: bytes=%d frames=%d ch2 mean=%d peak=%d env=%d | "
                "ch3 mean=%d peak=%d env=%d activity=%s ds=%d spectral_count=%d impulses=%d/%d",
                len(data), frames,
                mean_left, peak_left, self.envelope_left,
                mean_right, peak_right, self.envelope_right,
                activity, len(ds_left), self.spectral_count, left_impulses, right_impulses,
            )
            self.last_input_log_time = now
        if not activity:
            self.silence_packets += 1
            if self.output_active and self.silence_packets >= self.SILENCE_PACKETS_BEFORE_STOP:
                self.output_active = False
                self._emit_silence()
                return
            
            if self.output_active and hasattr(self, '_last_spectral'):
                # Fade out phase (Release Gate):
                # We freeze the frequencies to avoid noise-floor buzz, but smoothly decay
                # the amplitudes to zero over the duration of the SILENCE_PACKETS_BEFORE_STOP window.
                spectral = self._last_spectral.copy()
                multiplier = max(0.0, 1.0 - (self.silence_packets / self.SILENCE_PACKETS_BEFORE_STOP))
                
                spectral["left_lf_amp"] = int(spectral["left_lf_amp"] * multiplier)
                spectral["left_hf_amp"] = int(spectral["left_hf_amp"] * multiplier)
                spectral["right_lf_amp"] = int(spectral["right_lf_amp"] * multiplier)
                spectral["right_hf_amp"] = int(spectral["right_hf_amp"] * multiplier)
                
                left_intensity = self._to_legacy_intensity(max(spectral["left_lf_amp"], spectral["left_hf_amp"]))
                right_intensity = self._to_legacy_intensity(max(spectral["right_lf_amp"], spectral["right_hf_amp"]))
                
                self.callback(left_intensity, right_intensity, "SPECTRAL", spectral=spectral)
            return

        self.silence_packets = 0

        if self.spectral_count < self.SPECTRAL_WINDOW:
            needed = self.SPECTRAL_WINDOW - self.spectral_count
            take = min(needed, len(ds_left))
            # Both spectral buffers are np.ndarray; ds_left/ds_right may be list
            # (partial-block head/tail) or ndarray (full-block segment).
            # np.ndarray slice-assign accepts both without extra allocation.
            self.spectral_left[self.spectral_count:self.spectral_count + take] = ds_left[:take]
            self.spectral_right[self.spectral_count:self.spectral_count + take] = ds_right[:take]
            self.spectral_count += take
            if self.spectral_count < self.SPECTRAL_WINDOW:
                return

        low_freq_left, low_rms_left = self._analyze_band(
            self.spectral_left, self.LOW_BIN_MIN, self.LOW_BIN_MAX, 0x112)
        high_freq_left, high_rms_left = self._analyze_band(
            self.spectral_left, self.HIGH_BIN_MIN, self.HIGH_BIN_MAX, 0x187)
        low_freq_right, low_rms_right = self._analyze_band(
            self.spectral_right, self.LOW_BIN_MIN, self.LOW_BIN_MAX, 0x112)
        high_freq_right, high_rms_right = self._analyze_band(
            self.spectral_right, self.HIGH_BIN_MIN, self.HIGH_BIN_MAX, 0x187)

        self._shift_spectral_window(self.spectral_left)
        self._shift_spectral_window(self.spectral_right)
        self.spectral_count = self.SPECTRAL_WINDOW - self.SPECTRAL_HOP

        low_amp_left = self._map_spectral_amplitude(low_rms_left)
        high_amp_left = self._map_spectral_amplitude(high_rms_left)
        low_amp_right = self._map_spectral_amplitude(low_rms_right)
        high_amp_right = self._map_spectral_amplitude(high_rms_right)
        if (
            low_amp_left == 0 and
            high_amp_left == 0 and
            low_amp_right == 0 and
            high_amp_right == 0
        ):
            return

        self.output_active = True
        spectral = {
            "left_lf_freq": self._remap_lf_frequency(low_freq_left),
            "left_lf_amp": low_amp_left,
            "left_hf_freq": self._remap_hf_frequency(high_freq_left),
            "left_hf_amp": high_amp_left,
            "right_lf_freq": self._remap_lf_frequency(low_freq_right),
            "right_lf_amp": low_amp_right,
            "right_hf_freq": self._remap_hf_frequency(high_freq_right),
            "right_hf_amp": high_amp_right,
        }
        self._last_spectral = spectral

        left_intensity = self._to_legacy_intensity(max(low_amp_left, high_amp_left))
        right_intensity = self._to_legacy_intensity(max(low_amp_right, high_amp_right))

        now = time.time()
        if _PERF_DIAGNOSTICS and now - getattr(self, 'last_log_time', 0) > self.DIAGNOSTIC_LOG_INTERVAL:
            logger.info(
                "Haptic HD spectral - L lf=%d/%d hf=%d/%d | R lf=%d/%d hf=%d/%d",
                spectral["left_lf_freq"], spectral["left_lf_amp"],
                spectral["left_hf_freq"], spectral["left_hf_amp"],
                spectral["right_lf_freq"], spectral["right_lf_amp"],
                spectral["right_hf_freq"], spectral["right_hf_amp"],
            )
            self.last_log_time = now

        if self.callback:
            self.callback(left_intensity, right_intensity, "SPECTRAL", spectral=spectral)

    def _process_audio_packet_native(self, data: bytes):
        usable_length = (len(data) // self.FRAME_SIZE) * self.FRAME_SIZE
        if usable_length == 0:
            return
        result = _NativeHapticResult()
        buffer = ctypes.c_char_p(data[:usable_length])
        rc = self._native_lib.ds_haptic_process(
            self._native_handle,
            ctypes.cast(buffer, ctypes.c_void_p),
            usable_length,
            ctypes.byref(result),
        )
        if rc == 0 or not self.callback:
            return
        if rc == 2:
            self.callback(
                0,
                0,
                "SILENCE",
                spectral={
                    "left_lf_freq": result.left_lf_freq,
                    "left_lf_amp": 0,
                    "left_hf_freq": result.left_hf_freq,
                    "left_hf_amp": 0,
                    "right_lf_freq": result.right_lf_freq,
                    "right_lf_amp": 0,
                    "right_hf_freq": result.right_hf_freq,
                    "right_hf_amp": 0,
                },
            )
            return
        self.callback(
            result.left_intensity,
            result.right_intensity,
            "SPECTRAL",
            spectral={
                "left_lf_freq": result.left_lf_freq,
                "left_lf_amp": result.left_lf_amp,
                "left_hf_freq": result.left_hf_freq,
                "left_hf_amp": result.left_hf_amp,
                "right_lf_freq": result.right_lf_freq,
                "right_lf_amp": result.right_lf_amp,
                "right_hf_freq": result.right_hf_freq,
                "right_hf_amp": result.right_hf_amp,
            },
        )

    def _emit_silence(self):
        if self.callback:
            self.callback(
                0,
                0,
                "SILENCE",
                spectral={
                    "left_lf_freq": 0x0e1,
                    "left_lf_amp": 0,
                    "left_hf_freq": 0x1e1,
                    "left_hf_amp": 0,
                    "right_lf_freq": 0x0e1,
                    "right_lf_amp": 0,
                    "right_hf_freq": 0x1e1,
                    "right_hf_amp": 0,
                },
            )

    def _feed_downsampled(self, left, right):
        """Block-average decimate by DOWNSAMPLE_FACTOR into the spectral window,
        carrying a partial block across packets.

        Returns (ds_left, ds_right) where each is a list (possibly empty) of
        downsampled int32 values.  The partial-block head/tail are returned as
        plain Python list entries; the full-block segment is a numpy array that
        is appended via extend(), converted to Python ints once here rather than
        repeatedly in the caller.  The spectral ndarray buffers accept both list
        and ndarray segments in their slice-assign path.
        """
        factor = self.DOWNSAMPLE_FACTOR
        n = int(left.shape[0])
        i = 0
        ds_left = []
        ds_right = []

        # Finish the partial block left over from the previous packet.
        if self.downsample_count > 0:
            need = factor - self.downsample_count
            if n >= need:
                self.downsample_sum_left += int(left[:need].sum())
                self.downsample_sum_right += int(right[:need].sum())
                ds_left.append(self._div_toward_zero(self.downsample_sum_left, factor))
                ds_right.append(self._div_toward_zero(self.downsample_sum_right, factor))
                self.downsample_sum_left = 0
                self.downsample_sum_right = 0
                self.downsample_count = 0
                i = need
            else:
                self.downsample_sum_left += int(left.sum())
                self.downsample_sum_right += int(right.sum())
                self.downsample_count += n
                return [], []

        # Full blocks (vectorised sum over reshaped groups).
        # Return int32 ndarray directly — the caller's spectral ndarray slice-
        # assign accepts ndarrays without an extra list conversion.
        nblocks = (n - i) // factor
        if nblocks > 0:
            end = i + nblocks * factor
            left_sums = left[i:end].reshape(nblocks, factor).sum(axis=1)
            right_sums = right[i:end].reshape(nblocks, factor).sum(axis=1)
            full_left = self._div_array_toward_zero(left_sums, factor)
            full_right = self._div_array_toward_zero(right_sums, factor)
            ds_left.extend(full_left)
            ds_right.extend(full_right)
            i = end

        # Trailing partial block -> carry into the next packet.
        if i < n:
            self.downsample_sum_left += int(left[i:].sum())
            self.downsample_sum_right += int(right[i:].sum())
            self.downsample_count += (n - i)

        return ds_left, ds_right

    @staticmethod
    def _div_toward_zero(value, divisor):
        if value >= 0:
            return value // divisor
        return -((-value) // divisor)

    @staticmethod
    def _div_array_toward_zero(values, divisor):
        return np.where(values >= 0, values // divisor, -((-values) // divisor)).astype(np.int32)

    @classmethod
    def _suppress_isolated_fullscale_impulses(cls, samples):
        """Remove isolated full-scale clicks from the haptic channels.

        Windows speaker/channel tests can emit a sparse +/-32767 click into
        Ch3/Ch4 with only a short tail.  That is an audio endpoint click, not
        a DualSense haptic waveform, and the spectral analyzer turns it into a
        stable low/mid rumble.  Continuous waveform energy is left untouched.
        """
        if samples.size == 0:
            return samples, 0
        abs_samples = np.abs(samples)
        candidates = np.flatnonzero(abs_samples >= cls.ISOLATED_IMPULSE_PEAK_THRESHOLD)
        if candidates.size == 0:
            return samples, 0

        active = abs_samples > cls.ISOLATED_IMPULSE_NEIGHBOR_THRESHOLD
        active_count = int(np.count_nonzero(active))
        if active_count <= cls.SPARSE_CLICK_MAX_ACTIVE_SAMPLES:
            cleaned = samples.copy()
            cleaned[active] = 0
            return cleaned, active_count

        if candidates.size <= cls.FULLSCALE_OUTLIER_MAX_SAMPLES:
            cleaned = samples.copy()
            radius = cls.ISOLATED_IMPULSE_NEIGHBOR_FRAMES
            n = int(samples.shape[0])
            for raw_index in candidates.tolist():
                index = int(raw_index)
                start = max(0, index - radius)
                end = min(n, index + radius + 1)
                neighborhood = samples[start:end]
                neighbor_mask = np.abs(neighborhood) < cls.ISOLATED_IMPULSE_PEAK_THRESHOLD
                neighbor_mask[index - start] = False
                usable_neighbors = neighborhood[neighbor_mask]
                cleaned[index] = int(np.median(usable_neighbors)) if usable_neighbors.size else 0
            return cleaned, int(candidates.size)

        cleaned = None
        suppressed = 0
        radius = cls.ISOLATED_IMPULSE_NEIGHBOR_FRAMES
        limit = cls.ISOLATED_IMPULSE_NEIGHBOR_THRESHOLD
        n = int(samples.shape[0])
        for raw_index in candidates.tolist():
            index = int(raw_index)
            start = max(0, index - radius)
            end = min(n, index + radius + 1)
            center = index - start
            neighborhood = samples[start:end]
            if neighborhood.size <= 1:
                continue
            if center == 0:
                neighbors = neighborhood[1:]
            elif center == neighborhood.size - 1:
                neighbors = neighborhood[:-1]
            else:
                neighbors = np.concatenate((neighborhood[:center], neighborhood[center + 1:]))
            if neighbors.size == 0:
                continue
            neighbor_abs_peak = int(np.max(np.abs(neighbors)))
            if cleaned is None:
                cleaned = samples.copy()

            if neighbor_abs_peak <= limit:
                cleaned[index] = 0
                suppressed += 1
                continue

            # A single full-scale sample embedded in a much smaller waveform is
            # still an endpoint click.  Replace only the outlier sample; leave a
            # genuinely clipped continuous waveform intact.
            if neighbor_abs_peak >= int(abs(samples[index]) * cls.FULLSCALE_OUTLIER_NEIGHBOR_RATIO):
                continue
            cleaned[index] = int(np.median(neighbors))
            suppressed += 1

        if cleaned is None:
            return samples, 0
        return cleaned, suppressed

    @classmethod
    def _analyze_band(cls, samples, minimum_bin, maximum_bin, fallback_frequency):
        # `samples` is now a pre-allocated np.ndarray(dtype=int32); converting
        # directly to float64 view is cheaper than np.asarray() on a Python list.
        values = samples.astype(np.float64) / 16.0
        spectrum = np.fft.rfft(values, n=cls.SPECTRAL_WINDOW)
        band = spectrum[minimum_bin:maximum_bin + 1]
        if band.size == 0:
            return fallback_frequency, 0

        powers = np.real(band) * np.real(band) + np.imag(band) * np.imag(band)
        band_power = float(np.sum(powers))
        if band_power <= 0.0:
            return fallback_frequency, 0
        peak_offset = int(np.argmax(powers))
        peak_bin = minimum_bin + peak_offset

        rms = math.sqrt(band_power * 2.0) / cls.SPECTRAL_WINDOW * 16.0
        frequency = round(peak_bin * cls.SPECTRAL_RATE / cls.SPECTRAL_WINDOW)
        return int(frequency), int(max(0, min(round(rms), 0xFFFF)))

    @classmethod
    def _map_spectral_amplitude(cls, rms):
        physical = rms / 16384.0 * cls.SAFE_MAXIMUM_AMPLITUDE
        physical = int(round(max(0.0, min(physical, cls.SAFE_MAXIMUM_AMPLITUDE))))
        physical &= 0xFFC0
        return int(round(physical / cls.SAFE_MAXIMUM_AMPLITUDE * cls.OUTPUT_MAX_AMPLITUDE))

    @classmethod
    def _shift_spectral_window(cls, samples):
        # `samples` is np.ndarray; in-place slice copy is equivalent to the
        # previous list slice copy and does not allocate a new array.
        keep = cls.SPECTRAL_WINDOW - cls.SPECTRAL_HOP
        samples[:keep] = samples[cls.SPECTRAL_HOP:]

    @staticmethod
    def _smooth_envelope(previous, value):
        if value > previous:
            return (previous * 2 + value * 6 + 4) // 8
        return (previous * 7 + value + 4) // 8

    @staticmethod
    def _clamp_frequency(value, minimum, maximum):
        return minimum if value < minimum else maximum if value > maximum else value

    @classmethod
    def _remap_hf_frequency(cls, frequency):
        """Linearly redistribute the raw HF band (281..609 Hz) into the output ceiling
        (281..HF_OUTPUT_MAX_FREQUENCY) instead of hard-clamping the top bins to 369."""
        raw_min = cls.HF_RAW_MIN_FREQUENCY
        raw_span = cls.HF_RAW_MAX_FREQUENCY - raw_min
        out_span = cls.HF_OUTPUT_MAX_FREQUENCY - raw_min
        mapped = raw_min + (frequency - raw_min) * out_span / raw_span
        return min(cls.HF_OUTPUT_MAX_FREQUENCY, max(1, int(round(mapped))))

    @classmethod
    def _remap_lf_frequency(cls, frequency):
        """Linearly redistribute the raw LF band (94..234 Hz, bins 2..5) into the full
        output range [70,300], so the discrete low bins spread across the range instead
        of bunching at 94/141/188/234.  Mirrors _remap_hf_frequency in structure."""
        raw_min = cls.LF_RAW_MIN_FREQUENCY
        raw_span = cls.LF_RAW_MAX_FREQUENCY - raw_min
        out_min = cls.LF_OUTPUT_MIN_FREQUENCY
        out_span = cls.LF_OUTPUT_MAX_FREQUENCY - out_min
        if raw_span <= 0:
            return max(out_min, min(cls.LF_OUTPUT_MAX_FREQUENCY, int(round(frequency))))
        if frequency <= raw_min:
            return out_min
        mapped = out_min + (frequency - raw_min) * out_span / raw_span
        return min(cls.LF_OUTPUT_MAX_FREQUENCY, max(out_min, int(round(mapped))))

    @staticmethod
    def _to_legacy_intensity(amplitude):
        return max(0, min(96, int(round(amplitude / DualSenseHapticProcessor.SAFE_MAXIMUM_AMPLITUDE * 96.0))))
