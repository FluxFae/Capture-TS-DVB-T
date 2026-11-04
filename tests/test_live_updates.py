"""Live update tests — B1.

The dashboard's "Live Updates" claim depends on the server broadcasting log and
state messages to connected WebSocket clients. ``sync_log_callback`` and
``sync_state_callback`` are the only bridge between the recording thread and
the WebSocket fan-out, so they are exercised here through a real in-process
HTTP server and a real WebSocket client.
"""

import asyncio
import json
import socket
import unittest

import uvicorn
import websockets

import app.main as app_main


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LiveUpdateTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.port = free_port()
        config = uvicorn.Config(
            app_main.app,
            host="127.0.0.1",
            port=self.port,
            log_level="error",
            lifespan="on",
        )
        self.server = uvicorn.Server(config)
        self.server_task = asyncio.create_task(self.server.serve())
        await self.wait_until_serving()
        self.client = await websockets.connect(f"ws://127.0.0.1:{self.port}/ws")
        # The first frame is the initial state broadcast on connect.
        await asyncio.wait_for(self.client.recv(), timeout=5)

    async def asyncTearDown(self):
        await self.client.close()
        self.server.should_exit = True
        try:
            await asyncio.wait_for(self.server_task, timeout=10)
        except asyncio.TimeoutError:  # pragma: no cover - shutdown safety
            self.server_task.cancel()

    async def wait_until_serving(self):
        for _ in range(100):
            if self.server.started:
                return
            await asyncio.sleep(0.05)
        self.fail("uvicorn server did not start")

    async def receive_json(self, timeout=5):
        raw = await asyncio.wait_for(self.client.recv(), timeout=timeout)
        return json.loads(raw)

    async def test_thread_log_reaches_client(self):
        """A log emitted from the recording thread must reach the browser."""
        await asyncio.to_thread(app_main.sync_log_callback, "PROBE_LOG_LINE")
        message = await self.receive_json()
        self.assertEqual(message["type"], "log")
        self.assertIn("PROBE_LOG_LINE", message["message"])

    async def test_thread_state_reaches_client(self):
        """A state update emitted from the recording thread must reach the browser."""
        payload = {
            "state": "recording",
            "file_size": 2048,
            "file_size_display": "2.0 KB",
            "duration": 1.5,
            "duration_display": "00:01",
        }
        await asyncio.to_thread(app_main.sync_state_callback, payload)
        message = await self.receive_json()
        self.assertEqual(message["type"], "state")
        self.assertEqual(message["state"], "recording")
        self.assertEqual(message["file_size"], 2048)

    async def test_direct_broadcast_reaches_client(self):
        """broadcast_log must not fail when called from the server event loop."""
        loop = app_main._main_loop
        self.assertIsNotNone(loop, "lifespan did not record the running loop")
        assert loop is not None
        future = asyncio.run_coroutine_threadsafe(
            app_main.broadcast_log("DIRECT_LOG"), loop
        )
        await asyncio.get_running_loop().run_in_executor(None, future.result, 5)
        message = await self.receive_json()
        self.assertIn("DIRECT_LOG", message["message"])

    async def test_disconnected_client_does_not_break_broadcast(self):
        """A dead client must be dropped without killing the fan-out."""
        dead = await websockets.connect(f"ws://127.0.0.1:{self.port}/ws")
        await asyncio.wait_for(dead.recv(), timeout=5)
        await dead.close()
        await asyncio.sleep(0.2)

        await asyncio.to_thread(app_main.sync_log_callback, "AFTER_DEAD_CLIENT")
        message = await self.receive_json()
        self.assertIn("AFTER_DEAD_CLIENT", message["message"])


if __name__ == "__main__":
    unittest.main()
