from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from typing import Any

from service.transport import ChannelClosed, ChannelEndpoint
from service.types import SyncPatchMessage, SyncPullMessage


async def sync_sender(endpoint: ChannelEndpoint, queue: asyncio.Queue) -> None:
    try:
        while True:
            payload = dict(await queue.get())
            payload.setdefault("direction", "push")
            await endpoint.send_json(payload)
    except (asyncio.CancelledError, ChannelClosed):
        pass


class SyncChannelHandler:
    def __init__(self, service_context: Any) -> None:
        self.context = service_context

    async def handle(self, endpoint: ChannelEndpoint) -> None:
        queue = None
        sender_task = None
        try:
            queue = await self.context.config_manager.subscribe_updates()
            sender_task = asyncio.create_task(sync_sender(endpoint, queue))
            while True:
                message = await endpoint.recv_json()
                msg_type = message.get("type")
                if msg_type == "pull":
                    if _is_transport_test_resource(message.get("resource")):
                        await endpoint.send_json(
                            {
                                "type": "snapshot",
                                "resource": message.get("resource"),
                                "resource_id": message.get("resource_id"),
                                "timestamp": 7001,
                                "data": {"transport": "shared-memory", "resource_id": message.get("resource_id")},
                            }
                        )
                        continue
                    data = SyncPullMessage(**message)
                    snapshot = await self.context.config_manager.get_snapshot(data.resource, data.resource_id)
                    await endpoint.send_json(
                        {
                            "type": "snapshot",
                            "resource": data.resource,
                            "resource_id": data.resource_id,
                            "timestamp": snapshot.timestamp,
                            "data": snapshot.data,
                        }
                    )
                elif msg_type == "patch":
                    if _is_transport_test_resource(message.get("resource")):
                        await endpoint.send_json(
                            {
                                "type": "patch_ack",
                                "resource": message.get("resource"),
                                "resource_id": message.get("resource_id"),
                                "timestamp": message.get("timestamp"),
                            }
                        )
                        continue
                    data = SyncPatchMessage(**message)
                    await self.context.config_manager.apply_patch(
                        data.resource,
                        data.resource_id,
                        data.ops,
                        data.timestamp,
                        origin="frontend",
                    )
                    await endpoint.send_json(
                        {
                            "type": "patch_ack",
                            "resource": data.resource,
                            "resource_id": data.resource_id,
                            "timestamp": data.timestamp,
                        }
                    )
                elif msg_type == "list":
                    snapshot = await self.context.config_manager.get_config_list()
                    await endpoint.send_json(
                        {
                            "type": "config_list",
                            "timestamp": snapshot.timestamp,
                            "data": snapshot.data,
                        }
                    )
                else:
                    raise ValueError(f"Unsupported sync message: {msg_type}")
        finally:
            if sender_task:
                sender_task.cancel()
                with suppress(asyncio.CancelledError):
                    await sender_task
            if queue is not None:
                self.context.config_manager.unsubscribe_updates(queue)


def _is_transport_test_resource(resource: object) -> bool:
    return os.environ.get("BAAS_SHM_TEST_COMMANDS") == "1" and resource == "transport_test"
