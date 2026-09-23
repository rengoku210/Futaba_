"""
Futaba System & Context Tracker — Manages application context, browser reuse,
window focus, and semantic destination resolution.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
from dataclasses import dataclass, field
import enum
import logging
import os
import subprocess
import time
from typing import Any, Optional
import psutil

logger = logging.getLogger("futaba.system.context")

# Common web services and domains
KNOWN_WEB_SERVICES: dict[str, str] = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "reddit": "https://www.reddit.com",
    "github": "https://www.github.com",
    "twitter": "https://twitter.com",
    "x": "https://x.com",
    "twitch": "https://www.twitch.tv",
    "wikipedia": "https://www.wikipedia.org",
    "netflix": "https://www.netflix.com",
    "amazon": "https://www.amazon.com",
    "discord web": "https://discord.com/app",
    "spotify web": "https://open.spotify.com",
    "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "gmail": "https://mail.google.com",
    "maps": "https://maps.google.com",
}

BROWSER_EXECUTABLES = {
    "brave": "brave.exe",
    "chrome": "chrome.exe",
    "msedge": "msedge.exe",
    "edge": "msedge.exe",
    "firefox": "firefox.exe",
    "opera": "opera.exe",
}


class SystemContextTracker:
    """
    Tracks foreground window, running browsers, and application state.
    Enforces intelligent application reuse and browser tab navigation.
    """

    def __init__(self) -> None:
        self.last_app: str = ""
        self.last_browser: str = ""
        self.last_url: str = ""
        self.last_search_query: str = ""
        self.last_action_time: float = 0.0

    def get_foreground_window(self) -> dict[str, Any]:
        """Detect the active foreground window on Windows."""
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
            logger.debug("Error getting foreground window: %s", e)
            return {"title": "Unknown", "process": "unknown", "hwnd": 0, "pid": 0}

    def get_running_browsers(self) -> list[dict[str, Any]]:
        """Return list of currently running browser processes."""
        running = []
        seen = set()
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                name = proc.info["name"].lower()
                for b_name, b_exe in BROWSER_EXECUTABLES.items():
                    if name == b_exe and b_name not in seen:
                        seen.add(b_name)
                        running.append({
                            "name": b_name,
                            "executable": b_exe,
                            "pid": proc.info["pid"],
                        })
            except Exception:
                pass
        return running

    def is_app_running(self, app_name: str) -> tuple[bool, Optional[dict[str, Any]]]:
        """Check if an application is currently running."""
        clean = app_name.lower().strip()
        if clean.endswith(".exe"):
            clean = clean[:-4]

        # Check browser alias
        exe_target = BROWSER_EXECUTABLES.get(clean, f"{clean}.exe")

        for proc in psutil.process_iter(["pid", "name"]):
            try:
                pname = proc.info["name"].lower()
                if pname == exe_target.lower() or pname == f"{clean}.exe" or clean in pname:
                    return True, {"pid": proc.info["pid"], "name": proc.info["name"]}
            except Exception:
                pass
        return False, None

    def focus_app(self, app_name: str) -> bool:
        """Focus an existing window for the given application."""
        clean = app_name.strip()
        try:
            # 1. Try Windows Script Host AppActivate
            cmd = f"(New-Object -ComObject WScript.Shell).AppActivate('{clean}')"
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if "True" in res.stdout:
                logger.info("Focused existing window for %s via AppActivate", clean)
                return True
        except Exception as e:
            logger.debug("AppActivate error: %s", e)

        # 2. Try Win32 ShowWindow / SetForegroundWindow
        try:
            user32 = ctypes.windll.user32
            found_hwnd = 0

            def enum_cb(hwnd, lparam):
                nonlocal found_hwnd
                if user32.IsWindowVisible(hwnd):
                    length = user32.GetWindowTextLengthW(hwnd) + 1
                    if length > 1:
                        buf = ctypes.create_unicode_buffer(length)
                        user32.GetWindowTextW(hwnd, buf, length)
                        if clean.lower() in buf.value.lower():
                            found_hwnd = hwnd
                            return 0
                return 1

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
            user32.EnumWindows(WNDENUMPROC(enum_cb), 0)

            if found_hwnd:
                SW_RESTORE = 9
                user32.ShowWindow(found_hwnd, SW_RESTORE)
                user32.SetForegroundWindow(found_hwnd)
                logger.info("Focused window for %s via Win32 HWND %08X", clean, found_hwnd)
                return True
        except Exception as e:
            logger.debug("Win32 focus error: %s", e)

        return False

    def is_web_destination(self, target: str) -> tuple[bool, str]:
        """Determine if target represents a website, service, or URL."""
        t = target.strip().lower()

        # Check known web services
        if t in KNOWN_WEB_SERVICES:
            return True, KNOWN_WEB_SERVICES[t]

        # Check full or partial URLs
        if t.startswith(("http://", "https://", "www.")):
            url = t if t.startswith(("http://", "https://")) else f"https://{t}"
            return True, url

        # Check common domain suffixes
        domain_suffixes = (".com", ".org", ".net", ".io", ".gov", ".edu", ".ai", ".dev", ".tv", ".co", ".xyz")
        for suffix in domain_suffixes:
            if suffix in t:
                # If target is something like "youtube.com" or "reddit.com/r/python"
                url = f"https://{t}" if not t.startswith(("http://", "https://")) else t
                return True, url

        return False, ""

    def open_or_navigate(
        self,
        target: str,
        new_tab: bool = False,
        new_window: bool = False,
    ) -> dict[str, Any]:
        """
        Intelligently open an application or navigate an existing browser instance.
        Ensures:
        - No duplicate browser windows when opening a website like YouTube.
        - Existing browser is reused and navigated.
        - Already running desktop apps are brought to focus instead of relaunched.
        """
        target = target.strip()
        if not target:
            return {"status": "error", "message": "No application or destination specified."}

        is_web, url = self.is_web_destination(target)

        # -------------------------------------------------------------------
        # CASE 1: WEB DESTINATION (e.g. "YouTube", "Reddit", "google.com")
        # -------------------------------------------------------------------
        if is_web:
            running_browsers = self.get_running_browsers()
            preferred_browser = "brave"

            # Check if any browser is already running
            if running_browsers and not new_window:
                active_browser = running_browsers[0]["name"]
                logger.info("Reusing open browser '%s' for web navigation: %s", active_browser, url)

                # Focus the browser
                self.focus_app(active_browser)

                # Navigate existing browser via Windows shell command
                # Chromium browsers (Brave, Chrome, Edge) open the URL in the active instance
                try:
                    cmd = f"Start-Process '{active_browser}' -ArgumentList '{url}'"
                    subprocess.run(
                        ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                        check=True,
                        timeout=5,
                    )
                    self.last_browser = active_browser
                    self.last_url = url
                    self.last_action_time = time.monotonic()
                    return {
                        "status": "success",
                        "mode": "navigated_existing_browser",
                        "browser": active_browser,
                        "url": url,
                        "message": f"Opened {target} in your existing {active_browser.title()} browser.",
                    }
                except Exception as e:
                    logger.warning("Failed to navigate existing browser %s: %s", active_browser, e)

            # If no browser is currently open or new_window requested:
            # Pick installed browser (Brave, then Chrome, then Edge)
            chosen_browser = preferred_browser
            for b_name in ("brave", "chrome", "msedge"):
                which_cmd = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", f"Get-Command '{b_name}' -ErrorAction SilentlyContinue"],
                    capture_output=True,
                    text=True,
                )
                if which_cmd.stdout.strip():
                    chosen_browser = b_name
                    break

            logger.info("Launching browser '%s' with URL: %s", chosen_browser, url)
            try:
                cmd = f"Start-Process '{chosen_browser}' -ArgumentList '{url}'"
                subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                    check=True,
                    timeout=5,
                )
                self.last_browser = chosen_browser
                self.last_url = url
                self.last_action_time = time.monotonic()
                return {
                    "status": "success",
                    "mode": "launched_browser",
                    "browser": chosen_browser,
                    "url": url,
                    "message": f"Opened {chosen_browser.title()} with {target}.",
                }
            except Exception as e:
                # Direct os.startfile fallback
                try:
                    os.startfile(url)
                    return {
                        "status": "success",
                        "mode": "default_browser",
                        "url": url,
                        "message": f"Opened {target} in your default browser.",
                    }
                except Exception as fallback_err:
                    return {"status": "error", "message": f"Could not open {target}: {fallback_err}"}

        # -------------------------------------------------------------------
        # CASE 2: DESKTOP APPLICATION (e.g. "Brave", "Notepad", "Calculator")
        # -------------------------------------------------------------------
        is_running, proc_info = self.is_app_running(target)

        if is_running and not new_window:
            logger.info("Application '%s' is already running; focusing existing instance.", target)
            focused = self.focus_app(target)
            self.last_app = target
            self.last_action_time = time.monotonic()
            if target.lower() in BROWSER_EXECUTABLES:
                self.last_browser = target.lower()
            return {
                "status": "success",
                "mode": "focused_existing",
                "application": target,
                "message": f"{target.title()} is already open; brought it to the foreground.",
            }

        # Launch fresh instance
        logger.info("Launching application: %s", target)
        try:
            exe_name = target if target.lower().endswith((".exe", ".msc")) else f"{target}.exe"
            cmd = f"Start-Process '{exe_name}' -ErrorAction Stop"
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                check=True,
                timeout=5,
            )
            self.last_app = target
            if target.lower() in BROWSER_EXECUTABLES:
                self.last_browser = target.lower()
            self.last_action_time = time.monotonic()
            return {
                "status": "success",
                "mode": "launched_new",
                "application": target,
                "message": f"I opened {target.title()} on your computer.",
            }
        except Exception as e:
            # Fallback to os.startfile
            try:
                os.startfile(target if target.endswith((".exe", ".msc")) else f"{target}.exe")
                self.last_app = target
                return {
                    "status": "success",
                    "mode": "launched_startfile",
                    "application": target,
                    "message": f"Opened {target.title()}.",
                }
            except Exception as start_err:
                return {"status": "error", "message": f"Could not launch {target}: {start_err}"}


    def get_screen_context(self) -> dict[str, Any]:
        """
        Inspect the active screen/window to provide truthful context for screen questions.
        Prevents hallucinated app launches or blind stalls.
        """
        fg = self.get_foreground_window()
        title = fg.get("title", "").strip()
        proc = fg.get("process", "").strip()
        hwnd = fg.get("hwnd", 0)

        if title and title != "Desktop" and title != "Unknown":
            desc = f"You are currently looking at '{title}' ({proc})."
        else:
            desc = "You are currently looking at your Windows desktop with no focused application window."

        return {
            "status": "success",
            "window_title": title or "Desktop",
            "process_name": proc or "explorer.exe",
            "hwnd": hwnd,
            "description": desc,
            "message": desc,
            "supports_visual_interaction": True,
        }


# ---------------------------------------------------------------------------
# Semantic Intent Classification & Conversation Context
# ---------------------------------------------------------------------------

class UserIntent(str, enum.Enum):
    APPLICATION_CONTROL = "application_control"
    BROWSER_NAVIGATION = "browser_navigation"
    BROWSER_INTERACTION = "browser_interaction"
    SCREEN_UNDERSTANDING = "screen_understanding"
    INFORMATION_QUERY = "information_query"
    GENERAL_CONVERSATION = "general_conversation"
    TASK_EXECUTION = "task_execution"
    FOLLOW_UP = "follow_up"


@dataclass
class TurnRecord:
    turn_id: int
    speaker: str  # "user" | "futaba"
    text: str
    timestamp: float = field(default_factory=time.monotonic)
    intent: Optional[str] = None
    tool_called: Optional[str] = None


class ConversationContext:
    """
    Bounded rolling history tracking multi-turn conversation, user intents,
    and real Windows application/browser state.
    """

    def __init__(self, max_turns: int = 20) -> None:
        self.max_turns = max_turns
        self.turns: list[TurnRecord] = []
        self._turn_counter = 0

    def add_turn(
        self,
        speaker: str,
        text: str,
        intent: Optional[str] = None,
        tool_called: Optional[str] = None,
    ) -> TurnRecord:
        self._turn_counter += 1
        record = TurnRecord(
            turn_id=self._turn_counter,
            speaker=speaker,
            text=text,
            timestamp=time.monotonic(),
            intent=intent,
            tool_called=tool_called,
        )
        self.turns.append(record)
        if len(self.turns) > self.max_turns:
            self.turns.pop(0)
        return record

    def get_recent_turns(self, count: int = 5) -> list[TurnRecord]:
        return self.turns[-count:]

    def get_last_user_turn(self) -> Optional[TurnRecord]:
        for turn in reversed(self.turns):
            if turn.speaker == "user":
                return turn
        return None

    def get_summary(self) -> dict[str, Any]:
        tracker = get_context_tracker()
        fg = tracker.get_foreground_window()
        return {
            "active_window": fg.get("title", "Unknown"),
            "active_process": fg.get("process", "unknown"),
            "last_app": tracker.last_app,
            "last_browser": tracker.last_browser,
            "last_url": tracker.last_url,
            "turn_count": len(self.turns),
        }


def classify_intent(text: str, context: Optional[ConversationContext] = None) -> UserIntent:
    """
    Classify user text into a structured semantic intent.
    Prevents confusing conversational phrases with application launches.
    """
    t = text.lower().strip()
    if not t:
        return UserIntent.GENERAL_CONVERSATION

    # 1. Screen understanding / visual inspection queries
    screen_keywords = [
        "what am i looking at",
        "what's on my screen",
        "what is on my screen",
        "what is on the screen",
        "what's on the screen",
        "what am i seeing",
        "describe my screen",
        "read my screen",
        "what window is this",
        "what app is this",
        "look at my screen",
        "what is open on my screen",
        "what is currently on my screen",
    ]
    if any(kw in t for kw in screen_keywords):
        return UserIntent.SCREEN_UNDERSTANDING

    # 2. Dormant / sleep commands
    if any(kw in t for kw in ["go to sleep", "stop listening", "good night", "dismiss", "that's all for now", "sleep mode"]):
        return UserIntent.GENERAL_CONVERSATION

    # 3. Wake phrases alone
    if t in ("hey futaba", "hay futaba", "futaba", "hello futaba", "hi futaba"):
        return UserIntent.GENERAL_CONVERSATION

    # 4. Web search queries
    if t.startswith("search ") or "search for " in t or "google " in t or "search youtube for" in t:
        return UserIntent.BROWSER_NAVIGATION

    # 5. Explicit browser navigation (URLs or known web services)
    for nav_prefix in ("go to ", "navigate to ", "visit "):
        if t.startswith(nav_prefix):
            return UserIntent.BROWSER_NAVIGATION

    tracker = get_context_tracker()
    for prefix in ("open ", "launch ", "start "):
        if t.startswith(prefix):
            target = t[len(prefix):].strip()
            # If target looks like a question or conversational phrase, do not treat as app launch
            if any(target.startswith(w) for w in ["what", "how", "why", "who", "where", "a ", "the "]):
                return UserIntent.INFORMATION_QUERY
            is_web, _ = tracker.is_web_destination(target)
            if is_web:
                return UserIntent.BROWSER_NAVIGATION
            # Multi-step action disguised as launch
            if any(action in target for action in ("and type", "and search", "and write")):
                return UserIntent.TASK_EXECUTION
            return UserIntent.APPLICATION_CONTROL

    if any(t.startswith(prefix) for prefix in ("close ", "kill ", "quit ", "terminate ")):
        return UserIntent.APPLICATION_CONTROL

    # 6. Task execution (typing, clicking, file creation, multi-step actions)
    if any(kw in t for kw in ["type ", "write into", "type into", "click on", "press ", "save as", "create a ", "delete the ", "organize "]):
        return UserIntent.TASK_EXECUTION

    # 7. Follow-up commands
    if any(t.startswith(kw) for kw in ["now ", "and then ", "then ", "also ", "next ", "do it again", "close that", "save it"]):
        return UserIntent.FOLLOW_UP

    # 8. Information queries
    if any(t.startswith(kw) for kw in ["who is", "what is the", "why is", "tell me a", "how many", "explain ", "what time"]):
        return UserIntent.INFORMATION_QUERY

    # Default
    return UserIntent.GENERAL_CONVERSATION


# Global singletons
_tracker: Optional[SystemContextTracker] = None
_conversation_context: Optional[ConversationContext] = None

def get_context_tracker() -> SystemContextTracker:
    global _tracker
    if _tracker is None:
        _tracker = SystemContextTracker()
    return _tracker

def get_conversation_context() -> ConversationContext:
    global _conversation_context
    if _conversation_context is None:
        _conversation_context = ConversationContext()
    return _conversation_context
