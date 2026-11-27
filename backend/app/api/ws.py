"""WebSocket endpoint for real-time job state, download speed/progress, and logs."""

from __future__ import annotations

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .deps import AppContainer

router = APIRouter(tags=["ws"])


@router.websocket("/api/ws")
async def ws_events(websocket: WebSocket):
    container: AppContainer = websocket.app.state.container
    if not container.auth.verify_websocket_origin(websocket, container.env.parsed_allowed_origins()):
        await websocket.close(code=4403, reason="Forbidden origin")
        return
    if not container.auth.verify_websocket(websocket):
        # Accept before closing so browsers receive close code 4401 instead of 1006.
        await websocket.accept()
        await websocket.close(code=4401, reason="Unauthorized")
        return
    session_invalidated = container.auth.session_invalidated

    await websocket.accept()
    queue = container.events.subscribe()

    try:
        # Send initial snapshot so client renders immediately
        with anyio.fail_after(15):
            await websocket.send_json({
                "type": "snapshot",
                "data": {
                    "logs": container.events.recent_logs(200),
                    "live_progress": container.events.live_progress_snapshot(),
                    "scheduler_status": container.events.scheduler_status_snapshot(),
                    "scan_status": container.events.scan_status_snapshot(),
                },
            })

        async with anyio.create_task_group() as tg:
            async def _sender() -> None:
                try:
                    while container.auth.verify_websocket(websocket):
                        event = None
                        with anyio.move_on_after(25.0):
                            event = await queue.get()
                            queue.task_done()
                        if not container.auth.verify_websocket(websocket):
                            break
                        with anyio.fail_after(15):
                            await websocket.send_json(event if event is not None else {"type": "ping"})
                except (WebSocketDisconnect, RuntimeError, TimeoutError):
                    pass
                finally:
                    tg.cancel_scope.cancel()

            async def _receiver() -> None:
                try:
                    while True:
                        await websocket.receive_text()
                except (WebSocketDisconnect, RuntimeError):
                    pass
                finally:
                    tg.cancel_scope.cancel()

            async def _session_guard() -> None:
                await session_invalidated.wait()
                tg.cancel_scope.cancel()

            tg.start_soon(_sender)
            tg.start_soon(_receiver)
            tg.start_soon(_session_guard)
        if not container.auth.verify_websocket(websocket):
            await websocket.close(code=4401, reason="Session invalidated")
    except (WebSocketDisconnect, TimeoutError):
        pass
    finally:
        container.events.unsubscribe(queue)
