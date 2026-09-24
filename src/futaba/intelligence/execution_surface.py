"""
Futaba Execution Surface Selector — Routes intents to execution methods.

Given a resolved intent + context, selects the correct execution surface:
- DIRECT_APPLICATION: os.startfile, subprocess, focus (fastest)
- WINDOWS_UIA: pywinauto / UIA for in-app navigation
- HERMES_COMPUTER_USE: CUA for complex visual interaction
- BROWSER_DOM: Hermes browser tool for web DOM manipulation
- BROWSER_CUA: CUA fallback when browser DOM fails
- TERMINAL: PowerShell / cmd
- FILESYSTEM: File operations

Selection philosophy:
  App-first, web-second.
  Direct action over planner when possible.
  UIA over CUA for native apps.
  CUA as last resort, never first choice.
"""

from __future__ import annotations

import enum
import logging
from typing import Optional

from futaba.intelligence.intent_resolver import IntentCategory, ResolvedIntent

logger = logging.getLogger("futaba.intelligence.execution_surface")


class ExecutionSurface(str, enum.Enum):
    """Available execution methods, ordered from lightest to heaviest."""
    DIRECT_APPLICATION = "direct_application"       # subprocess, os.startfile, focus
    WINDOWS_UIA = "windows_uia"                     # pywinauto UIA for in-app nav
    HERMES_COMPUTER_USE = "hermes_computer_use"     # CUA driver for visual tasks
    BROWSER_DOM = "browser_dom"                     # Hermes browser tool (Playwright)
    BROWSER_CUA = "browser_cua"                     # CUA for browser when DOM fails
    TERMINAL = "terminal"                           # PowerShell / cmd
    FILESYSTEM = "filesystem"                       # File operations
    VOICE_ONLY = "voice_only"                       # No execution, just speak
    SUBMIT_TASK = "submit_task"                     # Full planner pipeline via Hermes


class ExecutionSurfaceSelector:
    """
    Selects the execution surface for a given intent.

    This is the routing logic that prevents FUTABA from:
    - Sending app launches through the full Hermes planner pipeline
    - Using CUA when UIA would work
    - Opening a browser for a native app operation
    - Submitting simple commands as multi-step tasks
    """

    def select(
        self,
        intent: ResolvedIntent,
        hermes_available: bool = True,
        uia_available: bool = True,
    ) -> ExecutionSurface:
        """
        Select the best execution surface for the given intent.

        Args:
            intent: Resolved intent from IntentResolver
            hermes_available: Whether Hermes runtime is available
            uia_available: Whether UIA/pywinauto is available

        Returns:
            The recommended ExecutionSurface
        """
        category = intent.category

        # If intent already has an execution surface hint, use it (unless invalid)
        if intent.execution_surface:
            try:
                return ExecutionSurface(intent.execution_surface)
            except ValueError:
                pass  # Invalid hint, fall through to logic

        # --- Direct actions (no planner needed) ---

        if category == IntentCategory.APPLICATION_LAUNCH:
            if intent.action == "close":
                return ExecutionSurface.DIRECT_APPLICATION
            return ExecutionSurface.DIRECT_APPLICATION

        if category == IntentCategory.APPLICATION_FOCUS:
            return ExecutionSurface.DIRECT_APPLICATION

        if category == IntentCategory.SLEEP_COMMAND:
            return ExecutionSurface.VOICE_ONLY

        if category == IntentCategory.CONVERSATION:
            return ExecutionSurface.VOICE_ONLY

        if category == IntentCategory.INFORMATION_QUERY:
            return ExecutionSurface.VOICE_ONLY

        if category == IntentCategory.SCREEN_QUERY:
            return ExecutionSurface.DIRECT_APPLICATION

        if category == IntentCategory.TASK_CONTROL:
            return ExecutionSurface.DIRECT_APPLICATION

        # --- Browser actions ---

        if category == IntentCategory.BROWSER_NAVIGATION:
            # Simple URL navigation → direct (no planner)
            return ExecutionSurface.DIRECT_APPLICATION

        if category == IntentCategory.BROWSER_INTERACTION:
            # Search, play, click → browser DOM tool
            if hermes_available:
                return ExecutionSurface.BROWSER_DOM
            # Fallback: submit as task
            return ExecutionSurface.SUBMIT_TASK

        # --- In-app navigation ---

        if category == IntentCategory.APPLICATION_NAVIGATION:
            # Navigate within Discord, Spotify, etc.
            if uia_available:
                return ExecutionSurface.WINDOWS_UIA
            if hermes_available:
                return ExecutionSurface.HERMES_COMPUTER_USE
            return ExecutionSurface.SUBMIT_TASK

        # --- In-app interaction ---

        if category == IntentCategory.APPLICATION_INTERACTION:
            # Read messages, inspect content within app
            if uia_available:
                return ExecutionSurface.WINDOWS_UIA
            if hermes_available:
                return ExecutionSurface.HERMES_COMPUTER_USE
            return ExecutionSurface.SUBMIT_TASK

        # --- Task execution (complex actions) ---

        if category == IntentCategory.TASK_EXECUTION:
            if hermes_available:
                return ExecutionSurface.SUBMIT_TASK
            return ExecutionSurface.TERMINAL

        # --- Follow-ups ---

        if category == IntentCategory.FOLLOW_UP:
            # Follow-ups inherit the execution surface of the previous action
            if intent.execution_surface:
                try:
                    return ExecutionSurface(intent.execution_surface)
                except ValueError:
                    pass

            # Default: submit as task if complex, direct if simple
            if intent.action in ("pause", "resume", "play", "stop"):
                return ExecutionSurface.DIRECT_APPLICATION
            return ExecutionSurface.SUBMIT_TASK

        # --- File and system operations ---

        if category == IntentCategory.FILE_OPERATION:
            return ExecutionSurface.FILESYSTEM

        if category == IntentCategory.SYSTEM_OPERATION:
            return ExecutionSurface.TERMINAL

        # --- Clarification ---

        if category == IntentCategory.CLARIFICATION_REQUIRED:
            return ExecutionSurface.VOICE_ONLY

        # Default: full task pipeline
        logger.debug("No specific surface for category %s, defaulting to SUBMIT_TASK", category.value)
        return ExecutionSurface.SUBMIT_TASK

    def get_fallback(self, surface: ExecutionSurface) -> ExecutionSurface | None:
        """
        Get the fallback execution surface if the primary fails.

        Returns None if there is no meaningful fallback.
        """
        fallbacks: dict[ExecutionSurface, ExecutionSurface] = {
            ExecutionSurface.DIRECT_APPLICATION: ExecutionSurface.SUBMIT_TASK,
            ExecutionSurface.WINDOWS_UIA: ExecutionSurface.HERMES_COMPUTER_USE,
            ExecutionSurface.HERMES_COMPUTER_USE: ExecutionSurface.SUBMIT_TASK,
            ExecutionSurface.BROWSER_DOM: ExecutionSurface.BROWSER_CUA,
            ExecutionSurface.BROWSER_CUA: ExecutionSurface.SUBMIT_TASK,
            ExecutionSurface.TERMINAL: ExecutionSurface.SUBMIT_TASK,
        }
        return fallbacks.get(surface)

    def is_direct_action(self, surface: ExecutionSurface) -> bool:
        """Check if this surface executes immediately without going through the planner."""
        return surface in (
            ExecutionSurface.DIRECT_APPLICATION,
            ExecutionSurface.VOICE_ONLY,
        )

    def requires_hermes(self, surface: ExecutionSurface) -> bool:
        """Check if this surface requires the Hermes runtime."""
        return surface in (
            ExecutionSurface.HERMES_COMPUTER_USE,
            ExecutionSurface.BROWSER_DOM,
            ExecutionSurface.BROWSER_CUA,
            ExecutionSurface.SUBMIT_TASK,
        )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_selector: ExecutionSurfaceSelector | None = None


def get_surface_selector() -> ExecutionSurfaceSelector:
    """Get the global ExecutionSurfaceSelector singleton."""
    global _selector
    if _selector is None:
        _selector = ExecutionSurfaceSelector()
    return _selector
