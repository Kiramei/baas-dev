from __future__ import annotations

from typing import Any, Protocol


class ChannelClosed(Exception):
    """Raised when the underlying transport has closed."""


class ChannelEndpoint(Protocol):
    """Transport-neutral async endpoint used by business channel handlers."""

    async def recv_json(self) -> dict[str, Any]: ...

    async def recv_bytes(self) -> bytes: ...

    async def send_json(self, payload: dict[str, Any]) -> None: ...

    async def send_bytes(self, payload: bytes) -> None: ...

    async def close(self) -> None: ...
