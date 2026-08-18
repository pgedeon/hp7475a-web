"""WebSocket status hub: broadcasts job/device events to connected clients."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WSHub:
    """Fan-out broadcaster. publish() is sync (called from worker thread);
    each client gets its own asyncio-safe send via its loop."""

    def __init__(self):
        self._clients: dict[int, tuple[WebSocket, asyncio.AbstractEventLoop]] = {}
        self._next_id = 0

    async def connect(self, ws: WebSocket) -> int:
        """Accept and register; returns the client id for disconnect()."""
        await ws.accept()
        cid = self._next_id
        self._clients[cid] = (ws, asyncio.get_running_loop())
        self._next_id += 1
        return cid

    def disconnect(self, client_id: int) -> None:
        self._clients.pop(client_id, None)

    def publish(self, event: dict[str, Any]) -> None:
        """Safely callable from any thread; drops unsendable clients."""
        import json

        payload = json.dumps({"type": event.get("type", "update"), **event})
        dead: list[int] = []
        for cid, (ws, loop) in list(self._clients.items()):
            try:
                asyncio.run_coroutine_threadsafe(ws.send_text(payload), loop).result(2.0)
            except Exception:
                dead.append(cid)
        for cid in dead:
            self.disconnect(cid)

    @property
    def client_count(self) -> int:
        return len(self._clients)
