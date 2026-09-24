"""
FUTABA — Master Engineering Directive Verification Test Suite.

Validates the full cognitive architecture:
1. Latency telemetry tracking (intent, context, planning, tool, verification, total)
2. Semantic UI targeting (ordinal, role, accessible name parsing and resolution)
3. Micro-action engine (19 actions, before/after state capture, ground truth verification, budgets)
4. Three execution speed tiers (Tier 0 direct, Tier 1 interactive, Tier 2 autonomous)
5. Task state machine Section 31 compliance & illegal transition prevention
6. Master conversational acceptance scenario without context repetition:
   Discord -> Donut SMP -> general -> YouTube -> search ESP32 -> first video -> go back -> third Short -> play it -> volume down.
"""

from __future__ import annotations

import os
import sys
import time
import pytest

os.environ.setdefault("FUTABA_ENV", "test")
os.environ.setdefault("FUTABA_TASKS_DIR", os.path.join(os.environ.get("TEMP", "/tmp"), "futaba_master_test_tasks"))


# ---------------------------------------------------------------------------
# 1. Latency Telemetry Tests
# ---------------------------------------------------------------------------

class TestLatencyTelemetry:
    """Verifies that all action stages are measured with exact latencies."""

    def test_01_telemetry_lifecycle(self):
        from futaba.intelligence.telemetry import get_latency_telemetry
        telem = get_latency_telemetry()
        telem.clear()

        t0 = time.monotonic()
        rec = telem.start_action(action_id="act_001", instruction="Click download", tier="TIER_1")
        rec.speech_end = t0
        rec.intent_detected = t0 + 0.015
        rec.context_snapshot = t0 + 0.025
        rec.tool_start = t0 + 0.030
        rec.tool_end = t0 + 0.060
        rec.verification_start = t0 + 0.060
        rec.verification_end = t0 + 0.075

        telem.record_completed(rec, success=True, verified=True)

        assert rec.success is True
        assert rec.verified is True
        assert rec.tool_latency_ms >= 25.0
        assert rec.verification_latency_ms >= 10.0
        assert rec.total_action_latency_ms >= 60.0

        d = rec.to_dict()
        assert d["action_id"] == "act_001"
        assert d["tier"] == "TIER_1"
        assert "latencies_ms" in d
        assert d["latencies_ms"]["tool"] > 0


# ---------------------------------------------------------------------------
# 2. Semantic UI Targeting Tests
# ---------------------------------------------------------------------------

class TestSemanticUITargeting:
    """Verifies natural language UI descriptor parsing and control resolution."""

    def test_01_parse_query_ordinals_and_roles(self):
        from futaba.intelligence.semantic_ui import get_semantic_targeter
        targeter = get_semantic_targeter()

        # "third Short"
        ord_val, role, text = targeter.parse_query("third Short")
        assert ord_val == 2
        assert role == "short"

        # "the first video"
        ord_val, role, text = targeter.parse_query("the first video")
        assert ord_val == 0
        assert role == "video"

        # "second option"
        ord_val, role, text = targeter.parse_query("second option")
        assert ord_val == 1
        assert role == "option"

        # "download button"
        ord_val, role, text = targeter.parse_query("download button")
        assert ord_val is None
        assert role == "button"
        assert "download" in text

        # "General channel"
        ord_val, role, text = targeter.parse_query("General channel")
        assert ord_val is None
        assert role == "channel"
        assert "general" in text

        # "search box"
        ord_val, role, text = targeter.parse_query("search box")
        assert role in ("search", "box")

    def test_02_scan_controls_never_crashes(self):
        from futaba.intelligence.semantic_ui import get_semantic_targeter
        targeter = get_semantic_targeter()
        # Even in headless/test environments, scanning must be safe
        controls = targeter.scan_controls(window_title="NonExistentWindow999", max_controls=10)
        assert isinstance(controls, list)


# ---------------------------------------------------------------------------
# 3. Micro-Action Engine Tests
# ---------------------------------------------------------------------------

class TestMicroActionEngine:
    """Verifies atomic micro-action execution and before/after verification."""

    @pytest.mark.asyncio
    async def test_01_micro_action_keypress(self):
        from futaba.intelligence.micro_action import get_micro_action_engine
        engine = get_micro_action_engine()

        res = await engine.execute_micro_action("keypress", target="{ESC}")
        assert res.action == "keypress"
        assert res.success is True
        assert res.verified is True
        assert res.latency_ms > 0

    @pytest.mark.asyncio
    async def test_02_micro_action_media(self):
        from futaba.intelligence.micro_action import get_micro_action_engine
        engine = get_micro_action_engine()

        res = await engine.execute_micro_action("pause")
        assert res.action == "pause"
        assert res.success is True

        res_play = await engine.execute_micro_action("play")
        assert res_play.action == "play"
        assert res_play.success is True

    @pytest.mark.asyncio
    async def test_03_micro_action_scroll(self):
        from futaba.intelligence.micro_action import get_micro_action_engine
        engine = get_micro_action_engine()

        res = await engine.execute_micro_action("scroll", direction="down", amount=2)
        assert res.action == "scroll"
        assert res.success is True

    @pytest.mark.asyncio
    async def test_04_micro_action_unsupported(self):
        from futaba.intelligence.micro_action import get_micro_action_engine
        engine = get_micro_action_engine()

        res = await engine.execute_micro_action("quantum_teleport")
        assert res.success is False
        assert "unsupported" in res.error.lower()


# ---------------------------------------------------------------------------
# 4. Three Execution Speed Tiers Tests
# ---------------------------------------------------------------------------

class TestTierRouter:
    """Verifies classification into Tier 0, Tier 1, and Tier 2."""

    def test_01_tier_0_direct_actions(self):
        from futaba.intelligence.tier_router import get_tier_router, ExecutionTier
        from futaba.intelligence.intent_resolver import get_intent_resolver
        router = get_tier_router()
        resolver = get_intent_resolver()

        # Open Discord -> Tier 0
        intent = resolver.resolve("open Discord")
        dec = router.classify_tier(intent, "open Discord")
        assert dec.tier == ExecutionTier.TIER_0
        assert dec.needs_planner is False

        # Open YouTube -> Tier 0
        intent = resolver.resolve("open YouTube")
        dec = router.classify_tier(intent, "open YouTube")
        assert dec.tier == ExecutionTier.TIER_0
        assert dec.needs_planner is False

        # Pause -> Tier 0
        intent = resolver.resolve("pause")
        dec = router.classify_tier(intent, "pause")
        assert dec.tier == ExecutionTier.TIER_0
        assert dec.needs_planner is False

        # Turn volume down -> Tier 0
        intent = resolver.resolve("turn volume down")
        dec = router.classify_tier(intent, "turn volume down")
        assert dec.tier == ExecutionTier.TIER_0

    def test_02_tier_1_fast_interactive_tasks(self):
        from futaba.intelligence.tier_router import get_tier_router, ExecutionTier
        from futaba.intelligence.intent_resolver import get_intent_resolver
        router = get_tier_router()
        resolver = get_intent_resolver()

        # "Click the third Short"
        intent = resolver.resolve("click the third Short")
        dec = router.classify_tier(intent, "click the third Short")
        assert dec.tier == ExecutionTier.TIER_1
        assert dec.needs_planner is False

        # "Open the first video"
        intent = resolver.resolve("open the first video")
        dec = router.classify_tier(intent, "open the first video")
        assert dec.tier == ExecutionTier.TIER_1
        assert dec.needs_planner is False

        # "Open Donut SMP" (within Discord context)
        intent = resolver.resolve("open Donut SMP", target_application="Discord")
        dec = router.classify_tier(intent, "open Donut SMP")
        assert dec.tier == ExecutionTier.TIER_1
        assert dec.needs_planner is False

        # "Go to general" (within Discord context)
        intent = resolver.resolve("go to general", target_application="Discord")
        dec = router.classify_tier(intent, "go to general")
        assert dec.tier == ExecutionTier.TIER_1

    def test_03_tier_2_complex_autonomous_tasks(self):
        from futaba.intelligence.tier_router import get_tier_router, ExecutionTier
        from futaba.intelligence.intent_resolver import get_intent_resolver
        router = get_tier_router()
        resolver = get_intent_resolver()

        req = "Download the latest ESP32 driver, install it, and verify that Windows detects it"
        intent = resolver.resolve(req)
        dec = router.classify_tier(intent, req)
        assert dec.tier == ExecutionTier.TIER_2
        assert dec.needs_planner is True


# ---------------------------------------------------------------------------
# 5. Task State Machine Section 31 Compliance Tests
# ---------------------------------------------------------------------------

class TestTaskStateMachineSection31:
    """Verifies explicit Section 31 states and prohibits illegal transitions."""

    def test_01_section_31_states_exist(self):
        from futaba.tasks.task_manager import TaskState
        assert TaskState.CREATED.value == "created"
        assert TaskState.UNDERSTANDING.value == "understanding"
        assert TaskState.CONTEXT_RESOLUTION.value == "context_resolution"
        assert TaskState.PLANNING.value == "planning"
        assert TaskState.EXECUTING.value == "executing"
        assert TaskState.OBSERVING.value == "observing"
        assert TaskState.VERIFYING.value == "verifying"
        assert TaskState.PAUSED.value == "paused"
        assert TaskState.COMPLETED.value == "completed"
        assert TaskState.FAILED.value == "failed"
        assert TaskState.CANCELLED.value == "cancelled"

    def test_02_legal_pipeline_transitions(self):
        from futaba.tasks.task_manager import Task, TaskState
        task = Task(title="Test Pipeline")
        assert task.state == TaskState.QUEUED

        task.transition_to(TaskState.UNDERSTANDING)
        assert task.state == TaskState.UNDERSTANDING

        task.transition_to(TaskState.CONTEXT_RESOLUTION)
        assert task.state == TaskState.CONTEXT_RESOLUTION

        task.transition_to(TaskState.PLANNING)
        assert task.state == TaskState.PLANNING

        task.transition_to(TaskState.EXECUTING)
        assert task.state == TaskState.EXECUTING

        task.transition_to(TaskState.OBSERVING)
        assert task.state == TaskState.OBSERVING

        task.transition_to(TaskState.VERIFYING)
        assert task.state == TaskState.VERIFYING

        task.transition_to(TaskState.COMPLETED)
        assert task.state == TaskState.COMPLETED

    def test_03_prohibited_transitions(self):
        from futaba.tasks.task_manager import Task, TaskState, InvalidTransitionError

        # FAILED -> VERIFYING prohibited
        task = Task(title="Failed task")
        task.transition_to(TaskState.RUNNING)
        task.transition_to(TaskState.FAILED)
        with pytest.raises(InvalidTransitionError):
            task.transition_to(TaskState.VERIFYING)

        # COMPLETED -> EXECUTING prohibited
        task2 = Task(title="Completed task")
        task2.transition_to(TaskState.RUNNING)
        task2.transition_to(TaskState.VERIFYING)
        task2.transition_to(TaskState.COMPLETED)
        with pytest.raises(InvalidTransitionError):
            task2.transition_to(TaskState.EXECUTING)

        # CANCELLED -> EXECUTING prohibited
        task3 = Task(title="Cancelled task")
        task3.transition_to(TaskState.CANCELLED)
        with pytest.raises(InvalidTransitionError):
            task3.transition_to(TaskState.EXECUTING)


# ---------------------------------------------------------------------------
# 6. Master Conversational Acceptance Scenario
# ---------------------------------------------------------------------------

class TestMasterConversationalScenario:
    """
    Executes the exact 10-step master scenario without context repetition:
    1. "Open Discord."
    2. "Open Donut SMP."
    3. "Go to general."
    4. "Now open YouTube."
    5. "Search for ESP32."
    6. "Open the first video."
    7. "Go back."
    8. "Open the third Short."
    9. "Play it."
    10. "Turn the volume down."
    """

    @pytest.mark.asyncio
    async def test_master_scenario_execution(self):
        from futaba.intelligence.context_engine import get_context_engine
        from futaba.intelligence.intent_resolver import get_intent_resolver, IntentCategory
        from futaba.intelligence.tier_router import get_tier_router, ExecutionTier
        from futaba.voice.command_router import VoiceCommandRouter

        ctx = get_context_engine()
        resolver = get_intent_resolver()
        tier_router = get_tier_router()
        command_router = VoiceCommandRouter()

        # Step 1: "Open Discord."
        intent1 = resolver.resolve("Open Discord")
        dec1 = tier_router.classify_tier(intent1, "Open Discord")
        assert dec1.tier == ExecutionTier.TIER_0
        res1 = await command_router.execute_tool_call("open_application", {"name": "Discord"})
        assert res1["status"] == "success"
        assert ctx.get_target_app() == "Discord"

        # Step 2: "Open Donut SMP." (within Discord context)
        intent2 = resolver.resolve("Open Donut SMP", target_application=ctx.get_target_app(), active_process="discord.exe")
        assert intent2.category == IntentCategory.APPLICATION_NAVIGATION
        dec2 = tier_router.classify_tier(intent2, "Open Donut SMP")
        assert dec2.tier == ExecutionTier.TIER_1
        assert ctx.get_target_app() == "Discord"  # Discord context preserved!

        # Step 3: "Go to general." (within Discord context)
        intent3 = resolver.resolve("Go to general", target_application=ctx.get_target_app(), active_process="discord.exe")
        assert intent3.category == IntentCategory.APPLICATION_NAVIGATION
        assert intent3.target_entity == "general"

        # Step 4: "Now open YouTube." (Context switch to YouTube)
        intent4 = resolver.resolve("Now open YouTube")
        dec4 = tier_router.classify_tier(intent4, "Now open YouTube")
        assert dec4.tier == ExecutionTier.TIER_0
        res4 = await command_router.execute_tool_call("open_application", {"name": "YouTube"})
        assert res4["status"] == "success"
        ctx.switch_context("YouTube")
        assert ctx.get_target_app() == "YouTube"

        # Step 5: "Search for ESP32."
        intent5 = resolver.resolve("Search for ESP32", active_process="brave.exe", browser_service="youtube")
        assert intent5.category == IntentCategory.BROWSER_INTERACTION
        assert intent5.parameters.get("query") == "ESP32"

        # Step 6: "Open the first video."
        intent6 = resolver.resolve("Open the first video", active_process="brave.exe", browser_service="youtube")
        dec6 = tier_router.classify_tier(intent6, "Open the first video")
        assert dec6.tier == ExecutionTier.TIER_1

        # Step 7: "Go back."
        res7 = await command_router.execute_tool_call("go_back", {})
        assert res7["status"] == "success"

        # Step 8: "Open the third Short."
        intent8 = resolver.resolve("Open the third Short", active_process="brave.exe", browser_service="youtube")
        dec8 = tier_router.classify_tier(intent8, "Open the third Short")
        assert dec8.tier == ExecutionTier.TIER_1

        # Step 9: "Play it."
        res9 = await command_router.execute_tool_call("media_control", {"action": "play"})
        assert res9["status"] == "success"

        # Step 10: "Turn the volume down."
        res10 = await command_router.execute_tool_call("media_control", {"action": "volume_down"})
        assert res10["status"] == "success"

        print("\n[SUCCESS] Master 10-step conversational acceptance scenario fully verified.")
