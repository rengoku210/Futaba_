"""
Futaba IPC — Inter-Process Communication between components.

The Futaba architecture has two main processes:
1. Python backend (agent, tools, Hermes runtime)
2. Native Windows frontend (HUD, tray, settings, dashboard)

They communicate via a local WebSocket server (JSON-RPC protocol).

Architecture:
    Native UI ←→ WebSocket ←→ FutabaServer ←→ Agent Controller
                     ↑
                  localhost:45850

Messages use a simple JSON-RPC-like protocol:
    Request:  {"id": "...", "method": "...", "params": {...}}
    Response: {"id": "...", "result": {...}} or {"id": "...", "error": {...}}
    Event:    {"event": "...", "data": {...}}  (server-push)

This enables:
- The HUD to show task status
- Settings to modify configuration
- The tray to control the runtime
- Voice commands to reach the agent
- Dashboard to show task details

The WebSocket server also serves as the API for any external integrations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
import secrets
from pathlib import Path
from typing import Any, Callable, Awaitable

logger = logging.getLogger("futaba.ipc")

# Default port for the IPC server
DEFAULT_PORT = 45850


# ---------------------------------------------------------------------------
# Message Types
# ---------------------------------------------------------------------------

class IPCMessage:
    """Base IPC message."""

    @staticmethod
    def request(method: str, params: dict | None = None, msg_id: str = "") -> str:
        return json.dumps({
            "id": msg_id or str(uuid.uuid4())[:8],
            "method": method,
            "params": params or {},
        })

    @staticmethod
    def response(msg_id: str, result: Any = None, error: str = "") -> str:
        if error:
            return json.dumps({"id": msg_id, "error": {"message": error}})
        return json.dumps({"id": msg_id, "result": result})

    @staticmethod
    def event(event_name: str, data: Any = None) -> str:
        return json.dumps({"event": event_name, "data": data or {}})


# ---------------------------------------------------------------------------
# IPC Server
# ---------------------------------------------------------------------------

class IPCServer:
    """
    WebSocket-based IPC server.

    Handles communication between the Futaba Python backend and any
    connected clients (native UI, external tools, etc.).

    Methods are registered as handlers:
        server.register("submit_task", handle_submit_task)

    Events are broadcast to all connected clients:
        await server.broadcast_event("task_status", {"task_id": "...", "status": "running"})
    """

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT):
        self._host = host
        self._port = port
        self._handlers: dict[str, Callable[..., Awaitable[Any]]] = {}
        self._clients: set[Any] = set()
        self._authenticated: set[Any] = set()
        self._auth_token: str = secrets.token_hex(32)
        self._server: Any = None
        self._running = False
        
        self.register("auth", self._handle_auth)

    @property
    def auth_token(self) -> str:
        """Return the active authentication token."""
        return self._auth_token

    def register(self, method: str, handler: Callable[..., Awaitable[Any]]) -> None:
        """Register a method handler."""
        self._handlers[method] = handler
        logger.debug("Registered IPC method: %s", method)

    def _write_token_file(self) -> None:
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if not local_app_data:
            logger.warning("LOCALAPPDATA not set, cannot write IPC token file")
            return
        token_dir = Path(local_app_data) / "Futaba" / "runtime"
        token_dir.mkdir(parents=True, exist_ok=True)
        token_file = token_dir / "ipc.token"
        token_file.write_text(self._auth_token, encoding="utf-8")
        logger.debug("Wrote IPC token to %s", token_file)

    async def start(self) -> None:
        """Start the WebSocket server."""
        try:
            self._write_token_file()
            import websockets
            self._server = await websockets.serve(
                self._handle_client,
                self._host,
                self._port,
                ping_interval=30,
                ping_timeout=10,
                max_size=1_048_576,
            )
            self._running = True
            logger.info("IPC server started on ws://%s:%d", self._host, self._port)
        except Exception as e:
            logger.error("Failed to start IPC server: %s", e)
            raise

    async def stop(self) -> None:
        """Stop the WebSocket server."""
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("IPC server stopped")
            
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            token_file = Path(local_app_data) / "Futaba" / "runtime" / "ipc.token"
            if token_file.exists():
                try:
                    token_file.unlink()
                except Exception as e:
                    logger.warning("Failed to delete token file: %s", e)

    async def broadcast_event(self, event_name: str, data: Any = None) -> None:
        """Broadcast an event to all connected clients."""
        if not self._authenticated:
            return

        message = IPCMessage.event(event_name, data)
        disconnected = set()

        for client in self._authenticated:
            try:
                await client.send(message)
            except Exception:
                disconnected.add(client)

        self._authenticated -= disconnected
        self._clients -= disconnected

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def _handle_auth(self, token: str = "") -> dict:
        """Handle authentication request."""
        if token == self._auth_token:
            return {"status": "authenticated"}
        raise ValueError("Invalid token")

    async def _handle_client(self, websocket: Any, path: str = "") -> None:
        """Handle a WebSocket client connection."""
        self._clients.add(websocket)
        client_id = str(uuid.uuid4())[:8]
        logger.info("IPC client connected: %s", client_id)
        authenticated = False

        try:
            async for raw_message in websocket:
                try:
                    msg = json.loads(raw_message)
                    
                    if not authenticated:
                        if msg.get("method") == "auth":
                            token = msg.get("params", {}).get("token")
                            if token == self._auth_token:
                                authenticated = True
                                self._authenticated.add(websocket)
                                # Let it fall through to _dispatch so _handle_auth runs
                            else:
                                msg_id = msg.get("id", "")
                                await websocket.send(IPCMessage.response(msg_id, error="Invalid token"))
                                break
                        else:
                            msg_id = msg.get("id", "") if isinstance(msg, dict) else ""
                            await websocket.send(IPCMessage.response(msg_id, error="Not authenticated"))
                            break

                    response = await self._dispatch(msg)
                    if response:
                        await websocket.send(response)
                except json.JSONDecodeError:
                    await websocket.send(
                        IPCMessage.response("", error="Invalid JSON")
                    )
                except Exception as e:
                    msg_id = msg.get("id", "") if isinstance(msg, dict) else ""
                    await websocket.send(
                        IPCMessage.response(msg_id, error=str(e))
                    )
        except Exception:
            pass
        finally:
            self._clients.discard(websocket)
            self._authenticated.discard(websocket)
            logger.info("IPC client disconnected: %s", client_id)

    async def _dispatch(self, msg: dict) -> str | None:
        """Dispatch a message to the appropriate handler."""
        msg_id = msg.get("id", "")
        method = msg.get("method", "")
        params = msg.get("params", {})

        if not method:
            return IPCMessage.response(msg_id, error="No method specified")

        handler = self._handlers.get(method)
        if not handler:
            return IPCMessage.response(
                msg_id, error=f"Unknown method: {method}"
            )

        try:
            result = await handler(**params)
            return IPCMessage.response(msg_id, result=result)
        except TypeError as e:
            return IPCMessage.response(msg_id, error=f"Invalid parameters: {e}")
        except Exception as e:
            logger.error("Handler error for %s: %s", method, e)
            return IPCMessage.response(msg_id, error=str(e))


# ---------------------------------------------------------------------------
# IPC Method Registry (Standard Futaba API)
# ---------------------------------------------------------------------------

class FutabaAPI:
    """
    Defines the standard IPC API that the native UI uses.

    Methods:
    - submit_task: Submit a new user request
    - get_task: Get task details
    - get_active_tasks: List active tasks
    - cancel_task: Cancel a task
    - provide_input: Provide user input for a blocker
    - get_config: Get current configuration
    - update_config: Update configuration
    - get_status: Get overall Futaba status
    - health_check: Run diagnostics
    - get_resource_status: Get resource/performance info
    - get_memory: Query memory
    """

    def __init__(self, server: IPCServer):
        self._server = server
        # Agent controller and other components are injected later
        self._controller = None
        self._config_manager = None
        self._resource_manager = None
        self._memory_manager = None
        self._hermes_bridge = None
        self._video_pipeline = None
        self._voice_pipeline = None
        self._credential_store = None
        self._model_router = None

    def inject(
        self,
        controller: Any = None,
        resource_manager: Any = None,
        memory_manager: Any = None,
        hermes_bridge: Any = None,
        video_pipeline: Any = None,
        voice_pipeline: Any = None,
        credential_store: Any = None,
        model_router: Any = None,
    ) -> None:
        """Inject dependencies after initialization."""
        self._controller = controller
        self._resource_manager = resource_manager
        self._memory_manager = memory_manager
        self._hermes_bridge = hermes_bridge
        self._video_pipeline = video_pipeline
        self._voice_pipeline = voice_pipeline
        self._credential_store = credential_store
        self._model_router = model_router

    def register_all(self) -> None:
        """Register all API methods with the IPC server."""
        self._server.register("submit_task", self.submit_task)
        self._server.register("get_task", self.get_task)
        self._server.register("get_active_tasks", self.get_active_tasks)
        self._server.register("get_all_tasks", self.get_all_tasks)
        self._server.register("cancel_task", self.cancel_task)
        self._server.register("provide_input", self.provide_input)
        self._server.register("get_config", self.get_config)
        self._server.register("update_config", self.update_config)
        self._server.register("get_status", self.get_status)
        self._server.register("health_check", self.health_check)
        self._server.register("get_diagnostics", self.get_diagnostics)
        self._server.register("get_resource_status", self.get_resource_status)
        self._server.register("get_memory", self.get_memory)
        self._server.register("process_video", self.process_video)
        self._server.register("speak", self.speak)
        self._server.register("get_hermes_status", self.get_hermes_status)
        self._server.register("get_providers", self.get_providers)
        self._server.register("test_provider", self.test_provider)
        self._server.register("set_credential", self.set_credential)
        self._server.register("pause", self.pause)
        self._server.register("resume", self.resume)
        self._server.register("set_dnd", self.set_dnd)
        self._server.register("set_gaming_mode", self.set_gaming_mode)
        logger.info("Registered %d IPC methods", 23)

    # --- Task methods ---

    async def submit_task(self, request: str = "", title: str = "") -> dict:
        if not self._controller:
            return {"error": "Agent not initialized"}
        task = await self._controller.submit(request, title)
        return task.to_dict()

    async def get_task(self, task_id: str = "") -> dict | None:
        if not self._controller:
            return None
        task = self._controller._task_manager.get_task(task_id)
        return task.to_dict() if task else None

    async def get_active_tasks(self) -> list[dict]:
        if not self._controller:
            return []
        tasks = self._controller._task_manager.get_active_tasks()
        return [t.to_dict() for t in tasks]

    async def get_all_tasks(self) -> list[dict]:
        if not self._controller:
            return []
        tasks = self._controller._task_manager.get_all_tasks()
        return [t.to_dict() for t in tasks]

    async def cancel_task(self, task_id: str = "") -> dict:
        if not self._controller:
            return {"error": "Agent not initialized"}
        task = await self._controller._task_manager.cancel_task(task_id)
        return task.to_dict()

    async def provide_input(self, task_id: str = "", input: str = "") -> dict:
        if not self._controller:
            return {"error": "Agent not initialized"}
        await self._controller.provide_input(task_id, input)
        return {"status": "ok"}

    # --- Config methods ---

    async def get_config(self) -> dict:
        from futaba.core.config import get_config
        return get_config().model_dump(mode="json")

    async def update_config(self, updates: dict = {}) -> dict:
        from futaba.core.config import update_config
        cfg = update_config(updates)
        return cfg.model_dump(mode="json")

    # --- Status methods ---

    async def get_status(self) -> dict:
        status = {
            "version": "0.1.0",
            "running": True,
            "active_tasks": 0,
            "queued_tasks": 0,
        }
        if self._controller:
            status["active_tasks"] = len(self._controller._task_manager.get_active_tasks())
            status["queued_tasks"] = self._controller._task_manager._queue.queued_count
        if self._resource_manager:
            status["resources"] = self._resource_manager.snapshot.to_dict()
            status["gaming_mode"] = self._resource_manager.is_gaming
            status["dnd_mode"] = self._resource_manager.is_dnd
        return status

    async def health_check(self) -> dict:
        results = {}
        if self._controller:
            results["tools"] = await self._controller._tools.health_check_all()
        if self._resource_manager:
            results["resources"] = self._resource_manager.snapshot.to_dict()
        return results

    async def get_resource_status(self) -> dict:
        if self._resource_manager:
            return self._resource_manager.snapshot.to_dict()
        return {}

    async def get_memory(self, query: str = "", category: str = "") -> list[dict]:
        if self._memory_manager:
            entries = self._memory_manager.store.query(
                category=category, search=query, limit=50
            )
            return [e.to_dict() for e in entries]
        return []

    # --- Control methods ---

    async def pause(self) -> dict:
        if self._controller:
            self._controller._task_manager._queue.pause()
        return {"status": "paused"}

    async def resume(self) -> dict:
        if self._controller:
            self._controller._task_manager._queue.resume()
        return {"status": "resumed"}

    async def set_dnd(self, enabled: bool = True) -> dict:
        if self._resource_manager:
            self._resource_manager.set_dnd(enabled)
        return {"dnd": enabled}

    async def set_gaming_mode(self, enabled: bool = True) -> dict:
        if self._resource_manager:
            self._resource_manager.force_gaming_mode(enabled)
        return {"gaming_mode": enabled}

    # --- Extended Subsystem Methods ---

    async def get_diagnostics(self) -> dict:
        """Run complete Futaba Doctor diagnostics across all subsystems."""
        diag: dict[str, Any] = {
            "futaba_version": "0.1.0",
            "timestamp": time.time(),
            "os": os.name,
            "python": sys.version,
            "status": "healthy",
            "checks": {},
        }

        # 1. Hermes check
        hermes_status = "UNAVAILABLE"
        if self._hermes_bridge:
            try:
                h_check = await self._hermes_bridge.health_check()
                diag["checks"]["hermes"] = h_check
                if h_check.get("status") in ("ready", "running", "healthy"):
                    hermes_status = "CONNECTED"
            except Exception as e:
                diag["checks"]["hermes"] = {"status": "error", "message": str(e)}
        else:
            diag["checks"]["hermes"] = {"status": "uninitialized"}

        # 2. CUA / Computer Use check
        cua_status = "NOT READY"
        if self._controller and self._controller._tools:
            try:
                tools_res = await self._controller._tools.health_check_all()
                diag["checks"]["tools"] = tools_res
                if tools_res.get("computer_use"):
                    cua_status = "READY"
            except Exception as e:
                diag["checks"]["tools"] = {"status": "error", "message": str(e)}

        # 3. Resources / GPU check
        if self._resource_manager:
            diag["checks"]["resources"] = self._resource_manager.snapshot.to_dict()

        # 4. Model Router / Providers check
        routing_status = "DEGRADED"
        if self._model_router:
            diag["checks"]["router"] = self._model_router.stats
            healthy_count = len(self._model_router.stats.get("healthy_providers", []))
            if healthy_count > 0:
                routing_status = "READY"

        # 5. Video Pipeline check
        if self._video_pipeline:
            diag["checks"]["video_pipeline"] = {
                "available": self._video_pipeline.is_available,
            }

        # 6. Voice Pipeline & Audio Hardware check
        mic_status = "FAILED"
        speaker_status = "FAILED"
        gemini_live_status = "DISCONNECTED"

        try:
            import sounddevice as sd
            devs = sd.query_devices()
            if any(d["max_input_channels"] > 0 for d in devs):
                mic_status = "ACTIVE"
            if any(d["max_output_channels"] > 0 for d in devs):
                speaker_status = "ACTIVE"
        except Exception:
            pass

        if self._voice_pipeline:
            gemini_key_ok = bool(self._credential_store.get("provider:gemini")) if self._credential_store else bool(os.environ.get("GEMINI_API_KEY"))
            diag["checks"]["voice_pipeline"] = {
                "running": self._voice_pipeline.is_running,
                "listening": self._voice_pipeline.is_listening,
                "active_provider": getattr(self._voice_pipeline, "_active_provider_name", "unknown"),
                "gemini_key_configured": gemini_key_ok,
            }
            if self._voice_pipeline.is_running:
                gemini_live_status = "CONNECTED"

        # 7. Browser check
        browser_status = "UNAVAILABLE"
        try:
            from futaba.system.context_tracker import get_context_tracker
            tracker = get_context_tracker()
            if tracker.get_running_browsers():
                browser_status = "AVAILABLE"
            else:
                # Check if executable installed
                for b in ("brave", "chrome", "msedge"):
                    is_r, _ = tracker.is_app_running(b)
                    if is_r:
                        browser_status = "AVAILABLE"
                        break
        except Exception:
            pass

        # 8. Qt UI check
        qt_status = "HEALTHY"
        try:
            from PySide6.QtWidgets import QApplication
            if QApplication.instance() is None:
                qt_status = "NOT_RUNNING"
        except Exception:
            qt_status = "UNAVAILABLE"

        # 9. Memory DB check
        if self._memory_manager:
            diag["checks"]["memory"] = self._memory_manager.store.stats()

        # Structured Doctor summary mapping
        diag["doctor_summary"] = {
            "gemini_live": gemini_live_status,
            "hermes": hermes_status,
            "cua": cua_status,
            "microphone": mic_status,
            "audio_output": speaker_status,
            "model_routing": routing_status,
            "browser": browser_status,
            "qt": qt_status,
        }

        return diag

    async def process_video(
        self,
        video_source: str = "",
        interval: float = 5.0,
        max_frames: int = 50,
    ) -> dict:
        """Process an instructional video and generate an executable plan."""
        if not self._video_pipeline:
            return {"error": "Video pipeline not initialized"}
        if not video_source:
            return {"error": "No video source provided"}

        plan = await self._video_pipeline.process(
            video_source=video_source,
            analyze_every_n_seconds=interval,
            max_frames=max_frames,
        )
        return plan.to_dict()

    async def speak(self, text: str = "") -> dict:
        """Speak text through TTS."""
        if not self._voice_pipeline:
            return {"error": "Voice pipeline not initialized"}
        if not text:
            return {"error": "No text provided"}
        await self._voice_pipeline.respond(text)
        return {"status": "ok", "text": text}

    async def get_hermes_status(self) -> dict:
        """Get Hermes runtime health and capabilities."""
        if not self._hermes_bridge:
            return {"hermes_available": False, "status": "uninitialized"}
        return await self._hermes_bridge.health_check()

    async def get_providers(self) -> list[dict]:
        """Get list of configured providers and their health."""
        if not self._model_router:
            return []
        clients = self._model_router._registry.all()
        return [
            {
                "name": c.name,
                "kind": c.config.kind,
                "base_url": c.config.base_url,
                "default_model": c.config.default_model,
                "healthy": c.is_healthy,
                "enabled": c.config.enabled,
            }
            for c in clients
        ]

    async def test_provider(self, provider_name: str = "") -> dict:
        """Test connectivity for a specific provider."""
        if not self._model_router:
            return {"success": False, "error": "Model router not initialized"}
        client = self._model_router._registry.get(provider_name)
        if not client:
            return {"success": False, "error": f"Provider not found: {provider_name}"}
        try:
            ok = await client.health_check()
            return {"success": ok, "error": "" if ok else "Health check failed"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def set_credential(self, name: str = "", value: str = "") -> dict:
        """Securely store an API credential in Windows Credential Manager."""
        if not self._credential_store:
            return {"success": False, "error": "Credential store not initialized"}
        if not name:
            return {"success": False, "error": "No credential name provided"}
        ok = self._credential_store.store(name, value)
        return {"success": ok}

