from __future__ import annotations

import os
import sys
import asyncio
import ctypes
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

from .native_ipc import NativeIpcError, open_notification_event, open_shared_memory_region
from .protocol import (
    LIFECYCLE_READY,
    LIFECYCLE_STOPPED,
    PROTOCOL_VERSION,
    SHARED_MEMORY_HEADER_STRUCT,
    pack_shared_memory_header,
    unpack_ring_control_block,
    unpack_shared_memory_header,
)
from .ring_buffer import NotEnoughData
from .shared_memory_endpoint import SharedMemoryChannelMux, SharedMemoryRingWriter, read_frame_from_region


class SharedMemoryTransportUnavailable(RuntimeError):
    """Raised when the native shared-memory transport is requested but unavailable."""


@dataclass(frozen=True)
class SharedMemoryLaunchOptions:
    ipc_instance: str
    parent_pid: int
    project_root: Path
    protocol_version: int = 1
    shm_name: Optional[str] = None
    notify_name: Optional[str] = None
    rust_to_python_notify_name: Optional[str] = None
    python_to_rust_notify_name: Optional[str] = None


async def run_shared_memory_server(options: SharedMemoryLaunchOptions) -> None:
    """Run the shared-memory backend.

    This entrypoint is intentionally explicit so `--transport shm` never starts
    FastAPI/Uvicorn or probes localhost.
    """
    if not options.shm_name:
        raise SharedMemoryTransportUnavailable("--transport shm requires --shm-name")
    if options.protocol_version != PROTOCOL_VERSION:
        raise SharedMemoryTransportUnavailable(
            f"ProtocolVersionMismatch: expected {PROTOCOL_VERSION}, got {options.protocol_version}"
        )
    from service.context import ServiceContext

    context = ServiceContext(options.project_root, enable_auth=False)
    await context.startup()
    try:
        await _run_shared_memory_server_with_context(options, _handler_factory(context))
    finally:
        await context.shutdown()


async def _run_shared_memory_server_with_context(
    options: SharedMemoryLaunchOptions,
    handler_factory: Callable,
) -> None:
    region_size = int(os.getenv("BAAS_IPC_REGION_BYTES", str(16 * 1024 * 1024)))
    try:
        with open_shared_memory_region(options.shm_name, region_size) as region:
            raw_header = region.read(0, SHARED_MEMORY_HEADER_STRUCT.size)
            header = unpack_shared_memory_header(raw_header)
            if header.total_size > region.size:
                raise SharedMemoryTransportUnavailable(
                    f"SharedMemoryCorrupted: header total_size {header.total_size} exceeds mapped region {region.size}"
                )
            _validate_ring_region(
                region,
                header.rust_to_python_ring_offset,
                header.rust_to_python_ring_length,
                header.generation_id_low,
                header.generation_id_high,
                "rust_to_python",
            )
            _validate_ring_region(
                region,
                header.python_to_rust_ring_offset,
                header.python_to_rust_ring_length,
                header.generation_id_low,
                header.generation_id_high,
                "python_to_rust",
            )
            updated = replace(header, peer_pid=os.getpid(), lifecycle_state=LIFECYCLE_READY)
            region.write(0, pack_shared_memory_header(updated))
            rust_to_python_event = open_notification_event(
                options.rust_to_python_notify_name or options.notify_name
            )
            python_to_rust_event = open_notification_event(
                options.python_to_rust_notify_name or options.notify_name
            )
            with _optional_event(rust_to_python_event), _optional_event(python_to_rust_event):
                if python_to_rust_event is not None:
                    python_to_rust_event.set()
                await _run_shared_memory_loop(
                    region,
                    updated,
                    rust_to_python_event,
                    python_to_rust_event,
                    options.parent_pid,
                    handler_factory,
                )
    except NativeIpcError as exc:
        raise SharedMemoryTransportUnavailable(
            f"TransportUnavailable: {exc}; no WebSocket fallback is allowed "
            f"(ipc_instance={options.ipc_instance!r}, parent_pid={options.parent_pid})"
        ) from exc


def _handler_factory(context):
    from service.channels.provider import ProviderChannelHandler
    from service.channels.remote import RemoteChannelHandler
    from service.channels.sync import SyncChannelHandler
    from service.channels.trigger import TriggerChannelHandler

    handlers = {
        "sync": SyncChannelHandler(context).handle,
        "provider": ProviderChannelHandler(context).handle,
        "trigger": TriggerChannelHandler(context).handle,
        "remote": RemoteChannelHandler(context).handle,
    }

    def resolve(channel_name: str):
        try:
            return handlers[channel_name]
        except KeyError as exc:
            raise SharedMemoryTransportUnavailable(
                f"UnsupportedTransport: shared-memory channel is not implemented: {channel_name}"
            ) from exc

    return resolve


def _validate_ring_region(
    region,
    offset: int,
    length: int,
    generation_id_low: int,
    generation_id_high: int,
    name: str,
) -> None:
    from .protocol import RING_CONTROL_BLOCK_STRUCT

    if offset < 0 or length <= RING_CONTROL_BLOCK_STRUCT.size or offset + length > region.size:
        raise SharedMemoryTransportUnavailable(
            f"SharedMemoryCorrupted: {name} ring descriptor is out of bounds"
        )
    block = unpack_ring_control_block(region.read(offset, RING_CONTROL_BLOCK_STRUCT.size))
    if block.capacity != length - RING_CONTROL_BLOCK_STRUCT.size:
        raise SharedMemoryTransportUnavailable(
            f"SharedMemoryCorrupted: {name} ring capacity {block.capacity} does not match descriptor {length}"
        )
    if block.generation_id_low != generation_id_low or block.generation_id_high != generation_id_high:
        raise SharedMemoryTransportUnavailable(
            f"SharedMemoryCorrupted: {name} ring generation ID does not match shared-memory header"
        )


class _optional_event:
    def __init__(self, event) -> None:
        self.event = event

    def __enter__(self):
        if self.event is not None:
            return self.event.__enter__()
        return None

    def __exit__(self, *exc: object) -> None:
        if self.event is not None:
            self.event.__exit__(*exc)


async def _run_shared_memory_loop(
    region,
    header,
    rust_to_python_event,
    python_to_rust_event,
    parent_pid: int,
    handler_factory=None,
) -> None:
    """Drain inbound frames until the Tauri parent exits.

    The loop waits on the Rust-to-Python notification event, dispatches frames
    through transport-neutral handlers, and wakes Rust through the
    Python-to-Rust event when handlers send responses.
    """
    writer = SharedMemoryRingWriter(
        region=region,
        offset=header.python_to_rust_ring_offset,
        length=header.python_to_rust_ring_length,
        notify_event=python_to_rust_event,
    )
    mux = SharedMemoryChannelMux(writer, handler_factory)
    try:
        while _parent_alive(parent_pid):
            if rust_to_python_event is not None:
                await asyncio.to_thread(rust_to_python_event.wait, 250)
            else:
                await asyncio.sleep(0.25)
            while True:
                try:
                    await mux.handle_frame(_read_inbound_frame(region, header))
                except NotEnoughData:
                    break
    finally:
        try:
            _publish_lifecycle_state(region, header, LIFECYCLE_STOPPED, python_to_rust_event)
        finally:
            await mux.close()


def _read_inbound_frame(region, header):
    return read_frame_from_region(
        region,
        header.rust_to_python_ring_offset,
        header.rust_to_python_ring_length,
    )


def _publish_lifecycle_state(region, header, lifecycle_state: int, notify_event=None) -> None:
    current = unpack_shared_memory_header(region.read(0, SHARED_MEMORY_HEADER_STRUCT.size))
    if (
        current.generation_id_low != header.generation_id_low
        or current.generation_id_high != header.generation_id_high
    ):
        raise SharedMemoryTransportUnavailable(
            "SharedMemoryCorrupted: header generation ID changed during shared-memory runtime"
        )
    updated = replace(current, lifecycle_state=lifecycle_state)
    region.write(0, pack_shared_memory_header(updated))
    if notify_event is not None:
        notify_event.set()


def _parent_alive(parent_pid: int) -> bool:
    if parent_pid <= 0:
        return False
    if sys.platform == "win32":
        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(process_query_limited_information, False, parent_pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(parent_pid, 0)
        return True
    except OSError:
        return False
