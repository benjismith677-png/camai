"""WebSocket server for real-time alert streaming."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

WS_PORT = 5556


class AlertWSServer:
    """WebSocket server that pushes alerts to connected clients.

    Runs in a separate thread with its own asyncio event loop.
    AlertDispatcher calls push_alert() from any thread.
    """

    def __init__(self, port: int = WS_PORT):
        self.port = port
        self._clients: set[Any] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def start(self) -> None:
        """Start WebSocket server in background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def push_alert(self, alert_dict: dict) -> None:
        """Push alert to all connected clients (thread-safe)."""
        if not self._loop or not self._clients:
            return
        message = json.dumps({"type": "alert", "data": alert_dict})
        asyncio.run_coroutine_threadsafe(
            self._broadcast(message),
            self._loop,
        )

    def push_status(self, status_dict: dict) -> None:
        """Push status update to all clients."""
        if not self._loop or not self._clients:
            return
        message = json.dumps({"type": "status", "data": status_dict})
        asyncio.run_coroutine_threadsafe(
            self._broadcast(message),
            self._loop,
        )

    async def _broadcast(self, message: str) -> None:
        dead: set[Any] = set()
        for ws in list(self._clients):
            try:
                await ws.send(message)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def _handler(self, websocket: Any) -> None:
        """Handle a single WebSocket connection."""
        self._clients.add(websocket)
        remote = websocket.remote_address
        print(f"[WS] Client connected: {remote} (total: {len(self._clients)})")

        try:
            # Send welcome message
            await websocket.send(json.dumps({
                "type": "connected",
                "data": {"message": "Sentinel alert stream", "clients": len(self._clients)},
            }))

            # Keep connection alive, listen for pings/messages
            async for msg in websocket:
                try:
                    data = json.loads(msg)
                    if data.get("type") == "ping":
                        await websocket.send(json.dumps({"type": "pong"}))
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass
        finally:
            self._clients.discard(websocket)
            print(f"[WS] Client disconnected: {remote} (total: {len(self._clients)})")

    def _run_loop(self) -> None:
        """Run asyncio event loop in background thread."""
        import websockets  # type: ignore[import-untyped]

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def serve() -> None:
            async with websockets.serve(self._handler, "127.0.0.1", self.port):
                print(f"[WS] Alert WebSocket server started on ws://127.0.0.1:{self.port}")
                while self._running:
                    await asyncio.sleep(1)

        try:
            self._loop.run_until_complete(serve())
        except Exception as e:
            if self._running:
                print(f"[WS] Server error: {e}")
        finally:
            self._loop.close()
            self._loop = None
