from __future__ import annotations

from dataclasses import dataclass

from .protocol import MAX_FRAME_LENGTH, MAX_MESSAGE_LENGTH, FrameHeader


class FramingError(Exception):
    pass


class MessageTooLarge(FramingError):
    pass


class IncompleteFragments(FramingError):
    pass


class FragmentMetadataMismatch(FramingError):
    pass


class FragmentOrderMismatch(FramingError):
    pass


@dataclass(frozen=True)
class EncodedFrame:
    header: FrameHeader
    payload: bytes


def fragment_payload(
    logical_channel_id: int,
    stream_id: int,
    message_kind: int,
    flags: int,
    sequence_number: int,
    correlation_id: int,
    payload: bytes,
    max_payload_per_frame: int,
) -> list[EncodedFrame]:
    if len(payload) > MAX_MESSAGE_LENGTH:
        raise MessageTooLarge("message length exceeds shared-memory message limit")
    if max_payload_per_frame <= 0 or max_payload_per_frame > MAX_FRAME_LENGTH:
        raise ValueError("invalid max payload per frame")
    fragment_count = 1 if not payload else (len(payload) + max_payload_per_frame - 1) // max_payload_per_frame
    frames: list[EncodedFrame] = []
    for index in range(fragment_count):
        if payload:
            start = index * max_payload_per_frame
            chunk = payload[start : start + max_payload_per_frame]
        else:
            chunk = b""
        frames.append(
            EncodedFrame(
                FrameHeader(
                    frame_version=1,
                    logical_channel_id=logical_channel_id,
                    stream_id=stream_id,
                    message_kind=message_kind,
                    flags=flags,
                    sequence_number=sequence_number,
                    correlation_id=correlation_id,
                    payload_length=len(chunk),
                    fragment_index=index,
                    fragment_count=fragment_count,
                ),
                chunk,
            )
        )
    return frames


def reassemble_frames(frames: list[EncodedFrame]) -> bytes:
    if not frames:
        raise IncompleteFragments("fragment sequence is incomplete")
    first = frames[0].header
    if len(frames) != first.fragment_count:
        raise IncompleteFragments("fragment sequence is incomplete")
    output = bytearray()
    for index, frame in enumerate(frames):
        header = frame.header
        if (
            header.logical_channel_id != first.logical_channel_id
            or header.stream_id != first.stream_id
            or header.message_kind != first.message_kind
            or header.sequence_number != first.sequence_number
            or header.correlation_id != first.correlation_id
            or header.fragment_count != first.fragment_count
        ):
            raise FragmentMetadataMismatch("fragment metadata does not match the first fragment")
        if header.fragment_index != index:
            raise FragmentOrderMismatch("fragment index order is invalid")
        if header.payload_length != len(frame.payload):
            raise ValueError("fragment payload length does not match header")
        output.extend(frame.payload)
        if len(output) > MAX_MESSAGE_LENGTH:
            raise MessageTooLarge("message length exceeds shared-memory message limit")
    return bytes(output)
