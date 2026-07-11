from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from service.auth import AuthenticationError, SecretStreamBox
from service.channels.provider import ProviderChannelHandler, provider_sender as channel_provider_sender
from service.transport.websocket_endpoint import WebSocketChannelEndpoint

from .security import perform_business_resume
from .state import context

router = APIRouter()


async def provider_sender(
    websocket: WebSocket,
    stream: SecretStreamBox,
    queue: asyncio.Queue,
    envelope_type: str,
    send_lock: Optional[asyncio.Lock] = None,
) -> None:
    await channel_provider_sender(WebSocketChannelEndpoint(websocket, stream), queue, envelope_type, send_lock)


@router.websocket("/ws/provider")
async def websocket_provider(websocket: WebSocket) -> None:
    try:
        _, stream = await perform_business_resume(websocket, channel="provider")
        await ProviderChannelHandler(context).handle(WebSocketChannelEndpoint(websocket, stream))
    except (AuthenticationError, HTTPException) as exc:
        with suppress(RuntimeError):
            await websocket.close(code=4401, reason=str(exc))
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 - surfaced to caller
        with suppress(RuntimeError):
            await websocket.close(code=1011, reason=str(exc))
