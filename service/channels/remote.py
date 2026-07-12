from __future__ import annotations

import asyncio
import os
from typing import Any

from service.transport import ChannelClosed, ChannelEndpoint

REMOTE_CONNECT_TIMEOUT_SECONDS = float(os.getenv("BAAS_REMOTE_CONNECT_TIMEOUT_SECONDS", "20"))


class RemoteChannelHandler:
    def __init__(self, service_context: Any) -> None:
        self.context = service_context

    async def handle(self, endpoint: ChannelEndpoint) -> None:
        proxy = None
        try:
            message = await endpoint.recv_json()
            config_id = message.get("config_id")
            if os.environ.get("BAAS_SHM_TEST_COMMANDS") == "1" and config_id == "transport_remote_test":
                control_payload = await endpoint.recv_bytes()
                await endpoint.send_json({"type": "remote_ack", "size": len(control_payload)})
                frame_count = int(message.get("frame_count", 4))
                frame_size = int(message.get("frame_size", 0) or 0)
                for index in range(frame_count):
                    payload = f"remote-frame-{index:02d}".encode("ascii") * 8
                    if frame_size > 0:
                        seed = f"remote-frame-{index:08d}:".encode("ascii")
                        payload = (seed * ((frame_size // len(seed)) + 1))[:frame_size]
                    await endpoint.send_bytes(payload)
                await endpoint.close()
                return

            await endpoint.send_json({"type": "remote_status", "message": "Initializing remote connection..."})
            try:
                client = await asyncio.wait_for(
                    self.context.runtime.require_remote_(config_id),
                    timeout=REMOTE_CONNECT_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                await endpoint.send_json(
                    {
                        "type": "remote_error",
                        "error": (
                            "Remote connection timed out. Check that the emulator/device is running "
                            "and ADB is reachable."
                        ),
                    }
                )
                return
            except Exception as exc:  # noqa: BLE001 - surface device initialization errors to frontend
                await endpoint.send_json({"type": "remote_error", "error": str(exc)})
                return
            from service.remote import ScrcpyProxySession

            proxy = ScrcpyProxySession(client, None, encrypt_adb_to_ws=False)
            await proxy.run_endpoint(endpoint)
        except ChannelClosed:
            return
        finally:
            if proxy is not None:
                await proxy.close()
