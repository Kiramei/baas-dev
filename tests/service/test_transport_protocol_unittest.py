from __future__ import annotations

import unittest

from service.transport.protocol import (
    ABI_VERSION,
    FRAME_HEADER_STRUCT,
    LIFECYCLE_READY,
    MAGIC,
    MAX_MESSAGE_LENGTH,
    PROTOCOL_VERSION,
    RING_CONTROL_BLOCK_STRUCT,
    SHARED_MEMORY_HEADER_STRUCT,
    FrameHeader,
    SharedMemoryHeader,
    new_ring_control_block,
    pack_frame_header,
    pack_ring_control_block,
    pack_shared_memory_header,
    unpack_frame_header,
    unpack_ring_control_block,
    unpack_shared_memory_header,
)
from service.transport.framing import (
    FragmentMetadataMismatch,
    IncompleteFragments,
    MessageTooLarge,
    fragment_payload,
    reassemble_frames,
)


class TransportProtocolGoldenTests(unittest.TestCase):
    SHARED_MEMORY_HEADER_HEX = (
        "4241415349504300010001007c0000000010000008070605040302011817161514131211"
        "64000000c800000002000000000000002c01000000000000900100000000000000020000"
        "000400000006000000040000000200008000000080020000800300000006000000040000"
        "000a000000040000000f000000000000"
    )
    FRAME_HEADER_HEX = (
        "0100020003000400050000000600000000000000070000000000000008000000090000000a000000"
    )
    RING_CONTROL_BLOCK_HEX = (
        "42414153524e470001004000000000000700000000000000000000000807060504030201"
        "18171615141312110000000000000000000000000000000000000000"
    )
    FRAGMENT_HEADER_HEXES = [
        "01000200030004000500000006000000000000000700000000000000030000000000000003000000",
        "01000200030004000500000006000000000000000700000000000000030000000100000003000000",
        "01000200030004000500000006000000000000000700000000000000020000000200000003000000",
    ]

    def test_frame_header_matches_rust_golden_vector(self) -> None:
        frame = FrameHeader(
            frame_version=1,
            logical_channel_id=2,
            stream_id=3,
            message_kind=4,
            flags=5,
            sequence_number=6,
            correlation_id=7,
            payload_length=8,
            fragment_index=9,
            fragment_count=10,
        )

        encoded = pack_frame_header(frame)

        self.assertEqual(FRAME_HEADER_STRUCT.size, 40)
        self.assertEqual(encoded.hex(), self.FRAME_HEADER_HEX)
        self.assertEqual(unpack_frame_header(encoded), frame)

    def test_frame_header_rejects_invalid_fragment_bounds(self) -> None:
        frame = FrameHeader(
            frame_version=1,
            logical_channel_id=2,
            stream_id=3,
            message_kind=4,
            flags=5,
            sequence_number=6,
            correlation_id=7,
            payload_length=8,
            fragment_index=1,
            fragment_count=1,
        )

        with self.assertRaisesRegex(ValueError, "fragment index"):
            pack_frame_header(frame)

    def test_shared_memory_header_round_trips(self) -> None:
        header = SharedMemoryHeader(
            magic=MAGIC,
            protocol_version=PROTOCOL_VERSION,
            abi_version=ABI_VERSION,
            header_size=SHARED_MEMORY_HEADER_STRUCT.size,
            total_size=4096,
            generation_id_low=0x0102030405060708,
            generation_id_high=0x1112131415161718,
            owner_pid=100,
            peer_pid=200,
            lifecycle_state=LIFECYCLE_READY,
            last_error_code=0,
            owner_heartbeat_ns=300,
            peer_heartbeat_ns=400,
            rust_to_python_ring_offset=512,
            rust_to_python_ring_length=1024,
            python_to_rust_ring_offset=1536,
            python_to_rust_ring_length=1024,
            control_lane_offset=512,
            control_lane_length=128,
            message_lane_offset=640,
            message_lane_length=896,
            bulk_lane_offset=1536,
            bulk_lane_length=1024,
            remote_lane_offset=2560,
            remote_lane_length=1024,
            last_error_offset=3840,
            last_error_length=0,
        )

        encoded = pack_shared_memory_header(header)

        self.assertEqual(SHARED_MEMORY_HEADER_STRUCT.size, 124)
        self.assertEqual(encoded.hex(), self.SHARED_MEMORY_HEADER_HEX)
        self.assertEqual(unpack_shared_memory_header(encoded), header)

    def test_shared_memory_header_rejects_protocol_version_mismatch(self) -> None:
        encoded = bytearray.fromhex(self.SHARED_MEMORY_HEADER_HEX)
        encoded[8:10] = (PROTOCOL_VERSION + 1).to_bytes(2, "little")

        with self.assertRaisesRegex(ValueError, "protocol version mismatch"):
            unpack_shared_memory_header(bytes(encoded))

    def test_ring_control_block_matches_rust_golden_vector(self) -> None:
        block = new_ring_control_block(7, 0x0102030405060708, 0x1112131415161718)

        encoded = pack_ring_control_block(block)

        self.assertEqual(RING_CONTROL_BLOCK_STRUCT.size, 64)
        self.assertEqual(encoded.hex(), self.RING_CONTROL_BLOCK_HEX)
        self.assertEqual(unpack_ring_control_block(encoded), block)

    def test_ring_control_block_rejects_abi_version_mismatch(self) -> None:
        encoded = bytearray.fromhex(self.RING_CONTROL_BLOCK_HEX)
        encoded[8:10] = (ABI_VERSION + 1).to_bytes(2, "little")

        with self.assertRaisesRegex(ValueError, "protocol version mismatch"):
            unpack_ring_control_block(bytes(encoded))

    def test_fragments_and_reassembles_payload(self) -> None:
        frames = fragment_payload(2, 3, 4, 5, 6, 7, b"abcdefgh", 3)

        self.assertEqual(len(frames), 3)
        self.assertEqual(frames[0].payload, b"abc")
        self.assertEqual(frames[1].payload, b"def")
        self.assertEqual(frames[2].payload, b"gh")
        self.assertTrue(all(frame.header.fragment_count == 3 for frame in frames))
        self.assertEqual(
            [pack_frame_header(frame.header).hex() for frame in frames],
            self.FRAGMENT_HEADER_HEXES,
        )
        self.assertEqual(reassemble_frames(frames), b"abcdefgh")

    def test_fragments_empty_payload_as_single_empty_frame(self) -> None:
        frames = fragment_payload(2, 3, 4, 5, 6, 7, b"", 3)

        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].payload, b"")
        self.assertEqual(frames[0].header.fragment_count, 1)
        self.assertEqual(reassemble_frames(frames), b"")

    def test_rejects_oversized_message(self) -> None:
        with self.assertRaises(MessageTooLarge):
            fragment_payload(2, 3, 4, 5, 6, 7, b"x" * (MAX_MESSAGE_LENGTH + 1), 1024)

    def test_reassembly_rejects_missing_fragment(self) -> None:
        frames = fragment_payload(2, 3, 4, 5, 6, 7, b"abcdefgh", 3)

        with self.assertRaises(IncompleteFragments):
            reassemble_frames(frames[:-1])

    def test_reassembly_rejects_metadata_mismatch(self) -> None:
        frames = fragment_payload(2, 3, 4, 5, 6, 7, b"abcdefgh", 3)
        changed = frames[1]
        frames[1] = type(changed)(
            header=FrameHeader(
                frame_version=changed.header.frame_version,
                logical_channel_id=changed.header.logical_channel_id,
                stream_id=changed.header.stream_id,
                message_kind=changed.header.message_kind,
                flags=changed.header.flags,
                sequence_number=changed.header.sequence_number,
                correlation_id=99,
                payload_length=changed.header.payload_length,
                fragment_index=changed.header.fragment_index,
                fragment_count=changed.header.fragment_count,
            ),
            payload=changed.payload,
        )

        with self.assertRaises(FragmentMetadataMismatch):
            reassemble_frames(frames)


if __name__ == "__main__":
    unittest.main()
