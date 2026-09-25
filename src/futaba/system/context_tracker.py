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
        """Focus an existing window for the given application with ground-truth verification."""
        clean = app_name.strip()
        try:
            from futaba.system.app_resolver import get_app_resolver
            resolver = get_app_resolver()
            windows = resolver.get_application_windows(app_name=clean)
            if windows:
                return resolver.focus_window(windows[0]["hwnd"])
        except Exception as e:
            logger.debug("AppResolver focus error: %s", e)

        try:
            # Fallback to Windows Script Host AppActivate
            cmd = f"(New-Object -ComObject WScript.Shell).AppActivate('{clean}')"
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if "True" in res.stdout:
                logger.info("Focused existing window for %s via AppActivate", clean)
                return True
        except Exception as e:
            logger.debug("AppActivate error: %s", e)

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
        # CASE 2: DESKTOP APPLICATION (e.g. "Roblox", "Discord", "Notepad", "VS Code")
        # -------------------------------------------------------------------
        try:
            from futaba.system.app_resolver import get_app_resolver
            resolver = get_app_resolver()
            res = resolver.open_or_focus(target)
            if res.success:
                self.last_app = res.display_name
                if target.lower() in BROWSER_EXECUTABLES:
                    self.last_browser = target.lower()
                self.last_action_time = time.monotonic()
                return res.to_dict()
            else:
                return {
                    "status": "error",
                    "action": res.action,
                    "application": res.display_name,
                    "message": res.message,
                    "error": res.error,
                }
        except Exception as e:
            logger.error("AppResolver execution failed for '%s': %s", target, e)
            return {"status": "error", "message": f"Could not launch or focus {target}: {e}"}

    def get_installed_applications(self) -> dict[str, str]:
        """
        Scan and return a mapping of application names to executable paths or launch targets.
        Scans common locations (Start Menu shortcuts, AppData, Program Files) with caching.
        """
        now = time.monotonic()
        if hasattr(self, "_installed_apps_cache") and self._installed_apps_cache:
            if now - getattr(self, "_installed_apps_cache_time", 0) < 300.0:
                return self._installed_apps_cache

        apps: dict[str, str] = {}
        try:
            from futaba.intelligence.context_engine import APPLICATION_ALIASES
            for name, exe in APPLICATION_ALIASES.items():
                apps[name] = f"{exe}.exe"
        except Exception:
            pass

        start_menu_dirs = [
            os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
            os.path.expandvars(r"%ALLUSERSPROFILE%\Microsoft\Windows\Start Menu\Programs"),
        ]
        for base_dir in start_menu_dirs:
            if not os.path.exists(base_dir):
                continue
            for root, _, files in os.walk(base_dir):
                for f in files:
                    if f.lower().endswith(".lnk"):
                        name = f[:-4].lower()
                        if name not in apps:
                            apps[name] = os.path.join(root, f)

        self._installed_apps_cache = apps
        self._installed_apps_cache_time = now
        return apps

    def get_screen_context(self, query: str = "") -> dict[str, Any]:
        """
        Inspect the active screen/window to provide truthful context for screen questions.
        Prevents hallucinated app launches or blind stalls.
        Delegates to ScreenContextProvider for safe UIA and multimodal inspection.
        """
        try:
            from futaba.system.screen_provider import get_screen_provider
            provider = get_screen_provider()
            analysis = provider._sync_analyze(query)
            return analysis.to_dict()
        except Exception as e:
            logger.debug("Screen provider context error: %s; falling back to basic foreground inspection", e)
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
    Uses the cognitive IntentResolver while maintaining full backward compatibility.
    """
    t = text.lower().strip()
    if not t:
        return UserIntent.GENERAL_CONVERSATION

    try:
        from futaba.intelligence.intent_resolver import get_intent_resolver, IntentCategory
        resolver = get_intent_resolver()
        active_app = ""
        active_process = ""
        target_application = ""
        browser_service = ""
        last_action = ""

        try:
            from futaba.intelligence.context_engine import get_context_engine
            ce = get_context_engine()
            target_application = ce.get_target_app()
            if ce.state.active_app:
                active_app = ce.state.active_app.name
                active_process = ce.state.active_app.process_name
            browser_service = ce.state.browser_state.service
            last_action = ce.state.last_action
        except Exception:
            pass

        resolved = resolver.resolve(
            text,
            active_app=active_app,
            active_process=active_process,
            target_application=target_application,
            browser_service=browser_service,
            last_action=last_action,
        )

        mapping = {
            IntentCategory.SCREEN_QUERY: UserIntent.SCREEN_UNDERSTANDING,
            IntentCategory.APPLICATION_LAUNCH: UserIntent.APPLICATION_CONTROL,
            IntentCategory.APPLICATION_FOCUS: UserIntent.APPLICATION_CONTROL,
            IntentCategory.APPLICATION_NAVIGATION: UserIntent.APPLICATION_CONTROL,
            IntentCategory.APPLICATION_INTERACTION: UserIntent.TASK_EXECUTION,
            IntentCategory.BROWSER_NAVIGATION: UserIntent.BROWSER_NAVIGATION,
            IntentCategory.BROWSER_INTERACTION: UserIntent.BROWSER_INTERACTION,
            IntentCategory.FILE_OPERATION: UserIntent.TASK_EXECUTION,
            IntentCategory.SYSTEM_OPERATION: UserIntent.TASK_EXECUTION,
            IntentCategory.TASK_EXECUTION: UserIntent.TASK_EXECUTION,
            IntentCategory.FOLLOW_UP: UserIntent.FOLLOW_UP,
            IntentCategory.INFORMATION_QUERY: UserIntent.INFORMATION_QUERY,
            IntentCategory.CONVERSATION: UserIntent.GENERAL_CONVERSATION,
            IntentCategory.TASK_CONTROL: UserIntent.GENERAL_CONVERSATION,
            IntentCategory.SLEEP_COMMAND: UserIntent.GENERAL_CONVERSATION,
            IntentCategory.CLARIFICATION_REQUIRED: UserIntent.GENERAL_CONVERSATION,
        }
        if resolved.category in mapping:
            return mapping[resolved.category]
    except Exception as e:
        logger.debug("IntentResolver fallback in classify_intent: %s", e)

    # Fallback to direct heuristic classification
    screen_keywords = [
        "what am i looking at",
        "what's on my screen",
        "what is on my screen",
        "what is on the screen",
        "what's on the screen",
        "what am i seeing",
        "describe my screen",
        "describe the screen",
        "describe current screen",
        "read my screen",
        "what window is this",
        "what app is this",
        "look at my screen",
        "what is open on my screen",
        "what is currently on my screen",
        "analyze my current screen",
        "analyze the screen",
        "analyze screen",
        "analyze current screen",
        "what am i watching",
        "what's playing",
        "what is playing",
        "what video is this",
    ]
    if any(kw in t for kw in screen_keywords):
        return UserIntent.SCREEN_UNDERSTANDING

    if any(kw in t for kw in ["go to sleep", "stop listening", "good night", "dismiss", "that's all for now", "sleep mode"]):
        return UserIntent.GENERAL_CONVERSATION

    if t in ("hey futaba", "hay futaba", "futaba", "hello futaba", "hi futaba"):
        return UserIntent.GENERAL_CONVERSATION

    if t.startswith("search ") or "search for " in t or "google " in t or "search youtube for" in t:
        return UserIntent.BROWSER_NAVIGATION

    for nav_prefix in ("go to ", "navigate to ", "visit "):
        if t.startswith(nav_prefix):
            return UserIntent.BROWSER_NAVIGATION

    tracker = get_context_tracker()
    for prefix in ("open ", "launch ", "start "):
        if t.startswith(prefix):
            target = t[len(prefix):].strip()
            if any(target.startswith(w) for w in ["what", "how", "why", "who", "where", "a ", "the "]):
                return UserIntent.INFORMATION_QUERY
            is_web, _ = tracker.is_web_destination(target)
            if is_web:
                return UserIntent.BROWSER_NAVIGATION
            if any(action in target for action in ("and type", "and search", "and write")):
                return UserIntent.TASK_EXECUTION
            return UserIntent.APPLICATION_CONTROL

    if any(t.startswith(prefix) for prefix in ("close ", "kill ", "quit ", "terminate ")):
        return UserIntent.APPLICATION_CONTROL

    if any(kw in t for kw in ["type ", "write into", "type into", "click on", "press ", "save as", "create a ", "delete the ", "organize "]):
        return UserIntent.TASK_EXECUTION

    if any(t.startswith(kw) for kw in ["now ", "and then ", "then ", "also ", "next ", "do it again", "close that", "save it"]):
        return UserIntent.FOLLOW_UP

    if any(t.startswith(kw) for kw in ["who is", "what is the", "why is", "tell me a", "how many", "explain ", "what time"]):
        return UserIntent.INFORMATION_QUERY

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
