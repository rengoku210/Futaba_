"""
Futaba Entity Resolver — Resolves ambiguous entities against live context.

When a user says "Donut SMP", "Mann Mera", or "general", the entity resolver
determines what they mean based on:
1. Active application context (if Discord is focused → Discord server)
2. Installed applications (Windows Start Menu / registry)
3. Running processes
4. Known web services
5. Browser tabs (if browser is active)
6. Recent conversation history

The resolver is pure heuristic — no LLM calls — for <5ms latency.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("futaba.intelligence.entity_resolver")

# Known web services that can be entities
WEB_SERVICE_ENTITIES: dict[str, str] = {
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

# Common domain suffixes for URL detection
DOMAIN_SUFFIXES = (
    ".com", ".org", ".net", ".io", ".gov", ".edu",
    ".ai", ".dev", ".tv", ".co", ".xyz", ".app",
)


@dataclass
class ResolvedEntity:
    """Result of resolving an ambiguous entity."""
    original: str                       # Raw user text
    resolved_name: str                  # Resolved canonical name
    entity_type: str                    # "discord_server" | "discord_channel" |
                                        # "youtube_query" | "application" | "url" |
                                        # "browser_tab" | "file" | "unknown"
    context_source: str                 # "active_app" | "installed_apps" |
                                        # "web_service" | "conversation" |
                                        # "running_process" | "heuristic"
    confidence: float = 0.8            # 0.0–1.0
    target_app: str | None = None      # Which app this entity belongs to
    url: str = ""                       # If entity resolves to a URL
    metadata: dict = field(default_factory=dict)


class EntityResolver:
    """
    Resolves ambiguous entities using context from ContextEngine.

    Not a standalone module — always reads from ContextEngine for current
    app state and ConversationContext for recent turns.
    """

    def __init__(self) -> None:
        self._installed_apps_cache: dict[str, str] | None = None
        self._cache_time: float = 0.0

    def resolve(
        self,
        entity: str,
        active_app_name: str = "",
        active_app_process: str = "",
        active_app_type: str = "",
        browser_service: str = "",
        target_application: str = "",
        recent_turns: list[str] | None = None,
    ) -> ResolvedEntity:
        """
        Resolve an entity string against all available context.

        Args:
            entity: The raw entity to resolve (e.g., "Donut SMP", "Mann Mera")
            active_app_name: Currently focused app name
            active_app_process: Currently focused process name
            active_app_type: "native_app" | "browser"
            browser_service: Active browser service ("youtube", "discord", etc.)
            target_application: Persistent target app from context engine
            recent_turns: Recent conversation turn texts

        Returns:
            ResolvedEntity with resolution details
        """
        entity = entity.strip()
        if not entity:
            return ResolvedEntity(
                original=entity,
                resolved_name=entity,
                entity_type="unknown",
                context_source="heuristic",
                confidence=0.0,
            )

        lower = entity.lower()

        # 1. Check if it's a URL or domain
        result = self._resolve_as_url(entity, lower)
        if result:
            return result

        # 2. Check if it's a known web service
        result = self._resolve_as_web_service(entity, lower)
        if result:
            return result

        # 3. Context-aware resolution based on active app
        result = self._resolve_in_app_context(
            entity, lower, active_app_name, active_app_process,
            active_app_type, target_application,
        )
        if result:
            return result

        # 4. Check browser context
        if active_app_type == "browser" or browser_service:
            result = self._resolve_in_browser_context(
                entity, lower, browser_service,
            )
            if result:
                return result

        # 5. Check if it matches an installed/known application
        result = self._resolve_as_application(entity, lower)
        if result:
            return result

        # 6. Check conversation history for context clues
        if recent_turns:
            result = self._resolve_from_conversation(entity, lower, recent_turns)
            if result:
                return result

        # 7. Default: unresolved entity
        return ResolvedEntity(
            original=entity,
            resolved_name=entity,
            entity_type="unknown",
            context_source="heuristic",
            confidence=0.3,
        )

    # ------------------------------------------------------------------
    # Resolution strategies
    # ------------------------------------------------------------------

    def _resolve_as_url(self, entity: str, lower: str) -> ResolvedEntity | None:
        """Check if entity is a URL or domain."""
        if lower.startswith(("http://", "https://", "www.")):
            url = entity if entity.startswith(("http://", "https://")) else f"https://{entity}"
            return ResolvedEntity(
                original=entity,
                resolved_name=entity,
                entity_type="url",
                context_source="heuristic",
                confidence=1.0,
                url=url,
            )

        for suffix in DOMAIN_SUFFIXES:
            if suffix in lower:
                url = f"https://{entity}" if not entity.startswith(("http://", "https://")) else entity
                return ResolvedEntity(
                    original=entity,
                    resolved_name=entity,
                    entity_type="url",
                    context_source="heuristic",
                    confidence=0.9,
                    url=url,
                )

        return None

    def _resolve_as_web_service(self, entity: str, lower: str) -> ResolvedEntity | None:
        """Check if entity is a known web service."""
        if lower in WEB_SERVICE_ENTITIES:
            return ResolvedEntity(
                original=entity,
                resolved_name=lower.title(),
                entity_type="web_service",
                context_source="web_service",
                confidence=0.95,
                url=WEB_SERVICE_ENTITIES[lower],
            )
        return None

    def _resolve_in_app_context(
        self,
        entity: str,
        lower: str,
        active_app_name: str,
        active_app_process: str,
        active_app_type: str,
        target_application: str,
    ) -> ResolvedEntity | None:
        """Resolve entity within the context of the active/target application."""
        # Determine which app context to use
        app_name = (target_application or active_app_name).lower()
        app_proc = active_app_process.lower()

        # Discord context
        if "discord" in app_name or "discord" in app_proc:
            return self._resolve_discord_entity(entity, lower)

        # Spotify context
        if "spotify" in app_name or "spotify" in app_proc:
            return ResolvedEntity(
                original=entity,
                resolved_name=entity,
                entity_type="spotify_query",
                context_source="active_app",
                confidence=0.7,
                target_app="Spotify",
            )

        # VS Code context
        if "code" in app_proc or "vscode" in app_name or "vs code" in app_name:
            return ResolvedEntity(
                original=entity,
                resolved_name=entity,
                entity_type="vscode_item",
                context_source="active_app",
                confidence=0.6,
                target_app="VS Code",
            )

        return None

    def _resolve_discord_entity(self, entity: str, lower: str) -> ResolvedEntity:
        """Resolve entity as a Discord server, channel, or user."""
        # Known Discord channel patterns
        if lower.startswith("#") or lower in ("general", "chat", "voice", "memes",
                                                "announcements", "welcome", "rules",
                                                "off-topic", "gaming", "music"):
            return ResolvedEntity(
                original=entity,
                resolved_name=entity.lstrip("#"),
                entity_type="discord_channel",
                context_source="active_app",
                confidence=0.85,
                target_app="Discord",
            )

        # Everything else in Discord context → treat as server name
        return ResolvedEntity(
            original=entity,
            resolved_name=entity,
            entity_type="discord_server",
            context_source="active_app",
            confidence=0.75,
            target_app="Discord",
        )

    def _resolve_in_browser_context(
        self,
        entity: str,
        lower: str,
        browser_service: str,
    ) -> ResolvedEntity | None:
        """Resolve entity within browser context."""
        if browser_service == "youtube":
            # Anything in YouTube context → search query or video
            return ResolvedEntity(
                original=entity,
                resolved_name=entity,
                entity_type="youtube_query",
                context_source="active_app",
                confidence=0.8,
                target_app="Browser",
                metadata={"service": "youtube"},
            )

        if browser_service == "google":
            return ResolvedEntity(
                original=entity,
                resolved_name=entity,
                entity_type="search_query",
                context_source="active_app",
                confidence=0.8,
                target_app="Browser",
                metadata={"service": "google"},
            )

        return None

    def _resolve_as_application(self, entity: str, lower: str) -> ResolvedEntity | None:
        """Check if entity matches a known/installed application."""
        from futaba.intelligence.context_engine import APPLICATION_ALIASES

        if lower in APPLICATION_ALIASES:
            return ResolvedEntity(
                original=entity,
                resolved_name=entity.title(),
                entity_type="application",
                context_source="installed_apps",
                confidence=0.9,
                target_app=entity.title(),
            )

        # Check running processes
        try:
            for proc in _iter_processes_safe():
                pname = proc.lower().replace(".exe", "")
                if lower == pname or lower in pname:
                    return ResolvedEntity(
                        original=entity,
                        resolved_name=entity.title(),
                        entity_type="application",
                        context_source="running_process",
                        confidence=0.7,
                        target_app=entity.title(),
                    )
        except Exception:
            pass

        return None

    def _resolve_from_conversation(
        self,
        entity: str,
        lower: str,
        recent_turns: list[str],
    ) -> ResolvedEntity | None:
        """Look for entity in recent conversation turns for context."""
        # If a recent turn mentioned an app, the entity might belong to that app
        app_mentions = {
            "discord": "Discord",
            "youtube": "YouTube",
            "brave": "Brave",
            "chrome": "Chrome",
            "notepad": "Notepad",
            "spotify": "Spotify",
        }

        for turn in reversed(recent_turns):
            turn_lower = turn.lower()
            for keyword, app_name in app_mentions.items():
                if keyword in turn_lower:
                    return ResolvedEntity(
                        original=entity,
                        resolved_name=entity,
                        entity_type=f"{keyword}_item",
                        context_source="conversation",
                        confidence=0.5,
                        target_app=app_name,
                    )

        return None


def _iter_processes_safe() -> list[str]:
    """Safely iterate process names without raising."""
    names: list[str] = []
    try:
        for proc in psutil.process_iter(["name"]):
            try:
                n = proc.info.get("name")
                if n:
                    names.append(n)
            except Exception:
                pass
    except Exception:
        pass
    return names


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_resolver: EntityResolver | None = None


def get_entity_resolver() -> EntityResolver:
    """Get the global EntityResolver singleton."""
    global _resolver
    if _resolver is None:
        _resolver = EntityResolver()
    return _resolver
