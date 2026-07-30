"""Summarise a controller rumble/input trace produced by the app.

Usage:
    python tools/analyze_system_bt_rumble_trace.py logs/system_bt_rumble_trace.txt
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def _percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    index = min(len(values) - 1, max(0, int(round((len(values) - 1) * fraction))))
    return values[index]


def _stats(values):
    return {
        "n": len(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": max(values) if values else None,
        "gt50": sum(value > 50.0 for value in values),
    }


def _analyse_run(events):
    events.sort(key=lambda item: item["ts_ns"])
    if not events:
        return {}
    start_ns = events[0]["ts_ns"]
    end_ns = events[-1]["ts_ns"]
    counts = Counter(item.get("event") for item in events)
    inputs = defaultdict(list)
    state_age = defaultdict(list)
    state_changed = defaultdict(int)
    crc_repeats = defaultdict(int)
    previous_crc = {}
    writes = []
    write_starts = []
    pending_high_watermark = 0
    queued_to_done_ms = []
    virtual_state_age = []
    contexts = set()
    has_generic_virtual_submit = any(
        item.get("event") == "VIRTUAL_INPUT_SUBMIT_START" for item in events)

    for item in events:
        contexts.add((
            str(item.get("connection_mode", "Unknown")),
            str(item.get("driver_type", "Unknown")),
            str(item.get("emulation_mode", "Unknown")),
            str(item.get("rumble_mode", "Unknown")),
        ))
        subject = item.get("subject", "unknown")
        event = item.get("event")
        if event == "BT_INPUT_NOTIFY_ENTER":
            inputs[subject].append(item["ts_ns"])
        elif event == "BT_INPUT_STATE":
            age = item.get("state_age_ms")
            if isinstance(age, (int, float)):
                state_age[subject].append(float(age))
            if item.get("state_changed"):
                state_changed[subject] += 1
            crc = item.get("raw_crc32")
            if crc is not None and previous_crc.get(subject) == crc:
                crc_repeats[subject] += 1
            previous_crc[subject] = crc
        elif (event == "VIRTUAL_INPUT_SUBMIT_START" or
              (event == "WINUHID_INPUT_SUBMIT_START" and not has_generic_virtual_submit)):
            age = item.get("physical_state_age_ms")
            if isinstance(age, (int, float)):
                virtual_state_age.append(float(age))
        elif event == "BT_RUMBLE_WRITE_START":
            write_starts.append(item["ts_ns"])
        elif event == "BT_RUMBLE_WRITE_END" and item.get("duration_ns") is not None:
            writes.append(item["duration_ns"] / 1_000_000.0)
        elif event == "RUMBLE_COROUTINE_QUEUED":
            pending_high_watermark = max(
                pending_high_watermark, int(item.get("pending_high_watermark", 0) or 0))
        elif event == "RUMBLE_FUTURE_DONE" and item.get("queued_to_done_ns") is not None:
            queued_to_done_ms.append(item["queued_to_done_ns"] / 1_000_000.0)

    input_summary = {}
    gap_count_50 = 0
    for subject, timestamps in inputs.items():
        intervals_ms = [
            (right - left) / 1_000_000.0
            for left, right in zip(timestamps, timestamps[1:])
        ]
        gap_count_50 += sum(value > 50.0 for value in intervals_ms)
        input_summary[subject] = {
            "notifications": len(timestamps),
            "interval_ms": _stats(intervals_ms),
            "state_age_ms": _stats(state_age.get(subject, [])),
            "state_changes": state_changed.get(subject, 0),
            "consecutive_raw_crc_repeats": crc_repeats.get(subject, 0),
        }

    write_intervals_ms = [
        (right - left) / 1_000_000.0
        for left, right in zip(write_starts, write_starts[1:])
    ]
    dry_counts = Counter(item.get("dry_run") for item in events if item.get("event") == "BT_RUMBLE_WRITE_END")
    return {
        "duration_s": (end_ns - start_ns) / 1_000_000_000.0,
        "events": len(events),
        "test_contexts": [
            {
                "connection_mode": connection_mode,
                "driver_type": driver_type,
                "emulation_mode": emulation_mode,
                "rumble_mode": rumble_mode,
            }
            for connection_mode, driver_type, emulation_mode, rumble_mode
            in sorted(contexts)
        ],
        "event_counts": dict(counts),
        "dry_run_write_counts": dict(dry_counts),
        "user_lock_markers": counts.get("USER_LOCK_MARKER", 0),
        "input": input_summary,
        "input_gaps_over_50ms": gap_count_50,
        "virtual_input_state_age_ms": _stats(virtual_state_age),
        "rumble_writes": {
            "count": len(writes),
            "duration_ms_p50": _percentile(writes, 0.50),
            "duration_ms_p95": _percentile(writes, 0.95),
            "duration_ms_p99": _percentile(writes, 0.99),
            "start_interval_ms_p50": _percentile(write_intervals_ms, 0.50),
            "start_interval_ms_p95": _percentile(write_intervals_ms, 0.95),
        },
        "rumble_queue": {
            "pending_high_watermark": pending_high_watermark,
            "queued_to_done_ms_p50": _percentile(queued_to_done_ms, 0.50),
            "queued_to_done_ms_p95": _percentile(queued_to_done_ms, 0.95),
            "queued_to_done_ms_max": max(queued_to_done_ms) if queued_to_done_ms else None,
        },
    }


def analyse(path):
    runs = defaultdict(list)
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and isinstance(item.get("ts_ns"), int):
                runs[str(item.get("run_id", "unknown"))].append(item)
    return {
        "run_count": len(runs),
        "runs": {run_id: _analyse_run(events) for run_id, events in runs.items()},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("trace", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyse(args.trace), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
