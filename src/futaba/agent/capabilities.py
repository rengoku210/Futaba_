"""
Futaba Tool Capability Registry — Authoritative runtime capabilities and validation.

Enforces:
1. No standalone 'vision' tool: 'computer_use' is the canonical screen understanding capability.
2. Canonical tool and action routing.
3. Fail-fast non-transient error classification.
4. Authoritative capability exposure for planner prompts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("futaba.agent.capabilities")


@dataclass
class ToolCapability:
    """Capability specification for a registered or virtual tool."""
    name: str
    description: str
    available: bool = True
    execution_backend: str = ""
    supports_visual_interaction: bool = False
    supports_dom_interaction: bool = False
    supports_native_ui: bool = False
    supported_actions: list[str] = field(default_factory=list)
    canonical_replacement: Optional[str] = None
    canonical_action: Optional[str] = None


class ToolCapabilityRegistry:
    """
    Authoritative registry of tool capabilities.
    Validates plans, canonicalizes actions, and provides prompts for planners.
    """

    def __init__(self) -> None:
        self._capabilities: dict[str, ToolCapability] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        # 1. computer_use: Canonical capability for screen understanding and UI interaction
        self.register(
            ToolCapability(
                name="computer_use",
                description=(
                    "Canonical capability for native Windows desktop control, screen understanding, "
                    "visual inspection, UI element location, clicking, and typing via Hermes CUA Driver / UIA."
                ),
                available=True,
                execution_backend="hermes_cua",
                supports_visual_interaction=True,
                supports_native_ui=True,
                supported_actions=[
                    "inspect", "screenshot", "capture", "analyze", "describe",
                    "click", "type", "key", "press", "move", "drag", "hotkey"
                ],
            )
        )

        # 2. application: Windows desktop application lifecycle
        self.register(
            ToolCapability(
                name="application",
                description="Launch, focus, and close desktop applications on Windows.",
                available=True,
                execution_backend="win32",
                supports_native_ui=True,
                supported_actions=["launch", "close", "list_running", "discover"],
            )
        )

        # 3. browser: Web browser automation
        self.register(
            ToolCapability(
                name="browser",
                description="Web browser automation for DOM navigation, clicking, typing, and reading web pages.",
                available=True,
                execution_backend="playwright",
                supports_dom_interaction=True,
                supported_actions=["navigate", "click", "type", "read", "evaluate", "screenshot"],
            )
        )

        # 4. terminal: Shell command execution
        self.register(
            ToolCapability(
                name="terminal",
                description="Execute shell commands via PowerShell, CMD, or Python.",
                available=True,
                execution_backend="win32_shell",
                supported_actions=[
                    "run_powershell", "run_cmd", "run_python", "run_command", "list_processes"
                ],
            )
        )

        # 5. filesystem: File operations
        self.register(
            ToolCapability(
                name="filesystem",
                description="Read, write, edit, list, and verify files and directories.",
                available=True,
                execution_backend="os_filesystem",
                supported_actions=["read", "write", "edit", "list_dir", "exists", "delete"],
            )
        )

        # 6. search: Web search queries
        self.register(
            ToolCapability(
                name="search",
                description="Execute web searches for information or documentation.",
                available=True,
                execution_backend="web",
                supported_actions=["query"],
            )
        )

        # 7. vision: Explicitly UNAVAILABLE. Canonical replacement is computer_use
        self.register(
            ToolCapability(
                name="vision",
                description=(
                    "STANDALONE VISION TOOL DOES NOT EXIST. Screen understanding and visual inspection "
                    "are canonically provided by 'computer_use' with action 'inspect' or 'screenshot'."
                ),
                available=False,
                execution_backend="",
                supports_visual_interaction=True,
                supported_actions=["inspect", "screenshot"],
                canonical_replacement="computer_use",
                canonical_action="inspect",
            )
        )

    def register(self, capability: ToolCapability) -> None:
        self._capabilities[capability.name.lower()] = capability

    def get(self, name: str) -> Optional[ToolCapability]:
        return self._capabilities.get(name.lower())

    def is_available(self, name: str) -> bool:
        cap = self.get(name)
        return cap is not None and cap.available

    def get_available_tools(self) -> list[str]:
        return [name for name, cap in self._capabilities.items() if cap.available]

    def canonicalize_step(
        self,
        tool: str,
        action: str,
        parameters: dict[str, Any],
    ) -> tuple[str, str, dict[str, Any]]:
        """
        Canonicalize tool name and action to authoritative runtime targets.
        - Translates 'vision' -> 'computer_use:inspect'
        - Translates 'open_application' / 'launch_application' -> 'application:launch'
        - Translates 'computer_use:launch' -> 'application:launch'
        """
        clean_tool = (tool or "").strip().lower()
        clean_action = (action or "").strip()
        params = dict(parameters) if parameters else {}

        # 1. Check for hallucinated 'vision'
        if clean_tool == "vision":
            logger.info("Canonicalizing 'vision' tool call -> 'computer_use:inspect'")
            clean_tool = "computer_use"
            if clean_action not in ("inspect", "screenshot"):
                clean_action = "inspect"

        # 2. Canonicalize application launch actions
        if clean_action in ("open_application", "launch_application", "start_application", "open_app"):
            clean_tool = "application"
            clean_action = "launch"

        # 3. Canonicalize computer_use launch to application tool
        if clean_tool == "computer_use" and clean_action in ("launch", "open_application"):
            clean_tool = "application"
            clean_action = "launch"

        return clean_tool, clean_action, params

    def is_transient_error(self, error: str) -> bool:
        """
        Determine if an execution error is transient (worth retrying) or non-transient (fail-fast).
        Non-transient errors (e.g. unknown tool, invalid action, not found) must NOT be retried.
        """
        if not error:
            return True

        err_lower = error.lower()

        # Auth errors are never transient
        if self.is_auth_error(error):
            return False

        non_transient_indicators = [
            "unknown tool",
            "unknown function",
            "unknown action",
            "unsupported capability",
            "not implemented",
            "no such file",
            "file not found",
            "path not found",
            "command not found",
            "permission denied",
            "access is denied",
            "syntax error",
            "invalid action",
            "invalid parameter",
        ]

        for indicator in non_transient_indicators:
            if indicator in err_lower:
                return False

        return True

    def is_auth_error(self, error: str) -> bool:
        """
        Determine if an error is an authentication/authorization failure.
        These are permanent until credentials change — never retry.
        """
        if not error:
            return False
        err_lower = error.lower()
        auth_indicators = [
            "401", "403", "unauthorized", "forbidden",
            "authentication", "invalid api key", "invalid_api_key",
            "incorrect api key", "api key not valid",
        ]
        return any(indicator in err_lower for indicator in auth_indicators)

    def get_planner_prompt_section(self) -> str:
        """
        Generate authoritative tool capabilities section for planner prompts.
        Explicitly excludes 'vision' and declares 'computer_use' for screen inspection.
        """
        lines = ["Available tools and capabilities:"]
        for name, cap in self._capabilities.items():
            if not cap.available:
                continue
            actions_str = ", ".join(cap.supported_actions)
            lines.append(f"- {name}: {cap.description} (Actions: {actions_str})")

        lines.append(
            "\nCRITICAL RULES FOR TOOL SELECTION:\n"
            "- Screen Understanding: There is NO 'vision' tool. ALWAYS use 'computer_use' with action 'inspect' or 'screenshot' to read the screen or locate UI elements.\n"
            "- Opening Applications: Use 'application' with action 'launch' and parameter {'name': '<app_name>'}.\n"
            "- Web Browsing: Use 'browser' for web page navigation and interaction, or 'search' for Google/web queries.\n"
            "- Command Line: Use 'terminal' with 'run_powershell' or 'run_cmd'."
        )
        return "\n".join(lines)


# Singleton instance
_capabilities_registry: Optional[ToolCapabilityRegistry] = None

def get_capability_registry() -> ToolCapabilityRegistry:
    global _capabilities_registry
    if _capabilities_registry is None:
        _capabilities_registry = ToolCapabilityRegistry()
    return _capabilities_registry
