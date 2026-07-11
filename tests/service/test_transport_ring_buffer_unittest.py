from __future__ import annotations

import unittest

from service.transport.framing import fragment_payload
from service.transport.protocol import FRAME_HEADER_STRUCT, RING_CONTROL_BLOCK_STRUCT
from service.transport.ring_buffer import (
    NotEnoughData,
    QueueFull,
    SharedRingBuffer,
    SpscRingBuffer,
)


class TransportRingBufferTests(unittest.TestCase):
    def test_empty_payload_round_trips(self) -> None:
        ring = SpscRingBuffer(4)

        ring.write(b"")

        self.assertEqual(ring.read(0), b"")
        self.assertEqual(ring.length, 0)

    def test_wraps_around_capacity_boundary(self) -> None:
        ring = SpscRingBuffer(5)

        ring.write(b"abcd")
        self.assertEqual(ring.read(3), b"abc")
        ring.write(b"efgh")

        self.assertEqual(ring.read(5), b"defgh")

    def test_queue_full_does_not_mutate_existing_bytes(self) -> None:
        ring = SpscRingBuffer(4)

        ring.write(b"abc")
        with self.assertRaises(QueueFull):
            ring.write(b"de")

        self.assertEqual(ring.read(3), b"abc")

    def test_read_past_available_data_fails(self) -> None:
        ring = SpscRingBuffer(4)
        ring.write(b"ab")

        with self.assertRaises(NotEnoughData):
            ring.read(3)

    def test_shared_ring_writes_and_reads_packet(self) -> None:
        ring = SharedRingBuffer.initialize(bytearray(RING_CONTROL_BLOCK_STRUCT.size + 16), 1, 2)

        ring.write_packet(b"hello")

        self.assertEqual(ring.available_read, 9)
        self.assertEqual(ring.read_packet(16), b"hello")
        self.assertEqual(ring.available_read, 0)

    def test_shared_ring_wraps_packet_across_data_boundary(self) -> None:
        ring = SharedRingBuffer.initialize(bytearray(RING_CONTROL_BLOCK_STRUCT.size + 12), 1, 2)

        ring.write_packet(b"ab")
        self.assertEqual(ring.read_packet(8), b"ab")
        ring.write_packet(b"cde")

        self.assertEqual(ring.read_packet(8), b"cde")

    def test_shared_ring_rejects_packet_when_queue_full(self) -> None:
        ring = SharedRingBuffer.initialize(bytearray(RING_CONTROL_BLOCK_STRUCT.size + 10), 1, 2)

        with self.assertRaises(QueueFull):
            ring.write_packet(b"abcdef")

        self.assertEqual(ring.available_read, 0)

    def test_shared_ring_round_trips_encoded_frame(self) -> None:
        ring = SharedRingBuffer.initialize(bytearray(RING_CONTROL_BLOCK_STRUCT.size + 128), 1, 2)
        frame = fragment_payload(2, 3, 4, 5, 6, 7, b"abc", 8)[0]

        ring.write_frame(frame)

        self.assertEqual(ring.available_read, 4 + FRAME_HEADER_STRUCT.size + len(frame.payload))
        self.assertEqual(ring.read_frame(8), frame)


if __name__ == "__main__":
    unittest.main()
