"""bench_dualsense_haptics.py — Equivalence & performance benchmark.

Usage:
    python tools/bench_dualsense_haptics.py [--baseline] [--compare baseline.json]

Run with --baseline to capture the current (un-optimised) output.
Run with --compare to diff against a saved baseline after optimising.

All PCM inputs are deterministic (seeded NumPy) so results are reproducible
across runs on the same machine.
"""
import argparse
import json
import os
import sys
import time

# Allow importing from src/ without installing the package.
_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, os.path.abspath(_SRC))

import numpy as np
from dualsense_haptic import DualSenseHapticProcessor

# ---------------------------------------------------------------------------
# Deterministic PCM test vectors
# ---------------------------------------------------------------------------
SAMPLE_RATE = 48000
CHANNELS = 4
BYTES_PER_SAMPLE = 2
FRAME_BYTES = CHANNELS * BYTES_PER_SAMPLE  # 8 bytes per frame

# Duration of each test vector: 50 ms (≈ 2400 frames × 4ch × 2B = 19200 B)
DURATION_FRAMES = int(SAMPLE_RATE * 0.05)

rng = np.random.default_rng(42)


def _pcm_bytes(ch0, ch1, ch2, ch3):
    """Interleave 4 int16 channels into raw PCM bytes."""
    n = len(ch0)
    arr = np.zeros((n, 4), dtype=np.int16)
    arr[:, 0] = np.clip(ch0, -32768, 32767).astype(np.int16)
    arr[:, 1] = np.clip(ch1, -32768, 32767).astype(np.int16)
    arr[:, 2] = np.clip(ch2, -32768, 32767).astype(np.int16)
    arr[:, 3] = np.clip(ch3, -32768, 32767).astype(np.int16)
    return arr.tobytes()


def make_silence():
    n = DURATION_FRAMES
    return _pcm_bytes(
        np.zeros(n, np.int16), np.zeros(n, np.int16),
        np.zeros(n, np.int16), np.zeros(n, np.int16),
    )


def make_left_low_freq():
    """Haptic channel 2 (left): 150 Hz sine at ~70% amplitude; ch3 silent."""
    n = DURATION_FRAMES
    t = np.arange(n) / SAMPLE_RATE
    sig = (np.sin(2 * np.pi * 150 * t) * 22000).astype(np.int32)
    return _pcm_bytes(np.zeros(n), np.zeros(n), sig, np.zeros(n))


def make_left_high_freq():
    """Haptic channel 2 (left): 450 Hz sine at ~70% amplitude; ch3 silent."""
    n = DURATION_FRAMES
    t = np.arange(n) / SAMPLE_RATE
    sig = (np.sin(2 * np.pi * 450 * t) * 22000).astype(np.int32)
    return _pcm_bytes(np.zeros(n), np.zeros(n), sig, np.zeros(n))


def make_both_mix():
    """Both haptic channels: ch2 150 Hz LF, ch3 400 Hz HF."""
    n = DURATION_FRAMES
    t = np.arange(n) / SAMPLE_RATE
    left = (np.sin(2 * np.pi * 150 * t) * 20000).astype(np.int32)
    right = (np.sin(2 * np.pi * 400 * t) * 20000).astype(np.int32)
    return _pcm_bytes(np.zeros(n), np.zeros(n), left, right)


def make_fullscale_sparse_click():
    """Sparse +/-32767 clicks in ch2 — isolated impulse suppression test."""
    n = DURATION_FRAMES
    ch2 = np.zeros(n, np.int32)
    for i in range(0, n, 800):
        ch2[i] = 32767
    return _pcm_bytes(np.zeros(n), np.zeros(n), ch2, np.zeros(n))


def make_continuous_clipped():
    """Continuously clipped waveform — should NOT be suppressed."""
    n = DURATION_FRAMES
    t = np.arange(n) / SAMPLE_RATE
    sig = np.clip((np.sin(2 * np.pi * 250 * t) * 40000), -32767, 32767).astype(np.int32)
    return _pcm_bytes(np.zeros(n), np.zeros(n), sig, sig)


def make_random_active():
    """Random active haptics on both channels."""
    n = DURATION_FRAMES
    ch2 = rng.integers(-25000, 25000, n, dtype=np.int32)
    ch3 = rng.integers(-25000, 25000, n, dtype=np.int32)
    return _pcm_bytes(np.zeros(n), np.zeros(n), ch2, ch3)


TEST_VECTORS = [
    ("silence",             make_silence),
    ("left_low_freq",       make_left_low_freq),
    ("left_high_freq",      make_left_high_freq),
    ("both_mix",            make_both_mix),
    ("fullscale_sparse",    make_fullscale_sparse_click),
    ("continuous_clipped",  make_continuous_clipped),
    ("random_active",       make_random_active),
]

# ---------------------------------------------------------------------------
# Benchmark harness
# ---------------------------------------------------------------------------

def run_benchmark(n_iters: int = 100):
    """Run all test vectors and collect spectral callbacks + timing."""
    results = {}

    for name, make_fn in TEST_VECTORS:
        pcm = make_fn()
        callbacks = []

        def cb(left_intensity, right_intensity, mode="CONTINUOUS", spectral=None,
               _store=callbacks):
            _store.append({
                "left_intensity": left_intensity,
                "right_intensity": right_intensity,
                "mode": mode,
                "spectral": spectral,
            })

        proc = DualSenseHapticProcessor(cb)

        # Warm-up (fill spectral window)
        proc.process_audio_packet(pcm)
        proc.reset()
        callbacks.clear()

        # Timed iterations
        t0 = time.perf_counter()
        for _ in range(n_iters):
            proc.reset()
            callbacks.clear()
            proc.process_audio_packet(pcm)
        elapsed = time.perf_counter() - t0

        avg_us = elapsed / n_iters * 1_000_000
        results[name] = {
            "avg_us": round(avg_us, 2),
            "n_callbacks": len(callbacks),
            "callbacks": callbacks,
        }
        print(f"  {name:30s}  avg={avg_us:8.1f} µs  callbacks={len(callbacks)}")

    return results


def serialise(results):
    """JSON-serialisable version of results (spectral dicts are already plain)."""
    out = {}
    for name, r in results.items():
        out[name] = {
            "avg_us": r["avg_us"],
            "n_callbacks": r["n_callbacks"],
            "callbacks": r["callbacks"],
        }
    return out


def compare(baseline: dict, current: dict):
    """Return (passed, report_lines)."""
    lines = []
    passed = True

    all_names = set(baseline) | set(current)
    for name in sorted(all_names):
        if name not in baseline:
            lines.append(f"  NEW  {name} (no baseline)")
            continue
        if name not in current:
            lines.append(f"  MISS {name} (missing in current)")
            passed = False
            continue

        b = baseline[name]
        c = current[name]

        if b["n_callbacks"] != c["n_callbacks"]:
            lines.append(
                f"  FAIL {name}: callback count baseline={b['n_callbacks']} current={c['n_callbacks']}"
            )
            passed = False
            continue

        for i, (bc, cc) in enumerate(zip(b["callbacks"], c["callbacks"])):
            if bc["mode"] != cc["mode"]:
                lines.append(f"  FAIL {name}[{i}]: mode {bc['mode']!r} → {cc['mode']!r}")
                passed = False
            if bc["left_intensity"] != cc["left_intensity"] or bc["right_intensity"] != cc["right_intensity"]:
                lines.append(
                    f"  FAIL {name}[{i}]: intensity baseline=({bc['left_intensity']},{bc['right_intensity']}) "
                    f"current=({cc['left_intensity']},{cc['right_intensity']})"
                )
                passed = False
            b_sp = bc.get("spectral") or {}
            c_sp = cc.get("spectral") or {}
            for key in set(b_sp) | set(c_sp):
                bv = b_sp.get(key)
                cv = c_sp.get(key)
                if bv != cv:
                    lines.append(f"  FAIL {name}[{i}] spectral.{key}: {bv} → {cv}")
                    passed = False

        avg_ratio = c["avg_us"] / max(b["avg_us"], 0.001)
        speedup = f" ({(1-avg_ratio)*100:+.1f}%)" if avg_ratio != 1 else ""
        lines.append(
            f"  OK   {name}: baseline={b['avg_us']:.1f}µs current={c['avg_us']:.1f}µs{speedup}"
        )

    return passed, lines


def main():
    parser = argparse.ArgumentParser(description="DualSenseHapticProcessor benchmark")
    parser.add_argument("--baseline", action="store_true",
                        help="Capture baseline and save to baseline.json")
    parser.add_argument("--compare", metavar="FILE",
                        help="Compare current run against saved baseline JSON")
    parser.add_argument("--iters", type=int, default=50,
                        help="Iterations per test vector (default: 50)")
    args = parser.parse_args()

    print(f"Running {args.iters} iterations per vector…")
    results = run_benchmark(args.iters)

    if args.baseline:
        path = os.path.join(os.path.dirname(__file__), "baseline.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(serialise(results), f, indent=2)
        print(f"\nBaseline saved to {path}")

    if args.compare:
        with open(args.compare, encoding="utf-8") as f:
            baseline = json.load(f)
        passed, report = compare(baseline, serialise(results))
        print("\nEquivalence check:")
        for line in report:
            print(line)
        if passed:
            print("\n✓ All outputs match baseline.")
        else:
            print("\n✗ Equivalence check FAILED — review diffs above.")
            sys.exit(1)


if __name__ == "__main__":
    main()
