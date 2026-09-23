"""
Futaba Hermes Bridge — Integration layer with the Hermes Agent runtime.

This is the critical connection point between Futaba's task orchestration
and Hermes' agent capabilities. Instead of reimplementing Hermes' tools,
conversation loop, and provider system, we bridge to them.

Hermes provides:
    - Conversation loop (run_agent.AIAgent)
    - Tool registry (terminal, browser, computer_use, files, MCP, etc.)
    - Provider system (OpenRouter, Anthropic, OpenAI, Ollama, etc.)
    - Session/state persistence (SQLite state.db)
    - CUA Driver integration (background desktop control)
    - Skill system
    - Memory manager

Futaba adds on top:
    - Task state machine with crash recovery
    - Multi-task queue with priority
    - Model routing layer (planner/executor/verifier)
    - Video→task pipeline
    - Voice/wake-word
    - Native Windows HUD/tray
    - Resource management / gaming mode
    - Persistent user memory (separate from Hermes per-session memory)

Integration strategy:
    1. Import Hermes' AIAgent as a library
    2. Create per-task agent sessions
    3. Use Hermes' tool system for actual execution
    4. Bridge Hermes callbacks to Futaba's IPC/UI events
    5. Let Futaba handle task lifecycle; let Hermes handle individual turns

Architecture:
    FutabaAgentController
         ↓
    HermesBridge
         ↓
    AIAgent.run_conversation()
         ↓
    Hermes tools (terminal, browser, CUA, filesystem, MCP)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from futaba.core.config import get_config, get_app_data_dir

logger = logging.getLogger("futaba.agent.hermes_bridge")

# ---------------------------------------------------------------------------
# Hermes Path Discovery
# ---------------------------------------------------------------------------

def find_hermes_dir() -> Path | None:
    """
    Find the Hermes Agent installation using portable, prioritized discovery.

    Priority order:
    1. Config override (hermes.hermes_dir)
    2. HERMES_HOME environment variable
    3. Application-relative runtime (packaged Futaba.exe adjacent or internal)
    4. FUTABA-managed runtime directory (%LOCALAPPDATA%\\Futaba\\runtime\\hermes)
    5. Upward directory tree traversal from binary, module, and CWD
    6. User profile locations (%LOCALAPPDATA%\\hermes, ~/.hermes)
    """
    config = get_config()
    if config.hermes.hermes_dir:
        p = Path(config.hermes.hermes_dir)
        if p.exists() and (p / "run_agent.py").exists():
            return p

    candidates: list[Path] = []

    # 1. Environment variable
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        candidates.append(Path(hermes_home))

    # 2. Packaged executable directory (crucial for Futaba.exe distribution)
    exe_dir = Path(sys.executable).resolve().parent
    candidates.append(exe_dir / "hermes")
    candidates.append(exe_dir / "_internal" / "hermes")

    # PyInstaller one-file temporary directory
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "hermes")

    # 3. FUTABA-managed runtime directory
    app_data = get_app_data_dir()
    candidates.append(app_data / "runtime" / "hermes")
    candidates.append(app_data / "hermes")

    # 4. Current working directory and parent search
    cwd = Path.cwd().resolve()
    candidates.append(cwd / "hermes")
    for parent in list(cwd.parents)[:3]:
        candidates.append(parent / "hermes")

    # 5. Module source tree search (walk up from this file)
    try:
        mod_dir = Path(__file__).resolve().parent
        for parent in list(mod_dir.parents)[:5]:
            candidates.append(parent / "hermes")
    except Exception:
        pass

    # 6. User profile and standard Windows locations
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        candidates.append(Path(local_app_data) / "hermes")
    candidates.append(Path.home() / ".hermes")

    for p in candidates:
        try:
            if p.is_dir() and (p / "run_agent.py").is_file():
                return p.resolve()
        except Exception:
            continue

    return None


def ensure_hermes_importable(hermes_dir: Path) -> bool:
    """
    Add Hermes to the Python path and verify its primary entry point is importable.

    Returns True only if run_agent is genuinely importable.
    """
    hermes_str = str(hermes_dir)
    if hermes_str not in sys.path:
        sys.path.insert(0, hermes_str)

    # Hermes optional bootstrap on Windows
    try:
        import hermes_bootstrap  # noqa: F401
    except ImportError:
        logger.debug("hermes_bootstrap not found, continuing without it")

    try:
        import run_agent  # noqa: F401
        from run_agent import AIAgent  # noqa: F401
        return True
    except Exception as e:
        logger.warning("Cannot import Hermes AIAgent from %s: %s", hermes_dir, e)
        return False


# ---------------------------------------------------------------------------
# Hermes Session
# ---------------------------------------------------------------------------

@dataclass
class HermesSession:
    """
    A Hermes agent session bound to a Futaba task.

    Each Futaba task creates one HermesSession. The session holds
    the AIAgent instance and manages its lifecycle.
    """
    task_id: str
    session_id: str = ""
    agent: Any = None  # run_agent.AIAgent
    messages: list[dict[str, Any]] = field(default_factory=list)
    created: bool = False
    closed: bool = False

    # Callbacks for streaming output
    on_stream_delta: Callable[[str], None] | None = None
    on_tool_start: Callable[[str, dict], None] | None = None
    on_tool_complete: Callable[[str, str], None] | None = None


# ---------------------------------------------------------------------------
# Hermes Bridge
# ---------------------------------------------------------------------------

class HermesBridge:
    """
    Bridge between Futaba's task system and Hermes' agent runtime.

    Responsibilities:
    - Discover and import Hermes
    - Create per-task agent sessions
    - Execute conversations through Hermes
    - Bridge callbacks for UI updates
    - Manage session lifecycle
    - Handle Hermes configuration

    Thread safety:
    - Hermes AIAgent.run_conversation() is synchronous and blocking
    - We run it in a thread pool to not block the async event loop
    - Each session gets its own agent instance (thread-safe)
    """

    def __init__(self, hermes_dir: Path | None = None):
        self._hermes_dir: Path | None = hermes_dir
        self._importable = False
        self._sessions: dict[str, HermesSession] = {}
        self._lock = threading.Lock()
        self._ai_agent_class: type | None = None

    async def initialize(self) -> bool:
        """
        Initialize the Hermes bridge.

        Discovers Hermes, adds it to the Python path, and validates
        that it can be imported.

        Returns True if Hermes is available.
        """
        if not self._hermes_dir:
            self._hermes_dir = find_hermes_dir()

        if not self._hermes_dir:
            logger.warning(
                "Hermes Agent not found. Computer-use, browser automation, "
                "and advanced tools will be unavailable. "
                "Futaba will operate in reduced-capability mode."
            )
            return False

        logger.info("Found Hermes at: %s", self._hermes_dir)

        # Add to path and import
        loop = asyncio.get_event_loop()
        self._importable = await loop.run_in_executor(
            None, ensure_hermes_importable, self._hermes_dir
        )

        if self._importable:
            try:
                from run_agent import AIAgent
                self._ai_agent_class = AIAgent
                logger.info("Hermes Agent imported successfully")
            except Exception as e:
                logger.error("Failed to import Hermes AIAgent: %s", e)
                self._importable = False

        return self._importable

    @property
    def is_available(self) -> bool:
        return self._importable and self._ai_agent_class is not None

    async def create_session(
        self,
        task_id: str,
        provider: str = "",
        model: str = "",
        api_key: str = "",
        base_url: str = "",
        enabled_toolsets: list[str] | None = None,
        system_prompt: str = "",
        on_stream_delta: Callable[[str], None] | None = None,
        on_tool_start: Callable[[str, dict], None] | None = None,
        on_tool_complete: Callable[[str, str], None] | None = None,
    ) -> HermesSession:
        """
        Create a new Hermes agent session for a Futaba task.

        Each task gets its own session with its own agent instance.
        The session is initialized with the specified provider/model
        (determined by Futaba's model router) and tool configuration.
        """
        if not self.is_available:
            raise RuntimeError("Hermes is not available")

        config = get_config()
        session_id = f"futaba-{task_id[:8]}"

        session = HermesSession(
            task_id=task_id,
            session_id=session_id,
            on_stream_delta=on_stream_delta,
            on_tool_start=on_tool_start,
            on_tool_complete=on_tool_complete,
        )

        # Create the Hermes AIAgent in a thread to avoid blocking
        loop = asyncio.get_event_loop()

        def _create_agent():
            agent_kwargs = {
                "session_id": session_id,
            }

            # Provider/model configuration
            if provider:
                agent_kwargs["provider"] = provider
            if model:
                agent_kwargs["model"] = model
            if api_key:
                agent_kwargs["api_key"] = api_key
            if base_url:
                agent_kwargs["base_url"] = base_url

            # Toolset configuration
            default_toolsets = enabled_toolsets or config.hermes.tools_enabled
            agent_kwargs["enabled_toolsets"] = default_toolsets

            # Callbacks for UI integration
            if session.on_stream_delta:
                agent_kwargs["stream_delta_callback"] = session.on_stream_delta
            if session.on_tool_start:
                agent_kwargs["tool_start_callback"] = (
                    lambda name, args: session.on_tool_start(name, args)
                )
            if session.on_tool_complete:
                agent_kwargs["tool_complete_callback"] = (
                    lambda name, result: session.on_tool_complete(name, result)
                )

            try:
                agent = self._ai_agent_class(**agent_kwargs)
                session.agent = agent
                session.created = True
                return True
            except Exception as e:
                logger.error("Failed to create Hermes session: %s", e)
                return False

        success = await loop.run_in_executor(None, _create_agent)
        if not success:
            raise RuntimeError(f"Failed to create Hermes session for task {task_id}")

        with self._lock:
            self._sessions[task_id] = session

        logger.info(
            "Created Hermes session %s for task %s (provider=%s, model=%s, tools=%s)",
            session_id, task_id[:8], provider, model, enabled_toolsets
        )
        return session

    async def run_conversation(
        self,
        task_id: str,
        user_message: str,
        images: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Run a conversation turn through Hermes.

        This is the primary execution method. It sends a user message
        to the Hermes agent, which then autonomously plans and executes
        tool calls until it produces a final response.

        Returns:
            {
                "final_response": str,
                "messages": list,
                "completed": bool,
                "tool_calls": list,
                "error": str | None,
            }
        """
        session = self._sessions.get(task_id)
        if not session or not session.agent:
            raise RuntimeError(f"No Hermes session for task {task_id}")

        loop = asyncio.get_event_loop()

        def _run():
            try:
                # Hermes' run_conversation is synchronous
                result = session.agent.run_conversation(
                    user_message=user_message,
                )
                return {
                    "final_response": result.get("final_response", ""),
                    "messages": result.get("messages", []),
                    "completed": result.get("completed", True),
                    "tool_calls": result.get("tool_calls_made", []),
                    "error": None,
                }
            except Exception as e:
                logger.error(
                    "Hermes conversation error for task %s: %s",
                    task_id[:8], e
                )
                return {
                    "final_response": "",
                    "messages": [],
                    "completed": False,
                    "tool_calls": [],
                    "error": str(e),
                }

        result = await loop.run_in_executor(None, _run)
        return result

    async def close_session(self, task_id: str) -> None:
        """Close a Hermes session and release resources."""
        session = self._sessions.pop(task_id, None)
        if session and session.agent:
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, session.agent.close)
                session.closed = True
                logger.info("Closed Hermes session for task %s", task_id[:8])
            except Exception as e:
                logger.warning("Error closing Hermes session: %s", e)

    async def close_all(self) -> None:
        """Close all active sessions."""
        task_ids = list(self._sessions.keys())
        for task_id in task_ids:
            await self.close_session(task_id)

    def get_session(self, task_id: str) -> HermesSession | None:
        return self._sessions.get(task_id)

    def get_available_toolsets(self) -> list[str]:
        """Get the list of toolsets Hermes provides."""
        if not self.is_available:
            return []
        try:
            from toolsets import TOOLSET_DEFINITIONS
            return list(TOOLSET_DEFINITIONS.keys())
        except ImportError:
            return [
                "terminal", "file_operations", "web", "browser",
                "computer_use", "mcp", "memory", "skills",
            ]

    async def health_check(self) -> dict[str, Any]:
        """Check Hermes integration health."""
        result = {
            "hermes_available": self.is_available,
            "hermes_dir": str(self._hermes_dir) if self._hermes_dir else None,
            "active_sessions": len(self._sessions),
            "available_toolsets": self.get_available_toolsets(),
        }

        if self.is_available:
            # Check CUA Driver
            try:
                from tools.computer_use.cua_backend_driver import resolve_cua_driver_cmd
                cua_cmd = resolve_cua_driver_cmd()
                result["cua_driver"] = str(cua_cmd) if cua_cmd else "not found"
            except Exception as e:
                result["cua_driver"] = f"check failed: {e}"

            # Check Git Bash (required for terminal tool)
            try:
                from tools.environments.local import _find_bash
                git_bash = _find_bash()
                result["git_bash"] = str(git_bash) if git_bash else "not found"
            except Exception as e:
                result["git_bash"] = f"check failed: {e}"

        return result


# ---------------------------------------------------------------------------
# Hermes-Aware Tool (bridges Futaba's tool interface to Hermes)
# ---------------------------------------------------------------------------

class HermesTool:
    """
    A Futaba Tool that delegates execution to Hermes.

    Instead of reimplementing terminal/browser/computer_use/etc.,
    this tool creates a Hermes session and runs the user's request
    through Hermes' full agent loop, which has access to all Hermes
    tools.

    This is the recommended way to execute complex multi-step operations
    that require Hermes' sophisticated tool orchestration.
    """

    def __init__(self, bridge: HermesBridge):
        self._bridge = bridge

    @property
    def name(self) -> str:
        return "hermes"

    @property
    def description(self) -> str:
        return (
            "Execute tasks through the Hermes Agent runtime. "
            "Provides access to terminal, browser, computer use, "
            "file operations, MCP tools, and more."
        )

    @property
    def capabilities(self) -> list[str]:
        return [
            "terminal", "browser", "computer_use", "file_operations",
            "web_search", "mcp", "code_execution", "vision",
        ]

    async def execute(
        self,
        task_id: str,
        instruction: str,
        provider: str = "",
        model: str = "",
        api_key: str = "",
        toolsets: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Execute an instruction through Hermes.

        This creates a session, runs the conversation, and returns
        the result. The session is kept alive for follow-up turns.
        """
        if not self._bridge.is_available:
            return {
                "success": False,
                "error": "Hermes is not available",
                "output": "",
            }

        # Create or reuse session
        session = self._bridge.get_session(task_id)
        if not session:
            session = await self._bridge.create_session(
                task_id=task_id,
                provider=provider,
                model=model,
                api_key=api_key,
                enabled_toolsets=toolsets,
            )

        # Run conversation
        result = await self._bridge.run_conversation(task_id, instruction)

        return {
            "success": result["error"] is None,
            "output": result["final_response"],
            "error": result["error"] or "",
            "tool_calls": result["tool_calls"],
            "completed": result["completed"],
        }
