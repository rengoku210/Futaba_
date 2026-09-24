"""
Futaba Intent Resolver — Application-aware intent classification.

Replaces the simple keyword-based classify_intent() with a context-aware
resolver that considers:
- Active application context
- Target application persistence
- Entity resolution
- Follow-up detection (pronouns, implicit references)
- Execution surface hints

All heuristic — no LLM calls — for <5ms latency.
"""

from __future__ import annotations

import enum
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("futaba.intelligence.intent_resolver")


# ---------------------------------------------------------------------------
# Intent Taxonomy
# ---------------------------------------------------------------------------

class IntentCategory(str, enum.Enum):
    APPLICATION_LAUNCH = "application_launch"           # "open Discord"
    APPLICATION_FOCUS = "application_focus"              # "switch to Discord"
    APPLICATION_NAVIGATION = "application_navigation"   # "open Donut SMP" (within Discord)
    APPLICATION_INTERACTION = "application_interaction"  # "what are latest messages" (in Discord)
    BROWSER_NAVIGATION = "browser_navigation"           # "open YouTube", "go to reddit"
    BROWSER_INTERACTION = "browser_interaction"         # "search Mann Mera", "play first result"
    FILE_OPERATION = "file_operation"                   # "create a file", "save as"
    SYSTEM_OPERATION = "system_operation"               # "shutdown", "restart", "volume up"
    TASK_EXECUTION = "task_execution"                   # "type Hello into Notepad"
    FOLLOW_UP = "follow_up"                             # "do it again", "now save it"
    SCREEN_QUERY = "screen_query"                       # "what am I looking at?"
    INFORMATION_QUERY = "information_query"             # "what time is it?"
    CONVERSATION = "conversation"                       # "hello", "how are you"
    TASK_CONTROL = "task_control"                       # "pause", "cancel", "status"
    CLARIFICATION_REQUIRED = "clarification_required"   # Ambiguous — need more info
    SLEEP_COMMAND = "sleep_command"                     # "go to sleep", "stop listening"


@dataclass
class ResolvedIntent:
    """Complete intent resolution result."""
    category: IntentCategory
    target_application: str | None = None   # "Discord", "Brave", "Notepad"
    target_entity: str | None = None        # "Donut SMP", "Mann Mera", "general"
    action: str = ""                        # "launch", "navigate", "search", "type", "play", "pause"
    parameters: dict = field(default_factory=dict)
    confidence: float = 0.8
    requires_context: bool = False          # True for follow-ups
    execution_surface: str = ""             # Hint: "direct" | "hermes" | "browser_dom" | ...
    raw_text: str = ""                      # Original user text
    context_used: str = ""                  # What context influenced resolution


# ---------------------------------------------------------------------------
# Pattern Sets
# ---------------------------------------------------------------------------

# Screen / visual queries
_SCREEN_PATTERNS: list[str] = [
    "what am i looking at",
    "what's on my screen", "what is on my screen",
    "what's on the screen", "what is on the screen",
    "what am i seeing",
    "describe my screen", "describe the screen", "describe current screen",
    "read my screen",
    "what window is this", "what app is this",
    "look at my screen",
    "what is open on my screen", "what is currently on my screen",
    "analyze my current screen", "analyze the screen", "analyze screen",
    "analyze current screen",
    "what am i watching", "what's playing", "what is playing",
    "what video is this",
]

# Sleep / dormant
_SLEEP_PATTERNS: list[str] = [
    "go to sleep", "stop listening", "good night",
    "dismiss", "that's all for now", "that is all for now",
    "sleep mode", "goodbye", "bye futaba",
]

# Wake phrases (alone = conversation, not action)
_WAKE_PHRASES: set[str] = {
    "hey futaba", "hay futaba", "futaba",
    "hello futaba", "hi futaba",
}

# Task control commands
_TASK_CONTROL_PATTERNS: dict[str, str] = {
    "pause": "pause",
    "resume": "resume",
    "cancel": "cancel",
    "stop": "cancel",
    "status": "status",
    "what are you doing": "status",
    "what's happening": "status",
    "how is it going": "status",
}

# Follow-up indicators
_FOLLOW_UP_PREFIXES: list[str] = [
    "now ", "and then ", "then ", "also ", "next ",
    "do it again", "close that", "save it", "after that ",
    "and ", "ok now ",
]

# Browser interaction verbs
_BROWSER_INTERACTION_VERBS: list[str] = [
    "search", "search for", "play", "pause", "click",
    "scroll", "like", "subscribe", "comment",
    "watch", "find", "look up", "look for",
]

# System operation patterns
_SYSTEM_PATTERNS: list[str] = [
    "shutdown", "shut down", "restart", "reboot",
    "volume up", "volume down", "mute", "unmute",
    "brightness up", "brightness down",
    "lock screen", "lock computer", "log off", "sign out",
    "take a screenshot", "screenshot",
]

# Application launch prefixes
_LAUNCH_PREFIXES: list[str] = ["open ", "launch ", "start ", "run "]
_CLOSE_PREFIXES: list[str] = ["close ", "kill ", "quit ", "terminate ", "exit "]
_FOCUS_PREFIXES: list[str] = ["switch to ", "go to ", "focus ", "bring up "]

# File operation indicators
_FILE_INDICATORS: list[str] = [
    "create a file", "create file", "make a file",
    "save as", "save to", "save file",
    "delete file", "remove file",
    "copy file", "move file",
    "rename file",
]

# Known browser services → if user says "open X" and X is one of these,
# it's browser navigation, not app launch
from futaba.intelligence.entity_resolver import WEB_SERVICE_ENTITIES


class IntentResolver:
    """
    Application-aware intent classifier.

    Unlike the old classify_intent(), this resolver:
    1. Considers active app context (target_application, active process)
    2. Resolves entities within app context
    3. Detects follow-ups and implicit references
    4. Provides execution surface hints
    """

    def resolve(
        self,
        text: str,
        active_app: str = "",
        active_process: str = "",
        target_application: str = "",
        browser_service: str = "",
        last_action: str = "",
        last_intent: str = "",
    ) -> ResolvedIntent:
        """
        Resolve user text into a structured intent.

        Args:
            text: Raw user transcription
            active_app: Currently focused app name
            active_process: Currently focused process name
            target_application: Persistent target from context engine
            browser_service: Active browser service (youtube, github, etc.)
            last_action: Last action performed
            last_intent: Category of the last resolved intent

        Returns:
            ResolvedIntent with full classification
        """
        t = text.strip()
        if not t:
            return ResolvedIntent(
                category=IntentCategory.CONVERSATION,
                raw_text=t,
                confidence=0.5,
            )

        lower = t.lower().strip()

        # 1. Screen queries — highest priority
        intent = self._check_screen_query(lower, t)
        if intent:
            return intent

        # 2. Sleep / dormant commands
        intent = self._check_sleep(lower, t)
        if intent:
            return intent

        # 3. Wake phrases alone
        if lower in _WAKE_PHRASES:
            return ResolvedIntent(
                category=IntentCategory.CONVERSATION,
                action="wake_acknowledgment",
                raw_text=t,
                confidence=0.95,
            )

        # 4. Task control commands
        intent = self._check_task_control(lower, t)
        if intent:
            return intent

        # 5. System operations
        intent = self._check_system_operation(lower, t)
        if intent:
            return intent

        # 6. File operations
        intent = self._check_file_operation(lower, t)
        if intent:
            return intent

        # 7. Close/kill commands
        intent = self._check_close(lower, t)
        if intent:
            return intent

        # 8. Launch / open / focus commands
        intent = self._check_launch_or_navigate(
            lower, t, active_app, active_process,
            target_application, browser_service,
        )
        if intent:
            return intent

        # 9. Browser interaction (search, play, click, etc.)
        intent = self._check_browser_interaction(
            lower, t, active_app, active_process,
            target_application, browser_service,
        )
        if intent:
            return intent

        # 10. Task execution (type, write, click on, press)
        intent = self._check_task_execution(lower, t)
        if intent:
            return intent

        # 11. Follow-up commands
        intent = self._check_follow_up(
            lower, t, target_application, last_action, last_intent,
        )
        if intent:
            return intent

        # 12. Application interaction (questions about current app)
        intent = self._check_app_interaction(
            lower, t, active_app, active_process, target_application,
        )
        if intent:
            return intent

        # 13. Information queries
        intent = self._check_information_query(lower, t)
        if intent:
            return intent

        # 14. Default → conversation
        return ResolvedIntent(
            category=IntentCategory.CONVERSATION,
            raw_text=t,
            confidence=0.4,
        )

    # ------------------------------------------------------------------
    # Classification methods
    # ------------------------------------------------------------------

    def _check_screen_query(self, lower: str, raw: str) -> ResolvedIntent | None:
        if any(p in lower for p in _SCREEN_PATTERNS):
            return ResolvedIntent(
                category=IntentCategory.SCREEN_QUERY,
                action="analyze_screen",
                raw_text=raw,
                confidence=0.95,
                execution_surface="direct",
            )
        return None

    def _check_sleep(self, lower: str, raw: str) -> ResolvedIntent | None:
        if any(p in lower for p in _SLEEP_PATTERNS):
            return ResolvedIntent(
                category=IntentCategory.SLEEP_COMMAND,
                action="go_to_sleep",
                raw_text=raw,
                confidence=0.95,
                execution_surface="direct",
            )
        return None

    def _check_task_control(self, lower: str, raw: str) -> ResolvedIntent | None:
        for pattern, action in _TASK_CONTROL_PATTERNS.items():
            if lower.startswith(pattern) or lower == pattern:
                return ResolvedIntent(
                    category=IntentCategory.TASK_CONTROL,
                    action=action,
                    raw_text=raw,
                    confidence=0.85,
                    execution_surface="direct",
                )
        return None

    def _check_system_operation(self, lower: str, raw: str) -> ResolvedIntent | None:
        for pattern in _SYSTEM_PATTERNS:
            if pattern in lower:
                return ResolvedIntent(
                    category=IntentCategory.SYSTEM_OPERATION,
                    action=pattern.replace(" ", "_"),
                    raw_text=raw,
                    confidence=0.85,
                    execution_surface="terminal",
                )
        return None

    def _check_file_operation(self, lower: str, raw: str) -> ResolvedIntent | None:
        for pattern in _FILE_INDICATORS:
            if pattern in lower:
                return ResolvedIntent(
                    category=IntentCategory.FILE_OPERATION,
                    action="file_operation",
                    parameters={"instruction": raw},
                    raw_text=raw,
                    confidence=0.8,
                    execution_surface="filesystem",
                )
        return None

    def _check_close(self, lower: str, raw: str) -> ResolvedIntent | None:
        for prefix in _CLOSE_PREFIXES:
            if lower.startswith(prefix):
                target = raw[len(prefix):].strip()
                return ResolvedIntent(
                    category=IntentCategory.APPLICATION_LAUNCH,  # Reuse for close
                    target_application=target,
                    action="close",
                    raw_text=raw,
                    confidence=0.9,
                    execution_surface="direct",
                )
        return None

    def _check_launch_or_navigate(
        self,
        lower: str,
        raw: str,
        active_app: str,
        active_process: str,
        target_application: str,
        browser_service: str,
    ) -> ResolvedIntent | None:
        """
        Handle 'open X', 'launch X', 'start X', 'go to X'.

        This is the most context-dependent classification:
        - "open YouTube" → browser navigation (web service)
        - "open Discord" → app launch (native app)
        - "open Donut SMP" while Discord is target → app navigation (within Discord)
        - "open general" while Discord is target → app navigation (Discord channel)
        """
        matched_prefix = ""
        is_focus = False

        for prefix in _LAUNCH_PREFIXES:
            if lower.startswith(prefix):
                matched_prefix = prefix
                break

        if not matched_prefix:
            for prefix in _FOCUS_PREFIXES:
                if lower.startswith(prefix):
                    matched_prefix = prefix
                    is_focus = True
                    break

        if not matched_prefix:
            return None

        target = raw[len(matched_prefix):].strip()
        target_lower = target.lower()

        # Guard: if target looks like a question, it's an info query
        question_starts = ("what", "how", "why", "who", "where", "when")
        if any(target_lower.startswith(w) for w in question_starts):
            return ResolvedIntent(
                category=IntentCategory.INFORMATION_QUERY,
                parameters={"query": raw},
                raw_text=raw,
                confidence=0.7,
            )

        # Check if target is a known web service → browser navigation
        if target_lower in WEB_SERVICE_ENTITIES:
            return ResolvedIntent(
                category=IntentCategory.BROWSER_NAVIGATION,
                target_entity=target,
                action="navigate",
                parameters={"url": WEB_SERVICE_ENTITIES[target_lower]},
                raw_text=raw,
                confidence=0.9,
                execution_surface="direct",
            )

        # Check if target is a URL/domain
        for suffix in (".com", ".org", ".net", ".io", ".ai", ".dev", ".tv"):
            if suffix in target_lower:
                url = f"https://{target}" if not target.startswith(("http://", "https://")) else target
                return ResolvedIntent(
                    category=IntentCategory.BROWSER_NAVIGATION,
                    target_entity=target,
                    action="navigate",
                    parameters={"url": url},
                    raw_text=raw,
                    confidence=0.9,
                    execution_surface="direct",
                )

        # Check if we're within an app context and target is a sub-entity
        context_app = (target_application or active_app).lower()
        if context_app:
            # Discord context: "open Donut SMP", "open general"
            if "discord" in context_app or "discord" in active_process.lower():
                return ResolvedIntent(
                    category=IntentCategory.APPLICATION_NAVIGATION,
                    target_application="Discord",
                    target_entity=target,
                    action="navigate_in_app",
                    raw_text=raw,
                    confidence=0.8,
                    execution_surface="windows_uia",
                    context_used="discord_context",
                )

            # Spotify context: "open playlist X"
            if "spotify" in context_app:
                return ResolvedIntent(
                    category=IntentCategory.APPLICATION_NAVIGATION,
                    target_application="Spotify",
                    target_entity=target,
                    action="navigate_in_app",
                    raw_text=raw,
                    confidence=0.7,
                    execution_surface="windows_uia",
                    context_used="spotify_context",
                )

        # Multi-step action disguised as launch (e.g., "open notepad and type hello")
        if any(action_kw in target_lower for action_kw in ("and type", "and search", "and write")):
            return ResolvedIntent(
                category=IntentCategory.TASK_EXECUTION,
                target_application=target.split(" and ")[0].strip(),
                action="compound",
                parameters={"instruction": raw},
                raw_text=raw,
                confidence=0.85,
                execution_surface="hermes",
            )

        # Simple focus request
        if is_focus:
            return ResolvedIntent(
                category=IntentCategory.APPLICATION_FOCUS,
                target_application=target,
                action="focus",
                raw_text=raw,
                confidence=0.85,
                execution_surface="direct",
            )

        # Standard app launch
        return ResolvedIntent(
            category=IntentCategory.APPLICATION_LAUNCH,
            target_application=target,
            action="launch",
            raw_text=raw,
            confidence=0.85,
            execution_surface="direct",
        )

    def _check_browser_interaction(
        self,
        lower: str,
        raw: str,
        active_app: str,
        active_process: str,
        target_application: str,
        browser_service: str,
    ) -> ResolvedIntent | None:
        """Check for browser interaction commands: search, play, pause, click."""
        for verb in _BROWSER_INTERACTION_VERBS:
            pattern = f"{verb} "
            if lower.startswith(pattern) or f" {pattern}" in lower:
                # Only classify as browser interaction if browser/web is in context
                in_browser = (
                    "browser" in (target_application or "").lower()
                    or active_process.lower() in ("brave.exe", "chrome.exe", "msedge.exe", "firefox.exe")
                    or bool(browser_service)
                )

                if in_browser or verb in ("search", "search for", "look up", "look for"):
                    query_text = raw
                    # Extract the query part after the longest matching verb
                    sorted_verbs = sorted(_BROWSER_INTERACTION_VERBS, key=len, reverse=True)
                    for v in sorted_verbs:
                        vp = f"{v} "
                        idx = lower.find(vp)
                        if idx >= 0:
                            query_text = raw[idx + len(vp):].strip()
                            break

                    if query_text.lower().startswith("for "):
                        query_text = query_text[4:].strip()

                    action = verb.replace(" ", "_")
                    if verb in ("search", "search for", "look up", "look for", "find"):
                        action = "search"
                    elif verb in ("play", "watch"):
                        action = "play"

                    return ResolvedIntent(
                        category=IntentCategory.BROWSER_INTERACTION,
                        target_entity=query_text,
                        action=action,
                        parameters={"query": query_text},
                        raw_text=raw,
                        confidence=0.8,
                        execution_surface="browser_dom",
                        context_used=f"browser_service={browser_service}" if browser_service else "",
                    )
        return None

    def _check_task_execution(self, lower: str, raw: str) -> ResolvedIntent | None:
        """Check for task execution: typing, clicking, pressing keys, downloading, installing."""
        task_indicators = [
            "type ", "write into", "type into", "click on", "click ",
            "press ", "save as", "create a ", "delete the ",
            "organize ", "drag ", "move the ", "download ", "install ",
            "configure ", "recreate ", "compare ", "verify that ",
        ]
        if any(kw in lower for kw in task_indicators):
            return ResolvedIntent(
                category=IntentCategory.TASK_EXECUTION,
                action="execute",
                parameters={"instruction": raw},
                raw_text=raw,
                confidence=0.8,
                execution_surface="hermes",
            )
        return None

    def _check_follow_up(
        self,
        lower: str,
        raw: str,
        target_application: str,
        last_action: str,
        last_intent: str,
    ) -> ResolvedIntent | None:
        """Detect follow-up commands, delegating to action resolvers if command follows."""
        for prefix in _FOLLOW_UP_PREFIXES:
            if lower.startswith(prefix):
                sub_lower = lower[len(prefix):].strip()
                sub_raw = raw[len(prefix):].strip()
                # If follow-up contains an explicit launch/navigate (e.g. "now open YouTube")
                if any(sub_lower.startswith(lp) for lp in ("open ", "launch ", "start ", "go to ")):
                    nav_intent = self._check_launch_or_navigate(
                        sub_lower, sub_raw, "", "", target_application, ""
                    )
                    if nav_intent:
                        return nav_intent

                return ResolvedIntent(
                    category=IntentCategory.FOLLOW_UP,
                    target_application=target_application or None,
                    action="follow_up",
                    parameters={"instruction": raw},
                    raw_text=raw,
                    confidence=0.75,
                    requires_context=True,
                    execution_surface="hermes" if last_intent in (
                        "task_execution", "browser_interaction"
                    ) else "direct",
                    context_used=f"last_action={last_action}",
                )

        # Pronoun-based follow-up: "pause it", "close that", "do that again"
        pronoun_patterns = [
            (r"\b(it|that|this)\b.*\b(again|too|also)\b", "repeat"),
            (r"^(pause|resume|stop|play|close|save|delete)\s+(it|that|this)$", "pronoun_action"),
        ]
        for pattern, follow_type in pronoun_patterns:
            if re.search(pattern, lower):
                action = lower.split()[0] if lower.split() else "follow_up"
                return ResolvedIntent(
                    category=IntentCategory.FOLLOW_UP,
                    target_application=target_application or None,
                    action=action,
                    raw_text=raw,
                    confidence=0.7,
                    requires_context=True,
                    context_used=f"pronoun_follow_up:{follow_type}",
                )

        return None

    def _check_app_interaction(
        self,
        lower: str,
        raw: str,
        active_app: str,
        active_process: str,
        target_application: str,
    ) -> ResolvedIntent | None:
        """Check for questions/interactions about the current app."""
        # Questions about current app content
        app_interaction_patterns = [
            r"what (are|is) (the )?(latest|recent|new|last) (message|notification|update)",
            r"what('s| is) (happening|going on|new)",
            r"read (the |my )?(messages?|notifications?|chat|feed)",
            r"show me (the |my )?(messages?|notifications?)",
            r"who (sent|posted|messaged|said)",
        ]

        context_app = target_application or active_app
        if context_app:
            for pattern in app_interaction_patterns:
                if re.search(pattern, lower):
                    return ResolvedIntent(
                        category=IntentCategory.APPLICATION_INTERACTION,
                        target_application=context_app,
                        action="query_content",
                        parameters={"query": raw},
                        raw_text=raw,
                        confidence=0.8,
                        execution_surface="windows_uia",
                        context_used=f"app_context={context_app}",
                    )

        return None

    def _check_information_query(self, lower: str, raw: str) -> ResolvedIntent | None:
        """Check for general information queries."""
        info_starts = [
            "who is", "what is the", "what is a", "why is",
            "tell me a", "how many", "explain ", "what time",
            "when is", "where is", "how do", "how to",
            "what does", "what do", "can you tell me",
        ]
        if any(lower.startswith(p) for p in info_starts):
            return ResolvedIntent(
                category=IntentCategory.INFORMATION_QUERY,
                parameters={"query": raw},
                raw_text=raw,
                confidence=0.7,
            )
        return None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_resolver: IntentResolver | None = None


def get_intent_resolver() -> IntentResolver:
    """Get the global IntentResolver singleton."""
    global _resolver
    if _resolver is None:
        _resolver = IntentResolver()
    return _resolver
