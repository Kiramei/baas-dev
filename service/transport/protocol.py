from __future__ import annotations

import struct
from dataclasses import dataclass


MAGIC = b"BAASIPC\0"
PROTOCOL_VERSION = 1
ABI_VERSION = 1
BYTE_ORDER = "little"
MAX_FRAME_LENGTH = 8 * 1024 * 1024
MAX_MESSAGE_LENGTH = 64 * 1024 * 1024
LIFECYCLE_STARTING = 1
LIFECYCLE_READY = 2
LIFECYCLE_STOPPED = 3
LIFECYCLE_FAILED = 4

SHARED_MEMORY_HEADER_STRUCT = struct.Struct("<8sHHIIQQIIIIQQIIIIIIIIIIIIII")
FRAME_HEADER_STRUCT = struct.Struct("<HHHHIQQIII")
RING_CONTROL_MAGIC = b"BAASRNG\0"
RING_CONTROL_BLOCK_STRUCT = struct.Struct("<8sHHIIIIQQQQI")

CHANNEL_CONTROL = 0
CHANNEL_PROVIDER = 1
CHANNEL_SYNC = 2
CHANNEL_TRIGGER = 3
CHANNEL_REMOTE = 4

MESSAGE_KIND_OPEN_CHANNEL = 1
MESSAGE_KIND_CLOSE_CHANNEL = 2
MESSAGE_KIND_JSON = 3
MESSAGE_KIND_BYTES = 4
MESSAGE_KIND_ERROR = 5

_CHANNEL_NAMES = {
    CHANNEL_CONTROL: "control",
    CHANNEL_PROVIDER: "provider",
    CHANNEL_SYNC: "sync",
    CHANNEL_TRIGGER: "trigger",
    CHANNEL_REMOTE: "remote",
}
_CHANNEL_IDS = {name: channel_id for channel_id, name in _CHANNEL_NAMES.items()}
_MESSAGE_KINDS = {
    MESSAGE_KIND_OPEN_CHANNEL,
    MESSAGE_KIND_CLOSE_CHANNEL,
    MESSAGE_KIND_JSON,
    MESSAGE_KIND_BYTES,
    MESSAGE_KIND_ERROR,
}


@dataclass(frozen=True)
class SharedMemoryHeader:
    magic: bytes
    protocol_version: int
    abi_version: int
    header_size: int
    total_size: int
    generation_id_low: int
    generation_id_high: int
    owner_pid: int
    peer_pid: int
    lifecycle_state: int
    last_error_code: int
    owner_heartbeat_ns: int
    peer_heartbeat_ns: int
    rust_to_python_ring_offset: int
    rust_to_python_ring_length: int
    python_to_rust_ring_offset: int
    python_to_rust_ring_length: int
    control_lane_offset: int
    control_lane_length: int
    message_lane_offset: int
    message_lane_length: int
    bulk_lane_offset: int
    bulk_lane_length: int
    remote_lane_offset: int
    remote_lane_length: int
    last_error_offset: int
    last_error_length: int


@dataclass(frozen=True)
class FrameHeader:
    frame_version: int
    logical_channel_id: int
    stream_id: int
    message_kind: int
    flags: int
    sequence_number: int
    correlation_id: int
    payload_length: int
    fragment_index: int
    fragment_count: int


@dataclass(frozen=True)
class RingControlBlock:
    magic: bytes
    abi_version: int
    header_size: int
    flags: int
    capacity: int
    read_cursor: int
    write_cursor: int
    generation_id_low: int
    generation_id_high: int
    sequence_number: int
    dropped_frames: int
    reserved: int


def pack_shared_memory_header(header: SharedMemoryHeader) -> bytes:
    if header.magic != MAGIC:
        raise ValueError("shared-memory magic mismatch")
    if header.protocol_version != PROTOCOL_VERSION or header.abi_version != ABI_VERSION:
        raise ValueError("shared-memory protocol version mismatch")
    return SHARED_MEMORY_HEADER_STRUCT.pack(
        header.magic,
        header.protocol_version,
        header.abi_version,
        header.header_size,
        header.total_size,
        header.generation_id_low,
        header.generation_id_high,
        header.owner_pid,
        header.peer_pid,
        header.lifecycle_state,
        header.last_error_code,
        header.owner_heartbeat_ns,
        header.peer_heartbeat_ns,
        header.rust_to_python_ring_offset,
        header.rust_to_python_ring_length,
        header.python_to_rust_ring_offset,
        header.python_to_rust_ring_length,
        header.control_lane_offset,
        header.control_lane_length,
        header.message_lane_offset,
        header.message_lane_length,
        header.bulk_lane_offset,
        header.bulk_lane_length,
        header.remote_lane_offset,
        header.remote_lane_length,
        header.last_error_offset,
        header.last_error_length,
    )


def unpack_shared_memory_header(data: bytes) -> SharedMemoryHeader:
    if len(data) != SHARED_MEMORY_HEADER_STRUCT.size:
        raise ValueError("invalid shared-memory header size")
    header = SharedMemoryHeader(*SHARED_MEMORY_HEADER_STRUCT.unpack(data))
    if header.magic != MAGIC:
        raise ValueError("shared-memory magic mismatch")
    if header.protocol_version != PROTOCOL_VERSION or header.abi_version != ABI_VERSION:
        raise ValueError("shared-memory protocol version mismatch")
    return header


def pack_frame_header(frame: FrameHeader) -> bytes:
    logical_channel_name(frame.logical_channel_id)
    validate_message_kind(frame.message_kind)
    if frame.payload_length < 0 or frame.payload_length > MAX_FRAME_LENGTH:
        raise ValueError("payload length exceeds shared-memory frame limit")
    if frame.fragment_count <= 0:
        raise ValueError("fragment count must be greater than zero")
    if frame.fragment_index >= frame.fragment_count:
        raise ValueError("fragment index must be smaller than fragment count")
    return FRAME_HEADER_STRUCT.pack(
        frame.frame_version,
        frame.logical_channel_id,
        frame.stream_id,
        frame.message_kind,
        frame.flags,
        frame.sequence_number,
        frame.correlation_id,
        frame.payload_length,
        frame.fragment_index,
        frame.fragment_count,
    )


def unpack_frame_header(data: bytes) -> FrameHeader:
    if len(data) != FRAME_HEADER_STRUCT.size:
        raise ValueError("invalid frame header size")
    frame = FrameHeader(*FRAME_HEADER_STRUCT.unpack(data))
    logical_channel_name(frame.logical_channel_id)
    validate_message_kind(frame.message_kind)
    if frame.payload_length > MAX_FRAME_LENGTH:
        raise ValueError("payload length exceeds shared-memory frame limit")
    if frame.fragment_count <= 0:
        raise ValueError("fragment count must be greater than zero")
    if frame.fragment_index >= frame.fragment_count:
        raise ValueError("fragment index must be smaller than fragment count")
    return frame


def logical_channel_id(name: str) -> int:
    try:
        return _CHANNEL_IDS[name]
    except KeyError as exc:
        raise ValueError("unknown logical channel") from exc


def logical_channel_name(channel_id: int) -> str:
    try:
        return _CHANNEL_NAMES[channel_id]
    except KeyError as exc:
        raise ValueError("unknown logical channel") from exc


def validate_message_kind(message_kind: int) -> None:
    if message_kind not in _MESSAGE_KINDS:
        raise ValueError("unknown message kind")


def new_ring_control_block(capacity: int, generation_id_low: int, generation_id_high: int) -> RingControlBlock:
    return RingControlBlock(
        magic=RING_CONTROL_MAGIC,
        abi_version=ABI_VERSION,
        header_size=RING_CONTROL_BLOCK_STRUCT.size,
        flags=0,
        capacity=capacity,
        read_cursor=0,
        write_cursor=0,
        generation_id_low=generation_id_low,
        generation_id_high=generation_id_high,
        sequence_number=0,
        dropped_frames=0,
        reserved=0,
    )


def pack_ring_control_block(block: RingControlBlock) -> bytes:
    if block.magic != RING_CONTROL_MAGIC:
        raise ValueError("ring control block magic mismatch")
    if block.abi_version != ABI_VERSION or block.header_size != RING_CONTROL_BLOCK_STRUCT.size:
        raise ValueError("ring control block protocol version mismatch")
    if block.read_cursor > block.capacity or block.write_cursor > block.capacity:
        raise ValueError("ring control block cursor is out of bounds")
    return RING_CONTROL_BLOCK_STRUCT.pack(
        block.magic,
        block.abi_version,
        block.header_size,
        block.flags,
        block.capacity,
        block.read_cursor,
        block.write_cursor,
        block.generation_id_low,
        block.generation_id_high,
        block.sequence_number,
        block.dropped_frames,
        block.reserved,
    )


def unpack_ring_control_block(data: bytes) -> RingControlBlock:
    if len(data) != RING_CONTROL_BLOCK_STRUCT.size:
        raise ValueError("invalid ring control block size")
    block = RingControlBlock(*RING_CONTROL_BLOCK_STRUCT.unpack(data))
    if block.magic != RING_CONTROL_MAGIC:
        raise ValueError("ring control block magic mismatch")
    if block.abi_version != ABI_VERSION or block.header_size != RING_CONTROL_BLOCK_STRUCT.size:
        raise ValueError("ring control block protocol version mismatch")
    if block.read_cursor > block.capacity or block.write_cursor > block.capacity:
        raise ValueError("ring control block cursor is out of bounds")
    return block
