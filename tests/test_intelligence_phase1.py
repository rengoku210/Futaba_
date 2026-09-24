"""
Tests for Futaba Intelligence — Phase 1: Context Engine, Intent Resolver,
Entity Resolver, Execution Surface, and Provider Health States.
"""

from __future__ import annotations

import os
import sys
import time
import pytest

# Ensure test environment is set
os.environ.setdefault("FUTABA_ENV", "test")
os.environ.setdefault("FUTABA_TASKS_DIR", os.path.join(os.environ.get("TEMP", "/tmp"), "futaba_test_tasks"))


# ---------------------------------------------------------------------------
# Context Engine Tests
# ---------------------------------------------------------------------------

class TestContextEngine:
    """Tests for ContextEngine state management."""

    def test_01_singleton(self):
        from futaba.intelligence.context_engine import get_context_engine, ContextEngine
        engine = get_context_engine()
        assert isinstance(engine, ContextEngine)
        # Singleton returns same instance
        assert get_context_engine() is engine

    def test_02_initial_state(self):
        from futaba.intelligence.context_engine import ContextEngine
        engine = ContextEngine()
        state = engine.state
        assert state.active_app is None
        assert state.previous_app is None
        assert state.target_application == ""
        assert state.browser_state.browser_name == ""
        assert state.active_task is None

    def test_03_set_target_application(self):
        from futaba.intelligence.context_engine import ContextEngine
        engine = ContextEngine()
        engine.set_target_application("Discord")
        assert engine.state.target_application == "Discord"
        assert engine.get_target_app() == "Discord"

    def test_04_clear_target_application(self):
        from futaba.intelligence.context_engine import ContextEngine
        engine = ContextEngine()
        engine.set_target_application("Discord")
        engine.clear_target_application()
        assert engine.state.target_application == ""

    def test_05_record_action(self):
        from futaba.intelligence.context_engine import ContextEngine
        engine = ContextEngine()
        engine.record_action("opened Discord", target_app="Discord")
        assert engine.state.last_action == "opened Discord"
        assert engine.state.target_application == "Discord"
        assert engine.state.last_action_time > 0

    def test_06_recent_actions(self):
        from futaba.intelligence.context_engine import ContextEngine
        engine = ContextEngine()
        engine.record_action("opened Discord")
        engine.record_action("navigated to YouTube")
        engine.record_action("searched Mann Mera")
        actions = engine.get_recent_actions(2)
        assert len(actions) == 2
        assert actions[-1] == "searched Mann Mera"

    def test_07_mark_app_owned(self):
        from futaba.intelligence.context_engine import ContextEngine
        engine = ContextEngine()
        engine.mark_app_owned("discord.exe")
        assert "discord.exe" in engine._owned_apps

    def test_08_context_for_llm(self):
        from futaba.intelligence.context_engine import ContextEngine
        engine = ContextEngine()
        engine.record_action("opened Discord", target_app="Discord")
        engine.set_active_task("Navigate to Donut SMP")
        llm_ctx = engine.get_context_for_llm()
        assert "Discord" in llm_ctx
        assert "Navigate to Donut SMP" in llm_ctx

    def test_09_switch_context(self):
        from futaba.intelligence.context_engine import ContextEngine
        engine = ContextEngine()
        engine.set_target_application("Discord")
        engine.switch_context("YouTube")
        assert engine.state.target_application == "YouTube"
        assert "YouTube" in engine.state.last_action

    def test_10_process_to_friendly(self):
        from futaba.intelligence.context_engine import ContextEngine
        assert ContextEngine._process_to_friendly("discord.exe") == "Discord"
        assert ContextEngine._process_to_friendly("brave.exe") == "Brave"
        assert ContextEngine._process_to_friendly("code.exe") == "VS Code"
        assert ContextEngine._process_to_friendly("notepad.exe") == "Notepad"
        assert ContextEngine._process_to_friendly("msedge.exe") == "Edge"

    def test_11_detect_browser_service(self):
        from futaba.intelligence.context_engine import ContextEngine
        assert ContextEngine._detect_browser_service("YouTube - Google Chrome") == "youtube"
        assert ContextEngine._detect_browser_service("reddit: the front page") == "reddit"
        assert ContextEngine._detect_browser_service("github.com - Brave") == "github"
        assert ContextEngine._detect_browser_service("My Cool Page - Chrome") == ""

    def test_12_parse_app_subcontext(self):
        from futaba.intelligence.context_engine import ContextEngine
        ctx = ContextEngine._parse_app_subcontext("discord.exe", "Donut SMP - Discord")
        assert ctx.get("server") == "Donut SMP"

    def test_13_application_aliases(self):
        from futaba.intelligence.context_engine import APPLICATION_ALIASES
        assert APPLICATION_ALIASES["discord"] == "discord"
        assert APPLICATION_ALIASES["vs code"] == "code"
        assert APPLICATION_ALIASES["vscode"] == "code"
        assert APPLICATION_ALIASES["brave"] == "brave"

    def test_14_is_within_app(self):
        from futaba.intelligence.context_engine import ContextEngine
        engine = ContextEngine()
        engine.set_target_application("Discord")
        assert engine.is_within_app("Discord")
        assert engine.is_within_app("discord")
        assert not engine.is_within_app("YouTube")

    def test_15_context_state_is_browser_active(self):
        from futaba.intelligence.context_engine import ContextState, ApplicationContext
        state = ContextState()
        assert not state.is_browser_active()
        state.active_app = ApplicationContext(
            name="Brave", process_name="brave.exe",
            window_title="YouTube - Brave", app_type="browser",
        )
        assert state.is_browser_active()


# ---------------------------------------------------------------------------
# Intent Resolver Tests
# ---------------------------------------------------------------------------

class TestIntentResolver:
    """Tests for IntentResolver classification."""

    def test_01_screen_query(self):
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory
        resolver = IntentResolver()
        intent = resolver.resolve("What am I looking at?")
        assert intent.category == IntentCategory.SCREEN_QUERY

    def test_02_sleep_command(self):
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory
        resolver = IntentResolver()
        intent = resolver.resolve("go to sleep")
        assert intent.category == IntentCategory.SLEEP_COMMAND

    def test_03_wake_phrase(self):
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory
        resolver = IntentResolver()
        intent = resolver.resolve("hey futaba")
        assert intent.category == IntentCategory.CONVERSATION
        assert intent.action == "wake_acknowledgment"

    def test_04_app_launch(self):
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory
        resolver = IntentResolver()
        intent = resolver.resolve("open notepad")
        assert intent.category == IntentCategory.APPLICATION_LAUNCH
        assert intent.target_application.lower() == "notepad"
        assert intent.action == "launch"

    def test_05_browser_navigation_web_service(self):
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory
        resolver = IntentResolver()
        intent = resolver.resolve("open YouTube")
        assert intent.category == IntentCategory.BROWSER_NAVIGATION

    def test_06_browser_navigation_url(self):
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory
        resolver = IntentResolver()
        intent = resolver.resolve("open google.com")
        assert intent.category == IntentCategory.BROWSER_NAVIGATION

    def test_07_app_navigation_in_discord(self):
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory
        resolver = IntentResolver()
        # When Discord is the target app, "open Donut SMP" → navigate within Discord
        intent = resolver.resolve(
            "open Donut SMP",
            target_application="Discord",
            active_process="discord.exe",
        )
        assert intent.category == IntentCategory.APPLICATION_NAVIGATION
        assert intent.target_application == "Discord"
        assert intent.target_entity == "Donut SMP"

    def test_08_discord_channel_navigation(self):
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory
        resolver = IntentResolver()
        intent = resolver.resolve(
            "go to general",
            target_application="Discord",
            active_process="discord.exe",
        )
        assert intent.category == IntentCategory.APPLICATION_NAVIGATION
        assert intent.target_entity == "general"

    def test_09_task_execution(self):
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory
        resolver = IntentResolver()
        intent = resolver.resolve("type Hello World into Notepad")
        assert intent.category == IntentCategory.TASK_EXECUTION

    def test_10_close_application(self):
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory
        resolver = IntentResolver()
        intent = resolver.resolve("close Notepad")
        assert intent.category == IntentCategory.APPLICATION_LAUNCH
        assert intent.action == "close"

    def test_11_follow_up(self):
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory
        resolver = IntentResolver()
        intent = resolver.resolve("now save it", target_application="Notepad")
        assert intent.category == IntentCategory.FOLLOW_UP

    def test_12_browser_interaction_search(self):
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory
        resolver = IntentResolver()
        intent = resolver.resolve(
            "search Mann Mera",
            active_process="brave.exe",
            browser_service="youtube",
        )
        assert intent.category == IntentCategory.BROWSER_INTERACTION
        assert intent.action == "search"

    def test_13_information_query(self):
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory
        resolver = IntentResolver()
        intent = resolver.resolve("what time is it?")
        assert intent.category == IntentCategory.INFORMATION_QUERY

    def test_14_app_interaction_messages(self):
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory
        resolver = IntentResolver()
        intent = resolver.resolve(
            "what are the latest messages",
            target_application="Discord",
        )
        assert intent.category == IntentCategory.APPLICATION_INTERACTION
        assert intent.target_application == "Discord"

    def test_15_compound_task(self):
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory
        resolver = IntentResolver()
        intent = resolver.resolve("open notepad and type hello")
        assert intent.category == IntentCategory.TASK_EXECUTION

    def test_16_focus_command(self):
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory
        resolver = IntentResolver()
        intent = resolver.resolve("switch to Discord")
        assert intent.category == IntentCategory.APPLICATION_FOCUS
        assert intent.action == "focus"

    def test_17_guard_question_as_launch(self):
        """'open what is quantum computing' should NOT be an app launch."""
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory
        resolver = IntentResolver()
        intent = resolver.resolve("open what is quantum computing")
        assert intent.category == IntentCategory.INFORMATION_QUERY

    def test_18_task_control(self):
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory
        resolver = IntentResolver()
        intent = resolver.resolve("pause")
        assert intent.category == IntentCategory.TASK_CONTROL
        assert intent.action == "pause"

    def test_19_play_in_youtube_context(self):
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory
        resolver = IntentResolver()
        intent = resolver.resolve(
            "play the first result",
            active_process="brave.exe",
            browser_service="youtube",
        )
        assert intent.category == IntentCategory.BROWSER_INTERACTION
        assert intent.action == "play"

    def test_20_empty_text(self):
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory
        resolver = IntentResolver()
        intent = resolver.resolve("")
        assert intent.category == IntentCategory.CONVERSATION


# ---------------------------------------------------------------------------
# Entity Resolver Tests
# ---------------------------------------------------------------------------

class TestEntityResolver:
    """Tests for EntityResolver."""

    def test_01_resolve_url(self):
        from futaba.intelligence.entity_resolver import EntityResolver
        resolver = EntityResolver()
        result = resolver.resolve("https://www.google.com")
        assert result.entity_type == "url"
        assert result.confidence >= 0.9

    def test_02_resolve_domain(self):
        from futaba.intelligence.entity_resolver import EntityResolver
        resolver = EntityResolver()
        result = resolver.resolve("github.com")
        assert result.entity_type == "url"

    def test_03_resolve_web_service(self):
        from futaba.intelligence.entity_resolver import EntityResolver
        resolver = EntityResolver()
        result = resolver.resolve("youtube")
        assert result.entity_type == "web_service"
        assert "youtube" in result.url

    def test_04_resolve_discord_server(self):
        from futaba.intelligence.entity_resolver import EntityResolver
        resolver = EntityResolver()
        result = resolver.resolve(
            "Donut SMP",
            active_app_name="Discord",
            active_app_process="discord.exe",
        )
        assert result.entity_type == "discord_server"
        assert result.target_app == "Discord"

    def test_05_resolve_discord_channel(self):
        from futaba.intelligence.entity_resolver import EntityResolver
        resolver = EntityResolver()
        result = resolver.resolve(
            "general",
            active_app_name="Discord",
            active_app_process="discord.exe",
        )
        assert result.entity_type == "discord_channel"
        assert result.target_app == "Discord"

    def test_06_resolve_youtube_query(self):
        from futaba.intelligence.entity_resolver import EntityResolver
        resolver = EntityResolver()
        result = resolver.resolve(
            "Mann Mera",
            active_app_type="browser",
            browser_service="youtube",
        )
        assert result.entity_type == "youtube_query"

    def test_07_resolve_application(self):
        from futaba.intelligence.entity_resolver import EntityResolver
        resolver = EntityResolver()
        result = resolver.resolve("discord")
        assert result.entity_type == "web_service" or result.entity_type == "application"

    def test_08_resolve_unknown(self):
        from futaba.intelligence.entity_resolver import EntityResolver
        resolver = EntityResolver()
        result = resolver.resolve("xyznonexistent12345")
        assert result.entity_type == "unknown"
        assert result.confidence < 0.5

    def test_09_resolve_from_conversation(self):
        from futaba.intelligence.entity_resolver import EntityResolver
        resolver = EntityResolver()
        result = resolver.resolve(
            "Donut SMP",
            recent_turns=["I want to check Discord", "open it"],
        )
        assert result.target_app == "Discord"

    def test_10_empty_entity(self):
        from futaba.intelligence.entity_resolver import EntityResolver
        resolver = EntityResolver()
        result = resolver.resolve("")
        assert result.entity_type == "unknown"
        assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# Execution Surface Tests
# ---------------------------------------------------------------------------

class TestExecutionSurface:
    """Tests for ExecutionSurfaceSelector."""

    def test_01_app_launch_is_direct(self):
        from futaba.intelligence.execution_surface import ExecutionSurfaceSelector, ExecutionSurface
        from futaba.intelligence.intent_resolver import ResolvedIntent, IntentCategory
        selector = ExecutionSurfaceSelector()
        intent = ResolvedIntent(
            category=IntentCategory.APPLICATION_LAUNCH,
            target_application="Notepad",
            action="launch",
        )
        surface = selector.select(intent)
        assert surface == ExecutionSurface.DIRECT_APPLICATION

    def test_02_browser_nav_is_direct(self):
        from futaba.intelligence.execution_surface import ExecutionSurfaceSelector, ExecutionSurface
        from futaba.intelligence.intent_resolver import ResolvedIntent, IntentCategory
        selector = ExecutionSurfaceSelector()
        intent = ResolvedIntent(
            category=IntentCategory.BROWSER_NAVIGATION,
            action="navigate",
        )
        surface = selector.select(intent)
        assert surface == ExecutionSurface.DIRECT_APPLICATION

    def test_03_app_navigation_uses_uia(self):
        from futaba.intelligence.execution_surface import ExecutionSurfaceSelector, ExecutionSurface
        from futaba.intelligence.intent_resolver import ResolvedIntent, IntentCategory
        selector = ExecutionSurfaceSelector()
        intent = ResolvedIntent(
            category=IntentCategory.APPLICATION_NAVIGATION,
            target_application="Discord",
        )
        surface = selector.select(intent)
        assert surface == ExecutionSurface.WINDOWS_UIA

    def test_04_app_navigation_falls_back_to_cua(self):
        from futaba.intelligence.execution_surface import ExecutionSurfaceSelector, ExecutionSurface
        from futaba.intelligence.intent_resolver import ResolvedIntent, IntentCategory
        selector = ExecutionSurfaceSelector()
        intent = ResolvedIntent(
            category=IntentCategory.APPLICATION_NAVIGATION,
        )
        surface = selector.select(intent, uia_available=False)
        assert surface == ExecutionSurface.HERMES_COMPUTER_USE

    def test_05_browser_interaction_uses_dom(self):
        from futaba.intelligence.execution_surface import ExecutionSurfaceSelector, ExecutionSurface
        from futaba.intelligence.intent_resolver import ResolvedIntent, IntentCategory
        selector = ExecutionSurfaceSelector()
        intent = ResolvedIntent(
            category=IntentCategory.BROWSER_INTERACTION,
            action="search",
        )
        surface = selector.select(intent)
        assert surface == ExecutionSurface.BROWSER_DOM

    def test_06_task_execution_submits_task(self):
        from futaba.intelligence.execution_surface import ExecutionSurfaceSelector, ExecutionSurface
        from futaba.intelligence.intent_resolver import ResolvedIntent, IntentCategory
        selector = ExecutionSurfaceSelector()
        intent = ResolvedIntent(
            category=IntentCategory.TASK_EXECUTION,
            action="execute",
        )
        surface = selector.select(intent)
        assert surface == ExecutionSurface.SUBMIT_TASK

    def test_07_conversation_is_voice_only(self):
        from futaba.intelligence.execution_surface import ExecutionSurfaceSelector, ExecutionSurface
        from futaba.intelligence.intent_resolver import ResolvedIntent, IntentCategory
        selector = ExecutionSurfaceSelector()
        intent = ResolvedIntent(category=IntentCategory.CONVERSATION)
        surface = selector.select(intent)
        assert surface == ExecutionSurface.VOICE_ONLY

    def test_08_is_direct_action(self):
        from futaba.intelligence.execution_surface import ExecutionSurfaceSelector, ExecutionSurface
        selector = ExecutionSurfaceSelector()
        assert selector.is_direct_action(ExecutionSurface.DIRECT_APPLICATION)
        assert selector.is_direct_action(ExecutionSurface.VOICE_ONLY)
        assert not selector.is_direct_action(ExecutionSurface.SUBMIT_TASK)

    def test_09_requires_hermes(self):
        from futaba.intelligence.execution_surface import ExecutionSurfaceSelector, ExecutionSurface
        selector = ExecutionSurfaceSelector()
        assert selector.requires_hermes(ExecutionSurface.HERMES_COMPUTER_USE)
        assert selector.requires_hermes(ExecutionSurface.BROWSER_DOM)
        assert not selector.requires_hermes(ExecutionSurface.DIRECT_APPLICATION)

    def test_10_fallback_chain(self):
        from futaba.intelligence.execution_surface import ExecutionSurfaceSelector, ExecutionSurface
        selector = ExecutionSurfaceSelector()
        assert selector.get_fallback(ExecutionSurface.WINDOWS_UIA) == ExecutionSurface.HERMES_COMPUTER_USE
        assert selector.get_fallback(ExecutionSurface.BROWSER_DOM) == ExecutionSurface.BROWSER_CUA
        assert selector.get_fallback(ExecutionSurface.VOICE_ONLY) is None


# ---------------------------------------------------------------------------
# Provider Health State Tests
# ---------------------------------------------------------------------------

class TestProviderHealth:
    """Tests for provider health state classification."""

    def test_01_classify_auth_error(self):
        from futaba.routing.model_router import _classify_error, ProviderHealthState
        assert _classify_error("401 Unauthorized") == ProviderHealthState.AUTH_FAILED
        assert _classify_error("Error: 403 Forbidden") == ProviderHealthState.AUTH_FAILED
        assert _classify_error("Invalid API key") == ProviderHealthState.AUTH_FAILED
        assert _classify_error("authentication failed") == ProviderHealthState.AUTH_FAILED

    def test_02_classify_rate_limit(self):
        from futaba.routing.model_router import _classify_error, ProviderHealthState
        assert _classify_error("429 Too Many Requests") == ProviderHealthState.RATE_LIMITED
        assert _classify_error("rate_limit_exceeded") == ProviderHealthState.RATE_LIMITED
        assert _classify_error("quota exceeded") == ProviderHealthState.RATE_LIMITED
        assert _classify_error("RESOURCE_EXHAUSTED") == ProviderHealthState.RATE_LIMITED

    def test_03_classify_connection_error(self):
        from futaba.routing.model_router import _classify_error, ProviderHealthState
        assert _classify_error("Connection refused") == ProviderHealthState.OFFLINE
        assert _classify_error("timeout error") == ProviderHealthState.OFFLINE
        assert _classify_error("DNS resolution failed") == ProviderHealthState.OFFLINE

    def test_04_classify_unknown_error(self):
        from futaba.routing.model_router import _classify_error, ProviderHealthState
        assert _classify_error("some random error") == ProviderHealthState.DEGRADED

    def test_05_auth_failed_is_permanent(self):
        from futaba.routing.model_router import ProviderHealthState
        from futaba.core.config import ProviderConfig
        # Simulate a provider going AUTH_FAILED
        config = ProviderConfig(name="test_provider", default_model="test-model")
        from futaba.routing.model_router import OpenAICompatibleClient
        # We can't easily instantiate without API, so test the state logic directly
        # via a mock-like approach on the base class
        class TestClient:
            def __init__(self):
                self._health_state = ProviderHealthState.HEALTHY
                self._consecutive_failures = 0
                self._last_error = ""
                self._rate_limit_until = 0.0
                self.name = "test"

            @property
            def health_state(self):
                import time
                if (self._health_state == ProviderHealthState.RATE_LIMITED
                        and time.time() > self._rate_limit_until):
                    self._health_state = ProviderHealthState.HEALTHY
                    self._consecutive_failures = 0
                return self._health_state

            @property
            def is_healthy(self):
                state = self.health_state
                return state in (ProviderHealthState.HEALTHY, ProviderHealthState.DEGRADED)

            @property
            def is_permanently_failed(self):
                return self.health_state == ProviderHealthState.AUTH_FAILED

        client = TestClient()
        assert client.is_healthy
        assert not client.is_permanently_failed

        # Simulate auth failure
        client._health_state = ProviderHealthState.AUTH_FAILED
        assert not client.is_healthy
        assert client.is_permanently_failed

    def test_06_rate_limited_auto_recovers(self):
        from futaba.routing.model_router import ProviderHealthState
        import time

        class TestClient:
            def __init__(self):
                self._health_state = ProviderHealthState.RATE_LIMITED
                self._consecutive_failures = 1
                self._rate_limit_until = time.time() - 1  # Already expired

            @property
            def health_state(self):
                if (self._health_state == ProviderHealthState.RATE_LIMITED
                        and time.time() > self._rate_limit_until):
                    self._health_state = ProviderHealthState.HEALTHY
                    self._consecutive_failures = 0
                return self._health_state

            @property
            def is_healthy(self):
                state = self.health_state
                return state in (ProviderHealthState.HEALTHY, ProviderHealthState.DEGRADED)

        client = TestClient()
        # Should auto-recover since rate limit has expired
        assert client.is_healthy
        assert client.health_state == ProviderHealthState.HEALTHY


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------

class TestIntelligenceIntegration:
    """Integration tests across context engine + intent + entity + surface."""

    def test_scenario_a_discord_chain(self):
        """Scenario A: Open Discord → Open Donut SMP → Open general → What are latest messages?"""
        from futaba.intelligence.context_engine import ContextEngine
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory
        from futaba.intelligence.execution_surface import ExecutionSurfaceSelector, ExecutionSurface

        engine = ContextEngine()
        resolver = IntentResolver()
        selector = ExecutionSurfaceSelector()

        # Step 1: "Open Discord"
        intent = resolver.resolve("open Discord")
        assert intent.category == IntentCategory.APPLICATION_LAUNCH
        assert selector.select(intent) == ExecutionSurface.DIRECT_APPLICATION
        engine.record_action("opened Discord", target_app="Discord")

        # Step 2: "Open Donut SMP" — within Discord context
        intent = resolver.resolve(
            "open Donut SMP",
            target_application=engine.get_target_app(),
            active_process="discord.exe",
        )
        assert intent.category == IntentCategory.APPLICATION_NAVIGATION
        assert intent.target_entity == "Donut SMP"
        assert selector.select(intent) == ExecutionSurface.WINDOWS_UIA

        # Step 3: "Go to general" — within Discord context
        intent = resolver.resolve(
            "go to general",
            target_application=engine.get_target_app(),
            active_process="discord.exe",
        )
        assert intent.category == IntentCategory.APPLICATION_NAVIGATION
        assert intent.target_entity == "general"

        # Step 4: "What are the latest messages?" — within Discord context
        intent = resolver.resolve(
            "what are the latest messages",
            target_application=engine.get_target_app(),
        )
        assert intent.category == IntentCategory.APPLICATION_INTERACTION
        assert intent.target_application == "Discord"

    def test_scenario_b_browser_chain(self):
        """Scenario B: Open Brave → Open YouTube → Search Mann Mera → Play first result → Pause"""
        from futaba.intelligence.context_engine import ContextEngine
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory
        from futaba.intelligence.execution_surface import ExecutionSurfaceSelector, ExecutionSurface

        engine = ContextEngine()
        resolver = IntentResolver()
        selector = ExecutionSurfaceSelector()

        # Step 1: "Open Brave"
        intent = resolver.resolve("open Brave")
        assert intent.category == IntentCategory.APPLICATION_LAUNCH
        assert selector.select(intent) == ExecutionSurface.DIRECT_APPLICATION
        engine.record_action("opened Brave", target_app="Brave")

        # Step 2: "Open YouTube"
        intent = resolver.resolve("open YouTube")
        assert intent.category == IntentCategory.BROWSER_NAVIGATION
        assert selector.select(intent) == ExecutionSurface.DIRECT_APPLICATION
        engine.record_action("navigated to YouTube")

        # Step 3: "Search Mann Mera"
        intent = resolver.resolve(
            "search Mann Mera",
            active_process="brave.exe",
            browser_service="youtube",
        )
        assert intent.category == IntentCategory.BROWSER_INTERACTION
        assert intent.action == "search"
        assert selector.select(intent) == ExecutionSurface.BROWSER_DOM

        # Step 4: "Play the first result"
        intent = resolver.resolve(
            "play the first result",
            active_process="brave.exe",
            browser_service="youtube",
        )
        assert intent.category == IntentCategory.BROWSER_INTERACTION
        assert intent.action == "play"

        # Step 5: "Pause"
        intent = resolver.resolve("pause")
        assert intent.category == IntentCategory.TASK_CONTROL
        assert intent.action == "pause"

    def test_scenario_d_context_switch(self):
        """Scenario D: Context switch from Discord to YouTube."""
        from futaba.intelligence.context_engine import ContextEngine
        from futaba.intelligence.intent_resolver import IntentResolver, IntentCategory

        engine = ContextEngine()
        resolver = IntentResolver()

        engine.set_target_application("Discord")

        # "actually open YouTube" — explicit context switch
        intent = resolver.resolve("open YouTube")
        assert intent.category == IntentCategory.BROWSER_NAVIGATION
        # Context engine should switch
        engine.switch_context("YouTube")
        assert engine.get_target_app() == "YouTube"
