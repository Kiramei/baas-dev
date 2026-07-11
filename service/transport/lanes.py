from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .protocol import CHANNEL_REMOTE, MESSAGE_KIND_BYTES, MESSAGE_KIND_CLOSE_CHANNEL, MESSAGE_KIND_OPEN_CHANNEL


class IpcLane(str, Enum):
    CONTROL = "control"
    MESSAGE = "message"
    BULK = "bulk"
    REMOTE_MEDIA = "remote_media"


class BackpressureAction(str, Enum):
    WAIT = "wait"
    ERROR = "error"
    DROP_OLDEST = "drop_oldest"
    COALESCE_LATEST = "coalesce_latest"


@dataclass(frozen=True)
class LanePolicy:
    lane: IpcLane
    reliable: bool
    backpressure: BackpressureAction
    max_queue_bytes: int

    @staticmethod
    def for_lane(lane: IpcLane) -> "LanePolicy":
        if lane == IpcLane.CONTROL:
            return LanePolicy(lane, True, BackpressureAction.ERROR, 256 * 1024)
        if lane == IpcLane.MESSAGE:
            return LanePolicy(lane, True, BackpressureAction.WAIT, 2 * 1024 * 1024)
        if lane == IpcLane.BULK:
            return LanePolicy(lane, True, BackpressureAction.WAIT, 16 * 1024 * 1024)
        if lane == IpcLane.REMOTE_MEDIA:
            return LanePolicy(lane, False, BackpressureAction.DROP_OLDEST, 8 * 1024 * 1024)
        raise ValueError(f"unsupported IPC lane: {lane}")


def lane_for_frame(channel_id: int, message_kind: int) -> IpcLane:
    if message_kind in {MESSAGE_KIND_OPEN_CHANNEL, MESSAGE_KIND_CLOSE_CHANNEL}:
        return IpcLane.CONTROL
    if channel_id == CHANNEL_REMOTE and message_kind == MESSAGE_KIND_BYTES:
        return IpcLane.REMOTE_MEDIA
    if message_kind == MESSAGE_KIND_BYTES:
        return IpcLane.BULK
    return IpcLane.MESSAGE
