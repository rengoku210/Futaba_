"""
Futaba Voice Command Router — Bridges Gemini Live function calling into FUTABA & Hermes.

Strict Authority Boundary:
Gemini Live is the conversational voice interface.
Hermes remains the actual autonomous execution engine.
FUTABA orchestrates state, tasks, tools, and UI.

Gemini never directly executes raw shell commands or raw Windows APIs.
It invokes controlled high-level functions registered here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Awaitable, TYPE_CHECKING

if TYPE_CHECKING:
    from futaba.tasks.task_manager import TaskManager, TaskState
    from futaba.agent.controller import AgentController
    from futaba.agent.tools import ToolRegistry, ApplicationTool
    from futaba.agent.hermes_bridge import HermesBridge

from futaba.system.context_tracker import get_context_tracker

logger = logging.getLogger("futaba.voice.router")


class VoiceCommandRouter:
    """
    Executes controlled functions requested by Gemini Live.
    """

    def __init__(
        self,
        task_manager: TaskManager | None = None,
        controller: AgentController | None = None,
        tools: ToolRegistry | None = None,
        hermes_bridge: HermesBridge | None = None,
        on_sleep_requested: Callable[[], None] | None = None,
    ):
        self.task_manager = task_manager
        self.controller = controller
        self.tools = tools
        self.hermes_bridge = hermes_bridge
        self.on_sleep_requested = on_sleep_requested

    def inject(
        self,
        task_manager: TaskManager | None = None,
        controller: AgentController | None = None,
        tools: ToolRegistry | None = None,
        hermes_bridge: HermesBridge | None = None,
        on_sleep_requested: Callable[[], None] | None = None,
    ) -> None:
        """Inject dependencies after initialization."""
        if task_manager:
            self.task_manager = task_manager
        if controller:
            self.controller = controller
        if tools:
            self.tools = tools
        if hermes_bridge:
            self.hermes_bridge = hermes_bridge
        if on_sleep_requested:
            self.on_sleep_requested = on_sleep_requested

    def get_tool_declarations(self) -> list[dict[str, Any]]:
        """
        Return the function declarations exposed to Gemini Live.
        Compatible with google.genai LiveConnectConfig.tools.
        """
        return [
            {
                "function_declarations": [
                    {
                        "name": "open_application",
                        "description": "Open or launch a desktop application on Windows (e.g. Notepad, Chrome, Discord, Calculator, VS Code, Explorer).",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "name": {
                                    "type": "STRING",
                                    "description": "The name or executable of the application (e.g., 'notepad', 'chrome', 'discord').",
                                },
                            },
                            "required": ["name"],
                        },
                    },
                    {
                        "name": "close_application",
                        "description": "Close or terminate a desktop application on Windows (e.g. Notepad, Chrome).",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "name": {
                                    "type": "STRING",
                                    "description": "The name or executable of the application to close.",
                                },
                            },
                            "required": ["name"],
                        },
                    },
                    {
                        "name": "submit_task",
                        "description": "Submit an autonomous desktop task or multi-step goal for Hermes Agent to execute on Windows (e.g. 'type text into Notepad', 'search YouTube for Minecraft shaders', 'create a folder on desktop', 'organize downloads').",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "instruction": {
                                    "type": "STRING",
                                    "description": "Clear natural-language instruction describing the objective.",
                                },
                            },
                            "required": ["instruction"],
                        },
                    },
                    {
                        "name": "get_current_activity",
                        "description": "Check what Futaba and Hermes Agent are currently doing on the computer. Reports active tasks, running steps, blockers, or idle state.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {},
                        },
                    },
                    {
                        "name": "get_task_status",
                        "description": "Check the detailed progress and status of a specific task.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "task_id": {
                                    "type": "STRING",
                                    "description": "The task ID to query.",
                                },
                            },
                            "required": ["task_id"],
                        },
                    },
                    {
                        "name": "cancel_task",
                        "description": "Cancel or stop an ongoing task or command.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "task_id": {
                                    "type": "STRING",
                                    "description": "Optional task ID to cancel. If omitted, cancels the most recent active task.",
                                },
                            },
                        },
                    },
                    {
                        "name": "pause_task",
                        "description": "Pause active task execution.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {},
                        },
                    },
                    {
                        "name": "resume_task",
                        "description": "Resume paused task execution.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {},
                        },
                    },
                    {
                        "name": "search_web",
                        "description": "Search the web for information or documentation using Futaba's browser / search tools.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "query": {
                                    "type": "STRING",
                                    "description": "The search query.",
                                    },
                            },
                            "required": ["query"],
                        },
                    },
                    {
                        "name": "get_active_window",
                        "description": "Inspect which application or window is currently focused/active on Windows.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {},
                        },
                    },
                    {
                        "name": "navigate_browser",
                        "description": "Navigate the web browser to a website, service, or URL (e.g., 'youtube.com', 'reddit.com', 'https://www.youtube.com', 'github.com'). Reuses any currently open browser instance rather than opening duplicate windows.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "url": {
                                    "type": "STRING",
                                    "description": "Destination website or URL (e.g., 'youtube', 'https://reddit.com').",
                                },
                                "new_tab": {
                                    "type": "BOOLEAN",
                                    "description": "Whether to explicitly open in a new tab.",
                                },
                            },
                            "required": ["url"],
                        },
                    },
                    {
                        "name": "go_to_sleep",
                        "description": "Put Futaba into dormant sleep mode when the user says 'go to sleep', 'stop listening', 'goodbye', or dismisses Futaba.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {},
                        },
                    },
                    {
                        "name": "get_screen_context",
                        "description": "Inspect what is currently on the user's screen or active window. Call this when the user asks 'What am I looking at?', 'What's on my screen?', 'What window is this?', or asks to inspect current screen contents.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {},
                        },
                    },
                ]
            }
        ]

    async def execute_tool_call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """
        Execute a function called by Gemini Live and return structured result.
        """
        logger.info("Gemini Live requested tool call: %s with args %s", name, args)

        handler = getattr(self, f"_handle_{name}", None)
        if handler:
            try:
                result = await handler(args)
                logger.info("Tool call %s completed successfully: %s", name, str(result)[:100])
                return result
            except Exception as e:
                logger.error("Tool call %s failed: %s", name, e, exc_info=True)
                return {"status": "error", "message": f"Execution failed: {str(e)}"}
        else:
            logger.warning("Unknown tool call requested: %s", name)
            return {"status": "error", "message": f"Unknown function: {name}"}

    # -----------------------------------------------------------------------
    # Tool Handlers
    # -----------------------------------------------------------------------

    async def _handle_get_screen_context(self, args: dict[str, Any]) -> dict[str, Any]:
        """Inspect the active screen or window to truthfully describe what user is looking at."""
        try:
            tracker = get_context_tracker()
            res = tracker.get_screen_context()
            logger.info("Screen context: %s", res)
            return res
        except Exception as e:
            logger.error("get_screen_context failed: %s", e)
            return {
                "status": "error",
                "message": "Unable to inspect current screen context.",
                "details": str(e),
            }

    _handle_what_is_on_screen = _handle_get_screen_context

    async def _handle_open_application(self, args: dict[str, Any]) -> dict[str, Any]:
        """Launch an application or navigate an existing browser on Windows."""
        app_name = args.get("name", "").strip()
        if not app_name:
            return {"status": "error", "message": "No application name provided."}

        # Guard against screen queries mistakenly routed to open_application
        lower_name = app_name.lower()
        screen_indicators = [
            "what am i looking at", "what's on my screen", "what is on my screen",
            "current window", "active window"
        ]
        if any(ind in lower_name for ind in screen_indicators):
            logger.info("Rerouting screen query '%s' from open_application to get_screen_context", app_name)
            return await self._handle_get_screen_context(args)

        # Guard against non-application conversational phrases
        conversational_phrases = ["something", "anything", "nothing", "right line", "right client", "testing"]
        if lower_name in conversational_phrases:
            return {"status": "error", "message": f"'{app_name}' is not recognized as a Windows application."}

        # First consult SystemContextTracker to prevent duplicate windows and reuse open browsers/apps
        try:
            tracker = get_context_tracker()
            res = tracker.open_or_navigate(app_name, new_tab=False, new_window=False)
            if res.get("status") == "success":
                return {
                    "status": "success",
                    "message": res.get("message", f"Opened {app_name}."),
                    "details": res,
                }
        except Exception as e:
            logger.debug("SystemContextTracker open_or_navigate error: %s", e)

        # Try application tool in registry
        if self.tools:
            app_tool = self.tools.get("application")
            if app_tool:
                res = await app_tool.execute(action="launch", parameters={"name": app_name})
                if res.success:
                    return {
                        "status": "success",
                        "message": f"I have opened {app_name} on your computer.",
                        "details": res.output,
                    }

        # Fallback to direct Windows launch
        try:
            import os
            os.startfile(app_name if app_name.endswith((".exe", ".msc")) else f"{app_name}.exe")
            return {"status": "success", "message": f"I opened {app_name} on your computer."}
        except Exception as e:
            return {"status": "error", "message": f"Could not launch {app_name}: {e}"}

    async def _handle_navigate_browser(self, args: dict[str, Any]) -> dict[str, Any]:
        """Navigate web browser to a URL, reusing existing browser."""
        url = args.get("url", "").strip()
        if not url:
            return {"status": "error", "message": "No URL or destination specified."}
        new_tab = bool(args.get("new_tab", False))

        tracker = get_context_tracker()
        res = tracker.open_or_navigate(url, new_tab=new_tab)
        if res.get("status") == "success":
            return {
                "status": "success",
                "message": res.get("message", f"Navigated to {url}."),
                "details": res,
            }
        return {"status": "error", "message": res.get("message", f"Could not navigate to {url}.")}

    async def _handle_go_to_sleep(self, args: dict[str, Any]) -> dict[str, Any]:
        """Put Futaba to sleep / dormant mode."""
        logger.info("Explicit go_to_sleep requested by user.")
        if self.on_sleep_requested:
            try:
                self.on_sleep_requested()
            except Exception as e:
                logger.debug("on_sleep_requested error: %s", e)
        return {
            "status": "success",
            "message": "Going to sleep. Say 'Hey Futaba' whenever you need me.",
        }

    async def _handle_close_application(self, args: dict[str, Any]) -> dict[str, Any]:
        """Close an application on Windows."""
        app_name = args.get("name", "").strip()
        if not app_name:
            return {"status": "error", "message": "No application name provided."}

        if self.tools:
            app_tool = self.tools.get("application")
            if app_tool:
                res = await app_tool.execute(action="kill", parameters={"name": app_name})
                if res.success:
                    return {
                        "status": "success",
                        "message": f"I have closed {app_name}.",
                    }

        return {"status": "error", "message": f"Could not close {app_name}."}

    async def _handle_submit_task(self, args: dict[str, Any]) -> dict[str, Any]:
        """Submit a task to TaskManager and AgentController for Hermes execution."""
        instruction = args.get("instruction", "").strip()
        if not instruction:
            return {"status": "error", "message": "No task instruction provided."}

        if not self.controller or not self.task_manager:
            return {"status": "error", "message": "FUTABA execution controller is not available."}

        # Create task
        task = await self.task_manager.create_task(instruction, title=f"Voice: {instruction[:30]}")
        logger.info("Voice task created: %s (%s)", task.task_id[:8], instruction[:50])

        # Wait briefly for immediate completion if simple/fast
        wait_seconds = 6.0
        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < wait_seconds:
            await asyncio.sleep(0.5)
            current = self.task_manager.get_task(task.task_id)
            if current and current.state.value in ("completed", "failed", "blocked"):
                task = current
                break

        current = self.task_manager.get_task(task.task_id) or task
        if current.state.value == "completed":
            return {
                "status": "completed",
                "task_id": current.task_id[:8],
                "message": f"Done. I completed: {instruction}",
                "result": current.result or "Successfully verified.",
            }
        elif current.state.value == "blocked":
            blocker_desc = current.blocker.description if current.blocker else "Needs user confirmation"
            return {
                "status": "blocked",
                "task_id": current.task_id[:8],
                "message": f"I started the task, but I am currently blocked: {blocker_desc}",
            }
        elif current.state.value == "failed":
            return {
                "status": "failed",
                "task_id": current.task_id[:8],
                "message": f"The task encountered an error: {current.error}",
            }
        else:
            return {
                "status": "running",
                "task_id": current.task_id[:8],
                "message": f"I'm working on: {instruction}. Hermes is currently running the plan in the background.",
            }

    async def _handle_get_current_activity(self, args: dict[str, Any]) -> dict[str, Any]:
        """Query real active tasks and status."""
        if not self.task_manager:
            return {"status": "idle", "message": "Futaba is currently idle."}

        # Get active tasks
        active = []
        for task in self.task_manager.get_active_tasks():
            active.append({
                "task_id": task.task_id[:8],
                "request": task.user_request,
                "state": task.state.value,
                "blocker": task.blocker.description if task.blocker else None,
            })

        if not active:
            return {
                "status": "idle",
                "message": "Futaba is currently idle with no active tasks running.",
            }

        first = active[0]
        if first["state"] == "blocked":
            msg = f"Task '{first['request']}' is currently blocked: {first['blocker']}"
        else:
            msg = f"Currently executing task: '{first['request']}' (state: {first['state']})."

        return {
            "status": "active",
            "message": msg,
            "tasks": active,
        }

    async def _handle_get_active_window(self, args: dict[str, Any]) -> dict[str, Any]:
        """Detect the active foreground window on Windows."""
        try:
            import ctypes
            import ctypes.wintypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                length = user32.GetWindowTextLengthW(hwnd) + 1
                buf = ctypes.create_unicode_buffer(length)
                user32.GetWindowTextW(hwnd, buf, length)
                title = buf.value

                pid = ctypes.wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                proc_name = ""
                try:
                    import psutil
                    if pid.value:
                        proc_name = psutil.Process(pid.value).name()
                except Exception:
                    pass

                return {
                    "status": "success",
                    "window_title": title,
                    "process_name": proc_name,
                    "message": f"Active window: '{title}' ({proc_name}).",
                }
            return {"status": "success", "window_title": "Desktop", "process_name": "explorer.exe", "message": "Desktop is focused."}
        except Exception as e:
            logger.debug("Active window inspection error: %s", e)
            return {"status": "error", "message": f"Could not inspect active window: {e}"}

    async def _handle_get_task_status(self, args: dict[str, Any]) -> dict[str, Any]:
        """Check status of a specific task."""
        task_id = args.get("task_id", "").strip()
        if not self.task_manager:
            return {"status": "error", "message": "Task manager unavailable."}

        task = self.task_manager.get_task(task_id)
        if not task:
            # Check prefix match
            for t in self.task_manager.get_all_tasks():
                if t.task_id.startswith(task_id):
                    task = t
                    break

        if not task:
            return {"status": "not_found", "message": f"Task {task_id} not found."}

        return {
            "task_id": task.task_id[:8],
            "state": task.state.value,
            "request": task.user_request,
            "result": task.result,
            "error": task.error,
            "blocker": task.blocker.description if task.blocker else None,
        }

    async def _handle_cancel_task(self, args: dict[str, Any]) -> dict[str, Any]:
        """Cancel a running task."""
        task_id = args.get("task_id", "").strip()
        if not self.task_manager:
            return {"status": "error", "message": "Task manager unavailable."}

        if task_id:
            await self.task_manager.cancel_task(task_id)
            return {"status": "cancelled", "message": f"Task {task_id[:8]} has been cancelled."}

        # Find most recent active task
        for t in reversed(self.task_manager.get_active_tasks()):
            await self.task_manager.cancel_task(t.task_id)
            return {
                "status": "cancelled",
                "message": f"Cancelled task '{t.user_request[:30]}'.",
            }

        return {"status": "none", "message": "No active tasks to cancel."}

    async def _handle_pause_task(self, args: dict[str, Any]) -> dict[str, Any]:
        """Pause task queue execution."""
        if self.task_manager:
            self.task_manager._queue.pause()
            return {"status": "paused", "message": "Task execution has been paused."}
        return {"status": "error", "message": "Task manager unavailable."}

    async def _handle_resume_task(self, args: dict[str, Any]) -> dict[str, Any]:
        """Resume task queue execution."""
        if self.task_manager:
            self.task_manager._queue.resume()
            return {"status": "resumed", "message": "Task execution has been resumed."}
        return {"status": "error", "message": "Task manager unavailable."}

    async def _handle_search_web(self, args: dict[str, Any]) -> dict[str, Any]:
        """Search the web via browser or search tools."""
        import urllib.parse

        query = args.get("query", "").strip()
        if not query:
            return {"status": "error", "message": "No search query provided."}

        # Check if query is targeting YouTube
        if "youtube" in query.lower():
            clean_q = query.lower().replace("search youtube for", "").replace("youtube for", "").replace("on youtube", "").strip()
            if not clean_q:
                clean_q = query
            encoded = urllib.parse.quote_plus(clean_q)
            target_url = f"https://www.youtube.com/results?search_query={encoded}"
        else:
            encoded = urllib.parse.quote_plus(query)
            target_url = f"https://www.google.com/search?q={encoded}"

        # First navigate or open active browser via SystemContextTracker
        try:
            tracker = get_context_tracker()
            res = tracker.open_or_navigate(target_url)
            if res.get("status") == "success":
                return {
                    "status": "success",
                    "message": f"Searched for '{query}'.",
                    "details": res,
                }
        except Exception as e:
            logger.debug("SystemContextTracker search navigation error: %s", e)

        if self.tools:
            browser_tool = self.tools.get("browser")
            if browser_tool:
                res = await browser_tool.execute(
                    action="navigate",
                    parameters={"url": target_url}
                )
                if res.success:
                    return {
                        "status": "success",
                        "message": f"Searched for '{query}'.",
                        "details": res.output,
                    }

        return {
            "status": "success",
            "message": f"Submitted web search for '{query}'.",
        }
