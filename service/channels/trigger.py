from __future__ import annotations

import os
from typing import Any, Dict

from service.api.commands import execute_command
from service.transport import ChannelEndpoint
from service.types import CommandMessage


class TriggerChannelHandler:
    def __init__(self, service_context: Any) -> None:
        self.context = service_context

    async def handle(self, endpoint: ChannelEndpoint) -> None:
        while True:
            message = await endpoint.recv_json()
            cmd = CommandMessage(**message)
            binary_payload = None
            if cmd.command == "import_config" and cmd.payload.get("binary") is True:
                binary_payload = await endpoint.recv_bytes()
            response_payload: Dict[str, Any]
            if os.environ.get("BAAS_SHM_TEST_COMMANDS") == "1" and cmd.command in {
                "transport_stream_test",
                "transport_stream_error",
            }:
                if cmd.command == "transport_stream_test":
                    for index in range(2):
                        await endpoint.send_json(
                            {
                                "type": "command_response",
                                "command": cmd.command,
                                "status": "ok",
                                "data": {"chunk": index},
                                "timestamp": cmd.timestamp,
                            }
                        )
                    response_payload = {"status": "ok", "data": {"done": True}}
                else:
                    response_payload = {
                        "status": "error",
                        "error": "transport stream test error",
                        "data": {"done": True},
                    }
                await endpoint.send_json(
                    {
                        "type": "command_response",
                        "command": cmd.command,
                        **response_payload,
                        "timestamp": cmd.timestamp,
                    }
                )
                continue
            if cmd.command == "test_all_sha_stream":
                try:
                    async for result in self.context.runtime.test_all_sha_stream(
                        cmd.payload.get("channel"),
                        cmd.payload.get("timeout"),
                    ):
                        await endpoint.send_json(
                            {
                                "type": "command_response",
                                "command": cmd.command,
                                "status": "ok",
                                "data": result,
                                "timestamp": cmd.timestamp,
                            }
                        )
                    response_payload = {"status": "ok", "data": {"done": True}}
                except Exception as inner_exc:  # noqa: BLE001 - returned to frontend
                    response_payload = {"status": "error", "error": str(inner_exc), "data": {"done": True}}
                await endpoint.send_json(
                    {
                        "type": "command_response",
                        "command": cmd.command,
                        **response_payload,
                        "timestamp": cmd.timestamp,
                    }
                )
                continue
            if cmd.command == "update_to_latest_stream":
                try:
                    async for result in self.context.runtime.update_to_latest_stream():
                        await endpoint.send_json(
                            {
                                "type": "command_response",
                                "command": cmd.command,
                                "status": "ok",
                                "data": result,
                                "timestamp": cmd.timestamp,
                            }
                        )
                    response_payload = {"status": "ok", "data": {"done": True}}
                except Exception as inner_exc:  # noqa: BLE001 - returned to frontend
                    response_payload = {"status": "error", "error": str(inner_exc), "data": {"done": True}}
                await endpoint.send_json(
                    {
                        "type": "command_response",
                        "command": cmd.command,
                        **response_payload,
                        "timestamp": cmd.timestamp,
                    }
                )
                continue
            try:
                response_payload = await execute_command(cmd, binary_payload=binary_payload)
            except Exception as inner_exc:  # noqa: BLE001 - returned to frontend
                response_payload = {"status": "error", "error": str(inner_exc)}
            binary_response = response_payload.pop("_binary", None)
            if binary_response is not None:
                data = response_payload.setdefault("data", {})
                data["binary"] = {
                    "size": len(binary_response),
                }
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
