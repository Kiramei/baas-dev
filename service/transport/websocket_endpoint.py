from __future__ import annotations

import json
from contextlib import suppress
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from service.auth import SecretStreamBox

from .base import ChannelClosed


def json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class WebSocketChannelEndpoint:
    """Business endpoint adapter for an authenticated/encrypted WebSocket."""

    def __init__(self, websocket: WebSocket, stream: SecretStreamBox) -> None:
        self.websocket = websocket
        self.stream = stream

    async def recv_json(self) -> dict[str, Any]:
        try:
            frame = await self.websocket.receive_bytes()
        except WebSocketDisconnect as exc:
            raise ChannelClosed from exc
        plaintext = self.stream.decrypt(frame)
        return json.loads(plaintext.decode("utf-8"))

    async def recv_bytes(self) -> bytes:
        try:
            frame = await self.websocket.receive_bytes()
        except WebSocketDisconnect as exc:
            raise ChannelClosed from exc
        return self.stream.decrypt(frame)

    async def send_json(self, payload: dict[str, Any]) -> None:
        await self.websocket.send_bytes(self.stream.encrypt(json_bytes(payload)))

    async def send_bytes(self, payload: bytes) -> None:
        await self.websocket.send_bytes(self.stream.encrypt(payload))

    async def close(self) -> None:
        with suppress(RuntimeError):
            await self.websocket.close()
