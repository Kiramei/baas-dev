from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, Optional

from service.transport import ChannelClosed, ChannelEndpoint
from service.types import ProviderRequest


async def provider_sender(
    endpoint: ChannelEndpoint,
    queue: asyncio.Queue,
    envelope_type: str,
    send_lock: Optional[asyncio.Lock] = None,
) -> None:
    if send_lock is None:
        send_lock = asyncio.Lock()
    try:
        while True:
            payload = await queue.get()
            if envelope_type == "status":
                response = {"type": envelope_type, "status": payload}
            else:
                response = {"type": envelope_type, "entry": payload}
            async with send_lock:
                await endpoint.send_json(response)
    except (asyncio.CancelledError, ChannelClosed):
        pass


class ProviderChannelHandler:
    def __init__(self, service_context: Any) -> None:
        self.context = service_context

    async def handle(self, endpoint: ChannelEndpoint) -> None:
        log_queue = status_queue = None
        log_task = status_task = None
        try:
            send_lock = asyncio.Lock()
            history = self.context.log_manager.get_history()
            scopes = self.context.log_manager.get_scopes()
            async with send_lock:
                await endpoint.send_json({"type": "logs_full", "scopes": scopes, "entries": history})
            async with send_lock:
                await endpoint.send_json({"type": "status", "status": self.context.runtime.current_status()})
            if self.context.runtime.is_all_data_initialized:
                async with send_lock:
                    await endpoint.send_json({"type": "status", "status": {"is_all_data_initialized": True}})
            log_queue = await self.context.log_manager.subscribe()
            status_queue = await self.context.runtime.subscribe_status()
            log_task = asyncio.create_task(provider_sender(endpoint, log_queue, "log", send_lock))
            status_task = asyncio.create_task(provider_sender(endpoint, status_queue, "status", send_lock))
            while True:
                message = await endpoint.recv_json()
                req_type = message.get("type")
                if req_type == "static_request":
                    ProviderRequest(**message)
                    snapshot = await self.context.config_manager.get_static_snapshot()
                    async with send_lock:
                        await endpoint.send_json(
                            {
                                "type": "static_snapshot",
                                "timestamp": snapshot.timestamp,
                                "data": snapshot.data,
                            }
                        )
                elif req_type == "status_request":
                    async with send_lock:
                        await endpoint.send_json({"type": "status", "status": self.context.runtime.current_status()})
                else:
                    raise ValueError(f"Unsupported provider message: {req_type}")
        finally:
            for task in (log_task, status_task):
                if task:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            if log_queue is not None:
                self.context.log_manager.unsubscribe(log_queue)
            if status_queue is not None:
                self.context.runtime.unsubscribe_status(status_queue)
