from __future__ import annotations

from .framing import EncodedFrame
from .protocol import (
    FRAME_HEADER_STRUCT,
    RING_CONTROL_BLOCK_STRUCT,
    new_ring_control_block,
    pack_frame_header,
    pack_ring_control_block,
    unpack_frame_header,
    unpack_ring_control_block,
)

_READ_CURSOR_OFFSET = 20
_WRITE_CURSOR_OFFSET = 24
_SEQUENCE_NUMBER_OFFSET = 44
_DROPPED_FRAMES_OFFSET = 52


class RingBufferError(Exception):
    pass


class QueueFull(RingBufferError):
    pass


class NotEnoughData(RingBufferError):
    pass


class InvalidRegion(RingBufferError):
    pass


class InvalidPacketLength(RingBufferError):
    pass


class SpscRingBuffer:
    """Bounded SPSC byte ring used by the shared-memory transport core."""

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("ring buffer capacity must be greater than zero")
        self._buffer = bytearray(capacity)
        self._read_cursor = 0
        self._write_cursor = 0
        self._len = 0

    @property
    def capacity(self) -> int:
        return len(self._buffer)

    @property
    def length(self) -> int:
        return self._len

    @property
    def available_write(self) -> int:
        return self.capacity - self._len

    def write(self, payload: bytes) -> None:
        if len(payload) > self.available_write:
            raise QueueFull("ring buffer queue is full")
        for byte in payload:
            self._buffer[self._write_cursor] = byte
            self._write_cursor = (self._write_cursor + 1) % self.capacity
        self._len += len(payload)

    def read(self, length: int) -> bytes:
        if length > self._len:
            raise NotEnoughData("not enough bytes are available")
        output = bytearray()
        for _ in range(length):
            output.append(self._buffer[self._read_cursor])
            self._read_cursor = (self._read_cursor + 1) % self.capacity
        self._len -= length
        return bytes(output)


class SharedRingBuffer:
    def __init__(self, region: bytearray) -> None:
        if len(region) <= RING_CONTROL_BLOCK_STRUCT.size + 1:
            raise InvalidRegion("shared ring region is too small")
        block = unpack_ring_control_block(bytes(region[: RING_CONTROL_BLOCK_STRUCT.size]))
        if block.capacity != len(region) - RING_CONTROL_BLOCK_STRUCT.size:
            raise InvalidRegion("shared ring capacity does not match region length")
        self._region = region

    @classmethod
    def initialize(
        cls,
        region: bytearray,
        generation_id_low: int,
        generation_id_high: int,
    ) -> "SharedRingBuffer":
        if len(region) <= RING_CONTROL_BLOCK_STRUCT.size + 1:
            raise InvalidRegion("shared ring region is too small")
        block = new_ring_control_block(
            len(region) - RING_CONTROL_BLOCK_STRUCT.size,
            generation_id_low,
            generation_id_high,
        )
        region[: RING_CONTROL_BLOCK_STRUCT.size] = pack_ring_control_block(block)
        region[RING_CONTROL_BLOCK_STRUCT.size :] = b"\0" * (len(region) - RING_CONTROL_BLOCK_STRUCT.size)
        return cls(region)

    @property
    def control_block(self):
        return unpack_ring_control_block(bytes(self._region[: RING_CONTROL_BLOCK_STRUCT.size]))

    @property
    def available_read(self) -> int:
        return _available_read(self.control_block)

    @property
    def available_write(self) -> int:
        block = self.control_block
        return block.capacity - _available_read(block) - 1

    def write_packet(self, payload: bytes) -> None:
        packet = len(payload).to_bytes(4, "little") + payload
        if len(packet) > self.available_write:
            raise QueueFull("ring buffer queue is full")
        self._write_bytes(packet)
        block = self.control_block
        self._write_u64(_SEQUENCE_NUMBER_OFFSET, (block.sequence_number + 1) & 0xFFFFFFFFFFFFFFFF)

    def read_packet(self, max_payload_len: int) -> bytes:
        if self.available_read < 4:
            raise NotEnoughData("not enough bytes are available")
        payload_len = int.from_bytes(self._peek_bytes(4), "little")
        if payload_len > max_payload_len:
            raise InvalidPacketLength("shared ring packet length is invalid")
        if payload_len + 4 > self.available_read:
            raise NotEnoughData("not enough bytes are available")
        self._read_bytes(4)
        return self._read_bytes(payload_len)

    def write_frame(self, frame: EncodedFrame) -> None:
        self.write_packet(pack_frame_header(frame.header) + frame.payload)

    def read_frame(self, max_payload_len: int) -> EncodedFrame:
        packet = self.read_packet(FRAME_HEADER_STRUCT.size + max_payload_len)
        return _decode_frame_packet(packet)

    def peek_frame(self, max_payload_len: int) -> EncodedFrame:
        if self.available_read < 4:
            raise NotEnoughData("not enough bytes are available")
        payload_len = int.from_bytes(self._peek_bytes(4), "little")
        if payload_len > FRAME_HEADER_STRUCT.size + max_payload_len:
            raise InvalidPacketLength("shared ring packet length is invalid")
        if payload_len + 4 > self.available_read:
            raise NotEnoughData("not enough bytes are available")
        packet = self._peek_bytes(4 + payload_len)[4:]
        return _decode_frame_packet(packet)

    def drop_frame(self, max_payload_len: int) -> EncodedFrame:
        frame = self.read_frame(max_payload_len)
        block = self.control_block
        self._write_u64(_DROPPED_FRAMES_OFFSET, (block.dropped_frames + 1) & 0xFFFFFFFFFFFFFFFF)
        return frame

    def _write_bytes(self, payload: bytes) -> None:
        block = self.control_block
        data_start = RING_CONTROL_BLOCK_STRUCT.size
        capacity = block.capacity
        cursor = block.write_cursor
        first_len = min(len(payload), capacity - cursor)
        self._region[data_start + cursor : data_start + cursor + first_len] = payload[:first_len]
        remaining = len(payload) - first_len
        if remaining:
            self._region[data_start : data_start + remaining] = payload[first_len:]
        cursor = (cursor + len(payload)) % capacity
        self._write_u32(_WRITE_CURSOR_OFFSET, cursor)

    def _read_bytes(self, length: int) -> bytes:
        if length > self.available_read:
            raise NotEnoughData("not enough bytes are available")
        block = self.control_block
        data_start = RING_CONTROL_BLOCK_STRUCT.size
        capacity = block.capacity
        cursor = block.read_cursor
        first_len = min(length, capacity - cursor)
        output = bytearray(self._region[data_start + cursor : data_start + cursor + first_len])
        remaining = length - first_len
        if remaining:
            output.extend(self._region[data_start : data_start + remaining])
        cursor = (cursor + length) % capacity
        self._write_u32(_READ_CURSOR_OFFSET, cursor)
        return bytes(output)

    def _peek_bytes(self, length: int) -> bytes:
        if length > self.available_read:
            raise NotEnoughData("not enough bytes are available")
        block = self.control_block
        data_start = RING_CONTROL_BLOCK_STRUCT.size
        capacity = block.capacity
        cursor = block.read_cursor
        first_len = min(length, capacity - cursor)
        output = bytearray(self._region[data_start + cursor : data_start + cursor + first_len])
        remaining = length - first_len
        if remaining:
            output.extend(self._region[data_start : data_start + remaining])
        return bytes(output)

    def _write_u32(self, offset: int, value: int) -> None:
        self._region[offset : offset + 4] = int(value).to_bytes(4, "little")

    def _write_u64(self, offset: int, value: int) -> None:
        self._region[offset : offset + 8] = int(value).to_bytes(8, "little")


class SharedRegionRingBuffer:
    """Shared ring backed directly by a mapped native region.

    This mirrors ``SharedRingBuffer`` but avoids copying the whole ring into a
    temporary bytearray for every frame. Only the control block and the actual
    packet bytes cross the Python/native boundary.
    """

    def __init__(self, region: object, offset: int, length: int) -> None:
        if length <= RING_CONTROL_BLOCK_STRUCT.size + 1:
            raise InvalidRegion("shared ring region is too small")
        self._region = region
        self._offset = offset
        self._length = length
        block = self.control_block
        if block.capacity != length - RING_CONTROL_BLOCK_STRUCT.size:
            raise InvalidRegion("shared ring capacity does not match region length")

    @property
    def control_block(self):
        return unpack_ring_control_block(self._read(0, RING_CONTROL_BLOCK_STRUCT.size))

    @property
    def available_read(self) -> int:
        return _available_read(self.control_block)

    @property
    def available_write(self) -> int:
        block = self.control_block
        return block.capacity - _available_read(block) - 1

    def write_packet(self, payload: bytes) -> None:
        packet_len = len(payload)
        if packet_len + 4 > self.available_write:
            raise QueueFull("ring buffer queue is full")
        self._write_bytes(packet_len.to_bytes(4, "little"))
        self._write_bytes(payload)
        block = self.control_block
        self._write_u64(_SEQUENCE_NUMBER_OFFSET, (block.sequence_number + 1) & 0xFFFFFFFFFFFFFFFF)

    def read_packet(self, max_payload_len: int) -> bytes:
        if self.available_read < 4:
            raise NotEnoughData("not enough bytes are available")
        payload_len = int.from_bytes(self._peek_bytes(4), "little")
        if payload_len > max_payload_len:
            raise InvalidPacketLength("shared ring packet length is invalid")
        if payload_len + 4 > self.available_read:
            raise NotEnoughData("not enough bytes are available")
        self._read_bytes(4)
        return self._read_bytes(payload_len)

    def write_frame(self, frame: EncodedFrame) -> None:
        self.write_packet(pack_frame_header(frame.header) + frame.payload)

    def read_frame(self, max_payload_len: int) -> EncodedFrame:
        packet = self.read_packet(FRAME_HEADER_STRUCT.size + max_payload_len)
        return _decode_frame_packet(packet)

    def peek_frame(self, max_payload_len: int) -> EncodedFrame:
        if self.available_read < 4:
            raise NotEnoughData("not enough bytes are available")
        payload_len = int.from_bytes(self._peek_bytes(4), "little")
        if payload_len > FRAME_HEADER_STRUCT.size + max_payload_len:
            raise InvalidPacketLength("shared ring packet length is invalid")
        if payload_len + 4 > self.available_read:
            raise NotEnoughData("not enough bytes are available")
        packet = self._peek_bytes(4 + payload_len)[4:]
        return _decode_frame_packet(packet)

    def drop_frame(self, max_payload_len: int) -> EncodedFrame:
        frame = self.read_frame(max_payload_len)
        block = self.control_block
        self._write_u64(_DROPPED_FRAMES_OFFSET, (block.dropped_frames + 1) & 0xFFFFFFFFFFFFFFFF)
        return frame

    def _write_bytes(self, payload: bytes) -> None:
        block = self.control_block
        data_start = RING_CONTROL_BLOCK_STRUCT.size
        capacity = block.capacity
        cursor = block.write_cursor
        first_len = min(len(payload), capacity - cursor)
        self._write(data_start + cursor, payload[:first_len])
        remaining = len(payload) - first_len
        if remaining:
            self._write(data_start, payload[first_len:])
        cursor = (cursor + len(payload)) % capacity
        self._write_u32(_WRITE_CURSOR_OFFSET, cursor)

    def _read_bytes(self, length: int) -> bytes:
        if length > self.available_read:
            raise NotEnoughData("not enough bytes are available")
        block = self.control_block
        data_start = RING_CONTROL_BLOCK_STRUCT.size
        capacity = block.capacity
        cursor = block.read_cursor
        first_len = min(length, capacity - cursor)
        if first_len == length:
            output = self._read(data_start + cursor, first_len)
        else:
            output = self._read(data_start + cursor, first_len) + self._read(data_start, length - first_len)
        cursor = (cursor + length) % capacity
        self._write_u32(_READ_CURSOR_OFFSET, cursor)
        return output

    def _peek_bytes(self, length: int) -> bytes:
        if length > self.available_read:
            raise NotEnoughData("not enough bytes are available")
        block = self.control_block
        data_start = RING_CONTROL_BLOCK_STRUCT.size
        capacity = block.capacity
        cursor = block.read_cursor
        first_len = min(length, capacity - cursor)
        if first_len == length:
            return self._read(data_start + cursor, first_len)
        return self._read(data_start + cursor, first_len) + self._read(data_start, length - first_len)

    def _read(self, relative_offset: int, length: int) -> bytes:
        return self._region.read(self._offset + relative_offset, length)

    def _write(self, relative_offset: int, payload: bytes) -> None:
        if payload:
            self._region.write(self._offset + relative_offset, payload)

    def _write_u32(self, offset: int, value: int) -> None:
        self._write(offset, int(value).to_bytes(4, "little"))

    def _write_u64(self, offset: int, value: int) -> None:
        self._write(offset, int(value).to_bytes(8, "little"))


def _available_read(block) -> int:
    if block.write_cursor >= block.read_cursor:
        return block.write_cursor - block.read_cursor
    return block.capacity - block.read_cursor + block.write_cursor


def _decode_frame_packet(packet: bytes) -> EncodedFrame:
    if len(packet) < FRAME_HEADER_STRUCT.size:
        raise InvalidPacketLength("shared ring packet length is invalid")
    header = unpack_frame_header(packet[: FRAME_HEADER_STRUCT.size])
    payload = packet[FRAME_HEADER_STRUCT.size :]
    if header.payload_length != len(payload):
        raise InvalidPacketLength("shared ring frame payload length is invalid")
    return EncodedFrame(header=header, payload=payload)
