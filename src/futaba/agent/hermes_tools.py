"""
Futaba Hermes Tools — Bridges Hermes capabilities and Futaba pipelines into the ToolRegistry.

This module provides Tool subclasses that conform to Futaba's Tool interface
while delegating execution directly to Hermes Agent built-ins:
- HermesExecutionTool: Full Hermes conversation loop (AIAgent)
- HermesComputerUseTool: Background desktop automation via Hermes cua-driver
- HermesBrowserTool: Browser automation via Hermes agent-browser
- VideoWorkflowTool: Video tutorial-to-workflow processing
- VoiceTool: Speech synthesis and microphone control
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from futaba.agent.tools import Tool, ToolResult
from futaba.agent.hermes_bridge import HermesBridge
from futaba.video.pipeline import VideoPipeline
from futaba.voice.pipeline import VoicePipeline

logger = logging.getLogger("futaba.agent.hermes_tools")


# ---------------------------------------------------------------------------
# Hermes Full Execution Tool
# ---------------------------------------------------------------------------

class HermesExecutionTool(Tool):
    """
    Delegates complex, multi-step tasks to the Hermes Agent runtime.
    """

    def __init__(self, bridge: HermesBridge):
        self._bridge = bridge

    @property
    def name(self) -> str:
        return "hermes"

    @property
    def description(self) -> str:
        return (
            "Execute complex multi-step objectives or sub-goals using the full "
            "Hermes Agent conversation loop with tool access."
        )

    @property
    def actions(self) -> list[str]:
        return ["run_conversation", "execute_task"]

    @property
    def capabilities(self) -> list[str]:
        return ["agent_loop", "multi_step", "autonomous_execution", "hermes_tools"]

    async def execute(
        self,
        action: str,
        parameters: dict[str, Any],
    ) -> ToolResult:
        start = time.monotonic()
        if not self._bridge.is_available:
            return ToolResult(
                success=False,
                error="Hermes Agent runtime is not available.",
                duration_seconds=time.monotonic() - start,
            )

        task_id = str(parameters.get("task_id", f"subtask-{int(time.time())}"))
        instruction = parameters.get("instruction") or parameters.get("message") or parameters.get("prompt", "")
        if not instruction:
            return ToolResult(
                success=False,
                error="No instruction or prompt provided.",
                duration_seconds=time.monotonic() - start,
            )

        provider = parameters.get("provider", "")
        model = parameters.get("model", "")
        api_key = parameters.get("api_key", "")
        toolsets = parameters.get("toolsets")

        try:
            session = self._bridge.get_session(task_id)
            if not session:
                session = await self._bridge.create_session(
                    task_id=task_id,
                    provider=provider,
                    model=model,
                    api_key=api_key,
                    enabled_toolsets=toolsets,
                )

            result = await self._bridge.run_conversation(task_id, instruction)
            elapsed = time.monotonic() - start

            if result.get("error"):
                return ToolResult(
                    success=False,
                    output=result.get("final_response", ""),
                    error=str(result["error"]),
                    duration_seconds=elapsed,
                    metadata={"tool_calls": result.get("tool_calls", [])},
                )

            return ToolResult(
                success=True,
                output=result.get("final_response", "Hermes conversation completed."),
                duration_seconds=elapsed,
                metadata={
                    "tool_calls": result.get("tool_calls", []),
                    "messages_count": len(result.get("messages", [])),
                },
            )

        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Hermes execution error: {e}",
                duration_seconds=time.monotonic() - start,
            )

    async def health_check(self) -> bool:
        return self._bridge.is_available


# ---------------------------------------------------------------------------
# Hermes Computer Use Tool
# ---------------------------------------------------------------------------

class HermesComputerUseTool(Tool):
    """
    Bridges Hermes computer_use (cua-driver / accessibility) to Futaba,
    with native Windows UI Automation fallback via PyWinAuto.
    Supports non-stealing background control, window inspection, typing, clicking.
    """

    def __init__(self, bridge: HermesBridge):
        self._bridge = bridge

    @property
    def name(self) -> str:
        return "computer_use"

    @property
    def description(self) -> str:
        return (
            "Universal background desktop control via Hermes CUA Driver (Windows UI Automation, "
            "non-stealing clicks, typing, window inspection, and screenshots) with native Windows fallback."
        )

    @property
    def actions(self) -> list[str]:
        return [
            "action", "screenshot", "capture", "click", "type", "press_key", "key",
            "scroll", "drag", "inspect_window", "list_windows", "list_apps", "focus_app", "launch_app",
            "launch", "open_application", "inspect", "analyze", "describe",
        ]

    @property
    def capabilities(self) -> list[str]:
        return ["desktop_control", "accessibility", "ui_automation", "background_input"]

    @staticmethod
    def _native_fallback(action: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Native Windows UI Automation fallback when cua-driver daemon drops or fails.
        Uses pywinauto and win32gui to provide resilient local execution.
        """
        import subprocess
        try:
            from pywinauto import Desktop
        except ImportError:
            return {"error": "pywinauto is not installed for native fallback"}

        norm_action = action.lower().strip()
        app_name = params.get("app") or params.get("process") or ""
        window_title = params.get("window_title") or params.get("title") or app_name

        if norm_action in ("launch_app", "launch"):
            cmd = params.get("command") or params.get("app") or params.get("executable")
            if not cmd:
                return {"error": "No app or command provided to launch"}
            proc = subprocess.Popen(cmd, shell=True)
            return {"ok": True, "action": "launch_app", "pid": proc.pid}

        elif norm_action in ("list_windows", "windows"):
            desktop = Desktop(backend="uia")
            windows = []
            for w in desktop.windows():
                try:
                    txt = w.window_text()
                    if txt:
                        rect = w.rectangle()
                        windows.append({
                            "title": txt,
                            "bounds": [rect.left, rect.top, rect.right, rect.bottom],
                            "is_visible": w.is_visible(),
                        })
                except Exception:
                    pass
            return {"windows": windows, "count": len(windows)}

        elif norm_action in ("capture", "inspect_window", "screenshot", "inspect", "analyze", "describe"):
            # Use ScreenContextProvider for safe, crash-proof perception
            try:
                from futaba.system.screen_provider import get_screen_provider
                provider = get_screen_provider()
                analysis = provider._sync_analyze(window_title)
                return analysis.to_dict()
            except Exception as sp_err:
                logger.debug("Screen provider fallback error: %s; trying pywinauto", sp_err)

            desktop = Desktop(backend="uia")
            target_win = None
            clean_title = window_title
            if clean_title.lower().endswith(".exe"):
                clean_title = clean_title[:-4]

            if clean_title:
                try:
                    target_win = desktop.window(title_re=f"(?i).*{re.escape(clean_title)}.*")
                except Exception:
                    pass

            if not target_win and clean_title:
                for w in desktop.windows():
                    try:
                        if clean_title.lower() in w.window_text().lower():
                            target_win = w
                            break
                    except Exception:
                        pass

            if not target_win:
                wins = [w for w in desktop.windows() if w.window_text()]
                if wins:
                    target_win = wins[0]

            if not target_win:
                return {"error": "No matching window found to inspect"}

            elements = []
            try:
                for idx, child in enumerate(target_win.descendants()[:50]):
                    try:
                        r = child.rectangle()
                        elements.append({
                            "index": idx,
                            "role": child.element_info.control_type,
                            "label": child.element_info.name,
                            "bounds": [r.left, r.top, r.right, r.bottom],
                        })
                    except Exception:
                        pass
                return {
                    "mode": "ax",
                    "window_title": target_win.window_text(),
                    "elements": elements,
                    "count": len(elements),
                }
            except Exception as e:
                return {"error": f"Failed to inspect window: {e}"}

        elif norm_action in ("type", "type_text", "set_value"):
            text = params.get("text") or params.get("value", "")
            desktop = Desktop(backend="uia")
            target_win = None
            clean_title = window_title
            if clean_title.lower().endswith(".exe"):
                clean_title = clean_title[:-4]

            if clean_title:
                try:
                    target_win = desktop.window(title_re=f"(?i).*{re.escape(clean_title)}.*")
                except Exception:
                    pass

            if not target_win and clean_title:
                for w in desktop.windows():
                    try:
                        if clean_title.lower() in w.window_text().lower():
                            target_win = w
                            break
                    except Exception:
                        pass

            if not target_win:
                wins = [w for w in desktop.windows() if w.window_text()]
                if wins:
                    target_win = wins[0]
            if not target_win:
                return {"error": "No matching window found for typing"}

            try:
                target_win.set_focus()
            except Exception:
                pass

            # Try direct UIA SetValue on Document or Edit control (works on modern Windows 11 Notepad, WordPad, etc.)
            try:
                for d in target_win.descendants()[:30]:
                    if d.element_info.control_type in ("Document", "Edit"):
                        try:
                            d.iface_value.SetValue(text)
                            return {"ok": True, "action": "type", "method": "SetValue", "text_len": len(text)}
                        except Exception:
                            pass
            except Exception:
                pass

            try:
                edit = target_win.child_window(control_type="Edit")
                edit.set_edit_text(text)
                return {"ok": True, "action": "type", "method": "set_edit_text", "text_len": len(text)}
            except Exception:
                try:
                    target_win.type_keys(text, with_spaces=True)
                    return {"ok": True, "action": "type", "method": "type_keys", "text_len": len(text)}
                except Exception as e:
                    return {"error": f"Failed to type: {e}"}

        elif norm_action in ("click", "left_click"):
            desktop = Desktop(backend="uia")
            coord = params.get("coordinate")
            if coord and len(coord) == 2:
                import pywinauto.mouse as mouse
                mouse.click(coords=(int(coord[0]), int(coord[1])))
                return {"ok": True, "action": "click", "coordinate": coord}
            elem_idx = params.get("element")
            if window_title and elem_idx is not None:
                try:
                    target_win = desktop.window(title_re=f".*{re.escape(window_title)}.*")
                    children = target_win.descendants()
                    if 0 <= int(elem_idx) < len(children):
                        children[int(elem_idx)].click_input()
                        return {"ok": True, "action": "click", "element": elem_idx}
                except Exception as e:
                    return {"error": f"Failed to click element: {e}"}
            return {"error": "Coordinate or element index required for click"}

        elif norm_action in ("key", "press_key"):
            keys = params.get("keys") or params.get("key", "")
            try:
                import pywinauto.keyboard as keyboard
                keyboard.send_keys(keys)
                return {"ok": True, "action": "key", "keys": keys}
            except Exception as e:
                return {"error": f"Failed to send keys: {e}"}

        elif norm_action in ("focus_app", "focus"):
            if not window_title:
                return {"error": "No window title or app provided to focus"}
            try:
                desktop = Desktop(backend="uia")
                target_win = desktop.window(title_re=f".*{re.escape(window_title)}.*")
                target_win.set_focus()
                return {"ok": True, "action": "focus_app", "app": window_title}
            except Exception as e:
                return {"error": f"Failed to focus window: {e}"}

        return {"error": f"Native fallback does not support action: {action}"}

    async def execute(
        self,
        action: str,
        parameters: dict[str, Any],
    ) -> ToolResult:
        start = time.monotonic()
        loop = asyncio.get_event_loop()

        # Action normalization
        act = parameters.get("action") or action
        action_map = {
            "screenshot": "capture",
            "inspect_window": "capture",
            "inspect": "capture",
            "analyze": "capture",
            "describe": "capture",
            "press_key": "key",
            "type_text": "type",
            "left_click": "click",
            "open_application": "launch",
            "launch_application": "launch",
            "start_application": "launch",
            "open_app": "launch",
            "start_app": "launch",
            "launch_app": "launch",
        }
        normalized_action = action_map.get(act, act)

        # Route application launching directly to ApplicationTool without CUA error
        if normalized_action == "launch":
            from futaba.agent.tools import ApplicationTool
            app_tool = ApplicationTool()
            app_params = dict(parameters)
            name = app_params.get("name") or app_params.get("app") or app_params.get("app_name") or app_params.get("path") or ""
            if "name" not in app_params and name:
                app_params["name"] = name
            return await app_tool.execute("launch", app_params)

        cua_args = dict(parameters)
        cua_args["action"] = normalized_action
        if normalized_action == "capture" and "mode" not in cua_args:
            cua_args["mode"] = "ax"

        # In test environments, allow automated test execution without interactive human prompts.
        if os.environ.get("FUTABA_ENV") == "test":
            try:
                import tools.approval as approval
                approval._YOLO_MODE_FROZEN = True
            except Exception:
                pass

        # Enforce explicit capability boundary (Requirement 26)
        allowed_cua_actions = {
            "inspect", "screenshot", "capture", "click", "type", "key",
            "press", "move", "drag", "hotkey", "left_click", "press_key"
        }
        if normalized_action not in allowed_cua_actions:
            return ToolResult(
                success=False,
                error=f"Action '{normalized_action}' is outside Futaba's configured capability boundary.",
            )

        def _dispatch_cua():
            if not self._bridge.is_available:
                return {"error": "Hermes runtime unavailable"}
            try:
                from tools.registry import registry, discover_builtin_tools
                discover_builtin_tools()
                return registry.dispatch("computer_use", cua_args)
            except Exception as e:
                return {"error": str(e)}

        res = await loop.run_in_executor(None, _dispatch_cua)
        engine_used = "cua_driver"

        # Check if CUA driver returned an error (either dict or JSON string)
        res_data = None
        if isinstance(res, str):
            try:
                res_data = json.loads(res)
            except Exception:
                pass
        elif isinstance(res, dict):
            res_data = res

        has_error = isinstance(res_data, dict) and bool(res_data.get("error"))

        # If CUA driver returns an error or fails, fallback to native Windows UIA
        if has_error:
            logger.warning("CUA driver returned error: %s; trying native Windows fallback", res_data.get("error"))
            native_res = await loop.run_in_executor(None, self._native_fallback, normalized_action, parameters)
            if not (isinstance(native_res, dict) and "error" in native_res):
                res = native_res
                engine_used = "native_windows_fallback"
                has_error = False

        elapsed = time.monotonic() - start

        if has_error:
            err_msg = res_data.get("error", "Unknown error") if isinstance(res_data, dict) else str(res)
            return ToolResult(
                success=False,
                error=err_msg,
                duration_seconds=elapsed,
            )

        output_str = json.dumps(res, default=str) if isinstance(res, (dict, list)) else str(res)
        return ToolResult(
            success=True,
            output=output_str,
            duration_seconds=elapsed,
            metadata={"raw": res if isinstance(res, dict) else {}, "engine": engine_used},
        )

    async def health_check(self) -> bool:
        if not self._bridge.is_available:
            return False
        try:
            from tools.computer_use.tool import check_computer_use_requirements
            status = check_computer_use_requirements()
            return bool(status.get("available", False) if isinstance(status, dict) else status)
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Hermes Browser Tool
# ---------------------------------------------------------------------------

class HermesBrowserTool(Tool):
    """
    Bridges Hermes browser automation tools (navigation, click, type, snapshot, etc.).
    """

    def __init__(self, bridge: HermesBridge):
        self._bridge = bridge

    @property
    def name(self) -> str:
        return "browser"

    @property
    def description(self) -> str:
        return (
            "Structured browser automation via Hermes browser tools (Chromium/Playwright/CDP). "
            "Navigate pages, click elements, fill forms, extract text, and capture snapshots."
        )

    @property
    def actions(self) -> list[str]:
        return [
            "navigate", "click", "type", "snapshot",
            "scroll", "press", "screenshot", "console",
        ]

    @property
    def capabilities(self) -> list[str]:
        return ["web_browsing", "page_interaction", "dom_snapshot", "form_filling"]

    async def execute(
        self,
        action: str,
        parameters: dict[str, Any],
    ) -> ToolResult:
        start = time.monotonic()
        if not self._bridge.is_available:
            return ToolResult(
                success=False,
                error="Hermes Agent runtime is not available for browser automation.",
                duration_seconds=time.monotonic() - start,
            )

        action_map = {
            "navigate": "browser_navigate",
            "goto": "browser_navigate",
            "click": "browser_click",
            "type": "browser_type",
            "snapshot": "browser_snapshot",
            "scroll": "browser_scroll",
            "press": "browser_press",
            "screenshot": "browser_vision",
            "console": "browser_console",
        }

        tool_name = action_map.get(action.lower(), f"browser_{action.lower()}")

        loop = asyncio.get_event_loop()

        def _dispatch_browser():
            try:
                from tools.registry import registry, discover_builtin_tools
                discover_builtin_tools()
                return registry.dispatch(tool_name, parameters)
            except Exception as e:
                return {"error": str(e)}

        res = await loop.run_in_executor(None, _dispatch_browser)
        elapsed = time.monotonic() - start

        if isinstance(res, dict) and "error" in res:
            return ToolResult(
                success=False,
                error=str(res["error"]),
                duration_seconds=elapsed,
            )

        output_str = json.dumps(res, default=str) if isinstance(res, (dict, list)) else str(res)
        return ToolResult(
            success=True,
            output=output_str,
            duration_seconds=elapsed,
        )

    async def health_check(self) -> bool:
        if not self._bridge.is_available:
            return False
        try:
            from tools.registry import registry, discover_builtin_tools
            discover_builtin_tools()
            return "browser_navigate" in registry._tools
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Video Workflow Tool
# ---------------------------------------------------------------------------

class VideoWorkflowTool(Tool):
    """
    Exposes Futaba's VideoPipeline as an agent tool.
    Converts tutorial videos into structured execution plans.
    """

    def __init__(self, pipeline: VideoPipeline):
        self._pipeline = pipeline

    @property
    def name(self) -> str:
        return "video"

    @property
    def description(self) -> str:
        return (
            "Analyze instructional or tutorial videos, extract demonstrated steps, "
            "commands, and code, and generate executable workflow plans."
        )

    @property
    def actions(self) -> list[str]:
        return ["process_video", "extract_workflow"]

    @property
    def capabilities(self) -> list[str]:
        return ["video_analysis", "speech_transcription", "ocr", "workflow_generation"]

    async def execute(
        self,
        action: str,
        parameters: dict[str, Any],
    ) -> ToolResult:
        start = time.monotonic()
        video_source = parameters.get("video_source") or parameters.get("path") or parameters.get("url", "")
        if not video_source:
            return ToolResult(
                success=False,
                error="No video_source, path, or url provided.",
                duration_seconds=time.monotonic() - start,
            )

        try:
            plan = await self._pipeline.process(
                video_source=video_source,
                analyze_every_n_seconds=float(parameters.get("interval", 5.0)),
                max_frames=int(parameters.get("max_frames", 50)),
            )

            plan_dict = plan.to_dict()
            return ToolResult(
                success=True,
                output=json.dumps(plan_dict, indent=2),
                duration_seconds=time.monotonic() - start,
                metadata={
                    "plan_id": plan.plan_id,
                    "steps_count": len(plan.steps),
                    "objective": plan.objective,
                },
            )

        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Video pipeline failed: {e}",
                duration_seconds=time.monotonic() - start,
            )

    async def health_check(self) -> bool:
        return self._pipeline.is_available


# ---------------------------------------------------------------------------
# Voice Control Tool
# ---------------------------------------------------------------------------

class VoiceTool(Tool):
    """
    Tool for speech synthesis and voice pipeline interactions.
    """

    def __init__(self, pipeline: VoicePipeline):
        self._pipeline = pipeline

    @property
    def name(self) -> str:
        return "voice"

    @property
    def description(self) -> str:
        return "Text-to-speech vocalization and voice pipeline status."

    @property
    def actions(self) -> list[str]:
        return ["speak", "status"]

    @property
    def capabilities(self) -> list[str]:
        return ["tts", "vocal_response", "voice_status"]

    async def execute(
        self,
        action: str,
        parameters: dict[str, Any],
    ) -> ToolResult:
        start = time.monotonic()
        if action == "speak":
            text = parameters.get("text", "")
            if not text:
                return ToolResult(success=False, error="No text to speak.")
            await self._pipeline.respond(text)
            return ToolResult(
                success=True,
                output=f"Spoke: {text[:80]}...",
                duration_seconds=time.monotonic() - start,
            )
        elif action == "status":
            return ToolResult(
                success=True,
                output=json.dumps({
                    "running": self._pipeline.is_running,
                    "listening": self._pipeline.is_listening,
                }),
                duration_seconds=time.monotonic() - start,
            )
        return ToolResult(success=False, error=f"Unknown action: {action}")

