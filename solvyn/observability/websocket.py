import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}

    def _key(self, tenant_id: str, session_id: str | None) -> str:
        return f"{tenant_id}:{session_id or '*'}" if session_id else tenant_id

    async def connect(self, websocket: WebSocket, tenant_id: str, session_id: str | None = None) -> None:
        await websocket.accept()
        key = self._key(tenant_id, session_id)
        if key not in self._connections:
            self._connections[key] = set()
        self._connections[key].add(websocket)
        logger.debug(f"WebSocket connected: {key}")

    def disconnect(self, websocket: WebSocket, tenant_id: str, session_id: str | None = None) -> None:
        key = self._key(tenant_id, session_id)
        if key in self._connections:
            self._connections[key].discard(websocket)
            if not self._connections[key]:
                del self._connections[key]

    async def broadcast(self, message: dict[str, Any], tenant_id: str, session_id: str | None = None) -> None:
        key = self._key(tenant_id, session_id)
        payload = json.dumps(message)
        dead: list[WebSocket] = []

        connections = self._connections.get(key, set())
        session_wild = self._connections.get(self._key(tenant_id, None), set())
        all_connections = connections | session_wild

        for ws in all_connections:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)

        for ws in dead:
            connections.discard(ws)
            session_wild.discard(ws)


class WebSocketManager:
    def __init__(self) -> None:
        self.connections = ConnectionManager()

    async def handle_graph_update(
        self,
        graph_id: str,
        node_id: str,
        agent_type: str,
        status: str,
        session_id: str,
        tenant_id: str = "default",
        error: str | None = None,
    ) -> None:
        await self.connections.broadcast(
            {
                "type": "graph_update",
                "graph_id": graph_id,
                "node_id": node_id,
                "agent_type": agent_type,
                "status": status,
                "error": error,
                "timestamp": __import__("datetime").datetime.now().isoformat(),
            },
            tenant_id=tenant_id,
            session_id=session_id,
        )

    async def handle_agent_message(
        self,
        from_agent: str,
        to_agent: str | None,
        message_type: str,
        content: dict[str, Any],
        session_id: str,
        tenant_id: str = "default",
    ) -> None:
        await self.connections.broadcast(
            {
                "type": "agent_message",
                "from_agent": from_agent,
                "to_agent": to_agent,
                "message_type": message_type,
                "content_preview": str(content)[:200],
                "timestamp": __import__("datetime").datetime.now().isoformat(),
            },
            tenant_id=tenant_id,
            session_id=session_id,
        )

    async def handle_workflow_update(
        self,
        workflow_name: str,
        step: str,
        status: str,
        session_id: str,
        tenant_id: str = "default",
    ) -> None:
        await self.connections.broadcast(
            {
                "type": "workflow_update",
                "workflow_name": workflow_name,
                "step": step,
                "status": status,
                "timestamp": __import__("datetime").datetime.now().isoformat(),
            },
            tenant_id=tenant_id,
            session_id=session_id,
        )


ws_manager = WebSocketManager()
