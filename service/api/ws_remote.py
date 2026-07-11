from __future__ import annotations

from fastapi import APIRouter, WebSocket

from service.channels.remote import RemoteChannelHandler
from service.transport.websocket_endpoint import WebSocketChannelEndpoint

from .security import perform_business_resume
from .state import context

router = APIRouter()


@router.websocket("/ws/remote")
async def websocket_remote(websocket: WebSocket) -> None:
    try:
        _, stream = await perform_business_resume(websocket, channel="remote")
        await RemoteChannelHandler(context).handle(WebSocketChannelEndpoint(websocket, stream))
    except Exception:
        import traceback

        traceback.print_exc()
