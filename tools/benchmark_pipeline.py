#!/usr/bin/env python3
"""Capture reproducible macOS `top` samples for a CCTV process tree."""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
from datetime import datetime
from pathlib import Path


def process_tree(root_pid: int) -> set[int]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,command="],
        check=True,
        capture_output=True,
        text=True,
    )
    children: dict[int, list[int]] = {}
    present = set()
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) < 2:
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        present.add(pid)
        children.setdefault(ppid, []).append(pid)
    selected = {root_pid} if root_pid in present else set()
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        for child in children.get(parent, []):
            if child not in selected:
                selected.add(child)
                pending.append(child)
    return selected


def parse_memory(value: str) -> int:
    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3}
    value = value.rstrip("+-")
    if value and value[-1] in multipliers:
        return int(float(value[:-1]) * multipliers[value[-1]])
    return int(float(value)) if value else 0


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def capture(root_pid: int, seconds: int, interval: int) -> dict:
    pids = process_tree(root_pid)
    loops = max(2, seconds // interval + 1)
    command = [
        "top", "-l", str(loops), "-s", str(interval), "-n", "250", "-o", "cpu",
        "-stats", "pid,command,cpu,mem,threads,time",
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    per_pid: dict[int, list[dict]] = {pid: [] for pid in pids}
    aggregate_samples: list[float] = []
    current: dict[int, float] = {}
    for line in result.stdout.splitlines():
        if line.startswith("Processes:"):
            if current:
                aggregate_samples.append(sum(current.values()))
                current = {}
            continue
        parts = line.split()
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        if pid not in pids:
            continue
        try:
            cpu = float(parts[2])
            memory = parse_memory(parts[3])
        except ValueError:
            continue
        current[pid] = cpu
        per_pid[pid].append({"cpu": cpu, "memory_bytes": memory})
    if current:
        aggregate_samples.append(sum(current.values()))
    # macOS top's first delta is always zero; exclude it from comparisons.
    if aggregate_samples:
        aggregate_samples = aggregate_samples[1:]

    return {
        "captured_at": datetime.now().astimezone().isoformat(),
        "root_pid": root_pid,
        "pids": sorted(pids),
        "seconds": seconds,
        "interval_seconds": interval,
        "top_command": command,
        "aggregate_cpu_samples": aggregate_samples,
        "aggregate_cpu_median": statistics.median(aggregate_samples) if aggregate_samples else 0,
        "aggregate_cpu_p95": percentile(aggregate_samples, 0.95),
        "per_pid": {str(pid): samples for pid, samples in per_pid.items()},
        "raw_top": result.stdout,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root_pid", type=int)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--interval", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = capture(args.root_pid, args.seconds, args.interval)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps({key: report[key] for key in ("pids", "aggregate_cpu_median", "aggregate_cpu_p95")}, indent=2))


if __name__ == "__main__":
    main()
