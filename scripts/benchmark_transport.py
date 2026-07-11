from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.transport.framing import EncodedFrame  # noqa: E402
from service.transport.protocol import (  # noqa: E402
    ABI_VERSION,
    CHANNEL_PROVIDER,
    CHANNEL_REMOTE,
    CHANNEL_SYNC,
    FRAME_HEADER_STRUCT,
    LIFECYCLE_STARTING,
    MAGIC,
    MESSAGE_KIND_BYTES,
    MESSAGE_KIND_CLOSE_CHANNEL,
    MESSAGE_KIND_JSON,
    MESSAGE_KIND_OPEN_CHANNEL,
    PROTOCOL_VERSION,
    RING_CONTROL_BLOCK_STRUCT,
    SHARED_MEMORY_HEADER_STRUCT,
    SharedMemoryHeader,
    FrameHeader,
    pack_shared_memory_header,
    unpack_ring_control_block,
)
from service.transport.ring_buffer import NotEnoughData, SharedRingBuffer  # noqa: E402
from service.transport.shared_memory_endpoint import read_frame_from_region, write_frame_to_region  # noqa: E402


JSON_PAYLOAD = b'{"type":"benchmark","value":123,"payload":"small-json"}'


@dataclass(frozen=True)
class LatencyStats:
    p50_ms: float
    p95_ms: float
    p99_ms: float


@dataclass(frozen=True)
class ThroughputStats:
    size_bytes: int
    iterations: int
    mbps: float
    wall_ms: float


@dataclass(frozen=True)
class MessageBurstStats:
    messages: int
    payload_size_bytes: int
    wall_ms: float
    messages_per_second: float
    mib_s: float


@dataclass(frozen=True)
class StartupIdleStats:
    supported: bool
    iterations: int
    startup_p50_ms: float | None = None
    startup_p95_ms: float | None = None
    idle_seconds: float | None = None
    idle_cpu_seconds_avg: float | None = None
    idle_cpu_percent_avg: float | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RemoteStressStats:
    supported: bool
    frames_requested: int
    frame_size_bytes: int
    frames_received: int = 0
    bytes_received: int = 0
    wall_ms: float | None = None
    mib_s: float | None = None
    close_received: bool = False
    dropped_frames: int | None = None
    reason: str | None = None


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark BAAS transport data-plane primitives")
    parser.add_argument("--latency-iterations", type=int, default=500)
    parser.add_argument("--binary-iterations", type=int, default=100)
    parser.add_argument("--large-binary-iterations", type=int, default=20)
    parser.add_argument("--message-burst-count", type=int, default=1000)
    parser.add_argument("--message-burst-payload-size", type=int, default=256)
    parser.add_argument("--startup-iterations", type=int, default=0)
    parser.add_argument("--idle-seconds", type=float, default=2.0)
    parser.add_argument("--remote-stress-frames", type=int, default=0)
    parser.add_argument("--remote-stress-frame-size", type=int, default=64 * 1024)
    parser.add_argument("--remote-stress-region-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--secure-websocket", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print JSON only")
    args = parser.parse_args()

    binary_cases = [
        (1024, args.binary_iterations),
        (64 * 1024, args.binary_iterations),
        (1024 * 1024, args.large_binary_iterations),
    ]

    started = time.perf_counter()
    cpu_started = time.process_time()
    result = {
        "environment": {
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "pid": os.getpid(),
        },
        "latency": {
            "shared_memory": benchmark_latency(shared_memory_roundtrip, args.latency_iterations).__dict__,
            "localhost_websocket": benchmark_latency(websocket_roundtrip, args.latency_iterations).__dict__,
        },
        "binary_throughput": {
            "shared_memory": [
                stat.__dict__ for stat in benchmark_binary(shared_memory_roundtrip, binary_cases)
            ],
            "localhost_websocket": [
                stat.__dict__ for stat in benchmark_binary(websocket_roundtrip, binary_cases)
            ],
        },
        "message_burst": {
            "shared_memory": shared_memory_message_burst(
                args.message_burst_count,
                args.message_burst_payload_size,
            ).__dict__,
            "localhost_websocket": websocket_message_burst(
                args.message_burst_count,
                args.message_burst_payload_size,
            ).__dict__,
        },
    }
    if args.startup_iterations > 0:
        result["process"] = {
            "shared_memory": benchmark_shm_process_startup_idle(
                args.startup_iterations,
                args.idle_seconds,
            ).__dict__
        }
    if args.remote_stress_frames > 0:
        result["remote_media_stress"] = {
            "shared_memory_subprocess": benchmark_remote_media_stress(
                args.remote_stress_frames,
                args.remote_stress_frame_size,
                args.remote_stress_region_bytes,
            ).__dict__
        }
    if args.secure_websocket:
        result["security_overhead"] = benchmark_secure_websocket(
            args.latency_iterations,
            binary_cases,
        )
    result["run"] = {
        "wall_ms": round((time.perf_counter() - started) * 1000, 3),
        "cpu_seconds": round(time.process_time() - cpu_started, 6),
    }

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print_markdown(result)


def benchmark_latency(roundtrip: Callable[[bytes], bytes], iterations: int) -> LatencyStats:
    samples = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        response = roundtrip(JSON_PAYLOAD)
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        if response != JSON_PAYLOAD:
            raise RuntimeError("roundtrip response payload mismatch")
        samples.append(elapsed_ms)
    return LatencyStats(
        p50_ms=round(percentile(samples, 50), 4),
        p95_ms=round(percentile(samples, 95), 4),
        p99_ms=round(percentile(samples, 99), 4),
    )


def benchmark_binary(
    roundtrip: Callable[[bytes], bytes],
    cases: list[tuple[int, int]],
) -> list[ThroughputStats]:
    results = []
    for size, iterations in cases:
        payload = deterministic_payload(size)
        started = time.perf_counter()
        for _ in range(iterations):
            response = roundtrip(payload)
            if response != payload:
                raise RuntimeError("binary response payload mismatch")
        wall = time.perf_counter() - started
        total_mb = (size * iterations * 2) / (1024 * 1024)
        results.append(
            ThroughputStats(
                size_bytes=size,
                iterations=iterations,
                mbps=round(total_mb / wall, 2),
                wall_ms=round(wall * 1000, 3),
            )
        )
    return results


def shared_memory_message_burst(messages: int, payload_size: int) -> MessageBurstStats:
    payloads = burst_payloads(messages, payload_size)
    ring_len = max(
        4 * 1024 * 1024,
        (payload_size + FRAME_HEADER_STRUCT.size + 4 + 64) * max(messages, 1),
    )
    raw_ring = bytearray(ring_len)
    SharedRingBuffer.initialize(raw_ring, 1, 2)
    ring = SharedRingBuffer(raw_ring)
    started = time.perf_counter()
    for index, payload in enumerate(payloads, start=1):
        ring.write_frame(
            EncodedFrame(
                header=FrameHeader(
                    frame_version=1,
                    logical_channel_id=CHANNEL_PROVIDER,
                    stream_id=0,
                    message_kind=MESSAGE_KIND_JSON,
                    flags=0,
                    sequence_number=index,
                    correlation_id=0,
                    payload_length=len(payload),
                    fragment_index=0,
                    fragment_count=1,
                ),
                payload=payload,
            )
        )
    for expected in payloads:
        frame = ring.read_frame(payload_size + 1024)
        if frame.payload != expected:
            raise RuntimeError("shared-memory burst payload mismatch")
    wall = time.perf_counter() - started
    return message_burst_stats(messages, payload_size, wall)


def websocket_message_burst(messages: int, payload_size: int) -> MessageBurstStats:
    echo = get_websocket_echo()
    payloads = burst_payloads(messages, payload_size)
    started = time.perf_counter()
    responses = echo.burst(payloads)
    wall = time.perf_counter() - started
    if responses != payloads:
        raise RuntimeError("websocket burst payload mismatch")
    return message_burst_stats(messages, payload_size, wall)


def burst_payloads(messages: int, payload_size: int) -> list[bytes]:
    if messages <= 0:
        raise ValueError("message burst count must be positive")
    if payload_size <= 0:
        raise ValueError("message burst payload size must be positive")
    payloads = []
    for index in range(messages):
        kind = "status" if index % 2 == 0 else "log"
        base = json.dumps(
            {
                "type": kind,
                "timestamp": index,
                "message": "transport burst benchmark",
            },
            separators=(",", ":"),
        ).encode("utf-8")
        if len(base) >= payload_size:
            payloads.append(base[:payload_size])
            continue
        padding = b"x" * (payload_size - len(base))
        payloads.append(base + padding)
    return payloads


def message_burst_stats(messages: int, payload_size: int, wall: float) -> MessageBurstStats:
    total_mib = (messages * payload_size) / (1024 * 1024)
    return MessageBurstStats(
        messages=messages,
        payload_size_bytes=payload_size,
        wall_ms=round(wall * 1000, 3),
        messages_per_second=round(messages / wall, 2) if wall > 0 else 0.0,
        mib_s=round(total_mib / wall, 2) if wall > 0 else 0.0,
    )


def benchmark_secure_websocket(iterations: int, cases: list[tuple[int, int]]) -> dict:
    try:
        from service.auth.channels import SecretStreamBox
    except Exception as exc:  # noqa: BLE001 - optional WebUI dependency benchmark
        return {
            "supported": False,
            "reason": f"SecretStream benchmark dependencies are unavailable: {type(exc).__name__}: {exc}",
        }
    try:
        reset_secure_websocket_roundtrip(SecretStreamBox)
    except Exception as exc:  # noqa: BLE001 - optional WebUI dependency benchmark
        return {
            "supported": False,
            "reason": f"SecretStream benchmark could not initialize: {type(exc).__name__}: {exc}",
        }
    return {
        "supported": True,
        "latency": benchmark_latency(secure_websocket_roundtrip, iterations).__dict__,
        "binary_throughput": [
            stat.__dict__ for stat in benchmark_binary(secure_websocket_roundtrip, cases)
        ],
    }


def benchmark_shm_process_startup_idle(iterations: int, idle_seconds: float) -> StartupIdleStats:
    if sys.platform != "win32":
        return StartupIdleStats(
            supported=False,
            iterations=iterations,
            reason="native shared-memory subprocess metrics are currently implemented for Windows only",
        )

    from service.transport.native_ipc import create_notification_event, create_shared_memory_region

    startup_samples = []
    idle_cpu_samples = []
    idle_percent_samples = []
    for _ in range(iterations):
        suffix = f"{os.getpid()}-{uuid.uuid4()}"
        shm_name = f"Local\\BAAS-benchmark-shm-{suffix}"
        rust_to_python_event_name = f"Local\\BAAS-benchmark-r2p-{suffix}"
        python_to_rust_event_name = f"Local\\BAAS-benchmark-p2r-{suffix}"
        total_size = 16 * 1024 * 1024
        header = process_metric_header(total_size)
        with (
            create_shared_memory_region(shm_name, total_size) as region,
            create_notification_event(rust_to_python_event_name) as rust_to_python_event,
            create_notification_event(python_to_rust_event_name) as python_to_rust_event,
        ):
            initialize_metric_region(region, header)
            env = os.environ.copy()
            env.update(
                {
                    "BAAS_IPC_REGION_BYTES": str(total_size),
                    "BAAS_ANDROID": "1",
                    "BAAS_SERVICE_TRANSPORT": "shm",
                    "BAAS_SERVICE_OCR_UPDATE_CHECK": "0",
                    "BAAS_UPDATE_CHECK_INTERVAL_SECONDS": "999999",
                }
            )
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "main.service.py",
                    "--transport",
                    "shm",
                    "--ipc-instance",
                    f"benchmark-{suffix}",
                    "--parent-pid",
                    str(os.getpid()),
                    "--shm-name",
                    shm_name,
                    "--rust-to-python-notify-name",
                    rust_to_python_event_name,
                    "--python-to-rust-notify-name",
                    python_to_rust_event_name,
                    "--no-ocr-update-check",
                ],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                started = time.perf_counter()
                if not python_to_rust_event.wait(10_000):
                    raise RuntimeError(f"shm backend did not signal readiness: {subprocess_output(proc)}")
                startup_samples.append((time.perf_counter() - started) * 1000)

                if idle_seconds > 0:
                    cpu_before = child_cpu_seconds(proc.pid)
                    time.sleep(idle_seconds)
                    cpu_after = child_cpu_seconds(proc.pid)
                    if cpu_before is not None and cpu_after is not None:
                        cpu_delta = max(0.0, cpu_after - cpu_before)
                        idle_cpu_samples.append(cpu_delta)
                        idle_percent_samples.append((cpu_delta / idle_seconds) * 100)
            finally:
                terminate_backend_process(proc, rust_to_python_event)

    return StartupIdleStats(
        supported=True,
        iterations=iterations,
        startup_p50_ms=round(percentile(startup_samples, 50), 3),
        startup_p95_ms=round(percentile(startup_samples, 95), 3),
        idle_seconds=idle_seconds,
        idle_cpu_seconds_avg=round(sum(idle_cpu_samples) / len(idle_cpu_samples), 6)
        if idle_cpu_samples
        else None,
        idle_cpu_percent_avg=round(sum(idle_percent_samples) / len(idle_percent_samples), 3)
        if idle_percent_samples
        else None,
    )


def benchmark_remote_media_stress(
    frames: int,
    frame_size: int,
    region_bytes: int,
) -> RemoteStressStats:
    if sys.platform not in {"win32", "linux", "darwin"}:
        return RemoteStressStats(
            supported=False,
            frames_requested=frames,
            frame_size_bytes=frame_size,
            reason="native shared-memory subprocess metrics require Windows, Linux, or macOS",
        )
    if frames <= 0:
        raise ValueError("remote stress frame count must be positive")
    if frame_size <= 0:
        raise ValueError("remote stress frame size must be positive")
    if region_bytes < 64 * 1024:
        raise ValueError("remote stress shared-memory region must be at least 64 KiB")

    from service.transport.native_ipc import create_notification_event, create_shared_memory_region

    suffix = f"{os.getpid()}-{uuid.uuid4()}"
    shm_name = native_resource_name("benchmark-remote-shm", suffix)
    rust_to_python_event_name = native_resource_name("benchmark-remote-r2p", suffix)
    python_to_rust_event_name = native_resource_name("benchmark-remote-p2r", suffix)
    header = process_metric_header(region_bytes)
    stream_id = 17
    with (
        create_shared_memory_region(shm_name, region_bytes) as region,
        create_notification_event(rust_to_python_event_name) as rust_to_python_event,
        create_notification_event(python_to_rust_event_name) as python_to_rust_event,
    ):
        initialize_metric_region(region, header)
        env = os.environ.copy()
        env.update(
            {
                "BAAS_IPC_REGION_BYTES": str(region_bytes),
                "BAAS_ANDROID": "1",
                "BAAS_SERVICE_TRANSPORT": "shm",
                "BAAS_SERVICE_OCR_UPDATE_CHECK": "0",
                "BAAS_UPDATE_CHECK_INTERVAL_SECONDS": "999999",
                "BAAS_SHM_TEST_COMMANDS": "1",
            }
        )
        proc = subprocess.Popen(
            [
                sys.executable,
                "main.service.py",
                "--transport",
                "shm",
                "--ipc-instance",
                f"benchmark-remote-{suffix}",
                "--parent-pid",
                str(os.getpid()),
                "--shm-name",
                shm_name,
                "--rust-to-python-notify-name",
                rust_to_python_event_name,
                "--python-to-rust-notify-name",
                python_to_rust_event_name,
                "--no-ocr-update-check",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            if not python_to_rust_event.wait(10_000):
                raise RuntimeError(f"remote stress shm backend did not signal readiness: {subprocess_output(proc)}")
            write_benchmark_inbound_frame(
                region,
                header,
                MESSAGE_KIND_OPEN_CHANNEL,
                b'{"name":"remote-benchmark"}',
                sequence_number=1,
                channel_id=CHANNEL_REMOTE,
                stream_id=stream_id,
            )
            write_benchmark_inbound_frame(
                region,
                header,
                MESSAGE_KIND_JSON,
                json.dumps(
                    {
                        "config_id": "transport_remote_test",
                        "frame_count": frames,
                        "frame_size": frame_size,
                    },
                    separators=(",", ":"),
                ).encode("utf-8"),
                sequence_number=2,
                channel_id=CHANNEL_REMOTE,
                stream_id=stream_id,
            )
            write_benchmark_inbound_frame(
                region,
                header,
                MESSAGE_KIND_BYTES,
                b"remote-control",
                sequence_number=3,
                channel_id=CHANNEL_REMOTE,
                stream_id=stream_id,
            )
            started = time.perf_counter()
            rust_to_python_event.set()
            received_frames = 0
            received_bytes = 0
            close_received = False
            deadline = started + 30
            while time.perf_counter() < deadline and not close_received:
                try:
                    outbound = read_frame_from_region(
                        region,
                        header.python_to_rust_ring_offset,
                        header.python_to_rust_ring_length,
                    )
                except NotEnoughData:
                    python_to_rust_event.wait(100)
                    continue
                if (
                    outbound.header.logical_channel_id != CHANNEL_REMOTE
                    or outbound.header.stream_id != stream_id
                ):
                    continue
                if outbound.header.message_kind == MESSAGE_KIND_BYTES:
                    received_frames += 1
                    received_bytes += len(outbound.payload)
                elif outbound.header.message_kind == MESSAGE_KIND_CLOSE_CHANNEL:
                    close_received = True
            wall = time.perf_counter() - started
            dropped = outbound_dropped_frames(region, header)
            return RemoteStressStats(
                supported=True,
                frames_requested=frames,
                frame_size_bytes=frame_size,
                frames_received=received_frames,
                bytes_received=received_bytes,
                wall_ms=round(wall * 1000, 3),
                mib_s=round((received_bytes / (1024 * 1024)) / wall, 2) if wall > 0 else 0.0,
                close_received=close_received,
                dropped_frames=dropped,
                reason=None if close_received else "remote stress timed out before close frame",
            )
        finally:
            terminate_backend_process(proc, rust_to_python_event)


def process_metric_header(total_size: int) -> SharedMemoryHeader:
    data_offset = 128
    ring_length = (total_size - data_offset) // 2
    generation = uuid.uuid4().int
    return SharedMemoryHeader(
        magic=MAGIC,
        protocol_version=PROTOCOL_VERSION,
        abi_version=ABI_VERSION,
        header_size=SHARED_MEMORY_HEADER_STRUCT.size,
        total_size=total_size,
        generation_id_low=generation & ((1 << 64) - 1),
        generation_id_high=generation >> 64,
        owner_pid=os.getpid(),
        peer_pid=0,
        lifecycle_state=LIFECYCLE_STARTING,
        last_error_code=0,
        owner_heartbeat_ns=0,
        peer_heartbeat_ns=0,
        rust_to_python_ring_offset=data_offset,
        rust_to_python_ring_length=ring_length,
        python_to_rust_ring_offset=data_offset + ring_length,
        python_to_rust_ring_length=ring_length,
        control_lane_offset=data_offset,
        control_lane_length=ring_length // 8,
        message_lane_offset=data_offset + ring_length // 8,
        message_lane_length=ring_length // 2,
        bulk_lane_offset=data_offset + ring_length,
        bulk_lane_length=ring_length // 4,
        remote_lane_offset=data_offset + ring_length + ring_length // 4,
        remote_lane_length=ring_length - ring_length // 4,
        last_error_offset=0,
        last_error_length=0,
    )


def initialize_metric_region(region, header: SharedMemoryHeader) -> None:
    region.write(0, pack_shared_memory_header(header))
    for offset, length in [
        (header.rust_to_python_ring_offset, header.rust_to_python_ring_length),
        (header.python_to_rust_ring_offset, header.python_to_rust_ring_length),
    ]:
        raw_ring = bytearray(region.read(offset, length))
        SharedRingBuffer.initialize(raw_ring, header.generation_id_low, header.generation_id_high)
        region.write(offset, raw_ring)


def write_benchmark_inbound_frame(
    region,
    header: SharedMemoryHeader,
    message_kind: int,
    payload: bytes,
    *,
    sequence_number: int,
    channel_id: int,
    stream_id: int,
) -> None:
    frame = EncodedFrame(
        header=FrameHeader(
            frame_version=1,
            logical_channel_id=channel_id,
            stream_id=stream_id,
            message_kind=message_kind,
            flags=0,
            sequence_number=sequence_number,
            correlation_id=0,
            payload_length=len(payload),
            fragment_index=0,
            fragment_count=1,
        ),
        payload=payload,
    )
    write_frame_to_region(
        region,
        header.rust_to_python_ring_offset,
        header.rust_to_python_ring_length,
        frame,
    )


def outbound_dropped_frames(region, header: SharedMemoryHeader) -> int:
    raw = region.read(header.python_to_rust_ring_offset, RING_CONTROL_BLOCK_STRUCT.size)
    return unpack_ring_control_block(raw).dropped_frames


def native_resource_name(kind: str, suffix: str) -> str:
    if sys.platform == "win32":
        return f"Local\\BAAS-{kind}-{suffix}"
    return f"/baas-{kind}-{suffix}"


def child_cpu_seconds(pid: int) -> float | None:
    if sys.platform != "win32":
        return None
    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetProcessTimes.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong),
    )
    kernel32.GetProcessTimes.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        creation = ctypes.c_ulonglong()
        exit_time = ctypes.c_ulonglong()
        kernel = ctypes.c_ulonglong()
        user = ctypes.c_ulonglong()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        return (kernel.value + user.value) / 10_000_000
    finally:
        kernel32.CloseHandle(handle)


def terminate_backend_process(proc: subprocess.Popen, rust_to_python_event) -> None:
    proc.terminate()
    with suppress(Exception):
        rust_to_python_event.set()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        with suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=1)
    with suppress(Exception):
        proc.communicate(timeout=1)


def subprocess_output(proc: subprocess.Popen) -> str:
    try:
        stdout, stderr = proc.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        return "subprocess is still running"
    return f"stdout:\n{stdout}\nstderr:\n{stderr}"


class SharedMemoryEcho:
    def __init__(self, ring_len: int = 4 * 1024 * 1024) -> None:
        self.rust_to_python = bytearray(ring_len)
        self.python_to_rust = bytearray(ring_len)
        self.sequence = 1
        SharedRingBuffer.initialize(self.rust_to_python, 1, 2)
        SharedRingBuffer.initialize(self.python_to_rust, 1, 2)

    def roundtrip(self, payload: bytes) -> bytes:
        message_kind = MESSAGE_KIND_BYTES if len(payload) > len(JSON_PAYLOAD) else MESSAGE_KIND_JSON
        self.write_frame(self.rust_to_python, payload, message_kind)
        inbound = SharedRingBuffer(self.rust_to_python).read_frame(len(payload) + 1024)
        self.write_frame(self.python_to_rust, inbound.payload, inbound.header.message_kind)
        outbound = SharedRingBuffer(self.python_to_rust).read_frame(len(payload) + 1024)
        return outbound.payload

    def write_frame(self, region: bytearray, payload: bytes, message_kind: int) -> None:
        frame = EncodedFrame(
            header=FrameHeader(
                frame_version=1,
                logical_channel_id=CHANNEL_SYNC,
                stream_id=0,
                message_kind=message_kind,
                flags=0,
                sequence_number=self.sequence,
                correlation_id=0,
                payload_length=len(payload),
                fragment_index=0,
                fragment_count=1,
            ),
            payload=payload,
        )
        self.sequence = (self.sequence + 1) & 0xFFFFFFFFFFFFFFFF or 1
        SharedRingBuffer(region).write_frame(frame)


_SHARED_MEMORY_ECHO: SharedMemoryEcho | None = None


def shared_memory_roundtrip(payload: bytes) -> bytes:
    global _SHARED_MEMORY_ECHO
    if _SHARED_MEMORY_ECHO is None:
        _SHARED_MEMORY_ECHO = SharedMemoryEcho()
    return _SHARED_MEMORY_ECHO.roundtrip(payload)


class WebSocketEcho:
    def __init__(self) -> None:
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.port = 0
        self.client: Optional[socket.socket] = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            raise RuntimeError("websocket echo server did not start")

    def roundtrip(self, payload: bytes) -> bytes:
        if self.client is None:
            self.client = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            websocket_handshake(self.client)
        send_ws_frame(self.client, payload, opcode=2)
        opcode, response = recv_ws_frame(self.client)
        if opcode != 2:
            raise RuntimeError(f"unexpected websocket opcode {opcode}")
        return response

    def burst(self, payloads: list[bytes]) -> list[bytes]:
        if self.client is None:
            self.client = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            websocket_handshake(self.client)
        for payload in payloads:
            send_ws_frame(self.client, payload, opcode=2)
        responses = []
        for _ in payloads:
            opcode, response = recv_ws_frame(self.client)
            if opcode != 2:
                raise RuntimeError(f"unexpected websocket opcode {opcode}")
            responses.append(response)
        return responses

    def _run(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            server.listen()
            self.port = server.getsockname()[1]
            self.ready.set()
            while not self.stopped.is_set():
                try:
                    server.settimeout(0.2)
                    conn, _addr = server.accept()
                except socket.timeout:
                    continue
                with conn:
                    try:
                        accept_websocket(conn)
                        while True:
                            opcode, payload = recv_ws_frame(conn)
                            if opcode == 8:
                                break
                            send_ws_frame(conn, payload, opcode=opcode, mask=False)
                    except OSError:
                        pass


_WEBSOCKET_ECHO: WebSocketEcho | None = None


def websocket_roundtrip(payload: bytes) -> bytes:
    return get_websocket_echo().roundtrip(payload)


def get_websocket_echo() -> WebSocketEcho:
    global _WEBSOCKET_ECHO
    if _WEBSOCKET_ECHO is None:
        _WEBSOCKET_ECHO = WebSocketEcho()
    return _WEBSOCKET_ECHO


class SecureWebSocketEcho:
    def __init__(self, secret_stream_box_cls) -> None:
        client_tx = hashlib.sha256(b"baas-secure-websocket-benchmark-client").digest()
        server_tx = hashlib.sha256(b"baas-secure-websocket-benchmark-server").digest()
        aad_prefix = b"baas:webui:benchmark:"
        self.client_stream = secret_stream_box_cls(
            tx_key=client_tx,
            rx_key=server_tx,
            aad_prefix=aad_prefix,
        )
        self.server_stream = secret_stream_box_cls(
            tx_key=server_tx,
            rx_key=client_tx,
            aad_prefix=aad_prefix,
        )
        self.client_stream.init_pull(self.server_stream.tx_header)
        self.server_stream.init_pull(self.client_stream.tx_header)
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.port = 0
        self.client: Optional[socket.socket] = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            raise RuntimeError("secure websocket echo server did not start")

    def roundtrip(self, payload: bytes) -> bytes:
        if self.client is None:
            self.client = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            websocket_handshake(self.client)
        send_ws_frame(self.client, self.client_stream.encrypt(payload), opcode=2)
        opcode, response = recv_ws_frame(self.client)
        if opcode != 2:
            raise RuntimeError(f"unexpected secure websocket opcode {opcode}")
        return self.client_stream.decrypt(response)

    def _run(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            server.listen()
            self.port = server.getsockname()[1]
            self.ready.set()
            while not self.stopped.is_set():
                try:
                    server.settimeout(0.2)
                    conn, _addr = server.accept()
                except socket.timeout:
                    continue
                with conn:
                    try:
                        accept_websocket(conn)
                        while True:
                            opcode, ciphertext = recv_ws_frame(conn)
                            if opcode == 8:
                                break
                            plaintext = self.server_stream.decrypt(ciphertext)
                            send_ws_frame(
                                conn,
                                self.server_stream.encrypt(plaintext),
                                opcode=opcode,
                                mask=False,
                            )
                    except OSError:
                        pass


_SECURE_WEBSOCKET_ECHO: SecureWebSocketEcho | None = None


def reset_secure_websocket_roundtrip(secret_stream_box_cls) -> None:
    global _SECURE_WEBSOCKET_ECHO
    _SECURE_WEBSOCKET_ECHO = SecureWebSocketEcho(secret_stream_box_cls)


def secure_websocket_roundtrip(payload: bytes) -> bytes:
    if _SECURE_WEBSOCKET_ECHO is None:
        raise RuntimeError("secure websocket benchmark is not initialized")
    return _SECURE_WEBSOCKET_ECHO.roundtrip(payload)


def websocket_handshake(sock: socket.socket) -> None:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        "GET /benchmark HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(request.encode("ascii"))
    response = read_until(sock, b"\r\n\r\n")
    if b" 101 " not in response.split(b"\r\n", 1)[0]:
        raise RuntimeError(f"websocket handshake failed: {response[:80]!r}")


def accept_websocket(sock: socket.socket) -> None:
    request = read_until(sock, b"\r\n\r\n").decode("latin1")
    headers = {}
    for line in request.split("\r\n")[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    ws_key = headers["sec-websocket-key"]
    accept = base64.b64encode(
        hashlib.sha1((ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
    ).decode("ascii")
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    )
    sock.sendall(response.encode("ascii"))


def send_ws_frame(sock: socket.socket, payload: bytes, opcode: int, *, mask: bool = True) -> None:
    first = 0x80 | opcode
    mask_bit = 0x80 if mask else 0
    length = len(payload)
    if length < 126:
        header = bytes([first, mask_bit | length])
    elif length <= 0xFFFF:
        header = bytes([first, mask_bit | 126]) + struct.pack("!H", length)
    else:
        header = bytes([first, mask_bit | 127]) + struct.pack("!Q", length)
    if mask:
        key = os.urandom(4)
        payload = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
        header += key
    sock.sendall(header + payload)


def recv_ws_frame(sock: socket.socket) -> tuple[int, bytes]:
    first, second = read_exact(sock, 2)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", read_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read_exact(sock, 8))[0]
    mask = read_exact(sock, 4) if masked else b""
    payload = read_exact(sock, length)
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload


def read_until(sock: socket.socket, marker: bytes) -> bytes:
    data = bytearray()
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("socket closed before marker")
        data.extend(chunk)
    return bytes(data)


def read_exact(sock: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise OSError("socket closed while reading frame")
        data.extend(chunk)
    return bytes(data)


def deterministic_payload(size: int) -> bytes:
    chunk = hashlib.sha256(str(size).encode("ascii")).digest()
    return (chunk * ((size // len(chunk)) + 1))[:size]


def percentile(samples: list[float], percent: int) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, int(round((percent / 100) * (len(ordered) - 1))))
    return ordered[index]


def print_markdown(result: dict) -> None:
    print("# BAAS Transport Benchmark")
    print()
    print(f"- Platform: `{result['environment']['platform']}`")
    print(f"- Python: `{result['environment']['python']}`")
    print(f"- Wall time: `{result['run']['wall_ms']} ms`")
    print(f"- CPU time: `{result['run']['cpu_seconds']} s`")
    print()
    print("## Small JSON Echo Latency")
    print()
    print("| Transport | p50 ms | p95 ms | p99 ms |")
    print("| --- | ---: | ---: | ---: |")
    for name, stats in result["latency"].items():
        print(f"| {name} | {stats['p50_ms']} | {stats['p95_ms']} | {stats['p99_ms']} |")
    print()
    print("## Binary Echo Throughput")
    print()
    print("| Transport | Payload | Iterations | MiB/s | Wall ms |")
    print("| --- | ---: | ---: | ---: | ---: |")
    for name, rows in result["binary_throughput"].items():
        for row in rows:
            print(f"| {name} | {row['size_bytes']} | {row['iterations']} | {row['mbps']} | {row['wall_ms']} |")
    print()
    print("## Status/Log Message Burst")
    print()
    print("| Transport | Messages | Payload bytes | Messages/s | MiB/s | Wall ms |")
    print("| --- | ---: | ---: | ---: | ---: | ---: |")
    for name, stats in result["message_burst"].items():
        print(
            f"| {name} | {stats['messages']} | {stats['payload_size_bytes']} | "
            f"{stats['messages_per_second']} | {stats['mib_s']} | {stats['wall_ms']} |"
        )
    if "process" in result:
        print()
        print("## Process Startup And Idle")
        print()
        print("| Transport | Startup p50 ms | Startup p95 ms | Idle seconds | Idle CPU seconds avg | Idle CPU % avg |")
        print("| --- | ---: | ---: | ---: | ---: | ---: |")
        for name, stats in result["process"].items():
            if not stats["supported"]:
                print(f"| {name} | n/a | n/a | n/a | n/a | n/a |")
                continue
            print(
                f"| {name} | {stats['startup_p50_ms']} | {stats['startup_p95_ms']} | "
                f"{stats['idle_seconds']} | {stats['idle_cpu_seconds_avg']} | "
                f"{stats['idle_cpu_percent_avg']} |"
            )
    if "remote_media_stress" in result:
        print()
        print("## Remote Media Stress")
        print()
        print("| Transport | Frames requested | Frame bytes | Frames received | MiB/s | Wall ms | Dropped frames | Close |")
        print("| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
        for name, stats in result["remote_media_stress"].items():
            if not stats["supported"]:
                print(f"| {name} | {stats['frames_requested']} | {stats['frame_size_bytes']} | n/a | n/a | n/a | n/a | no |")
                continue
            print(
                f"| {name} | {stats['frames_requested']} | {stats['frame_size_bytes']} | "
                f"{stats['frames_received']} | {stats['mib_s']} | {stats['wall_ms']} | "
                f"{stats['dropped_frames']} | {stats['close_received']} |"
            )
    if "security_overhead" in result:
        print()
        print("## WebUI SecretStream Overhead")
        print()
        security = result["security_overhead"]
        if not security["supported"]:
            print(f"- Not measured: `{security['reason']}`")
            return
        latency = security["latency"]
        print("| Metric | p50 ms | p95 ms | p99 ms |")
        print("| --- | ---: | ---: | ---: |")
        print(f"| encrypted websocket latency | {latency['p50_ms']} | {latency['p95_ms']} | {latency['p99_ms']} |")
        print()
        print("| Payload | Iterations | MiB/s | Wall ms |")
        print("| ---: | ---: | ---: | ---: |")
        for row in security["binary_throughput"]:
            print(f"| {row['size_bytes']} | {row['iterations']} | {row['mbps']} | {row['wall_ms']} |")


if __name__ == "__main__":
    main()
