from __future__ import annotations

import asyncio
from typing import Any

from .base import ChannelClosed

_CLOSE = object()


class InMemoryChannelEndpoint:
    """Queue-backed endpoint for transport-neutral channel tests."""

    def __init__(self) -> None:
        self._incoming: asyncio.Queue[tuple[str, Any] | object] = asyncio.Queue()
        self.sent: list[tuple[str, Any]] = []

    async def queue_json(self, payload: dict[str, Any]) -> None:
        await self._incoming.put(("json", payload))

    async def queue_bytes(self, payload: bytes) -> None:
        await self._incoming.put(("bytes", payload))

    async def queue_close(self) -> None:
        await self._incoming.put(_CLOSE)

    async def recv_json(self) -> dict[str, Any]:
        kind_and_payload = await self._incoming.get()
        if kind_and_payload is _CLOSE:
            raise ChannelClosed
        kind, payload = kind_and_payload
        if kind != "json":
            raise TypeError(f"expected json frame, got {kind}")
        return payload

    async def recv_bytes(self) -> bytes:
        kind_and_payload = await self._incoming.get()
        if kind_and_payload is _CLOSE:
            raise ChannelClosed
        kind, payload = kind_and_payload
        if kind != "bytes":
            raise TypeError(f"expected bytes frame, got {kind}")
        return payload

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(("json", payload))

    async def send_bytes(self, payload: bytes) -> None:
        self.sent.append(("bytes", payload))

    async def close(self) -> None:
        await self.queue_close()
