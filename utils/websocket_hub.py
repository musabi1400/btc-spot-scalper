"""
utils/websocket_hub.py
======================
WebSocketHub — manages WebSocket connections and broadcasts to dashboard clients.
"""
from __future__ import annotations

import logging

from fastapi import WebSocket

logger = logging.getLogger("utils.ws_hub")


class WebSocketHub:
    """Manage WebSocket connections and broadcast updates."""

    def __init__(self):
        self._clients: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.append(ws)
        logger.info("WS connected — total: %d", len(self._clients))

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._clients:
            self._clients.remove(ws)
        logger.info("WS disconnected — total: %d", len(self._clients))

    async def broadcast(self, message: dict) -> None:
        dead = []
        for ws in self._clients:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    @property
    def client_count(self) -> int:
        return len(self._clients)