from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from .base import ChannelClosed
from .framing import EncodedFrame
from .lanes import BackpressureAction, IpcLane, LanePolicy, lane_for_frame
from .protocol import (
    MAX_FRAME_LENGTH,
    MESSAGE_KIND_BYTES,
    MESSAGE_KIND_CLOSE_CHANNEL,
    MESSAGE_KIND_JSON,
    MESSAGE_KIND_OPEN_CHANNEL,
    RING_CONTROL_BLOCK_STRUCT,
    FrameHeader,
    logical_channel_name,
)
from .ring_buffer import NotEnoughData, QueueFull, SharedRingBuffer

_CLOSE = object()


class SharedMemoryProtocolError(RuntimeError):
    pass


@dataclass
class SharedMemoryRingWriter:
    region: object
    offset: int
    length: int
    notify_event: Optional[object] = None
    sequence_number: int = 1

    async def write(self, channel_id: int, stream_id: int, message_kind: int, payload: bytes) -> None:
        frame = EncodedFrame(
            header=FrameHeader(
                frame_version=1,
                logical_channel_id=channel_id,
                stream_id=stream_id,
                message_kind=message_kind,
                flags=0,
                sequence_number=self.sequence_number,
                correlation_id=0,
                payload_length=len(payload),
                fragment_index=0,
                fragment_count=1,
            ),
            payload=payload,
        )
        self.sequence_number = (self.sequence_number + 1) & 0xFFFFFFFFFFFFFFFF or 1
        before = bytearray(self.region.read(self.offset, self.length))
        raw_ring = bytearray(before)
        ring = SharedRingBuffer(raw_ring)
        write_frame_with_lane_policy(ring, frame)
        write_changed_ranges(self.region, self.offset, before, raw_ring)
        if self.notify_event is not None:
            await asyncio.to_thread(self.notify_event.set)


class SharedMemoryChannelEndpoint:
    def __init__(self, channel_id: int, stream_id: int, writer: SharedMemoryRingWriter) -> None:
        self.channel_id = channel_id
        self.stream_id = stream_id
        self.channel_name = logical_channel_name(channel_id)
        self._writer = writer
        self._incoming: asyncio.Queue[tuple[str, object] | object] = asyncio.Queue()
        self._closed = False

    async def queue_json(self, payload: dict) -> None:
        await self._incoming.put(("json", payload))

    async def queue_bytes(self, payload: bytes) -> None:
        await self._incoming.put(("bytes", payload))

    async def queue_close(self) -> None:
        self._closed = True
        await self._incoming.put(_CLOSE)

    async def recv_json(self) -> dict:
        item = await self._incoming.get()
        if item is _CLOSE:
            raise ChannelClosed
        kind, payload = item
        if kind != "json":
            raise TypeError(f"expected json frame, got {kind}")
        return payload

    async def recv_bytes(self) -> bytes:
        item = await self._incoming.get()
        if item is _CLOSE:
            raise ChannelClosed
        kind, payload = item
        if kind != "bytes":
            raise TypeError(f"expected bytes frame, got {kind}")
        return payload

    async def send_json(self, payload: dict) -> None:
        if self._closed:
            raise ChannelClosed
        await self._writer.write(
            self.channel_id,
            self.stream_id,
            MESSAGE_KIND_JSON,
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        )

    async def send_bytes(self, payload: bytes) -> None:
        if self._closed:
            raise ChannelClosed
        await self._writer.write(self.channel_id, self.stream_id, MESSAGE_KIND_BYTES, payload)

    async def close(self) -> None:
        if not self._closed:
            await self._writer.write(self.channel_id, self.stream_id, MESSAGE_KIND_CLOSE_CHANNEL, b"")
            await self.queue_close()


HandlerFactory = Callable[[SharedMemoryChannelEndpoint], Awaitable[None]]


class SharedMemoryChannelMux:
    def __init__(
        self,
        writer: SharedMemoryRingWriter,
        handler_factory: Optional[Callable[[str], HandlerFactory]] = None,
    ) -> None:
        self._writer = writer
        self._handler_factory = handler_factory
        self._endpoints: dict[tuple[int, int], SharedMemoryChannelEndpoint] = {}
        self._tasks: dict[tuple[int, int], asyncio.Task] = {}

    def endpoint(self, channel_id: int, stream_id: int = 0) -> Optional[SharedMemoryChannelEndpoint]:
        return self._endpoints.get((channel_id, stream_id))

    async def handle_frame(self, frame: EncodedFrame) -> None:
        header = frame.header
        channel_name = logical_channel_name(header.logical_channel_id)
        if header.message_kind == MESSAGE_KIND_OPEN_CHANNEL:
            await self._open_channel(header.logical_channel_id, header.stream_id, channel_name)
            return
        key = (header.logical_channel_id, header.stream_id)
        endpoint = self._endpoints.get(key)
        if endpoint is None:
            raise SharedMemoryProtocolError(f"channel is not open: {channel_name}/{header.stream_id}")
        if header.message_kind == MESSAGE_KIND_JSON:
            await endpoint.queue_json(json.loads(frame.payload.decode("utf-8")))
        elif header.message_kind == MESSAGE_KIND_BYTES:
            await endpoint.queue_bytes(frame.payload)
        elif header.message_kind == MESSAGE_KIND_CLOSE_CHANNEL:
            await endpoint.queue_close()
            task = self._tasks.pop(key, None)
            if task is not None:
                task.cancel()
        else:
            raise SharedMemoryProtocolError(f"unsupported message kind: {header.message_kind}")

    async def close(self) -> None:
        for endpoint in list(self._endpoints.values()):
            await endpoint.queue_close()
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._endpoints.clear()

    async def _open_channel(
        self,
        channel_id: int,
        stream_id: int,
        channel_name: str,
    ) -> SharedMemoryChannelEndpoint:
        key = (channel_id, stream_id)
        endpoint = self._endpoints.get(key)
        if endpoint is not None:
            return endpoint
        endpoint = SharedMemoryChannelEndpoint(channel_id, stream_id, self._writer)
        self._endpoints[key] = endpoint
        if self._handler_factory is not None:
            task = asyncio.create_task(self._handler_factory(channel_name)(endpoint))
            self._tasks[key] = task
        return endpoint


def write_frame_to_region(region: object, offset: int, length: int, frame: EncodedFrame) -> None:
    before = bytearray(region.read(offset, length))
    raw_ring = bytearray(before)
    ring = SharedRingBuffer(raw_ring)
    ring.write_frame(frame)
    write_changed_ranges(region, offset, before, raw_ring)


def read_frame_from_region(region: object, offset: int, length: int) -> EncodedFrame:
    before = bytearray(region.read(offset, length))
    raw_ring = bytearray(before)
    ring = SharedRingBuffer(raw_ring)
    frame = ring.read_frame(MAX_FRAME_LENGTH)
    write_changed_ranges(region, offset, before, raw_ring)
    return frame


def write_changed_ranges(region: object, base_offset: int, before: bytearray, after: bytearray) -> None:
    if len(before) != len(after):
        raise ValueError("ring snapshots must have the same length")
    index = 0
    length = len(after)
    ranges = []
    while index < length:
        if before[index] == after[index]:
            index += 1
            continue
        start = index
        while index < length and before[index] != after[index]:
            index += 1
        ranges.append((start, index))
    ranges.sort(key=lambda item: 1 if item[0] < RING_CONTROL_BLOCK_STRUCT.size else 0)
    for start, end in ranges:
        region.write(base_offset + start, bytes(after[start:end]))


def write_frame_with_lane_policy(ring: SharedRingBuffer, frame: EncodedFrame) -> None:
    lane = lane_for_frame(frame.header.logical_channel_id, frame.header.message_kind)
    policy = LanePolicy.for_lane(lane)
    while True:
        try:
            ring.write_frame(frame)
            return
        except QueueFull:
            if policy.backpressure != BackpressureAction.DROP_OLDEST:
                raise
            if not drop_oldest_frame_for_lane(ring, lane):
                raise


def drop_oldest_frame_for_lane(ring: SharedRingBuffer, lane: IpcLane) -> bool:
    try:
        oldest = ring.peek_frame(MAX_FRAME_LENGTH)
    except NotEnoughData:
        return False
    oldest_lane = lane_for_frame(oldest.header.logical_channel_id, oldest.header.message_kind)
    if oldest_lane != lane:
        return False
    ring.drop_frame(MAX_FRAME_LENGTH)
    return True
