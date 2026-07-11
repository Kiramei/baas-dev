from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import unittest
import uuid
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

from service.transport.framing import fragment_payload
from service.transport.native_ipc import create_notification_event, create_shared_memory_region
from service.transport.protocol import (
    ABI_VERSION,
    CHANNEL_PROVIDER,
    CHANNEL_REMOTE,
    CHANNEL_SYNC,
    CHANNEL_TRIGGER,
    LIFECYCLE_READY,
    LIFECYCLE_STARTING,
    LIFECYCLE_STOPPED,
    MAGIC,
    MESSAGE_KIND_BYTES,
    MESSAGE_KIND_CLOSE_CHANNEL,
    MESSAGE_KIND_JSON,
    MESSAGE_KIND_OPEN_CHANNEL,
    PROTOCOL_VERSION,
    RING_CONTROL_BLOCK_STRUCT,
    SHARED_MEMORY_HEADER_STRUCT,
    SharedMemoryHeader,
    pack_shared_memory_header,
    unpack_shared_memory_header,
)
from service.transport.ring_buffer import NotEnoughData, SharedRingBuffer
from service.transport.shared_memory_endpoint import read_frame_from_region, write_frame_to_region
from service.transport.shared_memory_server import SharedMemoryLaunchOptions, _run_shared_memory_server_with_context


@unittest.skipUnless(
    sys.platform in {"win32", "linux", "darwin"},
    "native named shared memory integration requires Windows, Linux, or macOS",
)
class NativeSharedMemoryServerIntegrationTests(unittest.TestCase):
    def test_native_named_ipc_round_trips_json_and_binary_frames(self) -> None:
        asyncio.run(_native_roundtrip_scenario())

    def test_main_service_shm_subprocess_signals_ready_without_webui_auth(self) -> None:
        _main_service_subprocess_ready_scenario()

    def test_main_service_shm_subprocess_handles_sync_and_provider_channels(self) -> None:
        _main_service_subprocess_channels_scenario()

    def test_main_service_shm_subprocess_handles_trigger_commands_and_binary_frames(self) -> None:
        _main_service_subprocess_trigger_scenario()

    def test_main_service_shm_subprocess_handles_trigger_stream_responses(self) -> None:
        _main_service_subprocess_trigger_stream_scenario()

    def test_main_service_shm_subprocess_handles_remote_binary_stream_and_close(self) -> None:
        _main_service_subprocess_remote_scenario()


def _main_service_subprocess_ready_scenario() -> None:
    suffix = f"{os.getpid()}-{uuid.uuid4()}"
    shm_name = _native_name("main-test-shm", suffix)
    rust_to_python_event_name = _native_name("main-test-r2p", suffix)
    python_to_rust_event_name = _native_name("main-test-p2r", suffix)
    total_size = 4096
    header = _test_header(total_size)
    root = Path(__file__).resolve().parents[2]

    with (
        create_shared_memory_region(shm_name, total_size) as region,
        create_notification_event(rust_to_python_event_name) as rust_to_python_event,
        create_notification_event(python_to_rust_event_name) as python_to_rust_event,
    ):
        _initialize_region(region, header)
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
                f"main-test-{suffix}",
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
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert python_to_rust_event.wait(5000), _subprocess_output(proc)
            updated = unpack_shared_memory_header(region.read(0, SHARED_MEMORY_HEADER_STRUCT.size))
            assert updated.peer_pid == proc.pid
            assert updated.lifecycle_state == LIFECYCLE_READY
            assert proc.poll() is None
        finally:
            proc.terminate()
            rust_to_python_event.set()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                with suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=1)
            with suppress(Exception):
                proc.communicate(timeout=1)


def _main_service_subprocess_channels_scenario() -> None:
    suffix = f"{os.getpid()}-{uuid.uuid4()}"
    shm_name = _native_name("main-test-channels-shm", suffix)
    rust_to_python_event_name = _native_name("main-test-channels-r2p", suffix)
    python_to_rust_event_name = _native_name("main-test-channels-p2r", suffix)
    total_size = 16 * 1024
    header = _test_header(total_size)
    root = Path(__file__).resolve().parents[2]

    with (
        create_shared_memory_region(shm_name, total_size) as region,
        create_notification_event(rust_to_python_event_name) as rust_to_python_event,
        create_notification_event(python_to_rust_event_name) as python_to_rust_event,
    ):
        _initialize_region(region, header)
        proc = _start_main_service_shm_subprocess(
            root,
            total_size,
            suffix,
            shm_name,
            rust_to_python_event_name,
            python_to_rust_event_name,
            extra_env={"BAAS_SHM_TEST_COMMANDS": "1"},
        )
        try:
            assert python_to_rust_event.wait(5000), _subprocess_output(proc)
            assert proc.poll() is None

            _write_inbound_frame(
                region,
                header,
                MESSAGE_KIND_OPEN_CHANNEL,
                b'{"name":"sync"}',
                sequence_number=1,
                channel_id=CHANNEL_SYNC,
            )
            _write_inbound_frame(
                region,
                header,
                MESSAGE_KIND_JSON,
                b'{"type":"list"}',
                sequence_number=2,
                channel_id=CHANNEL_SYNC,
            )
            rust_to_python_event.set()
            sync_frame = asyncio.run(
                _read_outbound_until(
                    region,
                    header,
                    python_to_rust_event,
                    lambda frame: frame.header.logical_channel_id == CHANNEL_SYNC
                    and json_payload(frame).get("type") == "config_list",
                )
            )
            sync_message = json_payload(sync_frame)
            assert isinstance(sync_message["data"], list)

            _write_inbound_frame(
                region,
                header,
                MESSAGE_KIND_JSON,
                json.dumps(
                    {
                        "type": "pull",
                        "resource": "transport_test",
                        "resource_id": "sync-subprocess",
                    }
                ).encode("utf-8"),
                sequence_number=3,
                channel_id=CHANNEL_SYNC,
            )
            rust_to_python_event.set()
            snapshot_frame = asyncio.run(
                _read_outbound_until(
                    region,
                    header,
                    python_to_rust_event,
                    lambda frame: frame.header.logical_channel_id == CHANNEL_SYNC
                    and json_payload(frame).get("type") == "snapshot"
                    and json_payload(frame).get("resource") == "transport_test",
                )
            )
            snapshot_message = json_payload(snapshot_frame)
            assert snapshot_message["resource_id"] == "sync-subprocess"
            assert snapshot_message["data"] == {
                "transport": "shared-memory",
                "resource_id": "sync-subprocess",
            }

            _write_inbound_frame(
                region,
                header,
                MESSAGE_KIND_JSON,
                json.dumps(
                    {
                        "type": "patch",
                        "resource": "transport_test",
                        "resource_id": "sync-subprocess",
                        "timestamp": 43,
                        "ops": [{"op": "replace", "path": "/enabled", "value": True}],
                    }
                ).encode("utf-8"),
                sequence_number=4,
                channel_id=CHANNEL_SYNC,
            )
            rust_to_python_event.set()
            patch_ack_frame = asyncio.run(
                _read_outbound_until(
                    region,
                    header,
                    python_to_rust_event,
                    lambda frame: frame.header.logical_channel_id == CHANNEL_SYNC
                    and json_payload(frame).get("type") == "patch_ack"
                    and json_payload(frame).get("timestamp") == 43,
                )
            )
            patch_ack_message = json_payload(patch_ack_frame)
            assert patch_ack_message["resource"] == "transport_test"
            assert patch_ack_message["resource_id"] == "sync-subprocess"

            _write_inbound_frame(
                region,
                header,
                MESSAGE_KIND_OPEN_CHANNEL,
                b'{"name":"provider"}',
                sequence_number=5,
                channel_id=CHANNEL_PROVIDER,
                stream_id=5,
            )
            rust_to_python_event.set()

            provider_frame = asyncio.run(
                _read_outbound_until(
                    region,
                    header,
                    python_to_rust_event,
                    lambda frame: frame.header.logical_channel_id == CHANNEL_PROVIDER
                    and json_payload(frame).get("type") in {"logs_full", "status"},
                )
            )
            assert provider_frame.header.stream_id == 5
        finally:
            proc.terminate()
            rust_to_python_event.set()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                with suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=1)
            with suppress(Exception):
                proc.communicate(timeout=1)


def _main_service_subprocess_trigger_scenario() -> None:
    suffix = f"{os.getpid()}-{uuid.uuid4()}"
    shm_name = _native_name("main-test-trigger-shm", suffix)
    rust_to_python_event_name = _native_name("main-test-trigger-r2p", suffix)
    python_to_rust_event_name = _native_name("main-test-trigger-p2r", suffix)
    total_size = 16 * 1024
    header = _test_header(total_size)
    root = Path(__file__).resolve().parents[2]

    with (
        create_shared_memory_region(shm_name, total_size) as region,
        create_notification_event(rust_to_python_event_name) as rust_to_python_event,
        create_notification_event(python_to_rust_event_name) as python_to_rust_event,
    ):
        _initialize_region(region, header)
        proc = _start_main_service_shm_subprocess(
            root,
            total_size,
            suffix,
            shm_name,
            rust_to_python_event_name,
            python_to_rust_event_name,
            extra_env={"BAAS_SHM_TEST_COMMANDS": "1"},
        )
        try:
            assert python_to_rust_event.wait(5000), _subprocess_output(proc)
            assert proc.poll() is None

            stream_id = 9
            _write_inbound_frame(
                region,
                header,
                MESSAGE_KIND_OPEN_CHANNEL,
                b'{"name":"trigger"}',
                sequence_number=1,
                channel_id=CHANNEL_TRIGGER,
                stream_id=stream_id,
            )
            _write_inbound_frame(
                region,
                header,
                MESSAGE_KIND_JSON,
                json.dumps(
                    {
                        "type": "command",
                        "command": "import_config",
                        "timestamp": 41,
                        "payload": {"binary": True},
                    }
                ).encode("utf-8"),
                sequence_number=2,
                channel_id=CHANNEL_TRIGGER,
                stream_id=stream_id,
            )
            _write_inbound_frame(
                region,
                header,
                MESSAGE_KIND_BYTES,
                b"archive-bytes",
                sequence_number=3,
                channel_id=CHANNEL_TRIGGER,
                stream_id=stream_id,
            )
            rust_to_python_event.set()

            import_frames = asyncio.run(
                _read_outbound_until_all(
                    region,
                    header,
                    python_to_rust_event,
                    lambda observed: _find_json_frame(observed, CHANNEL_TRIGGER, stream_id, 41) is not None,
                )
            )
            import_frame = _find_json_frame(import_frames, CHANNEL_TRIGGER, stream_id, 41)
            assert import_frame is not None
            import_message = json_payload(import_frame)
            assert import_message["status"] == "ok"
            assert import_message["data"]["received_binary_size"] == len(b"archive-bytes")

            _write_inbound_frame(
                region,
                header,
                MESSAGE_KIND_JSON,
                json.dumps(
                    {
                        "type": "command",
                        "command": "export_config",
                        "timestamp": 42,
                        "payload": {"content": "binary-response"},
                    }
                ).encode("utf-8"),
                sequence_number=4,
                channel_id=CHANNEL_TRIGGER,
                stream_id=stream_id,
            )
            rust_to_python_event.set()

            export_frames = asyncio.run(
                _read_outbound_until_all(
                    region,
                    header,
                    python_to_rust_event,
                    lambda observed: _find_json_frame(observed, CHANNEL_TRIGGER, stream_id, 42) is not None
                    and _find_bytes_frame(observed, CHANNEL_TRIGGER, stream_id) is not None,
                )
            )
            export_frame = _find_json_frame(export_frames, CHANNEL_TRIGGER, stream_id, 42)
            assert export_frame is not None
            export_message = json_payload(export_frame)
            assert export_message["status"] == "ok"
            assert export_message["data"]["binary"] == {"size": len(b"binary-response")}

            binary_frame = _find_bytes_frame(export_frames, CHANNEL_TRIGGER, stream_id)
            assert binary_frame is not None
            assert binary_frame.payload == b"binary-response"
        finally:
            proc.terminate()
            rust_to_python_event.set()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                with suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=1)
            with suppress(Exception):
                proc.communicate(timeout=1)


def _main_service_subprocess_trigger_stream_scenario() -> None:
    suffix = f"{os.getpid()}-{uuid.uuid4()}"
    shm_name = _native_name("main-test-trigger-stream-shm", suffix)
    rust_to_python_event_name = _native_name("main-test-trigger-stream-r2p", suffix)
    python_to_rust_event_name = _native_name("main-test-trigger-stream-p2r", suffix)
    total_size = 16 * 1024
    header = _test_header(total_size)
    root = Path(__file__).resolve().parents[2]

    with (
        create_shared_memory_region(shm_name, total_size) as region,
        create_notification_event(rust_to_python_event_name) as rust_to_python_event,
        create_notification_event(python_to_rust_event_name) as python_to_rust_event,
    ):
        _initialize_region(region, header)
        proc = _start_main_service_shm_subprocess(
            root,
            total_size,
            suffix,
            shm_name,
            rust_to_python_event_name,
            python_to_rust_event_name,
            extra_env={"BAAS_SHM_TEST_COMMANDS": "1"},
        )
        try:
            assert python_to_rust_event.wait(5000), _subprocess_output(proc)
            assert proc.poll() is None

            stream_id = 11
            _write_inbound_frame(
                region,
                header,
                MESSAGE_KIND_OPEN_CHANNEL,
                b'{"name":"trigger"}',
                sequence_number=1,
                channel_id=CHANNEL_TRIGGER,
                stream_id=stream_id,
            )
            _write_inbound_frame(
                region,
                header,
                MESSAGE_KIND_JSON,
                json.dumps(
                    {
                        "type": "command",
                        "command": "transport_stream_test",
                        "timestamp": 51,
                        "payload": {},
                    }
                ).encode("utf-8"),
                sequence_number=2,
                channel_id=CHANNEL_TRIGGER,
                stream_id=stream_id,
            )
            rust_to_python_event.set()

            success_frames = asyncio.run(
                _read_outbound_until_all(
                    region,
                    header,
                    python_to_rust_event,
                    lambda observed: len(_json_frames_for_timestamp(observed, CHANNEL_TRIGGER, stream_id, 51)) == 3,
                )
            )
            success_messages = [
                json_payload(frame)
                for frame in _json_frames_for_timestamp(success_frames, CHANNEL_TRIGGER, stream_id, 51)
            ]
            assert [message["data"] for message in success_messages] == [
                {"chunk": 0},
                {"chunk": 1},
                {"done": True},
            ]
            assert all(message["status"] == "ok" for message in success_messages)

            _write_inbound_frame(
                region,
                header,
                MESSAGE_KIND_JSON,
                json.dumps(
                    {
                        "type": "command",
                        "command": "transport_stream_error",
                        "timestamp": 52,
                        "payload": {},
                    }
                ).encode("utf-8"),
                sequence_number=3,
                channel_id=CHANNEL_TRIGGER,
                stream_id=stream_id,
            )
            rust_to_python_event.set()

            error_frames = asyncio.run(
                _read_outbound_until_all(
                    region,
                    header,
                    python_to_rust_event,
                    lambda observed: _find_json_frame(observed, CHANNEL_TRIGGER, stream_id, 52) is not None,
                )
            )
            error_frame = _find_json_frame(error_frames, CHANNEL_TRIGGER, stream_id, 52)
            assert error_frame is not None
            error_message = json_payload(error_frame)
            assert error_message["status"] == "error"
            assert error_message["data"] == {"done": True}
            assert "transport stream test error" in error_message["error"]
        finally:
            proc.terminate()
            rust_to_python_event.set()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                with suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=1)
            with suppress(Exception):
                proc.communicate(timeout=1)


def _main_service_subprocess_remote_scenario() -> None:
    suffix = f"{os.getpid()}-{uuid.uuid4()}"
    shm_name = _native_name("main-test-remote-shm", suffix)
    rust_to_python_event_name = _native_name("main-test-remote-r2p", suffix)
    python_to_rust_event_name = _native_name("main-test-remote-p2r", suffix)
    total_size = 16 * 1024
    header = _test_header(total_size)
    root = Path(__file__).resolve().parents[2]

    with (
        create_shared_memory_region(shm_name, total_size) as region,
        create_notification_event(rust_to_python_event_name) as rust_to_python_event,
        create_notification_event(python_to_rust_event_name) as python_to_rust_event,
    ):
        _initialize_region(region, header)
        proc = _start_main_service_shm_subprocess(
            root,
            total_size,
            suffix,
            shm_name,
            rust_to_python_event_name,
            python_to_rust_event_name,
            extra_env={"BAAS_SHM_TEST_COMMANDS": "1"},
        )
        try:
            assert python_to_rust_event.wait(5000), _subprocess_output(proc)
            assert proc.poll() is None

            stream_id = 13
            frame_count = 6
            _write_inbound_frame(
                region,
                header,
                MESSAGE_KIND_OPEN_CHANNEL,
                b'{"name":"remote-transport-test"}',
                sequence_number=1,
                channel_id=CHANNEL_REMOTE,
                stream_id=stream_id,
            )
            _write_inbound_frame(
                region,
                header,
                MESSAGE_KIND_JSON,
                json.dumps(
                    {
                        "config_id": "transport_remote_test",
                        "frame_count": frame_count,
                    }
                ).encode("utf-8"),
                sequence_number=2,
                channel_id=CHANNEL_REMOTE,
                stream_id=stream_id,
            )
            _write_inbound_frame(
                region,
                header,
                MESSAGE_KIND_BYTES,
                b"remote-control",
                sequence_number=3,
                channel_id=CHANNEL_REMOTE,
                stream_id=stream_id,
            )
            rust_to_python_event.set()

            frames = asyncio.run(
                _read_outbound_until_all(
                    region,
                    header,
                    python_to_rust_event,
                    lambda observed: _find_remote_ack(observed, stream_id) is not None
                    and len(_bytes_frames_for_channel(observed, CHANNEL_REMOTE, stream_id)) == frame_count
                    and _find_close_frame(observed, CHANNEL_REMOTE, stream_id) is not None,
                )
            )
            ack = _find_remote_ack(frames, stream_id)
            assert ack is not None
            assert json_payload(ack) == {"type": "remote_ack", "size": len(b"remote-control")}
            byte_frames = _bytes_frames_for_channel(frames, CHANNEL_REMOTE, stream_id)
            assert [frame.payload for frame in byte_frames] == [
                f"remote-frame-{index:02d}".encode("ascii") * 8 for index in range(frame_count)
            ]
            close_frame = _find_close_frame(frames, CHANNEL_REMOTE, stream_id)
            assert close_frame is not None
            assert close_frame.payload == b""
        finally:
            proc.terminate()
            rust_to_python_event.set()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                with suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=1)
            with suppress(Exception):
                proc.communicate(timeout=1)


def _start_main_service_shm_subprocess(
    root: Path,
    total_size: int,
    suffix: str,
    shm_name: str,
    rust_to_python_event_name: str,
    python_to_rust_event_name: str,
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.Popen:
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
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        [
            sys.executable,
            "main.service.py",
            "--transport",
            "shm",
            "--ipc-instance",
            f"main-test-{suffix}",
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
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


async def _native_roundtrip_scenario() -> None:
    suffix = f"{os.getpid()}-{uuid.uuid4()}"
    shm_name = _native_name("native-test-shm", suffix)
    rust_to_python_event_name = _native_name("native-test-r2p", suffix)
    python_to_rust_event_name = _native_name("native-test-p2r", suffix)
    total_size = 4096
    header = _test_header(total_size)
    keep_running = True

    with (
        create_shared_memory_region(shm_name, total_size) as region,
        create_notification_event(rust_to_python_event_name) as rust_to_python_event,
        create_notification_event(python_to_rust_event_name) as python_to_rust_event,
    ):
        _initialize_region(region, header)

        def parent_alive(_parent_pid: int) -> bool:
            return keep_running

        with (
            patch("service.transport.shared_memory_server._parent_alive", parent_alive),
            patch.dict(os.environ, {"BAAS_IPC_REGION_BYTES": str(total_size)}),
        ):
            task = asyncio.create_task(
                _run_shared_memory_server_with_context(
                    SharedMemoryLaunchOptions(
                        ipc_instance=f"native-test-{suffix}",
                        parent_pid=os.getpid(),
                        project_root=Path("."),
                        shm_name=shm_name,
                        rust_to_python_notify_name=rust_to_python_event_name,
                        python_to_rust_notify_name=python_to_rust_event_name,
                    ),
                    _handler_factory,
                )
            )
            try:
                self_ready = await asyncio.to_thread(python_to_rust_event.wait, 2000)
                assert self_ready, "Python shm server did not signal readiness"
                updated = unpack_shared_memory_header(region.read(0, SHARED_MEMORY_HEADER_STRUCT.size))
                assert updated.peer_pid == os.getpid()
                assert updated.lifecycle_state == LIFECYCLE_READY

                _write_inbound_frame(
                    region,
                    header,
                    MESSAGE_KIND_OPEN_CHANNEL,
                    b'{"name":"sync"}',
                    sequence_number=1,
                )
                _write_inbound_frame(
                    region,
                    header,
                    MESSAGE_KIND_JSON,
                    b'{"type":"ping","value":7}',
                    sequence_number=2,
                )
                rust_to_python_event.set()

                outbound = await _read_outbound_frames(region, header, python_to_rust_event, expected=2)
                assert outbound[0].header.logical_channel_id == CHANNEL_SYNC
                assert outbound[0].header.message_kind == MESSAGE_KIND_JSON
                assert outbound[0].payload == b'{"type":"pong","value":7}'
                assert outbound[1].header.message_kind == MESSAGE_KIND_BYTES
                assert outbound[1].payload == b"binary-ack"
            finally:
                keep_running = False
                rust_to_python_event.set()
                await asyncio.wait_for(task, timeout=2)
                stopped = unpack_shared_memory_header(region.read(0, SHARED_MEMORY_HEADER_STRUCT.size))
                assert stopped.lifecycle_state == LIFECYCLE_STOPPED


async def _handler(endpoint) -> None:
    message = await endpoint.recv_json()
    await endpoint.send_json({"type": "pong", "value": message["value"]})
    await endpoint.send_bytes(b"binary-ack")


def _handler_factory(channel_name: str):
    assert channel_name == "sync"
    return _handler


def _subprocess_output(proc: subprocess.Popen) -> str:
    try:
        stdout, stderr = proc.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        return "subprocess is still running"
        return f"stdout:\n{stdout}\nstderr:\n{stderr}"


def _native_name(kind: str, suffix: str) -> str:
    if sys.platform == "win32":
        return f"Local\\BAAS-{kind}-{suffix}"
    return f"/baas-{kind}-{suffix}"


async def _read_outbound_frames(region, header, event, expected: int):
    deadline = asyncio.get_running_loop().time() + 5
    frames = []
    while len(frames) < expected and asyncio.get_running_loop().time() < deadline:
        try:
            frames.append(
                read_frame_from_region(
                    region,
                    header.python_to_rust_ring_offset,
                    header.python_to_rust_ring_length,
                )
            )
        except NotEnoughData:
            await asyncio.to_thread(event.wait, 100)
    assert len(frames) == expected, f"expected {expected} outbound frames, got {len(frames)}"
    return frames


async def _read_outbound_until(region, header, event, predicate):
    deadline = asyncio.get_running_loop().time() + 5
    observed = []
    while asyncio.get_running_loop().time() < deadline:
        try:
            frame = read_frame_from_region(
                region,
                header.python_to_rust_ring_offset,
                header.python_to_rust_ring_length,
            )
            observed.append(_describe_frame(frame))
            if predicate(frame):
                return frame
        except NotEnoughData:
            await asyncio.to_thread(event.wait, 100)
    raise AssertionError(f"expected outbound frame was not observed; observed={observed!r}")


async def _read_outbound_until_all(region, header, event, predicate):
    deadline = asyncio.get_running_loop().time() + 5
    frames = []
    while asyncio.get_running_loop().time() < deadline:
        try:
            frame = read_frame_from_region(
                region,
                header.python_to_rust_ring_offset,
                header.python_to_rust_ring_length,
            )
            frames.append(frame)
            if predicate(frames):
                return frames
        except NotEnoughData:
            await asyncio.to_thread(event.wait, 100)
    raise AssertionError(
        f"expected outbound frame set was not observed; observed={[_describe_frame(frame) for frame in frames]!r}"
    )


def _find_json_frame(frames, channel_id: int, stream_id: int, timestamp: int):
    for frame in frames:
        if (
            frame.header.logical_channel_id == channel_id
            and frame.header.stream_id == stream_id
            and frame.header.message_kind == MESSAGE_KIND_JSON
            and json_payload(frame).get("timestamp") == timestamp
        ):
            return frame
    return None


def _json_frames_for_timestamp(frames, channel_id: int, stream_id: int, timestamp: int):
    return [
        frame
        for frame in frames
        if frame.header.logical_channel_id == channel_id
        and frame.header.stream_id == stream_id
        and frame.header.message_kind == MESSAGE_KIND_JSON
        and json_payload(frame).get("timestamp") == timestamp
    ]


def _find_bytes_frame(frames, channel_id: int, stream_id: int):
    for frame in frames:
        if (
            frame.header.logical_channel_id == channel_id
            and frame.header.stream_id == stream_id
            and frame.header.message_kind == MESSAGE_KIND_BYTES
        ):
            return frame
    return None


def _bytes_frames_for_channel(frames, channel_id: int, stream_id: int):
    return [
        frame
        for frame in frames
        if frame.header.logical_channel_id == channel_id
        and frame.header.stream_id == stream_id
        and frame.header.message_kind == MESSAGE_KIND_BYTES
    ]


def _find_close_frame(frames, channel_id: int, stream_id: int):
    for frame in frames:
        if (
            frame.header.logical_channel_id == channel_id
            and frame.header.stream_id == stream_id
            and frame.header.message_kind == MESSAGE_KIND_CLOSE_CHANNEL
        ):
            return frame
    return None


def _find_remote_ack(frames, stream_id: int):
    for frame in frames:
        if (
            frame.header.logical_channel_id == CHANNEL_REMOTE
            and frame.header.stream_id == stream_id
            and frame.header.message_kind == MESSAGE_KIND_JSON
            and json_payload(frame).get("type") == "remote_ack"
        ):
            return frame
    return None


def _describe_frame(frame) -> dict:
    description = {
        "channel_id": frame.header.logical_channel_id,
        "stream_id": frame.header.stream_id,
        "kind": frame.header.message_kind,
        "payload_len": len(frame.payload),
    }
    if frame.header.message_kind == MESSAGE_KIND_JSON:
        try:
            description["json"] = json_payload(frame)
        except Exception as exc:  # noqa: BLE001 - diagnostic only
            description["json_error"] = str(exc)
    return description


def json_payload(frame):
    return json.loads(frame.payload.decode("utf-8"))


def _write_inbound_frame(
    region,
    header,
    message_kind: int,
    payload: bytes,
    *,
    sequence_number: int,
    channel_id: int = CHANNEL_SYNC,
    stream_id: int = 0,
) -> None:
    frame = fragment_payload(
        channel_id,
        stream_id,
        message_kind,
        0,
        sequence_number,
        0,
        payload,
        1024,
    )[0]
    write_frame_to_region(
        region,
        header.rust_to_python_ring_offset,
        header.rust_to_python_ring_length,
        frame,
    )


def _initialize_region(region, header: SharedMemoryHeader) -> None:
    region.write(0, pack_shared_memory_header(header))
    for offset, length in [
        (header.rust_to_python_ring_offset, header.rust_to_python_ring_length),
        (header.python_to_rust_ring_offset, header.python_to_rust_ring_length),
    ]:
        raw_ring = bytearray(region.read(offset, length))
        SharedRingBuffer.initialize(raw_ring, header.generation_id_low, header.generation_id_high)
        region.write(offset, raw_ring)


def _test_header(total_size: int) -> SharedMemoryHeader:
    return SharedMemoryHeader(
        magic=MAGIC,
        protocol_version=PROTOCOL_VERSION,
        abi_version=ABI_VERSION,
        header_size=SHARED_MEMORY_HEADER_STRUCT.size,
        total_size=total_size,
        generation_id_low=11,
        generation_id_high=22,
        owner_pid=os.getpid(),
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


if __name__ == "__main__":
    unittest.main()
