from __future__ import annotations

import asyncio
from contextlib import suppress

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from service.auth import AuthenticationError, SecretStreamBox
from service.channels.sync import SyncChannelHandler, sync_sender as channel_sync_sender
from service.transport.websocket_endpoint import WebSocketChannelEndpoint

from .security import perform_business_resume
from .state import context

router = APIRouter()


async def sync_sender(websocket: WebSocket, stream: SecretStreamBox, queue: asyncio.Queue) -> None:
    await channel_sync_sender(WebSocketChannelEndpoint(websocket, stream), queue)


@router.websocket("/ws/sync")
async def websocket_sync(websocket: WebSocket) -> None:
    try:
        _, stream = await perform_business_resume(websocket, channel="sync")
        await SyncChannelHandler(context).handle(WebSocketChannelEndpoint(websocket, stream))
    except (AuthenticationError, HTTPException) as exc:
        import traceback

        traceback.print_exc()
        with suppress(RuntimeError):
            await websocket.close(code=4401, reason=str(exc))
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 - surfaced to caller
        import traceback

        traceback.print_exc()
        with suppress(RuntimeError):
            await websocket.close(code=1011, reason=str(exc))
