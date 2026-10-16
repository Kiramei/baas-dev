"""Compare the old per-record executor pump with the current LogManager.

Run from the repository root with the service dependencies installed::

    .venv/Scripts/python.exe develop_tools/benchmark_log_delivery.py \
        --records 10000 --rounds 5 \
        --output docs/performance/log-pump-20261015.json

This is a synthetic ingestion benchmark with no subscribers. It does not start
the service, connect to a running installation, or measure UI/transport latency.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import platform
import queue
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service.utils.logging import LogManager  # noqa: E402


class BaselineLogManager(LogManager):
    """Original pump: one thread-pool dispatch per record, including sentinel."""

    async def _pump_async(self, source: queue.Queue, scope: str, sentinel: object) -> None:
        while True:
            record = await asyncio.to_thread(source.get)
            if record is sentinel:
                break
            entry = self._normalize_record(record, scope)
            with self._history_lock:
                self._history_all.append(entry)
                self._history_per_scope[scope].append(entry)
            await self._broadcast.publish(entry)


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


async def measure(manager_type: type[LogManager], records: list[dict[str, Any]]) -> float:
    manager = manager_type(asyncio.get_running_loop())
    source: queue.Queue = queue.Queue()
    sentinel = object()
    for record in records:
        source.put_nowait(record)
    source.put_nowait(sentinel)

    started = time.perf_counter()
    await manager._pump_async(source, "global", sentinel)
    elapsed_ms = (time.perf_counter() - started) * 1000

    # Check equivalent output outside the timed section; keep every row in
    # history in both implementations rather than benchmarking dropped work.
    history = manager.get_history()
    assert len(history) == len(records)
    assert all(entry["message"] == record["message"] for entry, record in zip(history, records))
    assert source.empty()
    return elapsed_ms


async def benchmark(record_count: int, rounds: int) -> dict[str, Any]:
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    records = [
        {"time": timestamp, "level": 1, "message": f"log-{index}"}
        for index in range(record_count)
    ]
    implementations = {"baseline": BaselineLogManager, "optimized": LogManager}
    warmup_records = min(record_count, 256)
    for implementation in implementations.values():
        await measure(implementation, records[:warmup_records])

    samples: dict[str, list[float]] = {name: [] for name in implementations}
    round_results = []
    for round_index in range(rounds):
        order = ["baseline", "optimized"] if round_index % 2 == 0 else ["optimized", "baseline"]
        durations = {}
        for name in order:
            duration = await measure(implementations[name], records)
            samples[name].append(duration)
            durations[name] = duration
        round_results.append({"round": round_index + 1, "order": order, "elapsed_ms": durations})

    baseline_median = statistics.median(samples["baseline"])
    optimized_median = statistics.median(samples["optimized"])
    return {
        "benchmark": "LogManager log ingestion",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "records_per_sample": record_count,
        "rounds": rounds,
        "warmup_records_per_implementation": warmup_records,
        "subscribers": 0,
        "history_retained": True,
        "timing_scope": "Preloaded queue ingestion, normalization, history retention, and empty broadcast fanout",
        "excluded_from_timing": ["Queue construction", "Output validation", "Service startup", "Transport", "UI"],
        "baseline": "Original per-record asyncio.to_thread(source.get) pump in BaselineLogManager",
        "optimized": "Current service.utils.logging.LogManager pump",
        "round_results": round_results,
        "samples_ms": samples,
        "median_ms": {"baseline": baseline_median, "optimized": optimized_median},
        "median_speedup": baseline_median / optimized_median,
        "limitations": [
            "Synthetic no-subscriber benchmark; does not represent UI latency or end-to-end transport throughput.",
            "Measures an existing queue backlog, rather than sparse live-log latency.",
            "Both implementations run in one process on the same event loop and shared default executor; order alternates by round.",
            "This benchmark does not compare WebSocket, named pipes, or shared memory.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=positive_int, default=10_000, help="Records in each timed sample")
    parser.add_argument("--rounds", type=positive_int, default=5, help="Alternating baseline/optimized pairs")
    parser.add_argument("--output", type=Path, help="Also save the full JSON report to this path")
    args = parser.parse_args()
    report = asyncio.run(benchmark(args.records, args.rounds))
    report["command"] = ["python", "develop_tools/benchmark_log_delivery.py", *sys.argv[1:]]
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")


if __name__ == "__main__":
    main()
