"""
Futaba Application — Main orchestrator and entry point.

This module ties all Futaba components together into a single
application lifecycle:

    1. Bootstrap (check/provision runtime)
    2. Load configuration
    3. Initialize components
    4. Start services
    5. Run until shutdown
    6. Graceful cleanup

Components initialized:
    - Configuration
    - Credential store
    - Memory manager
    - Resource monitor
    - Model router
    - Tool registry
    - Agent controller
    - IPC server
    - Voice pipeline (if enabled)
    - HUD (if enabled)

The application can be started in multiple modes:
    - Full UI mode (HUD + tray + voice)
    - Headless mode (API only, for background operation)
    - CLI mode (interactive text interface)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path
from typing import Any

from futaba.core.config import (
    get_config, get_app_data_dir, get_logs_dir,
    FutabaConfig,
)
from futaba.tasks.task_manager import TaskManager, TaskJournal
from futaba.routing.model_router import ModelRouter, create_router
from futaba.agent.tools import ToolRegistry
from futaba.agent.hermes_bridge import HermesBridge
from futaba.agent.hermes_tools import (
    HermesExecutionTool, HermesComputerUseTool, HermesBrowserTool,
    VideoWorkflowTool, VoiceTool,
)
from futaba.video.pipeline import VideoPipeline
from futaba.voice.pipeline import VoicePipeline
from futaba.agent.controller import AgentController
from futaba.memory.memory_manager import MemoryManager
from futaba.performance.resource_manager import ResourceManager, ResourceMonitor
from futaba.security.credentials import CredentialStore
from futaba.ipc.server import IPCServer, FutabaAPI
from futaba.bootstrap.bootstrapper import bootstrap_runtime

logger = logging.getLogger("futaba")


# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------

class TaskContextFilter(logging.Filter):
    """Inject task_id into log records for traceability."""

    _current_task_id: str = ""

    def filter(self, record: logging.LogRecord) -> bool:
        record.task_id = self._current_task_id or "-"  # type: ignore[attr-defined]
        return True

    @classmethod
    def set_task(cls, task_id: str) -> None:
        cls._current_task_id = task_id

    @classmethod
    def clear_task(cls) -> None:
        cls._current_task_id = ""


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line for machine-parseable diagnostics."""

    def format(self, record: logging.LogRecord) -> str:
        import json as _json
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "task_id": getattr(record, "task_id", "-"),
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exc"] = self.formatException(record.exc_info)
        return _json.dumps(entry, default=str)


def setup_logging(level: int = logging.INFO) -> None:
    """Configure structured logging with task-ID traceability."""
    log_dir = get_logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    task_filter = TaskContextFilter()

    # Format: timestamp [level] component [task_id]: message
    fmt = "%(asctime)s [%(levelname)-7s] %(name)-25s [%(task_id)s]: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(logging.Formatter(fmt, datefmt))
    console.addFilter(task_filter)

    # File handler (rotating, human-readable)
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(
        log_dir / "futaba.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(fmt, datefmt))
    file_handler.addFilter(task_filter)

    # JSON structured log handler (machine-parseable)
    json_handler = RotatingFileHandler(
        log_dir / "futaba.jsonl",
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    json_handler.setLevel(logging.DEBUG)
    json_handler.setFormatter(_JsonFormatter())
    json_handler.addFilter(task_filter)

    # Root logger
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(file_handler)
    root.addHandler(json_handler)

    # Reduce noise from libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def check_single_instance() -> tuple[bool, Any]:
    """
    Check if an instance of Futaba is already running using a named Windows Mutex.
    Returns (is_only_instance, mutex_handle).
    """
    if sys.platform != "win32":
        return True, None
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        ERROR_ALREADY_EXISTS = 183
        mutex_name = "Global\\Futaba_Autonomous_Windows_Copilot_SingleInstance"
        mutex = kernel32.CreateMutexW(None, False, mutex_name)
        last_error = kernel32.GetLastError()
        if last_error == ERROR_ALREADY_EXISTS:
            return False, mutex
        return True, mutex
    except Exception as e:
        logger.debug("Mutex check exception: %s", e)
        return True, None


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class FutabaApp:
    """
    Main Futaba application.

    Owns and manages the lifecycle of all components.
    """

    def __init__(self):
        self.config: FutabaConfig | None = None
        self.credentials: CredentialStore | None = None
        self.memory: MemoryManager | None = None
        self.resources: ResourceManager | None = None
        self.router: ModelRouter | None = None
        self.hermes_bridge: HermesBridge | None = None
        self.video_pipeline: VideoPipeline | None = None
        self.voice_pipeline: VoicePipeline | None = None
        self.tools: ToolRegistry | None = None
        self.controller: AgentController | None = None
        self.ipc_server: IPCServer | None = None
        self.ipc_api: FutabaAPI | None = None
        self._shutdown_event = asyncio.Event()
        self._started = False

    async def start(self, mode: str = "full", port: int = 45850) -> None:
        """
        Initialize and start all components.

        Modes:
            "full" - All components including UI
            "headless" - No UI, API/agent only
            "cli" - Interactive CLI mode
        """
        logger.info("=" * 60)
        logger.info("FUTABA v0.1.0 — Autonomous Windows AI Copilot")
        logger.info("=" * 60)

        # 0. Bootstrap and self-provision
        logger.info("Running runtime bootstrapper...")
        report = bootstrap_runtime()
        if not report.is_healthy:
            logger.warning("Bootstrap warnings: %s", report.warnings)

        # 1. Configuration
        logger.info("Loading configuration...")
        self.config = get_config()

        # 3. Credentials
        logger.info("Initializing credential store...")
        self.credentials = CredentialStore()
        self._load_provider_credentials()

        # 4. Memory
        logger.info("Initializing memory system...")
        self.memory = MemoryManager()

        # 5. Resource monitor
        logger.info("Starting resource monitor...")
        self.resources = ResourceManager()
        await self.resources.start()

        # 6. Model router
        logger.info("Initializing model router...")
        self.router = create_router()

        # 6a. Hermes bridge
        logger.info("Initializing Hermes bridge...")
        self.hermes_bridge = HermesBridge()
        hermes_ok = await self.hermes_bridge.initialize()
        if hermes_ok:
            logger.info("Hermes Agent foundation active.")
        else:
            logger.warning("Hermes Agent unavailable; continuing in standalone mode.")

        # 6b. Video & Voice pipelines
        logger.info("Initializing video & voice pipelines...")
        self.video_pipeline = VideoPipeline(model_router=self.router)
        self.voice_pipeline = VoicePipeline(
            on_command=lambda cmd: asyncio.create_task(self.submit(cmd)),
            on_wake=lambda: logger.info("Wake word heard!"),
        )

        # 7. Tool registry
        logger.info("Registering tools...")
        self.tools = ToolRegistry.create_default()
        if hermes_ok:
            self.tools.register(HermesExecutionTool(self.hermes_bridge))
            self.tools.register(HermesComputerUseTool(self.hermes_bridge))
            self.tools.register(HermesBrowserTool(self.hermes_bridge))
        self.tools.register(VideoWorkflowTool(self.video_pipeline))
        self.tools.register(VoiceTool(self.voice_pipeline))

        # 8. Agent controller
        logger.info("Starting agent controller...")
        task_manager = TaskManager()
        self.controller = AgentController(
            task_manager=task_manager,
            model_router=self.router,
            tool_registry=self.tools,
        )

        # Wire up agent events to IPC broadcasts
        self.controller._on_status_update = self._on_task_status
        self.controller._on_blocker = self._on_task_blocker
        self.controller._on_complete = self._on_task_complete

        # Wire up voice pipeline to controller, task manager, and tools
        self.voice_pipeline.inject(
            task_manager=task_manager,
            controller=self.controller,
            tools=self.tools,
            hermes_bridge=self.hermes_bridge,
        )

        await self.controller.start()

        # 9. IPC Server
        logger.info("Starting IPC server on port %d...", port)
        self.ipc_server = IPCServer(port=port)
        self.ipc_api = FutabaAPI(self.ipc_server)
        self.ipc_api.inject(
            controller=self.controller,
            resource_manager=self.resources,
            memory_manager=self.memory,
            hermes_bridge=self.hermes_bridge,
            video_pipeline=self.video_pipeline,
            voice_pipeline=self.voice_pipeline,
            credential_store=self.credentials,
            model_router=self.router,
        )
        self.ipc_api.register_all()
        await self.ipc_server.start()

        # 10. Resource-based adjustments
        self.resources.monitor.add_listener(self._on_resource_change)

        self._started = True
        logger.info("Futaba is ready.")
        logger.info(
            "IPC server: ws://127.0.0.1:45850 | "
            "Active providers: %d | Tools: %d | Hermes: %s",
            len(self.router._registry.all()) if self.router else 0,
            len(self.tools.all_tools()) if self.tools else 0,
            "Connected" if hermes_ok else "Disabled",
        )

        if mode == "cli":
            await self._run_cli()
        elif mode in ("full", "gui"):
            if self.config.voice.enabled:
                try:
                    await self.voice_pipeline.start()
                except Exception as e:
                    logger.warning("Could not start voice pipeline: %s", e)
            await self._run_gui()
        elif mode == "headless":
            await self._shutdown_event.wait()

    async def stop(self) -> None:
        """Gracefully shut down all components."""
        logger.info("Shutting down Futaba...")
        self._shutdown_event.set()

        if self.voice_pipeline:
            await self.voice_pipeline.stop()

        if self.hermes_bridge:
            await self.hermes_bridge.close_all()

        if self.controller:
            await self.controller.stop()

        if self.ipc_server:
            await self.ipc_server.stop()

        if self.resources:
            await self.resources.stop()

        logger.info("Futaba shut down cleanly.")

    async def _run_gui(self) -> None:
        """Launch the native Windows UI (HUD, Tray, Dashboard)."""
        try:
            from futaba.ui.manager import run_ui_loop
            await run_ui_loop(self)
        except Exception as e:
            logger.error("Failed to run GUI: %s. Falling back to background wait.", e)
            await self._shutdown_event.wait()

    async def submit(self, request: str, title: str = "") -> Any:
        """Submit a task directly (for CLI/testing/UI)."""
        if not self.controller:
            raise RuntimeError("Agent not started")
        return await self.controller.submit(request, title)

    # -----------------------------------------------------------------------
    # CLI Mode
    # -----------------------------------------------------------------------

    async def _run_cli(self) -> None:
        """Interactive CLI mode."""
        print("\n╔══════════════════════════════════════════╗")
        print("║           FUTABA — AI Copilot            ║")
        print("║       Type a command or 'exit' to quit   ║")
        print("╚══════════════════════════════════════════╝\n")

        loop = asyncio.get_event_loop()

        while not self._shutdown_event.is_set():
            try:
                # Read input in executor to not block event loop
                user_input = await loop.run_in_executor(
                    None, lambda: input("Futaba> ").strip()
                )

                if not user_input:
                    continue
                if user_input.lower() in ("exit", "quit", "q"):
                    break
                if user_input.lower() == "status":
                    await self._print_status()
                    continue
                if user_input.lower() == "tasks":
                    await self._print_tasks()
                    continue
                if user_input.lower() == "resources":
                    await self._print_resources()
                    continue

                # Submit as a task
                task = await self.submit(user_input)
                print(f"  → Task {task.task_id[:8]}: {task.title}")
                print(f"    State: {task.state.value}")

            except EOFError:
                break
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"  Error: {e}")

    async def _print_status(self) -> None:
        """Print current status."""
        if self.ipc_api:
            status = await self.ipc_api.get_status()
            print(f"  Active tasks: {status.get('active_tasks', 0)}")
            print(f"  Queued tasks: {status.get('queued_tasks', 0)}")
            if 'resources' in status:
                res = status['resources']
                print(f"  CPU: {res.get('cpu_percent', 0):.1f}%")
                print(f"  RAM: {res.get('ram_percent', 0):.1f}%")
                print(f"  Gaming: {'Yes' if res.get('is_gaming') else 'No'}")

    async def _print_tasks(self) -> None:
        """Print active tasks."""
        if self.controller:
            tasks = self.controller._task_manager.get_active_tasks()
            if not tasks:
                print("  No active tasks.")
                return
            for t in tasks:
                print(f"  [{t.state.value:10}] {t.task_id[:8]}: {t.title}")

    async def _print_resources(self) -> None:
        """Print resource status."""
        if self.resources:
            snap = self.resources.snapshot
            print(f"  CPU: {snap.cpu_percent:.1f}%")
            print(f"  RAM: {snap.ram_percent:.1f}% ({snap.ram_available_mb:.0f} MB free)")
            if snap.gpu_available:
                print(f"  GPU: {snap.gpu_name} ({snap.gpu_utilization:.0f}%)")
            print(f"  Load: {snap.load_level.value}")
            print(f"  Gaming: {'Yes' if snap.is_gaming else 'No'}")
            print(f"  Foreground: {snap.foreground_process}")

    # -----------------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------------

    def _ensure_directories(self) -> None:
        """Create required runtime directories."""
        dirs = [
            get_app_data_dir() / "runtime",
            get_app_data_dir() / "config",
            get_app_data_dir() / "logs",
            get_app_data_dir() / "tasks",
            get_app_data_dir() / "memory",
            get_app_data_dir() / "cache",
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)

    def _load_provider_credentials(self) -> None:
        """Load API keys from credential store into provider configs."""
        if not self.config or not self.credentials:
            return

        for provider in self.config.ai.providers:
            cred_name = f"provider:{provider.name}"
            key = self.credentials.get(cred_name)
            if key:
                from pydantic import SecretStr
                provider.api_key = SecretStr(key)
                logger.debug("Loaded credential for provider: %s", provider.name)

    async def _on_task_status(self, task_id: str, status: str) -> None:
        """Broadcast task status update to UI."""
        if self.ipc_server:
            await self.ipc_server.broadcast_event("task_status", {
                "task_id": task_id,
                "status": status,
            })

    async def _on_task_blocker(self, task_id: str, blocker: Any) -> None:
        """Broadcast blocker notification to UI."""
        if self.ipc_server:
            await self.ipc_server.broadcast_event("task_blocked", {
                "task_id": task_id,
                "blocker": blocker.to_dict() if hasattr(blocker, 'to_dict') else str(blocker),
            })

    async def _on_task_complete(self, task_id: str, result: str) -> None:
        """Broadcast task completion to UI."""
        if self.ipc_server:
            await self.ipc_server.broadcast_event("task_complete", {
                "task_id": task_id,
                "result": result,
            })

    async def _on_resource_change(self, snapshot: Any) -> None:
        """Handle resource state changes."""
        if self.ipc_server:
            await self.ipc_server.broadcast_event("resource_change", snapshot.to_dict())

        # Adjust process priority
        if self.resources:
            self.resources.set_process_priority()


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

async def main(mode: str = "full", port: int = 45850) -> None:
    """Main entry point for Futaba."""
    setup_logging()

    app = FutabaApp()

    # Handle graceful shutdown
    def signal_handler(sig, frame):
        logger.info("Received signal %s, shutting down...", sig)
        app._shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        await app.start(mode=mode, port=port)
    except KeyboardInterrupt:
        pass
    finally:
        await app.stop()


def run() -> None:
    """Synchronous entry point."""
    import argparse
    parser = argparse.ArgumentParser(description="Futaba — Autonomous Windows AI Copilot")
    parser.add_argument(
        "--mode", choices=["cli", "headless", "full", "gui"],
        default="full", help="Start mode (default: full desktop experience with HUD/Tray/Dashboard)"
    )
    parser.add_argument(
        "--port", type=int, default=45850,
        help="IPC server port"
    )
    args = parser.parse_args()

    # Enforce single-instance guard to prevent duplicate process port collisions
    is_first, mutex = check_single_instance()
    if not is_first:
        msg = (
            "Futaba is already running.\n"
            "Check your system tray or taskbar to access the active instance."
        )
        print(f"\n[!] {msg}\n")
        logger.warning("Secondary instance launch prevented: Futaba is already running.")
        if args.mode in ("full", "gui"):
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, msg, "Futaba", 0x40)
            except Exception:
                pass
        sys.exit(0)

    try:
        if args.mode in ("full", "gui"):
            import threading
            import qasync
            from PySide6.QtWidgets import QApplication

            # Create QApplication on the main GUI thread first
            qapp = QApplication.instance()
            if qapp is None:
                qapp = QApplication(sys.argv)
                qapp.setApplicationName("Futaba")
                qapp.setQuitOnLastWindowClosed(False)

            # Enforce thread-safe event loop policy:
            # Main GUI thread gets qasync.QEventLoop; worker/Hermes/CUA threads get standard asyncio loop.
            # This prevents worker threads from calling QApplication::exec() or throwing thread errors.
            class SafeQEventLoopPolicy(asyncio.DefaultEventLoopPolicy):
                def new_event_loop(self):
                    if threading.current_thread() is threading.main_thread():
                        return qasync.QEventLoop(qapp)
                    return super().new_event_loop()

            asyncio.set_event_loop_policy(SafeQEventLoopPolicy())
            loop = asyncio.get_event_loop()
            try:
                loop.run_until_complete(main(mode=args.mode, port=args.port))
            finally:
                with contextlib.suppress(Exception):
                    loop.close()
        else:
            asyncio.run(main(mode=args.mode, port=args.port))
    except KeyboardInterrupt:
        pass
    except OSError as e:
        if getattr(e, "winerror", None) == 10048 or getattr(e, "errno", None) == 10048:
            msg = (
                f"Futaba port {args.port} is already in use by another instance or process.\n"
                f"Please close the conflicting process or specify another port with --port."
            )
            print(f"\n[!] {msg}\n")
            logger.error(msg)
            if args.mode in ("full", "gui"):
                try:
                    import ctypes
                    ctypes.windll.user32.MessageBoxW(0, msg, "Futaba Port Conflict", 0x30)
                except Exception:
                    pass
            sys.exit(0)
        else:
            logger.exception("Application startup failed: %s", e)
            sys.exit(1)
    except Exception as e:
        logger.exception("Unhandled error in Futaba: %s", e)
        sys.exit(1)
    finally:
        if mutex and sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(mutex)
            except Exception:
                pass


if __name__ == "__main__":
    run()

