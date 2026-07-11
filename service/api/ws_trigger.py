from __future__ import annotations

from contextlib import suppress

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from service.auth import AuthenticationError
from service.channels.trigger import TriggerChannelHandler
from service.transport.websocket_endpoint import WebSocketChannelEndpoint

from .security import perform_business_resume
from .state import context

router = APIRouter()


@router.websocket("/ws/trigger")
async def websocket_trigger(websocket: WebSocket) -> None:
    try:
        _, stream = await perform_business_resume(websocket, channel="trigger")
        await TriggerChannelHandler(context).handle(WebSocketChannelEndpoint(websocket, stream))
    except (AuthenticationError, HTTPException) as exc:
        with suppress(RuntimeError):
            await websocket.close(code=4401, reason=str(exc))
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 - surfaced to caller
        with suppress(RuntimeError):
            await websocket.close(code=1011, reason=str(exc))
