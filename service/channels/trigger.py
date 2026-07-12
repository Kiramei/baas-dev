from __future__ import annotations

import asyncio
from contextlib import suppress
import os
from typing import Any, Dict, Optional

from service.api.commands import execute_command
from service.transport import ChannelClosed, ChannelEndpoint
from service.types import CommandMessage


class TriggerChannelHandler:
    def __init__(self, service_context: Any) -> None:
        self.context = service_context

    async def handle(self, endpoint: ChannelEndpoint) -> None:
        pending: set[asyncio.Task[None]] = set()
        send_lock = asyncio.Lock()
        closed_normally = False
        try:
            while True:
                message = await endpoint.recv_json()
                cmd = CommandMessage(**message)
                binary_payload = None
                if cmd.command == "import_config" and cmd.payload.get("binary") is True:
                    binary_payload = await endpoint.recv_bytes()
                task = asyncio.create_task(self._dispatch(endpoint, send_lock, cmd, binary_payload))
                pending.add(task)
                task.add_done_callback(pending.discard)
        except ChannelClosed:
            closed_normally = True
        finally:
            if pending:
                if closed_normally:
                    await asyncio.gather(*pending, return_exceptions=True)
                else:
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)

    async def _dispatch(
        self,
        endpoint: ChannelEndpoint,
        send_lock: asyncio.Lock,
        cmd: CommandMessage,
        binary_payload: Optional[bytes],
    ) -> None:
        with suppress(ChannelClosed):
            response_payload: Dict[str, Any]
            if os.environ.get("BAAS_SHM_TEST_COMMANDS") == "1" and cmd.command in {
                "transport_stream_test",
                "transport_stream_error",
            }:
                if cmd.command == "transport_stream_test":
                    for index in range(2):
                        await self._send_response(
                            endpoint,
                            send_lock,
                            cmd,
                            {"status": "ok", "data": {"chunk": index}},
                        )
                    response_payload = {"status": "ok", "data": {"done": True}}
                else:
                    response_payload = {
                        "status": "error",
                        "error": "transport stream test error",
                        "data": {"done": True},
                    }
                await self._send_response(endpoint, send_lock, cmd, response_payload)
                return
            if cmd.command == "test_all_sha_stream":
                try:
                    async for result in self.context.runtime.test_all_sha_stream(
                        cmd.payload.get("channel"),
                        cmd.payload.get("timeout"),
                    ):
                        await self._send_response(
                            endpoint,
                            send_lock,
                            cmd,
                            {"status": "ok", "data": result},
                        )
                    response_payload = {"status": "ok", "data": {"done": True}}
                except Exception as inner_exc:  # noqa: BLE001 - returned to frontend
                    response_payload = {"status": "error", "error": str(inner_exc), "data": {"done": True}}
                await self._send_response(endpoint, send_lock, cmd, response_payload)
                return
            if cmd.command == "update_to_latest_stream":
                try:
                    async for result in self.context.runtime.update_to_latest_stream():
                        await self._send_response(
                            endpoint,
                            send_lock,
                            cmd,
                            {"status": "ok", "data": result},
                        )
                    response_payload = {"status": "ok", "data": {"done": True}}
                except Exception as inner_exc:  # noqa: BLE001 - returned to frontend
                    response_payload = {"status": "error", "error": str(inner_exc), "data": {"done": True}}
                await self._send_response(endpoint, send_lock, cmd, response_payload)
                return
            try:
                response_payload = await execute_command(cmd, binary_payload=binary_payload)
            except Exception as inner_exc:  # noqa: BLE001 - returned to frontend
                response_payload = {"status": "error", "error": str(inner_exc)}
            await self._send_response(endpoint, send_lock, cmd, response_payload)

    @staticmethod
    async def _send_response(
        endpoint: ChannelEndpoint,
        send_lock: asyncio.Lock,
        cmd: CommandMessage,
        response_payload: Dict[str, Any],
    ) -> None:
        binary_response = response_payload.pop("_binary", None)
        if binary_response is not None:
            data = response_payload.setdefault("data", {})
            data["binary"] = {
                "size": len(binary_response),
            }
        async with send_lock:
            await endpoint.send_json(
                {
                    "type": "command_response",
                    "command": cmd.command,
                    **response_payload,
                    "timestamp": cmd.timestamp,
                }
            )
            if binary_response is not None:
                await endpoint.send_bytes(binary_response)
