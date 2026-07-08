#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <thread>
#include <vector>

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#define DS_EXPORT extern "C" __declspec(dllexport)
#else
#define DS_EXPORT extern "C"
#include <chrono>
#endif

struct DsHapticResult {
    int mode;
    int left_intensity;
    int right_intensity;
    int left_lf_freq;
    int left_lf_amp;
    int left_hf_freq;
    int left_hf_amp;
    int right_lf_freq;
    int right_lf_amp;
    int right_hf_freq;
    int right_hf_amp;
};

class Processor {
public:
    static constexpr int CHANNELS = 4;
    static constexpr int FRAME_SIZE = CHANNELS * 2;
    static constexpr int DOWNSAMPLE_FACTOR = 16;
    static constexpr int SPECTRAL_RATE = 3000;
    static constexpr int SPECTRAL_WINDOW = 64;
    static constexpr int SPECTRAL_HOP = 36;
    static constexpr int LOW_BIN_MIN = 2;
    static constexpr int LOW_BIN_MAX = 5;
    static constexpr int HIGH_BIN_MIN = 6;
    static constexpr int HIGH_BIN_MAX = 13;
    static constexpr int HF_OUTPUT_MAX_FREQUENCY = 369;
    static constexpr int HF_RAW_MIN_FREQUENCY = 281;
    static constexpr int HF_RAW_MAX_FREQUENCY = 609;
    // Low band peak-bin frequencies for LOW_BIN_MIN/MAX: round(2*3000/64)=94,
    // round(5*3000/64)=234.  Redistribute that raw span into the full output range
    // [70,300] (like remap_hf_frequency) instead of hard-clamping to the same window.
    static constexpr int LF_OUTPUT_MIN_FREQUENCY = 70;
    static constexpr int LF_OUTPUT_MAX_FREQUENCY = 300;
    static constexpr int LF_RAW_MIN_FREQUENCY = 94;
    static constexpr int LF_RAW_MAX_FREQUENCY = 234;
    static constexpr int ACTIVITY_ENVELOPE_THRESHOLD = 512;
    static constexpr int ACTIVITY_PEAK_THRESHOLD = 2048;
    static constexpr int SILENCE_PACKETS_BEFORE_STOP = 1;
    static constexpr int SAFE_MAXIMUM_AMPLITUDE = 29000;
    static constexpr int ISOLATED_IMPULSE_PEAK_THRESHOLD = 30000;
    static constexpr int ISOLATED_IMPULSE_NEIGHBOR_THRESHOLD = 64;
    static constexpr int ISOLATED_IMPULSE_NEIGHBOR_FRAMES = 8;
    static constexpr int SPARSE_CLICK_MAX_ACTIVE_SAMPLES = 32;
    static constexpr int FULLSCALE_OUTLIER_MAX_SAMPLES = 16;
    static constexpr double FULLSCALE_OUTLIER_NEIGHBOR_RATIO = 0.75;
    static constexpr int OUTPUT_MAX_AMPLITUDE = 29000;

    Processor() { reset(); }

    void reset() {
        std::fill(std::begin(spectral_left), std::end(spectral_left), 0);
        std::fill(std::begin(spectral_right), std::end(spectral_right), 0);
        spectral_count = 0;
        downsample_sum_left = 0;
        downsample_sum_right = 0;
        downsample_count = 0;
        envelope_left = 0;
        envelope_right = 0;
        silence_packets = 0;
        output_active = false;
    }

    int process(const uint8_t* data, int length, DsHapticResult* result) {
        if (!data || length <= 0 || !result) {
            return 0;
        }
        const int usable = (length / FRAME_SIZE) * FRAME_SIZE;
        if (usable <= 0) {
            return 0;
        }
        const int frames = usable / FRAME_SIZE;
        std::vector<int32_t> left(frames);
        std::vector<int32_t> right(frames);
        for (int i = 0; i < frames; ++i) {
            const int base = i * FRAME_SIZE;
            left[i] = read_i16(data + base + 4);
            right[i] = read_i16(data + base + 6);
        }
        suppress_isolated_fullscale_impulses(left);
        suppress_isolated_fullscale_impulses(right);

        int peak_left = 0;
        int peak_right = 0;
        int64_t sum_left = 0;
        int64_t sum_right = 0;
        for (int i = 0; i < frames; ++i) {
            const int al = std::abs(left[i]);
            const int ar = std::abs(right[i]);
            peak_left = std::max(peak_left, al);
            peak_right = std::max(peak_right, ar);
            sum_left += al;
            sum_right += ar;
        }
        const int mean_left = static_cast<int>(sum_left / frames);
        const int mean_right = static_cast<int>(sum_right / frames);

        std::vector<int32_t> ds_left;
        std::vector<int32_t> ds_right;
        feed_downsampled(left, right, ds_left, ds_right);

        envelope_left = smooth_envelope(envelope_left, mean_left);
        envelope_right = smooth_envelope(envelope_right, mean_right);
        const bool activity =
            envelope_left >= ACTIVITY_ENVELOPE_THRESHOLD ||
            envelope_right >= ACTIVITY_ENVELOPE_THRESHOLD ||
            peak_left >= ACTIVITY_PEAK_THRESHOLD ||
            peak_right >= ACTIVITY_PEAK_THRESHOLD;
        if (!activity) {
            ++silence_packets;
            if (output_active && silence_packets >= SILENCE_PACKETS_BEFORE_STOP) {
                output_active = false;
                emit_silence(result);
                return 2;
            }
            if (output_active) {
                double multiplier = std::max(0.0, 1.0 - (static_cast<double>(silence_packets) / SILENCE_PACKETS_BEFORE_STOP));
                int faded_left_lf_amp = static_cast<int>(last_left_lf_amp * multiplier);
                int faded_left_hf_amp = static_cast<int>(last_left_hf_amp * multiplier);
                int faded_right_lf_amp = static_cast<int>(last_right_lf_amp * multiplier);
                int faded_right_hf_amp = static_cast<int>(last_right_hf_amp * multiplier);

                std::memset(result, 0, sizeof(*result));
                result->mode = 1;
                result->left_lf_freq = last_left_lf_freq;
                result->left_lf_amp = faded_left_lf_amp;
                result->left_hf_freq = last_left_hf_freq;
                result->left_hf_amp = faded_left_hf_amp;
                result->right_lf_freq = last_right_lf_freq;
                result->right_lf_amp = faded_right_lf_amp;
                result->right_hf_freq = last_right_hf_freq;
                result->right_hf_amp = faded_right_hf_amp;
                result->left_intensity = to_legacy_intensity(std::max(faded_left_lf_amp, faded_left_hf_amp));
                result->right_intensity = to_legacy_intensity(std::max(faded_right_lf_amp, faded_right_hf_amp));
                return 1;
            }
            return 0;
        }

        silence_packets = 0;
        if (spectral_count < SPECTRAL_WINDOW) {
            const int needed = SPECTRAL_WINDOW - spectral_count;
            const int take = std::min(needed, static_cast<int>(ds_left.size()));
            for (int i = 0; i < take; ++i) {
                spectral_left[spectral_count + i] = ds_left[i];
                spectral_right[spectral_count + i] = ds_right[i];
            }
            spectral_count += take;
            if (spectral_count < SPECTRAL_WINDOW) {
                return 0;
            }
        }

        auto low_l = analyze_band(spectral_left, LOW_BIN_MIN, LOW_BIN_MAX, 0x112);
        auto high_l = analyze_band(spectral_left, HIGH_BIN_MIN, HIGH_BIN_MAX, 0x187);
        auto low_r = analyze_band(spectral_right, LOW_BIN_MIN, LOW_BIN_MAX, 0x112);
        auto high_r = analyze_band(spectral_right, HIGH_BIN_MIN, HIGH_BIN_MAX, 0x187);

        shift_spectral_window(spectral_left);
        shift_spectral_window(spectral_right);
        spectral_count = SPECTRAL_WINDOW - SPECTRAL_HOP;

        const int low_amp_left = map_spectral_amplitude(low_l.rms);
        const int high_amp_left = map_spectral_amplitude(high_l.rms);
        const int low_amp_right = map_spectral_amplitude(low_r.rms);
        const int high_amp_right = map_spectral_amplitude(high_r.rms);
        if (low_amp_left == 0 && high_amp_left == 0 && low_amp_right == 0 && high_amp_right == 0) {
            return 0;
        }

        output_active = true;
        
        last_left_lf_freq = remap_lf_frequency(low_l.frequency);
        last_left_lf_amp = low_amp_left;
        last_left_hf_freq = remap_hf_frequency(high_l.frequency);
        last_left_hf_amp = high_amp_left;
        last_right_lf_freq = remap_lf_frequency(low_r.frequency);
        last_right_lf_amp = low_amp_right;
        last_right_hf_freq = remap_hf_frequency(high_r.frequency);
        last_right_hf_amp = high_amp_right;

        std::memset(result, 0, sizeof(*result));
        result->mode = 1;
        result->left_lf_freq = last_left_lf_freq;
        result->left_lf_amp = last_left_lf_amp;
        result->left_hf_freq = last_left_hf_freq;
        result->left_hf_amp = last_left_hf_amp;
        result->right_lf_freq = last_right_lf_freq;
        result->right_lf_amp = last_right_lf_amp;
        result->right_hf_freq = last_right_hf_freq;
        result->right_hf_amp = last_right_hf_amp;
        result->left_intensity = to_legacy_intensity(std::max(low_amp_left, high_amp_left));
        result->right_intensity = to_legacy_intensity(std::max(low_amp_right, high_amp_right));
        return 1;
    }

private:
    struct BandResult {
        int frequency;
        int rms;
    };

    int32_t spectral_left[SPECTRAL_WINDOW] = {};
    int32_t spectral_right[SPECTRAL_WINDOW] = {};
    int spectral_count = 0;
    int64_t downsample_sum_left = 0;
    int64_t downsample_sum_right = 0;
    int downsample_count = 0;
    int envelope_left = 0;
    int envelope_right = 0;
    int silence_packets = 0;
    bool output_active = false;

    int last_left_lf_freq = 0;
    int last_left_lf_amp = 0;
    int last_left_hf_freq = 0;
    int last_left_hf_amp = 0;
    int last_right_lf_freq = 0;
    int last_right_lf_amp = 0;
    int last_right_hf_freq = 0;
    int last_right_hf_amp = 0;

    static int16_t read_i16(const uint8_t* p) {
        return static_cast<int16_t>(static_cast<uint16_t>(p[0]) | (static_cast<uint16_t>(p[1]) << 8));
    }

    static int div_toward_zero(int64_t value, int divisor) {
        return value >= 0 ? static_cast<int>(value / divisor) : -static_cast<int>((-value) / divisor);
    }

    static int clamp(int value, int minimum, int maximum) {
        return value < minimum ? minimum : (value > maximum ? maximum : value);
    }

    static int smooth_envelope(int previous, int value) {
        if (value > previous) {
            return (previous * 2 + value * 6 + 4) / 8;
        }
        return (previous * 7 + value + 4) / 8;
    }

    static int median(std::vector<int32_t>& values) {
        if (values.empty()) {
            return 0;
        }
        const size_t mid = values.size() / 2;
        std::nth_element(values.begin(), values.begin() + mid, values.end());
        return static_cast<int>(values[mid]);
    }

    static void suppress_isolated_fullscale_impulses(std::vector<int32_t>& samples) {
        if (samples.empty()) {
            return;
        }
        std::vector<int> candidates;
        int active_count = 0;
        for (int i = 0; i < static_cast<int>(samples.size()); ++i) {
            const int a = std::abs(samples[i]);
            if (a >= ISOLATED_IMPULSE_PEAK_THRESHOLD) {
                candidates.push_back(i);
            }
            if (a > ISOLATED_IMPULSE_NEIGHBOR_THRESHOLD) {
                ++active_count;
            }
        }
        if (candidates.empty()) {
            return;
        }
        if (active_count <= SPARSE_CLICK_MAX_ACTIVE_SAMPLES) {
            for (int i = 0; i < static_cast<int>(samples.size()); ++i) {
                if (std::abs(samples[i]) > ISOLATED_IMPULSE_NEIGHBOR_THRESHOLD) {
                    samples[i] = 0;
                }
            }
            return;
        }
        if (candidates.size() > FULLSCALE_OUTLIER_MAX_SAMPLES) {
            return;
        }
        const int n = static_cast<int>(samples.size());
        for (int index : candidates) {
            const int start = std::max(0, index - ISOLATED_IMPULSE_NEIGHBOR_FRAMES);
            const int end = std::min(n, index + ISOLATED_IMPULSE_NEIGHBOR_FRAMES + 1);
            std::vector<int32_t> usable;
            int neighbor_abs_peak = 0;
            for (int j = start; j < end; ++j) {
                if (j == index) {
                    continue;
                }
                neighbor_abs_peak = std::max(neighbor_abs_peak, std::abs(samples[j]));
                if (std::abs(samples[j]) < ISOLATED_IMPULSE_PEAK_THRESHOLD) {
                    usable.push_back(samples[j]);
                }
            }
            if (neighbor_abs_peak <= ISOLATED_IMPULSE_NEIGHBOR_THRESHOLD) {
                samples[index] = 0;
            } else if (neighbor_abs_peak < static_cast<int>(std::abs(samples[index]) * FULLSCALE_OUTLIER_NEIGHBOR_RATIO)) {
                samples[index] = median(usable);
            }
        }
    }

    void feed_downsampled(const std::vector<int32_t>& left, const std::vector<int32_t>& right,
                          std::vector<int32_t>& ds_left, std::vector<int32_t>& ds_right) {
        const int n = static_cast<int>(left.size());
        int i = 0;
        if (downsample_count > 0) {
            const int need = DOWNSAMPLE_FACTOR - downsample_count;
            if (n >= need) {
                for (int j = 0; j < need; ++j) {
                    downsample_sum_left += left[j];
                    downsample_sum_right += right[j];
                }
                ds_left.push_back(div_toward_zero(downsample_sum_left, DOWNSAMPLE_FACTOR));
                ds_right.push_back(div_toward_zero(downsample_sum_right, DOWNSAMPLE_FACTOR));
                downsample_sum_left = 0;
                downsample_sum_right = 0;
                downsample_count = 0;
                i = need;
            } else {
                for (int j = 0; j < n; ++j) {
                    downsample_sum_left += left[j];
                    downsample_sum_right += right[j];
                }
                downsample_count += n;
                return;
            }
        }
        const int nblocks = (n - i) / DOWNSAMPLE_FACTOR;
        for (int b = 0; b < nblocks; ++b) {
            int64_t sum_l = 0;
            int64_t sum_r = 0;
            for (int j = 0; j < DOWNSAMPLE_FACTOR; ++j) {
                sum_l += left[i + b * DOWNSAMPLE_FACTOR + j];
                sum_r += right[i + b * DOWNSAMPLE_FACTOR + j];
            }
            ds_left.push_back(div_toward_zero(sum_l, DOWNSAMPLE_FACTOR));
            ds_right.push_back(div_toward_zero(sum_r, DOWNSAMPLE_FACTOR));
        }
        i += nblocks * DOWNSAMPLE_FACTOR;
        while (i < n) {
            downsample_sum_left += left[i];
            downsample_sum_right += right[i];
            ++downsample_count;
            ++i;
        }
    }

    static BandResult analyze_band(const int32_t* samples, int minimum_bin, int maximum_bin, int fallback_frequency) {
        double best_power = 0.0;
        int best_bin = minimum_bin;
        double band_power = 0.0;
        constexpr double pi = 3.14159265358979323846;
        for (int bin = minimum_bin; bin <= maximum_bin; ++bin) {
            double real = 0.0;
            double imag = 0.0;
            for (int n = 0; n < SPECTRAL_WINDOW; ++n) {
                const double value = static_cast<double>(samples[n]) / 16.0;
                const double angle = -2.0 * pi * static_cast<double>(bin) * static_cast<double>(n) / SPECTRAL_WINDOW;
                real += value * std::cos(angle);
                imag += value * std::sin(angle);
            }
            const double power = real * real + imag * imag;
            band_power += power;
            if (power > best_power) {
                best_power = power;
                best_bin = bin;
            }
        }
        if (band_power <= 0.0) {
            return {fallback_frequency, 0};
        }
        const double rms = std::sqrt(band_power * 2.0) / SPECTRAL_WINDOW * 16.0;
        const int frequency = static_cast<int>(std::lround(static_cast<double>(best_bin) * SPECTRAL_RATE / SPECTRAL_WINDOW));
        return {frequency, clamp(static_cast<int>(std::lround(rms)), 0, 0xFFFF)};
    }

    static int map_spectral_amplitude(int rms) {
        double physical = static_cast<double>(rms) / 16384.0 * SAFE_MAXIMUM_AMPLITUDE;
        physical = std::max(0.0, std::min(physical, static_cast<double>(SAFE_MAXIMUM_AMPLITUDE)));
        int value = static_cast<int>(std::lround(physical)) & 0xFFC0;
        return static_cast<int>(std::lround(static_cast<double>(value) / SAFE_MAXIMUM_AMPLITUDE * OUTPUT_MAX_AMPLITUDE));
    }

    static void shift_spectral_window(int32_t* samples) {
        constexpr int keep = SPECTRAL_WINDOW - SPECTRAL_HOP;
        for (int i = 0; i < keep; ++i) {
            samples[i] = samples[SPECTRAL_HOP + i];
        }
    }

    static int remap_hf_frequency(int frequency) {
        constexpr int raw_span = HF_RAW_MAX_FREQUENCY - HF_RAW_MIN_FREQUENCY;
        constexpr int out_span = HF_OUTPUT_MAX_FREQUENCY - HF_RAW_MIN_FREQUENCY;
        if (frequency <= HF_RAW_MIN_FREQUENCY) {
            return frequency;
        }
        const double t = static_cast<double>(frequency - HF_RAW_MIN_FREQUENCY) / raw_span;
        return clamp(static_cast<int>(std::lround(HF_RAW_MIN_FREQUENCY + t * out_span)),
                     HF_RAW_MIN_FREQUENCY, HF_OUTPUT_MAX_FREQUENCY);
    }

    static int remap_lf_frequency(int frequency) {
        // Linearly redistribute the raw LF band (94..234) into the full output range
        // [70,300], so the 4 discrete low bins spread across the range instead of
        // bunching at 94/141/188/234.  Mirrors remap_hf_frequency in structure.
        constexpr int raw_span = LF_RAW_MAX_FREQUENCY - LF_RAW_MIN_FREQUENCY;
        constexpr int out_span = LF_OUTPUT_MAX_FREQUENCY - LF_OUTPUT_MIN_FREQUENCY;
        if (frequency <= LF_RAW_MIN_FREQUENCY) {
            return LF_OUTPUT_MIN_FREQUENCY;
        }
        const double t = static_cast<double>(frequency - LF_RAW_MIN_FREQUENCY) / raw_span;
        return clamp(static_cast<int>(std::lround(LF_OUTPUT_MIN_FREQUENCY + t * out_span)),
                     LF_OUTPUT_MIN_FREQUENCY, LF_OUTPUT_MAX_FREQUENCY);
    }

    static int to_legacy_intensity(int amp) {
        return clamp(static_cast<int>(std::lround(static_cast<double>(amp) / OUTPUT_MAX_AMPLITUDE * 96.0)), 0, 96);
    }

    static void emit_silence(DsHapticResult* result) {
        std::memset(result, 0, sizeof(*result));
        result->mode = 2;
        result->left_lf_freq = 0x0e1;
        result->left_hf_freq = 0x1e1;
        result->right_lf_freq = 0x0e1;
        result->right_hf_freq = 0x1e1;
    }
};

DS_EXPORT void* ds_haptic_create() {
    try {
        return new Processor();
    } catch (...) {
        return nullptr;
    }
}

DS_EXPORT void ds_haptic_destroy(void* handle) {
    delete static_cast<Processor*>(handle);
}

DS_EXPORT void ds_haptic_reset(void* handle) {
    if (handle) {
        static_cast<Processor*>(handle)->reset();
    }
}

DS_EXPORT int ds_haptic_process(void* handle, const uint8_t* data, int length, DsHapticResult* result) {
    if (!handle) {
        return 0;
    }
    return static_cast<Processor*>(handle)->process(data, length, result);
}

DS_EXPORT void ds_precise_sleep_us(int microseconds) {
    if (microseconds <= 0) {
        return;
    }
#if defined(_WIN32)
    LARGE_INTEGER frequency{};
    LARGE_INTEGER start{};
    QueryPerformanceFrequency(&frequency);
    QueryPerformanceCounter(&start);
    const long long target =
        start.QuadPart + static_cast<long long>(
            (static_cast<double>(microseconds) / 1000000.0) * static_cast<double>(frequency.QuadPart));

    HANDLE timer = CreateWaitableTimerExW(nullptr, nullptr, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS);
    if (timer != nullptr && microseconds > 2000) {
        LARGE_INTEGER due{};
        due.QuadPart = -static_cast<long long>((microseconds - 1000) * 10);
        if (SetWaitableTimerEx(timer, &due, 0, nullptr, nullptr, nullptr, 0)) {
            WaitForSingleObject(timer, INFINITE);
        }
        CloseHandle(timer);
    } else if (microseconds > 2000) {
        Sleep(static_cast<DWORD>((microseconds - 1000) / 1000));
    }

    for (;;) {
        LARGE_INTEGER now{};
        QueryPerformanceCounter(&now);
        if (now.QuadPart >= target) {
            break;
        }
        const double remaining_us =
            static_cast<double>(target - now.QuadPart) * 1000000.0 / static_cast<double>(frequency.QuadPart);
        if (remaining_us > 2000.0) {
            Sleep(1);
        } else {
            YieldProcessor();
        }
    }
#else
    const auto target = std::chrono::steady_clock::now() + std::chrono::microseconds(microseconds);
    if (microseconds > 2000) {
        std::this_thread::sleep_for(std::chrono::microseconds(microseconds - 1000));
    }
    while (std::chrono::steady_clock::now() < target) {
        std::this_thread::yield();
    }
#endif
}
