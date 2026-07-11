from __future__ import annotations

import asyncio
import builtins
import importlib.util
import os
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from service.transport.protocol import (
    ABI_VERSION,
    LIFECYCLE_STARTING,
    LIFECYCLE_STOPPED,
    MAGIC,
    MESSAGE_KIND_OPEN_CHANNEL,
    PROTOCOL_VERSION,
    RING_CONTROL_BLOCK_STRUCT,
    SHARED_MEMORY_HEADER_STRUCT,
    SharedMemoryHeader,
    new_ring_control_block,
    pack_ring_control_block,
    pack_shared_memory_header,
    unpack_shared_memory_header,
)
from service.transport.framing import fragment_payload
from service.transport.ring_buffer import SharedRingBuffer
from service.transport.native_ipc import (
    NativeIpcError,
    create_notification_event,
    create_shared_memory_region,
    open_notification_event,
    open_shared_memory_region,
)
from service.transport.shared_memory_server import (
    SharedMemoryLaunchOptions,
    _run_shared_memory_server_with_context,
    run_shared_memory_server,
)


class FakeRegion:
    def __init__(self, data: bytearray) -> None:
        self.data = data
        self.size = len(data)

    def __enter__(self) -> "FakeRegion":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, offset: int, length: int) -> bytes:
        return bytes(self.data[offset : offset + length])

    def write(self, offset: int, data: bytes) -> None:
        self.data[offset : offset + len(data)] = data


class FakeEvent:
    def __init__(self) -> None:
        self.signaled = False

    def __enter__(self) -> "FakeEvent":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def set(self) -> None:
        self.signaled = True

    def wait(self, _timeout_ms: int) -> bool:
        return True


class SharedMemoryLaunchModeTests(unittest.TestCase):
    def test_native_ipc_helpers_report_unsupported_platform(self) -> None:
        with (
            patch("service.transport.native_ipc._HAS_WINDOWS_NATIVE", False),
            patch("service.transport.native_ipc._HAS_POSIX_NATIVE", False),
        ):
            with self.assertRaisesRegex(NativeIpcError, "not implemented on this platform"):
                create_shared_memory_region("/baas-test-shm", 4096)
            with self.assertRaisesRegex(NativeIpcError, "not implemented on this platform"):
                open_shared_memory_region("/baas-test-shm", 4096)
            with self.assertRaisesRegex(NativeIpcError, "not implemented on this platform"):
                create_notification_event("/baas-test-event")
            with self.assertRaisesRegex(NativeIpcError, "not implemented on this platform"):
                open_notification_event("/baas-test-event")

    def test_shm_transport_does_not_require_web_service_dependencies(self) -> None:
        root = Path(__file__).resolve().parents[2]

        result = subprocess.run(
            [
                sys.executable,
                "main.service.py",
                "--transport",
                "shm",
                "--ipc-instance",
                "test-instance",
                "--parent-pid",
                "1",
            ],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )

        combined = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--transport shm requires --shm-name", combined)
        self.assertNotIn("No module named 'fastapi'", combined)
        self.assertNotIn("No module named 'uvicorn'", combined)
        self.assertNotIn("No module named 'rich'", combined)

    def test_shm_main_ignores_host_port_and_does_not_touch_network(self) -> None:
        root = Path(__file__).resolve().parents[2]
        module = _load_main_service_module(root)
        captured = {}

        async def fake_run_shared_memory_server(options):
            captured["options"] = options

        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "uvicorn":
                raise AssertionError("shm transport must not import uvicorn")
            return real_import(name, *args, **kwargs)

        def fail_socket(*_args, **_kwargs):
            raise AssertionError("shm transport must not create network sockets")

        def run_immediate(coro):
            try:
                coro.send(None)
            except StopIteration as exc:
                return exc.value
            finally:
                coro.close()
            raise AssertionError("fake shared-memory server coroutine unexpectedly awaited")

        with (
            patch.object(
                sys,
                "argv",
                [
                    "main.service.py",
                    "--transport",
                    "shm",
                    "--ipc-instance",
                    "test-instance",
                    "--parent-pid",
                    "1",
                    "--shm-name",
                    "Local\\BAAS-test-shm",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "9",
                    "--no-ocr-update-check",
                ],
            ),
            patch.object(module, "run_shared_memory_server", fake_run_shared_memory_server),
            patch.object(module.asyncio, "run", run_immediate),
            patch.object(builtins, "__import__", guarded_import),
            patch.object(socket, "socket", fail_socket),
        ):
            module.main()

        options = captured["options"]
        self.assertEqual(options.ipc_instance, "test-instance")
        self.assertEqual(options.parent_pid, 1)
        self.assertEqual(options.shm_name, "Local\\BAAS-test-shm")
        self.assertFalse(hasattr(options, "host"))
        self.assertFalse(hasattr(options, "port"))

    def test_shm_server_updates_header_and_signals_event(self) -> None:
        header = SharedMemoryHeader(
            magic=MAGIC,
            protocol_version=PROTOCOL_VERSION,
            abi_version=ABI_VERSION,
            header_size=SHARED_MEMORY_HEADER_STRUCT.size,
            total_size=4096,
            generation_id_low=1,
            generation_id_high=2,
            owner_pid=111,
            peer_pid=0,
            lifecycle_state=LIFECYCLE_STARTING,
            last_error_code=0,
            owner_heartbeat_ns=0,
            peer_heartbeat_ns=0,
            rust_to_python_ring_offset=128,
            rust_to_python_ring_length=1024,
            python_to_rust_ring_offset=1152,
            python_to_rust_ring_length=1024,
            control_lane_offset=128,
            control_lane_length=128,
            message_lane_offset=256,
            message_lane_length=896,
            bulk_lane_offset=1152,
            bulk_lane_length=512,
            remote_lane_offset=1664,
            remote_lane_length=512,
            last_error_offset=0,
            last_error_length=0,
        )
        region_data = bytearray(4096)
        region_data[: SHARED_MEMORY_HEADER_STRUCT.size] = pack_shared_memory_header(header)
        for offset, length in [
            (header.rust_to_python_ring_offset, header.rust_to_python_ring_length),
            (header.python_to_rust_ring_offset, header.python_to_rust_ring_length),
        ]:
            region_data[offset : offset + RING_CONTROL_BLOCK_STRUCT.size] = pack_ring_control_block(
                new_ring_control_block(
                    length - RING_CONTROL_BLOCK_STRUCT.size,
                    header.generation_id_low,
                    header.generation_id_high,
                )
            )
        fake_region = FakeRegion(region_data)
        fake_event = FakeEvent()

        with (
            patch("service.transport.shared_memory_server.open_shared_memory_region", return_value=fake_region),
            patch("service.transport.shared_memory_server.open_notification_event", return_value=fake_event),
        ):
            asyncio.run(
                _run_shared_memory_server_with_context(
                    SharedMemoryLaunchOptions(
                        ipc_instance="test-instance",
                        parent_pid=0,
                        project_root=Path("."),
                        shm_name="Local\\BAAS-test-shm",
                        notify_name="Local\\BAAS-test-event",
                    ),
                    _noop_handler_factory,
                )
            )

        updated = unpack_shared_memory_header(bytes(region_data[: SHARED_MEMORY_HEADER_STRUCT.size]))
        self.assertEqual(updated.peer_pid, os.getpid())
        self.assertEqual(updated.lifecycle_state, LIFECYCLE_STOPPED)
        self.assertTrue(fake_event.signaled)

    def test_shm_server_drains_inbound_frame(self) -> None:
        header = SharedMemoryHeader(
            magic=MAGIC,
            protocol_version=PROTOCOL_VERSION,
            abi_version=ABI_VERSION,
            header_size=SHARED_MEMORY_HEADER_STRUCT.size,
            total_size=4096,
            generation_id_low=1,
            generation_id_high=2,
            owner_pid=111,
            peer_pid=0,
            lifecycle_state=LIFECYCLE_STARTING,
            last_error_code=0,
            owner_heartbeat_ns=0,
            peer_heartbeat_ns=0,
            rust_to_python_ring_offset=128,
            rust_to_python_ring_length=1024,
            python_to_rust_ring_offset=1152,
            python_to_rust_ring_length=1024,
            control_lane_offset=128,
            control_lane_length=128,
            message_lane_offset=256,
            message_lane_length=896,
            bulk_lane_offset=1152,
            bulk_lane_length=512,
            remote_lane_offset=1664,
            remote_lane_length=512,
            last_error_offset=0,
            last_error_length=0,
        )
        region_data = bytearray(4096)
        region_data[: SHARED_MEMORY_HEADER_STRUCT.size] = pack_shared_memory_header(header)
        for offset, length in [
            (header.rust_to_python_ring_offset, header.rust_to_python_ring_length),
            (header.python_to_rust_ring_offset, header.python_to_rust_ring_length),
        ]:
            region_data[offset : offset + RING_CONTROL_BLOCK_STRUCT.size] = pack_ring_control_block(
                new_ring_control_block(
                    length - RING_CONTROL_BLOCK_STRUCT.size,
                    header.generation_id_low,
                    header.generation_id_high,
                )
            )
        ring_region = bytearray(
            region_data[
                header.rust_to_python_ring_offset : header.rust_to_python_ring_offset
                + header.rust_to_python_ring_length
            ]
        )
        ring = SharedRingBuffer(ring_region)
        frame = fragment_payload(2, 0, MESSAGE_KIND_OPEN_CHANNEL, 0, 1, 0, b'{"name":"sync"}', 1024)[0]
        ring.write_frame(frame)
        region_data[
            header.rust_to_python_ring_offset : header.rust_to_python_ring_offset
            + header.rust_to_python_ring_length
        ] = ring_region
        fake_region = FakeRegion(region_data)
        fake_event = FakeEvent()

        with (
            patch("service.transport.shared_memory_server.open_shared_memory_region", return_value=fake_region),
            patch("service.transport.shared_memory_server.open_notification_event", return_value=fake_event),
            patch("service.transport.shared_memory_server._parent_alive", side_effect=[True, False]),
        ):
            asyncio.run(
                _run_shared_memory_server_with_context(
                    SharedMemoryLaunchOptions(
                        ipc_instance="test-instance",
                        parent_pid=111,
                        project_root=Path("."),
                        shm_name="Local\\BAAS-test-shm",
                        notify_name="Local\\BAAS-test-event",
                    ),
                    _noop_handler_factory,
                )
            )

        drained_ring = SharedRingBuffer(
            bytearray(
                region_data[
                    header.rust_to_python_ring_offset : header.rust_to_python_ring_offset
                    + header.rust_to_python_ring_length
                ]
            )
        )
        self.assertEqual(drained_ring.available_read, 0)
        updated = unpack_shared_memory_header(bytes(region_data[: SHARED_MEMORY_HEADER_STRUCT.size]))
        self.assertEqual(updated.lifecycle_state, LIFECYCLE_STOPPED)


async def _noop_handler(_endpoint) -> None:
    return None


def _noop_handler_factory(_channel_name: str):
    return _noop_handler


def _load_main_service_module(root: Path):
    spec = importlib.util.spec_from_file_location("baas_main_service_for_tests", root / "main.service.py")
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    unittest.main()
