import struct
import time
import math
import logging

logger = logging.getLogger(__name__)

class DualSenseHapticProcessor:
    def __init__(self, callback):
        self.callback = callback
        
        self.channels = 4
        self.bytes_per_sample = 2
        self.frame_size = self.channels * self.bytes_per_sample
        
        self.last_update_time = 0
        self.update_interval = 0.005
        
        self.reset()

    def reset(self):
        self.prev_env_l = 0
        self.prev_env_r = 0
        self.prev_peak_l = 0
        self.prev_peak_r = 0
        self.last_log_time = 0

    def process_audio_packet(self, data: bytes):
        if not data:
            return

        if len(data) % self.frame_size != 0:
            logger.debug("音訊封包大小不完整，忽略此次處理")
            return

        num_frames = len(data) // self.frame_size
        if num_frames == 0:
            return
        
        valid_length = num_frames * self.frame_size
        data = data[:valid_length]
        
        unpack_format = f"<{num_frames * self.channels}h"
        samples = struct.unpack(unpack_format, data)

        # Ported ESP32 envelope smoothing algorithm
        def smooth_envelope(previous, value):
            if value > previous:
                mixed = (previous * 2 + value * 6 + 4) // 8
            else:
                mixed = (previous * 7 + value + 4) // 8
            return min(mixed, 65535)

        def positive_delta(value, previous):
            return value - previous if value > previous else 0

        ch2_sum = 0
        ch3_sum = 0
        peak_l = 0
        peak_r = 0
        
        # Audio channels mapping (Strictly Channel 2 & 3 for Haptics)
        left_offset = 2 if self.channels >= 4 else 0
        right_offset = 3 if self.channels >= 4 else 1

        for i in range(num_frames):
            idx = i * self.channels
            
            # Read only haptic channels, ignore Channel 0/1 (background audio)
            abs_l = abs(samples[idx + left_offset])
            abs_r = abs(samples[idx + right_offset])
            
            ch2_sum += abs_l
            ch3_sum += abs_r
            if abs_l > peak_l: peak_l = abs_l
            if abs_r > peak_r: peak_r = abs_r

        mean_abs_l = ch2_sum // num_frames
        mean_abs_r = ch3_sum // num_frames

        # Envelope
        env_l = smooth_envelope(self.prev_env_l, mean_abs_l)
        env_r = smooth_envelope(self.prev_env_r, mean_abs_r)

        # Transients
        env_delta_l = positive_delta(env_l, self.prev_env_l)
        env_delta_r = positive_delta(env_r, self.prev_env_r)
        peak_delta_l = positive_delta(peak_l, self.prev_peak_l)
        peak_delta_r = positive_delta(peak_r, self.prev_peak_r)
        
        transient_l = max(env_delta_l, peak_delta_l)
        transient_r = max(env_delta_r, peak_delta_r)

        # Update previous state
        self.prev_env_l = env_l
        self.prev_env_r = env_r
        self.prev_peak_l = peak_l
        self.prev_peak_r = peak_r

        # Calculate Flags
        # 移除靜音門檻，只要有聲音就觸發
        activity = env_l > 0 or env_r > 0 or peak_l > 0 or peak_r > 0
        transient_flag = transient_l >= 200 or transient_r >= 200

        # Throttle logging to roughly once per 0.5 seconds if there is any activity
        current_time = time.time()
        if activity:
            if not hasattr(self, 'last_log_time') or current_time - self.last_log_time > 0.5:
                logger.info(f"Haptic HD - Env L: {env_l}, Env R: {env_r}, Transient L: {transient_l}, Transient R: {transient_r}")
                self.last_log_time = current_time

        def choose_effect_mode():
            if not activity:
                return "SILENCE"
            if transient_flag:
                return "PUNCH"
            if env_l > 5000 or env_r > 5000:
                return "CONTINUOUS"
            if peak_l > 2000 or peak_r > 2000:
                return "TEXTURE"
            return "TICK"

        mode = choose_effect_mode()
        
        def calculate_intensity(envelope, transient):
            # 引入非線性增益 (Non-linear gain)：提升微弱訊號的強度，並壓制過強訊號避免破音
            # 使用 Gamma 曲線 (0.65)，讓細微的腳步聲或雨聲能轉換成足夠驅動馬達的電壓
            normalized_env = envelope / 32768.0
            normalized_trans = transient / 32768.0
            
            env_boosted = (normalized_env ** 0.65) * 255.0
            transient_boosted = (normalized_trans ** 0.70) * 255.0
            
            # 將最終強度 x2 (依使用者要求)
            value = int((env_boosted + transient_boosted) * 2.0 + 0.5)
            return min(255, value)

        def apply_mode_limits(intensity, mode):
            if mode == "SILENCE" or intensity == 0:
                return 0
            shaped = intensity
                
            if mode == "TICK" and shaped > 64:
                shaped = 64
            elif mode == "TEXTURE" and shaped < 24:
                shaped = 24
            elif mode == "PUNCH" and shaped < 40:
                shaped = 40
            return shaped

        left_intensity_raw = calculate_intensity(env_l, transient_l)
        right_intensity_raw = calculate_intensity(env_r, transient_r)

        left_intensity = apply_mode_limits(left_intensity_raw, mode)
        right_intensity = apply_mode_limits(right_intensity_raw, mode)

        if current_time - self.last_update_time >= self.update_interval:
            self.last_update_time = current_time
            
            if self.callback:
                self.callback(left_intensity, right_intensity, mode)