"""
Futaba Context Engine — Continuous system state model.

Maintains a lightweight, continuously-updated representation of:
- Active foreground application (name, process, window title, HWND)
- Browser state (URL, tab title, detected service)
- Application ownership (did FUTABA open/navigate here?)
- Target application persistence across follow-ups
- Recent action history
- Conversation summary

The ContextEngine is polled every ~2 seconds by the voice pipeline or on
demand before every intent resolution.  It never blocks on Win32 calls —
all inspection is bounded to <50ms.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import psutil

logger = logging.getLogger("futaba.intelligence.context_engine")

# ---------------------------------------------------------------------------
# Known application metadata
# ---------------------------------------------------------------------------

# Maps user-friendly names → executable names (lowercase, without .exe)
APPLICATION_ALIASES: dict[str, str] = {
    "discord": "discord",
    "brave": "brave",
    "chrome": "chrome",
    "edge": "msedge",
    "firefox": "firefox",
    "notepad": "notepad",
    "calculator": "calculatorapp",
    "calc": "calculatorapp",
    "explorer": "explorer",
    "file explorer": "explorer",
    "vs code": "code",
    "vscode": "code",
    "visual studio code": "code",
    "spotify": "spotify",
    "steam": "steam",
    "obs": "obs64",
    "obs studio": "obs64",
    "vlc": "vlc",
    "terminal": "windowsterminal",
    "windows terminal": "windowsterminal",
    "cmd": "cmd",
    "powershell": "powershell",
    "word": "winword",
    "excel": "excel",
    "powerpoint": "powerpnt",
    "outlook": "outlook",
    "teams": "teams",
    "slack": "slack",
    "telegram": "telegram",
    "whatsapp": "whatsapp",
    "paint": "mspaint",
    "task manager": "taskmgr",
    "settings": "systemsettings",
    "control panel": "control",
    "snipping tool": "snippingtool",
}

# Browser process names (lowercase)
BROWSER_PROCESSES: set[str] = {
    "brave.exe", "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe",
}

# Known services detectable from browser window title
BROWSER_SERVICE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("youtube", re.compile(r"youtube", re.I)),
    ("discord", re.compile(r"discord", re.I)),
    ("reddit", re.compile(r"reddit", re.I)),
    ("github", re.compile(r"github", re.I)),
    ("twitter", re.compile(r"twitter|x\.com", re.I)),
    ("twitch", re.compile(r"twitch", re.I)),
    ("spotify", re.compile(r"spotify", re.I)),
    ("google", re.compile(r"google\s*(search|$)", re.I)),
    ("chatgpt", re.compile(r"chatgpt", re.I)),
    ("gmail", re.compile(r"gmail|mail\.google", re.I)),
    ("netflix", re.compile(r"netflix", re.I)),
]

# Native apps with known sub-context patterns (title parsing)
APP_SUBCONTEXT_PATTERNS: dict[str, list[tuple[str, re.Pattern]]] = {
    "discord.exe": [
        ("server", re.compile(r"^(.+?)\s*[-–]\s*Discord", re.I)),
        ("channel", re.compile(r"#(\S+)", re.I)),
    ],
    "spotify.exe": [
        ("track", re.compile(r"^(.+?)\s*[-–]\s*Spotify", re.I)),
    ],
    "code.exe": [
        ("workspace", re.compile(r"^(.+?)\s*[-–]\s*Visual Studio Code", re.I)),
    ],
}


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class BrowserState:
    """Current browser tab state."""
    url: str = ""
    tab_title: str = ""
    service: str = ""           # "youtube", "github", etc.
    browser_name: str = ""      # "brave", "chrome", etc.

    def is_active(self) -> bool:
        return bool(self.browser_name)


@dataclass
class ApplicationContext:
    """Snapshot of a single application's state."""
    name: str                       # Human-friendly: "Discord", "Brave", "Notepad"
    process_name: str               # "discord.exe", "brave.exe"
    window_title: str               # "Donut SMP - Discord"
    hwnd: int = 0
    pid: int = 0
    app_type: str = "native_app"    # "native_app" | "browser" | "system"
    sub_context: dict = field(default_factory=dict)
    # Browser sub_context: {url, tab_title, service}
    # Discord: {server, channel}
    last_focused: float = field(default_factory=time.monotonic)
    owned_by_futaba: bool = False   # Did FUTABA launch/navigate this?


@dataclass
class ContextState:
    """Complete context snapshot for LLM / routing decisions."""
    active_app: ApplicationContext | None = None
    previous_app: ApplicationContext | None = None
    target_application: str = ""        # Persists across follow-ups
    browser_state: BrowserState = field(default_factory=BrowserState)
    conversation_summary: str = ""      # Condensed last N turns
    active_task: str | None = None      # Currently executing task description
    last_action: str = ""               # "opened Discord", "navigated to YouTube"
    last_action_time: float = 0.0
    session_start: float = field(default_factory=time.monotonic)

    # Extended Section 9 Persistent Context
    active_server: str = ""
    active_channel: str = ""
    active_page: str = ""
    active_search_query: str = ""
    visible_controls: list[dict[str, Any]] = field(default_factory=list)
    recent_user_intent: str = ""
    previous_task: str | None = None
    last_successful_action: str = ""
    last_failed_action: str = ""
    current_goal: str = ""
    current_execution_surface: str = "DIRECT"
    user_interaction_mode: str = "VOICE"
    futaba_state: str = "IDLE"
    recent_entities: list[str] = field(default_factory=list)

    @property
    def active_application(self) -> str:
        return self.active_app.name if self.active_app else ""

    @property
    def active_window(self) -> str:
        return self.active_app.window_title if self.active_app else ""

    @property
    def active_process(self) -> str:
        return self.active_app.process_name if self.active_app else ""

    @property
    def active_browser(self) -> str:
        return self.browser_state.browser_name if self.browser_state else ""

    @property
    def active_url(self) -> str:
        return self.browser_state.url if self.browser_state else ""

    @property
    def active_tab(self) -> str:
        return self.browser_state.tab_title if self.browser_state else ""

    @property
    def current_task(self) -> str | None:
        return self.active_task

    def is_browser_active(self) -> bool:
        """Check if the current foreground is a browser."""
        if self.active_app and self.active_app.app_type == "browser":
            return True
        return False

    def is_app_active(self, app_name: str) -> bool:
        """Check if a specific application is currently focused."""
        if not self.active_app:
            return False
        clean = app_name.lower().strip()
        return (
            clean in self.active_app.name.lower()
            or clean in self.active_app.process_name.lower()
        )


# ---------------------------------------------------------------------------
# Context Engine
# ---------------------------------------------------------------------------

class ContextEngine:
    """
    Central context state model for FUTABA.

    Maintains a continuously-updated representation of what is happening
    on the user's Windows desktop.  All other intelligence components
    (IntentResolver, EntityResolver, ExecutionSurfaceSelector) read from
    this engine.
    """

    def __init__(self) -> None:
        self._state = ContextState()
        self._last_poll: float = 0.0
        self._poll_interval: float = 2.0       # seconds
        self._owned_apps: dict[str, float] = {} # process_name → timestamp
        self._action_history: list[tuple[float, str]] = []  # (time, description)
        self._max_history: int = 20

    @property
    def state(self) -> ContextState:
        return self._state

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def update(self) -> ContextState:
        """
        Poll the current Windows foreground and update state.

        This is cheap (~5ms) and safe to call from any thread.
        Skips polling if called within the poll interval.
        """
        now = time.monotonic()
        if now - self._last_poll < self._poll_interval:
            return self._state
        self._last_poll = now

        fg = self._get_foreground_window()
        title = fg.get("title", "")
        proc = fg.get("process", "").lower()
        hwnd = fg.get("hwnd", 0)
        pid = fg.get("pid", 0)

        # Skip if nothing has changed
        if (self._state.active_app
                and self._state.active_app.hwnd == hwnd
                and self._state.active_app.window_title == title):
            return self._state

        # Determine app type
        is_browser = proc in BROWSER_PROCESSES
        app_type = "browser" if is_browser else "native_app"

        # Friendly name
        friendly_name = self._process_to_friendly(proc)

        # Build sub-context
        sub_ctx: dict = {}
        if is_browser:
            service = self._detect_browser_service(title)
            browser_name = proc.replace(".exe", "")
            sub_ctx = {"tab_title": title, "service": service}
            self._state.browser_state = BrowserState(
                tab_title=title,
                service=service,
                browser_name=browser_name,
            )
        else:
            # Parse app-specific sub-context from title
            sub_ctx = self._parse_app_subcontext(proc, title)
            # Reset browser state if we left the browser
            if self._state.browser_state.is_active():
                # Keep browser state for reference but note it's not focused
                pass

        new_app = ApplicationContext(
            name=friendly_name,
            process_name=proc,
            window_title=title,
            hwnd=hwnd,
            pid=pid,
            app_type=app_type,
            sub_context=sub_ctx,
            last_focused=now,
            owned_by_futaba=proc in self._owned_apps,
        )

        # Shift active → previous
        if self._state.active_app and self._state.active_app.process_name != proc:
            self._state.previous_app = self._state.active_app

        self._state.active_app = new_app
        return self._state

    def force_update(self) -> ContextState:
        """Force an immediate poll regardless of interval."""
        self._last_poll = 0.0
        return self.update()

    # ------------------------------------------------------------------
    # Context Mutation
    # ------------------------------------------------------------------

    def set_target_application(self, app_name: str) -> None:
        """Set the persistent target application for follow-up commands."""
        self._state.target_application = app_name
        logger.debug("Target application set: %s", app_name)

    def clear_target_application(self) -> None:
        """Clear target application (explicit context switch)."""
        self._state.target_application = ""

    def record_action(self, description: str, target_app: str = "") -> None:
        """Record an action performed by FUTABA."""
        now = time.monotonic()
        self._state.last_action = description
        self._state.last_action_time = now
        self._action_history.append((now, description))
        if len(self._action_history) > self._max_history:
            self._action_history.pop(0)
        if target_app:
            self.set_target_application(target_app)
        logger.debug("Action recorded: %s", description)

    def mark_app_owned(self, process_name: str) -> None:
        """Mark an app as launched/navigated by FUTABA."""
        self._owned_apps[process_name.lower()] = time.monotonic()

    def set_active_task(self, description: str | None) -> None:
        """Set the currently executing task description."""
        self._state.active_task = description

    def set_conversation_summary(self, summary: str) -> None:
        """Update the conversation summary from ConversationContext."""
        self._state.conversation_summary = summary

    def switch_context(self, new_app: str) -> None:
        """Handle an explicit user-requested context switch."""
        logger.info("Explicit context switch to: %s", new_app)
        self._state.target_application = new_app
        self._state.last_action = f"switched context to {new_app}"
        self._state.last_action_time = time.monotonic()

    # ------------------------------------------------------------------
    # Context Queries
    # ------------------------------------------------------------------

    def get_context_for_llm(self) -> str:
        """
        Format current context as a string for injection into LLM prompts.

        Returns a concise, structured summary suitable for system messages.
        """
        parts: list[str] = []

        if self._state.active_app:
            app = self._state.active_app
            parts.append(f"Active window: {app.window_title} ({app.process_name})")
            if app.sub_context:
                for k, v in app.sub_context.items():
                    if v:
                        parts.append(f"  {k}: {v}")

        if self._state.browser_state.is_active():
            bs = self._state.browser_state
            parts.append(f"Browser: {bs.browser_name}")
            if bs.service:
                parts.append(f"  Service: {bs.service}")
            if bs.tab_title:
                parts.append(f"  Tab: {bs.tab_title}")

        if self._state.target_application:
            parts.append(f"Target application (for follow-ups): {self._state.target_application}")

        if self._state.last_action:
            elapsed = time.monotonic() - self._state.last_action_time
            if elapsed < 120:
                parts.append(f"Last action ({elapsed:.0f}s ago): {self._state.last_action}")

        if self._state.active_task:
            parts.append(f"Currently executing: {self._state.active_task}")

        if self._state.conversation_summary:
            parts.append(f"Conversation context: {self._state.conversation_summary}")

        if self._action_history:
            recent = self._action_history[-3:]
            history_lines = [desc for _, desc in recent]
            parts.append(f"Recent actions: {'; '.join(history_lines)}")

        return "\n".join(parts) if parts else "No context available — desktop idle."

    def get_active_app_name(self) -> str:
        """Get the friendly name of the currently active application."""
        if self._state.active_app:
            return self._state.active_app.name
        return ""

    def get_target_app(self) -> str:
        """Get the persistent target application name."""
        return self._state.target_application

    def is_within_app(self, app_name: str) -> bool:
        """Check if the current context is within a specific application."""
        clean = app_name.lower().strip()
        if self._state.target_application and clean in self._state.target_application.lower():
            return True
        if self._state.active_app:
            return (
                clean in self._state.active_app.name.lower()
                or clean in self._state.active_app.process_name.lower()
            )
        return False

    def get_recent_actions(self, count: int = 5) -> list[str]:
        """Get recent action descriptions."""
        return [desc for _, desc in self._action_history[-count:]]

    # ------------------------------------------------------------------
    # Win32 Inspection (internal)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_foreground_window() -> dict[str, Any]:
        """Read the current foreground window via Win32 API."""
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return {"title": "Desktop", "process": "explorer.exe", "hwnd": 0, "pid": 0}

            length = user32.GetWindowTextLengthW(hwnd) + 1
            buf = ctypes.create_unicode_buffer(length)
            user32.GetWindowTextW(hwnd, buf, length)
            title = buf.value

            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            proc_name = ""
            if pid.value:
                try:
                    proc_name = psutil.Process(pid.value).name()
                except Exception:
                    pass

            return {
                "title": title,
                "process": proc_name,
                "hwnd": hwnd,
                "pid": pid.value,
            }
        except Exception as e:
            logger.debug("Foreground window error: %s", e)
            return {"title": "Unknown", "process": "unknown", "hwnd": 0, "pid": 0}

    @staticmethod
    def _process_to_friendly(process_name: str) -> str:
        """Convert a process name to a human-friendly application name."""
        clean = process_name.lower().replace(".exe", "").strip()

        # Check special capitalizations first (handles multi-word names correctly)
        special = {
            "brave": "Brave",
            "chrome": "Chrome",
            "msedge": "Edge",
            "firefox": "Firefox",
            "discord": "Discord",
            "code": "VS Code",
            "spotify": "Spotify",
            "notepad": "Notepad",
            "explorer": "File Explorer",
            "steam": "Steam",
            "obs64": "OBS Studio",
            "vlc": "VLC",
            "windowsterminal": "Terminal",
            "powershell": "PowerShell",
        }
        if clean in special:
            return special[clean]

        # Reverse lookup in aliases
        for friendly, exe in APPLICATION_ALIASES.items():
            if exe == clean:
                return friendly.title()

        return clean.capitalize()

    @staticmethod
    def _detect_browser_service(title: str) -> str:
        """Detect which web service is active from browser window title."""
        for service_name, pattern in BROWSER_SERVICE_PATTERNS:
            if pattern.search(title):
                return service_name
        return ""

    @staticmethod
    def _parse_app_subcontext(process_name: str, title: str) -> dict:
        """Parse application-specific sub-context from window title."""
        patterns = APP_SUBCONTEXT_PATTERNS.get(process_name.lower(), [])
        ctx: dict = {}
        for key, pattern in patterns:
            m = pattern.search(title)
            if m:
                ctx[key] = m.group(1).strip()
        return ctx


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_engine: ContextEngine | None = None


def get_context_engine() -> ContextEngine:
    """Get the global ContextEngine singleton."""
    global _engine
    if _engine is None:
        _engine = ContextEngine()
    return _engine
