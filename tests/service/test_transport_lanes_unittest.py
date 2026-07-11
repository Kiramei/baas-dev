from __future__ import annotations

import unittest

from service.transport.lanes import BackpressureAction, IpcLane, LanePolicy
from service.transport.lanes import lane_for_frame
from service.transport.protocol import (
    CHANNEL_REMOTE,
    CHANNEL_SYNC,
    MESSAGE_KIND_BYTES,
    MESSAGE_KIND_JSON,
    MESSAGE_KIND_OPEN_CHANNEL,
)


class TransportLanePolicyTests(unittest.TestCase):
    def test_control_lane_is_reliable_and_never_drops(self) -> None:
        policy = LanePolicy.for_lane(IpcLane.CONTROL)

        self.assertTrue(policy.reliable)
        self.assertEqual(policy.backpressure, BackpressureAction.ERROR)

    def test_message_and_bulk_lanes_wait_instead_of_dropping(self) -> None:
        self.assertEqual(LanePolicy.for_lane(IpcLane.MESSAGE).backpressure, BackpressureAction.WAIT)
        self.assertEqual(LanePolicy.for_lane(IpcLane.BULK).backpressure, BackpressureAction.WAIT)

    def test_remote_media_lane_can_drop_old_frames(self) -> None:
        policy = LanePolicy.for_lane(IpcLane.REMOTE_MEDIA)

        self.assertFalse(policy.reliable)
        self.assertEqual(policy.backpressure, BackpressureAction.DROP_OLDEST)

    def test_lane_classifier_routes_remote_bytes_to_remote_media(self) -> None:
        self.assertEqual(lane_for_frame(CHANNEL_REMOTE, MESSAGE_KIND_BYTES), IpcLane.REMOTE_MEDIA)
        self.assertEqual(lane_for_frame(CHANNEL_REMOTE, MESSAGE_KIND_JSON), IpcLane.MESSAGE)
        self.assertEqual(lane_for_frame(CHANNEL_SYNC, MESSAGE_KIND_BYTES), IpcLane.BULK)
        self.assertEqual(lane_for_frame(CHANNEL_SYNC, MESSAGE_KIND_OPEN_CHANNEL), IpcLane.CONTROL)


if __name__ == "__main__":
    unittest.main()
