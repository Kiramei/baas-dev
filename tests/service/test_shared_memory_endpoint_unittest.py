from __future__ import annotations

import asyncio
import json
import unittest

from service.transport.framing import fragment_payload
from service.transport.protocol import (
    CHANNEL_REMOTE,
    CHANNEL_SYNC,
    MESSAGE_KIND_BYTES,
    MESSAGE_KIND_CLOSE_CHANNEL,
    MESSAGE_KIND_JSON,
    MESSAGE_KIND_OPEN_CHANNEL,
    RING_CONTROL_BLOCK_STRUCT,
    new_ring_control_block,
    pack_ring_control_block,
)
from service.transport import ChannelClosed
from service.transport.ring_buffer import NotEnoughData, QueueFull
from service.transport.shared_memory_endpoint import (
    SharedMemoryChannelMux,
    SharedMemoryRingWriter,
    read_frame_from_region,
)


class FakeRegion:
    def __init__(self, size: int) -> None:
        self.data = bytearray(size)
        self.size = size

    def read(self, offset: int, length: int) -> bytes:
        return bytes(self.data[offset : offset + length])

    def write(self, offset: int, data: bytes) -> None:
        self.data[offset : offset + len(data)] = data


class FakeEvent:
    def __init__(self) -> None:
        self.set_count = 0

    def set(self) -> None:
        self.set_count += 1


def init_ring(region: FakeRegion, offset: int, length: int) -> None:
    block = new_ring_control_block(length - RING_CONTROL_BLOCK_STRUCT.size, 1, 2)
    region.write(offset, pack_ring_control_block(block))


def frame(channel_id: int, message_kind: int, payload: bytes = b"", stream_id: int = 0):
    return fragment_payload(channel_id, stream_id, message_kind, 0, 1, 0, payload, 1024)[0]


class SharedMemoryEndpointTests(unittest.TestCase):
    def test_mux_routes_json_bytes_and_close_to_endpoint(self) -> None:
        async def run() -> None:
            region = FakeRegion(2048)
            init_ring(region, 0, 1024)
            writer = SharedMemoryRingWriter(region, 0, 1024)
            mux = SharedMemoryChannelMux(writer)

            await mux.handle_frame(frame(CHANNEL_SYNC, MESSAGE_KIND_OPEN_CHANNEL))
            endpoint = mux.endpoint(CHANNEL_SYNC)
            self.assertIsNotNone(endpoint)

            await mux.handle_frame(frame(CHANNEL_SYNC, MESSAGE_KIND_JSON, b'{"type":"list"}'))
            await mux.handle_frame(frame(CHANNEL_SYNC, MESSAGE_KIND_BYTES, b"abc"))
            await mux.handle_frame(frame(CHANNEL_SYNC, MESSAGE_KIND_CLOSE_CHANNEL))

            assert endpoint is not None
            self.assertEqual(await endpoint.recv_json(), {"type": "list"})
            self.assertEqual(await endpoint.recv_bytes(), b"abc")
            with self.assertRaises(ChannelClosed):
                await endpoint.recv_json()

        asyncio.run(run())

    def test_endpoint_send_json_writes_outbound_frame(self) -> None:
        async def run() -> None:
            region = FakeRegion(2048)
            init_ring(region, 512, 1024)
            event = FakeEvent()
            writer = SharedMemoryRingWriter(region, 512, 1024, event)
            mux = SharedMemoryChannelMux(writer)
            await mux.handle_frame(frame(CHANNEL_SYNC, MESSAGE_KIND_OPEN_CHANNEL))
            endpoint = mux.endpoint(CHANNEL_SYNC)
            assert endpoint is not None

            await endpoint.send_json({"type": "snapshot", "data": {"ok": True}})

            outbound = read_frame_from_region(region, 512, 1024)
            self.assertEqual(outbound.header.logical_channel_id, CHANNEL_SYNC)
            self.assertEqual(outbound.header.stream_id, 0)
            self.assertEqual(outbound.header.message_kind, MESSAGE_KIND_JSON)
            self.assertEqual(
                json.loads(outbound.payload.decode("utf-8")),
                {"type": "snapshot", "data": {"ok": True}},
            )
            self.assertEqual(event.set_count, 1)

        asyncio.run(run())

    def test_mux_handler_receives_request_and_writes_response(self) -> None:
        async def handler(endpoint) -> None:
            request = await endpoint.recv_json()
            await endpoint.send_json({"type": "ack", "seen": request["type"]})

        async def run() -> None:
            region = FakeRegion(2048)
            init_ring(region, 256, 1024)
            writer = SharedMemoryRingWriter(region, 256, 1024)
            mux = SharedMemoryChannelMux(writer, lambda _name: handler)

            await mux.handle_frame(frame(CHANNEL_SYNC, MESSAGE_KIND_OPEN_CHANNEL))
            await mux.handle_frame(frame(CHANNEL_SYNC, MESSAGE_KIND_JSON, b'{"type":"list"}'))
            await asyncio.sleep(0)

            outbound = read_frame_from_region(region, 256, 1024)
            self.assertEqual(outbound.header.logical_channel_id, CHANNEL_SYNC)
            self.assertEqual(outbound.header.stream_id, 0)
            self.assertEqual(outbound.header.message_kind, MESSAGE_KIND_JSON)
            self.assertEqual(json.loads(outbound.payload.decode("utf-8")), {"type": "ack", "seen": "list"})

        asyncio.run(run())

    def test_mux_keeps_dynamic_streams_separate(self) -> None:
        async def run() -> None:
            region = FakeRegion(4096)
            init_ring(region, 512, 2048)
            writer = SharedMemoryRingWriter(region, 512, 2048)
            mux = SharedMemoryChannelMux(writer)

            await mux.handle_frame(frame(CHANNEL_SYNC, MESSAGE_KIND_OPEN_CHANNEL, stream_id=7))
            await mux.handle_frame(frame(CHANNEL_SYNC, MESSAGE_KIND_OPEN_CHANNEL, stream_id=8))
            await mux.handle_frame(frame(CHANNEL_SYNC, MESSAGE_KIND_JSON, b'{"type":"a"}', stream_id=7))
            await mux.handle_frame(frame(CHANNEL_SYNC, MESSAGE_KIND_JSON, b'{"type":"b"}', stream_id=8))

            endpoint_a = mux.endpoint(CHANNEL_SYNC, 7)
            endpoint_b = mux.endpoint(CHANNEL_SYNC, 8)
            assert endpoint_a is not None
            assert endpoint_b is not None
            self.assertEqual(await endpoint_a.recv_json(), {"type": "a"})
            self.assertEqual(await endpoint_b.recv_json(), {"type": "b"})
            await endpoint_b.send_json({"type": "reply-b"})

            outbound = read_frame_from_region(region, 512, 2048)
            self.assertEqual(outbound.header.stream_id, 8)
            self.assertEqual(json.loads(outbound.payload.decode("utf-8")), {"type": "reply-b"})

        asyncio.run(run())

    def test_remote_media_bytes_drop_oldest_frames_under_backpressure(self) -> None:
        async def run() -> None:
            region = FakeRegion(2048)
            init_ring(region, 512, 1024)
            writer = SharedMemoryRingWriter(region, 512, 1024)
            mux = SharedMemoryChannelMux(writer)
            await mux.handle_frame(frame(CHANNEL_REMOTE, MESSAGE_KIND_OPEN_CHANNEL, stream_id=3))
            endpoint = mux.endpoint(CHANNEL_REMOTE, 3)
            assert endpoint is not None

            for index in range(20):
                await endpoint.send_bytes(f"video-frame-{index:02d}".encode("ascii") * 16)

            payloads = []
            while True:
                try:
                    payloads.append(read_frame_from_region(region, 512, 1024).payload)
                except NotEnoughData:
                    break

            self.assertLess(len(payloads), 20)
            self.assertEqual(payloads[-1], b"video-frame-19" * 16)

        asyncio.run(run())

    def test_reliable_bulk_bytes_do_not_drop_on_full_ring(self) -> None:
        async def run() -> None:
            region = FakeRegion(2048)
            init_ring(region, 512, 1024)
            writer = SharedMemoryRingWriter(region, 512, 1024)
            mux = SharedMemoryChannelMux(writer)
            await mux.handle_frame(frame(CHANNEL_SYNC, MESSAGE_KIND_OPEN_CHANNEL))
            endpoint = mux.endpoint(CHANNEL_SYNC)
            assert endpoint is not None

            with self.assertRaises(QueueFull):
                for _ in range(20):
                    await endpoint.send_bytes(b"bulk-frame" * 32)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
